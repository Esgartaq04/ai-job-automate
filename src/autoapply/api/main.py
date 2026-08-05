"""FastAPI service: dashboard counters, review queue, approve/reject, SSE run log.

SSE rather than WebSockets: logs are one-directional, SSE reconnects for free, and
it survives Cloud Run's proxy without extra config (README section 6).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import session_scope
from ..events import REVIEWABLE_STATES, IllegalTransition, log_event, start_run, transition
from ..models import Application, Artifact, Company, Job, Match, Run, RunEvent
from ..storage import ArtifactStore

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="AutoApply", version="0.1.0")


def require_token(authorization: str | None = Header(default=None)) -> None:
    """Single-user Phase 1 guard. Replace with real auth before multi-user."""
    expected = get_settings().api_token
    if not authorization or authorization.removeprefix("Bearer ").strip() != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text())


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    """Funnel counters + spend. Cheap enough to poll."""
    with session_scope() as session:
        counts = dict(
            session.execute(
                select(Application.state, func.count(Application.id)).group_by(Application.state)
            ).all()
        )
        spend = session.scalar(select(func.coalesce(func.sum(Run.cost_cents), 0.0))) or 0.0
        jobs = session.scalar(select(func.count(Job.id))) or 0
        matched = session.scalar(select(func.count(Match.id)).where(Match.score > 0)) or 0
        responses = sum(counts.get(state, 0) for state in ("confirmed", "interview", "declined"))
        submitted = counts.get("submitted", 0) + responses
        return {
            "by_state": counts,
            "funnel": {
                "discovered": jobs,
                "matched": matched,
                "submitted": submitted,
                "responses": responses,
            },
            "spend_cents": round(float(spend), 2),
            "cost_per_application_cents": round(float(spend) / submitted, 2) if submitted else None,
        }


def _application_row(session: Session, application: Application) -> dict[str, Any]:
    job = session.get(Job, application.job_id)
    company = session.get(Company, job.company_id) if job else None
    match = session.scalar(
        select(Match).where(Match.job_id == application.job_id).order_by(Match.created_at.desc())
    )
    return {
        "id": str(application.id),
        "state": application.state,
        "company": company.name if company else None,
        "title": job.title if job else None,
        "location": job.location if job else None,
        "apply_url": job.apply_url if job else None,
        "score": match.score if match else None,
        "reasons": match.reasons_json if match else [],
        "blockers": match.blockers_json if match else [],
        "needs_input": application.needs_input_json,
        "submitted_at": application.submitted_at.isoformat() if application.submitted_at else None,
        "updated_at": application.updated_at.isoformat() if application.updated_at else None,
        "has_resume": application.resume_artifact_id is not None,
    }


@app.get("/api/applications")
def list_applications(
    state: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
) -> list[dict[str, Any]]:
    with session_scope() as session:
        query = session.query(Application).order_by(Application.updated_at.desc())
        if state:
            query = query.filter(Application.state == state)
        return [_application_row(session, row) for row in query.limit(limit).all()]


@app.get("/api/review")
def review_queue(limit: int = Query(default=50, le=200)) -> list[dict[str, Any]]:
    """The primary daily surface: everything waiting on a human."""
    with session_scope() as session:
        rows = (
            session.query(Application)
            .filter(Application.state.in_(sorted(REVIEWABLE_STATES)))
            .order_by(Application.updated_at)
            .limit(limit)
            .all()
        )
        out = []
        for application in rows:
            item = _application_row(session, application)
            job = session.get(Job, application.job_id)
            item["job_description"] = (job.description if job else "")[:8000]
            item["tailored"] = application.tailored_json
            out.append(item)
        return out


@app.get("/api/applications/{application_id}")
def get_application(application_id: uuid.UUID) -> dict[str, Any]:
    with session_scope() as session:
        application = session.get(Application, application_id)
        if application is None:
            raise HTTPException(status_code=404, detail="not found")
        item = _application_row(session, application)
        job = session.get(Job, application.job_id)
        item["job_description"] = job.description if job else ""
        item["tailored"] = application.tailored_json
        item["timeline"] = _timeline(session, application_id)
        return item


def _timeline(session: Session, application_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = session.execute(
        select(RunEvent, Run.stage)
        .join(Run, Run.id == RunEvent.run_id)
        .where(
            or_(
                Run.application_id == application_id,
                RunEvent.payload_json["application_id"].astext == str(application_id),
            )
        )
        .order_by(RunEvent.id)
    ).all()
    return [
        {
            "id": event.id,
            "ts": event.ts.isoformat() if event.ts else None,
            "stage": stage,
            "level": event.level,
            "event_type": event.event_type,
            "message": event.message,
            "payload": event.payload_json,
        }
        for event, stage in rows
    ]


@app.post("/api/applications/{application_id}/approve", dependencies=[Depends(require_token)])
def approve(application_id: uuid.UUID, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Move an application into the submit queue. Submission never happens without this."""
    return _decide(application_id, "approved", (body or {}).get("note", "approved by reviewer"))


@app.post("/api/applications/{application_id}/reject", dependencies=[Depends(require_token)])
def reject(application_id: uuid.UUID, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return _decide(application_id, "rejected", (body or {}).get("note", "rejected by reviewer"))


def _decide(application_id: uuid.UUID, target: str, note: str) -> dict[str, Any]:
    with session_scope() as session:
        application = session.get(Application, application_id)
        if application is None:
            raise HTTPException(status_code=404, detail="not found")
        run = start_run(session, stage="review", application_id=application.id)
        try:
            transition(session, application, target, run=run, message=note, payload={"actor": "human"})
        except IllegalTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": str(application.id), "state": application.state}


@app.post("/api/applications/{application_id}/answers", dependencies=[Depends(require_token)])
def answer_questions(application_id: uuid.UUID, body: dict[str, Any]) -> dict[str, Any]:
    """Supply answers for a needs_input escalation, then re-approve."""
    answers = body.get("answers") or {}
    with session_scope() as session:
        application = session.get(Application, application_id)
        if application is None:
            raise HTTPException(status_code=404, detail="not found")
        if application.state != "needs_input":
            raise HTTPException(status_code=409, detail=f"application is {application.state}")
        payload = dict(application.needs_input_json or {})
        payload["answers"] = answers
        application.needs_input_json = payload
        session.add(application)
        run = start_run(session, stage="review", application_id=application.id)
        log_event(session, run, event_type="review.answers", message="human answered escalation", payload={"count": len(answers)})
        transition(session, application, "approved", run=run, message="escalation resolved by human")
        return {"id": str(application.id), "state": application.state}


@app.get("/api/artifacts/{artifact_id}")
def get_artifact(artifact_id: uuid.UUID) -> Response:
    with session_scope() as session:
        artifact = session.get(Artifact, artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="not found")
        data = ArtifactStore().get(artifact.uri)
        media = {
            "resume_pdf": "application/pdf",
            "cover_letter": "application/pdf",
            "screenshot": "image/png",
            "dom_snapshot": "text/html",
        }.get(artifact.kind, "application/octet-stream")
        return Response(content=data, media_type=media)


@app.get("/api/runs")
def list_runs(limit: int = Query(default=50, le=200)) -> list[dict[str, Any]]:
    with session_scope() as session:
        rows = session.query(Run).order_by(Run.started_at.desc()).limit(limit).all()
        return [
            {
                "id": str(run.id),
                "stage": run.stage,
                "status": run.status,
                "trace_id": run.trace_id,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "ended_at": run.ended_at.isoformat() if run.ended_at else None,
                "cost_cents": round(run.cost_cents or 0.0, 4),
                "application_id": str(run.application_id) if run.application_id else None,
            }
            for run in rows
        ]


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: uuid.UUID, after: int = 0) -> list[dict[str, Any]]:
    with session_scope() as session:
        rows = (
            session.query(RunEvent)
            .filter(RunEvent.run_id == run_id, RunEvent.id > after)
            .order_by(RunEvent.id)
            .all()
        )
        return [_event_dict(event) for event in rows]


def _event_dict(event: RunEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "ts": event.ts.isoformat() if event.ts else None,
        "level": event.level,
        "event_type": event.event_type,
        "message": event.message,
        "payload": event.payload_json,
    }


@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: uuid.UUID, after: int = 0, level: str | None = None) -> StreamingResponse:
    """Live tail. Polls the append-only log and pushes new rows as SSE frames."""

    async def generator():
        cursor = after
        idle = 0
        while True:
            events, finished = await asyncio.to_thread(_poll_events, run_id, cursor, level)
            for event in events:
                cursor = event["id"]
                yield f"id: {event['id']}\nevent: log\ndata: {json.dumps(event)}\n\n"
            if events:
                idle = 0
            else:
                idle += 1
                yield ": keepalive\n\n"
            if finished and not events:
                yield "event: end\ndata: {}\n\n"
                return
            if idle > 600:  # ~10 minutes with no activity
                return
            await asyncio.sleep(1.0)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def _poll_events(run_id: uuid.UUID, after: int, level: str | None) -> tuple[list[dict[str, Any]], bool]:
    with session_scope() as session:
        query = session.query(RunEvent).filter(RunEvent.run_id == run_id, RunEvent.id > after)
        if level:
            query = query.filter(RunEvent.level == level)
        events = [_event_dict(event) for event in query.order_by(RunEvent.id).limit(200).all()]
        run = session.get(Run, run_id)
        finished = bool(run and run.ended_at is not None)
        return events, finished


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    path = STATIC_DIR / "favicon.ico"
    if path.exists():
        return FileResponse(path)
    return Response(status_code=204)

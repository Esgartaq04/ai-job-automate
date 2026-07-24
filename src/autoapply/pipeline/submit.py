"""Stage 4 — Submit.

Runs only against applications a human has explicitly approved. One fresh browser
context per application. Verification is mandatory: a submit is not `submitted`
until the adapter confirms a success signal, and a screenshot + DOM snapshot are
captured either way.
"""

from __future__ import annotations

import datetime as dt
import logging
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..ats import Escalation, EscalationReason, FieldMapper, get_adapter
from ..config import get_settings
from ..events import finish_run, log_event, start_run, transition
from ..llm.client import LLMClient, get_llm
from ..models import Application, Artifact, Company, Job, Profile
from ..ratelimit import CircuitBreaker, TokenBucket, human_pacing_delay
from ..storage import ArtifactStore, record_artifact
from .render import launch_kwargs

log = logging.getLogger(__name__)


class DailyCapReached(RuntimeError):
    """Volume guard from README section 8. Human-plausible pacing, not 500/day."""


@dataclass
class SubmitOutcome:
    application_id: Any
    state: str
    detail: str = ""
    verified: bool = False
    screenshot_uri: str | None = None
    dom_uri: str | None = None
    cost_cents: float = 0.0
    escalation: dict[str, Any] | None = None
    unmapped: list[str] = field(default_factory=list)


def submitted_today(session: Session, user_id: Any) -> int:
    since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=24)
    return int(
        session.scalar(
            select(func.count(Application.id)).where(
                Application.user_id == user_id,
                Application.submitted_at.is_not(None),
                Application.submitted_at >= since,
            )
        )
        or 0
    )


def profile_values(profile: Profile, resume_path: str, cover_path: str | None) -> dict[str, Any]:
    """Canonical key -> value. Only ever populated from the stored profile."""
    resume = profile.base_resume_json or {}
    name = str(resume.get("name", "")).split()
    values: dict[str, Any] = {
        "first_name": resume.get("first_name") or (name[0] if name else ""),
        "last_name": resume.get("last_name") or (" ".join(name[1:]) if len(name) > 1 else ""),
        "full_name": resume.get("name", ""),
        "email": resume.get("email", ""),
        "phone": resume.get("phone", ""),
        "location": resume.get("location", ""),
        "linkedin": resume.get("linkedin"),
        "github": resume.get("github"),
        "portfolio": resume.get("portfolio"),
        "resume": resume_path,
        "cover_letter": cover_path,
        "work_authorized": resume.get("work_authorized"),
        "requires_sponsorship": resume.get("requires_sponsorship"),
        "start_date": resume.get("start_date"),
        "salary_expectation": resume.get("salary_expectation"),
        "years_experience": resume.get("years_experience"),
        "how_did_you_hear": resume.get("how_did_you_hear"),
    }
    return {k: v for k, v in values.items() if v not in (None, "")}


def _artifact_to_temp(store: ArtifactStore, artifact: Artifact | None, directory: Path) -> str | None:
    if artifact is None:
        return None
    path = directory / f"{artifact.kind}-{artifact.sha256[:12]}.pdf"
    path.write_bytes(store.get(artifact.uri))
    return str(path)


def _escalate(
    session: Session,
    application: Application,
    run: Any,
    escalation: Escalation,
) -> SubmitOutcome:
    application.needs_input_json = escalation.to_payload()
    session.add(application)
    transition(
        session,
        application,
        "needs_input",
        run=run,
        message=f"paused: {escalation.reason.value}",
        payload=escalation.to_payload(),
    )
    log_event(
        session,
        run,
        event_type="submit.escalated",
        level="warn",
        message=str(escalation),
        payload=escalation.to_payload(),
    )
    finish_run(session, run, status="needs_input")
    return SubmitOutcome(
        application.id, "needs_input", detail=str(escalation), escalation=escalation.to_payload()
    )


def submit_application(
    session: Session,
    application: Application,
    *,
    store: ArtifactStore | None = None,
    llm: LLMClient | None = None,
    browser_factory: Any | None = None,
) -> SubmitOutcome:
    """Fill and submit one approved application.

    `browser_factory` is injected in tests; in production it opens real Chromium.
    """
    settings = get_settings()
    store = store or ArtifactStore()
    llm = llm or get_llm()
    run = start_run(session, stage="submit", application_id=application.id)

    if application.state not in ("approved", "needs_input"):
        finish_run(session, run, status="skipped")
        return SubmitOutcome(application.id, application.state, detail="not approved")

    if submitted_today(session, application.user_id) >= settings.daily_submit_cap:
        log_event(
            session,
            run,
            event_type="submit.capped",
            level="warn",
            message=f"daily cap of {settings.daily_submit_cap} reached; deferring",
        )
        finish_run(session, run, status="deferred")
        raise DailyCapReached(f"daily submit cap {settings.daily_submit_cap} reached")

    job = session.get(Job, application.job_id)
    company = session.get(Company, job.company_id) if job else None
    profile = (
        session.query(Profile)
        .filter(Profile.user_id == application.user_id)
        .order_by(Profile.version.desc())
        .first()
    )
    if job is None or company is None or profile is None:
        finish_run(session, run, status="error")
        return SubmitOutcome(application.id, application.state, detail="missing job/company/profile")

    adapter = get_adapter(company.ats_type)
    mapper = FieldMapper(
        company.ats_type, llm=llm, confidence_floor=settings.field_confidence_floor
    )

    log_event(
        session, run, event_type="submit.start", message=f"opening {job.apply_url}",
        payload={"apply_url": job.apply_url, "ats": company.ats_type},
    )
    session.flush()

    time.sleep(human_pacing_delay(settings.submit_jitter_seconds))

    with tempfile.TemporaryDirectory() as tmpdir:
        directory = Path(tmpdir)
        resume_path = _artifact_to_temp(
            store, session.get(Artifact, application.resume_artifact_id) if application.resume_artifact_id else None, directory
        )
        cover_path = _artifact_to_temp(
            store, session.get(Artifact, application.cover_artifact_id) if application.cover_artifact_id else None, directory
        )
        if not resume_path:
            finish_run(session, run, status="error")
            return SubmitOutcome(application.id, application.state, detail="no resume artifact")

        values = profile_values(profile, resume_path, cover_path)

        factory = browser_factory or _playwright_page
        with factory(job.apply_url) as page:
            try:
                adapter.guard(page)
                fields = adapter.map_fields(page)
                for field_obj in fields.values():
                    mapper.resolve(session, field_obj)

                unmapped_required = [
                    f.label or f.selector
                    for f in fields.values()
                    if f.required and (not f.canonical or f.confidence < settings.field_confidence_floor)
                ]
                long_freetext = [
                    f.label or f.selector
                    for f in fields.values()
                    if f.kind.value == "textarea"
                    and f.canonical not in ("cover_letter", "resume")
                    and f.required
                ]

                if unmapped_required:
                    raise Escalation(
                        EscalationReason.UNMAPPED_REQUIRED_FIELD,
                        f"{len(unmapped_required)} required field(s) below the confidence floor",
                        questions=[{"label": label, "kind": "text"} for label in unmapped_required],
                    )
                if long_freetext:
                    raise Escalation(
                        EscalationReason.LONG_FREE_TEXT,
                        "required free-text question needs a human answer",
                        questions=[{"label": label, "kind": "textarea"} for label in long_freetext],
                    )

                written = adapter.fill(page, values, fields)
                log_event(
                    session,
                    run,
                    event_type="submit.filled",
                    message=f"filled {len(written)} of {len(fields)} controls",
                    payload={
                        "mapped": {f.selector: f.canonical for f in fields.values() if f.canonical},
                        "sources": {f.selector: f.source for f in fields.values()},
                    },
                )

                screenshot = page.screenshot(full_page=True)
                dom = page.content().encode()

                if settings.dry_run:
                    result_detail = "dry run: form filled, submit click skipped"
                    submitted = verified = False
                else:
                    transition(
                        session,
                        application,
                        "submitting",
                        run=run,
                        message="all fields mapped above the confidence floor; submitting",
                    )
                    session.flush()
                    result = adapter.submit(page)
                    submitted, verified = result.submitted, result.verified
                    result_detail = result.detail
                    # Capture again *after* the click — this is the artifact that
                    # makes the log viewer worth building.
                    screenshot = page.screenshot(full_page=True)
                    dom = page.content().encode()

            except Escalation as escalation:
                try:
                    record_artifact(
                        session, store, page.screenshot(full_page=True), kind="screenshot", suffix=".png"
                    )
                except Exception:  # noqa: BLE001 - never let capture failure mask the escalation
                    log.debug("screenshot capture failed during escalation", exc_info=True)
                return _escalate(session, application, run, escalation)

    shot_artifact = record_artifact(session, store, screenshot, kind="screenshot", suffix=".png")
    dom_artifact = record_artifact(session, store, dom, kind="dom_snapshot", suffix=".html")
    log_event(
        session,
        run,
        event_type="submit.captured",
        message="captured screenshot + DOM snapshot",
        payload={"screenshot": shot_artifact.uri, "dom": dom_artifact.uri},
    )

    if settings.dry_run:
        # Stay in `approved`: nothing was sent, so the queue entry is still live.
        log_event(
            session,
            run,
            event_type="submit.dry_run",
            level="warn",
            message="dry run — form filled and captured, no application sent",
        )
        finish_run(session, run, status="dry_run", cost_cents=mapper.cost_cents)
        return SubmitOutcome(
            application.id,
            application.state,
            detail=result_detail,
            screenshot_uri=shot_artifact.uri,
            dom_uri=dom_artifact.uri,
            cost_cents=mapper.cost_cents,
        )

    if submitted and verified:
        application.submitted_at = dt.datetime.now(dt.UTC)
        session.add(application)
        transition(session, application, "submitted", run=run, message=result_detail)
        state = "submitted"
    else:
        # Clicked but unconfirmed is a failure, not a success. Silent form failures
        # are exactly what verification exists to catch.
        transition(
            session,
            application,
            "failed",
            run=run,
            message=result_detail or "no confirmation signal after submit",
        )
        state = "failed"

    finish_run(session, run, status=state, cost_cents=mapper.cost_cents)
    return SubmitOutcome(
        application.id,
        state,
        detail=result_detail,
        verified=verified,
        screenshot_uri=shot_artifact.uri,
        dom_uri=dom_artifact.uri,
        cost_cents=mapper.cost_cents,
    )


class _playwright_page:
    """Fresh browser context per application — no shared cookies or storage state."""

    def __init__(self, url: str):
        self.url = url
        self._playwright = None
        self._browser = None
        self._context = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(**launch_kwargs())
        self._context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
        )
        page = self._context.new_page()
        page.goto(self.url, wait_until="domcontentloaded", timeout=60_000)
        return page

    def __exit__(self, *exc: Any) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:  # noqa: BLE001
                log.debug("browser teardown failed", exc_info=True)
        if self._playwright:
            self._playwright.stop()


def submit_approved(
    session: Session, *, limit: int = 10, store: ArtifactStore | None = None
) -> list[SubmitOutcome]:
    """Drain the approved queue, respecting the per-domain bucket and breaker."""
    settings = get_settings()
    bucket = TokenBucket(settings.ats_rate_per_min)
    breaker = CircuitBreaker(failure_threshold=settings.circuit_failure_threshold)
    outcomes: list[SubmitOutcome] = []

    applications = (
        session.query(Application)
        .filter(Application.state == "approved")
        .order_by(Application.updated_at)
        .limit(limit)
        .all()
    )
    for application in applications:
        try:
            breaker.check()
            bucket.acquire()
            outcome = submit_application(session, application, store=store)
            breaker.record_success()
        except DailyCapReached:
            log.info("daily submit cap reached; stopping")
            break
        except Exception as exc:  # noqa: BLE001 - one bad form must not stop the queue
            breaker.record_failure()
            log.warning("submit failed for %s", application.id, exc_info=True)
            outcome = SubmitOutcome(application.id, application.state, detail=f"{type(exc).__name__}: {exc}")
        outcomes.append(outcome)
        session.commit()
    return outcomes

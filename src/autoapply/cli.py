"""Operator CLI. Phase 1 is driven from here; the API is for review and logs."""

# ruff: noqa: B008  - typer.Option() in a default is the framework's calling convention.

from __future__ import annotations

import json
import uuid as uuidlib
from pathlib import Path
from typing import Any

import typer
from sqlalchemy import select

from .config import get_settings
from .db import apply_migrations, session_scope
from .events import log_event, start_run, transition
from .llm.embeddings import get_embedder
from .models import Application, Company, Fact, Job, Match, Profile, User
from .pipeline.gmail import GmailSource
from .pipeline.ingest import ingest_all, ingest_company
from .pipeline.match import Preferences, match_profile
from .pipeline.outcome import Message, process_messages
from .pipeline.submit import submit_approved
from .pipeline.tailor import tailor_application

app = typer.Typer(add_completion=False, help="AutoApply operator CLI")
companies_app = typer.Typer(help="Manage the company -> ATS board mapping")
app.add_typer(companies_app, name="companies")


@app.command("init-db")
def init_db() -> None:
    """Create the schema (idempotent)."""
    applied = apply_migrations()
    typer.echo(f"applied: {', '.join(applied)}")


@app.command("load-profile")
def load_profile(path: Path, email: str = typer.Option(..., help="User email")) -> None:
    """Load a profile + fact store from JSON. See docs/profile.example.json."""
    payload = json.loads(path.read_text())
    embedder = get_embedder()

    with session_scope() as session:
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(email=email)
            session.add(user)
            session.flush()

        latest = (
            session.query(Profile)
            .filter(Profile.user_id == user.id)
            .order_by(Profile.version.desc())
            .first()
        )
        version = (latest.version + 1) if latest else 1
        profile = Profile(
            user_id=user.id,
            version=version,
            base_resume_json=payload.get("profile", {}),
        )
        session.add(profile)
        session.flush()

        facts = payload.get("facts", [])
        for entry in facts:
            text = entry["text"]
            context = " ".join(x for x in [entry.get("role"), entry.get("org"), text] if x)
            session.add(
                Fact(
                    id=f"{entry['id']}:v{version}",
                    profile_id=profile.id,
                    type=entry.get("type", "experience"),
                    org=entry.get("org"),
                    role=entry.get("role"),
                    start_date=entry.get("start"),
                    end_date=entry.get("end"),
                    text=text,
                    skills=entry.get("skills", []),
                    embedding=embedder.embed(f"{context} {' '.join(entry.get('skills', []))}"),
                )
            )
        session.flush()
        summary = "\n".join(f"- {f['text']}" for f in facts)
        profile.embedding = embedder.embed(
            f"{payload.get('profile', {}).get('headline', '')}\n{summary}"
        )
        typer.echo(f"profile v{version} for {email}: {len(facts)} facts")


@companies_app.command("add")
def add_company(
    name: str,
    board_token: str,
    ats_type: str = typer.Option("greenhouse"),
    careers_url: str | None = typer.Option(None),
) -> None:
    with session_scope() as session:
        existing = session.scalar(
            select(Company).where(Company.ats_type == ats_type, Company.board_token == board_token)
        )
        if existing:
            typer.echo(f"already tracked: {existing.name}")
            return
        session.add(
            Company(name=name, ats_type=ats_type, board_token=board_token, careers_url=careers_url)
        )
        typer.echo(f"added {name} ({ats_type}:{board_token})")


@companies_app.command("import")
def import_companies(path: Path) -> None:
    """Seed from a JSON list of {name, ats_type, board_token}."""
    entries = json.loads(path.read_text())
    added = 0
    with session_scope() as session:
        for entry in entries:
            exists = session.scalar(
                select(Company).where(
                    Company.ats_type == entry.get("ats_type", "greenhouse"),
                    Company.board_token == entry["board_token"],
                )
            )
            if exists:
                continue
            session.add(
                Company(
                    name=entry["name"],
                    ats_type=entry.get("ats_type", "greenhouse"),
                    board_token=entry["board_token"],
                    careers_url=entry.get("careers_url"),
                )
            )
            added += 1
    typer.echo(f"added {added} companies")


@companies_app.command("list")
def list_companies() -> None:
    with session_scope() as session:
        for company in session.scalars(select(Company).order_by(Company.name)):
            flag = "" if company.active else " (inactive)"
            typer.echo(f"{company.name}\t{company.ats_type}:{company.board_token}{flag}")


@app.command()
def ingest(board: str | None = typer.Option(None, help="Only this board token")) -> None:
    """Stage 1 — pull postings from every active board."""
    with session_scope() as session:
        if board:
            company = session.scalar(select(Company).where(Company.board_token == board))
            if company is None:
                raise typer.BadParameter(f"no company with board_token={board}")
            report = ingest_company(session, company)
        else:
            report = ingest_all(session)
        typer.echo(
            f"fetched={report.fetched} new={report.inserted} dup={report.duplicates} updated={report.updated}"
        )
        for error in report.errors or []:
            typer.echo(f"  error: {error}", err=True)


@app.command()
def match(email: str = typer.Option(...), limit: int = typer.Option(50)) -> None:
    """Stage 2 — hard filters, vector recall, then a cheap model pass."""
    with session_scope() as session:
        profile = _profile_for(session, email)
        outcomes = match_profile(session, profile, limit=limit)
        kept = [o for o in outcomes if not o.blockers]
        typer.echo(f"scored {len(outcomes)}; {len(outcomes) - len(kept)} blocked by hard filters")
        for outcome in sorted(kept, key=lambda o: o.score, reverse=True)[:15]:
            job = session.get(Job, outcome.job_id)
            typer.echo(f"  {outcome.score:.2f}  {job.title}  ({job.location or 'n/a'})")


@app.command()
def draft(
    email: str = typer.Option(...),
    min_score: float = typer.Option(None, help="Overrides the profile preference"),
    limit: int = typer.Option(5),
) -> None:
    """Stage 3 — create applications for high-scoring matches and tailor them."""
    with session_scope() as session:
        profile = _profile_for(session, email)
        threshold = min_score if min_score is not None else Preferences.from_profile(profile).min_score

        rows = (
            session.query(Match)
            .filter(Match.profile_id == profile.id, Match.score >= threshold)
            .order_by(Match.score.desc())
            .limit(limit * 3)
            .all()
        )
        drafted = 0
        for row in rows:
            if drafted >= limit:
                break
            if row.blockers_json:
                continue
            existing = session.scalar(
                select(Application).where(
                    Application.user_id == profile.user_id, Application.job_id == row.job_id
                )
            )
            if existing is not None:
                continue

            application = Application(user_id=profile.user_id, job_id=row.job_id, state="discovered")
            session.add(application)
            session.flush()
            run = start_run(session, stage="tailor", application_id=application.id)
            transition(session, application, "matched", run=run, message=f"fit {row.score:.2f}")
            session.flush()

            result = tailor_application(session, application)
            job = session.get(Job, row.job_id)
            status = "verified" if result.ok else "sent to manual review"
            typer.echo(f"  {job.title}: {status} ({result.attempts} attempt(s))")
            drafted += 1
        typer.echo(f"drafted {drafted} applications; review them at /")


@app.command()
def submit(limit: int = typer.Option(5)) -> None:
    """Stage 4 — submit approved applications. Nothing here runs without approval."""
    settings = get_settings()
    if settings.dry_run:
        typer.echo("AUTOAPPLY_DRY_RUN=true — forms will be filled and captured, nothing sent")
    with session_scope() as session:
        outcomes = submit_approved(session, limit=limit)
    for outcome in outcomes:
        typer.echo(f"  {outcome.application_id} -> {outcome.state}: {outcome.detail}")
    if not outcomes:
        typer.echo("nothing approved")


@app.command()
def outcomes(
    email: str = typer.Option(...),
    path: Path | None = typer.Option(None, help="JSON array of messages"),
    gmail: bool = typer.Option(False, "--gmail", help="Pull from Gmail instead of a file"),
    query: str | None = typer.Option(None, help="Gmail search query; overrides the configured one"),
    reprocess: bool = typer.Option(False, help="Re-handle messages already recorded as processed"),
) -> None:
    """Stage 5 — classify inbound mail and close the funnel.

    Source is either a JSON export (--path) or Gmail (--gmail). Already-processed
    messages are skipped before the classifier runs, so re-running is cheap.
    """
    if gmail == bool(path):
        raise typer.BadParameter("pass exactly one of --path or --gmail")

    if gmail:
        settings = get_settings()
        source = GmailSource(
            credentials_path=Path(settings.gmail_credentials_path).expanduser(),
            token_path=Path(settings.gmail_token_path).expanduser(),
            query=query or settings.gmail_query,
        )
        try:
            messages = source.fetch()
        except ImportError as exc:  # google libs are an optional extra
            raise typer.BadParameter(
                'Gmail support needs the extra: pip install -e ".[gmail]"'
            ) from exc
        typer.echo(f"fetched {len(messages)} messages from gmail")
    else:
        payload = json.loads(path.read_text())  # type: ignore[union-attr]
        messages = [
            Message(
                message_id=str(entry.get("id", uuidlib.uuid4())),
                subject=entry.get("subject", ""),
                sender=entry.get("from", ""),
                body=entry.get("body", ""),
            )
            for entry in payload
        ]

    with session_scope() as session:
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            raise typer.BadParameter(f"unknown user {email}")
        results = process_messages(session, user.id, messages, reprocess=reprocess)
        for result in results:
            typer.echo(f"  {result.category:18} {result.state or '-':10} {result.detail}")
        skipped = len(messages) - len(results)
        typer.echo(f"handled {len(results)}, skipped {skipped} already processed")


@app.command()
def approve(application_id: str, note: str = typer.Option("approved via CLI")) -> None:
    """Approve one application for submission."""
    with session_scope() as session:
        application = session.get(Application, uuidlib.UUID(application_id))
        if application is None:
            raise typer.BadParameter("no such application")
        run = start_run(session, stage="review", application_id=application.id)
        transition(session, application, "approved", run=run, message=note, payload={"actor": "human"})
        log_event(session, run, event_type="review.approved", message=note)
    typer.echo("approved")


@app.command()
def status() -> None:
    """Counts by state plus total spend."""
    from sqlalchemy import func

    from .models import Run

    with session_scope() as session:
        rows = session.execute(
            select(Application.state, func.count(Application.id)).group_by(Application.state)
        ).all()
        spend = session.scalar(select(func.coalesce(func.sum(Run.cost_cents), 0.0))) or 0.0
        for state, count in sorted(rows, key=lambda r: -r[1]):
            typer.echo(f"  {state:16} {count}")
        typer.echo(f"  {'spend':16} ${spend / 100:.4f}")


@app.command()
def serve(reload: bool = typer.Option(False)) -> None:
    """Run the API + review UI."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "autoapply.api.main:app", host=settings.api_host, port=settings.api_port, reload=reload
    )


def _profile_for(session: Any, email: str) -> Profile:
    user = session.scalar(select(User).where(User.email == email))
    if user is None:
        raise typer.BadParameter(f"unknown user {email}")
    profile = (
        session.query(Profile)
        .filter(Profile.user_id == user.id)
        .order_by(Profile.version.desc())
        .first()
    )
    if profile is None:
        raise typer.BadParameter(f"{email} has no profile; run load-profile first")
    return profile


if __name__ == "__main__":
    app()

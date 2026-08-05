"""Application state machine and the append-only run-event log.

`run_events` is the source of truth. `applications.state` is a materialized cache
of the latest transition, written in the same transaction as the event.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Application, Run, RunEvent
from .scrub import scrub

# README section 4. Terminal states have an empty target set.
TRANSITIONS: dict[str, set[str]] = {
    "discovered": {"matched", "rejected"},
    "matched": {"drafted", "rejected"},
    "drafted": {"pending_review", "failed"},
    "pending_review": {"approved", "rejected", "needs_input"},
    "needs_input": {"approved", "rejected", "submitting"},
    # approved -> needs_input covers an escalation raised while filling the form,
    # before anything was submitted.
    "approved": {"submitting", "needs_input", "rejected"},
    "submitting": {"submitted", "failed", "blocked", "needs_input"},
    "submitted": {"confirmed", "interview", "declined"},
    "failed": {"approved"},  # operator can retry a failed submit after fixing the cause
    "blocked": {"rejected"},
    "confirmed": {"interview", "declined"},
    "interview": {"declined"},
    "rejected": set(),
    "declined": set(),
}

REVIEWABLE_STATES = {"pending_review", "needs_input"}
TERMINAL_STATES = {s for s, targets in TRANSITIONS.items() if not targets}


class IllegalTransition(ValueError):
    """Raised when a caller tries to move an application along an edge that doesn't exist."""


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, set())


def start_run(
    session: Session,
    *,
    stage: str,
    application_id: uuid.UUID | None = None,
    trace_id: str | None = None,
) -> Run:
    run = Run(
        stage=stage,
        application_id=application_id,
        trace_id=trace_id or uuid.uuid4().hex,
        status="running",
    )
    session.add(run)
    session.flush()
    return run


def finish_run(session: Session, run: Run, *, status: str = "ok", cost_cents: float = 0.0) -> Run:
    run.status = status
    run.ended_at = dt.datetime.now(dt.UTC)
    run.cost_cents = (run.cost_cents or 0.0) + cost_cents
    session.add(run)
    return run


def log_event(
    session: Session,
    run: Run,
    *,
    event_type: str,
    message: str = "",
    level: str = "info",
    payload: dict[str, Any] | None = None,
) -> RunEvent:
    """Append one event. Payloads are scrubbed on write, never on read."""
    event = RunEvent(
        run_id=run.id,
        event_type=event_type,
        message=scrub(message),
        level=level,
        payload_json=scrub(payload or {}),
    )
    session.add(event)
    session.flush()
    return event


def transition(
    session: Session,
    application: Application,
    dst: str,
    *,
    run: Run,
    message: str = "",
    payload: dict[str, Any] | None = None,
) -> RunEvent:
    """Move an application to `dst`, recording the edge as an append-only event.

    Raises IllegalTransition rather than silently coercing the state — a bad edge
    means the caller's logic is wrong and we want the run to fail loudly.
    """
    src = application.state
    if src == dst:
        return log_event(
            session, run, event_type="state.noop", message=message or f"already {dst}", payload=payload
        )
    if not can_transition(src, dst):
        raise IllegalTransition(f"{src} -> {dst} is not a legal transition")

    application.state = dst
    application.updated_at = dt.datetime.now(dt.UTC)
    session.add(application)
    return log_event(
        session,
        run,
        event_type="state.transition",
        message=message or f"{src} -> {dst}",
        # application_id travels in the payload so the log stays replayable even
        # when the run itself isn't scoped to one application (the outcome sweep
        # processes a whole mailbox in a single run).
        payload={
            "application_id": str(application.id),
            "from": src,
            "to": dst,
            **(payload or {}),
        },
    )


def replay_state(session: Session, application_id: uuid.UUID) -> str:
    """Recompute state from the event log. Used to verify the cache hasn't drifted."""
    rows = session.execute(
        select(RunEvent.payload_json)
        .where(
            RunEvent.event_type == "state.transition",
            RunEvent.payload_json["application_id"].astext == str(application_id),
        )
        .order_by(RunEvent.id)
    ).scalars()
    state = "discovered"
    for payload in rows:
        to = (payload or {}).get("to")
        if to:
            state = to
    return state

"""Stage 5 — Outcome tracking.

Classify inbound mail on the dedicated alias and match it back to an application,
so the dashboard shows real numbers instead of just "submitted".

The message source is deliberately abstract: Phase 1 feeds it from a JSON export
or an IMAP pull; the Gmail API watch lands in Phase 2 without touching this logic.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..events import finish_run, log_event, start_run, transition
from ..llm.client import LLMClient, fast_model, get_llm
from ..models import Application, Company, Job

CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": ["confirmation", "rejection", "interview_request", "other"],
        },
        "company_guess": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["category", "company_guess", "confidence"],
    "additionalProperties": False,
}

CLASSIFY_SYSTEM = """You classify an email received at a job-application alias.

Categories:
- confirmation: an automated "we received your application" acknowledgement.
- rejection: the employer is not moving forward.
- interview_request: any invitation to talk, schedule, or complete an assessment.
- other: newsletters, job alerts, anything not about a specific application.

Also return the employer's name as it appears in the email, and your confidence
(0..1). If the email is not about a specific application, category is "other"
and company_guess is an empty string."""

# Terminal outcome -> state machine target.
CATEGORY_STATE = {
    "confirmation": "confirmed",
    "rejection": "declined",
    "interview_request": "interview",
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass
class Message:
    message_id: str
    subject: str
    sender: str
    body: str
    received_at: Any = None


@dataclass
class OutcomeResult:
    message_id: str
    category: str
    application_id: Any | None
    state: str | None
    detail: str = ""
    cost_cents: float = 0.0


def _slug(value: str) -> str:
    return _NON_ALNUM.sub("", (value or "").lower())


def classify(llm: LLMClient, message: Message) -> tuple[dict[str, Any], float]:
    prompt = (
        f"From: {message.sender}\nSubject: {message.subject}\n\n{(message.body or '')[:6000]}"
    )
    result = llm.complete_json(
        model=fast_model(),
        system=CLASSIFY_SYSTEM,
        prompt=prompt,
        schema=CLASSIFY_SCHEMA,
        max_tokens=512,
    )
    return result.data, result.cost_cents


def find_application(
    session: Session, user_id: Any, company_guess: str, sender: str
) -> Application | None:
    """Match an email back to an application by company name, then sender domain.

    Only submitted applications are candidates, newest first — the same employer
    can appear more than once and the recent submission is the likely subject.
    """
    rows = (
        session.query(Application, Company)
        .join(Job, Job.id == Application.job_id)
        .join(Company, Company.id == Job.company_id)
        .filter(Application.user_id == user_id, Application.submitted_at.is_not(None))
        .order_by(Application.submitted_at.desc())
        .all()
    )
    guess = _slug(company_guess)
    domain = _slug(sender.split("@")[-1].split(".")[0]) if "@" in sender else ""

    if guess:
        for application, company in rows:
            slug = _slug(company.name)
            if slug and (slug == guess or slug in guess or guess in slug):
                return application
    if domain:
        for application, company in rows:
            slug = _slug(company.name)
            if slug and (slug in domain or domain in slug):
                return application
    return None


def process_messages(
    session: Session, user_id: Any, messages: Iterable[Message], *, llm: LLMClient | None = None
) -> list[OutcomeResult]:
    """Classify each message and close the funnel where it matches an application."""
    llm = llm or get_llm()
    run = start_run(session, stage="outcome")
    results: list[OutcomeResult] = []
    total_cost = 0.0

    for message in messages:
        data, cost = classify(llm, message)
        total_cost += cost
        category = data.get("category", "other")

        if category == "other":
            results.append(OutcomeResult(message.message_id, category, None, None, "not application mail", cost))
            continue

        application = find_application(session, user_id, data.get("company_guess", ""), message.sender)
        if application is None:
            log_event(
                session,
                run,
                event_type="outcome.unmatched",
                level="warn",
                message=f"{category} email matched no application",
                payload={"subject": message.subject, "company_guess": data.get("company_guess")},
            )
            results.append(OutcomeResult(message.message_id, category, None, None, "unmatched", cost))
            continue

        target = CATEGORY_STATE[category]
        from ..events import can_transition

        if not can_transition(application.state, target):
            results.append(
                OutcomeResult(
                    message.message_id,
                    category,
                    application.id,
                    application.state,
                    f"{application.state} -> {target} not legal; ignored",
                    cost,
                )
            )
            continue

        transition(
            session,
            application,
            target,
            run=run,
            message=f"{category} email from {message.sender}",
            payload={"subject": message.subject, "message_id": message.message_id},
        )
        results.append(OutcomeResult(message.message_id, category, application.id, target, "matched", cost))

    log_event(
        session,
        run,
        event_type="outcome.done",
        message=f"processed {len(results)} messages",
        payload={"cost_cents": round(total_cost, 4)},
    )
    finish_run(session, run, cost_cents=total_cost)
    return results

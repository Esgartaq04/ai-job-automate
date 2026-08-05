"""Stage 3 — Tailor. The part that needs discipline.

Generation contract (README section 3):
  1. Retrieve top-k relevant facts for this job.
  2. The model may only rephrase and reorder those facts. Never a new org, date,
     number or technology.
  3. A deterministic validator checks every bullet's citation and vocabulary.
     Fail -> regenerate once -> fail again -> route to manual review.
  4. Render to PDF, store hash-addressed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ..events import finish_run, log_event, start_run, transition
from ..llm.client import LLMClient, get_llm, smart_model
from ..llm.embeddings import cosine, get_embedder
from ..models import Application, Company, Fact, Job, Profile
from ..storage import ArtifactStore, record_artifact
from .render import render_cover_letter_pdf, render_resume_pdf
from .validator import ValidationReport, validate_bullets, validate_prose

TAILOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "2-3 sentence professional summary."},
        "bullets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "fact_id": {"type": "string"},
                },
                "required": ["text", "fact_id"],
                "additionalProperties": False,
            },
        },
        "cover_letter": {"type": "string"},
    },
    "required": ["summary", "bullets", "cover_letter"],
    "additionalProperties": False,
}

TAILOR_SYSTEM = """You tailor an existing resume to one job posting.

You are given a list of VERIFIED FACTS, each with a fact_id. These are the only
true statements about this candidate that exist. You are also given the job posting.

Your job is selection, ordering and rephrasing. Specifically:

- Every bullet you write MUST cite the fact_id it derives from.
- A bullet may only restate its cited fact in different words. You may make it
  crisper, front-load the outcome, or use vocabulary from the job posting for a
  concept the fact already contains.
- You MUST NOT introduce any organization, product name, technology, date,
  metric or number that is not already present in the cited fact. If a fact says
  "reduced latency", you may not write "reduced latency by 40%".
- If a fact does not support a claim the posting asks for, omit it. Never bridge
  a gap with an invented accomplishment. A shorter honest resume is the correct
  output.
- Prefer the facts most relevant to the posting; drop the rest. 5-8 bullets.
- The summary and cover letter are bound by the same rule: no new entities, no
  new numbers. The cover letter may name the company and role from the posting.
- The cover letter is 3 short paragraphs, specific, and free of filler openers.

A downstream checker verifies every claim against the cited fact. Output that
introduces unsupported detail is rejected and wastes the attempt."""


@dataclass
class TailorResult:
    ok: bool
    summary: str = ""
    bullets: list[dict[str, str]] = field(default_factory=list)
    cover_letter: str = ""
    fact_ids: list[str] = field(default_factory=list)
    cost_cents: float = 0.0
    attempts: int = 0
    report: dict[str, Any] | None = None


def retrieve_facts(session: Session, profile: Profile, job: Job, *, k: int = 12) -> list[Fact]:
    """Top-k facts by cosine similarity to the posting. Embeds facts lazily."""
    embedder = get_embedder()
    facts = list(profile.facts)
    if not facts:
        return []

    job_vector = job.embedding
    if job_vector is None:
        job_vector = embedder.embed(f"{job.title}\n{job.location or ''}\n{job.description}")
        job.embedding = job_vector
        session.add(job)

    scored: list[tuple[float, Fact]] = []
    for fact in facts:
        if fact.embedding is None:
            context = " ".join(x for x in [fact.role, fact.org, fact.text] if x)
            fact.embedding = embedder.embed(f"{context} {' '.join(fact.skills or [])}")
            session.add(fact)
        scored.append((cosine(list(job_vector), list(fact.embedding)), fact))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    session.flush()
    return [fact for _, fact in scored[:k]]


def _facts_block(facts: list[Fact]) -> str:
    lines = []
    for fact in facts:
        where = " @ ".join(x for x in [fact.role, fact.org] if x)
        window = " ".join(x for x in [fact.start_date, "-", fact.end_date or "present"] if x)
        skills = ", ".join(fact.skills or [])
        lines.append(f"[{fact.id}] {where} ({window}) :: {fact.text}" + (f" :: skills: {skills}" if skills else ""))
    return "\n".join(lines)


def _allowed_extra(profile: Profile, facts: list[Fact], job: Job, company: Company | None) -> set[str]:
    """Tokens that are legitimate anywhere: the candidate's own identity, the employer."""
    resume = profile.base_resume_json or {}
    tokens: set[str] = set()
    for value in (resume.get("name"), resume.get("headline"), resume.get("location")):
        if value:
            tokens.update(str(value).split())
    for fact in facts:
        tokens.update(fact.skills or [])
        if fact.org:
            tokens.update(fact.org.split())
    tokens.update((job.title or "").split())
    if company:
        tokens.update(company.name.split())
    return tokens


def generate(
    llm: LLMClient,
    *,
    profile: Profile,
    job: Job,
    company: Company | None,
    facts: list[Fact],
    critique: str | None = None,
) -> tuple[dict[str, Any], float]:
    prompt = (
        f"VERIFIED FACTS\n{_facts_block(facts)}\n\n"
        f"JOB POSTING\nCompany: {company.name if company else 'n/a'}\n"
        f"Title: {job.title}\nLocation: {job.location or 'n/a'}\n\n"
        f"{(job.description or '')[:14000]}"
    )
    if critique:
        prompt += (
            "\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED BY THE VERIFIER:\n"
            f"{critique}\n"
            "Rewrite so that every flagged claim is either removed or reduced to what "
            "the cited fact actually says. Do not argue with the verifier."
        )
    result = llm.complete_json(
        model=smart_model(),
        system=TAILOR_SYSTEM,
        prompt=prompt,
        schema=TAILOR_SCHEMA,
        max_tokens=8000,
    )
    return result.data, result.cost_cents


def _critique(report: ValidationReport, cover_finding: Any) -> str:
    lines: list[str] = []
    for finding in report.failures:
        for violation, detail in finding.violations:
            lines.append(f"- bullet {finding.index} ({violation.value}): {detail} :: {finding.text!r}")
    for violation, detail in getattr(cover_finding, "violations", []):
        lines.append(f"- cover letter ({violation.value}): {detail}")
    return "\n".join(lines)


def tailor_application(
    session: Session,
    application: Application,
    *,
    llm: LLMClient | None = None,
    store: ArtifactStore | None = None,
    max_attempts: int = 2,
) -> TailorResult:
    """Draft, verify, and render one application. Never returns unverified output."""
    llm = llm or get_llm()
    store = store or ArtifactStore()
    run = start_run(session, stage="tailor", application_id=application.id)

    job = session.get(Job, application.job_id)
    company = session.get(Company, job.company_id) if job else None
    profile = (
        session.query(Profile)
        .filter(Profile.user_id == application.user_id)
        .order_by(Profile.version.desc())
        .first()
    )

    if job is None or profile is None:
        finish_run(session, run, status="error")
        return TailorResult(ok=False, report={"error": "missing job or profile"})

    facts = retrieve_facts(session, profile, job)
    if not facts:
        log_event(session, run, event_type="tailor.no_facts", level="error", message="fact store is empty")
        finish_run(session, run, status="error")
        return TailorResult(ok=False, report={"error": "fact store is empty"})

    extra_allowed = _allowed_extra(profile, facts, job, company)
    total_cost = 0.0
    critique: str | None = None
    result = TailorResult(ok=False, fact_ids=[fact.id for fact in facts])

    for attempt in range(1, max_attempts + 1):
        result.attempts = attempt
        data, cost = generate(
            llm, profile=profile, job=job, company=company, facts=facts, critique=critique
        )
        total_cost += cost

        bullets = list(data.get("bullets", []))
        cover_letter = data.get("cover_letter", "")
        summary = data.get("summary", "")

        report = validate_bullets(bullets, facts, extra_allowed=extra_allowed)
        summary_finding = validate_prose(summary, facts, extra_allowed=extra_allowed, label="summary")
        cover_finding = validate_prose(
            cover_letter, facts, extra_allowed=extra_allowed, label="cover_letter"
        )

        payload = report.as_payload()
        payload["summary_violations"] = [v.value for v, _ in summary_finding.violations]
        payload["cover_letter_violations"] = [v.value for v, _ in cover_finding.violations]
        log_event(
            session,
            run,
            event_type="tailor.validated",
            level="info" if report.ok and cover_finding.ok and summary_finding.ok else "warn",
            message=f"attempt {attempt}: {len(report.failures)} bullet failures",
            payload=payload,
        )

        if report.ok and cover_finding.ok and summary_finding.ok:
            result = TailorResult(
                ok=True,
                summary=summary,
                bullets=bullets,
                cover_letter=cover_letter,
                fact_ids=[fact.id for fact in facts],
                cost_cents=total_cost,
                attempts=attempt,
                report=payload,
            )
            break

        critique = _critique(report, cover_finding) + "\n" + _critique(
            ValidationReport(findings=[summary_finding]), None
        )
        result.report = payload
        result.cost_cents = total_cost

    if not result.ok:
        # Fail twice -> a human decides. We never ship unverified claims.
        application.needs_input_json = {
            "reason": "validator_rejected",
            "detail": "Generated bullets introduced claims not traceable to the fact store.",
            "report": result.report,
        }
        transition(
            session,
            application,
            "drafted",
            run=run,
            message="draft produced but failed verification",
        )
        transition(
            session,
            application,
            "pending_review",
            run=run,
            message="routed to manual review: validator rejected two attempts",
        )
        finish_run(session, run, status="needs_review", cost_cents=total_cost)
        return result

    # Render and store artifacts.
    resume_pdf = render_resume_pdf(profile, job, company, result.summary, result.bullets, facts)
    cover_pdf = render_cover_letter_pdf(profile, job, company, result.cover_letter)
    resume_artifact = record_artifact(session, store, resume_pdf, kind="resume_pdf", suffix=".pdf")
    cover_artifact = record_artifact(session, store, cover_pdf, kind="cover_letter", suffix=".pdf")

    application.resume_artifact_id = resume_artifact.id
    application.cover_artifact_id = cover_artifact.id
    application.tailored_json = {
        "summary": result.summary,
        "bullets": result.bullets,
        "cover_letter": result.cover_letter,
        "fact_ids": result.fact_ids,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "attempts": result.attempts,
    }
    session.add(application)

    transition(session, application, "drafted", run=run, message="draft verified against fact store")
    transition(session, application, "pending_review", run=run, message="awaiting human approval")
    log_event(
        session,
        run,
        event_type="tailor.done",
        message=f"{len(result.bullets)} verified bullets in {result.attempts} attempt(s)",
        payload={"cost_cents": round(total_cost, 4), "resume_uri": resume_artifact.uri},
    )
    finish_run(session, run, cost_cents=total_cost)
    return result

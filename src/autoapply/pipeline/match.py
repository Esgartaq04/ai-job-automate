"""Stage 2 — Match.

Order matters and is the whole cost story: hard filters run *before* the LLM,
never after. Cheap rules kill most candidates for free; vector similarity gives
recall; one small-model call gives precision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..events import finish_run, log_event, start_run
from ..llm.client import LLMClient, fast_model, get_llm
from ..llm.embeddings import get_embedder
from ..models import Job, Match, Profile

# ---------------------------------------------------------------- hard filters


@dataclass
class Preferences:
    """Read from profiles.base_resume_json['preferences']."""

    needs_sponsorship: bool = False
    has_clearance: bool = False
    max_years_required: int = 5
    remote_ok: bool = True
    allowed_locations: list[str] = field(default_factory=list)
    excluded_companies: list[str] = field(default_factory=list)
    min_score: float = 0.6
    recall_k: int = 50

    @classmethod
    def from_profile(cls, profile: Profile) -> Preferences:
        raw = (profile.base_resume_json or {}).get("preferences", {})
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


_NO_SPONSORSHIP = re.compile(
    r"(no visa sponsorship|not (?:able|be able) to sponsor|unable to sponsor|"
    r"without (?:current |the need for )?sponsorship|do(?:es)? not (?:offer|provide) sponsorship|"
    r"sponsorship is not available)",
    re.I,
)
_CLEARANCE = re.compile(
    r"(security clearance|ts/sci|top secret|public trust clearance|polygraph)", re.I
)
_CLEARANCE_OPTIONAL = re.compile(r"(clearance (?:is )?(?:a )?(?:plus|preferred|nice to have))", re.I)
_YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:years?|yrs?)[^.\n]{0,40}experience", re.I)
_ONSITE = re.compile(r"\b(on[- ]?site|in[- ]office|hybrid)\b", re.I)


def required_years(description: str) -> int | None:
    """Smallest explicit "N years experience" requirement in the posting."""
    matches = [int(m.group(1)) for m in _YEARS.finditer(description or "")]
    return min(matches) if matches else None


def hard_filter(job: Job, prefs: Preferences, company_name: str = "") -> list[str]:
    """Return blocker strings. Non-empty means: never spend a model call on this job."""
    blockers: list[str] = []
    description = job.description or ""

    if company_name and any(
        excluded.lower() in company_name.lower() for excluded in prefs.excluded_companies
    ):
        blockers.append(f"company excluded by preference: {company_name}")

    if prefs.needs_sponsorship and _NO_SPONSORSHIP.search(description):
        blockers.append("posting states sponsorship is unavailable")

    if not prefs.has_clearance and _CLEARANCE.search(description) and not _CLEARANCE_OPTIONAL.search(description):
        blockers.append("requires a security clearance")

    years = required_years(description)
    if years is not None and years > prefs.max_years_required:
        blockers.append(f"requires {years}+ years, preference caps at {prefs.max_years_required}")

    if prefs.allowed_locations:
        location = (job.location or "").lower()
        remote_ok = prefs.remote_ok and (job.remote or "remote" in location)
        matches_location = any(allowed.lower() in location for allowed in prefs.allowed_locations)
        if not remote_ok and not matches_location:
            blockers.append(f"location {job.location!r} outside allowed set")
        elif job.remote and _ONSITE.search(description) and not matches_location and not prefs.remote_ok:
            blockers.append("posting is on-site/hybrid despite a remote label")

    return blockers


# ---------------------------------------------------------------- LLM precision

SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "number", "description": "0..1 fit score."},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "reasons", "blockers"],
    "additionalProperties": False,
}

_SCORE_SYSTEM = """You score how well a candidate fits one job posting.

You get a candidate summary (only verified facts) and a job description.

Return:
- score: 0..1. Calibrate honestly. 0.5 means "plausible but unremarkable".
- reasons: concrete overlaps between the candidate's actual experience and the role.
- blockers: hard requirements the candidate demonstrably does not meet.

Judge only against what the candidate summary states. Do not assume unstated
experience, and do not credit the candidate for skills that merely appear in the
job description."""


def candidate_summary(profile: Profile, facts: list[Any]) -> str:
    resume = profile.base_resume_json or {}
    lines = [f"Headline: {resume.get('headline', 'n/a')}"]
    for fact in facts[:40]:
        where = " @ ".join(x for x in [fact.role, fact.org] if x)
        lines.append(f"- [{fact.id}] {where}: {fact.text}")
    return "\n".join(lines)


@dataclass
class MatchOutcome:
    job_id: Any
    score: float
    similarity: float
    reasons: list[str]
    blockers: list[str]
    cost_cents: float = 0.0


def score_job(
    llm: LLMClient, *, summary: str, job: Job, company_name: str
) -> tuple[float, list[str], list[str], float]:
    prompt = (
        f"CANDIDATE\n{summary}\n\n"
        f"JOB\nCompany: {company_name}\nTitle: {job.title}\n"
        f"Location: {job.location or 'n/a'}\n\n{(job.description or '')[:12000]}"
    )
    result = llm.complete_json(
        model=fast_model(),
        system=_SCORE_SYSTEM,
        prompt=prompt,
        schema=SCORE_SCHEMA,
        max_tokens=1024,
    )
    data = result.data
    return (
        float(data.get("score", 0.0)),
        list(data.get("reasons", [])),
        list(data.get("blockers", [])),
        result.cost_cents,
    )


def recall_candidates(session: Session, profile: Profile, limit: int) -> list[tuple[Job, float, str]]:
    """Vector recall over unscored jobs. Returns (job, cosine_similarity, company_name)."""
    if profile.embedding is None:
        rows = session.execute(
            select(Job, text("0.0"), text("companies.name"))
            .join(text("companies"), text("companies.id = jobs.company_id"))
            .outerjoin(Match, (Match.job_id == Job.id) & (Match.profile_id == profile.id))
            .where(Match.id.is_(None))
            .limit(limit)
        ).all()
        return [(row[0], 0.0, row[2]) for row in rows]

    sql = text(
        """
        SELECT j.id AS job_id,
               1 - (j.embedding <=> CAST(:vec AS vector)) AS similarity,
               c.name AS company_name
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        LEFT JOIN matches m ON m.job_id = j.id AND m.profile_id = :profile_id
        WHERE m.id IS NULL AND j.embedding IS NOT NULL
        ORDER BY j.embedding <=> CAST(:vec AS vector)
        LIMIT :limit
        """
    )
    rows = session.execute(
        sql,
        {
            "vec": str(list(profile.embedding)),
            "profile_id": profile.id,
            "limit": limit,
        },
    ).all()
    out: list[tuple[Job, float, str]] = []
    for row in rows:
        job = session.get(Job, row.job_id)
        if job is not None:
            out.append((job, float(row.similarity), row.company_name))
    return out


def match_profile(
    session: Session, profile: Profile, *, llm: LLMClient | None = None, limit: int | None = None
) -> list[MatchOutcome]:
    """Score every unscored job for one profile. Writes a `matches` row per job."""
    llm = llm or get_llm()
    prefs = Preferences.from_profile(profile)
    run = start_run(session, stage="match")
    facts = list(profile.facts)
    summary = candidate_summary(profile, facts)

    if profile.embedding is None and facts:
        profile.embedding = get_embedder().embed(summary)
        session.add(profile)
        session.flush()

    outcomes: list[MatchOutcome] = []
    total_cost = 0.0
    filtered = 0

    for job, similarity, company_name in recall_candidates(session, profile, limit or prefs.recall_k):
        blockers = hard_filter(job, prefs, company_name)
        if blockers:
            filtered += 1
            outcome = MatchOutcome(job.id, 0.0, similarity, [], blockers)
        else:
            score, reasons, llm_blockers, cost = score_job(
                llm, summary=summary, job=job, company_name=company_name
            )
            total_cost += cost
            outcome = MatchOutcome(job.id, score, similarity, reasons, llm_blockers, cost)

        session.merge(
            Match(
                job_id=job.id,
                profile_id=profile.id,
                score=outcome.score,
                similarity=outcome.similarity,
                reasons_json=outcome.reasons,
                blockers_json=outcome.blockers,
            )
        )
        outcomes.append(outcome)

    log_event(
        session,
        run,
        event_type="match.done",
        message=f"scored {len(outcomes)} jobs, {filtered} killed by hard filters before any model call",
        payload={"scored": len(outcomes), "hard_filtered": filtered, "cost_cents": round(total_cost, 4)},
    )
    finish_run(session, run, cost_cents=total_cost)
    return outcomes

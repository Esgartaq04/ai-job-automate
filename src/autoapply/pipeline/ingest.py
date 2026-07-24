"""Stage 1 — Ingest.

Pull postings from public ATS board APIs, canonicalize, dedup, embed once per
content hash. Nothing here scrapes an aggregator.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ats import JobPosting, get_adapter
from ..config import get_settings
from ..events import log_event, start_run
from ..llm.embeddings import get_embedder
from ..models import Company, Job
from ..ratelimit import CircuitBreaker, TokenBucket

log = logging.getLogger(__name__)

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")


def normalize(value: str | None) -> str:
    if not value:
        return ""
    return _WS.sub(" ", _PUNCT.sub(" ", value.lower())).strip()


def canonical_hash(company: str, title: str, location: str | None, ats_job_id: str) -> str:
    """Stable identity for a posting across sources. Unique index enforces dedup."""
    key = "|".join([normalize(company), normalize(title), normalize(location), ats_job_id.strip()])
    return hashlib.sha256(key.encode()).hexdigest()


def content_hash(posting: JobPosting) -> str:
    """Embedding cache key — recompute only when the text actually changed."""
    key = f"{posting.title}\n{posting.location or ''}\n{posting.description}"
    return hashlib.sha256(key.strip().lower().encode()).hexdigest()


@dataclass
class IngestReport:
    fetched: int = 0
    inserted: int = 0
    duplicates: int = 0
    updated: int = 0
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        self.errors = self.errors or []


def ingest_company(session: Session, company: Company, *, bucket: TokenBucket | None = None) -> IngestReport:
    """Fetch and persist one company's board. Idempotent: re-running is a no-op."""
    settings = get_settings()
    bucket = bucket or TokenBucket(settings.ats_rate_per_min)
    embedder = get_embedder()
    adapter = get_adapter(company.ats_type)
    report = IngestReport()

    bucket.acquire()
    postings = adapter.fetch_jobs(company.board_token)
    report.fetched = len(postings)

    for posting in postings:
        digest = canonical_hash(company.name, posting.title, posting.location, posting.ats_job_id)
        existing = session.scalar(select(Job).where(Job.canonical_hash == digest))
        new_content = content_hash(posting)

        if existing is not None:
            report.duplicates += 1
            if existing.content_hash != new_content:
                existing.description = posting.description
                existing.title = posting.title
                existing.location = posting.location
                existing.content_hash = new_content
                existing.embedding = embedder.embed(_embed_text(posting))
                session.add(existing)
                report.updated += 1
            continue

        session.add(
            Job(
                company_id=company.id,
                ats_job_id=posting.ats_job_id,
                title=posting.title,
                location=posting.location,
                description=posting.description,
                apply_url=posting.apply_url,
                remote=posting.remote,
                canonical_hash=digest,
                content_hash=new_content,
                posted_at=posting.posted_at,
                embedding=embedder.embed(_embed_text(posting)),
            )
        )
        report.inserted += 1

    session.flush()
    return report


def _embed_text(posting: JobPosting) -> str:
    return f"{posting.title}\n{posting.location or ''}\n{posting.description}"


def ingest_all(session: Session, *, breaker: CircuitBreaker | None = None) -> IngestReport:
    """Walk every active company. One bad board trips its own breaker, not the run."""
    settings = get_settings()
    bucket = TokenBucket(settings.ats_rate_per_min)
    breaker = breaker or CircuitBreaker(failure_threshold=settings.circuit_failure_threshold)
    run = start_run(session, stage="ingest")
    total = IngestReport()

    companies = session.scalars(select(Company).where(Company.active.is_(True))).all()
    for company in companies:
        try:
            breaker.check()
            report = ingest_company(session, company, bucket=bucket)
            breaker.record_success()
        except Exception as exc:  # noqa: BLE001 - one board must not kill the sweep
            breaker.record_failure()
            message = f"{company.name}: {type(exc).__name__}: {exc}"
            total.errors.append(message)  # type: ignore[union-attr]
            log_event(session, run, event_type="ingest.error", level="error", message=message)
            log.warning("ingest failed for %s", company.name, exc_info=True)
            continue

        total.fetched += report.fetched
        total.inserted += report.inserted
        total.duplicates += report.duplicates
        total.updated += report.updated
        log_event(
            session,
            run,
            event_type="ingest.company",
            message=f"{company.name}: +{report.inserted} new / {report.fetched} fetched",
            payload={
                "company": company.name,
                "fetched": report.fetched,
                "inserted": report.inserted,
                "updated": report.updated,
            },
        )

    log_event(
        session,
        run,
        event_type="ingest.done",
        message=f"{total.inserted} new postings from {len(companies)} boards",
        payload={"inserted": total.inserted, "fetched": total.fetched, "errors": total.errors},
    )
    from ..events import finish_run

    finish_run(session, run, status="error" if total.errors else "ok")
    return total

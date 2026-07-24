"""The per-ATS adapter contract (README section 4)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class FieldKind(StrEnum):
    TEXT = "text"
    EMAIL = "email"
    PHONE = "phone"
    URL = "url"
    TEXTAREA = "textarea"
    SELECT = "select"
    CHECKBOX = "checkbox"
    FILE = "file"
    UNKNOWN = "unknown"


@dataclass
class Field:
    """One form control, plus what we think it wants and how sure we are."""

    selector: str
    kind: FieldKind
    label: str
    name: str | None = None
    required: bool = False
    options: list[str] = field(default_factory=list)
    # Canonical profile key this maps to, e.g. "first_name", "resume", "work_authorized".
    canonical: str | None = None
    confidence: float = 0.0
    source: str = "heuristic"  # heuristic | llm | cache

    @property
    def signature(self) -> str:
        """Stable identity for the field-mapping cache: (ats, field_signature)."""
        return f"{self.kind.value}|{(self.name or '').lower()}|{self.label.strip().lower()[:120]}"


class EscalationReason(StrEnum):
    CAPTCHA = "captcha"
    TWO_FACTOR = "two_factor"
    ACCOUNT_REQUIRED = "account_required"
    LOW_CONFIDENCE_FIELD = "low_confidence_field"
    LONG_FREE_TEXT = "long_free_text"
    UNMAPPED_REQUIRED_FIELD = "unmapped_required_field"


class Escalation(Exception):
    """Pause the workflow and ask the human. Never guess past one of these."""

    def __init__(self, reason: EscalationReason, detail: str, questions: list[dict[str, Any]] | None = None):
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail
        self.questions = questions or []

    def to_payload(self) -> dict[str, Any]:
        return {"reason": self.reason.value, "detail": self.detail, "questions": self.questions}


@dataclass
class SubmitResult:
    submitted: bool
    verified: bool
    detail: str = ""
    confirmation_text: str | None = None
    final_url: str | None = None
    screenshot: bytes | None = None
    dom_snapshot: bytes | None = None


@dataclass
class JobPosting:
    """Normalized posting, independent of which board it came from."""

    ats_job_id: str
    title: str
    location: str | None
    description: str
    apply_url: str
    remote: bool = False
    posted_at: dt.datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ATSAdapter(Protocol):
    ats_type: str

    def detect(self, url: str) -> bool: ...

    def fetch_jobs(self, board_token: str) -> list[JobPosting]: ...

    def map_fields(self, page: Any) -> dict[str, Field]: ...

    def fill(self, page: Any, application: Any) -> None: ...

    def submit(self, page: Any) -> SubmitResult: ...

    def verify(self, page: Any) -> bool: ...


_REGISTRY: dict[str, ATSAdapter] = {}


def register(adapter: ATSAdapter) -> ATSAdapter:
    _REGISTRY[adapter.ats_type] = adapter
    return adapter


def get_adapter(ats_type: str) -> ATSAdapter:
    try:
        return _REGISTRY[ats_type]
    except KeyError:
        raise LookupError(f"no adapter registered for ats_type={ats_type!r}") from None


def adapter_for_url(url: str) -> ATSAdapter | None:
    for adapter in _REGISTRY.values():
        if adapter.detect(url):
            return adapter
    return None


def registered_types() -> list[str]:
    return sorted(_REGISTRY)

"""ATS adapters. Importing this package registers every adapter."""

from . import ashby, greenhouse, lever  # noqa: F401  - import registers the adapter
from .base import (
    ATSAdapter,
    Escalation,
    EscalationReason,
    Field,
    FieldKind,
    JobPosting,
    SubmitResult,
    adapter_for_url,
    get_adapter,
    registered_types,
)
from .fields import FieldMapper, heuristic_map
from .form import FormDrivingAdapter
from .textutil import strip_html

__all__ = [
    "ATSAdapter",
    "Escalation",
    "EscalationReason",
    "Field",
    "FieldKind",
    "FieldMapper",
    "FormDrivingAdapter",
    "JobPosting",
    "SubmitResult",
    "adapter_for_url",
    "get_adapter",
    "heuristic_map",
    "registered_types",
    "strip_html",
]

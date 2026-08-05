"""Field mapping: heuristics first, cached LLM fallback second.

README section 4. The synonym dictionary handles the ~90% of ATS fields that are
some spelling of "first name"; anything it can't place goes to the model once,
and the answer is cached per (ats, field_signature) forever after.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from ..llm.client import LLMClient, LLMResult, fast_model, get_llm
from ..models import FieldMapCache
from .base import Field, FieldKind

# canonical key -> phrases that identify it. Order matters only for readability;
# scoring below picks the longest match, so "first name" beats "name".
SYNONYMS: dict[str, list[str]] = {
    "first_name": ["first name", "given name", "forename", "firstname", "first"],
    "last_name": ["last name", "surname", "family name", "lastname", "last"],
    "full_name": ["full name", "your name", "name"],
    "email": ["email", "e-mail", "email address"],
    "phone": ["phone", "telephone", "mobile", "phone number", "cell"],
    "resume": ["resume", "cv", "resume/cv", "attach resume", "upload resume"],
    "cover_letter": ["cover letter", "coverletter", "letter of interest"],
    "linkedin": ["linkedin", "linkedin profile", "linkedin url"],
    "github": ["github", "github profile", "git hub"],
    "portfolio": ["portfolio", "website", "personal site", "personal website"],
    "location": ["location", "city", "where are you based", "current location"],
    "work_authorized": [
        "authorized to work",
        "legally authorized",
        "work authorization",
        "eligible to work",
    ],
    "requires_sponsorship": [
        "require sponsorship",
        "need sponsorship",
        "visa sponsorship",
        "sponsorship now or in the future",
    ],
    "start_date": ["start date", "available start", "earliest start"],
    "salary_expectation": ["salary", "compensation expectation", "desired salary", "pay expectation"],
    "years_experience": ["years of experience", "yoe", "years experience"],
    "how_did_you_hear": ["how did you hear", "referral source", "where did you hear"],
    "pronouns": ["pronouns"],
    "gender": ["gender"],
    "race": ["race", "ethnicity", "hispanic or latino"],
    "veteran_status": ["veteran", "protected veteran"],
    "disability_status": ["disability", "disabled"],
}

# Demographic/EEO fields. We never auto-answer these; declining is always allowed
# and guessing on someone's behalf is not ours to do.
EEO_FIELDS = {"gender", "race", "veteran_status", "disability_status", "pronouns"}

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS.sub(" ", text.replace("_", " ").replace("*", " ").strip().lower())


def heuristic_map(field: Field) -> tuple[str | None, float]:
    """Match a field to a canonical key from its label, name and aria attributes.

    Returns (canonical, confidence). Confidence reflects how the match was made:
    an exact label match is worth more than a substring hit on the `name` attribute.
    """
    label = normalize(field.label)
    name = normalize(field.name or "")
    haystacks = [(label, 1.0), (name, 0.85)]

    best: tuple[str | None, float, int] = (None, 0.0, 0)
    for canonical, phrases in SYNONYMS.items():
        for phrase in phrases:
            for hay, weight in haystacks:
                if not hay:
                    continue
                if hay == phrase:
                    score, length = 0.97 * weight, len(phrase)
                elif re.search(rf"\b{re.escape(phrase)}\b", hay):
                    score, length = 0.88 * weight, len(phrase)
                elif phrase in hay:
                    score, length = 0.75 * weight, len(phrase)
                else:
                    continue
                # Prefer the longer phrase at equal-ish score: "first name" over "name".
                if (length, score) > (best[2], best[1]):
                    best = (canonical, score, length)

    canonical, confidence, _ = best

    # A file input is a resume unless the label says cover letter.
    if field.kind is FieldKind.FILE and canonical is None:
        canonical, confidence = "resume", 0.6

    # Yes/no selects about sponsorship are frequently phrased as negations.
    if canonical == "requires_sponsorship" and "will not" in label:
        confidence = min(confidence, 0.6)

    return canonical, round(confidence, 3)


FIELD_MAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "canonical": {
            "type": "string",
            "description": "Canonical profile key, or 'unknown' if none applies.",
            "enum": [*SYNONYMS.keys(), "unknown"],
        },
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["canonical", "confidence", "reason"],
    "additionalProperties": False,
}

_FIELD_MAP_SYSTEM = """You map job-application form fields to canonical profile keys.

You are given one form field's label, HTML name attribute, input kind and any
options. Answer with the canonical key it is asking for, or "unknown".

Rules:
- Answer only about what the field is asking for. Do not invent an answer to it.
- If the label is ambiguous between two keys, return "unknown" with low confidence.
  A human will resolve it; a wrong guess puts wrong data on a real application.
- confidence is 0..1 and should be honest, not encouraging."""


class FieldMapper:
    """Heuristics, then cache, then model. Every model answer is written back."""

    def __init__(self, ats_type: str, *, llm: LLMClient | None = None, confidence_floor: float = 0.7):
        self.ats_type = ats_type
        self.llm = llm or get_llm()
        self.confidence_floor = confidence_floor
        self.cost_cents = 0.0

    def resolve(self, session: Session, field: Field) -> Field:
        canonical, confidence = heuristic_map(field)
        if canonical and confidence >= self.confidence_floor:
            field.canonical, field.confidence, field.source = canonical, confidence, "heuristic"
            return field

        cached = session.get(FieldMapCache, (self.ats_type, field.signature))
        if cached is not None:
            field.canonical = cached.canonical
            field.confidence = cached.confidence
            field.source = "cache"
            return field

        result = self._ask_model(field)
        answer = result.data
        self.cost_cents += result.cost_cents
        model_canonical = answer.get("canonical")
        if model_canonical == "unknown":
            model_canonical = None
        model_confidence = float(answer.get("confidence", 0.0))

        session.merge(
            FieldMapCache(
                ats_type=self.ats_type,
                signature=field.signature,
                canonical=model_canonical,
                confidence=model_confidence,
            )
        )
        field.canonical, field.confidence, field.source = model_canonical, model_confidence, "llm"
        return field

    def _ask_model(self, field: Field) -> LLMResult:
        prompt = (
            f"ATS: {self.ats_type}\n"
            f"Label: {field.label!r}\n"
            f"Name attribute: {field.name!r}\n"
            f"Input kind: {field.kind.value}\n"
            f"Required: {field.required}\n"
            f"Options: {field.options or 'n/a'}"
        )
        return self.llm.complete_json(
            model=fast_model(),
            system=_FIELD_MAP_SYSTEM,
            prompt=prompt,
            schema=FIELD_MAP_SCHEMA,
            max_tokens=512,
        )

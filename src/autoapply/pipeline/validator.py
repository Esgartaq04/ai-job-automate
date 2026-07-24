"""The deterministic half of grounded generation.

The model proposes; this layer verifies; a human adjudicates the ambiguous middle.
A bullet is valid only if it cites a real fact and introduces no entity, number or
technology that isn't already in that fact. This is the difference between a
tailored resume and a fabricated one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from ..models import Fact


class Violation(StrEnum):
    MISSING_CITATION = "missing_citation"
    UNKNOWN_FACT_ID = "unknown_fact_id"
    NEW_NUMBER = "new_number"
    NEW_ENTITY = "new_entity"
    EMPTY = "empty"


# Words that legitimately appear capitalized without being a claim about the world.
_STOPWORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of", "on", "or",
    "the", "to", "with", "via", "per", "across", "through", "using", "used", "use",
    "built", "build", "led", "lead", "owned", "own", "shipped", "ship", "designed",
    "design", "developed", "develop", "implemented", "implement", "improved", "improve",
    "reduced", "reduce", "increased", "increase", "created", "create", "drove", "drive",
    "wrote", "write", "maintained", "maintain", "delivered", "deliver", "scaled", "scale",
    "migrated", "migrate", "automated", "automate", "launched", "launch", "managed",
    "manage", "supported", "support", "tested", "test", "refactored", "refactor",
    "collaborated", "partnered", "team", "teams", "service", "services", "system",
    "systems", "pipeline", "pipelines", "data", "code", "codebase", "product", "feature",
    "features", "customer", "customers", "user", "users", "engineer", "engineering",
    "software", "cross", "functional", "end", "new", "that", "which", "while", "after",
    "before", "over", "under", "than", "then", "was", "were", "is", "are", "be", "been",
    "it", "its", "their", "our", "my", "this", "these", "those", "also", "more", "most",
    # Pronouns and connective prose — a cover letter is written in first person and
    # "I" must not read as an invented entity.
    "i", "we", "me", "us", "you", "your", "yours", "am", "have", "has", "had", "do",
    "does", "did", "will", "would", "can", "could", "should", "not", "no", "if", "so",
    "when", "where", "who", "what", "how", "why", "there", "here", "about", "out",
    "up", "down", "all", "any", "some", "both", "each", "such", "role", "position",
    "job", "company", "work", "working", "experience", "years", "year", "months",
    "month", "week", "day", "days", "time", "now", "currently", "recently", "well",
    "very", "much", "many", "same", "other", "others", "between", "against",
    "during", "since", "until", "because", "but", "however", "still", "just", "only",
    # Month names — dates in prose, not claims about the world.
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
}

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#./_-]*")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _norm_token(token: str) -> str:
    return token.strip(".,;:()[]{}\"'").lower()


def vocabulary(fact: Fact) -> set[str]:
    """Everything a bullet derived from this fact is allowed to say."""
    parts: list[str] = [fact.text or "", fact.org or "", fact.role or "", fact.type or ""]
    parts.extend(fact.skills or [])
    vocab: set[str] = set()
    for part in parts:
        for token in _TOKEN.findall(part):
            normalized = _norm_token(token)
            if normalized:
                vocab.add(normalized)
                # Allow trivial morphology: "pipeline" covers "pipelines".
                vocab.add(normalized.rstrip("s"))
    return vocab


def numbers_in(value: str) -> set[str]:
    return {match.group(0).replace(",", "") for match in _NUMBER.finditer(value or "")}


@dataclass
class BulletFinding:
    index: int
    text: str
    fact_id: str | None
    violations: list[tuple[Violation, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


@dataclass
class ValidationReport:
    findings: list[BulletFinding]

    @property
    def ok(self) -> bool:
        return all(finding.ok for finding in self.findings)

    @property
    def failures(self) -> list[BulletFinding]:
        return [finding for finding in self.findings if not finding.ok]

    def as_payload(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "bullets": len(self.findings),
            "failures": [
                {
                    "index": finding.index,
                    "text": finding.text,
                    "fact_id": finding.fact_id,
                    "violations": [
                        {"type": violation.value, "detail": detail}
                        for violation, detail in finding.violations
                    ],
                }
                for finding in self.failures
            ],
        }


def validate_bullets(
    bullets: Iterable[dict[str, str]], facts: Iterable[Fact], *, extra_allowed: set[str] | None = None
) -> ValidationReport:
    """Check each generated bullet against the fact it claims to derive from.

    `bullets` items are {"text": ..., "fact_id": ...}. `extra_allowed` widens the
    vocabulary with globally-known tokens (the candidate's own name, every skill
    in the fact store) so cross-referencing a real skill isn't flagged.
    """
    by_id = {fact.id: fact for fact in facts}
    allowed_global = {_norm_token(token) for token in (extra_allowed or set())}
    findings: list[BulletFinding] = []

    for index, bullet in enumerate(bullets):
        raw_text = (bullet.get("text") or "").strip()
        fact_id = (bullet.get("fact_id") or "").strip() or None
        finding = BulletFinding(index=index, text=raw_text, fact_id=fact_id)

        if not raw_text:
            finding.violations.append((Violation.EMPTY, "bullet is empty"))
            findings.append(finding)
            continue
        if not fact_id:
            finding.violations.append((Violation.MISSING_CITATION, "bullet cites no fact_id"))
            findings.append(finding)
            continue

        fact = by_id.get(fact_id)
        if fact is None:
            finding.violations.append(
                (Violation.UNKNOWN_FACT_ID, f"fact_id {fact_id!r} is not in the retrieved set")
            )
            findings.append(finding)
            continue

        source_numbers = numbers_in(fact.text) | numbers_in(fact.start_date or "") | numbers_in(
            fact.end_date or ""
        )
        for number in numbers_in(raw_text):
            if number not in source_numbers:
                finding.violations.append(
                    (Violation.NEW_NUMBER, f"{number!r} does not appear in fact {fact_id}")
                )

        allowed = vocabulary(fact) | allowed_global | _STOPWORDS
        for token in _TOKEN.findall(raw_text):
            normalized = _norm_token(token)
            if len(normalized) < 2 or normalized in allowed or normalized.rstrip("s") in allowed:
                continue
            # Only flag things that look like claims about the world: proper nouns,
            # acronyms, and versioned technology names. Lowercase prose is the
            # model's to rephrase.
            looks_like_entity = token[0].isupper() or token.isupper() or any(c.isdigit() for c in token)
            if looks_like_entity:
                finding.violations.append(
                    (Violation.NEW_ENTITY, f"{token!r} is not present in fact {fact_id}")
                )

        findings.append(finding)

    return ValidationReport(findings=findings)


def validate_prose(
    prose: str, facts: Iterable[Fact], *, extra_allowed: set[str] | None = None, label: str = "prose"
) -> BulletFinding:
    """Same entity/number check for free-form text (cover letters).

    The allowed vocabulary is the union of every retrieved fact, plus whatever the
    caller whitelists (company name, job title). A cover letter is allowed to talk
    about the employer; it is not allowed to invent the candidate's history.
    """
    facts = list(facts)
    finding = BulletFinding(index=0, text=(prose or "").strip(), fact_id=label)
    if not finding.text:
        finding.violations.append((Violation.EMPTY, f"{label} is empty"))
        return finding

    allowed: set[str] = set(_STOPWORDS) | {_norm_token(t) for t in (extra_allowed or set())}
    source_numbers: set[str] = set()
    for fact in facts:
        allowed |= vocabulary(fact)
        source_numbers |= numbers_in(fact.text)

    for number in numbers_in(finding.text):
        if number not in source_numbers:
            finding.violations.append(
                (Violation.NEW_NUMBER, f"{number!r} is not in any retrieved fact")
            )

    for token in _TOKEN.findall(finding.text):
        normalized = _norm_token(token)
        if len(normalized) < 2 or normalized in allowed or normalized.rstrip("s") in allowed:
            continue
        if token[0].isupper() or token.isupper() or any(c.isdigit() for c in token):
            finding.violations.append((Violation.NEW_ENTITY, f"{token!r} is not in any retrieved fact"))

    return finding

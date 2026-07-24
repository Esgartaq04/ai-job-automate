"""Log scrubber. Redacts on write, not on read — once a secret is in the row it's leaked."""

from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEYS = {
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "cookie",
    "ssn",
    "date_of_birth",
    "dob",
    "ciphertext",
    "access_token",
    "refresh_token",
}

REDACTED = "[REDACTED]"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # API keys / bearer tokens
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), REDACTED),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}\b", re.I), f"Bearer {REDACTED}"),
    # Email addresses: keep the domain, drop the local part — useful for debugging
    # which ATS mailbox we hit without storing the address.
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b"), rf"{REDACTED}@\1"),
    # US-format phone numbers
    (re.compile(r"\b(?:\+?1[\s.\-])?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b"), REDACTED),
    # Long digit runs (SSN-ish, account numbers)
    (re.compile(r"\b\d{9,}\b"), REDACTED),
]


def scrub_text(value: str) -> str:
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def scrub(value: Any) -> Any:
    """Recursively redact secrets from anything headed for persistent storage."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.strip().lower() in SENSITIVE_KEYS:
                out[key] = REDACTED
            else:
                out[key] = scrub(item)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    return value

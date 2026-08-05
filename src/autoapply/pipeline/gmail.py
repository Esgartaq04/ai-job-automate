"""Gmail transport for stage 5.

The classification and matching logic lives in `outcome.py` and takes plain
`Message` objects. This module only moves bytes: OAuth, list, fetch, decode.

Design note: `parse_message` is a pure function over a Gmail API message
resource, so the MIME handling — nested multiparts, base64url without padding,
HTML-only mail — is testable without credentials or network.

Scope is read-only (`gmail.readonly`). This never sends, deletes, or modifies
mail; idempotency comes from the `processed_messages` table, not from mutating
the mailbox.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..ats.textutil import strip_html
from .outcome import Message

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Application mail arrives at the dedicated alias. Restricting the query keeps
# the classifier's bill proportional to real volume instead of whole-inbox size.
DEFAULT_QUERY = "newer_than:14d -category:promotions"


def _decode(data: str | None) -> str:
    if not data:
        return ""
    # Gmail uses base64url and strips padding.
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - a single undecodable part must not kill the run
        log.debug("could not decode message part", exc_info=True)
        return ""


def _walk(part: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield part
    for child in part.get("parts") or []:
        yield from _walk(child)


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {
        (h.get("name") or "").lower(): h.get("value") or ""
        for h in payload.get("headers") or []
    }


def extract_body(payload: dict[str, Any]) -> str:
    """Prefer text/plain; fall back to flattened HTML.

    Marketing-heavy ATS notifications are frequently HTML-only, so the fallback
    is the common path rather than an edge case.
    """
    plain: list[str] = []
    html: list[str] = []
    for part in _walk(payload):
        mime = (part.get("mimeType") or "").lower()
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        if mime == "text/plain":
            plain.append(_decode(data))
        elif mime == "text/html":
            html.append(_decode(data))

    if plain:
        return "\n".join(t for t in plain if t.strip()).strip()
    if html:
        return strip_html("\n".join(html))
    return _decode((payload.get("body") or {}).get("data"))


def parse_message(resource: dict[str, Any]) -> Message:
    """Gmail API message resource -> the `Message` the classifier expects."""
    payload = resource.get("payload") or {}
    headers = _headers(payload)

    received_at = None
    internal = resource.get("internalDate")
    if internal:
        try:
            received_at = dt.datetime.fromtimestamp(int(internal) / 1000, tz=dt.UTC)
        except (TypeError, ValueError):
            received_at = None

    return Message(
        message_id=str(resource.get("id", "")),
        subject=headers.get("subject", ""),
        sender=headers.get("from", ""),
        body=extract_body(payload),
        received_at=received_at,
    )


class GmailSource:
    """Read-only Gmail client. Google libraries are imported lazily so the rest
    of the system installs and tests without them."""

    def __init__(
        self,
        *,
        credentials_path: str | Path,
        token_path: str | Path,
        query: str = DEFAULT_QUERY,
        max_results: int = 100,
    ):
        self.credentials_path = Path(credentials_path)
        self.token_path = Path(token_path)
        self.query = query
        self.max_results = max_results

    def _credentials(self) -> Any:
        from google.auth.transport.requests import Request  # type: ignore[import-untyped]
        from google.oauth2.credentials import Credentials  # type: ignore[import-untyped]
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

        creds = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not self.credentials_path.exists():
                raise FileNotFoundError(
                    f"OAuth client secrets not found at {self.credentials_path}. "
                    "Create a Desktop app OAuth client in Google Cloud Console, enable "
                    "the Gmail API, and download the JSON."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), SCOPES)
            # Opens a browser once; the refresh token is cached thereafter.
            creds = flow.run_local_server(port=0)

        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json())
        self.token_path.chmod(0o600)
        return creds

    def service(self) -> Any:
        from googleapiclient.discovery import build  # type: ignore[import-untyped]

        return build("gmail", "v1", credentials=self._credentials(), cache_discovery=False)

    def fetch(self, *, query: str | None = None) -> list[Message]:
        """List matching messages and return them fully decoded."""
        service = self.service()
        messages = service.users().messages()

        listed = messages.list(
            userId="me", q=query or self.query, maxResults=self.max_results
        ).execute()

        out: list[Message] = []
        for stub in listed.get("messages", []):
            resource = messages.get(userId="me", id=stub["id"], format="full").execute()
            out.append(parse_message(resource))
        log.info("fetched %d messages from gmail", len(out))
        return out

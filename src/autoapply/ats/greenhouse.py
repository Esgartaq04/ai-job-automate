"""Greenhouse adapter.

Ingest uses the public board API (no scraping, no bot defenses). Submit drives the
public application form with Playwright, mapping controls by label rather than by
hard-coded selectors so a board-template change degrades to an escalation instead
of silently filling the wrong box.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from typing import Any

import httpx

from .base import (
    ATSAdapter,
    Escalation,
    EscalationReason,
    Field,
    FieldKind,
    JobPosting,
    SubmitResult,
    register,
)

BOARD_API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"

_TAG_RE = re.compile(r"<[^>]+>")
_REMOTE_RE = re.compile(r"\bremote\b", re.I)

CAPTCHA_MARKERS = [
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "div.g-recaptcha",
    "[data-sitekey]",
    "#cf-challenge-running",
]
CONFIRMATION_MARKERS = [
    "text=Thank you for applying",
    "text=Your application has been submitted",
    "text=Application submitted",
    "#application_confirmation",
]

_INPUT_KINDS: dict[str, FieldKind] = {
    "email": FieldKind.EMAIL,
    "tel": FieldKind.PHONE,
    "url": FieldKind.URL,
    "file": FieldKind.FILE,
    "checkbox": FieldKind.CHECKBOX,
    "text": FieldKind.TEXT,
}


def strip_html(raw: str) -> str:
    text = html.unescape(raw or "")
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


class GreenhouseAdapter:
    ats_type = "greenhouse"

    # ---------- ingest ----------

    def detect(self, url: str) -> bool:
        return "greenhouse.io" in (url or "").lower()

    def fetch_jobs(self, board_token: str, *, client: httpx.Client | None = None) -> list[JobPosting]:
        owns_client = client is None
        client = client or httpx.Client(timeout=30.0, headers={"User-Agent": "autoapply/0.1"})
        try:
            response = client.get(BOARD_API.format(token=board_token), params={"content": "true"})
            response.raise_for_status()
            payload = response.json()
        finally:
            if owns_client:
                client.close()
        return [self.parse_job(job) for job in payload.get("jobs", [])]

    def parse_job(self, raw: dict[str, Any]) -> JobPosting:
        location = (raw.get("location") or {}).get("name")
        description = strip_html(raw.get("content", ""))
        posted_at = None
        stamp = raw.get("updated_at") or raw.get("first_published")
        if stamp:
            try:
                posted_at = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                posted_at = None
        return JobPosting(
            ats_job_id=str(raw.get("id")),
            title=raw.get("title", "").strip(),
            location=location,
            description=description,
            apply_url=raw.get("absolute_url", ""),
            remote=bool(location and _REMOTE_RE.search(location)),
            posted_at=posted_at,
            raw=raw,
        )

    # ---------- submit ----------

    def guard(self, page: Any) -> None:
        """Bail out on anything we must not try to solve ourselves."""
        for selector in CAPTCHA_MARKERS:
            if page.locator(selector).count() > 0:
                raise Escalation(EscalationReason.CAPTCHA, f"challenge element present: {selector}")
        body = (page.locator("body").inner_text() or "").lower()
        for phrase, reason in (
            ("create an account", EscalationReason.ACCOUNT_REQUIRED),
            ("sign in to apply", EscalationReason.ACCOUNT_REQUIRED),
            ("verification code", EscalationReason.TWO_FACTOR),
        ):
            if phrase in body:
                raise Escalation(reason, f"page says {phrase!r}")

    def map_fields(self, page: Any) -> dict[str, Field]:
        """Read every control on the form with its visible label.

        Returns selector -> Field with `canonical` unset; FieldMapper fills that in.
        """
        fields: dict[str, Field] = {}
        handles = page.query_selector_all(
            "form input:not([type=hidden]):not([type=submit]), form select, form textarea"
        )
        for index, handle in enumerate(handles):
            tag = (handle.evaluate("el => el.tagName") or "").lower()
            input_type = (handle.get_attribute("type") or "text").lower()
            name = handle.get_attribute("name")
            element_id = handle.get_attribute("id")

            if tag == "textarea":
                kind = FieldKind.TEXTAREA
            elif tag == "select":
                kind = FieldKind.SELECT
            else:
                kind = _INPUT_KINDS.get(input_type, FieldKind.UNKNOWN)

            selector = f"#{element_id}" if element_id else (f"[name='{name}']" if name else None)
            if not selector:
                selector = f"form :nth-match(input, {index + 1})"

            options: list[str] = []
            if kind is FieldKind.SELECT:
                options = [
                    (option.inner_text() or "").strip()
                    for option in handle.query_selector_all("option")
                ]

            fields[selector] = Field(
                selector=selector,
                kind=kind,
                label=self._label_for(page, handle, element_id) or (name or ""),
                name=name,
                required=handle.get_attribute("required") is not None
                or handle.get_attribute("aria-required") == "true",
                options=[option for option in options if option],
            )
        return fields

    @staticmethod
    def _label_for(page: Any, handle: Any, element_id: str | None) -> str:
        for candidate in (
            handle.get_attribute("aria-label"),
            handle.get_attribute("placeholder"),
        ):
            if candidate and candidate.strip():
                return candidate.strip()
        if element_id:
            label = page.query_selector(f"label[for='{element_id}']")
            if label:
                text = (label.inner_text() or "").strip()
                if text:
                    return text
        # Fall back to the nearest enclosing label element.
        text = handle.evaluate("el => el.closest('label') ? el.closest('label').innerText : ''")
        return (text or "").strip()

    def fill(self, page: Any, values: dict[str, Any], fields: dict[str, Field]) -> list[str]:
        """Fill mapped controls. Returns the selectors actually written."""
        written: list[str] = []
        for selector, field in fields.items():
            if not field.canonical:
                continue
            value = values.get(field.canonical)
            if value in (None, ""):
                continue
            locator = page.locator(selector).first
            if field.kind is FieldKind.FILE:
                locator.set_input_files(value)
            elif field.kind is FieldKind.SELECT:
                locator.select_option(label=str(value))
            elif field.kind is FieldKind.CHECKBOX:
                if value:
                    locator.check()
            else:
                locator.fill(str(value))
            written.append(selector)
        return written

    def submit(self, page: Any) -> SubmitResult:
        button = page.locator(
            "form button[type=submit], form input[type=submit], button:has-text('Submit Application')"
        ).first
        if button.count() == 0:
            return SubmitResult(submitted=False, verified=False, detail="no submit control found")
        button.click()
        page.wait_for_load_state("networkidle", timeout=45_000)
        verified = self.verify(page)
        return SubmitResult(
            submitted=True,
            verified=verified,
            detail="submitted" if verified else "clicked submit but saw no confirmation signal",
            confirmation_text=self._confirmation_text(page),
            final_url=page.url,
        )

    def verify(self, page: Any) -> bool:
        """A submit isn't `submitted` until the page says so."""
        for marker in CONFIRMATION_MARKERS:
            try:
                if page.locator(marker).count() > 0:
                    return True
            except Exception:  # noqa: BLE001 - a bad selector must not fail verification
                continue
        return "confirmation" in (page.url or "").lower()

    @staticmethod
    def _confirmation_text(page: Any) -> str | None:
        for marker in CONFIRMATION_MARKERS:
            try:
                locator = page.locator(marker).first
                if locator.count() > 0:
                    return (locator.inner_text() or "").strip()[:500]
            except Exception:  # noqa: BLE001
                continue
        return None


adapter: ATSAdapter = register(GreenhouseAdapter())  # type: ignore[arg-type]

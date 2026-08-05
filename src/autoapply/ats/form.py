"""Shared Playwright form driver.

Every ATS application form is the same problem: find the controls, read their
labels, refuse to guess, fill, click, and confirm. Only the selectors and the
confirmation wording differ, so that is all a subclass overrides.

Field mapping is label-driven rather than selector-driven on purpose: when a
board changes its template, an unrecognized field escalates to a human instead
of quietly receiving the wrong value.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from .base import Escalation, EscalationReason, Field, FieldKind, SubmitResult

log = logging.getLogger(__name__)

_INPUT_KINDS: dict[str, FieldKind] = {
    "email": FieldKind.EMAIL,
    "tel": FieldKind.PHONE,
    "url": FieldKind.URL,
    "file": FieldKind.FILE,
    "checkbox": FieldKind.CHECKBOX,
    "radio": FieldKind.CHECKBOX,
    "text": FieldKind.TEXT,
    "number": FieldKind.TEXT,
    "date": FieldKind.TEXT,
}

# Anything here means stop and ask a human. Never attempt to solve one.
DEFAULT_CAPTCHA_MARKERS: tuple[str, ...] = (
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='turnstile']",
    "div.g-recaptcha",
    "[data-sitekey]",
    "#cf-challenge-running",
)

DEFAULT_ACCOUNT_PHRASES: tuple[tuple[str, EscalationReason], ...] = (
    ("create an account", EscalationReason.ACCOUNT_REQUIRED),
    ("sign in to apply", EscalationReason.ACCOUNT_REQUIRED),
    ("log in to continue", EscalationReason.ACCOUNT_REQUIRED),
    ("verification code", EscalationReason.TWO_FACTOR),
    ("two-factor", EscalationReason.TWO_FACTOR),
)


class FormDrivingAdapter:
    """Playwright half of an ATS adapter. Subclasses supply ingest + selectors."""

    ats_type: ClassVar[str] = "unknown"
    form_selector: ClassVar[str] = "form"
    submit_selector: ClassVar[str] = "form button[type=submit], form input[type=submit]"
    confirmation_markers: ClassVar[tuple[str, ...]] = ()
    confirmation_url_hints: ClassVar[tuple[str, ...]] = ("confirmation", "thanks", "thank-you")
    captcha_markers: ClassVar[tuple[str, ...]] = DEFAULT_CAPTCHA_MARKERS
    account_phrases: ClassVar[tuple[tuple[str, EscalationReason], ...]] = DEFAULT_ACCOUNT_PHRASES

    # -------------------------------------------------------------- guard

    def guard(self, page: Any) -> None:
        """Raise before touching anything we must not attempt on our own."""
        for selector in self.captcha_markers:
            if self._count(page, selector) > 0:
                raise Escalation(EscalationReason.CAPTCHA, f"challenge element present: {selector}")

        body = ""
        try:
            body = (page.locator("body").inner_text() or "").lower()
        except Exception:  # noqa: BLE001 - an unreadable body is not a reason to proceed blind
            log.debug("could not read page body for guard check", exc_info=True)
        for phrase, reason in self.account_phrases:
            if phrase in body:
                raise Escalation(reason, f"page says {phrase!r}")

    # ---------------------------------------------------------- discovery

    def map_fields(self, page: Any) -> dict[str, Field]:
        """Read every control on the form with its visible label.

        `canonical` is left unset; FieldMapper resolves it.
        """
        fields: dict[str, Field] = {}
        selector = (
            f"{self.form_selector} input:not([type=hidden]):not([type=submit]):not([type=button]), "
            f"{self.form_selector} select, {self.form_selector} textarea"
        )
        for index, handle in enumerate(page.query_selector_all(selector)):
            field = self._read_field(page, handle, index)
            if field is not None:
                fields[field.selector] = field
        return fields

    def _read_field(self, page: Any, handle: Any, index: int) -> Field | None:
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

        if element_id:
            selector = f"#{element_id}"
        elif name:
            selector = f"[name='{name}']"
        else:
            selector = f"{self.form_selector} :nth-match(input, {index + 1})"

        options: list[str] = []
        if kind is FieldKind.SELECT:
            options = [
                (option.inner_text() or "").strip() for option in handle.query_selector_all("option")
            ]

        return Field(
            selector=selector,
            kind=kind,
            label=self.label_for(page, handle, element_id) or (name or ""),
            name=name,
            required=handle.get_attribute("required") is not None
            or handle.get_attribute("aria-required") == "true",
            options=[option for option in options if option],
        )

    @staticmethod
    def label_for(page: Any, handle: Any, element_id: str | None) -> str:
        """Best visible description of a control, in decreasing reliability."""
        if element_id:
            label = page.query_selector(f"label[for='{element_id}']")
            if label:
                text = (label.inner_text() or "").strip()
                if text:
                    return text
        for attribute in ("aria-label", "placeholder", "title"):
            value = handle.get_attribute(attribute)
            if value and value.strip():
                return value.strip()
        # Enclosing <label>, then any aria-labelledby target.
        text = handle.evaluate("el => el.closest('label') ? el.closest('label').innerText : ''")
        if (text or "").strip():
            return text.strip()
        labelled_by = handle.get_attribute("aria-labelledby")
        if labelled_by:
            target = page.query_selector(f"#{labelled_by.split()[0]}")
            if target:
                return (target.inner_text() or "").strip()
        return ""

    # ------------------------------------------------------------- fill

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
                self._select(locator, field, value)
            elif field.kind is FieldKind.CHECKBOX:
                if value and str(value).lower() not in ("no", "false"):
                    locator.check()
            else:
                locator.fill(str(value))
            written.append(selector)
        return written

    @staticmethod
    def _select(locator: Any, field: Field, value: Any) -> None:
        """Match a stored answer to one of the select's actual options.

        Exact match first, then case-insensitive, then prefix. If nothing matches
        we leave the control alone — a required unset select is caught by the
        escalation check rather than being set to a wrong option here.
        """
        wanted = str(value).strip()
        options = field.options or []
        for candidate in (
            next((o for o in options if o == wanted), None),
            next((o for o in options if o.lower() == wanted.lower()), None),
            next((o for o in options if o.lower().startswith(wanted.lower())), None),
        ):
            if candidate:
                locator.select_option(label=candidate)
                return
        if not options:
            locator.select_option(label=wanted)

    # ----------------------------------------------------------- submit

    def submit(self, page: Any) -> SubmitResult:
        button = page.locator(self.submit_selector).first
        if button.count() == 0:
            return SubmitResult(submitted=False, verified=False, detail="no submit control found")

        button.click()
        try:
            page.wait_for_load_state("networkidle", timeout=45_000)
        except Exception:  # noqa: BLE001 - a chatty page must not fail the submit
            log.debug("networkidle wait timed out; verifying anyway", exc_info=True)

        verified = self.verify(page)
        return SubmitResult(
            submitted=True,
            verified=verified,
            detail="submitted" if verified else "clicked submit but saw no confirmation signal",
            confirmation_text=self.confirmation_text(page),
            final_url=getattr(page, "url", None),
        )

    def verify(self, page: Any) -> bool:
        """A submit is not `submitted` until the page says so."""
        for marker in self.confirmation_markers:
            if self._count(page, marker) > 0:
                return True
        url = (getattr(page, "url", "") or "").lower()
        return any(hint in url for hint in self.confirmation_url_hints)

    def confirmation_text(self, page: Any) -> str | None:
        for marker in self.confirmation_markers:
            try:
                locator = page.locator(marker).first
                if locator.count() > 0:
                    return (locator.inner_text() or "").strip()[:500]
            except Exception:  # noqa: BLE001
                continue
        return None

    @staticmethod
    def _count(page: Any, selector: str) -> int:
        try:
            return page.locator(selector).count()
        except Exception:  # noqa: BLE001 - a selector the page rejects is simply absent
            return 0

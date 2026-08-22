"""Lever adapter.

Ingest: `api.lever.co/v0/postings/{company}?mode=json`.

Lever splits a posting across several fields — an intro (`descriptionPlain`),
an ordered set of `lists` sections (What You'll Do, What You Bring, …), and a
closing `additionalPlain`. Matching against only the intro loses every actual
requirement, so `parse_job` reassembles the whole posting.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from .base import ATSAdapter, JobPosting, register
from .form import FormDrivingAdapter
from .textutil import strip_html

POSTINGS_API = "https://api.lever.co/v0/postings/{company}"
USER_AGENT = "autoapply/0.1"


class LeverAdapter(FormDrivingAdapter):
    ats_type = "lever"
    confirmation_markers = (
        "text=Thank you for applying",
        "text=Application submitted",
        "text=Thanks for applying",
        ".application-confirmation",
    )
    confirmation_url_hints = ("thanks", "confirmation")
    submit_selector = (
        "#btn-submit, form button[type=submit], form input[type=submit], "
        "button:has-text('Submit application')"
    )

    def detect(self, url: str) -> bool:
        return "lever.co" in (url or "").lower()

    def fetch_jobs(self, board_token: str, *, client: httpx.Client | None = None) -> list[JobPosting]:
        owns = client is None
        client = client or httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT})
        try:
            response = client.get(POSTINGS_API.format(company=board_token), params={"mode": "json"})
            response.raise_for_status()
            payload = response.json()
        finally:
            if owns:
                client.close()
        if not isinstance(payload, list):
            return []
        return [self.parse_job(job) for job in payload]

    def parse_job(self, raw: dict[str, Any]) -> JobPosting:
        categories = raw.get("categories") or {}
        location = categories.get("location")
        workplace = (raw.get("workplaceType") or "").lower()

        posted_at = None
        created = raw.get("createdAt")
        if isinstance(created, (int, float)):
            # Lever reports epoch milliseconds.
            posted_at = dt.datetime.fromtimestamp(created / 1000, tz=dt.UTC)

        return JobPosting(
            ats_job_id=str(raw.get("id", "")),
            title=(raw.get("text") or "").strip(),
            location=location,
            description=self._full_description(raw, categories),
            # hostedUrl is the posting; applyUrl is the form. Prefer the form.
            apply_url=raw.get("applyUrl") or raw.get("hostedUrl", ""),
            remote=workplace == "remote",
            posted_at=posted_at,
            raw=raw,
        )

    @staticmethod
    def _full_description(raw: dict[str, Any], categories: dict[str, Any]) -> str:
        parts: list[str] = []

        header = " · ".join(
            str(v)
            for v in (categories.get("team"), categories.get("commitment"), raw.get("workplaceType"))
            if v
        )
        if header:
            parts.append(header)

        intro = raw.get("descriptionPlain") or strip_html(raw.get("description", ""))
        if intro:
            parts.append(intro.strip())

        for section in raw.get("lists") or []:
            title = (section.get("text") or "").strip()
            body = strip_html(section.get("content", ""))
            if title or body:
                parts.append(f"{title}\n{body}".strip())

        closing = raw.get("additionalPlain") or strip_html(raw.get("additional", ""))
        if closing:
            parts.append(closing.strip())

        return "\n\n".join(parts).strip()


adapter: ATSAdapter = register(LeverAdapter())  # type: ignore[arg-type]

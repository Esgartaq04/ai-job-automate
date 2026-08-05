"""Greenhouse adapter.

Ingest: `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`.
The board API returns the description as an HTML-escaped blob.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import httpx

from .base import ATSAdapter, JobPosting, register
from .form import FormDrivingAdapter
from .textutil import strip_html

BOARD_API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
USER_AGENT = "autoapply/0.1"

_REMOTE = re.compile(r"\bremote\b", re.I)


class GreenhouseAdapter(FormDrivingAdapter):
    ats_type = "greenhouse"
    confirmation_markers = (
        "text=Thank you for applying",
        "text=Your application has been submitted",
        "text=Application submitted",
        "#application_confirmation",
    )
    submit_selector = (
        "form button[type=submit], form input[type=submit], button:has-text('Submit Application')"
    )

    def detect(self, url: str) -> bool:
        return "greenhouse.io" in (url or "").lower()

    def fetch_jobs(self, board_token: str, *, client: httpx.Client | None = None) -> list[JobPosting]:
        owns = client is None
        client = client or httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT})
        try:
            response = client.get(BOARD_API.format(token=board_token), params={"content": "true"})
            response.raise_for_status()
            payload = response.json()
        finally:
            if owns:
                client.close()
        return [self.parse_job(job) for job in payload.get("jobs", [])]

    def parse_job(self, raw: dict[str, Any]) -> JobPosting:
        location = (raw.get("location") or {}).get("name")
        posted_at = None
        stamp = raw.get("updated_at") or raw.get("first_published")
        if stamp:
            try:
                posted_at = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            except ValueError:
                posted_at = None
        return JobPosting(
            ats_job_id=str(raw.get("id")),
            title=(raw.get("title") or "").strip(),
            location=location,
            description=strip_html(raw.get("content", "")),
            apply_url=raw.get("absolute_url", ""),
            remote=bool(location and _REMOTE.search(location)),
            posted_at=posted_at,
            raw=raw,
        )


adapter: ATSAdapter = register(GreenhouseAdapter())  # type: ignore[arg-type]

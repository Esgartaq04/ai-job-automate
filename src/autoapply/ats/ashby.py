"""Ashby adapter.

Ingest: `api.ashbyhq.com/posting-api/job-board/{board}`.

Ashby gives a clean JSON job board, including `isRemote` and `secondaryLocations`
so we don't have to infer geography from a free-text string.

Caveat on submission: the Ashby application form is a client-rendered SPA whose
file upload and custom questions are React-controlled. Label-driven mapping
works on the standard name/email/resume fields, but expect a higher escalation
rate here than on Greenhouse — which is the designed behaviour, not a failure.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from .base import ATSAdapter, JobPosting, register
from .form import FormDrivingAdapter
from .textutil import strip_html

BOARD_API = "https://api.ashbyhq.com/posting-api/job-board/{board}"
USER_AGENT = "autoapply/0.1"


class AshbyAdapter(FormDrivingAdapter):
    ats_type = "ashby"
    confirmation_markers = (
        "text=Thank you for applying",
        "text=Your application has been submitted",
        "text=Application received",
        "text=Thanks for applying",
    )
    confirmation_url_hints = ("confirmation", "thanks", "submitted")
    submit_selector = (
        "form button[type=submit], button:has-text('Submit Application'), "
        "button:has-text('Submit application')"
    )

    def detect(self, url: str) -> bool:
        return "ashbyhq.com" in (url or "").lower()

    def fetch_jobs(self, board_token: str, *, client: httpx.Client | None = None) -> list[JobPosting]:
        owns = client is None
        client = client or httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT})
        try:
            response = client.get(
                BOARD_API.format(board=board_token), params={"includeCompensation": "true"}
            )
            response.raise_for_status()
            payload = response.json()
        finally:
            if owns:
                client.close()
        return [
            self.parse_job(job)
            for job in payload.get("jobs", [])
            # isListed=false means the posting is unpublished or internal.
            if job.get("isListed", True)
        ]

    def parse_job(self, raw: dict[str, Any]) -> JobPosting:
        posted_at = None
        published = raw.get("publishedAt")
        if published:
            try:
                posted_at = dt.datetime.fromisoformat(str(published).replace("Z", "+00:00"))
            except ValueError:
                posted_at = None

        description = raw.get("descriptionPlain") or strip_html(raw.get("descriptionHtml", ""))
        header = " · ".join(
            str(v) for v in (raw.get("department"), raw.get("team"), raw.get("employmentType")) if v
        )
        if header:
            description = f"{header}\n\n{description}".strip()

        return JobPosting(
            ats_job_id=str(raw.get("id", "")),
            title=(raw.get("title") or "").strip(),
            location=raw.get("location"),
            description=description,
            apply_url=raw.get("applyUrl") or raw.get("jobUrl", ""),
            remote=bool(raw.get("isRemote")),
            posted_at=posted_at,
            raw=raw,
        )

    @staticmethod
    def all_locations(raw: dict[str, Any]) -> list[str]:
        """Primary plus secondary locations — a role open in three cities lists three."""
        locations = [raw.get("location")] if raw.get("location") else []
        for secondary in raw.get("secondaryLocations") or []:
            name = secondary.get("location")
            if name:
                locations.append(name)
        return locations


adapter: ATSAdapter = register(AshbyAdapter())  # type: ignore[arg-type]

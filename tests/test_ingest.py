from __future__ import annotations

import json
from pathlib import Path

from autoapply.ats.greenhouse import GreenhouseAdapter
from autoapply.ats.textutil import strip_html
from autoapply.pipeline.ingest import canonical_hash, normalize

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_board.json"


def test_canonical_hash_is_stable_across_cosmetic_differences():
    a = canonical_hash("Morningstar, Inc.", "Software Engineer", "Chicago, IL", "12345")
    b = canonical_hash("morningstar inc", "  Software   Engineer ", "chicago il", "12345")
    assert a == b


def test_canonical_hash_separates_distinct_postings():
    base = canonical_hash("Acme", "Backend Engineer", "Remote", "1")
    assert base != canonical_hash("Acme", "Backend Engineer", "Remote", "2")
    assert base != canonical_hash("Acme", "Frontend Engineer", "Remote", "1")
    assert base != canonical_hash("Other", "Backend Engineer", "Remote", "1")


def test_normalize_strips_punctuation_and_case():
    assert normalize("Sr. Software Engineer (Backend)") == "sr software engineer backend"
    assert normalize(None) == ""


def test_strip_html_unescapes_and_flattens():
    raw = "&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;"
    text = strip_html(raw)
    assert "Build & ship" in text
    assert "<" not in text and "&lt;" not in text
    assert "Python" in text


def test_parse_greenhouse_payload():
    payload = json.loads(FIXTURE.read_text())
    adapter = GreenhouseAdapter()
    postings = [adapter.parse_job(job) for job in payload["jobs"]]

    assert len(postings) == 3
    first = postings[0]
    assert first.ats_job_id == "4001"
    assert first.title == "Backend Engineer"
    assert first.location == "Chicago, IL"
    assert "distributed systems" in first.description
    assert first.apply_url.startswith("https://")
    assert first.remote is False
    assert first.posted_at is not None

    remote = next(p for p in postings if p.ats_job_id == "4002")
    assert remote.remote is True


def test_detect_only_claims_greenhouse_urls():
    adapter = GreenhouseAdapter()
    assert adapter.detect("https://boards.greenhouse.io/acme/jobs/1")
    assert adapter.detect("https://job-boards.greenhouse.io/acme")
    assert not adapter.detect("https://jobs.lever.co/acme/123")

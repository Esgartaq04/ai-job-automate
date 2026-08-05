"""Hard filters must run before the LLM and must never be the reason a bad
application goes out — a false negative costs nothing, a false positive costs a
model call and a wasted submission."""

from __future__ import annotations

from dataclasses import dataclass

from autoapply.pipeline.match import Preferences, hard_filter, required_years


@dataclass
class FakeJob:
    description: str = ""
    location: str | None = "Chicago, IL"
    remote: bool = False
    title: str = "Backend Engineer"


def test_required_years_takes_the_lowest_stated_requirement():
    assert required_years("5+ years of experience required. 8 years experience preferred.") == 5
    assert required_years("3-5 years experience in backend systems") == 3
    assert required_years("No specific requirement.") is None


def test_sponsorship_blocker_only_when_the_candidate_needs_it():
    job = FakeJob(description="We are not able to sponsor visas for this role.")
    assert hard_filter(job, Preferences(needs_sponsorship=True))
    assert not hard_filter(job, Preferences(needs_sponsorship=False))


def test_clearance_blocks_unless_optional_or_held():
    required = FakeJob(description="Requires an active TS/SCI security clearance.")
    assert hard_filter(required, Preferences(has_clearance=False))
    assert not hard_filter(required, Preferences(has_clearance=True))

    optional = FakeJob(description="An active security clearance is a plus.")
    assert not hard_filter(optional, Preferences(has_clearance=False))


def test_years_cap():
    job = FakeJob(description="10 years experience building payment systems.")
    assert hard_filter(job, Preferences(max_years_required=5))
    assert not hard_filter(job, Preferences(max_years_required=12))


def test_location_filter_respects_remote():
    prefs = Preferences(allowed_locations=["Chicago"], remote_ok=True)
    assert not hard_filter(FakeJob(location="Chicago, IL"), prefs)
    assert not hard_filter(FakeJob(location="Remote - US", remote=True), prefs)
    assert hard_filter(FakeJob(location="Austin, TX"), prefs)


def test_location_filter_ignored_when_no_allowlist():
    assert not hard_filter(FakeJob(location="Anywhere"), Preferences())


def test_excluded_company():
    prefs = Preferences(excluded_companies=["Acme"])
    assert hard_filter(FakeJob(), prefs, company_name="Acme Corp")
    assert not hard_filter(FakeJob(), prefs, company_name="Globex")


def test_blockers_accumulate():
    job = FakeJob(
        description="Requires TS/SCI clearance and 12 years experience. We cannot sponsor visas.",
        location="Austin, TX",
    )
    blockers = hard_filter(job, Preferences(needs_sponsorship=True, allowed_locations=["Chicago"], remote_ok=False))
    assert len(blockers) >= 3


def test_preferences_ignore_unknown_keys_from_json():
    class FakeProfile:
        base_resume_json = {"preferences": {"min_score": 0.8, "not_a_real_key": 1}}

    prefs = Preferences.from_profile(FakeProfile())
    assert prefs.min_score == 0.8

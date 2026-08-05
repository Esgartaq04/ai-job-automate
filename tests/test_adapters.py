"""Lever and Ashby adapters, plus the shared form driver.

The board fixtures are real responses captured from the public APIs (trimmed),
not hand-written shapes — parsing bugs that only show up on live data get caught
here instead of on the first ingest run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoapply.ats import adapter_for_url, get_adapter, registered_types
from autoapply.ats.ashby import AshbyAdapter
from autoapply.ats.base import Escalation, EscalationReason, Field, FieldKind
from autoapply.ats.form import FormDrivingAdapter
from autoapply.ats.lever import LeverAdapter

FIXTURES = Path(__file__).parent / "fixtures"
LEVER = json.loads((FIXTURES / "lever_board.json").read_text())
ASHBY = json.loads((FIXTURES / "ashby_board.json").read_text())


# ------------------------------------------------------------------ registry


def test_every_designed_adapter_is_registered():
    assert set(registered_types()) >= {"greenhouse", "lever", "ashby"}


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://boards.greenhouse.io/acme/jobs/1", "greenhouse"),
        ("https://job-boards.greenhouse.io/acme/jobs/1", "greenhouse"),
        ("https://jobs.lever.co/ro/abc-123/apply", "lever"),
        ("https://jobs.ashbyhq.com/ashby/abc-123/application", "ashby"),
    ],
)
def test_urls_route_to_the_right_adapter(url, expected):
    adapter = adapter_for_url(url)
    assert adapter is not None and adapter.ats_type == expected


def test_unknown_host_routes_nowhere():
    assert adapter_for_url("https://careers.example.com/jobs/1") is None


def test_unknown_ats_type_raises_rather_than_returning_none():
    with pytest.raises(LookupError):
        get_adapter("workday")


# --------------------------------------------------------------------- lever


def by_id(postings, prefix):
    return next(p for p in postings if p.ats_job_id.startswith(prefix))


@pytest.fixture(scope="module")
def lever_postings():
    return [LeverAdapter().parse_job(job) for job in LEVER]


def test_lever_parses_core_fields(lever_postings):
    posting = by_id(lever_postings, "d2e897ae")
    assert posting.title == "Engineering Manager, Core Infrastructure"
    assert posting.location == "New York, NY"
    assert posting.posted_at is not None and posting.posted_at.year >= 2024


def test_lever_apply_url_is_the_form_not_the_posting(lever_postings):
    # hostedUrl renders the ad; applyUrl is the form. Submitting needs the form.
    for posting in lever_postings:
        assert posting.apply_url.endswith("/apply")


def test_lever_remote_comes_from_workplace_type_not_the_location_string(lever_postings):
    # This board has a posting whose location says "New York, NY or Remote" while
    # workplaceType is the authoritative field.
    remote = by_id(lever_postings, "9445bb8c")
    assert remote.remote is True
    assert by_id(lever_postings, "d2e897ae").remote is False  # hybrid
    assert by_id(lever_postings, "f25a6c49").remote is False  # onsite


def test_lever_description_reassembles_the_list_sections(lever_postings):
    """The requirements live in `lists`, not in descriptionPlain. Losing them
    would mean matching against the company boilerplate alone."""
    posting = by_id(lever_postings, "d2e897ae")
    assert "What You’ll Do" in posting.description
    assert "What You’ll Bring" in posting.description
    intro_only = LEVER[2].get("descriptionPlain", "")
    assert len(posting.description) > len(intro_only)
    assert "<li>" not in posting.description and "&lt;" not in posting.description


def test_lever_description_includes_team_and_commitment_header(lever_postings):
    assert "Full-time" in by_id(lever_postings, "f25a6c49").description


def test_lever_epoch_millis_become_aware_datetimes(lever_postings):
    posting = by_id(lever_postings, "f25a6c49")
    assert posting.posted_at.tzinfo is not None
    assert 2020 < posting.posted_at.year < 2100  # ms vs s confusion would blow this


def test_lever_handles_a_non_list_response():
    class Client:
        def get(self, *_a, **_k):
            class R:
                @staticmethod
                def raise_for_status():
                    return None

                @staticmethod
                def json():
                    return {"error": "not found"}

            return R()

    assert LeverAdapter().fetch_jobs("nope", client=Client()) == []


# --------------------------------------------------------------------- ashby


@pytest.fixture(scope="module")
def ashby_postings():
    return [AshbyAdapter().parse_job(job) for job in ASHBY["jobs"]]


def test_ashby_parses_core_fields(ashby_postings):
    posting = by_id(ashby_postings, "7458d4e9")
    assert posting.title == "Engineering Manager - EU"
    assert posting.location == "Remote - European Union"
    assert posting.remote is True
    assert posting.apply_url.endswith("/application")
    assert posting.posted_at is not None


def test_ashby_remote_flag_is_used_verbatim():
    parsed = AshbyAdapter().parse_job({"id": "x", "title": "T", "location": "Chicago", "isRemote": False})
    assert parsed.remote is False


def test_ashby_prefixes_department_and_team(ashby_postings):
    description = by_id(ashby_postings, "7458d4e9").description
    assert "Engineering" in description.splitlines()[0]
    assert "FullTime" in description.splitlines()[0]


def test_ashby_falls_back_to_html_when_plain_text_is_absent():
    parsed = AshbyAdapter().parse_job(
        {"id": "x", "title": "T", "descriptionHtml": "<p>Build <b>things</b></p>"}
    )
    assert "Build things" in parsed.description
    assert "<p>" not in parsed.description


def test_ashby_skips_unlisted_postings():
    class Client:
        def get(self, *_a, **_k):
            class R:
                @staticmethod
                def raise_for_status():
                    return None

                @staticmethod
                def json():
                    return {
                        "jobs": [
                            {"id": "1", "title": "Listed", "isListed": True},
                            {"id": "2", "title": "Draft", "isListed": False},
                        ]
                    }

            return R()

    postings = AshbyAdapter().fetch_jobs("board", client=Client())
    assert [p.title for p in postings] == ["Listed"]


def test_ashby_collects_secondary_locations():
    locations = AshbyAdapter.all_locations(ASHBY["jobs"][0])
    assert len(locations) > 1
    assert ASHBY["jobs"][0]["location"] in locations


# ------------------------------------------------------- shared form driver


class Handle:
    def __init__(self, tag="input", attrs=None, label="", options=None):
        self.tag, self.attrs, self.label = tag, attrs or {}, label
        self.options = options or []

    def evaluate(self, script):
        return self.tag.upper() if "tagName" in script else self.label

    def get_attribute(self, name):
        return self.attrs.get(name)

    def query_selector_all(self, _s):
        return [Handle(tag="option", label=o) for o in self.options]

    def inner_text(self):
        return self.label


class Locator:
    def __init__(self, page, selector, count=1):
        self.page, self.selector, self._count = page, selector, count
        self.selected = None

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def click(self):
        self.page.clicked = True

    def fill(self, value):
        self.page.filled[self.selector] = value

    def check(self):
        self.page.filled[self.selector] = True

    def set_input_files(self, path):
        self.page.filled[self.selector] = path

    def select_option(self, label=None):
        self.page.filled[self.selector] = label

    def inner_text(self):
        return self.page.body


class Page:
    def __init__(self, handles=None, body="Apply for this job", url="https://x/apply", present=()):
        self.handles = handles or []
        self.body = body
        self.url = url
        self.present = set(present)
        self.filled: dict[str, object] = {}
        self.clicked = False

    def locator(self, selector):
        if selector == "body":
            return Locator(self, selector)
        return Locator(self, selector, count=1 if selector in self.present else 0)

    def query_selector_all(self, _s):
        return self.handles

    def query_selector(self, selector):
        for handle in self.handles:
            if handle.attrs.get("id") and selector == f"label[for='{handle.attrs['id']}']":
                return Handle(tag="label", label=handle.label)
        return None

    def wait_for_load_state(self, *_a, **_k):
        return None


def test_guard_escalates_on_a_captcha_element():
    adapter = FormDrivingAdapter()
    page = Page(present={"div.g-recaptcha"})
    with pytest.raises(Escalation) as exc:
        adapter.guard(page)
    assert exc.value.reason is EscalationReason.CAPTCHA


def test_guard_escalates_on_an_account_wall():
    with pytest.raises(Escalation) as exc:
        FormDrivingAdapter().guard(Page(body="Please Create an Account to continue"))
    assert exc.value.reason is EscalationReason.ACCOUNT_REQUIRED


def test_guard_passes_a_clean_page():
    FormDrivingAdapter().guard(Page())  # must not raise


def test_map_fields_prefers_the_for_label_over_the_placeholder():
    page = Page([Handle(attrs={"id": "email", "name": "email", "type": "email", "placeholder": "you@co"}, label="Work Email")])
    fields = FormDrivingAdapter().map_fields(page)
    assert fields["#email"].label == "Work Email"
    assert fields["#email"].kind is FieldKind.EMAIL


def test_map_fields_records_required_from_either_attribute():
    page = Page(
        [
            Handle(attrs={"id": "a", "required": ""}, label="A"),
            Handle(attrs={"id": "b", "aria-required": "true"}, label="B"),
            Handle(attrs={"id": "c"}, label="C"),
        ]
    )
    fields = FormDrivingAdapter().map_fields(page)
    assert (fields["#a"].required, fields["#b"].required, fields["#c"].required) == (True, True, False)


def test_map_fields_falls_back_to_the_name_selector_without_an_id():
    page = Page([Handle(attrs={"name": "resume", "type": "file"}, label="Resume")])
    fields = FormDrivingAdapter().map_fields(page)
    assert "[name='resume']" in fields


def test_select_matches_an_option_case_insensitively():
    page = Page()
    field = Field(selector="#q", kind=FieldKind.SELECT, label="Authorized?", options=["Yes", "No"])
    field.canonical = "work_authorized"
    FormDrivingAdapter().fill(page, {"work_authorized": "yes"}, {"#q": field})
    assert page.filled["#q"] == "Yes"


def test_select_leaves_the_control_alone_when_nothing_matches():
    """Better an unset required select — which escalates — than a wrong answer
    on someone's real application."""
    page = Page()
    field = Field(selector="#q", kind=FieldKind.SELECT, label="Level", options=["Junior", "Senior"])
    field.canonical = "years_experience"
    FormDrivingAdapter().fill(page, {"years_experience": "Staff"}, {"#q": field})
    assert "#q" not in page.filled


def test_fill_skips_unmapped_and_empty_values():
    page = Page()
    mapped = Field(selector="#a", kind=FieldKind.TEXT, label="A")
    mapped.canonical = "first_name"
    unmapped = Field(selector="#b", kind=FieldKind.TEXT, label="B")
    blank = Field(selector="#c", kind=FieldKind.TEXT, label="C")
    blank.canonical = "phone"
    written = FormDrivingAdapter().fill(
        page, {"first_name": "Ada", "phone": ""}, {"#a": mapped, "#b": unmapped, "#c": blank}
    )
    assert written == ["#a"] and page.filled == {"#a": "Ada"}


def test_checkbox_is_not_ticked_for_a_negative_answer():
    page = Page()
    field = Field(selector="#c", kind=FieldKind.CHECKBOX, label="Sponsorship?")
    field.canonical = "requires_sponsorship"
    FormDrivingAdapter().fill(page, {"requires_sponsorship": "No"}, {"#c": field})
    assert "#c" not in page.filled


def test_submit_without_a_button_reports_failure_and_clicks_nothing():
    page = Page()
    result = FormDrivingAdapter().submit(page)
    assert result.submitted is False and page.clicked is False


def test_unconfirmed_submit_is_not_treated_as_success():
    class Adapter(FormDrivingAdapter):
        submit_selector = "#go"
        confirmation_markers = ("text=Thank you",)

    page = Page(present={"#go"}, url="https://x/apply")
    result = Adapter().submit(page)
    assert result.submitted is True
    assert result.verified is False  # clicked, but nothing confirmed it


def test_confirmation_marker_verifies():
    class Adapter(FormDrivingAdapter):
        submit_selector = "#go"
        confirmation_markers = ("text=Thank you",)

    page = Page(present={"#go", "text=Thank you"})
    assert Adapter().submit(page).verified is True


def test_confirmation_url_hint_verifies():
    class Adapter(FormDrivingAdapter):
        submit_selector = "#go"

    page = Page(present={"#go"}, url="https://jobs.lever.co/ro/abc/thanks")
    assert Adapter().submit(page).verified is True

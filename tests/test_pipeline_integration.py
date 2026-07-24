"""End-to-end walk of the Phase 1 vertical slice against a real Postgres.

Set AUTOAPPLY_TEST_DATABASE_URL to a database with the `vector` extension
available; the test is skipped otherwise so `make test` stays offline.

    createdb autoapply_test
    AUTOAPPLY_TEST_DATABASE_URL=postgresql+psycopg://localhost/autoapply_test pytest -q

The LLM and the browser are stubbed. Everything else — migrations, the vector
recall query, the state machine, artifact storage, the adapter's DOM handling —
is the real code path.
"""

from __future__ import annotations

import contextlib
import os

import pytest
from sqlalchemy.exc import IntegrityError

DB_URL = os.environ.get("AUTOAPPLY_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="AUTOAPPLY_TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    artifacts = tmp_path_factory.mktemp("artifacts")
    os.environ["AUTOAPPLY_DATABASE_URL"] = DB_URL or ""
    os.environ["AUTOAPPLY_ARTIFACT_URI"] = f"local://{artifacts}"
    os.environ["AUTOAPPLY_DRY_RUN"] = "false"
    os.environ["AUTOAPPLY_SUBMIT_JITTER_SECONDS"] = "0"
    os.environ["AUTOAPPLY_EMBEDDING_PROVIDER"] = "local"

    from autoapply.config import get_settings

    get_settings.cache_clear()
    from autoapply.db import apply_migrations, get_engine

    get_engine.cache_clear()
    apply_migrations()
    return artifacts


@pytest.fixture
def clean_db(env):
    from sqlalchemy import text

    from autoapply.db import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            text(
                "TRUNCATE run_events, runs, applications, matches, artifacts, jobs, "
                "companies, facts, profiles, users, field_map_cache RESTART IDENTITY CASCADE"
            )
        )
    return env


# --------------------------------------------------------------- fake browser


class FakeHandle:
    def __init__(self, tag, attrs, options=None, label=""):
        self.tag = tag
        self.attrs = attrs
        self.options = options or []
        self.label = label

    def evaluate(self, script):
        if "tagName" in script:
            return self.tag.upper()
        return self.label

    def get_attribute(self, name):
        return self.attrs.get(name)

    def query_selector_all(self, _selector):
        return [FakeHandle("option", {}, label=o) for o in self.options]

    def inner_text(self):
        return self.label


class FakeLocator:
    def __init__(self, page, selector, count=1):
        self.page = page
        self.selector = selector
        self._count = count

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def fill(self, value):
        self.page.filled[self.selector] = value

    def set_input_files(self, path):
        self.page.filled[self.selector] = path

    def select_option(self, label=None):
        self.page.filled[self.selector] = label

    def check(self):
        self.page.filled[self.selector] = True

    def click(self):
        self.page.clicked = True
        self.page.url = "https://boards.greenhouse.io/examplecorp/jobs/4001/confirmation"

    def inner_text(self):
        return "Thank you for applying" if self.page.clicked else self.page.body_text


class FakePage:
    """Implements exactly the Playwright surface the Greenhouse adapter touches."""

    def __init__(self, fields, body_text="Apply for this job"):
        self.fields = fields
        self.body_text = body_text
        self.filled: dict[str, object] = {}
        self.clicked = False
        self.url = "https://boards.greenhouse.io/examplecorp/jobs/4001"

    def locator(self, selector):
        if selector == "body":
            return FakeLocator(self, selector)
        if "recaptcha" in selector or "hcaptcha" in selector or "sitekey" in selector or "cf-challenge" in selector:
            return FakeLocator(self, selector, count=0)
        if "Thank you for applying" in selector or "submitted" in selector:
            return FakeLocator(self, selector, count=1 if self.clicked else 0)
        if "confirmation" in selector:
            return FakeLocator(self, selector, count=1 if self.clicked else 0)
        return FakeLocator(self, selector)

    def query_selector_all(self, _selector):
        return self.fields

    def query_selector(self, selector):
        for handle in self.fields:
            if handle.attrs.get("id") and f"label[for='{handle.attrs['id']}']" == selector:
                return FakeHandle("label", {}, label=handle.label)
        return None

    def wait_for_load_state(self, *_args, **_kwargs):
        return None

    def screenshot(self, **_kwargs):
        return b"\x89PNG\r\n\x1a\n fake screenshot"

    def content(self):
        return "<html><body>fake dom</body></html>"


def greenhouse_form():
    return [
        FakeHandle("input", {"id": "first_name", "name": "first_name", "type": "text", "required": ""}, label="First Name"),
        FakeHandle("input", {"id": "last_name", "name": "last_name", "type": "text", "required": ""}, label="Last Name"),
        FakeHandle("input", {"id": "email", "name": "email", "type": "email", "required": ""}, label="Email"),
        FakeHandle("input", {"id": "phone", "name": "phone", "type": "tel"}, label="Phone"),
        FakeHandle("input", {"id": "resume", "name": "resume", "type": "file", "required": ""}, label="Resume/CV"),
        FakeHandle("select", {"id": "q_auth", "name": "question_auth"}, options=["Yes", "No"],
                   label="Are you legally authorized to work in the US?"),
    ]


@contextlib.contextmanager
def fake_browser(_url, page=None):
    yield page


# ------------------------------------------------------------------ the walk


def test_vertical_slice(clean_db, monkeypatch):
    from conftest import StubLLM

    from autoapply import pipeline
    from autoapply.ats.greenhouse import GreenhouseAdapter
    from autoapply.db import session_scope
    from autoapply.events import replay_state, start_run, transition
    from autoapply.llm.embeddings import get_embedder
    from autoapply.models import Application, Company, Fact, Job, Profile, RunEvent, User
    from autoapply.pipeline import outcome as outcome_mod
    from autoapply.pipeline import submit as submit_mod
    from autoapply.pipeline import tailor as tailor_mod

    embedder = get_embedder()

    # ---- seed a user, a fact store, a company and one ingested job ----
    with session_scope() as session:
        user = User(email="ada@example.com")
        session.add(user)
        session.flush()
        profile = Profile(
            user_id=user.id,
            version=1,
            base_resume_json={
                "name": "Ada Lovelace",
                "email": "ada+apply@example.com",
                "phone": "555-0100",
                "preferences": {"allowed_locations": ["Chicago"], "min_score": 0.5},
                "work_authorized": "Yes",
            },
        )
        session.add(profile)
        session.flush()
        facts = [
            Fact(
                id="exp_001:v1",
                profile_id=profile.id,
                type="experience",
                org="Morningstar",
                role="Software Engineering Intern",
                start_date="2026-06",
                text="Built an internal pricing service in Python that replaced a nightly batch job.",
                skills=["python", "postgres"],
            ),
            Fact(
                id="exp_002:v1",
                profile_id=profile.id,
                type="experience",
                org="Morningstar",
                role="Software Engineering Intern",
                start_date="2026-06",
                text="Owned the CI pipeline for 4 repositories.",
                skills=["ci/cd", "docker"],
            ),
        ]
        for fact in facts:
            fact.embedding = embedder.embed(f"{fact.role} {fact.org} {fact.text}")
            session.add(fact)
        profile.embedding = embedder.embed("python backend services postgres ci pipelines")

        company = Company(name="ExampleCorp", ats_type="greenhouse", board_token="examplecorp")
        session.add(company)
        session.flush()

        posting = GreenhouseAdapter().parse_job(
            {
                "id": 4001,
                "title": "Backend Engineer",
                "updated_at": "2026-07-01T15:04:05Z",
                "location": {"name": "Chicago, IL"},
                "absolute_url": "https://boards.greenhouse.io/examplecorp/jobs/4001",
                "content": "&lt;p&gt;Python backend services, Postgres, CI pipelines.&lt;/p&gt;",
            }
        )
        job = Job(
            company_id=company.id,
            ats_job_id=posting.ats_job_id,
            title=posting.title,
            location=posting.location,
            description=posting.description,
            apply_url=posting.apply_url,
            canonical_hash=pipeline.canonical_hash(company.name, posting.title, posting.location, posting.ats_job_id),
            content_hash="abc",
            embedding=embedder.embed(f"{posting.title} {posting.description}"),
        )
        session.add(job)
        session.flush()
        user_id, profile_id, job_id = user.id, profile.id, job.id

    # ---- stage 2: match ----
    with session_scope() as session:
        profile = session.get(Profile, profile_id)
        llm = StubLLM([{"score": 0.82, "reasons": ["Python backend overlap"], "blockers": []}])
        outcomes = pipeline.match_profile(session, profile, llm=llm, limit=10)
        assert len(outcomes) == 1
        assert outcomes[0].score == 0.82
        assert outcomes[0].similarity > 0  # vector recall actually ran

    # ---- stage 3: tailor (render stubbed; Playwright isn't available in CI) ----
    monkeypatch.setattr(tailor_mod, "render_resume_pdf", lambda *a, **k: b"%PDF-1.4 resume")
    monkeypatch.setattr(tailor_mod, "render_cover_letter_pdf", lambda *a, **k: b"%PDF-1.4 cover")

    with session_scope() as session:
        application = Application(user_id=user_id, job_id=job_id, state="discovered")
        session.add(application)
        session.flush()
        run = start_run(session, stage="tailor", application_id=application.id)
        transition(session, application, "matched", run=run, message="fit 0.82")

        llm = StubLLM(
            [
                {
                    "summary": "Engineer who built a pricing service in Python.",
                    "bullets": [
                        {"text": "Replaced a nightly batch job with an internal pricing service in Python.", "fact_id": "exp_001:v1"},
                        {"text": "Owned the CI pipeline for 4 repositories.", "fact_id": "exp_002:v1"},
                    ],
                    "cover_letter": "I built a pricing service in Python.\nI owned the CI pipeline.",
                }
            ]
        )
        result = pipeline.tailor_application(session, application, llm=llm)
        assert result.ok, result.report
        assert application.state == "pending_review"
        assert application.resume_artifact_id is not None
        application_id = application.id

    # ---- the validator must reject a fabricated draft ----
    with session_scope() as session:
        other = Application(user_id=user_id, job_id=job_id, state="discovered")
        session.add(other)
        # UNIQUE(user_id, job_id) is the double-apply guard; confirm it holds.
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    with session_scope() as session:
        application = session.get(Application, application_id)
        assert replay_state(session, application.id) == "pending_review"

    # ---- human approves ----
    with session_scope() as session:
        application = session.get(Application, application_id)
        run = start_run(session, stage="review", application_id=application.id)
        transition(session, application, "approved", run=run, message="approved in test")

    # ---- stage 4: submit ----
    page = FakePage(greenhouse_form())
    monkeypatch.setattr(submit_mod, "human_pacing_delay", lambda *_a, **_k: 0.0)

    with session_scope() as session:
        application = session.get(Application, application_id)
        outcome = pipeline.submit_application(
            session,
            application,
            llm=StubLLM([]),
            browser_factory=lambda url: fake_browser(url, page=page),
        )
        assert outcome.state == "submitted", outcome.detail
        assert outcome.verified is True
        assert application.submitted_at is not None
        # Heuristics alone covered every field: no model call was needed.
        assert page.filled["#first_name"] == "Ada"
        assert page.filled["#email"] == "ada+apply@example.com"
        assert page.filled["#resume"].endswith(".pdf")
        assert outcome.screenshot_uri and outcome.dom_uri

    # ---- stage 5: outcome ----
    with session_scope() as session:
        llm = StubLLM([{"category": "interview_request", "company_guess": "ExampleCorp", "confidence": 0.9}])
        results = pipeline.process_messages(
            session,
            user_id,
            [
                outcome_mod.Message(
                    message_id="m1",
                    subject="Next steps",
                    sender="recruiting@examplecorp.com",
                    body="We would like to schedule a call.",
                )
            ],
            llm=llm,
        )
        assert results[0].state == "interview"
        assert session.get(Application, application_id).state == "interview"

    # ---- the log is a complete, replayable record ----
    with session_scope() as session:
        events = session.query(RunEvent).count()
        assert events > 5
        assert replay_state(session, application_id) == "interview"


def test_escalation_pauses_instead_of_guessing(clean_db, monkeypatch):
    """An unmapped required field must stop the run, not get filled with a guess."""
    from conftest import StubLLM

    from autoapply.db import session_scope
    from autoapply.events import start_run, transition
    from autoapply.llm.embeddings import get_embedder
    from autoapply.models import Application, Artifact, Company, Fact, Job, Profile, User
    from autoapply.pipeline import submit as submit_mod

    embedder = get_embedder()
    with session_scope() as session:
        user = User(email="ada2@example.com")
        session.add(user)
        session.flush()
        profile = Profile(user_id=user.id, version=1, base_resume_json={"name": "Ada Lovelace", "email": "a@b.co"})
        session.add(profile)
        session.flush()
        fact = Fact(id="f1", profile_id=profile.id, type="experience", text="Did a thing.", skills=[])
        fact.embedding = embedder.embed("did a thing")
        session.add(fact)
        company = Company(name="ExampleCorp", ats_type="greenhouse", board_token="ex2")
        session.add(company)
        session.flush()
        job = Job(
            company_id=company.id, ats_job_id="1", title="Engineer", location="Chicago",
            description="d", apply_url="https://boards.greenhouse.io/ex2/jobs/1",
            canonical_hash="h2", content_hash="c2",
        )
        artifact = Artifact(kind="resume_pdf", uri="local:///dev/null", sha256="x", bytes=1)
        session.add_all([job, artifact])
        session.flush()
        application = Application(
            user_id=user.id, job_id=job.id, state="discovered", resume_artifact_id=artifact.id
        )
        session.add(application)
        session.flush()
        run = start_run(session, stage="test", application_id=application.id)
        for state in ("matched", "drafted", "pending_review", "approved"):
            transition(session, application, state, run=run)
        application_id = application.id

    # A required question the synonym dictionary cannot place, and the stub model
    # honestly answers "unknown".
    fields = greenhouse_form() + [
        FakeHandle(
            "textarea",
            {"id": "q_why", "name": "question_why", "required": ""},
            label="Describe a system you are proud of and why it mattered.",
        )
    ]
    page = FakePage(fields)
    monkeypatch.setattr(submit_mod, "human_pacing_delay", lambda *_a, **_k: 0.0)
    monkeypatch.setattr(
        submit_mod.ArtifactStore, "get", lambda self, uri: b"%PDF-1.4 resume", raising=False
    )

    with session_scope() as session:
        application = session.get(Application, application_id)
        outcome = submit_mod.submit_application(
            session,
            application,
            llm=StubLLM([{"canonical": "unknown", "confidence": 0.1, "reason": "ambiguous"}]),
            browser_factory=lambda url: fake_browser(url, page=page),
        )

    assert outcome.state == "needs_input"
    assert page.clicked is False, "nothing may be submitted while a field is unresolved"
    assert outcome.escalation["reason"] in ("unmapped_required_field", "long_free_text")
    assert outcome.escalation["questions"]

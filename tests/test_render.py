"""HTML-level render tests. The PDF step just prints this HTML through Chromium,
so everything worth asserting is checkable without launching a browser."""

from __future__ import annotations

import types

from autoapply.pipeline.render import group_bullets, render_cover_letter_html, render_resume_html


def fact(**kwargs):
    base = {
        "id": "f1",
        "org": "Morningstar",
        "role": "Software Engineering Intern",
        "start_date": "2026-06",
        "end_date": None,
        "skills": ["python", "postgres"],
        "text": "Built a pricing service.",
    }
    return types.SimpleNamespace(**{**base, **kwargs})


PROFILE = types.SimpleNamespace(
    base_resume_json={
        "name": "Ada Lovelace",
        "headline": "Backend engineer",
        "email": "ada@example.com",
        "phone": "555-0100",
        "location": "Chicago, IL",
        "links": ["github.com/ada"],
    }
)
JOB = types.SimpleNamespace(title="Backend Engineer")
COMPANY = types.SimpleNamespace(name="ExampleCorp")


def test_bullets_group_under_the_role_of_the_fact_they_cite():
    facts = [fact(id="a", org="Morningstar", role="Intern"), fact(id="b", org="Acme", role="Engineer")]
    groups = group_bullets(
        [
            {"text": "one", "fact_id": "a"},
            {"text": "two", "fact_id": "b"},
            {"text": "three", "fact_id": "a"},
        ],
        facts,
    )
    assert [g["org"] for g in groups] == ["Morningstar", "Acme"]
    assert groups[0]["bullets"] == ["one", "three"]


def test_bullets_citing_unknown_facts_are_dropped_not_rendered():
    groups = group_bullets([{"text": "ghost", "fact_id": "nope"}], [fact(id="a")])
    assert groups == []


def test_open_ended_role_renders_as_present():
    groups = group_bullets([{"text": "x", "fact_id": "f1"}], [fact(end_date=None)])
    assert "Present" in groups[0]["window"]


def test_resume_html_includes_contact_bullets_and_skills():
    html = render_resume_html(
        PROFILE, JOB, COMPANY, "Backend engineer.", [{"text": "Built a thing.", "fact_id": "f1"}], [fact()]
    )
    assert "Ada Lovelace" in html
    assert "ada@example.com" in html
    assert "Built a thing." in html
    assert "python" in html and "postgres" in html


def test_render_escapes_html_in_generated_text():
    html = render_resume_html(
        PROFILE, JOB, COMPANY, "<script>alert(1)</script>", [{"text": "ok", "fact_id": "f1"}], [fact()]
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_cover_letter_splits_paragraphs_and_names_the_role():
    html = render_cover_letter_html(PROFILE, JOB, COMPANY, "First para.\n\nSecond para.")
    assert html.count("<p>") == 2
    assert "Backend Engineer" in html
    assert "ExampleCorp" in html

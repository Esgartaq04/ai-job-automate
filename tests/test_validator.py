"""The validator is the thing standing between this system and a fraudulent resume."""

from __future__ import annotations

from autoapply.pipeline.validator import Violation, validate_bullets, validate_prose


def _types(finding):
    return {violation for violation, _ in finding.violations}


def test_faithful_rephrase_passes(facts):
    report = validate_bullets(
        [{"text": "Replaced a nightly batch job with an internal pricing service in Python.", "fact_id": "exp_001"}],
        facts,
    )
    assert report.ok, report.as_payload()


def test_invented_metric_is_rejected(facts):
    # The classic failure: the fact says "replaced a nightly batch job", the model
    # decides that was worth 40%.
    report = validate_bullets(
        [{"text": "Built a pricing service in Python, cutting latency by 40%.", "fact_id": "exp_001"}],
        facts,
    )
    assert not report.ok
    assert Violation.NEW_NUMBER in _types(report.failures[0])


def test_number_present_in_source_is_allowed(facts):
    report = validate_bullets(
        [{"text": "Owned the CI pipeline for 4 repositories.", "fact_id": "exp_002"}], facts
    )
    assert report.ok, report.as_payload()


def test_invented_employer_is_rejected(facts):
    report = validate_bullets(
        [{"text": "Built a pricing service in Python at Goldman Sachs.", "fact_id": "exp_001"}], facts
    )
    assert not report.ok
    details = " ".join(detail for _, detail in report.failures[0].violations)
    assert "Goldman" in details


def test_invented_technology_is_rejected(facts):
    report = validate_bullets(
        [{"text": "Built the pricing service in Python on Kubernetes.", "fact_id": "exp_001"}], facts
    )
    assert not report.ok
    assert Violation.NEW_ENTITY in _types(report.failures[0])


def test_uncited_bullet_is_rejected(facts):
    report = validate_bullets([{"text": "Shipped a thing.", "fact_id": ""}], facts)
    assert Violation.MISSING_CITATION in _types(report.failures[0])


def test_citation_to_unknown_fact_is_rejected(facts):
    report = validate_bullets([{"text": "Shipped a thing.", "fact_id": "exp_999"}], facts)
    assert Violation.UNKNOWN_FACT_ID in _types(report.failures[0])


def test_skill_from_the_same_fact_is_allowed(facts):
    # "fastapi" is a listed skill on exp_001 even though it isn't in the prose.
    report = validate_bullets(
        [{"text": "Built a FastAPI pricing service in Python.", "fact_id": "exp_001"}], facts
    )
    assert report.ok, report.as_payload()


def test_cover_letter_may_name_the_employer_but_not_invent_history(facts):
    ok = validate_prose(
        "I am applying to Acme for the backend role. At Morningstar I built an internal "
        "pricing service in Python.",
        facts,
        extra_allowed={"Acme", "I", "backend"},
    )
    assert ok.ok, ok.violations

    bad = validate_prose(
        "At Morningstar I led a team of 12 engineers across three continents.",
        facts,
        extra_allowed={"I"},
    )
    assert not bad.ok
    assert Violation.NEW_NUMBER in _types(bad)


def test_empty_output_is_a_failure(facts):
    report = validate_bullets([{"text": "   ", "fact_id": "exp_001"}], facts)
    assert Violation.EMPTY in _types(report.failures[0])

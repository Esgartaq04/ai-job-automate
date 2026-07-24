from __future__ import annotations

import pytest

from autoapply.ats.base import Field, FieldKind
from autoapply.ats.fields import EEO_FIELDS, heuristic_map


def field(label: str, name: str | None = None, kind: FieldKind = FieldKind.TEXT) -> Field:
    return Field(selector=f"#{name or 'x'}", kind=kind, label=label, name=name)


@pytest.mark.parametrize(
    "label,name,expected",
    [
        ("First Name", "first_name", "first_name"),
        ("Given name", None, "first_name"),
        ("Last Name *", "last_name", "last_name"),
        ("Email", "email", "email"),
        ("Phone Number", "phone", "phone"),
        ("LinkedIn Profile", "urls[LinkedIn]", "linkedin"),
        ("Are you legally authorized to work in the US?", None, "work_authorized"),
        ("Will you now or in the future require sponsorship?", None, "requires_sponsorship"),
    ],
)
def test_common_fields_map_without_a_model_call(label, name, expected):
    canonical, confidence = heuristic_map(field(label, name))
    assert canonical == expected
    assert confidence >= 0.7, f"{label!r} should be confident enough to skip the LLM"


def test_first_name_beats_bare_name():
    # "name" is a substring of "first name"; longest-phrase wins.
    assert heuristic_map(field("First Name", "first_name"))[0] == "first_name"
    assert heuristic_map(field("Full Name", "name"))[0] == "full_name"


def test_file_input_defaults_to_resume_but_not_confidently():
    canonical, confidence = heuristic_map(field("", "resume_file", FieldKind.FILE))
    assert canonical == "resume"
    assert confidence < 0.9


def test_cover_letter_file_is_not_mistaken_for_a_resume():
    assert heuristic_map(field("Cover Letter", "cover_letter", FieldKind.FILE))[0] == "cover_letter"


def test_unrecognized_field_falls_through_to_the_model():
    canonical, confidence = heuristic_map(field("What is your favourite bug you've shipped?", "q_42"))
    assert canonical is None or confidence < 0.7


def test_signature_is_stable_and_case_insensitive():
    a = field("First Name", "first_name").signature
    b = field("  first name  ", "First_Name").signature
    assert a == b


def test_eeo_fields_are_recognized_so_they_can_be_left_alone():
    for label in ("Gender", "Race / Ethnicity", "Protected Veteran Status", "Disability Status"):
        canonical, _ = heuristic_map(field(label))
        assert canonical in EEO_FIELDS, label

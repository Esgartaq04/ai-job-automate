"""Gmail message parsing.

`parse_message` is a pure function over a Gmail API message resource, so the
MIME handling is testable without credentials, network, or the optional google
libraries being installed.
"""

from __future__ import annotations

import base64

from autoapply.pipeline.gmail import DEFAULT_QUERY, SCOPES, extract_body, parse_message


def b64(text: str) -> str:
    """Gmail returns base64url with the padding stripped."""
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def resource(payload, message_id="m1", internal_date="1750119882479"):
    return {"id": message_id, "internalDate": internal_date, "payload": payload}


def headers(**kwargs):
    return [{"name": k.replace("_", "-").title(), "value": v} for k, v in kwargs.items()]


def test_scope_is_read_only():
    # Anything beyond readonly would let a bug touch the user's real mailbox.
    assert SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert all("readonly" in scope for scope in SCOPES)


def test_default_query_is_time_windowed():
    assert "newer_than" in DEFAULT_QUERY


def test_parses_headers_and_plain_body():
    message = parse_message(
        resource(
            {
                "mimeType": "text/plain",
                "headers": headers(From="Recruiting <jobs@acme.com>", Subject="Next steps"),
                "body": {"data": b64("We would like to schedule a call.")},
            }
        )
    )
    assert message.message_id == "m1"
    assert message.sender == "Recruiting <jobs@acme.com>"
    assert message.subject == "Next steps"
    assert "schedule a call" in message.body


def test_prefers_plain_text_over_html_in_a_multipart():
    message = parse_message(
        resource(
            {
                "mimeType": "multipart/alternative",
                "headers": headers(Subject="Hi"),
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": b64("PLAIN VERSION")}},
                    {"mimeType": "text/html", "body": {"data": b64("<p>HTML VERSION</p>")}},
                ],
            }
        )
    )
    assert message.body == "PLAIN VERSION"


def test_falls_back_to_flattened_html_when_that_is_all_there_is():
    """ATS notifications are frequently HTML-only, so this is the common path."""
    message = parse_message(
        resource(
            {
                "mimeType": "multipart/alternative",
                "headers": headers(Subject="Hi"),
                "parts": [
                    {"mimeType": "text/html", "body": {"data": b64("<p>Thanks for <b>applying</b></p>")}}
                ],
            }
        )
    )
    assert "Thanks for applying" in message.body
    assert "<p>" not in message.body


def test_walks_nested_multiparts():
    payload = {
        "mimeType": "multipart/mixed",
        "headers": headers(Subject="Nested"),
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [{"mimeType": "text/plain", "body": {"data": b64("buried deep")}}],
            },
            {"mimeType": "application/pdf", "filename": "a.pdf", "body": {"attachmentId": "x"}},
        ],
    }
    assert "buried deep" in extract_body(payload)


def test_unpadded_base64url_decodes():
    # Length 5 forces padding that Gmail would have stripped.
    for text in ("a", "ab", "abc", "abcd", "abcde"):
        payload = {"mimeType": "text/plain", "body": {"data": b64(text)}}
        assert extract_body(payload) == text


def test_undecodable_part_does_not_raise():
    payload = {"mimeType": "text/plain", "body": {"data": "!!!not base64!!!"}}
    extract_body(payload)  # must not raise


def test_internal_date_becomes_an_aware_datetime():
    message = parse_message(resource({"headers": []}, internal_date="1750119882479"))
    assert message.received_at is not None
    assert message.received_at.tzinfo is not None
    assert 2020 < message.received_at.year < 2100  # ms-vs-seconds confusion


def test_bad_internal_date_is_tolerated():
    assert parse_message(resource({"headers": []}, internal_date="not-a-number")).received_at is None


def test_missing_headers_and_body_produce_an_empty_message_not_a_crash():
    message = parse_message({"id": "m9"})
    assert message.message_id == "m9"
    assert message.subject == "" and message.sender == "" and message.body == ""


def test_header_lookup_is_case_insensitive():
    message = parse_message(
        resource({"headers": [{"name": "sUbJeCt", "value": "Weird casing"}, {"name": "FROM", "value": "a@b.co"}]})
    )
    assert message.subject == "Weird casing"
    assert message.sender == "a@b.co"

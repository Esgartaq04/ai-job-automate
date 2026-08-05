from __future__ import annotations

import itertools

import pytest

from autoapply.ratelimit import CircuitBreaker, CircuitOpen, TokenBucket, backoff_delay
from autoapply.scrub import REDACTED, scrub


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_token_bucket_limits_burst_then_refills():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_min=60, burst=3, clock=clock)  # 1/sec

    assert [bucket.try_acquire() for _ in range(3)] == [True, True, True]
    assert bucket.try_acquire() is False

    clock.advance(2.0)
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False


def test_token_bucket_rejects_nonsense_rate():
    with pytest.raises(ValueError):
        TokenBucket(rate_per_min=0)


def test_circuit_trips_after_threshold_and_half_opens():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=3, reset_after=60.0, clock=clock)

    breaker.check()  # closed
    for _ in range(2):
        breaker.record_failure()
    breaker.check()  # still closed

    breaker.record_failure()
    assert breaker.state == "open"
    with pytest.raises(CircuitOpen):
        breaker.check()

    clock.advance(61)
    assert breaker.state == "half_open"
    breaker.check()  # allowed through to probe
    breaker.record_success()
    assert breaker.state == "closed"


def test_success_resets_the_failure_count():
    breaker = CircuitBreaker(failure_threshold=2)
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.state == "closed"


def test_backoff_is_bounded_and_jittered():
    values = [backoff_delay(attempt, base=1.0, cap=30.0, rng=lambda: 1.0) for attempt in range(8)]
    assert values[0] == 1.0
    assert max(values) <= 30.0
    assert values == sorted(values)
    # Jitter means a zero draw yields no delay.
    assert backoff_delay(5, rng=lambda: 0.0) == 0.0


def test_scrub_redacts_sensitive_keys_recursively():
    payload = {
        "user": {"email": "ada@example.com", "api_key": "sk-abcdef0123456789abcd"},
        "fields": [{"label": "Phone", "value": "312-555-0100"}],
        "authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
    }
    cleaned = scrub(payload)
    assert cleaned["user"]["api_key"] == REDACTED
    assert cleaned["authorization"] == REDACTED
    assert "ada@" not in cleaned["user"]["email"]
    assert cleaned["user"]["email"].endswith("@example.com")  # domain survives for debugging
    assert cleaned["fields"][0]["value"] == REDACTED


def test_scrub_redacts_secrets_embedded_in_free_text():
    text = "calling api with sk-livekey0123456789abcdef and ssn 123456789"
    cleaned = scrub(text)
    assert "sk-livekey" not in cleaned
    assert "123456789" not in cleaned


def test_scrub_leaves_ordinary_values_alone():
    assert scrub({"title": "Backend Engineer", "score": 0.82, "years": 4}) == {
        "title": "Backend Engineer",
        "score": 0.82,
        "years": 4,
    }


def test_scrub_handles_non_string_keys_and_tuples():
    assert scrub({1: ("a", "b")}) == {1: ["a", "b"]}
    assert scrub(list(itertools.repeat("ok", 2))) == ["ok", "ok"]

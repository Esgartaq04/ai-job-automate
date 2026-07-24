from __future__ import annotations

import pytest

from autoapply.events import TERMINAL_STATES, TRANSITIONS, can_transition


def test_happy_path_is_walkable():
    path = ["discovered", "matched", "drafted", "pending_review", "approved", "submitting", "submitted", "confirmed"]
    for src, dst in zip(path, path[1:], strict=False):
        assert can_transition(src, dst), f"{src} -> {dst} should be legal"


def test_cannot_skip_review():
    # The approval gate is the whole safety story; it must not be bypassable.
    assert not can_transition("drafted", "approved")
    assert not can_transition("matched", "submitting")
    assert not can_transition("discovered", "submitted")


def test_escalation_edges_exist():
    assert can_transition("submitting", "needs_input")
    assert can_transition("approved", "needs_input")
    assert can_transition("needs_input", "approved")


def test_unverified_submit_lands_in_failed_not_submitted():
    assert can_transition("submitting", "failed")
    assert can_transition("submitting", "blocked")


def test_terminal_states_are_terminal():
    assert TERMINAL_STATES == {"rejected", "declined"}
    for state in TERMINAL_STATES:
        assert TRANSITIONS[state] == set()


@pytest.mark.parametrize("state", sorted(TRANSITIONS))
def test_every_target_is_a_known_state(state):
    for target in TRANSITIONS[state]:
        assert target in TRANSITIONS, f"{state} -> {target} points at an undefined state"

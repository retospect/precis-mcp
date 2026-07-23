"""asa-slack's kind-allowlist policy — the hard enforcement behind "Slack
users can't kick off compute jobs" (not just prompt language)."""

from __future__ import annotations

from asa_slack.kind_policy import ALLOWED_KINDS, KNOWN_KINDS, slack_kinds_disabled


def test_allowed_is_subset_of_known():
    assert ALLOWED_KINDS <= KNOWN_KINDS


def test_compute_kinds_are_disabled():
    disabled = set(slack_kinds_disabled().split(","))
    for kind in ("job", "quest", "cron", "todo", "sandbox_run"):
        # sandbox_run isn't a registered kind (it's a job_type), but a
        # forward-compatible check costs nothing; the real guards are job/
        # quest/cron/todo.
        if kind in KNOWN_KINDS:
            assert kind in disabled, f"{kind} must be disabled for Slack turns"


def test_research_kinds_stay_enabled():
    disabled = set(slack_kinds_disabled().split(","))
    for kind in (
        "paper",
        "patent",
        "citation",
        "semanticscholar",
        "web",
        "websearch",
        "wikipedia",
        "perplexity-research",
        "perplexity-reasoning",
        "memory",
    ):
        assert kind not in disabled, f"{kind} must stay enabled for Slack turns"


def test_disabled_value_is_sorted_and_comma_joined():
    value = slack_kinds_disabled()
    parts = value.split(",")
    assert parts == sorted(parts)
    assert all(parts)  # no empty entries


def test_a_kind_neither_allowed_nor_known_is_not_silently_safe():
    # Documents the one real gap (see kind_policy's module docstring): a
    # brand new kind added to the live registry but never recorded in
    # KNOWN_KINDS here stays enabled by default. This test exists so that
    # gap is visible rather than assumed away — if the precis kind roster
    # ever exposes a live listing cheaply, tighten this into a real diff.
    assert "totally-made-up-kind" not in KNOWN_KINDS
    assert "totally-made-up-kind" not in slack_kinds_disabled().split(",")

"""Transient-failure classification at the ``record_failure`` funnel
(retryable child-failed, docs/backlog/todo-parked-transient-failures.md).

The classifier turns a failure ``reason`` that reads as transient
(rate/spend limits, transient API faults) into a ``meta.retry_after``
stamp on the failed job; the sweeper's unpark phase (tested in
``test_sweeper.py``) consumes the stamp. Tag shape stays uniform —
``child-failed:<job_id>`` — so nothing parsing the bubble changes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from precis.store import Store
from precis.workers.executors._common import (
    classify_transient_backoff_hours,
    record_failure,
)


@pytest.mark.parametrize(
    ("reason", "expected_hours"),
    [
        ("API error 429: rate limit exceeded", 0.25),
        ("anthropic.RateLimitError: overloaded_error", 0.25),
        ("Claude AI usage limit reached|1757203200", 2.0),
        ("run aborted: spending cap hit for this billing window", 2.0),
        ("Your credit balance is too low to access the API", 2.0),
        ("HTTPStatusError: 502 Bad Gateway from upstream", 0.5),
        ("service unavailable, please retry", 0.5),
        # Content-class failures must NOT classify as transient.
        ("AssertionError: expected 3 rows, got 4", None),
        ("plan_tick verdict: failed (hit split-cap 3 times)", None),
        ("KeyError: 'doi'", None),
    ],
)
def test_classify_transient_backoff_hours(
    reason: str, expected_hours: float | None
) -> None:
    assert classify_transient_backoff_hours(reason) == expected_hours


def test_classifier_prefers_shorter_rate_limit_horizon() -> None:
    """A reason naming both a rate limit and a spend cap is retryable at
    the shorter (rate-limit) horizon — first pattern wins."""
    reason = "429 rate limit while checking the spending cap"
    assert classify_transient_backoff_hours(reason) == 0.25


# ── weekly/session quota-limit classification (gr344988) ──────────────
#
# The Claude CLI's own quota-exhaustion message ("You've hit your weekly
# limit · resets 11am (UTC)") carries a literal 429 under the hood but
# must NOT fall into the generic 15-minute rate-limit horizon above — the
# sweeper would burn through its bounded unpark retries hours before the
# real reset and latch the leaf `child-failed-final`. These tests build
# the message from a live reference instant (not a fixed calendar time)
# so they never trip on a day/DST boundary.


def test_classify_transient_backoff_hours_weekly_quota_reset_parses() -> None:
    now = datetime.now(UTC)
    target = now + timedelta(hours=2, minutes=17)
    clock = target.strftime("%-I:%M%p").lower()  # e.g. "3:17pm"
    reason = (
        f"ClaudeAgentError: API error 429 - You've hit your weekly limit "
        f"· resets {clock} (UTC)"
    )

    hours = classify_transient_backoff_hours(reason)

    assert hours is not None
    computed_reset = datetime.now(UTC) + timedelta(hours=hours)
    assert abs((computed_reset - target).total_seconds()) < 60


def test_classify_transient_backoff_hours_session_quota_reset_other_tz() -> None:
    now = datetime.now(UTC)
    target = now + timedelta(hours=1, minutes=40)
    local = target.astimezone(ZoneInfo("America/Los_Angeles"))
    clock = local.strftime("%-I:%M%p").lower()
    reason = f"You've hit your session limit · resets {clock} (America/Los_Angeles)"

    hours = classify_transient_backoff_hours(reason)

    assert hours is not None
    computed_reset = datetime.now(UTC) + timedelta(hours=hours)
    assert abs((computed_reset - target).total_seconds()) < 60


def test_classify_transient_backoff_hours_quota_reset_unparseable_falls_back() -> None:
    reason = "You've hit your weekly limit · resets soon"
    assert classify_transient_backoff_hours(reason) == 6.0


def test_classify_transient_backoff_hours_quota_reset_missing_falls_back() -> None:
    reason = "You've hit your weekly limit"
    assert classify_transient_backoff_hours(reason) == 6.0


def test_classify_transient_backoff_hours_plain_429_unaffected() -> None:
    """A non-quota 429 (no 'hit your … limit' wording) keeps the generic
    15-minute rate-limit classification — the quota case must not
    swallow ordinary rate limiting."""
    assert classify_transient_backoff_hours("API error 429: rate limit exceeded") == (
        0.25
    )


def _fresh_job(store: Store) -> int:
    job = store.insert_ref(kind="job", slug=None, title="doomed job", meta={})
    return job.id


def _meta_of(store: Store, ref_id: int) -> dict:
    got = store.get_ref(kind="job", id=ref_id)
    assert got is not None
    return got.meta


def test_record_failure_stamps_retry_after_for_transient_reason(
    store: Store,
) -> None:
    job_id = _fresh_job(store)

    record_failure(
        store, job_id, "API error 429: rate limit exceeded", gripe_rollback=None
    )

    meta = _meta_of(store, job_id)
    assert meta["failure_class"] == "transient"
    assert "rate limit" in meta["error"]
    retry_after = datetime.fromisoformat(meta["retry_after"])
    assert retry_after > datetime.now(UTC)


def test_record_failure_stamps_retry_after_at_quota_reset_instant(
    store: Store,
) -> None:
    """The retry_after stamped for a weekly-quota 429 is the parsed reset
    instant, not the generic 15-minute rate-limit horizon — the sweeper
    must keep the leaf parked past it, not exhaust retries before it."""
    job_id = _fresh_job(store)
    now = datetime.now(UTC)
    target = now + timedelta(hours=5, minutes=3)
    clock = target.strftime("%-I:%M%p").lower()
    reason = f"API error 429 - You've hit your weekly limit · resets {clock} (UTC)"

    record_failure(store, job_id, reason, gripe_rollback=None)

    meta = _meta_of(store, job_id)
    assert meta["failure_class"] == "transient"
    retry_after = datetime.fromisoformat(meta["retry_after"])
    assert abs((retry_after - target).total_seconds()) < 60


def test_record_failure_leaves_non_transient_reason_unstamped(
    store: Store,
) -> None:
    job_id = _fresh_job(store)

    record_failure(
        store, job_id, "AssertionError: expected 3 rows, got 4", gripe_rollback=None
    )

    meta = _meta_of(store, job_id)
    assert "retry_after" not in meta
    assert "failure_class" not in meta


def test_record_failure_transient_keeps_caller_failure_class(
    store: Store,
) -> None:
    """An explicit ``failure_class`` from the caller (e.g. ``'infra'``)
    wins over the classifier's ``'transient'`` label; the ``retry_after``
    stamp still lands."""
    job_id = _fresh_job(store)

    record_failure(
        store,
        job_id,
        "container exited 1: service unavailable",
        gripe_rollback=None,
        failure_class="infra",
    )

    meta = _meta_of(store, job_id)
    assert meta["failure_class"] == "infra"
    assert "retry_after" in meta

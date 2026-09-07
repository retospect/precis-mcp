"""Transient-failure classification at the ``record_failure`` funnel
(retryable child-failed, docs/backlog/todo-parked-transient-failures.md).

The classifier turns a failure ``reason`` that reads as transient
(rate/spend limits, transient API faults) into a ``meta.retry_after``
stamp on the failed job; the sweeper's unpark phase (tested in
``test_sweeper.py``) consumes the stamp. Tag shape stays uniform —
``child-failed:<job_id>`` — so nothing parsing the bubble changes.
"""

from __future__ import annotations

from datetime import UTC, datetime

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

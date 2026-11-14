"""gr635381: ``record_failure``'s ``meta.error`` breadcrumb is stamped
on every failure — classified or not — and past ``_ERROR_META_CAP`` it
keeps the reason's tail (the traceback's last line, the actual
exception) instead of only its head.
"""

from __future__ import annotations

from precis.store import Store
from precis.workers.executors._common import _ERROR_META_CAP, record_failure


def _fresh_job(store: Store) -> int:
    job = store.insert_ref(kind="job", slug=None, title="doomed job", meta={})
    return job.id


def _meta_of(store: Store, ref_id: int) -> dict:
    got = store.get_ref(kind="job", id=ref_id)
    assert got is not None
    return got.meta


def test_record_failure_keeps_tail_of_long_reason(store: Store) -> None:
    job_id = _fresh_job(store)
    reason = "context line\n" * 400 + "ZeroDivisionError: x"
    assert len(reason) > _ERROR_META_CAP

    record_failure(store, job_id, reason, gripe_rollback=None)

    error = _meta_of(store, job_id)["error"]
    assert len(error) <= 520
    assert error.endswith("ZeroDivisionError: x")


def test_record_failure_stamps_error_without_failure_class(
    store: Store,
) -> None:
    """The crumb is unconditional now (gr635381): an unclassified,
    non-transient failure still lands ``meta.error`` — and still stamps
    no ``failure_class``."""
    job_id = _fresh_job(store)

    record_failure(
        store, job_id, "AssertionError: expected 3 rows, got 4", gripe_rollback=None
    )

    meta = _meta_of(store, job_id)
    assert meta["error"] == "AssertionError: expected 3 rows, got 4"
    assert "failure_class" not in meta


def test_record_failure_failure_class_semantics_unchanged(
    store: Store,
) -> None:
    """An explicit ``failure_class`` still lands beside the crumb — the
    class label isn't folded into or dropped by the always-stamp."""
    job_id = _fresh_job(store)

    record_failure(
        store,
        job_id,
        "container exited 1 mid-run",
        gripe_rollback=None,
        failure_class="infra",
    )

    meta = _meta_of(store, job_id)
    assert meta["failure_class"] == "infra"
    assert meta["error"] == "container exited 1 mid-run"

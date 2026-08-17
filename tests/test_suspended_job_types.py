"""Operator hold switch — ``PRECIS_SUSPENDED_JOB_TYPES``.

A suspended job_type's queued rows are never claimed (they wait in place;
in-flight rows are untouched), and ``dispatch_autocatpath`` stops minting
new fan-out trees while ``autocatpath_seed`` is held. The switch is the
"suspend spark autocatpath until further notice" lever (2026-08-17): the
deploy var ``precis_suspended_job_types`` renders it into every worker
unit, so it survives redeploys and resuming is a one-line var flip.
"""

from __future__ import annotations

import pytest

from precis.quest.compute import dispatch_autocatpath
from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors import suspended_job_types
from precis.workers.executors._common import claim_executor_jobs


def _queue_job(store: Store, *, executor: str, job_type: str | None) -> int:
    meta: dict = {"executor": executor, "params": {}}
    if job_type is not None:
        meta["job_type"] = job_type
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title=f"job type={job_type}",
        meta=meta,
    )
    store.add_tag(
        ref.id,
        Tag.closed("STATUS", "queued"),
        set_by="agent",
        replace_prefix=True,
    )
    return ref.id


def _claimed_ids(store: Store, executor: str) -> list[int]:
    """The ref_ids the claim would lock (rolled back — no side effects)."""
    with store.pool.connection() as conn:
        rows = claim_executor_jobs(conn, executor=executor, limit=50)
        conn.rollback()
    return [r[0] for r in rows]


def test_env_parse_trims_and_drops_empties(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_SUSPENDED_JOB_TYPES", " autocatpath_seed, ,demo ,")
    assert suspended_job_types() == frozenset({"autocatpath_seed", "demo"})
    monkeypatch.delenv("PRECIS_SUSPENDED_JOB_TYPES")
    assert suspended_job_types() == frozenset()


def test_suspended_type_is_not_claimed_others_are(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ex = "test_suspend_claim"
    held = _queue_job(store, executor=ex, job_type="autocatpath_seed")
    free = _queue_job(store, executor=ex, job_type="demo")
    monkeypatch.setenv(
        "PRECIS_SUSPENDED_JOB_TYPES", "autocatpath_seed,autocatpath_aggregate"
    )
    assert _claimed_ids(store, ex) == [free]
    # Hold cleared → the held row is claimable again, nothing was mutated.
    monkeypatch.delenv("PRECIS_SUSPENDED_JOB_TYPES")
    assert _claimed_ids(store, ex) == [held, free]


def test_job_without_job_type_stays_claimable_under_a_hold(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The COALESCE guard: a row with no ``meta.job_type`` can't match any
    suspended name — NULL semantics must not silently exclude it."""
    ex = "test_suspend_null_type"
    untyped = _queue_job(store, executor=ex, job_type=None)
    monkeypatch.setenv("PRECIS_SUSPENDED_JOB_TYPES", "autocatpath_seed")
    assert _claimed_ids(store, ex) == [untyped]


def test_dispatch_autocatpath_mints_nothing_while_seed_held(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_SUSPENDED_JOB_TYPES", "autocatpath_seed")
    note = dispatch_autocatpath(
        store, 999_999_999, {"substrate": "NO", "target": "NH3"}
    )
    assert "suspended" in note and "PRECIS_SUSPENDED_JOB_TYPES" in note


def test_dispatch_worker_skips_mint_for_held_type(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispatch worker mints no job for a suspended job_type — the
    parent stays an untagged ordinary candidate and mints once the hold
    clears (no halt, no failure count)."""
    from precis.workers.dispatch import run_dispatch_pass

    parent = store.insert_ref(
        kind="todo",
        slug=None,
        title="held compute rollup",
        meta={"executor": "ssh_node", "job_type": "struct_relax", "params": {}},
        parent_id=None,
    )
    monkeypatch.setenv("PRECIS_SUSPENDED_JOB_TYPES", "struct_relax")
    result = run_dispatch_pass(store)
    assert result.failed == 0
    assert _jobs_under(store, int(parent.id)) == []
    # Hold cleared → the same sweep path mints normally.
    monkeypatch.delenv("PRECIS_SUSPENDED_JOB_TYPES")
    run_dispatch_pass(store)
    assert len(_jobs_under(store, int(parent.id))) == 1


def _jobs_under(store: Store, parent_id: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs "
            "WHERE parent_id = %s AND kind = 'job' AND deleted_at IS NULL",
            (parent_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]

"""Pins the one boundary §M leaves standing: todo ↔ job.

A ``job`` is claimed/leased/executor-run — ``FOR UPDATE SKIP LOCKED``,
``meta.lease_until``, the sweeper, lease-steal, reserve-at-claim slots
(:func:`precis.workers.executors._common.claim_executor_jobs`). A
``todo`` is durable intent and is *never* leased: the dispatch worker's
candidate enumeration (:func:`precis.workers.dispatch._candidate_parent_ids`)
is a plain, lock-free ``SELECT`` — any number of concurrent readers see
the same candidate set, because nothing about "being a doable/dispatch
candidate" is a claim. The todo/job/finding kind boundary rules a todo/job merge out explicitly on
this physical ground: a job is claimed, a todo is not — merging would
force row-lock contention or two state machines onto one ref.

These two tests are the durable regression: a job claim genuinely
excludes a concurrent second claimant (real row-lock contention); a
todo's candidate read never does (no lock at all).
"""

from __future__ import annotations

from precis.store import Store
from precis.store.types import Tag
from precis.workers.dispatch import _candidate_parent_ids
from precis.workers.executors._common import claim_executor_jobs


def test_job_claim_holds_a_real_row_lock_skip_locked(store: Store) -> None:
    """A queued job claimed (uncommitted) by one connection is invisible
    to a second connection's claim of the same executor's queue —
    genuine ``FOR UPDATE SKIP LOCKED`` mutual exclusion. This is what
    "a job leases" means physically: the claim is a row lock a second
    claimant cannot also acquire."""
    executor = "test_boundary_job_lease"
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="leased job",
        meta={"job_type": "demo", "executor": executor, "params": {}},
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", "queued"), set_by="agent", replace_prefix=True
    )

    with store.pool.connection() as conn_a:
        # Connection A claims (and holds — no commit/rollback yet) the row.
        claimed_a = claim_executor_jobs(conn_a, executor=executor, limit=10)
        assert [r[0] for r in claimed_a] == [job.id]

        # Connection B races the same claim while A still holds the lock.
        # SKIP LOCKED means B sees nothing for this row — proof the claim
        # is a real, exclusive row lock, not an advisory / cooperative flag.
        with store.pool.connection() as conn_b:
            claimed_b = claim_executor_jobs(conn_b, executor=executor, limit=10)
            assert claimed_b == []
            conn_b.rollback()

        conn_a.rollback()

    # Lock released — a fresh claim now succeeds (the row was never
    # mutated by either probe above, so it's still queued and claimable).
    with store.pool.connection() as conn_c:
        claimed_c = claim_executor_jobs(conn_c, executor=executor, limit=10)
        assert [r[0] for r in claimed_c] == [job.id]
        conn_c.rollback()


def test_todo_candidate_enumeration_never_excludes_concurrent_readers(
    store: Store,
) -> None:
    """A dispatch-eligible todo is a candidate for every concurrent
    reader at once — enumeration is a plain SELECT, never a claim.

    Unlike the job claim above, holding a read-side lock on the todo row
    from one connection must NOT hide it from a second connection's
    candidate enumeration: a todo is durable intent, not a leasable unit. ``_candidate_parent_ids`` issues no ``FOR UPDATE`` at
    all, so two "workers" independently computing the candidate set see
    the identical todo — there is no lease to contend over."""
    ref = store.insert_ref(
        kind="todo",
        slug=None,
        title="dispatch-eligible leaf",
        meta={"llm_tier": "opus"},
    )

    with store.pool.connection() as conn_a:
        # Take an explicit row lock on the todo from connection A — the
        # closest thing to "leasing" it a hostile caller could attempt.
        conn_a.execute("SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE", (ref.id,))

        # Connection B's ordinary candidate enumeration must still see
        # the ref: dispatch reads todos, it never locks them.
        ids_b = _candidate_parent_ids(store, limit=50)
        assert ref.id in ids_b

        conn_a.rollback()

    # And the real dispatcher-facing enumeration agrees post-lock too —
    # same set either way, because the lock was never load-bearing.
    ids_after = _candidate_parent_ids(store, limit=50)
    assert ref.id in ids_after

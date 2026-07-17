"""Tests for the Slice-5 dispatch worker (``workers/dispatch.py``).

Covers candidate enumeration, the FOR UPDATE SKIP LOCKED claim,
child job minting with the right meta, auto_check auto-injection,
and the rejection paths (unknown executor / job_type / incompatible
combo).
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.workers.dispatch import run_dispatch_pass
from tests.conftest import id_of


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _child_jobs_under(store: Store, parent_id: int) -> list[dict]:
    """Fetch metadata for every kind='job' child of ``parent_id``."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, title, meta FROM refs "
            "WHERE parent_id = %s AND kind = 'job' AND deleted_at IS NULL "
            "ORDER BY ref_id",
            (parent_id,),
        ).fetchall()
    return [{"id": int(r[0]), "title": r[1], "meta": r[2]} for r in rows]


# ── candidate enumeration ────────────────────────────────────────


def test_no_executor_no_dispatch(handler: TodoHandler, store: Store) -> None:
    """A todo without meta.executor is not a candidate."""
    handler.put(text="plain todo, no executor")
    result = run_dispatch_pass(store)
    assert result.claimed == 0
    assert result.ok == 0


def test_skips_when_child_job_exists(handler: TodoHandler, store: Store) -> None:
    """Once a child job exists, no further dispatch (bubble-up rule)."""
    r = handler.put(
        text="dispatchable",
        meta={
            "executor": "claude_inproc",
            "job_type": "fix_gripe",
            "params": {},
        },
    )
    rid = id_of(r.body)
    # Pre-seed a child job so the dispatcher should skip.
    store.insert_ref(kind="job", slug=None, title="prior", meta={}, parent_id=rid)
    result = run_dispatch_pass(store)
    assert result.claimed == 0
    # Still only the pre-seeded one.
    assert len(_child_jobs_under(store, rid)) == 1


def test_skips_paused_parent(handler: TodoHandler, store: Store) -> None:
    r = handler.put(
        text="paused",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe"},
    )
    rid = id_of(r.body)
    from precis.store.types import Tag

    store.add_tag(
        rid, Tag.closed("STATUS", "paused"), set_by="agent", replace_prefix=True
    )
    result = run_dispatch_pass(store)
    assert result.claimed == 0
    assert _child_jobs_under(store, rid) == []


def test_skips_done_parent(handler: TodoHandler, store: Store) -> None:
    r = handler.put(
        text="done",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe"},
    )
    rid = id_of(r.body)
    handler.tag(id=rid, add=["STATUS:done"])
    result = run_dispatch_pass(store)
    assert result.claimed == 0


def test_skips_halted_parent(handler: TodoHandler, store: Store) -> None:
    """Halt tag on the parent must keep the dispatcher off it.

    Same registry as ``view='doable'``: ``halt`` belongs in
    ``_DOABLE_EXCLUSION_TAGS`` and both surfaces honour it.
    """
    from precis.store.types import Tag

    r = handler.put(
        text="halted dispatch target",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe"},
    )
    rid = id_of(r.body)
    store.add_tag(rid, Tag.open("halt"), set_by="user")
    result = run_dispatch_pass(store)
    assert result.claimed == 0
    assert _child_jobs_under(store, rid) == []


# ── happy path ───────────────────────────────────────────────────


def test_mints_child_job_under_parent(handler: TodoHandler, store: Store) -> None:
    r = handler.put(
        text="ready to dispatch",
        meta={
            "executor": "claude_inproc",
            "job_type": "fix_gripe",
            "params": {"key": "value"},
        },
    )
    rid = id_of(r.body)
    result = run_dispatch_pass(store)
    assert result.claimed == 1
    assert result.ok == 1
    children = _child_jobs_under(store, rid)
    assert len(children) == 1
    child = children[0]
    assert child["meta"]["job_type"] == "fix_gripe"
    assert child["meta"]["executor"] == "claude_inproc"
    assert child["meta"]["dispatched_from_todo"] == rid
    assert child["meta"]["params"] == {"key": "value"}
    # The child has STATUS:queued.
    tags = {str(t) for t in store.tags_for(child["id"])}
    assert "STATUS:queued" in tags
    # A dispatch event was appended on the parent.
    events = store.events_for(rid)
    assert any(e.event == "job-minted" and e.source == "dispatch" for e in events)


def test_auto_injects_auto_check_when_missing(
    handler: TodoHandler, store: Store
) -> None:
    """Parent didn't write meta.auto_check → dispatcher injects default."""
    r = handler.put(
        text="needs auto-check",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe"},
    )
    rid = id_of(r.body)
    run_dispatch_pass(store)
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None
    assert ref.meta.get("auto_check") == {"type": "child_job_succeeded"}


def test_preserves_existing_auto_check(handler: TodoHandler, store: Store) -> None:
    """Caller-supplied auto_check survives dispatch unchanged."""
    custom = {"type": "time_past", "at": "2099-01-01T00:00:00+00:00"}
    r = handler.put(
        text="explicit auto-check",
        meta={
            "executor": "claude_inproc",
            "job_type": "fix_gripe",
            "auto_check": custom,
        },
    )
    rid = id_of(r.body)
    run_dispatch_pass(store)
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None
    assert ref.meta.get("auto_check") == custom


def test_plan_tick_parent_gets_no_auto_check(
    handler: TodoHandler, store: Store
) -> None:
    """An LLM:*-tagged (plan_tick) parent must NOT get child_job_succeeded.

    The planner coroutine drives its own STATUS; a clean tick exits
    STATUS:succeeded even when it yielded or minted children. Injecting
    child_job_succeeded would auto-close the parent on its first tick.
    """
    r = handler.put(text="planner brief", tags=["LLM:opus"])
    rid = id_of(r.body)
    run_dispatch_pass(store)
    children = _child_jobs_under(store, rid)
    assert len(children) == 1
    assert children[0]["meta"]["job_type"] == "plan_tick"
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None
    assert "auto_check" not in ref.meta


def test_plan_tick_parent_strips_stale_child_job_succeeded(
    handler: TodoHandler, store: Store
) -> None:
    """A planner parent carrying a stale child_job_succeeded auto_check
    has it STRIPPED on dispatch.

    Declining to inject (test above) isn't enough when a legacy /
    hand-authored spec is already attached — that's exactly what
    auto-closed an in-progress paper cascade on its first clean tick.
    """
    r = handler.put(
        text="planner brief with stale footgun",
        tags=["LLM:opus"],
        meta={"auto_check": {"type": "child_job_succeeded"}},
    )
    rid = id_of(r.body)
    run_dispatch_pass(store)
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None
    assert "auto_check" not in ref.meta


def test_plan_tick_parent_keeps_non_footgun_auto_check(
    handler: TodoHandler, store: Store
) -> None:
    """Only the footgun type is stripped — a deliberate non-job auto_check
    on a planner survives."""
    custom = {"type": "time_past", "at": "2099-01-01T00:00:00+00:00"}
    r = handler.put(
        text="planner with deliberate timer",
        tags=["LLM:opus"],
        meta={"auto_check": custom},
    )
    rid = id_of(r.body)
    run_dispatch_pass(store)
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None
    assert ref.meta.get("auto_check") == custom


def test_succeeded_child_job_does_not_block_redispatch(
    handler: TodoHandler, store: Store
) -> None:
    """A terminal STATUS:succeeded job is a completed prior tick, not a
    live one — the planner parent must remain re-dispatchable."""
    from precis.store.types import Tag
    from precis.workers.dispatch import _candidate_parent_ids

    parent = handler.put(text="planner", tags=["LLM:opus"])
    pid = id_of(parent.body)
    job = store.insert_ref(
        kind="job", slug=None, title="prior tick", meta={}, parent_id=pid
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", "succeeded"), set_by="system", replace_prefix=True
    )
    assert pid in _candidate_parent_ids(store, limit=10)


def test_running_child_job_blocks_redispatch(
    handler: TodoHandler, store: Store
) -> None:
    """A non-terminal (running) job is in-flight and DOES block — guards
    against the dispatcher double-minting while a tick is live."""
    from precis.store.types import Tag
    from precis.workers.dispatch import _candidate_parent_ids

    parent = handler.put(text="planner", tags=["LLM:opus"])
    pid = id_of(parent.body)
    job = store.insert_ref(
        kind="job", slug=None, title="in flight", meta={}, parent_id=pid
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", "running"), set_by="system", replace_prefix=True
    )
    assert pid not in _candidate_parent_ids(store, limit=10)


# ── rejection paths ──────────────────────────────────────────────


def test_skips_unknown_executor(handler: TodoHandler, store: Store) -> None:
    r = handler.put(
        text="bad executor",
        meta={"executor": "imaginary", "job_type": "fix_gripe"},
    )
    rid = id_of(r.body)
    result = run_dispatch_pass(store)
    assert result.claimed == 1
    assert result.ok == 0
    assert result.failed == 1
    assert _child_jobs_under(store, rid) == []


def _open_tag_values(store: Store, ref_id: int) -> set[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = 'OPEN'",
            (ref_id,),
        ).fetchall()
    return {str(r[0]) for r in rows}


def test_unknown_executor_halts_parent_and_stops_re_dispatch(
    handler: TodoHandler, store: Store
) -> None:
    """A mis-configured parent self-halts so it stops flooding logs.

    Regression guard: a bogus executor used to warn-and-skip on *every*
    sweep (the parent stayed a candidate forever). Now the first sweep
    tags ``halt:bad-dispatch`` and the second sweep no longer claims it.
    """
    r = handler.put(
        text="bad executor",
        meta={"executor": "plan_tick", "job_type": "plan_tick"},
    )
    rid = id_of(r.body)

    first = run_dispatch_pass(store)
    assert first.claimed == 1
    assert first.failed == 1
    assert "halt:bad-dispatch" in _open_tag_values(store, rid)

    # The halt tag drops it from candidacy: no re-claim, no re-warn.
    second = run_dispatch_pass(store)
    assert second.claimed == 0
    assert _child_jobs_under(store, rid) == []


def test_skips_unknown_job_type(handler: TodoHandler, store: Store) -> None:
    r = handler.put(
        text="bad job_type",
        meta={
            "executor": "claude_inproc",
            "job_type": "simulate_warp_drive",
        },
    )
    rid = id_of(r.body)
    result = run_dispatch_pass(store)
    assert result.claimed == 1
    assert result.ok == 0
    assert result.failed == 1
    assert _child_jobs_under(store, rid) == []


def test_skips_missing_job_type(handler: TodoHandler, store: Store) -> None:
    r = handler.put(
        text="executor without job_type",
        meta={"executor": "claude_inproc"},
    )
    rid = id_of(r.body)
    result = run_dispatch_pass(store)
    assert result.claimed == 1
    assert result.ok == 0


# ── failure-bubble ───────────────────────────────────────────────


def test_bubble_helper_tags_parent_on_job_failure(
    handler: TodoHandler, store: Store
) -> None:
    """The bubble helper tags the parent todo ``child-failed:<job_id>``."""
    from precis.handlers._job_bubble import bubble_job_failure

    r = handler.put(text="Parent")
    rid = id_of(r.body)
    job = store.insert_ref(
        kind="job", slug=None, title="failed job", meta={}, parent_id=rid
    )
    bubble_job_failure(store, job.id)
    tags = {str(t) for t in store.tags_for(rid)}
    assert f"child-failed:{job.id}" in tags


def test_bubble_helper_noop_for_orphan_job(store: Store) -> None:
    """A job without parent_id (legacy) doesn't crash the bubble."""
    from precis.handlers._job_bubble import bubble_job_failure

    job = store.insert_ref(kind="job", slug=None, title="orphan", meta={})
    # Should not raise.
    bubble_job_failure(store, job.id)


def test_job_handler_tag_bubbles_status_failed(
    handler: TodoHandler, store: Store
) -> None:
    """``JobHandler.tag(add=['STATUS:failed'])`` triggers the bubble."""
    from precis.dispatch import Hub
    from precis.handlers.job import JobHandler

    job_handler = JobHandler(hub=Hub(store=store, embedder=None))
    r = handler.put(text="parent")
    rid = id_of(r.body)
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="will fail",
        meta={"job_type": "fix_gripe", "executor": "claude_inproc"},
        parent_id=rid,
    )
    from precis.store.types import Tag

    store.add_tag(
        job.id,
        Tag.closed("STATUS", "queued"),
        set_by="agent",
        replace_prefix=True,
    )
    job_handler.tag(id=job.id, add=["STATUS:failed"])
    parent_tags = {str(t) for t in store.tags_for(rid)}
    assert f"child-failed:{job.id}" in parent_tags


def test_job_handler_tag_other_status_does_not_bubble(
    handler: TodoHandler, store: Store
) -> None:
    """Tagging STATUS:succeeded doesn't add child-failed."""
    from precis.dispatch import Hub
    from precis.handlers.job import JobHandler

    job_handler = JobHandler(hub=Hub(store=store, embedder=None))
    r = handler.put(text="parent")
    rid = id_of(r.body)
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="ok",
        meta={"job_type": "fix_gripe", "executor": "claude_inproc"},
        parent_id=rid,
    )
    from precis.store.types import Tag

    store.add_tag(
        job.id,
        Tag.closed("STATUS", "running"),
        set_by="agent",
        replace_prefix=True,
    )
    job_handler.tag(id=job.id, add=["STATUS:succeeded"])
    parent_tags = {str(t) for t in store.tags_for(rid)}
    assert not any(t.startswith("child-failed:") for t in parent_tags)


# ── concurrency ──────────────────────────────────────────────────


def test_row_lock_serialises_concurrent_dispatch(
    handler: TodoHandler, store: Store
) -> None:
    """Holding the parent's row lock in tx A blocks dispatch in tx B."""
    r = handler.put(
        text="locked target",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe"},
    )
    rid = id_of(r.body)

    holder = store.pool.getconn()
    try:
        holder.execute("BEGIN")
        row = holder.execute(
            "SELECT ref_id FROM refs WHERE ref_id = %s FOR UPDATE",
            (rid,),
        ).fetchone()
        assert row is not None
        # While held, a parallel dispatch sees the row as SKIPPED.
        result = run_dispatch_pass(store)
        assert result.claimed == 0
        assert _child_jobs_under(store, rid) == []
        holder.execute("COMMIT")
    finally:
        store.pool.putconn(holder)

    # After release, the next pass mints normally.
    result2 = run_dispatch_pass(store)
    assert result2.claimed == 1
    assert result2.ok == 1


# ── prio propagation (slice 6a) ──────────────────────────────────


def _job_prio(store: Store, job_id: int) -> int | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT prio FROM refs WHERE ref_id = %s", (job_id,)
        ).fetchone()
    return None if row[0] is None else int(row[0])


def test_minted_job_inherits_parent_prio(handler: TodoHandler, store: Store) -> None:
    """A high-prio parent todo flows its prio onto the minted job (6a)."""
    r = handler.put(
        text="high-prio work",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe", "params": {}},
        prio=9,
    )
    rid = id_of(r.body)
    result = run_dispatch_pass(store)
    assert result.ok == 1
    jobs = _child_jobs_under(store, rid)
    assert len(jobs) == 1
    assert _job_prio(store, jobs[0]["id"]) == 9


def test_minted_job_prio_null_when_parent_unset(
    handler: TodoHandler, store: Store
) -> None:
    """An unset parent prio stays NULL on the job → claim's COALESCE default."""
    r = handler.put(
        text="commodity work",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe"},
    )
    rid = id_of(r.body)
    run_dispatch_pass(store)
    jobs = _child_jobs_under(store, rid)
    assert len(jobs) == 1
    assert _job_prio(store, jobs[0]["id"]) is None

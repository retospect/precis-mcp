"""Coordinator-parented child jobs (ADR 0044 extension) + ``ctx.spawn_child``.

good-search-coordinator §Substrate fixes #3 / §Gaps 1: a ``kind='job'``
may parent on another job, but ONLY when that parent is itself a
coordinator (``meta.executor == 'coordinator'``) — a campaign's fan-out
children hang under the coordinator that minted them, not under a todo.
``DispatchContext.spawn_child`` is the fan-out primitive: it routes
through ``JobHandler.put`` (registry / executor / params validation +
``idem_key`` dedupe), injects no ``auto_check`` anywhere, and requires
no link. A failing child of a coordinator bubbles nowhere (no
``requested`` requester → ``_job_bubble`` is a logged no-op).
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.job import JobHandler
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors._common import claim_executor_jobs
from precis.workers.executors.claude_inproc import _build_dispatch_context
from tests.conftest import id_of


@pytest.fixture
def todos(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


@pytest.fixture
def jobs(hub: Hub) -> JobHandler:
    return JobHandler(hub=hub)


# ── helpers ─────────────────────────────────────────────────────────


_COORD_META: dict[str, Any] = {
    "job_type": "demo_campaign",
    "executor": "coordinator",
    "params": {},
}


def _mk_job(
    store: Store,
    *,
    parent_id: int,
    executor: str,
    status: str = "running",
    job_type: str = "demo",
) -> int:
    """Insert a ``kind='job'`` ref directly (bypassing the handler)."""
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title=f"{job_type} (unlinked)",
        meta={"job_type": job_type, "executor": executor, "params": {}},
        parent_id=parent_id,
    )
    store.add_tag(
        ref.id,
        Tag.closed("STATUS", status),
        set_by="agent",
        replace_prefix=True,
    )
    return ref.id


def _row(store: Store, ref_id: int) -> tuple[int | None, dict[str, Any]]:
    with store.pool.connection() as conn:
        r = conn.execute(
            "SELECT parent_id, meta FROM refs WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
    assert r is not None
    return (int(r[0]) if r[0] is not None else None, dict(r[1] or {}))


def _tags(store: Store, ref_id: int) -> set[str]:
    return {str(t) for t in store.tags_for(ref_id)}


@pytest.fixture
def campaign(todos: TodoHandler, store: Store) -> tuple[int, int]:
    """A todo root + a coordinator job under it: ``(todo_id, coord_id)``."""
    todo_id = id_of(todos.put(text="campaign root").body)
    coord_id = _mk_job(
        store,
        parent_id=todo_id,
        executor="coordinator",
        job_type="demo_campaign",
    )
    return todo_id, coord_id


# ── put(kind='job', parent_id=<job>) — the ADR 0044 extension ──────


class TestCoordinatorJobParent:
    def test_put_accepts_coordinator_job_parent(
        self, jobs: JobHandler, store: Store, campaign: tuple[int, int]
    ) -> None:
        _todo_id, coord_id = campaign
        resp = jobs.put(
            job_type="plan_tick",
            parent_id=coord_id,
            params={"model": "haiku"},
        )
        assert "created job id=" in resp.body
        child_id = id_of(resp.body)
        parent_id, meta = _row(store, child_id)
        assert parent_id == coord_id
        assert meta["job_type"] == "plan_tick"

    def test_put_rejects_ordinary_job_parent(
        self, jobs: JobHandler, todos: TodoHandler, store: Store
    ) -> None:
        """A non-coordinator job (executor='claude_inproc') can't own
        a child tree."""
        todo_id = id_of(todos.put(text="root").body)
        plain_id = _mk_job(store, parent_id=todo_id, executor="claude_inproc")
        with pytest.raises(BadInput, match="may only parent on a coordinator job"):
            jobs.put(
                job_type="plan_tick",
                parent_id=plain_id,
                params={"model": "haiku"},
            )

    def test_put_rejects_disallowed_parent_kind_unchanged(
        self, jobs: JobHandler, store: Store
    ) -> None:
        """The pre-existing rejection for kinds outside the parent set
        keeps its shape."""
        mem = store.insert_ref(kind="memory", slug=None, title="m", meta={})
        with pytest.raises(BadInput, match="a job parents on a todo"):
            jobs.put(
                job_type="plan_tick",
                parent_id=mem.id,
                params={"model": "haiku"},
            )


# ── ctx.spawn_child ────────────────────────────────────────────────


class TestSpawnChild:
    def _ctx(self, store: Store, coord_id: int) -> Any:
        return _build_dispatch_context(store, coord_id, "campaign", dict(_COORD_META))

    def test_spawn_child_mints_claimable_job_under_coordinator(
        self, store: Store, campaign: tuple[int, int]
    ) -> None:
        todo_id, coord_id = campaign
        ctx = self._ctx(store, coord_id)

        child_id = ctx.spawn_child("plan_tick", {"timeout_s": 60}, model="haiku")

        parent_id, meta = _row(store, child_id)
        assert parent_id == coord_id
        assert meta["job_type"] == "plan_tick"
        assert meta["executor"] == "claude_inproc"
        # model= folds into params.model; the seed params survive.
        assert meta["params"] == {"timeout_s": 60, "model": "haiku"}
        assert "STATUS:queued" in _tags(store, child_id)

        # No auto_check injected anywhere — the coordinator reads child
        # status itself on resume.
        for rid in (child_id, coord_id, todo_id):
            assert "auto_check" not in _row(store, rid)[1]

        # Claimable by the child's executor.
        with store.pool.connection() as conn:
            claimed = claim_executor_jobs(conn, executor="claude_inproc", limit=10)
            conn.rollback()
        assert child_id in {r[0] for r in claimed}

    def test_spawn_child_rejects_unknown_job_type(
        self, store: Store, campaign: tuple[int, int]
    ) -> None:
        _todo_id, coord_id = campaign
        ctx = self._ctx(store, coord_id)
        with pytest.raises(BadInput, match="unknown job_type"):
            ctx.spawn_child("simulate_warp_drive", {})

    def test_spawn_child_rejects_incompatible_executor(
        self, store: Store, campaign: tuple[int, int]
    ) -> None:
        _todo_id, coord_id = campaign
        ctx = self._ctx(store, coord_id)
        with pytest.raises(BadInput, match="does not support executor"):
            ctx.spawn_child("plan_tick", {"model": "haiku"}, executor="coordinator")

    def test_spawn_child_idem_key_dedupes(
        self, store: Store, campaign: tuple[int, int]
    ) -> None:
        _todo_id, coord_id = campaign
        ctx = self._ctx(store, coord_id)
        first = ctx.spawn_child(
            "plan_tick", {"model": "haiku"}, idem_key="triage-batch-0"
        )
        second = ctx.spawn_child(
            "plan_tick", {"model": "haiku"}, idem_key="triage-batch-0"
        )
        assert first == second


# ── failure-bubble is a no-op for coordinator-parented children ────


class TestBubbleNoop:
    def test_child_failure_bubbles_nowhere(
        self, jobs: JobHandler, store: Store, campaign: tuple[int, int]
    ) -> None:
        """A failing child of a coordinator has no ``requested``
        requester, so ``_job_bubble`` logs and no-ops: no
        ``child-failed:`` tag on the coordinator or the grandparent
        todo (the coordinator tolerates partial child failure)."""
        todo_id, coord_id = campaign
        ctx = _build_dispatch_context(store, coord_id, "campaign", dict(_COORD_META))
        child_id = ctx.spawn_child("plan_tick", {"model": "haiku"})

        jobs.tag(id=child_id, add=["STATUS:failed"])

        assert "STATUS:failed" in _tags(store, child_id)
        bubble = f"child-failed:{child_id}"
        assert not any(t.startswith("child-failed:") for t in _tags(store, coord_id))
        assert bubble not in _tags(store, todo_id)
        assert not any(t.startswith("child-failed:") for t in _tags(store, todo_id))

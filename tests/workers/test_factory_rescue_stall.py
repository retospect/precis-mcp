"""factory_rescue.run_stall_pass — the stall watchdog (dark-factory
td574946, slice S2): every factory failure ends in done or rescue. A
``factory_rescue_tick`` job finds the plan nodes that went silent (never
dispatched, cancelled job, queued/build/land/review overrunning its stage
deadline, a sha-in-prod gate that never opens, a root closed under an
open node, a land's post-write that never landed) and sends each to
RESCUE through ``enter_rescue`` — plus the ``tick-silent`` heartbeat on
``plan_dispatch`` itself. A hold is not a stall: ``waiting-for:``,
``halt``, paused, an open ask, ``rescue:`` or a live ``blocked-by``
target exempt a node."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from precis.plans.lanes import LANE_PARK_WHY, Lane
from precis.plans.templates import PENDING_SHA
from precis.store import Store
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.workers import factory_rescue as fr
from precis.workers._mint import mint_background_job
from precis.workers.executors import _common
from precis.workers.executors._common import claim_executor_jobs, park_job_until
from precis.workers.job_types import get_job_type, known_job_types
from tests._lane_rows import use_lanes

# Rows carry database-stamped times (``now()`` at insert), so ``NOW`` must be
# the instant the test runs, not the import: a long xdist shard collects
# minutes before this file runs, which aged every "21 minutes past" row by
# the shard's elapsed time. The ``_now`` fixture re-stamps it per test; the
# import-time value is stale on purpose, so a test that escapes the fixture
# fails here without a slow CI shard.
NOW = datetime.now(UTC) - timedelta(hours=1)


@pytest.fixture(autouse=True)
def _now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.modules[__name__], "NOW", datetime.now(UTC))


@pytest.fixture(autouse=True)
def _factory_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)


# ── helpers ───────────────────────────────────────────────────────


def _root(store: Store, *, status: str = "open", tags: tuple[str, ...] = ()) -> int:
    ref = store.insert_ref(
        kind="todo", slug=None, title="plan root", meta={"auto_close": True}
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", status), set_by="system", replace_prefix=True
    )
    store.add_tag(ref.id, Tag.open(f"plan:{ref.id}"), set_by="system")
    for t in tags:
        store.add_tag(ref.id, Tag.open(t), set_by="system")
    return int(ref.id)


def _node(
    store: Store,
    root: int,
    *,
    title: str = "a task",
    meta: dict[str, Any] | None = None,
    status: str = "open",
    parent: int | None = None,
    tags: tuple[str, ...] = (),
) -> int:
    ref = store.insert_ref(
        kind="todo",
        slug=None,
        title=title,
        meta={"op": "run_slice"} if meta is None else meta,
        parent_id=parent or root,
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", status), set_by="system", replace_prefix=True
    )
    store.add_tag(ref.id, Tag.open(f"plan:{root}"), set_by="system")
    for t in tags:
        store.add_tag(ref.id, Tag.open(t), set_by="system")
    return int(ref.id)


def _gate(store: Store, root: int, sha: str, *, blocked_by: int | None = None) -> int:
    n = _node(
        store,
        root,
        title="wait for prod",
        meta={"gate": {"kind": "sha-in-prod", "args": {"sha": sha}}},
    )
    if blocked_by is not None:
        store.add_link(
            src_ref_id=n, dst_ref_id=blocked_by, relation="blocked-by", set_by="system"
        )
    return n


def _ask(store: Store, root: int, *, tags: tuple[str, ...] = ()) -> int:
    return _node(
        store,
        root,
        title="a ruling ask",
        meta={
            "ask": {
                "kind": "ruling",
                "options": ["acknowledged"],
                "blocks": "the node",
                "context": "test ask",
            }
        },
        tags=tags,
    )


def _job(
    store: Store,
    todo: int,
    *,
    job_type: str = "run_slice",
    executor: str = "ssh_node",
    status: str = "queued",
    params: dict[str, Any] | None = None,
    meta_extra: dict[str, Any] | None = None,
) -> int:
    with store.tx() as conn:
        ref = mint_background_job(
            store,
            conn=conn,
            job_type=job_type,
            executor=executor,
            title=job_type,
            params=params or {},
            parent_id=todo,
            status=status,
            meta_extra=meta_extra,
        )
    return int(ref.id)


def _tick_todo(store: Store, *, status: str = "open") -> int:
    ref = store.insert_ref(
        kind="todo",
        slug=None,
        title="plan_dispatch tick (dark factory)",
        meta={
            "schedule": {"every": "5m"},
            "executor": "job_inproc",
            "job_type": "plan_dispatch",
        },
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", status), set_by="system", replace_prefix=True
    )
    return int(ref.id)


def _pass_heartbeat(store: Store, *, age: timedelta = timedelta(0)) -> None:
    """One ``worker_logs`` liveness row for the lane ticker's pass (the
    runner's BatchResult digest shape, ``payload->>'handler' =
    'plan_tick'``) stamped ``age`` ago — the service-pass half of the
    tick's liveness (gr576145)."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO worker_logs (ts, host, process, level, logger, "
            "message, payload) VALUES (now() - %s::interval, 'h', 'p', "
            "'INFO', 'precis.workers.runner', 'worker: batch', "
            "jsonb_build_object('handler', 'plan_tick', 'claimed', 0, "
            "'ok', 0, 'failed', 0))",
            (f"{age.total_seconds()} seconds",),
        )
        conn.commit()


def _comment(store: Store, ref_id: int, text: str) -> None:
    ords = [int(c.ord) for c in store.chunks.list_chunks_for_ref(ref_id)]
    store.chunks.insert_chunks(
        ref_id,
        [
            ChunkInsert(
                ord=max(ords, default=0) + 1,
                text=text,
                meta={"chunk_kind": "todo_comment"},
            )
        ],
    )


def _open_tags(store: Store, ref_id: int) -> list[str]:
    return [str(t.value) for t in store.tags_for(ref_id) if t.namespace == "open"]


def _rescue_tags(store: Store, ref_id: int) -> list[str]:
    return [t for t in _open_tags(store, ref_id) if t.startswith("rescue:")]


def _status(store: Store, ref_id: int) -> str:
    for t in store.tags_for(ref_id):
        if t.prefix == "STATUS":
            return str(t.value)
    return "?"


def _comments(store: Store, ref_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'todo_comment' "
            "ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return [str(r[0]) for r in rows]


def _sig_gripes(store: Store) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id FROM refs r "
            "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE r.kind = 'gripe' AND r.retired_at IS NULL "
            "AND t.namespace = 'OPEN' AND t.value LIKE 'rescue-sig:%' "
            "ORDER BY r.ref_id",
        ).fetchall()
    return [int(r[0]) for r in rows]


def _links(store: Store, src: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT dst_ref_id FROM links WHERE src_ref_id = %s "
            "AND relation = 'related-to' ORDER BY dst_ref_id",
            (src,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _age(store: Store, ref_id: int, by: timedelta) -> None:
    """Backdate a ref's created_at + tag set times (an aged node the
    ``_dispatch`` path must see — ``run_stall_pass`` takes ``now``)."""
    past = datetime.now(UTC) - by
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = %s WHERE ref_id = %s", (past, ref_id)
        )
        conn.execute(
            "UPDATE ref_tags SET created_at = %s WHERE ref_id = %s", (past, ref_id)
        )


def _stamp_tags(store: Store, ref_id: int, at: datetime) -> None:
    """Set a ref's tag set times to an explicit instant — a queue claim
    stamped between ``NOW`` and the pass's ``now`` (gr625325)."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags SET created_at = %s WHERE ref_id = %s", (at, ref_id)
        )


def _job_meta(store: Store, ref_id: int) -> dict[str, Any]:
    ref = store.get_ref(kind="job", id=ref_id)
    assert ref is not None
    return dict(ref.meta)


# ── the deadline cases ────────────────────────────────────────────


def test_cancelled_job_node_is_rescued_after_20_minutes(store: Store) -> None:
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="cancelled")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=19))
    assert res.rescued == [] and _rescue_tags(store, task) == []

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))
    assert res.rescued == [(task, "cancelled", "idle")]
    assert _rescue_tags(store, task) == ["rescue:cancelled"]
    (gripe,) = _sig_gripes(store)
    assert "cluster:dark-factory" in _open_tags(store, gripe)


def test_terminal_root_node_is_rescued(store: Store) -> None:
    root = _root(store, status="done")
    leaf = _node(store, root)

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))

    assert res.rescued == [(leaf, "terminal-root", "idle")]
    assert _rescue_tags(store, leaf) == ["rescue:terminal-root"]


def test_terminal_root_rescue_waits_for_the_close_to_age(store: Store) -> None:
    """gr576155: the anomaly is "open under a dead root" — born at the
    root's own STATUS stamp, not at the node's stale signal. A node
    silent for days under a root closed seconds ago is a fresh orphan:
    the dead-root reapers (the dispatch sweep, the orphan close) get
    the idle deadline's grace before a rescue files."""
    root = _root(store, status="done")
    leaf = _node(store, root)
    _age(store, leaf, timedelta(hours=2))  # idle long before the close

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=19))
    assert res.rescued == [] and _rescue_tags(store, leaf) == []

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))
    assert res.rescued == [(leaf, "terminal-root", "idle")]
    assert _rescue_tags(store, leaf) == ["rescue:terminal-root"]


def test_terminal_root_stale_job_is_cleared(store: Store) -> None:
    """gr730548: a non-terminal job under a closed root has no consumer
    and no reaper — the pass clears it ``STATUS:cancelled`` outright and
    links it onto the signature gripe a rescued todo joins."""
    root = _root(store, status="done")
    job = _job(store, root, job_type="plan_tick", status="running")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))

    assert res.rescued == [(job, "terminal-root", "idle")]
    assert _status(store, job) == "cancelled"
    assert _rescue_tags(store, job) == []
    (gripe,) = _sig_gripes(store)
    assert "cluster:dark-factory" in _open_tags(store, gripe)
    assert _links(store, job) == [gripe]
    assert "no consumer" in _job_meta(store, job)["error"]


def test_terminal_root_job_clear_waits_for_the_close_to_age(
    store: Store,
) -> None:
    """The clear holds the same grace as the todo rescue: a job silent
    for hours under a root closed seconds ago is a fresh orphan, not an
    old stall — an in-flight final write lands inside the deadline."""
    root = _root(store, status="done")
    job = _job(store, root, job_type="plan_tick", status="running")
    _age(store, job, timedelta(hours=2))

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=19))
    assert res.rescued == [] and _status(store, job) == "running"

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))
    assert res.rescued == [(job, "terminal-root", "idle")]
    assert _status(store, job) == "cancelled"


def test_terminal_root_fresh_job_is_not_cleared(store: Store) -> None:
    """A job still signalling under a dead root may still land — only a
    signal-dead one is cleared (the td673697 live-job guard)."""
    root = _root(store, status="done")
    _stamp_tags(store, root, NOW - timedelta(hours=1))
    job = _job(store, root, job_type="plan_tick", status="running")

    # 19 min since mint is inside the idle deadline — the long-dead
    # close does not rescue a job that is still talking.
    res = fr.run_stall_pass(store, NOW + timedelta(minutes=19))
    assert res.rescued == [] and _status(store, job) == "running"
    assert _sig_gripes(store) == []

    # Past the deadline the same job clears.
    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))
    assert res.rescued == [(job, "terminal-root", "idle")]
    assert _status(store, job) == "cancelled"


def test_terminal_root_job_clear_dry_run_writes_nothing(store: Store) -> None:
    root = _root(store, status="done")
    job = _job(store, root, job_type="plan_tick", status="running")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21), dry_run=True)

    assert res.would_rescue == [(job, "terminal-root", "idle")]
    assert _status(store, job) == "running"
    assert _sig_gripes(store) == []


def test_terminal_root_second_job_clear_shares_the_gripe(
    store: Store,
) -> None:
    """Dedupe: a second dead-root job on the same signature joins the
    first's gripe — the ``dup-suggest`` the reporter asked for."""
    root = _root(store, status="done")
    job = _job(store, root, job_type="plan_tick", status="running")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))
    assert res.rescued == [(job, "terminal-root", "idle")]

    root2 = _root(store, status="done")
    job2 = _job(store, root2, job_type="plan_tick", status="running")
    res2 = fr.run_stall_pass(store, NOW + timedelta(minutes=40))
    assert res2.rescued == [(job2, "terminal-root", "idle")]

    (gripe,) = _sig_gripes(store)
    assert _links(store, job) == [gripe]
    assert _links(store, job2) == [gripe]


def test_queued_job_deadline_is_30_minutes(store: Store) -> None:
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="queued")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=29))
    assert res.rescued == []
    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))
    assert res.rescued == [(task, "stalled", "queued")]
    assert _rescue_tags(store, task) == ["rescue:stalled"]


def test_queued_job_on_a_moving_queue_is_a_wait(store: Store) -> None:
    """gr625325: a job still ``STATUS:queued`` while its queue drains is a
    wait, not a stall — same-(``job_type``, ``executor``) jobs claimed
    inside the window. The post-deploy unblock queued 148 jobs; the
    30 min silence rule filed twelve showstopper asks, every one moot."""
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="queued")
    other = _node(store, root)
    _job(
        store,
        other,
        status="running",
        # Claimed 11 min before the pass — inside the 30 min window.
        meta_extra={"started_at": (NOW + timedelta(minutes=20)).isoformat()},
    )

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []
    assert any("a wait, not a stall" in note for note in res.notes)


def test_queued_job_on_a_stalled_queue_is_rescued(store: Store) -> None:
    """The same queued job rescues once the queue itself has not moved in
    the window — the last same-type claim is older than the deadline."""
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="queued")
    other = _node(store, root)
    other_job = _job(
        store,
        other,
        status="running",
        meta_extra={"started_at": (NOW - timedelta(minutes=15)).isoformat()},
    )
    _age(store, other_job, timedelta(minutes=45))

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == [(task, "stalled", "queued")]
    assert _rescue_tags(store, task) == ["rescue:stalled"]


def test_queued_overlap_held_build_on_a_stalled_queue_waits(store: Store) -> None:
    """gr702610: the mint queues a file-overlap-held build — its job
    never claims while the holder builds, so its queue can sit still
    past the deadline. The hold, not a stall, owns the silence."""
    root = _root(store)
    holder = _node(store, root, title="holder build")
    _body(store, holder, "Edit src/precis/a.py")
    _job(
        store,
        holder,
        status="running",
        meta_extra={"started_at": (NOW - timedelta(minutes=15)).isoformat()},
    )
    task = _node(store, root, title="held build")
    _body(store, task, "Edit src/precis/a.py")
    _job(store, task, status="queued")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []
    assert any("file-overlap hold" in note for note in res.notes)


@pytest.mark.parametrize("build_first", [False, True])
def test_build_freed_by_a_same_pass_rescue_is_rechecked_next_pass(
    store: Store, build_first: bool
) -> None:
    """gr778684: a stuck pre-land review held its build's ``pending_land``,
    and that hold kept a build sharing a file queued. Rescuing the review
    releases the hold; the freed build is not judged stalled on the
    silence the hold caused — its stall clock restarts at the release,
    whether the pass judged it before the review (parked by the live
    hold) or after it."""
    from precis.plans import templates as T

    root = _root(store)
    if build_first:
        waiting = _node(store, root, title="held build")
        _body(store, waiting, "Edit src/precis/a.py")
        waiting_job = _job(store, waiting, status="queued")
    holder = _node(
        store,
        root,
        title="holder build",
        status="done",
        meta={"op": "run_slice", "branch_files": ["src/precis/a.py"]},
    )
    store.stamp_ref_meta(
        holder,
        {
            "pending_land": {
                "todo_id": holder,
                "branch": f"slice/{holder}",
                "head": "a" * 40,
            }
        },
    )
    _body(store, holder, "Edit src/precis/a.py")
    review = _node(
        store, root, title="pre-land review", meta=T._reviewer(holder, preland=True)
    )
    _job(store, review, status="queued")
    if not build_first:
        waiting = _node(store, root, title="held build")
        _body(store, waiting, "Edit src/precis/a.py")
        waiting_job = _job(store, waiting, status="queued")
    assert (waiting < review) is build_first  # the pass judges in id order

    released_at = NOW + timedelta(minutes=31)
    res = fr.run_stall_pass(store, released_at)

    assert res.rescued == [(review, "stalled", "queued")]
    assert _rescue_tags(store, waiting) == []
    assert (
        sum(
            f"td{waiting}: file-overlap hold — waits on td{holder}" in note
            and "until this pass's rescue released it" in note
            for note in res.notes
        )
        == 1
    )
    # The release event restarts the clock. It lands at wall-clock time;
    # this file's passes run on a shifted clock, so align it with the pass.
    with store.pool.connection() as conn:
        n_events = conn.execute(
            "UPDATE ref_events SET ts = %s WHERE ref_id = %s AND event = %s",
            (released_at, waiting_job, fr.OVERLAP_RELEASED_EVENT),
        ).rowcount
        conn.commit()
    assert n_events == 1

    # Inside the 30 min queued deadline from the release: still a wait.
    res = fr.run_stall_pass(store, released_at + timedelta(minutes=29))
    assert res.rescued == []
    assert _rescue_tags(store, waiting) == []

    # Past it with no claim: the queue really stalled.
    res = fr.run_stall_pass(store, released_at + timedelta(minutes=31))
    assert res.rescued == [(waiting, "stalled", "queued")]


def _pending_holder(
    store: Store, root: int, files: list[str], *, status: str = "done"
) -> int:
    """A build held for its pre-land review: ``pending_land`` stamped and
    ``branch_files`` set (it holds those files)."""
    node = _node(
        store,
        root,
        title="held-for-review build",
        status=status,
        meta={"op": "run_slice", "branch_files": files},
    )
    store.stamp_ref_meta(
        node,
        {
            "pending_land": {
                "todo_id": node,
                "branch": f"slice/{node}",
                "head": "a" * 40,
            }
        },
    )
    return node


def _release_events(store: Store, ref_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM ref_events WHERE ref_id = %s AND event = %s",
            (ref_id, fr.OVERLAP_RELEASED_EVENT),
        ).fetchone()
    return int(row[0]) if row else 0


def test_a_build_another_holder_still_blocks_is_not_freed(store: Store) -> None:
    """gr778684: B waits on A and on C. Rescuing A's review releases A,
    but C — an idle build that waits itself (on A and D) and holds its
    own branch files — still blocks B, and D still blocks C. Neither B
    nor C is freed and neither gets a release event."""
    from precis.plans import templates as T

    root = _root(store)
    a = _pending_holder(store, root, ["src/precis/a.py"])
    review = _node(
        store, root, title="pre-land review", meta=T._reviewer(a, preland=True)
    )
    _job(store, review, status="queued")
    _pending_holder(store, root, ["src/precis/d.py"])
    c = _pending_holder(
        store, root, ["src/precis/a.py", "src/precis/d.py"], status="open"
    )
    b = _node(store, root, title="held build")
    _body(store, b, "Edit src/precis/a.py")
    b_job = _job(store, b, status="queued")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert (review, "stalled", "queued") in res.rescued
    assert _rescue_tags(store, b) == [] and _rescue_tags(store, c) == []
    assert (_release_events(store, b_job), _release_events(store, b)) == (0, 0)
    assert _release_events(store, c) == 0
    assert b not in res.freed and c not in res.freed


def test_queued_job_other_type_drain_does_not_hold(store: Store) -> None:
    """gr625325's cohort is the queue the job actually waits in: another
    job_type (or executor) draining does not shorten this queue, so a
    dead ``run_slice`` claim path still rescues on schedule."""
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="queued")
    other = _node(store, root)
    _job(
        store,
        other,
        job_type="fetch_oa",
        status="running",
        meta_extra={"started_at": (NOW + timedelta(minutes=20)).isoformat()},
    )

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == [(task, "stalled", "queued")]


def test_queued_job_a_terminal_stamp_is_not_a_claim(store: Store) -> None:
    """gr625325 review: the drain clock reads durable claim stamps, not
    the current STATUS row — a STATUS write is DELETE+INSERT, so a
    sibling completed or cancelled inside the window re-dates its row
    without anything being claimed (a mass-cancel or a fail-cascade
    would read as a fresh drain on a dead queue). Only the claim's own
    stamp — ``meta.started_at``, here older than the deadline — counts,
    so the queued job still rescues on schedule."""
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="queued")
    finished = _node(store, root)
    finished_job = _job(
        store,
        finished,
        status="succeeded",
        # Claimed ~41 min before the pass; the STATUS:succeeded row is
        # stamped inside the window (below) but a completion is not a
        # claim.
        meta_extra={"started_at": (NOW - timedelta(minutes=10)).isoformat()},
    )
    cancelled = _node(store, root)
    cancelled_job = _job(store, cancelled, status="cancelled")  # never claimed
    # Fresh terminal STATUS rows inside the window — drain evidence under
    # the STATUS-stamp read, noise under the claim-stamp read. They also
    # keep the siblings' own idle clocks inside their deadlines.
    _stamp_tags(store, finished_job, NOW + timedelta(minutes=15))
    _stamp_tags(store, cancelled_job, NOW + timedelta(minutes=15))

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == [(task, "stalled", "queued")]
    assert _rescue_tags(store, task) == ["rescue:stalled"]


def test_queued_job_a_yielded_claim_still_counts(store: Store) -> None:
    """gr625325 review: a claim followed by a yield puts the row back on
    a claimable STATUS — the claim's tag evidence is gone — but
    ``meta.started_at`` survives the rewrite, so a queue whose claimer
    keeps claiming and yielding still reads as moving."""
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="queued")
    yielded = _node(store, root)
    _job(
        store,
        yielded,
        status="yielded",
        meta_extra={"started_at": (NOW + timedelta(minutes=20)).isoformat()},
    )

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []


def test_queued_job_a_reclaim_counts_as_movement(store: Store) -> None:
    """A stale-running row reclaimed inside the window is the same claim
    path working — the reclaim's ``meta.reclaims[].at`` stamp holds the
    queue even when the job's first claim is hours old."""
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="queued")
    running = _node(store, root)
    _job(
        store,
        running,
        status="running",
        meta_extra={
            "started_at": (NOW - timedelta(hours=2)).isoformat(),
            "reclaims": [
                {"at": (NOW + timedelta(minutes=20)).isoformat(), "why": "stale lease"}
            ],
        },
    )

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []


def test_queued_job_a_reclaim_from_queued_still_counts(store: Store) -> None:
    """gr625325 review: a job claimed once hours ago, yielded/re-queued,
    then claimed AGAIN inside the window provides recent movement
    evidence. The re-claim keeps the first ``started_at`` (set-if-absent)
    and appends no ``reclaims`` — only ``meta.last_claimed_at``,
    re-stamped on every claim, records it. Without that read the queue
    looks dead and the wait files a moot ask."""
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="queued")
    reclaimed = _node(store, root)
    _job(
        store,
        reclaimed,
        status="running",
        meta_extra={
            # First claimed 2 h ago; claimed a second time 11 min before
            # the pass — ``started_at`` alone reads as a dead queue.
            "started_at": (NOW - timedelta(hours=2)).isoformat(),
            "last_claimed_at": (NOW + timedelta(minutes=20)).isoformat(),
        },
    )

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []
    assert any("a wait, not a stall" in note for note in res.notes)


def test_every_claim_re_stamps_last_claimed_at(store: Store) -> None:
    """The writer side of the review finding: the claim itself must
    leave the durable stamp. A real ``claim_executor_jobs`` claim on a
    re-queued job — ``lease_until`` dropped, the FIRST ``started_at``
    still set, exactly the wake_runner's yield/re-queue shape — stamps
    ``meta.last_claimed_at`` afresh while ``started_at`` stays put, and
    the queue-drain read then holds a queued sibling as a wait."""
    root = _root(store)
    other = _node(store, root)
    other_job = _job(store, other, job_type="fetch_oa", status="queued")
    # Mint the stalled-task job second: the claim order takes the oldest
    # ``queued_since`` first, so ``other_job`` is the row each limit=1
    # claim picks.
    task = _node(store, root)
    task_job = _job(store, task, job_type="fetch_oa", status="queued")

    with store.pool.connection() as conn:
        claimed = claim_executor_jobs(conn, executor="ssh_node", limit=1)
        conn.commit()

    assert [r[0] for r in claimed] == [other_job]
    meta = _job_meta(store, other_job)
    assert meta["last_claimed_at"] == meta["started_at"]

    # Yield → re-queue: the STATUS tag is still a claimable value, the
    # wake_runner's _clear_lease drops only ``lease_until``, and the
    # first claim's ``started_at`` is preserved. Backdate both claim
    # stamps so the second claim's stamp is the only fresh evidence.
    old = (NOW - timedelta(hours=2)).isoformat()
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = (meta - 'lease_until') || "
            "jsonb_build_object('started_at', %s::text, "
            "'last_claimed_at', %s::text) WHERE ref_id = %s",
            (old, old, other_job),
        )
        conn.commit()

    with store.pool.connection() as conn:
        claimed = claim_executor_jobs(conn, executor="ssh_node", limit=1)
        conn.commit()

    assert [r[0] for r in claimed] == [other_job]
    meta = _job_meta(store, other_job)
    assert meta["started_at"] == old
    assert datetime.fromisoformat(meta["last_claimed_at"]) > NOW - timedelta(minutes=1)

    # The claim stamps are real wall-clock instants, so the pass must
    # judge at ~now: age the stalled task's own signals past the deadline
    # instead of passing a future ``now``.
    _age(store, task, timedelta(minutes=45))
    _age(store, task_job, timedelta(minutes=45))
    res = fr.run_stall_pass(store, NOW)

    assert res.rescued == []
    assert _rescue_tags(store, task) == []
    assert any("a wait, not a stall" in note for note in res.notes)


def test_running_build_is_judged_at_5_hours(store: Store) -> None:
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="running")

    res = fr.run_stall_pass(store, NOW + timedelta(hours=4))
    assert res.rescued == []
    res = fr.run_stall_pass(store, NOW + timedelta(hours=5, minutes=1))
    assert res.rescued == [(task, "stalled", "build")]


def test_running_build_with_a_fresh_log_is_not_rescued(
    store: Store, tmp_path: Path
) -> None:
    """gr626270: run_slice writes progress to run_slice.log, not to job
    events — no event for an hour is not a stall signal. The log's
    mtime is the session's liveness: a 5 h build whose log moved
    minutes ago is alive, not silent."""
    root = _root(store)
    task = _node(store, root)
    log = tmp_path / "run_slice.log"
    log.write_text("progress\n", encoding="utf-8")
    fresh = NOW + timedelta(hours=4, minutes=55)
    os.utime(log, (fresh.timestamp(), fresh.timestamp()))
    _job(
        store,
        task,
        status="running",
        meta_extra={"compute_handle": {"pid": 1, "log": str(log)}},
    )

    res = fr.run_stall_pass(store, NOW + timedelta(hours=5, minutes=1))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []


def test_running_build_with_a_stale_log_is_rescued(
    store: Store, tmp_path: Path
) -> None:
    """The same build rescues once its log stops inside the deadline —
    the pid's liveness is never consulted."""
    root = _root(store)
    task = _node(store, root)
    log = tmp_path / "run_slice.log"
    log.write_text("last write\n", encoding="utf-8")
    stale = NOW - timedelta(hours=1)
    os.utime(log, (stale.timestamp(), stale.timestamp()))
    _job(
        store,
        task,
        status="running",
        meta_extra={"compute_handle": {"pid": 1, "log": str(log)}},
    )

    res = fr.run_stall_pass(store, NOW + timedelta(hours=5, minutes=1))

    assert res.rescued == [(task, "stalled", "build")]
    (comment,) = _comments(store, task)
    assert "log mtime" in comment


def test_running_build_with_an_unreadable_log_keeps_db_judgment(
    store: Store, tmp_path: Path
) -> None:
    """A handle whose log path does not exist on this host (a run
    pinned to another node) adds no signal — the DB stamps judge as
    before."""
    root = _root(store)
    task = _node(store, root)
    _job(
        store,
        task,
        status="running",
        meta_extra={"compute_handle": {"pid": 1, "log": str(tmp_path / "gone.log")}},
    )

    res = fr.run_stall_pass(store, NOW + timedelta(hours=5, minutes=1))

    assert res.rescued == [(task, "stalled", "build")]


def test_running_build_with_a_fresh_persisted_stamp_is_not_rescued(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td639132: the tick is unpinned — claimed by a host that cannot
    see the builder's filesystem, the local stat adds nothing. The
    owning host's poll persists the log mtime as ``meta.log_fresh_at``;
    that cluster-wide stamp judges a build whose log is still moving
    on another host."""
    monkeypatch.setenv("PRECIS_NODE", "watchdog")
    root = _root(store)
    task = _node(store, root)
    fresh = NOW + timedelta(hours=4, minutes=55)
    _job(
        store,
        task,
        status="running",
        meta_extra={
            "params": {"target_node": "builder"},
            "compute_handle": {"pid": 1, "log": str(tmp_path / "gone.log")},
            "log_fresh_at": fresh.isoformat(),
        },
    )

    res = fr.run_stall_pass(store, NOW + timedelta(hours=5, minutes=1))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []


def test_running_build_pinned_elsewhere_ignores_a_local_log(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other edge of the pin: a same-named file on THIS host is not
    the job's log — when the pin names another node the local stat is
    no signal at all, so it can never mask a real stall."""
    monkeypatch.setenv("PRECIS_NODE", "watchdog")
    root = _root(store)
    task = _node(store, root)
    log = tmp_path / "run_slice.log"
    log.write_text("not this job's log\n", encoding="utf-8")
    fresh = NOW + timedelta(hours=4, minutes=55)
    os.utime(log, (fresh.timestamp(), fresh.timestamp()))
    _job(
        store,
        task,
        status="running",
        meta_extra={
            "params": {"target_node": "builder"},
            "compute_handle": {"pid": 1, "log": str(log)},
        },
    )

    res = fr.run_stall_pass(store, NOW + timedelta(hours=5, minutes=1))

    assert res.rescued == [(task, "stalled", "build")]


def test_running_build_pinned_here_reads_the_local_log(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job pinned to THIS host gets the direct stat before its first
    ``log_fresh_at`` stamp lands — the local read is still a signal."""
    monkeypatch.setenv("PRECIS_NODE", "builder")
    root = _root(store)
    task = _node(store, root)
    log = tmp_path / "run_slice.log"
    log.write_text("progress\n", encoding="utf-8")
    fresh = NOW + timedelta(hours=4, minutes=55)
    os.utime(log, (fresh.timestamp(), fresh.timestamp()))
    _job(
        store,
        task,
        status="running",
        meta_extra={
            "params": {"target_node": "builder"},
            "compute_handle": {"pid": 1, "log": str(log)},
        },
    )

    res = fr.run_stall_pass(store, NOW + timedelta(hours=5, minutes=1))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []


def test_running_build_with_a_stale_persisted_stamp_is_rescued(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stamp is honest evidence, not a heartbeat: a ``log_fresh_at``
    older than the deadline still rescues, cross-host or not."""
    monkeypatch.setenv("PRECIS_NODE", "watchdog")
    root = _root(store)
    task = _node(store, root)
    _job(
        store,
        task,
        status="running",
        meta_extra={
            "params": {"target_node": "builder"},
            "compute_handle": {"pid": 1, "log": str(tmp_path / "gone.log")},
            "log_fresh_at": (NOW - timedelta(hours=1)).isoformat(),
        },
    )

    res = fr.run_stall_pass(store, NOW + timedelta(hours=5, minutes=1))

    assert res.rescued == [(task, "stalled", "build")]
    (comment,) = _comments(store, task)
    assert "log mtime" in comment


def test_breaker_reparks_keep_the_first_park_deadline(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = "claude-sub"
    held = {lane}
    monkeypatch.setattr("precis.plans.lane_breaker.tripped_lanes", lambda store: held)
    task = _node(store, _root(store))
    job = _job(store, task, status="running", meta_extra={"params": {"lane": lane}})
    note = (
        f"{LANE_PARK_WHY}run_slice: lane {lane!r} pinned by params.lane is "
        "held out by an open lane-breaker ask; re-checked at "
    )

    for minutes in range(0, 301, 30):
        at = NOW + timedelta(minutes=minutes)
        wake = at + timedelta(minutes=30)
        park_note = note + wake.strftime("%Y-%m-%dT%H:%M:%SZ")
        with store.pool.connection() as conn:
            assert park_job_until(store, conn, job, wake, park_note)
            conn.execute(
                "UPDATE chunks SET created_at = %s WHERE ref_id = %s AND text = %s",
                (at, job, park_note),
            )
            conn.execute(
                "UPDATE refs SET meta = meta - 'pause_parks' WHERE ref_id = %s", (job,)
            )
        _stamp_tags(store, job, at)

    assert fr.run_stall_pass(store, NOW + timedelta(minutes=300)).rescued == []
    overdue = NOW + timedelta(minutes=301)
    held.clear()
    assert fr.run_stall_pass(store, overdue).rescued == []
    held.add(lane)
    assert fr.run_stall_pass(store, overdue).rescued == [(task, "stalled", "build")]
    assert fr.run_stall_pass(store, overdue).rescued == []
    (comment,) = _comments(store, task)
    assert "first lane-breaker park" in comment
    assert "open lane-breaker ask" in comment


def test_running_build_parked_on_an_outage_is_not_rescued(store: Store) -> None:
    """gr643076: a run_slice whose lane's quota window closed parks the
    live job — ``meta.compute_handle.not_before`` at the window's reset,
    no process, the owning poll resubmitting there — and stays silent
    BY CONSTRUCTION. An 11 h park crosses the 5 h build deadline while
    still deliberate; a quota park to 11:00Z was rescued at 05:10Z on
    306 min of engineered silence. The pass must hold the node while
    the relaunch instant is still ahead."""
    root = _root(store)
    task = _node(store, root)
    job = _job(
        store,
        task,
        status="running",
        meta_extra={
            "compute_handle": {
                "not_before": (NOW + timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "outage": "quota",
                "outage_parks": 1,
            }
        },
    )
    _age(store, job, timedelta(hours=6))  # every real signal is stale

    res = fr.run_stall_pass(store, NOW + timedelta(hours=5, minutes=1))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []
    assert any("parked until" in note for note in res.notes)


def test_running_build_park_lapsed_measures_from_relaunch(store: Store) -> None:
    """The stamp re-zeros the clock at the relaunch instant: once
    ``not_before`` passes, silence is measured from it — a relaunch a
    few minutes overdue is a wait inside the deadline, not a stall."""
    root = _root(store)
    task = _node(store, root)
    job = _job(
        store,
        task,
        status="running",
        meta_extra={
            "compute_handle": {
                "not_before": (NOW + timedelta(hours=4, minutes=50)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "outage": "quota",
                "outage_parks": 1,
            }
        },
    )
    _age(store, job, timedelta(hours=6))

    res = fr.run_stall_pass(store, NOW + timedelta(hours=5, minutes=1))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []


def test_running_build_park_long_overdue_still_rescues(store: Store) -> None:
    """The stamp is a re-zero, not an exemption: a relaunch overdue past
    the stage deadline rescues on the clock the park restarted — a dead
    poll is a stall."""
    root = _root(store)
    task = _node(store, root)
    job = _job(
        store,
        task,
        status="running",
        meta_extra={
            "compute_handle": {
                "not_before": (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "outage": "quota",
                "outage_parks": 1,
            }
        },
    )
    _age(store, job, timedelta(hours=7))

    res = fr.run_stall_pass(store, NOW + timedelta(hours=5, minutes=1))

    assert res.rescued == [(task, "stalled", "build")]
    assert _rescue_tags(store, task) == ["rescue:stalled"]


def test_land_parked_on_a_refusal_is_not_rescued(store: Store) -> None:
    """The same ``not_before`` covers a land_slice refusal deferral: the
    handle parks while ship's backoff runs and the poll owns the wake
    — the land stage's deadline does not rescue the wait."""
    root = _root(store)
    task = _node(store, root)
    job = _job(
        store,
        task,
        job_type="land_slice",
        status="running",
        meta_extra={
            "compute_handle": {
                "not_before": (NOW + timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        },
    )
    _age(store, job, timedelta(hours=7))

    res = fr.run_stall_pass(store, NOW + timedelta(hours=6, minutes=1))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []


def test_gate_not_open_is_judged_at_72_hours(store: Store) -> None:
    root = _root(store)
    build = _node(store, root, title="build", status="done")
    gate = _gate(store, root, "a1b2c3d4e5f", blocked_by=build)

    res = fr.run_stall_pass(store, NOW + timedelta(hours=71))
    assert res.rescued == []
    res = fr.run_stall_pass(store, NOW + timedelta(hours=73))
    assert res.rescued == [(gate, "stalled", "gate")]
    assert _rescue_tags(store, gate) == ["rescue:stalled"]


def test_post_land_write_fill_heals_the_gate(store: Store) -> None:
    """The build landed but the gate still holds PENDING_SHA: land_slice's
    post-land write (sha into meta.gate.args) never ran — the stall pass
    fills the gate from the build's own ``landed:`` comment instead of
    rescuing (gr576154)."""
    root = _root(store)
    build = _node(store, root, title="build", status="done")
    _comment(store, build, "landed: a1b2c3d — the fix")
    gate = _gate(store, root, PENDING_SHA, blocked_by=build)

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))

    assert res.rescued == [] and res.would_rescue == []
    assert _rescue_tags(store, gate) == []
    ref = store.get_ref(kind="todo", id=gate)
    assert ref is not None
    assert ref.meta["gate"]["args"]["sha"] == "a1b2c3d"
    assert _sig_gripes(store) == []
    (comment,) = _comments(store, gate)
    assert comment == f"sha: a1b2c3d — landed by td{build} (stall-pass fill)"


def test_post_land_write_fill_skips_a_second_repo_sha(store: Store) -> None:
    """td659522 step 2: a build landed in another repo (``meta.landed_repo``)
    left a sha the precis ``sha-in-prod`` gate can never see; the pass must
    not fill the gate with it, and it falls through to the rescue."""
    root = _root(store)
    build = _node(store, root, title="build", status="done")
    _comment(store, build, "landed: a1b2c3d — the fix")
    store.stamp_ref_meta(build, {"landed_repo": "catpath"})
    gate = _gate(store, root, PENDING_SHA, blocked_by=build)

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))

    assert res.rescued == [(gate, "post-land-write", "idle")]
    ref = store.get_ref(kind="todo", id=gate)
    assert ref is not None
    assert ref.meta["gate"]["args"]["sha"] == PENDING_SHA


def test_post_land_write_nothing_new_still_rescues(store: Store) -> None:
    """A no-diff land's ``landed: nothing`` note carries no sha — the gate
    stays pending and the pass rescues the GATE, class
    ``post-land-write``, at the idle deadline."""
    root = _root(store)
    build = _node(store, root, title="build", status="done")
    _comment(
        store,
        build,
        "landed: nothing — branch had no net diff against origin/main "
        "(already on main)",
    )
    gate = _gate(store, root, PENDING_SHA, blocked_by=build)

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))

    assert res.rescued == [(gate, "post-land-write", "idle")]
    assert _rescue_tags(store, gate) == ["rescue:post-land-write"]
    ref = store.get_ref(kind="todo", id=gate)
    assert ref is not None
    assert ref.meta["gate"]["args"]["sha"] == PENDING_SHA
    (comment,) = _comments(store, gate)
    assert "landed: nothing" in comment


def test_post_land_write_fill_stops_at_the_revert_handoff(store: Store) -> None:
    """td626034: a review REVERT resets the gate and requeues the build
    with a ``handoff: review: REVERT`` comment; after the rebuild's
    no-diff reland the gate is pending again, and the stall pass must
    NOT fill it from the build's pre-REVERT ``landed:`` comment — that
    sha is the commit the revert took off main."""
    root = _root(store)
    build = _node(store, root, title="build", status="done")
    _comment(store, build, "landed: a1b2c3d — the fix")
    _comment(
        store,
        build,
        "handoff: review: REVERT — breaks prod\n\n(review td7; reverted by "
        "td9 — rebuild after it lands, from the brief, with this finding "
        "in mind)",
    )
    _comment(
        store,
        build,
        "landed: nothing — branch had no net diff against origin/main "
        "(already on main)",
    )
    gate = _gate(store, root, PENDING_SHA, blocked_by=build)

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))

    assert res.rescued == [(gate, "post-land-write", "idle")]
    assert _rescue_tags(store, gate) == ["rescue:post-land-write"]
    ref = store.get_ref(kind="todo", id=gate)
    assert ref is not None
    assert ref.meta["gate"]["args"]["sha"] == PENDING_SHA
    # the rescue evidence never names the reverted sha either
    evidence = "\n".join(_comments(store, gate))
    assert "a1b2c3d" not in evidence


def test_post_land_write_fill_recovers_a_landed_comment_after_the_revert(
    store: Store,
) -> None:
    """The boundary is the REVERT handoff itself: a ``landed:`` comment
    NEWER than it — the rebuild's own land, its gate write lost — is
    still the sha the gate waits on (gr576154)."""
    root = _root(store)
    build = _node(store, root, title="build", status="done")
    _comment(store, build, "landed: a1b2c3d — the fix")
    _comment(
        store,
        build,
        "handoff: review: REVERT — breaks prod\n\n(review td7; reverted by "
        "td9 — rebuild after it lands, from the brief, with this finding "
        "in mind)",
    )
    _comment(store, build, "landed: d4e5f6a — the rebuild")
    gate = _gate(store, root, PENDING_SHA, blocked_by=build)

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))

    assert res.rescued == [] and res.would_rescue == []
    assert _rescue_tags(store, gate) == []
    ref = store.get_ref(kind="todo", id=gate)
    assert ref is not None
    assert ref.meta["gate"]["args"]["sha"] == "d4e5f6a"
    assert _sig_gripes(store) == []
    (comment,) = _comments(store, gate)
    assert comment == f"sha: d4e5f6a — landed by td{build} (stall-pass fill)"


# ── holds: a hold is not a stall (one test per hold) ──────────────


def test_waiting_for_any_value_is_exempt(store: Store) -> None:
    root = _root(store)
    _node(store, root, tags=("waiting-for:ci-green",))
    _node(store, root, tags=("waiting-for:human-answer",))

    res = fr.run_stall_pass(store, NOW + timedelta(hours=6))

    assert res.held == 2
    assert res.rescued == [] and res.would_rescue == []
    assert _sig_gripes(store) == []


def test_halt_is_exempt(store: Store) -> None:
    root = _root(store)
    _node(store, root, tags=("halt",))
    _node(store, root, tags=("halt:operator-call",))

    res = fr.run_stall_pass(store, NOW + timedelta(hours=6))

    assert res.held == 2
    assert res.rescued == []
    assert _sig_gripes(store) == []


def test_paused_status_is_exempt(store: Store) -> None:
    root = _root(store)
    _node(store, root, status="paused")

    res = fr.run_stall_pass(store, NOW + timedelta(hours=6))
    assert res.held == 1
    assert res.rescued == []

    # A paused ancestor holds the leaf under it the same way.
    root2 = _root(store)
    phase = _node(
        store,
        root2,
        title="phase",
        meta={"auto_close": True},
        status="paused",
    )
    leaf = _node(store, root2, parent=phase)
    res = fr.run_stall_pass(store, NOW + timedelta(hours=6))
    # Both plans judged in one pass: the paused node above, and this leaf
    # (open itself) held through its paused ancestor.
    assert res.held == 2
    assert _rescue_tags(store, leaf) == []


def test_open_blocking_ask_is_exempt(store: Store) -> None:
    root = _root(store)
    ask = _ask(store, root, tags=("ask-user",))
    task = _node(store, root)
    store.add_link(
        src_ref_id=task, dst_ref_id=ask, relation="blocked-by", set_by="system"
    )

    res = fr.run_stall_pass(store, NOW + timedelta(hours=6))

    assert res.held == 1
    assert res.rescued == []


def test_wont_do_is_exempt(store: Store) -> None:
    root = _root(store)
    _node(store, root, status="won't-do")

    res = fr.run_stall_pass(store, NOW + timedelta(hours=6))

    assert res.held == 1
    assert res.rescued == []
    assert _sig_gripes(store) == []


def test_node_already_in_rescue_is_exempt(store: Store) -> None:
    root = _root(store)
    task = _node(store, root, tags=("rescue:stalled", "rescue-sig:abc123def456"))

    res = fr.run_stall_pass(store, NOW + timedelta(hours=6))

    assert res.held == 1
    assert _rescue_tags(store, task) == ["rescue:stalled"]
    assert _sig_gripes(store) == []


def test_release_pass_plan_is_exempt(store: Store) -> None:
    """Release stages belong to ``release_poll::watch_stuck``: a root
    carrying ``release-pass`` (and a ``release-stuck:`` ask under it) is
    skipped entirely."""
    root = _root(store, tags=("release-pass",))
    _ask(store, root, tags=("ask-user", "release-stuck:r1:build"))
    _node(store, root)

    res = fr.run_stall_pass(store, NOW + timedelta(hours=6))

    assert res.held == 1
    assert res.rescued == []
    assert _sig_gripes(store) == []


def test_blocked_by_live_node_is_exempt(store: Store) -> None:
    """A node waiting on its dependency is judged through the dependency,
    not on its own clock."""
    root = _root(store)
    dep = _node(store, root, title="dependency")
    task = _node(store, root)
    store.add_link(
        src_ref_id=task, dst_ref_id=dep, relation="blocked-by", set_by="system"
    )

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))

    # The live dependency itself is past its idle deadline and rescued;
    # the node behind it is held, not judged on its own clock.
    assert res.rescued == [(dep, "stalled", "idle")]
    assert res.held == 1
    assert _rescue_tags(store, task) == []


def test_jobless_doing_node_is_a_hand_lease_not_a_stall(store: Store) -> None:
    """gr659982: a node at ``STATUS:doing`` with no job is being worked
    by hand — the factory's own ``doing`` stamp rides a claimed
    ``run_slice`` job (``run_slice._claim_todo``), so job-less means a
    human moved it. Dispatch mints only ``STATUS:open`` candidates, so
    the stall watch leaves it alone instead of rescuing it and minting
    a showstopper ask."""
    root = _root(store)
    task = _node(store, root, title="manual review", meta={}, status="doing")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=25))

    assert res.rescued == [] and res.would_rescue == []
    assert _rescue_tags(store, task) == []
    assert _sig_gripes(store) == []


def test_jobless_doing_node_dry_run_is_also_held(store: Store) -> None:
    """The hand-lease exemption holds under ``dry_run`` too — no
    ``would_rescue`` entry."""
    root = _root(store)
    task = _node(store, root, meta={}, status="doing")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=25), dry_run=True)

    assert res.would_rescue == []
    assert res.held == 1
    assert _rescue_tags(store, task) == []


def test_jobless_claimed_node_is_a_hand_lease_not_a_stall(store: Store) -> None:
    """gr659982's sibling door: a job-less node under a ``claimed-by:``
    tag is out of dispatch's reach the same way — the tick skips every
    claimed node — so the watch leaves it to hygiene's CLAIM_STALE
    release instead of rescuing the claim."""
    root = _root(store)
    task = _node(store, root, meta={}, tags=("claimed-by:manual",))

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=25))

    assert res.rescued == [] and res.would_rescue == []
    assert res.held == 1
    assert _rescue_tags(store, task) == []


def test_doing_node_with_a_cancelled_job_is_still_judged(store: Store) -> None:
    """The hand-lease exemption needs the node to be job-less: a node the
    factory once ran is factory-owned whatever its STATUS — a cancelled
    newest job still reads as the cancelled stall."""
    root = _root(store)
    task = _node(store, root, status="doing")
    _job(store, task, status="cancelled")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))

    assert res.rescued == [(task, "cancelled", "idle")]
    assert _rescue_tags(store, task) == ["rescue:cancelled"]


def test_idle_clock_starts_when_the_blocker_finishes(store: Store) -> None:
    """gr586180: a node blocked 2 h whose predecessor went done seconds ago
    is not 120 min idle — it only just became dispatchable."""
    root = _root(store)
    build = _node(store, root, title="build", status="done")
    task = _node(store, root, title="review")
    store.add_link(
        src_ref_id=task, dst_ref_id=build, relation="blocked-by", set_by="system"
    )
    _age(store, task, timedelta(minutes=120))
    # ``build`` keeps its database-stamped "done" time: just now.

    res = fr.run_stall_pass(store, NOW + timedelta(seconds=25))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []
    assert _sig_gripes(store) == []

    # The same node, once the deadline runs from the unblock, is rescued.
    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))
    assert res.rescued == [(task, "stalled", "idle")]


def test_silent_node_past_the_deadline_since_unblock_is_rescued(
    store: Store,
) -> None:
    """A real stall: the blocker finished long ago and the node is still
    silent, so the unblock time does not mask it."""
    root = _root(store)
    build = _node(store, root, title="build", status="done")
    task = _node(store, root, title="review")
    store.add_link(
        src_ref_id=task, dst_ref_id=build, relation="blocked-by", set_by="system"
    )
    _age(store, task, timedelta(minutes=120))
    _age(store, build, timedelta(minutes=45))

    res = fr.run_stall_pass(store, NOW)

    assert res.rescued == [(task, "stalled", "idle")]
    assert _rescue_tags(store, task) == ["rescue:stalled"]
    (comment,) = _comments(store, task)
    assert "newest signal blocked-by predecessor done" in comment
    assert "44 min ago" in comment or "45 min ago" in comment


# ── dispatcher holds are waits, not stalls (gr681342) ─────────────


def _free_failed_jobs(store: Store, task: int, runs: int) -> list[int]:
    """``runs`` consecutive identical ``attempt_free`` failures — the
    dispatcher's free-backoff input (``plan_dispatch._free_run``)."""
    return [
        _job(
            store,
            task,
            status="failed",
            meta_extra={"attempt_free": "index lock held"},
        )
        for _ in range(runs)
    ]


def test_free_backoff_wait_is_parked_not_stalled(store: Store) -> None:
    """Seven identical free failures back the re-mint off the full 30 min
    from the newest mint. 25 min in, the node is only waiting on the
    dispatcher: 25 min of silence is past the 20 min idle deadline, yet no
    ask is filed."""
    root = _root(store)
    task = _node(store, root)
    _free_failed_jobs(store, task, 7)

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=25))

    assert res.rescued == []
    assert res.held == 1
    assert _rescue_tags(store, task) == []
    assert any("held by the dispatcher until" in note for note in res.notes)
    assert any("free-backoff" in note for note in res.notes)


def test_free_backoff_past_ready_with_no_dispatch_is_stalled(store: Store) -> None:
    """The same node 6 min past its ready time (30 min + grace) with
    nothing minted is a real stall — the tick runs about once a minute."""
    root = _root(store)
    task = _node(store, root)
    _free_failed_jobs(store, task, 7)

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=36))

    assert res.rescued == [(task, "stalled", "idle")]
    assert _rescue_tags(store, task) == ["rescue:stalled"]


def test_free_backoff_just_past_ready_is_still_waiting(store: Store) -> None:
    """The idle clock restarts at the hold's end: 2 min past ready is the
    dispatcher's next tick, not a stall."""
    root = _root(store)
    task = _node(store, root)
    _free_failed_jobs(store, task, 7)

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=32))

    assert res.rescued == []
    assert any("dispatch due since" in note for note in res.notes)


def test_free_backoff_ready_formula_is_the_dispatchers() -> None:
    """The watchdog and the dispatch step share one formula."""
    from precis.workers.job_types import plan_dispatch as pd

    base = datetime(2026, 1, 1, tzinfo=UTC)
    assert pd.free_backoff_ready_at(base, pd.FREE_RUN_GRACE - 1) is None
    assert pd.free_backoff_ready_at(base, pd.FREE_RUN_GRACE) == base + timedelta(
        minutes=2
    )
    assert pd.free_backoff_ready_at(base, 20) == base + pd.FREE_BACKOFF_MAX


def test_latched_retry_after_park_is_a_wait(store: Store) -> None:
    """A ``child-failed:``-latched node whose named jobs all carry a
    future ``retry_after`` is the sweeper's transient park — the one
    place the stamp binds — so the watchdog reads a wait, and the note
    names the parked job."""
    root = _root(store)
    task = _node(store, root)
    job = _job(
        store,
        task,
        status="failed",
        meta_extra={"retry_after": (NOW + timedelta(minutes=40)).isoformat()},
    )
    store.add_tag(task, Tag.open(f"child-failed:{job}"), set_by="system")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=25))

    assert res.rescued == []
    assert res.held == 1
    assert any(f"retry_after on jo{job}" in note for note in res.notes)


def test_bare_retry_after_is_no_hold(store: Store) -> None:
    """review td688453: the dispatch tick never reads
    ``meta.retry_after`` — it re-mints an open node on its own
    schedule — so a transient stamp with no ``child-failed:`` latch
    parks nothing, and a node silent past the idle deadline still
    reads stalled."""
    root = _root(store)
    task = _node(store, root)
    _job(
        store,
        task,
        status="failed",
        meta_extra={"retry_after": (NOW + timedelta(minutes=40)).isoformat()},
    )

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=25))

    assert res.rescued == [(task, "stalled", "idle")]
    assert _rescue_tags(store, task) == ["rescue:stalled"]


def test_retry_after_park_needs_every_latched_job_stamped(
    store: Store,
) -> None:
    """The sweeper's short-circuit applies only when EVERY job the live
    ``child-failed:`` tags name carries ``retry_after`` — one
    un-stamped failure in the set means the exponential cool-down owns
    the leaf, not the stamp."""
    root = _root(store)
    task = _node(store, root)
    stamped = _job(
        store,
        task,
        status="failed",
        meta_extra={"retry_after": (NOW + timedelta(minutes=40)).isoformat()},
    )
    plain = _job(store, task, status="failed")
    for job in (stamped, plain):
        store.add_tag(task, Tag.open(f"child-failed:{job}"), set_by="system")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=25))

    assert res.rescued == [(task, "stalled", "idle")]


def test_latched_retry_after_past_wake_is_stalled(store: Store) -> None:
    """A latched transient park past its wake plus the grace is a real
    stall — the sweeper should have re-armed it by now."""
    root = _root(store)
    task = _node(store, root)
    job = _job(
        store,
        task,
        status="failed",
        meta_extra={"retry_after": (NOW + timedelta(minutes=10)).isoformat()},
    )
    store.add_tag(task, Tag.open(f"child-failed:{job}"), set_by="system")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=25))

    assert res.rescued == [(task, "stalled", "idle")]


def test_hold_no_dispatch_node_is_a_wait(store: Store) -> None:
    """review td688453: ``hold:no-dispatch`` rides the shared exclusion
    registry — a tagged node is never minted, so its silence is the
    coordinator's hold, not a stall. The note names the tag."""
    root = _root(store)
    task = _node(store, root, tags=("hold:no-dispatch",))

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=25))

    assert res.rescued == []
    assert res.held == 1
    assert _rescue_tags(store, task) == []
    assert any("hold:no-dispatch" in note for note in res.notes)


def test_hold_no_dispatch_on_an_ancestor_is_a_wait(store: Store) -> None:
    """The hold's reach is the whole subtree — ``_excluded`` walks
    ancestors, so a tagged plan root parks every descendant."""
    root = _root(store, tags=("hold:no-dispatch",))
    task = _node(store, root)

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=25))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []
    assert any(f"hold:no-dispatch on td{root}" in note for note in res.notes)


def _body(store: Store, ref_id: int, text: str) -> None:
    store.chunks.insert_chunks(
        ref_id,
        [ChunkInsert(ord=0, text=text, meta={"chunk_kind": TODO_BODY_KIND})],
    )


def test_file_overlap_hold_is_a_wait(store: Store) -> None:
    """review td688453: an idle build whose brief names a file a
    started, unlanded build touches waits on that one's land — the
    td603849 sequence, not a stall."""
    root = _root(store)
    building = _node(store, root, title="first build")
    _job(store, building, status="running")
    store.stamp_ref_meta(
        building, {"branch_files": ["src/precis/workers/plan_dispatch.py"]}
    )
    root2 = _root(store)
    waiting = _node(store, root2, title="second build")
    _body(store, waiting, "Fix src/precis/workers/plan_dispatch.py too")
    _job(store, waiting, status="failed")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=25))

    assert res.rescued == []
    assert _rescue_tags(store, waiting) == []
    assert any(
        f"file-overlap hold — waits on td{building} (building)" in note
        for note in res.notes
    )


def _claim_lease(store: Store, ref_id: int, holder: str, ttl: timedelta) -> None:
    """A ``claimed-by:<holder>`` open tag carrying a live lease —
    ``ref_tags.expires_at`` ``ttl`` out, the shape
    ``guards.stamp_claim_lease`` writes."""
    store.add_tag(ref_id, Tag.open(holder), set_by="system")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags rt SET expires_at = %s FROM tags t "
            "WHERE rt.tag_id = t.tag_id AND rt.ref_id = %s "
            "AND t.namespace = 'OPEN' AND t.value = %s",
            (NOW + ttl, ref_id, holder),
        )
        conn.commit()


def test_queued_job_gated_by_a_live_claim_lease_names_the_claim(
    store: Store,
) -> None:
    """``parent_not_paused``'s lease arm: a queued job under a node with
    a live ``claimed-by:`` lease is the claimer's wait — the note names
    the claim, like a ``child-failed:`` gate."""
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="queued")
    _claim_lease(store, task, "claimed-by:agent-x", timedelta(hours=4))

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == []
    assert res.held == 1
    assert _rescue_tags(store, task) == []
    assert any("gated by claimed-by:agent-x" in note for note in res.notes)


def test_queued_job_under_an_unstamped_claim_is_not_gated(
    store: Store,
) -> None:
    """A legacy ``claimed-by:`` row with no ``expires_at`` does not
    gate — the clause's pre-lease behaviour, so a dead queue behind
    one still reads stalled."""
    root = _root(store)
    task = _node(store, root, tags=("claimed-by:agent-x",))
    _job(store, task, status="queued")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == [(task, "stalled", "queued")]


def test_live_queued_job_is_a_wait_not_an_idle_stall(store: Store) -> None:
    """A queued job waits for an executor slot — 25 min of silence is past
    the idle deadline, but a queued node is judged on its queue (30 min),
    never as idle."""
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="queued")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=25))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []


def test_queued_job_gated_by_a_child_failed_tag_names_the_tag(
    store: Store,
) -> None:
    """``parent_not_paused`` refuses to claim a job under a node carrying
    ``child-failed:<job>``; past the 30 min queue deadline on a dead queue
    that read as a stall. It is the gate's wait, and the note names it."""
    root = _root(store)
    task = _node(store, root, tags=("child-failed:jo123",))
    _job(store, task, status="queued")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == []
    assert res.held == 1
    assert _rescue_tags(store, task) == []
    assert any("gated by child-failed:jo123" in note for note in res.notes)


def test_blocked_by_an_open_ask_after_a_failed_job_is_a_wait(store: Store) -> None:
    """The blocker owns the wait: an idle node behind an open ask is not
    filed as its own stall."""
    root = _root(store)
    ask = _ask(store, root, tags=("ask-user",))
    task = _node(store, root)
    _job(store, task, status="failed")
    store.add_link(
        src_ref_id=task, dst_ref_id=ask, relation="blocked-by", set_by="system"
    )

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=25))

    assert res.held == 1
    assert res.rescued == []
    assert _rescue_tags(store, task) == []


def test_node_log_mtime_is_one_signal_not_one_per_job(
    store: Store, tmp_path: Path
) -> None:
    """Every job of a node reads the same per-todo ``run_slice.log``;
    the evidence lists its mtime once, as the node's."""
    root = _root(store)
    task = _node(store, root)
    log = tmp_path / "run_slice.log"
    log.write_text("x\n", encoding="utf-8")
    stale = NOW - timedelta(hours=1)
    os.utime(log, (stale.timestamp(), stale.timestamp()))
    handle = {"compute_handle": {"pid": 1, "log": str(log)}}
    _job(store, task, status="failed", meta_extra=handle)
    _job(store, task, status="running", meta_extra=handle)

    res = fr.run_stall_pass(store, NOW + timedelta(hours=5, minutes=1))

    assert res.rescued == [(task, "stalled", "build")]
    (comment,) = _comments(store, task)
    assert comment.count("log mtime") == 1
    assert "- node log mtime:" in comment


# ── dry run, kill switch, dedupe, evidence ────────────────────────


def test_second_pass_files_nothing_new(store: Store) -> None:
    root = _root(store)
    nodes = [_node(store, root, title=f"slice {i}") for i in range(3)]

    first = fr.run_stall_pass(store, NOW + timedelta(minutes=21))
    assert len(first.rescued) == 3
    (gripe,) = _sig_gripes(store)
    assert "cluster:dark-factory" in _open_tags(store, gripe)
    for n in nodes:
        assert _links(store, n) == [gripe]

    second = fr.run_stall_pass(store, NOW + timedelta(minutes=40))
    assert second.rescued == [] and second.held == 3
    assert _sig_gripes(store) == [gripe]
    for n in nodes:
        assert len(_comments(store, n)) == 1
        assert _rescue_tags(store, n) == ["rescue:stalled"]


def test_dry_run_writes_nothing_and_lists_would_rescue(store: Store) -> None:
    root = _root(store)
    task = _node(store, root)

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21), dry_run=True)

    assert res.dry_run is True
    assert res.would_rescue == [(task, "stalled", "idle")]
    assert _rescue_tags(store, task) == []
    assert _comments(store, task) == []
    assert _sig_gripes(store) == []


def test_kill_switch_writes_nothing(store: Store) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config (host, service, prio) "
            "VALUES ('*', 'plan_dispatch', 0)"
        )
    root = _root(store)
    task = _node(store, root)

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))

    assert res.read_only is not None and "prio 0" in res.read_only
    assert res.dry_run is True
    assert res.would_rescue == [(task, "stalled", "idle")]
    assert _rescue_tags(store, task) == []
    assert _sig_gripes(store) == []


def test_evidence_names_stage_deadline_signal_and_commit(store: Store) -> None:
    root = _root(store)
    task = _node(store, root, title="with commits")
    _job(
        store,
        task,
        status="failed",
        meta_extra={"branch_commits": ["abc1234 feat: x", "def5678 feat: y"]},
    )
    bare = _node(store, root, title="no commits")

    fr.run_stall_pass(store, NOW + timedelta(minutes=21))

    (comment,) = _comments(store, task)
    assert comment.startswith("rescue: stalled")
    assert "stage idle" in comment
    assert "deadline 20 min" in comment
    assert "newest signal" in comment and "min ago" in comment
    assert f"slice/{task}" in comment
    assert "abc1234" in comment
    (bare_comment,) = _comments(store, bare)
    assert "slice/" not in bare_comment
    assert "abc1234" not in bare_comment


def test_one_failing_enter_rescue_does_not_stop_the_pass(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(store)
    bad = _node(store, root, title="raises")
    good = _node(store, root, title="fine")
    real = fr.enter_rescue

    def fake(store: Store, node: int, cls: str, ev: Any) -> Any:
        if node == bad:
            raise RuntimeError("boom")
        return real(store, node, cls, ev)

    monkeypatch.setattr(fr, "enter_rescue", fake)

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))

    assert res.rescued == [(good, "stalled", "idle")]
    assert _rescue_tags(store, good) == ["rescue:stalled"]
    assert any("rescue failed" in n and f"td{bad}" in n for n in res.notes)


# ── lanes: parked vs no fit ───────────────────────────────────────


def test_lane_parked_node_with_fresh_stamps_is_not_rescued(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fit lane exists but is full: the node is PARKED, not stalled —
    the live jobs on that lane are judged by their own stage deadlines."""
    full = Lane("full-lane", "devin_tree", "s", 1, frozenset({"code"}))
    use_lanes(monkeypatch, full)
    monkeypatch.setattr(
        "precis.plans.lanes.live_lane_counts", lambda store: {"full-lane": 1}
    )
    root = _root(store)
    task = _node(store, root)

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []
    assert any("parked" in n for n in res.notes)

    # A node whose own failed job carries a fresh stamp is not rescued
    # either — the stamp is its newest signal.
    root2 = _root(store)
    task2 = _node(store, root2)
    _job(store, task2, status="failed")
    res = fr.run_stall_pass(store, NOW + timedelta(minutes=5))
    assert res.rescued == []
    assert _rescue_tags(store, task2) == []


def test_reviewer_with_no_lane_fit_is_rescued(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No enabled lane takes the node's class: the tick could never
    dispatch it — that is a stall, not a park."""
    only_code = Lane("only-code", "devin_tree", "s", 2, frozenset({"code"}))
    use_lanes(monkeypatch, only_code)
    root = _root(store)
    review = _node(
        store,
        root,
        title="reviewer",
        meta={"generate": {"role": "reviewer", "from": "td1"}, "task_class": "review"},
    )

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))

    assert res.rescued == [(review, "stalled", "idle")]
    (comment,) = _comments(store, review)
    assert "no enabled lane fits review" in comment


@pytest.mark.parametrize("builder_lane_full", [False, True])
def test_reviewer_with_full_alternative_lane_uses_fallback_availability(
    store: Store, monkeypatch: pytest.MonkeyPatch, builder_lane_full: bool
) -> None:
    """An overdue review stalls when the builder's vendor can take it.
    Both lanes must be full for the capacity hold in this setup."""
    built = Lane("codex", "codex_exec", "s", 1, frozenset({"review"}), vendor="openai")
    alt = Lane("claude-sub", "claude_p", "s", 1, frozenset({"*"}), vendor="anthropic")
    use_lanes(monkeypatch, built, alt)
    monkeypatch.setattr(
        "precis.plans.lanes.live_lane_counts",
        lambda store: {"claude-sub": 1, "codex": int(builder_lane_full)},
    )
    root = _root(store)
    build = _node(store, root, title="build", status="done")
    _job(
        store,
        build,
        status="succeeded",
        meta_extra={"lane": "codex", "branch_commits": ["abc1234 feat: x"]},
    )
    review = _node(
        store,
        root,
        title="reviewer",
        meta={
            "generate": {"role": "reviewer", "from": f"td{build}"},
            "task_class": "review",
        },
    )

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))

    if builder_lane_full:
        assert res.rescued == []
        assert _rescue_tags(store, review) == []
        assert any("parked (full: claude-sub, codex)" in n for n in res.notes)
    else:
        assert res.rescued == [(review, "stalled", "idle")]
        assert _rescue_tags(store, review) == ["rescue:stalled"]


def test_reviewer_with_free_alternative_lane_is_judged(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The vendor-aware verdict still rescues a silent reviewer an
    alternative-vendor lane could take right now."""
    built = Lane("codex", "codex_exec", "s", 1, frozenset({"review"}), vendor="openai")
    alt = Lane("claude-sub", "claude_p", "s", 2, frozenset({"*"}), vendor="anthropic")
    use_lanes(monkeypatch, built, alt)
    root = _root(store)
    build = _node(store, root, title="build", status="done")
    _job(
        store,
        build,
        status="succeeded",
        meta_extra={"lane": "codex", "branch_commits": ["abc1234 feat: x"]},
    )
    review = _node(
        store,
        root,
        title="reviewer",
        meta={
            "generate": {"role": "reviewer", "from": f"td{build}"},
            "task_class": "review",
        },
    )

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))

    assert res.rescued == [(review, "stalled", "idle")]
    assert _rescue_tags(store, review) == ["rescue:stalled"]


def test_reviewer_judged_when_only_build_vendor_is_enabled(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No alternative vendor's lane is enabled at all: ``_review_lane``'s
    third step lets the builder's own vendor review — the reviewer is
    dispatchable, so a silent one is a stall, not a park."""
    built = Lane("codex", "codex_exec", "s", 1, frozenset({"review"}), vendor="openai")
    code_only = Lane(
        "devin", "devin_tree", "s", 2, frozenset({"code"}), vendor="cognition"
    )
    use_lanes(monkeypatch, built, code_only)
    root = _root(store)
    build = _node(store, root, title="build", status="done")
    _job(
        store,
        build,
        status="succeeded",
        meta_extra={"lane": "codex", "branch_commits": ["abc1234 feat: x"]},
    )
    review = _node(
        store,
        root,
        title="reviewer",
        meta={
            "generate": {"role": "reviewer", "from": f"td{build}"},
            "task_class": "review",
        },
    )

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))

    assert res.rescued == [(review, "stalled", "idle")]
    assert _rescue_tags(store, review) == ["rescue:stalled"]


# ── queued jobs and claim holds (gr685082) ───────────────────────

#: Every placement token a run_slice or land_slice declares, plus each
#: lane adapter's ``<a>_bin`` (td543917, gr676054): the default test host.
_ALL_PLACEMENT = frozenset(
    {
        "repo_checkout",
        "builder",
        "git",
        "gate_image",
        "uv_bin",
        "claude_bin",
        "codex_bin",
        "kimi_bin",
        "devin_bin",
    }
)


@pytest.fixture(autouse=True)
def _host_advertises_every_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """One live host offering every placement token, so a queued job's
    silence is judged on the queue, not on a binary the fixture lacks.
    Tests that exercise a placement gap re-patch it."""
    monkeypatch.setattr(
        _common, "_advertised_by_host", lambda conn: {"host-a": set(_ALL_PLACEMENT)}
    )


def test_queued_build_on_parked_pinned_lane_is_claimable(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane's park is not a claim hold (gr685082). The real claim loop
    takes the queued build without reading the lane; ``run_slice.submit``
    then parks it on the lane, so the parked lane is submit's verdict."""
    parked = Lane(
        "build-lane",
        "devin_tree",
        "s",
        1,
        frozenset({"code"}),
        parked_reason="quota:window",
    )
    use_lanes(monkeypatch, parked)
    # The claim gate reads the claiming host's own tokens (reserve_host).
    monkeypatch.setattr(_common, "reserve_host", lambda: "host-a")
    root = _root(store)
    task = _node(store, root)
    job = _job(
        store,
        task,
        status="queued",
        params={"lane": "build-lane"},
        meta_extra={"adapter": "devin_tree"},
    )

    with store.pool.connection() as conn:
        claimed = claim_executor_jobs(conn, executor="ssh_node", limit=1)
        conn.commit()

    assert [r[0] for r in claimed] == [job]


def test_queued_build_on_parked_pinned_lane_rescues_when_queue_is_silent(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard against over-holding: a queue the claim never drains still
    rescues on the plain queued signature, even with its lane parked."""
    parked = Lane(
        "build-lane",
        "devin_tree",
        "s",
        1,
        frozenset({"code"}),
        parked_reason="quota:window",
    )
    use_lanes(monkeypatch, parked)
    root = _root(store)
    task = _node(store, root)
    _job(
        store,
        task,
        status="queued",
        params={"lane": "build-lane"},
        meta_extra={"adapter": "devin_tree"},
    )

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == [(task, "stalled", "queued")]
    assert _rescue_tags(store, task) == ["rescue:stalled"]
    (gripe,) = _sig_gripes(store)
    ref = store.get_ref(kind="gripe", id=gripe)
    assert ref is not None
    assert "stage queued silent past its deadline" in str(ref.title)


def test_queued_build_no_live_host_advertises_its_adapter_is_a_wait(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim's placement gate never takes a pinned build whose adapter
    binary no live host advertises, so its silence is the placement's and
    the node parks (gr685082). The rescue and the claim gate agree."""
    lane = Lane("build-lane", "devin_tree", "s", 1, frozenset({"code"}))
    use_lanes(monkeypatch, lane)
    no_devin = set(_ALL_PLACEMENT - {"devin_bin"})
    monkeypatch.setattr(
        _common, "_advertised_by_host", lambda conn: {"host-a": no_devin}
    )
    root = _root(store)
    task = _node(store, root)
    job = _job(
        store,
        task,
        status="queued",
        params={"lane": "build-lane"},
        meta_extra={"adapter": "devin_tree"},
    )

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []
    assert any(
        "no live host advertises all of" in n and "devin_bin" in n for n in res.notes
    )
    assert not _common._placement_ok(
        _job_meta(store, job), {"host-a": no_devin}, "host-a"
    )


def test_queued_pinned_job_names_its_pinned_host_in_the_wait(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``target_node`` pin narrows the placement search to that host, so
    the wait reason names it rather than claiming no live host has the
    tokens when another host does (gr685082)."""
    lane = Lane("build-lane", "devin_tree", "s", 1, frozenset({"code"}))
    use_lanes(monkeypatch, lane)
    no_devin = set(_ALL_PLACEMENT - {"devin_bin"})
    monkeypatch.setattr(
        _common,
        "_advertised_by_host",
        lambda conn: {"host-a": no_devin, "host-b": set(_ALL_PLACEMENT)},
    )
    root = _root(store)
    task = _node(store, root)
    _job(
        store,
        task,
        status="queued",
        params={"lane": "build-lane", "target_node": "host-a"},
        meta_extra={"adapter": "devin_tree"},
    )

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []
    assert any(
        "pinned host host-a does not advertise all of" in n and "devin_bin" in n
        for n in res.notes
    )
    assert not any("no live host advertises" in n for n in res.notes)


def test_queued_land_is_judged_on_its_own_placement(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live job's own job_type decides the placement, not the todo's
    build op: a queued land waits on the repo checkout it needs, which no
    live host advertises (gr685082). ``gate_image`` is a rollout token and
    would not hold it (gr701559)."""
    no_repo = set(_ALL_PLACEMENT - {"repo_checkout"})
    monkeypatch.setattr(
        _common, "_advertised_by_host", lambda conn: {"host-a": no_repo}
    )
    root = _root(store)
    task = _node(store, root)
    job = _job(store, task, job_type="land_slice", status="queued")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []
    assert any("repo_checkout" in n for n in res.notes)
    assert not _common._placement_ok(
        _job_meta(store, job), {"host-a": no_repo}, "host-a"
    )


# ── queued jobs and operator claim holds (gr788297) ───────────────


def test_queued_pinned_job_on_a_cap_zero_host_is_a_wait(store: Store) -> None:
    """gr788297: the arming runbook's hold-at-zero — ``claim.executor_cap.
    ssh_node.<host> = 0`` — suspends claims on a host the way a placement
    gap does: a job pinned there waits on the operator's hold, and a
    silent queue under it is deliberate, not a stall
    (``plan_dispatch._zero_cap_hosts`` names the same hold in its
    unplaced ask)."""
    store.set_setting("claim.executor_cap.ssh_node.host-a", "0")
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="queued", params={"target_node": "host-a"})

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []
    assert any("pinned host host-a has ssh_node cap 0" in n for n in res.notes)


def test_queued_job_every_capable_host_cap_zero_is_a_wait(store: Store) -> None:
    """The unsuffixed ``claim.executor_cap.<executor> = 0`` suspends the
    executor on every host without an override — an unpinned job with
    nowhere to claim is the same deliberate wait."""
    store.set_setting("claim.executor_cap.ssh_node", "0")
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="queued")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []
    assert any(
        "every host that could claim it has ssh_node cap 0" in n for n in res.notes
    )


def test_queued_job_an_uncapped_capable_host_still_rescues(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The over-holding guard: while ONE host able to take the job is
    uncapped a claim is possible, so a queue that still hasn't moved is
    a stall."""
    monkeypatch.setattr(
        _common,
        "_advertised_by_host",
        lambda conn: {"host-a": set(_ALL_PLACEMENT), "host-b": set(_ALL_PLACEMENT)},
    )
    store.set_setting("claim.executor_cap.ssh_node.host-a", "0")
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="queued")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == [(task, "stalled", "queued")]


def test_queued_job_cap_zero_and_reserve_hold_different_hosts(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.workers.service_config import set_reserve

    monkeypatch.setattr(
        _common,
        "_advertised_by_host",
        lambda conn: {"host-a": set(_ALL_PLACEMENT), "host-b": set(_ALL_PLACEMENT)},
    )
    store.set_setting("claim.executor_cap.ssh_node.host-a", "0")
    set_reserve(store, "host-b", hours=1)
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="queued")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []
    assert any("cap 0 or is under an operator reserve" in n for n in res.notes)


def test_queued_job_a_cap_on_another_executor_is_no_hold(store: Store) -> None:
    """The cap keys are per-executor: an ``ssh_node`` hold-at-zero says
    nothing about a ``job_inproc`` row."""
    store.set_setting("claim.executor_cap.ssh_node", "0")
    root = _root(store)
    task = _node(store, root)
    _job(store, task, job_type="plan_tick", executor="job_inproc", status="queued")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == [(task, "stalled", "queued")]


def test_queued_pinned_job_on_a_reserved_host_is_a_wait(store: Store) -> None:
    """A live operator reserve on a ``respect_reserve`` lane
    (``ssh_node``, ``claude_docker``, ``job_inproc``) stops fresh claims
    on the pinned host until it expires — the same deliberate silence."""
    from precis.workers.service_config import set_reserve

    set_reserve(store, "host-a", hours=1)
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="queued", params={"target_node": "host-a"})

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []
    assert any(
        "pinned host host-a is under an operator reserve" in n for n in res.notes
    )


def test_queued_job_type_held_at_max_running_zero_is_a_wait(store: Store) -> None:
    """``claim.max_running.<job_type> = 0`` excludes the type from every
    fresh pick on every host — a fleet-wide type stop, not a stall."""
    store.set_setting("claim.max_running.run_slice", "0")
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="queued")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == []
    assert _rescue_tags(store, task) == []
    assert any("job_type run_slice held at claim.max_running 0" in n for n in res.notes)


# ── the tick heartbeat (tick-silent) ──────────────────────────────


def test_tick_silent_rescues_the_tick_todo_once(store: Store) -> None:
    tid = _tick_todo(store)

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))
    assert res.rescued == [(tid, "tick-silent", "heartbeat")]
    assert _rescue_tags(store, tid) == ["rescue:tick-silent"]

    # A second pass adds nothing: the rescue: tag holds the todo exempt.
    second = fr.run_stall_pass(store, NOW + timedelta(minutes=40))
    assert second.rescued == []
    assert _rescue_tags(store, tid) == ["rescue:tick-silent"]
    assert len(_sig_gripes(store)) == 1


def test_tick_silent_fresh_pass_is_not_silent(store: Store) -> None:
    tid = _tick_todo(store)
    # A fresh succeeded plan_dispatch job means the tick is alive.
    _job(store, tid, job_type="plan_dispatch", status="succeeded")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=10))
    assert res.rescued == []
    assert _rescue_tags(store, tid) == []


def test_tick_silent_paused_tick_todo_is_not_judged(store: Store) -> None:
    """A paused tick todo is the lane-ticker setup (``--only plan_tick``
    runs the pass with no job row): held, never rescued."""
    tid = _tick_todo(store, status="paused")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == []
    assert _rescue_tags(store, tid) == []
    assert any("tick" in n for n in res.notes)


def test_tick_silent_live_service_pass_needs_no_recurring_todo(
    store: Store,
) -> None:
    """The lane ticker (``--only plan_tick``) mints no job and keeps no
    live recurring todo: its ``worker_logs`` pass heartbeat alone means
    not silent — the prod setup the guard misread (gr576145)."""
    _pass_heartbeat(store)

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=10))

    assert res.rescued == []
    assert not any("tick-silent" in n for n in res.notes)


def test_tick_silent_live_service_pass_spares_the_stale_todo(
    store: Store,
) -> None:
    """A stale recurring todo is not rescued while the pass heartbeats:
    the dispatcher moved, so the todo's own silence is not a stall."""
    tid = _tick_todo(store)
    _pass_heartbeat(store)

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=10))

    assert res.rescued == []
    assert _rescue_tags(store, tid) == []


def test_tick_silent_when_neither_source_moves(store: Store) -> None:
    """A stale pass heartbeat plus a stale recurring todo is silent —
    rescues the todo once, same as a dead job-path tick."""
    tid = _tick_todo(store)
    _pass_heartbeat(store, age=timedelta(minutes=45))

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))

    assert res.rescued == [(tid, "tick-silent", "heartbeat")]
    assert _rescue_tags(store, tid) == ["rescue:tick-silent"]


# ── the job type ──────────────────────────────────────────────────


def test_factory_rescue_tick_is_a_registered_job_inproc_type_with_dedupe() -> None:
    spec = get_job_type("factory_rescue_tick")
    assert spec is not None
    assert spec.dedupe_pending is True
    assert spec.queue_ttl_exempt is True
    assert spec.compatible_executors == frozenset({"job_inproc"})
    assert "factory_rescue_tick" in known_job_types()


def test_tick_dispatch_dry_run_param_writes_nothing(store: Store) -> None:
    from precis.workers.job_types import factory_rescue_tick

    root = _root(store)
    task = _node(store, root)
    _age(store, task, timedelta(hours=1))
    meta_seen: dict[str, Any] = {}
    chunks: list[tuple[str, str]] = []
    failures: list[str] = []
    ctx = SimpleNamespace(
        store=store,
        meta={"params": {"dry_run": True}},
        set_meta=lambda **kw: meta_seen.update(kw),
        append_chunk=lambda kind, text: chunks.append((kind, text)),
        record_failure=lambda msg: failures.append(msg),
    )

    factory_rescue_tick._dispatch(ctx, factory_rescue_tick.SPEC)

    assert failures == []
    counts = meta_seen["factory_rescue"]
    assert counts["would_rescue"] == 1 and counts["dry_run"] is True
    kind, text = chunks[0]
    assert kind == "job_summary"
    assert "would_rescue=1" in text and "dry_run=True" in text
    assert _rescue_tags(store, task) == []
    assert _sig_gripes(store) == []

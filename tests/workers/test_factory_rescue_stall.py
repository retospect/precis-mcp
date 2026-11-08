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

import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from precis.plans.lanes import Lane
from precis.plans.templates import PENDING_SHA
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.workers import factory_rescue as fr
from precis.workers._mint import mint_background_job
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
    status: str = "queued",
    meta_extra: dict[str, Any] | None = None,
) -> int:
    with store.tx() as conn:
        ref = mint_background_job(
            store,
            conn=conn,
            job_type=job_type,
            executor="ssh_node",
            title=job_type,
            params={},
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


def test_queued_job_deadline_is_30_minutes(store: Store) -> None:
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="queued")

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=29))
    assert res.rescued == []
    res = fr.run_stall_pass(store, NOW + timedelta(minutes=31))
    assert res.rescued == [(task, "stalled", "queued")]
    assert _rescue_tags(store, task) == ["rescue:stalled"]


def test_running_build_is_judged_at_5_hours(store: Store) -> None:
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="running")

    res = fr.run_stall_pass(store, NOW + timedelta(hours=4))
    assert res.rescued == []
    res = fr.run_stall_pass(store, NOW + timedelta(hours=5, minutes=1))
    assert res.rescued == [(task, "stalled", "build")]


def test_gate_not_open_is_judged_at_72_hours(store: Store) -> None:
    root = _root(store)
    build = _node(store, root, title="build", status="done")
    gate = _gate(store, root, "a1b2c3d4e5f", blocked_by=build)

    res = fr.run_stall_pass(store, NOW + timedelta(hours=71))
    assert res.rescued == []
    res = fr.run_stall_pass(store, NOW + timedelta(hours=73))
    assert res.rescued == [(gate, "stalled", "gate")]
    assert _rescue_tags(store, gate) == ["rescue:stalled"]


def test_post_land_write_failure_rescues_the_gate(store: Store) -> None:
    """The build landed but the gate still holds PENDING_SHA: land_slice's
    post-land write (sha into meta.gate.args) failed — rescue the GATE,
    class ``post-land-write``, at the idle deadline."""
    root = _root(store)
    build = _node(store, root, title="build", status="done")
    _comment(store, build, "landed: a1b2c3d — the fix")
    gate = _gate(store, root, PENDING_SHA, blocked_by=build)

    res = fr.run_stall_pass(store, NOW + timedelta(minutes=21))

    assert res.rescued == [(gate, "post-land-write", "idle")]
    assert _rescue_tags(store, gate) == ["rescue:post-land-write"]
    (comment,) = _comments(store, gate)
    assert "landed: a1b2c3d" in comment


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


def test_reviewer_parked_on_full_alternative_lane_is_held(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``plan_dispatch._review_lane`` waits rather than pick the build
    vendor's lane while another vendor's fit lane exists: a reviewer
    waiting on a full alternative is PARKED even though the builder's
    vendor lane sits free — an unrestricted pick saw that free lane and
    rescued the reviewer (td575363)."""
    built = Lane("codex", "codex_exec", "s", 1, frozenset({"review"}), vendor="openai")
    alt = Lane("claude-sub", "claude_p", "s", 1, frozenset({"*"}), vendor="anthropic")
    use_lanes(monkeypatch, built, alt)
    monkeypatch.setattr(
        "precis.plans.lanes.live_lane_counts", lambda store: {"claude-sub": 1}
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

    assert res.rescued == []
    assert _rescue_tags(store, review) == []
    assert any("parked" in n for n in res.notes)


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

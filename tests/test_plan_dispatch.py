"""Dark-factory plan dispatcher tick (td542096): ``plan_dispatch`` walking a
plan subtree through gates, loops, phases, dispatch and hygiene on the dev
DB. ``run_slice`` jobs are faked by marking the minted job succeeded (and
its task done) between passes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from precis.plans import lanes as L
from precis.plans import release as R
from precis.plans import templates as T
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.workers._mint import mint_background_job
from precis.workers.job_types import get_job_type, known_job_types, run_slice
from precis.workers.job_types import plan_dispatch as pd
from precis.workers.job_types import release_poll as rp
from tests._lane_rows import seed_lane, use_lanes
from tests._refs import insert_gripe

PAST = {"kind": "not-before", "args": {"ts": "2020-01-01T00:00:00Z"}}
FUTURE = {"kind": "not-before", "args": {"ts": "2999-01-01T00:00:00Z"}}


@pytest.fixture(autouse=True)
def _factory_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    # The engine-review gate reads the cut range from git (gr597625) —
    # stub its seam: the host checkout's real ``origin/prod..origin/main``
    # carries engine lands that must not leak into a dispatch test.
    monkeypatch.setattr(rp, "_git_lands", lambda: [])


def _status(store: Store, ref_id: int) -> str | None:
    for t in store.tags_for(ref_id):
        if t.prefix == "STATUS":
            return str(t.value)
    return None


def _open_tags(store: Store, ref_id: int) -> set[str]:
    return {str(t.value) for t in store.tags_for(ref_id) if t.namespace == "open"}


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    ref = store.get_ref(kind="todo", id=ref_id)
    assert ref is not None
    return dict(ref.meta or {})


def _jobs_under(store: Store, todo_id: int) -> list[tuple[int, dict[str, Any]]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = 'job' AND parent_id = %s "
            "AND retired_at IS NULL ORDER BY ref_id",
            (todo_id,),
        ).fetchall()
    return [(int(r[0]), dict(r[1])) for r in rows]


def _all_jobs(store: Store) -> int:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT count(*) FROM refs WHERE kind = 'job'").fetchone()
    assert row is not None
    return int(row[0])


def _children(store: Store, todo_id: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'todo' AND parent_id = %s "
            "ORDER BY ref_id",
            (todo_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _finish_live_jobs(store: Store, *, close_task: bool = True) -> int:
    """The fake run_slice: every live job succeeds and closes its task."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.parent_id FROM refs r
             WHERE r.kind = 'job' AND NOT EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                    WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS'
                      AND t.value IN ('succeeded', 'failed', 'cancelled'))
            """
        ).fetchall()
    for job_id, parent_id in rows:
        store.add_tag(
            int(job_id),
            Tag.closed("STATUS", "succeeded"),
            set_by="system",
            replace_prefix=True,
        )
        if close_task:
            store.add_tag(
                int(parent_id),
                Tag.closed("STATUS", "done"),
                set_by="system",
                replace_prefix=True,
            )
    return len(rows)


def _dogfood_body(store: Store, loop_todo_id: int, **_ctx: Any) -> T.PlanMint:
    _loop, root = T._loop_context(store, loop_todo_id)

    def build(m: T._Minter) -> None:
        m.node("dogfood", "Dogfood the result", {"op": "run_slice"}, body="Use it.")

    return T._run_body(store, loop_todo_id, root, build)


def _small_plan(
    store: Store, *, body: str = "test-body", loop_max: int = 1
) -> T.PlanMint:
    """root phase: A (phase: t1, t2) -> gate G (not-before, past) -> loop L."""

    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        a = m.node("A", "Phase A", {"auto_close": True})
        m.node("t1", "Task one", {"op": "run_slice"}, parent=a, body="Do one.")
        m.node("t2", "Task two", {"op": "run_slice"}, parent=a, body="Do two.")
        m.node("G", "Wait", {"gate": PAST}, blocked_by=("A",))
        m.node(
            "L",
            "Loop",
            {"loop": {"max": loop_max, "until": FUTURE, "body": body}},
            blocked_by=("G",),
        )

    return T._run(store, None, build)


def test_registry_and_known_types() -> None:
    spec = get_job_type("plan_dispatch")
    assert spec is not None
    assert spec.dedupe_pending is True
    assert spec.compatible_executors == frozenset({"job_inproc"})
    assert "plan_dispatch" in known_job_types()


def test_small_plan_walks_to_completion(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(T.BODIES, "test-body", _dogfood_body)
    mint = _small_plan(store)
    ids = mint.ids

    # Pass 1: the two tasks of phase A are dispatched; G and L wait on A.
    res = pd.run_pass(store)
    assert res.roots == 1
    assert res.dispatched == 2
    assert (res.gates_opened, res.gates_closed) == (0, 0)
    for key in ("t1", "t2"):
        jobs = _jobs_under(store, ids[key])
        assert len(jobs) == 1
        job_id, meta = jobs[0]
        assert meta["job_type"] == "run_slice"
        assert meta["executor"] == "ssh_node"
        assert meta["params"] == {"todo_id": ids[key]}
        assert _status(store, job_id) == "queued"
    # A second pass while the jobs are live mints nothing more.
    assert pd.run_pass(store).dispatched == 0
    assert _finish_live_jobs(store) == 2

    # Pass 2: phase A closes.
    res = pd.run_pass(store)
    assert res.phases_closed == 1
    assert _status(store, ids["A"]) == "done"

    # Pass 3: the gate opens; the loop (unblocked in the same pass) mints its body.
    res = pd.run_pass(store)
    assert (res.gates_opened, res.loops_minted) == (1, 1)
    assert _status(store, ids["G"]) == "done"
    assert _meta(store, ids["G"])["gate_result"]["open"] is True
    assert _meta(store, ids["L"])["loop"]["count"] == 1
    body = _children(store, ids["L"])
    assert len(body) == 1
    assert f"plan:{mint.root_id}" in _open_tags(store, body[0])

    # Pass 4: the body task is dispatched.
    assert pd.run_pass(store).dispatched == 1
    assert _finish_live_jobs(store) == 1

    # Pass 5: count == max closes the loop, then the root phase closes.
    res = pd.run_pass(store)
    assert (res.loops_minted, res.loops_closed) == (0, 1)
    assert res.phases_closed == 1
    assert _status(store, ids["L"]) == "done"
    assert _status(store, mint.root_id) == "done"
    assert res.summary().startswith("plan_dispatch: roots=1 ")

    # Done plans are not roots any more.
    assert pd.run_pass(store).roots == 0


def test_kill_switch_makes_the_pass_read_only(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _small_plan(store)
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    meta_seen: dict[str, Any] = {}
    chunks: list[tuple[str, str]] = []
    ctx = SimpleNamespace(
        store=store,
        meta={"params": {}},
        set_meta=lambda **kw: meta_seen.update(kw),
        append_chunk=lambda kind, text: chunks.append((kind, text)),
    )
    pd._dispatch(ctx, pd.SPEC)
    assert meta_seen["plan_dispatch"]["dispatched"] == 2
    assert meta_seen["plan_dispatch"]["dry_run"] is True
    kind, text = chunks[0]
    assert kind == "job_summary"
    assert "dispatched=2" in text and "dry_run=True" in text
    assert "PRECIS_FACTORY=off" in text
    assert "would dispatch run_slice" in text
    assert _all_jobs(store) == 0
    for key in ("t1", "t2", "A", "G", "L"):
        assert _status(store, mint.ids[key]) == "open"


def test_service_config_prio_zero_is_the_other_switch(store: Store) -> None:
    _small_plan(store)
    assert pd.read_only_reason(store) is None
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config (host, service, prio) VALUES (%s, %s, 0)",
            ("*", pd.SERVICE),
        )
    reason = pd.read_only_reason(store)
    assert reason is not None and "prio 0" in reason
    res = pd.run_pass(store, dry_run=True, reason=reason)
    assert res.dispatched == 2
    assert _all_jobs(store) == 0


def _one_task_plan(store: Store) -> T.PlanMint:
    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        m.node("task", "The task", {"op": "run_slice"}, body="Do it.")

    return T._run(store, None, build)


def test_dict_op_pins_model_and_effort_on_the_job(store: Store) -> None:
    """gr549046: a child minted with a dict ``meta.op`` dispatches
    ``run_slice`` with the pinned params — the carrier for the triage's
    builder-model pick."""

    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        m.node(
            "task",
            "Build it",
            {
                "op": {
                    "type": "run_slice",
                    "params": {
                        "model": "claude-haiku-4-5-20251001",
                        "effort": "low",
                    },
                }
            },
            body="Do it.",
        )

    mint = T._run(store, None, build)
    assert pd.run_pass(store).dispatched == 1
    (job,) = _jobs_under(store, mint.ids["task"])
    assert job[1]["job_type"] == "run_slice"
    assert job[1]["params"] == {
        "todo_id": mint.ids["task"],
        "model": "claude-haiku-4-5-20251001",
        "effort": "low",
    }


def test_expired_claim_is_released(store: Store) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    store.add_tag(task, Tag.open("claimed-by:lane-claude-sub"), set_by="system")
    store.add_tag(
        task, Tag.closed("STATUS", "doing"), set_by="system", replace_prefix=True
    )
    # A fresh claim is left alone.
    res = pd.run_pass(store)
    assert (res.released, res.dispatched) == (0, 0)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags SET created_at = now() - interval '5 hours' "
            "WHERE ref_id = %s AND tag_id IN (SELECT tag_id FROM tags "
            "WHERE value = 'claimed-by:lane-claude-sub')",
            (task,),
        )
    res = pd.run_pass(store)
    assert res.released == 1
    assert "claimed-by:lane-claude-sub" not in _open_tags(store, task)
    assert _status(store, task) == "open"
    # Released this pass, dispatched on the next.
    assert pd.run_pass(store).dispatched == 1


def test_a_node_waits_for_a_mint_pass_still_in_flight(store: Store) -> None:
    """gr590954: a session mints each child with one ``put`` — the row
    commits open and linkless — and adds the ``blocked-by`` links in
    later calls, so a node minted mid-session gets no job before its
    links land. While the minting generator's job runs, both children
    wait (one under the generator, one under the root); once the job
    ends the linked blocker — not the mint — holds them."""

    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        m.node("gen", "Triage", {"generate": {"role": "triage", "from": "gr1"}})
        m.node("b", "Blocker", {"op": "run_slice"}, body="Blocks.")

    mint = T._run(store, None, build)
    gen, blocker = mint.ids["gen"], mint.ids["b"]
    job = _slice_job(store, gen, "run_slice", "running")

    # The session's first two puts land — one child under itself, one
    # under the root — each committed open and linkless (the put→link
    # gap gr590954 names).
    def kid(m: T._Minter) -> None:
        m.node("c", "Minted mid-pass", {"op": "run_slice"}, body="Do it.")

    c1 = T._run_body(store, gen, mint.root_id, kid).ids["c"]
    c2 = T._run_body(store, mint.root_id, mint.root_id, kid).ids["c"]

    # No job before the link: both hold while the session runs (the
    # pre-existing blocker, minted before the job, still dispatches —
    # its links are settled).
    res = pd.run_pass(store)
    assert res.dispatched == 1
    assert not _jobs_under(store, c1)
    assert not _jobs_under(store, c2)

    # The session links both and exits; the blocker now holds them.
    store.add_link(src_ref_id=c1, dst_ref_id=blocker, relation="blocked-by")
    store.add_link(src_ref_id=c2, dst_ref_id=blocker, relation="blocked-by")
    _set_job_status(store, job, "succeeded")
    pd.run_pass(store)
    assert not _jobs_under(store, c1)
    assert not _jobs_under(store, c2)

    # The blocker closes: both dispatch.
    store.add_tag(
        blocker, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )
    res = pd.run_pass(store)
    assert res.dispatched == 2
    assert len(_jobs_under(store, c1)) == 1
    assert len(_jobs_under(store, c2)) == 1


def _failed_job(
    store: Store,
    task: int,
    reason: str,
    *,
    lane: str | None = None,
    model: str | None = None,
    meta: dict[str, Any] | None = None,
) -> int:
    with store.tx() as conn:
        ref = mint_background_job(
            store,
            conn=conn,
            job_type="run_slice",
            executor="ssh_node",
            title="run_slice",
            params={"todo_id": task},
            parent_id=task,
            status="failed",
        )
    patch = {k: v for k, v in (("lane", lane), ("model", model)) if v is not None}
    patch.update(meta or {})
    if patch:
        store.stamp_ref_meta(ref.id, patch)
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=1, text=reason, meta={"chunk_kind": "job_event"})]
    )
    return int(ref.id)


def _slice_job(
    store: Store,
    task: int,
    job_type: str,
    status: str,
    *,
    params: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> int:
    with store.tx() as conn:
        ref = mint_background_job(
            store,
            conn=conn,
            job_type=job_type,
            executor="ssh_node",
            title=job_type,
            params=params or {"todo_id": task},
            parent_id=task,
            status=status,
        )
    if meta:
        store.stamp_ref_meta(ref.id, meta)
    return int(ref.id)


def _set_job_status(store: Store, job_id: int, status: str) -> None:
    store.add_tag(
        job_id, Tag.closed("STATUS", status), set_by="system", replace_prefix=True
    )


def test_build_task_closes_only_after_its_land(store: Store) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    store.add_tag(task, Tag.open("claimed-by:lane-claude-sub"), set_by="system")
    store.add_tag(
        task, Tag.closed("STATUS", "doing"), set_by="system", replace_prefix=True
    )
    _slice_job(store, task, "run_slice", "succeeded")
    # The build succeeded: no close on that alone, and no redispatch.
    res = pd.run_pass(store)
    assert (res.phases_closed, res.dispatched) == (0, 0)
    assert _status(store, task) == "doing"
    # run_slice's poll minted the lander; while it runs nothing moves.
    land = _slice_job(store, task, "land_slice", "queued")
    res = pd.run_pass(store)
    assert (res.phases_closed, res.dispatched) == (0, 0)
    assert _status(store, task) == "doing"
    # The land succeeded: the newest job is a succeeded land_slice.
    _set_job_status(store, land, "succeeded")
    res = pd.run_pass(store)
    assert _status(store, task) == "done"
    assert any(f"task td{task} closed: land_slice job:{land}" in n for n in res.notes)


def test_a_red_land_is_one_attempt_not_two(store: Store) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _slice_job(store, task, "run_slice", "succeeded")
    _slice_job(store, task, "land_slice", "failed")
    store.add_tag(task, Tag.open("attempt:1"), set_by="system")
    # build + red land = attempt 1 of 2: the tick retries the build.
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)
    assert _jobs_under(store, task)[-1][1]["job_type"] == "run_slice"


def test_attempt_free_key_matches_run_slice() -> None:
    """``plan_dispatch`` restates the key ``run_slice`` stamps (the
    dependency stays one-way); the two spellings must never drift."""
    assert pd.ATTEMPT_FREE_KEY == run_slice.ATTEMPT_FREE_KEY == "attempt_free"


def test_attempt_free_jobs_are_no_attempt(store: Store) -> None:
    """gr572118: a job that failed for a reason outside the slice's diff —
    ``meta.attempt_free``, e.g. a lane that never freed a slot — burned no
    attempt; the retry-cap ask must not fire on it."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    for _ in range(2):
        _failed_job(
            store,
            task,
            "run_slice: no lane free for task class 'code'",
            lane="claude-sub",
            meta={"attempt_free": "lane-wait"},
        )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)


def test_infra_red_land_relands_without_a_builder(store: Store) -> None:
    """gr570187: a land the gate infrastructure killed burns no attempt and
    needs no builder — the tick re-mints ``land_slice`` with the failed
    job's own params (idem ``land_slice:td<id>``)."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _slice_job(store, task, "run_slice", "succeeded")
    land_params = {
        "todo_id": task,
        "worktree": f"/checkout/.claude/worktrees/slice-td{task}",
        "branch": f"slice/{task}",
        "target_node": "slice-host",
        "checked": True,
    }
    _slice_job(
        store,
        task,
        "land_slice",
        "failed",
        params=land_params,
        meta={"attempt_free": "infra-land", "red_class": "gate infrastructure"},
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)
    job_id, meta = _jobs_under(store, task)[-1]
    assert meta["job_type"] == "land_slice"
    assert meta["params"] == land_params
    assert meta["idem_key"] == f"land_slice:td{task}"
    assert any("infra-land" in n and "not an attempt" in n for n in res.notes)


def test_ordinary_red_land_still_rebuilds(store: Store) -> None:
    """A land red on the diff itself (a lint ✖, a merge conflict — no infra
    mark, no ``attempt_free``) is still one real attempt: the tick rebuilds,
    as ``test_a_red_land_is_one_attempt_not_two`` pins."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _slice_job(store, task, "run_slice", "succeeded")
    _slice_job(store, task, "land_slice", "failed")
    store.add_tag(task, Tag.open("attempt:1"), set_by="system")
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)
    job = _jobs_under(store, task)[-1][1]
    assert job["job_type"] == "run_slice"
    assert job["params"]["todo_id"] == task


def test_reland_is_bounded_then_asks(store: Store) -> None:
    """gr570187: the re-land is bounded — ``LAND_AGAIN_CAP`` consecutive
    infra-red lands park the node with a "land again" ask (no builder);
    answering it re-arms and the next tick re-lands."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _slice_job(store, task, "run_slice", "succeeded")
    land_params = {
        "todo_id": task,
        "worktree": f"/checkout/.claude/worktrees/slice-td{task}",
        "branch": f"slice/{task}",
    }
    _slice_job(
        store,
        task,
        "land_slice",
        "failed",
        params=land_params,
        meta={"attempt_free": "infra-land"},
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)
    minted, minted_meta = _jobs_under(store, task)[-1]
    assert minted_meta["job_type"] == "land_slice"
    # The re-land dies on the gate again: the cap parks the node with an ask.
    _set_job_status(store, minted, "failed")
    store.stamp_ref_meta(minted, {"attempt_free": "infra-land"})
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (0, 1)
    (ask,) = [c for c in _children(store, mint.root_id) if c != task]
    ask_ref = store.get_ref(kind="todo", id=ask)
    assert ask_ref is not None
    assert "land failed outside the diff" in str(ask_ref.title)
    assert _meta(store, ask)["ask"]["options"][0] == "land again (no builder)"
    # Answering "land again (no builder)" re-arms; the next tick
    # re-lands, no second ask.
    store.chunks.insert_chunks(
        ask,
        [
            ChunkInsert(
                ord=1,
                text="answer: land again (no builder)",
                meta={"chunk_kind": "todo_comment"},
            )
        ],
    )
    store.add_tag(
        ask, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)
    assert _jobs_under(store, task)[-1][1]["job_type"] == "land_slice"


def _reland_ask(store: Store, task: int, root: int) -> int:
    """Park ``task`` behind the re-land cap ask and return the ask's id:
    ``LAND_AGAIN_CAP`` failed infra-land jobs at the tail, then one
    pass."""
    land_params = {
        "todo_id": task,
        "worktree": f"/checkout/.claude/worktrees/slice-td{task}",
        "branch": f"slice/{task}",
    }
    for _ in range(pd.LAND_AGAIN_CAP):
        _slice_job(
            store,
            task,
            "land_slice",
            "failed",
            params=land_params,
            meta={"attempt_free": "infra-land"},
        )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (0, 1)
    asks = [c for c in _children(store, root) if c != task]
    assert len(asks) == 1
    return asks[0]


def _answer_ask(store: Store, ask: int, text: str) -> None:
    """Post ``answer: <text>`` on the ask and close it done."""
    store.chunks.insert_chunks(
        ask,
        [
            ChunkInsert(
                ord=1, text=f"answer: {text}", meta={"chunk_kind": "todo_comment"}
            )
        ],
    )
    store.add_tag(
        ask, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )


def test_reland_ask_land_again_relands(store: Store) -> None:
    """gr570187: the re-land ask answered ``land again (no builder)``
    with the brief untouched re-lands the old branch — the failed job's
    own params, no builder."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _slice_job(store, task, "run_slice", "succeeded")
    ask = _reland_ask(store, task, mint.root_id)
    _answer_ask(store, ask, "land again (no builder)")
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)
    meta = _jobs_under(store, task)[-1][1]
    assert meta["job_type"] == "land_slice"


def test_reland_ask_rewrite_mints_a_builder(store: Store) -> None:
    """gr570187: a re-land ask answered ``rewrite the brief`` mints a
    builder on the task's new brief — never a re-land of the old branch."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _slice_job(store, task, "run_slice", "succeeded")
    ask = _reland_ask(store, task, mint.root_id)
    store.chunks.insert_chunks(
        task,
        [
            ChunkInsert(
                ord=1,
                text="brief: retry the gate with a smaller test selection",
                meta={"chunk_kind": "todo_comment"},
            )
        ],
    )
    _answer_ask(store, ask, "rewrite the brief")
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)
    meta = _jobs_under(store, task)[-1][1]
    assert meta["job_type"] == "run_slice"
    assert meta["params"]["todo_id"] == task


def test_reland_ask_land_again_with_a_rewritten_brief_builds(
    store: Store,
) -> None:
    """gr570187: even a ``land again`` answer yields to a changed brief —
    a ``brief:`` comment posted on the task after the ask means the next
    run is a build on the new brief, not a re-land."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _slice_job(store, task, "run_slice", "succeeded")
    ask = _reland_ask(store, task, mint.root_id)
    store.chunks.insert_chunks(
        task,
        [
            ChunkInsert(
                ord=1,
                text="brief: actually, fix the flaky gate first",
                meta={"chunk_kind": "todo_comment"},
            )
        ],
    )
    _answer_ask(store, ask, "land again (no builder)")
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)
    assert _jobs_under(store, task)[-1][1]["job_type"] == "run_slice"


def test_gate_kill_capped_land_goes_to_the_ask(store: Store) -> None:
    """gr570187: a land that failed past its own gate-kill retries
    (``meta.land_again_cap`` — the exit-137 cap, where the branch's own
    memory may be at fault) burns no attempt and does NOT auto re-land:
    the tick files the re-land ask directly."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _slice_job(store, task, "run_slice", "succeeded")
    _slice_job(
        store,
        task,
        "land_slice",
        "failed",
        meta={"attempt_free": "infra-land", "land_again_cap": True},
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (0, 1)
    (ask,) = [c for c in _children(store, mint.root_id) if c != task]
    ask_ref = store.get_ref(kind="todo", id=ask)
    assert ask_ref is not None
    assert "land failed outside the diff" in str(ask_ref.title)
    # The capped-137 ask does not clear the branch: exit 137 is also the
    # container's mem_limit — land_slice's blocked-handoff wording.
    body = _body_text(store, ask)
    assert "the docker VM's memory or the branch's own memory use" in body
    assert "not at fault" not in body
    # Answering "land again" re-arms the mark and the tick re-lands.
    _answer_ask(store, ask, "land again (no builder)")
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)
    assert _jobs_under(store, task)[-1][1]["job_type"] == "land_slice"


def test_retry_climbs_the_lane_model_ladder(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td559457 (Reto 2026-11-03): a failed slice is retried one rung up the
    lane's model ladder before the cap asks. First attempt: no pin (the
    lane's default); attempt 1: the rung above the default; a node whose
    op already pins a model keeps it. Pinned to the Claude lane alone so
    the seed table's vendor lanes (codex on since td562568) do not add
    rungs below it — the cross-lane case is the test after this one."""
    sub = seed_lane("claude-sub")
    use_lanes(monkeypatch, sub)
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    res = pd.run_pass(store)
    assert res.dispatched == 1
    first = _jobs_under(store, task)[-1][1]["params"]
    assert "model" not in first
    _set_job_status(store, _jobs_under(store, task)[-1][0], "failed")
    store.add_tag(task, Tag.open("child-failed:1"), set_by="system")

    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)
    retry = _jobs_under(store, task)[-1][1]["params"]
    lane = seed_lane("claude-sub")
    above_default = lane.models[lane.models.index(lane.default_model or "") + 1]
    assert retry["model"] == above_default
    assert retry["lane"] == "claude-sub"
    assert any("climbs to" in n and "rung" in n for n in res.notes)

    # A pinned op is the triage's pick: the ladder leaves it alone.
    pinned_mint = _pinned_plan(store, "claude-haiku-4-5-20251001")
    pinned = pinned_mint.ids["task"]
    _failed_job(store, pinned, "first failure")
    store.add_tag(pinned, Tag.open("child-failed:1"), set_by="system")
    res = pd.run_pass(store)
    assert res.dispatched == 1
    assert _jobs_under(store, pinned)[-1][1]["params"]["model"] == (
        "claude-haiku-4-5-20251001"
    )


def test_retry_climbs_across_lanes(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td561544: with a free vendor lane enabled the ladder is cross-lane —
    the vendor lane's models from its default up, then the Claude lane's.
    Each retry climbs one rung above where the last attempt ran."""
    free = L.Lane(
        name="free",
        adapter="devin_tree",
        credential_pool="free",
        cap=1,
        task_classes=frozenset({"code"}),
        vendor="cognition",
        models=("f-low", "f-high", "f-max"),
        default_model="f-high",
    )
    bare = L.Lane(
        name="bare",
        adapter="codex_exec",
        credential_pool="bare",
        cap=1,
        task_classes=frozenset({"code"}),
        vendor="openai",
    )
    sub = seed_lane("claude-sub")
    use_lanes(monkeypatch, free, bare, sub)
    assert [(r.lane, r.model) for r in L.ladder("code", store=store)] == [
        ("free", "f-high"),
        ("free", "f-max"),
        ("bare", None),
        ("claude-sub", "claude-sonnet-5"),
        ("claude-sub", "claude-opus-4-8"),
    ]

    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        m.node("task", "The task", {"op": "run_slice", "retries": 6}, body="Do it.")

    mint = T._run(store, None, build)
    task = mint.ids["task"]
    expected = [
        ("free", "f-max"),
        ("bare", None),
        ("claude-sub", "claude-sonnet-5"),
        ("claude-sub", "claude-opus-4-8"),
        ("claude-sub", "claude-opus-4-8"),  # floored at the top
    ]
    # The first attempt ran on the ladder's foot (as run_slice would record).
    _failed_job(store, task, "first failure", lane="free", model="f-high")
    for lane, model in expected:
        store.add_tag(task, Tag.open("child-failed:1"), set_by="system")
        res = pd.run_pass(store)
        assert (res.dispatched, res.asks) == (1, 0)
        job_id, meta = _jobs_under(store, task)[-1]
        assert meta["params"].get("lane") == lane
        assert meta["params"].get("model") == model
        # run_slice would stamp where it ran; mirror that, then fail it.
        store.stamp_ref_meta(
            job_id, {"lane": lane, "model": model or "account-default"}
        )
        _set_job_status(store, job_id, "failed")


def _pinned_plan(store: Store, model: str) -> T.PlanMint:
    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        m.node(
            "task",
            "The pinned task",
            {"op": {"type": "run_slice", "params": {"model": model}}},
            body="Do it.",
        )

    return T._run(store, None, build)


def test_retry_cap_climbs_the_ladder_then_rescues(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td584381 S1a: the retry cap is the ladder's length — a failed
    build climbs one rung per attempt with no ask, and at exhaustion the
    node enters rescue (``rescue:builder`` + the signature gripe), minting
    no ask-user."""
    use_lanes(monkeypatch)  # the seed table: a four-rung code ladder
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    rungs = [
        ("devin", "swe-2-high"),
        ("devin", "swe-2-max"),
        ("claude-sub", "claude-sonnet-5"),
        ("claude-sub", "claude-opus-4-8"),
    ]
    assert [(r.lane, r.model) for r in L.ladder("code", store=store)] == rungs
    # Attempt 1 ran the ladder's foot (as run_slice stamps meta.lane/model).
    _failed_job(store, task, "boom 1", lane="devin", model="swe-2-high")
    store.add_tag(task, Tag.open("child-failed:1"), set_by="system")
    for lane, model in rungs[1:]:
        res = pd.run_pass(store)
        # Unexhausted ladder: a climb, never an ask — not even at attempt 2.
        assert (res.dispatched, res.asks, res.rescued) == (1, 0, 0)
        assert "child-failed:1" not in _open_tags(store, task)
        job_id, meta = _jobs_under(store, task)[-1]
        assert (meta["params"]["lane"], meta["params"]["model"]) == (lane, model)
        # run_slice stamps where it ran at submit; mirror that, then fail.
        store.stamp_ref_meta(job_id, {"lane": lane, "model": model})
        _set_job_status(store, job_id, "failed")
        store.chunks.insert_chunks(
            job_id,
            [
                ChunkInsert(
                    ord=1, text="log tail: exit 1", meta={"chunk_kind": "job_event"}
                )
            ],
        )
    # Every rung spent: the node enters rescue — tag, signature gripe,
    # related-to link — and no ask is minted.
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (0, 0, 1)
    assert "rescue:builder" in _open_tags(store, task)
    assert [c for c in _children(store, mint.root_id) if "ask" in _meta(store, c)] == []
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT dst_ref_id FROM links WHERE src_ref_id = %s "
            "AND relation = 'related-to'",
            (task,),
        ).fetchall()
    assert len(rows) == 1
    gripe = int(rows[0][0])
    assert any(v.startswith("rescue-sig:") for v in _open_tags(store, gripe))
    # In rescue the tick leaves the node alone: no dispatch, no ask, no
    # second rescue write.
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (0, 0, 0)


def test_human_only_hit_files_the_ask_at_once(store: Store) -> None:
    """td584381 S1a: a failed attempt stamped ``human_only_hit`` skips
    the ladder and rescue — the ask names the paths a human must edit."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(
        store,
        task,
        "run_slice: session touched deny_write_paths: `deploy/inventory/x.yml`",
        lane="devin",
        model="swe-2-high",
        meta={"human_only_hit": ["deploy/inventory/x.yml"]},
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (0, 1, 0)
    (ask,) = [c for c in _children(store, mint.root_id) if "ask" in _meta(store, c)]
    meta = _meta(store, ask)["ask"]
    assert meta["kind"] == "ruling"
    assert meta["blocks"] == f"td{task}"
    assert "deploy/inventory/x.yml" in meta["context"]
    body = _body_text(store, ask)
    assert "a human must edit" in body
    assert "deploy/inventory/x.yml" in body
    assert {"ask-user", "showstopper", f"plan:{mint.root_id}"} <= _open_tags(store, ask)
    assert "rescue:builder" not in _open_tags(store, task)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
            "AND relation = 'blocked-by'",
            (task, ask),
        ).fetchone()
    assert row is not None and row[0] == 1

    # The open ask blocks the task: no second ask, no dispatch.
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (0, 0, 0)


def test_factory_deny_failure_climbs_then_rescues(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td584381 S1a: a branch touching a FACTORY-deny (engine) path only
    is an ordinary failure — no human stamp — so it climbs the ladder
    like any failure and enters rescue at exhaustion."""
    use_lanes(monkeypatch)

    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        m.node("task", "The task", {"op": "run_slice", "retries": 2}, body="Do it.")

    mint = T._run(store, None, build)
    task = mint.ids["task"]
    _failed_job(
        store,
        task,
        "run_slice: session touched deny_write_paths: `scripts/ship`",
        lane="devin",
        model="swe-2-high",
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (1, 0, 0)
    job_id, meta = _jobs_under(store, task)[-1]
    assert meta["params"]["lane"] == "devin" and meta["params"]["model"] == "swe-2-max"
    store.stamp_ref_meta(job_id, {"lane": "devin", "model": "swe-2-max"})
    _set_job_status(store, job_id, "failed")
    store.chunks.insert_chunks(
        job_id,
        [ChunkInsert(ord=1, text="deny hit", meta={"chunk_kind": "job_event"})],
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (0, 0, 1)
    assert "rescue:builder" in _open_tags(store, task)
    assert [c for c in _children(store, mint.root_id) if "ask" in _meta(store, c)] == []


def test_no_enabled_lane_still_asks(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td584381 S1a: with no enabled lane fit for the task class the cap
    stays :data:`DEFAULT_RETRIES` and mints the ask naming the gap —
    rescue needs a lane to diagnose on."""
    dead = L.Lane(
        name="devin",
        adapter="devin_tree",
        credential_pool="free-promo",
        cap=3,
        task_classes=frozenset({"code", "review", "validate"}),
        enabled=False,
        vendor="cognition",
        models=("swe-2-medium", "swe-2-high", "swe-2-max"),
        default_model="swe-2-high",
        cost_class=L.COST_FREE,
    )
    use_lanes(monkeypatch, dead)
    assert L.ladder("code", store=store) == ()
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(store, task, "first failure")
    _failed_job(store, task, "second failure")
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (0, 1, 0)
    (ask,) = [c for c in _children(store, mint.root_id) if "ask" in _meta(store, c)]
    meta = _meta(store, ask)["ask"]
    assert "no enabled lane" in meta["context"]
    assert "no enabled lane" in _body_text(store, ask)
    assert "rescue:builder" not in _open_tags(store, task)


def test_no_commit_failure_goes_straight_to_rescue(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td584381 S1a F4: a clean-exit failure that left no commit on the
    branch (``meta.no_commit``) is the LAST attempt — no ladder rung can
    teach a session to write what it chose not to — so the node enters
    rescue after one attempt, its ``moot:`` line in the evidence."""
    use_lanes(monkeypatch)  # the seed table: a four-rung code ladder
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(
        store,
        task,
        "run_slice: session left no commit on slice/td0 ahead of "
        "origin/main (nothing to land)\n--- log tail ---\n"
        "moot: gr123 already fixed on main\n",
        lane="devin",
        model="swe-2-high",
        meta={"no_commit": True},
    )
    res = pd.run_pass(store)
    # One attempt spent the whole road: rescue, never a climb or an ask.
    assert (res.dispatched, res.asks, res.rescued) == (0, 0, 1)
    assert "rescue:builder" in _open_tags(store, task)
    assert [c for c in _children(store, mint.root_id) if "ask" in _meta(store, c)] == []
    # The session's verdict line reaches the evidence on the node.
    assert any(
        "moot: gr123 already fixed on main" in c for c in _todo_comments(store, task)
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (0, 0, 0)


def test_no_commit_lane_gap_ask_carries_the_verdict_line(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td584381 S1a F4: a no-commit failure that goes to the ask instead
    (no enabled lane — rescue needs a lane to diagnose on) carries the
    session's first ``moot:``/``no-op:`` line in the ask body."""
    dead = L.Lane(
        name="devin",
        adapter="devin_tree",
        credential_pool="free-promo",
        cap=3,
        task_classes=frozenset({"code", "review", "validate"}),
        enabled=False,
        vendor="cognition",
        models=("swe-2-medium", "swe-2-high", "swe-2-max"),
        default_model="swe-2-high",
        cost_class=L.COST_FREE,
    )
    use_lanes(monkeypatch, dead)
    assert L.ladder("code", store=store) == ()
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(
        store,
        task,
        "run_slice: session left no commit on slice/td0 ahead of "
        "origin/main (nothing to land)\n--- log tail ---\n"
        "no-op: docs already match the code",
        meta={"no_commit": True},
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (0, 1, 0)
    (ask,) = [c for c in _children(store, mint.root_id) if "ask" in _meta(store, c)]
    assert "no-op: docs already match the code" in _body_text(store, ask)


def test_retry_cap_mints_exactly_one_ask(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td584381 S1a: the end of the road mints ONE ask per node — here
    the road is the no-lane gap (the cap stays ``DEFAULT_RETRIES``);
    closing the ask re-arms the task."""
    dead = L.Lane(
        name="devin",
        adapter="devin_tree",
        credential_pool="free-promo",
        cap=3,
        task_classes=frozenset({"code", "review", "validate"}),
        enabled=False,
        vendor="cognition",
        models=("swe-2-medium", "swe-2-high", "swe-2-max"),
        default_model="swe-2-high",
        cost_class=L.COST_FREE,
    )
    use_lanes(monkeypatch, dead)
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(store, task, "first failure")
    store.add_tag(task, Tag.open("child-failed:1"), set_by="system")

    # One failure (< DEFAULT_RETRIES 2): the tick retries, tag dropped.
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)
    assert "child-failed:1" not in _open_tags(store, task)
    live = _jobs_under(store, task)[-1][0]
    _set_job_status(store, live, "failed")
    store.chunks.insert_chunks(
        live,
        [ChunkInsert(ord=1, text="log tail: exit 1", meta={"chunk_kind": "job_event"})],
    )

    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (0, 1, 0)
    asks = [c for c in _children(store, mint.root_id) if "ask" in _meta(store, c)]
    assert len(asks) == 1
    ask = asks[0]
    meta = _meta(store, ask)["ask"]
    assert meta["kind"] == "ruling"
    assert meta["blocks"] == f"td{task}"
    assert "exit 1" in meta["context"]
    assert {"ask-user", "showstopper", f"plan:{mint.root_id}"} <= _open_tags(store, ask)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
            "AND relation = 'blocked-by'",
            (task, ask),
        ).fetchone()
    assert row is not None and row[0] == 1

    # The open ask blocks the task: no second ask, no dispatch.
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (0, 0)
    assert len([c for c in _children(store, mint.root_id) if c != task]) == 1

    # Answering (closing) the ask re-arms the task.
    _answer_ask(store, ask, "retry with a different lane/model")
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)


def test_model_pinned_node_caps_at_default_retries(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td584381 S1a F2: a ``model`` pin is the triage's pick —
    ``_escalate`` never climbs it, so the ladder is no budget: the cap
    falls back to :data:`DEFAULT_RETRIES` and the second failure enters
    rescue (it used to ride the four-rung ladder to four)."""
    use_lanes(monkeypatch)  # the seed table: a four-rung code ladder
    mint = _pinned_plan(store, "claude-haiku-4-5-20251001")
    task = mint.ids["task"]
    _failed_job(
        store,
        task,
        "first failure",
        lane="claude-sub",
        model="claude-haiku-4-5-20251001",
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (1, 0, 0)
    job_id, meta = _jobs_under(store, task)[-1]
    assert meta["params"]["model"] == "claude-haiku-4-5-20251001"
    store.stamp_ref_meta(job_id, {"lane": "claude-sub"})
    _set_job_status(store, job_id, "failed")
    store.chunks.insert_chunks(
        job_id,
        [ChunkInsert(ord=1, text="log tail: exit 1", meta={"chunk_kind": "job_event"})],
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (0, 0, 1)
    assert "rescue:builder" in _open_tags(store, task)
    assert [c for c in _children(store, mint.root_id) if "ask" in _meta(store, c)] == []


def test_rescue_ask_answer_clears_the_tag(store: Store) -> None:
    """td584381 S1a F1: a node parked in rescue behind its own ruling ask
    is freed by the answer — ``_step_rearm`` drops the ``rescue:<cls>``
    tag in the same step it stamps the re-arm floor; ``meta.rescue.n``
    stays, so a second exhaustion re-enters rescue for the human."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(store, task, "boom")
    # The rescue pass's own ask parked the node (meta.rescue.n >= 1).
    store.add_tag(task, Tag.open("rescue:builder"), set_by="system")
    store.stamp_ref_meta(task, {"rescue": {"n": 1}})
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title="rescue ruling",
            meta={
                "ask": {
                    "kind": "ruling",
                    "options": ["retry with a different lane/model"],
                    "blocks": f"td{task}",
                    "context": "in rescue",
                }
            },
            parent_id=mint.root_id,
            conn=conn,
        )
        ask = int(ref.id)
        store.add_tag(
            ask,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for v in ("ask-user", "showstopper", f"plan:{mint.root_id}"):
            store.add_tag(ask, Tag.open(v), set_by="system", conn=conn)
        store.add_link(
            src_ref_id=task, dst_ref_id=ask, relation="blocked-by", conn=conn
        )
    # The open ask keeps the node excluded: nothing moves.
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (0, 0, 0)
    assert "rescue:builder" in _open_tags(store, task)
    _answer_ask(store, ask, "retry with a different lane/model")
    # The answer IS the clearing: re-armed AND dispatched, tag gone.
    res = pd.run_pass(store)
    assert res.dispatched == 1
    assert "rescue:builder" not in _open_tags(store, task)
    meta = _meta(store, task)
    assert meta["rescue"]["n"] == 1
    assert meta["rearmed_by_ask"] == ask


def test_human_only_key_matches_run_slice() -> None:
    """``plan_dispatch`` restates the job-meta key ``run_slice`` stamps —
    the dependency stays one-way, like ``ATTEMPT_FREE_KEY``."""
    assert pd.HUMAN_ONLY_KEY == run_slice.HUMAN_ONLY_KEY == "human_only_hit"


def test_no_commit_key_matches_run_slice() -> None:
    """``plan_dispatch`` restates the job-meta flag ``run_slice`` stamps
    on a clean-exit no-commit failure — the same one-way rule as
    ``HUMAN_ONLY_KEY`` (td584381 S1a F4)."""
    assert pd.NO_COMMIT_KEY == run_slice.NO_COMMIT_KEY == "no_commit"


def test_host_refusal_jobs_are_no_attempt(store: Store) -> None:
    """gr571395: a launch refused for a host reason (the devin binary
    missing, a dead login, trust seeding failed) is not a build attempt —
    a broken host must never mint a retry-cap ask on an innocent todo."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    until = (datetime.now(UTC) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for _ in range(2):
        _failed_job(
            store,
            task,
            "run_slice: devin preflight: binary is not executable",
            lane="devin",
            meta={
                "host_refusal": {
                    "lane": "devin",
                    "until": until,
                    "why": "devin preflight: binary is not executable",
                }
            },
        )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)


def test_lane_backoffs_marks_only_the_newest_job_per_lane(store: Store) -> None:
    """gr571395: the backoff is derived, never stored — a lane is backed
    off while its NEWEST run_slice job carries a future refusal ``until``;
    a later unmarked job (a probe that launched) clears it."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    until = (datetime.now(UTC) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    past = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _failed_job(
        store,
        task,
        "run_slice: devin preflight: no binary",
        lane="devin",
        meta={"host_refusal": {"lane": "devin", "until": until, "why": "x"}},
    )
    backoffs = L.lane_backoffs(store)
    assert set(backoffs) == {"devin"}
    assert backoffs["devin"] > datetime.now(UTC)
    # A newer job on the lane without the mark clears the backoff.
    _failed_job(store, task, "probe launched and failed", lane="devin")
    assert "devin" not in L.lane_backoffs(store)
    # An expired window clears it too.
    _failed_job(
        store,
        task,
        "run_slice: codex host refused",
        lane="codex",
        meta={"host_refusal": {"lane": "codex", "until": past, "why": "x"}},
    )
    assert "codex" not in L.lane_backoffs(store)


def test_escalate_skips_a_lane_in_backoff(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr571395: the retry ladder skips rungs on lanes a host refusal
    backed off — an escalated retry must not climb onto a lane that just
    refused a launch (it would park, not build)."""
    low = L.Lane(
        name="low",
        adapter="devin_tree",
        credential_pool="low",
        cap=1,
        task_classes=frozenset({"code"}),
        vendor="cognition",
    )
    mid = L.Lane(
        name="mid",
        adapter="codex_exec",
        credential_pool="mid",
        cap=1,
        task_classes=frozenset({"code"}),
        vendor="openai",
    )
    sub = seed_lane("claude-sub")
    use_lanes(monkeypatch, low, mid, sub)
    assert [(r.lane, r.model) for r in L.ladder("code", store=store)] == [
        ("low", None),
        ("mid", None),
        ("claude-sub", "claude-sonnet-5"),
        ("claude-sub", "claude-opus-4-8"),
    ]
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    until = (datetime.now(UTC) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _failed_job(store, task, "real failure on the foot rung", lane="low")
    # Another slice's host refusal backed "mid" off (its newest run_slice
    # job carries the mark): the retry must jump to rung k+2, not park on k+1.
    _slice_job(
        store,
        mint.root_id,
        "run_slice",
        "failed",
        meta={
            "lane": "mid",
            "host_refusal": {"lane": "mid", "until": until, "why": "x"},
        },
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)
    params = _jobs_under(store, task)[-1][1]["params"]
    assert params["lane"] == "claude-sub" and params["model"] == "claude-sonnet-5"


def test_escalate_skips_a_parked_lane(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr588221: a parked lane (``parked_reason`` set — the lane's health,
    not the human ``enabled`` switch) drops out of the ladder entirely,
    so the retry climbs to the next lane like a backed-off one."""
    low = L.Lane(
        name="low",
        adapter="devin_tree",
        credential_pool="low",
        cap=1,
        task_classes=frozenset({"code"}),
        vendor="cognition",
    )
    mid = L.Lane(
        name="mid",
        adapter="codex_exec",
        credential_pool="mid",
        cap=1,
        task_classes=frozenset({"code"}),
        vendor="openai",
        parked_reason="auth:not-logged-in",
    )
    sub = seed_lane("claude-sub")
    use_lanes(monkeypatch, low, mid, sub)
    # The parked lane contributes no rungs at all.
    assert [(r.lane, r.model) for r in L.ladder("code", store=store)] == [
        ("low", None),
        ("claude-sub", "claude-sonnet-5"),
        ("claude-sub", "claude-opus-4-8"),
    ]
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(store, task, "real failure on the foot rung", lane="low")
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)
    params = _jobs_under(store, task)[-1][1]["params"]
    assert params["lane"] == "claude-sub" and params["model"] == "claude-sonnet-5"


def test_every_fit_lane_parked_asks_like_no_lane(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A class whose only fit lane is parked behaves like the
    no-enabled-lane case (gr588221): the cap is DEFAULT_RETRIES and the
    ask names the lane gap — rescue still needs a lane to diagnose on."""
    dead = L.Lane(
        name="devin",
        adapter="devin_tree",
        credential_pool="free-promo",
        cap=3,
        task_classes=frozenset({"code", "review", "validate"}),
        enabled=True,
        vendor="cognition",
        models=("swe-2-medium", "swe-2-high", "swe-2-max"),
        default_model="swe-2-high",
        cost_class=L.COST_FREE,
        parked_reason="outage:timeout",
    )
    use_lanes(monkeypatch, dead)
    assert L.ladder("code", store=store) == ()
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(store, task, "first failure")
    _failed_job(store, task, "second failure")
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (0, 1, 0)
    (ask,) = [c for c in _children(store, mint.root_id) if "ask" in _meta(store, c)]
    assert "no enabled lane" in _meta(store, ask)["ask"]["context"]


def test_escalate_climbs_from_the_last_real_attempt(store: Store) -> None:
    """gr571395: a host-refusal job is no attempt (:func:`pd._no_attempt`),
    so it is no rung to climb from either — the retry goes one rung up
    from the newest job that actually ran, not from the lane that refused."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(store, task, "real failure", lane="devin", model="swe-2-high")
    # The newest job is a codex host refusal whose window already ended
    # (no backoff): if it counted as the previous rung, the retry would
    # climb above codex; ignoring it, the retry takes devin's next rung.
    past = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _failed_job(
        store,
        task,
        "run_slice: codex host refused",
        lane="codex",
        meta={"host_refusal": {"lane": "codex", "until": past, "why": "x"}},
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)
    params = _jobs_under(store, task)[-1][1]["params"]
    assert params["lane"] == "devin" and params["model"] == "swe-2-max"


def test_escalate_never_drops_below_the_last_attempt(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr571395: the retry never takes a rung below the lane the last
    real attempt ran on — a backed-off lane lowers no floor. With the
    next rung's lane also out, the retry takes the next lane ABOVE it;
    with nothing free above it parks on lane-wait (no mint)."""
    low = L.Lane(
        name="low",
        adapter="devin_tree",
        credential_pool="low",
        cap=1,
        task_classes=frozenset({"code"}),
        vendor="cognition",
    )
    mid = L.Lane(
        name="mid",
        adapter="codex_exec",
        credential_pool="mid",
        cap=1,
        task_classes=frozenset({"code"}),
        vendor="openai",
    )
    sub = seed_lane("claude-sub")
    use_lanes(monkeypatch, low, mid, sub)
    until = (datetime.now(UTC) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # The last real attempt ran on mid, which is now backed off: the
    # retry climbs to the next lane above it — claude-sub — never back
    # to low.
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(store, task, "real failure on mid", lane="mid")
    _slice_job(
        store,
        mint.root_id,
        "run_slice",
        "failed",
        meta={
            "lane": "mid",
            "host_refusal": {"lane": "mid", "until": until, "why": "x"},
        },
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)
    params = _jobs_under(store, task)[-1][1]["params"]
    assert params["lane"] == "claude-sub"
    assert params["model"] == "claude-sonnet-5"

    # The last real attempt ran at the top rung and that lane is backed
    # off: nothing free sits above it, so the node parks — no mint, a
    # lane-wait note.
    mint2 = _one_task_plan(store)
    task2 = mint2.ids["task"]
    _failed_job(
        store,
        task2,
        "real failure at the top rung",
        lane="claude-sub",
        model="claude-opus-4-8",
    )
    _slice_job(
        store,
        mint2.root_id,
        "run_slice",
        "failed",
        meta={
            "lane": "claude-sub",
            "host_refusal": {"lane": "claude-sub", "until": until, "why": "x"},
        },
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (0, 0)
    assert any("lane-wait" in n and str(task2) in n for n in res.notes)


def test_zero_attempts_never_mints_the_cap_ask(store: Store) -> None:
    """gr571395: ``meta.retries: 0`` on a task whose only ended job is a
    host refusal must not mint the cap ask — ``0 >= 0`` parked a task
    nothing was ever tried on. With no attempt burned the tick requeues."""

    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        m.node("task", "The task", {"op": "run_slice", "retries": 0}, body="Do it.")

    mint = T._run(store, None, build)
    task = mint.ids["task"]
    until = (datetime.now(UTC) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _failed_job(
        store,
        task,
        "run_slice: devin preflight: no binary",
        lane="devin",
        meta={"host_refusal": {"lane": "devin", "until": until, "why": "x"}},
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)


def test_a_cancelled_job_requeues_its_node_free(store: Store) -> None:
    """gr587772: an operator's cancel is a free re-queue — the next pass
    mints a fresh ``run_slice`` under the node, burns no attempt, and two
    consecutive cancels still do not reach the cap."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _slice_job(store, task, "run_slice", "cancelled")
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)
    assert any(
        f"td{task}: newest job:" in n
        and "was cancelled; re-queued, not an attempt" in n
        for n in res.notes
    )
    job_id, meta = _jobs_under(store, task)[-1]
    assert meta["job_type"] == "run_slice" and meta["params"]["todo_id"] == task

    # A second consecutive cancel burns no attempt either: no cap, no ask,
    # no rescue — the node re-queues again.
    _set_job_status(store, job_id, "cancelled")
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (1, 0, 0)
    plan = pd._load(store)
    assert pd._attempts(plan, plan.nodes[task]) == 0


def test_a_cancelled_job_keeps_the_stop_guards(store: Store) -> None:
    """gr587772: the re-queue fires only once every ordinary guard has
    passed — a claimed, held, in-rescue, blocked or paused node stays
    stopped; stopping a node stays a tag's job, never the cancel's."""

    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        for key in ("claimed", "held", "rescued", "paused"):
            m.node(key, f"Task {key}", {"op": "run_slice"})
        m.node("blocker", "Blocker", {"op": "run_slice"})
        m.node("blocked", "Blocked task", {"op": "run_slice"}, blocked_by=("blocker",))

    mint = T._run(store, None, build)
    ids = mint.ids
    held_keys = ("claimed", "held", "rescued", "blocked", "paused")
    for key in held_keys:
        _slice_job(store, ids[key], "run_slice", "cancelled")
    store.add_tag(
        ids["claimed"], Tag.open("claimed-by:lane-claude-sub"), set_by="system"
    )
    store.add_tag(ids["held"], Tag.open("waiting-for:human"), set_by="system")
    store.add_tag(ids["rescued"], Tag.open("rescue:builder"), set_by="system")
    store.add_tag(
        ids["paused"],
        Tag.closed("STATUS", "paused"),
        set_by="system",
        replace_prefix=True,
    )

    res = pd.run_pass(store)
    # Only the blocker itself mints; every guarded node keeps its
    # cancelled job as the newest and gets nothing fresh.
    assert res.dispatched == 1
    for key in held_keys:
        assert len(_jobs_under(store, ids[key])) == 1
        assert not any(
            f"td{ids[key]}: newest job:" in n and "re-queued" in n for n in res.notes
        )


def test_a_cancelled_job_at_the_cap_takes_the_cap_path(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr587772: a cancel buys no retry budget — a node already at its
    retry cap (the failed attempt below the cancelled job) is not
    redispatched; it enters the cap path like a failed newest job:
    rescue, not a silent freeze."""
    use_lanes(monkeypatch)  # the seed table: a four-rung code ladder

    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        m.node("task", "The task", {"op": "run_slice", "retries": 1}, body="Do it.")

    mint = T._run(store, None, build)
    task = mint.ids["task"]
    _failed_job(store, task, "boom", lane="devin", model="swe-2-high")
    _slice_job(store, task, "run_slice", "cancelled")
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (0, 0, 1)
    assert "rescue:builder" in _open_tags(store, task)
    assert [c for c in _children(store, mint.root_id) if "ask" in _meta(store, c)] == []


def test_a_cancelled_review_requeues_through_the_review_lane(store: Store) -> None:
    """gr587772: a cancelled reviewer job re-queues like any other — the
    retry is a fresh review pinned to the lane ``_review_lane`` picks (a
    vendor that did not build), not a free-for-all dispatch."""
    mint = _review_plan(store)
    build, review = mint.ids["build"], mint.ids["review"]
    _job(store, build, lane="claude-sub")
    _todo_comment(store, build, "landed: abc1234 — the fix")
    _mark_done(store, build)
    _job(store, review, status="cancelled")
    res = pd.run_pass(store)
    assert res.dispatched == 1
    assert any(
        f"td{review}: newest job:" in n and "re-queued, not an attempt" in n
        for n in res.notes
    )
    jobs = _jobs_under(store, review)
    assert len(jobs) == 2
    assert jobs[-1][1]["params"]["todo_id"] == review
    # The builder ran on claude-sub; the cross-vendor pick is devin (as
    # test_review_miss_retry_is_not_escalated pins under this table).
    assert jobs[-1][1]["params"]["lane"] == "devin"


def test_a_cancelled_land_is_never_re_run_by_the_tick(store: Store) -> None:
    """gr587772: a land the operator killed mid-ship is the ONE cancel the
    tick leaves alone — whether the merge reached main is ambiguous and a
    blind re-run could double-land, so the old "not redispatched" note
    stays and the rescue tick's ``cancelled`` class owns the node."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _slice_job(store, task, "run_slice", "succeeded")
    _slice_job(store, task, "land_slice", "cancelled")
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (0, 0, 0)
    assert any(
        f"td{task}: newest job:" in n and "was cancelled; not redispatched" in n
        for n in res.notes
    )
    # Idempotent: the next pass still leaves it for the rescue tick.
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (0, 0, 0)


@pytest.mark.parametrize(
    ("op", "job_type"),
    [
        ("release_deploy", "release_deploy"),
        ("release_cut", "release_cut"),
        ("release_confirm", "release_confirm"),
        ("pr_action", "pr_action"),
        ("dep_check", "dep_check"),
        ("ci_red_digest", "ci_red_digest"),
        # An op the registry does not know defaults to the safe side.
        ("run_slice", "never_registered_op"),
    ],
)
def test_a_cancelled_side_effecting_op_keeps_the_old_skip(
    store: Store, op: str, job_type: str
) -> None:
    """gr587772 review S1: the free re-queue is allowlisted by job_type —
    only ops confirmed read-only/idempotent may re-run blind. A cancelled
    ``release_*`` step may already have fired, a ``pr_action`` may already
    have posted, ``dep_check`` and ``ci_red_digest`` write outside the
    node, and an unknown op defaults to the safe side: every one keeps
    the "not redispatched" skip for the rescue tick's ``cancelled``
    class."""

    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        m.node("task", "The task", {"op": op})

    mint = T._run(store, None, build)
    task = mint.ids["task"]
    _slice_job(store, task, job_type, "cancelled")
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (0, 0, 0)
    assert len(_jobs_under(store, task)) == 1
    assert any(
        f"td{task}: newest job:" in n and "was cancelled; not redispatched" in n
        for n in res.notes
    )


def test_a_cancelled_allowlisted_op_requeues(store: Store) -> None:
    """gr587772 review S1: ``ci_verdict_poll`` is on the re-queue
    allowlist — one unauthenticated GitHub GET plus a verdict row keyed
    per sha (:func:`precis.plans.release.ci_verdict_rows` replaces, never
    appends a second row), so a re-run after a cancel re-reads and
    re-writes the same state."""

    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        m.node("poll", "Poll CI", {"op": "ci_verdict_poll"})

    mint = T._run(store, None, build)
    poll = mint.ids["poll"]
    _slice_job(store, poll, "ci_verdict_poll", "cancelled")
    res = pd.run_pass(store)
    assert res.dispatched == 1
    assert any(
        f"td{poll}: newest job:" in n and "re-queued, not an attempt" in n
        for n in res.notes
    )
    _job_id, meta = _jobs_under(store, poll)[-1]
    assert meta["job_type"] == "ci_verdict_poll"
    assert meta["executor"] == "job_inproc"


def test_a_cancelled_review_on_full_lanes_logs_no_requeue_note(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr587772 review nit: the 're-queued' note claims a fresh job was
    minted — a cancelled reviewer whose every fit lane outside the
    builder's vendor is at cap parks on the lane instead, and must log
    no re-queue note that pass."""
    use_lanes(monkeypatch)  # the real multi-lane seed table
    mint = _review_plan(store)
    build, review = mint.ids["build"], mint.ids["review"]
    _job(store, build, lane="claude-sub")
    _todo_comment(store, build, "landed: abc1234 — the fix")
    _mark_done(store, build)
    _job(store, review, status="cancelled")
    # Fill every review-fit lane outside the builder's vendor: devin
    # (cap 3) and codex (cap 1). Live = queued, meta.lane set.
    for _ in range(3):
        _job(store, mint.root_id, status="queued", lane="devin")
    _job(store, mint.root_id, status="queued", lane="codex")

    res = pd.run_pass(store)
    assert res.dispatched == 0
    assert any(
        f"td{review}: review not dispatched" in n and "at its cap" in n
        for n in res.notes
    )
    assert not any("re-queued" in n for n in res.notes)


def test_answered_ask_rearms_despite_jobs_run_in_between(store: Store) -> None:
    """gr561075: jobs hand-run between a cap ask and its answer must not keep
    the node capped (td548989 got a second ask the tick after its first was
    closed with ``retry``)."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    # A human-only stamp files the ask at the first failure (td584381
    # S1a: an ordinary cap enters rescue, it no longer asks).
    _failed_job(
        store,
        task,
        "touched human-only",
        meta={"human_only_hit": ["deploy/inventory/x.yml"]},
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (0, 1)
    (ask,) = [c for c in _children(store, mint.root_id) if c != task]

    # Two hand-submitted lands fail while the ask is open.
    _failed_job(store, task, "hand-run land red")
    newest = _failed_job(store, task, "hand-run land red again")
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (0, 0)

    # Closing the ask resets the budget: a dispatch, not a second ask.
    store.add_tag(
        ask, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)
    meta = _meta(store, task)
    assert meta["rearmed_by_ask"] == ask
    assert meta["rearmed_after_job"] == newest
    assert len([c for c in _children(store, mint.root_id) if c != task]) == 1


def test_unknown_loop_body_closes_the_loop_with_a_comment(store: Store) -> None:
    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        m.node(
            "L", "Loop", {"loop": {"max": 2, "until": FUTURE, "body": "no-such-body"}}
        )

    mint = T._run(store, None, build)
    loop = mint.ids["L"]
    res = pd.run_pass(store)
    assert (res.loops_minted, res.loops_closed) == (0, 1)
    assert _status(store, loop) == "done"
    comments = [
        c.text
        for c in store.chunks.list_chunks_for_ref(loop)
        if c.chunk_kind == "todo_comment"
    ]
    assert any("no-such-body" in c and "BODIES" in c for c in comments)
    assert _children(store, loop) == []


def test_release_fix_body_shape(store: Store) -> None:
    mint = T.TEMPLATES["release-pass"].mint(store, None)
    loop = mint.ids["loop"]
    body = T.mint_body(store, loop, "release-fix", iteration=2)
    assert set(body.ids) == {"digest", "fix", "recut", "poll"}
    assert _meta(store, body.ids["digest"])["op"] == "ci_red_digest"
    assert _meta(store, body.ids["recut"])["op"] == "release_cut"
    assert _meta(store, body.ids["poll"])["op"] == "ci_verdict_poll"
    assert body.root_id == loop
    for todo_id in body.ids.values():
        assert f"plan:{mint.root_id}" in _open_tags(store, todo_id)
        ref = store.get_ref(kind="todo", id=todo_id)
        assert ref is not None and ref.parent_id == loop
        assert "iteration 2" in str(ref.title)
    assert "brief:" in _body_text(store, body.ids["fix"])


# ── the validated non-repro rerun (gr588028) ──────────────────────

_R_SHA = "c0ffee" + "1" * 34
_R_RNG = f"{'9' * 12}..{_R_SHA[:12]}"
_R_RUN = "31"


def _parked_release_fix(
    store: Store, *, host: str | None = None
) -> tuple[T.PlanMint, dict[str, int]]:
    """The state ``run_slice.poll`` leaves behind a ``validated:``
    non-repro fix session (gr588028): the cut recorded, the newest
    verdict red with a run id, the digest done, the fix node open on
    ``waiting-for:ci-rerun`` behind its attempt-free succeeded job."""
    mint = T.mint_release_pass(store)
    R.record_cut(store, mint.ids["cut"], "r7", _R_SHA, _R_RNG, repo="o/r", host=host)
    R.record_ci_verdict(store, _R_SHA, _R_RNG, "red", _R_RUN)
    ids = T.mint_body(store, mint.ids["loop"], "release-fix", iteration=1).ids
    for todo_id in (mint.ids["cut"], mint.ids["poll"], ids["digest"]):
        store.add_tag(
            todo_id,
            Tag.closed("STATUS", "done"),
            set_by="system",
            replace_prefix=True,
        )
    _slice_job(store, ids["digest"], "ci_red_digest", "succeeded")
    _slice_job(
        store,
        ids["fix"],
        "run_slice",
        "succeeded",
        meta={
            pd.VALIDATED_NOREPRO_KEY: {"line": "validated: no repro"},
            pd.ATTEMPT_FREE_KEY: "validated-norepro",
        },
    )
    store.add_tag(ids["fix"], Tag.open(pd.NOREPRO_WAIT_TAG), set_by="system")
    return mint, ids


def _rerun_asks(store: Store, root: int) -> list[int]:
    """The stall path's asks: children of ``root`` tagged ``showstopper``
    (the standing approval ask is ``ask-user`` only)."""
    return [c for c in _children(store, root) if "showstopper" in _open_tags(store, c)]


def _comment_texts(store: Store, todo_id: int) -> list[str]:
    return [
        str(c.text)
        for c in store.chunks.list_chunks_for_ref(todo_id)
        if c.chunk_kind == "todo_comment"
    ]


def _job_ids(store: Store, todo_id: int, job_type: str) -> list[int]:
    return [j for j, m in _jobs_under(store, todo_id) if m.get("job_type") == job_type]


def _succeed_newest(store: Store, todo_id: int, status: str = "succeeded") -> None:
    _set_job_status(store, _jobs_under(store, todo_id)[-1][0], status)


def _succeed_rerun(store: Store, todo_id: int, run_id: str = _R_RUN) -> None:
    """End the newest job succeeded carrying the stamp a real
    ``ci_rerun`` writes at settle (``meta.ci_rerun`` — the run id and
    the pre-rerun attempt baseline the script printed)."""
    job_id = _jobs_under(store, todo_id)[-1][0]
    store.stamp_ref_meta(
        job_id,
        {"ci_rerun": {"run_id": run_id, "rerun_attempt_before": 1}},
    )
    _set_job_status(store, job_id, "succeeded")


def test_parked_fix_mints_the_ci_rerun_once_per_run_id(store: Store) -> None:
    """gr588028 (r248): mint phase — one ``ci_rerun`` job on the cut's
    newest red run, pinned to the cut host, idem ``ci_rerun:<run id>``,
    attempt-free; the node stays parked and the rest of the iteration
    is untouched."""
    mint, it = _parked_release_fix(store, host="node-a")
    pd.run_pass(store)
    job_id, meta = _jobs_under(store, it["fix"])[-1]
    assert meta["job_type"] == "ci_rerun"
    assert meta["executor"] == "ssh_node"
    assert meta["params"] == {
        "todo_id": it["fix"],
        "target_node": "node-a",
    }
    assert meta["idem_key"] == f"ci_rerun:{_R_RUN}"
    assert meta[pd.ATTEMPT_FREE_KEY] == "ci-rerun"
    assert _status(store, it["fix"]) == "open"
    assert pd.NOREPRO_WAIT_TAG in _open_tags(store, it["fix"])
    assert _jobs_under(store, it["recut"]) == []
    assert _jobs_under(store, it["poll"]) == []
    assert _rerun_asks(store, mint.root_id) == []
    # A second pass while the rerun is in flight mints no second job.
    pd.run_pass(store)
    assert _job_ids(store, it["fix"], "ci_rerun") == [job_id]


def test_accepted_rerun_mints_the_verdict_poll(store: Store) -> None:
    """A succeeded ``ci_rerun`` (the rerun request was accepted) mints
    one ``ci_verdict_poll`` carrying ``rerun_of`` — the plan's own
    verdict reader, no second mechanism."""
    _mint, it = _parked_release_fix(store)
    pd.run_pass(store)
    _succeed_rerun(store, it["fix"])
    pd.run_pass(store)
    _job_id, meta = _jobs_under(store, it["fix"])[-1]
    assert meta["job_type"] == "ci_verdict_poll"
    assert meta["executor"] == "job_inproc"
    assert meta["params"] == {"todo_id": it["fix"]}
    assert meta["rerun_of"] == _R_RUN
    assert meta["rerun_attempt_before"] == 1
    assert meta[pd.ATTEMPT_FREE_KEY] == "ci-rerun"
    assert pd.NOREPRO_WAIT_TAG in _open_tags(store, it["fix"])


def test_green_rerun_closes_the_fix_and_the_recut_runs(store: Store) -> None:
    """The rerun's poll recording green closes the fix moot-style
    (``validated: flake`` comment, no attempt, no ask) — then the
    loop's own re-cut node re-reads the run: nothing re-mints it."""
    mint, it = _parked_release_fix(store)
    pd.run_pass(store)
    _succeed_rerun(store, it["fix"])
    pd.run_pass(store)
    R.record_ci_verdict(store, _R_SHA, _R_RNG, "green", _R_RUN)
    _succeed_newest(store, it["fix"])
    pd.run_pass(store)
    assert _status(store, it["fix"]) == "done"
    assert pd.NOREPRO_WAIT_TAG not in _open_tags(store, it["fix"])
    assert any(
        f"validated: flake — rerun of run {_R_RUN} green" in c
        for c in _comment_texts(store, it["fix"])
    )
    assert _rerun_asks(store, mint.root_id) == []
    pd.run_pass(store)
    _job_id, meta = _jobs_under(store, it["recut"])[-1]
    assert meta["job_type"] == "release_cut"


def test_red_rerun_stalls_once_and_rearms_the_digest(store: Store) -> None:
    """The rerun's verdict still red: the failure is real — one stall
    comment, one deduped root ask, the wait tag dropped and the digest
    REALLY re-armed: the ``rearmed_after_job`` floor keeps the phases
    backstop from re-closing it on the old succeeded job, a SECOND
    ``ci_red_digest`` job mints, and the fix node dispatches only after
    that job's SECOND ``brief:`` lands. Never a second rerun."""
    mint, it = _parked_release_fix(store)
    # The first digest's brief, as the real ci_red_digest posted it.
    run_slice._append_comment(store, it["fix"], "brief: release r7 is red")
    pd.run_pass(store)
    _succeed_rerun(store, it["fix"])
    pd.run_pass(store)
    _succeed_newest(store, it["fix"])  # the verdict poll; the run still red
    pd.run_pass(store)
    assert _status(store, it["fix"]) == "open"
    assert pd.NOREPRO_WAIT_TAG not in _open_tags(store, it["fix"])
    assert any(
        "rerun path stalled" in c and "still red" in c
        for c in _comment_texts(store, it["fix"])
    )
    (ask,) = _rerun_asks(store, mint.root_id)
    ask_meta = _meta(store, ask)["ask"]
    # The ask only RECORDS the stall: 'acknowledged' is the one option,
    # it blocks nothing and no blocked-by edge parks the plan on it.
    assert ask_meta["options"] == ["acknowledged"]
    assert ask_meta["blocks"] == "nothing"
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM links WHERE src_ref_id = %s "
            "AND dst_ref_id = %s AND relation = 'blocked-by'",
            (mint.root_id, ask),
        ).fetchone()
    assert row is not None and row[0] == 0
    assert _status(store, it["digest"]) == "open"  # re-armed
    digests = _job_ids(store, it["digest"], "ci_red_digest")
    # The SECOND ci_red_digest mints in the stall pass's own dispatch;
    # the floor sits on the fixture's original (old, succeeded) job.
    assert len(digests) == 2
    assert _meta(store, it["digest"])["rearmed_after_job"] == digests[0]
    # Still open next pass — the backstop can't re-close it — and the
    # fix stays parked on its blocker: no new run_slice while the
    # digest is open.
    pd.run_pass(store)
    assert _status(store, it["digest"]) == "open"
    assert len(_job_ids(store, it["fix"], "run_slice")) == 1
    # The digest job completes like the real one: the second brief:
    # lands on the fix node, then the job succeeds and the tick closes
    # the digest — only then does the fix node dispatch on the new brief.
    run_slice._append_comment(
        store, it["fix"], "brief: release r7 is still red — second digest"
    )
    _set_job_status(store, digests[-1], "succeeded")
    res = pd.run_pass(store)
    assert _status(store, it["digest"]) == "done"
    assert len(_job_ids(store, it["fix"], "run_slice")) == 2, res.notes
    briefs = [c for c in _comment_texts(store, it["fix"]) if c.startswith("brief:")]
    assert len(briefs) == 2
    # Deduped: the next pass files no second ask and never re-mints.
    pd.run_pass(store)
    assert _rerun_asks(store, mint.root_id) == [ask]
    assert len(_job_ids(store, it["fix"], "ci_rerun")) == 1


def test_failed_rerun_stalls_once_and_never_remints(store: Store) -> None:
    """A refused rerun (403, run gone) fails the ``ci_rerun`` job: one
    stall comment + one deduped root ask, the tag dropped — and no
    pass ever mints a second rerun for the run."""
    mint, it = _parked_release_fix(store)
    pd.run_pass(store)
    _succeed_newest(store, it["fix"], "failed")
    pd.run_pass(store)
    assert _status(store, it["fix"]) == "open"
    assert pd.NOREPRO_WAIT_TAG not in _open_tags(store, it["fix"])
    assert any(
        "rerun path stalled" in c and "ci_rerun job failed" in c
        for c in _comment_texts(store, it["fix"])
    )
    (ask,) = _rerun_asks(store, mint.root_id)
    pd.run_pass(store)
    assert _rerun_asks(store, mint.root_id) == [ask]
    assert len(_job_ids(store, it["fix"], "ci_rerun")) == 1


def test_a_fired_run_id_unparks_without_a_second_rerun(store: Store) -> None:
    """Once-per-run-id on the mint side: parked on a run whose
    ``ci_rerun:<id>`` job exists anywhere, the node drops the tag back
    to the ordinary path — no mint, no ask, no close."""
    mint, it = _parked_release_fix(store)
    _slice_job(
        store,
        it["digest"],
        "ci_rerun",
        "succeeded",
        meta={"idem_key": f"ci_rerun:{_R_RUN}"},
    )
    pd.run_pass(store)
    assert pd.NOREPRO_WAIT_TAG not in _open_tags(store, it["fix"])
    assert _status(store, it["fix"]) == "open"
    assert _job_ids(store, it["fix"], "ci_rerun") == []
    assert _rerun_asks(store, mint.root_id) == []


def test_an_already_green_verdict_closes_without_a_rerun(store: Store) -> None:
    """Never rerun a run that is not red: the verdict turned green
    before the tick looked (a hand rerun) — the fix closes on the
    flake comment and no ``ci_rerun`` mints."""
    mint, it = _parked_release_fix(store)
    R.record_ci_verdict(store, _R_SHA, _R_RNG, "green", _R_RUN)
    pd.run_pass(store)
    assert _status(store, it["fix"]) == "done"
    assert pd.NOREPRO_WAIT_TAG not in _open_tags(store, it["fix"])
    assert any("already green" in c for c in _comment_texts(store, it["fix"]))
    assert _job_ids(store, it["fix"], "ci_rerun") == []
    assert _rerun_asks(store, mint.root_id) == []


def test_a_rerun_wait_over_the_cap_stalls_but_stays_parked(store: Store) -> None:
    """The >2h wait cap is the only in-flight stall: it keeps the park
    (a human rules there) and files the deduped ask — the rerun job
    itself is never re-minted."""
    mint, it = _parked_release_fix(store)
    pd.run_pass(store)
    later = datetime.now(UTC) + pd.RERUN_WAIT_CAP + timedelta(minutes=1)
    pd.run_pass(store, now=later)
    assert pd.NOREPRO_WAIT_TAG in _open_tags(store, it["fix"])
    (ask,) = _rerun_asks(store, mint.root_id)
    assert any("over two hours" in c for c in _comment_texts(store, it["fix"]))
    assert _meta(store, it["fix"])["ci_rerun_stalled"]
    assert len(_job_ids(store, it["fix"], "ci_rerun")) == 1
    # The marker dedupes: a still-parked node does not re-ask each tick.
    pd.run_pass(store, now=later)
    assert _rerun_asks(store, mint.root_id) == [ask]
    pd.run_pass(store, now=later + timedelta(minutes=5))
    assert _rerun_asks(store, mint.root_id) == [ask]


def test_a_wait_tag_outside_a_release_pass_never_reruns(store: Store) -> None:
    """The tag is only meaningful on the release-fix loop: a parked
    node whose plan has no ``meta.release`` has no cut sha to read a
    red run from — it stalls (note + ask), it never mints."""

    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        m.node("task", "A fix-shaped task", {"op": "run_slice"})

    mint = T._run(store, None, build)
    task = mint.ids["task"]
    store.add_tag(task, Tag.open(pd.NOREPRO_WAIT_TAG), set_by="system")
    pd.run_pass(store)
    assert _job_ids(store, task, "ci_rerun") == []
    assert pd.NOREPRO_WAIT_TAG not in _open_tags(store, task)
    (ask,) = _rerun_asks(store, mint.root_id)
    assert any("rerun path stalled" in c for c in _comment_texts(store, task))


def _pcb(store: Store, slug: str = "amp") -> int:
    ref, _created, _counts = store.pcb_apply(
        slug=slug,
        title=slug,
        components=[{"refdes": "R1", "label": "R", "pins": [{"name": "1"}]}],
        nets=[{"name": "SIG"}],
        connections=[{"net": "SIG", "refdes": "R1", "pin": "1"}],
    )
    return int(ref.id)


def test_pcb_fix_body_reads_the_board_from_until(store: Store) -> None:
    pcb = _pcb(store)
    mint = T.TEMPLATES["pcb-pipeline"].mint(store, None, board="pcb:amp")
    body = T.mint_body(store, mint.ids["loop"], "pcb-fix")
    assert set(body.ids) == {"fix", "drc", "review"}
    assert _meta(store, body.ids["drc"])["op"] == {
        "type": "pcb_review",
        "params": {"pcb_ref_id": pcb},
    }
    assert _meta(store, body.ids["review"])["generate"] == {
        "role": "triage",
        "from": f"pb{pcb}",
    }


def test_pcb_fix_body_reads_a_legacy_tests_green_until(store: Store) -> None:
    pcb = _pcb(store)

    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        m.node(
            "loop",
            "Loop",
            {
                "loop": {
                    "max": 5,
                    "until": {"kind": "tests-green", "args": {"range": "drc:pcb:amp"}},
                    "body": "pcb-fix",
                }
            },
        )

    loop = T._run(store, None, build).ids["loop"]
    body = T.mint_body(store, loop, "pcb-fix")
    assert _meta(store, body.ids["drc"])["op"]["params"] == {"pcb_ref_id": pcb}


def test_pcb_fix_body_refuses_a_loop_naming_no_board(store: Store) -> None:
    from precis.errors import BadInput

    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        m.node(
            "loop",
            "Loop",
            {
                "loop": {
                    "max": 5,
                    "until": {"kind": "drc-clean", "args": {"board": "pb999999999"}},
                    "body": "pcb-fix",
                }
            },
        )

    loop = T._run(store, None, build).ids["loop"]
    with pytest.raises(BadInput, match="names no pcb design"):
        T.mint_body(store, loop, "pcb-fix")


def _body_text(store: Store, todo_id: int) -> str:
    return "\n".join(
        str(c.text)
        for c in store.chunks.list_chunks_for_ref(todo_id)
        if c.chunk_kind == "todo_body"
    )


def test_route_fix_body_shape(store: Store) -> None:
    """measure (slicer over the examples gate) -> review (reviewer over the
    slicer's children); the loop todo's body rides into the slicer brief."""
    mint = T.TEMPLATES["route-loop"].mint(store, None, context="Spec hub: me1.")
    loop = mint.ids["loop"]
    body = T.mint_body(store, loop, "route-fix", iteration=2)
    assert set(body.ids) == {"measure", "review"}
    assert body.root_id == loop
    measure = _meta(store, body.ids["measure"])
    assert measure["generate"] == {
        "role": "slicer",
        "from": "tests/test_pcb_examples_route.py",
    }
    assert measure["task_class"] == "slicer"
    review = _meta(store, body.ids["review"])
    assert review["generate"] == {
        "role": "reviewer",
        "from": f"td{body.ids['measure']}",
    }
    assert review["task_class"] == "review"
    for todo_id in body.ids.values():
        ref = store.get_ref(kind="todo", id=todo_id)
        assert ref is not None and ref.parent_id == loop
        assert "iteration 2" in str(ref.title)
        assert f"plan:{mint.root_id}" in _open_tags(store, todo_id)
    text = _body_text(store, body.ids["measure"])
    assert f"td{loop}" in text and "`measure: <origin/main sha>`" in text
    assert T.ROUTE_ANSWER in text and T.ROUTE_GATE_CMD in text
    assert "iteration 2 of 6" in text
    assert text.rstrip().endswith("Spec hub: me1.")
    assert "review: LAND" in _body_text(store, body.ids["review"])
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_ref_id, dst_ref_id FROM links WHERE relation = 'blocked-by' "
            "AND src_ref_id = ANY(%s)",
            (list(body.ids.values()),),
        ).fetchall()
    assert {(int(s), int(d)) for s, d in rows} == {
        (body.ids["review"], body.ids["measure"])
    }


def test_childless_route_loop_mints_its_first_iteration_once_unblocked(
    store: Store,
) -> None:
    """me570353: minted childless behind ``after``, the loop fires nothing;
    the pass after the prerequisite closes mints iteration 1, and the next
    pass mints nothing more while that iteration is open."""

    def build(m: T._Minter) -> None:
        m.node("root", "Prerequisite", {"auto_close": True})
        m.node("stage", "Land the search", {"op": "run_slice"}, body="Build it.")

    pre = T._run(store, None, build)
    mint = T.TEMPLATES["route-loop"].mint(store, None, after=f"td{pre.ids['stage']}")
    loop = mint.ids["loop"]

    res = pd.run_pass(store)
    assert res.loops_minted == 0
    assert _children(store, loop) == []

    _finish_live_jobs(store)  # the prerequisite's task lands
    res = pd.run_pass(store)
    assert res.loops_minted == 1
    assert len(_children(store, loop)) == 2
    assert _meta(store, loop)["loop"]["count"] == 1
    assert _status(store, loop) == "open"

    assert pd.run_pass(store).loops_minted == 0
    assert len(_children(store, loop)) == 2


def test_pcb_pipeline_until_is_a_live_drc_clean_gate(store: Store) -> None:
    from precis.plans.gates import evaluate

    pcb = _pcb(store)
    mint = T.TEMPLATES["pcb-pipeline"].mint(store, None, board=f"pb{pcb}")
    loop = _meta(store, mint.ids["loop"])["loop"]
    verdict = evaluate(store, loop["until"], todo_id=mint.ids["loop"])
    assert not verdict.open
    assert verdict.why == f"no DRC run yet on pb{pcb}"


def test_mechanical_task_closes_when_its_job_succeeds(store: Store) -> None:
    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        m.node("check", "Check", {"op": {"type": "secret_status", "params": {}}})

    mint = T._run(store, None, build)
    task = mint.ids["check"]
    res = pd.run_pass(store, now=datetime.now(UTC))
    assert res.dispatched == 1
    ((_job, meta),) = _jobs_under(store, task)
    assert meta["job_type"] == "secret_status"
    assert meta["executor"] == "job_inproc"
    assert meta["params"] == {}
    _finish_live_jobs(store, close_task=False)
    res = pd.run_pass(store)
    assert _status(store, task) == "done"
    assert _status(store, mint.root_id) == "done"
    assert res.phases_closed == 2


def _generator_plan(store: Store, *, with_after: bool = False) -> T.PlanMint:
    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        gen = m.node(
            "gen", "Slice it", {"generate": {"role": "slicer", "from": "x.md~H"}}
        )
        m.node("kid", "Child", {"op": "run_slice"}, parent=gen, body="Do it.")
        if with_after:
            m.node("after", "After", {"op": "run_slice"}, blocked_by=("gen",))

    return T._run(store, None, build)


def _mark_done(store: Store, *ids: int) -> None:
    for ref_id in ids:
        store.add_tag(
            ref_id, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
        )


def test_done_generator_with_open_child_is_reopened(store: Store) -> None:
    mint = _generator_plan(store, with_after=True)
    gen, kid, after = mint.ids["gen"], mint.ids["kid"], mint.ids["after"]
    # The generator's session closed it (what the old brief told it to do).
    _mark_done(store, gen)

    res = pd.run_pass(store, now=datetime.now(UTC))
    assert res.reopened == 1
    assert res.counts()["reopened"] == 1
    assert res.summary().endswith("dry_run=False reopened=1 reviews=0")
    assert _status(store, gen) == "open"
    # Its dependant stays blocked; the child, not the generator, is dispatched.
    assert _jobs_under(store, after) == []
    assert _jobs_under(store, gen) == []
    assert len(_jobs_under(store, kid)) == 1

    # Children closed: nothing to reopen, the generator closes as a phase.
    _finish_live_jobs(store)
    res = pd.run_pass(store)
    assert res.reopened == 0
    assert "reopened=0" in res.summary()
    assert _status(store, gen) == "done"


def test_done_generator_without_open_children_is_left_closed(store: Store) -> None:
    mint = _generator_plan(store)
    _mark_done(store, mint.ids["gen"], mint.ids["kid"])
    res = pd.run_pass(store)
    assert res.reopened == 0
    assert _status(store, mint.ids["gen"]) == "done"


def _ask_child(store: Store, root: int, blocks: str) -> int:
    """An open ask todo under ``root`` carrying ``meta.ask.blocks``."""
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title="an ask",
            meta={"ask": {"kind": "ruling", "options": ["ok"], "blocks": blocks}},
            parent_id=root,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        store.add_tag(ref.id, Tag.open("ask-user"), set_by="system", conn=conn)
    return int(ref.id)


def test_informational_ask_does_not_hold_the_root_open(store: Store) -> None:
    """gr589823: an open ``blocks: "nothing"`` ask is a record, not a
    gate — the root closes over it and the ask stays open for the human.
    With the root closed, release_poll no longer sees an open pass."""
    from precis.workers.job_types import release_poll

    mint = _one_task_plan(store)
    task, root = mint.ids["task"], mint.root_id
    store.add_tag(root, Tag.open("release-pass"), set_by="system")
    _mark_done(store, task)
    ask = _ask_child(store, root, "nothing")
    assert release_poll._open_pass_id(store) == root
    res = pd.run_pass(store)
    assert res.phases_closed == 1
    assert _status(store, root) == "done"
    assert _status(store, ask) == "open"
    assert release_poll._open_pass_id(store) is None


def test_blocking_ask_still_holds_the_root_open(store: Store) -> None:
    """gr589823: an ask whose ``blocks`` names a todo handle is a gate —
    the root stays open while it is."""
    mint = _one_task_plan(store)
    task, root = mint.ids["task"], mint.root_id
    _mark_done(store, task)
    ask = _ask_child(store, root, f"td{task}")
    res = pd.run_pass(store)
    assert res.phases_closed == 0
    assert _status(store, root) == "open"
    assert _status(store, ask) == "open"


def test_loop_ask_blocks_its_pass_root(store: Store) -> None:
    """gr589823 / me573231: the exhausted release-fix ask parks the pass,
    so it names the pass root in ``blocks`` (not ``"nothing"``) and the
    root stays open while it is."""
    mint = _one_task_plan(store)
    task, root = mint.ids["task"], mint.root_id
    _mark_done(store, task)
    node: Any = SimpleNamespace(id=task, root=root)
    ask = pd._mint_loop_ask(store, node, 2, "CI red")
    assert _meta(store, ask)["ask"]["blocks"] == f"td{root}"
    assert pd.run_pass(store).phases_closed == 0
    assert _status(store, root) == "open"


def _moot_plan(store: Store) -> T.PlanMint:
    """root -> childless triage generator -> after (blocked-by it)."""

    def build(m: T._Minter) -> None:
        m.node("root", "Fix gr1", {"auto_close": True})
        m.node("gen", "Triage gr1", {"generate": {"role": "triage", "from": "gr1"}})
        m.node("after", "Build", {"op": "run_slice"}, blocked_by=("gen",))

    return T._run(store, None, build)


def test_moot_generator_closes_the_plan(store: Store) -> None:
    """gr548813: run_slice closed the triage on a `moot:` verdict and left
    MOOT_TAG; the tick closes the rest of the plan as won't-do, once."""
    from precis.plans.nodes import MOOT_TAG

    mint = _moot_plan(store)
    gen, after = mint.ids["gen"], mint.ids["after"]
    _mark_done(store, gen)
    store.add_tag(gen, Tag.open(MOOT_TAG), set_by="system")

    res = pd.run_pass(store)
    assert _status(store, gen) == "done"
    assert _status(store, after) == "won't-do"
    assert _status(store, mint.root_id) == "won't-do"
    assert MOOT_TAG not in _open_tags(store, gen)
    assert any("closed as won't-do" in n for n in res.notes)
    assert _jobs_under(store, after) == []

    # Idempotent: nothing left to close, no second note.
    res = pd.run_pass(store)
    assert not any("closed as won't-do" in n for n in res.notes)


def test_verified_moot_build_closes_the_chain_and_the_gripe(
    store: Store,
) -> None:
    """td584381 S1d: a build whose ``run_slice`` verified its ``moot:``
    claim carries ``verdict:moot`` + ``meta.moot_verified.sha`` — the
    tick closes the whole chain (review, gate, validate, root) won't-do
    and resolves the plan's gripe on that sha exactly as a land does:
    ``resolved: <sha9>`` + ``STATUS:verify``, no ask, no job minted."""
    from precis.plans.nodes import MOOT_TAG

    gid = int(insert_gripe(store, "the flaky gate", status="fixing").id)
    mint = T.mint_gripe_fix(store, None, gripe_id=gid, direct=True)
    build = mint.ids["build"]
    sha = "f628e681f"
    _slice_job(
        store,
        build,
        "run_slice",
        "succeeded",
        meta={
            "moot_verified": {
                "sha": sha,
                "line": f"moot: gr{gid} already fixed by {sha}",
            }
        },
    )
    _mark_done(store, build)
    store.add_tag(build, Tag.open(MOOT_TAG), set_by="system")

    res = pd.run_pass(store)
    for key in ("review", "gate", "validate"):
        assert _status(store, mint.ids[key]) == "won't-do", key
    assert _status(store, mint.root_id) == "won't-do"
    assert MOOT_TAG not in _open_tags(store, build)
    assert (res.asks, res.dispatched) == (0, 0)
    # The gripe got the land's record.
    assert _status(store, gid) == "verify"
    assert any(
        c.lstrip().lower().startswith("resolved:") and sha[:9] in c
        for c in _comments(store, gid)
    )
    # Idempotent: a second pass stamps nothing.
    res2 = pd.run_pass(store)
    assert (res2.asks, res2.dispatched) == (0, 0)
    assert not any("closed as won't-do" in n for n in res2.notes)
    assert len(_comments(store, gid)) == 1


def test_moot_key_matches_run_slice() -> None:
    """``plan_dispatch`` restates the key ``run_slice`` stamps on a
    verified-moot job (the dependency stays one-way)."""
    assert pd.MOOT_VERIFIED_KEY == run_slice.MOOT_VERIFIED_KEY == "moot_verified"


def _moot_tag(store: Store, todo: int, sha: str = "f628e681f", gripe: int = 1) -> None:
    """The state ``run_slice.poll`` leaves after a VERIFIED moot: the
    succeeded job carries ``meta.moot_verified``, the todo is done and
    tagged ``verdict:moot``."""
    from precis.plans.nodes import MOOT_TAG

    _slice_job(
        store,
        todo,
        "run_slice",
        "succeeded",
        meta={
            "moot_verified": {
                "sha": sha,
                "line": f"moot: gr{gripe} already fixed by {sha}",
            }
        },
    )
    _mark_done(store, todo)
    store.add_tag(todo, Tag.open(MOOT_TAG), set_by="system")


def test_task_moot_closes_only_its_own_chain(store: Store) -> None:
    """td584381 S1d review round 1: a verified task moot closes only the
    moot node's ``blocked-by`` descendants — a second open build under
    the same root is untouched and the root stays open, the note naming
    the still-open node."""
    from precis.plans.nodes import MOOT_TAG

    def build(m: T._Minter) -> None:
        m.node("root", "Fix gr1", {"auto_close": True})
        m.node("b1", "Build one", {"op": "run_slice"})
        m.node("r1", "Review one", {"op": "run_slice"}, blocked_by=("b1",))
        m.node("v1", "Validate one", {"op": "run_slice"}, blocked_by=("r1",))
        m.node("b2", "Build two", {"op": "run_slice"})

    mint = T._run(store, None, build)
    b1, r1, v1, b2 = (mint.ids[k] for k in ("b1", "r1", "v1", "b2"))
    _moot_tag(store, b1)

    res = pd.run_pass(store)
    # Only b1's own chain closed; the sibling build and the root stay.
    assert _status(store, r1) == "won't-do"
    assert _status(store, v1) == "won't-do"
    assert _status(store, b2) != "won't-do"
    assert _status(store, mint.root_id) not in pd.TERMINAL_TODO_STATUSES
    assert MOOT_TAG in _open_tags(store, b1)
    assert any(
        f"plan td{mint.root_id} not closed: td{b2} still open" in n for n in res.notes
    )
    # The gripe stamp waits on the root close.
    assert not any("closed as won't-do" in n for n in res.notes)


def test_task_moot_defers_a_descendant_with_a_live_job(store: Store) -> None:
    """td584381 S1d review round 1: a descendant carrying a live job is
    never closed — the note says deferred and the tag stays for the next
    tick; once the job ends the pass closes it, the root and the gripe."""
    from precis.plans.nodes import MOOT_TAG

    gid = int(insert_gripe(store, "the flaky gate", status="fixing").id)
    mint = T.mint_gripe_fix(store, None, gripe_id=gid, direct=True)
    build, review = mint.ids["build"], mint.ids["review"]
    _moot_tag(store, build)
    live = _slice_job(store, review, "run_slice", "queued")

    res = pd.run_pass(store)
    assert _status(store, review) == "open"
    assert _status(store, mint.ids["gate"]) == "won't-do"
    assert _status(store, mint.ids["validate"]) == "won't-do"
    assert _status(store, mint.root_id) == "open"
    assert MOOT_TAG in _open_tags(store, build)
    assert any(
        f"td{review} has a live job; moot close deferred" in n for n in res.notes
    )
    # No root close yet, so no gripe stamp either.
    assert _status(store, gid) == "fixing"

    # The job ends: the next pass closes the review, the root, stamps.
    _set_job_status(store, live, "failed")
    res = pd.run_pass(store)
    assert _status(store, review) == "won't-do"
    assert _status(store, mint.root_id) == "won't-do"
    assert MOOT_TAG not in _open_tags(store, build)
    assert _status(store, gid) == "verify"
    assert len(_comments(store, gid)) == 1


def test_dead_build_closes_the_review_no_dispatch_no_ask(store: Store) -> None:
    """gr575912: a build closed ``won't-do`` by the coordinator with no
    ``landed:`` comment leaves its reviewer nothing to judge — the tick
    dispatches no review job, closes the review ``won't-do`` with one
    note, files no ask, and the root settles. The shape is the
    ``gripe-fix-direct`` plan td575465 was (build -> review, gate,
    validate)."""
    gid = int(insert_gripe(store, "the dead fix", status="fixing").id)
    mint = T.mint_gripe_fix(store, None, gripe_id=gid, direct=True)
    build, review = mint.ids["build"], mint.ids["review"]
    store.add_tag(
        build,
        Tag.closed("STATUS", "won't-do"),
        set_by="system",
        replace_prefix=True,
    )

    res = pd.run_pass(store)
    for key in ("review", "gate", "validate"):
        assert _status(store, mint.ids[key]) == "won't-do", key
    assert _status(store, mint.root_id) in pd.TERMINAL_TODO_STATUSES
    assert (res.asks, res.dispatched) == (0, 0)
    assert _jobs_under(store, review) == []
    assert any(
        f"td{review}: review closed won't-do" in n and "no `landed:` comment" in n
        for n in res.notes
    )

    # A second pass is quiet: no redispatch, no ask, no second close.
    res2 = pd.run_pass(store)
    assert (res2.asks, res2.dispatched) == (0, 0)
    assert not any("won't-do" in n for n in res2.notes)


def test_dead_review_target_via_blocked_by_only(store: Store) -> None:
    """gr575912, engine-review shape: ``templates.mint_reviewer`` hung
    under a DIFFERENT root points ``generate.from`` at a todo the loaded
    plan tree does not reach — the close still reads the dead target off
    the ``blocked-by`` edge's status."""
    gid = int(insert_gripe(store, "the dead fix", status="fixing").id)
    mint = T.mint_gripe_fix(store, None, gripe_id=gid, direct=True)
    build = mint.ids["build"]
    store.add_tag(
        build,
        Tag.closed("STATUS", "won't-do"),
        set_by="system",
        replace_prefix=True,
    )

    def other_(m: T._Minter) -> None:
        m.node("root", "Other plan", {"auto_close": True})

    other = T._run(store, None, other_)
    orphan = T.mint_reviewer(store, other.root_id, build)

    res = pd.run_pass(store)
    assert _status(store, orphan) == "won't-do"
    assert _jobs_under(store, orphan) == []
    assert _status(store, mint.ids["review"]) == "won't-do"
    assert _status(store, other.root_id) in pd.TERMINAL_TODO_STATUSES
    assert (res.asks, res.dispatched) == (0, 0)


def test_landed_build_lets_the_review_dispatch(store: Store) -> None:
    """The dead-chain close only bites on a target with no ``landed:``
    comment — a ``done`` build that did land keeps its review live."""
    mint = _review_plan(store)
    build, review = mint.ids["build"], mint.ids["review"]
    _job(store, build, lane="claude-sub")
    _todo_comment(store, build, "landed: abc1234 — the fix")
    _mark_done(store, build)

    res = pd.run_pass(store)
    assert res.dispatched == 1
    assert _status(store, review) == "open"
    ((_job_id, meta),) = _jobs_under(store, review)
    assert meta["job_type"] == "run_slice"


def test_partly_dead_slice_set_keeps_the_review(store: Store) -> None:
    """A reviewer over a slicer judges each minted slice's land: one
    slice won't-do while another landed still leaves it work — the
    close needs EVERY target dead."""

    def build_(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        s = m.node(
            "slicer", "Slice it", {"generate": {"role": "slicer", "from": "x.md~H"}}
        )
        m.node("t1", "Task one", {"op": "run_slice"}, parent=s)
        m.node("t2", "Task two", {"op": "run_slice"}, parent=s)
        m.node(
            "review",
            "Review the slices",
            T._reviewer(s),
            body=T._reviewer_brief(s),
            blocked_by=("slicer",),
        )

    mint = T._run(store, None, build_)
    slicer, review = mint.ids["slicer"], mint.ids["review"]
    _todo_comment(store, mint.ids["t1"], "landed: abc1234 — slice one")
    _mark_done(store, mint.ids["t1"], slicer)
    store.add_tag(
        mint.ids["t2"],
        Tag.closed("STATUS", "won't-do"),
        set_by="system",
        replace_prefix=True,
    )

    res = pd.run_pass(store)
    assert res.dispatched == 1
    assert _status(store, review) == "open"


def test_dead_chain_straggler_closes_once_its_job_ends(store: Store) -> None:
    """gr575912: a dead-chain node holding a live job defers the close —
    it is never cancelled — and the pass re-sweeps it once the job ends
    (a closed reviewer's targets stay dead), so nothing dispatches on
    the dead chain and the root still settles."""
    gid = int(insert_gripe(store, "the dead fix", status="fixing").id)
    mint = T.mint_gripe_fix(store, None, gripe_id=gid, direct=True)
    build, review, validate = (mint.ids[k] for k in ("build", "review", "validate"))
    store.add_tag(
        build,
        Tag.closed("STATUS", "won't-do"),
        set_by="system",
        replace_prefix=True,
    )
    live = _slice_job(store, validate, "run_slice", "queued")

    res = pd.run_pass(store)
    assert _status(store, review) == "won't-do"
    assert _status(store, mint.ids["gate"]) == "won't-do"
    assert _status(store, validate) == "open"
    assert (res.asks, res.dispatched) == (0, 0)
    assert any(
        f"td{validate} has a live job; dead-chain close deferred" in n
        for n in res.notes
    )

    _set_job_status(store, live, "failed")
    res = pd.run_pass(store)
    assert _status(store, validate) == "won't-do"
    assert _status(store, mint.root_id) in pd.TERMINAL_TODO_STATUSES
    assert (res.asks, res.dispatched) == (0, 0)


def test_task_moot_stamp_failure_keeps_the_tag(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td584381 S1d review round 1: ``_notify_gripe`` runs BEFORE
    ``MOOT_TAG`` drops — a raise leaves the tag so the next tick stamps
    and clears it; a pass after that stamps nothing (comment count 1)."""
    from precis.plans.nodes import MOOT_TAG
    from precis.workers.job_types import land_slice

    gid = int(insert_gripe(store, "the flaky gate", status="fixing").id)
    mint = T.mint_gripe_fix(store, None, gripe_id=gid, direct=True)
    build = mint.ids["build"]
    _moot_tag(store, build, gripe=gid)

    real = land_slice._notify_gripe
    calls = {"n": 0}

    def flaky(*args: Any) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db blip")
        return real(*args)

    monkeypatch.setattr(land_slice, "_notify_gripe", flaky)

    res = pd.run_pass(store)
    assert calls["n"] == 1
    # The root stays open — a closed root would drop the plan from the
    # candidates and the retained tag could never retry the stamp.
    assert _status(store, mint.root_id) not in pd.TERMINAL_TODO_STATUSES
    # The tag survived the raise: the stamp is retried next tick.
    assert MOOT_TAG in _open_tags(store, build)
    assert any("gripe stamp failed" in n for n in res.notes)
    assert _status(store, gid) == "fixing"

    res = pd.run_pass(store)
    assert calls["n"] == 2
    assert MOOT_TAG not in _open_tags(store, build)
    assert _status(store, mint.root_id) == "won't-do"
    assert _status(store, gid) == "verify"
    assert len(_comments(store, gid)) == 1

    pd.run_pass(store)
    assert calls["n"] == 2
    assert len(_comments(store, gid)) == 1


def test_task_moot_stamp_failure_asks_after_three_ticks(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td584381 S1d review round 2: a persistently raising
    ``_notify_gripe`` is bounded — ``meta.moot_stamp_failures`` counts
    the ticks, the third files ONE ask on the plan root (the exception
    TYPE, never its message), and later ticks neither retry nor ask
    while it stays open — no silent res.notes loop."""
    from precis.plans.nodes import MOOT_TAG
    from precis.workers.job_types import land_slice

    gid = int(insert_gripe(store, "the flaky gate", status="fixing").id)
    mint = T.mint_gripe_fix(store, None, gripe_id=gid, direct=True)
    build = mint.ids["build"]
    _moot_tag(store, build, gripe=gid)

    calls = {"n": 0}

    def boom(*args: Any) -> str:
        calls["n"] += 1
        raise RuntimeError("db blip")

    monkeypatch.setattr(land_slice, "_notify_gripe", boom)

    for _ in range(3):
        pd.run_pass(store)
    assert calls["n"] == 3
    assert _meta(store, build)["moot_stamp_failures"] == 3
    asks = [c for c in _children(store, mint.root_id) if "ask" in _meta(store, c)]
    assert len(asks) == 1
    body = _body_text(store, asks[0])
    assert "moot verified but the gripe stamp fails: RuntimeError" in body
    assert "db blip" not in body  # the exception TYPE only, no message
    assert f"td{build}" in body and f"td{mint.root_id}" in body
    assert f"gr{gid}" in body and "f628e681f" in body
    assert _blocked_by_ids(store, mint.root_id) == asks
    # The root stays open and the tag survives: the plan waits on a human.
    assert _status(store, mint.root_id) not in pd.TERMINAL_TODO_STATUSES
    assert MOOT_TAG in _open_tags(store, build)
    assert _status(store, gid) == "fixing"

    # Ticks 4 and 5: parked — no second ask, no ``_notify_gripe`` call.
    res = pd.run_pass(store)
    res = pd.run_pass(store)
    assert calls["n"] == 3
    assert [
        c for c in _children(store, mint.root_id) if "ask" in _meta(store, c)
    ] == asks
    assert any("gripe stamp parked" in n for n in res.notes)


def test_task_moot_stamp_failure_counter_clears_on_success(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td584381 S1d review round 2: a stamp that fails twice then
    succeeds clears ``meta.moot_stamp_failures`` and closes the root —
    two raises stay under the ask threshold, so no ask is ever filed."""
    from precis.plans.nodes import MOOT_TAG
    from precis.workers.job_types import land_slice

    gid = int(insert_gripe(store, "the flaky gate", status="fixing").id)
    mint = T.mint_gripe_fix(store, None, gripe_id=gid, direct=True)
    build = mint.ids["build"]
    _moot_tag(store, build, gripe=gid)

    real = land_slice._notify_gripe
    calls = {"n": 0}

    def flaky(*args: Any) -> str:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("db blip")
        return real(*args)

    monkeypatch.setattr(land_slice, "_notify_gripe", flaky)

    pd.run_pass(store)
    assert _meta(store, build)["moot_stamp_failures"] == 1
    pd.run_pass(store)
    assert _meta(store, build)["moot_stamp_failures"] == 2
    pd.run_pass(store)
    assert calls["n"] == 3
    # Success clears the counter, drops the tag and closes the root.
    assert _meta(store, build)["moot_stamp_failures"] == 0
    assert MOOT_TAG not in _open_tags(store, build)
    assert _status(store, mint.root_id) == "won't-do"
    assert _status(store, gid) == "verify"
    assert [c for c in _children(store, mint.root_id) if "ask" in _meta(store, c)] == []


def test_verified_moot_end_to_end_poll_then_tick(
    store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """td584381 S1d review round 1, end to end: ``run_slice.poll``
    verifies the ``moot:`` claim on the fake ``_git`` (fetch, merge-base
    ancestor, the sha's own message naming the chain), stamps
    ``meta.moot_verified`` and tags the todo; the tick closes the chain
    + root won't-do and resolves the gripe once — the second pass files
    nothing."""
    import json
    import subprocess

    from precis.plans.nodes import MOOT_TAG

    gid = int(insert_gripe(store, "the flaky gate", status="fixing").id)
    mint = T.mint_gripe_fix(store, None, gripe_id=gid, direct=True)
    build = mint.ids["build"]
    sha = "f628e681f"
    cmds: list[list[str]] = []

    def fake_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        cmds.append(list(args))
        out = ""
        if args[:3] == ["show", "-s", "--format=%B"]:
            out = f"fix(plan): the fix (td{build})\n"
        return subprocess.CompletedProcess([], 0, out, "")

    monkeypatch.setattr(run_slice, "_git", fake_git)
    monkeypatch.setattr(run_slice, "_mint_land", lambda *a, **k: 999)

    job = _slice_job(
        store,
        build,
        "run_slice",
        "running",
        meta={"lane": "claude-sub", "adapter": "claude_p"},
    )
    tree = tmp_path / "tree"
    tree.mkdir()
    log = tmp_path / "run.log"
    log.write_text(
        json.dumps(
            {
                "is_error": False,
                "result": f"Checked main first.\n"
                f"moot: gr{gid} already fixed by {sha}\nDone.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    exit_file = tmp_path / "exit"
    exit_file.write_text("0\n", encoding="utf-8")
    handle = {
        "pid": 1,
        "worktree": str(tree),
        "log": str(log),
        "exit_file": str(exit_file),
    }

    ctx = _PollCtx(store, job, {"todo_id": build})
    assert run_slice.poll(ctx, handle) is True
    assert ctx.status == "succeeded"
    assert MOOT_TAG in _open_tags(store, build)
    assert _status(store, build) == "done"
    assert ["fetch", "origin", "main"] in cmds
    assert ["merge-base", "--is-ancestor", sha, "origin/main"] in cmds
    assert ["show", "-s", "--format=%B", sha] in cmds

    res = pd.run_pass(store)
    for key in ("review", "gate", "validate"):
        assert _status(store, mint.ids[key]) == "won't-do", key
    assert _status(store, mint.root_id) == "won't-do"
    assert MOOT_TAG not in _open_tags(store, build)
    assert (res.asks, res.dispatched) == (0, 0)
    assert _status(store, gid) == "verify"
    assert any(
        c.lstrip().lower().startswith("resolved:") and sha[:9] in c
        for c in _comments(store, gid)
    )

    # Idempotent: a second pass closes and stamps nothing.
    res2 = pd.run_pass(store)
    assert (res2.asks, res2.dispatched) == (0, 0)
    assert not any("closed as won't-do" in n for n in res2.notes)
    assert len(_comments(store, gid)) == 1


class _PollCtx:
    """The ctx surface ``run_slice.poll`` writes through, bound to a real
    job row: ``set_meta`` stamps the ref so the tick reads
    ``meta.moot_verified`` off the plan's own load."""

    def __init__(self, store: Store, job_id: int, params: dict[str, Any]) -> None:
        self.store = store
        self.ref_id = job_id
        self.meta: dict[str, Any] = {
            "params": params,
            "lane": "claude-sub",
            "adapter": "claude_p",
        }
        self.status: str | None = None
        self.failure: str | None = None

    def set_status(self, value: str) -> None:
        self.status = value

    def set_meta(self, **kw: Any) -> None:
        self.store.stamp_ref_meta(self.ref_id, kw)

    def append_chunk(self, kind: str, text: str) -> None:
        ords = [int(c.ord) for c in self.store.chunks.list_chunks_for_ref(self.ref_id)]
        self.store.chunks.insert_chunks(
            self.ref_id,
            [
                ChunkInsert(
                    ord=max(ords, default=0) + 1,
                    text=text,
                    meta={"chunk_kind": kind},
                )
            ],
        )

    def record_failure(self, reason: str, **_kw: Any) -> None:
        self.failure = reason
        self.status = "failed"


def test_no_commit_rescue_reads_the_verdict_from_the_handoff(
    store: Store,
) -> None:
    """td584381 S1d: a JSON-log adapter parses the session's result out
    of the raw log, so the job's event text can miss the ``moot:`` line —
    the posted ``handoff:`` comment still carries it and the rescue
    evidence shows it (the log tail keeps precedence when it has one)."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    # The job's event text carries NO ``moot:`` substring — the verdict
    # lives only in the handoff, so ``_handoff_verdict`` stubbed out
    # fails this test for real (review round 2).
    _failed_job(
        store,
        task,
        '{"type":"result","is_error":false,"result":"checked it"}',
        meta={"no_commit": True},
    )
    _todo_comment(
        store,
        task,
        "handoff: moot: gr1 already fixed by f628e681f\n\nChecked origin/main.",
    )
    res = pd.run_pass(store)
    assert res.rescued == 1 and res.asks == 0
    assert "rescue:builder" in _open_tags(store, task)
    # The verdict must land in the rescue evidence's ``step:`` line —
    # the log tail has no ``moot:`` to give a false pass.
    evidence = [c for c in _todo_comments(store, task) if c.startswith("rescue:")]
    assert evidence, "no rescue evidence comment"
    steps = [ln for c in evidence for ln in c.splitlines() if ln.startswith("step: ")]
    assert steps, "no step: line in the rescue evidence"
    assert any(
        "the session's verdict: moot: gr1 already fixed by f628e681f" in ln
        for ln in steps
    )


def test_no_commit_ask_reads_the_handoff_verdict(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td584381 S1d review round 1: the cap ask's body carries the
    ``moot:`` line the log tail missed — read off the newest
    ``handoff:`` comment (the ``_mint_ask`` path)."""
    dead = L.Lane(
        name="devin",
        adapter="devin_tree",
        credential_pool="free-promo",
        cap=3,
        task_classes=frozenset({"code", "review", "validate"}),
        enabled=False,
        vendor="cognition",
        models=("swe-2-medium", "swe-2-high", "swe-2-max"),
        default_model="swe-2-high",
        cost_class=L.COST_FREE,
    )
    use_lanes(monkeypatch, dead)
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(
        store,
        task,
        '{"type":"result","is_error":false,"result":"checked it"}',
        meta={"no_commit": True},
    )
    _todo_comment(
        store,
        task,
        "handoff: moot: gr1 already fixed by f628e681f\n\nChecked origin/main.",
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (0, 1, 0)
    (ask,) = [c for c in _children(store, mint.root_id) if "ask" in _meta(store, c)]
    assert "moot: gr1 already fixed by f628e681f" in _body_text(store, ask)


def test_handoff_verdict_scopes_to_the_newest_attempt(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td584381 S1d review round 1: only the newest ``handoff:`` comment
    is read — an earlier attempt's ``moot:`` handoff is never the new
    attempt's verdict (here the newer handoff carries none, so the ask
    body shows no verdict)."""
    dead = L.Lane(
        name="devin",
        adapter="devin_tree",
        credential_pool="free-promo",
        cap=3,
        task_classes=frozenset({"code", "review", "validate"}),
        enabled=False,
        vendor="cognition",
        models=("swe-2-medium", "swe-2-high", "swe-2-max"),
        default_model="swe-2-high",
        cost_class=L.COST_FREE,
    )
    use_lanes(monkeypatch, dead)
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    # Attempt one left a ``moot:`` handoff; attempt two failed with no
    # verdict anywhere — neither log tail nor its own handoff.
    _todo_comment(store, task, "handoff: moot: gr1 already fixed by f628e681f")
    _failed_job(
        store,
        task,
        "run_slice: session left no commit\n--- log tail ---\nexit 0",
        meta={"no_commit": True},
    )
    _todo_comment(store, task, "handoff: second try, still nothing to land")
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks, res.rescued) == (0, 1, 0)
    (ask,) = [c for c in _children(store, mint.root_id) if "ask" in _meta(store, c)]
    assert "moot: gr1" not in _body_text(store, ask)
    assert "its verdict:" not in _body_text(store, ask)


def test_handoff_verdict_rejects_a_handoff_older_than_the_attempt(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td584381 S1d review round 2: ``_handoff_verdict(since=<newest
    job's created_at>)`` returns ``None`` when the newest ``handoff:``
    predates that job — attempt one's ``moot:`` is never attempt two's.
    And a ``handoff:`` whose ``created_at`` cannot be read fails CLOSED
    (no verdict): an unprovable scope is no scope."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    # Attempt one left the moot handoff; attempt two's job is newer and
    # left no handoff of its own.
    _todo_comment(store, task, "handoff: moot: gr1 already fixed by f628e681f")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET created_at = now() - interval '1 minute' "
            "WHERE ref_id = %s",
            (task,),
        )
    job = _failed_job(
        store,
        task,
        "run_slice: session left no commit\n--- log tail ---\nexit 0",
        meta={"no_commit": True},
    )
    job_ref = store.get_ref(kind="job", id=job)
    assert job_ref is not None
    assert pd._handoff_verdict(store, task, since=job_ref.created_at) is None
    # Without the scope the same handoff IS the verdict.
    assert pd._handoff_verdict(store, task) == ("moot: gr1 already fixed by f628e681f")
    # ``created_at`` missing on the comment chunk while ``since`` is
    # known: fail closed, never read an unscoped handoff.
    monkeypatch.setattr(
        pd,
        "_comments",
        lambda *_a: [
            SimpleNamespace(
                text="handoff: moot: gr1 already fixed by f628e681f",
            )
        ],
    )
    assert pd._handoff_verdict(store, task, since=job_ref.created_at) is None
    # …but a missing ``since`` still means no scoping at all.
    assert pd._handoff_verdict(store, task) == ("moot: gr1 already fixed by f628e681f")


def test_review_revert_floors_on_the_newest_ended_job(store: Store) -> None:
    """td584381 S1d: a still-queued job — a re-minted land that never
    ran — is no re-arm boundary: the REVERT floor is the newest ENDED
    job (the succeeded land), not the queued one."""
    mint = _reviewed_landed(store, "review: REVERT — breaks the public API")
    build = mint.ids["build"]
    land = _jobs_under(store, build)[-1][0]
    _slice_job(store, build, "land_slice", "queued")
    res = pd.run_pass(store)
    assert res.reviews == 1
    assert _meta(store, build)["rearmed_after_job"] == land


def test_dry_run_counts_the_reopen_without_writing(store: Store) -> None:
    mint = _generator_plan(store)
    _mark_done(store, mint.ids["gen"])
    res = pd.run_pass(store, dry_run=True, reason="params.dry_run")
    assert res.reopened == 1
    assert _status(store, mint.ids["gen"]) == "done"


# ── gripe lease expiry (gr542086) ──────────────────────────────────


def _gripe(hub: Any, status: str = "triaged") -> int:
    """A live gripe at ``status`` (the claim-ladder test's recipe)."""
    from precis.handlers.gripe import GripeHandler

    GripeHandler(hub=hub).put(text=f"lease expiry probe {status}")
    gid = int(hub.store.list_refs(kind="gripe", limit=1)[0].id)
    hub.store.add_tag(
        gid, Tag.closed("STATUS", status), replace_prefix=True, set_by="agent"
    )
    return gid


def _claim(hub: Any, gid: int, holder: str = "builder-a") -> None:
    from precis.handlers.gripe import GripeHandler

    GripeHandler(hub=hub).tag(id=gid, add=[f"claimed-by:{holder}"])


def _lapse(store: Store, gid: int) -> None:
    """Age the lease past its expiry (``expires_at`` is the lease clock)."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags SET expires_at = now() - interval '1 minute' "
            "WHERE ref_id = %s AND tag_id IN (SELECT tag_id FROM tags "
            "WHERE value LIKE 'claimed-by:%%')",
            (gid,),
        )


def _comments(store: Store, gid: int) -> list[str]:
    return [
        c.text
        for c in sorted(store.chunks.list_chunks_for_ref(gid), key=lambda c: c.ord)
        if c.chunk_kind == "gripe_comment"
    ]


def test_expired_gripe_lease_returns_to_triaged(hub: Any) -> None:
    store = hub.store
    gid = _gripe(hub)
    _claim(hub, gid)
    assert _status(store, gid) == "fixing"
    # A live lease is left alone.
    assert pd.run_pass(store).released == 0
    assert _status(store, gid) == "fixing"
    _lapse(store, gid)
    res = pd.run_pass(store)
    assert res.released == 1
    assert f"gr{gid}: released expired claimed-by:builder-a" in res.notes
    assert _status(store, gid) == "triaged"
    assert not any(t.startswith("claimed-by:") for t in _open_tags(store, gid))
    assert _comments(store, gid)[-1] == (
        "lease expired after 4 h; back to triaged (held by builder-a)"
    )
    # Idempotent: nothing left to release.
    assert pd.run_pass(store).released == 0
    assert len(_comments(store, gid)) == 1


def test_legacy_gripe_lease_is_aged_by_its_tag_row(hub: Any) -> None:
    store = hub.store
    gid = _gripe(hub, "fixing")
    store.add_tag(gid, Tag.open("claimed-by:legacy"), set_by="agent")
    assert pd.run_pass(store).released == 0
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags SET created_at = now() - interval '5 hours' "
            "WHERE ref_id = %s AND tag_id IN (SELECT tag_id FROM tags "
            "WHERE value = 'claimed-by:legacy')",
            (gid,),
        )
    assert pd.run_pass(store).released == 1
    assert _status(store, gid) == "triaged"


def test_expired_gripe_lease_with_a_live_job_is_kept(hub: Any) -> None:
    store = hub.store
    gid = _gripe(hub)
    _claim(hub, gid)
    _lapse(store, gid)
    with store.tx() as conn:
        mint_background_job(
            store,
            conn=conn,
            job_type="fix_gripe",
            executor="claude_inproc",
            title="fix_gripe",
            params={},
            parent_id=gid,
        )
    assert pd.run_pass(store).released == 0
    assert _status(store, gid) == "fixing"
    assert "claimed-by:builder-a" in _open_tags(store, gid)


def test_expired_gripe_lease_after_resolved_only_drops_the_lease(hub: Any) -> None:
    from precis.handlers.gripe import GripeHandler

    store = hub.store
    gid = _gripe(hub)
    _claim(hub, gid)
    GripeHandler(hub=hub).put(id=gid, text="resolved: abc1234 — done by hand")
    _lapse(store, gid)
    assert pd.run_pass(store).released == 1
    assert _status(store, gid) == "fixing"
    assert not any(t.startswith("claimed-by:") for t in _open_tags(store, gid))
    assert _comments(store, gid) == ["resolved: abc1234 — done by hand"]


def test_dry_run_counts_the_gripe_release_without_writing(hub: Any) -> None:
    store = hub.store
    gid = _gripe(hub)
    _claim(hub, gid)
    _lapse(store, gid)
    res = pd.run_pass(store, dry_run=True)
    assert res.released == 1
    assert _status(store, gid) == "fixing"
    assert _comments(store, gid) == []


# ── reviewer verdicts (td543732) ───────────────────────────────────


def _review_plan(store: Store) -> T.PlanMint:
    """root phase: build (run_slice) <- review (reviewer generator)."""

    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        b = m.node("build", "Build it", {"op": "run_slice"}, body="Build it.")
        m.node(
            "review",
            "Review it",
            T._reviewer(b),
            body=T._reviewer_brief(b),
            blocked_by=("build",),
        )

    return T._run(store, None, build)


def _job(
    store: Store,
    todo: int,
    *,
    status: str = "succeeded",
    lane: str | None = None,
    commits: list[str] | None = None,
) -> int:
    extra: dict[str, Any] = {}
    if lane:
        extra["lane"] = lane
    if commits is not None:
        extra["branch_commits"] = commits  # run_slice.poll's stamp
    with store.tx() as conn:
        ref = mint_background_job(
            store,
            conn=conn,
            job_type="run_slice",
            executor="ssh_node",
            title="run_slice",
            params={"todo_id": todo},
            parent_id=todo,
            status=status,
            meta_extra=extra or None,
        )
    return int(ref.id)


def _todo_comment(store: Store, todo: int, text: str) -> None:
    ords = [int(c.ord) for c in store.chunks.list_chunks_for_ref(todo)]
    store.chunks.insert_chunks(
        todo,
        [
            ChunkInsert(
                ord=max(ords, default=0) + 1,
                text=text,
                meta={"chunk_kind": "todo_comment"},
            )
        ],
    )


def _todo_comments(store: Store, todo: int) -> list[str]:
    return [
        str(c.text)
        for c in sorted(store.chunks.list_chunks_for_ref(todo), key=lambda c: c.ord)
        if c.chunk_kind == "todo_comment"
    ]


def _reviewed(store: Store, verdict: str) -> T.PlanMint:
    """The build landed (``landed:`` comment, done) on claude-sub; the
    review session ran, posted ``verdict`` and left its claim."""
    mint = _review_plan(store)
    build, review = mint.ids["build"], mint.ids["review"]
    _job(store, build, lane="claude-sub")
    _todo_comment(store, build, "landed: abc1234 — the fix")
    _mark_done(store, build)
    _job(store, review, lane="claude-sub")
    store.add_tag(review, Tag.open("claimed-by:lane-claude-sub"), set_by="system")
    store.add_tag(
        review, Tag.closed("STATUS", "doing"), set_by="system", replace_prefix=True
    )
    if verdict:
        _todo_comment(store, review, verdict)
    _todo_comment(store, review, "handoff: reviewed the squash diff")
    return mint


def _reviewed_landed(store: Store, verdict: str) -> T.PlanMint:
    """The production REVERT shape (gr586688): the reviewed todo's newest
    job is the succeeded ``land_slice`` ``run_slice`` mints as its child —
    what every engine-landed plan todo carries. ``_reviewed``'s
    run_slice-only newest job is the shape the old REVERT test ran on."""
    mint = _review_plan(store)
    build, review = mint.ids["build"], mint.ids["review"]
    _job(store, build, lane="claude-sub")
    _slice_job(store, build, "land_slice", "succeeded")
    _todo_comment(store, build, "landed: abc1234 — the fix")
    _mark_done(store, build)
    _job(store, review, lane="claude-sub")
    store.add_tag(review, Tag.open("claimed-by:lane-claude-sub"), set_by="system")
    store.add_tag(
        review, Tag.closed("STATUS", "doing"), set_by="system", replace_prefix=True
    )
    if verdict:
        _todo_comment(store, review, verdict)
    return mint


def _blocked_by_ids(store: Store, src: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT dst_ref_id FROM links WHERE src_ref_id = %s "
            "AND relation = 'blocked-by' ORDER BY dst_ref_id",
            (src,),
        ).fetchall()
    return [int(r[0]) for r in rows]


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("review: LAND", ("LAND", "")),
        ("  review: land — clean", ("LAND", "clean")),
        (
            "review: FIX — a.py misses the empty case",
            ("FIX", "a.py misses the empty case"),
        ),
        ("review: FIX - one\n- two", ("FIX", "one\n- two")),
        ("review: REVERT – breaks the API", ("REVERT", "breaks the API")),
        ("handoff: review: LAND", ("LAND", "")),
        ("review: FIX", None),
        ("review: REVERT —", None),
        ("review: LANDING", None),
        ("review: looks fine", None),
        ("LAND", None),
    ],
)
def test_parse_verdict(text: str, want: tuple[str, str] | None) -> None:
    assert pd.parse_verdict(text) == want


def test_parse_verdict_accepts_readonly_marker() -> None:
    """gr573555 / td573921: the read-only reviewer's verdict line ends
    with ``(read-only, tests not run)``; the parser strips it wherever it
    ends a line, so it never lands in the findings or the rest."""
    marker = "(read-only, tests not run)"
    assert pd.parse_verdict(f"review: LAND {marker}") == ("LAND", "")
    assert pd.parse_verdict(f"handoff: review: LAND {marker}") == ("LAND", "")
    assert pd.parse_verdict(f"all done.review: LAND {marker}") == ("LAND", "")
    assert pd.parse_verdict(f"review: FIX — a.py misses the empty case {marker}") == (
        "FIX",
        "a.py misses the empty case",
    )
    assert pd.parse_verdict(f"review: REVERT — breaks the API {marker}") == (
        "REVERT",
        "breaks the API",
    )
    # Marker ends the first line; the narration after it stays the rest.
    assert pd.parse_verdict(f"review: LAND {marker}\nChecked the diff.") == (
        "LAND",
        "Checked the diff.",
    )


def test_parse_verdict_marker_is_not_findings() -> None:
    """The marker alone after FIX/REVERT is no reason: a verdict still
    needs its findings / its why — a marker-only REVERT must not read as
    a REVERT whose why is the marker."""
    assert pd.parse_verdict("review: FIX (read-only, tests not run)") is None
    assert pd.parse_verdict("review: REVERT (read-only, tests not run)") is None


def test_parse_verdict_marker_in_grammar_echo_is_not_a_verdict() -> None:
    """The new grammar echoed back — marker on each line, ``<…>``
    placeholders — is still no verdict."""
    echo = (
        "handoff: the brief's grammar:\n"
        "review: LAND (read-only, tests not run)\n"
        "review: FIX — <findings> (read-only, tests not run)\n"
        "review: REVERT — <why> (read-only, tests not run)\n"
        "Checking now."
    )
    assert pd.parse_verdict(echo) is None


def test_parse_verdict_disagreeing_verdict_lines_strictest_wins() -> None:
    """gr574380: verdict lines that disagree resolve strictest-first
    (REVERT > FIX > LAND), the winner's own line as the rest plus a note
    naming the other verdict; the DOTALL tail never swallows a later
    ``review:`` line. A winner with an empty rest is still a miss; lines
    that all agree keep the verdict."""
    for text in (
        "review: LAND\nreview: FIX — real bug in a.py",
        "review: FIX — real bug in a.py\nreview: LAND",
    ):
        got = pd.parse_verdict(text)
        assert got is not None and got[0] == "FIX"
        assert got[1] == "real bug in a.py (also a LAND line in the same message)"
    got = pd.parse_verdict("review: LAND\nreview: REVERT — breaks the API")
    assert got == (
        "REVERT",
        "breaks the API (also a LAND line in the same message)",
    )
    got = pd.parse_verdict("review: FIX — a.py\nreview: REVERT — b.py\nreview: LAND")
    assert got == ("REVERT", "b.py (also a FIX/LAND line in the same message)")
    assert pd.parse_verdict("review: FIX\nreview: LAND") is None
    # A placeholder line is a grammar echo, never a winner.
    assert pd.parse_verdict("review: LAND\nreview: FIX — <list>") is None
    assert pd.parse_verdict("review: LAND\nreview: REVERT") is None
    got = pd.parse_verdict("review: LAND\nreview: LAND")
    assert got is not None and got[0] == "LAND"


def test_parse_verdict_enclosed_lines_are_not_verdicts() -> None:
    """td591267: a ``review:`` token inside backticks or parens quotes the
    grammar even at a line start — it is filtered before the strictest
    pick, so a LAND followed by an enclosed multiline
    ``review: REVERT — example`` stays a LAND, never an actionable
    REVERT."""
    for text in (
        "review: LAND\n```\nreview: REVERT — example\n```",
        "review: LAND\n`the grammar says:\nreview: REVERT — example`",
        "review: LAND\n(quoting the grammar:\nreview: REVERT — example)",
        "review: LAND\n`review: REVERT — example`",
    ):
        got = pd.parse_verdict(text)
        assert got is not None and got[0] == "LAND", text
    # An enclosed placeholder cannot hijack a real disagreement either.
    assert pd.parse_verdict(
        "review: LAND\nreview: FIX — real bug\n`review: REVERT — <why>`"
    ) == ("FIX", "real bug (also a LAND line in the same message)")
    # A quoted verdict line alone is no verdict at all.
    assert pd.parse_verdict("```\nreview: REVERT — example\n```") is None


def test_parse_verdict_marker_edge_cases() -> None:
    """The marker is stripped wherever it ends a line — any case, an
    optional trailing period, CRLF endings — and a message that is only
    the marker (or empty) is no verdict."""
    assert pd.parse_verdict("(read-only, tests not run)") is None
    assert pd.parse_verdict("") is None
    assert pd.parse_verdict("review: LAND (READ-ONLY, TESTS NOT RUN)") == (
        "LAND",
        "",
    )
    assert pd.parse_verdict("review: LAND (read-only, tests not run).") == (
        "LAND",
        "",
    )
    assert pd.parse_verdict(
        "handoff: review: FIX — a.py misses the empty case (read-only, tests not run)"
    ) == ("FIX", "a.py misses the empty case")
    assert pd.parse_verdict(
        "handoff: review: REVERT — breaks the API (read-only, tests not run)"
    ) == ("REVERT", "breaks the API")
    # CRLF: the marker ends the line before the ``\r\n``.
    assert pd.parse_verdict("review: LAND (read-only, tests not run)\r\n") == (
        "LAND",
        "",
    )
    assert pd.parse_verdict("review: LAND (read-only, tests not run)\r\nChecked.") == (
        "LAND",
        "Checked.",
    )


# Verbatim glued tails of the hand-off comments the td573503/td573504
# reviewer jobs posted (jo573513 and jo573551 on td573503, jo573561 and
# jo573574 on td573504) — the exact texts the parser missed (gr573564).
_TD573503_JO573513 = """handoff: Let me also run the typecheck to complete verification.review: FIX — tests/test_work_board_queue.py::test_blocked_row_shows_after_marker fails on landed main: a quest whose only leaf is blocked-by reads `idle`, and `render_work` skips row expansion for idle quests (`work_view.py:823`), so `after td<N>` never reaches `search(view='work')` text. Fix the test by adding a free doable sibling leaf (e.g. a second leaf under the root) so the quest is `queued` and the blocked row's marker expands — same for any assertion that needs the text view.

Checked: diff of 17897effe vs the brief item by item. Implementation is faithful — batched `_open_blockers`/`_job_fields` once per `_render_root`, hold precedence waiting-for → ask-user → stored `gate_result` (no gate evaluate), preview gated on plan-tagged block + leaf + open + unclaimed + no jobs + node in ("","task") via `ladder()`, `_job_line` leading text untouched with ` · lane=… model=… host=…` appended, template edit confined to the item cell. Verified in-container: ruff clean, mypy clean (4339 files), 53/53 existing tests pass, 6/7 new tests pass — only the one assertion above is red. No prohibited paths touched."""

_TD573503_JO573551 = """handoff: Running the brief's other test files to confirm nothing else is red on landed main.review: FIX — tests/test_work_board_queue.py::test_blocked_row_shows_after_marker fails on landed main (reproduced in-container): a quest whose only leaf is blocked-by reads `idle`, and `render_work` skips row expansion for idle quests (`work_view.py:823`), so `after td<N>` never reaches `search(view='work')` text — only that one assertion is red; the model-level `row.after` and the HTML `href="/eye/td<N>"` assertions pass. Fix the test by adding a free doable sibling leaf under the root so the quest is `queued` and the blocked row's marker expands.

Checked `17897effe` against the brief item by item: batched `_open_blockers`/`_job_fields` once per `_render_root`, hold precedence waiting-for → ask-user → stored `gate_result` (no gate evaluate), preview gated on plan-tagged block + leaf + open + unclaimed + zero jobs + node in ("","task") via `ladder()`, `_job_line` leading text byte-preserved with ` · lane=… model=… host=…` appended, template edit confined to the item cell. Verified in-container: 6/7 new tests pass, 53/53 existing tests pass, no prohibited paths touched."""

_TD573504_JO573561 = """handoff: Waiting on mypy.Typecheck clean. Two last API checks: `current_revision_context` and `actor_for` (the logbook actor path).review: LAND

Checked `b98653e38` against the td573297 brief, end to end:

- **Handler** (`quest.py`): `edit_modes` extended to `("replace","pause","resume")`; pause/resume branches reject `text=`/`meta=`, resolve the live ref, and delegate. `_strategic_roots` mirrors `work_view._todo_servers` (`links_for(direction="in", relation="serves")` + `rotation_root` meta). Pause skips done/won't-do/alread"""

_TD573504_JO573574 = """handoff: Docs updated. Checking the two nits the prior review noted.review: LAND

Independently re-verified `b98653e38` against the td573297 brief: the prior review's claims hold. `QuestHandler.edit` accepts pause/resume (validated via `edit_modes` at dispatch layer, `runtime/dispatch.py:3862`), rejects `text=`/`meta=`, stamps `STATUS:paused`+`paused-by:quest` on live `serves`-linked `rotation_root` todos through `TodoHandler.tag`, and is idempotent. Closed-axis `"""


@pytest.mark.parametrize(
    ("text", "verdict", "rest_prefix"),
    [
        (
            _TD573503_JO573513,
            "FIX",
            "tests/test_work_board_queue.py::test_blocked_row_shows_after_marker "
            "fails on landed main: a quest whose only leaf is blocked-by reads `idle`",
        ),
        (
            _TD573503_JO573551,
            "FIX",
            "tests/test_work_board_queue.py::test_blocked_row_shows_after_marker "
            "fails on landed main (reproduced in-container)",
        ),
        (_TD573504_JO573561, "LAND", "Checked `b98653e38`"),
        (_TD573504_JO573574, "LAND", "Independently re-verified `b98653e38`"),
    ],
)
def test_parse_verdict_verbatim_glued_tails(
    text: str, verdict: str, rest_prefix: str
) -> None:
    got = pd.parse_verdict(text)
    assert got is not None
    assert got[0] == verdict
    assert got[1].startswith(rest_prefix)


@pytest.mark.parametrize(
    ("text", "want"),
    [
        # td573578, coordinator ruling: a fallback token counts only at a
        # line start or right after sentence-final punctuation (the glued
        # shape ``...main.review: FIX — ...``), outside backticks/parens,
        # with a non-placeholder rest; REVERT never comes from a token.
        ("all done.review: LAND", ("LAND", "")),
        ("wrapped up\nreview: LAND", ("LAND", "")),
        # Markdown line-start markers still count as a line start.
        ("**review: LAND**", ("LAND", "**")),
        ("- review: FIX — x", ("FIX", "x")),
        ("all checked.\n* review: FIX — x", ("FIX", "x")),
        ("all checked.\n> review: LAND", ("LAND", "")),
        ("## findings\n# review: LAND", ("LAND", "")),
        # … but a marker that does not reach a line start is narration.
        ("saw it - review: LAND", None),
        # The grammar echo: every token sits inside backticks or carries a
        # <...> placeholder rest — none is a verdict.
        (
            "Brief wants `review: LAND` (clean) | `review: FIX — <list>` | "
            "`review: REVERT — <why>`. Checking now.",
            None,
        ),
        # … and the same echo un-backticked, one template per line.
        (
            "handoff: the brief's grammar:\nreview: LAND (clean)\n"
            "review: FIX — <list>\nreview: REVERT — <why>\nChecking now.",
            None,
        ),
        # A LAND glossed ``(clean)`` or continued ``|`` is the template.
        ("all done.review: LAND (clean)", None),
        ("one of:\nreview: LAND | review: FIX — <list>", None),
        # A placeholder FIX/REVERT template line suppresses LANDs nearby.
        ("echo below.\nreview: FIX — <list>\nreview: LAND", None),
        # A real FIX; the later LAND is quoted inside parens.
        (
            "Checked it.review: FIX — a.py is wrong (the prior review said "
            "review: LAND)",
            ("FIX", "a.py is wrong (the prior review said review: LAND)"),
        ),
        # A mid-sentence token is narration, not a verdict.
        ("I will post review: LAND if tests pass.", None),
        # Surviving candidates disagree: a reasoned FIX beats a LAND.
        (
            "Read it.review: LAND. Actually.review: FIX — a.py is wrong",
            ("FIX", "a.py is wrong"),
        ),
        # … but a bare FIX disagrees with a LAND: a miss, never a guess.
        ("Read it.review: LAND. Actually.review: FIX", None),
        # REVERT needs the line-start comment, never a glued token.
        ("all done.review: REVERT — breaks the API", None),
        ("handoff: review: REVERT — breaks the API", ("REVERT", "breaks the API")),
        ("all done.review: FIX", None),
        ("…the review: landed step", None),
    ],
)
def test_parse_verdict_glued_narration(text: str, want: tuple[str, str] | None) -> None:
    assert pd.parse_verdict(text) == want


def test_review_land_closes_the_reviewer(store: Store) -> None:
    mint = _reviewed(store, "review: LAND")
    review = mint.ids["review"]
    res = pd.run_pass(store)
    assert res.reviews == 1
    assert res.summary().endswith("reviews=1")
    assert _status(store, review) == "done"
    assert not any(v.startswith("claimed-by:") for v in _open_tags(store, review))
    assert _meta(store, review)["review"]["verdict"] == "LAND"
    # Nothing minted; the root phase closes over build + review.
    assert _children(store, mint.root_id) == [mint.ids["build"], review]
    assert _status(store, mint.root_id) == "done"
    # The judged job is not judged again.
    assert pd.run_pass(store).reviews == 0


def test_review_land_records_tests_not_run(store: Store) -> None:
    """gr573555: the reviewer is read-only — a LAND is a diff read, never
    test evidence. ``meta.review`` stamps ``tests_run: False`` and the
    pass note carries the marker."""
    mint = _reviewed(store, "review: LAND (read-only, tests not run)")
    build, review = mint.ids["build"], mint.ids["review"]
    res = pd.run_pass(store)
    assert res.reviews == 1
    assert _status(store, review) == "done"
    meta = _meta(store, review)["review"]
    assert meta["verdict"] == "LAND"
    assert meta["tests_run"] is False
    assert any(
        n.endswith(f"review LAND for td{build} (read-only, tests not run); closed")
        for n in res.notes
    )


def test_review_fix_mints_one_fix_task_under_the_root(store: Store) -> None:
    mint = _reviewed(store, "review: FIX — a.py misses the empty case")
    build, review = mint.ids["build"], mint.ids["review"]
    res = pd.run_pass(store)
    assert res.reviews == 1
    assert _status(store, review) == "done"
    (fix,) = [c for c in _children(store, mint.root_id) if c not in (build, review)]
    ref = store.get_ref(kind="todo", id=fix)
    assert ref is not None
    assert ref.title == f"Fix review findings for td{build}"
    fix_meta = dict(ref.meta or {})
    fix_meta.pop("filed_by", None)  # the server-side origin stamp is not under test
    assert fix_meta == {"op": "run_slice"}
    assert f"plan:{mint.root_id}" in _open_tags(store, fix)
    body = [
        c.text
        for c in store.chunks.list_chunks_for_ref(fix)
        if c.chunk_kind == "todo_body"
    ]
    assert body and str(body[0]).startswith("a.py misses the empty case")
    assert _blocked_by_ids(store, fix) == []
    assert _meta(store, review)["review"] == {
        "verdict": "FIX",
        "job": _meta(store, review)["review"]["job"],
        "at": _meta(store, review)["review"]["at"],
        "task": fix,
        "tests_run": False,
    }
    # The root stays open for the fix, which is dispatched in the same pass.
    assert _status(store, mint.root_id) == "open"
    assert res.dispatched == 1
    assert len(_jobs_under(store, fix)) == 1


def test_review_revert_mints_a_revert_and_requeues_the_build(store: Store) -> None:
    mint = _reviewed(store, "review: REVERT — breaks the public API")
    build, review = mint.ids["build"], mint.ids["review"]
    res = pd.run_pass(store)
    assert res.reviews == 1
    (revert,) = [c for c in _children(store, mint.root_id) if c not in (build, review)]
    ref = store.get_ref(kind="todo", id=revert)
    assert ref is not None and ref.title == "Revert abc1234 on main"
    # The build is reopened behind the revert, with the verdict as its handoff.
    assert _status(store, build) == "open"
    assert _blocked_by_ids(store, build) == [revert]
    assert _todo_comments(store, build)[-1].startswith(
        "handoff: review: REVERT — breaks the public API"
    )
    assert isinstance(_meta(store, build)["rearmed_after_job"], int)
    # The reviewer stays open (claim dropped) to review the rebuild.
    assert _status(store, review) == "open"
    assert not any(v.startswith("claimed-by:") for v in _open_tags(store, review))
    assert _meta(store, review)["review"]["verdict"] == "REVERT"
    # Only the revert is dispatched: the build waits on it, the review on the build.
    assert res.dispatched == 1
    assert len(_jobs_under(store, revert)) == 1
    assert len(_jobs_under(store, build)) == 1
    # Idempotent: the judged job is not acted on twice.
    res = pd.run_pass(store)
    assert (res.reviews, res.dispatched) == (0, 0)
    # The revert lands: the build is redispatched on a fresh retry budget.
    _finish_live_jobs(store)
    assert pd.run_pass(store).dispatched == 1
    assert len(_jobs_under(store, build)) == 2


def test_review_revert_reopen_survives_the_land_backstop(store: Store) -> None:
    """gr586688: on the production shape — the reviewed todo's newest job
    its succeeded ``land_slice`` — the REVERT reopen held only until
    ``_step_phases`` ran later in the same pass: its task backstop closed
    the todo again on the OLD land, the reviewer unblocked and re-judged
    the same sha, and every pass minted another revert task. The re-arm
    floor the REVERT stamps now keeps the todo open for the rebuild."""
    mint = _reviewed_landed(store, "review: REVERT — breaks the public API")
    build, review = mint.ids["build"], mint.ids["review"]
    land = _jobs_under(store, build)[-1][0]
    res = pd.run_pass(store)
    assert res.reviews == 1
    (revert,) = [c for c in _children(store, mint.root_id) if c not in (build, review)]
    # The reopen holds the pass: open, blocked-by the revert task, floored
    # on the old land — the backstop's note says why it did not close.
    assert _status(store, build) == "open"
    assert _blocked_by_ids(store, build) == [revert]
    assert _meta(store, build)["rearmed_after_job"] == land
    assert (
        f"task td{build} held open: newest land_slice job:{land} "
        f"is at or below re-arm floor {land}" in res.notes
    )
    # Only the revert task is dispatched — the build waits on it (no
    # rebuild while the revert task is open), the review on the build.
    assert res.dispatched == 1
    assert len(_jobs_under(store, revert)) == 1
    assert len(_jobs_under(store, build)) == 2
    # The judged job is not re-judged and the review does not re-run on
    # the same sha: after three passes exactly one revert task exists.
    for _ in range(2):
        res = pd.run_pass(store)
        assert (res.reviews, res.dispatched) == (0, 0)
    assert [c for c in _children(store, mint.root_id) if c not in (build, review)] == [
        revert
    ]
    assert _status(store, build) == "open"
    assert len(_jobs_under(store, review)) == 1


def test_review_revert_rebuild_land_closes_and_the_review_reruns(
    store: Store,
) -> None:
    """The floor supersedes only the jobs that existed at the re-arm: the
    rebuild's fresh succeeded ``land_slice`` (id above the floor) closes
    the todo through the backstop as before, and the held reviewer runs
    exactly once on the new land."""
    mint = _reviewed_landed(store, "review: REVERT — breaks the public API")
    build, review = mint.ids["build"], mint.ids["review"]
    res = pd.run_pass(store)
    (revert,) = [c for c in _children(store, mint.root_id) if c not in (build, review)]
    # One dispatch this pass — the revert task; no rebuild under the
    # build while the revert task is still open.
    assert res.dispatched == 1
    assert len(_jobs_under(store, revert)) == 1
    assert len(_jobs_under(store, build)) == 2
    # The revert lands: the build is redispatched on its fresh budget —
    # the one dispatch is a new run_slice under it.
    _finish_live_jobs(store)  # the revert job succeeds, the task closes
    res = pd.run_pass(store)
    assert res.dispatched == 1
    assert _jobs_under(store, build)[-1][1]["job_type"] == "run_slice"
    # The rebuild lands: a fresh succeeded land_slice above the floor.
    _finish_live_jobs(store, close_task=False)
    _slice_job(store, build, "land_slice", "succeeded")
    res = pd.run_pass(store)
    assert _status(store, build) == "done"
    # The reviewer, held through the rebuild, re-runs exactly once.
    assert res.dispatched == 1
    assert len(_jobs_under(store, review)) == 2
    _finish_live_jobs(store, close_task=False)
    _todo_comment(store, review, "review: LAND")
    res = pd.run_pass(store)
    assert res.reviews == 1
    assert _status(store, review) == "done"
    assert len(_jobs_under(store, review)) == 2


def test_landed_task_without_a_rearm_closes(store: Store) -> None:
    """The backstop's normal use, unchanged: a succeeded ``land_slice``
    with no re-arm floor still closes the task."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _slice_job(store, task, "run_slice", "succeeded")
    _slice_job(store, task, "land_slice", "succeeded")
    res = pd.run_pass(store)
    assert _status(store, task) == "done"
    assert any(f"task td{task} closed: land_slice" in n for n in res.notes)


def test_ask_rearm_on_a_landed_task_closes_it_without_a_rebuild(
    store: Store,
) -> None:
    """``_step_rearm`` must not floor a task whose newest ended job is a
    succeeded ``land_slice``: the slice already landed, there is nothing
    to retry — the backstop closes the task the same pass and no rebuild
    is dispatched. Flooring it here once held the task open and minted a
    second ``run_slice``: a duplicate build of landed work (gr586688
    review)."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(
        store,
        task,
        "touched human-only",
        meta={"human_only_hit": ["deploy/inventory/x.yml"]},
    )
    res = pd.run_pass(store)
    assert res.asks == 1
    (ask,) = [c for c in _children(store, mint.root_id) if c != task]
    # A hand-run land succeeds while the ask is open; the answer lands
    # before the closing tick. The task closes on the land, not on a
    # rebuild: no floor, no new job — ``rearmed_by_ask`` is still stamped
    # (the once-per-ask record is unchanged).
    land = _slice_job(store, task, "land_slice", "succeeded")
    _answer_ask(store, ask, "retry")
    res = pd.run_pass(store)
    assert res.dispatched == 0
    assert _status(store, task) == "done"
    meta = _meta(store, task)
    assert "rearmed_after_job" not in meta
    assert meta["rearmed_by_ask"] == ask
    assert _jobs_under(store, task)[-1][0] == land


def test_ask_rearm_on_a_failed_attempt_still_stamps_the_floor(
    store: Store,
) -> None:
    """The floor keeps its use where it belongs: an answered ask on a
    task whose newest ended job is a FAILED attempt stamps
    ``meta.rearmed_after_job`` on that failure — the rebuild dispatches
    and a fresh land above the floor closes the task, as before."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(
        store,
        task,
        "touched human-only",
        meta={"human_only_hit": ["deploy/inventory/x.yml"]},
    )
    res = pd.run_pass(store)
    assert res.asks == 1
    (ask,) = [c for c in _children(store, mint.root_id) if c != task]
    # A second attempt dies while the ask is open: the answer floors the
    # re-arm on it (unchanged behaviour — a failed newest job still
    # means "retry from here").
    failed = _failed_job(store, task, "hit the same wall")
    _answer_ask(store, ask, "retry")
    res = pd.run_pass(store)
    assert _meta(store, task)["rearmed_after_job"] == failed
    assert _status(store, task) == "open"
    assert res.dispatched == 1
    # Rebuilt and re-landed: a fresh land above the floor closes it.
    _finish_live_jobs(store, close_task=False)
    _slice_job(store, task, "land_slice", "succeeded")
    res = pd.run_pass(store)
    assert _status(store, task) == "done"


def test_review_revert_reopen_holds_for_an_s1b_minted_reviewer(
    store: Store,
) -> None:
    """The S1b engine-review shape (``templates.mint_reviewer``, td584381):
    a reviewer minted under the root over an already-landed todo, blocked
    by it — its REVERT holds the todo open through the same floor."""

    def build_(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        m.node("build", "Build it", {"op": "run_slice"}, body="Build it.")

    mint = T._run(store, None, build_)
    build = mint.ids["build"]
    _job(store, build, lane="claude-sub")
    _slice_job(store, build, "land_slice", "succeeded")
    _todo_comment(store, build, "landed: abc1234 — the fix")
    _mark_done(store, build)
    review = T.mint_reviewer(store, mint.root_id, build)
    _job(store, review, lane="devin")
    _todo_comment(store, review, "review: REVERT — breaks the public API")
    land = _jobs_under(store, build)[-1][0]
    res = pd.run_pass(store)
    assert res.reviews == 1
    (revert,) = [c for c in _children(store, mint.root_id) if c not in (build, review)]
    assert _status(store, build) == "open"
    assert _blocked_by_ids(store, build) == [revert]
    assert _meta(store, build)["rearmed_after_job"] == land
    assert res.dispatched == 1
    for _ in range(2):
        res = pd.run_pass(store)
        assert (res.reviews, res.dispatched) == (0, 0)
    assert [c for c in _children(store, mint.root_id) if c not in (build, review)] == [
        revert
    ]


def _gated_review_plan(store: Store) -> T.PlanMint:
    """The gripe-fix shape: build <- review, build <- gate. Two sha-in-prod
    gates, both already open (done): one on the sha the build landed
    (``abc1234``), one on an unrelated sha."""

    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        b = m.node("build", "Build it", {"op": "run_slice"}, body="Build it.")
        m.node(
            "review",
            "Review it",
            T._reviewer(b),
            body=T._reviewer_brief(b),
            blocked_by=("build",),
        )
        for key, sha in (("gate", "abc1234"), ("other", "deadbee")):
            m.node(
                key,
                f"Wait for {key}",
                {"gate": {"kind": "sha-in-prod", "args": {"sha": sha}}},
                blocked_by=("build",),
            )

    return T._run(store, None, build)


def _gate_sha(store: Store, gate: int) -> str:
    return str(_meta(store, gate)["gate"]["args"]["sha"])


def _revert_gated(store: Store) -> T.PlanMint:
    mint = _gated_review_plan(store)
    build, review = mint.ids["build"], mint.ids["review"]
    _job(store, build, lane="claude-sub")
    # 12-hex sha on the comment, 7-hex prefix on the gate: a prefix match.
    _todo_comment(store, build, "landed: abc1234def56 — the fix")
    _mark_done(store, build, mint.ids["gate"], mint.ids["other"])
    _job(store, review, lane="claude-sub")
    store.add_tag(review, Tag.open("claimed-by:lane-claude-sub"), set_by="system")
    store.add_tag(
        review, Tag.closed("STATUS", "doing"), set_by="system", replace_prefix=True
    )
    _todo_comment(store, review, "review: REVERT — breaks the public API")
    return mint


def test_review_revert_resets_the_sha_gate_on_the_reverted_sha(store: Store) -> None:
    mint = _revert_gated(store)
    gate, other = mint.ids["gate"], mint.ids["other"]
    res = pd.run_pass(store)
    assert res.reviews == 1
    # The gate that had opened on the reverted sha is pending again and open
    # for business (not done), with the reason on it.
    assert _gate_sha(store, gate) == T.PENDING_SHA
    assert _status(store, gate) == "open"
    assert _todo_comments(store, gate)[-1] == "sha reset after review REVERT"
    assert any(f"gate td{gate}" in n and "reset" in n for n in res.notes)
    # A gate on another sha is left alone.
    assert _gate_sha(store, other) == "deadbee"
    assert _status(store, other) == "done"
    # Idempotent: the judged job is not acted on twice.
    pd.run_pass(store)
    assert _gate_sha(store, gate) == T.PENDING_SHA
    assert len([c for c in _todo_comments(store, gate) if "sha reset" in c]) == 1


def test_review_revert_gate_reset_is_read_only_in_a_dry_run(store: Store) -> None:
    mint = _revert_gated(store)
    gate = mint.ids["gate"]
    res = pd.run_pass(store, dry_run=True, reason="params.dry_run")
    assert res.reviews == 1
    assert any(f"gate td{gate}" in n for n in res.notes)
    assert _gate_sha(store, gate) == "abc1234"
    assert _status(store, gate) == "done"


def test_review_without_verdict_retries_once_then_rescues(
    store: Store,
) -> None:
    """A reviewer leaving no verdict retries on the picked lane (a format
    miss never climbs, gr573564); at the picked lane's rung cap — two on
    devin under the real table — the node enters rescue under class
    ``review``, no ask (td584381 S1a F3/F5: the old single-lane switch
    hid the full-table cap bug)."""
    mint = _reviewed(store, "review: looks fine to me")
    review = mint.ids["review"]
    res = pd.run_pass(store)
    assert (res.reviews, res.asks) == (0, 0)
    assert _meta(store, review)["review"]["verdict"] is None
    assert "malformed verdict" in _todo_comments(store, review)[-1]
    # One retry, pinned to the review pick. The builder ran on the Claude
    # lane, so the cross-vendor rule picks the first other-vendor lane in
    # the table: devin (it fits `review`), ahead of codex.
    assert res.dispatched == 1
    (_old, (retry, meta)) = _jobs_under(store, review)
    assert meta["params"]["todo_id"] == review
    assert meta["params"]["lane"] == "devin"
    assert not any("shares the builder's vendor" in n for n in res.notes)
    # The retry posts nothing either: devin's two rungs are spent — the
    # node enters rescue, no ask.
    store.add_tag(
        retry, Tag.closed("STATUS", "succeeded"), set_by="system", replace_prefix=True
    )
    res = pd.run_pass(store)
    assert (res.reviews, res.dispatched, res.asks, res.rescued) == (0, 0, 0, 1)
    # The verdict-miss note is posted at judging; the rescue evidence
    # comment lands on top of it in the same pass.
    assert any(
        "no `review:` comment" in c or "malformed verdict" in c
        for c in _todo_comments(store, review)
    )
    assert "rescue:review" in _open_tags(store, review)
    assert [c for c in _children(store, mint.root_id) if "ask" in _meta(store, c)] == []


def test_review_cap_is_the_picked_lanes_rungs(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td584381 S1a F3: the reviewer's cap is the lane ``_review_lane``
    picked — under the real table a second no-verdict run on devin is
    the top rung (swe-2-max), so the node enters rescue; it does not
    ride the full review-class table (devin+codex+claude-sub)."""
    use_lanes(monkeypatch)  # the real multi-lane seed table
    mint = _reviewed(store, "review: looks fine to me")
    review = mint.ids["review"]
    res = pd.run_pass(store)
    # A format miss retries on the picked lane: devin — the builder's
    # vendor (anthropic) is excluded.
    assert res.dispatched == 1
    (_old, (retry, meta)) = _jobs_under(store, review)
    assert meta["params"]["lane"] == "devin"
    store.stamp_ref_meta(retry, {"lane": "devin"})
    _set_job_status(store, retry, "succeeded")
    res = pd.run_pass(store)
    # Two attempts on devin's two rungs spent the cap — rescue, never a
    # third attempt on a codex/claude-sub rung of the class table.
    assert (res.dispatched, res.asks, res.rescued) == (0, 0, 1)
    assert "rescue:review" in _open_tags(store, review)
    assert [c for c in _children(store, mint.root_id) if "ask" in _meta(store, c)] == []


def test_review_reads_a_glued_handoff(store: Store) -> None:
    """td573578: a ``handoff:`` that glues ``review: FIX — …`` to the
    narration is the verdict (the LAST token in the comment). The fixture
    is the verbatim td573503 review tail the parser missed (gr573564)."""
    mint = _reviewed(store, _TD573503_JO573551)
    build, review = mint.ids["build"], mint.ids["review"]
    res = pd.run_pass(store)
    assert res.reviews == 1
    assert _status(store, review) == "done"
    (fix,) = [c for c in _children(store, mint.root_id) if c not in (build, review)]
    ref = store.get_ref(kind="todo", id=fix)
    assert ref is not None
    assert ref.title == f"Fix review findings for td{build}"
    body = [
        c.text
        for c in store.chunks.list_chunks_for_ref(fix)
        if c.chunk_kind == "todo_body"
    ]
    assert body and str(body[0]).startswith(
        "tests/test_work_board_queue.py::test_blocked_row_shows_after_marker "
        "fails on landed main (reproduced in-container)"
    )


def test_review_line_comment_beats_a_glued_handoff_token(store: Store) -> None:
    """Coordinator ruling: a comment STARTING ``review:`` always wins over
    a token inside a ``handoff:`` — here posted newer, so a newest-first
    scan alone would read the LAND."""
    mint = _reviewed(store, "review: FIX — a.py misses the empty case")
    build, review = mint.ids["build"], mint.ids["review"]
    _todo_comment(store, review, "handoff: narration done.review: LAND")
    res = pd.run_pass(store)
    assert res.reviews == 1
    assert _status(store, review) == "done"
    assert _meta(store, review)["review"]["verdict"] == "FIX"
    (fix,) = [c for c in _children(store, mint.root_id) if c not in (build, review)]
    ref = store.get_ref(kind="todo", id=fix)
    assert ref is not None and ref.title == f"Fix review findings for td{build}"


def test_review_glued_revert_is_a_miss(store: Store) -> None:
    """A REVERT glued into a ``handoff:`` is never taken — the reviewer
    retries instead of reverting a good land."""
    mint = _reviewed(store, "handoff: checked the diff.review: REVERT — breaks it")
    review = mint.ids["review"]
    res = pd.run_pass(store)
    assert res.reviews == 0
    assert _status(store, review) == "open"
    assert _meta(store, review)["review"]["verdict"] is None
    # No revert (or fix) task minted.
    assert sorted(_children(store, mint.root_id)) == sorted([mint.ids["build"], review])


def test_review_enclosed_revert_is_not_actionable(store: Store) -> None:
    """td591267: a ``review: REVERT`` at a line start inside a backtick
    span quotes the grammar — the real LAND verdict rules and the
    reviewer closes; no revert task is minted."""
    mint = _reviewed(store, "review: LAND\n```\nreview: REVERT — example\n```")
    build, review = mint.ids["build"], mint.ids["review"]
    res = pd.run_pass(store)
    assert res.reviews == 1
    assert _status(store, review) == "done"
    assert _meta(store, review)["review"]["verdict"] == "LAND"
    assert sorted(_children(store, mint.root_id)) == sorted([build, review])


def test_review_miss_retry_is_not_escalated(store: Store) -> None:
    """gr573564: a review job that left no readable verdict retries on the
    lane ``_review_lane`` picked — no climb, no ``swe-2-max`` model pin."""
    mint = _reviewed(store, "review: looks fine to me")
    review = mint.ids["review"]
    res = pd.run_pass(store)
    assert res.dispatched == 1
    (_old, (retry, meta)) = _jobs_under(store, review)
    assert meta["params"]["todo_id"] == review
    assert meta["params"]["lane"] == "devin"
    assert "model" not in meta["params"]


def test_review_ignores_the_dispatchers_own_grammar_note(store: Store) -> None:
    """The dispatcher's retry note quotes the grammar — a comment that is
    not ``review:``/``handoff:``-prefixed is never scanned for a token."""
    mint = _reviewed(
        store,
        "plan_dispatch: job:9 succeeded with no `review:` comment; want "
        "review: LAND (clean) | review: FIX — <findings>",
    )
    review = mint.ids["review"]
    res = pd.run_pass(store)
    assert res.reviews == 0
    assert _meta(store, review)["review"]["verdict"] is None
    assert _status(store, review) == "open"


def test_dispatch_orders_by_prio_across_roots(store: Store) -> None:
    older = _one_task_plan(store)  # no prio anywhere: 5
    newer = _one_task_plan(store)
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET prio = 1 WHERE ref_id = %s", (newer.root_id,))
    res = pd.run_pass(store, dry_run=True)
    order = [n.split(":")[0] for n in res.notes if "would dispatch" in n]
    # The prio-1 root's task (inherited prio) goes before the older prio-5 one.
    assert order == [f"td{newer.ids['task']}", f"td{older.ids['task']}"]


def _pin_codex_and_sub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the table to the seed's codex and claude-sub lanes, the two
    vendors the review-vendor tests below were written against (the seed
    table also carries devin since td562568, which would take every review)."""
    codex, sub = seed_lane("codex"), seed_lane("claude-sub")
    use_lanes(monkeypatch, codex, sub)


def test_review_excludes_every_build_vendor_then_the_original_builder(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td565332 (2026-11-05): codex built the fix, a sonnet retry repaired the
    lint; reading only the newest job sent the review to codex, which then
    reviewed its own build. With both enabled vendors on the branch the
    review goes to the lane that is not the original builder's."""
    _pin_codex_and_sub(monkeypatch)
    mint = _review_plan(store)
    build, review = mint.ids["build"], mint.ids["review"]
    _job(store, build, status="failed", lane="codex")
    _job(store, build, lane="claude-sub")
    _todo_comment(store, build, "landed: abc1234 — the fix")
    _mark_done(store, build)
    plan = pd._load(store)
    node = plan.nodes[review]
    assert pd._builder_vendors(plan, node, pd._lanes.load_lanes(store)) == (
        "openai",
        "anthropic",
    )
    res = pd.run_pass(store)
    assert res.dispatched == 1
    ((_job_id, meta),) = _jobs_under(store, review)
    assert meta["params"]["lane"] == "claude-sub"
    assert not any("shares the builder's vendor" in n for n in res.notes)


def test_review_counts_only_jobs_that_left_commits(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUILDER 2026-11-05: a claude-sub attempt that died with no commit,
    then a codex build — counting the dead attempt would make anthropic the
    "original builder" and codex would review its own work. Only committing
    jobs are builders; the review goes to claude-sub."""
    _pin_codex_and_sub(monkeypatch)
    mint = _review_plan(store)
    build, review = mint.ids["build"], mint.ids["review"]
    _job(store, build, status="failed", lane="claude-sub", commits=[])
    _job(store, build, lane="codex", commits=["abc1234 fix(x): the fix"])
    _todo_comment(store, build, "landed: abc1234 — the fix")
    _mark_done(store, build)
    plan = pd._load(store)
    assert pd._builder_vendors(
        plan, plan.nodes[review], pd._lanes.load_lanes(store)
    ) == ("openai",)
    res = pd.run_pass(store)
    assert res.dispatched == 1
    ((_job_id, meta),) = _jobs_under(store, review)
    assert meta["params"]["lane"] == "claude-sub"
    assert not any("shares the builder's vendor" in n for n in res.notes)


def test_review_shares_a_vendor_only_when_nothing_else_is_enabled(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = L.Lane("claude-sub", "claude_p", "s", 2, frozenset({"*"}), vendor="anthropic")
    use_lanes(monkeypatch, sub)
    mint = _review_plan(store)
    build, review = mint.ids["build"], mint.ids["review"]
    _job(store, build, lane="claude-sub")
    _todo_comment(store, build, "landed: abc1234 — the fix")
    _mark_done(store, build)
    res = pd.run_pass(store)
    assert res.dispatched == 1
    ((_job_id, meta),) = _jobs_under(store, review)
    assert meta["params"]["lane"] == "claude-sub"
    assert any("shares the builder's vendor 'anthropic'" in n for n in res.notes)


def test_pick_lane_excludes_the_builder_vendor() -> None:
    from precis.plans.lanes import Lane, pick_lane

    sub = Lane("sub", "claude_p", "s", 2, frozenset({"*"}), vendor="anthropic")
    codex = Lane("codex", "codex_exec", "c", 1, frozenset({"review"}), vendor="openai")
    off = Lane(
        "codex", "codex_exec", "c", 1, frozenset({"review"}), False, vendor="openai"
    )
    pick = pick_lane(
        None, "review", lanes=(sub, codex), counts={}, exclude_vendor="anthropic"
    )
    assert pick is codex
    # Only the builder's vendor enabled: fall back to it.
    pick = pick_lane(
        None, "review", lanes=(sub, off), counts={}, exclude_vendor="anthropic"
    )
    assert pick is sub
    # The other vendor's lane is full: wait, do not bypass the rule.
    assert (
        pick_lane(
            None,
            "review",
            lanes=(sub, codex),
            counts={"codex": 1},
            exclude_vendor="anthropic",
        )
        is None
    )
    # Without the exclusion the pick is unchanged (table order).
    assert pick_lane(None, "review", lanes=(sub, codex), counts={}) is sub


# ── td543917: placement capabilities ───────────────────────────────


def _advertise(store: Store, host: str, slots: dict[str, int | None]) -> None:
    store.sync_host_resource_slots(host, slots)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat (host, ts) VALUES (%s, now()) "
            "ON CONFLICT (host) DO UPDATE SET ts = EXCLUDED.ts",
            (host,),
        )
        conn.commit()


def _backdate(store: Store, ref_id: int, minutes: int) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - make_interval(mins => %s) "
            "WHERE ref_id = %s",
            (minutes, ref_id),
        )
        conn.commit()


def _unplaced_notes(res: pd.PassResult) -> list[str]:
    return [n for n in res.notes if "no host advertises" in n]


def test_queued_slice_with_no_capable_host_gets_a_note(store: Store) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    job = _slice_job(store, task, "run_slice", "queued")
    _backdate(store, job, 15)
    # Capabilities split across two hosts: neither can place the slice.
    _advertise(store, "node-a", {"repo_checkout": 1})
    _advertise(store, "node-b", {"claude_bin": 1, "git": 1})
    res = pd.run_pass(store)
    assert _unplaced_notes(res) == [
        f"td{task}: job jo{job} queued 15 min — no host advertises "
        "{claude_bin, git, repo_checkout}"
    ]
    # A job still queued is no attempt: no ask, no second dispatch.
    assert (res.asks, res.dispatched) == (0, 0)
    assert _status(store, task) == "open"


def test_queued_slice_note_waits_ten_minutes(store: Store) -> None:
    mint = _one_task_plan(store)
    job = _slice_job(store, mint.ids["task"], "run_slice", "queued")
    _backdate(store, job, 5)
    assert _unplaced_notes(pd.run_pass(store)) == []


def test_queued_slice_with_a_capable_host_gets_no_note(store: Store) -> None:
    mint = _one_task_plan(store)
    job = _slice_job(store, mint.ids["task"], "run_slice", "queued")
    _backdate(store, job, 30)
    _advertise(store, "node-a", {"repo_checkout": 1, "claude_bin": 1, "git": 1})
    assert _unplaced_notes(pd.run_pass(store)) == []


# ── Tick lock ─────────────────────────────────────────────────────


def _plan_state(store: Store, ids: dict[str, int]) -> dict[str, str | None]:
    return {k: _status(store, v) for k, v in ids.items()}


def test_a_held_tick_lock_skips_the_pass_untouched(store: Store) -> None:
    mint = _small_plan(store)
    before = _plan_state(store, mint.ids)
    jobs_before = _all_jobs(store)
    with store.pool.connection() as other:
        other.execute("SELECT pg_advisory_xact_lock(%s)", (pd.TICK_LOCK_KEY,))
        res = pd.run_pass(store)
        assert res.skipped == "another tick holds the lock"
        assert res.counts()["skipped"] == res.skipped
        assert res.summary() == "plan_dispatch: skipped — another tick holds the lock"
        assert res.render().startswith("plan_dispatch: skipped")
        assert (res.roots, res.dispatched, res.loops_minted, res.asks) == (0, 0, 0, 0)
        assert _plan_state(store, mint.ids) == before
        assert _all_jobs(store) == jobs_before
        other.rollback()  # ends the transaction: the lock is released
    res = pd.run_pass(store)
    assert res.skipped is None
    assert "skipped" not in res.counts()
    assert res.dispatched == 2
    assert _all_jobs(store) == jobs_before + 2


def test_a_dry_run_also_takes_the_tick_lock(store: Store) -> None:
    _small_plan(store)
    with store.pool.connection() as other:
        other.execute("SELECT pg_advisory_xact_lock(%s)", (pd.TICK_LOCK_KEY,))
        res = pd.run_pass(store, dry_run=True, reason="params.dry_run")
        assert res.skipped is not None
        other.rollback()


def test_the_tick_lock_is_free_after_a_pass(store: Store) -> None:
    _small_plan(store)
    assert pd.run_pass(store).skipped is None
    with store.pool.connection() as other:
        row = other.execute(
            "SELECT pg_try_advisory_xact_lock(%s)", (pd.TICK_LOCK_KEY,)
        ).fetchone()
        other.rollback()
    assert row is not None and row[0] is True


def test_the_tick_lock_connection_is_bounded_and_labelled(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psycopg

    from precis.store import advisory

    seen: list[str] = []
    real_connect = psycopg.connect

    class _Recorder:
        def __init__(self, conn: Any) -> None:
            self._conn = conn

        def execute(self, query: Any, *a: Any, **kw: Any) -> Any:
            seen.append(
                query.as_string(self._conn)
                if hasattr(query, "as_string")
                else str(query)
            )
            return self._conn.execute(query, *a, **kw)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._conn, name)

    with monkeypatch.context() as mp:
        mp.setattr(
            advisory.psycopg,
            "connect",
            lambda *a, **kw: _Recorder(real_connect(*a, **kw)),
        )
        assert pd.run_pass(store).skipped is None
    assert "SET LOCAL idle_in_transaction_session_timeout = '900s'" in seen
    assert "SET LOCAL application_name = 'precis plan_tick lock'" in seen
    # Both are issued before the lock statement.
    lock_at = next(i for i, q in enumerate(seen) if "pg_try_advisory_xact_lock" in q)
    set_local_at = {i for i, q in enumerate(seen) if q.startswith("SET LOCAL")}
    assert len(set_local_at) == 2
    assert max(set_local_at) < lock_at


def test_the_tick_lock_connection_shows_in_pg_stat_activity(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    names: list[str] = []
    real_load = pd._load

    def _spy(s: Any) -> Any:
        with s.pool.connection() as c:
            rows = c.execute(
                "SELECT application_name FROM pg_stat_activity"
                " WHERE application_name = %s",
                (pd.TICK_LOCK_APP_NAME,),
            ).fetchall()
            names.extend(r[0] for r in rows)
        return real_load(s)

    monkeypatch.setattr(pd, "_load", _spy)
    pd.run_pass(store)
    assert names == [pd.TICK_LOCK_APP_NAME]


def test_rearm_releases_dead_claim_and_stamps_rescue_answered(store: Store) -> None:
    """gr598110: an answered rescue ask re-arms the node AND drops the
    dead ``claimed-by:`` lease — ``_step_dispatch`` skips every claimed
    node, so the stuck claim re-tagged the node (td598406)."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    job = _failed_job(store, task, "boom")
    store.add_tag(task, Tag.open("rescue:stalled"), set_by="system")
    store.add_tag(task, Tag.open("claimed-by:lane-devin"), set_by="system")
    store.stamp_ref_meta(task, {"rescue": {"n": 1}})
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title="rescue ruling",
            meta={
                "ask": {
                    "kind": "ruling",
                    "options": ["retry with a different lane/model"],
                    "blocks": f"td{task}",
                    "context": "in rescue",
                }
            },
            parent_id=mint.root_id,
            conn=conn,
        )
        ask = int(ref.id)
        store.add_tag(
            ask,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        store.add_tag(ask, Tag.open("ask-user"), set_by="system", conn=conn)
        store.add_link(
            src_ref_id=task, dst_ref_id=ask, relation="blocked-by", conn=conn
        )
    _answer_ask(store, ask, "retry with a different lane/model")
    res = pd.run_pass(store)
    assert "rescue:stalled" not in _open_tags(store, task)
    assert "claimed-by:lane-devin" not in _open_tags(store, task)
    meta = _meta(store, task)
    assert meta["rescue_answered"] == {"cls": "stalled", "ask": ask, "job": job}

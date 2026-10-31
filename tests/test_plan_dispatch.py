"""Dark-factory plan dispatcher tick (td542096): ``plan_dispatch`` walking a
plan subtree through gates, loops, phases, dispatch and hygiene on the dev
DB. ``run_slice`` jobs are faked by marking the minted job succeeded (and
its task done) between passes."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from precis.plans import templates as T
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.workers._mint import mint_background_job
from precis.workers.job_types import get_job_type, known_job_types
from precis.workers.job_types import plan_dispatch as pd

PAST = {"kind": "not-before", "args": {"ts": "2020-01-01T00:00:00Z"}}
FUTURE = {"kind": "not-before", "args": {"ts": "2999-01-01T00:00:00Z"}}


@pytest.fixture(autouse=True)
def _factory_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)


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


def _failed_job(store: Store, task: int, reason: str) -> int:
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
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=1, text=reason, meta={"chunk_kind": "job_event"})]
    )
    return int(ref.id)


def _slice_job(store: Store, task: int, job_type: str, status: str) -> int:
    with store.tx() as conn:
        ref = mint_background_job(
            store,
            conn=conn,
            job_type=job_type,
            executor="ssh_node",
            title=job_type,
            params={"todo_id": task},
            parent_id=task,
            status=status,
        )
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


def test_retry_cap_mints_exactly_one_ask(store: Store) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(store, task, "first failure")
    store.add_tag(task, Tag.open("child-failed:1"), set_by="system")

    # One failure (< retries 2): the tick retries and drops the bubble tag.
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)
    assert "child-failed:1" not in _open_tags(store, task)
    live = _jobs_under(store, task)[-1][0]
    store.add_tag(
        live, Tag.closed("STATUS", "failed"), set_by="system", replace_prefix=True
    )
    store.chunks.insert_chunks(
        live,
        [ChunkInsert(ord=1, text="log tail: exit 1", meta={"chunk_kind": "job_event"})],
    )

    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (0, 1)
    asks = [c for c in _children(store, mint.root_id) if c != task]
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
    store.add_tag(
        ask, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )
    res = pd.run_pass(store)
    assert (res.dispatched, res.asks) == (1, 0)


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
    assert set(body.ids) == {"triage", "fix", "recut"}
    assert body.root_id == loop
    for todo_id in body.ids.values():
        assert f"plan:{mint.root_id}" in _open_tags(store, todo_id)
        ref = store.get_ref(kind="todo", id=todo_id)
        assert ref is not None and ref.parent_id == loop
        assert "iteration 2" in str(ref.title)
    triage = _meta(store, body.ids["triage"])["generate"]
    assert triage == {"role": "triage", "from": f"gripes tagged plan:{mint.root_id}"}


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
    store: Store, todo: int, *, status: str = "succeeded", lane: str | None = None
) -> int:
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
            meta_extra={"lane": lane} if lane else None,
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
    assert dict(ref.meta or {}) == {"op": "run_slice"}
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


def test_review_without_verdict_retries_once_then_asks(store: Store) -> None:
    mint = _reviewed(store, "review: looks fine to me")
    review = mint.ids["review"]
    res = pd.run_pass(store)
    assert (res.reviews, res.asks) == (0, 0)
    assert _meta(store, review)["review"]["verdict"] is None
    assert "malformed verdict" in _todo_comments(store, review)[-1]
    # One retry, pinned to the review lane; one lane only, so the note says so.
    assert res.dispatched == 1
    (_old, (retry, meta)) = _jobs_under(store, review)
    assert meta["params"] == {"todo_id": review, "lane": "claude-sub"}
    assert any("shares the builder's vendor 'anthropic'" in n for n in res.notes)
    # The retry posts nothing either: the hygiene ask follows.
    store.add_tag(
        retry, Tag.closed("STATUS", "succeeded"), set_by="system", replace_prefix=True
    )
    res = pd.run_pass(store)
    assert (res.reviews, res.dispatched, res.asks) == (0, 0, 1)
    assert "no `review:` comment" in _todo_comments(store, review)[-1] or (
        "malformed verdict" in _todo_comments(store, review)[-1]
    )
    asks = [c for c in _children(store, mint.root_id) if "ask" in _meta(store, c)]
    assert len(asks) == 1


def test_dispatch_orders_by_prio_across_roots(store: Store) -> None:
    older = _one_task_plan(store)  # no prio anywhere: 5
    newer = _one_task_plan(store)
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET prio = 1 WHERE ref_id = %s", (newer.root_id,))
    res = pd.run_pass(store, dry_run=True)
    order = [n.split(":")[0] for n in res.notes if "would dispatch" in n]
    # The prio-1 root's task (inherited prio) goes before the older prio-5 one.
    assert order == [f"td{newer.ids['task']}", f"td{older.ids['task']}"]


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

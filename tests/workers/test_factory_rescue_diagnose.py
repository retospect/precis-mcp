"""factory_rescue.run_rescue_pass — diagnose, re-arm once, ask (dark-factory
td574946, slice S3): a node sitting in RESCUE gets ONE read-only diagnose
task on the ``claude-sub`` lane; a ``rescue: REARM`` verdict re-arms it once
with a corrected brief; a ``rescue: NOFIX``, a failed or unparsable
diagnose, a second rescue or an engine class files one ask-user todo
blocking the node — except a node under a closed ancestor, which the pass
closes ``won't-do`` instead (gr576258), and except an already-old rescue,
which closes the same way: one ask per live node (gr666159). The pass
writes nothing while the factory kill switch holds; ``dry_run`` lists
what it would do."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.todo.guards import check_ask_meta
from precis.workers import factory_rescue as fr
from precis.workers._mint import mint_background_job
from precis.workers.job_types import plan_dispatch as pd
from tests._lane_rows import seed_lane, use_lanes
from tests._refs import insert_gripe

# ``NOW`` is re-stamped per test (see test_factory_rescue_stall): rows carry
# database ``now()`` times, so an import-time constant ages by the shard's
# elapsed time. The import-time value is stale on purpose.
NOW = datetime.now(UTC) - timedelta(hours=1)


@pytest.fixture(autouse=True)
def _now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.modules[__name__], "NOW", datetime.now(UTC))


@pytest.fixture(autouse=True)
def _factory_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)


# ── helpers (the test_factory_rescue_stall shapes) ─────────────────


def _root(store: Store, *, status: str = "open") -> int:
    ref = store.insert_ref(
        kind="todo", slug=None, title="plan root", meta={"auto_close": True}
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", status), set_by="system", replace_prefix=True
    )
    store.add_tag(ref.id, Tag.open(f"plan:{ref.id}"), set_by="system")
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


def _chunk(store: Store, ref_id: int, kind: str, text: str, ord_: int = 0) -> None:
    store.chunks.insert_chunks(
        ref_id, [ChunkInsert(ord=ord_, text=text, meta={"chunk_kind": kind})]
    )


def _body(store: Store, ref_id: int, kind: str = "todo_body") -> str:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = %s "
            "ORDER BY ord",
            (ref_id, kind),
        ).fetchall()
    return "\n\n".join(str(r[0]) for r in rows)


def _open_tags(store: Store, ref_id: int) -> list[str]:
    return [str(t.value) for t in store.tags_for(ref_id) if t.namespace == "open"]


def _rescue_tags(store: Store, ref_id: int) -> list[str]:
    return [t for t in _open_tags(store, ref_id) if t.startswith("rescue:")]


def _status(store: Store, ref_id: int) -> str:
    for t in store.tags_for(ref_id):
        if t.prefix == "STATUS":
            return str(t.value)
    return "?"


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return dict(row[0] or {}) if row else {}


def _comments(store: Store, ref_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'todo_comment' "
            "ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return [str(r[0]) for r in rows]


def _blocked_by(store: Store, src: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT dst_ref_id FROM links WHERE src_ref_id = %s "
            "AND relation = 'blocked-by' ORDER BY dst_ref_id",
            (src,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _newest_job(store: Store, todo: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE parent_id = %s AND kind = 'job' "
            "ORDER BY ref_id DESC LIMIT 1",
            (todo,),
        ).fetchone()
    assert row is not None
    return int(row[0])


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


def _diagnose_tasks(store: Store) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'todo' AND retired_at IS NULL "
            "AND meta ? 'rescue_for' ORDER BY ref_id",
        ).fetchall()
    return [int(r[0]) for r in rows]


def _ask_refs(store: Store) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'todo' AND retired_at IS NULL "
            "AND meta ? 'ask' ORDER BY ref_id",
        ).fetchall()
    return [int(r[0]) for r in rows]


def _ev(**kw: Any) -> fr.RescueEvidence:
    base: dict[str, Any] = {
        "step": "run_slice",
        "error_line": "boom",
        "log_tail": "boom",
    }
    base.update(kw)
    return fr.RescueEvidence(**base)


def _rescued(store: Store, root: int, *, cls: str = "builder", **kw: Any) -> int:
    """A task node rescued through the real entry point (``rescue:<cls>``
    tag, signature gripe, evidence comment) behind one failed
    ``run_slice`` job that left a commit on ``slice/<id>``."""
    task = _node(
        store,
        root,
        title=kw.pop("title", "a task"),
        status=kw.pop("status", "open"),
        tags=kw.pop("tags", ()),
    )
    job = _job(
        store,
        task,
        status="failed",
        meta_extra={"branch_commits": ["abc1234 feat: x"]},
    )
    _chunk(store, job, "job_event", "run failed\nexit 1: kaboom-tail")
    kw.setdefault("branch", f"slice/{task}")
    kw.setdefault("head_sha", "abc1234")
    kw.setdefault("job", f"jo{job}")
    fr.enter_rescue(store, task, cls, _ev(**kw))
    return task


def _minted_diagnose(store: Store, task: int, root: int) -> int:
    """The diagnose task the first pass mints for ``task``."""
    fr.run_rescue_pass(store, NOW)
    (diag,) = _diagnose_tasks(store)
    assert _blocked_by(store, task) == [diag]
    return diag


def _age_signals(store: Store, ids: list[int], *, by: timedelta) -> None:
    """Backdate every signal row of ``ids`` — ref, tag sets, chunks,
    events — so a rescue tag stamped later reads on already-old work
    (gr666159's flood: nodes created 2026-11-11, tagged 2026-11-14)."""
    past = datetime.now(UTC) - by
    with store.pool.connection() as conn:
        for table, col in (
            ("refs", "created_at"),
            ("ref_tags", "created_at"),
            ("chunks", "created_at"),
            ("ref_events", "ts"),
        ):
            conn.execute(
                f"UPDATE {table} SET {col} = %s WHERE ref_id = ANY(%s)",
                (past, ids),
            )


def _stale_rescued(
    store: Store,
    root: int,
    *,
    cls: str = "stalled",
    age: timedelta = timedelta(days=3),
) -> int:
    """``_rescued`` on already-old work: node and failed job backdated
    ``age``, the ``rescue:`` tag itself stamped fresh by
    ``enter_rescue`` — the flood's exact shape (gr666159)."""
    task = _node(store, root)
    job = _job(
        store,
        task,
        status="failed",
        meta_extra={"branch_commits": ["abc1234 feat: x"]},
    )
    _chunk(store, job, "job_event", "run failed\nexit 1: kaboom-tail")
    _age_signals(store, [task, job], by=age)
    fr.enter_rescue(
        store,
        task,
        cls,
        _ev(job=f"jo{job}", branch=f"slice/{task}", head_sha="abc1234"),
    )
    return task


# ── the mint ───────────────────────────────────────────────────────


def test_rescued_task_node_gets_one_diagnose_task_pinned_to_claude_sub(
    store: Store,
) -> None:
    root = _root(store)
    task = _rescued(store, root)

    res = fr.run_rescue_pass(store, NOW)

    assert res.diagnosing == [task]
    (diag,) = _diagnose_tasks(store)
    ref = store.get_ref(kind="todo", id=diag)
    assert ref is not None and ref.parent_id == root
    assert ref.meta["op"] == {
        "type": "run_slice",
        "params": {"lane": "claude-sub", "lane_capability": "mcp"},
    }
    assert ref.meta["task_class"] == "diagnose"
    assert ref.meta["retries"] == 1
    assert ref.meta["rescue_for"] == task
    assert "llm_tier" not in ref.meta
    assert _status(store, diag) == "open"
    assert f"plan:{root}" in _open_tags(store, diag)
    assert _blocked_by(store, task) == [diag]
    body = _body(store, diag)
    assert "rescue: builder" in body  # the enter_rescue evidence comment
    (gripe,) = _sig_gripes(store)
    assert f"gr{gripe}" in body
    assert "kaboom-tail" in body  # the newest job's event tail
    assert f"slice/{task}" in body and "abc1234" in body
    assert "rescue: REARM" in body and "rescue: NOFIX" in body


def test_second_tick_mints_no_second_diagnose_task(store: Store) -> None:
    root = _root(store)
    task = _rescued(store, root)
    _minted_diagnose(store, task, root)

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))
    assert res.diagnosing == [] and res.waiting == 1
    assert len(_diagnose_tasks(store)) == 1

    (diag,) = _diagnose_tasks(store)
    _job(store, diag, status="queued")
    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=2))
    assert res.diagnosing == [] and res.waiting == 1
    assert len(_diagnose_tasks(store)) == 1


@pytest.mark.parametrize(
    "dead,word",
    [
        ({"enabled": False}, "disabled"),
        ({"parked_reason": "lane-auth:not-logged-in"}, "parked"),
    ],
    ids=["disabled", "parked"],
)
def test_dead_lane_pin_falls_back_to_an_enabled_mcp_lane(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    dead: dict[str, Any],
    word: str,
) -> None:
    """gr604579: the hard ``claude-sub`` pin died at submit in 0 s while
    the lane sat parked, and the retry-cap ask landed on a human for a
    mechanical cause. The mint falls back to ``pick_lane`` over the
    write-MCP lanes — the task pins the enabled one and a comment names
    the fallback."""
    sub = replace(seed_lane("claude-sub"), **dead)
    alt = replace(seed_lane("claude-sub"), name="claude-sub-2")
    use_lanes(monkeypatch, seed_lane("devin"), seed_lane("codex"), sub, alt)
    root = _root(store)
    task = _rescued(store, root)

    res = fr.run_rescue_pass(store, NOW)

    assert res.diagnosing == [task]
    (diag,) = _diagnose_tasks(store)
    ref = store.get_ref(kind="todo", id=diag)
    assert ref is not None
    assert ref.meta["op"] == {
        "type": "run_slice",
        "params": {"lane": "claude-sub-2", "lane_capability": "mcp"},
    }
    assert any(word in c and "claude-sub-2" in c for c in _comments(store, diag))
    assert any("claude-sub-2" in n for n in res.notes)


def test_dead_lane_pin_with_no_enabled_mcp_lane_mints_capability_bound(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No enabled lane fits ``diagnose``: the task mints UNPINNED but
    still bound — ``params.lane_capability`` keeps submit's pick on MCP
    lanes so the lane-wait park holds it attempt-free (td610544) — a
    free code-fit lane like devin is no fallback (no MCP for the
    reads), and the dead pin is never re-minted."""
    sub = replace(seed_lane("claude-sub"), enabled=False)
    use_lanes(monkeypatch, seed_lane("devin"), seed_lane("codex"), sub)
    root = _root(store)
    task = _rescued(store, root)

    res = fr.run_rescue_pass(store, NOW)

    assert res.diagnosing == [task]
    (diag,) = _diagnose_tasks(store)
    ref = store.get_ref(kind="todo", id=diag)
    assert ref is not None
    assert ref.meta["op"] == {
        "type": "run_slice",
        "params": {"lane_capability": "mcp"},
    }
    assert any("no enabled MCP lane" in c for c in _comments(store, diag))


# ── REARM ──────────────────────────────────────────────────────────


def test_rearm_once_rearms_and_clears_the_tag(store: Store) -> None:
    root = _root(store)
    task = _rescued(store, root)
    diag = _minted_diagnose(store, task, root)
    _job(store, diag, status="succeeded")
    _comment(
        store,
        diag,
        "handoff: rescue: REARM — pin pytest-rerunfailures and name "
        "tests/test_x.py; the branch already carries the parser fix\n\n"
        "a second line of corrected brief",
    )

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))

    assert res.rearmed == [task]
    assert _rescue_tags(store, task) == []
    meta = _meta(store, task)
    assert meta["rescue"]["n"] == 1
    assert meta["rescue"]["cls"] == "builder"
    assert meta["rescue"]["diagnose"] == f"td{diag}"
    assert meta["rescue"]["at"].endswith("Z")
    # the re-arm floor is the node's newest ended job (its failed build)
    assert meta["rearmed_after_job"] == _newest_job(store, task)
    assert meta["rearmed_ask_at"].endswith("Z")
    assert meta["rearmed_answer"] == "rescue: REARM"
    # the newest brief:/handoff: comment is the corrected brief + Resume
    comments = _comments(store, task)
    newest = comments[-1]
    assert newest.startswith("brief: pin pytest-rerunfailures")
    assert "a second line of corrected brief" in newest
    assert "Resume:" in newest
    assert f"slice/{task}" in newest and "abc1234" in newest
    from precis.workers.job_types import run_slice

    _body_txt, latest = run_slice._brief_parts(store, task)
    assert latest == newest
    # the audit line sits under it, still naming the diagnose task
    assert comments[-2].startswith(f"rescue: re-armed from td{diag}")
    assert _status(store, diag) == "done"
    # the tick sees a fresh, dispatchable node with a zeroed budget
    plan = pd._load(store)
    n = plan.nodes[task]
    assert pd._attempts(plan, n) == 0
    assert not pd._capped(plan, n, 2)
    assert pd._dispatchable(plan, n)
    pres = pd.run_pass(store, dry_run=True)
    assert any(f"td{task}: would dispatch run_slice" in note for note in pres.notes)
    # a second rescue pass writes nothing: no tag, nothing to judge
    second = fr.run_rescue_pass(store, NOW + timedelta(minutes=2))
    assert second.diagnosing == [] and second.rearmed == [] and second.asked == []


def test_rearm_of_a_doing_node_returns_it_to_open(store: Store) -> None:
    """A node rescued while ``STATUS:doing`` (its run died before
    ``release_todo``) must come back ``STATUS:open`` on REARM: the
    rescue tag alone cleared leaves it invisible to
    ``plan_dispatch._step_dispatch``, which only dispatches ``open``
    (review td575437)."""
    root = _root(store)
    task = _rescued(store, root, status="doing")
    diag = _minted_diagnose(store, task, root)
    _job(store, diag, status="succeeded")
    _comment(store, diag, "rescue: REARM — rebuild on the corrected brief")

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))

    assert res.rearmed == [task]
    assert _status(store, task) == "open"
    assert _rescue_tags(store, task) == []
    assert _status(store, diag) == "done"
    pres = pd.run_pass(store, dry_run=True)
    assert any(f"td{task}: would dispatch run_slice" in note for note in pres.notes)


def test_rearm_after_infra_red_land_mints_a_builder_not_a_reland(
    store: Store,
) -> None:
    """The newest job is an infra-red ``land_slice``: without the
    ``rearmed_ask_at`` floor, ``_reland_params`` would re-mint a land of
    the old branch instead of a builder on the corrected brief."""
    root = _root(store)
    task = _node(store, root)
    _job(
        store,
        task,
        job_type="land_slice",
        status="failed",
        meta_extra={"attempt_free": "infra-land", "params": {"branch": "x"}},
    )
    fr.enter_rescue(store, task, "builder", _ev())
    diag = _minted_diagnose(store, task, root)
    _job(store, diag, status="succeeded")
    _comment(store, diag, "rescue: REARM — rebuild on the corrected brief")

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))

    assert res.rearmed == [task]
    plan = pd._load(store)
    assert pd._reland_params(plan, plan.nodes[task], store) is None
    pres = pd.run_pass(store, dry_run=True)
    assert any(f"td{task}: would dispatch run_slice" in note for note in pres.notes)


def test_rearm_after_a_succeeded_land_stamps_no_floor(store: Store) -> None:
    """gr586688's rule in the rescue pass (td584381 S1d): the newest
    ended job being a succeeded ``land_slice`` means the slice already
    landed — flooring the todo on its own land would hold it open for a
    duplicate build of landed work, so no ``rearmed_after_job``."""
    root = _root(store)
    task = _node(store, root)
    _job(store, task, status="succeeded")
    _job(store, task, job_type="land_slice", status="succeeded")
    fr.enter_rescue(store, task, "builder", _ev())
    diag = _minted_diagnose(store, task, root)
    _job(store, diag, status="succeeded")
    _comment(store, diag, "rescue: REARM — rebuild on the corrected brief")

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))

    assert res.rearmed == [task]
    assert "rearmed_after_job" not in _meta(store, task)


# ── ASK outcomes ───────────────────────────────────────────────────


def test_nofix_files_one_ask_blocking_the_node(store: Store) -> None:
    root = _root(store)
    task = _rescued(store, root)
    diag = _minted_diagnose(store, task, root)
    _job(store, diag, status="succeeded")
    _comment(store, diag, "rescue: NOFIX — the denied path means a human must do it")

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))

    assert res.asked == [task]
    (ask,) = _ask_refs(store)
    ref = store.get_ref(kind="todo", id=ask)
    assert ref is not None and ref.parent_id == root
    assert check_ask_meta(ref.meta) == task
    assert ref.meta["ask"]["blocks"] == f"td{task}"
    assert ref.meta["ask"]["options"] == list(pd._ASK_OPTIONS)
    tags = _open_tags(store, ask)
    assert "ask-user" in tags and "showstopper" in tags and f"plan:{root}" in tags
    assert _blocked_by(store, task) == [diag, ask]
    body = _body(store, ask)
    assert "denied path" in body
    assert "rescue: builder" in body
    assert f"slice/{task}" in body and "abc1234" in body
    # the tag stays for now: the tick's ``_step_rearm`` clears it on the
    # answered ask (td584381 S1a F1) — the rescue pass never clears it
    assert _rescue_tags(store, task) == ["rescue:builder"]
    assert _meta(store, task)["rescue"]["n"] == 1
    assert _meta(store, task)["rescue"]["ask"] == f"td{ask}"
    assert _status(store, diag) == "done"
    # one ask per episode: a second pass files nothing
    res2 = fr.run_rescue_pass(store, NOW + timedelta(minutes=2))
    assert res2.asked == [] and len(_ask_refs(store)) == 1


def test_failed_diagnose_job_asks(store: Store) -> None:
    root = _root(store)
    task = _rescued(store, root)
    diag = _minted_diagnose(store, task, root)
    _job(store, diag, status="failed")

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))

    assert res.asked == [task]
    (ask,) = _ask_refs(store)
    assert _blocked_by(store, task) == [diag, ask]
    assert _status(store, diag) == "won't-do"


def test_diagnose_task_that_is_itself_rescued_asks(store: Store) -> None:
    root = _root(store)
    task = _rescued(store, root)
    diag = _minted_diagnose(store, task, root)
    fr.enter_rescue(store, diag, "stalled", _ev())

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))

    assert res.asked == [task]
    (ask,) = _ask_refs(store)
    assert _blocked_by(store, task) == [diag, ask]
    assert _status(store, diag) == "won't-do"


@pytest.mark.parametrize(
    "reply",
    [
        "I read the job log and it looks fine to me.",
        "rescue: MAYBE — not sure",
        "rescue: REARM —",
        "rescue: REARM — fix a\nrescue: NOFIX — because b",
        None,
    ],
    ids=["prose", "unknown-verdict", "empty-rest", "disagreeing", "no-comment"],
)
def test_unparsable_reply_asks(store: Store, reply: str | None) -> None:
    root = _root(store)
    task = _rescued(store, root)
    diag = _minted_diagnose(store, task, root)
    _job(store, diag, status="succeeded")
    if reply is not None:
        _comment(store, diag, reply)

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))

    assert res.asked == [task]
    (ask,) = _ask_refs(store)
    assert _status(store, diag) == "won't-do"


def test_second_rescue_is_refused_and_asks(store: Store) -> None:
    root = _root(store)
    task = _rescued(store, root)
    store.stamp_ref_meta(
        task, {"rescue": {"n": 1, "diagnose": "td9999", "at": "2026-01-01T00:00:00Z"}}
    )

    res = fr.run_rescue_pass(store, NOW)

    assert res.asked == [task] and res.diagnosing == []
    assert _diagnose_tasks(store) == []
    (ask,) = _ask_refs(store)
    assert _meta(store, task)["rescue"]["n"] == 1
    assert _meta(store, task)["rescue"]["ask"] == f"td{ask}"


@pytest.mark.parametrize("cls", ["stalled", "cancelled", "tick-silent"])
def test_engine_class_asks_without_a_diagnose_task(store: Store, cls: str) -> None:
    root = _root(store)
    task = _node(store, root)
    fr.enter_rescue(store, task, cls, _ev())

    res = fr.run_rescue_pass(store, NOW)

    assert res.asked == [task] and res.diagnosing == []
    assert _diagnose_tasks(store) == []
    (ask,) = _ask_refs(store)
    assert _blocked_by(store, task) == [ask]


def test_ask_is_not_repeated_after_it_is_answered(store: Store) -> None:
    """A node still tagged after its ask was answered is NOT asked again;
    clearing ``rescue:<cls>`` is the tick's ``_step_rearm`` step on the
    answered ask (td584381 S1a F1), never the rescue pass's."""
    root = _root(store)
    task = _rescued(store, root)
    diag = _minted_diagnose(store, task, root)
    _job(store, diag, status="succeeded")
    _comment(store, diag, "rescue: NOFIX — nope")
    fr.run_rescue_pass(store, NOW + timedelta(minutes=1))
    (ask,) = _ask_refs(store)
    store.add_tag(
        ask, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=2))

    assert res.asked == [] and res.diagnosing == [] and res.rearmed == []
    assert len(_ask_refs(store)) == 1
    assert _diagnose_tasks(store) == [diag]
    assert _rescue_tags(store, task) == ["rescue:builder"]


# ── a closed ancestor: close won't-do, never ask (gr576258) ────────


def test_terminal_root_orphan_closes_wont_do_with_no_ask(store: Store) -> None:
    """The whole-tick shape: the stall pass tags the orphan
    ``rescue:terminal-root``, the rescue pass closes it — ``root td<N>
    is <state>; closed by the watchdog`` — and no ``ask-user`` todo
    appears."""
    root = _root(store, status="done")
    task = _node(store, root)

    stall = fr.run_stall_pass(store, NOW + timedelta(minutes=21))
    assert stall.rescued == [(task, "terminal-root", "idle")]

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=22))

    assert res.closed == [task]
    assert res.asked == [] and res.diagnosing == []
    assert _ask_refs(store) == [] and _diagnose_tasks(store) == []
    assert _status(store, task) == "won't-do"
    assert any(
        f"root td{root} is done; closed by the watchdog" in c
        for c in _comments(store, task)
    )
    meta = _meta(store, task)
    assert meta["rescue"]["closed"] == "won't-do"
    assert meta["rescue"]["cls"] == "terminal-root"

    # a second pass writes nothing: a closed node is never re-judged
    res2 = fr.run_rescue_pass(store, NOW + timedelta(minutes=23))
    assert res2.closed == [] and res2.asked == []


def test_wont_do_ancestor_closes_any_rescue_class(store: Store) -> None:
    """A node rescued under another class whose own parent chain holds
    a won't-do closes the same way — the work is dead whatever the
    failure signature was."""
    root = _root(store)
    phase = _node(
        store, root, title="phase", meta={"auto_close": True}, status="won't-do"
    )
    leaf = _node(store, root, parent=phase)
    fr.enter_rescue(store, leaf, "stalled", _ev())

    res = fr.run_rescue_pass(store, NOW)

    assert res.closed == [leaf]
    assert res.asked == [] and res.diagnosing == []
    assert _ask_refs(store) == []
    assert _status(store, leaf) == "won't-do"
    assert _status(store, root) == "open"  # the open root is untouched
    assert any(
        f"ancestor td{phase} is won't-do; closed by the watchdog" in c
        for c in _comments(store, leaf)
    )


def test_done_parent_does_not_mask_a_higher_wont_do_ancestor(
    store: Store,
) -> None:
    """td598267 regression: ``_terminal_ancestor`` stopped at the
    NEAREST terminal ancestor, so a done parent masked a higher
    won't-do and a non-``terminal-root`` class fell through to mint a
    diagnose or file an ask for work the won't-do already ended."""
    root = _root(store)
    dead = _node(store, root, title="a won't-do phase", status="won't-do")
    parent = _node(store, root, title="a done parent", status="done", parent=dead)
    leaf = _node(store, root, parent=parent)
    fr.enter_rescue(store, leaf, "builder", _ev())

    res = fr.run_rescue_pass(store, NOW)

    assert res.closed == [leaf]
    assert res.asked == [] and res.diagnosing == []
    assert _ask_refs(store) == [] and _diagnose_tasks(store) == []
    assert _status(store, leaf) == "won't-do"
    assert _status(store, root) == "open"
    assert any(
        f"ancestor td{dead} is won't-do; closed by the watchdog" in c
        for c in _comments(store, leaf)
    )


def test_rescued_node_under_an_open_root_is_unchanged(store: Store) -> None:
    """Control: a live root keeps the human-choice path — an engine
    class still files its one ask."""
    root = _root(store)
    task = _node(store, root)
    fr.enter_rescue(store, task, "stalled", _ev())

    res = fr.run_rescue_pass(store, NOW)

    assert res.closed == [] and res.asked == [task]
    assert _status(store, task) == "open"


def test_a_pending_ask_on_the_orphan_closes_with_it(store: Store) -> None:
    """An ask already open on the orphan (filed before this fix, or by
    a racer) is moot the moment the close lands — closed won't-do with
    it, never left orphaned."""
    root = _root(store, status="done")
    task = _node(store, root)
    fr.enter_rescue(store, task, "terminal-root", _ev())
    ask = _node(
        store,
        root,
        title="a ruling ask",
        meta={
            "ask": {
                "kind": "ruling",
                "options": ["retry"],
                "blocks": f"td{task}",
                "context": "pending",
            }
        },
        tags=("ask-user",),
    )
    store.add_link(
        src_ref_id=task, dst_ref_id=ask, relation="blocked-by", set_by="system"
    )

    res = fr.run_rescue_pass(store, NOW)

    assert res.closed == [task]
    assert _status(store, task) == "won't-do"
    assert _status(store, ask) == "won't-do"
    assert any("superseded" in c for c in _comments(store, ask))


def test_orphan_closes_are_a_bounded_batch(store: Store) -> None:
    """``ORPHAN_BATCH`` orphans close in one pass; the rest wait a
    tick — an old backlog drains in a few passes, not one ask a
    tick."""
    root = _root(store, status="done")
    tasks = [
        _node(store, root, title=f"orphan {i}") for i in range(fr.ORPHAN_BATCH + 1)
    ]
    for t in tasks:
        fr.enter_rescue(store, t, "terminal-root", _ev())

    res = fr.run_rescue_pass(store, NOW)

    assert res.closed == tasks[:-1]
    assert _status(store, tasks[-1]) == "open"
    assert any("batch full" in n for n in res.notes)

    res2 = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))
    assert res2.closed == [tasks[-1]]
    assert _status(store, tasks[-1]) == "won't-do"
    assert _ask_refs(store) == []


def test_dry_run_lists_the_close_and_writes_nothing(store: Store) -> None:
    root = _root(store, status="done")
    task = _node(store, root)
    fr.enter_rescue(store, task, "terminal-root", _ev())

    res = fr.run_rescue_pass(store, NOW, dry_run=True)

    assert res.dry_run is True
    assert (task, "close") in res.would
    assert res.closed == []
    assert _status(store, task) == "open"
    assert "rescue" not in _meta(store, task)


# ── stale rescues (gr666159) ───────────────────────────────────────


def test_stale_rescued_node_closes_wont_do_without_an_ask(store: Store) -> None:
    """gr666159: ~62 stalled-rescue asks landed in a few hours on build
    nodes whose work had already been dead for days — one ask per LIVE
    node. The already-old rescue closes won't-do in the same bounded
    batch as orphans and files nothing."""
    root = _root(store)
    task = _stale_rescued(store, root)

    res = fr.run_rescue_pass(store, NOW)

    assert res.closed == [task]
    assert res.asked == [] and res.diagnosing == []
    assert _ask_refs(store) == [] and _diagnose_tasks(store) == []
    assert _status(store, task) == "won't-do"
    assert _rescue_tags(store, task) == ["rescue:stalled"]
    assert any(
        "stale rescue closed by the watchdog" in c for c in _comments(store, task)
    )
    meta = _meta(store, task)
    assert meta["rescue"]["closed"] == "won't-do"
    assert meta["rescue"]["cls"] == "stalled"

    # a second pass writes nothing: a closed node is never re-judged
    res2 = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))
    assert res2.closed == [] and res2.asked == []


def test_a_node_inside_the_stale_bound_still_asks(store: Store) -> None:
    """The bound, not the tag, decides: silence younger than
    ``STALE_ASK_AFTER`` is a live stall and keeps its one ask."""
    root = _root(store)
    task = _stale_rescued(store, root, age=fr.STALE_ASK_AFTER - timedelta(hours=1))

    res = fr.run_rescue_pass(store, NOW)

    assert res.asked == [task] and res.closed == []
    assert _status(store, task) == "open"
    assert len(_ask_refs(store)) == 1


def test_a_fresh_job_keeps_an_old_rescue_live(store: Store) -> None:
    """Job signals are unfiltered: a job minted after the tag — work
    resumed — makes the node live again, so it asks rather than
    closing."""
    root = _root(store)
    task = _stale_rescued(store, root)
    _job(store, task, status="running")

    res = fr.run_rescue_pass(store, NOW)

    assert res.asked == [task] and res.closed == []
    assert _status(store, task) == "open"


def test_a_stale_node_with_a_pending_ask_keeps_its_engagement(
    store: Store,
) -> None:
    """An ask already filed on the stale node is live engagement — the
    episode-ask rule skips it BEFORE the stale close, so the pass never
    mints a second ask and never closes a node a human may still rule
    on; bulk-closing the leftover flood asks is the coordinator's call."""
    root = _root(store)
    task = _stale_rescued(store, root)
    ask = _node(
        store,
        root,
        title="a ruling ask",
        meta={
            "ask": {
                "kind": "ruling",
                "options": ["retry"],
                "blocks": f"td{task}",
                "context": "pending",
            }
        },
        tags=("ask-user",),
    )
    store.add_link(
        src_ref_id=task, dst_ref_id=ask, relation="blocked-by", set_by="system"
    )

    res = fr.run_rescue_pass(store, NOW)

    assert res.closed == [] and res.asked == []
    assert _status(store, task) == "open"
    assert _status(store, ask) == "open"
    assert len(_ask_refs(store)) == 1
    assert not any("superseded" in c for c in _comments(store, ask))


def test_stale_closes_share_the_orphan_batch(store: Store) -> None:
    """Stale closes draw the same ``ORPHAN_BATCH`` budget as orphan
    closes — a mixed backlog drains a few closes a tick, never one
    unbounded sweep."""
    root = _root(store, status="done")
    orphans = [_node(store, root, title=f"orphan {i}") for i in range(fr.ORPHAN_BATCH)]
    for t in orphans:
        fr.enter_rescue(store, t, "terminal-root", _ev())
    stale = _stale_rescued(store, _root(store))

    res = fr.run_rescue_pass(store, NOW)

    assert res.closed == orphans
    assert _status(store, stale) == "open"
    assert any("batch full" in n for n in res.notes)

    res2 = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))
    assert res2.closed == [stale]
    assert _status(store, stale) == "won't-do"


def test_stale_close_dry_run_writes_nothing(store: Store) -> None:
    root = _root(store)
    task = _stale_rescued(store, root)

    res = fr.run_rescue_pass(store, NOW, dry_run=True)

    assert res.dry_run is True
    assert (task, "close") in res.would
    assert res.closed == []
    assert _status(store, task) == "open"


def test_post_tag_engagement_keeps_a_stale_rescue_live(store: Store) -> None:
    """gr666159 review: the bound reads real activity, not pre-tag
    activity — a human comment or a fresh hand tag after the
    ``rescue:`` tag is engagement, so the node asks rather than
    closing won't-do over work someone is still holding."""
    root = _root(store)
    task = _stale_rescued(store, root)
    _comment(store, task, "handoff: picking this up tomorrow")
    store.add_tag(task, Tag.open("watching"), set_by="system")

    res = fr.run_rescue_pass(store, NOW)

    assert res.asked == [task] and res.closed == []
    assert _status(store, task) == "open"


def test_post_tag_rescue_notes_do_not_keep_a_stale_rescue_live(
    store: Store,
) -> None:
    """The exclusion is the pass's own bookkeeping specifically: a
    ``rescue:``-prefixed note left after the tag (the re-flag/ask
    trail) is not engagement, so the already-old node still closes
    won't-do."""
    root = _root(store)
    task = _stale_rescued(store, root)
    _comment(store, task, "rescue: stalled re-flagged; ask td1 still open")

    res = fr.run_rescue_pass(store, NOW)

    assert res.closed == [task] and res.asked == []
    assert _status(store, task) == "won't-do"


def test_a_fresh_job_event_keeps_an_old_rescue_live(store: Store) -> None:
    """gr666159 review: a child job's newest ``ref_events`` stamp is a
    consumed signal — a job event fresher than the bound means work is
    still moving, so the node asks rather than closing stale."""
    root = _root(store)
    task = _stale_rescued(store, root)
    store.append_event(
        _newest_job(store, task), source="run_slice", event="claim", payload={}
    )

    res = fr.run_rescue_pass(store, NOW)

    assert res.asked == [task] and res.closed == []
    assert _status(store, task) == "open"


def test_a_live_job_with_stale_signals_defers_the_stale_close(
    store: Store,
) -> None:
    """td673697 review: a nonterminal child job whose signals ALL predate
    ``STALE_ASK_AFTER`` — a runner still going whose every stamp aged
    out — must not be closed over: the job may still land, so the pass
    waits (the orphan path's live-job guard), never closing won't-do
    under it."""
    root = _root(store)
    task = _stale_rescued(store, root)
    live = _job(store, task, status="running")
    _age_signals(store, [live], by=timedelta(days=3))

    res = fr.run_rescue_pass(store, NOW)

    assert res.closed == [] and res.asked == [] and res.diagnosing == []
    assert res.waiting == 1
    assert _status(store, task) == "open"
    assert any("live job with stale signals" in n for n in res.notes)


def test_a_stale_node_with_a_live_job_is_not_closed_from_the_ask_path(
    store: Store,
) -> None:
    """td673697 review: the diagnose branches reach ``_ask_node`` without
    ``_judge_rescued``'s stale check — a NOFIX landing on a node whose
    signals all aged out while a child job is still live defers the
    close the same way: no ask, no won't-do, the pass waits for the
    job."""
    root = _root(store)
    task = _rescued(store, root)
    failed_job = _newest_job(store, task)
    diag = _minted_diagnose(store, task, root)
    _job(store, diag, status="succeeded")
    _comment(store, diag, "rescue: NOFIX — the denied path means a human must do it")
    live = _job(store, task, status="running")
    _age_signals(store, [task, failed_job, live], by=timedelta(days=3))

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))

    assert res.closed == [] and res.asked == [] and res.diagnosing == []
    assert res.waiting == 1
    assert _ask_refs(store) == []
    assert _status(store, task) == "open"
    assert _status(store, diag) == "done"  # the NOFIX verdict still closes it
    assert any("live job with stale signals" in n for n in res.notes)


# ── holds, gripes, supersession ────────────────────────────────────


def test_held_rescued_node_is_not_diagnosed(store: Store) -> None:
    root = _root(store)
    _rescued(store, root, tags=("waiting-for:human",))
    _rescued(store, root, tags=("halt",))
    phase = _node(
        store, root, title="paused phase", meta={"auto_close": True}, status="paused"
    )
    leaf = _node(store, root, parent=phase)
    fr.enter_rescue(store, leaf, "builder", _ev())
    paused = _node(store, root, status="paused")
    fr.enter_rescue(store, paused, "builder", _ev())

    res = fr.run_rescue_pass(store, NOW)

    assert res.diagnosing == [] and res.asked == []
    assert res.waiting == 3
    assert _diagnose_tasks(store) == [] and _ask_refs(store) == []


def test_rescued_gripe_is_skipped_with_a_note(store: Store) -> None:
    g = int(insert_gripe(store, "a gripe that cannot mint").id)
    fr.enter_rescue(store, g, "intake", _ev())

    res = fr.run_rescue_pass(store, NOW)

    assert res.asked == [] and res.diagnosing == []
    assert any(f"gr{g}" in n for n in res.notes)
    assert _ask_refs(store) == [] and _diagnose_tasks(store) == []


def test_cap_ask_on_diagnose_task_is_superseded(store: Store) -> None:
    """plan_dispatch already filed its retry-cap ask on the diagnose
    task: the pass files the node's ask and closes the cap ask so no ask
    is orphaned."""
    root = _root(store)
    task = _rescued(store, root)
    diag = _minted_diagnose(store, task, root)
    _job(store, diag, status="failed")
    cap = _node(
        store,
        root,
        title="a retry-cap ask",
        meta={
            "ask": {
                "kind": "ruling",
                "options": ["retry"],
                "blocks": f"td{diag}",
                "context": "cap ask",
            }
        },
        tags=("ask-user",),
    )
    store.add_link(
        src_ref_id=diag, dst_ref_id=cap, relation="blocked-by", set_by="system"
    )

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))

    assert res.asked == [task]
    asks = _ask_refs(store)
    assert len(asks) == 2
    (node_ask,) = [a for a in asks if a != cap]
    assert _blocked_by(store, task) == [diag, node_ask]
    assert _status(store, cap) == "won't-do"
    assert any(f"superseded by td{node_ask}" in c for c in _comments(store, cap))


# ── dry run / kill switch ──────────────────────────────────────────


def test_dry_run_writes_nothing_and_lists_the_plan(store: Store) -> None:
    root = _root(store)
    task = _rescued(store, root)
    engine = _node(store, root)
    fr.enter_rescue(store, engine, "stalled", _ev())

    res = fr.run_rescue_pass(store, NOW, dry_run=True)

    assert res.dry_run is True
    assert (task, "diagnose") in res.would
    assert (engine, "ask") in res.would
    assert _diagnose_tasks(store) == [] and _ask_refs(store) == []
    assert _blocked_by(store, task) == []
    assert len(_comments(store, task)) == 1  # just the enter_rescue evidence
    assert "rescue" not in _meta(store, task)
    assert "rescue" not in _meta(store, engine)


def test_kill_switch_writes_nothing(store: Store) -> None:
    root = _root(store)
    task = _rescued(store, root)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config (host, service, prio) "
            "VALUES ('*', 'plan_dispatch', 0)"
        )

    res = fr.run_rescue_pass(store, NOW)

    assert res.read_only is not None and "prio 0" in res.read_only
    assert res.dry_run is True
    assert res.would == [(task, "diagnose")]
    assert _diagnose_tasks(store) == [] and _ask_refs(store) == []
    assert _blocked_by(store, task) == []
    assert "rescue" not in _meta(store, task)


# ── the reply grammar ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("rescue: REARM — fix the parser test", ("REARM", "fix the parser test")),
        ("rescue: REARM - fix the parser test", ("REARM", "fix the parser test")),
        ("rescue: REARM – fix the parser test", ("REARM", "fix the parser test")),
        ("rescue: REARM: fix the parser test", ("REARM", "fix the parser test")),
        ("rescue: NOFIX — outside the brief", ("NOFIX", "outside the brief")),
        (
            "handoff: rescue: REARM — fix the parser test",
            ("REARM", "fix the parser test"),
        ),
        ("rescue: rearm — lowercase wins", ("REARM", "lowercase wins")),
        (
            "rescue: REARM — line one\nline two of the brief",
            ("REARM", "line one\nline two of the brief"),
        ),
        ("rescue: REARM —", None),
        ("rescue: NOFIX", None),
        ("I diagnosed it, looks fine", None),
        ("rescue: MAYBE — dunno", None),
        ("rescue: REARM — a\nrescue: NOFIX — b", None),
    ],
    ids=[
        "rearm-emdash",
        "rearm-hyphen",
        "rearm-endash",
        "rearm-colon",
        "nofix",
        "handoff-prefix",
        "lowercase",
        "multiline-brief",
        "empty-rest-rearm",
        "empty-rest-nofix",
        "prose",
        "unknown-verdict",
        "disagreeing-lines",
    ],
)
def test_parse_diagnosis_grammar(text: str, expected: tuple[str, str] | None) -> None:
    assert fr.parse_diagnosis(text) == expected


@pytest.mark.parametrize(
    "rest,expected",
    [
        (
            "fixed b2383e574 — gr548629 landed there",
            ("b2383e574", "gr548629 landed there"),
        ),
        ("fixed B2383E574 - upper-case sha", ("b2383e574", "upper-case sha")),
        ("fixed abc1234: colon form", ("abc1234", "colon form")),
        ("fixed abc1234", ("abc1234", "")),
        ("fixed " + "a" * 40 + " — full sha", ("a" * 40, "full sha")),
        ("gr1 is already fixed by abc1234", None),
        ("fixed by abc1234 — prose", None),
        ("fixed abc12 — too short", None),
        ("fixed abc1234zzz — not a bare sha", None),
        ("the engine is down", None),
    ],
)
def test_parse_nofix_fixed_grammar(rest: str, expected: tuple[str, str] | None) -> None:
    assert fr.parse_nofix_fixed(rest) == expected


def test_nofix_fixed_line_still_parses_as_a_nofix_verdict() -> None:
    assert fr.parse_diagnosis("rescue: NOFIX fixed abc1234 — landed under gr9") == (
        "NOFIX",
        "fixed abc1234 — landed under gr9",
    )


def test_diagnose_prompt_documents_the_fixed_form(store: Store) -> None:
    root = _root(store)
    task = _rescued(store, root)
    diag = _minted_diagnose(store, task, root)
    assert "rescue: NOFIX fixed <sha>" in _body(store, diag)


# ── NOFIX fixed <sha>: the already-fixed close (gr635531) ──────────

SHA = "b2383e574"


class _GitCalls(list[list[str]]):
    """The git argvs ``run_slice._git`` saw; ``rc`` is what a
    ``merge-base`` answers (an exception instance is raised)."""

    rc: Any = 0


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> _GitCalls:
    """The fix repo is ``tmp_path``; ``run_slice._git`` answers
    ``merge-base --is-ancestor`` per ``git_calls.rc`` (0 = ancestor)."""
    from precis.workers.job_types import run_slice

    monkeypatch.setenv("PRECIS_FIX_REPO_DIR", str(tmp_path))
    rec = _GitCalls()

    def fake_git(args: list[str], cwd: Any) -> Any:
        assert str(cwd) == str(tmp_path)
        rec.append(list(args))
        if isinstance(rec.rc, BaseException):
            raise rec.rc
        rc = rec.rc if args[0] == "merge-base" else 0
        return SimpleNamespace(returncode=rc, stdout="", stderr="")

    monkeypatch.setattr(run_slice, "_git", fake_git)
    return rec


def _fixed_case(
    store: Store,
    reply: str,
    *,
    gripe_tags: tuple[str, ...] = (),
    gripe_status: str = "open",
) -> tuple[int, int, int, int]:
    """``(root, task, diag, gripe)``: a rescued builder under a
    gripe-fix plan whose diagnose job succeeded with ``reply``."""
    root = _root(store)
    gid = int(insert_gripe(store, "the defect", status=gripe_status).id)
    for t in gripe_tags:
        store.add_tag(gid, Tag.open(t), set_by="agent")
    store.add_link(src_ref_id=root, dst_ref_id=gid, relation="related-to")
    task = _rescued(store, root)
    diag = _minted_diagnose(store, task, root)
    _job(store, diag, status="succeeded")
    _comment(store, diag, reply)
    return root, task, diag, gid


def _gripe_comments(store: Store, gid: int) -> list[str]:
    return [
        str(c.text)
        for c in store.chunks.list_chunks_for_ref(gid)
        if c.chunk_kind == "gripe_comment"
    ]


_FIXED_REPLY = f"rescue: NOFIX fixed {SHA} — gr1 was fixed under another plan"


def test_nofix_fixed_verified_closes_node_and_resolves_gripe(
    store: Store, git_calls: list[list[str]]
) -> None:
    root, task, diag, gid = _fixed_case(store, _FIXED_REPLY)

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))

    assert res.asked == [] and res.closed == [task]
    assert _ask_refs(store) == []
    assert ["merge-base", "--is-ancestor", SHA, "origin/main"] in git_calls
    assert _status(store, task) == "won't-do"
    assert any(SHA in c and "already fixed" in c for c in _comments(store, task))
    assert _meta(store, task)["rescue"]["fixed_by"] == SHA
    assert _status(store, diag) == "done"
    (resolved,) = _gripe_comments(store, gid)
    assert resolved.startswith(f"resolved: {SHA} — gr1 was fixed under another plan")
    assert _status(store, gid) == "verify"
    # a second pass has nothing left to do
    res2 = fr.run_rescue_pass(store, NOW + timedelta(minutes=2))
    assert res2.asked == [] and _ask_refs(store) == []


def test_nofix_fixed_dry_run_writes_nothing(
    store: Store, git_calls: list[list[str]]
) -> None:
    root, task, diag, gid = _fixed_case(store, _FIXED_REPLY)

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=1), dry_run=True)

    assert (task, "close") in res.would
    assert all(a[0] != "fetch" for a in git_calls)
    assert _status(store, task) == "open"
    assert _status(store, gid) == "open" and _gripe_comments(store, gid) == []


@pytest.mark.parametrize(
    "rc",
    [1, 128, subprocess.TimeoutExpired("git", 1), FileNotFoundError("git")],
    ids=["not-ancestor", "bad-object", "timeout", "git-missing"],
)
def test_nofix_fixed_unverified_falls_through_to_the_ask(
    store: Store, git_calls: _GitCalls, rc: Any
) -> None:
    git_calls.rc = rc
    root, task, diag, gid = _fixed_case(store, _FIXED_REPLY)

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))

    assert res.asked == [task] and res.closed == []
    (ask,) = _ask_refs(store)
    assert _blocked_by(store, task) == [diag, ask]
    assert _status(store, task) == "open"
    assert _status(store, gid) == "open" and _gripe_comments(store, gid) == []


def test_nofix_fixed_without_a_fix_repo_asks(
    store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("PRECIS_FIX_REPO_DIR", str(tmp_path / "missing"))
    root, task, diag, gid = _fixed_case(store, _FIXED_REPLY)

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))

    assert res.asked == [task] and _status(store, task) == "open"


def test_nofix_fixed_with_no_plan_gripe_asks(
    store: Store, git_calls: list[list[str]]
) -> None:
    root = _root(store)
    task = _rescued(store, root)
    diag = _minted_diagnose(store, task, root)
    _job(store, diag, status="succeeded")
    _comment(store, diag, _FIXED_REPLY)

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))

    assert res.asked == [task] and _status(store, task) == "open"


def test_nofix_fixed_respects_hold_no_autoclose(
    store: Store, git_calls: list[list[str]]
) -> None:
    root, task, diag, gid = _fixed_case(
        store, _FIXED_REPLY, gripe_tags=("hold:no-autoclose",)
    )

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))

    assert res.asked == [] and _status(store, task) == "won't-do"
    assert _status(store, gid) == "open" and _gripe_comments(store, gid) == []


@pytest.mark.parametrize("gstatus", ["done", "verify", "wontfix"])
def test_nofix_fixed_leaves_a_settled_gripe_alone(
    store: Store, git_calls: _GitCalls, gstatus: str
) -> None:
    root, task, diag, gid = _fixed_case(store, _FIXED_REPLY, gripe_status=gstatus)

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))

    assert res.asked == [] and _status(store, task) == "won't-do"
    assert _status(store, gid) == gstatus and _gripe_comments(store, gid) == []


def test_legacy_plain_nofix_naming_a_sha_still_asks(
    store: Store, git_calls: list[list[str]]
) -> None:
    root, task, diag, gid = _fixed_case(
        store, f"rescue: NOFIX — gr1 is already fixed by {SHA} on main"
    )

    res = fr.run_rescue_pass(store, NOW + timedelta(minutes=1))

    assert res.asked == [task] and _status(store, task) == "open"
    assert git_calls == []  # prose is never parsed for a sha
    assert _status(store, gid) == "open"


# ── the tick job ───────────────────────────────────────────────────


def test_tick_runs_rescue_pass_after_stall_pass(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.workers.job_types import factory_rescue_tick

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
    root = _root(store)
    _rescued(store, root)

    factory_rescue_tick._dispatch(ctx, factory_rescue_tick.SPEC)

    assert failures == []
    assert "factory_rescue" in meta_seen
    assert "factory_rescue_diagnose" in meta_seen
    assert meta_seen["factory_rescue_diagnose"]["would"] == 1
    assert any(k == "job_summary" for k, _ in chunks)
    assert _diagnose_tasks(store) == []

    # a raising rescue pass still lets the stall pass report its counts
    def boom(store: Store, now: datetime, dry_run: bool = False) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(factory_rescue_tick, "run_rescue_pass", boom)
    meta_seen.clear()
    factory_rescue_tick._dispatch(ctx, factory_rescue_tick.SPEC)
    assert "factory_rescue" in meta_seen  # the stall pass still ran
    assert failures and "raised" in failures[0]

    # and a raising stall pass still lets the rescue pass run
    seen: list[str] = []

    def ok(store: Store, now: datetime, dry_run: bool = False) -> Any:
        seen.append("rescue")
        return fr.RescuePassResult(dry_run=dry_run)

    monkeypatch.setattr(factory_rescue_tick, "run_stall_pass", boom)
    monkeypatch.setattr(factory_rescue_tick, "run_rescue_pass", ok)
    meta_seen.clear()
    failures.clear()
    factory_rescue_tick._dispatch(ctx, factory_rescue_tick.SPEC)
    assert seen == ["rescue"]
    assert "factory_rescue_diagnose" in meta_seen
    assert failures and "raised" in failures[0]


# ── one open ask per node and class (gr598110) ─────────────────────


def _rescue_ask(
    store: Store, root: int, task: int, cls: str, *, status: str = "open"
) -> int:
    """An open ruling ask blocking ``task``, shaped like ``_ask_node``'s."""
    ask = _node(
        store,
        root,
        title=f"td{task}: in rescue ({cls}) — stalled: retry?",
        meta={
            "ask": {
                "kind": "ruling",
                "options": ["retry"],
                "blocks": f"td{task}",
                "context": "test",
            },
            "rescue_cls": cls,
        },
        status=status,
        tags=("ask-user",),
    )
    store.add_link(
        src_ref_id=task, dst_ref_id=ask, relation="blocked-by", set_by="system"
    )
    return ask


def test_open_same_class_ask_gets_a_comment_not_a_new_ask(store: Store) -> None:
    """gr598110: a tick that finds the same class ask open comments on
    it — never a second ask (td597226/td597612/td597977). The ask here
    predates the tag: a re-tag moved ``tag_at`` past its mint, so the
    ``_episode_ask`` bound no longer covers it — the class check does."""
    root = _root(store)
    task = _node(store, root)
    ask = _rescue_ask(store, root, task, "stalled")
    fr.enter_rescue(store, task, "stalled", _ev())

    res = fr.run_rescue_pass(store, NOW)

    assert res.asked == [] and res.diagnosing == []
    assert _ask_refs(store) == [ask]
    assert any("no new ask" in c for c in _comments(store, ask))


def test_retag_after_answered_ask_rearms_releases_the_claim(store: Store) -> None:
    """gr598110 root cause: ``_step_rearm`` cleared ``rescue:stalled``
    on the answer, the dead ``claimed-by:`` kept the node undispatched
    and the watchdog re-tagged — the same episode re-arms (claim
    released), it never re-asks."""
    root = _root(store)
    task = _rescued(store, root, cls="stalled", status="doing")
    job = _newest_job(store, task)
    ask = _rescue_ask(store, root, task, "stalled", status="done")
    store.stamp_ref_meta(
        task, {"rescue_answered": {"cls": "stalled", "ask": ask, "job": job}}
    )
    store.add_tag(task, Tag.open("claimed-by:lane-devin"), set_by="system")

    res = fr.run_rescue_pass(store, NOW)

    assert res.asked == [] and res.rearmed == [task]
    assert _rescue_tags(store, task) == []
    assert "claimed-by:lane-devin" not in _open_tags(store, task)
    assert _status(store, task) == "open"

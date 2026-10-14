"""``memory_thread_sweep`` — gr485358's scheduled thread-node retirement.

DB-backed: real ``section:threads`` memories through
:class:`precis.handlers.memory.MemoryHandler` (body lands in a
``memory_body`` chunk, tags through the normal path), then the pass
itself. No LLM, no network.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.store.types import Tag
from precis.workers.memory_thread_sweep import (
    plan_thread_sweep,
    run_memory_thread_sweep_pass,
)
from tests.conftest import id_of


@pytest.fixture
def handler(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def _thread(handler: MemoryHandler, body: str, *, title: str = "Campaign") -> int:
    r = handler.put(
        text=body,
        title=title,
        tags=["SPACE:repo-dev", "section:threads"],
    )
    return id_of(r.body)


def _memory(handler: MemoryHandler, body: str, *, tags: list[str]) -> int:
    return id_of(handler.put(text=body, title="Note", tags=tags).body)


def _age(store: Any, ref_id: int, days: int) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = now() - make_interval(days => %s) "
            "WHERE ref_id = %s",
            (days, ref_id),
        )
        conn.commit()


def _retired(store: Any, ref_id: int) -> bool:
    return store.fetch_refs_by_ids([ref_id])[ref_id].retired_at is not None


def test_rule_a_retires_a_left_none_thread(store: Any, handler: MemoryHandler) -> None:
    tid = _thread(handler, "Left: none\n\nWrapped up; the sha list is in main.")

    result = run_memory_thread_sweep_pass(store)

    assert (result.claimed, result.ok, result.failed) == (1, 1, 0)
    assert _retired(store, tid)


def test_rule_b_retires_a_superseded_thread(store: Any, handler: MemoryHandler) -> None:
    old = _thread(handler, "Left: port the sync\n\nV1 of the campaign.")
    new = _thread(handler, "Left: ship the sync\n\nV2, picking up.", title="Campaign")
    store.add_link(
        src_ref_id=new,
        dst_ref_id=old,
        relation="supersedes",
        set_by="agent",
    )

    result = run_memory_thread_sweep_pass(store)

    assert result.ok == 1
    assert _retired(store, old)
    assert not _retired(store, new)
    meta = store.fetch_refs_by_ids([old])[old].meta or {}
    assert meta.get("superseded_by") == new


def test_rule_b_ignores_a_prose_only_supersedes(
    store: Any, handler: MemoryHandler
) -> None:
    """ "Supersedes meN" in prose retires nothing — only the edge counts
    (memory-lint's job to flag the drift)."""
    old = _thread(handler, "Left: port the sync\n\nV1.")
    _thread(handler, f"Left: ship it\n\nSupersedes me{old}", title="Campaign")

    result = run_memory_thread_sweep_pass(store)

    assert result.ok == 0
    assert not _retired(store, old)


def test_rule_c_retires_a_stale_thread_and_files_the_leftover(
    store: Any, handler: MemoryHandler
) -> None:
    tid = _thread(handler, "Left: land the parser slice\n\nDetail.")
    _age(store, tid, 20)

    result = run_memory_thread_sweep_pass(store)

    assert result.ok == 1
    assert _retired(store, tid)
    # the Left: line became a todo linked back to the retired node
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT r.ref_id, r.title FROM refs r WHERE r.kind = 'todo' "
            "AND r.retired_at IS NULL "
            "AND (r.meta->>'memory_thread_sweep_from')::int = %s",
            (tid,),
        ).fetchone()
    assert row is not None
    todo_id = int(row[0])
    assert "land the parser slice" in row[1]
    tags = {str(t) for t in store.tags_for(todo_id)}
    assert "STATUS:open" in tags
    with store.pool.connection() as conn:
        link = conn.execute(
            "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
            "AND relation = 'related-to'",
            (todo_id, tid),
        ).fetchone()
    assert link is not None


def test_rule_c_without_a_left_line_mints_no_todo(
    store: Any, handler: MemoryHandler
) -> None:
    tid = _thread(handler, "Stale body with no Left marker.")
    _age(store, tid, 30)

    result = run_memory_thread_sweep_pass(store)

    assert result.ok == 1
    assert _retired(store, tid)
    with store.pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM refs WHERE kind='todo' AND retired_at IS NULL"
        ).fetchone()
    assert n is not None and int(n[0]) == 0


def test_a_fresh_thread_node_is_untouched(store: Any, handler: MemoryHandler) -> None:
    tid = _thread(handler, "Left: keep going\n\nActive campaign.")

    result = run_memory_thread_sweep_pass(store)

    assert (result.claimed, result.ok) == (0, 0)
    assert not _retired(store, tid)


def test_non_thread_sections_and_hubs_are_untouched(
    store: Any, handler: MemoryHandler
) -> None:
    """Negatives: a stale runbook and a ``section:index`` hub that someone
    also tagged ``threads`` must both survive — the sweep's scope is the
    threads tag on non-hub memories only."""
    runbook = _memory(
        handler, "Left: none\n\nA durable recipe.", tags=["section:runbooks"]
    )
    hub_node = _memory(
        handler,
        "Left: none\n\nThe threads hub.",
        tags=["section:index", "section:threads"],
    )
    other_kind = store.insert_ref(kind="todo", slug=None, title="not a memory")
    store.add_tag(other_kind.id, Tag.open("section:threads"), set_by="system")
    for rid in (runbook, hub_node, other_kind.id):
        _age(store, rid, 60)

    result = run_memory_thread_sweep_pass(store)

    assert result.claimed == 0
    assert not _retired(store, runbook)
    assert not _retired(store, hub_node)
    assert not _retired(store, other_kind.id)


def test_dry_run_lists_without_writing(
    store: Any, handler: MemoryHandler, caplog: Any
) -> None:
    tid = _thread(handler, "Left: none\n\nDone.")
    _age(store, tid, 1)

    with caplog.at_level(logging.INFO, logger="precis.workers.memory_thread_sweep"):
        result = run_memory_thread_sweep_pass(store, dry_run=True)

    assert result.claimed == 1
    assert result.ok == 0
    assert not _retired(store, tid)
    assert f"would retire me{tid} rule=a" in caplog.text


def test_re_run_is_a_no_op(store: Any, handler: MemoryHandler) -> None:
    _thread(handler, "Left: none\n\nDone.")

    first = run_memory_thread_sweep_pass(store)
    second = run_memory_thread_sweep_pass(store)

    assert first.ok == 1
    assert (second.claimed, second.ok, second.failed) == (0, 0, 0)


def test_plan_reports_each_rule(store: Any, handler: MemoryHandler) -> None:
    a = _thread(handler, "Left: none\n\nDone.", title="Campaign A")
    b = _thread(handler, "Left: more\n\nOld.", title="Campaign B")
    store.add_link(src_ref_id=a, dst_ref_id=b, relation="supersedes", set_by="agent")
    c = _thread(handler, "Left: carry me\n\nStale.", title="Campaign C")
    _age(store, c, 15)

    plan = {item.ref_id: item for item in plan_thread_sweep(store)}

    assert plan[a].rule == "a"
    assert plan[b].rule == "b"
    assert plan[b].superseded_by == a
    assert plan[c].rule == "c"
    assert plan[c].left == "carry me"

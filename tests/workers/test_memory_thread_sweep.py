"""``memory_thread_sweep`` — gr485358's scheduled thread-node retirement.

DB-backed: real ``section:threads`` memories through
:class:`precis.handlers.memory.MemoryHandler` (body lands in a
``memory_body`` chunk, tags through the normal path), then the pass
itself. No LLM, no network.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.store.types import Tag
from precis.utils.memory_threads import STALE_DAYS
from precis.workers.memory_thread_sweep import (
    SCRATCH_FLOOR_DAYS,
    SCRATCH_OWNER_GRACE_DAYS,
    SCRATCH_TTL_DAYS,
    plan_scratch_sweep,
    plan_thread_sweep,
    run_memory_thread_sweep_pass,
    run_scratch_sweep_pass,
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


def test_rule_s_never_retires_a_currency_suspect(
    store: Any, handler: MemoryHandler
) -> None:
    """gr503035: a stale thread whose body carries a dated section inside
    the stale window claims to be current — a currency suspect, listed but
    never retired."""
    today = datetime.now(UTC).date().isoformat()
    tid = _thread(handler, f"Left: hold for Reto\n\n## {today}\nNewest live state.")
    _age(store, tid, 20)

    plan = {item.ref_id: item for item in plan_thread_sweep(store)}
    assert plan[tid].rule == "s"
    result = run_memory_thread_sweep_pass(store)
    assert (result.claimed, result.ok, result.failed) == (0, 0, 0)
    assert not _retired(store, tid)


def test_rule_c_still_retires_when_dates_are_all_old(
    store: Any, handler: MemoryHandler
) -> None:
    old = (datetime.now(UTC) - timedelta(days=30)).date().isoformat()
    tid = _thread(handler, f"Left: carry\n\n## {old}\nStale state.")
    _age(store, tid, 20)

    result = run_memory_thread_sweep_pass(store)

    assert result.ok == 1
    assert _retired(store, tid)


def test_a_retired_thread_leaves_no_links(store: Any, handler: MemoryHandler) -> None:
    """gr503031: the sweep's retire path drops the node's edges too."""
    tid = _thread(handler, "Left: none\n\nDone.")
    other = _memory(handler, "a gotcha", tags=["section:gotchas"])
    store.add_link(
        src_ref_id=other, dst_ref_id=tid, relation="qualifies", set_by="agent"
    )
    store.add_link(
        src_ref_id=tid, dst_ref_id=other, relation="related-to", set_by="agent"
    )

    run_memory_thread_sweep_pass(store)

    assert _retired(store, tid)
    with store.pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM links WHERE src_ref_id = %s OR dst_ref_id = %s",
            (tid, tid),
        ).fetchone()
    assert n is not None and int(n[0]) == 0


# ---------------------------------------------------------------------------
# rule s — the SPACE:scratch TTL sweep (gr568398)
# ---------------------------------------------------------------------------


def _scratch(
    handler: MemoryHandler,
    body: str,
    *,
    title: str = "Scratch",
    meta: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> int:
    return id_of(
        handler.put(
            text=body,
            title=title,
            tags=[*(tags or []), "SPACE:scratch"],
            meta=meta,
        ).body
    )


def test_rule_s_retires_a_scratch_node_past_the_default_ttl(
    store: Any, handler: MemoryHandler
) -> None:
    sid = _scratch(handler, "a probe")
    _age(store, sid, SCRATCH_TTL_DAYS + 1)

    plan = {item.ref_id: item for item in plan_scratch_sweep(store)}
    assert plan[sid].rule == "s"
    result = run_scratch_sweep_pass(store)

    assert (result.claimed, result.ok, result.failed) == (1, 1, 0)
    assert _retired(store, sid)


def test_rule_s_leaves_a_fresh_scratch_node_alone(
    store: Any, handler: MemoryHandler
) -> None:
    sid = _scratch(handler, "still useful")
    _age(store, sid, SCRATCH_TTL_DAYS - 1)

    result = run_scratch_sweep_pass(store)

    assert (result.claimed, result.ok) == (0, 0)
    assert not _retired(store, sid)


def test_rule_s_meta_expires_retires_before_the_default_ttl(
    store: Any, handler: MemoryHandler
) -> None:
    """``meta.expires`` overrides shorter too: a fresh node whose declared
    expiry already passed retires anyway."""
    sid = _scratch(
        handler,
        "short-lived",
        meta={"expires": (datetime.now(UTC) - timedelta(days=1)).isoformat()},
    )

    result = run_scratch_sweep_pass(store)

    assert result.ok == 1
    assert _retired(store, sid)


def test_rule_s_meta_expires_extends_past_the_default_ttl(
    store: Any, handler: MemoryHandler
) -> None:
    sid = _scratch(
        handler,
        "keep until the round closes",
        meta={"expires": "30d"},
    )
    _age(store, sid, SCRATCH_TTL_DAYS + 10)

    result = run_scratch_sweep_pass(store)

    assert (result.claimed, result.ok) == (0, 0)
    assert not _retired(store, sid)


def test_rule_s_raw_duration_expires_counts_from_updated_at(
    store: Any, handler: MemoryHandler
) -> None:
    """A duration meta.expires that bypassed write validation still
    resolves — relative to ``updated_at``, so it slides with edits."""
    sid = _scratch(handler, "stamped by hand")
    store.stamp_ref_meta(sid, {"expires": "2d"})
    _age(store, sid, 3)

    result = run_scratch_sweep_pass(store)

    assert result.ok == 1
    assert _retired(store, sid)


def test_rule_s_unparseable_expires_falls_back_to_the_default_ttl(
    store: Any, handler: MemoryHandler
) -> None:
    sid = _scratch(handler, "typo'd expiry")
    store.stamp_ref_meta(sid, {"expires": "whenever"})
    _age(store, sid, SCRATCH_TTL_DAYS + 1)

    result = run_scratch_sweep_pass(store)

    assert result.ok == 1
    assert _retired(store, sid)


def test_rule_s_never_touches_other_spaces(store: Any, handler: MemoryHandler) -> None:
    """The untagged default stays ``SPACE:research`` — an old research
    node and a node double-tagged with a second space both survive."""
    research = _memory(handler, "a durable note", tags=[])
    # A second SPACE: value can only exist by bypassing the handler's
    # replace_prefix tag write — the ambiguous state the sweep refuses.
    mixed = _scratch(handler, "ambiguous")
    store.add_tag(mixed, Tag.closed("SPACE", "research"), set_by="system")
    for rid in (research, mixed):
        _age(store, rid, 60)

    result = run_scratch_sweep_pass(store)

    assert (result.claimed, result.ok) == (0, 0)
    assert not _retired(store, research)
    assert not _retired(store, mixed)


def test_rule_s_skips_a_section_index_hub(store: Any, handler: MemoryHandler) -> None:
    hub_node = _scratch(handler, "a scratch hub", tags=["section:index"], title="Hub")
    _age(store, hub_node, 60)

    result = run_scratch_sweep_pass(store)

    assert (result.claimed, result.ok) == (0, 0)
    assert not _retired(store, hub_node)


def test_rule_s_dry_run_lists_without_writing(
    store: Any, handler: MemoryHandler, caplog: Any
) -> None:
    sid = _scratch(handler, "a probe")
    _age(store, sid, SCRATCH_TTL_DAYS + 1)

    with caplog.at_level(logging.INFO, logger="precis.workers.memory_thread_sweep"):
        result = run_scratch_sweep_pass(store, dry_run=True)

    assert result.claimed == 1
    assert result.ok == 0
    assert not _retired(store, sid)
    assert f"would retire me{sid} rule=s" in caplog.text


def test_rule_s_retirement_drops_links(store: Any, handler: MemoryHandler) -> None:
    sid = _scratch(handler, "linked scratch")
    other = _memory(handler, "a gotcha", tags=["section:gotchas"])
    store.add_link(
        src_ref_id=other, dst_ref_id=sid, relation="qualifies", set_by="agent"
    )
    _age(store, sid, SCRATCH_TTL_DAYS + 1)

    run_scratch_sweep_pass(store)

    assert _retired(store, sid)
    with store.pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM links WHERE src_ref_id = %s OR dst_ref_id = %s",
            (sid, sid),
        ).fetchone()
    assert n is not None and int(n[0]) == 0


def test_rule_s_re_run_is_a_no_op(store: Any, handler: MemoryHandler) -> None:
    sid = _scratch(handler, "a probe")
    _age(store, sid, SCRATCH_TTL_DAYS + 1)

    first = run_scratch_sweep_pass(store)
    second = run_scratch_sweep_pass(store)

    assert first.ok == 1
    assert (second.claimed, second.ok, second.failed) == (0, 0, 0)


# ---------------------------------------------------------------------------
# scratch/thread overlap — which pass owns a SPACE:scratch thread node
# ---------------------------------------------------------------------------


def test_a_scratch_thread_node_is_the_scratch_pass(
    store: Any, handler: MemoryHandler
) -> None:
    """Regression (td575123 on td575122): a ``section:threads`` node whose
    only ``SPACE:`` is ``scratch`` is scratch-owned end to end — rule c's
    14-day clock must not cut a 30-day ``meta.expires`` short, however far
    ``now`` runs. The scratch pass alone retires it, on its own clock."""
    sid = _scratch(
        handler,
        "Left: keep the probe running\n\nThirty-day experiment.",
        tags=["section:threads"],
        meta={"expires": "30d"},
    )
    _age(store, sid, STALE_DAYS + 1)

    assert run_memory_thread_sweep_pass(store).claimed == 0
    assert run_scratch_sweep_pass(store).claimed == 0
    assert not _retired(store, sid)

    later = datetime.now(UTC) + timedelta(days=31)
    assert run_memory_thread_sweep_pass(store, now=later).claimed == 0
    assert run_scratch_sweep_pass(store, now=later).ok == 1
    assert _retired(store, sid)


def test_scratch_ownership_boundary_between_the_passes(
    store: Any, handler: MemoryHandler
) -> None:
    """The boundary both passes agree on: rule a does not fire on a
    scratch thread (``Left: none`` waits out the TTL — scratch means
    self-cleaning), and a second ``SPACE:`` value puts the node back
    under thread rules — the scratch pass refuses ambiguous space tags,
    so nothing falls between the passes."""
    done = _scratch(handler, "Left: none\n\nFinished probe.", tags=["section:threads"])
    _age(store, done, SCRATCH_TTL_DAYS + 1)
    mixed = _scratch(
        handler, "Left: carry\n\nAmbiguous space.", tags=["section:threads"]
    )
    store.add_tag(mixed, Tag.closed("SPACE", "research"), set_by="system")
    _age(store, mixed, STALE_DAYS + 1)

    thread = run_memory_thread_sweep_pass(store)

    assert (thread.claimed, thread.ok) == (1, 1)
    assert not _retired(store, done)
    assert _retired(store, mixed)

    scratch = run_scratch_sweep_pass(store)

    assert (scratch.claimed, scratch.ok) == (1, 1)
    assert _retired(store, done)


# ---------------------------------------------------------------------------
# scratch-for:<owner> — expiry keyed to the owning task's close (gr605668)
# ---------------------------------------------------------------------------


def _owner_task(
    store: Any,
    *,
    kind: str = "todo",
    status: str = "open",
    closed_days: int | None = None,
) -> int:
    """A task row (todo or gripe) carrying ``STATUS:<status>`` —
    ``closed_days`` backdates the tag's ``ref_tags.created_at`` so the
    close stamp lands where the test needs it. One transaction: the
    deferred gripe-STATUS check (migration 0176) fires at commit."""
    with store.tx() as conn:
        ref = store.insert_ref(kind=kind, slug=None, title="task", conn=conn)
        store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="agent", conn=conn)
    if closed_days is not None:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE ref_tags rt SET created_at = "
                "now() - make_interval(days => %s) "
                "FROM tags t "
                "WHERE rt.tag_id = t.tag_id AND rt.ref_id = %s "
                "AND t.namespace = 'STATUS' AND t.value = %s",
                (closed_days, ref.id, status),
            )
            conn.commit()
    return int(ref.id)


def test_rule_s_an_open_owner_keeps_scratch_past_the_default_ttl(
    store: Any, handler: MemoryHandler
) -> None:
    """The gripe's driver: wall-clock expiry loses a long task's notes
    mid-flight — an open owner suspends the 7-day clock; only the 30-day
    floor still runs."""
    tid = _owner_task(store)
    sid = _scratch(handler, "mid-flight probe", tags=[f"scratch-for:td{tid}"])
    _age(store, sid, SCRATCH_TTL_DAYS + 5)

    result = run_scratch_sweep_pass(store)

    assert (result.claimed, result.ok) == (0, 0)
    assert not _retired(store, sid)


def test_rule_s_a_closed_owner_expires_scratch_after_the_grace(
    store: Any, handler: MemoryHandler
) -> None:
    """The other half: the task closed 5 days ago, so its one-day-old
    scratch is already past the close+grace expiry — wall-clock youth
    does not keep a finished task's notes in recall."""
    tid = _owner_task(store, status="done", closed_days=SCRATCH_OWNER_GRACE_DAYS + 2)
    sid = _scratch(handler, "finished probe", tags=[f"scratch-for:td{tid}"])

    plan = {item.ref_id: item for item in plan_scratch_sweep(store)}
    assert f"owner td{tid} closed" in plan[sid].reason
    result = run_scratch_sweep_pass(store)

    assert (result.claimed, result.ok, result.failed) == (1, 1, 0)
    assert _retired(store, sid)


def test_rule_s_a_closed_owner_inside_grace_keeps_scratch(
    store: Any, handler: MemoryHandler
) -> None:
    """Closed yesterday — inside the 3-day grace — keeps the scratch
    even though the 7-day default has already passed; once the grace
    lapses it goes."""
    tid = _owner_task(store, status="done", closed_days=1)
    sid = _scratch(handler, "grace-window probe", tags=[f"scratch-for:td{tid}"])
    _age(store, sid, SCRATCH_TTL_DAYS + 5)

    assert run_scratch_sweep_pass(store).claimed == 0
    assert not _retired(store, sid)

    later = datetime.now(UTC) + timedelta(days=SCRATCH_OWNER_GRACE_DAYS)
    assert run_scratch_sweep_pass(store, now=later).ok == 1
    assert _retired(store, sid)


def test_rule_s_the_floor_dissolves_scratch_of_a_never_closing_task(
    store: Any, handler: MemoryHandler
) -> None:
    """The hard floor stays: a task that is still open (or never
    closes) cannot pin scratch past 30 days untouched."""
    tid = _owner_task(store)
    sid = _scratch(handler, "orphaned probe", tags=[f"scratch-for:td{tid}"])
    _age(store, sid, SCRATCH_FLOOR_DAYS + 1)

    result = run_scratch_sweep_pass(store)

    assert result.ok == 1
    assert _retired(store, sid)


def test_rule_s_meta_expires_overrides_the_owner_clock(
    store: Any, handler: MemoryHandler
) -> None:
    """``meta.expires`` overrides both clocks: a scratch pinned 30 days
    survives its owner's close+grace expiry."""
    tid = _owner_task(store, status="done", closed_days=SCRATCH_OWNER_GRACE_DAYS + 5)
    sid = _scratch(
        handler,
        "pinned past the close",
        tags=[f"scratch-for:td{tid}"],
        meta={"expires": "30d"},
    )

    result = run_scratch_sweep_pass(store)

    assert (result.claimed, result.ok) == (0, 0)
    assert not _retired(store, sid)


def test_rule_s_an_unresolvable_owner_keeps_the_default_ttl(
    store: Any, handler: MemoryHandler
) -> None:
    """A ``scratch-for:`` naming no live todo/gripe — or a handle
    outside the td/gr grammar — is ignored: the node stays on the
    7-day clock (a typo must not make a throwaway node immortal)."""
    dangling = _scratch(handler, "dead handle", tags=["scratch-for:td999999999"])
    not_a_task = _scratch(handler, "memory handle", tags=["scratch-for:me1"])
    for rid in (dangling, not_a_task):
        _age(store, rid, SCRATCH_TTL_DAYS + 1)

    result = run_scratch_sweep_pass(store)

    assert (result.claimed, result.ok) == (2, 2)
    assert _retired(store, dangling)
    assert _retired(store, not_a_task)


def test_rule_s_a_gripe_owner_closes_scratch_too(
    store: Any, handler: MemoryHandler
) -> None:
    """``scratch-for:gr<N>`` — a gripe closed ``wontfix`` is as terminal
    as a todo's ``done``."""
    gid = _owner_task(
        store, kind="gripe", status="wontfix", closed_days=SCRATCH_OWNER_GRACE_DAYS + 2
    )
    sid = _scratch(handler, "gripe probe", tags=[f"scratch-for:gr{gid}"])

    result = run_scratch_sweep_pass(store)

    assert result.ok == 1
    assert _retired(store, sid)


def test_rule_s_a_retired_owner_counts_as_closed(
    store: Any, handler: MemoryHandler
) -> None:
    """A tombstoned task never got a terminal STATUS — its
    ``retired_at`` is the close stamp, and the grace runs from it."""
    tid = _owner_task(store)
    store.retire_ref(tid)
    sid = _scratch(handler, "dead task's probe", tags=[f"scratch-for:td{tid}"])
    _age(store, sid, SCRATCH_TTL_DAYS + 5)

    assert run_scratch_sweep_pass(store).claimed == 0
    assert not _retired(store, sid)

    later = datetime.now(UTC) + timedelta(days=SCRATCH_OWNER_GRACE_DAYS + 1)
    assert run_scratch_sweep_pass(store, now=later).ok == 1
    assert _retired(store, sid)


def test_rule_s_multiple_owners_wait_for_the_last_close(
    store: Any, handler: MemoryHandler
) -> None:
    """Two named owners: one open is enough to keep the scratch — it
    dissolves on the last close, not the first."""
    open_t = _owner_task(store)
    done_t = _owner_task(store, status="done", closed_days=SCRATCH_OWNER_GRACE_DAYS + 5)
    sid = _scratch(
        handler,
        "two tasks' probe",
        tags=[f"scratch-for:td{open_t}", f"scratch-for:td{done_t}"],
    )
    _age(store, sid, SCRATCH_TTL_DAYS + 5)

    assert run_scratch_sweep_pass(store).claimed == 0
    assert not _retired(store, sid)

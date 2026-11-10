"""Memory graph-environment and shape-rollup views (gr570722)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from precis.runtime import PrecisRuntime
from precis.store import Store, Tag
from precis.store.types import ChunkInsert

pytestmark = pytest.mark.db

_NOW = datetime.now(UTC)


def _memory(
    store: Store,
    title: str,
    body: str = "body text",
    *,
    section: str | None = None,
    space: str | None = None,
    updated_days_ago: int | None = None,
) -> int:
    ref = store.insert_ref(kind="memory", slug=None, title=title, meta={})
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})],
    )
    if section is not None:
        store.add_tag(ref.id, Tag.open(f"section:{section}"), set_by="system")
    if space is not None:
        store.add_tag(ref.id, Tag.closed("SPACE", space), set_by="system")
    if updated_days_ago is not None:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET updated_at = %s WHERE ref_id = %s",
                (_NOW - timedelta(days=updated_days_ago), ref.id),
            )
    return int(ref.id)


def _get_call(
    store: Store,
    ref_id: int,
    session: str | None,
    *,
    arg_id: Any = None,
    ts: datetime | None = None,
) -> None:
    """One tool_calls row the way a ``get(id=…)`` dispatch logs it."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO tool_calls (verb, kind, outcome, session_id, ts, arg_sample) "
            "VALUES ('get', 'memory', 'ok', %s, %s, %s::jsonb)",
            (
                session,
                ts or _NOW,
                json.dumps({"id": arg_id if arg_id is not None else f"me{ref_id}"}),
            ),
        )


def _revisions(store: Store, ref_id: int, n: int, *, actor: str = "agent") -> None:
    # revisions.xact defaults to txid_current() and (target, xact) is unique —
    # one connection (transaction) per row so each gets its own xid.
    for _ in range(n):
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO revisions (target_kind, target_id, event, actor, "
                "reason, prev_state) VALUES ('ref', %s, 'edited', %s, 'test', "
                "'{}'::jsonb)",
                (ref_id, actor),
            )


def _environment(runtime: PrecisRuntime, ref_id: int, **options: Any) -> str:
    body, is_error = runtime.dispatch_with_status(
        "get",
        {
            "kind": "memory",
            "id": ref_id,
            "view": "environment",
            "__extras__": options or None,
        },
    )
    assert not is_error, body
    return body


def _shape(runtime: PrecisRuntime, **options: Any) -> str:
    body, is_error = runtime.dispatch_with_status(
        "search", {"kind": "memory", "view": "shape", **options}
    )
    assert not is_error, body
    return body


def _error(runtime: PrecisRuntime, verb: str, args: dict[str, Any]) -> str:
    body, is_error = runtime.dispatch_with_status(verb, args)
    assert is_error, body
    return body


def _smell_lines(body: str) -> str:
    assert "## smells" in body, body
    return body.split("## smells", 1)[1]


def test_environment_lists_neighbourhood_nodes_and_edges(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    hub = _memory(store, "Memory hub", section="index")
    member = _memory(store, "A reference", section="reference")
    gotcha = _memory(store, "A gotcha", section="gotchas")
    thread = _memory(store, "A thread", section="threads")
    store.add_link(src_ref_id=member, dst_ref_id=hub, relation="part-of")
    store.add_link(src_ref_id=gotcha, dst_ref_id=hub, relation="part-of")
    store.add_link(src_ref_id=thread, dst_ref_id=hub, relation="part-of")
    store.add_link(src_ref_id=gotcha, dst_ref_id=thread, relation="qualifies")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_reads (ref_id, n_agent, n_human) VALUES (%s, 40, 7)",
            (member,),
        )
    _revisions(store, member, 3)

    out = _environment(runtime_with_store, hub, hops=1)

    assert f"environment: me{hub}" in out
    assert "hops=1" in out and "4 nodes" in out
    for ref_id in (member, gotcha, thread):
        assert f"me{ref_id}" in out
    assert any(f"me{member}" in row and "reference" in row for row in out.splitlines())
    for needle in ("part-of", "qualifies", "explicit"):
        assert needle in out
    # A qualified gotcha and thread flag nothing.
    smells = _smell_lines(out)
    assert "unqualified-gotcha" not in smells
    assert "unqualified-thread" not in smells


def test_environment_hops_bound_the_walk(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    a = _memory(store, "Chain root", section="reference")
    b = _memory(store, "Chain middle", section="reference")
    c = _memory(store, "Chain end", section="reference")
    store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")
    store.add_link(src_ref_id=b, dst_ref_id=c, relation="related-to")

    one = _environment(runtime_with_store, a, hops=1)
    assert f"me{b}" in one and f"me{c}" not in one
    two = _environment(runtime_with_store, a, hops=2)
    assert f"me{c}" in two


def test_environment_refusals(runtime_with_store: PrecisRuntime, store: Store) -> None:
    ref_id = _memory(store, "Refusal target")
    for bad in ("0", "9", "x"):
        out = _error(
            runtime_with_store,
            "get",
            {
                "kind": "memory",
                "id": ref_id,
                "view": "environment",
                "__extras__": {"hops": bad},
            },
        )
        assert "hops=" in out, out
    out = _error(
        runtime_with_store,
        "get",
        {"kind": "memory", "id": [ref_id], "view": "environment"},
    )
    assert "one memory id" in out, out
    out = _error(
        runtime_with_store,
        "get",
        {"kind": "memory", "id": ref_id, "view": "links", "__extras__": {"hops": 2}},
    )
    assert "hops=" in out and "environment" in out, out


def test_environment_smells_flag_their_evidence(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    slug = _memory(store, "snake_case_title", section="reference")
    dated = _memory(store, "Snapshot 2026-01-05", section="threads")
    untyped = _memory(store, "No section tag")
    churn = _memory(store, "Churned claim", section="workflow")
    dead = _memory(store, "Hot dead end", section="reference")
    _revisions(store, churn, 10)
    for i in range(4):
        _get_call(store, dead, f"s{i}")
    for ref_id in (dated, untyped, churn, dead):
        store.add_link(src_ref_id=ref_id, dst_ref_id=slug, relation="related-to")

    out = _environment(runtime_with_store, slug, hops=1)
    smells = _smell_lines(out)
    for code in ("slug-title", "dated-title", "no-section", "edit-churn"):
        assert code in smells, smells
    assert f"me{dead}" in out and "hot-dead-end" in smells
    assert "unqualified-thread" in smells  # `dated` is an unqualified thread


def test_environment_counts_sessions_mentions_and_cross_kind(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    focus = _memory(store, "Focus node", section="reference")
    fan = _memory(store, "Mention fan", section="reference")
    store.add_link(src_ref_id=fan, dst_ref_id=focus, relation="related-to")
    for i in range(12):
        other = _memory(store, f"Citing {i}", section="reference")
        store.add_link(
            src_ref_id=other,
            dst_ref_id=fan,
            relation="related-to",
            meta={"auto": "mention"},
        )
    todo = store.insert_ref(kind="todo", slug=None, title="a todo", meta={})
    store.add_link(src_ref_id=focus, dst_ref_id=int(todo.id), relation="cites")

    _get_call(store, focus, "s1")
    _get_call(store, focus, "s1")
    _get_call(store, focus, "s2")
    _get_call(store, focus, None)  # no session — counts a read, not a session
    out = _environment(runtime_with_store, focus, hops=1)

    assert "mention-fan-leaf" in _smell_lines(out)
    assert f"me{fan}" in out
    # 3 get calls on focus across 2 named sessions (the NULL-session row
    # adds a read, not a session).
    assert "sess" in out
    assert "mention" in out  # generated-mention edges render origin=mention


def test_environment_co_read_pairs_flag_far_nodes(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    a = _memory(store, "Near focus", section="reference")
    x = _memory(store, "Middle one", section="reference")
    y = _memory(store, "Middle two", section="reference")
    b = _memory(store, "Far node", section="reference")
    store.add_link(src_ref_id=a, dst_ref_id=x, relation="related-to")
    store.add_link(src_ref_id=x, dst_ref_id=y, relation="related-to")
    store.add_link(src_ref_id=y, dst_ref_id=b, relation="related-to")
    for i in range(3):
        session = f"s{i}"
        _get_call(store, a, session)
        _get_call(store, b, session)
    out = _environment(runtime_with_store, a, hops=4)
    assert "co-read pairs" in out
    assert f"me{a} ↔ me{b}: 3 sessions, 3 hops" in out


def test_environment_mermaid_on_request(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    a = _memory(store, "Sketch root", section="index")
    b = _memory(store, "Sketch child", section="threads")
    store.add_link(src_ref_id=b, dst_ref_id=a, relation="part-of")
    out = _environment(runtime_with_store, a, hops=1, mermaid=True)
    assert "```mermaid" in out and "graph LR" in out
    assert f"m{b} --> m{a}" in out
    no_sketch = _environment(runtime_with_store, a, hops=1)
    assert "```mermaid" not in no_sketch


def test_shape_rollup_scopes_smells_under_the_hub(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    hub = _memory(store, "Scoped hub", section="index")
    flagged = _memory(store, "slug_titled_member", section="reference")
    outside = _memory(store, "outside_slug_title", section="reference")
    store.add_link(src_ref_id=flagged, dst_ref_id=hub, relation="part-of")

    out = _shape(runtime_with_store, under=f"me{hub}")
    assert f"shape: me{hub}" in out
    smells = _smell_lines(out)
    assert "slug-title" in smells and f"me{flagged}" in smells
    assert f"me{outside}" not in out


def test_shape_refusals(runtime_with_store: PrecisRuntime, store: Store) -> None:
    _memory(store, "Anything", section="reference")
    for extra in (
        {"q": "words"},
        {"mode": "semantic"},
        {"scope": "title"},
        {"status": "open"},
        {"page": 2},
        {"sort": "recency"},
    ):
        out = _error(
            runtime_with_store,
            "search",
            {"kind": "memory", "view": "shape", **extra},
        )
        assert "view='shape'" in out, (extra, out)


def test_shape_sweep_ready_children(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    hub = _memory(store, "Threads hub", section="index")
    parent = _memory(store, "Campaign summary", section="threads")
    child = _memory(
        store,
        "Done campaign",
        body="Left: none\n\nWrapped.",
        section="threads",
    )
    store.add_link(src_ref_id=parent, dst_ref_id=hub, relation="part-of")
    store.add_link(src_ref_id=child, dst_ref_id=parent, relation="part-of")
    out = _shape(runtime_with_store, under=f"me{hub}")
    assert "sweep-ready-children" in _smell_lines(out)
    assert f"me{child}" in out


def test_shape_co_read_pairs_disconnected_nodes(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    # a — mid — y — b inside the scope: three hops apart, so the pair
    # qualifies even though every link is part-of.
    hub = _memory(store, "Hub", section="index")
    a = _memory(store, "Pair member A", section="reference")
    mid = _memory(store, "Pair middle", section="reference")
    y = _memory(store, "Pair middle two", section="reference")
    b = _memory(store, "Pair member B", section="reference")
    store.add_link(src_ref_id=a, dst_ref_id=hub, relation="part-of")
    store.add_link(src_ref_id=mid, dst_ref_id=a, relation="part-of")
    store.add_link(src_ref_id=y, dst_ref_id=mid, relation="part-of")
    store.add_link(src_ref_id=b, dst_ref_id=y, relation="part-of")
    for i in range(3):
        session = f"s{i}"
        _get_call(store, a, session)
        _get_call(store, b, session)
    out = _shape(runtime_with_store, under=f"me{hub}")
    assert f"me{a} ↔ me{b}: 3 sessions, 3 hops" in out


def test_environment_counts_children_past_the_hop_cut(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """td627401: a part-of child beyond the hop boundary still counts —
    the cut must not invent a leaf for ``mention-fan-leaf``."""
    focus = _memory(store, "Focus", section="reference")
    fan = _memory(store, "Cited member", section="reference")
    child = _memory(store, "Child outside the cut", section="reference")
    store.add_link(src_ref_id=fan, dst_ref_id=focus, relation="related-to")
    store.add_link(src_ref_id=child, dst_ref_id=fan, relation="part-of")
    for i in range(12):
        other = _memory(store, f"Citing {i}", section="reference")
        store.add_link(src_ref_id=other, dst_ref_id=fan, relation="related-to")

    out = _environment(runtime_with_store, focus, hops=1)

    assert f"me{child}" not in out  # the boundary cut dropped it
    assert "mention-fan-leaf" not in _smell_lines(out)


def test_shape_counts_children_outside_the_tag_scope(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """td627401: same boundary rule on the ``view='shape'`` side — a
    tag-filtered scope keeps the member but drops its child."""
    hub = _memory(store, "Hub", section="index")
    fan = _memory(store, "Cited member", section="reference", space="personal")
    child = _memory(store, "Child outside the cut", section="reference")
    store.add_link(src_ref_id=fan, dst_ref_id=hub, relation="part-of")
    store.add_link(src_ref_id=child, dst_ref_id=fan, relation="part-of")
    for i in range(12):
        other = _memory(store, f"Citing {i}", section="reference")
        store.add_link(src_ref_id=other, dst_ref_id=fan, relation="related-to")

    out = _shape(runtime_with_store, under=f"me{hub}", tags=["SPACE:personal"])

    assert f"me{child}" not in out
    assert "mention-fan-leaf" not in _smell_lines(out)


def test_environment_smell_lines_carry_their_evidence(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """td627401: each flagged handle renders its computed evidence, not
    just the detector's gloss."""
    slug = _memory(store, "snake_case_title", section="reference")
    churn = _memory(store, "Churned claim", section="workflow")
    dead = _memory(store, "Hot dead end", section="reference")
    _revisions(store, churn, 10)
    for i in range(4):
        _get_call(store, dead, f"s{i}")
    for ref_id in (churn, dead):
        store.add_link(src_ref_id=ref_id, dst_ref_id=slug, relation="related-to")

    out = _environment(runtime_with_store, slug, hops=1)
    smells = _smell_lines(out)

    assert f"me{churn} — " in smells and "edits by" in smells
    assert f"me{dead} — 4 sessions, 1 mentions, 0 typed edges" in smells


def test_shape_co_read_pairs_ignore_paths_through_excluded_nodes(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """td627401: a bridge node the scope cut excluded is no gap — two
    hops through it means the pair is already linked."""
    hub = _memory(store, "Hub", section="index")
    mid = _memory(store, "Inner middle", section="reference")
    a = _memory(store, "Pair member A", section="reference", space="personal")
    b = _memory(store, "Pair member B", section="reference", space="personal")
    bridge = _memory(store, "Bridge excluded", section="reference")
    store.add_link(src_ref_id=a, dst_ref_id=hub, relation="part-of")
    store.add_link(src_ref_id=mid, dst_ref_id=hub, relation="part-of")
    store.add_link(src_ref_id=b, dst_ref_id=mid, relation="part-of")
    store.add_link(src_ref_id=a, dst_ref_id=bridge, relation="related-to")
    store.add_link(src_ref_id=bridge, dst_ref_id=b, relation="related-to")
    for i in range(3):
        session = f"s{i}"
        _get_call(store, a, session)
        _get_call(store, b, session)

    out = _shape(runtime_with_store, under=f"me{hub}", tags=["SPACE:personal"])

    assert "co-read pairs" not in out


def test_memory_renders_record_result_ids(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """td627401: the ledger row records WHICH refs a call returned —
    the recall audit's missing half (gr570722)."""
    a = _memory(store, "Parchment memory", body="parchment body")
    b = _memory(store, "Second memory", body="other body")

    def last_ids() -> list[int] | None:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT result_ids FROM tool_calls ORDER BY call_id DESC LIMIT 1"
            ).fetchone()
        assert row is not None
        return row[0]

    body, is_error = runtime_with_store.dispatch_with_status(
        "search", {"kind": "memory", "view": "index"}
    )
    assert not is_error, body
    assert sorted(last_ids() or []) == sorted([a, b])

    body, is_error = runtime_with_store.dispatch_with_status(
        "search", {"kind": "memory", "q": "parchment"}
    )
    assert not is_error, body
    assert last_ids() == [a]

    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "memory", "id": a, "view": "environment"}
    )
    assert not is_error, body
    assert last_ids() == [a]

    body, is_error = runtime_with_store.dispatch_with_status(
        "search", {"kind": "memory", "view": "shape"}
    )
    assert not is_error, body
    # Both nodes are untyped — ``no-section`` flags them both.
    assert sorted(last_ids() or []) == sorted([a, b])


def test_index_search_records_result_count(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """gr570722 data gap: a recall hit must land ``result_count`` in the
    tool-call ledger (was NULL/0 for every index search)."""
    _memory(store, "Ledger-visible memory", body="some body")
    body, is_error = runtime_with_store.dispatch_with_status(
        "search", {"kind": "memory", "view": "index"}
    )
    assert not is_error, body
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT result_count FROM tool_calls "
            "WHERE verb = 'search' AND kind = 'memory' "
            "ORDER BY call_id DESC LIMIT 1"
        ).fetchone()
    assert row is not None and row[0] is not None and row[0] >= 1, row

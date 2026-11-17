"""Missing-rationale scans retain position when earlier rows are backfilled."""

from __future__ import annotations

import ast

import pytest
from psycopg.types.json import Jsonb

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.memory import MemoryHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.types import ChunkInsert, Tag


def test_parallel_scopes_space_and_subtree(hub: Hub) -> None:
    store = hub.live_store
    root = store.insert_ref(kind="memory", slug=None, title="root").id
    src = store.insert_ref(kind="memory", slug=None, title="source").id
    dst = store.insert_ref(kind="memory", slug=None, title="target").id
    store.add_link(src_ref_id=src, dst_ref_id=root, relation="part-of")
    store.add_tag(src, Tag.closed("SPACE", "repo-dev"), set_by="agent")
    store.chunks.insert_chunks(dst, [ChunkInsert(ord=0, text="passage", meta={})])
    whole = store.add_link(src_ref_id=src, dst_ref_id=dst, relation="cites")
    pinned = store.add_link(src_ref_id=src, dst_ref_id=dst, relation="cites", dst_pos=0)
    rows = store.links_missing_reason(space="repo-dev", under=root)["rows"]
    assert {r["link_id"] for r in rows} == {whole.id, pinned.id}
    assert {r["dst_chunk_id"] is None for r in rows} == {True, False}
    assert store.links_missing_reason(space="repo-dev", endpoint="dst")["total"] == 0
    assert store.links_missing_reason(space="repo-dev", endpoint="either")["total"] == 2


def test_mcp_queue_args(runtime_with_store: PrecisRuntime) -> None:
    edge_id = _edge(runtime_with_store.live_store, {})
    out = runtime_with_store.dispatch(
        "get", {"kind": "memory", "id": "/links", "args": {"limit": 2}}
    )
    assert str(edge_id) in out and "shown 1 of 1 missing" in out


@pytest.mark.parametrize(
    "relation",
    [
        "part-of",
        "contains",
        "specialises",
        "generalises",
        "represents",
        "represented-by",
        "realized-by",
        "realizes",
        "made-of",
        "used-in",
        "qualifies",
        "qualified-by",
        "instance-of",
        "has-instance",
    ],
)
def test_structural_defaults_cover_stored_spellings(hub: Hub, relation: str) -> None:
    store = hub.live_store
    edge_id = _edge(store, {})
    # Exercise legacy stored inverses as well as canonical write spellings.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE links SET relation = %s WHERE link_id = %s", (relation, edge_id)
        )
    assert store.links_missing_reason()["total"] == 0
    assert store.links_missing_reason(count_only=True)["total"] == 0
    assert [
        row["link_id"]
        for row in store.links_missing_reason(include_structural=True)["rows"]
    ] == [edge_id]


@pytest.mark.parametrize("view", [None, "raw"])
def test_missing_filter_requires_queue_view(
    hub_stateless: Hub, view: str | None
) -> None:
    handler = MemoryHandler(hub=hub_stateless)
    with pytest.raises(BadInput, match="view='links'"):
        handler.get(id=123, view=view, reason="missing")


def _edge(store: Store, meta: dict, *, relation: str = "cites") -> int:
    src = store.insert_ref(kind="memory", slug=None, title="source").id
    dst = store.insert_ref(kind="memory", slug=None, title="target").id
    return store.add_link(
        src_ref_id=src, dst_ref_id=dst, relation=relation, meta=meta
    ).id


def test_keyset_backfill_and_reason_states(hub: Hub) -> None:
    store = hub.live_store
    missing = [
        _edge(store, m)
        for m in ({}, {"reason": ""}, {"reason": " \t\n"}, {"reason": None})
    ]
    for meta in (
        {"reason": "."},
        {"reason": "a bridge"},
        {"why": "legacy bridge"},
        {"reason": {"unsupported": True}},
    ):
        _edge(store, meta)
    structural = _edge(store, {}, relation="part-of")
    first = store.links_missing_reason(limit=2)
    assert [r["link_id"] for r in first["rows"]] == missing[:2]
    assert first["total"] == 4
    assert not first["done"]
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE links SET meta = %s WHERE link_id = ANY(%s)",
            (Jsonb({"reason": "reviewed bridge"}), missing[:2]),
        )
    later = _edge(store, {})
    rest = store.links_missing_reason(cursor=first["next_cursor"])
    assert [r["link_id"] for r in rest["rows"]] == [*missing[2:], later]
    assert rest["done"] and rest["next_cursor"] is None
    assert store.links_missing_reason(count_only=True)["total"] == 3
    assert structural in [
        r["link_id"]
        for r in store.links_missing_reason(include_structural=True)["rows"]
    ]
    with pytest.raises(BadInput, match="cursor"):
        store.links_missing_reason(cursor=first["next_cursor"], src_kind="paper")


def test_queue_read_is_pure_and_focus_filters(hub: Hub) -> None:
    store = hub.live_store
    edge_id = _edge(store, {})
    with store.pool.connection() as conn:
        before = conn.execute(
            "SELECT meta FROM links WHERE link_id = %s", (edge_id,)
        ).fetchone()
        assert before is not None
        ends = conn.execute(
            "SELECT src_ref_id, dst_ref_id FROM links WHERE link_id = %s", (edge_id,)
        ).fetchone()
        assert ends is not None
    assert (
        store.links_missing_reason(
            focus_ref_id=ends[1], direction="in", relations=["cited-by"]
        )["rows"][0]["link_id"]
        == edge_id
    )
    assert (
        store.links_missing_reason(focus_ref_id=ends[1], direction="out")["total"] == 0
    )
    assert store.links_missing_reason(src_kind="paper")["total"] == 0
    with store.pool.connection() as conn:
        assert (
            conn.execute(
                "SELECT meta FROM links WHERE link_id = %s", (edge_id,)
            ).fetchone()
            == before
        )


def test_view_footer_roundtrips(hub: Hub) -> None:
    store = hub.live_store
    ids = [_edge(store, {}) for _ in range(3)]
    handler = MemoryHandler(hub=hub)
    first = handler.get(id="/links", limit=2).body
    assert "shown 2 of 3 missing" in first
    expression = ast.parse(first.split(" · next: ")[1], mode="eval").body
    assert isinstance(expression, ast.Call)
    kwargs = {str(kw.arg): ast.literal_eval(kw.value) for kw in expression.keywords}
    args = kwargs.pop("args")
    kwargs.pop("kind")
    last = handler.get(**kwargs, **args).body
    assert str(ids[-1]) in last and "complete" in last
    count = handler.get(id="/links", count=True).body
    assert "shown 0 of 3 missing" in count
    with pytest.raises(BadInput, match="id='/links'"):
        handler.get(id="/links", cursor="broken")
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT src_ref_id FROM links WHERE link_id = %s", (ids[0],)
        ).fetchone()
        assert row is not None
    assert (
        "shown 1 of 1 missing"
        in handler.get(id=row[0], view="links", reason="missing").body
    )

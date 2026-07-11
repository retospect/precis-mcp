"""Smartdraft — the fisheye rail's LLM-free relevance engine (design:
docs/proposals/draft-reader-fisheye-rail.md). Pins eye-pressure ranking + the
fisheye-collapse TOC + the three-pane view model."""

from __future__ import annotations

from precis.utils import handle_registry
from precis_web.smartdraft import (
    ChunkNode,
    _left_toc,
    build_view,
    focus_index,
    pressures,
)


def _node(idx, kws, *, kind="paragraph", emb=None, pinned=False) -> ChunkNode:
    return ChunkNode(
        idx=idx,
        dc=f"dc{100 + idx}",
        base58=f"b{idx}",
        chunk_id=100 + idx,
        depth=1,
        chunk_kind=kind,
        text=f"body {idx}",
        summary=f"summary {idx}",
        keywords=kws,
        embedding=emb,
        pinned=pinned,
    )


# ── eye-pressure ──────────────────────────────────────────────────────


def test_focus_is_max_pressure() -> None:
    nodes = [_node(0, ["alpha"]), _node(1, ["beta"])]
    assert pressures(nodes, 0)[0] == 1.0


def test_keyword_match_wins_at_equal_distance() -> None:
    # focus 'alpha' at idx2; idx0 (alpha) and idx4 (gamma) are equidistant, so
    # only the keyword signal separates them.
    nodes = [
        _node(0, ["alpha"]),
        _node(1, ["x"]),
        _node(2, ["alpha"]),
        _node(3, ["y"]),
        _node(4, ["gamma"]),
    ]
    p = pressures(nodes, 2)
    assert p[0] > p[4]


def test_status_boost_pokes_through() -> None:
    # a pinned but topically-unrelated chunk still clears the keep bar.
    nodes = [_node(0, ["alpha"]), _node(1, ["zzz"], pinned=True), _node(2, ["alpha"])]
    p = pressures(nodes, 0)
    assert p[1] >= 10.0


# ── fisheye TOC collapse ──────────────────────────────────────────────


def _spread() -> list[ChunkNode]:
    # a heading, two relevant, then a far quiet run of unrelated chunks.
    return [
        _node(0, ["hd"], kind="heading"),
        _node(1, ["alpha"]),
        _node(2, ["alpha"]),
        _node(3, ["zzz"]),
        _node(4, ["zzz"]),
        _node(5, ["zzz"]),
        _node(6, ["zzz"]),
        _node(7, ["zzz"]),
    ]


def test_relevance_off_is_the_full_outline() -> None:
    nodes = _spread()
    rows = _left_toc(nodes, pressures(nodes, 1), relevance=False)
    assert len(rows) == len(nodes)
    assert all(r.node is not None for r in rows)


def test_relevance_on_collapses_quiet_runs_but_keeps_the_heading() -> None:
    nodes = _spread()
    rows = _left_toc(nodes, pressures(nodes, 1), relevance=True)
    kept = {r.node.dc for r in rows if r.node}
    assert nodes[0].dc in kept  # heading always survives
    assert nodes[1].dc in kept  # the relevant/focus chunk
    assert any(r.collapsed_nodes for r in rows)  # the far quiet run collapsed
    # order never reshuffles: kept nodes stay in reading order
    kept_idx = [r.node.idx for r in rows if r.node]
    assert kept_idx == sorted(kept_idx)


def test_a_lone_quiet_chunk_is_shown_not_collapsed() -> None:
    # focus at idx0; idx1 is relevant (kept), idx2 is a single quiet chunk
    # between two kept chunks — collapsing it to "⋯ 1 para ⋯" saves nothing.
    nodes = [
        _node(0, ["alpha"]),
        _node(1, ["alpha"]),
        _node(2, ["zzz"]),
        _node(3, ["alpha"]),
    ]
    # force idx2 quiet: far weighting can't help here, but proximity keeps it —
    # so use a spread where the lone chunk is genuinely below threshold.
    nodes = [
        _node(0, ["alpha"]),
        _node(1, ["alpha"]),
        _node(2, ["alpha"]),
        _node(3, ["alpha"]),
        _node(4, ["zzz"]),  # lone quiet chunk, far
        _node(5, ["alpha"]),
    ]
    rows = _left_toc(nodes, pressures(nodes, 0), relevance=True)
    # no collapse marker of size 1 is ever emitted
    assert all(len(r.collapsed_nodes) != 1 for r in rows)


def test_focus_index_defaults_to_first_body_chunk() -> None:
    nodes = [_node(0, ["h"], kind="heading"), _node(1, ["a"]), _node(2, ["b"])]
    assert focus_index(nodes, None) == 1  # skips the heading
    assert focus_index(nodes, "dc102") == 2  # explicit handle wins
    assert focus_index(nodes, "dc999") == 1  # stale handle → default


# ── the whole view (real store) ───────────────────────────────────────


def _seed_draft(store, *, regimes):
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    ref, _title = store.create_draft(
        name="sd", title="Smart Draft", project_ref_id=proj
    )
    store.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="\n\n".join(f"body of chunk {i}" for i in range(len(regimes))),
        at={"last": True},
    )
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id=%s AND ord>=0 ORDER BY ord",
            (ref.id,),
        ).fetchall()
        # row 0 is the title heading; body chunks follow
        body = [r[0] for r in rows][1:]
        for cid, kws in zip(body, regimes, strict=False):
            conn.execute("UPDATE chunks SET keywords=%s WHERE chunk_id=%s", (kws, cid))
    return ref.id


def test_build_view_populates_three_panes(hub) -> None:
    store = hub.store
    ref_id = _seed_draft(store, regimes=[["alpha"], ["alpha"], ["beta"], ["gamma"]])
    view = build_view(store, ref_id, relevance=True)
    assert view.focus is not None
    assert view.toc  # left pane
    assert view.middle  # middle pane
    assert any(m.is_focus for m in view.middle)
    # the title heading is always present in the TOC
    assert any(r.node and r.node.is_heading for r in view.toc)


def test_build_view_marks_a_pinned_chunk(hub) -> None:
    store = hub.store
    ref_id = _seed_draft(store, regimes=[["alpha"], ["beta"]])
    chunks = store.reading_order(ref_id)
    body = next(c for c in chunks if c.chunk_kind != "heading")
    pin_dc = handle_registry.format_handle("draft", body.chunk_id, chunk=True)
    view = build_view(store, ref_id, marks={"pens": [pin_dc], "eyes": {}})
    pinned = [r.node for r in view.toc if r.node and r.node.pinned]
    assert any(n.dc == pin_dc for n in pinned)

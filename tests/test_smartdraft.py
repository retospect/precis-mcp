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
    search_chunks,
)


def _snode(idx, *, text="", kws=None, tags=None) -> ChunkNode:
    return ChunkNode(
        idx=idx,
        dc=f"dc{100 + idx}",
        base58=f"b{idx}",
        chunk_id=100 + idx,
        depth=1,
        chunk_kind="paragraph",
        text=text,
        summary=text[:40],
        keywords=kws or [],
        tags=tags or [],
    )


def _node(idx, kws, *, kind="paragraph", pinned=False) -> ChunkNode:
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


# ── search (RRF fusion) ───────────────────────────────────────────────


def test_search_rrf_ranks_multisignal_and_surfaces_semantic_only() -> None:
    nodes = [
        _snode(0, text="alpha appears here", kws=["alpha"]),  # V + K
        _snode(1, text="nothing here", kws=["alpha"]),  # K only
        _snode(2, text="unrelated", kws=["zzz"]),  # semantic only (chunk_id 102)
    ]
    # semantic top-N (from the HNSW query) ranks chunk 102 first
    hits = search_chunks(
        nodes, "alpha", active={"v", "k", "t", "s"}, semantic_ranks={102: 1}
    )
    by = {h.node.dc: h for h in hits}
    # V+K outranks K-only
    assert hits.index(by["dc100"]) < hits.index(by["dc101"])
    # the semantic-only chunk still surfaces (not buried) with a rank
    assert "dc102" in by and by["dc102"].s_rank == 1


def test_search_toggling_a_signal_drops_its_only_matches() -> None:
    nodes = [
        _snode(0, text="alpha here", kws=["alpha"]),
        _snode(1, text="unrelated", kws=["zzz"]),  # semantic only (chunk_id 101)
    ]
    # semantic OFF → the semantic-only chunk is gone; the literal one stays
    hits = search_chunks(
        nodes, "alpha", active={"v", "k", "t"}, semantic_ranks={101: 1}
    )
    dcs = {h.node.dc for h in hits}
    assert "dc100" in dcs and "dc101" not in dcs


def test_search_tag_outweighs_a_single_literal_signal() -> None:
    nodes = [
        _snode(0, text="alpha", kws=[]),  # V only
        _snode(1, text="x", tags=["alpha"]),  # T only (weighted higher)
    ]
    hits = search_chunks(nodes, "alpha", active={"v", "k", "t"})
    # the tag match ranks above the lone verbatim match (human-curated weight)
    assert hits[0].node.dc == "dc101" and hits[0].t


def test_assemble_view_keep_dcs_keeps_a_hit_uncollapsed() -> None:
    from precis_web.smartdraft import assemble_view

    # a far, quiet chunk that would normally collapse — but it's a search hit,
    # so keep_dcs keeps it visible in the in-TOC view.
    nodes = (
        [_node(0, ["alpha"])]
        + [_node(i, ["z"]) for i in range(1, 6)]
        + [_node(6, ["target"])]
    )
    view = assemble_view(nodes, focus_dc="dc100", relevance=True, keep_dcs={"dc106"})
    kept = {r.node.dc for r in view.toc if r.node}
    assert "dc106" in kept


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


def test_full_doc_mode_renders_every_chunk_verbatim(hub) -> None:
    # relevance=False (the Fisheye⇄Full toggle) → the middle is the whole
    # uncompressed document, every chunk verbatim, focus still framed.
    store = hub.store
    ref_id = _seed_draft(store, regimes=[["a"], ["b"], ["c"], ["d"]])
    view = build_view(store, ref_id, relevance=False)
    assert len(view.middle) == len(store.reading_order(ref_id))
    assert all(m.mode in ("full", "doc") for m in view.middle)
    assert sum(1 for m in view.middle if m.is_focus) == 1


def test_keyword_shared_distal_chunk_surfaces_in_the_toc(hub) -> None:
    # The focus (first body chunk) shares 'zeta' with a FAR chunk; the chunks
    # between share nothing. The distal shared-keyword chunk is kept + flagged
    # (bg-emerald 🔑), even though proximity/Jaccard alone wouldn't clear the bar.
    store = hub.store
    ref_id = _seed_draft(
        store,
        regimes=[["zeta"], ["a"], ["b"], ["c"], ["d"], ["e"], ["zeta", "f"]],
    )
    view = build_view(store, ref_id, relevance=True)
    shared = [r for r in view.toc if r.node and r.shared]
    assert any("zeta" in (r.node.keywords or []) for r in shared)


def test_focus_carries_a_content_sha_and_is_editable(hub) -> None:
    # The inline editor needs the focus's content_sha (optimistic concurrency)
    # and a body chunk must be flagged editable.
    store = hub.store
    ref_id = _seed_draft(store, regimes=[["a"], ["b"]])
    view = build_view(store, ref_id)
    assert view.focus is not None
    assert view.focus.sha  # non-empty content_sha
    assert view.focus.editable  # a paragraph is inline-editable


def test_chunk_tag_round_trips_and_feeds_the_T_signal(hub) -> None:
    from precis.store.types import Tag

    store = hub.store
    ref_id = _seed_draft(store, regimes=[["a"], ["b"]])
    body = next(c for c in store.reading_order(ref_id) if c.chunk_kind != "heading")
    dc = handle_registry.format_handle("draft", body.chunk_id, chunk=True)
    rh = store.resolve_handle(dc)
    store.add_tag(ref_id, Tag.open("important"), pos=rh.chunk_ord)

    nodes = build_view(store, ref_id).nodes
    tagged = next(n for n in nodes if n.chunk_id == body.chunk_id)
    assert "important" in tagged.tags  # loads into the T signal
    hits = search_chunks(nodes, "important", active={"v", "k", "t"})
    assert any(h.t and h.node.chunk_id == body.chunk_id for h in hits)

    store.remove_tag(ref_id, Tag.open("important"), pos=rh.chunk_ord)
    nodes2 = build_view(store, ref_id).nodes
    assert (
        "important" not in next(n for n in nodes2 if n.chunk_id == body.chunk_id).tags
    )


def test_build_view_marks_a_pinned_chunk(hub) -> None:
    store = hub.store
    ref_id = _seed_draft(store, regimes=[["alpha"], ["beta"]])
    chunks = store.reading_order(ref_id)
    body = next(c for c in chunks if c.chunk_kind != "heading")
    pin_dc = handle_registry.format_handle("draft", body.chunk_id, chunk=True)
    view = build_view(store, ref_id, marks={"pens": [pin_dc], "eyes": {}})
    pinned = [r.node for r in view.toc if r.node and r.node.pinned]
    assert any(n.dc == pin_dc for n in pinned)

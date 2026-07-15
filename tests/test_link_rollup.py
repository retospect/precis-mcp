"""source-backfill 8a.2 — the visibility-scoped link-rollup logic.

Pure unit tests against a hand-built tree + demand map (no DB): the
coarsest-visible-ancestor resolver and the per-section edge aggregation with
held-vs-tail partitioning and the ``top_k`` fold.

The tree used across most tests::

    root(1) [visible]
      secA(2) [visible]
        para(3)   [collapsed]
      secB(4)     [collapsed]
        para(5)   [collapsed]
"""

from __future__ import annotations

from precis.backfill.link_rollup import (
    ChunkEdge,
    NamedTarget,
    coarsest_visible_ancestor,
    rollup_edges,
)

_PARENT = {1: None, 2: 1, 3: 2, 4: 1, 5: 4}
# 1 (root) and 2 (secA) open; 3/4/5 collapsed (absent → NONE).
_DEMAND = {1: 3, 2: 3}
_THIS = 100  # the draft's ref_id


# ── coarsest_visible_ancestor ──────────────────────────────────────


def test_open_target_points_at_itself() -> None:
    assert coarsest_visible_ancestor(2, parent_of=_PARENT, demand=_DEMAND) == 2


def test_collapsed_para_rolls_up_to_open_section() -> None:
    # para(3) collapsed under secA(2) open → resolves to the section.
    assert coarsest_visible_ancestor(3, parent_of=_PARENT, demand=_DEMAND) == 2


def test_collapsed_branch_rolls_up_to_visible_root() -> None:
    # para(5) under secB(4), both collapsed, but root(1) is visible.
    assert coarsest_visible_ancestor(5, parent_of=_PARENT, demand=_DEMAND) == 1
    assert coarsest_visible_ancestor(4, parent_of=_PARENT, demand=_DEMAND) == 1


def test_total_collapse_falls_back_to_root() -> None:
    # Nothing visible anywhere → the coarsest (root) ancestor.
    assert coarsest_visible_ancestor(5, parent_of=_PARENT, demand={}) == 1
    assert coarsest_visible_ancestor(3, parent_of=_PARENT, demand={}) == 1


def test_cross_doc_target_is_unresolved() -> None:
    # A chunk id not in this doc's tree → None (caller resolves to a ref).
    assert coarsest_visible_ancestor(999, parent_of=_PARENT, demand=_DEMAND) is None
    assert coarsest_visible_ancestor(None, parent_of=_PARENT, demand=_DEMAND) is None


def test_extent_enum_and_bare_int_agree() -> None:
    from precis.workers.working_set import Extent

    enum_demand = {1: Extent.FULL, 2: Extent.SUMMARY}
    # Extent.SUMMARY > NONE → visible, same verdict as the bare-int fixture.
    assert coarsest_visible_ancestor(3, parent_of=_PARENT, demand=enum_demand) == 2
    # A demand of NONE is *not* visible (0 is not > 0).
    assert (
        coarsest_visible_ancestor(
            2, parent_of=_PARENT, demand={2: Extent.NONE, 1: Extent.FULL}
        )
        == 1
    )


# ── rollup_edges ───────────────────────────────────────────────────


def _named_set(rollup) -> set[tuple[int, str, int, int]]:  # type: ignore[no-untyped-def]
    return {
        (
            n.src,
            "c" if n.dst_chunk is not None else "r",
            n.dst_chunk or n.dst_ref or 0,
            n.count,
        )
        for n in rollup.named
    }


def test_in_doc_edges_group_by_visible_ancestors() -> None:
    # Two links from paras under secA(2) into paras under secB(4): both collapse
    # to the section-level aggregate secA(2) → root(1) (secB's visible rep).
    edges = [
        ChunkEdge(
            src_chunk_id=3, dst_chunk_id=5, dst_ref_id=_THIS, relation="see-also"
        ),
        ChunkEdge(
            src_chunk_id=3, dst_chunk_id=4, dst_ref_id=_THIS, relation="see-also"
        ),
    ]
    r = rollup_edges(
        edges, this_ref_id=_THIS, parent_of=_PARENT, demand=_DEMAND, held_ref_ids=set()
    )
    assert r.named == (NamedTarget(src=2, dst_chunk=1, dst_ref=None, count=2),)
    assert r.tail == ()


def test_open_target_points_directly() -> None:
    # secA(2) is open, so a link landing in it points right at it, not the root.
    edges = [ChunkEdge(src_chunk_id=5, dst_chunk_id=3, dst_ref_id=_THIS, relation="x")]
    r = rollup_edges(
        edges, this_ref_id=_THIS, parent_of=_PARENT, demand=_DEMAND, held_ref_ids=set()
    )
    # src para(5) → root(1); dst para(3) → secA(2).
    assert r.named == (NamedTarget(src=1, dst_chunk=2, dst_ref=None, count=1),)


def test_self_loop_after_collapse_is_dropped() -> None:
    # para(3) → para(3-sibling) that both collapse to secA(2): src == dst → noise.
    edges = [ChunkEdge(src_chunk_id=3, dst_chunk_id=2, dst_ref_id=_THIS, relation="x")]
    r = rollup_edges(
        edges, this_ref_id=_THIS, parent_of=_PARENT, demand=_DEMAND, held_ref_ids=set()
    )
    assert not r  # both endpoints resolve to secA(2) → dropped


def test_held_source_named_unheld_goes_to_tail() -> None:
    edges = [
        # held paper 700, cited twice from secA
        ChunkEdge(src_chunk_id=3, dst_chunk_id=None, dst_ref_id=700, relation="cites"),
        ChunkEdge(src_chunk_id=3, dst_chunk_id=None, dst_ref_id=700, relation="cites"),
        # two distinct unheld papers from secA → tail
        ChunkEdge(src_chunk_id=3, dst_chunk_id=None, dst_ref_id=801, relation="cites"),
        ChunkEdge(src_chunk_id=3, dst_chunk_id=None, dst_ref_id=802, relation="cites"),
    ]
    r = rollup_edges(
        edges, this_ref_id=_THIS, parent_of=_PARENT, demand=_DEMAND, held_ref_ids={700}
    )
    assert r.named == (NamedTarget(src=2, dst_chunk=None, dst_ref=700, count=2),)
    assert len(r.tail) == 1
    (tail,) = r.tail
    assert tail.src == 2 and tail.links == 2 and tail.targets == 2


def test_cross_doc_chunk_link_named_by_ref_when_held() -> None:
    # A link to a *chunk* in another held draft/paper (dst_ref != this) is named
    # by its ref, not its chunk (we hold no tree for it).
    edges = [
        ChunkEdge(src_chunk_id=3, dst_chunk_id=42, dst_ref_id=700, relation="cites")
    ]
    r = rollup_edges(
        edges, this_ref_id=_THIS, parent_of=_PARENT, demand=_DEMAND, held_ref_ids={700}
    )
    assert r.named == (NamedTarget(src=2, dst_chunk=None, dst_ref=700, count=1),)


def test_top_k_folds_overflow_into_tail() -> None:
    # 5 held papers from secA, top_k=2 → 2 named, 3 fold into the tail.
    held = {900, 901, 902, 903, 904}
    edges = [
        ChunkEdge(src_chunk_id=3, dst_chunk_id=None, dst_ref_id=r, relation="cites")
        for r in (900, 900, 900, 901, 901, 902, 903, 904)  # 900×3, 901×2, rest ×1
    ]
    r = rollup_edges(
        edges,
        this_ref_id=_THIS,
        parent_of=_PARENT,
        demand=_DEMAND,
        held_ref_ids=held,
        top_k=2,
    )
    # top 2 by count: 900 (3), 901 (2). 902/903/904 (1 each) → tail.
    assert _named_set(r) == {(2, "r", 900, 3), (2, "r", 901, 2)}
    (tail,) = r.tail
    assert tail.src == 2 and tail.links == 3 and tail.targets == 3


def test_ref_level_src_edge_is_skipped() -> None:
    # No src chunk → not section-attributable in v1.
    edges = [
        ChunkEdge(
            src_chunk_id=None, dst_chunk_id=None, dst_ref_id=700, relation="cites"
        )
    ]
    r = rollup_edges(
        edges, this_ref_id=_THIS, parent_of=_PARENT, demand=_DEMAND, held_ref_ids={700}
    )
    assert not r


def test_whole_doc_self_ref_link_is_noise() -> None:
    # A ref-level link back to this same draft is dropped, not tailed.
    edges = [
        ChunkEdge(src_chunk_id=3, dst_chunk_id=None, dst_ref_id=_THIS, relation="x")
    ]
    r = rollup_edges(
        edges, this_ref_id=_THIS, parent_of=_PARENT, demand=_DEMAND, held_ref_ids=set()
    )
    assert not r


def test_empty_edges_is_falsy() -> None:
    r = rollup_edges(
        [], this_ref_id=_THIS, parent_of=_PARENT, demand=_DEMAND, held_ref_ids=set()
    )
    assert not r and r.named == () and r.tail == ()

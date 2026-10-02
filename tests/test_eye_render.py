"""Per-kind eye render — the ladder generalizes, the neighborhood
shape is kind-specific: memory = link graph (by relation), paper/patent/web =
the dynamic keyword-cluster fisheye (pc-addressed), draft/plan = the
reading-order fisheye."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.plan import PlanHandler
from precis.store.types import ChunkInsert
from precis.utils import handle_registry
from precis.utils.eye_render import _cluster_map, _fisheye_split, render_eye
from precis.utils.toc_db import cluster_blocks
from precis.utils.working_set_render import render_working_set
from precis.workers.working_set import Extent, WorkingSet


def _pe(body: str) -> str:
    m = re.search(r"pe\d+", body)
    assert m is not None, f"pe-handle not found in {body!r}"
    return m.group(0)


@dataclass
class _B:
    """A minimal ChunkRow stand-in for the pure cluster-render helpers — they read
    ``id`` / ``ord`` / ``keywords`` / ``text`` / ``chunk_kind`` only."""

    id: int
    ord: int
    keywords: list[str] = field(default_factory=list)
    text: str = ""
    chunk_kind: str = "paragraph"


def test_memory_eye_1hop_shows_link_neighborhood_by_relation(hub: Hub) -> None:
    store = hub.live_store
    mem = store.insert_ref(kind="memory", slug=None, title="SEI thickness anomaly note")
    paper = store.insert_ref(kind="paper", slug="wenzel16", title="XPS depth profiling")
    mem2 = store.insert_ref(
        kind="memory", slug=None, title="Kinetics vs thermodynamics"
    )
    store.add_link(src_ref_id=mem.id, dst_ref_id=paper.id, relation="supports")
    store.add_link(src_ref_id=mem.id, dst_ref_id=mem2.id, relation="related-to")

    h = handle_registry.format_handle("memory", mem.id)
    out = render_eye(store, h, "fisheye+1hop")

    assert "SEI thickness anomaly note" in out  # the memory itself
    assert "— linked (1 hop) —" in out
    # each neighbor by handle + relation type
    assert f"supports: pa{paper.id} — XPS depth profiling" in out
    assert f"related-to: me{mem2.id} — Kinetics vs thermodynamics" in out


def test_memory_eye_1hop_caps_a_relation_group_with_overflow_line(hub: Hub) -> None:
    """A relation group over ``_NEIGHBOR_GROUP_CAP`` (8) is capped, not
    dumped flat — the truncated tail surfaces as an explicit ``… +N more``
    line rather than silently vanishing (project §6: no silent cap)."""
    store = hub.live_store
    mem = store.insert_ref(kind="memory", slug=None, title="Hub note")
    others = [
        store.insert_ref(kind="memory", slug=None, title=f"Related note {i}")
        for i in range(10)
    ]
    for other in others:
        store.add_link(src_ref_id=mem.id, dst_ref_id=other.id, relation="related-to")

    h = handle_registry.format_handle("memory", mem.id)
    out = render_eye(store, h, "fisheye+1hop")

    assert out.count("related-to: me") == 8
    assert "… +2 more" in out


def test_memory_eye_1hop_counts_overflow_against_live_neighbours(hub: Hub) -> None:
    """The cap and the ``+N more`` count are against *rendered* neighbours,
    not raw edges. Nine edges of which two point at soft-deleted refs is
    seven live neighbours — under the cap, so no overflow line at all.
    Counting edges instead would render seven rows and claim two more."""
    store = hub.live_store
    mem = store.insert_ref(kind="memory", slug=None, title="Hub note")
    others = [
        store.insert_ref(kind="memory", slug=None, title=f"Related note {i}")
        for i in range(9)
    ]
    for other in others:
        store.add_link(src_ref_id=mem.id, dst_ref_id=other.id, relation="related-to")
    for dead in others[:2]:
        store.retire_ref(dead.id)

    h = handle_registry.format_handle("memory", mem.id)
    out = render_eye(store, h, "fisheye+1hop")

    assert out.count("related-to: me") == 7
    assert "more" not in out


def test_ring_registry_names_only_registered_relations(hub: Hub) -> None:
    """Every slug the ring follows is a live relation — a typo in a ring
    group would otherwise render nothing, silently."""
    from precis.utils.refeye import RING_RELATIONS

    unregistered = RING_RELATIONS - hub.live_store.valid_relations()
    assert not unregistered, sorted(unregistered)


def test_quest_eye_1hop_reads_serves_from_both_sides(hub: Hub) -> None:
    """``fisheye-everywhere.md`` AC 2: a quest's ring shows what it serves
    and what serves it, under the Roadmap group, each edge read from this
    side — the one stored ``serves`` row is ``serves`` on the child's eye
    and ``served-by`` on the parent's."""
    store = hub.live_store
    parent = store.insert_ref(kind="quest", slug=None, title="Grow the mesh")
    child = store.insert_ref(kind="quest", slug=None, title="Ship the ring")
    grandchild = store.insert_ref(kind="quest", slug=None, title="Pin the groups")
    store.add_link(src_ref_id=child.id, dst_ref_id=parent.id, relation="serves")
    store.add_link(src_ref_id=grandchild.id, dst_ref_id=child.id, relation="serves")

    h = handle_registry.format_handle("quest", child.id)
    out = render_eye(store, h, "fisheye+1hop")
    ph = handle_registry.format_handle("quest", parent.id)
    gh = handle_registry.format_handle("quest", grandchild.id)
    assert "Roadmap:" in out
    assert f"  serves: {ph} — Grow the mesh" in out
    assert f"  served-by: {gh} — Pin the groups" in out


def test_concept_and_component_rings_render_their_families(hub: Hub) -> None:
    store = hub.live_store
    calc = store.insert_ref(kind="concept", slug=None, title="Calculus")
    algebra = store.insert_ref(kind="concept", slug=None, title="Algebra")
    store.add_link(
        src_ref_id=calc.id, dst_ref_id=algebra.id, relation="has-prerequisite"
    )
    out = render_eye(
        store, handle_registry.format_handle("concept", algebra.id), "fisheye+1hop"
    )
    ch = handle_registry.format_handle("concept", calc.id)
    assert "Concepts:" in out and f"prerequisite-of: {ch} — Calculus" in out

    frame = store.insert_ref(kind="component", slug="frame", title="Frame")
    bolt = store.insert_ref(kind="component", slug="m3-bolt", title="M3 bolt")
    store.add_link(src_ref_id=frame.id, dst_ref_id=bolt.id, relation="contains")
    out = render_eye(
        store, handle_registry.format_handle("component", bolt.id), "fisheye+1hop"
    )
    fh = handle_registry.format_handle("component", frame.id)
    assert "Parts:" in out and f"part-of: {fh} — Frame" in out


def test_inbound_edge_without_an_inverse_reads_with_an_arrow(hub: Hub) -> None:
    """``see-also`` is asymmetric with no inverse slug: from the target's
    side it reads ``<-see-also``, never as if this ref were the source."""
    store = hub.live_store
    a = store.insert_ref(kind="memory", slug=None, title="Points out")
    b = store.insert_ref(kind="memory", slug=None, title="Pointed at")
    store.add_link(src_ref_id=a.id, dst_ref_id=b.id, relation="see-also")
    out = render_eye(
        store, handle_registry.format_handle("memory", b.id), "fisheye+1hop"
    )
    assert f"  <-see-also: me{a.id} — Points out" in out
    out = render_eye(
        store, handle_registry.format_handle("memory", a.id), "fisheye+1hop"
    )
    assert f"  see-also: me{b.id} — Pointed at" in out


def test_memory_eye_below_1hop_omits_the_link_neighborhood(hub: Hub) -> None:
    store = hub.live_store
    mem = store.insert_ref(kind="memory", slug=None, title="A note")
    other = store.insert_ref(kind="memory", slug=None, title="Linked")
    store.add_link(src_ref_id=mem.id, dst_ref_id=other.id, relation="related-to")
    h = handle_registry.format_handle("memory", mem.id)
    assert "— linked" not in render_eye(store, h, "verbatim")
    assert "— linked" not in render_eye(store, h, "summary")


def test_memory_eye_kwd_is_a_one_line_bookmark(hub: Hub) -> None:
    store = hub.live_store
    mem = store.insert_ref(kind="memory", slug=None, title="Bookmark me")
    h = handle_registry.format_handle("memory", mem.id)
    out = render_eye(store, h, "kwd")
    assert out.startswith("· ") and "Bookmark me" in out and "\n" not in out


def test_doc_eye_empty_paper_renders_the_head(hub: Hub) -> None:
    # A paper with no body chunks yet degrades to its head line — never a crash.
    store = hub.live_store
    paper = store.insert_ref(kind="paper", slug="li21", title="Cryo-EM SEI")
    h = handle_registry.format_handle("paper", paper.id)
    out = render_eye(store, h, "verbatim")
    assert f"pa{paper.id}" in out and "Cryo-EM SEI" in out


# ── the keyword-cluster fisheye (paper / patent / web) ────────────────


def test_cluster_blocks_short_body_is_one_cluster_per_block() -> None:
    blocks = [_B(id=100 + i, ord=i, keywords=[f"k{i}"], text=f"t{i}") for i in range(5)]
    clusters = cluster_blocks(blocks)
    assert len(clusters) == 5
    assert all(len(bucket) == 1 for bucket, _ in clusters)


def test_cluster_blocks_groups_a_long_body_by_keyword_regime() -> None:
    # Two keyword regimes over a long body → a boundary the DP splits on, every
    # block accounted for in reading order.
    blocks = [
        _B(
            id=200 + i,
            ord=i,
            keywords=(["alpha", "beta"] if i < 20 else ["gamma", "delta"]),
            text="x",
        )
        for i in range(40)
    ]
    clusters = cluster_blocks(blocks)
    assert len(clusters) >= 2
    flat = [b.ord for bucket, _ in clusters for b in bucket]
    assert flat == list(range(40))


def test_cluster_map_is_pc_addressed_with_no_verbatim() -> None:
    blocks = [
        _B(id=300 + i, ord=i, keywords=[f"k{i}"], text=f"BODYTEXT{i}") for i in range(5)
    ]
    out = _cluster_map("paper", cluster_blocks(blocks))
    assert "clusters" in out
    assert "pc300" in out  # lead chunk handle, universal
    assert "BODYTEXT0" not in out  # a whole-doc eye never spills verbatim text
    assert "~" not in out  # never the legacy slug~pos form


def test_fisheye_split_opens_the_eye_chunk_within_its_cluster() -> None:
    blocks = [
        _B(
            id=400 + i,
            ord=i,
            keywords=(["alpha"] if i < 20 else ["gamma"]),
            text=f"VERBATIM{i}",
        )
        for i in range(40)
    ]
    clusters = cluster_blocks(blocks)
    out = _fisheye_split("paper", clusters, eye_ord=5, ext=Extent.FULL)
    # the eye chunk: marked + full verbatim text
    assert "▸ pc405" in out
    assert "VERBATIM5" in out
    # a same-cluster neighbour appears as a summary line (its keywords), not text
    assert "pc404" in out
    assert "VERBATIM4" not in out
    # the far cluster is collapsed to one drillable label, not expanded
    assert "pc420" in out
    assert "VERBATIM25" not in out
    assert "~" not in out


def test_fisheye_split_collapses_a_big_home_clusters_far_tail() -> None:
    # A keyword-homogeneous section clusters into one big bucket; eyeing into it
    # windows around the eye and collapses the far tail to a ⋯ marker.
    blocks = [
        _B(id=600 + i, ord=i, keywords=["alpha"], text=f"t{i}") for i in range(40)
    ]
    clusters = cluster_blocks(blocks)
    # one dominant cluster (all keywords identical → distance 0 everywhere)
    out = _fisheye_split("paper", clusters, eye_ord=20, ext=Extent.FULL)
    assert "▸ pc620" in out
    assert "⋯" in out  # the far tail collapsed, not dumped
    # a chunk far past the forward window is not rendered as its own line
    assert "pc639" not in out


def test_fisheye_split_summary_eye_is_a_summary_not_verbatim() -> None:
    blocks = [
        _B(id=500 + i, ord=i, keywords=["alpha"], text=f"VERBATIM{i}") for i in range(6)
    ]
    clusters = cluster_blocks(blocks)
    out = _fisheye_split("paper", clusters, eye_ord=2, ext=Extent.SUMMARY)
    assert "▸ pc502" in out
    assert "VERBATIM2" not in out  # summary eye is a summary, no full text


def _seed_paper_with_keyworded_body(
    store, *, slug: str, title: str, regimes: list[list[str]]
) -> int:
    """Insert a paper + one body chunk per keyword set, stamping the keywords the
    ``chunk_keywords`` worker would (it doesn't run in tests). Returns ref_id."""
    ref = store.insert_ref(kind="paper", slug=slug, title=title)
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=i, text=f"body of chunk {i}") for i in range(len(regimes))],
    )
    with store.pool.connection() as conn:
        for i, kws in enumerate(regimes):
            conn.execute(
                "UPDATE chunks SET keywords = %s WHERE ref_id = %s AND ord = %s",
                (kws, ref.id, i),
            )
    return ref.id


def test_doc_eye_whole_paper_renders_the_cluster_map(hub: Hub) -> None:
    store = hub.live_store
    ref_id = _seed_paper_with_keyworded_body(
        store,
        slug="mao18",
        title="Rigidity percolation",
        regimes=[["isostatic"], ["maxwell"], ["floppy"], ["auxetic"]],
    )
    out = render_eye(store, handle_registry.format_handle("paper", ref_id), "summary")
    assert "Rigidity percolation" in out
    assert "clusters" in out  # the cluster map, not a flat body
    assert re.search(r"pc\d+", out)  # pc-addressed cluster handles
    assert "~" not in out


def test_doc_eye_chunk_handle_opens_that_chunk(hub: Hub) -> None:
    store = hub.live_store
    ref_id = _seed_paper_with_keyworded_body(
        store,
        slug="sun19",
        title="Twisted kagome",
        regimes=[["a"], ["a"], ["a"], ["a"], ["a"]],
    )
    # resolve the ord=2 chunk's universal pc handle, then eye it
    blocks = store.chunks.list_chunks_for_ref(ref_id)
    eye = blocks[2]
    pc = handle_registry.format_handle("paper", eye.id, chunk=True)
    out = render_eye(store, pc, "verbatim")
    assert f"▸ {pc}" in out  # the eye chunk, marked
    assert "body of chunk 2" in out  # its verbatim text


def test_paper_eye_1hop_surfaces_a_linked_note_both_ways(hub: Hub) -> None:
    """Links are symmetric: a note linked to a paper must appear when you fisheye
    the PAPER (not only when you fisheye the note)."""
    store = hub.live_store
    paper = store.insert_ref(kind="paper", slug="wenzel16", title="XPS profiling")
    note = store.insert_ref(kind="memory", slug=None, title="XPS caveat note")
    store.add_link(src_ref_id=note.id, dst_ref_id=paper.id, relation="related-to")

    paper_out = render_eye(
        store, handle_registry.format_handle("paper", paper.id), "fisheye+1hop"
    )
    assert "— linked (1 hop) —" in paper_out
    assert f"related-to: me{note.id} — XPS caveat note" in paper_out

    note_out = render_eye(
        store, handle_registry.format_handle("memory", note.id), "fisheye+1hop"
    )
    assert f"related-to: pa{paper.id} — XPS profiling" in note_out


def test_working_set_mixes_tree_and_link_eyes(hub: Hub) -> None:
    store = hub.live_store
    plan = PlanHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    plan.put(id="p", title="A Plan", project=proj)
    sec = _pe(plan.put(id="p", text="a section", at={"last": True}).body)
    mem = store.insert_ref(kind="memory", slug=None, title="a standalone note")

    ws = WorkingSet()
    ws.focus(sec, "fisheye")  # tree eye
    ws.focus(handle_registry.format_handle("memory", mem.id), "verbatim")  # link eye

    out = render_working_set(store, ws)
    assert "a section" in out  # the plan block
    assert "a standalone note" in out  # the memory block, rendered standalone


def test_unresolvable_flat_eye_degrades_not_crashes(hub: Hub) -> None:
    ws = WorkingSet()
    ws.focus("me999999", "verbatim")  # no such memory
    out = render_working_set(hub.live_store, ws)
    assert "unrenderable" in out  # marker, not an exception


# ── fisheye+2hop and +recall (fisheye-everywhere.md AC 3-4) ────────────


def _two_hop_fixture(hub: Hub) -> tuple[Any, Any, list[Any]]:
    """me(focus) —related-to→ me(b), me(c); b cites p1, p2; c cites p1.
    The second hop from the focus is {p1, p2} via cites — two distinct
    papers, though three edges reach them."""
    store = hub.live_store
    focus = store.insert_ref(kind="memory", slug=None, title="Focus note")
    b = store.insert_ref(kind="memory", slug=None, title="Note B")
    c = store.insert_ref(kind="memory", slug=None, title="Note C")
    papers = [
        store.insert_ref(kind="paper", slug=f"p{i}", title=f"Paper {i}") for i in (1, 2)
    ]
    for near in (b, c):
        store.add_link(src_ref_id=focus.id, dst_ref_id=near.id, relation="related-to")
    store.add_link(src_ref_id=b.id, dst_ref_id=papers[0].id, relation="cites")
    store.add_link(src_ref_id=b.id, dst_ref_id=papers[1].id, relation="cites")
    store.add_link(src_ref_id=c.id, dst_ref_id=papers[0].id, relation="cites")
    # An edge back to the focus and one between two first-hop refs are
    # already on screen; neither may count.
    store.add_link(src_ref_id=b.id, dst_ref_id=c.id, relation="see-also")
    return store, focus, papers


def test_second_hop_counts_distinct_refs_per_kind_and_label(hub: Hub) -> None:
    store, focus, _papers = _two_hop_fixture(hub)
    out = render_eye(
        store, handle_registry.format_handle("memory", focus.id), "fisheye+2hop"
    )
    assert "— linked (1 hop) —" in out, "the first hop still renders"
    assert "— second hop (2 neighbours out" in out
    assert "  2 paper via cites" in out
    assert "see-also" not in out.split("— second hop")[1]


def test_second_hop_expands_one_group_with_q(hub: Hub) -> None:
    store, focus, papers = _two_hop_fixture(hub)
    h = handle_registry.format_handle("memory", focus.id)
    out = render_eye(store, h, "fisheye+2hop", q="paper:cites")
    assert "— second hop: 2 paper via cites —" in out
    for p in papers:
        assert f"pa{p.id} — Paper" in out
    assert "— linked (1 hop) —" not in out, "an expansion shows the group only"
    with pytest.raises(ValueError, match="groups here: paper:cites"):
        render_eye(store, h, "fisheye+2hop", q="patent:cites")
    with pytest.raises(ValueError, match="fisheye\\+2hop only"):
        render_eye(store, h, "fisheye+1hop", q="paper:cites")


def test_second_hop_and_recall_refused_on_tree_kinds() -> None:
    for extent in ("fisheye+2hop", "+recall"):
        with pytest.raises(ValueError, match="stop at fisheye\\+1hop"):
            render_eye(None, "dc5", extent)


def test_parse_extent_reads_the_recall_suffix() -> None:
    from precis.utils.eye_render import parse_extent

    assert parse_extent("+recall") == (Extent.HOP1, True)
    assert parse_extent("fisheye+recall") == (Extent.FIDELITY, True)
    assert parse_extent("fisheye+2hop") == (Extent.HOP2, False)
    assert parse_extent("kwd") == (Extent.TOC, False)
    with pytest.raises(ValueError, match="unknown extent"):
        parse_extent("fisheye+3hop")


def _embedded_memory(runtime: Any, text: str) -> int:
    out = runtime.dispatch("put", {"kind": "memory", "text": text})
    m = re.search(r"id=(\d+)", out)
    assert m, out
    ref_id = int(m.group(1))
    store = runtime.hub.live_store
    (cid,) = store.chunks.card_chunk_ids([ref_id])
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, attempts) "
            "VALUES (%s, 'bge-m3', %s, 'ok', 1) "
            "ON CONFLICT (chunk_id, embedder) DO UPDATE "
            "SET vector = EXCLUDED.vector, status = 'ok'",
            (cid, runtime.hub.embed_one(text)),
        )
    return ref_id


def test_recall_lists_the_nearest_unlinked_memory(runtime_with_store: Any) -> None:
    rt = runtime_with_store
    a = _embedded_memory(rt, "copper catalyses nitrate reduction")
    b = _embedded_memory(rt, "copper catalyses nitrate reduction")
    store = rt.hub.live_store
    out = render_eye(store, handle_registry.format_handle("memory", a), "+recall")
    assert "— recall (nearest by embedding, finding+memory, k≤8) —" in out
    assert f"me{b}" in out
    assert f"me{a} —" not in out.split("— recall")[1], "never recalls itself"


def test_recall_says_so_when_the_ref_is_not_embedded(hub: Hub) -> None:
    store = hub.live_store
    mem = store.insert_ref(kind="memory", slug=None, title="Fresh note")
    out = render_eye(store, handle_registry.format_handle("memory", mem.id), "+recall")
    assert "— recall: no embedded chunk on this ref yet —" in out


# ── skill eyes: file-backed, atomic (no neighborhood) ──────────────────


def test_skill_eye_toc_is_a_one_line_bookmark() -> None:
    out = render_eye(None, "sk:precis-citation-help", "kwd")
    assert out.startswith("· ")
    assert "sk:precis-citation-help" in out
    assert "\n" not in out


def test_skill_eye_verbatim_renders_the_skill_body() -> None:
    out = render_eye(None, "sk:precis-citation-help", "verbatim")
    assert "sk:precis-citation-help" in out
    assert "[skill]" in out
    # the skill's own markdown body shows up verbatim, not just a bookmark
    assert "citation" in out.lower()
    assert len(out.splitlines()) > 1


def test_skill_eye_unknown_slug_raises() -> None:
    with pytest.raises(ValueError):
        render_eye(None, "sk:no-such-skill-slug", "verbatim")


def test_skill_eye_unknown_slug_degrades_not_crashes_in_working_set(hub: Hub) -> None:
    ws = WorkingSet()
    ws.focus("sk:no-such-skill-slug", "verbatim")
    out = render_working_set(hub.live_store, ws)
    assert "unrenderable" in out  # marker, not an exception

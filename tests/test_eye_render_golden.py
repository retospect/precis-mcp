"""Golden-output safety net for the link-ring half of ``render_eye``.

Pins the byte-exact text of every ``fisheye+1hop`` / ``+2hop`` link-ring
render exercised by ``tests/test_eye_render.py`` and ``tests/test_finding.py``,
so the neighbour query behind ``eye_render._first_hop`` can be re-plumbed
without silently changing ordering, labels, caps or flags. Ref handles differ
per DB run, so each seeded ref is normalised to a stable ``<A>``/``<B>``
placeholder before comparing.
"""

from __future__ import annotations

import re
from typing import Any

from precis.dispatch import Hub
from precis.handlers.finding import FindingHandler
from precis.handlers.plan import PlanHandler
from precis.store.types import ChunkInsert, Tag
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, link_claims, mint_hub
from precis.utils import handle_registry
from precis.utils.eye_render import render_eye
from precis.utils.working_set_render import render_working_set
from precis.workers.working_set import WorkingSet

# Normalised render output captured from the pre-refactor code.

GOLDEN_MEMORY_BY_RELATION = (
    "<A> [memory] SEI thickness anomaly note\n"
    "\n"
    "— linked (1 hop) —\n"
    "Notes & links:\n"
    "  related-to: <C> — Kinetics vs thermodynamics\n"
    "  supports: <B> — XPS depth profiling"
)

GOLDEN_AUDIT_FLAGGED = (
    "<A> [memory] Focus\n"
    "\n"
    "— linked (1 hop) —\n"
    "Notes & links:\n"
    "  related-to: <B> — Clean note\n"
    "  related-to: <C> — Flagged note  [AUDIT:ungrounded-number]"
)

GOLDEN_CODELESS = (
    "<A> [memory] Focus\n"
    "\n"
    "— linked (1 hop) —\n"
    "Notes & links:\n"
    "  related-to: websearch:characteristic-length-nanoparticles — ℓc search\n"
    "  related-to: <B> — XPS profiling"
)

GOLDEN_GROUP_CAP = (
    "<A> [memory] Hub note\n"
    "\n"
    "— linked (1 hop) —\n"
    "Notes & links:\n"
    "  related-to: <N0> — Related note 0\n"
    "  related-to: <N1> — Related note 1\n"
    "  related-to: <N2> — Related note 2\n"
    "  related-to: <N3> — Related note 3\n"
    "  related-to: <N4> — Related note 4\n"
    "  related-to: <N5> — Related note 5\n"
    "  related-to: <N6> — Related note 6\n"
    "  related-to: <N7> — Related note 7\n"
    "    … +2 more · get(kind='memory', id='<A>', extent='fisheye+1hop', q='related-to')"
)

GOLDEN_OVERFLOW_LIVE_ONLY = (
    "<A> [memory] Hub note\n"
    "\n"
    "— linked (1 hop) —\n"
    "Notes & links:\n"
    "  related-to: <N2> — Related note 2\n"
    "  related-to: <N3> — Related note 3\n"
    "  related-to: <N4> — Related note 4\n"
    "  related-to: <N5> — Related note 5\n"
    "  related-to: <N6> — Related note 6\n"
    "  related-to: <N7> — Related note 7\n"
    "  related-to: <N8> — Related note 8"
)

GOLDEN_QUEST_CHILD = (
    "<C> [quest] Ship the ring\n"
    "Ship the ring\n"
    "\n"
    "— linked (1 hop) —\n"
    "Roadmap:\n"
    "  served-by: <G> — Pin the groups\n"
    "  serves: <P> — Grow the mesh"
)

GOLDEN_QUEST_PARENT = (
    "<P> [quest] Grow the mesh\n"
    "Grow the mesh\n"
    "\n"
    "— linked (1 hop) —\n"
    "Roadmap:\n"
    "  served-by: <C> — Ship the ring"
)

GOLDEN_CONCEPT_ALGEBRA = (
    "<AL> [concept] Algebra\n"
    "\n"
    "— linked (1 hop) —\n"
    "Concepts:\n"
    "  prerequisite-of: <CA> — Calculus"
)

GOLDEN_CONCEPT_CALCULUS = (
    "<CA> [concept] Calculus\n"
    "\n"
    "— linked (1 hop) —\n"
    "Concepts:\n"
    "  has-prerequisite: <AL> — Algebra"
)

GOLDEN_COMPONENT_BOLT = (
    "<BO> [component] M3 bolt\n\n— linked (1 hop) —\nParts:\n  part-of: <FR> — Frame"
)

GOLDEN_COMPONENT_FRAME = (
    "<FR> [component] Frame\n\n— linked (1 hop) —\nParts:\n  contains: <BO> — M3 bolt"
)

GOLDEN_SEE_ALSO_TARGET = (
    "<B> [memory] Pointed at\n"
    "\n"
    "— linked (1 hop) —\n"
    "Notes & links:\n"
    "  <-see-also: <A> — Points out"
)

GOLDEN_SEE_ALSO_SOURCE = (
    "<A> [memory] Points out\n"
    "\n"
    "— linked (1 hop) —\n"
    "Notes & links:\n"
    "  see-also: <B> — Pointed at"
)

GOLDEN_PAPER_RING = (
    "<A> [paper] XPS profiling\n"
    "\n"
    "— linked (1 hop) —\n"
    "Notes & links:\n"
    "  related-to: <B> — XPS caveat note"
)

GOLDEN_NOTE_RING = (
    "<B> [memory] XPS caveat note\n"
    "\n"
    "— linked (1 hop) —\n"
    "Notes & links:\n"
    "  related-to: <A> — XPS profiling"
)

GOLDEN_WORKING_SET = (
    "# A Plan  (p)\n"
    "<PE> [heading]\n"
    "A Plan\n"
    "<PE> [paragraph]\n"
    "a section\n"
    "\n"
    "<A> [memory] a standalone note\n"
    "\n"
    "— linked (1 hop) —\n"
    "Notes & links:\n"
    "  related-to: <B> — linked from the note"
)

GOLDEN_2HOP = (
    "<F> [memory] Focus note\n"
    "\n"
    "— linked (1 hop) —\n"
    "Notes & links:\n"
    "  related-to: <B> — Note B\n"
    "  related-to: <C> — Note C\n"
    "\n"
    "— second hop (2 neighbours out; counts — expand one with q='<kind>:<label>') —\n"
    "  2 paper via cites"
)

GOLDEN_2HOP_EXPAND = (
    "<F> [memory] Focus note\n"
    "\n"
    "— second hop: 2 paper via cites —\n"
    "  <P1> — Paper 1\n"
    "  <P2> — Paper 2  [AUDIT:ungrounded-number]"
)

GOLDEN_RING_KIND_NARROWED = (
    "<F> [memory] Focus\n"
    "\n"
    "— linked (1 hop) —\n"
    "Notes & links:\n"
    "  related-to: <N> — A note"
)

GOLDEN_RING_KIND_FULL = (
    "<F> [memory] Focus\n"
    "\n"
    "— linked (1 hop) —\n"
    "Notes & links:\n"
    "  related-to: <N> — A note\n"
    "  related-to: <P> — XPS"
)

GOLDEN_RING_SPACE = (
    "<F> [memory] Focus\n"
    "\n"
    "— linked (1 hop) —\n"
    "Notes & links:\n"
    "  related-to: <R> — Repo note"
)

GOLDEN_2HOP_FULL = (
    "<F> [memory] Focus\n"
    "\n"
    "— linked (1 hop) —\n"
    "Notes & links:\n"
    "  related-to: <N> — Near\n"
    "  related-to: <S> — Side paper\n"
    "\n"
    "— second hop (2 neighbours out; counts — expand one with q='<kind>:<label>') —\n"
    "  1 memory via related-to\n"
    "  1 paper via related-to"
)

GOLDEN_2HOP_KIND_NARROWED = (
    "<F> [memory] Focus\n"
    "\n"
    "— linked (1 hop) —\n"
    "Notes & links:\n"
    "  related-to: <N> — Near\n"
    "\n"
    "— second hop (1 neighbours out; counts — expand one with q='<kind>:<label>') —\n"
    "  1 memory via related-to"
)

GOLDEN_ROADMAP_ORDER = (
    "<Q> [quest] The mesh quest\n"
    "The mesh quest\n"
    "\n"
    "— linked (1 hop) —\n"
    "Roadmap:\n"
    "  served-by: <S1> — Sub quest one\n"
    "  served-by: <S2> — Sub quest two\n"
    "  served-by: <P0> — Paper 0\n"
    "  served-by: <P1> — Paper 1\n"
    "  served-by: <P2> — Paper 2\n"
    "  served-by: <P3> — Paper 3\n"
    "  served-by: <P4> — Paper 4\n"
    "  served-by: <P5> — Paper 5\n"
    "    … +3 more · get(kind='quest', id='<Q>', extent='fisheye+1hop', q='served-by')"
)

GOLDEN_QUEST_GIST = (
    "<Q> [quest] Catalyse NO to NH3 selectivity above 90% at ambient\n"
    "Catalyse NO to NH3\n"
    "selectivity above 90% at ambient\n"
    "rubric: log_tof max · P_side max (optional)\n"
    "demand: log_tof=2.0"
)

GOLDEN_ORDERING = (
    "<F> [memory] Order focus\n"
    "\n"
    "— linked (1 hop) —\n"
    "Roadmap:\n"
    "  served-by: <SV> — Server quest\n"
    "Notes & links:\n"
    "  related-to: <SO> — Related outbound\n"
    "  related-to: <SI> — Related inbound\n"
    "  supports: <P2> — Support two\n"
    "  supports: <P3> — Support three\n"
    "  supports: <P1> — Support one"
)

GOLDEN_FINDING_CLAIM_GRAPH = (
    "◆ claim hub — state: unminted · support: 1✓ · flags: disputed\n"
    "\n"
    "<H> [finding] Bilayer graphene opens a bandgap under a perpendicular field.\n"
    "Bilayer graphene opens a bandgap under a perpendicular field.\n"
    "\n"
    "— linked (1 hop) —\n"
    "Claim graph:\n"
    "  <-establishes: <S> — Test paper est23a\n"
    "  <-refines: <R> — Bilayer graphene opens a tunable bandgap of up to 250 meV.\n"
    "  contradicted-by: <O> — Test paper contra23a"
)

GOLDEN_FINDING_POSTURE = (
    "◆ claim hub — state: unminted · support: 1✓\n"
    "\n"
    "<H> [finding] Bilayer graphene opens a bandgap under a perpendicular field.\n"
    "Bilayer graphene opens a bandgap under a perpendicular field.\n"
    "\n"
    "— linked (1 hop) —\n"
    "Claim graph:\n"
    "  <-corroborates: <P> — Test paper post23a"
)

GOLDEN_FINDING_PLAIN = (
    "<F> [finding] t\n"
    "claim body\n"
    "\n"
    "— linked (1 hop) —\n"
    "Argument:\n"
    "  derived-from: <P> — Test paper miller23a"
)


def _handle(ref: Any) -> str:
    return handle_registry.format_handle(ref.kind, ref.id)


def _norm(out: str, **refs: Any) -> str:
    """Replace each seeded ref's handle with ``<NAME>``, longest handle first
    so ``me12`` never clobbers ``me123``."""
    pairs = sorted(((_handle(r), n) for n, r in refs.items()), key=lambda p: -len(p[0]))
    for handle, name in pairs:
        out = re.sub(rf"(?<![A-Za-z0-9]){re.escape(handle)}(?!\d)", f"<{name}>", out)
    return out


def _check(expected: str, out: str, **refs: Any) -> None:
    got = _norm(out, **refs)
    assert not re.search(r"\b[a-z]{2}\d+\b", got), f"un-normalised handle in {got!r}"
    assert got == expected


def test_memory_1hop_by_relation(hub: Hub) -> None:
    store = hub.live_store
    mem = store.insert_ref(kind="memory", slug=None, title="SEI thickness anomaly note")
    paper = store.insert_ref(kind="paper", slug="wenzel16", title="XPS depth profiling")
    mem2 = store.insert_ref(
        kind="memory", slug=None, title="Kinetics vs thermodynamics"
    )
    store.add_link(src_ref_id=mem.id, dst_ref_id=paper.id, relation="supports")
    store.add_link(src_ref_id=mem.id, dst_ref_id=mem2.id, relation="related-to")

    out = render_eye(store, _handle(mem), "fisheye+1hop")
    _check(GOLDEN_MEMORY_BY_RELATION, out, A=mem, B=paper, C=mem2)


def test_memory_1hop_audit_flagged_neighbour(hub: Hub) -> None:
    store = hub.live_store
    mem = store.insert_ref(kind="memory", slug=None, title="Focus")
    clean = store.insert_ref(kind="memory", slug=None, title="Clean note")
    flagged = store.insert_ref(kind="memory", slug=None, title="Flagged note")
    for near in (clean, flagged):
        store.add_link(src_ref_id=mem.id, dst_ref_id=near.id, relation="related-to")
    store.add_tag(flagged.id, Tag.closed("AUDIT", "ungrounded-number"), set_by="system")

    out = render_eye(store, _handle(mem), "fisheye+1hop")
    _check(GOLDEN_AUDIT_FLAGGED, out, A=mem, B=clean, C=flagged)


def test_memory_1hop_codeless_neighbour(hub: Hub) -> None:
    store = hub.live_store
    mem = store.insert_ref(kind="memory", slug=None, title="Focus")
    cache = store.insert_ref(
        kind="websearch", slug="characteristic-length-nanoparticles", title="ℓc search"
    )
    paper = store.insert_ref(kind="paper", slug="wenzel16", title="XPS profiling")
    for near in (cache, paper):
        store.add_link(src_ref_id=mem.id, dst_ref_id=near.id, relation="related-to")

    out = render_eye(store, _handle(mem), "fisheye+1hop")
    _check(GOLDEN_CODELESS, out, A=mem, B=paper)


def test_memory_1hop_group_cap_overflow_line(hub: Hub) -> None:
    store = hub.live_store
    mem = store.insert_ref(kind="memory", slug=None, title="Hub note")
    others = [
        store.insert_ref(kind="memory", slug=None, title=f"Related note {i}")
        for i in range(10)
    ]
    for other in others:
        store.add_link(src_ref_id=mem.id, dst_ref_id=other.id, relation="related-to")

    out = render_eye(store, _handle(mem), "fisheye+1hop")
    refs = {f"N{i}": o for i, o in enumerate(others)}
    _check(GOLDEN_GROUP_CAP, out, A=mem, **refs)


def test_memory_1hop_overflow_counts_live_neighbours_only(hub: Hub) -> None:
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

    out = render_eye(store, _handle(mem), "fisheye+1hop")
    refs = {f"N{i}": o for i, o in enumerate(others)}
    _check(GOLDEN_OVERFLOW_LIVE_ONLY, out, A=mem, **refs)


def test_quest_1hop_serves_from_both_sides(hub: Hub) -> None:
    store = hub.live_store
    parent = store.insert_ref(kind="quest", slug=None, title="Grow the mesh")
    child = store.insert_ref(kind="quest", slug=None, title="Ship the ring")
    grandchild = store.insert_ref(kind="quest", slug=None, title="Pin the groups")
    store.add_link(src_ref_id=child.id, dst_ref_id=parent.id, relation="serves")
    store.add_link(src_ref_id=grandchild.id, dst_ref_id=child.id, relation="serves")

    refs = {"P": parent, "C": child, "G": grandchild}
    out = render_eye(store, _handle(child), "fisheye+1hop")
    _check(GOLDEN_QUEST_CHILD, out, **refs)
    out = render_eye(store, _handle(parent), "fisheye+1hop")
    _check(GOLDEN_QUEST_PARENT, out, **refs)


def test_concept_and_component_rings(hub: Hub) -> None:
    store = hub.live_store
    calc = store.insert_ref(kind="concept", slug=None, title="Calculus")
    algebra = store.insert_ref(kind="concept", slug=None, title="Algebra")
    store.add_link(
        src_ref_id=calc.id, dst_ref_id=algebra.id, relation="has-prerequisite"
    )
    frame = store.insert_ref(kind="component", slug="frame", title="Frame")
    bolt = store.insert_ref(kind="component", slug="m3-bolt", title="M3 bolt")
    store.add_link(src_ref_id=frame.id, dst_ref_id=bolt.id, relation="contains")

    refs = {"CA": calc, "AL": algebra, "FR": frame, "BO": bolt}
    out = render_eye(store, _handle(algebra), "fisheye+1hop")
    _check(GOLDEN_CONCEPT_ALGEBRA, out, **refs)
    out = render_eye(store, _handle(calc), "fisheye+1hop")
    _check(GOLDEN_CONCEPT_CALCULUS, out, **refs)
    out = render_eye(store, _handle(bolt), "fisheye+1hop")
    _check(GOLDEN_COMPONENT_BOLT, out, **refs)
    out = render_eye(store, _handle(frame), "fisheye+1hop")
    _check(GOLDEN_COMPONENT_FRAME, out, **refs)


def test_inbound_edge_without_inverse_reads_with_arrow(hub: Hub) -> None:
    store = hub.live_store
    a = store.insert_ref(kind="memory", slug=None, title="Points out")
    b = store.insert_ref(kind="memory", slug=None, title="Pointed at")
    store.add_link(src_ref_id=a.id, dst_ref_id=b.id, relation="see-also")

    out = render_eye(store, _handle(b), "fisheye+1hop")
    _check(GOLDEN_SEE_ALSO_TARGET, out, A=a, B=b)
    out = render_eye(store, _handle(a), "fisheye+1hop")
    _check(GOLDEN_SEE_ALSO_SOURCE, out, A=a, B=b)


def test_paper_1hop_linked_note_both_ways(hub: Hub) -> None:
    store = hub.live_store
    paper = store.insert_ref(kind="paper", slug="wenzel16", title="XPS profiling")
    note = store.insert_ref(kind="memory", slug=None, title="XPS caveat note")
    store.add_link(src_ref_id=note.id, dst_ref_id=paper.id, relation="related-to")

    out = render_eye(store, _handle(paper), "fisheye+1hop")
    _check(GOLDEN_PAPER_RING, out, A=paper, B=note)
    out = render_eye(store, _handle(note), "fisheye+1hop")
    _check(GOLDEN_NOTE_RING, out, A=paper, B=note)


def test_working_set_mixes_tree_and_link_eyes(hub: Hub) -> None:
    store = hub.live_store
    plan = PlanHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    plan.put(id="p", title="A Plan", project=proj)
    resp = plan.put(id="p", text="a section", at={"last": True})
    m = re.search(r"pe\d+", resp.body)
    assert m is not None
    sec = m.group(0)
    mem = store.insert_ref(kind="memory", slug=None, title="a standalone note")
    other = store.insert_ref(kind="memory", slug=None, title="linked from the note")
    store.add_link(src_ref_id=mem.id, dst_ref_id=other.id, relation="related-to")

    ws = WorkingSet()
    ws.focus(sec, "fisheye")  # tree eye
    ws.focus(_handle(mem), "fisheye+1hop")  # link eye, with its ring

    out = re.sub(r"pe\d+", "<PE>", render_working_set(store, ws))
    _check(GOLDEN_WORKING_SET, out, A=mem, B=other)


def _two_hop_fixture(hub: Hub) -> tuple[Any, Any, list[Any], list[Any]]:
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
    store.add_link(src_ref_id=b.id, dst_ref_id=c.id, relation="see-also")
    return store, focus, papers, [b, c]


def test_2hop_counts_and_expansion(hub: Hub) -> None:
    store, focus, papers, (b, c) = _two_hop_fixture(hub)
    store.add_tag(
        papers[1].id, Tag.closed("AUDIT", "ungrounded-number"), set_by="system"
    )
    h = _handle(focus)
    refs = {"F": focus, "B": b, "C": c, "P1": papers[0], "P2": papers[1]}
    _check(GOLDEN_2HOP, render_eye(store, h, "fisheye+2hop"), **refs)
    out = render_eye(store, h, "fisheye+2hop", q="paper:cites")
    _check(GOLDEN_2HOP_EXPAND, out, **refs)


def test_ring_filter_by_kind(hub: Hub) -> None:
    store = hub.live_store
    focus = store.insert_ref(kind="memory", slug=None, title="Focus")
    note = store.insert_ref(kind="memory", slug=None, title="A note")
    paper = store.insert_ref(kind="paper", slug="wenzel16", title="XPS")
    for near in (note, paper):
        store.add_link(src_ref_id=focus.id, dst_ref_id=near.id, relation="related-to")

    refs = {"F": focus, "N": note, "P": paper}
    h = _handle(focus)
    out = render_eye(store, h, "fisheye+1hop", ring={"kinds": "memory"})
    _check(GOLDEN_RING_KIND_NARROWED, out, **refs)
    _check(GOLDEN_RING_KIND_FULL, render_eye(store, h, "fisheye+1hop"), **refs)


def test_ring_filter_by_space(hub: Hub) -> None:
    store = hub.live_store
    focus = store.insert_ref(kind="memory", slug=None, title="Focus")
    repo = store.insert_ref(kind="memory", slug=None, title="Repo note")
    other = store.insert_ref(kind="memory", slug=None, title="Research note")
    store.add_tag(repo.id, Tag.closed("SPACE", "repo-dev"), set_by="system")
    for near in (repo, other):
        store.add_link(src_ref_id=focus.id, dst_ref_id=near.id, relation="related-to")

    out = render_eye(store, _handle(focus), "fisheye+1hop", ring={"space": "repo-dev"})
    _check(GOLDEN_RING_SPACE, out, F=focus, R=repo, O=other)


def test_ring_filter_reaches_second_hop(hub: Hub) -> None:
    store = hub.live_store
    focus = store.insert_ref(kind="memory", slug=None, title="Focus")
    near = store.insert_ref(kind="memory", slug=None, title="Near")
    side = store.insert_ref(kind="paper", slug="side20", title="Side paper")
    deep_paper = store.insert_ref(kind="paper", slug="deep21", title="Deep paper")
    deep_mem = store.insert_ref(kind="memory", slug=None, title="Deep note")
    store.add_link(src_ref_id=focus.id, dst_ref_id=near.id, relation="related-to")
    store.add_link(src_ref_id=focus.id, dst_ref_id=side.id, relation="related-to")
    for far in (deep_paper, deep_mem):
        store.add_link(src_ref_id=near.id, dst_ref_id=far.id, relation="related-to")

    refs = {"F": focus, "N": near, "S": side, "DP": deep_paper, "DM": deep_mem}
    h = _handle(focus)
    _check(GOLDEN_2HOP_FULL, render_eye(store, h, "fisheye+2hop"), **refs)
    out = render_eye(store, h, "fisheye+2hop", ring={"kinds": "memory"})
    _check(GOLDEN_2HOP_KIND_NARROWED, out, **refs)


def test_roadmap_lists_serving_sub_quests_before_papers(hub: Hub) -> None:
    store = hub.live_store
    q = store.insert_ref(kind="quest", slug=None, title="The mesh quest")
    papers = []
    for i in range(9):
        p = store.insert_ref(kind="paper", slug=f"s{i}", title=f"Paper {i}")
        papers.append(p)
        store.add_link(src_ref_id=p.id, dst_ref_id=q.id, relation="serves")
    sub1 = store.insert_ref(kind="quest", slug=None, title="Sub quest one")
    sub2 = store.insert_ref(kind="quest", slug=None, title="Sub quest two")
    store.add_link(src_ref_id=sub1.id, dst_ref_id=q.id, relation="serves")
    store.add_link(src_ref_id=sub2.id, dst_ref_id=q.id, relation="serves")

    refs = {f"P{i}": p for i, p in enumerate(papers)}
    out = render_eye(store, _handle(q), "fisheye+1hop")
    _check(GOLDEN_ROADMAP_ORDER, out, Q=q, S1=sub1, S2=sub2, **refs)


def test_quest_eye_gist_with_ring(hub: Hub) -> None:
    from precis.quest.logbook import append_entry

    store = hub.live_store
    q = store.insert_ref(
        kind="quest",
        slug=None,
        title="Catalyse NO to NH3\nselectivity above 90% at ambient",
        meta={
            "rubric_objectives": [
                {"key": "log_tof", "sense": "max"},
                {"key": "P_side", "sense": "max", "optional": True},
            ],
            "demand": {"log_tof": {"value": 2.0}},
        },
    )
    append_entry(
        store, q.id, text="Tried Fe-N4 sites", entry_type="hypothesis", by="agent"
    )
    _check(GOLDEN_QUEST_GIST, render_eye(store, _handle(q), "fisheye+1hop"), Q=q)


def test_ordering_within_label_inverse_symmetric_and_retired(hub: Hub) -> None:
    """Locks block order: three ``supports`` neighbours linked in a different
    order than their ids, an inbound ``serves`` (inverse reading), a
    symmetric ``related-to`` in each direction, and a retired neighbour."""
    store = hub.live_store
    focus = store.insert_ref(kind="memory", slug=None, title="Order focus")
    p1 = store.insert_ref(kind="paper", slug="ord1", title="Support one")
    p2 = store.insert_ref(kind="paper", slug="ord2", title="Support two")
    p3 = store.insert_ref(kind="paper", slug="ord3", title="Support three")
    dead = store.insert_ref(kind="paper", slug="ord4", title="Support retired")
    server = store.insert_ref(kind="quest", slug=None, title="Server quest")
    sym_out = store.insert_ref(kind="memory", slug=None, title="Related outbound")
    sym_in = store.insert_ref(kind="memory", slug=None, title="Related inbound")
    for near in (p2, dead, p3, p1):  # link order != id order
        store.add_link(src_ref_id=focus.id, dst_ref_id=near.id, relation="supports")
    store.add_link(src_ref_id=server.id, dst_ref_id=focus.id, relation="serves")
    store.add_link(src_ref_id=focus.id, dst_ref_id=sym_out.id, relation="related-to")
    store.add_link(src_ref_id=sym_in.id, dst_ref_id=focus.id, relation="related-to")
    store.retire_ref(dead.id)

    out = render_eye(store, _handle(focus), "fisheye+1hop")
    refs = {"P1": p1, "P2": p2, "P3": p3, "D": dead, "SV": server}
    _check(GOLDEN_ORDERING, out, F=focus, SO=sym_out, SI=sym_in, **refs)


# ── finding (claim hub) rings ─────────────────────────────────────────


def _seed_paper(store, *, cite_key: str) -> Any:
    ref = store.insert_ref(
        kind="paper", slug=cite_key, title=f"Test paper {cite_key}", meta={}
    )
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=f"Body chunk of {cite_key}.", meta={})]
    )
    return ref


def _ref(store, ref_id: int) -> Any:
    ref = store.get_ref(kind="finding", id=ref_id)
    assert ref is not None
    return ref


def test_finding_1hop_claim_graph(store) -> None:
    claim = CanonicalClaim(
        sentence="Bilayer graphene opens a bandgap under a perpendicular field.",
        scope={"material": "graphene"},
    )
    hub_id = mint_hub(store, claim)
    supporter = _seed_paper(store, cite_key="est23a")
    opponent = _seed_paper(store, cite_key="contra23a")
    sharper = mint_hub(
        store,
        CanonicalClaim(
            sentence="Bilayer graphene opens a tunable bandgap of up to 250 meV.",
            scope={"material": "graphene"},
        ),
    )
    attach_evidence(
        store,
        hub_ref_id=hub_id,
        paper_ref_id=supporter.id,
        role="establishes",
        meta={"support": "yes", "verified_by": "test"},
        set_by="system",
    )
    attach_evidence(
        store,
        hub_ref_id=hub_id,
        paper_ref_id=opponent.id,
        role="contradicts",
        meta={"support": "no", "verified_by": "test"},
        set_by="system",
    )
    link_claims(
        store,
        from_hub_ref_id=sharper,
        to_hub_ref_id=hub_id,
        relation="refines",
        set_by="system",
    )

    out = FindingHandler(hub=Hub(store=store)).get(id=hub_id, view="fisheye+1hop")
    refs = {"S": supporter, "O": opponent, "R": _ref(store, sharper)}
    _check(GOLDEN_FINDING_CLAIM_GRAPH, out.body, H=_ref(store, hub_id), **refs)


def test_finding_1hop_posture_header(store) -> None:
    claim = CanonicalClaim(
        sentence="Bilayer graphene opens a bandgap under a perpendicular field.",
        scope={"material": "graphene"},
    )
    hub_id = mint_hub(store, claim)
    paper = _seed_paper(store, cite_key="post23a")
    attach_evidence(
        store,
        hub_ref_id=hub_id,
        paper_ref_id=paper.id,
        role="corroborates",
        meta={"support": "yes", "verified_by": "test"},
        set_by="system",
    )

    out = FindingHandler(hub=Hub(store=store)).get(id=hub_id, view="fisheye+1hop")
    _check(GOLDEN_FINDING_POSTURE, out.body, H=_ref(store, hub_id), P=paper)


def test_finding_1hop_non_hub(store) -> None:
    paper = _seed_paper(store, cite_key="miller23a")
    h = FindingHandler(hub=Hub(store=store))
    resp = h.put(title="t", body="claim body", scope={}, cited_in="miller23a")
    m = re.search(r"id=(\d+)", resp.body)
    assert m is not None
    ref_id = int(m.group(1))

    out = h.get(id=ref_id, view="fisheye+1hop")
    _check(GOLDEN_FINDING_PLAIN, out.body, F=_ref(store, ref_id), P=paper)


def test_relation_inverses_are_an_involution(store: Any) -> None:
    """The ring maps ``neighbourhood``'s inbound inverse slug back to the
    stored relation (``eye_render._neighbourhood_edges``); that is exact
    only while every ``relations.inverse_slug`` points back at its slug."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT slug, inverse_slug FROM relations WHERE inverse_slug IS NOT NULL"
        ).fetchall()
    inverses = {str(s): str(i) for s, i in rows}
    broken = sorted(s for s, i in inverses.items() if inverses.get(i) != s)
    assert not broken, f"non-involutive relation inverses: {broken}"

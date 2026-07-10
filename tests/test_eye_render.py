"""Per-kind eye render (ADR 0051 §6) — the ladder generalizes, the neighborhood
shape is kind-specific: memory = link graph (by relation), paper/web = doc,
draft/plan = the reading-order fisheye."""

from __future__ import annotations

import re

from precis.dispatch import Hub
from precis.handlers.plan import PlanHandler
from precis.utils import handle_registry
from precis.utils.eye_render import render_eye
from precis.utils.working_set_render import render_working_set
from precis.workers.working_set import WorkingSet


def _pe(body: str) -> str:
    return re.search(r"pe\d+", body).group(0)


def test_memory_eye_1hop_shows_link_neighborhood_by_relation(hub: Hub) -> None:
    store = hub.store
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


def test_memory_eye_below_1hop_omits_the_link_neighborhood(hub: Hub) -> None:
    store = hub.store
    mem = store.insert_ref(kind="memory", slug=None, title="A note")
    other = store.insert_ref(kind="memory", slug=None, title="Linked")
    store.add_link(src_ref_id=mem.id, dst_ref_id=other.id, relation="related-to")
    h = handle_registry.format_handle("memory", mem.id)
    assert "— linked" not in render_eye(store, h, "verbatim")
    assert "— linked" not in render_eye(store, h, "summary")


def test_memory_eye_kwd_is_a_one_line_bookmark(hub: Hub) -> None:
    store = hub.store
    mem = store.insert_ref(kind="memory", slug=None, title="Bookmark me")
    h = handle_registry.format_handle("memory", mem.id)
    out = render_eye(store, h, "kwd")
    assert out.startswith("· ") and "Bookmark me" in out and "\n" not in out


def test_doc_eye_paper_renders_the_ref(hub: Hub) -> None:
    store = hub.store
    paper = store.insert_ref(kind="paper", slug="li21", title="Cryo-EM SEI")
    h = handle_registry.format_handle("paper", paper.id)
    out = render_eye(store, h, "verbatim")
    assert f"pa{paper.id}" in out and "Cryo-EM SEI" in out


def test_paper_eye_1hop_surfaces_a_linked_note_both_ways(hub: Hub) -> None:
    """Links are symmetric: a note linked to a paper must appear when you fisheye
    the PAPER (not only when you fisheye the note)."""
    store = hub.store
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
    store = hub.store
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
    out = render_working_set(hub.store, ws)
    assert "unrenderable" in out  # marker, not an exception

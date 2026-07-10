"""The reference ring — ``fisheye+1hop`` (ADR 0051 §6, refeye slice). Exercised
against a real ``plan`` section that cites a paper, mentions a memory (outbound),
and has a memory linked to it (inbound), plus the fisheye HOP1 wiring."""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.handlers.plan import PlanHandler
from precis.utils.fisheye import render_fisheye
from precis.utils.refeye import render_reference_ring


def _handles(body: str) -> list[str]:
    return re.findall(r"pe\d+", body)


@pytest.fixture
def plan(hub: Hub) -> PlanHandler:
    return PlanHandler(hub=hub)


def _section_with_refs(hub: Hub, plan: PlanHandler):
    """A plan whose 'Mechanisms' section cites a paper + a memory in its child,
    with a second memory linked *to* the section. Returns (section_chunk,
    reading_order, {names→id})."""
    store = hub.store
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    paper = store.insert_ref(
        kind="paper", slug="coolpaper", title="A Cool Paper On SEI"
    )
    mem_out = store.insert_ref(kind="memory", slug=None, title="Note the section cites")
    mem_in = store.insert_ref(
        kind="memory", slug=None, title="Note linked to the section"
    )

    plan.put(id="p", title="Root", project=proj)
    sec = _handles(plan.put(id="p", text="Mechanisms", at={"last": True}).body)[0]
    plan.put(
        id="p",
        text=f"This builds on paper:{paper.id} and the idea in memory:{mem_out.id}.",
        at={"into": sec},
    )
    sec_chunk = store.get_draft_chunk(sec, kind="plan")
    chunks = store.reading_order(sec_chunk.ref_id, kind="plan")
    # inbound: a memory that links TO the section (the "noted on this" edge)
    store.add_link(
        src_ref_id=mem_in.id, dst_ref_id=sec_chunk.ref_id, relation="related-to"
    )
    return (
        sec_chunk,
        chunks,
        {
            "paper": paper.id,
            "mem_out": mem_out.id,
            "mem_in": mem_in.id,
        },
    )


def test_ring_groups_cited_and_notes(hub: Hub, plan: PlanHandler) -> None:
    sec_chunk, chunks, ids = _section_with_refs(hub, plan)
    ring = render_reference_ring(hub.store, sec_chunk, chunks)

    assert "— referenced (1 hop) —" in ring
    # cited paper, rendered by kind (cite_key + title)
    assert "Cited:" in ring
    assert "paper:coolpaper — A Cool Paper On SEI" in ring
    # both the outbound-mentioned and the inbound-linked memory land in Notes
    assert "Notes:" in ring
    assert "Note the section cites" in ring  # outbound mention
    assert "Note linked to the section" in ring  # inbound related-to edge


def test_ring_empty_when_section_points_nowhere(hub: Hub, plan: PlanHandler) -> None:
    store = hub.store
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    plan.put(id="p", title="Root", project=proj)
    sec = _handles(plan.put(id="p", text="A lonely section", at={"last": True}).body)[0]
    sec_chunk = store.get_draft_chunk(sec, kind="plan")
    chunks = store.reading_order(sec_chunk.ref_id, kind="plan")
    assert render_reference_ring(store, sec_chunk, chunks) == "— no references —"


def test_ring_caps_each_group_with_overflow(hub: Hub, plan: PlanHandler) -> None:
    store = hub.store
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    paper_ids = [
        store.insert_ref(kind="paper", slug=f"p{i}", title=f"Paper {i}").id
        for i in range(11)
    ]
    plan.put(id="p", title="Root", project=proj)
    cites = " ".join(f"paper:{pid}" for pid in paper_ids)
    sec = _handles(
        plan.put(id="p", text=f"Cites all: {cites}", at={"last": True}).body
    )[0]
    sec_chunk = store.get_draft_chunk(sec, kind="plan")
    chunks = store.reading_order(sec_chunk.ref_id, kind="plan")

    ring = render_reference_ring(store, sec_chunk, chunks, cap=8)
    assert "Cited:" in ring
    assert ring.count("  · ") == 8  # capped
    assert "+3 more — focus to expand" in ring  # 11 - 8


def test_fisheye_hop1_appends_the_ring(hub: Hub, plan: PlanHandler) -> None:
    sec_chunk, _chunks, _ids = _section_with_refs(hub, plan)
    out = render_fisheye(
        hub.store, kind="plan", handle=sec_chunk.dc, extent="fisheye+1hop"
    )
    # the fidelity span (the section body) is present …
    assert "Mechanisms" in out
    # … and so is the reference ring
    assert "— referenced (1 hop) —" in out
    assert "paper:coolpaper" in out

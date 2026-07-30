"""The reference ring — ``fisheye+1hop`` (ADR 0051 §6, refeye slice). Exercised
against a real ``plan`` section that cites a paper, mentions a memory (outbound),
and has a memory linked to it (inbound), plus the fisheye HOP1 wiring."""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.plan import PlanHandler
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
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


# ── Claims group — [pub_id] claim-hub explosion (Taproot slice R1) ──────

_CLAIM = CanonicalClaim(
    sentence="Pd/C catalyzes Suzuki coupling at room temperature with a mild base.",
    scope={"material": "Pd/C", "method": "Suzuki coupling", "regime": "RT"},
)


def _hub_pub_id(store: Any, hub_ref_id: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT id_value FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = 'pub_id'",
            (hub_ref_id,),
        ).fetchone()
    assert row is not None, f"no pub_id minted for hub ref_id={hub_ref_id}"
    return str(row[0])


def _cites(store: Any, *, src: int, dst: int) -> None:
    store.add_link(src_ref_id=src, dst_ref_id=dst, relation="cites")


def _plan_section_citing(hub: Hub, plan: PlanHandler, text: str):
    """A minimal plan with one section whose body is ``text``. Returns
    ``(section_chunk, reading_order)``."""
    store = hub.store
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    plan.put(id="p", title="Root", project=proj)
    sec = _handles(plan.put(id="p", text="Section", at={"last": True}).body)[0]
    plan.put(id="p", text=text, at={"into": sec})
    sec_chunk = store.get_draft_chunk(sec, kind="plan")
    chunks = store.reading_order(sec_chunk.ref_id, kind="plan")
    return sec_chunk, chunks


def test_ring_claims_group_explodes_cited_hub_with_grounding(
    hub: Hub, plan: PlanHandler
) -> None:
    store = hub.store
    claim_hub = mint_hub(store, _CLAIM)
    originator = store.insert_ref(
        kind="paper", slug="orig1", title="The original report", year=2001
    ).id
    follower = store.insert_ref(
        kind="paper", slug="folw1", title="Follows the original", year=2005
    ).id
    attach_evidence(
        store,
        hub_ref_id=claim_hub,
        paper_ref_id=originator,
        role="corroborates",
        meta={"source_handle": "pc999"},
    )
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=follower, role="corroborates"
    )
    _cites(store, src=follower, dst=originator)  # promotes `originator`

    pub_id = _hub_pub_id(store, claim_hub)
    sec_chunk, chunks = _plan_section_citing(
        hub, plan, f"This claim is grounded: [{pub_id}]."
    )

    ring = render_reference_ring(store, sec_chunk, chunks)

    assert "Claims:" in ring
    assert "The original report" in ring  # claim hub title
    assert "★" in ring  # derived originator marked
    assert "pa" in ring  # paper handle prefix for the originator
    assert "grounding: pc999" in ring  # the grounding chunk pointer
    assert "+1 corroborators" in ring  # follower is a corroborator, not shown flat


def test_ring_claims_group_skips_non_hub_finding(hub: Hub, plan: PlanHandler) -> None:
    from precis.identity import make_pub_id

    store = hub.store
    finding = store.insert_ref(
        kind="finding", slug=None, title="An ordinary finding", meta={}
    ).id
    pub_id = make_pub_id(f"sha256:not-a-hub-{finding}")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('pub_id', %s, %s, 'test')",
            (pub_id, finding),
        )
        conn.commit()

    sec_chunk, chunks = _plan_section_citing(
        hub, plan, f"Cites a non-hub finding: [{pub_id}]."
    )

    ring = render_reference_ring(store, sec_chunk, chunks)

    assert "Claims" not in ring
    assert ring == "— no references —"


def test_ring_claims_group_caps_originators_with_overflow(
    hub: Hub, plan: PlanHandler
) -> None:
    store = hub.store
    claim_hub = mint_hub(store, _CLAIM)
    roots = [
        store.insert_ref(
            kind="paper", slug=f"root{i}", title=f"Root {i}", year=2000 + i
        ).id
        for i in range(10)
    ]
    citer = store.insert_ref(kind="paper", slug="citer1", title="Citer", year=2020).id
    for p in (*roots, citer):
        attach_evidence(
            store, hub_ref_id=claim_hub, paper_ref_id=p, role="corroborates"
        )
    for r in roots:
        _cites(store, src=citer, dst=r)  # all 10 roots become originators

    pub_id = _hub_pub_id(store, claim_hub)
    sec_chunk, chunks = _plan_section_citing(hub, plan, f"See [{pub_id}].")

    ring = render_reference_ring(store, sec_chunk, chunks)

    assert ring.count("★") == 8  # capped at the ring's default cap
    assert "+2 more — focus to expand" in ring  # 10 - 8


def test_ring_claims_group_falls_back_to_corroborators_when_undetermined(
    hub: Hub, plan: PlanHandler
) -> None:
    store = hub.store
    claim_hub = mint_hub(store, _CLAIM)
    a = store.insert_ref(kind="paper", slug="fba", title="Supporter A", year=2001).id
    b = store.insert_ref(kind="paper", slug="fbb", title="Supporter B", year=2002).id
    for p in (a, b):
        attach_evidence(
            store, hub_ref_id=claim_hub, paper_ref_id=p, role="corroborates"
        )
    # no intra-set `cites` edges → no originator derives (seniority.py fallback)

    pub_id = _hub_pub_id(store, claim_hub)
    sec_chunk, chunks = _plan_section_citing(hub, plan, f"See [{pub_id}].")

    ring = render_reference_ring(store, sec_chunk, chunks)

    assert "Claims:" in ring
    assert "no originator derived yet" in ring
    assert "Supporter A" in ring and "Supporter B" in ring
    assert "★" not in ring

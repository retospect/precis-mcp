"""The reference ring — ``fisheye+1hop`` (refeye slice). Exercised
against a real ``plan`` section that cites a paper, mentions a memory (outbound),
and has a memory linked to it (inbound), plus the fisheye HOP1 wiring."""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.plan import PlanHandler
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, link_claims, mint_hub
from precis.utils import handle_registry
from precis.utils.fisheye import render_fisheye
from precis.utils.refeye import collect_ring, render_reference_ring


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


# ── Claims group — authorial pin marker (Taproot slice A2) ─────────────


def test_ring_claims_group_marks_pinned_paper(hub: Hub, plan: PlanHandler) -> None:
    """A ``[<pub_id>>...]`` replace pin naming the derived originator
    itself just marks it 📌 (no divergence — pin matches the derived
    set)."""
    store = hub.store
    claim_hub = mint_hub(store, _CLAIM)
    originator = store.insert_ref(
        kind="paper", slug="pinorig1", title="The pinned original report", year=2001
    ).id
    follower = store.insert_ref(
        kind="paper", slug="pinfoll1", title="Follows the original", year=2005
    ).id
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=originator, role="corroborates"
    )
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=follower, role="corroborates"
    )
    _cites(store, src=follower, dst=originator)

    pub_id = _hub_pub_id(store, claim_hub)
    origin_handle = handle_registry.format_handle("paper", originator)
    sec_chunk, chunks = _plan_section_citing(
        hub, plan, f"This claim is grounded: [{pub_id}>{origin_handle}]."
    )

    ring = render_reference_ring(store, sec_chunk, chunks)

    assert "Claims:" in ring
    assert "📌" in ring
    assert "(pinned; derived:" not in ring  # pin matches derived — no divergence note


def test_ring_claims_group_notes_pin_divergence(hub: Hub, plan: PlanHandler) -> None:
    """A pin naming a *different* paper than the derived originator gets
    both the 📌 marker (surfaced even though it isn't part of the derived
    evidence) and a short divergence note."""
    store = hub.store
    claim_hub = mint_hub(store, _CLAIM)
    originator = store.insert_ref(
        kind="paper", slug="divorig1", title="The original report", year=2001
    ).id
    follower = store.insert_ref(
        kind="paper", slug="divfoll1", title="Follows the original", year=2005
    ).id
    pinned_paper = store.insert_ref(
        kind="paper", slug="divpin1", title="Author's pick", year=2010
    ).id
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=originator, role="corroborates"
    )
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=follower, role="corroborates"
    )
    _cites(store, src=follower, dst=originator)

    pub_id = _hub_pub_id(store, claim_hub)
    pinned_handle = handle_registry.format_handle("paper", pinned_paper)
    sec_chunk, chunks = _plan_section_citing(
        hub, plan, f"This claim is grounded: [{pub_id}>{pinned_handle}]."
    )

    ring = render_reference_ring(store, sec_chunk, chunks)

    assert "Claims:" in ring
    assert "📌" in ring
    assert "Author's pick" in ring  # surfaced even though not derived evidence
    assert "(pinned; derived:" in ring
    origin_handle = handle_registry.format_handle("paper", originator)
    assert origin_handle in ring


def test_ring_claims_group_supplement_pin_never_notes_divergence(
    hub: Hub, plan: PlanHandler
) -> None:
    """A `+` supplement pin naming a paper the derivation never picked is
    correct usage (derived plus this), not a divergence — only `>` gets
    the `(pinned; derived: ...)` note."""
    store = hub.store
    claim_hub = mint_hub(store, _CLAIM)
    originator = store.insert_ref(
        kind="paper", slug="supporig1", title="The original report", year=2001
    ).id
    follower = store.insert_ref(
        kind="paper", slug="supfoll1", title="Follows the original", year=2005
    ).id
    pinned_paper = store.insert_ref(
        kind="paper", slug="suppin1", title="Extra evidence", year=2010
    ).id
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=originator, role="corroborates"
    )
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=follower, role="corroborates"
    )
    _cites(store, src=follower, dst=originator)

    pub_id = _hub_pub_id(store, claim_hub)
    pinned_handle = handle_registry.format_handle("paper", pinned_paper)
    sec_chunk, chunks = _plan_section_citing(
        hub, plan, f"This claim is grounded: [{pub_id}+{pinned_handle}]."
    )

    ring = render_reference_ring(store, sec_chunk, chunks)

    assert "Claims:" in ring
    assert "📌" in ring  # still marked as pinned/cited
    assert "Extra evidence" in ring
    assert "(pinned; derived:" not in ring  # supplement never diverges


def test_ring_claims_group_pin_overflow_gets_more_line(
    hub: Hub, plan: PlanHandler
) -> None:
    """More pinned-but-not-derived-evidence papers than `cap` still get a
    visible `+N more` line — the ring never silently truncates (§6)."""
    store = hub.store
    claim_hub = mint_hub(store, _CLAIM)
    originator = store.insert_ref(
        kind="paper", slug="ovorig1", title="The original report", year=2001
    ).id
    follower = store.insert_ref(
        kind="paper", slug="ovfoll1", title="Follows the original", year=2005
    ).id
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=originator, role="corroborates"
    )
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=follower, role="corroborates"
    )
    _cites(store, src=follower, dst=originator)

    extra_papers = [
        store.insert_ref(kind="paper", slug=f"ovextra{i}", title=f"Extra {i}").id
        for i in range(10)
    ]
    pub_id = _hub_pub_id(store, claim_hub)
    handles = ",".join(handle_registry.format_handle("paper", r) for r in extra_papers)
    sec_chunk, chunks = _plan_section_citing(
        hub, plan, f"This claim is grounded: [{pub_id}+{handles}]."
    )

    ring = render_reference_ring(store, sec_chunk, chunks, cap=8)

    assert "Claims:" in ring
    assert "📌" in ring
    assert "+2 more — focus to expand" in ring  # 10 extra pins - cap 8


# ── Claims group — [fi<id>] finding-handle claim-hub cite (kind+serial) ──


def test_ring_claims_group_explodes_cited_hub_via_finding_handle(
    hub: Hub, plan: PlanHandler
) -> None:
    """A hub cited by its ``fi<id>`` handle (the preferred form) explodes
    into evidence exactly like the content-hash ``[pub_id]`` form."""
    store = hub.store
    claim_hub = mint_hub(store, _CLAIM)
    originator = store.insert_ref(
        kind="paper", slug="fihorig1", title="The original report", year=2001
    ).id
    follower = store.insert_ref(
        kind="paper", slug="fihfoll1", title="Follows the original", year=2005
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

    hub_handle = handle_registry.format_handle("finding", claim_hub)
    sec_chunk, chunks = _plan_section_citing(
        hub, plan, f"This claim is grounded: [{hub_handle}]."
    )

    ring = render_reference_ring(store, sec_chunk, chunks)

    assert "Claims:" in ring
    assert "The original report" in ring  # claim hub title
    assert "★" in ring  # derived originator marked
    assert "grounding: pc999" in ring  # the grounding chunk pointer
    assert "+1 corroborators" in ring  # follower is a corroborator, not shown flat

    # the hub explodes ONLY under Claims — the generic outbound handle walk
    # also resolves `[fi<id>]` as an ordinary finding link, but that flat
    # Notes/Cross-refs copy is redundant noise now that Claims has the
    # richer render, so it's deduped out.
    groups = collect_ring(store, sec_chunk, chunks)
    assert claim_hub in {rid for rid, _block in groups["Claims"]}
    for name in ("Notes", "Cross-refs"):
        assert claim_hub not in {rid for rid, _label in groups.get(name, [])}


def test_ring_claims_group_marks_pinned_paper_via_finding_handle(
    hub: Hub, plan: PlanHandler
) -> None:
    """A ``[fi<id>>...]`` replace pin — same pin marking as the ``[pub_id>...]``
    form, via the finding-handle cite."""
    store = hub.store
    claim_hub = mint_hub(store, _CLAIM)
    originator = store.insert_ref(
        kind="paper", slug="fihpinorig1", title="The pinned original report", year=2001
    ).id
    follower = store.insert_ref(
        kind="paper", slug="fihpinfoll1", title="Follows the original", year=2005
    ).id
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=originator, role="corroborates"
    )
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=follower, role="corroborates"
    )
    _cites(store, src=follower, dst=originator)

    hub_handle = handle_registry.format_handle("finding", claim_hub)
    origin_handle = handle_registry.format_handle("paper", originator)
    sec_chunk, chunks = _plan_section_citing(
        hub, plan, f"This claim is grounded: [{hub_handle}>{origin_handle}]."
    )

    ring = render_reference_ring(store, sec_chunk, chunks)

    assert "Claims:" in ring
    assert "📌" in ring
    assert "(pinned; derived:" not in ring  # pin matches derived — no divergence note


def test_ring_claims_group_dedups_hub_cited_via_both_forms(
    hub: Hub, plan: PlanHandler
) -> None:
    """A hub cited via BOTH ``[pub_id]`` and ``[fi<id>]`` in one span
    explodes once, not twice."""
    store = hub.store
    claim_hub = mint_hub(store, _CLAIM)
    originator = store.insert_ref(
        kind="paper", slug="fihdedup1", title="The original report", year=2001
    ).id
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=originator, role="corroborates"
    )

    pub_id = _hub_pub_id(store, claim_hub)
    hub_handle = handle_registry.format_handle("finding", claim_hub)
    sec_chunk, chunks = _plan_section_citing(
        hub, plan, f"Grounded here: [{pub_id}] and again here: [{hub_handle}]."
    )

    ring = render_reference_ring(store, sec_chunk, chunks)

    assert "Claims:" in ring
    assert ring.count("The original report") == 1  # exploded once, not twice


# ── Claims group — refines advisory neighbours (migration 0100) ─────────


def test_ring_claims_group_surfaces_refined_by_when_a_sharper_hub_exists(
    hub: Hub, plan: PlanHandler
) -> None:
    """Citing the ORIGINAL claim surfaces an advisory ``↰ refined by`` line
    naming the sharper hub — the "a sharper version exists" nudge."""
    store = hub.store
    original = mint_hub(store, _CLAIM)
    sharper = mint_hub(
        store,
        CanonicalClaim(
            sentence="Pd/C: Suzuki at 25 °C, K2CO3, aqueous EtOH, >90% yield.",
            scope={},
        ),
    )
    link_claims(store, from_hub_ref_id=sharper, to_hub_ref_id=original)

    orig_handle = handle_registry.format_handle("finding", original)
    sharper_handle = handle_registry.format_handle("finding", sharper)
    sec_chunk, chunks = _plan_section_citing(
        hub, plan, f"The original claim: [{orig_handle}]."
    )

    ring = render_reference_ring(store, sec_chunk, chunks)

    assert "Claims:" in ring
    assert "refined by" in ring
    assert sharper_handle in ring  # names the sharper hub by its fi<id>
    assert "Suzuki at 25 °C" in ring  # the sharper claim's sentence


def test_ring_claims_group_surfaces_refines_when_citing_the_sharper_hub(
    hub: Hub, plan: PlanHandler
) -> None:
    """Citing the SHARPER claim surfaces an advisory ``↳ refines`` line
    naming the coarser hub it sharpens."""
    store = hub.store
    original = mint_hub(store, _CLAIM)
    sharper = mint_hub(
        store,
        CanonicalClaim(
            sentence="Pd/C: Suzuki at 25 °C, K2CO3, aqueous EtOH, >90% yield.",
            scope={},
        ),
    )
    link_claims(store, from_hub_ref_id=sharper, to_hub_ref_id=original)

    original_handle = handle_registry.format_handle("finding", original)
    sharper_handle = handle_registry.format_handle("finding", sharper)
    sec_chunk, chunks = _plan_section_citing(
        hub, plan, f"The sharper claim: [{sharper_handle}]."
    )

    ring = render_reference_ring(store, sec_chunk, chunks)

    assert "Claims:" in ring
    assert "refines" in ring
    assert original_handle in ring  # names the coarser original by its fi<id>


def test_ring_claims_group_no_refines_lines_when_hub_has_no_claim_links(
    hub: Hub, plan: PlanHandler
) -> None:
    """A cited hub with no ``refines`` neighbours renders no advisory lines."""
    store = hub.store
    claim_hub = mint_hub(store, _CLAIM)
    hub_handle = handle_registry.format_handle("finding", claim_hub)
    sec_chunk, chunks = _plan_section_citing(
        hub, plan, f"A lone claim: [{hub_handle}]."
    )

    ring = render_reference_ring(store, sec_chunk, chunks)

    assert "Claims:" in ring
    assert "refined by" not in ring
    assert "↳ refines" not in ring


# ── Claims group — interleaved mining across the two grammars ───────────

_CLAIM_B = CanonicalClaim(
    sentence="Nickel foam electrodes reduce overpotential in alkaline OER.",
    scope={"material": "Ni foam", "method": "OER"},
)


def test_ring_claims_group_orders_across_grammars_by_text_position(
    hub: Hub, plan: PlanHandler
) -> None:
    """A ``[fi<id>]`` cite earlier in the text than a ``[pub_id]`` cite of
    a DIFFERENT hub must still render hubA before hubB — the two grammars
    are mined as one interleaved, position-sorted pass, not sequentially
    (which would put every ``[pub_id]`` hit ahead of every handle hit
    regardless of where each actually sits in the text)."""
    store = hub.store
    hub_a = mint_hub(store, _CLAIM)
    hub_b = mint_hub(store, _CLAIM_B)
    paper_a = store.insert_ref(kind="paper", slug="ordera1", title="Paper A").id
    paper_b = store.insert_ref(kind="paper", slug="orderb1", title="Paper B").id
    attach_evidence(store, hub_ref_id=hub_a, paper_ref_id=paper_a, role="corroborates")
    attach_evidence(store, hub_ref_id=hub_b, paper_ref_id=paper_b, role="corroborates")

    hub_a_handle = handle_registry.format_handle("finding", hub_a)
    pub_id_b = _hub_pub_id(store, hub_b)
    sec_chunk, chunks = _plan_section_citing(
        hub,
        plan,
        f"First cites hubA: [{hub_a_handle}]. Then cites hubB: [{pub_id_b}].",
    )

    ring = render_reference_ring(store, sec_chunk, chunks)

    assert "Claims:" in ring
    pos_a = ring.index(_CLAIM.sentence)
    pos_b = ring.index(_CLAIM_B.sentence)
    assert pos_a < pos_b  # hubA cited first in text -> rendered first


def test_ring_claims_group_keeps_pin_when_pinned_form_seen_first(
    hub: Hub, plan: PlanHandler
) -> None:
    """The SAME hub cited twice in one chunk — pinned via its ``fi<id>``
    handle FIRST, then bare via ``[pub_id]`` later — must keep the pin:
    the first-in-text occurrence wins the ``seen`` slot, so scanning the
    two grammars sequentially (which would let the later, unpinned
    ``[pub_id]`` position claim ``seen`` first if pub_ids are scanned
    before handles) would silently drop the pin."""
    store = hub.store
    claim_hub = mint_hub(store, _CLAIM)
    originator = store.insert_ref(
        kind="paper", slug="pinfirst1", title="The pinned original report", year=2001
    ).id
    follower = store.insert_ref(
        kind="paper", slug="pinfirst2", title="Follows the original", year=2005
    ).id
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=originator, role="corroborates"
    )
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=follower, role="corroborates"
    )
    _cites(store, src=follower, dst=originator)

    hub_handle = handle_registry.format_handle("finding", claim_hub)
    origin_handle = handle_registry.format_handle("paper", originator)
    pub_id = _hub_pub_id(store, claim_hub)
    sec_chunk, chunks = _plan_section_citing(
        hub,
        plan,
        f"Pinned first: [{hub_handle}>{origin_handle}]. Bare again: [{pub_id}].",
    )

    ring = render_reference_ring(store, sec_chunk, chunks)

    assert "Claims:" in ring
    assert ring.count(_CLAIM.sentence) == 1  # still deduped to one block
    assert "📌" in ring  # the pin from the first-seen occurrence survives

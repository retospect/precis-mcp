"""Taproot Phase-2 slice 2c — seniority derivation
(`src/precis/taproot/seniority.py`) + `finding` `view='evidence'`.

DB-backed (real `refs`/`chunks`/`ref_tags`/`links` via the `store`
fixture); no LLM. Mirrors the setup style of `tests/test_taproot_hub.py`:
mint a hub via `hub.mint_hub`, attach evidence via `hub.attach_evidence`,
and write `cites` edges / `retraction_status` directly (neither is a
taproot-vocab write, so a raw `store.add_link` / UPDATE is fine here).
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.finding import FindingHandler
from precis.store.types import Tag
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, link_claims, mint_hub
from precis.taproot.seniority import derive_evidence, derive_refines
from precis.utils import handle_registry
from tests.workers._helpers import seed_chunk, seed_ref

_CLAIM = CanonicalClaim(
    sentence="Pd/C catalyzes Suzuki coupling at room temperature with a mild base.",
    scope={"material": "Pd/C", "method": "Suzuki coupling", "regime": "RT"},
)


def _make_handler(store: Any) -> FindingHandler:
    return FindingHandler(hub=Hub(store=store))


_slug_counter = itertools.count(1)


def _paper(store: Any, *, title: str, year: int | None = None) -> int:
    slug = f"sen{next(_slug_counter)}"
    ref = store.insert_ref(kind="paper", slug=slug, title=title, year=year, meta={})
    return ref.id


def _cites(store: Any, *, src: int, dst: int) -> None:
    """Direct write: `cites` is not a taproot role, so this bypasses hub.py."""
    store.add_link(src_ref_id=src, dst_ref_id=dst, relation="cites")


def _set_retraction_status(store: Any, ref_id: int, status: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET retraction_status = %s WHERE ref_id = %s",
            (status, ref_id),
        )
        conn.commit()


# ── acceptance #4: originator derivation from intra-set cites ──────────


def test_derive_evidence_splits_originators_from_corroborators(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    a = _paper(store, title="A — the original report", year=2001)
    b = _paper(store, title="B — a second original report", year=2002)
    c = _paper(store, title="C — follows A", year=2005)
    d = _paper(store, title="D — follows A and B", year=2006)
    for p in (a, b, c, d):
        attach_evidence(store, hub_ref_id=hub, paper_ref_id=p, role="corroborates")

    # C, D cite A; D also cites B — so A, B are originators.
    _cites(store, src=c, dst=a)
    _cites(store, src=d, dst=a)
    _cites(store, src=d, dst=b)

    evidence = derive_evidence(store, hub)

    assert [e.paper_ref_id for e in evidence.originators] == [a, b]
    assert all(e.derived_role == "establishes" for e in evidence.originators)
    assert all(e.is_originator for e in evidence.originators)

    assert [e.paper_ref_id for e in evidence.corroborators] == [c, d]
    assert all(e.derived_role == "corroborates" for e in evidence.corroborators)
    assert not any(e.is_originator for e in evidence.corroborators)

    assert evidence.coverage_note is None


def test_derive_evidence_orders_within_group_by_year_then_ref_id(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    late = _paper(store, title="Late originator", year=2010)
    early = _paper(store, title="Early originator", year=2000)
    no_year = _paper(store, title="Undated originator", year=None)
    citer = _paper(store, title="Citer", year=2015)
    for p in (late, early, no_year, citer):
        attach_evidence(store, hub_ref_id=hub, paper_ref_id=p, role="corroborates")
    _cites(store, src=citer, dst=late)
    _cites(store, src=citer, dst=early)
    _cites(store, src=citer, dst=no_year)

    evidence = derive_evidence(store, hub)

    # Earliest year first; NULL year sorts last.
    assert [e.paper_ref_id for e in evidence.originators] == [early, late, no_year]


# ── fallback: no intra-set cites held ───────────────────────────────────


def test_derive_evidence_falls_back_to_all_corroborators_when_no_intra_set_cites(
    store: Any,
) -> None:
    hub = mint_hub(store, _CLAIM)
    a = _paper(store, title="A", year=2001)
    b = _paper(store, title="B", year=2002)
    for p in (a, b):
        attach_evidence(store, hub_ref_id=hub, paper_ref_id=p, role="corroborates")

    evidence = derive_evidence(store, hub)

    assert evidence.originators == []
    assert {e.paper_ref_id for e in evidence.corroborators} == {a, b}
    assert not any(e.is_originator for e in evidence.corroborators)
    assert evidence.coverage_note == (
        "seniority undetermined: no intra-set citation edges held"
    )


# ── contradicts is a separate group ─────────────────────────────────────


def test_derive_evidence_keeps_contradictors_out_of_the_seniority_split(
    store: Any,
) -> None:
    hub = mint_hub(store, _CLAIM)
    originator = _paper(store, title="Originator", year=2001)
    corroborator = _paper(store, title="Corroborator", year=2004)
    contradictor = _paper(store, title="Contradictor", year=2003)
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=originator, role="establishes")
    attach_evidence(
        store, hub_ref_id=hub, paper_ref_id=corroborator, role="corroborates"
    )
    attach_evidence(
        store, hub_ref_id=hub, paper_ref_id=contradictor, role="contradicts"
    )
    _cites(store, src=corroborator, dst=originator)
    # A contradictor citing the originator must NOT promote it (or itself)
    # into the seniority split — contradicts is excluded from S entirely.
    _cites(store, src=contradictor, dst=originator)

    evidence = derive_evidence(store, hub)

    assert [e.paper_ref_id for e in evidence.originators] == [originator]
    assert [e.paper_ref_id for e in evidence.corroborators] == [corroborator]
    assert [e.paper_ref_id for e in evidence.contradictors] == [contradictor]
    assert evidence.contradictors[0].derived_role == "contradicts"
    assert not evidence.contradictors[0].is_originator


# ── regression: contradicts inverse-mirror + hub<->hub leak ────────────
#
# `contradicts` has a registered inverse `contradicted-by` (migration
# 0001); a naive `store.links_for(hub, direction='in',
# relation='contradicts')` read matches BOTH the real paper->hub edge AND
# (a) `(src=hub, relation='contradicted-by')` rows — the hub mirroring
# itself as its own contradictor — and (b) hub<->hub opposite-claim
# `contradicts` edges (`hub.apply_placement`'s `new_contradicts` branch),
# surfacing another *finding* as a "contradictor." `derive_evidence` reads
# via a direct `dst=hub` query requiring `src.kind='paper'` instead
# (taproot.md decision #2: endpoint kinds disambiguate) — these pin that.


def test_derive_evidence_excludes_hub_to_hub_contradicts(store: Any) -> None:
    hub_a = mint_hub(store, _CLAIM)
    hub_b = mint_hub(
        store,
        CanonicalClaim(
            sentence="Pd/C does NOT catalyze Suzuki coupling at RT.", scope={}
        ),
    )
    # Opposite-claim hub<->hub edge — exactly what
    # hub.apply_placement's new_contradicts branch writes.
    store.add_link(src_ref_id=hub_b, dst_ref_id=hub_a, relation="contradicts")

    paper = _paper(store, title="Real supporting paper", year=2001)
    attach_evidence(store, hub_ref_id=hub_a, paper_ref_id=paper, role="corroborates")

    evidence = derive_evidence(store, hub_a)

    assert evidence.contradictors == []  # hub_b is a finding, not a paper
    assert [e.paper_ref_id for e in evidence.corroborators] == [paper]


def test_derive_evidence_stray_contradicted_by_does_not_self_reference(
    store: Any,
) -> None:
    hub = mint_hub(store, _CLAIM)
    other_paper = _paper(store, title="Some other paper", year=2002)
    # The shape a naive links_for(direction='in', relation='contradicts')
    # inverse-mirror rewrite would match: a row stored src=hub,
    # relation='contradicted-by'.
    store.add_link(src_ref_id=hub, dst_ref_id=other_paper, relation="contradicted-by")

    evidence = derive_evidence(store, hub)

    assert evidence.contradictors == []
    assert all(e.paper_ref_id != hub for e in evidence.contradictors)


# ── regression: cites-graph edge cases ──────────────────────────────────


def test_derive_evidence_self_cite_is_not_an_originator(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    p = _paper(store, title="Self-citing paper", year=2001)
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=p, role="corroborates")
    # A ref-level self-loop (src_ref_id == dst_ref_id, both endpoints
    # NULL-position) is rejected by the `links_check` CHECK constraint —
    # the only way a paper "cites itself" can land in the DB is a
    # chunk-level self-loop (distinct ord positions on the same ref), so
    # give it a second chunk and cite chunk 0 from chunk 1.
    seed_chunk(store, ref_id=p, text="an earlier passage", ord=0)
    seed_chunk(store, ref_id=p, text="a later passage citing the above", ord=1)
    store.add_link(src_ref_id=p, dst_ref_id=p, src_pos=1, dst_pos=0, relation="cites")

    evidence = derive_evidence(store, hub)

    assert evidence.originators == []
    assert [e.paper_ref_id for e in evidence.corroborators] == [p]
    assert evidence.coverage_note == (
        "seniority undetermined: no intra-set citation edges held"
    )


def test_derive_evidence_cites_outside_supporter_set_do_not_count(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    supporter = _paper(store, title="Supporter", year=2001)
    outsider = _paper(store, title="Not attached to this hub", year=1999)
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=supporter, role="corroborates")
    _cites(store, src=supporter, dst=outsider)

    evidence = derive_evidence(store, hub)

    assert evidence.originators == []
    assert [e.paper_ref_id for e in evidence.corroborators] == [supporter]
    assert all(e.paper_ref_id != outsider for e in evidence.corroborators)


# ── per-paper facts: retraction status → integrity ──────────────────────


def test_derive_evidence_surfaces_retraction_status_as_integrity(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    retracted = _paper(store, title="Retracted paper", year=2001)
    clean = _paper(store, title="Clean paper", year=2002)
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=retracted, role="corroborates")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=clean, role="corroborates")
    _set_retraction_status(store, retracted, "retracted")

    evidence = derive_evidence(store, hub)

    by_id = {e.paper_ref_id: e for e in evidence.corroborators}
    assert by_id[retracted].integrity == "retracted"
    assert by_id[clean].integrity == "clean"


# ── edge meta: support / caveats ─────────────────────────────────────────


def test_derive_evidence_surfaces_edge_meta_support_and_caveats(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    with_meta = _paper(store, title="With meta", year=2001)
    without_meta = _paper(store, title="Without meta", year=2002)
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=with_meta,
        role="corroborates",
        meta={"support": "yes", "caveats": ["narrow scope"]},
    )
    attach_evidence(
        store, hub_ref_id=hub, paper_ref_id=without_meta, role="corroborates"
    )

    evidence = derive_evidence(store, hub)

    by_id = {e.paper_ref_id: e for e in evidence.corroborators}
    assert by_id[with_meta].support == "yes"
    assert by_id[with_meta].caveats == ["narrow scope"]
    assert by_id[without_meta].support is None
    assert by_id[without_meta].caveats == []


# ── EvidenceEdge.source_handle (Taproot slice R1 — refeye Claims) ───────


def test_derive_evidence_surfaces_source_handle_from_edge_meta(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    with_handle = _paper(store, title="Grounded", year=2001)
    without_handle = _paper(store, title="No grounding pointer", year=2002)
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=with_handle,
        role="corroborates",
        meta={"source_handle": "pc4242"},
    )
    attach_evidence(
        store, hub_ref_id=hub, paper_ref_id=without_handle, role="corroborates"
    )

    evidence = derive_evidence(store, hub)

    by_id = {e.paper_ref_id: e for e in evidence.corroborators}
    assert by_id[with_handle].source_handle == "pc4242"
    assert by_id[without_handle].source_handle is None


# ── guard: non-claim finding rejected ────────────────────────────────────


def test_derive_evidence_rejects_non_claim_finding(store: Any) -> None:
    review = seed_ref(store, title="acronym unexpanded", kind="finding")
    with store.pool.connection() as conn:
        store.add_tag(
            review, Tag.closed("TAPROOT", "review"), set_by="agent", conn=conn
        )
        conn.commit()

    with pytest.raises(BadInput):
        derive_evidence(store, review)

    # A non-finding ref is never a hub either.
    paper = _paper(store, title="Just a paper")
    with pytest.raises(BadInput):
        derive_evidence(store, paper)


# ── view='evidence' rendering ─────────────────────────────────────────────


def test_finding_view_evidence_renders_sections_and_originator_mark(store: Any) -> None:
    handler = _make_handler(store)
    hub = mint_hub(store, _CLAIM)
    originator = _paper(store, title="Originator paper", year=2001)
    corroborator = _paper(store, title="Corroborator paper", year=2005)
    contradictor = _paper(store, title="Contradictor paper", year=2006)
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=originator, role="corroborates")
    attach_evidence(
        store, hub_ref_id=hub, paper_ref_id=corroborator, role="corroborates"
    )
    attach_evidence(
        store, hub_ref_id=hub, paper_ref_id=contradictor, role="contradicts"
    )
    _cites(store, src=corroborator, dst=originator)

    resp = handler.get(id=hub, view="evidence")

    assert "originators (establishes)" in resp.body
    assert "corroborators" in resp.body
    assert "contradicts" in resp.body
    assert "★" in resp.body  # the originator mark
    assert "support outcomes are populated by chase (Phase 3)" in resp.body


def test_finding_view_evidence_empty_hub(store: Any) -> None:
    handler = _make_handler(store)
    hub = mint_hub(store, _CLAIM)

    resp = handler.get(id=hub, view="evidence")

    assert "no evidence edges yet for this claim hub" in resp.body


# ── derive_refines — claim→claim advisory neighbours (migration 0100) ────


def _sharper_hub(store: Any, sentence: str) -> int:
    return mint_hub(store, CanonicalClaim(sentence=sentence, scope={}))


def test_derive_refines_reads_both_directions(store: Any) -> None:
    original = mint_hub(store, _CLAIM)
    sharper = _sharper_hub(store, "Pd/C: Suzuki at 25 °C, K2CO3, >90% yield.")
    link_claims(store, from_hub_ref_id=sharper, to_hub_ref_id=original)

    # From the ORIGINAL's view: a sharper version refines it (inbound).
    orig_links = derive_refines(store, original)
    assert [cr.hub_ref_id for cr in orig_links.refined_by] == [sharper]
    assert orig_links.refines == []

    # From the SHARPER's view: it refines the original (outbound).
    sharp_links = derive_refines(store, sharper)
    assert [cr.hub_ref_id for cr in sharp_links.refines] == [original]
    assert sharp_links.refined_by == []
    # The neighbour sentence is carried for the ring's "see also" line.
    assert sharp_links.refines[0].sentence == _CLAIM.sentence


def test_derive_refines_empty_when_no_links(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    links = derive_refines(store, hub)
    assert links.refines == []
    assert links.refined_by == []


def test_derive_refines_drops_a_soft_deleted_neighbour(store: Any) -> None:
    original = mint_hub(store, _CLAIM)
    sharper = _sharper_hub(store, "Pd/C: Suzuki at 25 °C, K2CO3, >90% yield.")
    link_claims(store, from_hub_ref_id=sharper, to_hub_ref_id=original)

    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET deleted_at = now() WHERE ref_id = %s", (sharper,))
        conn.commit()

    # The original no longer surfaces the deleted sharper hub as a neighbour.
    assert derive_refines(store, original).refined_by == []


# ── grounding passages: per-chunk, not per-paper (corroborates-pc regression) ──


def test_grounding_surfaces_both_chunks_of_one_paper(store: Any) -> None:
    """A paper grounding a claim at TWO passages yields TWO grounding refs.
    Seniority still dedupes the paper to one corroborator, but grounding is
    per-chunk — the old per-paper dedup kept only the first passage's handle,
    silently dropping the second (the "we don't want to lose the 2
    corroborates" regression)."""
    hub = mint_hub(store, _CLAIM)
    paper = _paper(store, title="Two-passage supporter", year=2004)
    c0 = seed_chunk(store, ref_id=paper, text="First supporting passage.", ord=0)
    c1 = seed_chunk(store, ref_id=paper, text="Second supporting passage.", ord=1)
    pc0 = handle_registry.format_handle("paper", c0, chunk=True)
    pc1 = handle_registry.format_handle("paper", c1, chunk=True)
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": pc0},
    )
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": pc1},
    )

    ev = derive_evidence(store, hub)

    assert len(ev.corroborators) == 1  # one paper -> one seniority row
    assert {g.source_handle for g in ev.grounding} == {pc0, pc1}
    assert all(g.paper_ref_id == paper for g in ev.grounding)


def test_grounding_falls_back_to_src_chunk_id_without_source_handle(
    store: Any,
) -> None:
    """The draft-backfill arm pins the grounding chunk in ``links.src_chunk_id``
    and leaves ``meta.source_handle`` unset. derive_evidence must still surface
    the passage — formatting ``src_chunk_id`` as a ``pc<id>`` handle — so a
    backfill-origin claim isn't left with empty grounding."""
    hub = mint_hub(store, _CLAIM)
    paper = _paper(store, title="Backfill supporter", year=2004)
    c0 = seed_chunk(store, ref_id=paper, text="Ballistic transport passage.", ord=0)
    pc0 = handle_registry.format_handle("paper", c0, chunk=True)
    # No meta.source_handle — grounding lives ONLY in the edge's src_chunk_id.
    store.add_link(src_ref_id=paper, dst_ref_id=hub, relation="corroborates", src_pos=0)

    ev = derive_evidence(store, hub)

    assert [g.source_handle for g in ev.grounding] == [pc0]
    assert ev.corroborators[0].paper_ref_id == paper


def test_grounding_relation_distinguishes_support_from_contradiction(
    store: Any,
) -> None:
    """One paper that BOTH corroborates (at chunk A) and contradicts (at chunk
    B) the same claim yields two grounding refs carrying their RAW relation —
    so the web layer attributes B to the contradictor role, not support
    (keying attribution by paper alone would relabel it)."""
    hub = mint_hub(store, _CLAIM)
    paper = _paper(store, title="Mixed-evidence paper", year=2004)
    c_a = seed_chunk(store, ref_id=paper, text="Supports the claim.", ord=0)
    c_b = seed_chunk(store, ref_id=paper, text="Contradicts the claim.", ord=1)
    pc_a = handle_registry.format_handle("paper", c_a, chunk=True)
    pc_b = handle_registry.format_handle("paper", c_b, chunk=True)
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": pc_a},
    )
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="contradicts",
        meta={"source_handle": pc_b},
    )

    ev = derive_evidence(store, hub)

    assert {g.source_handle: g.relation for g in ev.grounding} == {
        pc_a: "corroborates",
        pc_b: "contradicts",
    }

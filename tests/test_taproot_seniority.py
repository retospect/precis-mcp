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
from precis.handlers._finding_evidence import _independent_supporter_counts
from precis.handlers.finding import FindingHandler
from precis.store.types import Tag
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, link_claims, mint_hub
from precis.taproot.seniority import (
    EvidenceEdge,
    conjunct_atoms_bulk,
    derive_conjuncts,
    derive_evidence,
    derive_refines,
)
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


def _paper_with_authors(
    store: Any, *, title: str, authors: list[dict[str, Any]], year: int | None = None
) -> int:
    slug = f"sen{next(_slug_counter)}"
    ref = store.insert_ref(
        kind="paper", slug=slug, title=title, year=year, meta={}, authors=authors
    )
    return ref.id


def _edge(pid: int, *, is_originator: bool = False) -> EvidenceEdge:
    """A bare-minimum :class:`EvidenceEdge` for exercising
    :func:`_independent_supporter_counts` directly, without a hub."""
    return EvidenceEdge(
        paper_ref_id=pid,
        title=f"paper {pid}",
        year=None,
        derived_role="establishes" if is_originator else "corroborates",
        is_originator=is_originator,
        support=None,
        caveats=[],
        integrity="clean",
    )


def _cites(store: Any, *, src: int, dst: int) -> None:
    """Direct write: `cites` is not a taproot role, so this bypasses hub.py."""
    store.add_link(src_ref_id=src, dst_ref_id=dst, relation="cites")


def _patent(
    store: Any,
    *,
    title: str,
    year: int | None = None,
    publication_date: str | None = None,
) -> int:
    """A ``patent`` ref with an optional stored ``year`` and an optional
    ``meta.publication_date`` — mirrors the real ingest shape where a
    patent's date lives in meta (and, post-fix, also in ``refs.year``)."""
    slug = f"senpat{next(_slug_counter)}"
    meta: dict[str, Any] = {}
    if publication_date is not None:
        meta["publication_date"] = publication_date
    ref = store.insert_ref(kind="patent", slug=slug, title=title, year=year, meta=meta)
    return ref.id


def _patent_with_meta(
    store: Any, *, slug: str, title: str, meta: dict[str, Any], year: int | None = None
) -> int:
    """A ``patent`` ref carrying arbitrary ``meta`` (applicants/doc_number/
    kind_code/family_id) — for the patent-bibliography-line and
    family-collapse rendering tests, which need fields :func:`_patent`
    doesn't set."""
    ref = store.insert_ref(kind="patent", slug=slug, title=title, year=year, meta=meta)
    return ref.id


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


# ── patent-evidence-parity.md: patent refs.year fallback to meta ────────
#
# Patent ingest didn't populate ``refs.year`` before the fix in
# ``_patent_ingest.py`` — every already-ingested patent has ``refs.year
# IS NULL`` with the real date only in ``meta.publication_date``.
# ``_fetch_paper_facts`` falls back to that meta date so those patents
# still interleave correctly against papers with a real ``refs.year``.


def test_derive_evidence_interleaves_patent_meta_year_with_paper_years(
    store: Any,
) -> None:
    hub = mint_hub(store, _CLAIM)
    early_paper = _paper(store, title="Early paper", year=2000)
    # NULL refs.year, real date only in meta — the un-backfilled shape.
    mid_patent = _patent(
        store, title="Mid patent", year=None, publication_date="2005-06-01"
    )
    late_paper = _paper(store, title="Late paper", year=2010)
    for p in (early_paper, mid_patent, late_paper):
        attach_evidence(store, hub_ref_id=hub, paper_ref_id=p, role="corroborates")
    # citer isn't itself in S, but forces originator derivation.
    citer = _paper(store, title="Citer", year=2015)
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=citer, role="corroborates")
    _cites(store, src=citer, dst=early_paper)
    _cites(store, src=citer, dst=mid_patent)
    _cites(store, src=citer, dst=late_paper)

    evidence = derive_evidence(store, hub)

    # Earliest-first, interleaved by real date: paper 2000, patent 2005
    # (via meta fallback), paper 2010.
    assert [e.paper_ref_id for e in evidence.originators] == [
        early_paper,
        mid_patent,
        late_paper,
    ]


def test_derive_evidence_sorts_last_with_neither_year_nor_meta_date(
    store: Any,
) -> None:
    hub = mint_hub(store, _CLAIM)
    dated = _paper(store, title="Dated", year=2003)
    undated_patent = _patent(store, title="Undated patent", year=None)
    for p in (dated, undated_patent):
        attach_evidence(store, hub_ref_id=hub, paper_ref_id=p, role="corroborates")

    evidence = derive_evidence(store, hub)

    # NULLS LAST semantics preserved when neither refs.year nor a
    # parseable meta.publication_date exists.
    assert [e.paper_ref_id for e in evidence.corroborators] == [
        dated,
        undated_patent,
    ]


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


def test_finding_view_evidence_hypothesis_shows_motivated_by_not_supporters(
    store: Any,
) -> None:
    """A hypothesis hub has zero evidence edges *by definition* (the mint
    gates reject one that arrives with grounding passages) —
    indistinguishable, on edge count alone, from an orphan bibliography-stub
    hub the admissibility test exists to catch. Its ``motivated-by`` edges
    render instead, under their own heading, explicitly not support."""
    handler = _make_handler(store)
    pa1 = _paper(store, title="First motivating paper")
    ch1 = seed_chunk(store, ref_id=pa1, text="A motivating passage.")
    pa2 = _paper(store, title="Second motivating paper")

    resp = handler.put(
        title="DFT predicts an analogous transfer effect in a new system.",
        hypothesis=True,
        motivation="Both systems share a mechanism; the transfer is untested.",
        testable_by="an experiment discriminating the two candidate mechanisms",
        motivated_by=[f"pc{ch1}", f"pa{pa2}"],
        llm_models=["test-model"],
    )
    hub = int(resp.body.split("fi", 1)[1].split()[0])

    ev = handler.get(id=hub, view="evidence")

    assert "no supporters" in ev.body and "hypothesis" in ev.body
    assert "## motivated by" in ev.body
    assert "First motivating paper" in ev.body and f"pc{ch1}" in ev.body
    assert "Second motivating paper" in ev.body and f"pa{pa2}" in ev.body
    # Never rendered as if it were the ordinary role sections.
    assert "originators (establishes)" not in ev.body
    assert "corroborators" not in ev.body


def test_finding_view_evidence_marks_only_zero_block_papers_unfetched(
    store: Any,
) -> None:
    """gr180155's per-hub analogue of the draft citations view's to-fetch
    worklist: an evidence paper with no body chunks renders ``(unfetched)``;
    one with a body chunk does not."""
    handler = _make_handler(store)
    hub = mint_hub(store, _CLAIM)
    fetched = _paper(store, title="Fetched corroborator", year=2001)
    seed_chunk(store, ref_id=fetched, text="a grounding passage")
    unfetched = _paper(store, title="Unfetched corroborator", year=2002)
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=fetched, role="corroborates")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=unfetched, role="corroborates")

    resp = handler.get(id=hub, view="evidence")
    body = resp.body

    fetched_line = next(
        line for line in body.splitlines() if "Fetched corroborator" in line
    )
    unfetched_line = next(
        line for line in body.splitlines() if "Unfetched corroborator" in line
    )
    assert "(unfetched)" not in fetched_line
    assert "(unfetched)" in unfetched_line


# ── independent-supporter count (derived, read-only) ────────────────────


def test_independent_supporter_counts_zero_when_no_supporters(store: Any) -> None:
    assert _independent_supporter_counts([], {}) == (0, 0)


def test_independent_supporter_counts_single_supporter(store: Any) -> None:
    p = _paper_with_authors(store, title="Solo", authors=[{"name": "A. One"}])
    refs_by_id = store.fetch_refs_by_ids([p])

    assert _independent_supporter_counts([_edge(p)], refs_by_id) == (1, 1)


def test_independent_supporter_counts_collapses_shared_author(store: Any) -> None:
    """Two supporting papers sharing an author (case/whitespace-insensitive
    match) count as one independent supporter, even though both papers are
    still counted in the paper total."""
    p1 = _paper_with_authors(
        store, title="Paper one", authors=[{"name": "Jane Doe"}, {"name": "Bob Roe"}]
    )
    p2 = _paper_with_authors(
        store, title="Paper two", authors=[{"name": "  jane doe  "}, {"name": "Cy Fu"}]
    )
    refs_by_id = store.fetch_refs_by_ids([p1, p2])

    assert _independent_supporter_counts([_edge(p1), _edge(p2)], refs_by_id) == (1, 2)


def test_independent_supporter_counts_distinct_authors_stay_separate(
    store: Any,
) -> None:
    p1 = _paper_with_authors(store, title="Paper one", authors=[{"name": "Jane Doe"}])
    p2 = _paper_with_authors(store, title="Paper two", authors=[{"name": "Cy Fu"}])
    refs_by_id = store.fetch_refs_by_ids([p1, p2])

    assert _independent_supporter_counts([_edge(p1), _edge(p2)], refs_by_id) == (2, 2)


def test_independent_supporter_counts_transitive_chain_collapses_to_one(
    store: Any,
) -> None:
    """A shares an author with B, B shares a *different* author with C:
    transitive closure collapses all three into one supporter."""
    p1 = _paper_with_authors(
        store, title="Paper one", authors=[{"name": "Jane Doe"}, {"name": "Ann Alpha"}]
    )
    p2 = _paper_with_authors(
        store, title="Paper two", authors=[{"name": "Ann Alpha"}, {"name": "Bea Beta"}]
    )
    p3 = _paper_with_authors(store, title="Paper three", authors=[{"name": "Bea Beta"}])
    refs_by_id = store.fetch_refs_by_ids([p1, p2, p3])

    assert _independent_supporter_counts(
        [_edge(p1), _edge(p2), _edge(p3)], refs_by_id
    ) == (1, 3)


def test_independent_supporter_counts_no_authors_stays_singleton(store: Any) -> None:
    """A paper with no resolvable authors is its own group — sparse
    author data never collapses papers into each other."""
    p1 = _paper(store, title="No-author paper A")
    p2 = _paper(store, title="No-author paper B")
    refs_by_id = store.fetch_refs_by_ids([p1, p2])

    assert _independent_supporter_counts([_edge(p1), _edge(p2)], refs_by_id) == (2, 2)


def test_finding_view_evidence_renders_independent_supporters_line(
    store: Any,
) -> None:
    handler = _make_handler(store)
    hub = mint_hub(store, _CLAIM)
    a = _paper_with_authors(
        store, title="Support A", authors=[{"name": "Jane Doe"}], year=2001
    )
    b = _paper_with_authors(
        store, title="Support B", authors=[{"name": "jane doe"}], year=2002
    )
    c = _paper_with_authors(
        store, title="Support C", authors=[{"name": "Cy Fu"}], year=2003
    )
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=a, role="corroborates")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=b, role="corroborates")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=c, role="corroborates")

    resp = handler.get(id=hub, view="evidence")

    assert "independent supporters: 2 (3 papers)" in resp.body


def test_finding_view_evidence_independent_supporters_zero_with_only_contradictors(
    store: Any,
) -> None:
    handler = _make_handler(store)
    hub = mint_hub(store, _CLAIM)
    contradictor = _paper(store, title="Contradictor only", year=2001)
    attach_evidence(
        store, hub_ref_id=hub, paper_ref_id=contradictor, role="contradicts"
    )

    resp = handler.get(id=hub, view="evidence")

    assert "independent supporters: 0 (0 papers)" in resp.body


# ── view='evidence' patent rendering (patent-evidence-parity.md Phase 3) ──


def test_finding_view_evidence_renders_patent_bibliography_line(store: Any) -> None:
    """A patent evidence edge renders its full bibliography-style citation
    (applicant, title, publication number + kind code, year), not a bare
    title."""
    handler = _make_handler(store)
    hub = mint_hub(store, _CLAIM)
    patent = _patent_with_meta(
        store,
        slug="ep1111111b1",
        title="A widget with improved catalysis",
        year=2020,
        meta={
            "applicants": [{"name": "Acme Corp"}],
            "doc_number": "1111111",
            "kind_code": "b1",
        },
    )
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=patent, role="corroborates")

    resp = handler.get(id=hub, view="evidence")

    assert "Acme Corp. A widget with improved catalysis. 1111111B1, 2020." in resp.body


def test_finding_view_evidence_collapses_same_family_patents(store: Any) -> None:
    """Two evidence edges from sibling members of the same patent family
    collapse to one row, keyed to the family's deterministic (earliest-
    published) representative; the non-representative sibling's number
    still surfaces as a "passage in" note."""
    handler = _make_handler(store)
    hub = mint_hub(store, _CLAIM)
    rep = _patent_with_meta(
        store,
        slug="ep2222220a1",
        title="Representative family member",
        meta={
            "family_id": "fam-x",
            "publication_date": "2018-01-01",
            "doc_number": "2222220",
            "kind_code": "a1",
        },
    )
    sibling = _patent_with_meta(
        store,
        slug="ep2222221b1",
        title="Later sibling bearing the grounded passage",
        meta={
            "family_id": "fam-x",
            "publication_date": "2020-01-01",
            "doc_number": "2222221",
            "kind_code": "b1",
        },
    )
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=rep, role="corroborates")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=sibling, role="corroborates")

    resp = handler.get(id=hub, view="evidence")

    assert resp.body.count("2222220A1") == 1
    assert "Later sibling bearing the grounded passage" not in resp.body
    assert "passage in EP2222221B1" in resp.body


def test_finding_view_evidence_collapses_three_sibling_family_patents(
    store: Any,
) -> None:
    """With THREE grounded siblings of one family on a hub, every non-
    representative sibling's number surfaces in the collapsed row's note
    -- not just the first one (a prior version dropped the rest)."""
    handler = _make_handler(store)
    hub = mint_hub(store, _CLAIM)
    rep = _patent_with_meta(
        store,
        slug="ep3333330a1",
        title="Representative family member",
        meta={
            "family_id": "fam-y",
            "publication_date": "2018-01-01",
            "doc_number": "3333330",
            "kind_code": "a1",
        },
    )
    sibling_b = _patent_with_meta(
        store,
        slug="ep3333331b1",
        title="Second sibling bearing a grounded passage",
        meta={
            "family_id": "fam-y",
            "publication_date": "2019-01-01",
            "doc_number": "3333331",
            "kind_code": "b1",
        },
    )
    sibling_c = _patent_with_meta(
        store,
        slug="ep3333332b1",
        title="Third sibling bearing a grounded passage",
        meta={
            "family_id": "fam-y",
            "publication_date": "2020-01-01",
            "doc_number": "3333332",
            "kind_code": "b1",
        },
    )
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=rep, role="corroborates")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=sibling_b, role="corroborates")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=sibling_c, role="corroborates")

    resp = handler.get(id=hub, view="evidence")

    assert resp.body.count("3333330A1") == 1
    assert "Second sibling bearing a grounded passage" not in resp.body
    assert "Third sibling bearing a grounded passage" not in resp.body
    assert "passage in EP3333331B1, EP3333332B1" in resp.body


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


# ── derive_conjuncts — atomic-claims claim→claim advisory (migration 0126) ──


def test_derive_conjuncts_reads_both_directions(store: Any) -> None:
    compound = mint_hub(store, _CLAIM)
    atom_a = _sharper_hub(store, "Atom A: Pd/C alone catalyzes the coupling.")
    atom_b = _sharper_hub(store, "Atom B: a mild base is required.")
    link_claims(
        store, from_hub_ref_id=atom_a, to_hub_ref_id=compound, relation="conjunct-of"
    )
    link_claims(
        store, from_hub_ref_id=atom_b, to_hub_ref_id=compound, relation="conjunct-of"
    )

    # From the COMPOUND's view: its atoms are inbound.
    compound_links = derive_conjuncts(store, compound)
    assert {cr.hub_ref_id for cr in compound_links.refined_by} == {atom_a, atom_b}
    assert compound_links.refines == []

    # From an ATOM's view: the compound it belongs to is outbound.
    atom_links = derive_conjuncts(store, atom_a)
    assert [cr.hub_ref_id for cr in atom_links.refines] == [compound]
    assert atom_links.refined_by == []
    assert atom_links.refines[0].sentence == _CLAIM.sentence


def test_derive_conjuncts_empty_for_a_plain_atomic_hub(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    links = derive_conjuncts(store, hub)
    assert links.refines == []
    assert links.refined_by == []


def test_derive_conjuncts_drops_a_soft_deleted_atom(store: Any) -> None:
    compound = mint_hub(store, _CLAIM)
    atom = _sharper_hub(store, "Atom: a mild base is required.")
    link_claims(
        store, from_hub_ref_id=atom, to_hub_ref_id=compound, relation="conjunct-of"
    )

    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET deleted_at = now() WHERE ref_id = %s", (atom,))
        conn.commit()

    # A deleted atom no longer surfaces as one of the compound's conjuncts.
    assert derive_conjuncts(store, compound).refined_by == []


def test_conjunct_atoms_bulk_maps_compounds_to_their_atoms(store: Any) -> None:
    compound = mint_hub(store, _CLAIM)
    atom_a = _sharper_hub(store, "Atom A: Pd/C alone catalyzes the coupling.")
    atom_b = _sharper_hub(store, "Atom B: a mild base is required.")
    link_claims(
        store, from_hub_ref_id=atom_a, to_hub_ref_id=compound, relation="conjunct-of"
    )
    link_claims(
        store, from_hub_ref_id=atom_b, to_hub_ref_id=compound, relation="conjunct-of"
    )
    plain_hub = _sharper_hub(store, "A plain atomic hub with no conjuncts.")

    result = conjunct_atoms_bulk(store, [compound, plain_hub])

    assert result[compound] == sorted([atom_a, atom_b])
    assert result[plain_hub] == []


def test_conjunct_atoms_bulk_empty_input_returns_empty_map(store: Any) -> None:
    assert conjunct_atoms_bulk(store, []) == {}


# ── regression: a compound hub derives NO originators/corroborators ────
#
# A compound's only inbound edges are `conjunct-of` from its atom findings.
# `_fetch_evidence_rows` only matches `_ALL_ROLES` (establishes/corroborates/
# contradicts) from an `_EVIDENCE_SRC_KINDS` (paper/patent/edgar) source —
# `conjunct-of` isn't a matched role AND a finding isn't a matched source
# kind, so a compound is doubly excluded even though its atoms individually
# carry real evidence. Pins step 5's "compounds derive no originators" flag
# (docs/backlog/taproot-atomic-claims.md) — no code change was needed for
# this, the existing guard already does it; this test just proves it.


def test_derive_evidence_on_compound_hub_yields_no_evidence(store: Any) -> None:
    compound = mint_hub(store, _CLAIM)
    atom = _sharper_hub(store, "Atom: a mild base is required.")
    link_claims(
        store, from_hub_ref_id=atom, to_hub_ref_id=compound, relation="conjunct-of"
    )
    supporter = _paper(store, title="Supports the atom, not the compound", year=2001)
    attach_evidence(store, hub_ref_id=atom, paper_ref_id=supporter, role="corroborates")

    evidence = derive_evidence(store, compound)

    assert evidence.originators == []
    assert evidence.corroborators == []
    assert evidence.contradictors == []
    assert evidence.coverage_note is None
    assert evidence.grounding == []


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

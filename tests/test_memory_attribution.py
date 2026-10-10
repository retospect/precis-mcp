"""Memory attribution gate, pure part (no DB).

``ungrounded_cited_numbers`` with its two DB seams (``_resolve_cite``,
``_fetch_evidence``) replaced by in-memory tables.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, cast

import pytest

from precis.handlers import _attribution as attr
from precis.handlers._attribution import (
    UngroundedNumber,
    gate_mode,
    grounding_failures,
    ungrounded_cited_numbers,
)

if TYPE_CHECKING:
    from precis.store.store import Store

#: cite as written -> (ref_id, chunk_ord)
_TARGETS = {
    "websearch:170350": (170350, None),
    "pc995663": (995663, 4),
    "pa5835": (5835, None),
    "pa12": (12, None),
    "pa13": (13, None),
    "pc111": (101, None),
    "pc222": (102, None),
    "pa555": (5, None),
    "pa666": (6, None),
}


@pytest.fixture()
def world(monkeypatch):
    """Evidence text per ``(ref_id, chunk_ord)``; set by each test."""
    evidence: dict[tuple[int, int | None], str] = {}

    def resolve(store, span, cache):
        return _TARGETS.get(span.cite, (None, None))

    def fetch(store, targets):
        return {t: attr.evidence_from_text(evidence.get(t, "")) for t in targets}

    monkeypatch.setattr(attr, "_resolve_cite", resolve)
    monkeypatch.setattr(attr, "_fetch_evidence", fetch)
    return evidence


def _check(body: str) -> list[UngroundedNumber]:
    # The resolver and evidence fetch are monkeypatched, so no store is read.
    return ungrounded_cited_numbers(cast("Store", None), body)


def test_grounded_number_passes(world):
    world[(170350, None)] = "The crossover scale is about 10 nm in stiff solids."
    assert _check("ell_c is ~10 nm per websearch:170350.") == []


def test_absent_number_fails(world):
    world[(170350, None)] = "No numeric scale is given here."
    out = _check("ell_c is ~10 nm for stiff solids per websearch:170350.")
    assert out == [UngroundedNumber("10 nm", "websearch:170350", 170350)]
    assert out[0].advisory() == (
        'ungrounded: "10 nm" near websearch:170350 — not in the cited text; '
        'drop the attribution or write "(my estimate)"'
    )


def test_decimal_prefix_tolerance(world):
    world[(5835, None)] = "measured 1.33 eV"
    assert _check("gap of 1.3 eV [pa5835]") == []
    # ...but not the reverse direction or a different decimal.
    assert [m.token for m in _check("gap of 1.4 eV [pa5835]")] == ["1.4 eV"]


def test_integer_is_exact(world):
    world[(5835, None)] = "temperature 1900 K"
    assert [m.token for m in _check("held at 19 K, see pa5835")] == ["19 K"]
    assert _check("held at 1900 K, see pa5835") == []


def test_exemption_phrase(world):
    world[(170350, None)] = "nothing"
    for phrase in (
        "(my estimate)",
        "my own estimate",
        "(est.)",
        "(estimate)",
        "a rough guess",
    ):
        body = f"scale ~10 nm {phrase} next to websearch:170350."
        assert _check(body) == [], phrase
    # Phrase too far after the number does not exempt.
    far = "scale ~10 nm " + "x" * 80 + " my estimate websearch:170350."
    assert [m.token for m in _check(far)] == ["10 nm"]


def test_number_inside_handle_ignored(world):
    # "pa12" / "170350" digits are not unit-bearing numbers and never fire.
    world[(12, None)] = ""
    assert _check("see pa12 and websearch:170350 for details") == []


@pytest.mark.parametrize(
    "body,cite",
    [
        ("The value is 7 nm, see pc995663.", "pc995663"),
        ("The value is 7 nm [pa12].", "pa12"),
        ("The value is 7 nm per websearch:170350.", "websearch:170350"),
    ],
)
def test_cite_forms_detected(world, body, cite):
    for key in ((170350, None), (995663, 4), (12, None)):
        world[key] = "nothing relevant"
    out = _check(body)
    assert [m.cite for m in out] == [cite]
    assert out[0].ref_id is not None


def test_nearest_cite_wins(world):
    world[(12, None)] = "reports 5 nm"
    world[(13, None)] = "nothing relevant"
    # 5 nm sits right next to pa12; pa13 is further away.
    assert _check("Layers of 5 nm [pa12] were compared with other work [pa13].") == []
    out = _check("Earlier work [pa12] found other things; layers of 5 nm [pa13].")
    assert [(m.token, m.cite) for m in out] == [("5 nm", "pa13")]


def test_other_sentence_cite_does_not_apply(world):
    world[(12, None)] = "nothing"
    assert _check("The gap is 5 nm. Separately, see [pa12].") == []


def test_chunk_level_cite_uses_chunk_evidence(world):
    world[(995663, 4)] = "film thickness 7 nm"
    assert _check("film is 7 nm thick (pc995663)") == []
    assert [m.token for m in _check("film is 8 nm thick (pc995663)")] == ["8 nm"]


def test_unresolvable_cite_reported_not_a_failure(world):
    out = _check("value 3 nm per websearch:999999999")
    assert out == [UngroundedNumber("3 nm", "websearch:999999999", None)]
    assert grounding_failures(out) == []


def test_unresolvable_bare_paper_key_is_dropped(world):
    # "abc12" is shaped like a paper key but names nothing: not a citation.
    assert _check("about 3 nm in the abc12 regime") == []


def test_years_counts_and_bare_numbers_never_fire(world):
    world[(12, None)] = "nothing"
    body = "In 2021, 40 samples and 3 trials (pa12) followed eq. 5 and r^2 fits."
    assert _check(body) == []


def test_no_cite_no_check(world):
    assert _check("about 10 nm, trust me") == []
    assert _check("") == []


def test_gate_mode(monkeypatch):
    monkeypatch.delenv(attr.GATE_ENV, raising=False)
    assert gate_mode() == "warn"
    monkeypatch.setenv(attr.GATE_ENV, "reject")
    assert gate_mode() == "reject"
    monkeypatch.setenv(attr.GATE_ENV, "bogus")
    assert gate_mode() == "warn"


# --- review fixes -----------------------------------------------------------


def test_sentence_breaks():
    import re

    def segs(t):
        return [x for x in re.split(attr._SENTENCE_BREAK_RE, t) if x]

    assert len(segs("See pc111. 10 nm is typical.")) == 2
    assert len(segs("line one 5 nm\nline two [pa12]")) == 2
    assert len(segs("- a 5 nm\n- b [pa12]")) == 2
    assert len(segs("p1\n\np2")) == 2
    assert len(segs("Typical, e.g. 10 nm, per pa12.")) == 1
    assert len(segs("As in Fig. 3 the gap is 5 nm.")) == 1
    assert len(segs("Smith et al. found 5 nm.")) == 1
    assert len(segs("so i.e. 5 nm holds")) == 1


def test_digit_sentence_opener_separates_cite(world):
    world[(12, None)] = "nothing"
    assert _check("See [pa12]. 10 nm is typical.") == []


def test_newline_separates_bullets(world):
    world[(12, None)] = "nothing"
    assert _check("- gap 5 nm\n- other work [pa12]") == []


@pytest.mark.parametrize(
    "body",
    [
        "gap of 7 nm (pc111, pc222)",
        "gap of 7 nm [pc111][pc222]",
        "gap of 7 nm pa555 / pa666",
    ],
)
def test_cite_cluster_pools_evidence(world, body):
    for k in (101, 5):
        world[(k, None)] = "nothing"
    world[(102, None)] = "found 7 nm"
    world[(6, None)] = "found 7 nm"
    assert _check(body) == []
    world[(102, None)] = world[(6, None)] = "found 9 nm"
    assert [m.token for m in _check(body)] == ["7 nm"]


def test_distant_cites_do_not_cluster(world):
    world[(5, None)] = "nothing"
    world[(6, None)] = "found 7 nm"
    body = "gap of 7 nm per pa555 and then a long aside here, also pa666"
    assert [m.token for m in _check(body)] == ["7 nm"]


def test_unit_aware_comparison(world):
    world[(12, None)] = "film of 10 nm and 500 K"
    assert _check("temp 10 K [pa12]") != []  # 10 exists, but as nm
    assert _check("thickness 10 nm [pa12]") == []
    assert _check("thickness 10 um [pa12]") != []
    world[(12, None)] = "film of 10 µm"
    assert _check("thickness 10 um [pa12]") == []


def test_bare_digit_run_never_grounds_a_unit_claim(world):
    # The first prod dogfood write (2026-10-09) passed because the cited
    # websearch body carried "10." as a DOI prefix and nothing in nm.
    world[(170350, None)] = (
        "Surface stress and elasticity. doi 10.1098/rspa.2020.0477; "
        "results for 10 samples, see refs 1-10."
    )
    assert [m.token for m in _check("ell_c is ~10 nm per websearch:170350.")] == [
        "10 nm"
    ]
    world[(12, None)] = "Title: results for 10 samples"
    assert _check("thickness 10 nm [pa12]") != []


def test_range_leaves_its_neighbours_alone():
    from precis.handlers._attribution import _quantities

    toks = [t for t, _s, _e in _quantities("5 nm before, 10-20 nm, then 30 nm")]
    assert toks == ["5 nm", "10 nm", "20 nm", "30 nm"]


def test_ranges_yield_both_endpoints_no_negative():
    from precis.handlers._attribution import _quantities

    for text in ("10-20 nm", "10–20 nm", "10 to 20 nm"):
        assert [t for t, _s, _e in _quantities(text)] == ["10 nm", "20 nm"], text
    assert [t for t, _s, _e in _quantities("5-10%")] == ["5%", "10%"]


def test_range_claim_checked_against_both_ends(world):
    world[(12, None)] = "spans 10 nm and 20 nm"
    assert _check("layers of 10-20 nm [pa12]") == []
    world[(12, None)] = "spans 10 nm only"
    assert [m.token for m in _check("layers of 10-20 nm [pa12]")] == ["20 nm"]


def test_empty_evidence_is_nothing_to_check(world):
    world[(12, None)] = ""
    assert _check("gap of 7 nm [pa12]") == []


def test_evidence_union_and_usable():
    a = attr.evidence_from_text("5 nm")
    b = attr.Evidence()
    assert (a | b).units == a.units
    assert not b.usable and a.usable


def test_exemption_before_number(world):
    world[(12, None)] = "nothing"
    assert _check("my estimate: ~10 nm per [pa12].") == []
    far = "my estimate " + "x" * 80 + " ~10 nm per [pa12]."
    assert [m.token for m in _check(far)] == ["10 nm"]


def test_evidence_cache_is_lru_bounded(monkeypatch):
    monkeypatch.setattr(attr, "_EVIDENCE_CACHE_MAX", 3)
    c = attr.AttributionCache()
    ev = attr.Evidence()
    c.put_evidence({(i, None): ev for i in range(3)})
    assert c.get_evidence((0, None)) is ev  # touch 0 -> 1 is now oldest
    c.put_evidence({(9, None): ev})
    assert len(c.evidence) == 3
    assert c.get_evidence((1, None)) is None
    assert c.get_evidence((0, None)) is ev


def test_reword_accessor_cached():
    assert attr._reword() is attr._reword()


def test_gloss_pins_number_to_its_citation(world):
    """Prod sample 2026-10-10 (me6240): 60 kPa sat in the first cite's gloss
    but was nearer to the following cite, which does not carry it."""
    world[(12, None)] = "nozzle at 60 kPa internal pressure, 20 nm line width"
    world[(13, None)] = "bipartite graphs"
    body = (
        "I notice [pa12] (dead proboscides as nozzles — 20 nm line width, 60 kPa "
        "internal pressure; lineage with necrobotics) and [pa13] (ribosomal graphs)."
    )
    assert _check(body) == []
    world[(12, None)] = "nothing numeric"
    assert [(m.token, m.cite) for m in _check(body)] == [
        ("20 nm", "pa12"),
        ("60 kPa", "pa12"),
    ]


def test_cite_inside_gloss_nearer_than_opener_wins(world):
    world[(12, None)] = "nothing"
    world[(13, None)] = "reports 40%"
    assert (
        _check("I notice [pa12] (a graph model; see [pa13] which reports 40%).") == []
    )


def test_decade_is_not_seconds(world):
    world[(12, None)] = "nothing"
    assert _check("any 2010s rotaxane-pump motif [pa12] re-expressed") == []
    assert [m.token for m in _check("ran for 2010 s [pa12]")] == ["2010 s"]


def test_chunk_cite_falls_back_to_the_document(world):
    world[(995663, 4)] = "film thickness 7 nm"
    world[(995663, None)] = "the abstract gives 8 nm"
    assert _check("film is 8 nm thick (pc995663)") == []
    assert [m.token for m in _check("film is 9 nm thick (pc995663)")] == ["9 nm"]


def test_micro_units_unverifiable_against_numerics(world, monkeypatch):
    """PDF text writes micro as ``$\\mu$ m``; a paper's numerics never hold it."""

    def fetch(store, targets):
        ev = attr.evidence_from_text("375 nm ; 29.7 GHz", has_text=True)
        return {t: dataclasses.replace(ev, numerics_only=True) for t in targets}

    monkeypatch.setattr(attr, "_fetch_evidence", fetch)
    assert _check("mobility 10,000 at 2 µm channel [pa12]") == []
    assert _check("mobility at 2 μm channel [pa12]") == []  # Greek mu
    assert [m.token for m in _check("mobility at 3 nm channel [pa12]")] == ["3 nm"]

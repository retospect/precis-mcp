"""Tests for the generated subject axes (`precis.taxonomy.subjects` and its
composition/periodic/termination/elements dependencies).

Plain, DB-free. Covers `taxonomy-bootstrap.md` AC6 and the parser traps
called out in the module docstrings: ``ase.formula.Formula`` raising on
every real-world shape, the unvalidated-scanner risk, and the axis-on-edge
property that keeps widening from leaking across axes.
"""

from __future__ import annotations

from precis.taxonomy.composition import parse_composition
from precis.taxonomy.config import load_campaign
from precis.taxonomy.elements import ELEMENTS, element
from precis.taxonomy.subjects import memberships, subject_key, tag_memberships
from precis.taxonomy.termination import cubic_equivalents

_CONFIG = load_campaign("norr-her-meta")


def _parents(label: str, axis: str) -> set[str]:
    return {e.parent for e in memberships(label, _CONFIG) if e.axis == axis}


def test_pdcop_is_a_compound_not_an_alloy() -> None:
    """AC6's own text says `PdCoP` -> ternary-alloy; that is wrong (spec is
    being corrected). P is not a metal (`elements.element("P").is_metal` is
    False, series="reactive-nonmetal"), so "every element is a metal" fails
    and the honest class is ternary-compound. Assert the honest class."""
    comp = parse_composition("PdCoP")
    assert comp is not None
    assert {sym for sym, _ in comp.elements} == {"Pd", "Co", "P"}
    assert _parents("PdCoP", "composition") == {"pd", "co", "p", "ternary-compound"}


def test_pt3ni_is_a_binary_alloy() -> None:
    """Both Pt and Ni are metals — this pair is what makes the
    alloy/compound split testable (PdCoP alone only proves the compound
    side)."""
    assert _parents("Pt3Ni", "composition") == {"pt", "ni", "binary-alloy"}


def test_mo2c_and_fractional_ceo2() -> None:
    assert _parents("Mo2C", "composition") == {"mo", "c", "binary-compound"}

    comp = parse_composition("Ce0.5Zr0.5O2")
    assert comp is not None
    assert comp.elements == (("Ce", 0.5), ("Zr", 0.5), ("O", 2.0))
    assert _parents("Ce0.5Zr0.5O2", "composition") == {
        "ce",
        "zr",
        "o",
        "ternary-compound",
    }


def test_class_suffix_support_and_decoration() -> None:
    ldh = parse_composition("NiFe-LDH")
    assert ldh is not None
    assert {sym for sym, _ in ldh.elements} == {"Ni", "Fe"}
    assert ldh.class_suffix == "LDH"
    assert "ldh" in _parents("NiFe-LDH", "material-class")

    on_carbon = parse_composition("Pd/C")
    assert on_carbon is not None
    assert on_carbon.support == "C"

    foam = parse_composition("Cu-foam@mesh")
    assert foam is not None
    assert {sym for sym, _ in foam.elements} == {"Cu"}
    assert foam.decoration == "foam"
    assert foam.support == "mesh"


def test_unparseable_labels_return_none_not_nonsense() -> None:
    """`Formula` itself does not validate symbols (`Formula("Xy2")` happily
    returns ``{"Xy": 2}``), so the trap here is not "does Formula raise" but
    "does an all-lowercase-after-first-letter word get read as element
    fragments". Both words fail to fully consume as a symbol run (the
    scanner requires every character to belong to an ``[A-Z][a-z]?``+digits
    token) and return ``None`` rather than a partial parse."""
    assert parse_composition("Mesh") is None
    assert parse_composition("Catalyst") is None


def test_ac6_two_parents_two_axes() -> None:
    edges = memberships("Pd(111)", _CONFIG)
    composition_edges = [
        e for e in edges if e.axis == "composition" and e.parent == "pd"
    ]
    termination_edges = [
        e for e in edges if e.axis == "termination" and e.parent == "fcc-111"
    ]
    assert len(composition_edges) == 1
    assert len(termination_edges) == 1
    assert composition_edges[0].axis != termination_edges[0].axis
    # Both parents widen the same node.
    assert composition_edges[0].child == termination_edges[0].child


def test_ac6_widening_does_not_leak_across_axes() -> None:
    """The test that matters: axis-on-edge (`types.AxisEdge`'s docstring) is
    what stops widening along one axis from leaking along another.
    `Pd(111)` and `Cu(111)` share a termination parent (`fcc-111` — both
    are fcc metals) and even share the composition-axis *class* parent
    (`unary-alloy` — both are single-metal formulas), but must NOT share
    the composition-axis *element-identity* parent: a composition widening
    from `Pd(111)` reaches `pd`, never `cu`, because a composition-only
    query follows only `axis="composition"` edges and would otherwise
    wander from Pd onto Cu through the shared facet."""
    pd_comp = _parents("Pd(111)", "composition")
    cu_comp = _parents("Cu(111)", "composition")
    assert pd_comp == {"pd", "unary-alloy"}
    assert cu_comp == {"cu", "unary-alloy"}
    assert "cu" not in pd_comp
    # "unary-alloy" is a legitimate shared parent (both are single-metal
    # formulas) — the element-identity parent is what must not leak.
    assert pd_comp - {"unary-alloy"} != cu_comp - {"unary-alloy"}

    pd_term = _parents("Pd(111)", "termination")
    cu_term = _parents("Cu(111)", "termination")
    assert "fcc-111" in pd_term
    assert "fcc-111" in cu_term  # the axis they DO legitimately share


def test_cubic_equivalents_counts() -> None:
    """Symmetry assumption: the full cubic point group m-3m (3! coordinate
    permutations x 2**3 independent sign flips = 48 raw operations,
    deduplicated as a set of resulting index tuples). This collapses to the
    textbook cubic multiplicities 6 / 8 / 12 for <100> / <111> / <110>
    directions.

    `taxonomy-bootstrap.md`'s own text claims `(1,-1,0)` has 24 members;
    that number is wrong (checked here rather than trusted, per
    `probe-numbers-need-a-pinned-method`) — 24 double-counts because two of
    the six coordinate permutations of `(1,-1,0)` are degenerate under sign
    flips (swapping the two magnitude-1 slots and then flipping both signs
    reproduces a tuple the other permutation already produced), a
    degeneracy `(1,1,1)`'s all-equal case has too (6 permutations -> 1
    effective) but `(1,-1,0)`'s already-mixed signs partially hide. 12
    matches the standard {110} cubic family size."""
    assert len(cubic_equivalents((1, 1, 1))) == 8
    assert len(cubic_equivalents((1, 0, 0))) == 6
    assert len(cubic_equivalents((1, -1, 0))) == 12


def test_elements_table() -> None:
    assert len(ELEMENTS) == 118
    from ase.data import chemical_symbols

    assert {e.symbol for e in ELEMENTS} == set(chemical_symbols[1:])

    pd = element("Pd")
    assert pd is not None
    assert (pd.group, pd.period, pd.block) == (10, 5, "d")
    assert pd.is_transition_metal
    assert pd.is_platinum_group

    ce = element("Ce")
    assert ce is not None
    assert ce.series == "lanthanide"
    assert ce.group is None
    assert ce.block == "f"

    h = element("H")
    assert h is not None
    assert (h.period, h.block, h.series) == (1, "s", "reactive-nonmetal")

    assert element("Xx") is None


def test_tag_memberships_splits_compound_tag() -> None:
    edges = tag_memberships("catalyst:sulfide-mof", _CONFIG)
    parents = {e.parent for e in edges}
    assert parents == {"sulfide", "mof"}
    assert all(e.axis == "material-class" for e in edges)


def test_memberships_sorted_and_deduplicated() -> None:
    edges = memberships("Pt3Ni", _CONFIG)
    assert list(edges) == sorted(edges, key=lambda e: (e.axis, e.parent, e.child))
    keys = [(e.axis, e.parent, e.child) for e in edges]
    assert len(keys) == len(set(keys))


def test_subject_key_canonicalises() -> None:
    assert subject_key("Pd(111)") == "pd-111"
    assert subject_key("NiFe-LDH") == "nife-ldh"

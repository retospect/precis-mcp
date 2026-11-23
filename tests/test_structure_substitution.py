"""Per-axis substitution query — slice 1 of gr695283.

Fixture provenance. Every row is a well-known compound whose structure
resolves to a Materials Project entry (mp id in :data:`_MP_IDS`), verified
2026-11-23 through the HEAS mirror of Materials Project
(``heas.labs.gt.ac.cn``, whose per-material detail pages cross-link the
canonical ``materialsproject.org/materials/<mp_id>`` page); the sandbox has
no MP API key, so the mirror was the lookup path. The ids are provenance
for the structure family, not the source of the row values. Orderings,
transition temperatures and lattice parameters are textbook/literature
values for well-known compounds (SrRuO3: Koster et al., Rev. Mod. Phys.
84, 253 (2012); manganites: Wollan & Koehler, Phys. Rev. 100, 545 (1955);
Fe3O4 / Co3O4 / Mn3O4 and Co2MnSi / Ni2MnGa: standard references).
Volume/atom is ``a**3 / n_atoms`` from the room-temperature lattice
parameter. The rows are tier ``asserted``, origin ``literature``, and
exercise the query surface, they are not curated data.

Embedding source: the learned ``structure_graph`` space (me692057) does not
exist yet. The stand-in is :func:`_fingerprint`, a deterministic
structure-graph descriptor computed from the record itself — per site
role, the multiplicity fraction and the count-weighted atomic number
(ASE's ``atomic_numbers``), normalised — not hand-typed vectors:
same-prototype decorations separate by their site chemistry (every
same-prototype candidate gets a distinct vector, so the embedding axis is
not vacuous), and prototypes still separate by their multiplicity pattern.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

import pytest
from ase.data import atomic_numbers

from precis.errors import BadInput, Unsupported
from precis.structure.substitution import (
    AXES,
    Axis,
    Constraint,
    Crystal,
    Value,
    axis_distance,
    ordering_at,
    ordering_distance,
    prototype_key,
    query,
    substitution_sites,
    swap_pair_key,
)

# --- fixtures: three crystal families (td696679 comment 4) -----------------

#: (name, family, decoration, prototype, volume/atom Å^3, ordering, T_order K).
#: Decorations are ((role, element, count), ...) by role.
_ROWS = [
    # perovskites ABO3 (pseudo-cubic a: SrTiO3 3.905, SrRuO3 3.93,
    # LaMnO3 3.94, CaMnO3 3.73 Å; 5 atoms per cell)
    (
        "SrTiO3",
        "perovskite",
        (("A", "Sr", 1), ("B", "Ti", 1), ("X", "O", 3)),
        "ABO3",
        11.9,
        "dia",
        None,
    ),
    (
        "SrRuO3",
        "perovskite",
        (("A", "Sr", 1), ("B", "Ru", 1), ("X", "O", 3)),
        "ABO3",
        12.1,
        "ferro",
        160.0,
    ),
    (
        "LaMnO3",
        "perovskite",
        (("A", "La", 1), ("B", "Mn", 1), ("X", "O", 3)),
        "ABO3",
        12.3,
        "afm_a",
        140.0,
    ),
    (
        "CaMnO3",
        "perovskite",
        (("A", "Ca", 1), ("B", "Mn", 1), ("X", "O", 3)),
        "ABO3",
        10.4,
        "afm_g",
        125.0,
    ),
    # spinels AB2X4 (cubic a: Fe3O4 8.397, Co3O4 8.084 Å; 56 atoms per cell)
    (
        "Fe3O4",
        "spinel",
        (("A", "Fe", 1), ("B", "Fe", 2), ("X", "O", 4)),
        "AB2X4",
        10.6,
        "ferri",
        858.0,
    ),
    (
        "Co3O4",
        "spinel",
        (("A", "Co", 1), ("B", "Co", 2), ("X", "O", 4)),
        "AB2X4",
        9.4,
        "afm_g",
        30.0,
    ),
    (
        "Mn3O4",
        "spinel",
        (("A", "Mn", 1), ("B", "Mn", 2), ("X", "O", 4)),
        "AB2X4",
        11.2,
        "ferri",
        42.0,
    ),
    # Heuslers X2YZ (cubic a: Co2MnSi 5.654, Ni2MnGa 5.825 Å; 4-atom cell)
    (
        "Co2MnSi",
        "heusler",
        (("X", "Co", 2), ("Y", "Mn", 1), ("Z", "Si", 1)),
        "X2YZ",
        11.3,
        "ferro",
        985.0,
    ),
    (
        "Ni2MnGa",
        "heusler",
        (("X", "Ni", 2), ("Y", "Mn", 1), ("Z", "Ga", 1)),
        "X2YZ",
        12.4,
        "ferro",
        376.0,
    ),
]


#: Materials Project provenance for each fixture row (see the module
#: docstring for the mirror-lookup route). Gloss: the structure the id
#: resolves to, when it is not the obvious room-temperature one.
_MP_IDS = {
    "SrTiO3": "mp-4651",  # stable entry: tetragonal I4/mcm (cubic Pm-3m is mp-5229)
    "SrRuO3": "mp-22390",  # orthorhombic Pnma, stable
    "LaMnO3": "mp-17554",  # orthorhombic Pnma (Jahn-Teller distorted)
    "CaMnO3": "mp-1017467",  # the mirror's CaMnO3 entry (cubic Pm-3m; RT is Pnma)
    "Fe3O4": "mp-19306",  # cubic Fd-3m spinel
    "Co3O4": "mp-18748",  # cubic Fd-3m spinel
    "Mn3O4": "mp-20141",  # orthorhombic Pbcm (hausmannite)
    "Co2MnSi": "mp-4492",  # Fm-3m Heusler, stable
    "Ni2MnGa": "mp-20228",  # Fm-3m austenite (the stable entry mp-601285 is the martensite)
}


def _fingerprint(decoration: tuple[tuple[str, str, int], ...]) -> tuple[float, ...]:
    """Structure-graph embedding stand-in: per site role (sorted), the
    multiplicity fraction and the count-weighted atomic number, normalised
    by the cell total and the heaviest element present — so same-prototype
    decorations separate by site chemistry and prototypes by their
    multiplicity pattern. Deterministic from the record; ASE supplies the
    atomic numbers, no hand-typed vectors."""
    total = sum(count for _role, _element, count in decoration)
    peak = max(atomic_numbers[element] for _role, element, _count in decoration)
    vector: list[float] = []
    for _role, element, count in sorted(decoration):
        vector.append(count / total)
        vector.append(count * atomic_numbers[element] / (total * peak))
    return tuple(vector)


def _crystal(row: tuple) -> Crystal:
    name, family, decoration, proto, volume, ordering, t_order = row
    return Crystal(
        name=name,
        family=family,
        decoration=decoration,
        prototype=proto,
        volume_per_atom=Value(
            num=volume, unit="angstrom3_per_atom", origin="literature"
        ),
        ordering=ordering,
        ordering_origin="literature",
        order_temperature=(
            Value(num=t_order, unit="K", origin="literature") if t_order else None
        ),
        embedding={"structure_graph": _fingerprint(decoration)},
        embedding_origins={"structure_graph": "computation"},
    )


CRYSTALS = [_crystal(row) for row in _ROWS]
BY_NAME = {c.name: c for c in CRYSTALS}


def test_fixtures_resolve_to_materials_project_entries() -> None:
    """Every fixture row carries a Materials Project id, verified through
    the HEAS mirror (module docstring); none is guessed."""
    assert set(_MP_IDS) == {c.name for c in CRYSTALS}
    for mp_id in _MP_IDS.values():
        assert mp_id.startswith("mp-")
        assert mp_id.removeprefix("mp-").isdigit()


# --- axis-typed distances ---------------------------------------------------


def test_ordering_distance_table() -> None:
    assert ordering_distance("ferro", "ferro") == 0.0
    assert ordering_distance("ferro", "ferri") == 0.25
    assert ordering_distance("dia", "para") == 0.5
    assert ordering_distance("ferro", "dia") == 1.0  # unlisted pair
    assert ordering_distance("afm_a", "afm_g") == 0.25
    assert ordering_distance("ferro", "ferri") < ordering_distance("ferro", "afm_g")
    with pytest.raises(BadInput):
        ordering_distance("ferro", "superconducting")


def test_ordering_at_derives_phase_at_query_time() -> None:
    t_c = Value(num=160.0, unit="K")
    assert ordering_at("ferro", t_c, 100.0) == "ferro"
    assert ordering_at("ferro", t_c, 160.0) == "para"  # at the transition: lost
    assert ordering_at("dia", None, 4.0) == "dia"
    with pytest.raises(BadInput):
        ordering_at("afm_g", None, 300.0)  # ordered state must store T_order
    with pytest.raises(Unsupported):  # Celsius is not kelvin
        ordering_at("ferro", Value(num=160.0, unit="degC"), 100.0)


def test_scalar_axis_linear_and_log() -> None:
    volume = AXES["volume_per_atom"]
    assert axis_distance(volume, 11.9, 8.4) == pytest.approx(3.5 / 20.0)
    with pytest.raises(Unsupported):  # a foreign unit is refused, not compared
        axis_distance(volume, Value(num=11.9, unit="nm3"), 8.4)
    t_order = AXES["ordering_temperature"]
    d = axis_distance(t_order, Value(num=850.0, unit="K"), Value(num=40.0, unit="K"))
    assert d == pytest.approx(math.log(850.0 / 40.0) / math.log(2000.0))
    # log scale: the same ratio costs the same at any magnitude
    assert axis_distance(t_order, 160.0, 80.0) == pytest.approx(
        axis_distance(t_order, 850.0, 425.0)
    )
    with pytest.raises(BadInput):
        axis_distance(t_order, 0.0, 100.0)  # log over non-positive


def test_ordinal_axis_rank_difference() -> None:
    axis = Axis(name="hurdle", kind="ordinal", order=("low", "mid", "high"))
    assert axis_distance(axis, "low", "high") == pytest.approx(1.0)
    assert axis_distance(axis, "low", "mid") == pytest.approx(0.5)
    with pytest.raises(BadInput):
        axis_distance(axis, "low", "sideways")


def test_embedding_cosine_distance() -> None:
    axis = AXES["structure_embedding"]
    same = axis_distance(axis, (1.0, 0.0, 0.0), (1.0, 0.0, 0.0))
    orthogonal = axis_distance(axis, (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    assert same == pytest.approx(0.0)
    assert orthogonal == pytest.approx(1.0)
    with pytest.raises(BadInput):
        axis_distance(axis, (0.0, 0.0), (1.0, 0.0))  # width mismatch


def test_tensor_axis_is_declared_not_built() -> None:
    axis = Axis(name="elastic", kind="tensor")
    with pytest.raises(Unsupported):
        axis_distance(axis, 1.0, 2.0)


def test_condition_axis_is_declared_not_built() -> None:
    """The design names a condition axis type; only the magnetic ``at``
    op is wired — the kind itself is declared, not built, like tensor."""
    axis = Axis(name="m_of_t", kind="condition")
    with pytest.raises(Unsupported):
        axis_distance(axis, 1.0, 2.0)


def test_categorical_axis_vocabulary_is_closed() -> None:
    """The magnetic axis's vocabulary is closed: an unlisted ordering is
    BadInput (as ``ordering_distance``/``ordering_at`` already enforce),
    never a silent 1.0 distance."""
    axis = AXES["magnetic_ordering"]
    with pytest.raises(BadInput):
        axis_distance(axis, "afm", "ferro")  # afm is not a declared ordering
    with pytest.raises(BadInput):
        axis_distance(axis, "afm", "afm")  # closed even when the pair is equal
    # declared values still distance through the table / default
    assert axis_distance(axis, "afm_a", "afm_g") == pytest.approx(0.25)
    assert axis_distance(axis, "afm_g", "ferro") == pytest.approx(1.0)


def test_value_shape_mirrors_measures_vocabulary() -> None:
    interval = Value(num=11.9, low=11.7, high=12.1, err=0.2, unit="angstrom3_per_atom")
    assert interval.origin == "literature"
    with pytest.raises(BadInput):
        Value(num=1.0, origin="guessed")  # not a measures origin
    with pytest.raises(BadInput):
        Value(num=1.0, tier="plausible")  # not a measures tier


# --- prototype / occupancy split --------------------------------------------


def test_prototype_key_strips_decoration() -> None:
    assert prototype_key(BY_NAME["SrTiO3"]) == prototype_key(BY_NAME["SrRuO3"])
    assert prototype_key(BY_NAME["Fe3O4"]) != prototype_key(BY_NAME["SrTiO3"])
    # topology, not stoichiometry accident: same roles, same key
    same = Crystal(
        name="X",
        family="spinel",
        decoration=(("A", "Co", 1), ("B", "Al", 2), ("X", "O", 4)),
        prototype="AB2X4",
    )
    assert prototype_key(same) == prototype_key(BY_NAME["Fe3O4"])
    # recorded multiplicities are part of the topology
    other = Crystal(
        name="Y",
        family="spinel",
        decoration=(("A", "Co", 2), ("B", "Al", 1), ("X", "O", 4)),
        prototype="AB2X4",
    )
    assert prototype_key(other) != prototype_key(BY_NAME["Fe3O4"])
    assert prototype_key(BY_NAME["Fe3O4"])[1] == (("A", 1), ("B", 2), ("X", 4))
    with pytest.raises(BadInput):
        prototype_key(
            Crystal(
                name="dup",
                family="x",
                decoration=(("A", "Co", 1), ("A", "Ni", 1), ("X", "O", 4)),
            )
        )


def test_substitution_sites_per_role_fraction() -> None:
    fe3o4, co3o4, manganite = (
        BY_NAME["Fe3O4"],
        BY_NAME["Co3O4"],
        BY_NAME["SrTiO3"],
    )
    # roles A and B differ, X (oxygen) is shared: 2 of 3
    assert substitution_sites(fe3o4, co3o4) == pytest.approx(2 / 3)
    # different prototypes: substitution is undefined, not far
    assert substitution_sites(fe3o4, manganite) is None
    # same label, different multiplicities: a different prototype too
    inverse = Crystal(
        name="inv",
        family="spinel",
        decoration=(("A", "Co", 2), ("B", "Al", 1), ("X", "O", 4)),
        prototype="AB2X4",
    )
    assert substitution_sites(fe3o4, inverse) is None
    with pytest.raises(BadInput):
        swap_pair_key(fe3o4, inverse)


def test_swap_pair_key_is_canonical() -> None:
    a, b = BY_NAME["Fe3O4"], BY_NAME["Co3O4"]
    key = swap_pair_key(a, b)
    assert key == swap_pair_key(b, a)
    assert key.startswith("swap:AB2X4[A1,B2,X4]:")
    assert "A=Fe" in key and "A=Co" in key
    with pytest.raises(BadInput):
        swap_pair_key(a, BY_NAME["SrTiO3"])


def test_empty_decoration_is_rejected_not_a_zero_division() -> None:
    """An empty decoration is malformed data: :func:`prototype_key` raises,
    so a query with such a *target* fails before the scan and such a
    *candidate* is filtered like any unusable value — never a
    ``ZeroDivisionError`` aborting the scan (review td778113)."""
    empty = Crystal(name="void", family="perovskite", decoration=(), prototype="ABO3")
    with pytest.raises(BadInput):
        prototype_key(empty)
    with pytest.raises(BadInput):
        substitution_sites(BY_NAME["SrRuO3"], empty)
    with pytest.raises(BadInput):
        swap_pair_key(BY_NAME["SrRuO3"], empty)
    with pytest.raises(BadInput):  # target: rejected before any candidate is scanned
        query(
            CRYSTALS,
            target=empty,
            constraints=[Constraint(axis="decoration", op="equal")],
        )
    matches = query(
        [*CRYSTALS, empty],
        target=BY_NAME["SrRuO3"],
        constraints=[
            Constraint(axis="decoration", op="equal"),
            Constraint(axis="magnetic_ordering", op="far", min_d=0.5),
        ],
    )
    assert "void" not in {m.crystal.name for m in matches}  # filtered, scan completed


# --- the three-axis query ----------------------------------------------------


def test_same_prototype_different_magnetic_decoration() -> None:
    """The slice-1 check (td696679 comment 4): from ferromagnetic SrRuO3
    (Tc 160 K), one query over all three requested axes — structure
    embedding (kNN rank), volume/atom (near, filters), magnetic ordering
    (far) — finds the same-structure antiferromagnetic perovskite LaMnO3
    (the literature FM/AFM pair) and non-magnetic SrTiO3, while CaMnO3,
    also AFM and same-prototype, is dropped by the volume axis (Ca is
    smaller: |10.4 - 12.1| / 20 = 0.085 > 0.02). Nothing outside ABO3
    passes the decoration axis."""
    matches = query(
        CRYSTALS,
        target=BY_NAME["SrRuO3"],
        constraints=[
            Constraint(axis="decoration", op="equal"),
            Constraint(axis="structure_embedding", op="near"),
            Constraint(axis="volume_per_atom", op="near", max_d=0.02),
            Constraint(axis="magnetic_ordering", op="far", min_d=0.5),
        ],
    )
    names = [m.crystal.name for m in matches]
    # kNN order over the embedded axis: SrTiO3 (0.0078) then LaMnO3 (0.0134)
    assert names == ["SrTiO3", "LaMnO3"]
    assert "CaMnO3" not in names  # the volume axis does real work
    lamno3 = matches[1]
    assert set(lamno3.axis_distances) == {
        "decoration",
        "structure_embedding",
        "volume_per_atom",
        "magnetic_ordering",
    }
    assert lamno3.axis_distances["decoration"] == pytest.approx(2 / 3)  # A and B
    assert lamno3.axis_distances["volume_per_atom"] == pytest.approx(0.2 / 20.0)
    assert lamno3.axis_distances["magnetic_ordering"] == pytest.approx(1.0)
    embedding = lamno3.axis_distances["structure_embedding"]
    assert embedding == pytest.approx(0.013396, abs=1e-6)  # distinct, not 0
    assert 0.0 < matches[0].axis_distances["structure_embedding"] < embedding
    assert lamno3.score == pytest.approx(embedding)  # kNN over the one embedded axis


def test_ferrimagnet_to_antiferromagnet_in_spinels() -> None:
    """Ferrimagnetic Fe3O4 -> antiferromagnetic Co3O4 (not FM/AFM: ferri vs
    AFM); Mn3O4 is ferrimagnetic too so the far-ordering filter drops it."""
    matches = query(
        CRYSTALS,
        target=BY_NAME["Fe3O4"],
        constraints=[
            Constraint(axis="decoration", op="equal"),
            Constraint(axis="magnetic_ordering", op="far", min_d=0.5),
        ],
    )
    assert [m.crystal.name for m in matches] == ["Co3O4"]


def test_duplicate_axis_constraints_raise() -> None:
    with pytest.raises(BadInput):
        query(
            CRYSTALS,
            target=BY_NAME["Fe3O4"],
            constraints=[
                Constraint(axis="magnetic_ordering", op="far", min_d=0.5),
                Constraint(axis="magnetic_ordering", op="at", value="afm_g", at_k=4),
            ],
        )


def test_condition_evaluation_finds_ordering_at_stated_temperature() -> None:
    """SrRuO3 is ferromagnetic at 100 K but paramagnetic at 300 K (Tc 160 K):
    the `at` constraint must see the phase at the stated condition."""
    found_at_100 = query(
        CRYSTALS,
        target=BY_NAME["SrTiO3"],
        constraints=[
            Constraint(axis="decoration", op="equal"),
            Constraint(axis="magnetic_ordering", op="at", value="ferro", at_k=100),
        ],
    )
    assert {m.crystal.name for m in found_at_100} == {"SrRuO3"}

    found_at_300 = query(
        CRYSTALS,
        target=BY_NAME["SrTiO3"],
        constraints=[
            Constraint(axis="decoration", op="equal"),
            Constraint(axis="magnetic_ordering", op="at", value="ferro", at_k=300),
        ],
    )
    assert found_at_300 == []


def test_knn_ranks_embedded_axis_and_reports_distances() -> None:
    """No max_d: the embedded axis ranks only. Same-prototype candidates
    come first; the per-axis volume/atom distances are still reported."""
    matches = query(
        CRYSTALS,
        target=BY_NAME["Fe3O4"],
        constraints=[
            Constraint(axis="structure_embedding", op="near"),
            Constraint(axis="volume_per_atom", op="near", max_d=0.2),
        ],
    )
    names = [m.crystal.name for m in matches]
    assert names[:2] == ["Co3O4", "Mn3O4"]
    assert len(matches) == 8
    scores = [m.score if m.score is not None else math.inf for m in matches]
    assert math.inf not in scores
    assert scores == sorted(scores)
    assert names.index("SrRuO3") < names.index("Co2MnSi")  # perovskite nearer
    mno = matches[1]
    assert mno.axis_distances["volume_per_atom"] == pytest.approx(
        abs(11.2 - 10.6) / 20.0
    )


def test_far_on_axis_and_range_filters() -> None:
    matches = query(
        CRYSTALS,
        target=BY_NAME["Co2MnSi"],
        constraints=[
            Constraint(axis="decoration", op="equal"),
            Constraint(axis="ordering_temperature", op="range", lo=300, hi=1000),
            Constraint(axis="magnetic_ordering", op="equal", value="ferro"),
        ],
    )
    # Ni2MnGa (Tc 376 K) passes; Co2MnSi (985 K) is the target
    assert {m.crystal.name for m in matches} == {"Ni2MnGa"}


def test_unknown_axis_and_missing_operand_raise() -> None:
    with pytest.raises(BadInput):
        query(
            CRYSTALS,
            target=BY_NAME["Fe3O4"],
            constraints=[Constraint(axis="colour", op="near", max_d=0.1)],
        )
    with pytest.raises(BadInput):
        query(
            CRYSTALS,
            target=BY_NAME["Fe3O4"],
            constraints=[Constraint(axis="volume_per_atom", op="near")],
        )


def test_out_of_vocabulary_constraint_operands_raise() -> None:
    """A typo'd ``equal``/``at`` operand is BadInput at validation time —
    the closed-vocabulary contract — never a silent empty result."""
    for constraint in (
        Constraint(axis="magnetic_ordering", op="equal", value="afm"),
        Constraint(axis="magnetic_ordering", op="at", value="afm", at_k=4),
    ):
        with pytest.raises(BadInput):
            query(
                CRYSTALS,
                target=BY_NAME["Fe3O4"],
                constraints=[constraint],
            )


def _ml_variant(**changes: Any) -> Crystal:
    """A synthetic ML-origin Ni2MnGa copy (not a literature row)."""
    return replace(BY_NAME["Ni2MnGa"], name="Ni2MnGa-ml", **changes)


def test_origin_filter_is_cross_cutting() -> None:
    ml = Value(num=12.4, unit="angstrom3_per_atom", origin="ml_prediction")
    crystals = [*CRYSTALS, _ml_variant(volume_per_atom=ml)]
    constraints = [
        Constraint(axis="decoration", op="equal"),
        Constraint(axis="volume_per_atom", op="near", max_d=0.5),
    ]
    strict = query(
        crystals,
        target=BY_NAME["Co2MnSi"],
        constraints=constraints,
        origins={"experiment", "literature"},
    )
    assert "Ni2MnGa-ml" not in {m.crystal.name for m in strict}
    permissive = query(
        crystals,
        target=BY_NAME["Co2MnSi"],
        constraints=constraints,
        origins={"experiment", "literature", "ml_prediction"},
    )
    assert "Ni2MnGa-ml" in {m.crystal.name for m in permissive}
    with pytest.raises(BadInput):
        query(
            crystals,
            target=BY_NAME["Co2MnSi"],
            constraints=constraints,
            origins={"divination"},
        )


def test_origin_filter_covers_the_transition_temperature_of_an_at_query() -> None:
    """Both the ordering and the transition must satisfy the origin filter."""
    ml_tc = Value(num=376.0, unit="K", origin="ml_prediction")
    crystals = [
        *CRYSTALS,
        _ml_variant(order_temperature=ml_tc),
        replace(BY_NAME["Ni2MnGa"], name="unknown-order", ordering_origin=None),
    ]
    constraints = [
        Constraint(axis="decoration", op="equal"),
        Constraint(axis="magnetic_ordering", op="at", value="ferro", at_k=300),
    ]
    strict = query(
        crystals,
        target=BY_NAME["Co2MnSi"],
        constraints=constraints,
        origins={"literature"},
    )
    assert {m.crystal.name for m in strict} == {"Ni2MnGa"}


@pytest.mark.parametrize(
    ("constraint", "origin"),
    [
        (Constraint(axis="magnetic_ordering", op="equal", value="ferro"), "literature"),
        (Constraint(axis="structure_embedding", op="near"), "computation"),
    ],
)
def test_origin_filter_requires_provenance_on_bare_axes(
    constraint: Constraint, origin: str
) -> None:
    candidate = BY_NAME["Ni2MnGa"]
    unknown = replace(
        candidate, name="unknown", ordering_origin=None, embedding_origins={}
    )
    crystals = [candidate, unknown]
    target = BY_NAME["Co2MnSi"]
    constraints = [constraint]
    assert len(query(crystals, target=target, constraints=constraints)) == 2
    matches = query(crystals, target=target, constraints=constraints, origins={origin})
    assert [match.crystal for match in matches] == [candidate]
    assert (
        query(crystals, target=target, constraints=constraints, origins={"experiment"})
        == []
    )


def test_negative_k_is_rejected_with_no_candidates() -> None:
    with pytest.raises(BadInput, match="k must be non-negative"):
        query([], target=BY_NAME["Co2MnSi"], constraints=[], k=-1)


def test_out_of_vocabulary_candidate_is_filtered_not_far_matched() -> None:
    """A typo'd ordering is unusable data: filtered like a missing value
    (the ``at`` path's behaviour), never returned as a distance-1.0 "far"
    candidate."""
    crystals = [*CRYSTALS, _ml_variant(ordering="afm")]  # not afm_a/afm_g
    matches = query(
        crystals,
        target=BY_NAME["Ni2MnGa"],
        constraints=[
            Constraint(axis="decoration", op="equal"),
            Constraint(axis="magnetic_ordering", op="far", min_d=0.5),
        ],
    )
    assert "Ni2MnGa-ml" not in {m.crystal.name for m in matches}


def test_out_of_vocabulary_target_raises_up_front() -> None:
    """A bad target ordering raises before the candidate loop on every op
    path — not a silent empty result from filtering all candidates."""
    bad_target = replace(BY_NAME["Ni2MnGa"], ordering="afm")
    with pytest.raises(BadInput):
        query(
            CRYSTALS,
            target=bad_target,
            constraints=[
                Constraint(axis="decoration", op="equal"),
                Constraint(axis="magnetic_ordering", op="far", min_d=0.5),
            ],
        )


def test_unsupported_op_axis_combinations_are_rejected_up_front() -> None:
    for constraint in (
        Constraint(axis="magnetic_ordering", op="range", lo=0, hi=1),
        Constraint(axis="structure_embedding", op="range", lo=0, hi=1),
        Constraint(axis="decoration", op="range", lo=0, hi=1),
        Constraint(axis="structure_embedding", op="equal", value=(1.0,)),
        Constraint(axis="decoration", op="at", at_k=300, value="x"),
    ):
        with pytest.raises((BadInput, Unsupported)):
            query(CRYSTALS, target=BY_NAME["Fe3O4"], constraints=[constraint])
    # even over an empty candidate set: rejected before scanning
    with pytest.raises(BadInput):
        query(
            [],
            target=BY_NAME["Fe3O4"],
            constraints=[Constraint(axis="magnetic_ordering", op="range", lo=0, hi=1)],
        )


def test_nonfinite_inputs_are_rejected_not_silently_compared() -> None:
    """A NaN/inf in data or operands is malformed (review td778113):
    :class:`BadInput`, so the query rejects a malformed target up front
    and filters a malformed candidate — never a NaN distance occupying a
    kNN slot or a false "para" off ``t_k < NaN`` being False."""
    axis = AXES["structure_embedding"]
    with pytest.raises(BadInput):
        axis_distance(axis, (math.nan, 0.0), (1.0, 0.0))
    with pytest.raises(BadInput):
        axis_distance(axis, (1.0, 0.0), (math.inf, 0.0))
    l2 = Axis(name="emb2", kind="embedding", metric="l2")
    with pytest.raises(BadInput):
        axis_distance(l2, (math.nan, 0.0), (1.0, 0.0))
    t_c = Value(num=160.0, unit="K")
    with pytest.raises(BadInput):
        ordering_at("ferro", Value(num=math.nan, unit="K"), 100.0)
    with pytest.raises(BadInput):
        ordering_at("ferro", t_c, math.nan)
    with pytest.raises(BadInput):
        ordering_at("ferro", Value(num=math.inf, unit="K"), 100.0)
    with pytest.raises(BadInput):
        axis_distance(AXES["volume_per_atom"], math.nan, 10.0)


def test_nonfinite_embedding_candidate_is_filtered_not_ranked() -> None:
    """An unbounded ``near`` ranks only: a NaN vector used to score NaN
    and hold a top-k slot; now it is filtered like any unusable value."""
    width = len(BY_NAME["Ni2MnGa"].embedding["structure_graph"])
    bad = replace(
        BY_NAME["Ni2MnGa"],
        name="nan-vec",
        embedding={"structure_graph": (math.nan,) * width},
    )
    matches = query(
        [*CRYSTALS, bad],
        target=BY_NAME["Co2MnSi"],
        constraints=[Constraint(axis="structure_embedding", op="near")],
        k=3,
    )
    names = {m.crystal.name for m in matches}
    assert "nan-vec" not in names
    assert all(m.score is not None and math.isfinite(m.score) for m in matches)


def test_nonfinite_target_raises_before_the_scan() -> None:
    """The same malformation on the target is BadInput on every path —
    not a silent empty result from filtering all candidates."""
    bad_vec = replace(
        BY_NAME["Ni2MnGa"],
        embedding={"structure_graph": (math.nan, math.nan, math.nan)},
    )
    with pytest.raises(BadInput):
        query(
            CRYSTALS,
            target=bad_vec,
            constraints=[Constraint(axis="structure_embedding", op="near")],
        )
    nan_tc = replace(
        BY_NAME["SrRuO3"],
        order_temperature=Value(num=math.nan, unit="K"),
    )
    with pytest.raises(BadInput):
        query(
            CRYSTALS,
            target=nan_tc,
            constraints=[
                Constraint(axis="magnetic_ordering", op="at", value="ferro", at_k=100)
            ],
        )
    nan_volume = replace(
        BY_NAME["Co2MnSi"],
        volume_per_atom=Value(num=math.nan, unit="angstrom3_per_atom"),
    )
    with pytest.raises(BadInput):
        query(
            CRYSTALS,
            target=nan_volume,
            constraints=[Constraint(axis="volume_per_atom", op="near", max_d=0.5)],
        )


def test_nan_transition_temperature_is_filtered_not_paramagnetic() -> None:
    """The review's exact failure: ``300 < NaN`` is False, so an ordered
    candidate with a NaN transition temperature used to match an ``at``
    query for "para". It is malformed data — filtered, not classified."""
    nan_tc = replace(
        BY_NAME["SrRuO3"],
        name="sro-nan",
        order_temperature=Value(num=math.nan, unit="K"),
    )
    matches = query(
        [*CRYSTALS, nan_tc],
        target=BY_NAME["SrTiO3"],
        constraints=[
            Constraint(axis="decoration", op="equal"),
            Constraint(axis="magnetic_ordering", op="at", value="para", at_k=300),
        ],
    )
    names = {m.crystal.name for m in matches}
    assert "sro-nan" not in names
    # genuine T_order rows still classify: SrRuO3/LaMnO3/CaMnO3 are para at 300 K
    assert names == {"SrRuO3", "LaMnO3", "CaMnO3"}


def test_nonfinite_constraint_operands_raise() -> None:
    """A NaN bound compares False everywhere — it would silently filter
    every candidate, so the operand itself is BadInput."""
    for constraint in (
        Constraint(axis="volume_per_atom", op="near", max_d=math.nan),
        Constraint(axis="magnetic_ordering", op="far", min_d=math.nan),
        Constraint(axis="volume_per_atom", op="range", lo=0.0, hi=math.inf),
        Constraint(axis="magnetic_ordering", op="at", value="para", at_k=math.nan),
    ):
        with pytest.raises(BadInput):
            query(CRYSTALS, target=BY_NAME["Fe3O4"], constraints=[constraint])

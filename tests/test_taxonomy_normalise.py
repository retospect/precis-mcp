"""Tests for stage 3 (`precis.taxonomy.normalise`).

Plain, DB-free. Fixtures build a private `pint.UnitRegistry` loaded with the
`norr-her-meta` campaign's extra unit definitions — never the process-wide
singleton in `precis.utils.units` (that would leak campaign vocabulary into
unrelated call sites, per the taxonomy-bootstrap decisions log).
"""

from __future__ import annotations

import itertools

import pint
import pytest

from precis.taxonomy.config import load_campaign
from precis.taxonomy.normalise import (
    MergeSuggestion,
    alias_key,
    normalise,
    resolve_dimension,
    si_vector,
)
from precis.taxonomy.types import Anchor, DiscoveredTerm, Half, Mention

_seq = itertools.count()


def make_term(
    ref_id: int,
    measurand: str,
    *,
    half: Half = "A",
    dimension_text: str | None = None,
    raw_unit: str | None = None,
    reference_state: str | None = None,
    convention: str | None = None,
    normalisation_basis: str | None = None,
) -> DiscoveredTerm:
    """``raw_unit`` is the literal token recorded on the ``Mention`` (feeds
    ``TermNode.units_seen``); ``dimension_text`` is stage 2's own dimension
    string (feeds ``resolve_dimension`` / the grouping key). They are
    independent on purpose — a fixture can hold the dimension fixed while
    varying the raw unit spelling, which is what the ``units_seen`` tests
    need.
    """
    n = next(_seq)
    anchor = Anchor(source_ref_id=ref_id, start=n, end=n + 1)
    mention = Mention(anchor=anchor, kind="value", literal="1", raw_unit=raw_unit)
    return DiscoveredTerm(
        mention=mention,
        measurand=measurand,
        half=half,
        dimension_text=dimension_text,
        reference_state=reference_state,
        convention=convention,
        normalisation_basis=normalisation_basis,
    )


@pytest.fixture
def config():
    return load_campaign("norr-her-meta")


@pytest.fixture
def registry(config) -> pint.UnitRegistry:
    reg = pint.UnitRegistry()
    for definition in config.unit_definitions:
        reg.define(definition)
    return reg


# --- si_vector -----------------------------------------------------------


def test_si_vector_inverse_seconds(registry):
    dim = registry.parse_expression("1/s").dimensionality
    assert si_vector(dim) == "0,0,-1,0,0,0,0"


def test_si_vector_current_density(registry):
    dim = registry.Quantity(1, "mA/cm^2").dimensionality
    vector = si_vector(dim).split(",")
    # SI_BASE_ORDER = length, mass, time, current, temperature, substance, luminosity
    assert vector[0] == "-2"  # length^-2
    assert vector[3] == "1"  # current^1


def test_si_vector_voltage_vs_energy_differ(registry):
    volt = si_vector(registry.Quantity(1, "V").dimensionality)
    ev = si_vector(registry.Quantity(1, "eV").dimensionality)
    assert volt != ev


# --- resolve_dimension -----------------------------------------------------


def test_resolve_dimension_percent_is_dimensionless(registry, config):
    spec = resolve_dimension("%", registry, config)
    assert spec is not None
    assert spec.kind == "dimensionless"


def test_resolve_dimension_currency_carries_code_and_year(registry, config):
    spec = resolve_dimension("USD", registry, config, base_year=2015)
    assert spec is not None
    assert spec.kind == "currency"
    assert spec.currency_code == "USD"
    assert spec.base_year == 2015


def test_resolve_dimension_scale_not_silently_si(registry, config):
    scale = resolve_dimension("mV/dec", registry, config)
    volt = resolve_dimension("V", registry, config)
    assert scale is not None
    assert scale.kind == "scale"
    assert volt is not None and volt.kind == "si"
    # The decade base must not be silently dropped, which would make a
    # Tafel slope compare equal to a plain voltage.
    assert not scale.comparable_with(volt)
    assert scale.si_vector is None


def test_resolve_dimension_unknown_token_is_none(registry, config):
    assert resolve_dimension("hubs", registry, config) is None


def test_resolve_dimension_none_unit_is_none(registry, config):
    assert resolve_dimension(None, registry, config) is None


# --- alias_key --------------------------------------------------------------


def test_alias_key_greek_letter_folds_to_name():
    assert alias_key("ΔG_H*") == alias_key("delta G H*")


def test_alias_key_subscript_folds_to_ascii_digit():
    assert alias_key("NH₃ yield rate") == alias_key("NH3 yield rate")


def test_alias_key_british_american_spelling_folds():
    assert alias_key("normalisation") == alias_key("normalization")
    assert alias_key("polarisation") == alias_key("polarization")
    assert alias_key("current normalised") == alias_key("current normalized")


def test_alias_key_is_stable_and_lowercase():
    key = alias_key("Applied Potential")
    assert key == "applied-potential"
    assert alias_key(key) == key


# --- normalise: dimension mismatch gate (AC5) --------------------------------


def test_ac5_tof_splits_into_two_nodes_plus_unresolved(registry, config):
    terms = [
        make_term(1, "TOF", dimension_text="1/s"),
        make_term(2, "TOF", dimension_text="us"),
        make_term(3, "TOF", dimension_text=None),
    ]
    nodes, _suggestions = normalise(terms, registry, config)
    assert len(nodes) == 3

    by_dim_kind = {(n.dimension.kind if n.dimension else None): n for n in nodes}
    assert set(by_dim_kind) == {"si", None}
    frequency_node = next(
        n for n in nodes if n.dimension and n.dimension.si_vector == "0,0,-1,0,0,0,0"
    )
    time_node = next(
        n for n in nodes if n.dimension and n.dimension.si_vector == "0,0,1,0,0,0,0"
    )
    unresolved_node = next(n for n in nodes if n.dimension is None)

    assert frequency_node.dimension is not None
    assert time_node.dimension is not None
    assert not frequency_node.dimension.comparable_with(time_node.dimension)
    assert any("tof" in note for note in frequency_node.notes)
    assert any("tof" in note for note in time_node.notes)
    assert any("escalate" in note for note in unresolved_node.notes)


def test_ac5_no_unit_row_never_merges_into_a_resolved_node(registry, config):
    terms = [
        make_term(1, "TOF", dimension_text="1/s"),
        make_term(2, "TOF", dimension_text=None),
    ]
    nodes, _ = normalise(terms, registry, config)
    dims = {n.dimension for n in nodes}
    assert None in dims
    assert len(nodes) == 2


# --- normalise: reference-state split -----------------------------------


def test_reference_state_disagreement_splits_the_group(registry, config):
    terms = [
        make_term(1, "applied potential", dimension_text="V", reference_state="rhe"),
        make_term(
            2, "applied potential", dimension_text="V", reference_state="ag-agcl"
        ),
        make_term(3, "applied potential", dimension_text="V", reference_state="rhe"),
    ]
    nodes, _ = normalise(terms, registry, config)
    assert len(nodes) == 2
    by_ref_state = {n.reference_state: n for n in nodes}
    assert set(by_ref_state) == {"rhe", "ag-agcl"}
    assert by_ref_state["rhe"].mention_count == 2
    assert by_ref_state["ag-agcl"].mention_count == 1
    # Same key, same dimension — never merged despite sharing both.
    assert by_ref_state["rhe"].key == by_ref_state["ag-agcl"].key
    assert by_ref_state["rhe"].dimension == by_ref_state["ag-agcl"].dimension


def test_convention_disagreement_splits_the_group(registry, config):
    terms = [
        make_term(1, "onset potential", dimension_text="V", convention="iupac"),
        make_term(
            2, "onset potential", dimension_text="V", convention="us-electrochem"
        ),
        make_term(3, "onset potential", dimension_text="V", convention="iupac"),
    ]
    nodes, _ = normalise(terms, registry, config)
    assert len(nodes) == 2
    conventions = {n.convention for n in nodes}
    assert conventions == {"iupac", "us-electrochem"}
    # Same pint dimension on both — the split is convention-driven, not
    # dimension-driven.
    dims = {n.dimension for n in nodes}
    assert len(dims) == 1


def test_normalisation_basis_disagreement_splits_the_group(registry, config):
    terms = [
        make_term(
            1,
            "yield rate",
            dimension_text="g h^-1 g^-1",
            normalisation_basis="per-catalyst-mass",
        ),
        make_term(
            2,
            "yield rate",
            dimension_text="g h^-1 g^-1",
            normalisation_basis="per-geometric-area",
        ),
    ]
    nodes, _ = normalise(terms, registry, config)
    assert len(nodes) == 2
    bases = {n.normalisation_basis for n in nodes}
    assert bases == {"per-catalyst-mass", "per-geometric-area"}
    # pint collapses "g h^-1 g^-1" to plain [time]^-1 on both — proves the
    # split is basis-driven, not a hidden dimension difference.
    dims = {n.dimension for n in nodes}
    assert len(dims) == 1


def test_identity_differs_for_reference_state_split(registry, config):
    terms = [
        make_term(1, "applied potential", dimension_text="V", reference_state="rhe"),
        make_term(
            2, "applied potential", dimension_text="V", reference_state="ag-agcl"
        ),
    ]
    nodes, _ = normalise(terms, registry, config)
    assert len(nodes) == 2
    identities = {n.identity() for n in nodes}
    assert len(identities) == 2


# --- units_seen / canonical_unit ------------------------------------------


def test_units_seen_ordering_is_deterministic_under_reordering(registry, config):
    terms = [
        make_term(1, "onset potential", dimension_text="V", raw_unit="V"),
        make_term(2, "onset potential", dimension_text="V", raw_unit="mV"),
        make_term(3, "onset potential", dimension_text="V", raw_unit="V"),
    ]
    nodes_forward, _ = normalise(terms, registry, config)
    nodes_reversed, _ = normalise(list(reversed(terms)), registry, config)
    expected = (("V", 2), ("mV", 1))
    assert nodes_forward[0].units_seen == expected
    assert nodes_reversed[0].units_seen == expected


def test_canonical_unit_is_most_frequent_and_none_when_absent(registry, config):
    terms = [
        make_term(1, "onset potential", dimension_text="V", raw_unit="mV"),
        make_term(2, "onset potential", dimension_text="V", raw_unit="mV"),
        make_term(3, "onset potential", dimension_text="V", raw_unit="V"),
    ]
    nodes, _ = normalise(terms, registry, config)
    assert nodes[0].canonical_unit == "mV"

    no_unit_terms = [make_term(1, "onset potential", dimension_text="V")]
    no_unit_nodes, _ = normalise(no_unit_terms, registry, config)
    assert no_unit_nodes[0].canonical_unit is None


# --- normalise: grouping basics ------------------------------------------


def test_normalise_pools_aliases_and_papers_within_one_group(registry, config):
    terms = [
        make_term(1, "onset potential", dimension_text="V"),
        make_term(1, "onset potential", dimension_text="V"),
        make_term(2, "Onset Potential", dimension_text="V"),
    ]
    nodes, _ = normalise(terms, registry, config)
    assert len(nodes) == 1
    node = nodes[0]
    assert node.mention_count == 3
    assert node.paper_ref_ids == frozenset({1, 2})
    assert node.status == "proposed"


def test_normalise_output_is_deterministic(registry, config):
    terms = [
        make_term(1, "TOF", dimension_text="1/s"),
        make_term(2, "onset potential", dimension_text="V"),
        make_term(3, "TOF", dimension_text="us"),
    ]
    first_nodes, first_suggestions = normalise(terms, registry, config)
    second_nodes, second_suggestions = normalise(terms, registry, config)
    assert [n.to_json() for n in first_nodes] == [n.to_json() for n in second_nodes]
    assert first_suggestions == second_suggestions


# --- MergeSuggestion ----------------------------------------------------


def test_merge_suggestion_fires_for_near_duplicate_comparable_dimension(
    registry, config
):
    terms = [
        make_term(1, "onset potential", dimension_text="V"),
        make_term(2, "onset-potental", dimension_text="V"),  # typo, comparable dim
    ]
    _nodes, suggestions = normalise(terms, registry, config)
    assert len(suggestions) == 1
    suggestion = suggestions[0]
    assert isinstance(suggestion, MergeSuggestion)
    assert {suggestion.left, suggestion.right} == {"onset-potential", "onset-potental"}
    assert suggestion.similarity > 0.82


def test_merge_suggestion_suppressed_across_dimension_mismatch(registry, config):
    terms = [
        make_term(1, "TOF", dimension_text="1/s"),
        make_term(2, "TOF-rate", dimension_text="us"),  # near-dup key, incomparable dim
    ]
    _nodes, suggestions = normalise(terms, registry, config)
    assert suggestions == ()

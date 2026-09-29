"""Tests for stage 4 (`precis.taxonomy.select`).

Plain, DB-free. `make_node`/`make_term` are small inline factories — no
shared conftest, per the coder brief for this pair of test files.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from precis.taxonomy.config import CampaignConfig, load_campaign
from precis.taxonomy.select import (
    escape_rate,
    promote,
    select_entries,
    vocabulary_stability,
)
from precis.taxonomy.types import (
    Anchor,
    DimensionSpec,
    DiscoveredTerm,
    Half,
    HubCount,
    ListEntry,
    Mention,
    Snapshot,
    Status,
    TermNode,
    Thresholds,
)

_FREQUENCY = DimensionSpec(kind="si", si_vector="0,0,-1,0,0,0,0")
_VOLTAGE = DimensionSpec(kind="si", si_vector="-2,1,-3,-1,0,0,0")
_seq = itertools.count()


def make_node(
    key: str,
    *,
    dimension: DimensionSpec | None = _FREQUENCY,
    reference_state: str | None = None,
    convention: str | None = None,
    normalisation_basis: str | None = None,
    units_seen: tuple[tuple[str, int], ...] = (),
    papers: tuple[int, ...] = (1, 2, 3),
    halves: tuple[Half, ...] = ("A", "B"),
    status: Status = "proposed",
    notes: tuple[str, ...] = (),
) -> TermNode:
    return TermNode(
        key=key,
        label=key,
        dimension=dimension,
        reference_state=reference_state,
        convention=convention,
        normalisation_basis=normalisation_basis,
        units_seen=units_seen,
        paper_ref_ids=frozenset(papers),
        halves=frozenset(halves),
        mention_count=len(papers),
        status=status,
        notes=notes,
    )


def make_term(measurand: str, half: Half) -> DiscoveredTerm:
    n = next(_seq)
    anchor = Anchor(source_ref_id=n, start=0, end=1)
    mention = Mention(anchor=anchor, kind="value", literal="1")
    return DiscoveredTerm(mention=mention, measurand=measurand, half=half)


def make_sociology_config() -> CampaignConfig:
    """A minimal non-chemistry campaign, built directly (no YAML file) —
    AC7's proof that stages 3-4 carry no chemistry dependency."""
    snapshot = Snapshot(
        source="test-fixture",
        row_count=2,
        sha256="0" * 64,
        pulled_at="2026-01-01T00:00:00Z",
        text_field="text",
        ref_field="ref_id",
    )
    return CampaignConfig(
        campaign="sociology-fixture",
        config_version=1,
        snapshot=snapshot,
        snapshot_path=Path("."),
        thresholds=Thresholds(),
        currency_codes=frozenset({"USD"}),
        required_conditions_default=("year",),
        required_conditions_by_measurand={
            "unemployment-rate": ("population-definition",),
            "gdp-per-capita": ("population-definition",),
        },
    )


@pytest.fixture
def config() -> CampaignConfig:
    return load_campaign("norr-her-meta")


# --- promote: AC3 ------------------------------------------------------


def test_ac3_two_papers_stays_proposed():
    node = make_node("x", papers=(1, 2), halves=("A", "B"))
    (promoted,) = promote([node], Thresholds())
    assert promoted.status == "proposed"
    assert any("2 paper" in note for note in promoted.notes)


def test_ac3_three_papers_both_halves_one_dimension_becomes_systematic():
    node = make_node("x", papers=(1, 2, 3), halves=("A", "B"))
    (promoted,) = promote([node], Thresholds())
    assert promoted.status == "systematic"
    assert promoted.notes == ()


def test_ac3_three_papers_two_dimensions_same_key_stays_proposed():
    node_a = make_node("x", dimension=_FREQUENCY, papers=(1, 2, 3), halves=("A", "B"))
    node_b = make_node("x", dimension=_VOLTAGE, papers=(4, 5, 6), halves=("A", "B"))
    promoted = promote([node_a, node_b], Thresholds())
    assert all(n.status == "proposed" for n in promoted)
    assert all(
        any("distinct" in note and "dimensions" in note for note in n.notes)
        for n in promoted
    )


def test_require_both_halves_all_in_half_a_stays_proposed():
    node = make_node("x", papers=(1, 2, 3), halves=("A",))
    (promoted,) = promote([node], Thresholds())
    assert promoted.status == "proposed"
    assert any("halves" in note for note in promoted.notes)


def test_promote_no_dimension_never_systematic():
    node = make_node("x", dimension=None, papers=(1, 2, 3), halves=("A", "B"))
    (promoted,) = promote([node], Thresholds())
    assert promoted.status == "proposed"
    assert any("no dimension" in note for note in promoted.notes)


def test_promote_reference_state_split_does_not_count_as_multi_dimension():
    # Same key, same dimension, distinguished only by the structured
    # reference_state field (as normalise() produces it) — must NOT trip
    # require_single_dimension.
    node_a = make_node(
        "applied-potential",
        dimension=_VOLTAGE,
        reference_state="rhe",
        papers=(1, 2, 3),
        halves=("A", "B"),
    )
    node_b = make_node(
        "applied-potential",
        dimension=_VOLTAGE,
        reference_state="ag-agcl",
        papers=(4, 5, 6),
        halves=("A", "B"),
    )
    promoted = promote([node_a, node_b], Thresholds())
    assert all(n.status == "systematic" for n in promoted)


# --- vocabulary_stability --------------------------------------------------


def test_vocabulary_stability_identical_halves_is_one():
    terms = [
        make_term("x", "A"),
        make_term("x", "A"),
        make_term("y", "A"),
        make_term("x", "B"),
        make_term("x", "B"),
        make_term("y", "B"),
    ]
    assert vocabulary_stability(terms) == pytest.approx(1.0)


def test_vocabulary_stability_disjoint_is_zero():
    terms = [
        make_term("x", "A"),
        make_term("x", "A"),
        make_term("y", "B"),
        make_term("y", "B"),
    ]
    assert vocabulary_stability(terms) == pytest.approx(0.0)


def test_vocabulary_stability_partial_case_matches_hand_computed_jaccard():
    # A: x*3, y*1.  B: x*1, y*1, z*2.
    terms = (
        [make_term("x", "A")] * 3
        + [make_term("y", "A")]
        + [make_term("x", "B")]
        + [make_term("y", "B")]
        + [make_term("z", "B")] * 2
    )
    # intersection weight = min(3,1) + min(1,1) + min(0,2) = 1 + 1 + 0 = 2
    # union weight        = max(3,1) + max(1,1) + max(0,2) = 3 + 1 + 2 = 6
    assert vocabulary_stability(terms) == pytest.approx(2 / 6)


def test_vocabulary_stability_empty_is_one():
    assert vocabulary_stability([]) == 1.0


# --- select_entries -------------------------------------------------------


def test_select_entries_hub_threshold_29_rejected_30_admitted(config):
    node = make_node("x", status="systematic")
    below, rejected = select_entries(
        [node], {node.identity(): HubCount(total=29, by_side={})}, Thresholds(), config
    )
    assert below == ()
    assert rejected == (("x", "29 hubs < required 30"),)

    admitted, rejected2 = select_entries(
        [node], {node.identity(): HubCount(total=30, by_side={})}, Thresholds(), config
    )
    assert rejected2 == ()
    assert len(admitted) == 1
    assert admitted[0].key == "x"
    assert admitted[0].hub_count == 30


def test_select_entries_join_side_rejects_naming_the_side(config):
    node = make_node("x", status="systematic")
    hub_counts: dict[tuple[str, ...], HubCount] = {
        node.identity(): HubCount(total=40, by_side={"expt": 20, "dft": 14})
    }
    entries, rejected = select_entries(
        [node], hub_counts, Thresholds(), config, join_sides=("expt", "dft")
    )
    assert entries == ()
    assert len(rejected) == 1
    key, reason = rejected[0]
    assert key == "x"
    assert "dft" in reason
    assert "expt" not in reason


def test_select_entries_proposed_node_always_rejected(config):
    node = make_node("x", status="proposed")
    entries, rejected = select_entries(
        [node],
        {node.identity(): HubCount(total=1000, by_side={})},
        Thresholds(),
        config,
    )
    assert entries == ()
    assert rejected[0] == ("x", "not systematic")


def test_select_entries_never_converts(config):
    node = make_node("x", status="systematic")
    entries, _ = select_entries(
        [node], {node.identity(): HubCount(total=30, by_side={})}, Thresholds(), config
    )
    assert entries[0].convert is False


def test_select_entries_wires_canonical_unit_and_reference_state_from_node(config):
    node = make_node(
        "x",
        status="systematic",
        reference_state="rhe",
        normalisation_basis="per-catalyst-mass",
        units_seen=(("mV", 5), ("V", 1)),
    )
    entries, _ = select_entries(
        [node], {node.identity(): HubCount(total=30, by_side={})}, Thresholds(), config
    )
    entry = entries[0]
    assert entry.canonical_unit == "mV"
    assert entry.allowed_reference_states == ("rhe",)
    assert entry.normalisation_bases == ("per-catalyst-mass",)


def test_select_entries_canonical_unit_none_when_node_saw_no_unit(config):
    node = make_node("x", status="systematic")
    entries, _ = select_entries(
        [node], {node.identity(): HubCount(total=30, by_side={})}, Thresholds(), config
    )
    assert entries[0].canonical_unit is None
    assert entries[0].allowed_reference_states == ()
    assert entries[0].normalisation_bases == ()


def test_select_entries_looks_up_hub_counts_by_identity_not_bare_key(config):
    # Reviewer finding 2: two nodes share a bare key ("applied-potential")
    # but differ in reference_state, which is why they are two identities.
    # Keying the hub-count read by `node.key` would have both read whichever
    # of the two entries a plain-string dict happened to keep — here, wiring
    # by key would make either both admitted or both rejected together,
    # instead of the rhe side clearing its own 35-hub threshold while the
    # ag-agcl side is rejected on its own 10.
    rhe = make_node(
        "applied-potential",
        dimension=_VOLTAGE,
        reference_state="rhe",
        status="systematic",
    )
    ag_agcl = make_node(
        "applied-potential",
        dimension=_VOLTAGE,
        reference_state="ag-agcl",
        status="systematic",
    )
    hub_counts: dict[tuple[str, ...], HubCount] = {
        rhe.identity(): HubCount(total=35, by_side={}),
        ag_agcl.identity(): HubCount(total=10, by_side={}),
    }
    entries, rejected = select_entries([rhe, ag_agcl], hub_counts, Thresholds(), config)
    assert len(entries) == 1
    assert entries[0].allowed_reference_states == ("rhe",)
    assert entries[0].hub_count == 35
    assert rejected == (("applied-potential", "10 hubs < required 30"),)


def test_select_entries_wires_contributing_hubs_from_hub_count_ref_ids(config):
    # Reviewer finding 1: AC4 requires every frozen entry to list the hubs
    # that put it there; `contributing_hubs` must not be silently empty on
    # the real path (only a hand-built `ListEntry` fixture ever set it).
    node = make_node("x", status="systematic")
    entries, _ = select_entries(
        [node],
        {node.identity(): HubCount(total=30, by_side={}, ref_ids=(11, 12, 13))},
        Thresholds(),
        config,
    )
    assert entries[0].contributing_hubs == (11, 12, 13)
    assert entries[0].contributing_hubs != ()


# --- escape_rate ------------------------------------------------------------


def test_escape_rate_one_of_eight_unmatched_exceeds_default():
    entries = (
        ListEntry(key="a", label="A", dimension=DimensionSpec(kind="dimensionless")),
    )
    observed = ["a"] * 7 + ["totally-unmatched-key"]
    rate = escape_rate(observed, entries)
    assert rate == pytest.approx(0.125)
    assert rate > Thresholds().max_escape_rate


def test_escape_rate_matches_via_alias_too():
    entries = (
        ListEntry(
            key="onset-potential",
            label="Onset potential",
            dimension=DimensionSpec(kind="si", si_vector="-2,1,-3,-1,0,0,0"),
            aliases=("onset-potental",),
        ),
    )
    assert escape_rate(["onset-potential", "onset-potental"], entries) == pytest.approx(
        0.0
    )


# --- AC7: non-chemistry, stage 3/4 half ------------------------------------


def test_ac7_non_chemistry_measurands_pass_stage3_and_4():
    config = make_sociology_config()
    unemployment = make_node(
        "unemployment-rate", dimension=DimensionSpec(kind="dimensionless")
    )
    gdp = make_node(
        "gdp-per-capita",
        dimension=DimensionSpec(kind="currency", currency_code="USD", base_year=2015),
        papers=(7, 8, 9),
    )
    promoted = promote([unemployment, gdp], Thresholds())
    assert {n.status for n in promoted} == {"systematic"}

    hub_counts: dict[tuple[str, ...], HubCount] = {
        node.identity(): HubCount(total=40, by_side={}) for node in promoted
    }
    entries, rejected = select_entries(promoted, hub_counts, Thresholds(), config)
    assert rejected == ()
    by_key = {e.key: e for e in entries}
    assert by_key["unemployment-rate"].required_conditions == (
        "year",
        "population-definition",
    )
    assert by_key["unemployment-rate"].dimension.kind == "dimensionless"
    assert by_key["unemployment-rate"].dimension.si_vector is None  # never SI-zero
    assert by_key["gdp-per-capita"].dimension.kind == "currency"
    assert by_key["gdp-per-capita"].dimension.currency_code == "USD"
    assert by_key["gdp-per-capita"].dimension.base_year == 2015

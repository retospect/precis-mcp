"""Tests for the taxonomy-bootstrap orchestrator.

`run.py` had no dedicated coverage when a pre-ship review found four bugs in
it, every one a case where a stage's rule was correct and the wiring around it
quietly was not. These tests pin the wiring: what gets handed to the paid
stage, how multi-valued tags are split, and — the load-bearing one — that
evidence is attributed per node identity rather than per bare measurand key.
"""

from __future__ import annotations

import pytest

from precis.taxonomy import run
from precis.taxonomy.config import load_campaign
from precis.taxonomy.types import (
    Anchor,
    DimensionSpec,
    Mention,
    MentionKind,
    TermNode,
)


@pytest.fixture
def config():
    return load_campaign("norr-her-meta")


def _mention(ref_id: int, kind: MentionKind = "value", literal: str = "10") -> Mention:
    return Mention(
        anchor=Anchor(source_ref_id=ref_id, start=0, end=len(literal)),
        kind=kind,
        literal=literal,
    )


def _node(
    key: str,
    *,
    refs: tuple[int, ...],
    reference_state: str | None = None,
    si_vector: str = "0,0,-1,0,0,0,0",
) -> TermNode:
    return TermNode(
        key=key,
        label=key,
        dimension=DimensionSpec(kind="si", si_vector=si_vector),
        reference_state=reference_state,
        anchors=tuple(Anchor(source_ref_id=r, start=0, end=2) for r in refs),
    )


# --- what reaches the paid stage ------------------------------------------


def test_mentions_by_ref_keeps_only_value_mentions(config):
    """Reference-state and basis phrases must not reach discovery.

    Stage 1 emits them as their own kinds precisely because they have no
    measurand; handing one to the model asks it to invent a name and spends a
    call to produce a row that then pollutes stages 3 and 4.
    """
    mentions = (
        _mention(1, "value", "10"),
        _mention(1, "reference_state", "vs RHE"),
        _mention(1, "normalisation_basis", "cm-2"),
        _mention(2, "value", "20"),
    )
    grouped = run.mentions_by_ref(mentions)
    assert set(grouped) == {1, 2}
    assert [m.kind for m in grouped[1]] == ["value"]
    assert all(m.kind == "value" for items in grouped.values() for m in items)


def test_mentions_by_ref_drops_rows_left_with_nothing():
    """A row whose only mentions were qualifiers disappears entirely, so
    `discover` never makes a call for it."""
    grouped = run.mentions_by_ref((_mention(7, "reference_state", "vs SCE"),))
    assert grouped == {}


def test_mentions_by_ref_can_be_widened_explicitly():
    grouped = run.mentions_by_ref(
        (_mention(1, "reference_state", "vs RHE"),), kinds=("reference_state",)
    )
    assert len(grouped[1]) == 1


# --- multi-valued tag fields ----------------------------------------------


def test_split_sides_splits_pipe_joined_tags():
    """Production rows carry compound tags; 84 hubs in the snapshot are
    `side:h2-generation|side:stability-feed`. Treated as one opaque string
    such a hub counts toward neither real side, undercounting both against
    the per-side join threshold."""
    assert run.split_sides("mode:dft|mode:mixed") == ("mode:dft", "mode:mixed")


def test_split_sides_handles_single_empty_and_none():
    assert run.split_sides("mode:dft") == ("mode:dft",)
    assert run.split_sides(None) == ("unknown",)
    assert run.split_sides("") == ("unknown",)
    assert run.split_sides("  ") == ("unknown",)


def test_split_sides_is_deterministic_and_deduplicated():
    assert run.split_sides("b|a|b") == ("a", "b")


# --- paper attribution ----------------------------------------------------


def test_papers_by_ref_reads_the_configured_field(config):
    rows = [
        {"ref_id": 1, "paper_ref_ids": [100, 101]},
        {"ref_id": 2, "paper_ref_ids": []},
    ]
    assert run.papers_by_ref(rows, config) == {
        1: frozenset({100, 101}),
        2: frozenset(),
    }


def test_papers_by_ref_is_empty_when_rows_are_papers(config):
    """No `paper_field` means the scanned rows are themselves papers, so the
    row id already answers the question and no remap is wanted."""
    from dataclasses import replace

    rows_are_papers = replace(config, snapshot=replace(config.snapshot, paper_field=""))
    assert run.papers_by_ref([{"ref_id": 1}], rows_are_papers) == {}


def test_attribute_papers_collapses_many_hubs_of_one_paper():
    """The bug this prevents: the usage test promotes on ">=3 independent
    papers". Three hubs of a single paper is one paper's worth of evidence,
    but `normalise` fills `paper_ref_ids` from row ids, so uncorrected it
    reads as three. On this snapshot 1664 hubs map to 620 papers.
    """
    node = _node("tafel-slope", refs=(11, 12, 13))
    papers = {11: frozenset({500}), 12: frozenset({500}), 13: frozenset({500})}
    assert len(node.anchors) == 3, "three hubs of evidence going in"
    (out,) = run.attribute_papers([node], papers)
    assert out.paper_ref_ids == frozenset({500}), "one paper coming out"
    assert node.paper_ref_ids == frozenset(), "the input node is not mutated"
    assert node.anchors == out.anchors, "the evidence itself is untouched"


def test_attribute_papers_unions_across_hubs():
    node = _node("tafel-slope", refs=(11, 12))
    papers = {11: frozenset({500}), 12: frozenset({501, 502})}
    (out,) = run.attribute_papers([node], papers)
    assert out.paper_ref_ids == frozenset({500, 501, 502})


def test_attribute_papers_is_a_noop_without_a_paper_map():
    node = _node("tafel-slope", refs=(11,))
    assert run.attribute_papers([node], {}) == (node,)


# --- the load-bearing one: evidence per identity, not per key -------------


def test_hub_counts_does_not_merge_two_nodes_sharing_a_key(config):
    """AC5's TOF case and every reference-state split produce siblings under
    one key. Keyed by key, each sibling reads the union of the others'
    evidence — so a 2-hub variant clears a 30-hub threshold on its sibling's
    hubs, undoing the split stage 3 just made.
    """
    rhe = _node("applied-potential", refs=(1, 2, 3), reference_state="rhe")
    agcl = _node("applied-potential", refs=(4,), reference_state="ag-agcl")
    rows = [{"ref_id": r, "mode": "mode:dft"} for r in (1, 2, 3, 4)]

    counts = run.hub_counts([rhe, agcl], rows, config)

    # The collision is real: these two nodes DO share a bare key. That is what
    # makes keying by key wrong and this test non-vacuous.
    assert rhe.key == agcl.key
    assert rhe.identity() != agcl.identity()
    assert len(counts) == 2, "two entries, not one merged bucket"
    assert counts[rhe.identity()].total == 3
    assert counts[agcl.identity()].total == 1
    assert counts[rhe.identity()].ref_ids == (1, 2, 3)
    assert counts[agcl.identity()].ref_ids == (4,)


def test_hub_counts_carries_ref_ids_for_provenance(config):
    """AC4 requires every frozen entry to list its contributing hubs, and
    `select_entries` can only do that if the count carries them."""
    node = _node("tafel-slope", refs=(9, 7, 8))
    rows = [{"ref_id": r, "mode": "mode:dft"} for r in (7, 8, 9)]
    count = run.hub_counts([node], rows, config)[node.identity()]
    assert count.ref_ids == (7, 8, 9), "sorted, for a reproducible artifact"
    assert count.total == 3


def test_hub_counts_counts_hubs_not_mentions(config):
    """Three numbers in one sentence are one hub's worth of evidence."""
    node = TermNode(
        key="faradaic-efficiency",
        label="FE",
        dimension=DimensionSpec(kind="dimensionless"),
        anchors=tuple(Anchor(source_ref_id=5, start=i, end=i + 2) for i in (0, 10, 20)),
    )
    rows = [{"ref_id": 5, "mode": "mode:expt-electrochemical"}]
    assert run.hub_counts([node], rows, config)[node.identity()].total == 1


def test_hub_counts_splits_a_compound_side_into_both_sides(config):
    node = _node("tafel-slope", refs=(1,))
    rows = [{"ref_id": 1, "mode": "mode:dft|mode:mixed"}]
    by_side = run.hub_counts([node], rows, config)[node.identity()].by_side
    assert by_side == {"mode:dft": 1, "mode:mixed": 1}


def test_hub_counts_attaches_papers_from_the_snapshot(config):
    node = _node("tafel-slope", refs=(1, 2))
    rows = [
        {"ref_id": 1, "mode": "mode:dft", "paper_ref_ids": [900]},
        {"ref_id": 2, "mode": "mode:dft", "paper_ref_ids": [900, 901]},
    ]
    count = run.hub_counts([node], rows, config)[node.identity()]
    assert count.paper_ref_ids == frozenset({900, 901})
    assert count.total == 2, "two hubs, two papers — the counts are independent"


def test_hub_counts_tolerates_a_missing_side_field(config):
    node = _node("tafel-slope", refs=(1,))
    rows = [{"ref_id": 1}]
    assert run.hub_counts([node], rows, config)[node.identity()].by_side == {
        "unknown": 1
    }


def test_hub_counts_tolerates_an_unparseable_row_id(config):
    """A malformed row is skipped, not fatal — a snapshot dump with one bad
    line must not abort a whole run."""
    node = _node("tafel-slope", refs=(1,))
    rows: list[dict[str, object]] = [
        {"ref_id": "not-a-number"},
        {"ref_id": 1, "mode": "mode:dft"},
    ]
    assert run.hub_counts([node], rows, config)[node.identity()].total == 1

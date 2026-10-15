"""Interface contracts and insertion cones on se
(docs/backlog/design-state-core.md item 7a; :mod:`precis_se.contracts`,
plugin migration ``0021_se_contracts.sql``).

What these pin down: a contract stores up to six load components and an
absent one stays UNKNOWN (validate names it, never reads it as zero);
each component carries provenance through the shared core vocabulary,
with the spec's ``load_provenance`` enum as shorthand; the contract and a
block's insertion cone survive a save; and the cone intersection rejects
a set of perpendicular single-direction clips while accepting cones that
share a direction.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import contracts, persist
from precis_se.handler import SeHandler
from precis_se.ops import OpError, SeTree, apply_ops
from precis_se.validate import contract_findings


def _pair_ops() -> list[dict[str, Any]]:
    return [
        {"op": "add_block", "name": "base", "envelope": "box:w0.1d0.1h0.02"},
        {"op": "add_block", "name": "clip", "envelope": "box:w0.02d0.02h0.02"},
        {"op": "add_port", "block": "base", "name": "seat"},
        {"op": "add_port", "block": "clip", "name": "tab"},
        {"op": "connect", "a": "base.seat", "b": "clip.tab"},
    ]


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    return SeHandler(hub=hub)


def _ref_id(store: Store, slug: str) -> int:
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    return int(ref.id)


# ── the six-component contract ───────────────────────────────────────────


def test_absent_component_is_unknown_and_named_by_validate() -> None:
    tree = apply_ops(SeTree(), _pair_ops())
    apply_ops(
        tree,
        [
            {
                "op": "set_contract",
                "a": "base.seat",
                "b": "clip.tab",
                "loads": {"Fx": 0.0, "Fy": 0.0, "Fz": -40.0, "Mx": 0.0, "My": 0.0},
                "load_provenance": "derived_by_statics",
            }
        ],
    )
    (c,) = tree.connects
    assert c.contract is not None
    assert "Mz" not in c.contract["loads"]  # unknown, not 0.0
    assert c.contract["loads"]["Fx"] == 0.0  # a stated zero stays a zero
    assert c.contract["provenance"]["Fz"] == {
        "source": "derived",
        "fidelity": "analytic",
        "solver_id": "statics",
        "assumptions": [],
    }
    (finding,) = contract_findings(tree)
    assert finding.rule == "contract_component_unknown"
    assert "Mz unknown" in finding.detail
    assert "Fx" not in finding.detail.split("unknown")[0]


def test_merge_and_unset_and_explicit_provenance() -> None:
    tree = apply_ops(SeTree(), _pair_ops())
    edge = {"a": "base.seat", "b": "clip.tab"}
    apply_ops(
        tree,
        [
            {"op": "set_contract", **edge, "loads": {"Fz": -40.0}},
            {
                "op": "set_contract",
                **edge,
                "loads": {"Mz": 0.5},
                "provenance": {"Mz": {"source": "user_stated", "margin_origin": "2x"}},
            },
        ],
    )
    (c,) = tree.connects
    assert c.contract is not None
    assert c.contract["loads"] == {"Fz": -40.0, "Mz": 0.5}
    assert c.contract["provenance"]["Mz"]["margin_origin"] == "2x"
    # null makes a component unknown again (and drops its claim)
    apply_ops(tree, [{"op": "set_contract", **edge, "loads": {"Mz": None}}])
    assert c.contract == {"loads": {"Fz": -40.0}, "provenance": {}}
    with pytest.raises(OpError, match="Fq"):
        apply_ops(tree, [{"op": "set_contract", **edge, "loads": {"Fq": 1.0}}])
    with pytest.raises(OpError, match="load_provenance"):
        apply_ops(
            tree,
            [
                {
                    "op": "set_contract",
                    **edge,
                    "loads": {"Fx": 1.0},
                    "load_provenance": "x",
                }
            ],
        )
    with pytest.raises(OpError, match="unknown on this contract"):
        apply_ops(
            tree,
            [
                {
                    "op": "set_contract",
                    **edge,
                    "provenance": {"My": {"source": "user_stated"}},
                }
            ],
        )
    apply_ops(tree, [{"op": "set_contract", **edge, "clear": True}])
    assert c.contract is None
    assert contract_findings(tree) == []  # no contract = nothing declared


def test_contract_and_cone_survive_a_save(handler: SeHandler, store: Store) -> None:
    ops = _pair_ops() + [
        {
            "op": "set_contract",
            "a": "base.seat",
            "b": "clip.tab",
            "loads": {"Fz": -40.0},
            "load_provenance": "llm_assumed",
        },
        {
            "op": "set_insertion",
            "block": "clip",
            "direction": [0, 0, -2],
            "half_angle": "15 deg",
        },
    ]
    handler.put(id="clipped", text=json.dumps({"ops": ops}))
    handler.edit(id="clipped", ops=[{"op": "add_block", "name": "label"}])
    tree = persist.load_tree(store, _ref_id(store, "clipped"))
    (c,) = tree.connects
    assert c.contract == {
        "loads": {"Fz": -40.0},
        "provenance": {"Fz": {"source": "llm_assumed", "assumptions": []}},
    }
    cone = tree.blocks["clip"].insertion
    assert cone is not None and cone["direction"] == [0.0, 0.0, -1.0]
    assert math.isclose(cone["half_angle_rad"], math.radians(15.0))
    body = handler.get(id="clipped", view="contracts").body
    assert "| base.seat—clip.tab | ? | ? | -40 | ? | ? | ? | Fz llm_assumed |" in body
    assert "one assembly direction fits all" in body
    validate = handler.get(id="clipped", view="validate").body
    assert "contract_component_unknown" in validate
    with pytest.raises(BadInput, match="half_angle"):
        handler.edit(
            id="clipped",
            ops=[
                {
                    "op": "set_insertion",
                    "block": "clip",
                    "direction": [0, 0, 1],
                    "half_angle": 15,
                }
            ],
        )


# ── insertion cones ──────────────────────────────────────────────────────


def test_perpendicular_clips_have_no_common_direction() -> None:
    """The spec's case: individually satisfiable perpendicular clips that no
    one stroke can close."""
    tree = apply_ops(
        SeTree(),
        [
            {"op": "add_block", "name": "a"},
            {"op": "add_block", "name": "b"},
            {
                "op": "set_insertion",
                "block": "a",
                "direction": [0, 0, 1],
                "half_angle": "20 deg",
            },
            {
                "op": "set_insertion",
                "block": "b",
                "direction": [1, 0, 0],
                "half_angle": "20 deg",
            },
        ],
    )
    (finding,) = contract_findings(tree)
    assert finding.rule == "insertion_cones_disjoint"
    assert finding.severity == "error"
    assert finding.subject == "a, b"
    # Widening one cone until the two overlap clears it.
    apply_ops(
        tree,
        [
            {
                "op": "set_insertion",
                "block": "b",
                "direction": [1, 0, 0],
                "half_angle": "75 deg",
            }
        ],
    )
    assert contract_findings(tree) == []


def test_cone_axis_follows_the_block_rotation() -> None:
    """Directions are block-frame: a block rotated 90° about y turns its
    local +z cone onto the design's +x."""
    tree = apply_ops(
        SeTree(),
        [
            {"op": "add_block", "name": "a"},
            {"op": "add_block", "name": "b", "rot": "[0 90 0] deg"},
            {
                "op": "set_insertion",
                "block": "a",
                "direction": [1, 0, 0],
                "half_angle": "5 deg",
            },
            {
                "op": "set_insertion",
                "block": "b",
                "direction": [0, 0, 1],
                "half_angle": "5 deg",
            },
        ],
    )
    assert contract_findings(tree) == []


def test_common_direction_handles_a_lens_shaped_intersection() -> None:
    """Three cones whose common region contains none of their axes — the
    answer is a boundary crossing, found exactly. The region's centre,
    (1, 1, 1)/√3, sits 54.7° from each axis: 58° cones meet, 50° don't."""
    h = math.radians(58.0)
    cones = [
        ([1.0, 0.0, 0.0], h),
        ([0.0, 1.0, 0.0], h),
        ([0.0, 0.0, 1.0], h),
    ]
    d = contracts.common_direction(cones)
    assert d is not None
    for axis, half in cones:
        assert sum(x * y for x, y in zip(d, axis, strict=True)) >= math.cos(half) - 1e-6
    tight = [(a, math.radians(50.0)) for a, _ in cones]
    assert contracts.common_direction(tight) is None

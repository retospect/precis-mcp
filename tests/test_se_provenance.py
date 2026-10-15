"""Per-number provenance on se (docs/backlog/design-state-core.md item 4;
plugin migration ``0019_se_provenance.sql``, :mod:`precis.design.provenance`).

What these pin down: a solver that writes a number stamps its provenance
(formfind → the pose facet), the claim survives a re-save and a branch
copy, a manual overwrite restates it instead of leaving a stale solver
claim, ops take an explicit ``provenance=`` validated through the shared
core vocabulary, and the margin audit is a query that returns rows.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import persist
from precis_se.handler import SeHandler
from precis_se.ops import OpError, SeTree, apply_ops

_TIE = {"tension_capacity": 100.0, "compression_capacity": 0.0}


def _tripod_ops() -> list[dict[str, Any]]:
    """Three fully fixed anchors and one free apex on three ties — the
    smallest design formfind moves."""
    nodes = {
        "b0": [0.0, 0.0, 0.0],
        "b1": [2.0, 0.0, 0.0],
        "b2": [1.0, 2.0, 0.0],
        "apex": [0.0, 0.0, 5.0],
    }
    ops: list[dict[str, Any]] = []
    for name, pose in nodes.items():
        ops.append({"op": "add_block", "name": name, "pose": pose, "pose_unit": "m"})
        ops.append({"op": "add_port", "block": name, "name": "pin"})
    for anchor in ("b0", "b1", "b2"):
        ops.append(
            {
                "op": "connect",
                "a": f"{anchor}.pin",
                "b": "apex.pin",
                "joint": {"class": "axial", "params": dict(_TIE)},
            }
        )
        ops.append({"op": "set_load", "block": anchor, "fixed": True})
    return ops


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    return SeHandler(hub=hub)


def _ref_id(store: Store, slug: str) -> int:
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    return int(ref.id)


def test_formfind_stamps_solver_provenance_that_survives_a_resave(
    handler: SeHandler, store: Store
) -> None:
    handler.put(id="tripod", text=json.dumps({"ops": _tripod_ops()}))
    handler.edit(id="tripod", ops=[{"op": "formfind", "move": "all"}])
    # An unrelated edit re-saves every row (retire-all/reinsert-all).
    handler.edit(id="tripod", ops=[{"op": "add_block", "name": "label"}])
    tree = persist.load_tree(store, _ref_id(store, "tripod"))
    entry = tree.blocks["apex"].provenance["pose"]
    assert entry["source"] == "solver"
    assert entry["fidelity"] == "analytic"
    assert entry["solver_id"] == "structsolve.form_find"
    assert "force-density equilibrium" in entry["assumptions"]
    # Every block formfind solved carries it; the fixed anchors weren't solved.
    solved = {n for n, b in tree.blocks.items() if b.origins.get("pose") == "proposed"}
    assert solved == {"apex"}
    assert all("pose" in tree.blocks[n].provenance for n in solved)
    assert tree.blocks["b0"].provenance == {}
    body = handler.get(id="tripod", view="provenance").body
    assert "| apex | pose | solver | analytic | structsolve.form_find |" in body


def test_manual_overwrite_restates_a_solver_claim() -> None:
    tree = apply_ops(SeTree(), _tripod_ops())
    apply_ops(tree, [{"op": "formfind", "move": "all"}])
    apply_ops(
        tree,
        [
            {
                "op": "set_pose",
                "block": "apex",
                "pose": [1.0, 0.5, 0.0],
                "pose_unit": "m",
            }
        ],
    )
    # No origin given: the facet keeps its 'proposed' stamp, so the number
    # is now an assumed one, no longer the solver's.
    assert tree.blocks["apex"].provenance["pose"] == {
        "source": "llm_assumed",
        "assumptions": [],
    }
    apply_ops(
        tree,
        [
            {
                "op": "set_pose",
                "block": "apex",
                "pose": [1.0, 0.4, 0.0],
                "pose_unit": "m",
                "origin": "user",
            }
        ],
    )
    assert tree.blocks["apex"].provenance["pose"]["source"] == "user_stated"
    # An unclaimed facet stays unclaimed — no default claim is invented.
    apply_ops(
        tree,
        [{"op": "set_pose", "block": "b0", "pose": [0.0, 0.0, 0.1], "pose_unit": "m"}],
    )
    assert "pose" not in tree.blocks["b0"].provenance


def test_explicit_provenance_is_validated_by_the_core_vocabulary() -> None:
    tree = apply_ops(SeTree(), [{"op": "add_block", "name": "hub"}])
    apply_ops(
        tree,
        [
            {
                "op": "set_envelope",
                "block": "hub",
                "envelope": "cyl:r0.008h0.03",
                "provenance": {
                    "source": "library",
                    "fidelity": "template",
                    "library_version": "skf-2026.3",
                },
            }
        ],
    )
    assert tree.blocks["hub"].provenance["envelope"]["library_version"] == "skf-2026.3"
    with pytest.raises(OpError, match="fidelity"):
        apply_ops(
            tree,
            [
                {
                    "op": "set_provenance",
                    "block": "hub",
                    "facet": "envelope",
                    "provenance": {"source": "solver"},
                }
            ],
        )
    with pytest.raises(OpError, match="facet"):
        apply_ops(
            tree,
            [
                {
                    "op": "set_provenance",
                    "block": "hub",
                    "facet": "colour",
                    "provenance": {"source": "user_stated"},
                }
            ],
        )
    # Clearing an envelope drops the claim about it.
    apply_ops(tree, [{"op": "set_envelope", "block": "hub", "envelope": None}])
    assert "envelope" not in tree.blocks["hub"].provenance


def test_margin_audit_returns_every_number_with_a_margin_origin(
    handler: SeHandler, store: Store
) -> None:
    handler.put(
        id="seat",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "hub", "envelope": "cyl:r0.008h0.03"},
                    {
                        "op": "add_measure",
                        "block": "hub",
                        "name": "bore",
                        "value": 0.00798,
                        "provenance": {
                            "source": "derived",
                            "fidelity": "analytic",
                            "margin_origin": "press-fit interference 20 um",
                        },
                    },
                    {"op": "add_measure", "block": "hub", "name": "od", "value": 0.016},
                    {
                        "op": "set_provenance",
                        "block": "hub",
                        "facet": "envelope",
                        "provenance": {
                            "source": "user_stated",
                            "margin_origin": "10% clearance allowance",
                        },
                    },
                ]
            }
        ),
    )
    ref_id = _ref_id(store, "seat")
    audit = persist.provenance_rows(store, ref_id=ref_id, margin_only=True)
    assert [(r["block"], r["param"]) for r in audit] == [
        ("hub", "envelope"),
        ("hub", "measure:bore"),
    ]
    # Across every design too — the audit is a store query, not a view.
    assert any(
        r["ref_id"] == ref_id for r in persist.provenance_rows(store, margin_only=True)
    )
    body = handler.get(id="seat", view="provenance", args={"margin": True}).body
    assert "margin audit (2)" in body
    assert "press-fit interference 20 um" in body
    # set_measure can attach one later; a malformed one is refused loudly.
    handler.edit(
        id="seat",
        ops=[
            {
                "op": "set_measure",
                "block": "hub",
                "name": "od",
                "provenance": {"source": "measured", "margin_origin": "wear"},
            }
        ],
    )
    assert len(persist.provenance_rows(store, ref_id=ref_id, margin_only=True)) == 3
    with pytest.raises(BadInput):
        handler.edit(
            id="seat",
            ops=[
                {
                    "op": "set_measure",
                    "block": "hub",
                    "name": "od",
                    "provenance": {"source": "guessed"},
                }
            ],
        )


def test_branch_copy_carries_provenance(handler: SeHandler, store: Store) -> None:
    handler.put(id="tripod", text=json.dumps({"ops": _tripod_ops()}))
    handler.edit(id="tripod", ops=[{"op": "formfind", "move": "all"}])
    handler.edit(
        id="tripod",
        ops=[{"op": "pin", "as": "tripod-b", "reason": "keep the solved apex"}],
    )
    tree = persist.load_tree(store, _ref_id(store, "tripod-b"))
    assert tree.blocks["apex"].provenance["pose"]["source"] == "solver"

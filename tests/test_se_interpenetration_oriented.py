"""``undeclared_interpenetration`` over rotated cylinders (gr450524 #4).

The posed envelope is built through the block's full ``rot`` and the
narrow phase is the kernel's SDF clearance, so the AABB broad phase can
only ever over-include. These tests pin that: pairs whose AABBs overlap
but whose oriented solids do not stay clean, genuinely overlapping rotated
solids are still flagged, and a bolt parented under a rotated member is
posed through the composed (world) placement, not the raw local one.
"""

from __future__ import annotations

import math
from typing import Any

from precis_se.ops import SeTree, apply_ops
from precis_se.validate import envelope_overlaps

_Q = math.pi / 4
_H = math.pi / 2
_ROD = "cyl:r0.02h0.1"  # base-at-pose, +z spanning 0..h


def _pair(a: dict[str, Any], b: dict[str, Any]) -> list[tuple[str, str, float]]:
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "a", "envelope": _ROD, **a},
            {"op": "add_block", "name": "b", "envelope": _ROD, **b},
        ],
    )
    return envelope_overlaps(tree, budget_s=None)[0]


def test_parallel_45deg_rods_with_overlapping_aabbs_do_not_interpenetrate() -> None:
    # perpendicular separation 0.06 > 2r = 0.04, yet the AABBs overlap
    assert (
        _pair(
            {"pose": [0, 0, 0], "rot": [0, _Q, 0]},
            {"pose": [0.0424, 0, -0.0424], "rot": [0, _Q, 0]},
        )
        == []
    )


def test_parallel_45deg_rods_that_really_overlap_are_flagged() -> None:
    # perpendicular separation 0.03 < 2r
    out = _pair(
        {"pose": [0, 0, 0], "rot": [0, _Q, 0]},
        {"pose": [0.0212, 0, -0.0212], "rot": [0, _Q, 0]},
    )
    assert [(a, b) for a, b, _ in out] == [("a", "b")]
    assert out[0][2] < 0


def test_rods_rotated_about_different_axes_do_not_interpenetrate() -> None:
    # a spans x -0.1..0 (rot about y); b spans y 0.04..0.14 (rot about x)
    assert (
        _pair(
            {"pose": [0, 0, 0], "rot": [0, -_H, 0]},
            {"pose": [0.05, 0.04, 0], "rot": [-_H, 0, 0]},
        )
        == []
    )


def test_bolts_under_cranks_use_composed_placement() -> None:
    """The gr450524 geometry: a bolt parented under each crank (pose
    relative to the crank, rotated 180 deg about x). At the crank ends
    (y = +-0.1) the bolts are 0.2 m apart and clear of a wheel at
    y 0..0.045; read as raw world poses they would coincide."""
    tree = SeTree()
    crank = "box:w0.17d0.02h0.03"
    bolt = "cyl:r0.002h0.02"
    apply_ops(
        tree,
        [
            {
                "op": "add_block",
                "name": "crank_l",
                "envelope": crank,
                "pose": [0, 0.1, 0.254],
            },
            {
                "op": "add_block",
                "name": "crank_r",
                "envelope": crank,
                "pose": [0, -0.1, 0.254],
            },
            {
                "op": "add_block",
                "name": "bolt_l",
                "parent": "crank_l",
                "envelope": bolt,
                "pose": [0, 0, 0.016],
                "rot": [math.pi, 0, 0],
            },
            {
                "op": "add_block",
                "name": "bolt_r",
                "parent": "crank_r",
                "envelope": bolt,
                "pose": [0, 0, 0.016],
                "rot": [math.pi, 0, 0],
            },
            {
                "op": "add_block",
                "name": "wheel",
                "envelope": "cyl:r0.254h0.045",
                "pose": [0, 0.0225, 0.254],
                "rot": [_H, 0, 0],
            },
        ],
    )
    pairs = {(a, b) for a, b, _ in envelope_overlaps(tree, budget_s=None)[0]}
    assert ("bolt_l", "bolt_r") not in pairs
    assert not [p for p in pairs if "wheel" in p and any("bolt" in n for n in p)]

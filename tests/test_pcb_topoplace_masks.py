"""Vector routing masks (topoplace.masks): the two-kernel pipeline.

Small-fixture tests on the synthetic design: SMD pads seed their mount
layer only, TH pads every layer, via-prevent is board-level and bounded,
pad-bank spines come out as one immobile wire per collinear bank, and
the opening filter fuses only sub-kernel gaps.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import numpy as np
import pytest
from shapely.geometry import Polygon

from precis.pcb.topoplace.masks import (
    _collinear_banks,
    _opening,
    build_masks,
)
from tests.fixtures.pcb.topo_synth import synth_design

_GROWTH = 0.15 + 0.15 / 2.0  # min_clearance + min_wire/2 defaults


def _design(n_parts: int = 8, pad_mm: float = 1.0):
    d, _inst, _nets = synth_design(
        seed=3, n_parts=n_parts, n_nets=3, n_multinet=0, pad_mm=pad_mm
    )
    return d


def test_masks_smd_pads_block_mount_layer_only() -> None:
    d = _design()
    masks = build_masks(d, d.pose0, n_layers=4)
    assert len(masks.layers) == 4
    assert masks.layers[0].route_block, "top SMD pads must block F.Cu"
    # top-SMD pads seed F.Cu only: inners/B.Cu carry no route-block rings
    assert masks.layers[3].route_block == ()
    assert masks.layers[1].route_block == ()
    # ... and no spines either
    assert len(masks.layers[0].spines) == 8  # one bank per part
    assert masks.layers[3].spines == ()


def test_masks_th_pads_block_every_layer() -> None:
    d = _design()
    th = np.full(len(d.pin_side), 2, dtype=np.int8)
    d = dataclasses.replace(d, pin_side=th)
    masks = build_masks(d, d.pose0, n_layers=4)
    for lm in masks.layers:
        assert lm.route_block, "TH pads obstruct every layer"
        assert len(lm.spines) == 8


def test_masks_via_prevent_is_board_level_and_bounded() -> None:
    d = _design(n_parts=16, pad_mm=1.2)  # dense enough to bite
    masks = build_masks(d, d.pose0, n_layers=4)
    assert masks.via_prevent, "via-prevent rings exist"
    assert 0.0 < masks.layers[0].via_prevent_free < 1.0
    assert 0.0 < masks.layers[0].route_block_free < 1.0
    # the barrel keep-out is at least as hungry as the route block
    assert masks.layers[0].via_prevent_free <= masks.layers[0].route_block_free + 1e-9


def test_masks_spine_geometry() -> None:
    d = _design()
    (lm,) = [build_masks(d, d.pose0, n_layers=4).layers[0]]
    spine = next(s for s in lm.spines if s.refdes == "U1")
    cx, cy = float(d.pose0[0, 0]), float(d.pose0[0, 1])
    (x0, y0), (x1, y1) = spine.pts
    assert (x0, y0) == pytest.approx((cx - 0.5, cy))
    assert (x1, y1) == pytest.approx((cx + 0.5, cy))
    assert spine.width_mm == pytest.approx(1.0 + 2 * _GROWTH)


def test_collinear_banks_split_by_gap_and_axis() -> None:
    row = [(0.0, 0.0, 1.0, 1.0), (1.0, 0.0, 1.0, 1.0), (6.0, 0.0, 1.0, 1.0)]
    banks = _collinear_banks(row)
    assert [len(b) for b in banks] == [2, 1]  # 6 mm gap splits the bank
    row_y = [(0.0, 0.0, 1.0, 1.0), (0.0, 1.0, 1.0, 1.0)]
    assert [len(b) for b in _collinear_banks(row_y)] == [1, 1]


def test_opening_absorbs_sub_kernel_necks() -> None:
    def joined(neck_w: float) -> Any:
        left = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
        right = Polygon(
            [(1 + neck_w + 1, 0), (3 + neck_w, 0), (3 + neck_w, 1), (1 + neck_w + 1, 1)]
        )
        return left.union(
            Polygon(
                [
                    (1, (1 - neck_w) / 2),
                    (2 + neck_w, (1 - neck_w) / 2),
                    (2 + neck_w, (1 + neck_w) / 2),
                    (1, (1 + neck_w) / 2),
                ]
            )
        ).union(right)

    thin = _opening(joined(0.1), 0.15)  # 0.1 neck < 0.3 diameter: absorbed
    assert len(getattr(thin, "geoms", [thin])) == 2
    wide = _opening(joined(0.5), 0.15)  # 0.5 neck survives the opening
    assert len(getattr(wide, "geoms", [wide])) == 1

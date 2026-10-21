"""Slice-1 problem state, torch-free (spec §State, fixed pads only).

A :class:`PrProblem` is the structure-of-arrays view of a board the
prototype solves: static obstacles as core+radius shapes with a layer
set, two-pin wires as ratsnest edges (no Steiner points), and the rule
context every ``required(a, b, layer)`` answer is resolved against.
Everything here is numpy; torch tensors are built downstream (the host
venv has no torch).

Pads become cores by shape: circle → a degenerate segment (a point),
rect → an oriented box, obround → the end-centres segment, polygon →
its edges (containment not modelled — a wire slipping inside a large
polygon pad is a known prototype gap, gated by the slice-1 fixtures
which have none). A drilled pad's land occupies EVERY layer, matching
``drc._clearance_items``'s own rule; a non-plated hole would too
(drill-to-copper), but slice 1 has none.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from precis.pcb import realize, rules
from precis.pcb.ir import PcbIR, pin_point


@dataclass(frozen=True, slots=True)
class Obstacles:
    """Static copper as core+radius rows. ``seg_*`` rows are segment
    cores (a point is a degenerate segment); ``box_*`` rows are oriented
    rectangles (``cs`` = (cos θ, sin θ) of the box's x axis), needed
    because a rect pad's corners must block a wire, not just its
    skeleton. ``net`` is the IR net id, −1 for unnetted copper;
    ``lmask`` the bitmask over :attr:`PrProblem.layers` the row sits on.
    """

    seg_xy: np.ndarray  # (S, 4) x0, y0, x1, y1
    seg_r: np.ndarray  # (S,)
    seg_net: np.ndarray  # (S,) int64
    seg_lmask: np.ndarray  # (S,) uint8
    box_c: np.ndarray  # (B, 2) centre
    box_hw: np.ndarray  # (B, 2) half extents
    box_cs: np.ndarray  # (B, 2) cos, sin of the box's x axis
    box_net: np.ndarray  # (B,) int64
    box_lmask: np.ndarray  # (B,) uint8


@dataclass(frozen=True, slots=True)
class WireSpec:
    """One two-pin ratsnest edge. ``a_lmask``/``b_lmask`` are the layer
    sets the terminals' copper occupies — an SMD pad pins its span to
    its own side, a through-hole pin admits every layer."""

    seg_id: int
    net_id: int
    a: tuple[float, float]
    b: tuple[float, float]
    a_lmask: int
    b_lmask: int
    width_mm: float


@dataclass(frozen=True, slots=True)
class PrProblem:
    """The whole slice-1 instance. Distances in mm; ``layers`` is the
    stackup's copper layer names in order (index = layer id)."""

    layers: tuple[str, ...]
    outline: np.ndarray  # (V, 2) convex polygon, CCW
    edge_mm: float  # copper-to-board-edge clearance
    via_dia_mm: float
    via_drill_mm: float
    wires: tuple[WireSpec, ...]
    obs: Obstacles
    ctx: rules.RuleContext
    net_rules: dict[str, rules.NetRules]
    net_name: tuple[str, ...]  # net id -> name, index-aligned with the IR

    @property
    def n_layers(self) -> int:
        return len(self.layers)

    def required_mm(self, net_a: int, net_b: int, layer: int) -> float:
        """The warn-tier spacing net ``net_a`` owes net ``net_b`` on
        ``layer`` — ``rules.required`` resolved (net class, pair rules,
        voltage tiers). Same net merges (0)."""
        if net_a >= 0 and net_a == net_b:
            return 0.0
        name_a = self.net_name[net_a] if net_a >= 0 else ""
        name_b = self.net_name[net_b] if net_b >= 0 else ""
        req = rules.required(
            rules.Owner(name_a), rules.Owner(name_b), self.layers[layer], self.ctx
        )
        return float(req.warn_mm if req.warn_mm is not None else req.error_mm)


def _pad_lmask(pad: dict[str, Any], layer_index: dict[str, int]) -> int:
    """Bitmask of the copper layers one pad row occupies: drilled (plated)
    pads span every layer, an SMD pad only its side's outer layer."""
    if pad.get("drill"):
        return (1 << len(layer_index)) - 1
    bit = layer_index.get(str(pad.get("layer")))
    return 0 if bit is None else (1 << bit)


def _obstacle_rows(
    pad: dict[str, Any],
    net_id: int,
    lmask: int,
) -> tuple[list[tuple[float, float, float, float, float]], list[tuple[float, ...]]]:
    """One gerber pad row as ``(seg cores, box cores)`` — the slice-1
    core+radius table. Seg rows carry ``(x0, y0, x1, y1, r)``; a box row
    is ``(cx, cy, hw, hh, theta)``."""
    segs: list[tuple[float, float, float, float, float]] = []
    boxes: list[tuple[float, ...]] = []
    shape = str(pad.get("shape", "circle"))
    x, y = float(pad["x"]), float(pad["y"])
    w = float(pad.get("w", 0.0))
    h = float(pad.get("h", w))
    if w <= 0 or h <= 0:
        return segs, boxes
    if shape == "polygon":
        poly = pad.get("poly") or []
        for i, p in enumerate(poly):
            q = poly[(i + 1) % len(poly)]
            segs.append((float(p[0]), float(p[1]), float(q[0]), float(q[1]), 0.0))
        return segs, boxes
    if shape == "circle":
        segs.append((x, y, x, y, w / 2.0))
    elif shape == "rect":
        boxes.append((x, y, w / 2.0, h / 2.0, 0.0))
    elif shape == "obround":
        if w == h:
            segs.append((x, y, x, y, w / 2.0))
        elif w > h:
            half = (w - h) / 2.0
            segs.append((x - half, y, x + half, y, h / 2.0))
        else:
            half = (h - w) / 2.0
            segs.append((x, y - half, x, y + half, w / 2.0))
    else:
        raise ValueError(f"unknown pad shape {shape!r}")
    # A slotted hole's barrel: the slot's own stadium, on every layer.
    if pad.get("slot"):
        (ax, ay), (bx, by) = pad["slot"][:2]
        segs.append(
            (float(ax), float(ay), float(bx), float(by), float(pad["drill"]) / 2.0)
        )
    return segs, boxes


def build_problem(
    ir: PcbIR,
    *,
    outline: list[list[float]],
    config: realize.RealizeConfig,
    footprints: dict[str, dict[str, Any]] | None = None,
) -> PrProblem:
    """The slice-1 problem off a built IR: every 2-pin segment a wire,
    every pad a static shape, rules resolved the realize way."""
    layers = tuple(str(l["name"]) for l in ir.stackup)
    layer_index = {name: i for i, name in enumerate(layers)}
    class_rules = config.class_rules or {}
    net_name = tuple(str(ir.net_name[i]) for i in range(ir.n_nets))
    net_rules = {
        name: rules.resolve_net_rules(
            str(ir.net_class[i]),
            layer_is_outer=True,
            fab_caps=config.fab_caps,
            overrides=class_rules.get(str(ir.net_class[i])),
        )
        for i, name in enumerate(net_name)
    }
    ctx = rules.RuleContext(
        fab_caps=config.fab_caps,
        net_rules=net_rules,
        outer_layers=frozenset({layers[0], layers[-1]}),
        coated=True,
    )
    pads = realize.pads_for_ir(ir, list(layers), footprints)
    name_to_net = {name: i for i, name in enumerate(net_name)}
    seg_rows: list[tuple[float, float, float, float, float]] = []
    box_rows: list[tuple[float, ...]] = []
    seg_meta: list[tuple[int, int]] = []
    box_meta: list[tuple[int, int]] = []
    pad_by_pin: dict[tuple[str, str], dict[str, Any]] = {}
    for pad in pads:
        net_id = name_to_net.get(str(pad.get("net") or ""), -1)
        lmask = _pad_lmask(pad, layer_index)
        segs, boxes = _obstacle_rows(pad, net_id, lmask)
        seg_rows += segs
        seg_meta += [(net_id, lmask)] * len(segs)
        box_rows += boxes
        box_meta += [(net_id, lmask)] * len(boxes)
        pad_by_pin.setdefault((str(pad.get("refdes")), str(pad.get("pin"))), pad)
    geoms = realize.pad_geometry(ir, footprints)

    def terminal_lmask(pin_id: int) -> int:
        inst = int(ir.pin_instance[pin_id])
        pad = pad_by_pin.get((str(ir.instance_refdes[inst]), str(ir.pin_label[pin_id])))
        if pad is not None:
            return _pad_lmask(pad, layer_index)
        # No pad row (unplaced or net-less): a drilled pad still sits on
        # every layer, else the instance's own side — same rule pads use.
        if geoms[pin_id].drill_mm:
            return (1 << len(layers)) - 1
        return 1 << (len(layers) - 1 if bool(ir.inst_bottom[inst]) else 0)

    wires: list[WireSpec] = []
    for seg_id in range(ir.n_segments):
        pa, pb = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
        a_pt, b_pt = pin_point(ir, pa), pin_point(ir, pb)
        if a_pt is None or b_pt is None:
            continue
        net_id = int(ir.seg_net[seg_id])
        wires.append(
            WireSpec(
                seg_id=seg_id,
                net_id=net_id,
                a=a_pt,
                b=b_pt,
                a_lmask=terminal_lmask(pa),
                b_lmask=terminal_lmask(pb),
                width_mm=net_rules[net_name[net_id]].track_width_mm,
            )
        )
    # One via geometry for the prototype: the class's resolved figure.
    diams = {(r.via_dia_mm, r.via_drill_mm) for r in net_rules.values()}
    via_dia_mm = max(d[0] for d in diams if d[0] is not None) if diams else 0.0
    via_drill_mm = max(d[1] for d in diams if d[1] is not None) if diams else 0.0
    obs = Obstacles(
        seg_xy=np.asarray(seg_rows, dtype=np.float64)[:, :4]
        if seg_rows
        else np.zeros((0, 4)),
        seg_r=np.asarray([r[4] for r in seg_rows], dtype=np.float64)
        if seg_rows
        else np.zeros(0),
        seg_net=np.asarray([m[0] for m in seg_meta], dtype=np.int64)
        if seg_rows
        else np.zeros(0, dtype=np.int64),
        seg_lmask=np.asarray([m[1] for m in seg_meta], dtype=np.uint8)
        if seg_rows
        else np.zeros(0, dtype=np.uint8),
        box_c=np.asarray([r[:2] for r in box_rows], dtype=np.float64)
        if box_rows
        else np.zeros((0, 2)),
        box_hw=np.asarray([r[2:4] for r in box_rows], dtype=np.float64)
        if box_rows
        else np.zeros((0, 2)),
        box_cs=np.asarray(
            [(math.cos(r[4]), math.sin(r[4])) for r in box_rows], dtype=np.float64
        )
        if box_rows
        else np.zeros((0, 2)),
        box_net=np.asarray([m[0] for m in box_meta], dtype=np.int64)
        if box_rows
        else np.zeros(0, dtype=np.int64),
        box_lmask=np.asarray([m[1] for m in box_meta], dtype=np.uint8)
        if box_rows
        else np.zeros(0, dtype=np.uint8),
    )
    return PrProblem(
        layers=layers,
        outline=np.asarray(outline, dtype=np.float64),
        edge_mm=realize._board_edge_min_mm(config),
        via_dia_mm=via_dia_mm,
        via_drill_mm=via_drill_mm,
        wires=tuple(wires),
        obs=obs,
        ctx=ctx,
        net_rules=net_rules,
        net_name=net_name,
    )

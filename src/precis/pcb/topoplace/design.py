"""The ``TopoDesign`` input shape — one immutable, vectorised snapshot of
everything the topo-place kernel reads, built once from the IR.

Why a separate shape instead of reading :class:`~precis.pcb.ir.PcbIR`
directly: the IR carries every fidelity the optimizer/realizer cascade
needs (dirty masks, segment layers, rotation darts); the placer needs a
small, dense, numpy-only view it can hold next to a ``(N, 4)`` pose
array and re-derive nothing. This module is that view plus
:func:`design_from_ir`, the one sanctioned IR → snapshot bridge.

Pin positions stay FOOTPRINT-LOCAL (``pin_xy``, pre-rotation,
pre-mirror — the same frame :attr:`PcbIR.pin_dx` uses); a consumer
applies the pose itself, mirror-before-rotate like
:func:`precis.pcb.pose.rel_to_world`, so placer and pairmap can never
disagree about where a pad went.

``flex``/``swap_groups`` are the 1b declarations' slots — the shape is
fixed here so 1b only fills semantics, never reshapes the input.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from precis.pcb import ratsnest
from precis.pcb.ir import NO_NET, PcbIR

#: ``freedoms`` bitset values (Decision 17's "allowed freedoms").
#: ROTATE — quarter-turn moves are legal; BACK_SIDE — the part may be
#: flipped to the bottom (mirrors its pin x, per the pose convention).
FREEDOM_ROTATE = np.uint8(1)
FREEDOM_BACK_SIDE = np.uint8(2)


@dataclass(frozen=True, slots=True)
class FlexSlot:
    """One ``part.pin(any_of=[...])`` declaration (1b): the solver picks
    ONE pin out of ``pins`` for the slot's signal. ``name`` feeds the
    firmware pin map (Decision 11); ``net`` is the net the slot's wire
    belongs to, filled by ``net.tie`` at declaration time."""

    part: int
    pins: tuple[int, ...]
    net: int
    name: str | None = None


@dataclass(frozen=True, slots=True)
class SwapGroup:
    """A declared swap group (1b, Decision 13): ``members`` are pin ids
    that may permute among themselves (resistor packs), or — when
    ``tuple_`` — equal-length tuples moved as units (buffer channels:
    ``in_i``/``out_i`` move together)."""

    part: int
    members: tuple[tuple[int, ...], ...]
    tuple_: bool = False


@dataclass(frozen=True, slots=True)
class ObjectiveShares:
    """The named share vector over the proxy's objective terms
    (Decision 18 / pcb-intent-api §Weights): ``crossings`` is the
    straight-airwire crossing count, ``ratsnest`` the total airwire
    length in mm (the term name pcb-intent-api Decision 3 fixes).
    Shares are normalised to sum 1; :class:`~.proxy.ProxyState` applies
    them reference-normalised at its seed pose, so a share means "this
    fraction of J at the seed", not a raw unit weight. Later slices add
    the terms only their rungs can price (``vias``, ``area``, …)."""

    crossings: float = 0.8
    ratsnest: float = 0.2

    def __post_init__(self) -> None:
        total = self.crossings + self.ratsnest
        if total <= 0:
            raise ValueError("objective shares must not all be zero")
        object.__setattr__(self, "crossings", self.crossings / total)
        object.__setattr__(self, "ratsnest", self.ratsnest / total)


@dataclass(frozen=True, slots=True)
class TopoDesign:
    """The placer's whole world (spec §1 input shape). Index = part id
    everywhere; ``parts[i]`` is the refdes. ``pose0`` rows are
    ``(x, y, rot_quarter, side)`` — ``rot_quarter`` 0..3 is the authored
    rotation snapped to the quarter turn the mover uses (the board
    frame's clockwise-from-north convention;
    :func:`precis.pcb.pose.rotate_cw`), ``side`` 0 top / 1 bottom.
    ``nan`` x/y means unplaced."""

    parts: tuple[str, ...]  # refdes, index = part id
    pin_xy: np.ndarray  # (P, 2) pin offsets in part frame, mm
    pin_part: np.ndarray  # (P,) int part id
    pin_side: np.ndarray  # (P,) int8: 0 top SMD, 1 bottom SMD, 2 TH
    net_pins: tuple[np.ndarray, ...]  # per signal net: pin ids (plane nets dropped)
    flex: tuple[FlexSlot, ...]  # any_of slots (1b)
    swap_groups: tuple[SwapGroup, ...]  # plain / tuple groups (1b)
    fixed: np.ndarray  # (N,) bool: xy locked
    freedoms: np.ndarray  # (N,) uint8 bitset: ROTATE | BACK_SIDE
    outline: np.ndarray  # (K, 2) board outline, mm
    pose0: np.ndarray  # (N, 4) x, y, rot_quarter (0..3), side (0/1)


def _pin_sides(ir: PcbIR, footprints: Mapping[str, Mapping[str, Any]]) -> np.ndarray:
    """0 top-SMD / 1 bottom-SMD / 2 TH per pin. A pad carrying ``drill``
    is through-hole regardless of side (Decision 6's endpoint rule
    keys off this); no footprint row → SMD on the instance's side."""
    drilled: dict[str, set[str]] = {}
    for refdes, fp in footprints.items():
        nums = {
            str(p.get("number") or "").strip()
            for p in (fp.get("pads") or [])
            if p.get("drill") is not None
        }
        if nums:
            drilled[refdes] = nums
    sides = np.where(ir.inst_bottom, np.int8(1), np.int8(0))
    out = sides[ir.pin_instance].astype(np.int8)
    for pin_id in range(ir.n_pins):
        refdes = str(ir.instance_refdes[int(ir.pin_instance[pin_id])])
        if str(ir.pin_label[pin_id]) in drilled.get(refdes, ()):
            out[pin_id] = 2
    return out


def design_from_ir(
    ir: PcbIR, footprints: Mapping[str, Mapping[str, Any]]
) -> TopoDesign:
    """Build the one immutable placer input from the IR.

    ``footprints`` is the refdes-keyed pad cache
    (:func:`precis.pcb.session.footprints_by_refdes` shape — ``pads`` /
    ``pin_map`` / ``courtyard`` / ``centroid`` / ``holes``); only
    ``pads[*].number``/``drill`` are read, to tag through-hole pins.

    Conventions the spec leaves to the builder, fixed here:
    - ``net_pins`` keeps every non-plane signal net with ≥2 pins
      (``net_class`` against :data:`ratsnest.PLANE_CLASSES`, the same
      drop ``build_airwires`` makes — single-pin nets carry no wire).
    - ``freedoms`` derives from the lock bits: ROTATE unless
      ``inst_fixed_rot``, BACK_SIDE unless ``inst_fixed_xy`` (a part
      that may not move may not change sides either). The authored
      intent channels (Decision 17) land with 1b.
    - ``pose0`` rotation snaps ``inst_rot`` to the nearest quarter turn
      (mod 4); non-quarter authored rotations are approximated, not
      refused — the mover only ever produces quarter turns.
    - ``outline`` empty when the IR carries none — the placer falls
      back to a part-count-scaled square, like ``autoplace``.
    """
    parts = tuple(str(r) for r in ir.instance_refdes)
    n = len(parts)

    net_pins: list[np.ndarray] = []
    for net_id in range(ir.n_nets):
        cls = str(ir.net_class[net_id] or "").strip().lower()
        if cls in ratsnest.PLANE_CLASSES:
            continue
        pins = np.flatnonzero(ir.pin_net == net_id).astype(np.int64)
        if len(pins) >= 2:
            net_pins.append(pins)

    rot_q = np.nan_to_num(ir.inst_rot, nan=0.0)
    rot_q = np.round(rot_q / 90.0).astype(np.int64) % 4
    pose0 = np.column_stack(
        [
            np.asarray(ir.inst_x, dtype=np.float64),
            np.asarray(ir.inst_y, dtype=np.float64),
            rot_q.astype(np.float64),
            np.asarray(ir.inst_bottom, dtype=np.float64),
        ]
    )

    freedoms = np.zeros(n, dtype=np.uint8)
    freedoms[~ir.inst_fixed_rot] |= FREEDOM_ROTATE
    freedoms[~ir.inst_fixed_xy] |= FREEDOM_BACK_SIDE

    outline = np.empty((0, 2), dtype=np.float64)
    if ir.outline:
        outline = np.asarray(ir.outline, dtype=np.float64)

    return TopoDesign(
        parts=parts,
        pin_xy=np.column_stack(
            [
                np.asarray(ir.pin_dx, dtype=np.float64),
                np.asarray(ir.pin_dy, dtype=np.float64),
            ]
        ),
        pin_part=np.asarray(ir.pin_instance, dtype=np.int64),
        pin_side=_pin_sides(ir, footprints),
        net_pins=tuple(net_pins),
        flex=(),
        swap_groups=(),
        fixed=np.asarray(ir.inst_fixed_xy, dtype=bool).copy(),
        freedoms=freedoms,
        outline=outline,
        pose0=pose0,
    )


#: Sentinel for "this pin connects nothing" — mirrors ``ir.NO_NET`` so
#: 1b slot code need not import the IR module for the check.
__all__ = [
    "FREEDOM_BACK_SIDE",
    "FREEDOM_ROTATE",
    "NO_NET",
    "FlexSlot",
    "ObjectiveShares",
    "SwapGroup",
    "TopoDesign",
    "design_from_ir",
]

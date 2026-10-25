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

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from precis.pcb import ratsnest
from precis.pcb.ir import NO_NET, PcbIR
from precis_geom.pcb import PinConflict

#: ``freedoms`` bitset values (Decision 17's "allowed freedoms").
#: ROTATE — quarter-turn moves are legal; BACK_SIDE — the part may be
#: flipped to the bottom (mirrors its pin x, per the pose convention).
FREEDOM_ROTATE = np.uint8(1)
FREEDOM_BACK_SIDE = np.uint8(2)


@dataclass(frozen=True, slots=True)
class FlexSlot:
    """One ``part.pin(any_of=[...])`` declaration (1b): the solver picks
    ONE pin out of ``pins`` for the slot's signal. ``name`` feeds the
    firmware pin map (Decision 11); ``net`` indexes
    :attr:`TopoDesign.decl_nets` — the net the slot's wire belongs to,
    filled by ``net.tie`` at declaration time. ``site`` is the
    ``file:line`` claim site the wheel records for conflict messages;
    ``requirement_a`` the tie's ``current=Max`` bound in amps (Decision
    15 — ``Min`` is not a pin filter); ``pin_max_a`` is the per-candidate
    drive rating the ``pin_functions`` table carries, NaN = unrated."""

    part: int
    pins: tuple[int, ...]
    net: int
    name: str | None = None
    site: str | None = None
    requirement_a: float | None = None
    pin_max_a: tuple[float, ...] = ()


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


#: The named share vectors (Decision 18's presets) over the terms slice 1
#: prices. ``low-via`` and ``2-layer-only`` differ from ``compact`` only on
#: terms later rungs price (via count, inner-layer direction), so they
#: collapse onto crossings-heavy shares here — a via exists exactly where
#: a crossing cannot be resolved on one layer, so crossings are the
#: slice-1 proxy for both (spec-call, 1c).
PRESETS: dict[str, ObjectiveShares] = {
    "compact": ObjectiveShares(crossings=0.8, ratsnest=0.2),
    "low-via": ObjectiveShares(crossings=0.95, ratsnest=0.05),
    "2-layer-only": ObjectiveShares(crossings=1.0, ratsnest=0.0),
}


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
    pin_label: np.ndarray  # (P,) object: footprint pin/pad name per pin (1b)
    pin_net: np.ndarray  # (P,) int32: ir net id per pin, NO_NET unwired (1b)
    net_pins: tuple[np.ndarray, ...]  # per signal net: pin ids (plane nets dropped)
    flex: tuple[FlexSlot, ...]  # any_of slots (1b)
    swap_groups: tuple[SwapGroup, ...]  # plain / tuple groups (1b)
    fixed: np.ndarray  # (N,) bool: xy locked
    freedoms: np.ndarray  # (N,) uint8 bitset: ROTATE | BACK_SIDE
    outline: np.ndarray  # (K, 2) board outline, mm
    pose0: np.ndarray  # (N, 4) x, y, rot_quarter (0..3), side (0/1)
    claims: tuple[tuple[int, str], ...] = ()  # (pin id, name) named fixed claims
    #: The nets 1b declarations reference — slot ties plus swap-group
    #: members' wired nets. Indexed by :attr:`FlexSlot.net`. ``decl_nets``
    #: is the ir-level pin membership (plane and singleton nets included,
    #: unlike ``net_pins``); ``decl_net_ir`` the ir net id (-1 when the
    #: declared name matches no ir net); ``decl_net_route`` the
    #: ``net_pins`` index or -1; ``decl_net_plane`` marks plane-class
    #: nets (a pin bound there ties to the plane — no airwire exists).
    decl_nets: tuple[np.ndarray, ...] = ()
    decl_net_ir: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int32))
    decl_net_route: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))
    decl_net_plane: np.ndarray = field(default_factory=lambda: np.zeros(0, bool))
    decl_net_name: tuple[str, ...] = ()
    #: The 1b ``pin_functions`` tables as pin-id-keyed function sets,
    #: indexed by part — ``pin_functions[part][pin_id] = ("GPIO","INT1")``.
    #: The summary's widen-a-slot ranked edit needs them (1c spec-call).
    pin_functions: tuple[Mapping[int, tuple[str, ...]], ...] = ()


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
    ir: PcbIR,
    footprints: Mapping[str, Mapping[str, Any]],
    *,
    pin_decls: Mapping[str, Mapping[str, Any]] | None = None,
) -> TopoDesign:
    """Build the one immutable placer input from the IR.

    ``footprints`` is the refdes-keyed pad cache
    (:func:`precis.pcb.session.footprints_by_refdes` shape — ``pads`` /
    ``pin_map`` / ``courtyard`` / ``centroid`` / ``holes``); only
    ``pads[*].number``/``drill`` are read, to tag through-hole pins.

    ``pin_decls`` overrides :attr:`PcbIR.inst_pin_decls` (refdes → the
    ``pin_decls`` component blob) — for callers holding decls that never
    passed through a graph row; ``None`` reads the IR's own.

    Conventions the spec leaves to the builder, fixed here:
    - ``net_pins`` keeps every non-plane signal net with ≥2 pins
      (``net_class`` against :data:`ratsnest.PLANE_CLASSES`, the same
      drop ``build_airwires`` makes — single-pin nets carry no wire).
    - 1b decls ride ``decl_nets`` — NOT ``net_pins``: a plane net's
      member list would mint airwires the proxy would count as
      crossings. A slot's ``net`` index points into ``decl_nets``;
      ``decl_net_route`` maps it back to ``net_pins`` when the net is
      routable.
    - ``freedoms`` derives from the lock bits: ROTATE unless
      ``inst_fixed_rot``, BACK_SIDE unless ``inst_fixed_xy`` (a part
      that may not move may not change sides either). The authored
      intent channels (Decision 17) land with 1b.
    - ``pose0`` rotation snaps ``inst_rot`` to the nearest quarter turn
      (mod 4); non-quarter authored rotations are approximated, not
      refused — the mover only ever produces quarter turns.
    - ``outline`` empty when the IR carries none — the placer falls
      back to a part-count-scaled square, like ``autoplace``.
    - a decl pin label that is not a live pin raises :class:`PinConflict`
      naming the part and label — the wheel declares every candidate so
      this only fires on hand-authored ``pin_decls``.
    """
    parts = tuple(str(r) for r in ir.instance_refdes)
    n = len(parts)

    net_pins: list[np.ndarray] = []
    route_of: dict[int, int] = {}  # ir net id -> net_pins index
    for net_id in range(ir.n_nets):
        cls = str(ir.net_class[net_id] or "").strip().lower()
        if cls in ratsnest.PLANE_CLASSES:
            continue
        pins = np.flatnonzero(ir.pin_net == net_id).astype(np.int64)
        if len(pins) >= 2:
            route_of[net_id] = len(net_pins)
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

    decl = _decls(ir, parts, route_of, pin_decls)

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
        pin_label=np.asarray(ir.pin_label, dtype=object),
        pin_net=np.asarray(ir.pin_net, dtype=np.int32),
        net_pins=tuple(net_pins),
        flex=decl["flex"],
        swap_groups=decl["swap_groups"],
        fixed=np.asarray(ir.inst_fixed_xy, dtype=bool).copy(),
        freedoms=freedoms,
        outline=outline,
        pose0=pose0,
        claims=decl["claims"],
        decl_nets=decl["nets"],
        decl_net_ir=decl["net_ir"],
        decl_net_route=decl["net_route"],
        decl_net_plane=decl["net_plane"],
        decl_net_name=decl["net_name"],
        pin_functions=decl["pin_functions"],
    )


def _decls(
    ir: PcbIR,
    parts: tuple[str, ...],
    route_of: dict[int, int],
    pin_decls: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """The 1b ``pin_decls`` blobs as TopoDesign fields (Decisions 11–13):
    named fixed ``claims``, ``any_of`` ``slots`` and ``swap_groups``, with
    the decl-referenced nets collected into ``decl_nets`` so a slot's
    ``net`` index also reaches plane and singleton members ``net_pins``
    drops. Pin LABELS translate through ``ir.pin_label`` — a label that
    is not a live pin raises :class:`PinConflict`."""
    by_inst: dict[int, Mapping[str, Any]] = {}
    if pin_decls is not None:
        ref_to_i = {r: i for i, r in enumerate(parts)}
        for refdes, decl in pin_decls.items():
            if decl is not None and str(refdes) in ref_to_i:
                by_inst[ref_to_i[str(refdes)]] = decl
    elif ir.inst_pin_decls is not None:
        for i, decl in enumerate(ir.inst_pin_decls):
            if decl:
                by_inst[i] = decl

    pin_of: dict[tuple[int, str], int] = {
        (int(ir.pin_instance[pid]), str(ir.pin_label[pid])): pid
        for pid in range(ir.n_pins)
    }
    net_id_of = {str(n): k for k, n in enumerate(ir.net_name)}

    nets: list[np.ndarray] = []
    net_ir: list[int] = []
    net_route: list[int] = []
    net_plane: list[bool] = []
    net_name: list[str] = []
    decl_of_ir: dict[int, int] = {}

    def decl_net(ir_net: int | None, name: str) -> int:
        if ir_net is not None and ir_net in decl_of_ir:
            return decl_of_ir[ir_net]
        j = len(nets)
        if ir_net is None:
            nets.append(np.zeros(0, dtype=np.int64))
            net_ir.append(-1)
            net_route.append(-1)
            net_plane.append(False)
        else:
            nets.append(np.flatnonzero(ir.pin_net == ir_net).astype(np.int64))
            net_ir.append(ir_net)
            net_route.append(route_of.get(ir_net, -1))
            net_plane.append(
                str(ir.net_class[ir_net] or "").strip().lower()
                in ratsnest.PLANE_CLASSES
            )
        net_name.append(name)
        if ir_net is not None:
            decl_of_ir[ir_net] = j
        return j

    def pin_id(inst: int, label: Any) -> int:
        pid = pin_of.get((inst, str(label)))
        if pid is None:
            raise PinConflict(
                f"{parts[inst]}.{label}: pin_decls names a pin that is "
                "not live on the part — declare it or use a footprint "
                "pad name"
            )
        return pid

    flex: list[FlexSlot] = []
    swap_groups: list[SwapGroup] = []
    claims: list[tuple[int, str]] = []
    pin_functions: list[dict[int, tuple[str, ...]]] = [{} for _ in parts]
    for i in sorted(by_inst):
        decl = by_inst[i]
        funcs = decl.get("pin_functions") or {}

        # The table is informational for every pin of the part — labels
        # that never became live pins (undeclared, no pad) resolve to
        # nothing and drop out; a slot's candidates are checked through
        # ``pin_id`` separately, so this cannot mask a decl typo.
        def _funcs_of(rec: Any) -> tuple[str, ...]:
            fl = rec.get("functions") if isinstance(rec, Mapping) else rec
            return tuple(str(f) for f in (fl or ()))

        pin_functions[i] = {
            pid: _funcs_of(funcs.get(label))
            for label in funcs
            if (pid := pin_of.get((i, str(label)))) is not None
        }
        for c in decl.get("claims") or []:
            claims.append((pin_id(i, c["pin"]), str(c["name"])))
        for s in decl.get("slots") or []:
            labels = [str(p) for p in s.get("pins") or []]
            pins = tuple(pin_id(i, p) for p in labels)
            net_name_s = s.get("net")
            j = decl_net(
                net_id_of.get(str(net_name_s)) if net_name_s else None,
                str(net_name_s or ""),
            )
            ratings = tuple(
                float(v)
                if (v := (funcs.get(p) or {}).get("max_current_a")) is not None
                else math.nan
                for p in labels
            )
            flex.append(
                FlexSlot(
                    part=i,
                    pins=pins,
                    net=j,
                    name=s.get("name"),
                    site=s.get("site"),
                    requirement_a=(
                        float(s["requirement_a"])
                        if s.get("requirement_kind") == "max"
                        and s.get("requirement_a") is not None
                        else None
                    ),
                    pin_max_a=ratings,
                )
            )
        for g in decl.get("swap_groups") or []:
            members = tuple(
                tuple(pin_id(i, p) for p in unit) for unit in g.get("members") or []
            )
            for unit in members:
                for p in unit:
                    n = int(ir.pin_net[p])
                    if n != NO_NET:
                        decl_net(n, str(ir.net_name[n]))
            swap_groups.append(
                SwapGroup(part=i, members=members, tuple_=bool(g.get("tuple")))
            )

    return {
        "flex": tuple(flex),
        "swap_groups": tuple(swap_groups),
        "claims": tuple(claims),
        "nets": tuple(nets),
        "net_ir": np.asarray(net_ir, dtype=np.int32),
        "net_route": np.asarray(net_route, dtype=np.int64),
        "net_plane": np.asarray(net_plane, dtype=bool),
        "net_name": tuple(net_name),
        "pin_functions": tuple(pin_functions),
    }


#: Sentinel for "this pin connects nothing" — mirrors ``ir.NO_NET`` so
#: 1b slot code need not import the IR module for the check.
__all__ = [
    "FREEDOM_BACK_SIDE",
    "FREEDOM_ROTATE",
    "NO_NET",
    "PRESETS",
    "FlexSlot",
    "ObjectiveShares",
    "SwapGroup",
    "TopoDesign",
    "design_from_ir",
]

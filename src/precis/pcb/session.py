"""Shared IR<->DB session glue for the ``pcb_place``/``pcb_route`` worker
jobs (docs/backlog/pcb-guided-place-route.md Slice 10 — the phase-machine
gate hookup needs the tool surface to actually RUN ``optimize.py``/
``realize.py``, not just unit-test them).

Deliberately thin and self-contained: builds a
:class:`~precis.pcb.ir.PcbIR` from :meth:`Store.pcb_graph`, re-applies any
previously-persisted sketch choices (pinned side / layer assignment) onto
the fresh IR before a run, and serializes the settled IR back into the
``pcb_routes`` shape after one. No optimizer/cost/realizer logic lives
here — this module never imports :mod:`precis.pcb.optimize`,
:mod:`precis.pcb.cost`, or :mod:`precis.pcb.drc`.

**Segment identity must be durable across IR rebuilds** — the IR is never
cached between runs (``build_ir`` is called fresh every job), so the
in-memory integer ``seg_id`` :mod:`precis.pcb.ir` uses is meaningless
across two runs. :func:`segment_key` names a segment by its two endpoint
pins (``"REFDES.PIN"``, sorted) instead — that identity survives a rebuild,
which is what lets a pinned topology choice (or a targeted rip-up) persist
in ``pcb_routes`` and still land on the SAME segment next time.

**Hub selection must be deterministic, which the raw graph isn't.**
:func:`~precis.pcb.ir.from_graph` stars each net off its first
``members`` entry (the "hub"); :meth:`Store.pcb_graph`'s netconns SELECT
now carries an ``ORDER BY``, but :func:`sorted_graph` still sorts each
net's members by ``(refdes, pin)`` at the IR-build boundary as
defense-in-depth — a caller shouldn't have to trust the store's SQL to
keep hub selection deterministic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from precis.pcb import geom as pcb_geom
from precis.pcb import ir as pcb_ir
from precis.pcb import padplace as pcb_padplace
from precis.pcb import pose
from precis.pcb.rules import resolve_net_rules
from precis_geom.pcb_shape import Keepout

if TYPE_CHECKING:
    from precis.pcb.capabilities import CapabilityRow
    from precis.pcb.drc import DrcFinding
    from precis.pcb.rules import NetRules

log = logging.getLogger(__name__)


def _pin_key(refdes: str, pin: str) -> str:
    return f"{refdes}.{pin}"


def sorted_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """A shallow copy of ``graph`` with every net's ``members`` sorted by
    ``(refdes, pin)`` — see the module docstring for why this must run
    before :func:`build_ir` calls :func:`~precis.pcb.ir.from_graph`.
    Defense-in-depth over :meth:`Store.pcb_graph`'s now-ordered netconns
    SELECT, not the sole mitigation."""
    nets = []
    for net in graph.get("nets") or []:
        members = sorted(
            net.get("members") or [], key=lambda m: (m["refdes"], m["pin"])
        )
        nets.append({**net, "members": members})
    return {**graph, "nets": nets}


def outline_from_features(features: list[dict[str, Any]]) -> list[list[float]] | None:
    """The ``ftype='outline'`` feature's ``geom.path`` (a polygon ring of
    ``[x, y]`` pairs), or ``None`` when the design hasn't authored one yet
    — the same extraction :meth:`~precis.handlers.pcb.PcbHandler.
    _outline_from_features` does off :meth:`Store.pcb_features_list`
    (mirrored here, not imported: that handler owns the tool surface and
    isn't this module's to reach into — see the module docstring's "never
    imports optimize/cost/drc" boundary, which extends to not importing
    the handler layer either). ``build_ir``'s own caller is responsible
    for fetching ``features`` (this module has no store handle of its
    own, by design).

    ``geom.corner_radius_mm`` is honoured here exactly as the handler
    mirror honours it (fillet + polygonize via :func:`precis.pcb.geom.
    rounded_polygon`) — the two extractions drifting on the radius would
    hand the router a SHARP outline for a board every render draws
    rounded, i.e. copper poured into corners the fab mills away."""
    for f in features:
        geom = f.get("geom") or {}
        if str(f.get("ftype") or "") == "outline" and isinstance(
            geom.get("path"), list
        ):
            path = [[float(p[0]), float(p[1])] for p in geom["path"]]
            radius = geom.get("corner_radius_mm")
            if radius is not None and float(radius) > 0:
                ring: list[pcb_geom.Point] = [(p[0], p[1]) for p in path]
                return [
                    [p[0], p[1]] for p in pcb_geom.rounded_polygon(ring, float(radius))
                ]
            return path
    return None


OUTLINE_FIX_HINT = (
    "put(kind='pcb', id='<slug>', args={'features':[{'ftype':'outline',"
    "'geom':{'path':[[0,0],[40,0],[40,30],[0,30]]}}]}) — a closed polygon of "
    "at least 3 points that does not cross itself"
)


def outline_defect(outline: list[list[float]] | None) -> str | None:
    """Why ``outline`` is not a usable board outline, or ``None`` when it
    is a closed simple polygon. The one predicate DRC (``outline_missing``),
    the ``op='route'`` refusal and the route job share: no outline, fewer
    than 3 distinct points, or a ring that crosses itself / has ~zero area
    (Reto 2026-10-20: a board must have a closed outline, otherwise
    nothing goes)."""
    from shapely.geometry import Polygon

    if not outline:
        return "the board has no outline"
    pts = {(round(float(p[0]), 6), round(float(p[1]), 6)) for p in outline}
    if len(pts) < 3:
        return f"the outline has only {len(pts)} distinct point(s); it needs at least 3"
    poly = Polygon([(float(p[0]), float(p[1])) for p in outline])
    if not poly.is_valid:
        return "the outline ring crosses itself (not a simple polygon)"
    if poly.area < 1e-6:
        return "the outline ring encloses ~zero area"
    return None


def mounting_holes_from_features(
    features: list[dict[str, Any]],
) -> tuple[pcb_ir.MountingHole, ...]:
    """Every ``ftype='mounting_hole'`` feature as a
    :class:`~precis.pcb.ir.MountingHole` — the board-config companion to
    :func:`outline_from_features`, and carried onto the IR for the same
    reason the outline is: a hole only the handler's feature list knows
    about is one the router draws copper through (round-3 review item 4,
    the ``npth_clearance`` family). ``geom.diameter`` is the drill;
    optional ``geom.ring_dia_mm`` (+ ``geom.plated``) describe a
    solder-nut's copper annulus; optional ``geom.head_dia_mm`` describes
    the hardware envelope above the board (screw head / nut flange /
    washer) for placement keep-out only."""
    out: list[pcb_ir.MountingHole] = []
    for f in features:
        if str(f.get("ftype") or "") != "mounting_hole":
            continue
        geom = f.get("geom") or {}
        try:
            x, y = float(f.get("x", 0.0)), float(f.get("y", 0.0))
            drill = float(geom.get("diameter") or 0.0)
        except (TypeError, ValueError):
            continue
        if drill <= 0.0:
            continue
        out.append(
            pcb_ir.MountingHole(
                x=x,
                y=y,
                drill_mm=drill,
                ring_dia_mm=float(geom.get("ring_dia_mm") or 0.0),
                head_dia_mm=float(geom.get("head_dia_mm") or 0.0),
                plated=bool(geom.get("plated")),
                part=str(geom["part"]) if geom.get("part") else None,
            )
        )
    return tuple(out)


def keepouts_from_features(
    features: list[dict[str, Any]],
) -> tuple[Keepout, ...]:
    """Every ``ftype='keepout'`` feature as a
    :class:`~precis_geom.pcb_shape.Keepout`` — the authored keep-out
    regions :func:`precis.pcb.drc.check_keepout_copper` enforces against
    copper, parsed here once so the DRC check and the se fit check read
    the same shape (``geom.poly`` in board-frame mm; ``layers`` empty or
    absent means every copper layer; ``reason`` is the keepout's name).
    A polygon with fewer than 3 points or a non-numeric vertex is skipped
    rather than guessed at — same contract as
    :func:`mounting_holes_from_features` above."""
    out: list[Keepout] = []
    for f in features:
        if str(f.get("ftype") or "") != "keepout":
            continue
        poly = (f.get("geom") or {}).get("poly") or []
        if len(poly) < 3:
            continue
        try:
            ring = tuple((float(p[0]), float(p[1])) for p in poly)
        except (TypeError, ValueError, IndexError):
            continue
        out.append(
            Keepout(
                poly=ring,
                layers=tuple(
                    str(layer)
                    for layer in (
                        f.get("layers") or (f.get("geom") or {}).get("layers") or ()
                    )
                ),
                reason=(
                    str(f["reason"])
                    if f.get("reason")
                    else (
                        str((f.get("geom") or {})["reason"])
                        if (f.get("geom") or {}).get("reason")
                        else None
                    )
                ),
            )
        )
    return tuple(out)


def fab_layers_from_features(features: list[dict[str, Any]]) -> dict[str, Any]:
    """The non-copper fab-layer ``features[]`` rows (gr520329) as the
    model keys :mod:`precis.pcb.gerber` and
    :func:`precis.pcb.drc.check_bend_zones` read — the same extraction
    contract :func:`outline_from_features` holds: every consumer parses
    the feature list here, once, so the handler's ``_fab_model`` and the
    laptop kernel's ``_model`` cannot drift on where a stiffener's
    thickness lives.

    Keys are present only with entries (a rigid board's features produce
    an empty dict — ``model.get(...)`` is the reader's shape)::

        coverlay_openings: [{"side", "polygon"}]
        stiffeners:        [{"side", "material", "thickness_mm", "polygon"}]
        adhesives:         [{"side", "polygon"}]
        tape_masks:        [{"kind", "polygon"}]
        doc_notes:         [{"text", "x", "y", "layer"}]
        bend_zones:        [{"line": [[x,y],[x,y]], "radius_mm",
                             "angle_deg"}]

    A malformed row is skipped, not fatal — the same reading
    :func:`mounting_holes_from_features` gives a hole with no drill:
    one bad region should not blank the whole export.
    """
    out: dict[str, Any] = {}

    def _poly(geom: Any) -> list[list[float]] | None:
        poly = (geom or {}).get("polygon")
        if not isinstance(poly, list) or len(poly) < 3:
            return None
        try:
            return [[float(p[0]), float(p[1])] for p in poly]
        except (TypeError, ValueError, IndexError):
            return None

    def _side(f: dict[str, Any]) -> str | None:
        side = str(f.get("layer") or "").strip().lower()
        return side if side in ("top", "bottom") else None

    for f in features:
        ftype = str(f.get("ftype") or "")
        geom = f.get("geom") or {}
        if ftype == "coverlay_opening":
            side, poly = _side(f), _poly(geom)
            if side and poly:
                out.setdefault("coverlay_openings", []).append(
                    {"side": side, "polygon": poly}
                )
        elif ftype == "stiffener":
            side, poly = _side(f), _poly(geom)
            try:
                t = float(geom.get("thickness_mm") or 0.0)
            except (TypeError, ValueError):
                t = 0.0
            if side and poly and t > 0:
                out.setdefault("stiffeners", []).append(
                    {
                        "side": side,
                        "material": str(geom.get("material") or ""),
                        "thickness_mm": t,
                        "polygon": poly,
                    }
                )
        elif ftype == "adhesive":
            side, poly = _side(f), _poly(geom)
            if side and poly:
                out.setdefault("adhesives", []).append({"side": side, "polygon": poly})
        elif ftype == "tape_mask":
            poly = _poly(geom)
            if poly:
                out.setdefault("tape_masks", []).append(
                    {"kind": str(geom.get("kind") or "parylene"), "polygon": poly}
                )
        elif ftype == "doc_note":
            text = str(f.get("note") or "").strip()
            try:
                x, y = float(f["x"]), float(f["y"])
            except (KeyError, TypeError, ValueError):
                continue
            if text:
                out.setdefault("doc_notes", []).append(
                    {
                        "text": text,
                        "x": x,
                        "y": y,
                        "layer": str(f.get("layer") or "Dwgs.User"),
                    }
                )
        elif ftype == "bend_zone":
            line = geom.get("line")
            try:
                pts = [[float(p[0]), float(p[1])] for p in line or []]
                radius, angle = (
                    float(geom["radius_mm"]),
                    float(geom["angle_deg"]),
                )
            except (TypeError, ValueError, KeyError, IndexError):
                continue
            if len(pts) == 2 and radius > 0 and angle != 0:
                out.setdefault("bend_zones", []).append(
                    {"line": pts, "radius_mm": radius, "angle_deg": angle}
                )
    return out


def fixed_vias_from_copper(
    fixed_copper: list[dict[str, Any]] | None,
) -> tuple[pcb_ir.FixedVia, ...]:
    """Every authored via in :meth:`Store.pcb_fixed_copper_list`'s rows as
    a :class:`~precis.pcb.ir.FixedVia` — the placer-side companion to
    :func:`precis.pcb.realize._claim_fixed_copper`, which reads the same
    rows for the router. A row with no usable position or diameter is
    skipped rather than guessed at."""
    out: list[pcb_ir.FixedVia] = []
    for row in fixed_copper or []:
        if str(row.get("ctype") or "") != "via":
            continue
        try:
            x, y = float(row["x"]), float(row["y"])
            dia = float(row.get("dia_mm") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        if dia <= 0.0:
            continue
        out.append(pcb_ir.FixedVia(x=x, y=y, dia_mm=dia, net=str(row.get("net") or "")))
    return tuple(out)


def land_rects_by_instance(
    ir: pcb_ir.PcbIR, footprints: dict[str, dict[str, Any]]
) -> tuple[tuple[np.ndarray, ...], tuple[tuple[str, ...], ...]]:
    """``(rects, nets)``: :attr:`~precis.pcb.ir.PcbIR.inst_land_rects` for
    ``ir`` — every pad of each instance's REAL footprint as a
    footprint-local ``(cx, cy, half_w, half_h)`` row, falling back per
    instance to the pin-derived rects (:func:`~precis.pcb.ir.
    instance_land_rects`) where ``footprints`` has no row for it — and
    :attr:`~precis.pcb.ir.PcbIR.inst_land_nets`, each row's net in the same
    order: the net of the netlist pin the pad's label names, ``""`` for a
    pad no connected pin names and for every pin-derived fallback row (net
    unknown).

    Read off the RAW footprint pads rather than through
    :func:`precis.pcb.realize.pad_geometry`: that resolves a pad at the
    instance's CURRENT rotation, and the placer needs a shape it can carry
    through rotations it has not tried yet. A pad's own ``rot`` is folded
    in here (the same 90/270 swap :func:`precis.pcb.padplace.
    place_footprint_pads` applies); an oblique one is covered by a square
    of its longer side, which over-covers and so can only reject."""
    derived = pcb_ir.instance_land_rects(ir)
    pin_nets: dict[int, dict[str, str]] = {}
    for pid in range(ir.n_pins):
        net_id = int(ir.pin_net[pid])
        if net_id == pcb_ir.NO_NET:
            continue
        pin_nets.setdefault(int(ir.pin_instance[pid]), {})[str(ir.pin_label[pid])] = (
            str(ir.net_name[net_id])
        )
    out: list[np.ndarray] = []
    out_nets: list[tuple[str, ...]] = []
    for inst_id in range(ir.n_instances):
        fp = footprints.get(str(ir.instance_refdes[inst_id]))
        pads = (fp or {}).get("pads") or []
        pin_map = (fp or {}).get("pin_map") or {}
        by_pin = pin_nets.get(inst_id, {})
        rows: list[tuple[float, float, float, float]] = []
        nets: list[str] = []
        for pad in pads:
            try:
                cx, cy = float(pad["x"]), float(pad["y"])
                w = float(pad["w"])
                h = float(pad.get("h", pad["w"]))
            except (KeyError, TypeError, ValueError):
                continue
            poly = pad.get("poly")
            if poly:
                xs = [float(v[0]) for v in poly]
                ys = [float(v[1]) for v in poly]
                cx, cy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
                w, h = max(xs) - min(xs), max(ys) - min(ys)
            else:
                rot = float(pad.get("rot") or 0.0)
                if pcb_padplace.rect_swaps_wh(rot):
                    w, h = h, w
                elif not pcb_padplace.pad_axis_aligned(rot):
                    w = h = max(w, h)
            rows.append((cx, cy, w / 2.0, h / 2.0))
            nets.append(by_pin.get(pcb_padplace.pad_label(pad, pin_map) or "", ""))
        if rows:
            out.append(np.array(rows, dtype=np.float64).reshape(-1, 4))
            out_nets.append(tuple(nets))
        else:
            out.append(derived[inst_id])
            out_nets.append(("",) * len(derived[inst_id]))
    return tuple(out), tuple(out_nets)


def clearance_net_rules(
    design: dict[str, Any], capability: CapabilityRow
) -> dict[str, NetRules]:
    """Net name -> resolved :class:`~precis.pcb.rules.NetRules` for the
    clearance checks, from a :meth:`~precis.store.Store.pcb_load` design:
    what the put judge's ``check_clearance`` judges by, what the seed
    legaliser places against, and (via :func:`net_clearance_floors`) what
    the place job's anneal keeps lands off authored vias by. One source."""
    net_classes = design.get("net_classes") or {}
    return {
        str(n["name"]): resolve_net_rules(
            str(n.get("net_class") or ""),
            # Clearance (the only field check_clearance reads off this
            # map) doesn't depend on layer -- an arbitrary True is fine
            # here; realize.py is the caller that resolves per-layer.
            layer_is_outer=True,
            fab_caps=capability,
            overrides=net_classes.get(n.get("net_class") or ""),
            current_a=n.get("est_current_a"),
        )
        for n in design["nets"]
    }


def route_net_rules(
    design: dict[str, Any], capability: CapabilityRow
) -> dict[str, NetRules]:
    """Net name → resolved :class:`~precis.pcb.rules.NetRules` for the
    topo ROUTE stage — the same resolution as :func:`clearance_net_rules`
    with every net's ``est_current_a`` dropped. The current-derived
    IPC-2221 width is the rail's post-route widen TARGET
    (:func:`~precis.pcb.topoplace.realise.widen_power` claims it where the
    DRC oracle allows), never a routing demand: corridor capacity, the
    A* occupancy and the emitted track width all price the width the
    track is BORN at, and handing the router a 5 A rail's ~2.8 mm refuses
    channels its routing width fits (Reto's ruling — GND/power route as
    normal nets at the signal width; an authored class
    ``track_width_mm`` still stands, it is a rule width, not a widen
    target)."""
    net_classes = design.get("net_classes") or {}
    return {
        str(n["name"]): resolve_net_rules(
            str(n.get("net_class") or ""),
            # An arbitrary True like clearance_net_rules' — the route
            # stage resolves one width/clearance pair per net.
            layer_is_outer=True,
            fab_caps=capability,
            overrides=net_classes.get(n.get("net_class") or ""),
            current_a=None,
        )
        for n in design["nets"]
    }


def net_clearance_floors(
    design: dict[str, Any], capability: CapabilityRow
) -> dict[str, float]:
    """:attr:`~precis.pcb.ir.PcbIR.net_clearance_floor_mm` for a design:
    each net's resolved ``min_clearance_mm`` HARD floor, for the nets that
    have one (the coated hv floor on a parylene EWOD board). gr491942: the
    handler's seed legaliser hydrated it but the place job did not, so the
    anneal judged an HV land against a foreign-net via at the flat gap and
    could undo the legalised seed. Per NET only; a per-(net, layer) floor
    model is the vector-router spec's checker step
    (docs/backlog/pcb-vector-router.md), and this is the seam it replaces."""
    return {
        name: float(rules.min_clearance_mm)
        for name, rules in clearance_net_rules(design, capability).items()
        if rules.min_clearance_mm is not None
    }


def build_ir(
    graph: dict[str, Any],
    *,
    outline: list[list[float]] | None = None,
    mounting_holes: tuple[pcb_ir.MountingHole, ...] = (),
    footprints_by_lcsc: dict[str, dict[str, Any]] | None = None,
    local_footprints_by_name: dict[str, dict[str, Any]] | None = None,
    fixed_copper: list[dict[str, Any]] | None = None,
) -> pcb_ir.PcbIR:
    """Build a fresh L0(+L3) IR from a :meth:`Store.pcb_graph` payload.
    ``outline`` (see :func:`outline_from_features`) is optional — a caller
    with no board-outline feature yet (or one that hasn't been updated to
    fetch it) simply gets an IR whose ``outline`` is ``None``, same as
    today; ``cost.py``'s ``board_edge_clearance`` term and ``optimize.
    py``'s TRANSLATE clamp both already degrade cleanly for that case.
    ``mounting_holes`` (see :func:`mounting_holes_from_features`) rides
    the same pattern — ``()`` degrades to today's router-blind behavior.

    An instance row in ``graph["instances"]`` may also carry ``"group"``/
    ``"group_offset"`` (an authored rigid "super footprint") or
    ``"pattern"``/``"pattern_instance"`` (an auto-derived rigid tile) —
    parsed straight through by :func:`~precis.pcb.ir.from_graph` (see
    :func:`precis.pcb.ir._parse_instance_groups`) into
    :attr:`~precis.pcb.ir.PcbIR.inst_group` and friends; this function has
    no group-specific logic of its own, it just forwards whatever the
    graph rows already carry, same as every other per-instance field.

    ``footprints_by_lcsc`` (:meth:`Store.pcb_footprints_for`) and
    ``local_footprints_by_name`` (:meth:`Store.pcb_local_footprints_for`)
    are both optional and both feed exactly one thing:
    :func:`apply_real_pin_offsets`, so a pin whose part has a real
    footprint gets its REAL position instead of the landpattern guess
    (gripe 338983). Wired HERE rather than at each caller because every IR
    consumer that reads a pin position — DRC, the router, ratsnest,
    courtyards — reads it off the IR: one rule, one call site, which is
    the discipline this package's recurring defect keeps punishing.
    Omitting them is still valid (a unit test with no store, the same
    behaviour as before this existed): every pin then stays synthesized,
    which :attr:`~precis.pcb.ir.PcbIR.pin_offsets_synthesized` says out
    loud.

    ``fixed_copper`` (:meth:`Store.pcb_fixed_copper_list`, optional) puts
    the board's authored vias on the IR (:func:`fixed_vias_from_copper`)
    so the placer treats them as obstacles; the same footprints then also
    supply each instance's real lands (:func:`land_rects_by_instance`),
    the shapes that must stay off those vias."""
    board = graph.get("board") or {}
    stackup = board.get("stackup")
    ir = pcb_ir.from_graph(
        sorted_graph(graph),
        stackup=stackup,
        outline=outline,
        mounting_holes=mounting_holes,
    )
    ir.fixed_vias = fixed_vias_from_copper(fixed_copper)
    if footprints_by_lcsc or local_footprints_by_name:
        by_refdes = footprints_by_refdes(
            ir,
            footprints_by_lcsc or {},
            local_footprints_by_name=local_footprints_by_name,
            local_names_by_refdes=local_footprint_names_by_refdes(graph),
        )
        apply_real_pin_offsets(ir, by_refdes)
        ir.inst_land_rects, ir.inst_land_nets = land_rects_by_instance(ir, by_refdes)
        derived = footprint_holes(graph, by_refdes)
        # The footprint wins: a legacy part-owned feature for a refdes whose
        # footprint now carries holes would drill the same hole twice.
        owners = {h.part for h in derived}
        ir.mounting_holes = (
            *(h for h in ir.mounting_holes if h.part not in owners),
            *derived,
        )
    return ir


def footprint_holes(
    graph: dict[str, Any], by_refdes: dict[str, dict[str, Any]]
) -> tuple[pcb_ir.MountingHole, ...]:
    """Every placed instance's footprint holes (``footprint["holes"]``,
    footprint-local ``{x, y, dia_mm}``) as :class:`~precis.pcb.ir.MountingHole`
    at the instance's CURRENT pose (:func:`precis.pcb.pose.to_world`), with
    ``part=<refdes>`` so the owning part's courtyard may cover its own hole.
    Derived on every IR build rather than stored, so a moved part carries its
    holes with it. An unplaced instance (no ``x``/``y``) has no world
    position yet and yields none."""
    out: list[pcb_ir.MountingHole] = []
    for inst in graph.get("instances") or []:
        refdes = str(inst["refdes"])
        holes = (by_refdes.get(refdes) or {}).get("holes") or []
        if not holes or inst.get("x") is None or inst.get("y") is None:
            continue
        for h in holes:
            x, y = pose.to_world(float(h["x"]), float(h["y"]), inst)
            out.append(
                pcb_ir.MountingHole(
                    x=x,
                    y=y,
                    drill_mm=float(h["dia_mm"]),
                    part=refdes,
                    local_x=float(h["x"]),
                    local_y=float(h["y"]),
                )
            )
    return tuple(out)


def local_footprint_names_by_refdes(graph: dict[str, Any]) -> dict[str, str]:
    """``{refdes: footprint name}`` for every instance in ``graph`` whose
    ``footprint`` field is set — pcb-ewod-multitile Slice 1's
    authored-copper join key (``pcb_components.footprint``, selected by
    :meth:`Store.pcb_graph`).

    No longer restricted to partless instances (2026-10-14): an .epro2
    import carries BOTH ``part_lcsc`` (the catalog C-number, which queues
    datasheet pulls) AND the board's own footprint as a
    ``pcb_local_footprints`` row, and the local row wins when it exists —
    "keep the board's own footprint where it really differs". The lookup
    itself is the guard: a catalog part's footprint LABEL (e.g.
    ``"SOT-23"``) resolves only when the design genuinely authored a
    local row by that name, which IS the instance having one."""
    out: dict[str, str] = {}
    for inst in graph.get("instances") or []:
        name = inst.get("footprint")
        if name:
            out[str(inst["refdes"])] = str(name)
    return out


def footprints_by_refdes(
    ir: pcb_ir.PcbIR,
    footprints_by_lcsc: dict[str, dict[str, Any]],
    *,
    local_footprints_by_name: dict[str, dict[str, Any]] | None = None,
    local_names_by_refdes: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Remap :meth:`~precis.store.Store.pcb_footprints_for`'s C-number-keyed
    cache onto the refdes-keyed shape :func:`precis.pcb.realize.pad_geometry`
    (and everything built on top of it — :func:`~precis.pcb.realize.
    pads_for_ir`, :func:`~precis.pcb.realize.realize`) accepts as
    ``footprints``.

    This is the missing join, not a new lookup: :attr:`~precis.pcb.ir.
    PcbIR.instance_part_lcsc` (from :meth:`Store.pcb_graph`'s own
    ``pcb_components`` join) is the ONLY thing on the IR side that knows an
    instance's C-number, and ``pcb_footprints_for``'s cache is the ONLY
    thing on the store side that knows a C-number's real pad geometry —
    every caller with both a :class:`PcbIR` and a live ``Store`` handle
    needs exactly this remap before ``footprints=`` means anything.

    An instance with no linked catalog part (``instance_part_lcsc`` is
    ``None``), or whose C-number has no cached footprint row yet, is
    simply absent from the result — :func:`~precis.pcb.realize.
    pad_geometry`'s own per-pin fallback to synthesized geometry already
    handles "no real data for this instance" correctly; this function
    doesn't need a second way to say the same thing.

    ``local_footprints_by_name``/``local_names_by_refdes`` (pcb-ewod-
    multitile Slice 1, both optional) are the design-local counterpart:
    :meth:`~precis.store.Store.pcb_local_footprints_for`'s cache and
    :func:`local_footprint_names_by_refdes`'s join, checked FIRST — an
    instance that names a ``pcb_local_footprints`` row uses the board's
    own authored geometry even when it also has a catalog part
    (Reto 2026-10-14: the .epro2 import keeps its footprint; the C-number
    is for datasheets and ordering, not pads).
    """
    out: dict[str, dict[str, Any]] = {}
    local_footprints_by_name = local_footprints_by_name or {}
    local_names_by_refdes = local_names_by_refdes or {}
    for inst_id in range(ir.n_instances):
        refdes = str(ir.instance_refdes[inst_id])
        lcsc = ir.instance_part_lcsc[inst_id]
        fp = None
        name = local_names_by_refdes.get(refdes)
        if name:
            fp = local_footprints_by_name.get(name)
        if fp is None and lcsc:
            fp = footprints_by_lcsc.get(str(lcsc))
        if fp:
            out[refdes] = fp
    return out


def _real_pin_offsets(fp: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """One footprint's REAL per-pin pad CENTER, keyed by netlist pin name —
    footprint-local mm, unrotated and unmirrored, which is exactly
    :attr:`~precis.pcb.ir.PcbIR.pin_dx`'s own frame (:func:`~precis.pcb.ir.
    pin_point` applies the instance pose itself).

    ``fp`` is a ``part_footprints``/``pcb_local_footprints`` row
    (``{"pads": [...], "pin_map": {...}}``) — every pad in either carries
    ``x``/``y``, including a ``shape: 'polygon'`` one, whose x/y is its
    vertex ring's bbox center (``precis.pcb.rows.normalize_pad`` puts it
    there for exactly this class of reader). No transform is needed or
    wanted here, so this does NOT go
    through :func:`precis.pcb.padplace.place_footprint_pads` the way
    :func:`precis.pcb.realize._real_pad_sizes` must for w/h.

    **FIRST pad wins per pin**, the same rule ``_real_pad_sizes`` uses for
    size/shape/poly — and deliberately the same ORDER, so a pin with
    several pads (an EWOD electrode emits body + escape stub + via, all
    one pin) gets its position from the same pad that supplied its
    outline. Picking a different one would put a real polygon at another
    pad's center, which is worse than the synthesized guess this replaces.
    First-wins decides the PIN's position only, never which pads exist:
    the later same-named pads are pads in their own right in the IR's pad
    set (:func:`~precis.pcb.ir.footprint_pad_set`, keyed by pad number,
    gr451276), so nothing is dropped here.
    """
    pin_map = fp.get("pin_map") or {}
    out: dict[str, tuple[float, float]] = {}
    for pad in fp.get("pads") or []:
        name = pcb_padplace.pad_label(pad, pin_map)
        if not name or name in out:
            continue
        try:
            out[name] = (float(pad["x"]), float(pad["y"]))
        except (KeyError, TypeError, ValueError):
            continue  # a malformed pad is an absent one, never a guess at 0,0
    return out


_PinPad = tuple[float, float, list[tuple[float, float]] | None]


def _real_pin_pads(fp: dict[str, Any]) -> dict[str, _PinPad]:
    """One footprint's REAL per-pin pad, keyed by netlist pin name:
    ``(w, h, poly)`` in footprint-local mm, unrotated by the instance pose
    (:attr:`~precis.pcb.ir.PcbIR.pin_w`'s frame). The first pad per pin
    supplies it — the same pad :func:`_real_pin_offsets` takes the centre
    from. Every later pad with the same pin name (a split-tab connector
    numbers four tabs ``1``) is a pad in its own right in the IR's pad
    set (:func:`~precis.pcb.ir.footprint_pad_set`), not this pin's size."""
    pin_map = fp.get("pin_map") or {}
    out: dict[str, _PinPad] = {}
    for pad in fp.get("pads") or []:
        name = pcb_padplace.pad_label(pad, pin_map)
        if not name or name in out:
            continue
        ext = pcb_ir.footprint_pad_extent(pad)
        if ext is None:
            continue
        w, h, ring = ext[2], ext[3], ext[4]
        out[name] = (w, h, ring)
    return out


def pin_name_mismatches(
    ir: pcb_ir.PcbIR, footprints: dict[str, dict[str, Any]]
) -> list[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    """``(refdes, declared names with no pad, the footprint's own names)``
    for every instance whose footprint IS cached but does not name one of
    its declared pins.

    That is a name error, not a missing footprint: the join in
    :func:`apply_real_pin_offsets` and :func:`precis.pcb.realize.
    pad_geometry` is by pin NAME, so an unmatched pin silently falls back
    to a synthesized landpattern bound while the cache row still reads
    ``cached: yes``. The EWOD sink declared ``OUT0…OUT63`` against a part
    whose pads are ``HVOUT1…HVOUT64``, and 56 of its 59 pins were bounds
    for a week of measurements before anyone noticed. ``footprints`` is
    the refdes-keyed dict
    :func:`footprints_by_refdes` returns; an instance absent from it has no
    footprint to mismatch and is not reported here."""
    labels: dict[int, list[str]] = {}
    for pid in range(ir.n_pins):
        labels.setdefault(int(ir.pin_instance[pid]), []).append(str(ir.pin_label[pid]))
    out: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    for inst_id in range(ir.n_instances):
        refdes = str(ir.instance_refdes[inst_id])
        fp = footprints.get(refdes)
        if not fp or not fp.get("pads"):
            continue
        real = _real_pin_offsets(fp)
        unmatched = tuple(
            dict.fromkeys(n for n in labels.get(inst_id, []) if n not in real)
        )
        if unmatched:
            out.append((refdes, unmatched, tuple(sorted(real))))
    return out


def pin_name_mismatch_note(
    mismatches: list[tuple[str, tuple[str, ...], tuple[str, ...]]],
) -> str:
    """The warning block a design put and ``view='footprints'`` lead with
    when :func:`pin_name_mismatches` found any — empty otherwise."""
    if not mismatches:
        return ""

    def _names(names: tuple[str, ...]) -> str:
        shown = ", ".join(names[:12])
        return shown + (f", … (+{len(names) - 12})" if len(names) > 12 else "")

    lines = [
        "⚠️  pin names that match no pad on the part's cached footprint — "
        "these pins sit at a synthesized bound, not the real land:"
    ]
    for refdes, unmatched, available in mismatches:
        lines.append(
            f"- {refdes}: declared {_names(unmatched)}; "
            f"the footprint names {_names(available)}"
        )
    return (
        "\n".join(lines) + "\n\nNext: rename each pin to the footprint's name with "
        "put(args={'op':'pin_rename','refdes':...,'pin':...,'to':...}), or, "
        "when that name already exists, op='pin_remove' the wrong pin and "
        "re-put its connection on the right one.\n"
    )


def apply_real_pin_offsets(
    ir: pcb_ir.PcbIR, footprints: dict[str, dict[str, Any]]
) -> int:
    """Overwrite every pin's synthesized land-pattern offset with its REAL
    footprint coordinate where one exists; returns how many pins changed.

    The POSITION half of the same join :func:`footprints_by_refdes` already
    serves for pad SIZE (:func:`precis.pcb.realize.pad_geometry`) — same
    refdes-keyed dict in, per-pin override out, same "no match ⇒ keep the
    synthesized bound" fallback for a part with no cached/authored
    footprint or a pin its ``pin_map`` doesn't cover. gripe 338983: pad
    size was real while pad position stayed a guess, so DRC clearance, the
    the router's pad obstacles, connectivity and courtyard hulls all measured
    correctly-sized polygons anchored at the wrong places — harmless-looking
    for a catalog part whose label names a package family the synthesis
    approximates, meaningless for an authored custom footprint (an
    ``ewod_pad_array`` electrode sat ~10mm from its real position).

    Mutates through :meth:`~precis.pcb.ir.PcbIR.set_pin_offset` (the
    sanctioned mutator, which owns the dirty-mask cascade) rather than
    assigning into the arrays, and is idempotent: re-running with the same
    footprints writes the same coordinates.

    **A catalog instance with no cached footprint logs a warning here**
    (gripe gr341532) — this is the ONE seam where "no real footprint" is
    known (``footprints`` is already refdes-keyed and already dropped that
    instance if :func:`footprints_by_refdes` found nothing to remap onto
    it) and where whether it MATTERS is known (``instance_part_lcsc`` says
    whether this is a catalog part at all, vs. an authored/local-footprint
    or bare-``label`` instance that was never going to have a cached
    footprint in the first place). Every pin of that instance still falls
    back to :mod:`precis.pcb.landpattern`'s synthesized bound exactly as
    before — this only makes the fallback audible; it does not change it.

    Also hydrates the IR's pad set (:attr:`~precis.pcb.ir.PcbIR.
    footprint_pads`, gr451276) from the same footprints: this is the one
    seam where footprints meet the IR, and the courtyard/land-rect
    readers that take no ``footprints`` argument read it from there.
    """
    ir.footprint_pads = pcb_ir.footprint_pad_set(ir, footprints)
    real_by_inst: dict[int, dict[str, tuple[float, float]]] = {}
    pads_by_inst: dict[int, dict[str, _PinPad]] = {}
    for inst_id in range(ir.n_instances):
        refdes = str(ir.instance_refdes[inst_id])
        fp = footprints.get(refdes) if footprints else None
        if fp and fp.get("pads"):
            real_by_inst[inst_id] = _real_pin_offsets(fp)
            pads_by_inst[inst_id] = _real_pin_pads(fp)
            continue
        lcsc = ir.instance_part_lcsc[inst_id]
        if lcsc:
            log.warning(
                "pcb: %s (lcsc %s) has no cached footprint — its pins stay "
                "at the synthesized landpattern bound",
                refdes,
                lcsc,
            )
    if not real_by_inst:
        return 0
    changed = 0
    for pid in range(ir.n_pins):
        offset = real_by_inst.get(int(ir.pin_instance[pid]), {}).get(
            str(ir.pin_label[pid])
        )
        if offset is None:
            continue
        ir.set_pin_offset(pid, offset[0], offset[1])
        # Size travels with position (gr460567): a real centre with a
        # synthesized size hulls the wrong pad.
        pad = pads_by_inst[int(ir.pin_instance[pid])].get(str(ir.pin_label[pid]))
        if pad is not None:
            ir.set_pin_pad(pid, pad[0], pad[1], pad[2])
        changed += 1
    return changed


@dataclass(frozen=True)
class AppliedPlanes:
    """What :func:`apply_plane_assignments` put on the IR."""

    #: Every effective assignment (:func:`precis.pcb.planes.
    #: resolve_assignments`), rows and stackup declarations alike.
    assignments: list[Any]
    #: A note per stackup declaration that pours nothing.
    notes: list[str]
    #: Net ids the anneal must not demote: every assignment but the
    #: anneal's own derived write-back.
    locked: frozenset[int]
    #: Net names carrying such a locked assignment — excluded from the
    #: derived write-back.
    locked_names: frozenset[str]


def apply_plane_assignments(
    ir: pcb_ir.PcbIR, rows: list[dict[str, Any]]
) -> AppliedPlanes:
    """Promote every effective plane assignment onto a fresh IR: the
    ``pcb_planes`` rows plus the stackup's own ``plane_net``/``fill``
    declarations (pcb-stackup-regions slices 3-4), each with its region
    and priority. ``promote_plane`` is L1 state the IR never persists, so
    every job that builds an IR re-applies it here — one call for the
    route job, the place job and the capacity map, so the three cannot
    disagree on which nets are planes (gr495287 was the stackup half of
    exactly that drift)."""
    from precis.pcb import planes as pcb_planes

    names = [str(ir.net_name[n]) for n in range(ir.n_nets)]
    assignments, notes = pcb_planes.resolve_assignments(ir.stackup, rows, names)
    net_id = {name: n for n, name in enumerate(names)}
    layer_idx = {str(layer.get("name")): i for i, layer in enumerate(ir.stackup)}
    locked: set[int] = set()
    locked_names: set[str] = set()
    for a in assignments:
        n = net_id.get(a.net)
        li = layer_idx.get(a.layer)
        if n is None or li is None:
            continue
        ir.promote_plane(n, li, region=a.region, priority=a.priority)
        if a.locked:
            locked.add(n)
            locked_names.add(a.net)
    return AppliedPlanes(assignments, notes, frozenset(locked), frozenset(locked_names))


def signal_layers(ir: pcb_ir.PcbIR) -> list[int]:
    """The stackup indices that may carry a routed trace — a thin
    delegate to :func:`precis.pcb.ir.routable_layers` (the single answer
    to "may this layer be routed", now independent of whether the SAME
    layer also carries a copper pour; see that function's docstring).
    Kept under this name (rather than renamed to ``routable_layers``
    everywhere) only because it is this module's own established public
    name; :mod:`precis.pcb.realize` used to keep a second, duplicated
    four-line copy of this exact query rather than import it (its own
    docstring said so) — that duplication is now gone, both modules call
    :func:`precis.pcb.ir.routable_layers` directly."""
    return pcb_ir.routable_layers(ir)


def segment_key(ir: pcb_ir.PcbIR, seg_id: int) -> str:
    """``seg_id``'s durable identity: its two endpoint pins, sorted."""
    a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
    ia, ib = int(ir.pin_instance[a]), int(ir.pin_instance[b])
    ka = _pin_key(str(ir.instance_refdes[ia]), str(ir.pin_label[a]))
    kb = _pin_key(str(ir.instance_refdes[ib]), str(ir.pin_label[b]))
    return "|".join(sorted((ka, kb)))


def apply_route_overrides(
    ir: pcb_ir.PcbIR, routes_by_net: dict[str, dict[str, Any]]
) -> int:
    """Re-apply a design's previously-PERSISTED sketch (pinned side
    choices + layer assignments, ``pcb_routes.topology``/``layer_assign``)
    onto a freshly-built IR, matched via :func:`segment_key`. A segment
    named in the persisted sketch that no longer exists (the netlist
    changed under it) is skipped — never an error, since the optimizer
    will just re-decide it fresh — but COUNTED: the return value is how
    many stored entries matched no segment, and a nonzero count logs a
    warning. The stored keys come from the IR of the EFFECTIVE netlist,
    so callers must build from :func:`precis.pcb.resolved.apply` (the
    latest resolved snapshot over the authored graph); a silent skip here
    is how a wrong netlist hid."""
    seg_by_key = {segment_key(ir, s): s for s in range(ir.n_segments)}
    unmatched = 0
    for row in routes_by_net.values():
        if not row:
            continue
        for field, setter in (("topology", "side"), ("layer_assign", "layer")):
            for entry in row.get(field) or []:
                seg_id = seg_by_key.get(
                    "|".join(sorted((entry.get("a", ""), entry.get("b", ""))))
                )
                value = entry.get(setter)
                if seg_id is None:
                    unmatched += 1
                elif value is not None:
                    if setter == "side":
                        ir.set_side(seg_id, int(value))
                    else:
                        ir.set_layer(seg_id, int(value))
    if unmatched:
        log.warning(
            "pcb: %d stored sketch entr%s matched no segment (stale netlist, "
            "or the resolved snapshot not applied)",
            unmatched,
            "y" if unmatched == 1 else "ies",
        )
    return unmatched


def extract_sketch(ir: pcb_ir.PcbIR) -> dict[str, dict[str, Any]]:
    """The settled IR's per-net sketch, ready for
    :meth:`Store.pcb_routes_write` — ``tree``/``topology``/``layer_assign``
    each a list of segment records keyed by :func:`segment_key`'s two pin
    endpoints, never by the ephemeral ``seg_id``."""
    out: dict[str, dict[str, Any]] = {}
    for seg_id in range(ir.n_segments):
        net_id = int(ir.seg_net[seg_id])
        net_name = str(ir.net_name[net_id])
        a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
        ia, ib = int(ir.pin_instance[a]), int(ir.pin_instance[b])
        ka = _pin_key(str(ir.instance_refdes[ia]), str(ir.pin_label[a]))
        kb = _pin_key(str(ir.instance_refdes[ib]), str(ir.pin_label[b]))
        entry = out.setdefault(
            net_name, {"tree": [], "topology": [], "layer_assign": []}
        )
        entry["tree"].append({"a": ka, "b": kb})
        entry["topology"].append({"a": ka, "b": kb, "side": int(ir.seg_side[seg_id])})
        layer = int(ir.seg_layer[seg_id])
        if layer != pcb_ir.UNSET_LAYER:
            entry["layer_assign"].append({"a": ka, "b": kb, "layer": layer})
    return out


def positions(ir: pcb_ir.PcbIR) -> dict[str, tuple[float, float, float]]:
    """Every PLACED instance's ``(x, y, rot)`` — an instance the optimizer
    never seeded (still ``NaN``) is omitted rather than written as a
    phantom ``(nan, nan, ...)`` pose."""
    out: dict[str, tuple[float, float, float]] = {}
    for i in range(ir.n_instances):
        x, y, rot = float(ir.inst_x[i]), float(ir.inst_y[i]), float(ir.inst_rot[i])
        if x == x and y == y:  # NaN != NaN
            out[str(ir.instance_refdes[i])] = (x, y, rot)
    return out


def placement_drc_findings(
    ir: pcb_ir.PcbIR,
    *,
    capability: CapabilityRow,
    footprints: dict[str, dict[str, Any]] | None = None,
    fixed_copper: list[dict[str, Any]] | None = None,
    outline: list[list[float]] | None = None,
    net_rules: dict[str, NetRules] | None = None,
) -> list[DrcFinding]:
    """The pre-route DRC pass — :func:`precis.pcb.drc.run_placement_drc`
    over a PLACED IR plus only its AUTHORED fixed copper, framed from the
    IR here so the place job and the route job cannot drift on HOW they
    build the pre-route check (this package's recurring defect is one rule,
    two call sites — ``pcb_route.py``'s own outline/footprint comments
    record three prior instances of exactly that). It assembles the same
    three inputs ``handlers/pcb.py::_render_drc`` builds for the full
    ``view='drc'`` run, restricted to the geometry that exists before a
    router draws anything:

    - ``model['pads']`` is :func:`precis.pcb.realize.pads_for_ir` — the
      single pad-geometry definition every other consumer already uses.
    - ``model['copper']`` is the ``fixed_copper`` rows with each row's
      ``net`` normalised to ``''`` when absent, exactly as
      :func:`precis.pcb.realize._fixed_copper_connectivity_model` frames the
      same placement-time input (``fixed: True`` already rides every row —
      what ``check_via_pad_keepout``'s same-net authored-via exemption
      reads). No derived/routed copper: it does not exist yet at this seam.
    - courtyards are :func:`precis.pcb.ir.instance_courtyard_polygon`s
      placed through :func:`precis.pcb.landpattern.place_points` — the SAME
      objects the placer reserves and silk draws, with bottom-side
      membership tracked separately so a part directly under another on the
      OTHER side of the board is not a false overlap (the identical split
      ``_render_drc`` uses via ``courtyard_bottom``).

    Unplaced (NaN-pose) instances contribute no courtyard, the same way
    :func:`positions` omits them.
    """
    # Local imports: realize/cost/drc/landpattern all carry shapely (and
    # cost pulls the whole optimizer objective stack), which this module
    # deliberately keeps out of its import-time surface (module docstring:
    # no realizer/cost logic lives here). Pulled in only when a caller
    # actually asks for the pre-route geometry check.
    from precis.pcb import drc as pcb_drc
    from precis.pcb import edge_mate as pcb_edge_mate
    from precis.pcb import realize as pcb_realize
    from precis.pcb.cost import COURTYARD_MIN_SEPARATION_MM
    from precis.pcb.ir import COURTYARD_CLEARANCE_MM, world_courtyard_polygons

    layers = [str(layer.get("name")) for layer in ir.stackup]
    model = {
        "layers": layers,
        "pads": pcb_realize.pads_for_ir(ir, layers, footprints),
        "copper": [
            {**row, "net": row.get("net") or ""} for row in (fixed_copper or [])
        ],
    }
    # Same clearance + pinless-fallback the PLACER reserved with
    # (`optimize.py`/`_render_drc` both pass exactly these), so this checks
    # the shape placement actually held space for, not a second one.
    courtyards: list[tuple[str, list[tuple[float, float]]]] = []
    courtyard_bottom: dict[str, bool] = {}
    for i, poly in enumerate(
        world_courtyard_polygons(
            ir,
            clearance_mm=COURTYARD_CLEARANCE_MM,
            fallback_half_extent_mm=COURTYARD_MIN_SEPARATION_MM / 2.0,
        )
    ):
        if not poly:
            continue
        refdes = str(ir.instance_refdes[i])
        courtyards.append((refdes, poly))
        courtyard_bottom[refdes] = bool(ir.inst_bottom[i])

    return pcb_drc.run_placement_drc(
        model,
        capability=capability,
        outline=outline,
        courtyards=courtyards,
        courtyard_bottom=courtyard_bottom,
        net_rules=net_rules,
        edge_mates=pcb_edge_mate.records(ir),
        courtyard_clearance_mm=COURTYARD_CLEARANCE_MM,
    )


def fixed_copper_findings(
    ir: pcb_ir.PcbIR,
    footprints: dict[str, dict[str, Any]],
    fixed_copper: list[dict[str, Any]],
    fab_caps: CapabilityRow,
) -> list[DrcFinding]:
    """Every error-severity clearance finding between AUTHORED fixed copper
    and a pad (or other fixed copper) of another net — what ``op='move'``
    of a generator group judges its carried copper by. A pad-to-pad
    overlap is the placer's to fix and is not included."""
    if not fixed_copper:
        return []
    from precis.pcb import drc as pcb_drc
    from precis.pcb import realize as pcb_realize

    layers = [str(layer.get("name")) for layer in ir.stackup]
    model = {
        "layers": layers,
        "copper": list(fixed_copper),
        "pads": pcb_realize.pads_for_ir(ir, layers, footprints),
    }
    return [
        f
        for f in pcb_drc.check_clearance(model, fab_caps)
        if f.severity == "error"
        and any(o.get("ctype") in ("via", "track") for o in f.objects)
    ]


def pose_moved(
    before: tuple[float, float, float] | None, after: tuple[float, float, float]
) -> bool:
    """Whether an anneal (or its legaliser) moved a part between two
    :func:`positions` entries: a new value on any axis, or a first pose
    for a part that had none."""
    if before is None:
        return True
    return any(
        abs(float(a) - float(b)) > 1e-6 for a, b in zip(before, after, strict=True)
    )


def rippable_fixed_copper(
    fixed_copper: list[dict[str, Any]], generator_names: set[str]
) -> list[dict[str, Any]]:
    """The fixed copper a placer may rip up: every row not owned by a live
    generator (``pcb_generators``). A generator's fabric is one unit with
    its parts — op='move' carries it rigidly, and a changed expansion
    retires it whole — so retiring single rows of it would leave a
    half-fabric the generator still claims. An import's source routing
    (``__epro_source``) has no generator row and is rippable."""
    return [r for r in fixed_copper if r.get("generator_name") not in generator_names]


def fixed_copper_branches(
    ir: pcb_ir.PcbIR,
    footprints: dict[str, dict[str, Any]] | None,
    fixed_copper: list[dict[str, Any]],
) -> list[tuple[frozenset[int], frozenset[str]]]:
    """:func:`precis.pcb.connectivity.fixed_copper_branches` at the IR's
    CURRENT poses, each branch as ``(fixed_ids, refdes it touches)`` —
    measured before an anneal, while the copper still meets the pads it
    was laid against."""
    from precis.pcb import connectivity as pcb_connectivity
    from precis.pcb import realize as pcb_realize

    layers = [str(layer.get("name")) for layer in ir.stackup]
    model = {
        "layers": layers,
        "pads": pcb_realize.pads_for_ir(ir, layers, footprints),
        "copper": [{**row, "net": row.get("net") or ""} for row in fixed_copper],
    }
    return [
        (frozenset(int(fixed_copper[i]["fixed_id"]) for i in rows), refdes)
        for rows, refdes in pcb_connectivity.fixed_copper_branches(model)
    ]


def fixed_copper_pad_shorts(
    ir: pcb_ir.PcbIR,
    footprints: dict[str, dict[str, Any]],
    fixed_copper: list[dict[str, Any]],
    capability: CapabilityRow | None,
) -> set[int]:
    """``fixed_id`` of every fixed-copper row in a clearance error against
    a pad at the IR's CURRENT poses — measured on the stored placement,
    this is copper the board already carries as a short."""
    if capability is None:
        return set()
    return {
        int(o["fixed_id"])
        for finding in fixed_copper_findings(ir, footprints, fixed_copper, capability)
        if any(o.get("ctype") == "pad" for o in finding.objects)
        for o in finding.objects
        if o.get("fixed_id") is not None
    }


def fixed_copper_ripup(
    ir: pcb_ir.PcbIR,
    footprints: dict[str, dict[str, Any]],
    fixed_copper: list[dict[str, Any]],
    branches: list[tuple[frozenset[int], frozenset[str]]],
    moved: set[str],
    capability: CapabilityRow | None,
    shorted_before: set[int] | frozenset[int] = frozenset(),
) -> set[int]:
    """The fixed-copper rows a placer write retires (gr489911 part 2, Reto
    2026-10-16: "When the placer moves a part, the fixed copper attached to
    its pads is ripped up"): every branch from :func:`fixed_copper_branches`
    that touched a pad of a part in ``moved``, plus every branch in a
    clearance error against a pad at the NEW poses when that pad belongs to
    a moved part, the branch touches no pad at all, or the branch was
    already shorting a pad on the stored board (``shorted_before``, from
    :func:`fixed_copper_pad_shorts` before the anneal). The last two are
    copper an earlier placement pulled its parts off (prod taz15 after
    jo489777: 172 track-on-foreign-pad shorts), which no router can use or
    clear. Copper that was legal and sits between parts that stayed is
    kept. Only rows inside ``branches`` are ever retired, so a caller
    exempts copper by leaving it out of :func:`fixed_copper_branches`
    (:func:`rippable_fixed_copper`). Only copper is retired, never a pose
    or a lock. No rip-up cost exists in the anneal's objective; this runs
    after it."""
    ripped: set[int] = set()
    for ids, refdes in branches:
        if refdes & moved:
            ripped |= ids
    if capability is None:
        return ripped
    branch_of = {fid: ids for ids, _refdes in branches for fid in ids}
    kept = [row for row in fixed_copper if row["fixed_id"] not in ripped]
    broken = {fid for ids, refdes in branches if not refdes for fid in ids}
    broken |= {fid for f in shorted_before if f in branch_of for fid in branch_of[f]}
    for finding in fixed_copper_findings(ir, footprints, kept, capability):
        pads = [o for o in finding.objects if o.get("ctype") == "pad"]
        if not pads:
            continue
        pad_moved = any(o.get("refdes") in moved for o in pads)
        for o in finding.objects:
            fid = o.get("fixed_id")
            if fid is not None and int(fid) in branch_of:
                if pad_moved or int(fid) in broken:
                    ripped |= branch_of[int(fid)]
    return ripped


def ripup_note(
    n_ripped: int, fixed_copper: list[dict[str, Any]], ripped: set[int]
) -> str:
    """A job summary's rip-up line: how many fixed-copper rows the write
    retired and on which nets, now ordinary unrouted work. Empty when
    nothing was retired."""
    if not n_ripped:
        return ""
    nets = sorted(
        {str(r.get("net") or "?") for r in fixed_copper if r["fixed_id"] in ripped}
    )
    shown = ", ".join(nets[:12]) + (f" (+{len(nets) - 12})" if len(nets) > 12 else "")
    return (
        f"ripped up {n_ripped} fixed-copper row(s) attached to moved parts, "
        f"{len(nets)} net(s): {shown} — their connections are unrouted work\n\n"
    )


def fixed_via_pad_findings(
    ir: pcb_ir.PcbIR,
    footprints: dict[str, dict[str, Any]],
    fixed_copper: list[dict[str, Any]],
    fab_caps: CapabilityRow,
) -> list[DrcFinding]:
    """``via_pad_keepout`` errors for AUTHORED vias against pads — the rule
    ``clearance`` cannot express (a same-net via on a land), judged on
    fixed copper only (``op='move'`` of a generator group's carried vias)."""
    vias = [r for r in fixed_copper if r.get("ctype") == "via"]
    if not vias:
        return []
    from precis.pcb import drc as pcb_drc
    from precis.pcb import realize as pcb_realize

    layers = [str(layer.get("name")) for layer in ir.stackup]
    model = {
        "layers": layers,
        "copper": vias,
        "pads": pcb_realize.pads_for_ir(ir, layers, footprints),
    }
    return [
        f
        for f in pcb_drc.check_via_pad_keepout(model, fab_caps)
        if f.severity == "error"
    ]


def invariant_measure_findings(
    *,
    graph: dict[str, Any],
    measures: list[dict[str, Any]],
    features: list[dict[str, Any]] | None,
    generators_rows: dict[str, dict[str, Any]],
    pads: list[dict[str, Any]],
    copper: list[dict[str, Any]],
    layers: list[str],
) -> list[DrcFinding]:
    """The ``strength='invariant'`` half of the always-valid gate
    (docs/backlog/pcb-missing-constraint-classes.md §F, ruled 2026-10-12):
    every stored measure at that strength, evaluated by
    :func:`precis.pcb.eyes.evaluate_measures` over the SAME data the
    geometric rules see, becomes an error-severity :class:`DrcFinding`
    under rule ``invariant:<metric>`` when VIOLATED — a ``hard`` measure
    only steers, an ``invariant`` one gates.

    A ``pending`` row yields nothing — an invariant that cannot be
    evaluated is not a violation (the same "never gate on a guess" stance
    as the synthesized-pad filter in ``_validity_findings``). The finding's
    ``objects`` come from the evaluator (:func:`precis.pcb.generators.
    evaluate_invariant`), already in :func:`router_nets_of`'s shape, so a
    violation naming ROUTER copper rips that net and one naming only pads
    or authored copper refuses the mutation."""
    inv = [
        m
        for m in measures
        if str(m.get("strength") or "").strip().lower() == "invariant"
    ]
    if not inv:
        return []
    from precis.pcb import drc as pcb_drc
    from precis.pcb import eyes as pcb_eyes

    fabric = {
        "generators": generators_rows,
        "pads": pads,
        "copper": copper,
        "layers": layers,
    }
    findings: list[DrcFinding] = []
    for m, res in zip(
        inv,
        pcb_eyes.evaluate_measures(graph, list(inv), features, fabric=fabric),
        strict=True,
    ):
        if res.get("verdict") != "VIOLATED":
            continue
        goal = res.get("goal")
        findings.append(
            pcb_drc.DrcFinding(
                rule=f"invariant:{res['metric']}",
                severity="error",
                where=(
                    f"{res['metric']} violated — {res.get('detail') or 'off'}"
                    f" (value {res.get('value')}, goal "
                    f"{'' if goal is None else f'{goal:g}'}"
                    f"{'; ' + str(m.get('reason')) if m.get('reason') else ''})"
                ),
                detail=(
                    f"measure #{m.get('measure_id')} (strength=invariant) on "
                    f"{res.get('over') or m.get('metric')}"
                ),
                objects=tuple(res.get("objects") or ()),
            )
        )
    return findings


def router_copper_conflicts(
    ir: pcb_ir.PcbIR,
    footprints: dict[str, dict[str, Any]],
    *,
    router_rows: list[dict[str, Any]],
    carried_rows: list[dict[str, Any]],
    members: set[str],
    fab_caps: CapabilityRow,
) -> dict[str, list[str]]:
    """``{net: [finding line, ...]}`` for every net whose STORED router
    copper (``router_rows``, flat ``pcb_copper_list`` items) has an ERROR
    finding against a moved generator group — the group's own pads
    (``members``, refdes) or its carried fixed copper (``carried_rows``,
    already at their new pose). The model holds ONLY those plus the router
    rows, so a router-vs-router or router-vs-bystander pair (not this
    move's doing) never appears. Clearance, via/pad and via/via keep-out."""
    if not router_rows:
        return {}
    from precis.pcb import drc as pcb_drc
    from precis.pcb import gerber as pcb_gerber
    from precis.pcb import realize as pcb_realize

    layers = [str(layer.get("name")) for layer in ir.stackup]
    all_pads = pcb_realize.pads_for_ir(ir, layers, footprints)
    pads = [p for p in all_pads if str(p.get("refdes")) in members]
    # Coating is a board fact: a moved driver group without the electrode
    # array still sits on the parylene-coated board.
    coated = pcb_gerber.declares_parylene_coat({"pads": all_pads})
    model = {
        "layers": layers,
        "pads": pads,
        "copper": [
            *({**r, "net": r.get("net") or ""} for r in carried_rows),
            *({**r, "derived": True} for r in router_rows),
        ],
    }
    findings = [
        *pcb_drc.check_clearance(model, fab_caps, coated=coated),
        *pcb_drc.check_via_pad_keepout(model, fab_caps),
        *pcb_drc.check_via_via_keepout(model, fab_caps),
    ]
    out: dict[str, list[str]] = {}
    for f in findings:
        if f.severity != "error":
            continue
        objs = f.objects
        if f.rule == "clearance":
            involves_group = any(not o.get("derived") for o in objs)
        elif f.rule == "via_via_keepout":
            involves_group = any(
                not (o.get("a_derived") and o.get("b_derived")) for o in objs
            )
        else:  # via_pad_keepout: the other side is always a group pad
            involves_group = True
        if not involves_group:
            continue
        for obj in objs:
            for net_key, flag_key in _ROUTER_NET_KEYS:
                if obj.get(flag_key) and obj.get(net_key):
                    out.setdefault(str(obj[net_key]), []).append(f"{f.rule}: {f.where}")
    return out


def _copper_model(
    ir: pcb_ir.PcbIR,
    copper_rows: list[dict[str, Any]],
    *,
    footprints: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    """Pads + the given copper rows + mounting-hole drills, quantized the
    way :func:`precis.pcb.realize.to_gerber_model` quantizes it — the ONE
    board model shape both post-route gates judge
    (:func:`routed_drc_findings`, :func:`split_nets`) and the incumbent
    side of ``pcb_route``'s worse-board guard
    (:func:`stored_copper_drc_findings`) reads."""
    from precis.pcb import realize as pcb_realize

    layers = [str(layer.get("name")) for layer in ir.stackup]
    return pcb_realize._quantized(
        {
            "layers": layers,
            "pads": pcb_realize.pads_for_ir(ir, layers, footprints),
            "copper": copper_rows,
            "drills": [
                {
                    "x": h.x,
                    "y": h.y,
                    "dia_mm": h.drill_mm,
                    "plated": bool(h.plated),
                }
                for h in ir.mounting_holes
            ],
        }
    )


#: ``(net key, derived key)`` pairs a finding's ``objects`` use, per rule
#: family (clearance/width/ring/edge/npth: plain; via-pad: ``via_``; via-via:
#: ``a_``/``b_``). A net is ROUTER copper only where the paired flag is set.
_ROUTER_NET_KEYS = (
    ("net", "derived"),
    ("via_net", "via_derived"),
    ("a_net", "a_derived"),
    ("b_net", "b_derived"),
)


def router_nets_of(finding: DrcFinding) -> set[str]:
    """The nets whose ROUTER copper (``derived``-tagged) a finding's objects
    name — what yields to that finding. Empty when only pads and authored
    copper are involved."""
    return {
        str(obj[net_key])
        for obj in finding.objects
        for net_key, flag_key in _ROUTER_NET_KEYS
        if obj.get(flag_key) and obj.get(net_key)
    }


def finding_refdes(finding: DrcFinding) -> list[str]:
    """Every part a finding names, in order, deduplicated — a pad's
    ``refdes`` (or its ``a_``/``b_`` halves) and a courtyard pair's
    ``a``/``b`` (a ``hole…`` label is not a part)."""
    out: list[str] = []
    for o in finding.objects:
        for key in ("refdes", "a_refdes", "b_refdes", "a", "b"):
            v = o.get(key)
            if v and not str(v).startswith("hole") and str(v) not in out:
                out.append(str(v))
    return out


def placement_drc_report(
    findings: list[DrcFinding],
    *,
    fixed_refdes: Collection[str] = (),
    subject: str = "as placed",
) -> tuple[int, str | None]:
    """``(error_count, block)`` — the loud, NAMED account of a pre-route DRC
    pass a job appends to its ``job_summary`` chunk.

    ``block`` is ``None`` when there is nothing to say (no errors): a clean
    placement subset is deliberately SILENT here rather than a reassuring
    "clean" line, because this is a SUBSET check — it has not looked at
    trace width, connectivity or anything a router draws, so "clean" would
    overclaim exactly the way ``handlers/pcb.py``'s pads-only DRC note
    guards against. When it DOES fire, it names every erroring rule, its
    location and the measured margin — the "writing a named finding, not
    just a bit flipped" contract the report asks for — so the first time a
    placement is illegal is at PLACE/ROUTE time, in the job's own account,
    not the next time a human happens to open ``view='drc'``. Warnings
    (house-margin tier) are intentionally not surfaced here: the floor
    ERRORS are the manufacturability facts this early gate exists to raise;
    the full ``view='drc'`` run, which resolves per-net class rules, is
    where the margin tier belongs."""
    errors = [f for f in findings if f.severity == "error"]
    if not errors:
        return 0, None
    lines = [
        f"pre-route DRC: {len(errors)} placement error(s) — unmanufacturable "
        f"{subject}, before a single trace is routed (run view='drc' for the "
        "full check):"
    ]
    violators: list[str] = []
    for f in errors:
        margin = "" if f.margin_mm is None else f" ({f.margin_mm:+.3f}mm)"
        lines.append(f"  ✗ {f.rule}: {f.where}{margin} — {f.detail}")
        violators += [r for r in finding_refdes(f) if r not in violators]
    # gr489913 (b): the fixed='both' hint only where a violator IS fixed —
    # printed unconditionally it read as "your parts are locked" on a board
    # with none fixed. Free violators are named as the ones to pin or move.
    fixed = set(fixed_refdes)
    pinned = [r for r in violators if r in fixed]
    free = [r for r in violators if r not in fixed]
    if pinned:
        lines.append(
            f"  fixed violator(s) {', '.join(pinned)} cannot be auto-shifted; "
            "unpin or move them by hand"
        )
    if free:
        lines.append(f"  violator(s) you can pin or move: {', '.join(free)}")
    return len(errors), "\n".join(lines)


def _authored_measure_meta(m: dict[str, Any]) -> tuple[str, ...]:
    """The hash element for a measure row's AUTHORED ``meta`` keys (``()``
    when there are none, so rows without meta hash exactly as before)."""
    authored = {k: v for k, v in (m.get("meta") or {}).items() if k != "snapped"}
    if not authored:
        return ()
    return (json.dumps(authored, sort_keys=True, default=str),)


def content_hash(
    graph: dict[str, Any],
    params: dict[str, Any],
    *,
    session_state: dict[str, Any] | None = None,
) -> str:
    """A stable digest of the netlist+placement+params an op runs against
    — the ``content-hash`` half of the ``(design, op, content-hash)``
    idempotency key (backlog, verbatim). Deliberately excludes the
    volatile ``route_status``/``board_id`` summary fields
    :meth:`Store.pcb_graph` also carries — including those would make a
    route job's OWN write-back change the hash of an otherwise-identical
    re-submit, defeating idempotency.

    ``session_state`` (gr266041) covers the six extra store surfaces
    :mod:`precis.workers.job_types.pcb_place`/``pcb_route`` read that
    ``graph``/``params`` don't: features (outline/mounting holes),
    route-topology overrides, authored pin swaps (none exist: the key is
    kept so digests keep their shape), plane assignments, measures, and
    fixed copper. Callers (:meth:`PcbHandler._enqueue_op`) hand in the RAW
    store rows (``Store.pcb_features_list``/``pcb_routes_get``/
    ``pcb_planes_list``/``pcb_measures_list``/
    ``pcb_fixed_copper_list``, verbatim) — the narrowing to a stable,
    human-authored projection happens HERE, not at the call site, so
    there is exactly one place that can get it wrong.

    Deliberately excluded, and why:

    - ``pcb_routes_get``'s ``status``/``fail``/``meta``/``tree``/
      ``layer_assign`` — all job-written checkpoint fields the SAME
      ``pcb_route`` run persists back (:meth:`Store.pcb_routes_write`).
      Only ``topology`` (the human/``pin_side`` override) is hashed per
      net.
    - any ``pcb_planes_list`` row whose ``source`` is ``'derived'`` —
      optimizer write-back (:meth:`Store.pcb_planes_replace_derived`).
      Only ``source == 'authored'`` rows are hashed. The resolved netlist
      snapshot (``pcb_netlist_snapshots``) is job-written the same way and
      is never passed in.

    Hashing the job-written side of either surface would repeat this
    function's own ``route_status``/``board_id`` hazard one layer down:
    a completed job's write-back would change the digest of an
    otherwise-identical resubmit and mint a fresh duplicate job on every
    single re-check — the opposite failure from the one this parameter
    exists to fix. ``None`` (the default) reproduces the pre-gr266041
    digest byte-for-byte, so no in-flight idempotency key changes shape
    on deploy.

    Expected ``session_state`` shape (each key optional, missing ==
    empty), matching the store methods' own return shapes verbatim::

        {
            "features": Store.pcb_features_list(ref_id),
            "routes": Store.pcb_routes_get(ref_id),
            "planes": Store.pcb_planes_list(ref_id),
            "measures": Store.pcb_measures_list(ref_id),
            "fixed_copper": Store.pcb_fixed_copper_list(board_id),
        }
    """
    instances = sorted(
        (
            i["refdes"],
            i.get("x"),
            i.get("y"),
            i.get("rot"),
            i.get("fixed"),
        )
        # the soft pin is placement input like `fixed` itself — a pin
        # change must not collapse onto a job run without it. Appended
        # only when set, so unpinned designs keep their pre-pin digest.
        + ((i["max_travel_mm"],) if i.get("max_travel_mm") is not None else ())
        for i in graph.get("instances") or []
    )
    nets = sorted(
        (
            n["name"],
            n.get("net_class"),
            tuple(sorted((m["refdes"], m["pin"]) for m in n.get("members") or [])),
        )
        for n in graph.get("nets") or []
    )
    stackup = (graph.get("board") or {}).get("stackup")
    payload: dict[str, Any] = {
        "instances": instances,
        "nets": nets,
        "stackup": stackup,
        "params": params,
    }
    if session_state is not None:
        state = session_state
        features = sorted(
            (
                f.get("ftype"),
                f.get("x"),
                f.get("y"),
                f.get("rot"),
                f.get("layer"),
                f.get("fixed"),
                json.dumps(f.get("geom"), sort_keys=True, default=str),
                f.get("note"),
            )
            for f in state.get("features") or []
        )
        routes = sorted(
            (net_name, json.dumps(row.get("topology"), sort_keys=True, default=str))
            for net_name, row in (state.get("routes") or {}).items()
        )
        pin_swaps = sorted(
            (p.get("refdes"), p.get("pin"), p.get("net"))
            for p in state.get("pin_swaps") or []
            if p.get("source", "authored") == "authored"
        )
        # `region` and `priority` change the pour (pcb-stackup-regions
        # slice 4), so a re-run with a new region must not reuse a cached
        # result. `region` replaced `region_hint` (0219 copied it across;
        # no writer ever set the old column), and priority joins the tuple
        # only when set, so a pre-region row keeps its digest.
        planes = sorted(
            (
                (
                    p.get("layer"),
                    p.get("net"),
                    json.dumps(p.get("region"), sort_keys=True)
                    if p.get("region") is not None
                    else None,
                )
                + ((int(p["priority"]),) if p.get("priority") else ())
            )
            for p in state.get("planes") or []
            if p.get("source", "authored") == "authored"
        )
        # `meta` is hashed only for its AUTHORED keys (align's axis/offset)
        # and only when there are any: `snapped` is job-written, and an
        # empty meta must leave every pre-align row's digest unchanged.
        # Sorted by their JSON form: operands are dicts and goal/weight may
        # be None, so two rows tied on the leading fields cannot compare
        # natively (TypeError on any board with two same-shaped measures).
        measures = sorted(
            (
                (
                    m.get("metric"),
                    m.get("direction"),
                    m.get("goal"),
                    m.get("strength"),
                    m.get("weight"),
                    tuple(m.get("operands") or []),
                    m.get("reason"),
                    *_authored_measure_meta(m),
                )
                for m in state.get("measures") or []
            ),
            key=lambda row: json.dumps(row, sort_keys=True, default=str),
        )
        fixed_copper = sorted(
            json.dumps(fc, sort_keys=True, default=str)
            for fc in state.get("fixed_copper") or []
        )
        payload["session_state"] = {
            "features": features,
            "routes": routes,
            "pin_swaps": pin_swaps,
            "planes": planes,
            "measures": measures,
            "fixed_copper": fixed_copper,
        }
    payload_json = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.blake2b(payload_json.encode("utf-8"), digest_size=12).hexdigest()


def _declared_capability(process: str, stackup: list[dict[str, Any]]) -> CapabilityRow:
    """The capability row an authored ``meta.process`` names — checked
    against the stackup's layer count (the row's ``layers`` figure), so a
    ``flex-2layer-*`` board on four layers refuses as contradictory
    authoring rather than checking against the wrong process
    (:mod:`precis.pcb.capabilities`' module docstring). Raises
    ``ValueError`` for an unknown name too — a typo'd selector that
    silently fell back to the derived row would check the board against a
    process nobody picked."""
    from precis.pcb.capabilities import capability_for, process_layer_counts

    counts = process_layer_counts()
    if process not in counts:
        raise ValueError(
            f"board meta.process {process!r} is not a capability-table "
            f"process — known: {sorted(counts)}"
        )
    want = counts[process]
    if want != len(stackup):
        raise ValueError(
            f"board meta.process {process!r} is a {want}-layer process; "
            f"the stackup has {len(stackup)} layer(s)"
        )
    return capability_for(process)


def board_capability(
    stackup: list[dict[str, Any]], profile: str | None = None, *, meta: Any = None
) -> CapabilityRow:
    """:func:`stackup_capability` that raises ``ValueError`` (from
    :func:`precis.pcb.drc.process_for_stackup`) for an unsupported layer
    count — for the DRC and route paths, which refuse rather than judge by
    a missing row.

    ``meta`` is the board's meta dict: an authored ``process`` key
    overrides the layer-count derivation entirely (gr520549's board-level
    process selector — the gate, DRC and fab export all read this)."""
    from precis.pcb import drc as pcb_drc
    from precis.pcb.capabilities import apply_profile, capability_for
    from precis.pcb.profiles import profile_rules

    process = (meta or {}).get("process")
    if process:
        row = _declared_capability(str(process), stackup)
    else:
        row = capability_for(pcb_drc.process_for_stackup(stackup))
    return apply_profile(row, profile_rules(profile))


def stackup_capability(
    stackup: list[dict[str, Any]], profile: str | None = None, *, meta: Any = None
) -> CapabilityRow | None:
    """This board's fab-capability row, or ``None`` for a layer count
    :func:`precis.pcb.drc.process_for_stackup` has no row for.

    ``profile`` is the board's authored profile name (``None``/``'prod'``
    return the fab row unchanged); a non-prod profile is laid over the row
    by :func:`precis.pcb.capabilities.apply_profile`, so every consumer of
    this lookup sees the raised house tier and strict set. ``meta`` is the
    board's meta dict (``process`` selector) — an authored selection's
    own errors (unknown name, layer-count contradiction) PROPAGATE, unlike
    the derived path's unsupported-count ``None``: a declared-but-wrong
    process is authored fact to fix, not an absent figure.

    Was ``PcbHandler._stackup_capability`` until ``pcb_argue`` needed the
    same lookup and the workers->handlers layering contract refused the
    import. ``None`` rather than raising, for exactly the reason
    :func:`furniture_clearance_mm` documents — a board on an unsupported
    layer count still has to export and preview."""
    if meta and meta.get("process"):
        return board_capability(stackup, profile, meta=meta)
    try:
        return board_capability(stackup, profile)
    except ValueError:
        return None


def furniture_clearance_mm(stackup: list[dict[str, Any]]) -> float | None:
    """The clearance :func:`precis.pcb.furniture.board_furniture` hands to
    :func:`precis.pcb.planes.cut_antipads` for a fiducial's antipad ring —
    resolved from :mod:`precis.pcb.capabilities` through
    :func:`precis.pcb.rules.resolve_net_rules`, the SAME resolver the DRC
    render calls per-net. No net-class override applies to a fiducial (it
    carries no net), so this is the fab-floor figure.

    ``None`` for a stackup layer count with no capability row, rather
    than raising: the gerber and fab-SVG renders (unlike the DRC render)
    never called ``capability_for`` before this ring existed, and a board
    that has not been routed yet — so carries no pour, hence nothing for
    ``cut_antipads`` to cut — must still export/preview cleanly on an
    unsupported layer count."""
    capability = stackup_capability(stackup)
    if capability is None:
        return None
    from precis.pcb.rules import resolve_net_rules

    return resolve_net_rules("", layer_is_outer=True, fab_caps=capability).clearance_mm


#: The label abbreviations a real part label uses for "electrolytic"/
#: "tantalum"/generic "polarized" — the polarity test
#: :func:`polarized_refdes` shares with every silk caller.
_POLARIZED_LABEL_RE = re.compile(r"ELEC|TANT|POL", re.IGNORECASE)


def polarized_refdes(design: dict[str, Any]) -> frozenset[str]:
    """The refdes of every instance that carries REAL polarity — the
    ``polarized`` set :func:`~precis.pcb.silk.build_silk` (via
    :func:`~precis.pcb.furniture.board_furniture`) uses to decide which
    R/C/L/FB parts still get a pin-1 mark (that module's own docstring,
    "Not every part needs one"). Was ``PcbHandler._polarized_refdes`` —
    moved here with ``stackup_capability``/``furniture_clearance_mm`` when
    ``pcb_argue`` needed the same determination and the workers->handlers
    layering contract refused the import.

    The IR silk builds from has refdes but no labels, so this happens off
    the raw design's ``instances`` list. An instance is polarized when it
    declares ``"polarized": true`` outright, OR its ``label`` matches
    (case-insensitively) one of ELEC/TANT/POL — the EXPLICIT flag, either
    way, is authoritative over the label test (gr520373): a ``polarized``:
    false`` part keeps its unpolarized verdict even under an ELEC/TANT/POL
    label, while a label remains the fallback for a design with no flag at
    all (the common case — free text a BOM author may not have written
    consistently)."""
    out: set[str] = set()
    for i in design.get("instances") or []:
        refdes = str(i.get("refdes") or "")
        if not refdes:
            continue
        if i.get("polarized") is not None:
            if i["polarized"]:
                out.add(refdes)
            continue
        label = str(i.get("label") or "")
        if _POLARIZED_LABEL_RE.search(label):
            out.add(refdes)
    return frozenset(out)


def unpolarized_refdes(design: dict[str, Any]) -> frozenset[str]:
    """The refdes of every instance EXPLICITLY declared unpolarized —
    ``"polarized": false`` on the component (gr520373). This is the
    footprint-level "no pin-1 mark" opt-out that used to work only by
    accident of refdes family (R/C/L/FB were exempt regardless); any
    family may now declare it — the authored wetness-comb ``SNS``
    footprints whose forced pin-1 marks the gripe reported. Deliberate
    skips, not failures: ``build_silk`` renders them census
    ``not_applicable``, never ``dropped``, so ``check_silk_missing``
    stays quiet. Reads the same ``design['instances']`` shape
    :func:`polarized_refdes` does."""
    return frozenset(
        refdes
        for i in design.get("instances") or []
        if (refdes := str(i.get("refdes") or "")) and i.get("polarized") is False
    )


def label_spots(design: dict[str, Any]) -> dict[str, str]:
    """The per-instance refdes-label spot overrides — the ``label_spots``
    map :func:`~precis.pcb.silk.build_silk` (via
    :func:`~precis.pcb.furniture.board_furniture`) reads (gr496552: "we
    can move it"). Like :func:`polarized_refdes` this runs off the raw
    design's ``instances`` list: the override lives on
    ``pcb_instances.meta["label_spot"]`` (``op='move'`` writes it,
    ``pcb_load`` hoists it), so a plain dict is all a caller needs.
    Values pass through unchecked — the write path validates against
    ``silk.LABEL_SPOT_ORDER`` and the builder ignores a name that matches
    no candidate."""
    return {
        str(i["refdes"]): str(i["label_spot"])
        for i in design.get("instances") or []
        if i.get("label_spot")
    }


__all__ = [
    "OUTLINE_FIX_HINT",
    "apply_real_pin_offsets",
    "apply_route_overrides",
    "board_capability",
    "build_ir",
    "content_hash",
    "extract_sketch",
    "fixed_vias_from_copper",
    "footprints_by_refdes",
    "furniture_clearance_mm",
    "keepouts_from_features",
    "label_spots",
    "land_rects_by_instance",
    "local_footprint_names_by_refdes",
    "mounting_holes_from_features",
    "outline_defect",
    "outline_from_features",
    "polarized_refdes",
    "positions",
    "segment_key",
    "signal_layers",
    "sorted_graph",
    "stackup_capability",
    "unpolarized_refdes",
]

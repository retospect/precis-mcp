"""EasyEDA Pro ``.epro2`` -> the ``pcb`` kind (pcb-epro-import slice 1b).

The impure half of the importer: :mod:`precis.pcb.epro` turns ``bytes``
into a :class:`~precis.pcb.epro.Design` and makes every judgement call,
this module writes it. It adds NOTHING to what the reader decided — so a
``--dry-run`` and a real import cannot report different things — and owns
only the four questions that need a database:

1. **Ordering.** Footprints before components (a component names one),
   nets before the stackup (plane assignment resolves net names).
2. **The stackup**, derived from the board's own ``LAYER`` records rather
   than assumed, with an inner layer carrying exactly one net's pour
   becoming a ``plane`` — which is the right precis model for a pour and
   the reason pours are not imported as copper. An OUTER-layer pour that
   covers most of the board is not copper either: it lands as
   ``pour_nets`` on the still-signal layer and a ``pcb_planes`` row, so
   the router skips the net the source already carries by fill
   (gr486958 — taz15's B.Cu GND / F.Cu 3V3 zones).
3. **Refusals** that only a loaded design can see: a non-empty slug, and
   a layer count the router would reject later anyway.
4. **Provenance**, stamped on the ref so a future format failure can be
   dated to an editor version and a source file.

Fresh routed intake preserves source tracks/arcs/vias as authored fixed
copper (gr470192, Reto 2026-10-06), in the same transaction as the design.
Each accepted LINE/ARC remains one track row: counts and bendy corners
survive. This is source geometry, not an inferred routing sketch or a
validity certificate. The older 2026-09-30 regenerate-only ruling remains
available as copper="none", also the conservative partial-update default:
changed copper cannot safely attach to deliberately unchanged nets/outline.
Every import still reports source widths, vias and gaps through
:func:`report_copper` and :mod:`precis.pcb.copper_report`.

``--update`` (the rest of 1c) re-reads the source onto an existing import
of the same board: moved parts take their new pose, new parts are added,
and everything else the source changed — a removed part, a rewired pin,
the outline — is reported and left alone (:class:`UpdatePlan` says why).
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from shapely.geometry import Polygon
from shapely.ops import unary_union

from precis.pcb import copper_report, datasheets, drc, epro, ir, padplace
from precis.pcb.capabilities import CapabilityRow, capability_for
from precis.pcb.rules import NetRules, resolve_net_rules
from precis.store._pcb_ops import (
    _LOCAL_FP_MATCH_TOL_MM,
    footprint_differ,
)
from precis.store._pcb_ops import (
    _normalize_local_footprint as normalize_local_footprint,
)

#: What ``op='place'``/``op='route'`` accept (``_enqueue_op``) and what
#: ``_FIXED_COPPER_FAB_PROCESS`` is pinned to. Refusing HERE, naming the
#: board's actual count, beats letting it surface as a confusing refusal
#: at the user's first route — which is the step the import exists for.
_SUPPORTED_LAYER_COUNTS = (4,)

log = logging.getLogger(__name__)


class EproImportError(ValueError):
    """The archive parses but cannot be imported as it stands."""


@dataclass
class ImportResult:
    """What the import did, for the CLI to print and a test to assert on."""

    slug: str
    ref_id: int
    created: bool
    counts: dict[str, int] = field(default_factory=dict)
    stackup: list[dict[str, Any]] = field(default_factory=list)
    planes: dict[str, str] = field(default_factory=dict)
    #: ``{layer name: [pour-carried net names]}`` — fills covering most of
    #: the board on still-signal layers (``derive_stackup``). A layer
    #: listing exactly one also gained a ``pcb_planes`` row.
    pour_nets: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    copper: copper_report.CopperReport | None = None
    update: UpdatePlan | None = None
    #: ``(refdes, why)`` for parts that look alignment-critical
    #: (:func:`alignment_candidates`). Listed, never locked.
    alignment_candidates: list[tuple[str, str]] = field(default_factory=list)


#: Refdes prefixes of parts a user sees or touches through an enclosure:
#: an LED, a switch or a display usually sits under a hole or light pipe.
_USER_FACING_PREFIXES = frozenset(
    {"LED", "DS", "SW", "BTN", "S", "K", "DISP", "LCD", "OLED", "BZ", "BUZ"}
)
#: The same, read off the footprint name when the refdes is generic.
_USER_FACING_FOOTPRINT_WORDS = ("LED", "SWITCH", "BUTTON", "TACT", "DISPLAY", "OLED")
#: Connector prefixes; one near the board edge mates with something outside.
_CONNECTOR_PREFIXES = frozenset({"J", "CN", "P", "USB", "X", "CON"})
#: A connector whose origin is within this of the outline polygon's
#: boundary counts as an edge connector.
_EDGE_CONNECTOR_MM = 5.0


def _refdes_prefix(refdes: str) -> str:
    out = ""
    for ch in refdes:
        if not ch.isalpha():
            break
        out += ch
    return out.upper()


def _distance_to_outline(x: float, y: float, path: list[list[float]]) -> float:
    """Distance from ``(x, y)`` to the closed polyline ``path`` (the board
    edge, inner edges of a cut-out included)."""
    best = float("inf")
    for i, (ax, ay) in enumerate(path):
        bx, by = path[(i + 1) % len(path)]
        dx, dy = float(bx) - float(ax), float(by) - float(ay)
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0.0 else ((x - ax) * dx + (y - ay) * dy) / seg2
        t = min(1.0, max(0.0, t))
        best = min(best, math.hypot(x - (ax + t * dx), y - (ay + t * dy)))
    return best


def alignment_candidates(design: epro.Design) -> list[tuple[str, str]]:
    """Parts that LOOK alignment-critical, for the import report: user-facing
    parts (LED/switch/display, by refdes prefix or footprint name), parts
    that carry their own holes, and connectors near the board edge. Parts
    the source already locked are left out (they are locked anyway).

    A heuristic list, never a lock (Reto 2026-10-02, review-queue
    pcb-easyeda-round-trip-5): which parts really are aligned to something
    takes the author's knowledge. The author confirms one by locking it
    (``op='move'`` ``fixed=``) or with an ``align`` measure."""
    holed_fp = {str(f["name"]) for f in design.footprints if f.get("holes")}
    holed = {
        str(c["refdes"])
        for c in design.components
        if str(c.get("footprint") or "") in holed_fp
    }
    outline = next(
        (f["geom"]["path"] for f in design.features if f.get("ftype") == "outline"),
        None,
    )
    out: list[tuple[str, str]] = []
    for comp in sorted(design.components, key=lambda c: str(c["refdes"])):
        if comp.get("fixed") is not None:
            continue
        refdes = str(comp["refdes"])
        prefix = _refdes_prefix(refdes)
        fp = str(comp.get("footprint") or "").upper()
        why: list[str] = []
        if prefix in _USER_FACING_PREFIXES or any(
            w in fp for w in _USER_FACING_FOOTPRINT_WORDS
        ):
            why.append("user-facing (LED/switch/display)")
        if refdes in holed:
            why.append("has its own hole(s)")
        if prefix in _CONNECTOR_PREFIXES and outline:
            x, y = float(comp.get("x") or 0.0), float(comp.get("y") or 0.0)
            edge = _distance_to_outline(x, y, outline)
            if edge <= _EDGE_CONNECTOR_MM:
                why.append(f"connector {edge:.1f} mm from the board edge")
        if why:
            out.append((refdes, "; ".join(why)))
    return out


#: Below these a pose is unchanged. EasyEDA stores mils as floats, so a
#: round trip through mm can differ in the last bits without anyone
#: having moved anything.
_POS_TOL_MM = 1e-3
_ROT_TOL_DEG = 1e-2


@dataclass
class UpdatePlan:
    """What ``--update`` would do to an existing import, decided before
    anything is written so a ``--dry-run`` shows exactly the same thing.

    Only POSES, NEW PARTS and refreshed footprint GEOMETRY (same pads and
    pin map) are applied. Everything else the source
    changed is reported and left alone, because precis is where the
    design is being corrected: a re-import that rewired a pin or retired
    a part would silently undo a fix made here since the last import.
    """

    #: ``(refdes, (x, y, rot, layer) before, after, was fixed)``
    moved: list[tuple[str, tuple[Any, ...], tuple[Any, ...], bool]] = field(
        default_factory=list
    )
    added: list[str] = field(default_factory=list)
    #: On the board, not in the source. Kept: it may be a part added in
    #: precis, and retiring it would drop its connections.
    removed: list[str] = field(default_factory=list)
    #: ``"R1.2: board GND, source VCC"`` for surviving parts only.
    rewired: list[str] = field(default_factory=list)
    features_added: int = 0
    features_missing: int = 0
    #: Stored footprints whose geometry (pad sizes, shapes, courtyard)
    #: differs from a fresh read of the source with the same pad numbers
    #: and pin map. Refreshed: the reader improved, or the author fixed the
    #: footprint, and neither changes what a pin means.
    footprints_refreshed: list[str] = field(default_factory=list)
    #: Same name, different pad numbers or pin map: not applied, since the
    #: board's connections name pins of the stored version.
    footprints_differ: list[str] = field(default_factory=list)
    #: Legacy part-owned ``mounting_hole`` features (``feature_id``) that
    #: the part's footprint now carries as footprint holes: retired so the
    #: hole is not doubled (and so it follows its part from now on).
    legacy_hole_features: list[int] = field(default_factory=list)
    #: Live measures with a ``feature_id`` operand on a retired legacy hole
    #: (one warning line each). Reported, never edited or deleted.
    legacy_hole_measures: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"update: {len(self.moved)} moved, {len(self.added)} added, "
            f"{len(self.removed)} not in the source (kept), "
            f"{len(self.rewired)} pin(s) wired differently (not applied), "
            f"{len(self.footprints_refreshed)} footprint(s) refreshed"
        ]
        for refdes, old, new, fixed in self.moved:
            lock = " (locked; moved anyway — the source is the author's edit)"
            out.append(
                f"  moved {refdes}: ({old[0]:.3f}, {old[1]:.3f}) {old[2]:g}° "
                f"{old[3]} -> ({new[0]:.3f}, {new[1]:.3f}) {new[2]:g}° {new[3]}"
                + (lock if fixed else "")
            )
        out += [f"  added {r}" for r in self.added]
        out += [f"  kept {r}: on the board, not in the source" for r in self.removed]
        out += [f"  differs {w}" for w in self.rewired]
        out += [f"  refreshed footprint {n}" for n in self.footprints_refreshed]
        out += [
            f"  footprint {n}: pad numbers or pin map changed in the source "
            f"(not applied; import to a fresh slug to take it)"
            for n in self.footprints_differ
        ]
        if self.legacy_hole_features:
            out.append(
                f"  retired {len(self.legacy_hole_features)} part-owned "
                f"mounting-hole feature(s): their footprints now carry the "
                f"holes, which follow the part"
            )
        out += [f"  warning: {w}" for w in self.legacy_hole_measures]
        if self.features_added or self.features_missing:
            out.append(
                f"  features: {self.features_added} in the source but not on "
                f"the board, {self.features_missing} on the board but not in "
                f"the source; --update does not change the outline or holes"
            )
        return out


def _canon(v: Any) -> Any:
    """A comparable form of a feature: floats rounded to the tolerance."""
    if isinstance(v, float):
        return round(v, 3)
    if isinstance(v, dict):
        return tuple(sorted((k, _canon(x)) for k, x in v.items()))
    if isinstance(v, list | tuple):
        return tuple(_canon(x) for x in v)
    return v


def _feature_key(f: dict[str, Any]) -> Any:
    return _canon(
        {k: f.get(k) for k in ("ftype", "x", "y", "geom") if f.get(k) is not None}
    )


def _pose(c: dict[str, Any]) -> tuple[float, float, float, str]:
    return (
        float(c.get("x") or 0.0),
        float(c.get("y") or 0.0),
        float(c.get("rot") or 0.0) % 360.0,
        str(c.get("layer") or "top"),
    )


def _same_pose(a: tuple[Any, ...], b: tuple[Any, ...]) -> bool:
    drot = abs(a[2] - b[2]) % 360.0
    return (
        abs(a[0] - b[0]) <= _POS_TOL_MM
        and abs(a[1] - b[1]) <= _POS_TOL_MM
        and min(drot, 360.0 - drot) <= _ROT_TOL_DEG
        and a[3] == b[3]
    )


def plan_update(
    graph: dict[str, Any],
    features: list[dict[str, Any]],
    design: epro.Design,
) -> UpdatePlan:
    """Diff an existing import (``store.pcb_graph`` + its live features)
    against a fresh read of the source. Pure; refuses (raises
    :class:`EproImportError`) when a surviving part's footprint changed,
    since its pins and connections would no longer mean the same pads."""
    plan = UpdatePlan()
    board = {str(i["refdes"]): i for i in graph["instances"]}
    source = {str(c["refdes"]): c for c in design.components}

    swapped = sorted(
        r
        for r in board.keys() & source.keys()
        if (board[r].get("footprint") or None) != (source[r].get("footprint") or None)
    )
    if swapped:
        raise EproImportError(
            f"{len(swapped)} part(s) changed footprint in the source "
            f"({', '.join(swapped)}); their pins would name different pads, "
            f"which --update cannot reconcile. Import to a fresh slug"
        )

    for refdes in sorted(board.keys() & source.keys()):
        old, new = _pose(board[refdes]), _pose(source[refdes])
        if not _same_pose(old, new):
            fixed = board[refdes].get("fixed") is not None
            plan.moved.append((refdes, old, new, fixed))
    plan.added = sorted(source.keys() - board.keys())
    plan.removed = sorted(board.keys() - source.keys())

    on_board = {
        (str(m["refdes"]), str(m["pin"])): str(n["name"])
        for n in graph["nets"]
        for m in n["members"]
    }
    in_source = {
        (str(k["refdes"]), str(k["pin"])): str(k["net"]) for k in design.connections
    }
    surviving = board.keys() & source.keys()
    for key in sorted(on_board.keys() | in_source.keys()):
        if key[0] not in surviving:
            continue
        b, s = on_board.get(key), in_source.get(key)
        if b != s:
            plan.rewired.append(
                f"{key[0]}.{key[1]}: board {b or 'unconnected'}, "
                f"source {s or 'unconnected'}"
            )

    have = [_feature_key(f) for f in features]
    want = [_feature_key(f) for f in design.features]
    plan.features_added = sum(1 for k in want if k not in have)
    plan.features_missing = sum(1 for k in have if k not in want)
    return plan


def _resolve_rules(
    nets: list[dict[str, Any]],
    net_classes: dict[str, Any],
    capability: CapabilityRow,
    copper: list[dict[str, Any]],
    outer: set[str],
) -> dict[str, NetRules]:
    """``net name -> NetRules`` the way the router resolves them.

    Width depends on the layer only through a current annotation (inner
    copper needs more width for the same current), so a net is resolved as
    outer when every track the SOURCE drew for it is on an outer layer —
    the comparison is against what precis would draw where the author did.
    """
    inner_nets = {
        str(c.get("net"))
        for c in copper
        if c.get("ctype") == "track" and c.get("layer") not in outer
    }
    out: dict[str, NetRules] = {}
    for n in nets:
        name = str(n["name"])
        cls = str(n.get("net_class") or "")
        out[name] = resolve_net_rules(
            cls,
            layer_is_outer=name not in inner_nets,
            fab_caps=capability,
            overrides=net_classes.get(cls),
            current_a=n.get("est_current_a"),
        )
    return out


def _report(
    board: epro.EproDocument,
    frame: epro.Frame,
    stackup: list[dict[str, Any]],
    nets: list[dict[str, Any]],
    net_classes: dict[str, Any],
    pads: list[dict[str, Any]],
) -> tuple[copper_report.CopperReport, list[str]]:
    copper, warnings = epro.measured_copper(board, frame)
    names = [str(s["name"]) for s in stackup]
    capability = capability_for(drc.process_for_stackup(stackup))
    rules = _resolve_rules(nets, net_classes, capability, copper, {names[0], names[-1]})
    return (
        copper_report.report(copper, pads, names, rules, capability),
        warnings,
    )


def _outline_path(design: epro.Design) -> list[list[float]] | None:
    """The board outline ring in board-frame mm (the feature
    :func:`~precis.pcb.epro.build_design` minted from the OUTLINE layer's
    POLY records) — the denominator a pour's coverage is measured
    against."""
    return next(
        (f["geom"]["path"] for f in design.features if f.get("ftype") == "outline"),
        None,
    )


def _design_pads(
    design: epro.Design, stackup: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The board pads ``design`` would place once written — for a dry
    run, which has no stored footprints to read them back from."""
    local = dict(normalize_local_footprint(f) for f in design.footprints)
    pin_to_net = {(k["refdes"], k["pin"]): k["net"] for k in design.connections}
    pads, _drills = padplace.board_pads(
        design.components,
        {},
        layers=[str(s["name"]) for s in stackup],
        pin_to_net=pin_to_net,
        local_footprints=local,
    )
    return pads


def report_copper(
    store: Any, data: bytes, *, slug: str, board_uuid: str | None = None
) -> copper_report.CopperReport:
    """The source board's copper measured against ``slug``'s CURRENT spec.

    Re-runnable after annotating nets (a current, a class), which is the
    point: the first report is against defaults, and each later one shows
    which disagreements the annotations resolved. ``slug`` must be an
    import of this file — the pads that close the gaps come from it.
    """
    ref = store.get_ref(kind="pcb", id=slug)
    if ref is None:
        raise EproImportError(f"no pcb {slug!r}; import the board first")
    ref_id = int(ref.id)
    project = epro.read_archive(data)
    board = project.pcb(board_uuid)
    _design, frame = epro.build_design(project, board)
    stackup, _planes, _w = derive_stackup(
        board, pours=_design.epro_pours, outline=_outline_path(_design)
    )
    graph = store.pcb_graph(ref_id)
    pin_to_net = {
        (m["refdes"], m["pin"]): n["name"] for n in graph["nets"] for m in n["members"]
    }
    pads, _drills = padplace.board_pads(
        graph["instances"],
        {},
        layers=[str(s["name"]) for s in stackup],
        pin_to_net=pin_to_net,
        local_footprints=store.pcb_local_footprints_for(ref_id),
    )
    rep, _warnings = _report(
        board, frame, stackup, graph["nets"], graph.get("net_classes") or {}, pads
    )
    return rep


#: A net whose pour zones cover at least this share of the board outline
#: is carried by fill — the way a plane carries it — not by a local pour
#: (gr486958). Picked from the fixtures: the committed fixture's zone IS
#: the outline verbatim (~100%) and taz15's outer zones are full-board
#: pours drawn to the board edge; a fill serving one circuit is bounded
#: by that circuit, an order smaller. "More than half" is the cleanest
#: split, and it makes the several-carried-nets "mixed" case rare by
#: construction — two disjoint fills can each claim half, never two
#: thirds.
_POUR_CARRIES_BOARD = 0.5


def _pour_coverage(
    pours: list[dict[str, Any]] | None,
    outline: list[list[float]] | None,
) -> dict[str, dict[str, float]]:
    """``{layer name: {net name: share of the board outline covered}}``
    from the authored pour zone outlines (:func:`precis.pcb.epro.
    extract_pours`, already in board-frame mm). The zone's own ring is
    measured, not Pro's computed fill — the decision it feeds is "did the
    author intend this layer flooded on this net", and the authored zone
    is the intent. ``{}`` when no outline exists to measure against."""
    if not pours or not outline or len(outline) < 3:
        return {}
    board = Polygon([(float(p[0]), float(p[1])) for p in outline])
    if not board.is_valid:
        board = board.buffer(0)
    if board.is_empty or board.area <= 0:
        return {}
    by_net: dict[tuple[str, str], list[Polygon]] = {}
    for pour in pours:
        ring = pour.get("polygon") or []
        net = str(pour.get("net") or "")
        if len(ring) < 3 or not net:
            continue
        poly = Polygon([(float(p[0]), float(p[1])) for p in ring])
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty:
            continue
        by_net.setdefault((str(pour.get("layer") or ""), net), []).append(poly)
    out: dict[str, dict[str, float]] = {}
    for (layer, net), polys in by_net.items():
        if not layer:
            continue
        share = unary_union(polys).intersection(board).area / board.area
        if share > 0:
            out.setdefault(layer, {})[net] = share
    return out


def derive_stackup(
    pcb: epro.EproDocument,
    *,
    pours: list[dict[str, Any]] | None = None,
    outline: list[list[float]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str], list[str]]:
    """``(stackup, {layer name: plane net}, warnings)`` from the board's
    ``LAYER`` and ``POUR`` records.

    An INNER copper layer whose pours all name one net is a plane on that
    net: that is what the copper actually is, and modelling it as a plane
    rather than importing the pour polygon as frozen geometry means the
    far side re-pours it (``pcb_fixed_copper``'s CHECK is ``track|via``
    anyway, so a pour has no other home). An inner layer with pours on
    several nets, or none, stays a signal layer — guessing which net
    "wins" would silently connect copper.

    ``pours``/``outline`` (``design.epro_pours`` + the outline feature's
    ring) add the COVERAGE side (``_pour_coverage``): on any layer that
    did not classify as a plane, a net whose zones cover at least
    ``_POUR_CARRIES_BOARD`` of the outline is recorded as ``pour_nets``
    on the still-``signal`` entry — the import turns exactly one such
    net per layer into the same ``pcb_planes`` row ``op='plane_net'``
    writes, so the router skips it as a trace and re-pours its pads'
    fill. Several qualifying nets on one layer are all recorded (the
    "mixed" case — a layer that is neither a clean plane nor plain
    signal) but NONE is assigned: one sheet of copper cannot pour two
    nets, and picking one would silently connect copper the author
    separated. Small local pours change nothing — no key, no row, and
    the layer keeps its plain shape so content-hash digests of pour-free
    boards are unchanged. Coverage-less calls (``pours``/``outline``
    omitted) fall back to the name-only rule.

    **A plane the author also routed on stays routable** (``"routable":
    True``, :func:`precis.pcb.ir.layer_is_routable`). Measured 2026-10-01
    on a real board: both inner layers carried a single-net pour AND 81
    track runs on 50 other nets. Stacking them as pure planes took away
    half of the author's routing layers before the router ever ran. An
    outer pour-carried layer likewise stays routable for every OTHER
    net: signals route around the fill on the same sheet.
    """
    warnings: list[str] = []
    copper = epro.copper_layers(pcb)
    names = list(copper.values())

    pour_nets: dict[str, set[str]] = {}
    for b in pcb.bodies("POUR"):
        lid = b.get("layerId")
        name = copper.get(int(lid)) if lid is not None else None
        if name is None:
            continue
        pour_nets.setdefault(name, set()).add(str(b.get("netName") or ""))
    track_nets: dict[str, set[str]] = {}
    for kind in ("LINE", "ARC"):
        for b in pcb.bodies(kind):
            lid = b.get("layerId")
            name = copper.get(int(lid)) if lid is not None else None
            if name is not None and b.get("netName"):
                track_nets.setdefault(name, set()).add(str(b["netName"]))
    coverage = _pour_coverage(pours, outline)

    outer = {names[0], names[-1]}
    stackup: list[dict[str, Any]] = []
    planes: dict[str, str] = {}
    for name in names:
        nets = {n for n in pour_nets.get(name, set()) if n}
        if name not in outer and len(nets) == 1:
            net = next(iter(nets))
            entry: dict[str, Any] = {"name": name, "role": "plane", "plane_net": net}
            if track_nets.get(name, set()) - {net}:
                entry["routable"] = True
            stackup.append(entry)
            planes[name] = net
            continue
        shares = coverage.get(name) or {}
        carried = sorted(
            n for n, share in shares.items() if share >= _POUR_CARRIES_BOARD
        )
        entry = {"name": name, "role": "signal"}
        if carried:
            entry["pour_nets"] = carried
        stackup.append(entry)
        if name not in outer and len(nets) > 1:
            warnings.append(
                f"inner layer {name} carries pours on {len(nets)} nets "
                f"({', '.join(sorted(nets))}); imported as a SIGNAL layer "
                f"rather than guessing which net the plane is"
            )
            if len(carried) == 1:
                warnings.append(
                    f"inner layer {name}: {carried[0]}'s pour covers "
                    f"{shares[carried[0]]:.0%} of the board — recorded in "
                    f"pour_nets and assigned like a plane net; the layer "
                    f"stays routable for the rest"
                )
            elif carried:
                warnings.append(
                    f"inner layer {name}: {len(carried)} pours each cover at "
                    f"least {_POUR_CARRIES_BOARD:.0%} of the board "
                    f"({', '.join(carried)}); recorded in pour_nets, none "
                    f"assigned — two fills on one sheet cannot each be the "
                    f"plane"
                )
        elif name in outer and nets:
            if len(carried) == 1:
                warnings.append(
                    f"outer layer {name} carries a pour on {carried[0]} "
                    f"covering {shares[carried[0]]:.0%} of the board — "
                    f"recorded in pour_nets and assigned like a plane net "
                    f"(gr486958); the layer stays routable for the rest"
                )
            elif carried:
                warnings.append(
                    f"outer layer {name} carries pours on {len(carried)} "
                    f"nets each covering at least {_POUR_CARRIES_BOARD:.0%} "
                    f"of the board ({', '.join(carried)}); recorded in "
                    f"pour_nets, none assigned — two fills on one sheet "
                    f"cannot each be the plane"
                )
            else:
                warnings.append(
                    f"outer layer {name} carries a pour on "
                    f"{', '.join(sorted(nets))}; outer pours are local fill, "
                    f"not planes, so it stays a signal layer"
                )
    return stackup, planes, warnings


def _intake_copper_rows(
    board: epro.EproDocument, frame: epro.Frame, source_hash: str
) -> list[dict[str, Any]]:
    """Split measurement chains into one row per accepted source LINE/ARC.

    Preserve the reader's board-frame segment, width and arc handedness;
    unsupported records retain its warnings, never fabricated geometry.
    """
    rows = []
    for track in epro.extract_tracks(board, frame).tracks:
        for segment in track["geom"]["segments"]:
            rows.append(
                {
                    **track,
                    "geom": {**track["geom"], "segments": [segment]},
                    "meta": {"source_sha256": source_hash},
                }
            )
    rows.extend(
        {**via, "meta": {"source_sha256": source_hash}}
        for via in epro.extract_vias(board, frame).vias
    )
    return rows


def import_epro(
    store: Any,
    data: bytes,
    *,
    slug: str,
    title: str | None = None,
    board_uuid: str | None = None,
    source_name: str | None = None,
    dry_run: bool = False,
    update: bool = False,
    freeze: bool = False,
    copper: str | None = None,
) -> ImportResult:
    """Import one ``.epro2`` board into the ``pcb`` kind under ``slug``.

    ``dry_run`` reads, derives and refuses exactly as a real import would
    and then writes nothing — so a user can see the warnings, the counts
    and any refusal before committing to a board.

    ``update`` re-imports onto an existing import of the SAME board (see
    :class:`UpdatePlan` for what is applied and what is only reported).

    A part the source locked is imported locked (``fixed='both'``);
    nothing else is (Reto 2026-09-30, "only actually freeze when needed";
    ``docs/backlog/pcb-freeze-mechanicals-and-parts.md``). ``freeze``
    (``--freeze``) locks every part instead, for a board whose whole
    placement must survive a route. A 2026-10-02 freeze-by-default was
    reverted the same day: which parts are alignment-critical needs the
    author's annotations, not a blanket lock. ``--update`` still applies
    the source's moves to locked parts, as before.
    """
    copper_mode = copper if copper is not None else ("none" if update else "fixed")
    if copper_mode not in {"fixed", "none"}:
        raise EproImportError("copper must be 'fixed' (preserve source) or 'none'")
    if update and copper_mode == "fixed":
        raise EproImportError(
            "fixed-copper intake requires a fresh slug: --update deliberately "
            "retains some old nets/outline. Use --copper none for that partial "
            "update, or import into a new preview slug to preserve source copper"
        )
    project = epro.read_archive(data)
    board = project.pcb(board_uuid)
    design, frame = epro.build_design(project, board)
    stackup, planes, stackup_warnings = derive_stackup(
        board, pours=design.epro_pours, outline=_outline_path(design)
    )
    warnings = [*design.warnings, *stackup_warnings]
    if freeze:
        newly = 0
        for comp in design.components:
            if comp.get("fixed") is None:
                comp["fixed"] = "both"
                newly += 1
        if newly and update:
            # Only added parts are written by an update; existing ones keep
            # whatever lock they have on the board.
            warnings.append(
                "parts this update adds are imported LOCKED (fixed='both'); "
                "unlock one with op='move' fixed=None"
            )
        elif newly:
            warnings.append(
                f"{newly} part(s) imported LOCKED (fixed='both') so op='route' "
                f"cannot re-place them; unlock one with op='move' fixed=None"
            )

    if len(stackup) not in _SUPPORTED_LAYER_COUNTS:
        raise EproImportError(
            f"this board has {len(stackup)} copper layers "
            f"({', '.join(s['name'] for s in stackup)}); precis' placer and "
            f"router accept only "
            f"{' or '.join(str(n) for n in _SUPPORTED_LAYER_COUNTS)} today, so "
            f"importing it would produce a board that refuses to route "
            f"(docs/backlog/pcb-engine-plan.md §5 is the n-layer work)"
        )
    # validate_stackup is the authored-input contract; run it on a derived
    # stackup too, so a reader bug cannot write a shape the handler would
    # reject on the next edit.
    stackup = ir.validate_stackup(stackup)
    drc.process_for_stackup(stackup)

    existing = store.get_ref(kind="pcb", id=slug)
    if update:
        return _update(
            store,
            existing,
            data,
            slug=slug,
            project=project,
            board=board,
            design=design,
            frame=frame,
            stackup=stackup,
            planes=planes,
            warnings=warnings,
            source_name=source_name,
            dry_run=dry_run,
        )
    if existing is not None:
        raise EproImportError(
            f"pcb {slug!r} already exists. A plain second import would extend "
            f"it: an existing refdes keeps its OLD position (so a moved part "
            f"silently does not move) and features have no dedup key at all "
            f"(so the outline and every mounting hole would be duplicated). "
            f"Pass --update to re-import onto it, or import to a fresh slug"
        )

    result = ImportResult(
        slug=slug,
        ref_id=0,
        created=False,
        stackup=stackup,
        planes=planes,
        pour_nets={
            str(e["name"]): list(e["pour_nets"]) for e in stackup if e.get("pour_nets")
        },
        warnings=warnings,
        stats=dict(design.stats),
        alignment_candidates=alignment_candidates(design),
    )
    source_rows = _intake_copper_rows(board, frame, hashlib.sha256(data).hexdigest())
    result.stats["source_track_records"] = sum(
        r["ctype"] == "track" for r in source_rows
    )
    result.stats["source_vias"] = sum(r["ctype"] == "via" for r in source_rows)
    if copper_mode == "fixed":
        # Copper records carry their own explicit netName. Some source files
        # omit the corresponding NET declaration; retain that name rather
        # than dropping its geometry or inventing a pin connection. A
        # copper-only net takes the same default rule class the pad-named
        # nets got in build_design (EasyEDA's no-selector semantic).
        known_nets = {n["name"] for n in design.nets}
        copper_nets = {str(r["net"]) for r in source_rows}
        default_class = next(
            (str(n["net_class"]) for n in design.nets if n.get("net_class")),
            None,
        )
        design.nets.extend(
            {"name": n, **({"net_class": default_class} if default_class else {})}
            for n in sorted(copper_nets - known_nets)
        )
        result.stats["nets"] = len(design.nets)
    if dry_run:
        # The pads a real import would write, placed in memory: the same
        # normaliser the store applies, so --dry-run measures gaps to pads
        # exactly as the real import's report does (2026-10-02: it measured
        # tracks and vias only, and said so). The report resolves rules
        # against the design's OWN imported classes, so a dry run shows the
        # same clearances the write would record.
        result.copper, copper_warnings = _report(
            board,
            frame,
            stackup,
            design.nets,
            design.net_classes,
            _design_pads(design, stackup),
        )
        result.warnings.extend(copper_warnings)
        return result

    # Part lookup (the item's order — mfr+MPN, C-number, parametric) runs
    # before the footprint dedup, which reads the resolved part_lcsc.
    result.warnings.extend(_resolve_catalog_lcscs(store, design.components))
    # The local-footprint dedup runs on the WRITE paths only: a dry run
    # reports the source's pads (they exist either way), and the kept-vs-
    # dropped verdict is a store decision (it needs the catalog cache).
    dropped_fp = _dedupe_local_footprints(store, design.components, design.footprints)
    if dropped_fp:
        design.footprints = [
            f for f in design.footprints if f.get("name") not in dropped_fp
        ]
        result.stats["footprints"] = len(design.footprints)
        result.warnings.append(
            f"{len(dropped_fp)} local footprint(s) matched the catalog's "
            f"cached pads for their LCSC part (pad count, centres within "
            f"{_LOCAL_FP_MATCH_TOL_MM}mm) — not stored; those instances use "
            f"the catalog footprint: {', '.join(sorted(dropped_fp)[:8])}"
            + ("…" if len(dropped_fp) > 8 else "")
        )

    meta = {
        "epro": {
            **_provenance(data, project, board, source_name),
            "copper_mode": copper_mode,
        },
        # The design-rule table verbatim — the writer reads it back out of
        # the ref meta so a re-export carries the same RULE records. The
        # mapped slice lives in pcb_net_classes; this is the lossless copy.
        "epro_rules": design.epro_rules,
        # The geometry-free board records (physical stackup, title, canvas
        # state) — same verbatim write-back contract.
        "epro_board": design.epro_board,
        # The AUTHORED pour zone outlines (board coords). An inner pour
        # is a plane to precis and an outer pour has no home at all — the
        # polygon survives here so export writes the authored zone back.
        "epro_pours": design.epro_pours,
    }
    # ONE transaction for the design, the stackup and the planes. A board
    # that got its instances but not its stackup would silently keep
    # DEFAULT_STACKUP, which claims a GND plane on In1.Cu — so a partial
    # import would leave a board that lies about its own layers rather
    # than no board at all.
    with store.tx() as conn:
        ref, created, counts = store.pcb_apply(
            slug=slug,
            title=title or project.title or slug,
            components=design.components,
            nets=design.nets,
            connections=design.connections,
            features=design.features,
            footprints=design.footprints,
            meta=meta,
            conn=conn,
        )
        result.ref_id = int(ref.id)
        result.created = bool(created)
        result.counts = dict(counts)

        board_id = store.pcb_ensure_board(result.ref_id, conn=conn)
        store.pcb_set_stackup(board_id, stackup, conn=conn)
        if design.net_classes:
            result.stats["net_classes"] = store.pcb_upsert_net_classes(
                result.ref_id, design.net_classes, conn=conn
            )
        # A layer carrying ONE pour-carried net gets the same authored
        # pcb_planes row op='plane_net' writes — the router's skip-net
        # decision reads those rows, not the stackup label. A layer with
        # SEVERAL is recorded in pour_nets but assigned to none
        # (derive_stackup's mixed case: one sheet cannot pour two nets).
        assignments = dict(planes)
        for e in stackup:
            pour = e.get("pour_nets") or []
            if len(pour) == 1:
                assignments[str(e["name"])] = str(pour[0])
        unresolved = [
            layer
            for layer, net in sorted(assignments.items())
            if not store.pcb_assign_plane(result.ref_id, layer, net, conn=conn)
        ]
        if copper_mode == "fixed":
            result.counts["fixed_copper"] = store.pcb_fixed_copper_put(
                result.ref_id,
                board_id,
                "__epro_source",
                "epro_import",
                "1",
                source_rows,
                conn=conn,
            )
    for layer in unresolved:
        result.warnings.append(
            f"plane {layer} -> {assignments[layer]}: the net did not survive the "
            f"import, so the layer is stacked as a plane with no net"
        )
    # Each imported C-number gets its datasheet-of link where the store
    # already holds the sheet (family sharing: one ref, one link per
    # C-number). What still lacks one is the caller's pull queue — the
    # CLI runs datasheets.enqueue_pulls after this returns. Best-effort:
    # the import committed above, a link hiccup must not re-raise it.
    try:
        linked = datasheets.link_known(store, result.ref_id)
    except Exception:
        log.warning("epro import: datasheet linking failed", exc_info=True)
    else:
        if linked:
            result.stats["datasheets_linked"] = linked
    # Read back from the DB rather than reusing `design`: the report must
    # see the pads exactly as every later reader (DRC, route) will.
    result.copper = report_copper(store, data, slug=slug, board_uuid=board_uuid)
    result.warnings.extend(epro.measured_copper(board, frame)[1])
    return result


def _dedupe_local_footprints(
    store: Any,
    components: list[dict[str, Any]],
    footprints: list[dict[str, Any]],
) -> set[str]:
    """The local-vs-catalog dedup (Reto 2026-10-14: "import the numbers,
    then keep the board's own footprint where it really differs — no
    deferred sweep").

    A footprint NAME is dropped from the write batch when EVERY component
    that names it has a ``part_lcsc`` AND the catalog's cached
    ``part_footprints`` row holds the same pads (same count, centres
    within ``_LOCAL_FP_MATCH_TOL_MM``). The component keeps the name as
    its ``footprint`` label — it is the source file's authored name and a
    fair BOM comment — but no ``pcb_local_footprints`` row is written, so
    the local-first join finds nothing and the C-number resolves the
    catalog footprint. A name is KEPT when any user differs (the
    ``local_fp_lcsc``/``local_fp_diff`` meta the apply stamps records
    why) or has no catalog row to compare ("uncached" — the verdict waits
    for the cache to land). Returns the dropped names."""
    local = {str(f.get("name")): f for f in footprints}
    want = {
        str(c.get("part_lcsc")).strip().upper()
        for c in components
        if c.get("part_lcsc") and str(c.get("footprint") or "") in local
    }
    catalog = store.part_footprints_many(want)
    matched: set[str] = set()
    differs: set[str] = set()
    for c in components:
        name = str(c.get("footprint") or "")
        lcsc = str(c.get("part_lcsc") or "").strip().upper()
        if not name or name not in local or not lcsc:
            continue
        f = local[name]
        if f.get("holes"):
            # The local row OWNS footprint drills (NPTH mounting holes
            # ride the footprint doc); the compare says nothing about
            # them, so such a footprint is always special.
            differs.add(name)
            continue
        row = catalog.get(lcsc)
        if row is None:
            differs.add(name)  # nothing to compare -> keep
        elif footprint_differ(f, row) is None:
            matched.add(name)
        else:
            differs.add(name)
    return matched - differs


# ── part lookup (pcb-datasheet-review.md slice 1) ────────────────────────
# A footprint name's size token: "R0603"/"C0805" -> "0603"/"0805" — the
# package the JLC catalog calls the part by.
_PACKAGE_FROM_FP = re.compile(r"(\d{4})")

#: A value attr: "100n", "100nF", "0.1uF", "4.7K", "10kΩ", "1R0" —
#: magnitude + multiplier + optional unit letter.
_VALUE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([pnuμmkKM]?)\s*([FfΩohmRr]*)")

_VALUE_SCALE = {
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "μ": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "": 1.0,
}


def _canon_value(text: str) -> tuple[float, str] | None:
    """``(magnitude, unit)`` from a value string — "100nF" -> (1e-7, "F"),
    "10kΩ" -> (1e4, "R"). A missing unit letter stays ``""`` (a wildcard:
    "10k" still matches a catalog param printed as "10KΩ")."""
    m = _VALUE_RE.match(str(text))
    if not m:
        return None
    unit = m.group(3).lower()
    unit = (
        "F"
        if unit.startswith("f")
        else ("R" if unit[:1] in ("ω", "o") or "ohm" in unit or unit == "r" else "")
    )
    try:
        return (float(m.group(1)) * _VALUE_SCALE[m.group(2)], unit)
    except ValueError:
        return None


def _mfr_matches(catalog_mfr: str, claimed: str) -> bool:
    """The two spellings of one manufacturer ("YAGEO(国巨)" vs "YAGEO",
    "TI(德州仪器)" vs "TI") agree when either's base name contains the
    other's."""
    a = re.split(r"[(（]", claimed)[0].strip().lower()
    b = re.split(r"[(（]", catalog_mfr)[0].strip().lower()
    return bool(a and b) and (a in b or b in a)


def _parametric_lcsc(store: Any, comp: dict[str, Any]) -> str:
    """The parametric fallback (generic passives with no C-number and no
    catalog MPN): value + the footprint's package token -> a UNIQUE
    ``parts`` row, else nothing. A guess that could pick a wrong part is
    worse than no match — the C-number drives datasheet pulls, and a
    wrong sheet is wrong part facts."""
    meta = comp.get("part_meta") or {}
    value = meta.get("value") or meta.get("name") or ""
    pkg_m = _PACKAGE_FROM_FP.search(str(comp.get("footprint") or ""))
    canon = _canon_value(value)
    if not canon or not pkg_m:
        return ""
    hits = [
        lcsc
        for lcsc, params in store.parts_in_package(pkg_m.group(1))
        if any(
            (cand := _canon_value(str(v))) is not None
            and cand[0] == canon[0]
            and (not canon[1] or not cand[1] or cand[1] == canon[1])
            for v in params.values()
            if isinstance(v, str)
        )
    ]
    return hits[0] if len(hits) == 1 else ""


def _resolve_catalog_lcscs(store: Any, components: list[dict[str, Any]]) -> list[str]:
    """Part lookup on import (the item's order): manufacturer + MPN first,
    then the source's claimed C-number, then a unique parametric match.

    Rewrites ``c["part_lcsc"]`` to the RESOLVED number — the physical part
    IS what its MPN says; the ``Supplier Part`` string is only the source's
    own catalog pointer (kept verbatim in ``part_meta.supplier_part``). A
    claim and an MPN match that disagree get a warning; ``lcsc_source`` in
    ``part_meta`` records which path won."""
    warnings: list[str] = []
    mpns = {
        str((c.get("part_meta") or {}).get("mpn") or "")
        for c in components
        if (c.get("part_meta") or {}).get("mpn")
    }
    by_mpn = store.parts_by_mpns(mpns)
    for c in components:
        meta = c.get("part_meta") or {}
        claimed = str(c.get("part_lcsc") or "").strip().upper()
        resolved, source = claimed, "supplier-part"
        mpn = str(meta.get("mpn") or "")
        mfr = str(meta.get("mfr") or "")
        if mpn and mpn.lower() in by_mpn:
            hits = by_mpn[mpn.lower()]
            if len(hits) > 1 and mfr:
                narrowed = [h for h in hits if _mfr_matches(h[1], mfr)]
                if narrowed:
                    hits = narrowed
            if len(hits) == 1:
                resolved, source = hits[0][0], "mfr"
        if not resolved:
            p = _parametric_lcsc(store, c)
            if p:
                resolved, source = p, "parametric"
        if not resolved:
            continue
        c["part_lcsc"] = resolved
        if source != "supplier-part":
            meta["lcsc_source"] = source
            c["part_meta"] = meta
        if claimed and resolved != claimed:
            warnings.append(
                f"{c['refdes']}: Supplier Part {claimed} but its "
                f"Manufacturer Part {mpn} is catalog {resolved}; the MPN "
                "match wins (the C-number is only the source's listing)"
            )
    return warnings


def _provenance(
    data: bytes,
    project: epro.EproProject,
    board: epro.EproDocument,
    source_name: str | None,
) -> dict[str, Any]:
    return {
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "source_name": source_name,
        "project_title": project.title,
        "editor_version": project.editor_version,
        "board_uuid": board.uuid,
        "imported_at": datetime.now(UTC).isoformat(),
        # Bump when a reader change would make an old import's numbers
        # non-comparable; a stored version is what lets a future failure
        # be dated rather than guessed at.
        "reader_version": 1,
    }


def _plan_footprints(
    plan: UpdatePlan, stored: dict[str, dict[str, Any]], design: epro.Design
) -> None:
    """Sort the source's footprints that already exist on the board into
    refreshed (same pads and pin map, other geometry differs) and differ
    (pad numbers or pin map changed). New names ride in with new parts."""
    for f in design.footprints:
        name, fresh = normalize_local_footprint(f)
        old = stored.get(name)
        if old is None:
            continue
        if _canon(_pad_numbers(old)) != _canon(_pad_numbers(fresh)) or _canon(
            old.get("pin_map") or {}
        ) != _canon(fresh.get("pin_map") or {}):
            plan.footprints_differ.append(name)
        elif any(
            _canon(old.get(k)) != _canon(fresh.get(k))
            for k in ("pads", "courtyard", "centroid", "holes")
        ):
            plan.footprints_refreshed.append(name)
    plan.footprints_refreshed.sort()
    plan.footprints_differ.sort()


def _legacy_hole_features(
    plan: UpdatePlan,
    graph: dict[str, Any],
    features: list[dict[str, Any]],
    stored: dict[str, dict[str, Any]],
    design: epro.Design,
) -> list[int]:
    """Part-owned ``mounting_hole`` features of parts whose footprint carries
    holes after this update (the fresh source geometry for a footprint that
    is refreshed, the stored one otherwise)."""
    fresh = {f["name"]: f.get("holes") or [] for f in design.footprints}
    refreshed = set(plan.footprints_refreshed)
    fp_of = {
        str(i["refdes"]): str(i.get("footprint") or "") for i in graph["instances"]
    }
    out: list[int] = []
    for f in features:
        part = (f.get("geom") or {}).get("part")
        if f.get("ftype") != "mounting_hole" or not part:
            continue
        name = fp_of.get(str(part), "")
        if name in refreshed:
            holes = fresh.get(name)
        else:
            holes = (stored.get(name) or {}).get("holes")
        if holes:
            out.append(int(f["feature_id"]))
    return sorted(out)


def _legacy_hole_measure_warnings(
    retired: list[int],
    features: list[dict[str, Any]],
    measures: list[dict[str, Any]],
) -> list[str]:
    """One warning per (live measure, retired feature) pair where the measure
    has a ``{"feature_id": n}`` operand on a retired legacy hole."""
    owner = {
        int(f["feature_id"]): str((f.get("geom") or {}).get("part") or "?")
        for f in features
        if int(f["feature_id"]) in set(retired)
    }
    out: list[str] = []
    for m in measures:
        for o in m.get("operands") or []:
            if not isinstance(o, dict) or "feature_id" not in o:
                continue
            try:
                fid = int(o["feature_id"])
            except (TypeError, ValueError):
                continue
            if fid in owner:
                out.append(
                    f"measure #{m.get('measure_id')} ({m.get('metric')}) "
                    f"references feature {fid}, the retired hole of part "
                    f"{owner[fid]}: the hole now rides the footprint, so this "
                    f"measure is redundant and now reads unresolved; remove it"
                )
    return out


def _pad_numbers(footprint: dict[str, Any]) -> list[str]:
    return sorted(str(p.get("number")) for p in footprint.get("pads") or [])


def _stackup_shape(stackup: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (
            s.get("name"),
            s.get("role"),
            s.get("plane_net"),
            bool(s.get("routable")),
            tuple(s.get("pour_nets") or ()),
        )
        for s in stackup
    ]


def _update(
    store: Any,
    existing: Any,
    data: bytes,
    *,
    slug: str,
    project: epro.EproProject,
    board: epro.EproDocument,
    design: epro.Design,
    frame: epro.Frame,
    stackup: list[dict[str, Any]],
    planes: dict[str, str],
    warnings: list[str],
    source_name: str | None,
    dry_run: bool,
) -> ImportResult:
    """``--update``: apply the source's moves and new parts to an existing
    import of the same board, report the rest (:class:`UpdatePlan`)."""
    if existing is None:
        raise EproImportError(
            f"no pcb {slug!r} to update; import it without --update first"
        )
    prior = dict((existing.meta or {}).get("epro") or {})
    if not prior:
        raise EproImportError(
            f"pcb {slug!r} was not imported from EasyEDA Pro, so there is no "
            f"source to update it from"
        )
    if prior.get("board_uuid") != board.uuid:
        raise EproImportError(
            f"pcb {slug!r} was imported from board {prior.get('board_uuid')}; "
            f"this file's board is {board.uuid}. Updating one board from "
            f"another would match parts by refdes alone. Import to a fresh "
            f"slug, or pass --board {prior.get('board_uuid')}"
        )
    ref_id = int(existing.id)
    graph = store.pcb_graph(ref_id)
    features = store.pcb_features_list(ref_id)
    plan = plan_update(graph, features, design)
    stored = (graph.get("board") or {}).get("stackup") or []
    if _stackup_shape(stored) != _stackup_shape(stackup):
        warnings.append(
            "the source's layers now derive a different stackup than the "
            "board carries; --update does not change the stackup (import to "
            "a fresh slug to take the new one)"
        )
    stored_fps = store.pcb_local_footprints_for(ref_id)
    _plan_footprints(plan, stored_fps, design)
    plan.legacy_hole_features = _legacy_hole_features(
        plan, graph, features, stored_fps, design
    )
    plan.features_missing -= len(plan.legacy_hole_features)
    if plan.legacy_hole_features:
        plan.legacy_hole_measures = _legacy_hole_measure_warnings(
            plan.legacy_hole_features, features, store.pcb_measures_list(ref_id)
        )
    result = ImportResult(
        slug=slug,
        ref_id=ref_id,
        created=False,
        stackup=stackup,
        planes=planes,
        pour_nets={
            str(e["name"]): list(e["pour_nets"]) for e in stackup if e.get("pour_nets")
        },
        warnings=warnings,
        stats=dict(design.stats),
        update=plan,
    )
    if not dry_run:
        added = set(plan.added)
        components = [c for c in design.components if c["refdes"] in added]
        warnings.extend(_resolve_catalog_lcscs(store, components))
        dropped_fp = _dedupe_local_footprints(store, components, design.footprints)
        used = {c.get("footprint") for c in components} - dropped_fp
        used |= set(plan.footprints_refreshed) - dropped_fp
        if dropped_fp:
            warnings.append(
                f"{len(dropped_fp)} local footprint(s) matched the catalog's "
                f"cached pads — not stored; those instances use the catalog "
                f"footprint: {', '.join(sorted(dropped_fp)[:8])}"
                + ("…" if len(dropped_fp) > 8 else "")
            )
        connections = [k for k in design.connections if k["refdes"] in added]
        wired = {k["net"] for k in connections}
        meta = {
            "epro": {
                **_provenance(data, project, board, source_name),
                "imported_at": prior.get("imported_at"),
                "updated_at": datetime.now(UTC).isoformat(),
            },
            # Refresh the verbatim rule passthrough in the same write — an
            # update that left the OLD table behind would re-export rules
            # the source no longer carries.
            "epro_rules": design.epro_rules,
            "epro_board": design.epro_board,
            "epro_pours": design.epro_pours,
        }
        # One transaction: a half-applied update leaves a board that
        # matches neither the old import nor the new source.
        with store.tx() as conn:
            _ref, _created, counts = store.pcb_apply(
                slug=slug,
                title=existing.title,
                components=components,
                nets=[n for n in design.nets if n["name"] in wired],
                connections=connections,
                features=[],
                footprints=[f for f in design.footprints if f["name"] in used],
                meta=meta,
                conn=conn,
            )
            if design.net_classes:
                store.pcb_upsert_net_classes(ref_id, design.net_classes, conn=conn)
            if dropped_fp:
                # Truly drop the copy: a row a PRE-this-slice import wrote
                # would otherwise keep winning the local-first join. A row
                # is deleted only when EVERY live instance still naming it
                # resolves to a matching catalog footprint — a partless
                # namer, or one whose C-number's cache row is absent or
                # differs, keeps it (deleting would orphan its pads). A
                # name with no live namer at all is dead weight: it goes.
                live = conn.execute(
                    "SELECT c.footprint, upper(btrim(c.part_lcsc)) "
                    "FROM pcb_instances i "
                    "JOIN pcb_components c ON c.component_id = i.component_id "
                    "WHERE i.ref_id = %s AND i.retired_at IS NULL "
                    "  AND c.footprint = ANY(%s)",
                    (ref_id, sorted(dropped_fp)),
                ).fetchall()
                catalog = store.part_footprints_many({str(lc) for _n, lc in live if lc})
                rows = store.pcb_local_footprints_for(ref_id)
                deletable = [
                    name
                    for name in dropped_fp
                    if name in rows
                    and all(
                        lc
                        and lc in catalog
                        and footprint_differ(rows[name], catalog[str(lc)]) is None
                        for n, lc in live
                        if n == name
                    )
                ]
                if deletable:
                    conn.execute(
                        "DELETE FROM pcb_local_footprints "
                        "WHERE ref_id = %s AND name = ANY(%s)",
                        (ref_id, deletable),
                    )
            store.pcb_features_retire(ref_id, plan.legacy_hole_features, conn=conn)
            for refdes, _old, new, _fixed in plan.moved:
                store.pcb_move_instance(
                    ref_id,
                    refdes,
                    x=new[0],
                    y=new[1],
                    rot=new[2],
                    layer=new[3],
                    conn=conn,
                )
        result.counts = dict(counts)
        try:
            linked = datasheets.link_known(store, ref_id)
        except Exception:
            log.warning("epro update: datasheet linking failed", exc_info=True)
        else:
            if linked:
                result.stats["datasheets_linked"] = linked
    # Against the board as it now stands (pre-update on a dry run).
    result.copper = report_copper(store, data, slug=slug, board_uuid=board.uuid)
    result.warnings.extend(epro.measured_copper(board, frame)[1])
    return result

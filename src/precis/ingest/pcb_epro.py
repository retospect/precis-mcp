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
   the reason pours are not imported as copper.
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
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from precis.pcb import copper_report, drc, epro, ir, padplace
from precis.pcb.capabilities import CapabilityRow, capability_for
from precis.pcb.rules import NetRules, resolve_net_rules
from precis.store._pcb_ops import (
    _normalize_local_footprint as normalize_local_footprint,
)

#: What ``op='place'``/``op='route'`` accept (``_enqueue_op``) and what
#: ``_FIXED_COPPER_FAB_PROCESS`` is pinned to. Refusing HERE, naming the
#: board's actual count, beats letting it surface as a confusing refusal
#: at the user's first route — which is the step the import exists for.
_SUPPORTED_LAYER_COUNTS = (4,)


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
    holed = {
        str(f["geom"]["part"])
        for f in design.features
        if f.get("ftype") == "mounting_hole" and (f.get("geom") or {}).get("part")
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
    stackup, _planes, _w = derive_stackup(board)
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


def derive_stackup(
    pcb: epro.EproDocument,
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

    Outer layers are always signal: a pour on F.Cu/B.Cu is local fill
    around parts, not a plane, and calling it one would tell the router
    that side is unavailable.

    **A plane the author also routed on stays routable** (``"routable":
    True``, :func:`precis.pcb.ir.layer_is_routable`). Measured 2026-10-01
    on a real board: both inner layers carried a single-net pour AND 81
    track runs on 50 other nets. Stacking them as pure planes took away
    half of the author's routing layers before the router ever ran.
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
        else:
            stackup.append({"name": name, "role": "signal"})
            if name not in outer and len(nets) > 1:
                warnings.append(
                    f"inner layer {name} carries pours on {len(nets)} nets "
                    f"({', '.join(sorted(nets))}); imported as a SIGNAL layer "
                    f"rather than guessing which net the plane is"
                )
            elif name in outer and nets:
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
    stackup, planes, stackup_warnings = derive_stackup(board)
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
        # than dropping its geometry or inventing a pin connection.
        known_nets = {n["name"] for n in design.nets}
        copper_nets = {str(r["net"]) for r in source_rows}
        design.nets.extend({"name": n} for n in sorted(copper_nets - known_nets))
        result.stats["nets"] = len(design.nets)
    if dry_run:
        # The pads a real import would write, placed in memory: the same
        # normaliser the store applies, so --dry-run measures gaps to pads
        # exactly as the real import's report does (2026-10-02: it measured
        # tracks and vias only, and said so).
        result.copper, copper_warnings = _report(
            board, frame, stackup, design.nets, {}, _design_pads(design, stackup)
        )
        result.warnings.extend(copper_warnings)
        return result

    meta = {
        "epro": {
            **_provenance(data, project, board, source_name),
            "copper_mode": copper_mode,
        }
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
        unresolved = [
            layer
            for layer, net in sorted(planes.items())
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
            f"plane {layer} -> {planes[layer]}: the net did not survive the "
            f"import, so the layer is stacked as a plane with no net"
        )
    # Read back from the DB rather than reusing `design`: the report must
    # see the pads exactly as every later reader (DRC, route) will.
    result.copper = report_copper(store, data, slug=slug, board_uuid=board_uuid)
    result.warnings.extend(epro.measured_copper(board, frame)[1])
    return result


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
            for k in ("pads", "courtyard", "centroid")
        ):
            plan.footprints_refreshed.append(name)
    plan.footprints_refreshed.sort()
    plan.footprints_differ.sort()


def _pad_numbers(footprint: dict[str, Any]) -> list[str]:
    return sorted(str(p.get("number")) for p in footprint.get("pads") or [])


def _stackup_shape(stackup: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (s.get("name"), s.get("role"), s.get("plane_net"), bool(s.get("routable")))
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
    plan = plan_update(graph, store.pcb_features_list(ref_id), design)
    stored = (graph.get("board") or {}).get("stackup") or []
    if _stackup_shape(stored) != _stackup_shape(stackup):
        warnings.append(
            "the source's layers now derive a different stackup than the "
            "board carries; --update does not change the stackup (import to "
            "a fresh slug to take the new one)"
        )
    _plan_footprints(plan, store.pcb_local_footprints_for(ref_id), design)
    result = ImportResult(
        slug=slug,
        ref_id=ref_id,
        created=False,
        stackup=stackup,
        planes=planes,
        warnings=warnings,
        stats=dict(design.stats),
        update=plan,
    )
    if not dry_run:
        added = set(plan.added)
        components = [c for c in design.components if c["refdes"] in added]
        used = {c.get("footprint") for c in components}
        used |= set(plan.footprints_refreshed)
        connections = [k for k in design.connections if k["refdes"] in added]
        wired = {k["net"] for k in connections}
        meta = {
            "epro": {
                **_provenance(data, project, board, source_name),
                "imported_at": prior.get("imported_at"),
                "updated_at": datetime.now(UTC).isoformat(),
            }
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
    # Against the board as it now stands (pre-update on a dry run).
    result.copper = report_copper(store, data, slug=slug, board_uuid=board.uuid)
    result.warnings.extend(epro.measured_copper(board, frame)[1])
    return result

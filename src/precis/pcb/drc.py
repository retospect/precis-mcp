"""Geometric DRC on realized copper (L5) — pcb-guided-place-route Slice 8.

**The DRC split, restated** (see ``ir.py``'s own module docstring for its
half): the backlog's DRC section was re-cut 2026-08-27 into two engines at
the IR level boundary. ``ir.py`` owns **graph feasibility** (L0-L4, no
geometry, no shapely) and runs *inside* the optimizer's constraint set.
This module owns **geometric DRC** (L5, on realized copper) — class-rule
clearance, trace width, annular ring, courtyard overlap, board-edge
clearance — the *final* check, not the main event. Both emit
``pcb_drc_findings`` rows and share ``view='drc'`` (``handlers/pcb.py``);
``eyes.drc_lite`` is superseded and retired.

**Input model**: the same copper-model dict :mod:`precis.pcb.realize`
produces (:func:`precis.pcb.realize.to_gerber_model`) and
:mod:`precis.pcb.gerber` consumes — ``{"layers": [...], "outline": [...],
"copper": [{"ctype": "track"|"via"|"pour", ...}]}`` (see that module's
docstring for the exact per-``ctype`` shape). This module never invents a
second geometry representation for the same board.

**``realize.py`` now emits ``ctype='via'`` copper** (2026-08-28, closing
the master backlog's "no via geometry is realized" gap) — wherever a
track's routed layer differs from its pads' layer, always carrying
``span``/``layers``, never a scalar ``layer`` (:class:`~precis.pcb.
realize.RealizedVia`'s own docstring explains why that distinction is
load-bearing here). :func:`check_annular_ring` and the via halves of
:func:`check_clearance`/:func:`check_npth_clearance` — correct and covered
by synthetic-model tests since this module was written — now have real
production input to check, not just synthetic fixtures.

**Two-tier margin, not bare pass/fail** (backlog, verbatim: "report the
margin"). Every rule reads BOTH tiers off :mod:`precis.pcb.capabilities` —
``jlc_min`` (the fab's published, unmanufacturable-below floor) and
``house_default`` (our deliberate margin above it) — and a finding fires
in one of two severities: **error** when a value is below ``jlc_min``
outright (unmanufacturable), **warn** when it clears ``jlc_min`` but still
eats into the ``house_default`` margin. Either way the finding's
``margin_mm`` and ``detail`` name the exact numbers ("JLC min 3.5 mil,
house default 6, this trace spends 2.5 mil of headroom") — never a bare
boolean. A ``None`` capability field (JLC publishes no figure for that
process/field — see ``capabilities.py``) means the rule genuinely does not
apply and is silently skipped, never treated as a violated zero.

**The class-rule clearance rule reads BOTH the fab capability row AND a
``pcb_net_classes`` override, through ONE resolver.** ``trace_spacing_mm``
is JLC's own name for copper-to-copper clearance, keyed by *process*
(2-layer/4-layer/aluminum — the "class" in "class rule" here), mirroring
the precedent already set by :func:`precis.pcb.escape.compute_gaps` — that
stays the absolute floor no clearance may go below. A net whose class
carries an authored ``pcb_net_classes.rules.clearance_mm`` (or whose
current annotation implies a wider IPC-2221 trace, which in turn implies
more copper-to-copper room is worth having) resolves through
:func:`precis.pcb.rules.resolve_net_rules` — the SAME resolver
:mod:`precis.pcb.realize` uses for track width and :mod:`precis.pcb.cost`
uses for ``thermal_rise`` — and :func:`check_clearance`'s caller supplies
the resolved :class:`~precis.pcb.rules.NetRules` per net name via
``net_rules=``. Passing ``net_rules=None`` (the default) keeps today's
capability-only behaviour for a caller that hasn't computed one yet.

**Board-edge clearance is two fields, not one** — ``board_edge_clearance_
routed_mm`` vs. ``..._vcut_mm``. When the caller doesn't know the panel
type yet, :func:`check_board_edge_clearance` uses the V-cut figure (the
conservative one, per ``capabilities.py``'s own guidance).

**The O(n^2) reference oracle — the highest-value code in this module.**
:func:`clearance_violations_naive` computes every same-layer,
different-net track/via pair's exact copper-to-copper gap using ONLY
closed-form circle/segment math (no shapely, no spatial index, no shared
code with the accelerated path below the primitive-flattening step) and is
asserted equal to :func:`check_clearance`'s STRtree-accelerated engine over
many randomized layouts (``tests/test_pcb_drc.py``). This build has
already shipped four silent-but-fatal bugs that crashed nothing and failed
no type check (an inverted hardening penalty, a schedule-mismatched
acceptance test, a temperature decayed below eligibility, an estimator
that was provably always zero) — a spatial-index bug that silently misses
a neighbour is the same family, and produces a clean DRC pass on a shorted
board. Pour polygons are checked by the accelerated engine only (not
cross-validated by the dependency-free oracle, which is deliberately
restricted to the circle/capsule primitives that make closed-form math
possible) — a stated, not silent, gap in oracle coverage.

**STRtree, and why it's used only for the clearance rule.** Copper-to-copper
clearance is the one genuinely O(n^2) rule (every different-net pair on a
layer is a candidate); the others (trace width, annular ring, NPTH
clearance, board-edge clearance) are O(n) or O(n . holes) per-item checks
that don't need a spatial index. Courtyard overlap is also pairwise and
uses the same STRtree machinery, at instance rather than copper-item scale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# shapely ships no py.typed marker in this environment (mypy wants the
# separate `types-shapely` stub package, a pyproject/dev-dep decision
# outside this module's remit) -- silence the import-untyped noise here
# rather than let it mask real errors elsewhere in the file (tiling.py
# sets the same precedent).
from shapely.geometry import LineString, Point, Polygon  # type: ignore[import-untyped]
from shapely.geometry.base import BaseGeometry  # type: ignore[import-untyped]
from shapely.strtree import STRtree  # type: ignore[import-untyped]

from precis.pcb.capabilities import CapabilityRow
from precis.pcb.rules import NetRules

Coord = tuple[float, float]

#: Tolerance for two-tier comparisons and shapely distance queries — well
#: below any real manufacturing figure (mm), just float noise absorption.
_EPS = 1e-9

#: A generic courtyard fallback radius for an instance with no real
#: footprint courtyard data supplied — the same honest fallback
#: :class:`precis.pcb.realize.RealizeConfig`'s ``default_obstacle_radius_mm``
#: uses, for the same reason (no per-footprint courtyard wired in yet).
DEFAULT_COURTYARD_RADIUS_MM = 1.0

#: How finely an arc segment is flattened for geometry construction (both
#: the STRtree-accelerated path and the reference oracle use this SAME
#: tessellation, via :func:`_arc_points` — so a disagreement between the
#: two engines can only be about the distance QUERY, never about differing
#: input geometry).
_ARC_MAX_SEG_DEG = 15.0

#: shapely's ``buffer()`` approximates a round cap/join as a many-sided
#: polygon; the default resolution (8 segments/quarter-circle) leaves a
#: sub-micron systematic gap versus the exact closed-form capsule the
#: reference oracle computes. Raised well past where it could ever be
#: mistaken for a real spatial-index miss (see the module docstring's
#: oracle section) rather than loosening the property test's tolerance to
#: paper over it.
_BUFFER_QUAD_SEGS = 64


# ── findings ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DrcFinding:
    """One geometric DRC finding — the row shape both a caller renders as a
    TOON digest and persists to ``pcb_drc_findings`` (board_id/run_id are
    added by the caller, not this module, which knows nothing about a
    board's identity). ``margin_mm`` is always signed NEGATIVE when a
    finding fires — how far below the tier's own threshold the measured
    value sits (module docstring's two-tier rule) — so "how bad" is a
    number, never just a severity string."""

    rule: str
    severity: str  # "error" | "warn"
    where: str
    detail: str
    objects: tuple[dict[str, Any], ...] = ()
    margin_mm: float | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "objects": list(self.objects),
            "detail": self.detail,
        }


def process_for_stackup(stackup: list[dict[str, Any]]) -> str:
    """The capability-table process row for a board's layer COUNT — v1 is
    4-layer-only (backlog decision); 2-layer is kept as a hedge since the
    capability table already carries that row. Raises rather than
    silently guessing for a layer count with no row (:func:`precis.pcb.
    capabilities.capability_for`'s own "raise, don't default" precedent)."""
    n = len(stackup)
    if n == 2:
        return "2layer"
    if n == 4:
        return "4layer"
    raise ValueError(
        f"no DRC capability row for a {n}-layer stackup — v1 checks 2- or "
        "4-layer boards only"
    )


def _two_tier(
    value_mm: float, jlc_min: float | None, house_default: float | None
) -> tuple[str, float] | None:
    """(severity, margin_mm) if ``value_mm`` fails the two-tier bar, else
    ``None`` (clean — no finding). ``jlc_min=None`` means the process/field
    combination is genuinely inapplicable (JLC publishes nothing for it):
    never a finding, never an invented number (capabilities.py's own
    discipline). ``margin_mm`` is always negative when a finding fires —
    the deficit below whichever tier's threshold was crossed."""
    if jlc_min is None:
        return None
    if value_mm < jlc_min - _EPS:
        return "error", value_mm - jlc_min
    if house_default is not None and value_mm < house_default - _EPS:
        return "warn", value_mm - house_default
    return None


def _margin_detail(
    label: str,
    value_mm: float,
    capability: CapabilityRow,
    field: str,
    severity: str,
    margin_mm: float,
) -> str:
    jlc_min = capability.jlc_min[field]
    house = capability.house_default.get(field)
    assert jlc_min is not None  # _two_tier already checked this
    if severity == "error":
        return (
            f"{label} {value_mm:.3f}mm < JLC min {jlc_min:.3f}mm "
            f"({capability.process}) — {abs(margin_mm):.3f}mm short of "
            "manufacturable"
        )
    assert house is not None  # a "warn" only fires when house_default exists
    return (
        f"{label} {value_mm:.3f}mm — JLC min {jlc_min:.3f}mm, house default "
        f"{house:.3f}mm ({capability.process}) — spends {abs(margin_mm):.3f}mm "
        "of headroom"
    )


# ── shared geometry: arc flattening (both engines use this identically) ───


def _arc_points(
    seg: dict[str, Any], *, max_seg_deg: float = _ARC_MAX_SEG_DEG
) -> list[Coord]:
    """A gerber-shaped ``{"shape": "arc", ...}`` segment flattened into a
    polyline (start..end inclusive) — the SAME sweep-direction convention
    :mod:`precis.pcb.realize`'s own arcs are generated with and
    ``tests/test_pcb_realize.py``'s sampler already verifies: recompute the
    raw modular angle difference, then take the ``cw`` flag literally
    rather than assuming every arc is the "short way" (defensive: correct
    for either a short or a long arc, not just this codebase's own
    producer's convention)."""
    cx, cy = float(seg["center"][0]), float(seg["center"][1])
    sx, sy = float(seg["start"][0]), float(seg["start"][1])
    ex, ey = float(seg["end"][0]), float(seg["end"][1])
    r = math.hypot(sx - cx, sy - cy)
    a1 = math.atan2(sy - cy, sx - cx)
    a2 = math.atan2(ey - cy, ex - cx)
    diff = (a2 - a1) % (2 * math.pi)
    sweep = diff if not seg.get("cw", True) else diff - 2 * math.pi
    n = max(1, math.ceil(abs(math.degrees(sweep)) / max_seg_deg))
    return [
        (cx + r * math.cos(a1 + sweep * k / n), cy + r * math.sin(a1 + sweep * k / n))
        for k in range(n + 1)
    ]


def _flatten_segments(segments: list[dict[str, Any]]) -> list[Coord]:
    """Every segment (line or arc) of one track, start to end, as one
    polyline — shared prep for both :func:`_copper_item_polygon` (the
    accelerated engine) and, indirectly, :func:`_copper_primitives` (the
    reference oracle builds capsules straight off ``segments`` instead, but
    an arc's flattening comes from the same :func:`_arc_points`)."""
    pts: list[Coord] = []
    for seg in segments:
        if not pts:
            pts.append((float(seg["start"][0]), float(seg["start"][1])))
        if seg.get("shape") == "arc":
            pts.extend(_arc_points(seg)[1:])
        else:
            pts.append((float(seg["end"][0]), float(seg["end"][1])))
    return pts


def _via_layer_names(item: dict[str, Any], all_layers: list[str]) -> list[str]:
    """Copper layers a via flashes on — mirrors :func:`precis.pcb.gerber.
    _via_layers` (duplicated, not imported: that helper is private to its
    own module, and this one is small enough that owning it here beats
    coupling to another module's internal). An explicit ``layers``
    override, else a blind/buried ``span``, else every layer (through)."""
    if item.get("layers"):
        return list(item["layers"])
    span = item.get("span")
    if span:
        i0, i1 = all_layers.index(span[0]), all_layers.index(span[1])
        lo, hi = min(i0, i1), max(i0, i1)
        return all_layers[lo : hi + 1]
    return list(all_layers)


# ── the STRtree-accelerated engine's geometry (per copper ITEM) ───────────


def _copper_item_polygon(item: dict[str, Any]) -> BaseGeometry | None:
    """The physical copper shape of one ``model["copper"]`` item as a
    single shapely polygon — a track's full polyline buffered by its half-
    width in one shot (round caps/joins are the physically correct shape
    for a routed trace), a via as a buffered point, a pour as its polygon
    verbatim. ``None`` for a degenerate item (a track with < 2 points)."""
    ctype = item.get("ctype")
    if ctype == "track":
        pts = _flatten_segments(item.get("segments") or [])
        if len(pts) < 2:
            return None
        r = float(item.get("width_mm", 0.0)) / 2.0
        geom = LineString(pts)
        return geom.buffer(r, quad_segs=_BUFFER_QUAD_SEGS) if r > 0 else geom
    if ctype == "via":
        r = float(item.get("dia_mm", 0.0)) / 2.0
        return Point(float(item["x"]), float(item["y"])).buffer(
            r, quad_segs=_BUFFER_QUAD_SEGS
        )
    if ctype == "pour":
        poly = item.get("polygon") or []
        if len(poly) < 3:
            return None
        return Polygon([(float(p[0]), float(p[1])) for p in poly])
    return None


def clearance_pairs_indexed(
    model: dict[str, Any], *, required_mm: float
) -> list[tuple[int, int, float]]:
    """``(item_i, item_j, gap_mm)`` for every same-layer, different-net
    copper-item pair (tracks/vias/pours) whose true edge-to-edge gap is
    below ``required_mm`` — the STRtree-accelerated engine. Indices are
    positions in ``model["copper"]``. A per-layer STRtree prunes candidates
    (``predicate='dwithin'``); the reported gap itself is shapely's own
    exact polygon-to-polygon ``distance()``, not an estimate — the
    acceleration is entirely in *which pairs get checked*, never in the
    number reported for a pair that IS checked.

    **A via has no single ``item["layer"]``** — it flashes on every layer
    :func:`_via_layer_names` says it spans, exactly like
    :func:`_copper_primitives` already handles for the reference oracle.
    One source item can therefore contribute an entry to SEVERAL per-layer
    STRtrees at once (``entries`` below is keyed by ``(source_idx, layer)``,
    not by ``source_idx`` alone) — an earlier version of this function
    read ``item.get("layer")`` unconditionally, which silently bucketed
    every via under a nonexistent ``""`` layer and dropped it out of every
    real layer's candidate set. Caught by :func:`clearance_violations_naive`
    disagreeing on exactly this case (property test,
    ``tests/test_pcb_drc.py``) — the oracle doing its job."""
    items = model.get("copper") or []
    all_layers = list(model.get("layers") or [])
    # (source_idx, net, layer, polygon) — one entry per (item, layer it's on).
    entries: list[tuple[int, str, str, BaseGeometry]] = []
    for idx, item in enumerate(items):
        poly = _copper_item_polygon(item)
        if poly is None or poly.is_empty:
            continue
        net = str(item.get("net", ""))
        if item.get("ctype") == "via":
            for layer in _via_layer_names(item, all_layers):
                entries.append((idx, net, layer, poly))
        else:
            entries.append((idx, net, str(item.get("layer", "")), poly))

    by_layer: dict[str, list[int]] = {}
    for local_i, (_src, _net, layer, _poly) in enumerate(entries):
        by_layer.setdefault(layer, []).append(local_i)

    out: list[tuple[int, int, float]] = []
    seen_src: set[tuple[int, int]] = set()
    for locals_ in by_layer.values():
        if len(locals_) < 2:
            continue
        layer_polys = [entries[i][3] for i in locals_]
        tree = STRtree(layer_polys)
        seen_local: set[tuple[int, int]] = set()
        for local_i in locals_:
            src_i, net_i, _layer_i, poly_i = entries[local_i]
            for c in tree.query(poly_i, predicate="dwithin", distance=required_mm):
                local_j = locals_[int(c)]
                src_j, net_j, _layer_j, poly_j = entries[local_j]
                if src_i == src_j or net_i == net_j:
                    continue
                key_local = (min(local_i, local_j), max(local_i, local_j))
                if key_local in seen_local:
                    continue
                seen_local.add(key_local)
                src_key = (min(src_i, src_j), max(src_i, src_j))
                if src_key in seen_src:
                    continue  # a through via already matched this source pair on another layer
                gap = poly_i.distance(poly_j)
                if gap < required_mm - _EPS:
                    seen_src.add(src_key)
                    out.append((src_key[0], src_key[1], gap))
    return out


def _clearance_detail(
    value_mm: float,
    jlc_min: float,
    required_mm: float | None,
    process: str,
    severity: str,
    margin_mm: float,
) -> str:
    """Same two-tier wording as :func:`_margin_detail`, but takes the
    per-PAIR required clearance directly rather than pulling a single
    figure off ``capability.house_default`` — the ``check_clearance``
    threshold is now per-net (``net_rules``), not always the fab's generic
    house default (module docstring)."""
    if severity == "error":
        return (
            f"copper clearance {value_mm:.3f}mm < JLC min {jlc_min:.3f}mm "
            f"({process}) — {abs(margin_mm):.3f}mm short of manufacturable"
        )
    assert required_mm is not None  # a "warn" only fires when a tier exists
    return (
        f"copper clearance {value_mm:.3f}mm — JLC min {jlc_min:.3f}mm, required "
        f"{required_mm:.3f}mm ({process}) — spends {abs(margin_mm):.3f}mm of headroom"
    )


def check_clearance(
    model: dict[str, Any],
    capability: CapabilityRow,
    *,
    net_rules: dict[str, NetRules] | None = None,
) -> list[DrcFinding]:
    """Copper-to-copper clearance, different nets — the class-rule check
    (module docstring: "class" = fab process, per JLC's own naming, read
    off ``capabilities.py``, house-default tier). STRtree-accelerated; see
    :func:`clearance_violations_naive` for the independent reference this
    is checked against.

    ``net_rules`` (net NAME -> :class:`~precis.pcb.rules.NetRules`, the
    same resolved rules :mod:`precis.pcb.realize` used to draw the copper)
    supplies a PER-NET required clearance in place of the flat
    ``house_default`` — a pair's actual threshold is the STRICTER
    (larger) of its two nets' own resolved clearance, since a net that
    wants more room never gets less just because its neighbour wants
    less. A net absent from ``net_rules`` (or ``net_rules=None``
    entirely) falls back to the generic ``house_default`` tier, today's
    behaviour unchanged."""
    field = "trace_spacing_mm"
    jlc_min = capability.jlc_min[field]
    house = capability.house_default.get(field)
    if jlc_min is None:
        return []
    if net_rules:
        resolved_values = [r.clearance_mm for r in net_rules.values()]
        query_radius = (
            max(resolved_values)
            if resolved_values
            else (house if house is not None else jlc_min)
        )
    else:
        query_radius = house if house is not None else jlc_min
    pairs = clearance_pairs_indexed(model, required_mm=query_radius)
    items = model.get("copper") or []
    findings: list[DrcFinding] = []
    for i, j, gap in pairs:
        a, b = items[i], items[j]
        required = house
        if net_rules:
            candidates = [
                r.clearance_mm
                for r in (
                    net_rules.get(str(a.get("net"))),
                    net_rules.get(str(b.get("net"))),
                )
                if r is not None
            ]
            if candidates:
                required = max(candidates)
        result = _two_tier(gap, jlc_min, required)
        if result is None:
            continue
        severity, margin = result
        where = (
            f"{a.get('ctype')}[{a.get('net')}] <-> {b.get('ctype')}[{b.get('net')}] "
            f"on {a.get('layer')}"
        )
        findings.append(
            DrcFinding(
                rule="clearance",
                severity=severity,
                where=where,
                detail=_clearance_detail(
                    gap, jlc_min, required, capability.process, severity, margin
                ),
                objects=(
                    {
                        "ctype": a.get("ctype"),
                        "net": a.get("net"),
                        "layer": a.get("layer"),
                    },
                    {
                        "ctype": b.get("ctype"),
                        "net": b.get("net"),
                        "layer": b.get("layer"),
                    },
                ),
                margin_mm=margin,
            )
        )
    return findings


# ── the O(n^2) reference oracle — no shapely, no spatial index ────────────
#
# A brute-force, obviously-correct implementation over the same primitive
# alphabet realize.py's closed-form geometry already uses (circles for
# vias, capsules — a line segment with radius — for track segments): every
# pairwise clearance is computed by exact point/segment math, not shapely.
# This is the strongest oracle available against a spatial-index bug
# precisely BECAUSE it cannot share a bug with the STRtree engine above —
# it doesn't import shapely at all.


@dataclass(frozen=True, slots=True)
class _Prim:
    """One circle (``b=None``) or capsule (line segment ``a``-``b``,
    radius ``r``) — ``group`` is the parent ``model["copper"]`` index (two
    primitives of the SAME group, e.g. two segments of one track, never
    count as a clearance pair against each other, mirroring
    :func:`precis.pcb.geom.sweep_line_crossings`'s ``group_id`` exclusion)."""

    a: Coord
    b: Coord | None
    r: float
    group: int
    net: str
    layer: str


def _copper_primitives(model: dict[str, Any]) -> list[_Prim]:
    """Track/via items only, flattened to circles/capsules — pours are
    deliberately excluded (module docstring: the oracle's scope is the
    closed-form-representable primitives; pour clearance is validated by
    the accelerated engine alone)."""
    prims: list[_Prim] = []
    for idx, item in enumerate(model.get("copper") or []):
        ctype = item.get("ctype")
        net = str(item.get("net", ""))
        if ctype == "track":
            r = float(item.get("width_mm", 0.0)) / 2.0
            layer = str(item.get("layer", ""))
            for seg in item.get("segments") or []:
                if seg.get("shape") == "arc":
                    pts = _arc_points(seg)
                    prims.extend(
                        _Prim(pts[k], pts[k + 1], r, idx, net, layer)
                        for k in range(len(pts) - 1)
                    )
                else:
                    a = (float(seg["start"][0]), float(seg["start"][1]))
                    b = (float(seg["end"][0]), float(seg["end"][1]))
                    prims.append(_Prim(a, b, r, idx, net, layer))
        elif ctype == "via":
            r = float(item.get("dia_mm", 0.0)) / 2.0
            x, y = float(item["x"]), float(item["y"])
            layers = _via_layer_names(item, list(model.get("layers") or []))
            for layer in layers:
                prims.append(_Prim((x, y), None, r, idx, net, layer))
    return prims


def _dist(a: Coord, b: Coord) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _dist_point_to_segment(p: Coord, a: Coord, b: Coord) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 < 1e-12:
        return _dist(p, a)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return _dist(p, (ax + t * dx, ay + t * dy))


def _orient(a: Coord, b: Coord, c: Coord) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(p: Coord, q: Coord, r: Coord, *, eps: float = 1e-9) -> bool:
    """``q`` assumed collinear with ``p``/``r``: is it between them?"""
    return (
        min(p[0], r[0]) - eps <= q[0] <= max(p[0], r[0]) + eps
        and min(p[1], r[1]) - eps <= q[1] <= max(p[1], r[1]) + eps
    )


def _segments_intersect(p1: Coord, p2: Coord, p3: Coord, p4: Coord) -> bool:
    """Classic orientation-test segment intersection, collinear-overlap and
    touching-endpoint cases included — needed because two SEGMENTS
    (unlike two points) can cross at an interior point that is neither
    endpoint, which no point-to-segment distance below would ever find as
    zero (see :func:`_capsule_capsule_gap`'s docstring)."""
    d1, d2 = _orient(p3, p4, p1), _orient(p3, p4, p2)
    d3, d4 = _orient(p1, p2, p3), _orient(p1, p2, p4)
    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return True
    eps = 1e-9
    if abs(d1) < eps and _on_segment(p3, p1, p4):
        return True
    if abs(d2) < eps and _on_segment(p3, p2, p4):
        return True
    if abs(d3) < eps and _on_segment(p1, p3, p2):
        return True
    return bool(abs(d4) < eps and _on_segment(p1, p4, p2))


def _capsule_capsule_gap(x: _Prim, y: _Prim) -> float:
    """Exact edge-to-edge gap between two circles/capsules, closed form.

    Circle-circle and circle-capsule reduce to a single point/segment
    distance. Segment-segment is the one non-trivial case: the minimum
    distance between two DISJOINT segments is always achieved at an
    endpoint of one projected onto the other (a standard computational-
    geometry fact — the min of the 4 endpoint-to-opposite-segment
    distances), but if the segments actually cross, that minimum is 0 at
    an INTERIOR point neither endpoint distance would find, so crossing is
    checked explicitly first via :func:`_segments_intersect`."""
    if x.b is None and y.b is None:
        center = _dist(x.a, y.a)
    elif x.b is None:
        assert y.b is not None  # the first branch already excluded "both circles"
        center = _dist_point_to_segment(x.a, y.a, y.b)
    elif y.b is None:
        center = _dist_point_to_segment(y.a, x.a, x.b)
    elif _segments_intersect(x.a, x.b, y.a, y.b):
        center = 0.0
    else:
        center = min(
            _dist_point_to_segment(x.a, y.a, y.b),
            _dist_point_to_segment(x.b, y.a, y.b),
            _dist_point_to_segment(y.a, x.a, x.b),
            _dist_point_to_segment(y.b, x.a, x.b),
        )
    return max(0.0, center - x.r - y.r)


def clearance_violations_naive(
    model: dict[str, Any], *, required_mm: float
) -> list[tuple[int, int, float]]:
    """The O(n^2) reference oracle (backlog, verbatim): every same-layer,
    different-net pair of track/via copper ITEMS whose exact minimum
    edge-to-edge gap (the min over every constituent circle/capsule sub-
    primitive pair sharing a layer — see :func:`_capsule_capsule_gap`) is
    below ``required_mm``. Pure closed-form math; no shapely, no
    dependency. Compare against :func:`check_clearance` / :func:`clearance_
    pairs_indexed` — the property test in ``tests/test_pcb_drc.py`` asserts
    the two agree on both WHICH pairs violate and the gap number itself,
    over many randomized track/via-only layouts.

    **Bug found on contact 2026-08-28, fixed here**: this used to compare
    only each GROUP's FIRST primitive's ``.layer`` to decide "do these two
    items share a layer at all" (correct for a track, whose every
    sub-primitive is on the same single layer, or a single-layer via — the
    only shapes the pre-existing synthetic test fixtures ever exercised).
    A real multi-layer via (:mod:`precis.pcb.realize`'s own
    :class:`~precis.pcb.realize.RealizedVia`, e.g. a blind via spanning
    F.Cu..In1.Cu) breaks that shortcut: two vias with only a PARTIALLY
    overlapping span (say F.Cu..In1.Cu and In1.Cu..B.Cu) can have first
    primitives on different layers, so the old check silently skipped the
    pair even though they DO share In1.Cu and DO clash there. Found by
    this exact randomized-real-via property test disagreeing with
    :func:`clearance_pairs_indexed` (which already checked every layer a
    via spans, per-layer STRtree) — the oracle doing its job on itself.
    Fixed by checking every (sub-primitive, sub-primitive) pair for a
    SHARED layer directly, taking the minimum gap over only those pairs
    that share one, rather than gating the whole group pair on one
    primitive's layer."""
    prims = _copper_primitives(model)
    by_group: dict[int, list[_Prim]] = {}
    for p in prims:
        by_group.setdefault(p.group, []).append(p)
    groups = sorted(by_group)
    out: list[tuple[int, int, float]] = []
    for gi in range(len(groups)):
        for gj in range(gi + 1, len(groups)):
            i, j = groups[gi], groups[gj]
            group_i, group_j = by_group[i], by_group[j]
            if group_i[0].net == group_j[0].net:
                continue
            best_gap: float | None = None
            for px in group_i:
                for py in group_j:
                    if px.layer != py.layer:
                        continue
                    gap = _capsule_capsule_gap(px, py)
                    if best_gap is None or gap < best_gap:
                        best_gap = gap
            if best_gap is None:
                continue  # these two items share no layer at all
            if best_gap < required_mm - _EPS:
                out.append((i, j, best_gap))
    return out


# ── trace width ─────────────────────────────────────────────────────────


def check_trace_width(
    model: dict[str, Any], capability: CapabilityRow
) -> list[DrcFinding]:
    field = "trace_width_mm"
    jlc_min = capability.jlc_min[field]
    house = capability.house_default.get(field)
    findings: list[DrcFinding] = []
    for item in model.get("copper") or []:
        if item.get("ctype") != "track":
            continue
        width = float(item.get("width_mm", 0.0))
        result = _two_tier(width, jlc_min, house)
        if result is None:
            continue
        severity, margin = result
        net, layer = item.get("net"), item.get("layer")
        findings.append(
            DrcFinding(
                rule="trace_width",
                severity=severity,
                where=f"{net} on {layer}",
                detail=_margin_detail(
                    "trace width", width, capability, field, severity, margin
                ),
                objects=({"net": net, "layer": layer},),
                margin_mm=margin,
            )
        )
    return findings


# ── annular ring (vias) ────────────────────────────────────────────────


def check_annular_ring(
    model: dict[str, Any], capability: CapabilityRow
) -> list[DrcFinding]:
    field = "annular_ring_mm"
    jlc_min = capability.jlc_min[field]
    house = capability.house_default.get(field)
    findings: list[DrcFinding] = []
    for item in model.get("copper") or []:
        if item.get("ctype") != "via":
            continue
        dia, drill = float(item.get("dia_mm", 0.0)), float(item.get("drill_mm", 0.0))
        ring = (dia - drill) / 2.0
        result = _two_tier(ring, jlc_min, house)
        if result is None:
            continue
        severity, margin = result
        net = item.get("net")
        where = f"via[{net}] @ ({item.get('x')}, {item.get('y')})"
        findings.append(
            DrcFinding(
                rule="annular_ring",
                severity=severity,
                where=where,
                detail=_margin_detail(
                    "via annular ring", ring, capability, field, severity, margin
                ),
                objects=({"net": net, "x": item.get("x"), "y": item.get("y")},),
                margin_mm=margin,
            )
        )
    return findings


# ── NPTH copper clearance (a distinct field from via annular ring) ───────


def check_npth_clearance(
    model: dict[str, Any], capability: CapabilityRow
) -> list[DrcFinding]:
    """Copper must clear a non-plated hole by ``npth_annular_ring_mm`` (JLC
    needs bare copper cleared around an NPTH for the sealing film — a
    DIFFERENT field from a via's plated annular ring, per
    ``capabilities.py``'s own module docstring). Checked against every
    layer's copper (a mechanical hole passes through the whole board, so
    the conservative check is against all of it, same "unknown ⇒
    conservative" instinct as the board-edge V-cut default)."""
    field = "npth_annular_ring_mm"
    jlc_min = capability.jlc_min[field]
    house = capability.house_default.get(field)
    if jlc_min is None:
        return []
    holes = [d for d in (model.get("drills") or []) if not d.get("plated", True)]
    if not holes:
        return []
    prims = _copper_primitives(model)
    findings: list[DrcFinding] = []
    for h in holes:
        hx, hy = float(h["x"]), float(h["y"])
        hr = float(h.get("dia_mm", 0.0)) / 2.0
        best = math.inf
        for p in prims:
            d = (
                _dist(p.a, (hx, hy))
                if p.b is None
                else _dist_point_to_segment((hx, hy), p.a, p.b)
            )
            best = min(best, d - hr - p.r)
        if best is math.inf:
            continue
        result = _two_tier(best, jlc_min, house)
        if result is None:
            continue
        severity, margin = result
        findings.append(
            DrcFinding(
                rule="npth_clearance",
                severity=severity,
                where=f"NPTH @ ({hx}, {hy})",
                detail=_margin_detail(
                    "NPTH copper clearance", best, capability, field, severity, margin
                ),
                objects=({"hole_x": hx, "hole_y": hy},),
                margin_mm=margin,
            )
        )
    return findings


# ── courtyard overlap ──────────────────────────────────────────────────


def check_courtyard_overlap(
    courtyards: list[tuple[str, float, float, float]],
) -> list[DrcFinding]:
    """``(refdes, x, y, radius_mm)`` circular courtyard approximations
    (module docstring: the same honest fallback ``realize.py`` uses when no
    real footprint courtyard is available) — any two overlapping is a hard
    error (no capability two-tier here; overlap is categorical, not a
    manufacturability margin), reported with the overlap depth so it's
    still a number, not just a flag."""
    if len(courtyards) < 2:
        return []
    geoms = [
        Point(x, y).buffer(r, quad_segs=_BUFFER_QUAD_SEGS) for _, x, y, r in courtyards
    ]
    tree = STRtree(geoms)
    findings: list[DrcFinding] = []
    seen: set[tuple[int, int]] = set()
    for i, (refdes_i, xi, yi, ri) in enumerate(courtyards):
        for c in tree.query(geoms[i], predicate="intersects"):
            j = int(c)
            if j == i:
                continue
            key = (min(i, j), max(i, j))
            if key in seen:
                continue
            seen.add(key)
            refdes_j, xj, yj, rj = courtyards[j]
            gap = _dist((xi, yi), (xj, yj)) - ri - rj
            if gap < -_EPS:
                findings.append(
                    DrcFinding(
                        rule="courtyard_overlap",
                        severity="error",
                        where=f"{refdes_i} <-> {refdes_j}",
                        detail=(
                            f"{refdes_i} and {refdes_j} courtyards overlap by "
                            f"{abs(gap):.3f}mm"
                        ),
                        objects=({"a": refdes_i, "b": refdes_j},),
                        margin_mm=gap,
                    )
                )
    return findings


# ── board-edge clearance ───────────────────────────────────────────────


def check_board_edge_clearance(
    model: dict[str, Any],
    capability: CapabilityRow,
    *,
    outline: list[list[float]] | None,
    panel_type: str | None = None,
) -> list[DrcFinding]:
    """Copper-to-board-edge clearance. ``board_edge_clearance_mm`` is TWO
    fields (module docstring) — ``panel_type='routed'`` selects the
    mechanically-routed-edge figure; anything else (including ``None``,
    the "don't know yet" default) selects V-cut, the conservative one."""
    field = (
        "board_edge_clearance_routed_mm"
        if panel_type == "routed"
        else "board_edge_clearance_vcut_mm"
    )
    jlc_min = capability.jlc_min[field]
    house = capability.house_default.get(field)
    if jlc_min is None or not outline or len(outline) < 3:
        return []
    ring_pts = [(float(p[0]), float(p[1])) for p in outline]
    if ring_pts[0] != ring_pts[-1]:
        ring_pts.append(ring_pts[0])
    boundary = LineString(ring_pts)
    findings: list[DrcFinding] = []
    for item in model.get("copper") or []:
        geom = _copper_item_polygon(item)
        if geom is None or geom.is_empty:
            continue
        gap = boundary.distance(geom)
        result = _two_tier(gap, jlc_min, house)
        if result is None:
            continue
        severity, margin = result
        net, layer, ctype = item.get("net"), item.get("layer"), item.get("ctype")
        findings.append(
            DrcFinding(
                rule="board_edge_clearance",
                severity=severity,
                where=f"{ctype}[{net}] on {layer}",
                detail=_margin_detail(
                    "board-edge clearance", gap, capability, field, severity, margin
                ),
                objects=({"net": net, "layer": layer, "ctype": ctype},),
                margin_mm=margin,
            )
        )
    return findings


# ── orchestrator ────────────────────────────────────────────────────────


def run_geometric_drc(
    model: dict[str, Any],
    *,
    capability: CapabilityRow,
    outline: list[list[float]] | None = None,
    courtyards: list[tuple[str, float, float, float]] | None = None,
    panel_type: str | None = None,
    net_rules: dict[str, NetRules] | None = None,
) -> list[DrcFinding]:
    """Every geometric DRC rule over one realized board, in one call — what
    ``view='drc'`` and the ``netlist_drc_clean`` gate evaluator both run.
    ``net_rules`` (net name -> resolved :class:`~precis.pcb.rules.NetRules`)
    threads the per-net clearance override into :func:`check_clearance`
    only — the other rules stay capability-only (module docstring: they
    check the fab's own hard limits, not an authored class preference)."""
    findings: list[DrcFinding] = []
    findings += check_clearance(model, capability, net_rules=net_rules)
    findings += check_trace_width(model, capability)
    findings += check_annular_ring(model, capability)
    findings += check_npth_clearance(model, capability)
    findings += check_board_edge_clearance(
        model, capability, outline=outline, panel_type=panel_type
    )
    if courtyards:
        findings += check_courtyard_overlap(courtyards)
    return findings


__all__ = [
    "DEFAULT_COURTYARD_RADIUS_MM",
    "DrcFinding",
    "check_annular_ring",
    "check_board_edge_clearance",
    "check_clearance",
    "check_courtyard_overlap",
    "check_npth_clearance",
    "check_trace_width",
    "clearance_pairs_indexed",
    "clearance_violations_naive",
    "process_for_stackup",
    "run_geometric_drc",
]

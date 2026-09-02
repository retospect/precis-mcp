"""Geometric DRC on realized copper (L5) — pcb-guided-place-route Slice 8.

**The DRC split** (see ``ir.py``'s module docstring for its half): two
engines at the IR level boundary. ``ir.py`` owns **graph feasibility**
(L0-L4, no geometry, no shapely), inside the optimizer's constraint set.
This module owns **geometric DRC** (L5, on realized copper) — class-rule
clearance, trace width, annular ring, via-to-pad keep-out, via-to-via
keep-out, courtyard overlap, board-edge clearance, silk rendering/
printability (:func:`check_silk_missing`/:func:`check_silk_printability`,
off :mod:`precis.pcb.silk`'s own ``SilkPlacement`` census) — the *final*
check, not the main event. Both emit
``pcb_drc_findings`` rows and share ``view='drc'`` (``handlers/pcb.py``);
``eyes.drc_lite`` is superseded and retired.

**Input model**: the same copper-model dict :mod:`precis.pcb.realize`
produces (:func:`precis.pcb.realize.to_gerber_model`) that
:mod:`precis.pcb.gerber` consumes — ``{"layers": [...], "outline": [...],
"copper": [{"ctype": "track"|"via"|"pour", ...}]}`` (exact per-``ctype``
shape: that module's docstring). Never a second geometry representation
for the same board.

``realize.py`` emits ``ctype='via'`` copper wherever a track's routed
layer differs from its pads' layer, always carrying ``span``/``layers``,
never a scalar ``layer`` (:class:`~precis.pcb.realize.RealizedVia`'s
docstring: why that distinction is load-bearing here) — real input for
:func:`check_annular_ring` and the via halves of
:func:`check_clearance`/:func:`check_npth_clearance`.

**Two-tier margin, not bare pass/fail.** Every rule reads both tiers off
:mod:`precis.pcb.capabilities`: ``jlc_min`` (fab's published,
unmanufacturable-below floor) and ``house_default`` (our margin above
it). A finding fires **error** below ``jlc_min`` (unmanufacturable) or
**warn** clearing ``jlc_min`` but eating ``house_default`` margin — either
way ``margin_mm``/``detail`` name the exact numbers, never a bare boolean.
A ``None`` capability field (JLC publishes none for that process/field)
means the rule doesn't apply — silently skipped, never a violated zero.

**Class-rule clearance reads both the fab capability row and a
``pcb_net_classes`` override, through ONE resolver.** ``trace_spacing_mm``
is JLC's name for copper-to-copper clearance, keyed by process
(2-layer/4-layer/aluminum — the "class"), same precedent as
:func:`precis.pcb.escape.compute_gaps`; that stays the absolute floor. A
net whose class carries an authored ``pcb_net_classes.rules.clearance_mm``
(or whose current annotation implies a wider IPC-2221 trace, hence more
room) resolves through :func:`precis.pcb.rules.resolve_net_rules` — the
SAME resolver :mod:`precis.pcb.realize` uses for track width and
:mod:`precis.pcb.cost` uses for ``thermal_rise``. :func:`check_clearance`'s
caller supplies the resolved :class:`~precis.pcb.rules.NetRules` per net
name via ``net_rules=``; ``None`` (default) keeps capability-only
behaviour.

**Board-edge clearance is two fields**: ``board_edge_clearance_routed_mm``
vs. ``..._vcut_mm``. Unknown panel type → :func:`check_board_edge_clearance`
uses the V-cut figure (the conservative one, per ``capabilities.py``).

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
board. Pour polygons — and, since 2026-08-29, PAD polygons
(:func:`clearance_pairs_indexed` now folds ``model["pads"]`` into the same
per-layer check every other copper item gets; see its own docstring) — are
checked by the accelerated engine only, not cross-validated by the
dependency-free oracle, which is deliberately restricted to the
circle/capsule primitives that make closed-form math possible (a
rect/obround pad is neither). A stated, not silent, gap in oracle
coverage — and note it is the SAME gap pours already had: the oracle
agreeing with the accelerated engine on tracks/vias was never evidence
that either one was checking pads or pours at all.

**A reference oracle only checks the inputs it's fed**: ``clearance_
violations_naive`` was correct against every synthetic fixture and still
shipped a real bug (its own docstring: compared only each group's FIRST
primitive when deciding two items shared a layer, so a multi-layer via
with a partially-overlapping span could be silently skipped). Agreement
with a synthetic-fixture oracle proves agreement on the shapes tested,
not correctness against production geometry.

**STRtree is used only for the clearance rule**: copper-to-copper
clearance is the one genuinely O(n^2) rule (every different-net pair per
layer is a candidate); trace width, annular ring, NPTH clearance and
board-edge clearance are O(n) or O(n·holes) per-item, no spatial index
needed. Courtyard overlap is also pairwise and uses the same STRtree
machinery, at instance rather than copper-item scale.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
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
from precis.pcb.geom import _orient, dist_point_to_segment
from precis.pcb.geom import dist as _dist
from precis.pcb.rules import NetRules
from precis.pcb.silk import SilkPlacement

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
    """The physical copper shape of one copper-bearing item as a single
    shapely polygon — a track's full polyline buffered by its half-width in
    one shot (round caps/joins are the physically correct shape for a
    routed trace), a via as a buffered point, a pour as its exterior
    polygon WITH its ``holes`` cut out as interior rings, a PAD (``model
    ["pads"]``, tagged ``ctype="pad"`` by every caller that mixes it into a
    copper-item list — pads carry no ``ctype`` of their own on
    ``model["pads"]``) as its real ``shape`` (circle/rect/obround), not a
    circle stand-in. ``None`` for a degenerate item (a track with < 2
    points, or a pad/via with zero size).

    **Pads are copper, not a lesser class of it — this is the ONE shape
    function.** Every other geometric rule that needs "what does this pad
    physically occupy" (:func:`clearance_pairs_indexed` via
    :func:`check_clearance`) reads it from here, not a second circle/rect
    approximation — that divergence (two functions independently deciding
    "what shape is this thing", answering differently) is this
    subsystem's own most-repeated defect (see :func:`pads_for_ir`'s own
    docstring in :mod:`precis.pcb.realize` for the pad-geometry half of
    the same lesson). ``check_via_pad_keepout`` and ``check_outline_
    containment`` still carry their own PRE-EXISTING circumscribed-circle
    pad approximations (a via's keep-out uses plain circle/circle math
    with no shapely dependency at all; containment predates this
    function's pad support) — reported, not silently merged in, since
    changing either one's numbers was not asked for here.

    **A pour's ``holes`` are antipads, not decoration.** :mod:`precis.pcb.
    planes` (:func:`~precis.pcb.planes.plane_pours`, its own docstring)
    punches a hole around every foreign-net via/track that passes through a
    poured layer specifically so the fill does not short it — the same
    reason :mod:`precis.pcb.gerber`'s ``_emit_region`` images a hole's
    interior as clear-polarity copper rather than solid. Reading only
    ``polygon`` here made every consumer of this function see a pour as a
    SOLID sheet where the real copper has a hole cut in it — so a trace
    correctly antipadded inside that hole reported a clearance violation
    against copper that, on the real board, is not there at all. Now that
    copper fill can share a layer with routing (``ir.layer_is_routable``/
    ``layer_is_pourable``), every antipadded feature on a filled layer hit
    this, not just a rare edge case."""
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
        holes = [
            [(float(p[0]), float(p[1])) for p in hole]
            for hole in item.get("holes") or []
            if len(hole) >= 3
        ]
        return Polygon([(float(p[0]), float(p[1])) for p in poly], holes)
    if ctype == "pad":
        shape = item.get("shape", "circle")
        x, y = float(item["x"]), float(item["y"])
        w = float(item.get("w", 0.0))
        h = float(item.get("h", w))
        if w <= 0 or h <= 0:
            return None
        if shape == "circle":
            return Point(x, y).buffer(w / 2.0, quad_segs=_BUFFER_QUAD_SEGS)
        if shape == "rect":
            hw, hh = w / 2.0, h / 2.0
            return Polygon(
                [(x - hw, y - hh), (x + hw, y - hh), (x + hw, y + hh), (x - hw, y + hh)]
            )
        if shape == "obround":
            # A capsule -- the same "buffer a segment" construction a track
            # uses above -- with the rounded ends on the LONGER of w/h (the
            # conventional "stadium" reading of an oval pad) and a straight
            # zero-length degenerate case at w == h so this never divides
            # by an undefined axis.
            if w == h:
                return Point(x, y).buffer(w / 2.0, quad_segs=_BUFFER_QUAD_SEGS)
            if w > h:
                half_len, r = (w - h) / 2.0, h / 2.0
                line = LineString([(x - half_len, y), (x + half_len, y)])
            else:
                half_len, r = (h - w) / 2.0, w / 2.0
                line = LineString([(x, y - half_len), (x, y + half_len)])
            return line.buffer(r, quad_segs=_BUFFER_QUAD_SEGS)
        raise ValueError(f"unknown pad shape {shape!r}")
    return None


def _clearance_items(model: dict[str, Any]) -> list[dict[str, Any]]:
    """Every copper-bearing item :func:`clearance_pairs_indexed` /
    :func:`check_clearance` reason about, as ONE list: ``model["copper"]``
    followed by ``model["pads"]`` (each pad shallow-copied with
    ``ctype="pad"`` tacked on so :func:`_copper_item_polygon`'s existing
    ``ctype`` dispatch — and every finding's ``where``/``objects`` text —
    handles a pad exactly like a track/via/pour, never a special case).
    Both functions below build this list the SAME way so an index computed
    by one always means the same item to the other — mirrors :mod:`precis.
    pcb.connectivity`'s ``_pad_primitives(model, start_group=len(model
    ["copper"]))`` offset convention, the precedent already set for
    concatenating pads onto a copper-item index space."""
    return [*(model.get("copper") or []), *_tagged_pads(model)]


def _tagged_pads(model: dict[str, Any]) -> list[dict[str, Any]]:
    """``model["pads"]`` with ``ctype="pad"`` added (shallow copy — never
    mutates the caller's pad dicts) so a pad can sit in the same item list
    as ``model["copper"]``'s tracks/vias/pours."""
    return [{**pad, "ctype": "pad"} for pad in model.get("pads") or []]


def clearance_pairs_indexed(
    model: dict[str, Any], *, required_mm: float
) -> list[tuple[int, int, float]]:
    """``(item_i, item_j, gap_mm)`` for every same-layer, different-net
    copper-item pair (tracks/vias/pours/PADS) whose true edge-to-edge gap
    is below ``required_mm`` — the STRtree-accelerated engine. Indices are
    positions in :func:`_clearance_items`'s combined list —
    ``model["copper"]`` followed by ``model["pads"]`` — NOT
    ``model["copper"]`` alone; a pad-involving pair's index can land past
    ``len(model["copper"])``. A per-layer STRtree prunes candidates
    (``predicate='dwithin'``); the reported gap itself is shapely's own
    exact polygon-to-polygon ``distance()``, not an estimate — the
    acceleration is entirely in *which pairs get checked*, never in the
    number reported for a pair that IS checked.

    **Pads used to be invisible here entirely** — this function iterated
    ``model["copper"]`` only, so a pad (a separate top-level model key,
    ``to_gerber_model``'s/``_render_drc``'s own shape) was never a
    candidate on EITHER side of a pair: pad-vs-pour, pad-vs-track,
    pad-vs-via and pad-vs-pad clearance all went unchecked, on a board
    whose pads are exactly the copper you solder to. Fixed by folding
    ``model["pads"]`` into the same per-layer STRtree pass as every other
    copper item (:func:`_clearance_items`), not a parallel pad-only pass —
    a pad is a same-net-exempt, different-net-checked copper item like any
    other, not a second kind of question.

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
    ``tests/test_pcb_drc.py``) — the oracle doing its job.

    **The reference oracle does NOT cross-check a pad pair.**
    :func:`clearance_violations_naive` is restricted to the circle/capsule
    primitive alphabet (module docstring); a rect/obround pad is neither,
    so pad clearance — like pour clearance before it — is validated by
    this accelerated engine alone, a stated gap in oracle coverage, not a
    silent one."""
    items = _clearance_items(model)
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
    is checked against (pad pairs are NOT part of that comparison — see
    :func:`clearance_pairs_indexed`'s own docstring).

    **Pads are checked here too** (:func:`_clearance_items`) — a pad is
    the copper you solder to, and is exempt from this rule on the same
    terms as any other same-net copper (a trace legitimately lands on its
    own pad) and checked on the same terms otherwise (a foreign pad, pour,
    track or via too close is exactly what this rule exists to catch).

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
    items = _clearance_items(model)
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
    """Track/via items only, flattened to circles/capsules — pours AND
    pads are deliberately excluded (module docstring: the oracle's scope
    is the closed-form-representable primitives; a rect/obround pad no
    more fits a circle/capsule than a pour polygon does, so pad clearance
    — like pour clearance before it — is validated by the accelerated
    engine alone)."""
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
        center = dist_point_to_segment(x.a, y.a, y.b)
    elif y.b is None:
        center = dist_point_to_segment(y.a, x.a, x.b)
    elif _segments_intersect(x.a, x.b, y.a, y.b):
        center = 0.0
    else:
        center = min(
            dist_point_to_segment(x.a, y.a, y.b),
            dist_point_to_segment(x.b, y.a, y.b),
            dist_point_to_segment(y.a, x.a, x.b),
            dist_point_to_segment(y.b, x.a, x.b),
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

    **Checks every (sub-primitive, sub-primitive) pair for a SHARED
    layer directly**, taking the minimum gap over only those pairs that
    share one, rather than gating the whole group pair on one
    primitive's layer. Comparing only each GROUP's FIRST primitive's
    ``.layer`` is not enough to decide "do these two items share a layer
    at all": that's correct for a track (every sub-primitive on the same
    single layer) or a single-layer via, but a real multi-layer via
    (:mod:`precis.pcb.realize`'s own
    :class:`~precis.pcb.realize.RealizedVia`, e.g. a blind via spanning
    F.Cu..In1.Cu) breaks that shortcut: two vias with only a PARTIALLY
    overlapping span (say F.Cu..In1.Cu and In1.Cu..B.Cu) can have first
    primitives on different layers even though they DO share In1.Cu and
    DO clash there."""
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
                else dist_point_to_segment((hx, hy), p.a, p.b)
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


# ── via-to-pad keep-out (a via is a drilled hole, not a trace) ──────────


def check_via_pad_keepout(
    model: dict[str, Any], capability: CapabilityRow
) -> list[DrcFinding]:
    """A via must clear every pad — including a pad on its OWN net — by
    the fab's minimum copper isolation. Closest analogue is
    :func:`check_npth_clearance` (drill-versus-copper, same closed-form
    circle math, no shapely): a via is likewise a drilled hole, just a
    plated one, and this rule is that same "hole must clear copper"
    question asked of vias against pads specifically.

    **Why this cannot be expressed by tightening :func:`check_clearance`.**
    That rule deliberately EXEMPTS same-net copper (module docstring's
    two-tier section) — correctly: same-net copper touching is how a
    trace joins a pad. A via is not a trace. It is a hole drilled through
    the board; landing its annulus on a pad — even the via's own net's
    pad — wicks solder down the barrel and starves the joint, or on a
    through-hole pad drills a second hole through the first. Widening the
    clearance exemption to cover this would make the wrong case legal
    (same-net trace-into-pad is fine; same-net via-onto-pad is not), so
    the two questions need two rules over the same geometry — the same
    relationship :func:`check_connectivity` has to :func:`check_clearance`:
    a different question, not a variant of the same one.

    The margin is DERIVED, not invented: ``trace_spacing_mm`` is already
    this project's figure for how close two independent copper features
    may legally sit (module docstring, and :func:`check_clearance`'s own
    field) — a via's plated annulus and a pad's solder land are
    independent features by this rule's whole premise, so the same jlc_min
    floor applies, net or no net. Severity is always ``error`` (no warn
    tier): a via drilled into a land is a manufacturing defect, not a
    margin the house_default tier would grade.
    """
    field = "trace_spacing_mm"
    required = capability.jlc_min[field]
    if required is None:
        return []
    all_layers = list(model.get("layers") or [])
    pads = model.get("pads") or []
    findings: list[DrcFinding] = []
    for item in model.get("copper") or []:
        if item.get("ctype") != "via":
            continue
        vx, vy = float(item["x"]), float(item["y"])
        vr = float(item.get("dia_mm", 0.0)) / 2.0
        via_net = item.get("net")
        via_layers = set(_via_layer_names(item, all_layers))
        for pad in pads:
            if pad.get("layer") not in via_layers:
                continue
            px, py = float(pad["x"]), float(pad["y"])
            w = float(pad.get("w", 0.0))
            h = float(pad.get("h", w))
            # Circumscribed, not inscribed: the same conservative direction
            # check_outline_containment already takes for a rect/obround
            # pad approximated as a circle — over-stating the pad can only
            # produce an extra finding a human sees, understating it can
            # hide a real via-on-pad.
            pr = max(w, h) / 2.0
            gap = _dist((vx, vy), (px, py)) - vr - pr
            if gap >= required - _EPS:
                continue
            pad_net, pad_layer = pad.get("net"), pad.get("layer")
            findings.append(
                DrcFinding(
                    rule="via_pad_keepout",
                    severity="error",
                    where=(
                        f"via[{via_net}] @ ({vx}, {vy}) <-> "
                        f"pad[{pad_net}] on {pad_layer}"
                    ),
                    detail=(
                        f"via clears pad[{pad_net}] by {gap:.3f}mm, needs "
                        f"{required:.3f}mm (JLC min {field}, "
                        f"{capability.process}) — a via drilled into a solder "
                        "land starves the joint regardless of net"
                    ),
                    objects=(
                        {
                            "via_net": via_net,
                            "via_x": vx,
                            "via_y": vy,
                            "pad_net": pad_net,
                            "pad_layer": pad_layer,
                        },
                    ),
                    margin_mm=gap - required,
                )
            )
    return findings


# ── via-to-via keep-out (two drilled holes, not a clearance pair) ───────


def check_via_via_keepout(
    model: dict[str, Any], capability: CapabilityRow
) -> list[DrcFinding]:
    """Two vias must not overlap — COPPER (the plated barrel) and the
    DRILLED HOLE alike, net-blind, same tier of defect as
    :func:`check_via_pad_keepout` (that function's own docstring already
    makes the argument this one inherits: :func:`check_clearance`
    deliberately exempts same-net copper because a trace legally lands on
    its own pad, and that exemption never covered a via — a via is a hole
    drilled through the board, not a trace, and this is that same
    "hole must clear copper" question restricted to another via's barrel
    and, more fundamentally, its DRILL).

    **Two distinct geometries, two distinct margins:**

    - **copper-to-copper** (barrel annulus vs. barrel annulus): the SAME
      ``trace_spacing_mm`` jlc_min :func:`check_via_pad_keepout` already
      reads for a via's annulus against independent copper — two via
      barrels are exactly that, independent copper features, net or no
      net.
    - **hole-to-hole** (drilled circle vs. drilled circle): this
      capability table publishes no hole-to-hole spacing figure, and none
      is invented here (``capabilities.py``'s own "never carry a figure
      across" discipline, and this codebase's live-JLC-page verification
      standard for every OTHER figure in that table — a number this
      module cannot check against a live page has no business claiming
      that provenance). The threshold used instead is the physical
      definition of two DISTINCT holes: their drilled circles must not
      intersect (required = 0, i.e. centre distance >= the sum of the two
      drill radii). Two holes that overlap at all are, physically,
      already one larger hole or an unintended slot — true regardless of
      any fab's published tolerance, so no published figure is needed to
      state it. This is the SAME "categorical, not a manufacturability
      margin" treatment :func:`check_courtyard_overlap` already gives
      physical overlap; there is no ``house_default`` tier for either.

    **A correctly-spread stitched group stays quiet.**
    :mod:`precis.pcb.realize` (``_route_pass``) spreads a same-net
    ampacity-sized via group along a pitch of ``via_dia_mm +
    clearance_mm`` — a copper gap of exactly ``clearance_mm`` (the
    resolved net/house clearance, which is always at or above this rule's
    ``trace_spacing_mm`` floor by construction) and a hole gap of
    ``via_dia_mm + clearance_mm - drill_mm`` (strictly positive, since a
    via's annular ring — ``(dia_mm - drill_mm) / 2`` — is itself positive
    by construction): neither margin trips for a group spread by that
    code path.

    **Two vias at the EXACT SAME coordinate, on purpose, is a DIFFERENT
    construct this rule cannot tell apart from the defect it exists to
    catch.** Nothing in the realized copper model marks "this pair is one
    deliberate stack" — same net, same net, same (x, y) is indistinguishable
    from a real duplicate-via bug (two placement/route passes silently
    emitting the same via twice), and this codebase's own discipline is to
    report a real geometric collision rather than silently assume intent
    (module docstring: a spatial-index bug that silently misses a
    neighbour "produces a clean DRC pass on a shorted board" — inventing a
    same-coordinate exemption here is the identical failure mode aimed at
    a different rule). Stated here, not guessed past: if this codebase
    grows an intentional same-spot via-stack construct, it needs its own
    marker in the model for this rule to key off, not a same-net/
    same-coordinate heuristic.

    Severity is always ``error`` — like :func:`check_via_pad_keepout`, a
    categorical manufacturing defect, not a margin the ``house_default``
    tier would grade."""
    field = "trace_spacing_mm"
    copper_required = capability.jlc_min[field]
    if copper_required is None:
        return []
    all_layers = list(model.get("layers") or [])
    vias = [item for item in model.get("copper") or [] if item.get("ctype") == "via"]
    findings: list[DrcFinding] = []
    for a_i, via_a in enumerate(vias):
        ax, ay = float(via_a["x"]), float(via_a["y"])
        a_dia = float(via_a.get("dia_mm", 0.0))
        a_drill = float(via_a.get("drill_mm", 0.0))
        a_net = via_a.get("net")
        a_layers = set(_via_layer_names(via_a, all_layers))
        for b_i in range(a_i + 1, len(vias)):
            via_b = vias[b_i]
            b_layers = set(_via_layer_names(via_b, all_layers))
            if not (a_layers & b_layers):
                continue  # no shared copper layer -- not the same physical barrel
            bx, by = float(via_b["x"]), float(via_b["y"])
            b_dia = float(via_b.get("dia_mm", 0.0))
            b_drill = float(via_b.get("drill_mm", 0.0))
            b_net = via_b.get("net")
            center = _dist((ax, ay), (bx, by))
            where = f"via[{a_net}] @ ({ax}, {ay}) <-> via[{b_net}] @ ({bx}, {by})"
            objects = (
                {
                    "a_net": a_net,
                    "a_x": ax,
                    "a_y": ay,
                    "b_net": b_net,
                    "b_x": bx,
                    "b_y": by,
                },
            )

            copper_gap = center - (a_dia / 2.0 + b_dia / 2.0)
            if copper_gap < copper_required - _EPS:
                findings.append(
                    DrcFinding(
                        rule="via_via_keepout",
                        severity="error",
                        where=where,
                        detail=(
                            f"via barrels clear each other by {copper_gap:.3f}mm, "
                            f"needs {copper_required:.3f}mm (JLC min {field}, "
                            f"{capability.process}) — independent via copper "
                            "regardless of net"
                        ),
                        objects=objects,
                        margin_mm=copper_gap - copper_required,
                    )
                )

            hole_gap = center - (a_drill / 2.0 + b_drill / 2.0)
            if hole_gap < -_EPS:
                findings.append(
                    DrcFinding(
                        rule="via_via_keepout",
                        severity="error",
                        where=where,
                        detail=(
                            f"drilled holes overlap by {abs(hole_gap):.3f}mm "
                            f"(centres {center:.3f}mm apart, drills "
                            f"{a_drill:.3f}mm/{b_drill:.3f}mm) — two distinct "
                            "holes cannot occupy the same space; a broken bit "
                            "or an unintended slot, regardless of net"
                        ),
                        objects=objects,
                        margin_mm=hole_gap,
                    )
                )
    return findings


# ── courtyard overlap ──────────────────────────────────────────────────


#: One instance's courtyard as DRC receives it: refdes plus the polygon in
#: BOARD coordinates. Was ``(refdes, x, y, radius_mm)`` until 2026-08-30 —
#: a circle could not stand in for the real shape, over-reserving an edge
#: connector eightfold while UNDER-reserving a SOIC-8, so a rule built on
#: it enforced a boundary that was not the one the placer respected or the
#: one the silkscreen showed. See ``docs/backlog/pcb-courtyard-polygon.md``.
Courtyard = tuple[str, list[tuple[float, float]]]


def check_courtyard_overlap(courtyards: list[Courtyard]) -> list[DrcFinding]:
    """Any two parts' courtyards overlapping is a hard error — no
    capability two-tier here; overlap is categorical, not a
    manufacturability margin.

    The polygons are :func:`precis.pcb.ir.instance_courtyard_polygon`
    placed into board coordinates — **the same objects the placer reserves
    and the silkscreen draws.** That identity is the point of this
    signature: DRC on circles while the placer went polygon would recreate
    exactly the drift the courtyard work exists to remove, with the added
    trap that the drift only shows on parts whose aspect ratio is far from
    square.

    Reported with two numbers rather than one, because a polygon overlap
    has no single "depth": the intersection AREA (exact, unambiguous) in
    the detail, and ``margin_mm`` as the negative of the overlap's
    depth — the shorter side of the overlap region
    (:func:`_overlap_depth_mm`), i.e. how far the parts must move apart
    along the easier axis to separate. A pair that overlaps only near a corner and a pair
    that is half-buried report very different second numbers, which is the
    thing a reader wants and a bare flag cannot give."""
    if len(courtyards) < 2:
        return []
    geoms = [Polygon(poly) if len(poly) >= 3 else None for _, poly in courtyards]
    indexed = [(i, g) for i, g in enumerate(geoms) if g is not None and not g.is_empty]
    if len(indexed) < 2:
        return []
    tree = STRtree([g for _, g in indexed])
    findings: list[DrcFinding] = []
    seen: set[tuple[int, int]] = set()
    for pos, (i, gi) in enumerate(indexed):
        for c in tree.query(gi, predicate="intersects"):
            if int(c) == pos:
                continue
            j, gj = indexed[int(c)]
            key = (min(i, j), max(i, j))
            if key in seen:
                continue
            seen.add(key)
            overlap = gi.intersection(gj)
            if overlap.is_empty or overlap.area <= _EPS:
                # A shared edge or a single touching vertex: STRtree's
                # `intersects` counts it, zero area means it is not one.
                continue
            findings.append(
                DrcFinding(
                    rule="courtyard_overlap",
                    severity="error",
                    where=f"{courtyards[i][0]} <-> {courtyards[j][0]}",
                    detail=(
                        f"{courtyards[i][0]} and {courtyards[j][0]} courtyards "
                        f"overlap over {overlap.area:.4f}mm^2, "
                        f"{_overlap_depth_mm(overlap):.3f}mm deep"
                    ),
                    objects=({"a": courtyards[i][0], "b": courtyards[j][0]},),
                    margin_mm=-_overlap_depth_mm(overlap),
                )
            )
    return findings


def _overlap_depth_mm(overlap: BaseGeometry) -> float:
    """How far two courtyards have to move apart to separate, along the
    easier axis — the SHORTER side of the overlap region's bounding box.

    Not the minimum translation distance (the textbook penetration depth,
    which needs a full Minkowski difference), and deliberately not "how
    far is the deepest vertex buried", which was the first cut here and
    returned **zero** for the commonest case there is: two axis-aligned
    courtyards overlapping in a band, where every vertex of one lies
    exactly ON the other's edge and shapely's ``contains`` — correctly —
    excludes a boundary point. A depth measure that reads 0.000mm on a
    real 1mm² overlap is worse than no number, because it looks like a
    near-miss.

    The shorter bbox side has none of that fragility and keeps the
    intuition a reader needs: a wide shallow band and a deep narrow one
    report differently, and the number falls to zero only when the
    overlap really is degenerate.

    **It is a LOWER BOUND, exact only when both courtyards share an
    axis-aligned orientation** — which is the common case, and the case
    the constant was chosen for. Two long thin parts rotated to +45 and
    -45 degrees and crossing near their centres measure ~0.85mm by this
    reading while genuinely needing ~14mm of travel to separate: the
    intersection's bbox is a small diamond, but backing either part out
    means sliding it the length of the other. The overlap AREA in the
    same finding does not have that blind spot, which is why the detail
    carries both numbers and this one is never the whole report."""
    if overlap.is_empty:
        return 0.0
    x0, y0, x1, y1 = overlap.bounds
    return min(x1 - x0, y1 - y0)


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
    the "don't know yet" default) selects V-cut, the conservative one.

    **Pads used to be invisible here** — this rule iterated
    ``model["copper"]`` only, so a pad (a separate top-level model key) was
    never a candidate: a pad sitting inside the outline but nearer the edge
    than the fab can manufacture went unreported, on a board whose pads are
    exactly the copper you solder to. Fixed by reading
    :func:`_clearance_items` (copper items followed by ``model["pads"]``,
    :func:`_copper_item_polygon`'s exact ``ctype == "pad"`` branch) instead
    of ``model["copper"]`` alone — the same fix already made to
    :func:`clearance_pairs_indexed`, for the same reason. This is a
    DIFFERENT question from :func:`check_outline_containment` (that
    function's own docstring): containment asks whether a pad is on the
    board at all (binary, no margin); this rule asks whether a pad that IS
    on the board sits too near the edge to manufacture (two-tier margin).
    A pad can fail one, the other, both, or neither."""
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
    for item in _clearance_items(model):
        geom = _copper_item_polygon(item)
        if geom is None or geom.is_empty:
            continue
        gap = boundary.distance(geom)
        result = _two_tier(gap, jlc_min, house)
        if result is None:
            continue
        severity, margin = result
        net, layer, ctype = item.get("net"), item.get("layer"), item.get("ctype")
        # Carry the offending item's own coordinates when it has them.
        # Without these the finding names a CLASS of object ("a via on
        # VCC3V3") and not an object, and a board carries many of those --
        # an investigation then cannot get from the finding to the geometry
        # without instrumenting a run, which is how a 10um shortfall on this
        # very rule sat unattributed. Tracks and pours have no single point
        # and simply omit them.
        obj: dict[str, Any] = {"net": net, "layer": layer, "ctype": ctype}
        if item.get("x") is not None and item.get("y") is not None:
            obj["x"], obj["y"] = float(item["x"]), float(item["y"])
        findings.append(
            DrcFinding(
                rule="board_edge_clearance",
                severity=severity,
                where=f"{ctype}[{net}] on {layer}",
                detail=_margin_detail(
                    "board-edge clearance", gap, capability, field, severity, margin
                ),
                objects=(obj,),
                margin_mm=margin,
            )
        )
    return findings


def check_outline_containment(
    model: dict[str, Any],
    *,
    outline: list[list[float]] | None,
    courtyards: list[Courtyard] | None = None,
) -> list[DrcFinding]:
    """Copper, pads and parts must be ON the board.

    **A different question from :func:`check_board_edge_clearance`, and the
    reason that one cannot answer it.** Edge clearance measures
    ``boundary.distance(geom)`` — the distance to the outline as a *line*.
    That is unsigned, so it is symmetric about the edge: it fires on copper
    1mm inside and copper 1mm outside alike, and stays silent on copper
    20mm outside, because 20mm is not a small gap. The check is about
    proximity to a boundary and says nothing about which side of it you are
    on.

    Measured on the reference design with the outline shrunk to a board the
    parts cannot fit in: at 20mm square, 24 of 29 parts, 48 of 81 pads and
    70 copper items lay outside the board and DRC reported 10 errors. At
    2mm square — every pad outside, the whole design off the board — it
    reported **nine**. The count went DOWN as the board got more absurd,
    which is exactly what a proximity check does when the geometry walks
    away from the boundary entirely.

    There is no two-tier margin here and no capability field to read. A
    fab images what is inside the profile; copper outside it is not
    marginal, it does not exist on the delivered board. Severity is always
    ``error``.
    """
    if not outline or len(outline) < 3:
        return []
    ring = [(float(p[0]), float(p[1])) for p in outline]
    board = Polygon(ring)
    if not board.is_valid:
        board = board.buffer(0)
    if board.is_empty:
        return []

    findings: list[DrcFinding] = []

    def outside(geom: BaseGeometry, rule_where: str, obj: dict[str, Any]) -> None:
        if board.covers(geom):
            return
        # How far out, and whether any of it is on the board at all — the
        # two things a reader needs to tell "a pad hanging over the edge"
        # from "this part was never placed on the board".
        over = geom.difference(board)
        wholly = not board.intersects(geom)
        gap = board.distance(geom) if wholly else 0.0
        detail = (
            f"{rule_where} lies {'entirely' if wholly else 'partly'} outside "
            "the board outline"
        )
        detail += (
            f" — {gap:.3f}mm beyond the edge at the nearest point"
            if wholly
            else f" — {over.area:.4f}mm² of it overhangs"
        )
        findings.append(
            DrcFinding(
                rule="outline_containment",
                severity="error",
                where=rule_where,
                detail=detail + "; a fab images only what is inside the profile",
                objects=(obj,),
                margin_mm=-gap if wholly else None,
            )
        )

    for item in model.get("copper") or []:
        geom = _copper_item_polygon(item)
        if geom is None or geom.is_empty:
            continue
        net, layer, ctype = item.get("net"), item.get("layer"), item.get("ctype")
        outside(
            geom,
            f"{ctype}[{net}] on {layer}",
            {"net": net, "layer": layer, "ctype": ctype},
        )

    for pad in model.get("pads") or []:
        # Deliberately NOT _copper_item_polygon's exact rect/obround pad
        # shape (that function's own docstring names this function as one
        # of two pre-existing pad approximations it did not unify away) —
        # this is a circumscribed-circle stand-in that predates the exact
        # polygon, and swapping it in here would change which pads report
        # a containment violation and how much area is claimed to overhang,
        # a real behaviour change nobody asked for while fixing board-edge
        # clearance's missing pad coverage. A THIRD pad-shape notion, now
        # named rather than silently duplicated: circumscribed circle here,
        # circumscribed circle again in check_via_pad_keepout, exact
        # polygon everywhere else via _copper_item_polygon.
        w = float(pad.get("w", 0.0))
        h = float(pad.get("h", w))
        geom = Point(float(pad["x"]), float(pad["y"])).buffer(
            max(w, h) / 2.0, quad_segs=_BUFFER_QUAD_SEGS
        )
        net, layer = pad.get("net"), pad.get("layer")
        outside(geom, f"pad[{net}] on {layer}", {"net": net, "layer": layer})

    for refdes, poly in courtyards or []:
        if len(poly) < 3:
            continue  # a pinless part the caller gave no fallback shape
        outside(Polygon(poly), f"part {refdes}", {"refdes": refdes})

    # Silkscreen ink is imaged too — a refdes label or a courtyard drawn
    # past the outline is exactly as absent from the delivered board as
    # copper would be, and until this loop existed `view='drc'` never saw
    # it: `precis.pcb.silk.build_silk` checked a candidate against pads/
    # vias/committed silk but never against the board itself (silk.py's
    # own module docstring). `_silk_item_polygon` reads the SAME
    # ``model["silkscreen"]`` shape :func:`check_silk_missing` already
    # consumes, so a caller building one model gets both checks over it
    # for free.
    for side, draws in (model.get("silkscreen") or {}).items():
        for draw in draws:
            geom = _silk_item_polygon(draw)
            if geom is None or geom.is_empty:
                continue
            role = str(draw.get("role") or "")
            refdes = str(draw.get("refdes") or "")
            outside(
                geom,
                f"silk {role}[{refdes}] on {side}",
                {"role": role, "refdes": refdes, "side": side},
            )

    return findings


# ── silkscreen: a label/courtyard that never rendered is a DRC error ─────
#
# `precis.pcb.silk.build_silk` DROPS a refdes label, a courtyard outline or
# a pin-1 tick outright when it cannot be placed without colliding with a
# pad/via/other silk. Before this section that fact lived ONLY in
# `SilkResult.dropped`/`.relocated` -- human-readable prose nothing checked
# -- so a board could ship with unlabelled parts and read as DRC-clean.
# `SilkPlacement` (silk.py) is the structured census this reads instead of
# re-parsing that prose.


def _silk_item_polygon(item: dict[str, Any]) -> BaseGeometry | None:
    """One ``model["silkscreen"]`` draw's physical ink footprint.

    ``precis.pcb.silk._draw`` emits ``segments``/``width_mm`` in exactly
    the shape :func:`_copper_item_polygon`'s ``ctype == "track"`` branch
    already reads (a polyline buffered by its half-width, round caps) —
    so this is that branch, not a second implementation of "what shape is
    a stroke": a silk draw carries no ``ctype`` of its own, and adding one
    just to dispatch through the same function beats writing the buffer
    arithmetic twice."""
    return _copper_item_polygon({**item, "ctype": "track"})


#: A LEGIBILITY judgement, not a fab spec: below this cap height a human
#: reading an assembled board's silkscreen struggles to make a refdes out
#: at arm's length (a soldering-iron-and-tweezers distance, not a
#: magnifier). `capabilities.py`'s `silk_width_mm` is a PRINTABILITY floor
#: -- can the fab's silkscreen process resolve a line that thin at all --
#: and is checked separately, per-item, against the real fab-capability
#: table below; this number is never dressed up as one of that table's
#: entries (capabilities.py's own "checked directly against live JLCPCB
#: capability pages" standard does not, and cannot, apply to a readability
#: opinion this codebase is stating for itself).
SILK_LEGIBILITY_HEIGHT_MM = 0.8

#: `precis.pcb.silk._draw`'s own `role=` convention, inverted: which
#: `SilkPlacement.kind` a given `model["silkscreen"]` draw's `role` proves
#: was actually rendered. `"title"`/`"sn-text"`/`"sn-box"` (board-level
#: furniture -- title block, S/N patch) are deliberately absent: they carry
#: no per-instance census row to cross-check against (silk.py's own module
#: docstring: fiducials/title block are board-level, not part of the
#: per-instance loop `build_silk` returns a census for).
_ROLE_TO_SILK_KIND = {"outline": "courtyard", "pin1": "pin1", "refdes": "refdes"}


def check_silk_missing(
    census: Sequence[SilkPlacement], model: dict[str, Any]
) -> list[DrcFinding]:
    """An error per :class:`~precis.pcb.silk.SilkPlacement` the builder
    never rendered (``outcome == "dropped"``) -- a refdes nobody can read
    off the assembled board, a courtyard silently absent, or a pin-1 tick
    that never got drawn is exactly as real a defect as a clearance
    violation, and stayed invisible to every DRC run before this rule
    existed.

    **The cross-check this rule ALSO runs is not optional (task brief,
    verbatim: "this is the guard; do not skip it").** A dropped item
    reported here is a REPORTING channel — it repeats what
    :func:`~precis.pcb.silk.build_silk` already knew when it built the
    census, so a bug that makes the census claim success when nothing was
    actually drawn would sail straight through it. So this function
    independently reads ``model["silkscreen"]`` (the same
    ``{"top": [...], "bottom": [...]}`` shape :func:`build_silk` itself
    returns, and what a real gerber/SVG render is built from) and asserts
    that every census row claiming ``"placed"``/``"relocated"`` has a
    matching draw there — by ``(refdes, kind)`` identity, never a bare
    COUNT (this subsystem's own fixture-symmetry lesson: a count-based
    check cannot tell "the right N items" from "some other N items", and a
    census claiming success over an empty silk layer must produce a
    finding, not agree with it because the totals happen to match)."""
    findings: list[DrcFinding] = []
    for c in census:
        if c.outcome != "dropped":
            continue
        findings.append(
            DrcFinding(
                rule="silk_missing",
                severity="error",
                where=f"{c.refdes} ({c.kind}, {c.side})",
                detail=f"{c.kind} silk for {c.refdes} was not rendered: {c.reason}",
                objects=({"refdes": c.refdes, "kind": c.kind, "side": c.side},),
            )
        )

    silkscreen = model.get("silkscreen") or {}
    drawn: set[tuple[str, str]] = set()
    for side_draws in silkscreen.values():
        for draw in side_draws:
            kind = _ROLE_TO_SILK_KIND.get(str(draw.get("role") or ""))
            refdes = str(draw.get("refdes") or "")
            if kind is not None and refdes:
                drawn.add((refdes, kind))
    for c in census:
        if c.outcome not in ("placed", "relocated"):
            continue
        if (c.refdes, c.kind) in drawn:
            continue
        findings.append(
            DrcFinding(
                rule="silk_missing",
                severity="error",
                where=f"{c.refdes} ({c.kind}, {c.side})",
                detail=(
                    f"census claims {c.kind} silk for {c.refdes} was "
                    f"{c.outcome}, but no matching draw appears in "
                    "model['silkscreen'] -- the census does not describe the "
                    "board actually being shipped"
                ),
                objects=({"refdes": c.refdes, "kind": c.kind, "side": c.side},),
            )
        )
    return findings


def check_silk_printability(
    census: Sequence[SilkPlacement], capability: CapabilityRow
) -> list[DrcFinding]:
    """Two independent findings over the same census, both restricted to
    items that were actually drawn (``outcome in ("placed", "relocated")``
    -- a dropped item's silk never reaches the fab at all, and is already
    covered by :func:`check_silk_missing`, so checking its would-be width
    here would just be a second finding for the same one defect):

    - **error** when ``stroke_width_mm`` is below the fab's declared
      minimum printable silk width (``capabilities.py``'s
      ``silk_width_mm``, ``jlc_min`` tier) — a line the process cannot
      physically resolve, the exact gap this rule closes: ``silk_width_mm``
      was declared in :data:`precis.pcb.capabilities.FIELDS` and read by
      nothing (see that field's own docstring), while :mod:`precis.pcb.silk`
      carried its own ``stroke_width_mm`` default and never consulted it.
    - **warn** when a refdes label's ``height_mm`` is below
      :data:`SILK_LEGIBILITY_HEIGHT_MM` — a READABILITY judgement, not a
      fab limit (see that constant's own docstring for why the two must
      never be presented as the same kind of number).
    """
    findings: list[DrcFinding] = []
    min_width = capability.jlc_min.get("silk_width_mm")
    if min_width is not None:
        for c in census:
            if c.outcome not in ("placed", "relocated"):
                continue
            if c.stroke_width_mm >= min_width - _EPS:
                continue
            findings.append(
                DrcFinding(
                    rule="silk_printability",
                    severity="error",
                    where=f"{c.refdes} ({c.kind}, {c.side})",
                    detail=(
                        f"{c.kind} silk for {c.refdes} uses a "
                        f"{c.stroke_width_mm:.3f}mm stroke, below "
                        f"{capability.process}'s {min_width:.3f}mm minimum "
                        "printable silk width -- the fab cannot resolve a "
                        "thinner line"
                    ),
                    objects=(
                        {
                            "refdes": c.refdes,
                            "kind": c.kind,
                            "stroke_width_mm": c.stroke_width_mm,
                        },
                    ),
                    margin_mm=c.stroke_width_mm - min_width,
                )
            )

    for c in census:
        if c.outcome not in ("placed", "relocated") or c.height_mm is None:
            continue
        if c.height_mm >= SILK_LEGIBILITY_HEIGHT_MM - _EPS:
            continue
        findings.append(
            DrcFinding(
                rule="silk_printability",
                severity="warn",
                where=f"{c.refdes} ({c.kind}, {c.side})",
                detail=(
                    f"refdes label for {c.refdes} has a {c.height_mm:.3f}mm "
                    f"cap height, below the {SILK_LEGIBILITY_HEIGHT_MM:.3f}mm "
                    "legibility floor -- a human reading the assembled board "
                    "may struggle to make it out (a readability judgement, "
                    "not a fab limit)"
                ),
                objects=(
                    {"refdes": c.refdes, "kind": c.kind, "height_mm": c.height_mm},
                ),
                margin_mm=c.height_mm - SILK_LEGIBILITY_HEIGHT_MM,
            )
        )
    return findings


#: Float-noise tolerance for the 45-degree direction test — the DRC-side
#: twin of :data:`precis.pcb.realize._OCTILINEAR_EPS_MM` (kept as a local
#: constant: this module deliberately imports no realizer). A genuinely
#: octilinear segment misses by rounding only (~1e-14 at board scale);
#: an off-angle emitter misses by whole hundredths of a millimetre.
_OCTILINEAR_EPS_MM = 1e-6


def check_octilinear(model: dict[str, Any]) -> list[DrcFinding]:
    """Every drawn copper LINE segment must run at a multiple of 45
    degrees — axis-aligned or a true diagonal (|dx| == |dy|) — the
    octilinear discipline every emitter on the maze-router path in
    :mod:`precis.pcb.realize` guarantees since 2026-08-31 (user
    requirement: every wire on every layer fully 90/45).

    Scope: ``segments`` entries with ``shape == "line"`` on
    ``model["copper"]`` — the drawn wires, dogbone stubs included. Arcs
    are exempt (a corner fillet between two octilinear runs is still an
    octilinear layout, and an arc has no single direction to grade);
    pour rims are exempt (a pour follows its blockers, it is not a
    wire); silkscreen is ink, not copper. No capability field and no
    two-tier margin — a fab can image any angle, so this is a HOUSE
    style rule; severity is ``error`` so the acceptance fixtures'
    copper-class hard zero holds it board-wide."""
    findings: list[DrcFinding] = []
    for item in model.get("copper") or []:
        for seg in item.get("segments") or []:
            if seg.get("shape") != "line":
                continue
            x1, y1 = (float(v) for v in seg["start"])
            x2, y2 = (float(v) for v in seg["end"])
            dx, dy = abs(x2 - x1), abs(y2 - y1)
            if (
                dx <= _OCTILINEAR_EPS_MM
                or dy <= _OCTILINEAR_EPS_MM
                or abs(dx - dy) <= _OCTILINEAR_EPS_MM
            ):
                continue
            angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
            net, layer = item.get("net"), item.get("layer")
            findings.append(
                DrcFinding(
                    rule="octilinear",
                    severity="error",
                    where=f"track[{net}] on {layer}",
                    detail=(
                        f"segment ({x1:.3f},{y1:.3f})->({x2:.3f},{y2:.3f}) "
                        f"on {layer} runs at {angle:.1f}deg; every drawn "
                        f"wire must be a multiple of 45deg"
                    ),
                    objects=({"net": net, "layer": layer, "x": x1, "y": y1},),
                )
            )
    return findings


def check_silk_edge_clearance(
    model: dict[str, Any],
    capability: CapabilityRow,
    *,
    outline: list[list[float]] | None,
) -> list[DrcFinding]:
    """Silk-to-board-edge clearance — the same two-tier bar
    :func:`check_board_edge_clearance` applies to copper, applied here to
    silkscreen ink, off the same ``board_edge_clearance_vcut_mm`` field
    and the same :func:`_two_tier`/:func:`_margin_detail` machinery.

    ``precis.pcb.silk.build_silk`` already keeps a caller-supplied
    ``outline`` clear of this margin at CANDIDATE time
    (``silk._silk_edge_margin_mm``) for the refdes label and the pin-1
    dot — but only for those two, and only when it was given an outline
    at all. This rule is the independent DRC-time check of the same fact,
    over EVERY item in ``model["silkscreen"]``: the courtyard ring and the
    corner tick, which the generator deliberately never relocates (silk.py's
    own module docstring — their footprint is the part's own, not a
    candidate choice), a render built with no ``outline`` passed to
    :func:`~precis.pcb.silk.build_silk` at all, and a genuine regression in
    the generator itself. A candidate that this module's own generation
    side already rejected should never fire this rule; one that does is
    the two disagreeing about the one board-edge number the module
    docstring's edge-margin note says they must not."""
    field = "board_edge_clearance_vcut_mm"
    jlc_min = capability.jlc_min[field]
    house = capability.house_default.get(field)
    if jlc_min is None or not outline or len(outline) < 3:
        return []
    ring_pts = [(float(p[0]), float(p[1])) for p in outline]
    if ring_pts[0] != ring_pts[-1]:
        ring_pts.append(ring_pts[0])
    boundary = LineString(ring_pts)
    findings: list[DrcFinding] = []
    for side, draws in (model.get("silkscreen") or {}).items():
        for draw in draws:
            geom = _silk_item_polygon(draw)
            if geom is None or geom.is_empty:
                continue
            gap = boundary.distance(geom)
            result = _two_tier(gap, jlc_min, house)
            if result is None:
                continue
            severity, margin = result
            role = str(draw.get("role") or "")
            refdes = str(draw.get("refdes") or "")
            findings.append(
                DrcFinding(
                    rule="silk_edge_clearance",
                    severity=severity,
                    where=f"silk {role}[{refdes}] on {side}",
                    detail=_margin_detail(
                        "silk-to-board-edge clearance",
                        gap,
                        capability,
                        field,
                        severity,
                        margin,
                    ),
                    objects=({"role": role, "refdes": refdes, "side": side},),
                    margin_mm=margin,
                )
            )
    return findings


# ── orchestrator ────────────────────────────────────────────────────────


def check_connectivity(model: dict[str, Any]) -> list[DrcFinding]:
    """Every net's copper must be ONE connected component.

    The rule that makes "zero DRC errors" mean something. Every other rule
    here checks that copper is not too close, too thin, or too near an
    edge — all of which a board with a severed net passes. See
    :mod:`precis.pcb.connectivity` for the two shipped defects this catches
    and why they were invisible to the rest of this module.
    """
    # Lazy, to break a genuine cycle rather than paper over one: the
    # connectivity module deliberately reuses THIS module's primitive
    # alphabet and gap arithmetic (two notions of "touching" between
    # clearance and connectivity would be a defect generator of its own),
    # so it imports drc and drc cannot import it at module scope. Same
    # idiom, same reason, as handlers/_paper_search.py.
    from precis.pcb.connectivity import net_islands

    return [
        DrcFinding(
            rule="connectivity",
            severity="error",
            where=f"net {island.net}",
            detail=(
                f"net {island.net} copper is in {island.components} disconnected "
                "pieces; a net's copper must be one connected component. "
                "Witnesses (one point per piece): "
                + "; ".join(
                    f"({x:.3f},{y:.3f}) on {layer}"
                    for x, y, layer in island.witnesses[:6]
                )
            ),
            objects=({"net": island.net, "components": island.components},),
        )
        for island in net_islands(model)
    ]


def check_unrouted(
    unrouted: list[dict[str, Any]] | None,
) -> list[DrcFinding]:
    """An unrouted connection is a DRC error, not a side channel.

    It was reported only in ``RealizeResult.unrouted`` and a ``failed`` row
    while ``view='drc'`` said zero errors — so "DRC clean" did not mean
    "board is finished". That is the same trap as reaching zero errors by
    routing nothing, relocated: the number a reader trusts stays silent
    about the thing that matters.

    Each entry needs a ``net``; ``from``/``to`` name the two endpoints when
    the caller knows them (the realizer does — it works per connection) and
    are omitted when it only knows the net (the DRC view reads per-net
    ``pcb_routes`` status). The finding says which it got rather than
    printing ``?`` for a fact nobody claimed to have.
    """
    out: list[DrcFinding] = []
    for item in unrouted or []:
        net = str(item.get("net", "?"))
        a, b = item.get("from"), item.get("to")
        what = f"connection {a} -> {b}" if a and b else "at least one connection"
        note = f" ({item['note']})" if item.get("note") else ""
        out.append(
            DrcFinding(
                rule="unrouted",
                severity="error",
                where=f"net {net}",
                detail=(
                    f"{what} on net {net} has no route{note}; the board is not finished"
                ),
                objects=(dict(item),),
            )
        )
    return out


def run_geometric_drc(
    model: dict[str, Any],
    *,
    capability: CapabilityRow,
    outline: list[list[float]] | None = None,
    courtyards: list[Courtyard] | None = None,
    panel_type: str | None = None,
    net_rules: dict[str, NetRules] | None = None,
    unrouted: list[dict[str, Any]] | None = None,
    census: tuple[SilkPlacement, ...] | None = None,
) -> list[DrcFinding]:
    """Every geometric DRC rule over one realized board, in one call — what
    ``view='drc'`` and the ``netlist_drc_clean`` gate evaluator both run.
    ``net_rules`` (net name -> resolved :class:`~precis.pcb.rules.NetRules`)
    threads the per-net clearance override into :func:`check_clearance`
    only — the other rules stay capability-only (module docstring: they
    check the fab's own hard limits, not an authored class preference).

    ``unrouted`` is the realizer's list of connections it could not route.
    It is an argument rather than something derived from ``model`` because
    a model cannot distinguish "not routed" from "not attempted" — absence
    of copper is not evidence of failure, and guessing here would either
    invent errors or hide them.

    ``census`` is :func:`precis.pcb.silk.build_silk`'s own per-item
    placement record (:class:`~precis.pcb.silk.SilkPlacement`) — an
    argument for the exact same reason ``unrouted`` is one: this module
    cannot derive "was label X actually rendered, and if not, why" from
    ``model["silkscreen"]`` alone, since an absent draw there is
    indistinguishable from "never attempted" versus "dropped for a stated
    reason" without the record :func:`~precis.pcb.silk.build_silk` already
    produced while deciding. ``census=None`` (the default) keeps every
    existing caller — none of which built a census before this rule
    existed — DRC-clean on this axis exactly as before, the identical
    "silent about a thing this module was never told" contract
    ``unrouted=None`` already has.
    """
    findings: list[DrcFinding] = []
    findings += check_clearance(model, capability, net_rules=net_rules)
    findings += check_trace_width(model, capability)
    findings += check_annular_ring(model, capability)
    findings += check_npth_clearance(model, capability)
    findings += check_via_pad_keepout(model, capability)
    findings += check_via_via_keepout(model, capability)
    findings += check_board_edge_clearance(
        model, capability, outline=outline, panel_type=panel_type
    )
    findings += check_silk_edge_clearance(model, capability, outline=outline)
    findings += check_octilinear(model)
    findings += check_outline_containment(model, outline=outline, courtyards=courtyards)
    findings += check_connectivity(model)
    findings += check_unrouted(unrouted)
    if courtyards:
        findings += check_courtyard_overlap(courtyards)
    findings += check_silk_missing(census or (), model)
    findings += check_silk_printability(census or (), capability)
    return findings


__all__ = [
    "DEFAULT_COURTYARD_RADIUS_MM",
    "SILK_LEGIBILITY_HEIGHT_MM",
    "DrcFinding",
    "check_annular_ring",
    "check_board_edge_clearance",
    "check_clearance",
    "check_connectivity",
    "check_courtyard_overlap",
    "check_npth_clearance",
    "check_octilinear",
    "check_outline_containment",
    "check_silk_edge_clearance",
    "check_silk_missing",
    "check_silk_printability",
    "check_trace_width",
    "check_unrouted",
    "check_via_pad_keepout",
    "check_via_via_keepout",
    "clearance_pairs_indexed",
    "clearance_violations_naive",
    "process_for_stackup",
    "run_geometric_drc",
]

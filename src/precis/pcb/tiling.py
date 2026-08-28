"""Copper as a TILING, not a flood (pcb-guided-place-route Slice 5).

**One primitive, from the decisions log verbatim: a net owns a region on a
layer.** Trace, plane, pour and keepout stop being different objects — a
:class:`Tile` is the same thing whether it came from a hair-thin signal
trace or a ground pour that covers most of a layer; only its geometry and
its net/layer differ.

**Do NOT optimize the tiling — DERIVE it.** The backlog is explicit: the
tiling problem is continuous and non-convex, so this module never scores or
searches. :func:`grow_tiles` is a deterministic function of (skeleton,
per-net expansion weight, clearance): every net's L2 skeleton grows
simultaneously — a weighted-Voronoi / multi-source expansion, implemented
here as synchronized incremental buffering rather than a grid distance
transform (this repo has no core ``scipy`` dependency to lean on for
``distance_transform_edt``; buffering is the ``shapely``-native
equivalent and stays exact rather than grid-quantized) — until each net's
class-driven width cap binds or a neighbour's clearance binds first.
**Ground/plane nets have no cap and the most skeleton, so they fill
whatever the capped nets didn't claim** — implemented as a final pass, not
a participant in the growth loop, since "fill the remainder" is exactly
what an uncapped multi-source expansion converges to anyway and computing
it directly is exact instead of asymptotic.

Per-net expansion weight comes from the connection's
:class:`precis.pcb.objectives.ObjectiveVector` — see
:func:`expansion_rate_from_objective` — never a bespoke width-policy enum
(objectives.py's module docstring: "it absorbs the width-policy enum too").

**Two checks are mandatory, not optional** (decisions log, verbatim):
:func:`find_acute_angles` / :func:`cull_slivers` (acid traps — a sliver
etches away or, worse, stays as a resist bridge) and
:func:`find_floating_pieces` / :func:`drop_floating_pieces` (every tile
must connect to its own net, or the "copper" is worse than no copper —
unstitched pours become coupling paths). The floating-tile check is the
geometric sibling of :func:`precis.pcb.ir.plane_connectivity`'s stitch-via
count, but it is a fresh implementation, not a shared one: ``ir.py``'s
version is a topological proxy over the via/segment graph (no geometry at
all, by design — see its own module docstring), while this one operates on
real polygon geometry, which ``ir.py`` explicitly says is a later module's
job. There was nothing geometric there yet to reuse.

**Widened traces need a neck-down at pads** (:func:`neck_down_at_pads`) —
a wide trace run straight into a pad heat-sinks it during reflow and
tombstones the part; forcing the trace back to the class-minimum width
within a short zone around every pad prevents it, and is cheap to do as a
final geometric pass over an already-grown tile.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import acos, degrees, hypot

# shapely ships no py.typed marker in this environment (mypy wants the
# separate `types-shapely` stub package, a pyproject/dev-dep decision
# outside this module's remit) -- silence the import-untyped noise here
# rather than let it mask real errors elsewhere in the file.
from shapely.geometry import (  # type: ignore[import-untyped]
    MultiPolygon,
    Point,
    Polygon,
)
from shapely.geometry.base import BaseGeometry  # type: ignore[import-untyped]
from shapely.ops import unary_union  # type: ignore[import-untyped]

from precis.pcb.objectives import ObjectiveVector

#: Growth-comparison tolerance — shapely polygon areas carry float noise
#: from repeated buffer ops; without this, "did it grow?" flickers on
#: noise and the loop never converges.
_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class NetTilingSpec:
    """One net's growth spec for :func:`grow_tiles` — everything the
    simultaneous expansion needs to know about a net."""

    net_id: int
    #: L2 topology's per-net skeleton (a LineString/MultiLineString the
    #: caller derives from the sketch — this module takes it as given,
    #: since sketch.py doesn't exist yet at this slice; see the module
    #: docstring's scope note).
    skeleton: BaseGeometry
    #: Class-minimum trace half-width (precis.pcb.capabilities house
    #: default) — the width a net never shrinks below, growth or not.
    min_half_width_mm: float
    #: mm of half-width grown per :func:`grow_tiles` step; 0 means this
    #: net stays pinned at ``min_half_width_mm`` (a fixed/controlled-
    #: impedance net — see :func:`expansion_rate_from_objective`).
    expansion_rate: float = 0.0
    #: Half-width cap; ``None`` = uncapped. Every ordinary signal/power
    #: net has a cap (a via-array power net still isn't infinite); only a
    #: ``is_plane`` net goes uncapped in practice.
    max_half_width_mm: float | None = None
    #: True for the ground/fill net that absorbs whatever is left over
    #: (module docstring) instead of participating in the growth loop.
    is_plane: bool = False


@dataclass(frozen=True, slots=True)
class Tile:
    """The one primitive: a net's region on a layer."""

    net_id: int
    layer: int
    geom: BaseGeometry


def expansion_rate_from_objective(obj: ObjectiveVector) -> float:
    """Per-net growth rate above the class-minimum width, derived from the
    objective vector (backlog: "low capacitance => narrow, low resistance
    => wide"). A controlled-impedance / matched-length net (the ``rf``/
    ``diffpair`` presets in objectives.py: high ``low_capacitance``, low
    ``low_resistance``) lands at rate 0 from the same arithmetic — "fixed
    width for controlled impedance" is a consequence of this formula, not
    a separate flag to maintain."""
    return max(0.0, obj.low_resistance - obj.low_capacitance)


# ── growth: the weighted multi-source expansion ──────────────────────────
def grow_tiles(
    specs: list[NetTilingSpec],
    board_outline: BaseGeometry,
    *,
    clearance_mm: float,
    step_mm: float = 0.05,
    max_iterations: int = 400,
) -> dict[int, BaseGeometry]:
    """Derive every net's tile geometry (module docstring: DERIVE, never
    optimize). Capped nets grow simultaneously in lockstep buffer steps,
    clipped to the board and to ``clearance_mm`` outside every other net's
    *current* tile (so two nets growing toward each other both stop with
    exactly ``clearance_mm`` between them, not one net eating the gap).
    Plane nets are assigned afterward: whatever the board doesn't already
    belong to, once every capped net has stopped growing.
    """
    capped = [s for s in specs if not s.is_plane]
    planes = [s for s in specs if s.is_plane]

    tiles: dict[int, BaseGeometry] = {}
    half_width: dict[int, float] = {}
    spec_by_id = {s.net_id: s for s in capped}
    for s in capped:
        tiles[s.net_id] = s.skeleton.buffer(s.min_half_width_mm).intersection(
            board_outline
        )
        half_width[s.net_id] = s.min_half_width_mm

    active = {s.net_id for s in capped if s.expansion_rate > 0}

    # Gauss-Seidel, not Jacobi: each net's growth this round sees every
    # OTHER net's growth already committed earlier in the SAME round, not
    # a stale snapshot from before the round. That matters here, not just
    # for speed — updating every net simultaneously off a common stale
    # snapshot is an unstable fixed point for two nets growing toward each
    # other (both "see" the same gap and both claim it, so the pair
    # oscillates around the correct clearance instead of converging to
    # it); processing sequentially and committing immediately removes the
    # oscillation because the second net in a round already respects the
    # first net's fresh boundary.
    for _ in range(max_iterations):
        if not active:
            break
        still_active: set[int] = set()
        for net_id in sorted(active):
            spec = spec_by_id[net_id]
            new_half = half_width[net_id] + step_mm * spec.expansion_rate
            if spec.max_half_width_mm is not None:
                new_half = min(new_half, spec.max_half_width_mm)
            grown = spec.skeleton.buffer(new_half).intersection(board_outline)
            others = unary_union(
                [g.buffer(clearance_mm) for oid, g in tiles.items() if oid != net_id]
            )
            if not others.is_empty:
                grown = grown.difference(others)
            if not grown.is_empty and grown.area > tiles[net_id].area + _EPS:
                tiles[net_id] = grown
                half_width[net_id] = new_half
                if spec.max_half_width_mm is None or new_half < spec.max_half_width_mm:
                    still_active.add(net_id)
        active = still_active

    if planes:
        claimed = unary_union([g.buffer(clearance_mm) for g in tiles.values()])
        remainder = (
            board_outline.difference(claimed) if not claimed.is_empty else board_outline
        )
        for spec in planes:
            tiles[spec.net_id] = remainder

    return tiles


# ── mandatory check 1: sliver / acute-angle cull (acid traps) ───────────
def find_acute_angles(
    geom: BaseGeometry, *, min_angle_deg: float = 20.0
) -> list[tuple[tuple[float, float], float]]:
    """Every exterior vertex whose interior angle is below
    ``min_angle_deg`` — an acid trap (etchant pools in the point during
    manufacture, over-etching it; if it survives, it's a stress
    concentrator). A **finding**, not a fix — :func:`cull_slivers` is the
    fix; this is what a DRC digest quotes to name where."""
    polys: list[Polygon] = _pieces(geom)
    out: list[tuple[tuple[float, float], float]] = []
    for poly in polys:
        coords = list(poly.exterior.coords)[:-1]
        n = len(coords)
        for i in range(n):
            a, b, c = coords[(i - 1) % n], coords[i], coords[(i + 1) % n]
            v1 = (a[0] - b[0], a[1] - b[1])
            v2 = (c[0] - b[0], c[1] - b[1])
            len1, len2 = hypot(*v1), hypot(*v2)
            if len1 < _EPS or len2 < _EPS:
                continue
            cos_angle = max(
                -1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (len1 * len2))
            )
            angle = degrees(acos(cos_angle))
            if angle < min_angle_deg:
                out.append((b, angle))
    return out


def cull_slivers(geom: BaseGeometry, *, min_width_mm: float) -> BaseGeometry:
    """Remove any feature narrower than ``min_width_mm`` — morphological
    opening (erode by half the width, then dilate back). This is not a
    heuristic stand-in for angle detection: an erosion of radius r
    physically closes off (removes) any spike or channel narrower than
    2r, which is exactly the failure mode a sliver/acid-trap represents —
    the geometric operation matches the physical one (an over-etch thins
    a narrow feature to nothing) rather than approximating it."""
    r = min_width_mm / 2.0
    return geom.buffer(-r).buffer(r)


# ── mandatory check 2: every tile must connect to its own net ───────────
def _pieces(geom: BaseGeometry) -> list[Polygon]:
    """Every polygon connected-component of ``geom`` — the geometric
    equivalent of a graph's connected-components list."""
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    if geom.geom_type == "GeometryCollection":
        return [g for g in geom.geoms if isinstance(g, Polygon) and not g.is_empty]
    return []


def find_floating_pieces(
    tile_geom: BaseGeometry, skeleton: BaseGeometry
) -> list[Polygon]:
    """Connected components of ``tile_geom`` that do NOT touch this net's
    own skeleton — floating copper, worse than no copper (backlog:
    unstitched pours become coupling paths). Empty result = every piece is
    genuinely connected to the net."""
    return [p for p in _pieces(tile_geom) if not p.intersects(skeleton)]


def drop_floating_pieces(
    tile_geom: BaseGeometry, skeleton: BaseGeometry
) -> BaseGeometry:
    """The fix half of the mandatory check: keep only the pieces that DO
    touch the net's own skeleton, unioned back into one geometry."""
    kept = [p for p in _pieces(tile_geom) if p.intersects(skeleton)]
    if not kept:
        return Polygon()  # nothing survives — an empty tile, not an error
    return unary_union(kept)


# ── widened traces need a neck-down at pads ──────────────────────────────
def neck_down_at_pads(
    tile_geom: BaseGeometry,
    skeleton: BaseGeometry,
    pad_centers: list[tuple[float, float]],
    *,
    min_half_width_mm: float,
    neck_length_mm: float = 0.5,
) -> BaseGeometry:
    """Force a widened tile back down to ``min_half_width_mm`` within
    ``neck_length_mm`` of every pad center (backlog: "widened traces need
    thermal relief or a neck-down at pads, or they heat-sink the pad
    during reflow and cause tombstoning"). Outside every neck zone the
    tile keeps whatever width :func:`grow_tiles` gave it; inside a zone it
    is replaced by a ``min_half_width_mm`` buffer of the skeleton, so the
    connection to the pad is always through the thin neck, never the wide
    run directly."""
    if not pad_centers:
        return tile_geom
    neck_zones = unary_union([Point(p).buffer(neck_length_mm) for p in pad_centers])
    necked = skeleton.intersection(neck_zones).buffer(min_half_width_mm)
    outside = tile_geom.difference(neck_zones)
    return unary_union(
        [outside, necked.intersection(tile_geom.buffer(min_half_width_mm))]
    )


__all__ = [
    "NetTilingSpec",
    "Tile",
    "cull_slivers",
    "drop_floating_pieces",
    "expansion_rate_from_objective",
    "find_acute_angles",
    "find_floating_pieces",
    "grow_tiles",
    "neck_down_at_pads",
]

"""Footprint escape-routing precompute (pcb-guided-place-route Slice 5).

**The key property, from the backlog's decisions log (2026-08-27), verbatim
in spirit: escape routing is footprint-INTRINSIC and PRECOMPUTED.** Pad
gaps, shell depth and per-gap escape capacity follow from the footprint
alone — not the board, not placement, not rotation — so they are derived
once per footprint from :func:`precis.pcb.easyeda.parse_component`'s
canonical pads and cached in ``part_footprints.escape``
(:meth:`precis.store._pcb_ops.PcbMixin.part_footprint_get`/``_put``).
Available at L0/L1, before any placement exists. A placement move never
touches this data; an instance rotation only *permutes* which physical
corner holds which pad number, it does not change the multiset of shells
or gap capacities (tested in ``tests/test_pcb_escape.py``).

**The payoff that matters more than the escape routing itself:**
:func:`required_layers` derives how many routing layers a package needs
from escape *demand*, instead of the router asserting a layer count up
front — this is what makes emergent layer roles (backlog: "layer ROLE is a
decision variable, not a constant") workable at all: the stackup search
space is scoped to what a part actually needs, not padded to a worst case.

**Shell decomposition** (:func:`compute_shells`) is convex-layer ("onion")
peeling of pad centers: the outer boundary — every pad on the convex hull,
INCLUDING pads that sit collinear along a hull edge, not just the extreme
corner vertices — is shell 0; peel it off and repeat on what remains. A
perimeter package (SOIC/QFP: every pad already lies on one of the package's
four edges) has nothing left inside after one peel, so every pad lands on
shell 0, matching the backlog's stated invariant. An area-array package
(BGA/LGA) peels down through shells 0..k; a pad on shell k must cross every
shell 0..k-1 to reach the board edge — exactly the property
:func:`required_layers` turns into a layer count.

**Gap capacity** (:func:`compute_gaps`, :func:`gap_capacity`) follows the
backlog formula verbatim: ``capacity = floor((pitch - pad_extent -
2*clearance) / (trace_width + clearance))``, floored at 0. Trace width and
clearance minimums come from :mod:`precis.pcb.capabilities`'
``house_default`` tier (never ``jlc_min`` — the deliberate margin above the
fab's own minimum is the number that should bind here), and some
capability fields are genuinely ``None`` (JLC publishes no figure for that
process), so :func:`_trace_and_clearance` raises rather than silently
treating an absent minimum as ``0.0``.

**Adjacency** — "between adjacent pads" needs a definition this module
supplies: two pads are adjacent iff their center distance is within 5% of
the footprint's own minimum pairwise pitch. This is a v1 approximation
tuned for the evenly-pitched grids/strips every real JLC-assemblable
footprint uses; a footprint with genuinely irregular, non-uniform pad
spacing would get an approximate (possibly incomplete) gap graph — noted
honestly rather than pretending full generality.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from precis.pcb.capabilities import CapabilityRow, capability_for

#: Cross-product / collinearity tolerance for the hull peel — mm-scale
#: coordinates, rounded to 4 decimals by easyeda.parse_component, so this
#: only needs to absorb float round-off, not real geometric slop.
_EPS = 1e-6

#: Two pads count as "adjacent" (share a gap) when their center distance is
#: within this factor of the footprint's own minimum pairwise pitch — see
#: the module docstring's "Adjacency" section for why 5%.
_ADJACENCY_SLOP = 1.05


@dataclass(frozen=True, slots=True)
class EscapeGap:
    """One free channel between two adjacent pads that a routed strand may
    pass through to escape outward. ``capacity`` is strands-that-fit on ONE
    layer — the same quantity :mod:`precis.pcb.ir`'s L4
    ``seg_gap_capacity`` estimates from placement geometry, computed here
    instead from the footprint alone, before any placement exists."""

    pad_a: str
    pad_b: str
    pitch_mm: float
    width_mm: float
    capacity: int


@dataclass(frozen=True, slots=True)
class EscapeGraph:
    """The per-footprint escape graph — computed ONCE by
    :func:`compute_escape_graph`, cached verbatim (via
    :func:`escape_graph_to_dict`) in ``part_footprints.escape``."""

    #: pad number -> shell index (0 = outermost).
    shells: dict[str, int]
    n_shells: int
    gaps: list[EscapeGap]
    #: shell index -> summed capacity of gaps whose both pads sit on that
    #: shell (the escape channel a pad on that ring, or any pad further
    #: inside, must thread through to get past this ring).
    per_shell_capacity: dict[int, int]
    #: how many routing layers this package needs, derived from escape
    #: demand outrunning per-shell capacity on a single layer.
    required_layers: int


# ── shell decomposition (convex-layer / onion peel) ─────────────────────
def _cross(
    o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Andrew's monotone chain — the minimal (extreme-vertex-only) hull.
    Collinear points along an edge are dropped here on purpose;
    :func:`_on_hull_boundary` adds them back so a perimeter package's
    edge-strip pads all land on shell 0, not just the four corners."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def build(seq: list[tuple[float, float]]) -> list[tuple[float, float]]:
        hull: list[tuple[float, float]] = []
        for p in seq:
            while len(hull) >= 2 and _cross(hull[-2], hull[-1], p) <= _EPS:
                hull.pop()
            hull.append(p)
        return hull

    lower = build(pts)
    upper = build(list(reversed(pts)))
    return lower[:-1] + upper[:-1]


def _on_hull_boundary(
    pt: tuple[float, float], hull: list[tuple[float, float]], *, eps: float = 1e-6
) -> bool:
    """True if ``pt`` lies on one of ``hull``'s edges (collinear + within
    the edge's span) — the "add collinear points back" half of the peel."""
    n = len(hull)
    if n < 2:
        return True
    for i in range(n):
        a, b = hull[i], hull[(i + 1) % n]
        seg_len = math.hypot(b[0] - a[0], b[1] - a[1])
        if seg_len < eps:
            on_line = abs(pt[0] - a[0]) <= eps and abs(pt[1] - a[1]) <= eps
        else:
            on_line = abs(_cross(a, b, pt)) / seg_len <= eps
        if (
            on_line
            and min(a[0], b[0]) - eps <= pt[0] <= max(a[0], b[0]) + eps
            and min(a[1], b[1]) - eps <= pt[1] <= max(a[1], b[1]) + eps
        ):
            return True
    return False


def compute_shells(pads: list[dict[str, Any]]) -> dict[str, int]:
    """Onion-peel pad centers into shells: shell 0 is every pad on the
    footprint's outer boundary (see module docstring); peeling that off and
    repeating gives shell 1, 2, ... for an area-array package's interior
    rings. Returns ``{pad_number: shell}`` for every pad."""
    positions: dict[str, tuple[float, float]] = {
        str(pad["number"]): (float(pad["x"]), float(pad["y"])) for pad in pads
    }
    remaining = dict(positions)
    shell_of: dict[str, int] = {}
    shell = 0
    while remaining:
        by_pos: dict[tuple[float, float], list[str]] = {}
        for number, pos in remaining.items():
            by_pos.setdefault(pos, []).append(number)
        unique_pts = sorted(by_pos)
        if len(unique_pts) <= 2:
            for pt in unique_pts:
                for number in by_pos[pt]:
                    shell_of[number] = shell
            break
        hull = _convex_hull(unique_pts)
        boundary_pts = {pt for pt in unique_pts if _on_hull_boundary(pt, hull)}
        if not boundary_pts:
            # Degenerate safety net only — every real hull has >=1 vertex,
            # so this is unreachable in practice; never leave a pad
            # unassigned rather than trust that unreachability blindly.
            boundary_pts = set(unique_pts)
        for pt in boundary_pts:
            for number in by_pos[pt]:
                shell_of[number] = shell
                del remaining[number]
        shell += 1
    return shell_of


# ── gap capacity ─────────────────────────────────────────────────────────
def gap_capacity(
    pitch_mm: float, pad_extent_mm: float, trace_width_mm: float, clearance_mm: float
) -> int:
    """Strands that fit through one gap, per the backlog formula verbatim:
    ``(pitch - pad_extent - 2*clearance) / (trace_width + clearance)``,
    floored at 0 — a gap too tight for even one strand is not an error,
    just a 0-capacity gap (every net through it must find another path or
    another layer)."""
    width = pitch_mm - pad_extent_mm - 2 * clearance_mm
    if width <= 0:
        return 0
    return max(0, math.floor(width / (trace_width_mm + clearance_mm)))


def _trace_and_clearance(capability: CapabilityRow) -> tuple[float, float]:
    """The (trace_width, clearance) pair, house_default tier — never
    ``jlc_min`` (module docstring). Raises rather than silently treating a
    ``None`` field (JLC publishes nothing for this process) as 0.0, which
    would understate every gap's cost and overstate its capacity."""
    trace = capability.house_default.get("trace_width_mm")
    clearance = capability.house_default.get("trace_spacing_mm")
    if trace is None or clearance is None:
        raise ValueError(
            f"{capability.process}: house_default is missing trace_width_mm/"
            "trace_spacing_mm (JLC publishes no figure for this process) — "
            "escape capacity has no defined value without a minimum "
            "trace/clearance to divide by"
        )
    return trace, clearance


def _pad_extent_along(pad: dict[str, Any], ux: float, uy: float) -> float:
    """The pad's footprint extent projected onto the unit direction
    ``(ux, uy)`` connecting it to its neighbour — an axis-aligned
    approximation from the pad's own ``w``/``h`` (pad ``rot`` is ignored:
    every fixture and every JLC-assemblable footprint we've seen escapes
    on an axis-aligned grid or strip, so this is the honest input we have
    without re-deriving a rotated bounding box)."""
    return abs(ux) * float(pad.get("w") or 0.0) + abs(uy) * float(pad.get("h") or 0.0)


def _gap_between(
    pad_a: dict[str, Any],
    pad_b: dict[str, Any],
    trace_width_mm: float,
    clearance_mm: float,
) -> EscapeGap | None:
    dx = float(pad_b["x"]) - float(pad_a["x"])
    dy = float(pad_b["y"]) - float(pad_a["y"])
    pitch = math.hypot(dx, dy)
    if pitch <= _EPS:
        return None
    ux, uy = dx / pitch, dy / pitch
    extent = (_pad_extent_along(pad_a, ux, uy) + _pad_extent_along(pad_b, ux, uy)) / 2.0
    capacity = gap_capacity(pitch, extent, trace_width_mm, clearance_mm)
    width = max(0.0, pitch - extent - 2 * clearance_mm)
    return EscapeGap(
        pad_a=str(pad_a["number"]),
        pad_b=str(pad_b["number"]),
        pitch_mm=round(pitch, 6),
        width_mm=round(width, 6),
        capacity=capacity,
    )


def compute_gaps(
    pads: list[dict[str, Any]], capability: CapabilityRow | None = None
) -> list[EscapeGap]:
    """Every adjacent-pad gap (module docstring's "Adjacency" section) with
    its escape capacity. ``capability`` defaults to the 4-layer house
    process (:data:`precis.pcb.DEFAULT_STACKUP`'s process)."""
    if capability is None:
        capability = capability_for("4layer")
    trace_width_mm, clearance_mm = _trace_and_clearance(capability)
    n = len(pads)
    if n < 2:
        return []

    def _dist(i: int, j: int) -> float:
        return math.hypot(
            float(pads[j]["x"]) - float(pads[i]["x"]),
            float(pads[j]["y"]) - float(pads[i]["y"]),
        )

    all_dists = [
        d for i in range(n) for j in range(i + 1, n) if (d := _dist(i, j)) > _EPS
    ]
    if not all_dists:
        return []
    threshold = min(all_dists) * _ADJACENCY_SLOP

    gaps: list[EscapeGap] = []
    for i in range(n):
        for j in range(i + 1, n):
            d = _dist(i, j)
            if _EPS < d <= threshold:
                gap = _gap_between(pads[i], pads[j], trace_width_mm, clearance_mm)
                if gap is not None:
                    gaps.append(gap)
    return gaps


# ── required_layers: the payoff (module docstring) ──────────────────────
def _capacity_per_shell(
    shells: dict[str, int], gaps: list[EscapeGap]
) -> dict[int, int]:
    capacity: dict[int, int] = {}
    for gap in gaps:
        sa, sb = shells.get(gap.pad_a), shells.get(gap.pad_b)
        if sa is not None and sa == sb:
            capacity[sa] = capacity.get(sa, 0) + gap.capacity
    return capacity


def _layers_for_escape(
    shells: dict[str, int], capacity_per_shell: dict[int, int]
) -> int:
    """A pad on shell k must cross every shell 0..k-1 to reach the board
    edge (module docstring); so at ring m, the traffic that must pass
    through ring m's own gap capacity is every pad on shell m or deeper.
    Where a ring supplies zero gap capacity (e.g. a single-pad shell — no
    gaps possible with only one pad) it imposes no constraint rather than
    a division-by-zero block: nothing obstructs a lone pad's own egress."""
    if not shells:
        return 1
    max_shell = max(shells.values())
    count_per_shell: dict[int, int] = {}
    for s in shells.values():
        count_per_shell[s] = count_per_shell.get(s, 0) + 1
    layers = 1
    for ring in range(max_shell, -1, -1):
        capacity = capacity_per_shell.get(ring, 0)
        if capacity <= 0:
            continue
        demand = sum(count_per_shell.get(s, 0) for s in range(ring, max_shell + 1))
        layers = max(layers, math.ceil(demand / capacity))
    return layers


def compute_escape_graph(
    pads: list[dict[str, Any]], *, capability: CapabilityRow | None = None
) -> EscapeGraph:
    """The full per-footprint escape graph — shells, gaps, per-shell
    capacity, and the derived :attr:`EscapeGraph.required_layers`. This is
    the one function a footprint-ingestion caller needs; cache its
    :func:`escape_graph_to_dict` output in ``part_footprints.escape``."""
    if capability is None:
        capability = capability_for("4layer")
    shells = compute_shells(pads)
    gaps = compute_gaps(pads, capability)
    n_shells = (max(shells.values()) + 1) if shells else 0
    per_shell_capacity = _capacity_per_shell(shells, gaps)
    layers = _layers_for_escape(shells, per_shell_capacity)
    return EscapeGraph(
        shells=shells,
        n_shells=n_shells,
        gaps=gaps,
        per_shell_capacity=per_shell_capacity,
        required_layers=layers,
    )


def required_layers(
    pads: list[dict[str, Any]], *, capability: CapabilityRow | None = None
) -> int:
    """Convenience wrapper: how many routing layers this footprint needs,
    derived from escape demand (module docstring — "the payoff"). Equal to
    ``compute_escape_graph(pads, capability=capability).required_layers``."""
    return compute_escape_graph(pads, capability=capability).required_layers


# ── (de)serialization for the part_footprints.escape jsonb cache ────────
def escape_graph_to_dict(graph: EscapeGraph) -> dict[str, Any]:
    """Plain-dict form for ``part_footprint_put(..., {"escape": ...})`` —
    jsonb object keys must be strings, so ``per_shell_capacity`` keys are
    stringified here and restored by :func:`escape_graph_from_dict`."""
    return {
        "shells": dict(graph.shells),
        "n_shells": graph.n_shells,
        "gaps": [
            {
                "pad_a": g.pad_a,
                "pad_b": g.pad_b,
                "pitch_mm": g.pitch_mm,
                "width_mm": g.width_mm,
                "capacity": g.capacity,
            }
            for g in graph.gaps
        ],
        "per_shell_capacity": {str(k): v for k, v in graph.per_shell_capacity.items()},
        "required_layers": graph.required_layers,
    }


def escape_graph_from_dict(data: dict[str, Any]) -> EscapeGraph:
    """The inverse of :func:`escape_graph_to_dict` — reloads a cached
    ``part_footprints.escape`` value back into an :class:`EscapeGraph`."""
    return EscapeGraph(
        shells={str(k): int(v) for k, v in data["shells"].items()},
        n_shells=int(data["n_shells"]),
        gaps=[EscapeGap(**g) for g in data["gaps"]],
        per_shell_capacity={
            int(k): int(v) for k, v in data["per_shell_capacity"].items()
        },
        required_layers=int(data["required_layers"]),
    )


__all__ = [
    "EscapeGap",
    "EscapeGraph",
    "compute_escape_graph",
    "compute_gaps",
    "compute_shells",
    "escape_graph_from_dict",
    "escape_graph_to_dict",
    "gap_capacity",
    "required_layers",
]

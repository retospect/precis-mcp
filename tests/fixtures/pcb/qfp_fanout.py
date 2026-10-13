"""Synthetic QFP fanout family — the mechanism the EWOD dogfood-6 wall is
made of, as a parametric in-memory board (no store, no DB).

One square package with ``4 * pins_per_side`` pads on a ring (long axis
radial, like the real PQFP-80's 2.0 x 0.5 mm pads at 0.8 mm pitch), every
pad swappable with every other, and one through-hole "source" per net
(a plated 0.45/0.15 mm pad — geometrically a via — standing in for the
EWOD plaza vias or a real board's connector pins). Sources sit either on
a ring OUTSIDE the package (a connector fan-in) or on a GRID centred on
it (the EWOD case: plazas under the array, the driver on the other
side). The initial pad assignment is deliberately random; the pin-swap
warm start is what makes the family routable, exactly as on the
dogfood board (``docs/backlog/pcb-global-plan-router.md``,
``docs/backlog/threads/ewod-pcb.md``).

Why a family and not one board: He 2024's 10-board ranking reversed on
1182 boards (``docs/backlog/pcb-global-plan-router.md`` Benchmarks). A
sweep over pitch, pad count, source layout and layer count says WHERE
the router falls over, not merely THAT it does on one fixture.

Use::

    from tests.fixtures.pcb import qfp_fanout as qf
    r = qf.route(qf.FanoutSpec(pins_per_side=20, n_nets=55, sources="grid"))
    print(qf.scoreboard([r]))

``python -m tests.fixtures.pcb.qfp_fanout`` prints the default sweep.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field, replace
from typing import Any

from precis.pcb import feedback as pcb_feedback
from precis.pcb import pinswap as pcb_pinswap
from precis.pcb import realize as pcb_realize
from precis.pcb import session as pcb_session
from precis.pcb.ir import PcbIR, pin_point
from precis.workers.job_types.pcb_route import _resolve_pin_swap_groups

QFP_LCSC = "SYN-QFP"
SOURCE_LCSC = "SYN-SRC"
NET_CLASS = "fanout"


@dataclass(frozen=True)
class FanoutSpec:
    """One member of the family. Distances in mm."""

    pins_per_side: int = 20
    #: Nets to connect; ``None`` = every pad. The EWOD driver has 80 pads
    #: and 55 escape nets.
    n_nets: int | None = None
    pitch_mm: float = 0.8
    pad_w_mm: float = 0.5  # tangential
    pad_h_mm: float = 2.0  # radial
    #: Half-extent of the pad-centre ring; default leaves 0.5 mm of body
    #: inside the pads' inner ends.
    body_half_mm: float | None = None
    sources: str = "ring"  # "ring" | "grid" | "plaza"
    source_radius_mm: float = 12.0
    source_pitch_mm: float = 3.0
    #: ``plaza``: the sources sit in ``n_plazas`` tight clusters (the EWOD
    #: dogfood's nine plazas under the array), each a square packing at
    #: ``plaza_pitch_mm`` (the via envelope), cluster centres on a square
    #: lattice at ``source_pitch_mm * plaza_spread``.
    n_plazas: int = 9
    plaza_pitch_mm: float = 0.65
    plaza_spread: float = 3.0
    source_dia_mm: float = 0.45
    source_drill_mm: float = 0.15
    outline_mm: float = 32.0
    #: Routing layers for the fanout class. The 4-layer default mirrors
    #: the EWOD class (F.Cu reserved, In1.Cu a plane).
    layers: tuple[str, ...] = ("In2.Cu", "B.Cu")
    track_mm: float = 0.09
    clearance_mm: float = 0.099
    via_dia_mm: float = 0.45
    via_drill_mm: float = 0.15
    driver_bottom: bool = True
    seed: int = 1

    @property
    def n_pads(self) -> int:
        return 4 * self.pins_per_side

    @property
    def net_count(self) -> int:
        return self.n_pads if self.n_nets is None else min(self.n_nets, self.n_pads)

    @property
    def ring_half_mm(self) -> float:
        if self.body_half_mm is not None:
            return self.body_half_mm
        span = (self.pins_per_side - 1) * self.pitch_mm
        return span / 2.0 + self.pad_h_mm / 2.0 + 0.5

    @property
    def stackup(self) -> list[dict[str, Any]]:
        if set(self.layers) <= {"F.Cu", "B.Cu"}:
            return [
                {"name": "F.Cu", "role": "signal"},
                {"name": "B.Cu", "role": "signal"},
            ]
        return [
            {"name": "F.Cu", "role": "signal"},
            {"name": "In1.Cu", "role": "plane"},
            {"name": "In2.Cu", "role": "signal"},
            {"name": "B.Cu", "role": "signal"},
        ]

    def label(self) -> str:
        return (
            f"qfp{self.n_pads}/{self.net_count}n p{self.pitch_mm} "
            f"{self.sources}{f'@{self.source_radius_mm}' if self.sources == 'ring' else ''}"
            f" {len(self.stackup)}L s{self.seed}"
        )


# ── footprints ───────────────────────────────────────────────────────────


def _pad(
    number: str, x: float, y: float, w: float, h: float, *, drill: float | None = None
) -> dict[str, Any]:
    return {
        "number": number,
        "x": round(x, 4),
        "y": round(y, 4),
        "w": w,
        "h": h,
        "rot": 0.0,
        "layer": "F.Cu",
        "drill": drill,
        "shape": "circle" if drill else "rect",
    }


def qfp_footprint(spec: FanoutSpec) -> dict[str, Any]:
    """Ring of ``n_pads`` pads, numbered 1..N counter-clockwise from the
    bottom-left, each pad's long axis pointing out of the package. Pins
    are named ``P<n>``."""
    n, p, half = spec.pins_per_side, spec.pitch_mm, spec.ring_half_mm
    span = (n - 1) * p
    walk = [-span / 2.0 + i * p for i in range(n)]
    positions: list[tuple[float, float, bool]] = []  # x, y, vertical?
    positions += [(x, -half, True) for x in walk]  # bottom, left -> right
    positions += [(half, y, False) for y in walk]  # right, bottom -> top
    positions += [(x, half, True) for x in reversed(walk)]  # top, right -> left
    positions += [(-half, y, False) for y in reversed(walk)]  # left, top -> bottom
    pads = []
    pin_map = {}
    for i, (x, y, vertical) in enumerate(positions):
        number = str(i + 1)
        w, h = (
            (spec.pad_w_mm, spec.pad_h_mm)
            if vertical
            else (spec.pad_h_mm, spec.pad_w_mm)
        )
        pads.append(_pad(number, x, y, w, h))
        pin_map[number] = {"name": f"P{number}", "tags": []}
    return {"pads": pads, "pin_map": pin_map}


def source_footprint(spec: FanoutSpec) -> dict[str, Any]:
    d = spec.source_dia_mm
    return {
        "pads": [_pad("1", 0.0, 0.0, d, d, drill=spec.source_drill_mm)],
        "pin_map": {"1": {"name": "S", "tags": []}},
    }


def source_points(spec: FanoutSpec) -> list[tuple[float, float]]:
    """Where the ``net_count`` sources sit. Ring: evenly spaced at
    ``source_radius_mm``, phase seeded. Grid: a square lattice at
    ``source_pitch_mm`` centred on the package, a seeded sample of its
    cells (so the plazas are not a neat subset of the first rows)."""
    rng = random.Random(spec.seed)
    k = spec.net_count
    if spec.sources == "ring":
        phase = rng.random() * 2 * math.pi
        return [
            (
                round(spec.source_radius_mm * math.cos(phase + 2 * math.pi * i / k), 4),
                round(spec.source_radius_mm * math.sin(phase + 2 * math.pi * i / k), 4),
            )
            for i in range(k)
        ]
    if spec.sources == "plaza":
        side = math.ceil(math.sqrt(spec.n_plazas))
        centres = [
            (
                (i - (side - 1) / 2.0) * spec.source_pitch_mm * spec.plaza_spread,
                (j - (side - 1) / 2.0) * spec.source_pitch_mm * spec.plaza_spread,
            )
            for i in range(side)
            for j in range(side)
        ][: spec.n_plazas]
        per = math.ceil(k / spec.n_plazas)
        inner = math.ceil(math.sqrt(per))
        pts: list[tuple[float, float]] = []
        for cx, cy in centres:
            cells = [
                (
                    round(cx + (a - (inner - 1) / 2.0) * spec.plaza_pitch_mm, 4),
                    round(cy + (b - (inner - 1) / 2.0) * spec.plaza_pitch_mm, 4),
                )
                for a in range(inner)
                for b in range(inner)
            ]
            pts.extend(cells[:per])
        return sorted(rng.sample(pts, k))
    if spec.sources == "grid":
        side = math.ceil(math.sqrt(k))
        cells = [
            (
                round((i - (side - 1) / 2.0) * spec.source_pitch_mm, 4),
                round((j - (side - 1) / 2.0) * spec.source_pitch_mm, 4),
            )
            for i in range(side)
            for j in range(side)
        ]
        limit = spec.outline_mm / 2.0 - spec.source_dia_mm
        cells = [c for c in cells if abs(c[0]) < limit and abs(c[1]) < limit]
        if len(cells) < k:
            raise ValueError(
                f"grid of {len(cells)} cells inside the outline cannot seat {k} sources; "
                f"lower source_pitch_mm or raise outline_mm"
            )
        return sorted(rng.sample(cells, k))
    raise ValueError(f"sources must be 'ring', 'grid' or 'plaza', got {spec.sources!r}")


# ── graph ────────────────────────────────────────────────────────────────


def build_graph(spec: FanoutSpec) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """A ``Store.pcb_graph``-shaped dict plus its footprint cache, no DB.
    The initial pad assignment is a seeded random permutation of the
    first ``net_count`` pads — the warm start has to earn the result."""
    rng = random.Random(spec.seed + 1000)
    pads = [f"P{i + 1}" for i in range(spec.n_pads)]
    assigned = rng.sample(pads, spec.net_count)
    sources = source_points(spec)
    instances: list[dict[str, Any]] = [
        {
            "refdes": "U1",
            "part_lcsc": QFP_LCSC,
            "x": 0.0,
            "y": 0.0,
            "rot": 0.0,
            "layer": "bottom" if spec.driver_bottom else "top",
            "fixed": "both",
            "pin_swap_groups": [pads],
        }
    ]
    nets: list[dict[str, Any]] = []
    for i, ((sx, sy), pad) in enumerate(zip(sources, assigned, strict=True)):
        refdes = f"S{i + 1}"
        instances.append(
            {
                "refdes": refdes,
                "part_lcsc": SOURCE_LCSC,
                "x": sx,
                "y": sy,
                "rot": 0.0,
                "layer": "top",
                "fixed": "both",
            }
        )
        nets.append(
            {
                "name": f"N{i + 1:02d}",
                "net_class": NET_CLASS,
                "domain": "electrical",
                "members": [
                    {"refdes": refdes, "pin": "S"},
                    {"refdes": "U1", "pin": pad},
                ],
            }
        )
    graph = {
        "instances": instances,
        "nets": nets,
        "net_classes": {
            NET_CLASS: {
                "track_width_mm": spec.track_mm,
                "clearance_mm": spec.clearance_mm,
                "via_dia_mm": spec.via_dia_mm,
                "via_drill_mm": spec.via_drill_mm,
                "layers": list(spec.layers),
            }
        },
        "board": {"stackup": spec.stackup},
    }
    footprints = {QFP_LCSC: qfp_footprint(spec), SOURCE_LCSC: source_footprint(spec)}
    return graph, footprints


def outline(spec: FanoutSpec) -> list[list[float]]:
    h = spec.outline_mm / 2.0
    return [[-h, -h], [h, -h], [h, h], [-h, h]]


@dataclass
class Built:
    spec: FanoutSpec
    ir: PcbIR
    graph: dict[str, Any]
    footprints: dict[str, dict[str, Any]]
    config: pcb_realize.RealizeConfig
    groups: tuple[pcb_pinswap.PinSwapGroup, ...]
    warnings: list[str] = field(default_factory=list)


def build(spec: FanoutSpec) -> Built:
    """The IR the route job would build for this board, with real pad
    offsets applied and the swap group resolved the job's own way."""
    graph, by_lcsc = build_graph(spec)
    ir = pcb_session.build_ir(graph, outline=outline(spec), footprints_by_lcsc=by_lcsc)
    footprints = pcb_session.footprints_by_refdes(ir, by_lcsc)
    config = pcb_realize.RealizeConfig(class_rules=graph["net_classes"])
    warnings: list[str] = []
    groups = _resolve_pin_swap_groups(ir, graph, warnings)
    return Built(spec, ir, graph, footprints, config, groups, warnings)


# ── warm start ───────────────────────────────────────────────────────────


def warm_start(built: Built, method: str = "distance") -> str:
    """The job's pin-swap warm start, with the far end of each airwire
    read off the IR (the source pad's own position) instead of fixed
    copper: this family has no plaza copper, its sources are pads.
    Returns the method actually applied."""
    if method == "none":
        return "none"
    ir = built.ir
    applied = "radial"
    for group in built.groups:
        far_by_pin: dict[int, tuple[float, float]] = {}
        near = pcb_pinswap._segments_near_pin(ir, group.instance)
        for seg, pin in near.items():
            a, b = int(ir.seg_pin_a[seg]), int(ir.seg_pin_b[seg])
            other = b if a == pin else a
            pt = pin_point(ir, other)
            if pt is not None:
                far_by_pin[pin] = pt
        proposal = (
            pcb_pinswap.propose_distance_assignment(ir, group, far_by_pin)
            if method == "distance"
            else None
        )
        if proposal is None:
            pairs = pcb_pinswap.propose_radial_assignment(ir, group) or ()
        else:
            pairs, _ = proposal
            applied = "distance"
        for a, b in pairs:
            ir.swap_pins(a, b)
    return applied


# ── route + scoreboard ───────────────────────────────────────────────────


@dataclass(frozen=True)
class FanoutResult:
    spec: FanoutSpec
    warm_start: str
    routed: int
    total: int
    vias: int
    length_mm: float
    seconds: float
    unrouted: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.routed == self.total


def route(spec: FanoutSpec, *, warm: str = "distance") -> FanoutResult:
    """Build, warm-start, realize with the production maze (default
    config, 12 reorder passes), and count."""
    built = build(spec)
    applied = warm_start(built, warm)
    t0 = time.monotonic()
    rres = pcb_realize.realize(
        built.ir, config=built.config, footprints=built.footprints
    )
    seconds = time.monotonic() - t0
    outcome = pcb_feedback.outcome_from_realize(built.ir, rres)
    ir = built.ir
    unrouted = tuple(
        sorted({str(ir.net_name[int(ir.seg_net[s])]) for s in rres.unrouted})
    )
    length = 0.0
    for t in rres.tracks:
        for seg in t.segments:
            if seg.get("shape") == "line":
                (x0, y0), (x1, y1) = seg["start"], seg["end"]
                length += math.hypot(x1 - x0, y1 - y0)
    return FanoutResult(
        spec=spec,
        warm_start=applied,
        routed=outcome.routed,
        total=outcome.total,
        vias=len(rres.vias),
        length_mm=round(length, 1),
        seconds=round(seconds, 1),
        unrouted=unrouted,
    )


def scoreboard(results: list[FanoutResult]) -> str:
    rows = [
        "| board | warm | routed | vias | length mm | s |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        rows.append(
            f"| {r.spec.label()} | {r.warm_start} | {r.routed}/{r.total} | {r.vias} "
            f"| {r.length_mm} | {r.seconds} |"
        )
    return "\n".join(rows)


#: The default sweep: a small sanity member, the EWOD-shaped member
#: (80 pads, 55 nets, 0.8 mm pitch, grid sources, In2+B), and the same
#: with a 2-layer stack and with a finer pitch.
DEFAULT_SWEEP: tuple[FanoutSpec, ...] = (
    FanoutSpec(
        pins_per_side=4,
        pitch_mm=1.0,
        pad_h_mm=1.5,
        source_radius_mm=8.0,
        outline_mm=24.0,
    ),
    FanoutSpec(pins_per_side=20, n_nets=55, sources="grid"),
    FanoutSpec(pins_per_side=20, n_nets=55, sources="grid", layers=("F.Cu", "B.Cu")),
    FanoutSpec(pins_per_side=20, n_nets=55, sources="ring", source_radius_mm=13.0),
    FanoutSpec(pins_per_side=20, n_nets=55, sources="grid", pitch_mm=0.65),
    FanoutSpec(pins_per_side=20, n_nets=55, sources="plaza"),
)


def sweep(
    specs: tuple[FanoutSpec, ...] = DEFAULT_SWEEP, *, warm: str = "distance"
) -> str:
    return scoreboard([route(replace(s), warm=warm) for s in specs])


if __name__ == "__main__":
    print(sweep())

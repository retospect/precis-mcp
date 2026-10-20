"""The PCB sanity board — the acceptance board of
``docs/backlog/pcb-unified-place-route.md``: if the router cannot do this
it cannot do anything.

100 x 100 mm, four routable layers, four 10-pin 2.54 mm-pitch SMT headers,
one on each side, each centred on its side and 25 mm in from the edge
(``J_T``/``J_B`` at y = 75/25, ``J_L``/``J_R`` at x = 25/75). Every part is
``fixed='both'`` unless the variant frees it: the router is the only thing
under test. Top connects to bottom and left to right, 20 two-pin nets,
nothing else on the board.

Variants differ in the pin permutation of the two connections:

- ``straight`` — top pin ``i`` to bottom pin ``i``, left ``i`` to right ``i``.
- ``tb-s<N>`` — top-to-bottom is a seeded random permutation, left-right
  straight (the "raised difficulty" board; seeds 1-3).
- ``both-s1`` — both are permuted.
- ``free-bottom`` — straight, bottom header unpinned (placement test).

Pure in-memory (no store, no DB, like ``qfp_fanout``): the IR the route job
would build, routed with ``realize`` and checked with the routed DRC.

Iterate without the gate queue::

    uv run python -m precis.pcb.bench sanity [variant ...]

prints routed/20, DRC by rule and the per-net verdicts. ``--svg DIR`` writes
a drawing per variant; ``--upload`` puts each routed board in the configured
store as a frozen ``sanity-<variant>`` pcb board for viewing (never replaces
an existing slug; ``--version`` takes the next free ``-vN``).
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from precis.pcb import realize as pcb_realize
from precis.pcb import session as pcb_session
from precis.pcb.ir import PcbIR, pin_point
from precis.pcb.optimize import OptimizeConfig, optimize
from precis.workers.job_types.pcb_place import _DEFAULT_ITERS, _PLACE_ONLY_SCHEDULE
from precis.workers.job_types.pcb_route import _graph_net_rules

BOARD_MM = 100.0
INSET_MM = 25.0
PINS = 10
PITCH_MM = 2.54
FOOTPRINT = "HDR-SMT-1x10"
STACKUP: list[dict[str, Any]] = [
    {"name": "F.Cu", "role": "signal"},
    {"name": "In1.Cu", "role": "signal"},
    {"name": "In2.Cu", "role": "signal"},
    {"name": "B.Cu", "role": "signal"},
]


@dataclass(frozen=True)
class SanitySpec:
    """``tb_seed``/``lr_seed``: ``None`` = straight 1:1, else the seed of the
    random permutation of the second header's pin order. ``free``: refdes
    left unpinned for the placer."""

    name: str
    tb_seed: int | None = None
    lr_seed: int | None = None
    free: tuple[str, ...] = ()


SPECS: dict[str, SanitySpec] = {
    s.name: s
    for s in (
        SanitySpec("straight"),
        SanitySpec("tb-s1", tb_seed=1),
        SanitySpec("tb-s2", tb_seed=2),
        SanitySpec("tb-s3", tb_seed=3),
        SanitySpec("both-s1", tb_seed=1, lr_seed=1),
        SanitySpec("free-bottom", free=("J_B",)),
    )
}
#: The variants every router change must keep at 20/20 (all parts pinned).
PINNED = ("straight", "tb-s1", "tb-s2", "tb-s3", "both-s1")


def permutation(seed: int | None) -> list[int]:
    """Pin numbers 1..PINS in connection order; identity when unseeded."""
    pins = list(range(1, PINS + 1))
    if seed is not None:
        random.Random(seed).shuffle(pins)
    return pins


def footprint() -> dict[str, Any]:
    """One row of ``PINS`` SMT pads along local x, centred on the origin,
    long axis pointing out of the board edge (local y). Pins are ``P<n>``."""
    pads = []
    pin_map = {}
    for i in range(PINS):
        number = str(i + 1)
        pads.append(
            {
                "number": number,
                "x": round((i - (PINS - 1) / 2.0) * PITCH_MM, 4),
                "y": 0.0,
                "w": 1.5,
                "h": 3.0,
                "rot": 0.0,
                "layer": "F.Cu",
                "drill": None,
                "shape": "rect",
            }
        )
        pin_map[number] = {"name": f"P{number}", "tags": []}
    return {"pads": pads, "pin_map": pin_map}


def _header(
    refdes: str, x: float, y: float, rot: float, fixed: str | None
) -> dict[str, Any]:
    return {
        "refdes": refdes,
        "part_lcsc": FOOTPRINT,
        "x": x,
        "y": y,
        "rot": rot,
        "layer": "top",
        "fixed": fixed,
    }


def build_graph(spec: SanitySpec) -> dict[str, Any]:
    """A ``Store.pcb_graph``-shaped dict, no DB."""
    mid = BOARD_MM / 2.0
    far = BOARD_MM - INSET_MM
    placed = (
        ("J_T", mid, far, 0.0),
        ("J_B", mid, INSET_MM, 0.0),
        ("J_L", INSET_MM, mid, 90.0),
        ("J_R", far, mid, 90.0),
    )
    instances = [
        _header(r, x, y, rot, None if r in spec.free else "both")
        for r, x, y, rot in placed
    ]
    nets: list[dict[str, Any]] = []
    for prefix, a, b, seed in (
        ("TB", "J_T", "J_B", spec.tb_seed),
        ("LR", "J_L", "J_R", spec.lr_seed),
    ):
        for i, pin_b in enumerate(permutation(seed), start=1):
            nets.append(
                {
                    "name": f"{prefix}{i:02d}",
                    "net_class": "default",
                    "domain": "electrical",
                    "members": [
                        {"refdes": a, "pin": f"P{i}"},
                        {"refdes": b, "pin": f"P{pin_b}"},
                    ],
                }
            )
    return {
        "instances": instances,
        "nets": nets,
        "net_classes": {},
        "board": {"stackup": STACKUP},
    }


def outline() -> list[list[float]]:
    return [[0.0, 0.0], [BOARD_MM, 0.0], [BOARD_MM, BOARD_MM], [0.0, BOARD_MM]]


@dataclass
class Built:
    spec: SanitySpec
    ir: PcbIR
    graph: dict[str, Any]
    footprints: dict[str, dict[str, Any]]
    config: pcb_realize.RealizeConfig


def build(spec: SanitySpec) -> Built:
    """The IR the route job would build for this board."""
    graph = build_graph(spec)
    by_lcsc = {FOOTPRINT: footprint()}
    ir = pcb_session.build_ir(graph, outline=outline(), footprints_by_lcsc=by_lcsc)
    footprints = pcb_session.footprints_by_refdes(ir, by_lcsc)
    return Built(spec, ir, graph, footprints, pcb_realize.RealizeConfig())


def net_length_mm(built: Built, prefix: str = "") -> float:
    """Sum of the straight pin-to-pin distances of every airwire whose net
    name starts with ``prefix`` (``"TB"`` = the top-bottom nets) — the
    ratsnest length placement is meant to shorten."""
    ir = built.ir
    total = 0.0
    for s in range(len(ir.seg_net)):
        if not str(ir.net_name[int(ir.seg_net[s])]).startswith(prefix):
            continue
        a = pin_point(ir, int(ir.seg_pin_a[s]))
        b = pin_point(ir, int(ir.seg_pin_b[s]))
        if a is not None and b is not None:
            total += math.hypot(a[0] - b[0], a[1] - b[1])
    return total


def place(built: Built, *, seed: int = 1) -> dict[str, tuple[float, float, float]]:
    """The place job's anneal (its own schedule and default iterations) on
    the IR in place; returns the poses. Pinned parts do not move."""
    optimize(
        built.ir,
        OptimizeConfig(
            iters=_DEFAULT_ITERS,
            seed=seed,
            schedule=_PLACE_ONLY_SCHEDULE,
            pin_point_cost=True,
            centroid_jump=True,
        ),
    )
    return pcb_session.positions(built.ir)


def placement_errors(built: Built) -> list[str]:
    """Pre-route DRC errors (courtyard overlap, outline containment, ...) of
    the IR's current poses, as ``rule:where`` strings."""
    findings = pcb_session.placement_drc_findings(
        built.ir,
        capability=built.config.fab_caps,
        footprints=built.footprints,
        outline=outline(),
    )
    return sorted(
        f"{f.rule}:{getattr(f, 'where', '')}" for f in findings if f.severity == "error"
    )


@dataclass(frozen=True)
class SanityResult:
    spec: SanitySpec
    nets: int
    routed: int
    vias: int
    seconds: float
    #: net name -> why it did not route (empty when 20/20).
    failed: dict[str, str]
    #: ``rule:severity:where`` per DRC finding.
    findings: tuple[str, ...]
    #: The realize result and final poses, for the SVG and the upload.
    rres: Any = field(default=None, repr=False, compare=False)
    poses: dict[str, tuple[float, float, float]] = field(
        default_factory=dict, repr=False, compare=False
    )

    @property
    def errors_by_rule(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            rule, severity, _ = f.split(":", 2)
            if severity == "error":
                out[rule] = out.get(rule, 0) + 1
        return out


def route(spec: SanitySpec, built: Built | None = None) -> SanityResult:
    """Realize with the vector router (default config) and run the routed
    DRC. ``built`` lets a caller route a board it already placed."""
    built = built or build(spec)
    ir = built.ir
    t0 = time.monotonic()
    rres = pcb_realize.realize(ir, config=built.config, footprints=built.footprints)
    seconds = time.monotonic() - t0
    names = {int(n): str(ir.net_name[int(n)]) for n in ir.seg_net}
    unrouted = {int(ir.seg_net[s]) for s in rres.unrouted}
    reasons = _failure_reasons(rres)
    failed = {names[n]: reasons.get(names[n], "unrouted") for n in sorted(unrouted)}
    findings = pcb_session.routed_drc_findings(
        ir,
        rres,
        capability=built.config.fab_caps,
        footprints=built.footprints,
        outline=outline(),
        net_rules=_graph_net_rules(built.graph, built.config.fab_caps),
    )
    return SanityResult(
        spec=spec,
        nets=len(names),
        routed=len(names) - len(unrouted),
        vias=len(rres.vias),
        seconds=round(seconds, 1),
        failed=failed,
        findings=tuple(
            sorted(f"{f.rule}:{f.severity}:{getattr(f, 'where', '')}" for f in findings)
        ),
        rres=rres,
        poses=pcb_session.positions(ir),
    )


def _failure_reasons(rres: Any) -> dict[str, str]:
    """Per-net failure text off the realize result, when it carries any."""
    for attr in ("unrouted_reasons", "failures", "reasons"):
        got = getattr(rres, attr, None)
        if isinstance(got, dict):
            return {str(k): str(v) for k, v in got.items()}
    return {}


def report(results: list[SanityResult]) -> str:
    lines = ["| variant | routed | vias | s | DRC errors |", "|---|---|---|---|---|"]
    for r in results:
        errs = ", ".join(f"{k} {v}" for k, v in sorted(r.errors_by_rule.items()))
        lines.append(
            f"| {r.spec.name} | {r.routed}/{r.nets} | {r.vias} | {r.seconds} "
            f"| {sum(r.errors_by_rule.values())}: {errs or '—'} |"
        )
    for r in results:
        for net, why in r.failed.items():
            lines.append(f"  {r.spec.name}: {net} FAILED — {why}")
        for f in r.findings:
            lines.append(f"  {r.spec.name}: {f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m precis.pcb.bench sanity")
    parser.add_argument("variants", nargs="*", help=f"default: {list(PINNED)}")
    parser.add_argument("--svg", metavar="DIR", help="write DIR/sanity-<variant>.svg")
    parser.add_argument(
        "--upload",
        action="store_true",
        help="put each routed board in the configured store as a frozen "
        "sanity-<variant> pcb board (never replaces one)",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="with --upload: take the next free sanity-<variant>-vN when the "
        "slug exists instead of refusing",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    names = args.variants or list(PINNED)
    unknown = [n for n in names if n not in SPECS]
    if unknown:
        print(f"unknown variant(s) {unknown}; known: {list(SPECS)}", file=sys.stderr)
        return 2
    if args.version and not args.upload:
        print("--version only applies with --upload", file=sys.stderr)
        return 2
    runs = [_run(SPECS[n]) for n in names]
    results = [r for r, _ in runs]
    print(report(results))
    if args.svg:
        _write_svgs(args.svg, runs)
    if args.upload and not _upload(runs, version=args.version):
        return 1
    return 0 if all(not r.failed and not r.errors_by_rule for r in results) else 1


def _write_svgs(directory: str, runs: list[tuple[SanityResult, Built]]) -> None:
    from pathlib import Path

    from precis.pcb import sanity_render

    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    for result, built in runs:
        instances = [
            {**i, "x": result.poses[i["refdes"]][0], "y": result.poses[i["refdes"]][1]}
            | {"rot": result.poses[i["refdes"]][2]}
            for i in built.graph["instances"]
        ]
        path = out / f"sanity-{result.spec.name}.svg"
        path.write_text(
            sanity_render.svg(
                outline=outline(),
                instances=instances,
                footprint=footprint(),
                tracks=list(result.rres.tracks),
                vias=list(result.rres.vias),
                title=f"{result.spec.name}: {result.routed}/{result.nets}",
            ),
            encoding="utf-8",
        )
        print(f"{result.spec.name}: wrote {path}")


def _upload(runs: list[tuple[SanityResult, Built]], *, version: bool) -> bool:
    """Upload every run to the store ``PRECIS_DATABASE_URL`` names; a refused
    slug is reported and skipped (never replaced). True when none refused."""
    from precis.cli._common import resolve_dsn
    from precis.pcb import sanity_upload
    from precis.store import Store

    store = Store.connect(resolve_dsn(None))
    ok = True
    try:
        for result, built in runs:
            try:
                slug = sanity_upload.upload(store, result, built, version=version)
            except sanity_upload.UploadRefused as exc:
                print(f"{result.spec.name}: not uploaded — {exc}", file=sys.stderr)
                ok = False
                continue
            print(
                f"{result.spec.name}: uploaded {slug} -> {sanity_upload.page_path(slug)}"
            )
    finally:
        store.close()
    return ok


def _run(spec: SanitySpec) -> tuple[SanityResult, Built]:
    """Route a variant; one with unpinned parts is placed first, and what
    the placer did is printed (poses, TB airwire length, placement DRC)."""
    if not spec.free:
        built = build(spec)
        return route(spec, built), built
    built = build(spec)
    before = net_length_mm(built, "TB")
    poses = place(built)
    print(
        f"{spec.name}: placed "
        + ", ".join(
            f"{r}=({poses[r][0]:.2f}, {poses[r][1]:.2f}, rot {poses[r][2]:.0f})"
            for r in spec.free
        )
        + f"; TB airwire {before:.1f} -> {net_length_mm(built, 'TB'):.1f} mm; "
        f"placement DRC errors {placement_errors(built) or 'none'}"
    )
    return route(spec, built), built


if __name__ == "__main__":
    raise SystemExit(main())

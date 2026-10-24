"""Slice-2 place-and-route bench (``docs/backlog/pcb-place-route-
datamodel.md`` §Slices item 2): the bench boards, each measured twice on
the SAME job-placed IR — the production vector router (``realize``,
fixed poses) and the ``pcb.pr`` v1 engine (free parts) — into one
:class:`PrRow` per router. DB-free: the design dicts the job path
authors through ``PcbHandler`` are turned into the ``Store.pcb_graph``
shape here (:func:`design_graph`), built with :func:`session.build_ir`
and placed with the place job's own call (``optimize.optimize`` on the
place-only schedule, field polish on), so the bench runs wherever torch
does.

Boards:

- ``nano`` — ``tests/fixtures/pcb/nano_usb_pd_testboard.py`` (catalog +
  local footprints), placed at seed 1.
- ``esp32c3-s<N>`` — the 4-layer reference board, seeds 1–5, synthesized
  footprints (as the job path has them: no catalog rows). **No declared
  planes**: Reto 2026-10-23 (memory me507871) rules out flood pours, so
  GND/VCC3V3 are routed nets for both routers here (the frozen grid
  table's rows had F/B.Cu planes).
- ``qfp-plaza`` — :mod:`tests.fixtures.pcb.qfp_fanout`'s plaza member
  with the distance warm start, every part fixed (pr's free parts have
  nothing to move: a pure routing comparison).

``wall_s`` is the route step alone; DRC is the exact kernel pass
(:func:`precis.pcb.pr.finish.exact_drc`: copper rules plus the placement
rules, since pr moves parts) with silk rules dropped, errors and warns
split by rule as :mod:`route_bench` does.
"""

from __future__ import annotations

import collections
import copy
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any

from precis.pcb import DEFAULT_STACKUP, optimize, realize, session
from precis.pcb.capabilities import capability_for
from precis.pcb.drc import process_for_stackup
from precis.pcb.optimize import MoveKind, OptimizeConfig, ScheduleStage
from precis.pcb.pr import finish, v1
from precis.pcb.pr.v1 import V1Params
from precis.workers.job_types.pcb_route import _graph_net_rules, _graph_net_voltages
from tests.fixtures.pcb import qfp_fanout as qf
from tests.fixtures.pcb import route_bench as rb
from tests.fixtures.pcb.route_bench import _ESP32C3_STACKUP, ESP32C3_FIXTURE

#: The place job's schedule (``pcb_place._PLACE_ONLY_SCHEDULE``), mirrored
#: so the bench's 4-0 placement is the job's.
PLACE_ONLY_SCHEDULE = (
    ScheduleStage(
        1.0, {MoveKind.TRANSLATE: 0.6, MoveKind.ROTATE: 0.15, MoveKind.SWAP: 0.25}
    ),
)
PLACE_ITERS = 2000


@dataclass(frozen=True)
class Board:
    """One bench board as the pr engine and ``realize`` both consume it."""

    name: str
    ir: Any
    outline: list[list[float]]
    config: realize.RealizeConfig
    footprints: dict[str, dict[str, Any]]
    net_rules: dict[str, Any]
    net_voltages: dict[str, float]
    fixed_copper: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class PrRow:
    board: str
    router: str  # "pr" | "vroute"
    nets: int
    connections: int
    unrouted: int
    vias: int
    length_mm: float
    wall_s: float
    drc_s: float
    errors_by_rule: dict[str, int]
    warns_by_rule: dict[str, int]
    #: pr only: mean part travel / turn vs the job placement, evals/s,
    #: the belief entropy trail and the swept-block fraction.
    travel_mm: float = 0.0
    turn_deg: float = 0.0
    evals_per_s: float = 0.0
    entropy: tuple[float, ...] = ()
    blocked_frac: float = 0.0
    sugar: dict[str, int] = field(default_factory=dict)
    over_capacity: str = ""
    #: pr only: fraction of the movable parts whose final rotation is off
    #: the 90° grid — the placement gate's cap is
    #: ``tests/test_pcb_esp32c3_rotation_grid.py::MAX_OFF90_FRAC`` (0.10);
    #: anything above it would fail that gate were pr ever wired in.
    off90_frac: float = 0.0

    @property
    def errors(self) -> int:
        return sum(self.errors_by_rule.values())

    @property
    def warns(self) -> int:
        return sum(self.warns_by_rule.values())

    def key(self) -> tuple[Any, ...]:
        return (
            self.board,
            self.router,
            self.nets,
            self.connections,
            self.unrouted,
            self.vias,
            round(self.length_mm, 1),
            tuple(sorted(self.errors_by_rule.items())),
            tuple(sorted(self.warns_by_rule.items())),
        )


def render_table(rows: list[PrRow]) -> str:
    lines = [
        "| board | router | conns | unrouted | vias | wire mm | wall s | DRC s "
        "| DRC errors | DRC warns | travel mm | turn ° | off-90 | evals/s |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r.board} | {r.router} | {r.connections} | {r.unrouted} | {r.vias} "
            f"| {r.length_mm:.1f} | {r.wall_s:.1f} | {r.drc_s:.2f} "
            f"| {r.errors}: {rb._rules(r.errors_by_rule)} "
            f"| {r.warns}: {rb._rules(r.warns_by_rule)} "
            f"| {r.travel_mm:.2f} | {r.turn_deg:.1f} | {r.off90_frac:.2f} "
            f"| {r.evals_per_s:.1f} |"
        )
    return "\n".join(lines)


def _split(findings: list[Any]) -> tuple[dict[str, int], dict[str, int]]:
    errors: collections.Counter[str] = collections.Counter()
    warns: collections.Counter[str] = collections.Counter()
    for f in findings:
        rule = str(getattr(f, "rule", "unknown"))
        if rule.startswith("silk_"):
            continue
        (errors if getattr(f, "severity", "error") == "error" else warns)[rule] += 1
    return dict(errors), dict(warns)


def _counts(ir: Any) -> tuple[int, int]:
    nets: collections.Counter[int] = collections.Counter(int(n) for n in ir.seg_net)
    return len(nets), sum(nets.values())


# ── boards ──────────────────────────────────────────────────────────────

_INSTANCE_PASSTHROUGH = (
    "edge_mate",
    "group",
    "group_offset",
    "pattern",
    "pattern_instance",
    "max_travel_mm",
    "pin_swap_groups",
)
_NET_PASSTHROUGH = (
    ("working_voltage_v", "voltage"),
    ("edge_rate_v_per_ns", "edge_rate"),
    ("impedance_ohm", "impedance"),
    ("function_hint", "hint"),
)


def design_graph(
    design: dict[str, Any], *, stackup: list[dict[str, Any]]
) -> dict[str, Any]:
    """A ``put(kind='pcb')`` design payload (components / nets /
    connections) as the ``Store.pcb_graph`` shape — the field mapping
    ``pcb_apply`` performs (``part``→``part_lcsc``, ``class``→
    ``net_class``, ``current``→``est_current_a``, members grouped from
    connections), without the store."""
    instances: list[dict[str, Any]] = []
    for comp in design.get("components") or []:
        inst: dict[str, Any] = {
            "refdes": str(comp["refdes"]),
            "label": comp.get("label") or comp["refdes"],
            "part_lcsc": comp.get("part_lcsc") or comp.get("part"),
            "footprint": comp.get("footprint"),
            "x": comp.get("x"),
            "y": comp.get("y"),
            "rot": comp.get("rot") or 0.0,
            "layer": str(comp.get("layer") or "top"),
            "fixed": comp.get("fixed"),
            "roles": list(comp.get("roles") or []),
        }
        for key in _INSTANCE_PASSTHROUGH:
            if comp.get(key) is not None:
                inst[key] = comp[key]
        instances.append(inst)
    members: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
    for c in design.get("connections") or []:
        members[str(c["net"])].append(
            {"refdes": str(c["refdes"]), "pin": str(c["pin"])}
        )
    nets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for n in design.get("nets") or []:
        name = str(n["name"])
        seen.add(name)
        net: dict[str, Any] = {
            "name": name,
            "net_class": n.get("net_class") or n.get("class"),
            "domain": n.get("domain") or "electrical",
            "est_current_a": n.get("est_current_a") or n.get("current"),
            "members": members.get(name, []),
        }
        for key, alt in _NET_PASSTHROUGH:
            val = n.get(key, n.get(alt))
            if val is not None:
                net[key] = val
        nets.append(net)
    for name, mem in members.items():
        if name not in seen:
            nets.append(
                {
                    "name": name,
                    "net_class": None,
                    "domain": "electrical",
                    "members": mem,
                }
            )
    return {
        "instances": instances,
        "nets": nets,
        "net_classes": dict(design.get("net_classes") or {}),
        "board": {"stackup": list(stackup)},
    }


#: The bench opts the field polish IN (as the route bench's main column
#: does); the place job's own default is off (``optimize.
#: FIELD_POLISH_DEFAULT``), so this is a bench choice, named here.
PLACE_FIELD_POLISH = True


def _place(ir: Any, seed: int) -> None:
    """The place job's 4-0 placement on ``ir`` in place: seed + the
    place-only anneal, field polish per :data:`PLACE_FIELD_POLISH`."""
    optimize.optimize(
        ir,
        OptimizeConfig(
            iters=PLACE_ITERS,
            seed=seed,
            schedule=PLACE_ONLY_SCHEDULE,
            field_polish=PLACE_FIELD_POLISH,
        ),
    )


def board_from_design(
    name: str,
    design: dict[str, Any],
    *,
    footprints_by_lcsc: dict[str, dict[str, Any]],
    stackup: list[dict[str, Any]],
    seed: int,
) -> Board:
    graph = design_graph(design, stackup=stackup)
    features = list(design.get("features") or [])
    outline = session.outline_from_features(features)
    assert outline is not None, f"{name}: no outline"
    local = {str(f["name"]): f for f in design.get("footprints") or []}
    ir = session.build_ir(
        graph,
        outline=outline,
        mounting_holes=session.mounting_holes_from_features(features),
        footprints_by_lcsc=footprints_by_lcsc,
        local_footprints_by_name=local,
    )
    footprints = session.footprints_by_refdes(
        ir,
        footprints_by_lcsc,
        local_footprints_by_name=local,
        local_names_by_refdes=session.local_footprint_names_by_refdes(graph),
    )
    _place(ir, seed)
    caps = capability_for(process_for_stackup(ir.stackup))
    config = realize.RealizeConfig(fab_caps=caps, class_rules=graph["net_classes"])
    return Board(
        name=name,
        ir=ir,
        outline=outline,
        config=config,
        footprints=footprints,
        net_rules=_graph_net_rules(graph, caps),
        net_voltages=_graph_net_voltages(graph),
    )


def board_nano(seed: int = 1) -> Board:
    from tests.fixtures.pcb import nano_usb_pd_testboard as nano_fx

    return board_from_design(
        "nano" if seed == 1 else f"nano-s{seed}",
        nano_fx.DESIGN,
        footprints_by_lcsc=dict(nano_fx.CATALOG_FOOTPRINTS),
        stackup=DEFAULT_STACKUP,
        seed=seed,
    )


def board_esp32c3(seed: int) -> Board:
    with ESP32C3_FIXTURE.open(encoding="utf-8") as fh:
        design: dict[str, Any] = copy.deepcopy(json.load(fh))
    m = rb._ESP32C3_BOARD_MM
    ring = [[0.0, 0.0], [m, 0.0], [m, m], [0.0, m]]
    design["features"] = [{"ftype": "outline", "geom": {"path": ring + [ring[0]]}}]
    return board_from_design(
        f"esp32c3-s{seed}",
        design,
        footprints_by_lcsc={},
        stackup=_ESP32C3_STACKUP,
        seed=seed,
    )


def board_qfp_plaza() -> Board:
    built = qf.build(qf.FanoutSpec(pins_per_side=20, n_nets=55, sources="plaza"))
    qf.warm_start(built, "distance")
    caps = built.config.fab_caps
    return Board(
        name="qfp-plaza",
        ir=built.ir,
        outline=qf.outline(built.spec),
        config=built.config,
        footprints=built.footprints,
        net_rules=_graph_net_rules(built.graph, caps),
        net_voltages={},
    )


def bench_boards(names: set[str] | None = None) -> list[Board]:
    """The bench set, optionally filtered by name."""

    def wanted(name: str) -> bool:
        return names is None or name in names

    out: list[Board] = []
    if wanted("nano"):
        out.append(board_nano())
    out += [board_esp32c3(s) for s in rb.ESP32C3_SEEDS if wanted(f"esp32c3-s{s}")]
    if wanted("qfp-plaza"):
        out.append(board_qfp_plaza())
    return out


# ── measurement ─────────────────────────────────────────────────────────


def _drc(board: Board, ir: Any, rres: Any) -> tuple[list[Any], float]:
    t0 = time.monotonic()
    findings = finish.exact_drc(
        ir,
        rres,
        capability=board.config.fab_caps,
        footprints=board.footprints,
        outline=board.outline,
        net_rules=board.net_rules,
        net_voltages=board.net_voltages,
        fixed_copper=board.fixed_copper or None,
    )
    return findings, time.monotonic() - t0


def measure_vroute(
    board: Board, skip_nets: frozenset[str] = frozenset(), label: str | None = None
) -> PrRow:
    """``skip_nets`` leaves those nets unrouted on purpose (counted as
    unrouted, so the denominator stays the board's) — the diagnostic
    rows that take the star-hub ground net out."""
    ir = copy.deepcopy(board.ir)
    seg_ids = (
        [
            s
            for s in range(ir.n_segments)
            if str(ir.net_name[int(ir.seg_net[s])]) not in skip_nets
        ]
        if skip_nets
        else None
    )
    t0 = time.monotonic()
    rres = realize.realize(
        ir,
        config=board.config,
        seg_ids=seg_ids,
        footprints=board.footprints,
        fixed_copper=board.fixed_copper or None,
    )
    wall = time.monotonic() - t0
    findings, drc_s = _drc(board, ir, rres)
    errors, warns = _split(findings)
    nets, conns = _counts(ir)
    skipped = 0 if seg_ids is None else ir.n_segments - len(seg_ids)
    return PrRow(
        board=label or board.name,
        router="vroute",
        nets=nets,
        connections=conns,
        unrouted=len(rres.unrouted) + skipped,
        vias=len(rres.vias),
        length_mm=round(sum(rb._track_length(t.segments) for t in rres.tracks), 1),
        wall_s=round(wall, 1),
        drc_s=round(drc_s, 2),
        errors_by_rule=errors,
        warns_by_rule=warns,
    )


def measure_pr(
    board: Board, params: V1Params | None = None, label: str | None = None
) -> tuple[PrRow, v1.V1Run]:
    t0 = time.monotonic()
    run = v1.run(
        board.ir,
        outline=board.outline,
        config=board.config,
        footprints=board.footprints,
        params=params,
    )
    wall = time.monotonic() - t0
    findings, drc_s = _drc(board, run.ir, run.result)
    errors, warns = _split(findings)
    nets, conns = _counts(run.ir)
    row = PrRow(
        board=label or board.name,
        router="pr",
        nets=nets,
        connections=conns,
        unrouted=len(run.unrouted),
        vias=run.via_count,
        length_mm=round(run.wire_mm, 1),
        wall_s=round(wall, 1),
        drc_s=round(drc_s, 2),
        errors_by_rule=errors,
        warns_by_rule=warns,
        travel_mm=round(run.part_travel_mm, 3),
        turn_deg=round(run.part_turn_deg, 2),
        evals_per_s=round(run.evals / max(run.relax_seconds, 1e-9), 1),
        entropy=tuple(round(e, 3) for e in run.entropy_hist),
        blocked_frac=round(run.blocked_frac, 3),
        sugar=dict(run.sugar),
        over_capacity=run.over_capacity[0] if run.over_capacity else "",
        off90_frac=round(off90_fraction(run), 3),
    )
    return row, run


#: The placement gate's cap on off-90° rotations
#: (``tests/test_pcb_esp32c3_rotation_grid.py::MAX_OFF90_FRAC``), mirrored
#: so the bench reports against it.
MAX_OFF90_FRAC = 0.10


def off90_fraction(run: v1.V1Run, tol_deg: float = 1e-6) -> float:
    """Fraction of the movable parts whose final rotation is not a
    multiple of 90° (within ``tol_deg``) — the rotation-grid gate's
    ``off90`` read, over the parts pr could turn."""
    dyn = run.problem.dyn
    if not dyn.n_parts:
        return 0.0
    deg = (math.degrees(float(th)) % 360.0 for th in dyn.pose0[:, 2])
    off = sum(1 for d in deg if min(d % 90.0, 90.0 - d % 90.0) > tol_deg)
    return off / dyn.n_parts


def off_grid_count(run: v1.V1Run, tol_deg: float = 1e-6) -> int:
    """Movable parts whose written rotation is not on the 45° grid — the
    gate's ``bad_grid``; must be 0."""
    from precis.pcb.field import ROT_SNAP_DEG

    dyn = run.problem.dyn
    snap = ROT_SNAP_DEG
    deg = (math.degrees(float(th)) % 360.0 for th in dyn.pose0[:, 2])
    return sum(1 for d in deg if min(d % snap, snap - d % snap) > tol_deg)


def unrouted_names(ir: Any, rres: Any) -> tuple[str, ...]:
    return tuple(sorted({str(ir.net_name[int(ir.seg_net[s])]) for s in rres.unrouted}))


def nan_free(run: v1.V1Run) -> bool:
    for poly in run.polylines.values():
        if not math.isfinite(float(abs(poly).sum())):
            return False
    return all(math.isfinite(float(v)) for v in run.problem.dyn.pose0.ravel())

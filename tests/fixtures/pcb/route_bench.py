"""PCB route benchmark set — the measurement harness of
``docs/backlog/pcb-vector-router.md`` slice 0.

Every board the vector-router slices gate on, routed the way its own
regression test routes it, measured into one :class:`BenchRow`:

- ``nano`` — ``tests/test_pcb_nano_usb_pd_testboard.py`` at seed 1
  (place + route jobs through ``PcbHandler``, DRC recorded).
- ``esp32c3/s<N>`` — ``tests/test_pcb_fab_render_all_layers.py``, seeds
  1-5 (4-layer stackup, GND/VCC3V3 outer planes, place + route jobs).
- ``ewod-replay`` — ``tests/test_pcb_escape_replay.py``'s
  ``("distance", open_inner=True)`` member (fixed poses, ``realize``
  direct, routed DRC).
- ``qfp-plaza`` — ``tests/fixtures/pcb/qfp_fanout.py``'s plaza member
  with the distance warm start (DB-free, ``realize`` direct).
- ``taz15-blank`` — the gitignored prod export
  (``scripts/pcb-snapshot export-prod taz15-blank <path>``) at its source
  poses, every instance ``fixed='both'``, GND→B.Cu and 3V3→F.Cu declared,
  one route job at ``iters=1 seed=1``. Runs only when the snapshot exists.

The job-path boards report the persisted state (``pcb_route_status``,
``pcb_copper``, a recorded ``view='drc'`` run); the realize-path boards
report the in-memory result and :func:`precis.pcb.session.routed_drc_findings`
(copper rules only — their tests' own legality check). ``routed``/``failed``
count nets with two or more members, the honest denominator every floor
uses. ``wall_s`` is the route step alone (the route job's drain, or the
``realize`` call), never placement or DRC.
"""

from __future__ import annotations

import collections
import copy
import json
import math
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import drc, realize, resolved, session
from precis.pcb.capabilities import capability_for
from precis.pcb.drc import process_for_stackup
from precis.pcb.snapshot import load_snapshot, read_snapshot
from precis.store import Store
from precis.workers.executors._common import TERMINAL, current_status
from precis.workers.executors.job_inproc import run_job_inproc_pass
from precis.workers.job_types.pcb_route import (
    _apply_pin_swap_warm_start,
    _graph_net_rules,
    _graph_net_voltages,
    _resolve_pin_swap_groups,
)
from tests.fixtures.pcb import nano_usb_pd_testboard as nano_fx
from tests.fixtures.pcb import qfp_fanout as qf

FIXTURES = Path(__file__).parent
ESP32C3_FIXTURE = FIXTURES / "esp32c3_reference.json"
EWOD_REPLAY_FIXTURE = FIXTURES / "ewod-dogfood-6-replay-v1.json.gz"
#: Where ``scripts/pcb-snapshot export-prod taz15-blank`` output is looked
#: for by default (gitignored: exact board data is opt-in, not test data).
TAZ15_BLANK_SNAPSHOT = (
    FIXTURES.parent.parent.parent / ".scratch" / "pcb-snapshot" / "taz15-blank.json"
)
ESP32C3_SEEDS: tuple[int, ...] = (1, 2, 3, 4, 5)
_ESP32C3_BOARD_MM = 40.0
_ESP32C3_STACKUP: list[dict[str, Any]] = [
    {"name": "F.Cu", "role": "signal", "routable": True, "pourable": True},
    {"name": "In1.Cu", "role": "signal", "routable": True, "pourable": False},
    {"name": "In2.Cu", "role": "signal", "routable": True, "pourable": False},
    {"name": "B.Cu", "role": "signal", "routable": True, "pourable": True},
]


@dataclass(frozen=True)
class BenchRow:
    """One board's measurement. ``key()`` is everything but wall time —
    what a rerun must reproduce exactly."""

    board: str
    nets: int
    connections: int
    routed: int
    failed: int
    vias: int
    length_mm: float
    wall_s: float
    #: The DRC pass alone (the recorded ``view='drc'`` run, or
    #: ``routed_drc_findings``) — slice 2's +20 % budget reads this.
    drc_s: float = 0.0
    errors_by_rule: dict[str, int] = field(default_factory=dict)
    warns_by_rule: dict[str, int] = field(default_factory=dict)
    failed_nets: tuple[str, ...] = ()
    #: ``rule:severity:finding_key`` per finding, sorted — what "DRC
    #: identical by finding_key" (slice 2's exit) is diffed on.
    finding_keys: tuple[str, ...] = ()

    @property
    def errors(self) -> int:
        return sum(self.errors_by_rule.values())

    @property
    def warns(self) -> int:
        return sum(self.warns_by_rule.values())

    def key(self) -> tuple[Any, ...]:
        return (
            self.board,
            self.nets,
            self.connections,
            self.routed,
            self.failed,
            self.vias,
            self.length_mm,
            tuple(sorted(self.errors_by_rule.items())),
            tuple(sorted(self.warns_by_rule.items())),
            self.failed_nets,
            self.finding_keys,
        )


def _rules(counts: dict[str, int]) -> str:
    return ", ".join(f"{r} {n}" for r, n in sorted(counts.items())) or "—"


def render_table(rows: Iterable[BenchRow]) -> str:
    """The slice-0 table: one row per board, DRC broken down by rule."""
    lines = [
        "| board | routed/nets | failed | conns | vias | wire mm | wall s "
        "| s/net | s/conn | DRC s | DRC errors | DRC warns |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        per_net = r.wall_s / r.nets if r.nets else 0.0
        per_conn = r.wall_s / r.connections if r.connections else 0.0
        lines.append(
            f"| {r.board} | {r.routed}/{r.nets} | {r.failed} | {r.connections} "
            f"| {r.vias} | {r.length_mm:.1f} | {r.wall_s:.1f} | {per_net:.2f} "
            f"| {per_conn:.3f} | {r.drc_s:.2f} | {r.errors}: {_rules(r.errors_by_rule)} "
            f"| {r.warns}: {_rules(r.warns_by_rule)} |"
        )
    return "\n".join(lines)


def _track_length(segments: Iterable[dict[str, Any]]) -> float:
    """Centreline length of line and arc segments."""
    total = 0.0
    for seg in segments:
        if seg.get("shape") == "line":
            (x0, y0), (x1, y1) = seg["start"], seg["end"]
            total += math.hypot(x1 - x0, y1 - y0)
        elif seg.get("shape") == "arc":
            (x0, y0), (x1, y1) = seg["start"], seg["end"]
            cx, cy = seg["center"]
            a0 = math.atan2(y0 - cy, x0 - cx)
            a1 = math.atan2(y1 - cy, x1 - cx)
            sweep = (a0 - a1 if seg.get("cw") else a1 - a0) % math.tau
            total += math.hypot(x0 - cx, y0 - cy) * sweep
    return total


def _split_findings(
    findings: Iterable[Any],
) -> tuple[dict[str, int], dict[str, int], tuple[str, ...]]:
    """Errors and warns by rule, plus the sorted finding keys. A persisted
    row carries its ``finding_key``; an in-memory finding is keyed here."""
    errors: collections.Counter[str] = collections.Counter()
    warns: collections.Counter[str] = collections.Counter()
    keys: list[str] = []
    for f in findings:
        if isinstance(f, dict):
            rule, severity, key = f["rule"], f["severity"], f.get("finding_key")
        else:
            rule, severity, key = f.rule, f.severity, drc.finding_key(f)
        (errors if severity == "error" else warns)[str(rule)] += 1
        keys.append(f"{rule}:{severity}:{key}")
    return dict(errors), dict(warns), tuple(sorted(keys))


# ── job-path boards (place/route jobs through PcbHandler) ────────────────


def _drain_latest_job(store: Store, parent_id: int) -> None:
    """Drain this design's newest job to a terminal status. A failed route
    job is a measurement (any failed net fails the job), not an error."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'job' AND parent_id = %s "
            "ORDER BY ref_id DESC LIMIT 1",
            (parent_id,),
        ).fetchone()
    assert row is not None, f"no job queued for parent_id={parent_id}"
    for _ in range(25):
        with store.pool.connection() as conn:
            if current_status(conn, row[0]) in TERMINAL:
                return
        result = run_job_inproc_pass(store, limit=1)
        assert result["claimed"] == 1, f"expected a queued job, got {result}"
    raise AssertionError(f"job {row[0]} never reached a terminal status")


def _measure_job_board(
    store: Store, pcb: PcbHandler, slug: str, route_args: dict[str, Any]
) -> BenchRow:
    """Run one route job on an existing design and read back the row."""
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    enq = pcb.put(id=slug, args={"op": "route", **route_args})
    assert "enqueued" in enq.body, enq.body
    t0 = time.monotonic()
    _drain_latest_job(store, ref.id)
    wall = time.monotonic() - t0

    graph = store.pcb_graph(ref.id)
    fanout = {str(n["name"]): len(n["members"]) for n in graph["nets"]}
    multi = {name for name, k in fanout.items() if k >= 2}
    status = {
        str(r["name"]): r["status"]
        for r in store.pcb_route_status(ref.id)
        if r["name"] in multi
    }
    failed = tuple(sorted(n for n, s in status.items() if s == "failed"))
    board_id = store.pcb_ensure_board(ref.id)
    vias = 0
    length = 0.0
    for item in store.pcb_copper_list(board_id):
        if item.get("fixed"):
            continue
        if item["ctype"] == "via":
            vias += 1
        elif item["ctype"] == "track":
            length += _track_length(item.get("segments") or ())

    t0 = time.monotonic()
    pcb._drc_run(ref.id, store.pcb_load(ref.id), record=True)
    drc_s = time.monotonic() - t0
    _run, findings = store.pcb_drc_findings_latest(ref.id)
    errors, warns, keys = _split_findings(findings)
    return BenchRow(
        board=slug,
        nets=len(multi),
        connections=sum(fanout[n] - 1 for n in multi),
        routed=sum(1 for s in status.values() if s == "realized"),
        failed=len(failed),
        vias=vias,
        length_mm=round(length, 1),
        wall_s=round(wall, 1),
        drc_s=round(drc_s, 2),
        errors_by_rule=errors,
        warns_by_rule=warns,
        failed_nets=failed,
        finding_keys=keys,
    )


def bench_nano(store: Store, seed: int = 1) -> BenchRow:
    for lcsc, fp in nano_fx.CATALOG_FOOTPRINTS.items():
        store.part_footprint_put(lcsc, fp)
    pcb = PcbHandler(hub=Hub(store=store))
    slug = "nano" if seed == 1 else f"nano-s{seed}"
    assert "created" in pcb.put(id=slug, args=dict(nano_fx.DESIGN)).body
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    assert "enqueued" in pcb.put(id=slug, args={"op": "place", "seed": seed}).body
    _drain_latest_job(store, ref.id)
    return _measure_job_board(store, pcb, slug, {"seed": seed})


def bench_esp32c3(store: Store, seed: int) -> BenchRow:
    with ESP32C3_FIXTURE.open(encoding="utf-8") as fh:
        design: dict[str, Any] = copy.deepcopy(json.load(fh))
    m = _ESP32C3_BOARD_MM
    design["features"] = [
        {
            "ftype": "outline",
            "geom": {"path": [[0, 0], [m, 0], [m, m], [0, m], [0, 0]]},
        }
    ]
    slug = f"esp32c3-s{seed}"
    pcb = PcbHandler(hub=Hub(store=store))
    assert "created" in pcb.put(id=slug, args=design).body
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    board_id = store.pcb_ensure_board(ref.id)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE pcb_boards SET stackup = %s WHERE board_id = %s",
            (json.dumps(_ESP32C3_STACKUP), board_id),
        )
    pcb.put(id=slug, args={"op": "plane_net", "layer": "F.Cu", "net": "GND"})
    pcb.put(id=slug, args={"op": "plane_net", "layer": "B.Cu", "net": "VCC3V3"})
    assert "enqueued" in pcb.put(id=slug, args={"op": "place", "seed": seed}).body
    _drain_latest_job(store, ref.id)
    return _measure_job_board(store, pcb, slug, {"seed": seed})


def bench_taz15_blank(store: Store, snapshot: Path = TAZ15_BLANK_SNAPSHOT) -> BenchRow:
    """gr489923's repro: source poses, all frozen, the two outer rails
    declared as planes, one route pass."""
    slug = "taz15-blank"
    with store.pool.connection() as conn:
        ref_id, _params = load_snapshot(conn, read_snapshot(snapshot), slug)
        conn.execute(
            "UPDATE pcb_instances SET fixed = 'both' WHERE board_id IN "
            "(SELECT board_id FROM pcb_boards WHERE ref_id = %s)",
            (ref_id,),
        )
    pcb = PcbHandler(hub=Hub(store=store))
    pcb.put(id=slug, args={"op": "plane_net", "layer": "B.Cu", "net": "GND"})
    pcb.put(id=slug, args={"op": "plane_net", "layer": "F.Cu", "net": "3V3"})
    return _measure_job_board(store, pcb, slug, {"iters": 1, "seed": 1})


# ── realize-path boards (fixed poses, realize direct) ─────────────────────


def _measure_realized(
    board: str,
    ir: Any,
    rres: realize.RealizeResult,
    wall: float,
    findings: list[Any],
    drc_s: float,
) -> BenchRow:
    nets: collections.Counter[int] = collections.Counter(int(n) for n in ir.seg_net)
    unrouted = {int(ir.seg_net[s]) for s in rres.unrouted}
    failed = tuple(sorted(str(ir.net_name[n]) for n in unrouted))
    errors, warns, keys = _split_findings(findings)
    length = sum(_track_length(t.segments) for t in rres.tracks)
    return BenchRow(
        board=board,
        nets=len(nets),
        connections=sum(nets.values()),
        routed=len(nets) - len(unrouted),
        failed=len(failed),
        vias=len(rres.vias),
        length_mm=round(length, 1),
        wall_s=round(wall, 1),
        drc_s=round(drc_s, 2),
        errors_by_rule=errors,
        warns_by_rule=warns,
        failed_nets=failed,
        finding_keys=keys,
    )


def bench_ewod_replay(store: Store) -> BenchRow:
    """``test_connected_via_channel_assignment_replay[distance-True]``:
    distance warm start, In2.Cu opened for the escape class."""
    fixture = read_snapshot(EWOD_REPLAY_FIXTURE)
    with store.pool.connection() as conn:
        ref_id, _ = load_snapshot(conn, fixture, "ewod-replay-bench")
    graph = store.pcb_graph(ref_id)
    features = store.pcb_features_list(ref_id)
    local = store.pcb_local_footprints_for(ref_id)
    cached = store.pcb_footprints_for(ref_id)
    fixed = store.pcb_fixed_copper_list(graph["board"]["board_id"])
    ir = session.build_ir(
        resolved.apply(graph, store.pcb_netlist_snapshot_latest(ref_id)),
        outline=session.outline_from_features(features),
        mounting_holes=session.mounting_holes_from_features(features),
        footprints_by_lcsc=cached,
        local_footprints_by_name=local,
        fixed_copper=fixed,
    )
    session.apply_route_overrides(ir, store.pcb_routes_get(ref_id))
    footprints = session.footprints_by_refdes(
        ir,
        cached,
        local_footprints_by_name=local,
        local_names_by_refdes=session.local_footprint_names_by_refdes(graph),
    )
    config = realize.RealizeConfig(
        fab_caps=capability_for(process_for_stackup(ir.stackup)),
        class_rules=graph["net_classes"],
    )
    groups = _resolve_pin_swap_groups(ir, graph, [])
    _apply_pin_swap_warm_start(ir, groups, footprints, fixed, "distance")
    inner = next(i for i, layer in enumerate(ir.stackup) if layer["name"] == "In2.Cu")
    ir.stackup[inner] = {"name": "In2.Cu", "role": "signal", "routable": True}
    assert config.class_rules is not None
    config.class_rules["ewod_ARR1_escape"]["layers"] = ["In2.Cu", "B.Cu"]
    t0 = time.monotonic()
    rres = realize.realize(ir, config=config, footprints=footprints, fixed_copper=fixed)
    wall = time.monotonic() - t0
    t0 = time.monotonic()
    findings = session.routed_drc_findings(
        ir,
        rres,
        capability=config.fab_caps,
        footprints=footprints,
        fixed_copper=fixed,
        outline=session.outline_from_features(features),
        net_rules=_graph_net_rules(graph, config.fab_caps),
        net_voltages=_graph_net_voltages(graph),
    )
    drc_s = time.monotonic() - t0
    return _measure_realized("ewod-replay", ir, rres, wall, findings, drc_s)


def bench_qfp_plaza() -> BenchRow:
    """The plaza member of ``test_ewod_like_member_scoreboard`` with the
    distance warm start; DB-free."""
    built = qf.build(qf.FanoutSpec(pins_per_side=20, n_nets=55, sources="plaza"))
    qf.warm_start(built, "distance")
    t0 = time.monotonic()
    rres = realize.realize(built.ir, config=built.config, footprints=built.footprints)
    wall = time.monotonic() - t0
    t0 = time.monotonic()
    findings = session.routed_drc_findings(
        built.ir,
        rres,
        capability=built.config.fab_caps,
        footprints=built.footprints,
        outline=qf.outline(built.spec),
        net_rules=_graph_net_rules(built.graph, built.config.fab_caps),
    )
    drc_s = time.monotonic() - t0
    return _measure_realized("qfp-plaza", built.ir, rres, wall, findings, drc_s)

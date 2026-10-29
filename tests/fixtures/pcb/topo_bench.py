"""topo-place vs default anneal bench — Reto's default-switch bar
(``gr515107``, ``docs/backlog/pcb-topo-place.md`` dogfood ruling
2026-10-25: "yes dogfood all the time when it starts kinda working").

Every board runs through the REAL job path on the test DB so both
pipelines see the same gates and stores:

- ``anneal+vroute`` — ``put(op='route')``: RETIRED. The vector router
  was deleted 2026-10-25 (tag ``vroute-final``) and ``op='route'``
  refuses, so this side can only ever report the refusal. It runs only
  under ``run_bench(anneal=True)`` — the numbers table is topo-only.
- ``topo+route`` — ``put(op='place', placer='topo')``: ONE job (the
  slice-5 ruling). It writes the phase-0 winning pose through the same
  placement-DRC gate and fixed-copper rip-up the anneal gets, then
  routes that pose in-process (``realise.route_design`` +
  ``widen_power``) and writes the copper + ``pcb_routes`` rows itself.
  The ``route_mode`` field names the mechanism so the columns'
  provenance stays explicit.

Per board the row carries routed/nets (nets with two or more members,
the route bench's honest denominator), DRC errors, median routed-net
track length, via count and wall time for each pipeline; the topo-place
stage numbers (proxy crossings, evals, evals/s) plus the phase-1 fields
that do not exist yet — odd cycles and the via bracket read
``n/a (slice 2)`` — and a per-metric PASS/FAIL against the bar: no
fewer routed nets, no more DRC errors, median length and vias no
worse, faster overall.

Boards: the standing dogfood set (``heater-base-test`` exported from
pb460559, ``taz15-blank`` exported from pb482131 — prod exports under
``.scratch/pcb-snapshot/``, present only when exported) plus the
route-bench members that have a placement step: ``nano``, ``esp32c3``
seeds 1-5, and ``ewod-replay`` (a fixed-pose control — the topo side
respects fixed flags, so its two pipelines should agree).
Prod dogfood runs go to the lossy epro COPIES pb517197/pb517198 only
(Reto 2026-10-25); the bench measures exports of the originals and each
row's ``source`` records which board id it came from.
``qfp-plaza`` is left out: every instance is ``fixed='both'`` and its
fixture builds an IR directly, not a store design, so there is no
placement comparison to measure.
"""

from __future__ import annotations

import collections
import copy
import dataclasses
import json
import math
import os
import re
import statistics
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import drc
from precis.pcb.snapshot import load_snapshot, read_snapshot
from precis.store import Store
from precis.workers.executors._common import SUCCEEDED, TERMINAL, current_status
from precis.workers.executors.job_inproc import run_job_inproc_pass
from tests.fixtures.pcb import nano_usb_pd_testboard as nano_fx

FIXTURES = Path(__file__).parent
ESP32C3_FIXTURE = FIXTURES / "esp32c3_reference.json"
EWOD_REPLAY_FIXTURE = FIXTURES / "ewod-dogfood-6-replay-v1.json.gz"
ESP32C3_SEEDS: tuple[int, ...] = (1, 2, 3, 4, 5)
_ESP32C3_BOARD_MM = 40.0
_ESP32C3_STACKUP: list[dict[str, Any]] = [
    {"name": "F.Cu", "role": "signal", "routable": True, "pourable": True},
    {"name": "In1.Cu", "role": "signal", "routable": True, "pourable": False},
    {"name": "In2.Cu", "role": "signal", "routable": True, "pourable": False},
    {"name": "B.Cu", "role": "signal", "routable": True, "pourable": True},
]
REPO = FIXTURES.parent.parent.parent
#: Gitignored prod exports, captured read-only by
#: ``scripts/pcb-snapshot export-prod <slug> .scratch/pcb-snapshot/<slug>.json``.
SNAPSHOT_DIR = REPO / ".scratch" / "pcb-snapshot"
SNAPSHOT_BOARDS: tuple[str, ...] = ("heater-base-test", "taz15-blank")
#: Which prod board id each snapshot export came from — recorded on the
#: row so the table's provenance survives the lossy epro copies
#: (pb517197/pb517198, prod-dogfood-only per Reto 2026-10-25).
SNAPSHOT_SOURCES: dict[str, str] = {
    "heater-base-test": "pb460559",
    "taz15-blank": "pb482131",
}
#: The topo placer's eval budget (``pcb.local.topo_place``'s own default,
#: mirrored by the ``placer='topo'`` job).
TOPO_EFFORT = 20000
#: Named provenance for the topo columns: the place job's own route
#: stage (slice 5) replaced the pose hand-off to a vroute leg.
TOPO_ROUTE_MODE = "in-job topo route (slice 5)"
_BASE_CROSS_RE = re.compile(r"crossings (\d+) → (\d+)")


#: Net classes that mark a rail — the plane-class set ``ratsnest`` uses
#: plus the power names ``planes`` knows.
_POWER_CLASSES = frozenset({"gnd", "ground", "power", "pwr", "plane"})
#: Rail-shaped names for boards whose nets carry no class — EasyEDA
#: exports tag every net with the DEFAULT class, so GND/3V3/12V_*/PV+
#: are only findable by name (gr515107 loop-9: GND alone took 75/169
#: vias and skewed the via comparison).
_POWER_NAME_RE = re.compile(
    r"(?:gnd|ground|vcc|vdd|vss|vee|vbat|vbus|pv\+|(?:^|_)\d+v\d*(?:_|$))",
    re.IGNORECASE,
)


def power_net_names(nets: list[dict[str, Any]]) -> set[str]:
    """The nets the widen stage and the split metrics treat as power/GND:
    a plane/power net class, ``domain='power'``, a declared current, or a
    rail-shaped name (``GND``, ``3V3``, ``12V_POWER``, ``PV+``)."""
    out: set[str] = set()
    for n in nets:
        name = str(n.get("name") or "")
        cls = str(n.get("net_class") or "").strip().lower()
        if (
            cls in _POWER_CLASSES
            or str(n.get("domain") or "").strip().lower() == "power"
            or (n.get("est_current_a") or 0) > 0
            or _POWER_NAME_RE.search(name)
        ):
            out.add(name)
    return out


@dataclass(frozen=True)
class PipeSide:
    """One pipeline's route numbers. ``None`` fields print ``n/a`` —
    refused runs, absent boards and gate failures all land as ``None``
    with the reason in ``note`` rather than as a 0. The
    ``*_signal``/``*_power`` splits report power/GND copper separately
    (:func:`power_net_names`) so a rail-dominated via count does not
    skew the pipeline comparison; the bar metrics compare signal-side
    numbers when present, the totals otherwise. ``drc_rules`` is the
    error count per DRC rule (:func:`_split_findings`) — the class
    breakdown the numbers table prints."""

    routed: int | None = None
    nets: int | None = None
    drc_errors: int | None = None
    drc_rules: dict[str, int] | None = None
    median_mm: float | None = None
    vias: int | None = None
    vias_signal: int | None = None
    vias_power: int | None = None
    median_mm_signal: float | None = None
    median_mm_power: float | None = None
    wall_s: float | None = None
    note: str = ""


@dataclass(frozen=True)
class TopoStage:
    """The topo-place stage's own metrics. ``odd_cycles``/``via_bracket``
    are phase-1 (spec slice 2) outputs — text sentinels until then."""

    crossings_start: int | None = None
    crossings: int | None = None
    evals: int | None = None
    evals_per_s: float | None = None
    airwire_mm: float | None = None
    place_s: float | None = None
    odd_cycles: str = "n/a (slice 2)"
    via_bracket: str = "n/a (slice 2)"
    #: Post-pass rows from ``manifest.json.result`` (absent pre-shove).
    shove_mm: float | None = None
    shove_rounds: int | None = None
    legalize_mm: float | None = None
    legalize_left: int | None = None
    #: Swallowed mesh conflicts the shove pass counted (S1 loud-failure
    #: reporting — a 0/absent run prints no note).
    shove_reason: str = ""
    mesh_errors: int | None = None
    #: S6: corridor edges over capacity on the in-job route (gr520904).
    corridor_overflow: int | None = None


@dataclass(frozen=True)
class TopoBenchRow:
    board: str
    #: Provenance — ``pb<id>`` for prod-export boards, ``fixture`` for
    #: designs built in-test. Rendered as ``board (source)``.
    source: str = ""
    anneal: PipeSide = field(default_factory=PipeSide)
    topo: PipeSide = field(default_factory=PipeSide)
    topo_stage: TopoStage = field(default_factory=TopoStage)
    note: str = ""


#: Reto's bar (GRIPE_FIX 2026-10-25): (metric, table header, passes-when).
_METRICS: tuple[tuple[str, str, Any], ...] = (
    ("routed", "routed ≥", lambda a, t: t >= a),
    ("drc_errors", "DRC ≤", lambda a, t: t <= a),
    ("median_mm", "med mm ≤", lambda a, t: t <= a),
    ("vias", "vias ≤", lambda a, t: t <= a),
    ("wall_s", "faster", lambda a, t: t < a),
)


def _bar_value(side: PipeSide, metric: str) -> Any:
    """The number a bar metric compares: the signal-side split when it
    exists (a GND-dominated via or length total would skew the flip
    comparison — the review's pour-net ask), the plain field otherwise."""
    split = getattr(side, f"{metric}_signal", None)
    return split if split is not None else getattr(side, metric)


def verdicts(row: TopoBenchRow) -> dict[str, str]:
    """PASS/FAIL/n/a per bar metric — ``n/a`` whenever either side lacks
    the number (a refused pipeline is a missing measurement, not a 0)."""
    out: dict[str, str] = {}
    for metric, _header, passes in _METRICS:
        a = _bar_value(row.anneal, metric)
        t = _bar_value(row.topo, metric)
        out[metric] = (
            "n/a" if a is None or t is None else ("PASS" if passes(a, t) else "FAIL")
        )
    return out


def overall(row: TopoBenchRow) -> str:
    """FAIL on any failed metric, PASS when every metric passes, else
    ``n/a`` — an unmeasured bar is pending, not a verdict."""
    vs = verdicts(row)
    if "FAIL" in vs.values():
        return "FAIL"
    if all(v == "PASS" for v in vs.values()):
        return "PASS"
    return "n/a"


def _cell(side: PipeSide, attr: str, fmt: str = "{}") -> str:
    v = getattr(side, attr)
    return "n/a" if v is None else fmt.format(v)


def _board_cell(row: TopoBenchRow) -> str:
    return f"{row.board} ({row.source})" if row.source else row.board


def _vias_cell(side: PipeSide) -> str:
    """``sig+pow`` when the power split exists — the totals when it does
    not (a refused side, a board with no power copper)."""
    if side.vias is None:
        return "n/a"
    if side.vias_power is None:
        return str(side.vias)
    return f"{side.vias_signal}+{side.vias_power}p"


def _mm_cell(side: PipeSide) -> str:
    """Median net length as ``sig+pow`` — same split as ``_vias_cell``."""
    if side.median_mm is None:
        return "n/a"
    if side.median_mm_power is None:
        return f"{side.median_mm:.1f}"
    return f"{side.median_mm_signal:.1f}+{side.median_mm_power:.1f}p"


def render_table(rows: list[TopoBenchRow]) -> str:
    """The two-pipeline results table — the topo+route columns measure
    the place job's own route stage. ``vias`` and ``med net mm`` render
    ``signal+power`` (:func:`power_net_names`) so rail copper reports
    separately from the signal comparison."""
    lines = [
        f"route mode: {TOPO_ROUTE_MODE}",
        "",
        "| board | pipeline | routed/nets | DRC err | med net mm | vias | wall s |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        for label, side in (("anneal+vroute", r.anneal), ("topo+route", r.topo)):
            note = f" — {side.note}" if side.note else ""
            lines.append(
                f"| {_board_cell(r)} | {label} | "
                f"{_cell(side, 'routed')}/{_cell(side, 'nets')} "
                f"| {_cell(side, 'drc_errors')} "
                f"| {_mm_cell(side)} "
                f"| {_vias_cell(side)} "
                f"| {_cell(side, 'wall_s', '{:.1f}')}{note} |"
            )
    return "\n".join(lines)


def render_drc_table(rows: list[TopoBenchRow]) -> str:
    """The topo side's DRC errors broken out by rule — one column per
    rule class seen across the run (``keepout_copper`` gets its own
    column like every other), ``-`` for a rule a board never hit."""
    rules = sorted({rule for r in rows for rule in (r.topo.drc_rules or {})})
    lines = [
        "",
        "| board | " + " | ".join(rules or ["(no findings)"]) + " | total |",
        "|---|" + "---|" * (len(rules) + 1 or 2),
    ]
    for r in rows:
        counts = r.topo.drc_rules or {}
        cells = " | ".join(str(counts.get(rule, "-")) for rule in rules) or "-"
        total = r.topo.drc_errors
        lines.append(
            f"| {_board_cell(r)} | {cells} | {'n/a' if total is None else total} |"
        )
    return "\n".join(lines)


def render_stage_table(rows: list[TopoBenchRow]) -> str:
    """The topo-place stage table — phase-0 fields live now; the
    phase-1 columns hold their ``n/a (slice N)`` sentinels until the
    topological route exists."""
    lines = [
        "",
        "| board | proxy crossings | evals | evals/s | airwire mm | place s "
        "| odd cycles | via bracket | corridor ovf |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        s = r.topo_stage
        xings = (
            f"{s.crossings_start} → {s.crossings}"
            if s.crossings_start is not None
            else ("n/a" if s.crossings is None else str(s.crossings))
        )
        shove_note = ""
        if s.shove_reason == "mesh" or s.mesh_errors:
            shove_note = (
                f" — shove:{s.shove_reason or 'ok'} ({s.mesh_errors or 0} mesh)"
            )
        lines.append(
            f"| {_board_cell(r)} | {xings} "
            f"| {'n/a' if s.evals is None else s.evals} "
            f"| {'n/a' if s.evals_per_s is None else f'{s.evals_per_s:.0f}'} "
            f"| {'n/a' if s.airwire_mm is None else f'{s.airwire_mm:.0f}'} "
            f"| {'n/a' if s.place_s is None else f'{s.place_s:.1f}'} "
            f"| {s.odd_cycles} | {s.via_bracket} "
            f"| {'n/a' if s.corridor_overflow is None else s.corridor_overflow} |"
            f"{shove_note}"
        )
    return "\n".join(lines)


def render_verdicts(rows: list[TopoBenchRow]) -> str:
    """PASS/FAIL per metric and overall against Reto's bar."""
    header = " | ".join(h for _m, h, _f in _METRICS)
    lines = [
        "",
        f"| board | {header} | overall |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        vs = verdicts(r)
        cells = " | ".join(vs[m] for m, _h, _f in _METRICS)
        note = f" — {r.note}" if r.note else ""
        lines.append(f"| {_board_cell(r)} | {cells} | {overall(r)}{note} |")
    return "\n".join(lines)


def bench_json(rows: list[TopoBenchRow], *, generated_utc: str) -> dict[str, Any]:
    """The machine-readable mirror — ``route_mode`` pins the column
    provenance so a later mechanism change is not mistaken for the
    in-job slice-5 route these rows measured."""

    def side_dict(s: PipeSide) -> dict[str, Any]:
        return {
            "routed": s.routed,
            "nets": s.nets,
            "drc_errors": s.drc_errors,
            "drc_rules": s.drc_rules,
            "median_mm": s.median_mm,
            "vias": s.vias,
            "vias_signal": s.vias_signal,
            "vias_power": s.vias_power,
            "median_mm_signal": s.median_mm_signal,
            "median_mm_power": s.median_mm_power,
            "wall_s": s.wall_s,
            "note": s.note,
        }

    return {
        "schema": "precis-pcb-topo-bench/1",
        "generated_utc": generated_utc,
        "route_mode": TOPO_ROUTE_MODE,
        "bar": {
            "routed": "topo >= anneal",
            "drc_errors": "topo <= anneal",
            "median_mm": "topo <= anneal",
            "vias": "topo <= anneal",
            "wall_s": "topo < anneal",
        },
        "boards": [
            {
                "board": r.board,
                "source": r.source,
                "anneal": side_dict(r.anneal),
                "topo": side_dict(r.topo),
                "topo_stage": {
                    "crossings_start": r.topo_stage.crossings_start,
                    "crossings": r.topo_stage.crossings,
                    "evals": r.topo_stage.evals,
                    "evals_per_s": r.topo_stage.evals_per_s,
                    "airwire_mm": r.topo_stage.airwire_mm,
                    "place_s": r.topo_stage.place_s,
                    "odd_cycles": r.topo_stage.odd_cycles,
                    "via_bracket": r.topo_stage.via_bracket,
                    "corridor_overflow": r.topo_stage.corridor_overflow,
                },
                "verdicts": verdicts(r),
                "overall": overall(r),
                "note": r.note,
            }
            for r in rows
        ],
    }


# ── job-path measurement ────────────────────────────────────────────────


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


def job_summary_text(store: Store, job_id: int) -> str:
    """A finished job's result text — stored as a ``job_summary`` chunk on
    the job ref, not a refs column (gr513874)."""
    texts = [
        b.text
        for b in store.chunks.list_chunks_for_ref(job_id)
        if b.chunk_kind == "job_summary"
    ]
    assert texts, f"no job_summary chunk on job {job_id}"
    return "\n".join(texts)


def _drain(store: Store, parent_id: int) -> tuple[int, str, float]:
    """Drain this design's newest job; returns ``(job_id, status,
    wall_s)`` — a failed job is a measurement, not an exception."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'job' AND parent_id = %s "
            "ORDER BY ref_id DESC LIMIT 1",
            (parent_id,),
        ).fetchone()
    assert row is not None, f"no job queued for parent_id={parent_id}"
    job_id = int(row[0])
    t0 = time.monotonic()
    for _ in range(60):
        with store.pool.connection() as conn:
            status = current_status(conn, job_id)
            if status in TERMINAL:
                assert status is not None
                return job_id, status, time.monotonic() - t0
        result = run_job_inproc_pass(store, limit=1)
        assert result["claimed"] == 1, f"expected a queued job, got {result}"
    raise AssertionError(f"job {job_id} never reached a terminal status")


def _job_meta(store: Store, job_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (job_id,)
        ).fetchone()
    return dict(row[0]) if row and row[0] else {}


def _topo_stage(store: Store, job_id: int, place_wall: float) -> TopoStage:
    """A finished ``placer='topo'`` job's own metrics, off the run files
    the job stamped into ``refs.meta.topo_run`` — via the store accessor
    (gr524274), not a hand-rolled meta dig (crossings come from
    ``summary.txt``'s ``crossings A → B`` line, the rest from
    ``manifest.json.result``)."""
    run = store.pcb_job_run_files(job_id) or {}
    route_meta = run.get("route") or {}
    files = run.get("files") or {}
    result = ((files.get("manifest.json") or {}).get("result")) or {}
    summary = str(run.get("summary") or "")
    m = _BASE_CROSS_RE.search(summary)
    evals = int(result["evals"]) if result.get("evals") is not None else None
    return TopoStage(
        crossings_start=int(m.group(1)) if m else None,
        crossings=int(m.group(2)) if m else result.get("crossings"),
        evals=evals,
        evals_per_s=(
            round(evals / place_wall, 1)
            if evals is not None and place_wall > 0
            else None
        ),
        airwire_mm=(
            round(float(result["length_mm"]), 1)
            if result.get("length_mm") is not None
            else None
        ),
        place_s=round(place_wall, 1),
        shove_mm=(
            float((result.get("shove") or {})["moved_mm"])
            if (result.get("shove") or {}).get("moved_mm") is not None
            else None
        ),
        shove_rounds=(result.get("shove") or {}).get("rounds"),
        shove_reason=str((result.get("shove") or {}).get("reason") or ""),
        mesh_errors=(result.get("shove") or {}).get("mesh_errors"),
        legalize_mm=(
            float((result.get("legalize") or {})["moved_mm"])
            if (result.get("legalize") or {}).get("moved_mm") is not None
            else None
        ),
        legalize_left=(result.get("legalize") or {}).get("remaining"),
        corridor_overflow=route_meta.get("corridor_overflow"),
    )


def _fail_side(store: Store, job_id: int, what: str) -> PipeSide:
    """A failed job as an empty side: ``refs.meta.error`` is the bounded
    breadcrumb ``record_failure`` stamps; fall back to the first summary
    line when the job got far enough to write one."""
    meta = _job_meta(store, job_id)
    reason = str(meta.get("error") or "")
    if not reason:
        try:
            reason = job_summary_text(store, job_id).splitlines()[0]
        except (AssertionError, IndexError):
            pass
    return PipeSide(note=f"{what}: {reason.splitlines()[0]}".strip(": "))


def _measure(store: Store, pcb: PcbHandler, slug: str, wall: float) -> PipeSide:
    """Read a finished route run back: routed/total over >=2-member nets,
    via count, median per-net track length, the recorded DRC errors."""
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    graph = store.pcb_graph(ref.id)
    fanout = {str(n["name"]): len(n["members"]) for n in graph["nets"]}
    multi = {name for name, k in fanout.items() if k >= 2}
    status = {
        str(r["name"]): r["status"]
        for r in store.pcb_route_status(ref.id)
        if r["name"] in multi
    }
    board_id = store.pcb_ensure_board(ref.id)
    power = power_net_names(graph["nets"])
    vias = 0
    via_per_net: dict[str, int] = collections.defaultdict(int)
    per_net: dict[str, float] = collections.defaultdict(float)
    for item in store.pcb_copper_list(board_id):
        if item.get("fixed"):
            continue
        if item["ctype"] == "via":
            vias += 1
            via_per_net[str(item["net"])] += 1
        elif item["ctype"] == "track":
            per_net[str(item["net"])] += _track_length(item.get("segments") or ())
    pcb._drc_run(ref.id, store.pcb_load(ref.id), record=True)
    _run, findings = store.pcb_drc_findings_latest(ref.id)
    errors, _warns, _keys = _split_findings(findings)
    lengths = [v for v in per_net.values() if v > 0]
    sig_lengths = [v for k, v in per_net.items() if v > 0 and k not in power]
    pow_lengths = [v for k, v in per_net.items() if v > 0 and k in power]
    vias_power = sum(c for n, c in via_per_net.items() if n in power)
    return PipeSide(
        routed=sum(1 for s in status.values() if s == "realized"),
        nets=len(multi),
        drc_errors=sum(errors.values()),
        drc_rules=dict(errors),
        median_mm=(round(statistics.median(lengths), 2) if lengths else 0.0),
        vias=vias,
        vias_signal=vias - vias_power,
        vias_power=vias_power,
        median_mm_signal=(
            round(statistics.median(sig_lengths), 2) if sig_lengths else 0.0
        ),
        median_mm_power=(
            round(statistics.median(pow_lengths), 2) if pow_lengths else 0.0
        ),
        wall_s=round(wall, 1),
    )


def _anneal_side(store: Store, pcb: PcbHandler, slug: str, *, seed: int) -> PipeSide:
    """The default pipeline: one ``op='route'`` job — joint place+sketch
    anneal, then realize."""
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    try:
        enq = pcb.put(id=slug, args={"op": "route", "seed": seed})
    except BadInput as exc:
        return PipeSide(note=f"anneal route refused: {exc}")
    if "enqueued" not in enq.body:
        return PipeSide(note=f"anneal route refused: {enq.body.splitlines()[0]}")
    job_id, status, wall = _drain(store, ref.id)
    if status != SUCCEEDED:
        # A failed route still writes its partial copper — report the
        # counts it did get, with the failure as the note.
        return dataclasses.replace(
            _measure(store, pcb, slug, wall),
            note=_fail_side(store, job_id, "anneal+vroute job failed").note,
        )
    return _measure(store, pcb, slug, wall)


def _topo_side(
    store: Store,
    pcb: PcbHandler,
    slug: str,
    *,
    seed: int,
    effort: int,
    force: bool = False,
    route: bool = True,
) -> tuple[PipeSide, TopoStage]:
    """topo place+route, measured off the ONE ``placer='topo'`` job: the
    slice-5 ruling made place and route a single job — the committed
    pose is routed in-process and its copper + ``pcb_routes`` rows are
    written before the job returns, so there is no second route leg to
    drain. A refused place leaves the stored pose — nothing new was
    routed — so a failed job ends the side with the refusal in
    ``note``. ``force`` rides the place gate's escape hatch (the proxy
    pose can be placement-illegal); the bench does not use it — it
    exists for cheap end-to-end smoke."""
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    try:
        enq = pcb.put(
            id=slug,
            args={
                "op": "place",
                "placer": "topo",
                "route": route,
                "iters": effort,
                "seed": seed,
                "force": force,
            },
        )
    except BadInput as exc:
        return PipeSide(note=f"topo place refused: {exc}"), TopoStage()
    if "enqueued" not in enq.body:
        return (
            PipeSide(note=f"topo place refused: {enq.body.splitlines()[0]}"),
            TopoStage(),
        )
    place_job, status, place_wall = _drain(store, ref.id)
    if status != SUCCEEDED:
        # The job stamps meta.topo_run before recording the refusal —
        # the refused run's crossings/evals still land in the table. A
        # route-stage raise fails the job too (S1): name it from
        # meta.topo_run.route.error so it reads as a route failure, not
        # a place-gate refusal.
        route_meta = (_job_meta(store, place_job).get("topo_run") or {}).get(
            "route"
        ) or {}
        if route_meta.get("error"):
            return PipeSide(
                note=f"topo route stage raised: {route_meta['error']}"
            ), _topo_stage(store, place_job, place_wall)
        return _fail_side(store, place_job, "topo place refused"), _topo_stage(
            store, place_job, place_wall
        )
    return _measure(store, pcb, slug, place_wall), _topo_stage(
        store, place_job, place_wall
    )


# ── boards ──────────────────────────────────────────────────────────────


def _refresh_generators(store: Store, pcb: PcbHandler, slug: str) -> int:
    """Re-put every stale generator entry at the current code version
    (gr480010's gate refuses route on stale copper; the refusal's own
    ``next`` is to put the same entry again, which re-expands and
    re-places its output). Both board copies get the same refresh."""
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    from precis.pcb import generators as pcb_generators

    rows = store.pcb_generators_for(ref.id)
    n = 0
    for name, gen, _stored, _cur in pcb_generators.stale_generators(rows):
        entry = {
            "name": name,
            "generator": gen,
            "params": dict(rows[name].get("params") or {}),
        }
        # A true refusal raises BadInput; the response body is the
        # expansion report.
        pcb.put(id=slug, args={"generators": [entry]})
        n += 1
    return n


#: The anneal column's standing note when the side is skipped —
#: ``op='route'`` refuses unconditionally (vroute deleted 2026-10-25),
#: so the comparison column can never carry a number again.
_ANNEAL_RETIRED = "anneal+vroute retired — op='route' refuses"


def _snapshot_board(
    store: Store,
    pcb: PcbHandler,
    board: str,
    fixture: Path,
    *,
    seed: int,
    effort: int,
    anneal: bool,
) -> TopoBenchRow:
    """A prod export loaded twice (anneal/topo copies share nothing) —
    once when the anneal side is skipped."""
    fixture_data = read_snapshot(fixture)
    suffixes = ("anneal", "topo") if anneal else ("topo",)
    with store.pool.connection() as conn:
        for suffix in suffixes:
            load_snapshot(conn, fixture_data, f"{board}-{suffix}")
    refreshed = sum(
        _refresh_generators(store, pcb, f"{board}-{suffix}") for suffix in suffixes
    )
    note = f"re-expanded {refreshed} stale generator(s)" if refreshed else ""
    anneal_side = (
        _anneal_side(store, pcb, f"{board}-anneal", seed=seed)
        if anneal
        else PipeSide(note=_ANNEAL_RETIRED)
    )
    topo, stage = _topo_side(store, pcb, f"{board}-topo", seed=seed, effort=effort)
    return TopoBenchRow(
        board=board,
        source=SNAPSHOT_SOURCES.get(board, "fixture"),
        anneal=anneal_side,
        topo=topo,
        topo_stage=stage,
        note=note,
    )


def _design_board(
    store: Store,
    pcb: PcbHandler,
    board: str,
    design: dict[str, Any],
    *,
    seed: int,
    effort: int,
    setup: Any = None,
    anneal: bool,
) -> TopoBenchRow:
    """A batch design put under two slugs (one when the anneal side is
    skipped); ``setup(pcb, slug)`` applies board extras (stackup, plane
    nets) identically to every copy."""
    suffixes = ("anneal", "topo") if anneal else ("topo",)
    for suffix in suffixes:
        slug = f"{board}-{suffix}"
        assert "created" in pcb.put(id=slug, args=copy.deepcopy(design)).body
        if setup is not None:
            setup(store, pcb, slug)
    anneal_side = (
        _anneal_side(store, pcb, f"{board}-anneal", seed=seed)
        if anneal
        else PipeSide(note=_ANNEAL_RETIRED)
    )
    topo, stage = _topo_side(store, pcb, f"{board}-topo", seed=seed, effort=effort)
    return TopoBenchRow(
        board=board,
        source="fixture",
        anneal=anneal_side,
        topo=topo,
        topo_stage=stage,
    )


def bench_nano(
    store: Store, seed: int = 1, *, effort: int = TOPO_EFFORT, anneal: bool = False
) -> TopoBenchRow:
    for lcsc, fp in nano_fx.CATALOG_FOOTPRINTS.items():
        store.part_footprint_put(lcsc, fp)
    pcb = PcbHandler(hub=Hub(store=store))
    return _design_board(
        store,
        pcb,
        "nano",
        dict(nano_fx.DESIGN),
        seed=seed,
        effort=effort,
        anneal=anneal,
    )


def _esp32c3_setup(store: Store, pcb: PcbHandler, slug: str) -> None:
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


def bench_esp32c3(
    store: Store, seed: int, *, effort: int = TOPO_EFFORT, anneal: bool = False
) -> TopoBenchRow:
    with ESP32C3_FIXTURE.open(encoding="utf-8") as fh:
        design: dict[str, Any] = copy.deepcopy(json.load(fh))
    m = _ESP32C3_BOARD_MM
    design["features"] = [
        {
            "ftype": "outline",
            "geom": {"path": [[0, 0], [m, 0], [m, m], [0, m], [0, 0]]},
        }
    ]
    pcb = PcbHandler(hub=Hub(store=store))
    return _design_board(
        store,
        pcb,
        f"esp32c3-s{seed}",
        design,
        seed=seed,
        effort=effort,
        setup=_esp32c3_setup,
        anneal=anneal,
    )


def bench_ewod(
    store: Store, *, seed: int = 1, effort: int = TOPO_EFFORT, anneal: bool = False
) -> TopoBenchRow:
    """The replay fixture as a fixed-pose control — the topo side must
    leave the frozen placement alone and equal the anneal side."""
    pcb = PcbHandler(hub=Hub(store=store))
    return _snapshot_board(
        store,
        pcb,
        "ewod-replay",
        EWOD_REPLAY_FIXTURE,
        seed=seed,
        effort=effort,
        anneal=anneal,
    )


def snapshot_fixture(board: str) -> Path | None:
    """The board's exported snapshot path, overridable per board via
    ``PRECIS_PCB_SNAP_<SLUG>`` (slug uppercased, ``-`` → ``_``); ``None``
    when the export does not exist (the board is skipped, not failed)."""
    env = os.environ.get(f"PRECIS_PCB_SNAP_{board.upper().replace('-', '_')}")
    path = Path(env) if env else SNAPSHOT_DIR / f"{board}.json"
    return path if path.exists() else None


def bench_snapshot(
    store: Store,
    board: str,
    *,
    seed: int = 1,
    effort: int = TOPO_EFFORT,
    anneal: bool = False,
) -> TopoBenchRow:
    pcb = PcbHandler(hub=Hub(store=store))
    fixture = snapshot_fixture(board)
    assert fixture is not None, f"no snapshot for {board}"
    return _snapshot_board(
        store, pcb, board, fixture, seed=seed, effort=effort, anneal=anneal
    )


def run_bench(
    store: Store, *, effort: int = TOPO_EFFORT, anneal: bool = False
) -> list[TopoBenchRow]:
    """The full set: standing boards when their exports exist, then the
    route bench's placement boards. ``anneal=True`` still runs the
    retired ``op='route'`` side (it can only report the refusal — kept
    for the comparison table's shape)."""
    rows: list[TopoBenchRow] = []
    for board in SNAPSHOT_BOARDS:
        if snapshot_fixture(board) is not None:
            rows.append(bench_snapshot(store, board, effort=effort, anneal=anneal))
    rows.append(bench_nano(store, effort=effort, anneal=anneal))
    rows += [
        bench_esp32c3(store, seed, effort=effort, anneal=anneal)
        for seed in ESP32C3_SEEDS
    ]
    rows.append(bench_ewod(store, effort=effort, anneal=anneal))
    return rows


def render(rows: list[TopoBenchRow]) -> str:
    return "\n".join(
        [
            render_table(rows),
            render_drc_table(rows),
            render_stage_table(rows),
            render_verdicts(rows),
        ]
    )


__all__ = [
    "TOPO_EFFORT",
    "TOPO_ROUTE_MODE",
    "PipeSide",
    "TopoBenchRow",
    "TopoStage",
    "bench_esp32c3",
    "bench_ewod",
    "bench_json",
    "bench_nano",
    "bench_snapshot",
    "overall",
    "power_net_names",
    "render",
    "render_drc_table",
    "render_stage_table",
    "render_table",
    "render_verdicts",
    "run_bench",
    "snapshot_fixture",
    "verdicts",
]

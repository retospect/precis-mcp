"""The PCB power sanity boards: :mod:`precis.pcb.sanity_board`'s sibling for
current-carrying nets, in the same pure in-memory style (no store, no DB).

- ``p1`` — "power cross": 100 x 100 mm, four layers, a 2-pin VIN/GND input
  connector on the left edge and a 2-pin VOUT/GND load connector on the
  right edge (VIN and VOUT are one net, 5 A), six 0805 decoupling caps in a
  row between that rail and GND.
- ``p3`` — ``p1`` plus the 20 crossing signal nets of the signal sanity
  board (four fixed 10-pin headers, straight 1:1).

Every part is fixed: the router is the only thing under test. Stackup is
the ``4l-two-sided`` preset (F/B GND fill, In1 a GND plane, In2 the
derived power region layer), with the plane assignments and power regions applied the way
the route job applies them.

``check`` returns the numbers per assertion; the board is clean when every
``ok`` is true. Iterate: ``uv run python -m precis.pcb.bench power [p1|p3]``.
"""

from __future__ import annotations

import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from precis.pcb import STACKUP_PRESETS
from precis.pcb import realize as pcb_realize
from precis.pcb import sanity_board as sb
from precis.pcb import session as pcb_session
from precis.pcb.ir import pin_point
from precis.pcb.rules import ipc2221_track_width_mm
from precis.workers.job_types.pcb_route import _graph_net_rules

PRESET = "4l-two-sided"
CURRENT_A = 5.0
CONN_FOOTPRINT = "CONN-SMT-1x2"
CAP_FOOTPRINT = "C0805-SANITY"
CAPS = 6
CAP_ROW_Y_MM = 88.0
CAP_PITCH_MM = 6.0
RAIL = "VIN"
GND = "GND"
#: Float slack on "width >= derived width": the router rounds to 4 places.
WIDTH_TOL_MM = 1e-3


@dataclass(frozen=True)
class PowerSpec:
    name: str
    #: Add the 20 crossing signal nets of the signal sanity board.
    signals: bool = False
    #: ``"cross"`` (P1/P3) or ``"split"`` (P4: two rails share In2).
    kind: str = "cross"


SPECS: dict[str, PowerSpec] = {
    s.name: s
    for s in (
        PowerSpec("p1"),
        PowerSpec("p3", signals=True),
        PowerSpec("p4", kind="split"),
    )
}

#: P4: the two rails that split In2, left rail first.
SPLIT_LEFT = "3V3"
SPLIT_RIGHT = "5V"
SPLIT_CURRENT_A = 2.0
STRADDLE_FOOTPRINT = "CONN-SMT-1x3"


def conn_footprint(pins: int = 2) -> dict[str, Any]:
    """``pins`` 3 x 3 mm SMT pads 5.08 mm apart along local y, centred."""
    pads = [
        {
            "number": str(n),
            "x": 0.0,
            "y": round((n - (pins + 1) / 2.0) * 5.08, 4),
            "w": 3.0,
            "h": 3.0,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
            "shape": "rect",
        }
        for n in range(1, pins + 1)
    ]
    return {
        "pads": pads,
        "pin_map": {str(n): {"name": str(n), "tags": []} for n in range(1, pins + 1)},
    }


def cap_footprint() -> dict[str, Any]:
    """0805: two 1.0 x 1.45 mm pads 1.9 mm apart along local x."""
    pads = [
        {
            "number": str(n),
            "x": round((n - 1.5) * 1.9, 4),
            "y": 0.0,
            "w": 1.0,
            "h": 1.45,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
            "shape": "rect",
        }
        for n in (1, 2)
    ]
    return {
        "pads": pads,
        "pin_map": {"1": {"name": "1", "tags": []}, "2": {"name": "2", "tags": []}},
    }


def _inst(refdes: str, lcsc: str, x: float, y: float, rot: float) -> dict[str, Any]:
    return {
        "refdes": refdes,
        "part_lcsc": lcsc,
        "x": x,
        "y": y,
        "rot": rot,
        "layer": "top",
        "fixed": "both",
    }


def _power_nets(rails: dict[str, list[tuple[str, str]]], amps: float) -> list[dict]:
    return [
        {
            "name": name,
            "net_class": "power",
            "domain": "electrical",
            "est_current_a": amps,
            "members": [{"refdes": r, "pin": p} for r, p in members],
        }
        for name, members in rails.items()
    ]


def build_graph_split(spec: PowerSpec) -> dict[str, Any]:
    """P4: 3V3 feeds the left half, 5V the right, GND is In1. Two 2-pin
    loads per side, plus one 3-pin load (3V3 / GND / 5V) on the split."""
    mid = sb.BOARD_MM / 2.0
    instances = [
        _inst("J_L", CONN_FOOTPRINT, 8.0, mid, 0.0),
        _inst("J_R", CONN_FOOTPRINT, sb.BOARD_MM - 8.0, mid, 0.0),
        _inst("LA", CONN_FOOTPRINT, 26.0, 28.0, 0.0),
        _inst("LB", CONN_FOOTPRINT, 26.0, 72.0, 0.0),
        _inst("RA", CONN_FOOTPRINT, 74.0, 28.0, 0.0),
        _inst("RB", CONN_FOOTPRINT, 74.0, 72.0, 0.0),
        _inst("X", STRADDLE_FOOTPRINT, mid, mid, 90.0),
    ]
    left, right = ["J_L", "LA", "LB"], ["J_R", "RA", "RB"]
    nets = _power_nets(
        {
            SPLIT_LEFT: [(r, "1") for r in left] + [("X", "1")],
            SPLIT_RIGHT: [(r, "1") for r in right] + [("X", "3")],
            GND: [(r, "2") for r in left + right] + [("X", "2")],
        },
        SPLIT_CURRENT_A,
    )
    return {
        "instances": instances,
        "nets": nets,
        "net_classes": {},
        "board": {"stackup": [dict(e) for e in STACKUP_PRESETS[PRESET]]},
    }


def build_graph(spec: PowerSpec) -> dict[str, Any]:
    """A ``Store.pcb_graph``-shaped dict, no DB."""
    if spec.kind == "split":
        return build_graph_split(spec)
    mid = sb.BOARD_MM / 2.0
    instances = [
        _inst("J_IN", CONN_FOOTPRINT, 8.0, mid, 0.0),
        _inst("J_OUT", CONN_FOOTPRINT, sb.BOARD_MM - 8.0, mid, 0.0),
    ]
    x0 = mid - (CAPS - 1) * CAP_PITCH_MM / 2.0
    for i in range(CAPS):
        instances.append(
            _inst(f"C{i + 1}", CAP_FOOTPRINT, x0 + i * CAP_PITCH_MM, CAP_ROW_Y_MM, 0.0)
        )
    caps = [r["refdes"] for r in instances if r["refdes"].startswith("C")]
    rail = [("J_IN", "1"), ("J_OUT", "1")] + [(c, "1") for c in caps]
    gnd = [("J_IN", "2"), ("J_OUT", "2")] + [(c, "2") for c in caps]
    nets = [
        {
            "name": name,
            "net_class": "power",
            "domain": "electrical",
            "est_current_a": CURRENT_A,
            "members": [{"refdes": r, "pin": p} for r, p in members],
        }
        for name, members in ((RAIL, rail), (GND, gnd))
    ]
    graph = {
        "instances": instances,
        "nets": nets,
        "net_classes": {},
        "board": {"stackup": [dict(e) for e in STACKUP_PRESETS[PRESET]]},
    }
    if spec.signals:
        sig = sb.build_graph(sb.SPECS["straight"])
        graph["instances"] += sig["instances"]
        graph["nets"] += sig["nets"]
    return graph


@dataclass
class Built:
    spec: PowerSpec
    ir: Any
    graph: dict[str, Any]
    footprints: dict[str, dict[str, Any]]
    config: pcb_realize.RealizeConfig


def build(spec: PowerSpec) -> Built:
    """The IR the route job would build, planes and power regions applied."""
    graph = build_graph(spec)
    by_lcsc = {
        CONN_FOOTPRINT: conn_footprint(),
        CAP_FOOTPRINT: cap_footprint(),
        STRADDLE_FOOTPRINT: conn_footprint(3),
        sb.FOOTPRINT: sb.footprint(),
    }
    ir = pcb_session.build_ir(graph, outline=sb.outline(), footprints_by_lcsc=by_lcsc)
    pcb_session.apply_plane_assignments(ir, [])
    footprints = pcb_session.footprints_by_refdes(ir, by_lcsc)
    fab_caps = pcb_session.board_capability(ir.stackup, None)
    config = pcb_realize.RealizeConfig(fab_caps=fab_caps)
    regions, _notes = pcb_realize.derive_power_regions(ir, config)
    for n, layer, region in regions:
        ir.promote_plane(n, layer, region=region.region, priority=region.priority)
    return Built(spec, ir, graph, footprints, config)


@dataclass(frozen=True)
class PowerResult:
    spec: PowerSpec
    seconds: float
    #: assertion name -> {"ok": bool, ...numbers}
    asserts: dict[str, dict[str, Any]]
    findings: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return all(a["ok"] for a in self.asserts.values())


def route(spec: PowerSpec, built: Built | None = None) -> PowerResult:
    built = built or build(spec)
    ir = built.ir
    t0 = time.monotonic()
    rres = pcb_realize.realize(ir, config=built.config, footprints=built.footprints)
    seconds = time.monotonic() - t0
    findings = pcb_session.routed_drc_findings(
        ir,
        rres,
        capability=built.config.fab_caps,
        footprints=built.footprints,
        outline=sb.outline(),
        net_rules=_graph_net_rules(built.graph, built.config.fab_caps),
    )
    return PowerResult(
        spec=spec,
        seconds=round(seconds, 1),
        asserts=check(built, rres, findings),
        findings=tuple(
            sorted(f"{f.rule}:{f.severity}:{getattr(f, 'where', '')}" for f in findings)
        ),
    )


def check(built: Built, rres: Any, findings: Any) -> dict[str, dict[str, Any]]:
    """The numbers behind each assertion of the power sanity boards."""
    ir = built.ir
    names = {int(n): str(ir.net_name[int(n)]) for n in ir.seg_net}
    nets = len(names)
    unrouted = {names[int(ir.seg_net[s])] for s in rres.unrouted}
    errors: dict[str, int] = defaultdict(int)
    for f in findings:
        if f.severity == "error":
            errors[f.rule] += 1
    pieces = {
        (q.net, q.layer): q.fragments for q in getattr(rres, "pour_quality", ()) or ()
    }
    blocked = sorted(
        names.get(int(r.net_id), str(r.net_id))
        for r in rres.unrouted_reasons
        if r.kind == "plane_drop_blocked"
    )
    layers = [str(e["name"]) for e in ir.stackup]
    shortfalls = pcb_session.power_drop_shortfalls(ir, rres)
    width_short: list[str] = []
    narrowest: dict[str, float] = {}
    geoms = pcb_realize.pad_geometry(ir, built.footprints)
    necks: dict[tuple[float, float], float] = {}
    for pid in range(ir.n_pins):
        pt = pin_point(ir, pid)
        if pt is not None:
            necks[(round(pt[0], 3), round(pt[1], 3))] = min(
                geoms[pid].w_mm, geoms[pid].h_mm
            )
    for t in rres.tracks:
        net = str(ir.net_name[t.net_id])
        cur = next(
            (n.get("est_current_a") for n in built.graph["nets"] if n["name"] == net),
            None,
        )
        if not cur:
            continue
        outer = layers[t.layer] in (layers[0], layers[-1])
        need = ipc2221_track_width_mm(float(cur), layer_is_outer=outer)
        if t.is_dogbone:
            # A dogbone is necked to the pad where the pad is narrower.
            sx, sy = t.segments[0]["start"]
            need = min(need, necks.get((round(sx, 3), round(sy, 3)), need))
        narrowest[net] = min(narrowest.get(net, t.width_mm), t.width_mm)
        if t.width_mm + WIDTH_TOL_MM < need:
            width_short.append(f"{net}@{layers[t.layer]} {t.width_mm:.3f}<{need:.3f}")
    out: dict[str, dict[str, Any]] = {
        "connected": {
            "ok": not unrouted,
            "routed": nets - len(unrouted),
            "nets": nets,
            "unrouted": sorted(unrouted),
        },
        "drc": {"ok": not errors, "errors": dict(errors)},
        "plane_pieces": {
            "ok": bool(pieces) and all(v == 1 for v in pieces.values()),
            "pieces": {f"{n}@{la}": v for (n, la), v in sorted(pieces.items())},
        },
        "plane_drop_blocked": {"ok": not blocked, "nets": blocked},
        "drop_ampacity": {
            "ok": not shortfalls,
            "short": [
                f"{n}@({p[0]:.1f},{p[1]:.1f}) {c:.1f}A<{need:.1f}A"
                for n, p, c, need in shortfalls
            ],
            "drop_vias": len(rres.vias),
        },
        "power_width": {
            "ok": not width_short,
            "short": width_short,
            "narrowest_mm": {k: round(v, 3) for k, v in narrowest.items()},
        },
    }
    if built.spec.kind == "split":
        region_layer = next(
            str(e["name"]) for e in ir.stackup if e.get("power_regions")
        )
        want = {f"{n}@{region_layer}" for n in (SPLIT_LEFT, SPLIT_RIGHT)}
        have = {k for k in out["plane_pieces"]["pieces"] if k in want}
        out["split_regions"] = {
            "ok": have == want,
            "poured": sorted(have),
            "missing": sorted(want - have),
        }
    if built.spec.signals:
        out["signal_on_pour_layer"] = _signal_on_pour_layer(built, rres)
    return out


def _signal_on_pour_layer(built: Built, rres: Any) -> dict[str, Any]:
    """No signal track on a layer another net pours as a plane or power
    region (``plane_net`` / ``power_regions``; the GND ``fill`` of the outer
    layers is cleared around signal copper by design)."""
    ir = built.ir
    layers = [str(e["name"]) for e in ir.stackup]
    poured = {
        i
        for i, e in enumerate(ir.stackup)
        if e.get("plane_net") or e.get("power_regions")
    }
    bad = sorted(
        {
            f"{ir.net_name[t.net_id]}@{layers[t.layer]}"
            for t in rres.tracks
            if t.layer in poured and str(ir.net_name[t.net_id]) not in (RAIL, GND)
        }
    )
    return {
        "ok": not bad,
        "tracks": bad,
        "plane_layers": [layers[i] for i in sorted(poured)],
    }


def report(results: list[PowerResult]) -> str:
    lines = ["| variant | s | " + " | ".join(_cols(results)) + " |"]
    lines.append("|" + "---|" * (len(_cols(results)) + 2))
    for r in results:
        cells = [
            ("ok" if r.asserts[c]["ok"] else "FAIL") if c in r.asserts else "—"
            for c in _cols(results)
        ]
        lines.append(f"| {r.spec.name} | {r.seconds} | " + " | ".join(cells) + " |")
    for r in results:
        for name, a in r.asserts.items():
            detail = {k: v for k, v in a.items() if k != "ok"}
            lines.append(
                f"  {r.spec.name}: {name} {'ok' if a['ok'] else 'FAIL'} {detail}"
            )
        for f in r.findings:
            lines.append(f"  {r.spec.name}: {f}")
    return "\n".join(lines)


def _cols(results: list[PowerResult]) -> list[str]:
    seen: list[str] = []
    for r in results:
        for k in r.asserts:
            if k not in seen:
                seen.append(k)
    return seen


def main(argv: list[str] | None = None) -> int:
    names = (argv if argv is not None else sys.argv[1:]) or list(SPECS)
    unknown = [n for n in names if n not in SPECS]
    if unknown:
        print(f"unknown variant(s) {unknown}; known: {list(SPECS)}", file=sys.stderr)
        return 2
    results = [route(SPECS[n]) for n in names]
    print(report(results))
    return 0 if all(r.clean for r in results) else 1


__all__ = ["SPECS", "PowerSpec", "build", "check", "main", "route"]

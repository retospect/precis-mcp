"""The PCB power sanity boards: :mod:`precis.pcb.sanity_board`'s sibling for
current-carrying nets, in the same pure in-memory style (no store, no DB).

- ``p1`` — "power cross": 100 x 100 mm, four layers, a 2-pin VIN/GND input
  connector on the left edge and a 2-pin VOUT/GND load connector on the
  right edge (VIN and VOUT are one net, 5 A), six 0805 decoupling caps in a
  row between that rail and GND.
- ``p2`` — "regulator hub": a SOT-223 regulator fed 8 A from the input
  connector, its VOUT running 8 A to a 9-pin distribution hub, eight 1 A
  loads on a ring around it. The hub stands in for per-segment current:
  the IR carries ``net_current_a``/``seg_current_a`` per net/segment
  (gr497904), but the IR and DRC have NO net-tie or shorted-pin footprint
  concept, so VOUT and LD1..LD8 are modelled as separate nets that a real
  board would join inside the hub. Nothing here may assert anything that
  depends on VOUT and LDn being separate or joined.
- ``p3`` — ``p1`` plus the 20 crossing signal nets of the signal sanity
  board (four fixed 10-pin headers, straight 1:1).
- ``p4`` — two rails (3V3 left, 5V right) split the In2 region layer; one
  3-pin load straddles the split (gr497905).

Every part is fixed: the router is the only thing under test. Stackup is
the ``4l-two-sided`` preset (F/B GND fill, In1 a GND plane, In2 the
derived power region layer), with the plane assignments and power regions applied the way
the route job applies them.

``check`` returns the numbers per assertion; the board is clean when every
``ok`` is true. Iterate: ``uv run python -m precis.pcb.bench power [p1|p3]``.
"""

from __future__ import annotations

import math
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
from precis.pcb.optimize import OptimizeConfig, optimize
from precis.pcb.rules import ipc2221_track_width_mm, net_current_a_or_none
from precis.workers.job_types.pcb_place import _DEFAULT_ITERS, _PLACE_ONLY_SCHEDULE
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
    #: ``"cross"`` (P1/P3), ``"split"`` (P4: two rails share In2) or
    #: ``"hub"`` (P2: regulator feeding a distribution hub).
    kind: str = "cross"
    #: refdes left unpinned — placed by the anneal before routing.
    free: tuple[str, ...] = ()


SPECS: dict[str, PowerSpec] = {
    s.name: s
    for s in (
        PowerSpec("p1"),
        PowerSpec("p3", signals=True),
        PowerSpec("p4", kind="split"),
    )
}

#: Fixtures a `bench power <name>` accepts but the assert gate does not
#: run yet — p2/p2u land their VOUT region and connect 11/11, but the
#: hub pin's stub still drops to the fab floor next to the regulator's
#: crowded drop-via groups (and p2u's anneal parks U1 past the hub).
#: They move into SPECS when those site-search misses are fixed.
PENDING: dict[str, PowerSpec] = {
    s.name: s
    for s in (
        PowerSpec("p2", kind="hub"),
        PowerSpec("p2u", kind="hub", free=("U1",)),
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


def reg_footprint() -> dict[str, Any]:
    """SOT-223: pads 1-3 are 1.5x2.0 mm at 2.3 mm pitch along local y; pad
    4 is the 3.4x2.0 tab on the other side, mapped to pin 2 (VOUT)."""
    pads = [
        {
            "number": str(n),
            "x": 0.0,
            "y": round((n - 2) * 2.3, 4),
            "w": 1.5,
            "h": 2.0,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
            "shape": "rect",
        }
        for n in (1, 2, 3)
    ]
    pads.append(
        {
            "number": "4",
            "x": 3.0,
            "y": 0.0,
            "w": 3.4,
            "h": 2.0,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
            "shape": "rect",
        }
    )
    return {
        "pads": pads,
        "pin_map": {
            "1": {"name": "1", "tags": []},
            "2": {"name": "2", "tags": ["power_out"]},
            "3": {"name": "3", "tags": ["power_in"]},
            "4": {"name": "2", "tags": []},
        },
    }


def hub_footprint() -> dict[str, Any]:
    """The P2 distribution hub: 9 pads 1.5x1.5 at 2.54 mm pitch along
    local x — pin 1 takes the VOUT trunk, pins 2-9 fan out to LD1..LD8."""
    pads = [
        {
            "number": str(n),
            "x": round((n - 5) * 2.54, 4),
            "y": 0.0,
            "w": 1.5,
            "h": 1.5,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
            "shape": "rect",
        }
        for n in range(1, 10)
    ]
    return {
        "pads": pads,
        "pin_map": {str(n): {"name": str(n), "tags": []} for n in range(1, 10)},
    }


def _inst(
    refdes: str, lcsc: str, x: float, y: float, rot: float, fixed: str | None = "both"
) -> dict[str, Any]:
    return {
        "refdes": refdes,
        "part_lcsc": lcsc,
        "x": x,
        "y": y,
        "rot": rot,
        "layer": "top",
        "fixed": fixed,
    }


def _power_nets(
    rails: dict[str, list[tuple[str, str]]], amps: float | dict[str, float]
) -> list[dict]:
    if not isinstance(amps, dict):
        amps = {name: amps for name in rails}
    return [
        {
            "name": name,
            "net_class": "power",
            "domain": "electrical",
            "est_current_a": amps[name],
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


#: P2: the hub stands in for per-segment current (gr497904). VOUT is the
#: 8 A trunk net (regulator pin 2 → hub pin 1); LD1..LD8 are the 1 A
#: branch nets (hub pins 2-9 → the loads' ring). The IR and DRC have no
#: net-tie or shorted-pin footprint, so the hub's short between VOUT and
#: every LDn is modelled as separate nets rather than joined.
REG_FOOTPRINT = "SOT223-SANITY"
HUB_FOOTPRINT = "HUB-SMT-1x9"
HUB_CURRENT_A = 8.0
BRANCH_CURRENT_A = 1.0
LOADS = 8
HUB_X_MM = 62.0
#: Ring radius — large enough that no load's Voronoi territory walls off
#: the VOUT trunk corridor between the regulator and the hub pin.
LOAD_RING_R_MM = 30.0


def build_graph_hub(spec: PowerSpec) -> dict[str, Any]:
    """P2: J_IN feeds the regulator 8 A; VOUT runs 8 A to the hub; each
    LDn is a 1 A two-pin load on a ring around it. ``spec.free`` leaves
    U1 unpinned — the anneal then places it (between J_IN and the hub)."""
    mid = sb.BOARD_MM / 2.0
    free = set(spec.free)
    instances = [
        _inst("J_IN", CONN_FOOTPRINT, 8.0, mid, 0.0),
        _inst(
            "U1",
            REG_FOOTPRINT,
            # The pinned variant sits between feed and hub; the unpinned
            # one starts far away (bottom edge) so the anneal must move it.
            30.0 if "U1" not in free else sb.BOARD_MM - 8.0,
            mid - 4.0 if "U1" not in free else sb.BOARD_MM - 8.0,
            0.0,
            None if "U1" in free else "both",
        ),
        _inst("HUB", HUB_FOOTPRINT, HUB_X_MM, mid, 0.0),
    ]
    for k in range(LOADS):
        # Half-step offset: no load sits on the J_IN→HUB trunk line, so a
        # VOUT region can form a corridor between the regulator and the hub.
        theta = 2.0 * math.pi * (k + 0.5) / LOADS
        instances.append(
            _inst(
                f"LD{k + 1}",
                CONN_FOOTPRINT,
                HUB_X_MM + LOAD_RING_R_MM * math.cos(theta),
                mid + LOAD_RING_R_MM * math.sin(theta),
                0.0,
            )
        )
    rails: dict[str, list[tuple[str, str]]] = {
        "VIN": [("J_IN", "1"), ("U1", "3")],
        "VOUT": [("U1", "2"), ("HUB", "1")],
        GND: [("J_IN", "2"), ("U1", "1")]
        + [(f"LD{k}", "2") for k in range(1, LOADS + 1)],
    }
    rails.update(
        {f"LD{k}": [("HUB", str(k + 1)), (f"LD{k}", "1")] for k in range(1, LOADS + 1)}
    )
    amps = {name: HUB_CURRENT_A for name in rails}
    amps.update({f"LD{k}": BRANCH_CURRENT_A for k in range(1, LOADS + 1)})
    nets = _power_nets(rails, amps)
    # The regulator's roles ride the members: pin 2 sources VOUT, pin 3
    # draws from VIN (est_current_a stays net-level on this board — the
    # hub nets are the per-segment stand-in, not per-pin declarations).
    for n in nets:
        for m in n["members"]:
            if m["refdes"] == "U1" and m["pin"] == "2":
                m["tags"] = ["power_out"]
            elif m["refdes"] == "U1" and m["pin"] == "3":
                m["tags"] = ["power_in"]
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
    if spec.kind == "hub":
        return build_graph_hub(spec)
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
        REG_FOOTPRINT: reg_footprint(),
        HUB_FOOTPRINT: hub_footprint(),
        sb.FOOTPRINT: sb.footprint(),
    }
    ir = pcb_session.build_ir(graph, outline=sb.outline(), footprints_by_lcsc=by_lcsc)
    if spec.free:
        # Place the unpinned parts before the regions derive — a region's
        # hull is drawn from pin positions, so it must see the settled pose.
        optimize(
            ir,
            OptimizeConfig(iters=_DEFAULT_ITERS, seed=1, schedule=_PLACE_ONLY_SCHEDULE),
        )
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
        cur = net_current_a_or_none(float(ir.seg_current_a[t.seg_id]))
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
    if built.spec.free:
        x_of = {str(r): float(ir.inst_x[i]) for i, r in enumerate(ir.instance_refdes)}
        out["placement_order"] = {
            # The anneal settles the free regulator between its feed and
            # its hub: J_IN.x < U1.x < HUB.x.
            "ok": x_of["J_IN"] < x_of["U1"] < x_of["HUB"],
            "x_mm": {r: round(x_of[r], 2) for r in ("J_IN", "U1", "HUB")},
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
    # A layer marked ``routable`` (the hybrid preset's dual-role In1) is
    # meant to carry traces beside its pour — it is priced, not banned.
    poured = {
        i
        for i, e in enumerate(ir.stackup)
        if (e.get("plane_net") or e.get("power_regions")) and not e.get("routable")
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
    known = {**SPECS, **PENDING}
    unknown = [n for n in names if n not in known]
    if unknown:
        print(f"unknown variant(s) {unknown}; known: {list(known)}", file=sys.stderr)
        return 2
    results = [route(known[n]) for n in names]
    print(report(results))
    return 0 if all(r.clean for r in results) else 1


__all__ = ["SPECS", "PowerSpec", "build", "check", "main", "route"]

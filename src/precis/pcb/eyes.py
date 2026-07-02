"""DRC-lite, proximity, signal-trace, and measure evaluation (ADR 0042 §8).

Pure folds over the graph dict the store hands up
(:meth:`precis.store._pcb_ops.PcbMixin.pcb_graph`):

    {
      "instances":  [{refdes, x, y, layer, roles, label, height_mm, n_pins}],
      "nets":       [{name, net_class, members:[{refdes, pin}]}],
      "unconnected":[{refdes, pin}],
    }
"""

from __future__ import annotations

from typing import Any

from precis.pcb.geom import Point, dist

# Heuristic: a net whose name/class says "power rail" wants a decoupling cap.
_POWER_CLASSES = frozenset({"power", "pwr"})
_CAP_HINTS = ("nf", "uf", "µf", "pf", "cap")

# ── measure direction (ADR 0042 §8.3) ────────────────────────────────
# pcb_measures.direction: min|max|target|keep_above|keep_below. It decides
# which side of `goal` is "ok" — the evaluator AND the placer's penalty must
# agree, so both go through measure_bound().
_LOWER_DIRECTIONS = frozenset({"min", "keep_above"})
_UPPER_DIRECTIONS = frozenset({"max", "keep_below"})
_DEFAULT_BOUND = {"separation": "lower", "proximity": "upper", "height": "upper"}


def measure_bound(direction: str | None, metric: str) -> str:
    """Normalise a stored ``direction`` to ``'lower'`` (value must stay ≥ goal),
    ``'upper'`` (≤ goal) or ``'target'`` (aim at goal). Falls back to the
    metric's natural sense: separation keeps apart (lower), proximity keeps
    close (upper), height is a ceiling (upper)."""
    d = (direction or "").strip().lower()
    if d in _LOWER_DIRECTIONS:
        return "lower"
    if d in _UPPER_DIRECTIONS:
        return "upper"
    if d == "target":
        return "target"
    return _DEFAULT_BOUND.get(metric, "upper")


def _placed(graph: dict[str, Any]) -> dict[str, Point]:
    return {
        i["refdes"]: (float(i["x"]), float(i["y"]))
        for i in graph["instances"]
        if i.get("x") is not None and i.get("y") is not None
    }


def _is_cap(label: str | None) -> bool:
    lo = (label or "").lower()
    return any(h in lo for h in _CAP_HINTS)


def drc_lite(graph: dict[str, Any]) -> list[dict[str, str]]:
    """Basic electrical sanity the LLM should see *before* the router
    (ADR 0042 §8.1). Each finding: ``{severity, code, where, message}``."""
    out: list[dict[str, str]] = []

    # 1. unconnected pins
    for u in graph.get("unconnected") or []:
        out.append(
            {
                "severity": "warn",
                "code": "unconnected-pin",
                "where": f"{u['refdes']}.{u['pin']}",
                "message": "pin is on no net",
            }
        )

    # 2. dangling nets (a single pin — nothing to connect to)
    for net in graph["nets"]:
        n = len(net.get("members") or [])
        if n < 2:
            out.append(
                {
                    "severity": "warn",
                    "code": "dangling-net",
                    "where": net["name"],
                    "message": f"net has {n} pin(s); needs ≥2 to be a connection",
                }
            )

    # 3. power rail without a decoupling cap (needs pin tags / a cap member)
    labels = {i["refdes"]: i.get("label") for i in graph["instances"]}
    for net in graph["nets"]:
        if (net.get("net_class") or "").strip().lower() not in _POWER_CLASSES:
            continue
        members = net.get("members") or []
        if not any(_is_cap(labels.get(m["refdes"])) for m in members):
            out.append(
                {
                    "severity": "warn",
                    "code": "no-bypass-cap",
                    "where": net["name"],
                    "message": "power net has no decoupling capacitor on it",
                }
            )
    return out


def proximity(graph: dict[str, Any], a: str, b: str) -> dict[str, Any]:
    """Centre-to-centre gap between two placed instances (ADR 0042 §8.1).

    v1 reports centroid distance; courtyard-edge gap lands with footprint
    dims (Slice 2)."""
    placed = _placed(graph)
    if a not in placed or b not in placed:
        missing = [r for r in (a, b) if r not in placed]
        raise KeyError(f"unplaced or unknown: {', '.join(missing)}")
    return {"a": a, "b": b, "gap_mm": dist(placed[a], placed[b])}


def _pin_net_index(graph: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """refdes → [(pin, net), …] from the net membership."""
    idx: dict[str, list[tuple[str, str]]] = {}
    for net in graph["nets"]:
        for m in net.get("members") or []:
            idx.setdefault(m["refdes"], []).append((m["pin"], net["name"]))
    return idx


def trace(
    graph: dict[str, Any], start_net: str, *, max_hops: int = 32
) -> dict[str, Any]:
    """Follow a signal from a net, hopping through **2-pin pass-throughs**
    (series R / C / ferrite) onto the next net (ADR 0042 §8.2).

    Returns ``{path:[{net, via}], ends:[...]}``. A multi-pin component (a mux,
    an MCU) is a *terminus* of the automatic walk — the LLM supplies the
    internal hop (datasheet pass-through) for those.
    """
    nets_by_name = {n["name"]: n for n in graph["nets"]}
    if start_net not in nets_by_name:
        raise KeyError(f"unknown net {start_net!r}")
    npins = {i["refdes"]: int(i.get("n_pins") or 0) for i in graph["instances"]}
    pin_net = _pin_net_index(graph)

    path: list[dict[str, str]] = [{"net": start_net, "via": "—"}]
    ends: list[str] = []
    seen_nets = {start_net}
    frontier = [start_net]
    hops = 0
    while frontier and hops < max_hops:
        hops += 1
        net = frontier.pop()
        for m in nets_by_name[net].get("members") or []:
            rd = m["refdes"]
            if npins.get(rd, 0) == 2:
                # series pass-through: hop to the other pin's net
                others = [nt for pn, nt in pin_net.get(rd, []) if nt != net]
                for nxt in others:
                    if nxt not in seen_nets and nxt in nets_by_name:
                        seen_nets.add(nxt)
                        path.append({"net": nxt, "via": rd})
                        frontier.append(nxt)
            else:
                ends.append(f"{rd}.{m['pin']}")
    return {"path": path, "ends": sorted(set(ends))}


#: Relative tolerance for direction='target' verdicts (±10% of goal, with a
#: 0.1 mm floor so a goal of 0 doesn't demand exact equality).
_TARGET_REL_TOL = 0.10
_TARGET_ABS_TOL = 0.1


def _judge(values: list[float], bound: str, goal: Any) -> tuple[float, bool]:
    """(binding value, ok) for a measure over ``values`` under ``bound``.

    ``lower``: every value must stay ≥ goal → the binding value is the min.
    ``upper``: every value must stay ≤ goal → the binding value is the max.
    ``target``: aim at goal → the binding value is the one furthest from it,
    ok within ±10% of goal (0.1 mm floor). No goal → always ok."""
    if goal is None:
        return (min(values) if bound == "lower" else max(values)), True
    g = float(goal)
    if bound == "lower":
        val = min(values)
        return val, val >= g
    if bound == "upper":
        val = max(values)
        return val, val <= g
    val = max(values, key=lambda v: abs(v - g))
    return val, abs(val - g) <= max(_TARGET_REL_TOL * abs(g), _TARGET_ABS_TOL)


def evaluate_measures(
    graph: dict[str, Any], measures: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Evaluate stored measures against the current placement (ADR 0042 §8.3).

    v1 covers the placement-geometry metrics — ``separation``, ``proximity``
    (pairwise gaps), ``height`` — over operands that name instances or roles.
    A stored ``direction`` (min|max|target|keep_above|keep_below) picks which
    side of ``goal`` is ok (:func:`measure_bound`); without one each metric
    keeps its natural sense. The connectivity metrics (parallelism /
    supply-path / topology / plane-continuity) are stored and reported as
    ``pending`` until their evaluators land.
    """
    placed = _placed(graph)
    roles = {i["refdes"]: set(i.get("roles") or []) for i in graph["instances"]}

    def _resolve(operands: list[dict[str, Any]]) -> list[str]:
        """operands → concrete placed refdes list (instances + role classes)."""
        out: list[str] = []
        for op in operands or []:
            if "instance" in op and op["instance"] in placed:
                out.append(op["instance"])
            elif "role" in op:
                out += [
                    r for r, rs in roles.items() if op["role"] in rs and r in placed
                ]
        return list(dict.fromkeys(out))

    results: list[dict[str, Any]] = []
    for m in measures:
        metric = (m.get("metric") or "").lower()
        refs = _resolve(m.get("operands") or [])
        goal = m.get("goal")
        strength = m.get("strength") or "gauge"
        row: dict[str, Any] = {
            "metric": metric,
            "strength": strength,
            "goal": goal,
            "reason": m.get("reason") or "",
            "value": None,
            "verdict": "—",
        }
        bound = measure_bound(m.get("direction"), metric)
        if metric in ("separation", "proximity") and len(refs) >= 2:
            pairs = [
                dist(placed[refs[i]], placed[refs[j]])
                for i in range(len(refs))
                for j in range(i + 1, len(refs))
            ]
            val, ok = _judge(pairs, bound, goal)
            row["value"] = round(val, 3)
            row["verdict"] = "ok" if ok else "VIOLATED"
            row["over"] = ", ".join(refs)
        elif metric == "height" and refs:
            heights = [
                float(i.get("height_mm") or 0.0)
                for i in graph["instances"]
                if i["refdes"] in refs
            ]
            val, ok = _judge(heights, bound, goal)
            row["value"] = round(val, 3)
            row["verdict"] = "ok" if ok else "VIOLATED"
        else:
            row["verdict"] = "pending"  # connectivity metrics / unresolved operands
        results.append(row)
    return results

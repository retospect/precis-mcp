"""Ratsnest/crossings, proximity, signal-trace, and measure evaluation.

Pure folds over the graph dict the store hands up
(:meth:`precis.store._pcb_ops.PcbMixin.pcb_graph`):

    {
      "instances":  [{refdes, x, y, layer, roles, label, height_mm, n_pins}],
      "nets":       [{name, net_class, members:[{refdes, pin}]}],
      "unconnected":[{refdes, pin}],
    }

``drc_lite`` is RETIRED (pcb-guided-place-route Slice 8) — ``view='drc'``
is now backed by :mod:`precis.pcb.drc` (geometric DRC on realized copper,
L5); the graph-shape half of what ``drc_lite`` checked (unconnected pins,
dangling nets) lives in :mod:`precis.pcb.ir`'s graph-feasibility functions
instead. This module also inspects pinout without changing the graph. It
walks stored physical pads, not synthesized IR pins: duplicate and unclaimed
lands are real, while inferred bounds cannot prove connector numbering.
Positions reuse padplace's transform; pin-to-pad and footprint names remain
distinct mapping evidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from precis.pcb import padplace
from precis.pcb.geom import Point, dist


def _pinout_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def pinout(
    instance: dict[str, Any],
    footprint: dict[str, Any],
    pins: list[dict[str, Any]],
    layers: list[str],
) -> dict[str, Any]:
    """One row per stored physical pad, with independent mapping evidence.

    Missing placement never becomes an origin; duplicate IDs never become
    one physical row. No input is changed or missing geometry inferred.
    """
    pads = footprint.get("pads") or []
    pin_map = footprint.get("pin_map") or {}
    placed = all(_pinout_number(instance.get(k)) is not None for k in ("x", "y"))
    placed = placed and _pinout_number(instance.get("rot", 0) or 0) is not None
    bottom = padplace.is_bottom_instance(instance)
    duplicates: dict[str, list[int]] = {}
    for index, raw in enumerate(pads, 1):
        if isinstance(raw, dict) and raw.get("number") is not None:
            duplicates.setdefault(str(raw["number"]), []).append(index)
    rows = []
    matched: set[str] = set()
    for index, raw in enumerate(pads, 1):
        pad = raw if isinstance(raw, dict) else {}
        number = str(pad["number"]) if pad.get("number") is not None else None
        entry = pin_map.get(number)
        named = (
            isinstance(entry, dict)
            and entry.get("name") is not None
            and str(entry["name"]) != number
        )
        label = padplace.pad_label(pad, pin_map) if number is not None else None
        bindings: dict[str, dict[str, Any]] = {}
        notes = []
        for pin in pins:
            name = str(pin["pin"])
            explicit = str(pin["pad"]) if pin.get("pad") is not None else None
            sources = []
            if number is not None and explicit == number:
                sources.append("explicit-pin-pad")
                if named and name != label:
                    notes.append(
                        f"explicit {name} disagrees with footprint name {label}"
                    )
            if label is not None and name == label:
                sources.append("footprint-pin-map" if named else "pad-number-identity")
                if explicit is not None and explicit != number:
                    notes.append(
                        f"{name} explicitly binds pad {explicit}, not {number}"
                    )
            if not sources:
                continue
            matched.add(name)
            binding = bindings.setdefault(
                name, {"pin": name, "nets": [], "sources": []}
            )
            binding["sources"] = sorted(set(binding["sources"] + sources))
            if pin.get("net") is not None:
                binding["nets"] = sorted(set(binding["nets"] + [str(pin["net"])]))
        candidates = [bindings[k] for k in sorted(bindings)]
        nets = sorted({net for b in candidates for net in b["nets"]})
        state = (
            "ambiguous"
            if notes or len(candidates) > 1 or len(nets) > 1
            else "connected"
            if nets
            else "unconnected"
            if candidates
            else "unclaimed"
        )
        lx, ly = _pinout_number(pad.get("x")), _pinout_number(pad.get("y"))
        valid = bool(number) and lx is not None and ly is not None
        bx = by = None
        if valid and placed:
            bx, by = padplace.place_pad_point(pad, instance)
            if not math.isfinite(bx) or not math.isfinite(by):
                bx = by = None
        if not valid:
            notes.append("missing pad number or missing/nonfinite local center")
        layer = str(pad.get("layer") or "F.Cu")
        board_layers = (
            list(layers)
            if pad.get("drill")
            else [padplace._effective_layer(layer, bottom=bottom)]
        )
        rows.append(
            {
                "pad_index": index,
                "pad_number": number,
                "local_x_mm": lx,
                "local_y_mm": ly,
                "board_x_mm": bx,
                "board_y_mm": by,
                "pad_layer": layer,
                "board_layers": board_layers,
                "pad_rotation_deg": _pinout_number(pad.get("rot", 0) or 0),
                "footprint_pin": label,
                "pin_names": sorted(bindings),
                "net_names": nets,
                "mapping_sources": candidates,
                "mapping_state": state,
                "duplicate_indices": duplicates.get(number or "", [])
                if len(duplicates.get(number or "", [])) > 1
                else [],
                "notes": sorted(set(notes)),
                "geometry": "available" if valid else "invalid_geometry",
            }
        )
    numbers = set(duplicates)
    unmatched = [
        {
            "pin": str(p["pin"]),
            "pad": p.get("pad"),
            "net": p.get("net"),
            "reason": "explicit pad has no geometry"
            if p.get("pad") is not None and str(p["pad"]) not in numbers
            else "no physical pad mapping",
        }
        for p in pins
        if str(p["pin"]) not in matched
        or (p.get("pad") is not None and str(p["pad"]) not in numbers)
    ]
    return {"rows": rows, "unmatched": unmatched, "placed": placed}


def pinout_preview(
    instance: dict[str, Any],
    footprint: dict[str, Any],
    pins: list[dict[str, Any]],
    layers: list[str],
    proposals: list[dict[str, Any]],
) -> dict[str, Any]:
    """Overlay unsaved explicit labels without replacing stored evidence.

    Conflicting drafts are inspectable, not canonical electrical identities.
    Stored pads/placement and mapping still use the ordinary pinout fold.
    """
    result = pinout(instance, footprint, pins, layers)
    by_name: dict[str, set[str | None]] = {}
    by_pad: dict[str, set[str]] = {}
    for proposal in proposals:
        name, pad = proposal["name"], proposal["pad"]
        by_name.setdefault(name, set()).add(pad)
        if pad is not None:
            by_pad.setdefault(pad, set()).add(name)
    entries = []
    numbers = {row["pad_number"] for row in result["rows"]}
    for index, proposal in enumerate(proposals, 1):
        name, pad = proposal["name"], proposal["pad"]
        conflicting = len(by_name[name]) > 1 or len(by_pad.get(pad, set())) > 1
        state = (
            "conflicting"
            if conflicting
            else "unknown"
            if pad is None
            else "unmatched"
            if pad not in numbers
            else "proposed"
        )
        notes = []
        if conflicting:
            notes.append(
                "canonical authoring would refuse; use one canonical name/pad "
                "in pins and connections or correct the pad number"
            )
        if pad is None:
            notes.append("pad binding unknown; supply an explicit pad ID")
        elif pad not in numbers:
            notes.append("no stored physical pad with this ID; correct the pad number")
        entries.append(
            {
                "entry_index": index,
                "name": name,
                "pad": pad,
                "provenance": "proposed-explicit",
                "state": state,
                "notes": notes,
            }
        )
    for row in result["rows"]:
        matches = [
            e for e in entries if e["pad"] is not None and e["pad"] == row["pad_number"]
        ]
        row["proposed_names"] = sorted({e["name"] for e in matches})
        row["proposal_indices"] = [e["entry_index"] for e in matches]
        row["proposal_provenance"] = "proposed-explicit" if matches else "none"
        row["proposal_state"] = (
            "conflicting"
            if any(e["state"] == "conflicting" for e in matches)
            else "repeated"
            if len(matches) > 1
            else "proposed"
            if matches
            else "not-proposed"
        )
        row_notes = {note for e in matches for note in e["notes"]}
        if matches and any(e["name"] != row["footprint_pin"] for e in matches):
            row_notes.add(
                "proposed label differs from stored footprint naming; no rebinding"
            )
        if matches and set(row["proposed_names"]) != set(row["pin_names"]):
            row_notes.add(
                "proposed labels differ from stored pin evidence; no rebinding"
            )
        row["proposal_notes"] = sorted(row_notes)
    result["proposals"] = entries
    return result


# ── measure direction ────────────────────────────────
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


# ── align measure ────────────────────────────────────
# `align`: pos_b - pos_a == offset on the constrained axes, within a
# tolerance. The evaluator (here), the annealer (optimize.py) and the quick
# placer (place.py) all parse one measure row through parse_align() and score
# it through align_residual(), so the three cannot disagree about what a row
# means.
ALIGN_DEFAULT_TOL_MM = 0.05
_ALIGN_AXES = frozenset({"xy", "x", "y"})


@dataclass(frozen=True, slots=True)
class AlignSpec:
    """One resolved `align` row. ``ref_b`` names a second part; otherwise
    ``datum`` is a fixed absolute point (a ``point`` operand, or a
    ``feature_id`` already bound to its feature's x/y)."""

    ref_a: str
    ref_b: str | None
    datum: Point | None
    offset: Point
    axis: str
    tol_mm: float


def bind_feature_operands(
    measures: list[dict[str, Any]], features: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Rewrite every ``{"feature_id": n}`` operand to ``{"point": [x, y]}``
    from ``features`` (features never move, so the lookup is a constant).
    An unknown id — or a feature with no x/y — is left as ``feature_id`` so
    :func:`parse_align` reports it unresolved. Returns ``measures`` itself
    when there is nothing to bind."""
    by_id = {
        int(f["feature_id"]): (float(f["x"]), float(f["y"]))
        for f in features or []
        if f.get("feature_id") is not None
        and f.get("x") is not None
        and f.get("y") is not None
    }
    out: list[dict[str, Any]] = []
    changed = False
    for m in measures:
        ops = m.get("operands") or []
        if not any(isinstance(o, dict) and "feature_id" in o for o in ops):
            out.append(m)
            continue
        new_ops: list[Any] = []
        for o in ops:
            if isinstance(o, dict) and "feature_id" in o:
                try:
                    pt = by_id.get(int(o["feature_id"]))
                except (TypeError, ValueError):
                    pt = None
                if pt is not None:
                    o = {k: v for k, v in o.items() if k != "feature_id"}
                    o["point"] = [pt[0], pt[1]]
            new_ops.append(o)
        out.append({**m, "operands": new_ops})
        changed = True
    return out if changed else measures


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _operand(op: Any) -> tuple[str, Any] | None:
    """One align operand -> ``("inst", refdes)`` / ``("datum", (x, y))``;
    ``None`` when it does not resolve (a ``feature_id`` reaching here was
    never bound — an unknown id; ``role`` operands are unsupported)."""
    if not isinstance(op, dict):
        return None
    ref = op.get("instance")
    if isinstance(ref, str) and ref:
        return ("inst", ref)
    pt = op.get("point")
    if isinstance(pt, (list, tuple)) and len(pt) == 2:
        x, y = _num(pt[0]), _num(pt[1])
        if x is not None and y is not None:
            return ("datum", (x, y))
    return None


def parse_align(m: dict[str, Any]) -> AlignSpec | None:
    """An `align` measure row -> :class:`AlignSpec`, or ``None`` when it does
    not resolve: not exactly two operands, an operand that is not an
    ``instance`` / ``point`` / (bound) ``feature_id``, two datums (nothing
    to move), an instance aligned to itself, a malformed ``meta`` axis /
    offset, or a non-numeric or negative goal.

    ``axis`` (``x|y|xy``) and ``offset`` (``[dx, dy]`` mm) live in the row's
    ``meta``; the operands keep the proximity shape. The offset is operand 2
    relative to operand 1 (``pos_2 - pos_1 == offset``), so swapping the two
    operands means negating the offset — a zero-offset align is symmetric.
    The returned spec is normalised so ``ref_a`` is always an instance: a
    ``[datum, instance]`` row comes back as ``(instance, datum)`` with the
    offset negated, which has the identical residual magnitude."""
    ops = m.get("operands") or []
    if len(ops) != 2:
        return None
    first, second = _operand(ops[0]), _operand(ops[1])
    if first is None or second is None:
        return None
    meta = m.get("meta") or {}
    if not isinstance(meta, dict):
        return None
    axis = str(meta.get("axis") or "xy").strip().lower()
    if axis not in _ALIGN_AXES:
        return None
    off = meta.get("offset")
    ox = oy = 0.0
    if off is not None:
        if not isinstance(off, (list, tuple)) or len(off) != 2:
            return None
        vx, vy = _num(off[0]), _num(off[1])
        if vx is None or vy is None:
            return None
        ox, oy = vx, vy
    goal = m.get("goal")
    tol = ALIGN_DEFAULT_TOL_MM if goal is None else _num(goal)
    if tol is None or tol < 0:
        return None
    if first[0] == "inst" and second[0] == "inst":
        if first[1] == second[1]:
            return None
        return AlignSpec(first[1], second[1], None, (ox, oy), axis, tol)
    if first[0] == "inst":  # (instance, datum)
        return AlignSpec(first[1], None, second[1], (ox, oy), axis, tol)
    if second[0] == "inst":  # (datum, instance): normalise, negate the offset
        return AlignSpec(second[1], None, first[1], (-ox, -oy), axis, tol)
    return None  # two datums


def align_residual(pa: Point, pb: Point, offset: Point, axis: str) -> float:
    """Euclidean norm of ``pb - pa - offset`` over the constrained axes."""
    rx = pb[0] - pa[0] - offset[0]
    ry = pb[1] - pa[1] - offset[1]
    if axis == "x":
        return abs(rx)
    if axis == "y":
        return abs(ry)
    return math.hypot(rx, ry)


def proximity(graph: dict[str, Any], a: str, b: str) -> dict[str, Any]:
    """Centre-to-centre gap between two placed instances.

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
    (series R / C / ferrite) onto the next net.

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
    graph: dict[str, Any],
    measures: list[dict[str, Any]],
    features: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate stored measures against the current placement.

    v1 covers the placement-geometry metrics — ``separation``, ``proximity``
    (pairwise gaps), ``height`` — over operands that name instances or roles.
    A stored ``direction`` (min|max|target|keep_above|keep_below) picks which
    side of ``goal`` is ok (:func:`measure_bound`); without one each metric
    keeps its natural sense. The connectivity metrics (parallelism /
    supply-path / topology / plane-continuity) are stored and reported as
    ``pending`` until their evaluators land. ``align`` (a part and a part /
    ``point`` / ``feature_id``) reads ``features`` — the
    ``Store.pcb_features_list`` rows — to resolve a ``feature_id`` operand;
    the graph itself carries none.
    """
    measures = bind_feature_operands(measures, features)
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
        elif metric == "align":
            spec = parse_align(m)
            pa = placed.get(spec.ref_a) if spec is not None else None
            pb = None
            over = ""
            if spec is not None:
                if spec.ref_b is not None:
                    pb = placed.get(spec.ref_b)
                    over = f"{spec.ref_a}, {spec.ref_b}"
                elif spec.datum is not None:
                    pb = spec.datum
                    over = f"{spec.ref_a}, ({spec.datum[0]:g}, {spec.datum[1]:g})"
            if spec is None or pa is None or pb is None:
                row["verdict"] = "pending"  # unresolved operand: never ok
            else:
                val = align_residual(pa, pb, spec.offset, spec.axis)
                row["goal"] = spec.tol_mm
                row["value"] = round(val, 3)
                row["verdict"] = "ok" if val <= spec.tol_mm else "VIOLATED"
                row["over"] = over
                # `meta.snapped` is stamped by the place/route job when the
                # optimizer's deterministic snap pass closed the last
                # sub-step residual (optimize.py); only an `ok` row can
                # claim it — a snap the placement has since drifted from
                # is not one.
                if row["verdict"] == "ok" and (m.get("meta") or {}).get("snapped"):
                    row["detail"] = "snapped"
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

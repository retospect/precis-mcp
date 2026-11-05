"""Stored transition observables for the reaction explorer.

Re-derive the engine's physical-name join here, without importing its runtime.
Keep provenance per quantity: a v2 headline does not turn the accompanying
chain rates into mean-field rates. Ambiguous physical joins stay absent.
X_RC uses the shared complete-map sum rule and its recorded source, never
an inference from turnover or mean-field solve status.
Arrhenius fits use aggregate turnover at fixed pressure and composition:
sweeps do not store per-step rates, so their fitted parameters are apparent
whole-network values and require at least three positive-rate temperatures.
"""

from __future__ import annotations

import json
import math
from typing import Any

from precis.utils.pathway_rate_control import rate_control_note, usable_rate_control
from precis_web.pathway_kinetics import _mf_reading

_KB = 8.617333262145e-5  # eV/K


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else None
    return None


def _physical(value: Any) -> str:
    return str(value).split("~", 1)[0]


def _pair(name: str) -> tuple[str, str] | None:
    if "->" not in name:
        return None
    a, b = name.split("->", 1)
    return _physical(a), _physical(b)


def _transition(link: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    transitions = record.get("transitions")
    if not isinstance(transitions, list):
        return {}
    exact = f"{link['source']}->{link['target']}"
    pair = (_physical(link["source"]), _physical(link["target"]))
    matches = []
    for row in transitions:
        if not isinstance(row, dict):
            continue
        if row.get("name") == exact:
            return row
        if row.get("from") is not None and row.get("to") is not None:
            if (_physical(row["from"]), _physical(row["to"])) == pair:
                matches.append(row)
    return matches[0] if len(matches) == 1 else {}


def _named(table: dict[str, Any], name: str) -> Any:
    if name in table:
        return table[name]
    pair = _pair(name)
    matches = [value for key, value in table.items() if pair and _pair(key) == pair]
    return matches[0] if len(matches) == 1 else None


def _environment(record: dict[str, Any]) -> dict[str, Any]:
    env = _mapping(record.get("environment"))
    if env:
        return env
    cond = _mapping(record.get("conditions"))
    return {**cond, "T": cond.get("T", cond.get("temperature"))} if cond else {}


def _arrhenius(
    results: dict[str, Any], temperature: float | None
) -> list[dict[str, Any]]:
    sweep = _mapping(results.get("kinetics_sweep"))
    points = sweep.get("points")
    if not isinstance(points, list):
        return []
    groups: dict[str, dict[str, Any]] = {}
    for point in points:
        if not isinstance(point, dict):
            continue
        t = _number(point.get("T"))
        turnover = _number(point.get("tof_mf"))
        if t is None or t <= 0 or turnover is None or turnover <= 0:
            continue
        env = {
            k: v
            for k, v in _environment(point).items()
            if k not in {"T", "temperature"}
        }
        for key in ("p_total", "pressures", "composition"):
            if key in point:
                env[key] = point[key]
        key = json.dumps(env, sort_keys=True)
        group = groups.setdefault(
            key, {"environment": env, "solve": "v2 mean-field", "points": []}
        )
        group["points"].append({"T": t, "turnover": turnover})
    if not groups:
        return []
    group = max(groups.values(), key=lambda g: len({p["T"] for p in g["points"]}))
    rows = sorted(group["points"], key=lambda p: p["T"])
    if len({p["T"] for p in rows}) < 3:
        return []
    xs, ys = [1 / p["T"] for p in rows], [math.log(p["turnover"]) for p in rows]
    xmean, ymean = sum(xs) / len(xs), sum(ys) / len(ys)
    slope = sum((x - xmean) * (y - ymean) for x, y in zip(xs, ys, strict=True)) / sum(
        (x - xmean) ** 2 for x in xs
    )
    intercept = ymean - slope * xmean
    reads = []
    if temperature is not None:
        for t in (temperature - 10, temperature + 10):
            if rows[0]["T"] <= t <= rows[-1]["T"]:
                exponent = intercept + slope / t
                if -745 < exponent < 709:
                    reads.append({"T": t, "turnover": math.exp(exponent)})
    return [
        {
            **group,
            "points": rows,
            "prefactor": math.exp(intercept) if -745 < intercept < 709 else None,
            "ea_eV": -slope * _KB,
            "read_off": reads,
        }
    ]


def attach_step_kinetics(links: list[dict[str, Any]], results: dict[str, Any]) -> None:
    """Attach nullable observables, never rates inferred from graph energies."""
    v1 = _mapping(results.get("kinetics"))
    v2 = _mapping(results.get("kinetics_v2"))
    env1, env2 = _environment(v1), _environment(v2)
    chain_drc = _mapping(_mapping(v1.get("drc")).get("X_RC"))
    drc_is_v2 = isinstance(v2.get("drc"), dict)
    drc = _mapping(v2.get("drc")) if drc_is_v2 else chain_drc
    drc_source = (v2 if drc_is_v2 else v1).get("drc_source")
    drc_usable = usable_rate_control(drc)
    drc_note = rate_control_note(drc_source, drc)
    failure, _, failed_observables, _, _ = _mf_reading(v2)
    mf_failed = failure is not None or (
        "tof_mf" in v2 and _number(v2["tof_mf"]) is None
    )
    failed = {item["name"] for item in failed_observables}
    listed = v2.get("failed_observables")
    if isinstance(listed, list):
        failed.update(
            str(item.get("name")) for item in listed if isinstance(item, dict)
        )

    def v2_solve(quantity: str) -> str:
        return "v1 chain" if mf_failed or quantity in failed else "v2 mean-field"

    arrhenius = _arrhenius(results, _number((env2 or env1).get("T")))
    for link in links:
        row = _transition(link, v1)
        name = str(row.get("name") or f"{link['source']}->{link['target']}")
        quantities: dict[str, Any] = {}

        def add(
            key: str,
            raw: Any,
            solve: str,
            env: dict[str, Any],
            values: dict[str, Any] = quantities,
        ) -> None:
            value = _number(raw)
            if value is not None:
                values[key] = {"value": value, "solve": solve, "environment": env}

        for key in ("k_f", "k_b", "net_rate"):
            add(key, row.get(key), "v1 chain", env1)
        barrier, dg = _number(row.get("barrier_eV")), _number(row.get("dG_eV"))
        # The rate builder floors ONE TS to both endpoints. A raw barrier
        # can be below the final state; subtracting dG before the floor
        # would invent a negative reverse activation energy.
        if barrier is not None and dg is not None:
            forward = max(barrier, dg, 0.0)
            add("ea_forward", forward, "v1 chain", env1)
            add("ea_reverse", forward - dg, "v1 chain", env1)
        if drc_usable:
            add(
                "X_RC",
                _named(drc, name),
                # gr564356: warm re-solves can store X_RC near 1 on a fast step.
                f"{drc_note} Under audit (gr564356)."
                if drc_source == "mf"
                else drc_note,
                env2 if drc_is_v2 else env1,
            )
        rev = _mapping(v2.get("reversibility"))
        add(
            "z_i",
            _named(rev or _mapping(v1.get("reversibility")), name),
            v2_solve("reversibility") if rev else "v1 chain",
            env2 if rev else env1,
        )
        product = row.get("gas")
        desorption = "desorption" in (
            row.get("kind"),
            link.get("link_type"),
            link.get("kind"),
        )
        if desorption and not product:
            removed = _mapping(link.get("removed"))
            product = next(iter(removed)) if len(removed) == 1 else None
        if desorption and product:
            selectivity = _mapping(v2.get("selectivity"))
            if product in selectivity:
                add("selectivity", selectivity[product], v2_solve("selectivity"), env2)
            elif product == v1.get("product"):
                add("selectivity", v1.get("selectivity"), "v1 chain", env1)
        source = link.get("barrier_source", "none")
        # The solve can have rejected a measured barrier and used BEP.
        note = str(row.get("note") or "")
        if "BEP estimate" in note:
            source = "bep"
        link["step_kinetics"] = {
            "name": name,
            "source": source,
            "band_eV": _number(_mapping(link.get("bep")).get("rmse"))
            if source == "bep"
            else None,
            "quantities": quantities,
            "drc_note": drc_note,
            "product": product,
            "arrhenius": arrhenius,
        }

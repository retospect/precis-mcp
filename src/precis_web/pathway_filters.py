"""Stored rate inputs for visual pathway filters, independent of panel trimming.

Keep each solve's environment beside its transitions: the panel's v2 overlay
can retain v1 transitions, whose rates must not acquire the v2 environment.
Missing per-step flux is unknown, never a zero inferred from the overall TOF.
The browser joins physical endpoints (without landing history) and prefers a
finite v2 transition over the v1 chain's transition on that same edge.
"""

from __future__ import annotations

import math
from typing import Any


def _number(value: Any) -> float | None:
    return (
        float(value)
        if isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        else None
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def filter_kinetics_payload(results: dict[str, Any]) -> list[dict[str, Any]]:
    """Trim stored transitions and their actual environments, v2 first.

    Older v2 records contain no transitions. They deliberately contribute
    no rates; a v1 fallback is named as such even when a v2 TOF exists.
    """
    records = []
    for key, label in (
        ("kinetics_v2", "v2 per-step rate"),
        ("kinetics", "v1 chain net_rate"),
    ):
        record = _mapping(results.get(key))
        transitions = record.get("transitions")
        rows = []
        for row in transitions if isinstance(transitions, list) else []:
            if (
                not isinstance(row, dict)
                or (rate := _number(row.get("net_rate"))) is None
            ):
                continue
            source, target = row.get("from"), row.get("to")
            if not isinstance(source, str) or not isinstance(target, str):
                # Some records identify transitions by the physical edge name.
                name = row.get("name")
                if not isinstance(name, str) or name.count("->") != 1:
                    continue
                source, target = name.split("->")
            rows.append({"from": source, "to": target, "net_rate": rate})
        if not rows:
            continue
        env = _mapping(record.get("environment"))
        conditions = _mapping(record.get("conditions"))
        gases = _mapping(env.get("gases"))
        pressures = _mapping(conditions.get("pressures"))
        if gases:
            pressures = {
                **_mapping(gases.get("inputs")),
                **_mapping(gases.get("outputs")),
                **_mapping(gases.get("inerts")),
            }
        pressures = {
            k: n for k, v in pressures.items() if (n := _number(v)) is not None
        }
        electro = _mapping(env.get("electrochemical"))
        records.append(
            {
                "source": label,
                "transitions": rows,
                "environment": {
                    "T": _number(env.get("T", conditions.get("temperature"))),
                    "p_total": _number(gases.get("p_total", env.get("p_total"))),
                    "pressures": pressures,
                    "U": _number(electro.get("U_vs_RHE")),
                    "pH": _number(electro.get("pH")),
                },
            }
        )
    return records

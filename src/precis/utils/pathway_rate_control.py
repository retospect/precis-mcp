"""Read-only rate-control provenance shared by pathway and web readers.

Equal numerical sensitivities do not establish which solve produced them.
Only the record's explicit source licenses a mean-field or chain label.
All readers gate control claims before dropping missing values. This module
also works without the optional engine; it never solves or mutates a record.
Competing routes or inhibition can give large offsetting X_RC values: their
magnitude warrants a sensitivity caution, not rejection of a unit-sum map.
An explicit TOF noise flag for the recorded source does reject X_RC: a
normalised map cannot make sensitivities about an unresolved rate usable.
"""

from __future__ import annotations

import math
from typing import Any

# autocatpath/kinetics.py::degree_of_rate_control's ``sum_tol`` default.
# The engine exposes a function parameter, not an importable constant.
RATE_CONTROL_SUM_TOL = 0.05
# Above this magnitude, show X_RC values with a sensitivity caution.
RATE_CONTROL_MAX_ABS = 2.0


def rate_control_near_noise(record: dict[str, Any] | None) -> bool:
    """Use only the named solve's TOF flag; unknown sources check both."""
    if record is None:
        return False
    status = record.get("mf_status")
    mf_noise = isinstance(status, dict) and status.get("tof_near_noise") is True
    chain_noise = record.get("tof_chain_near_noise") is True
    source = record.get("drc_source")
    if source == "mf":
        return mf_noise
    if source == "chain":
        return chain_noise
    return mf_noise or chain_noise


def usable_rate_control(values: Any, *, record: dict[str, Any] | None = None) -> bool:
    """Reject flagged TOF noise, then require complete finite unit-sum X_RC."""
    if rate_control_near_noise(record):
        return False
    if not isinstance(values, dict) or not values:
        return False
    finite = finite_rate_control(values)
    if len(finite) != len(values):
        return False
    try:
        total = math.fsum(finite.values())
    except OverflowError:
        return False
    # Compare the inclusive bounds directly: abs(1.05 - 1) rounds above 0.05.
    return 1.0 - RATE_CONTROL_SUM_TOL <= total <= 1.0 + RATE_CONTROL_SUM_TOL


def usable_thermodynamic_rate_control(values: Any) -> bool:
    """Require complete finite X_TRC; the engine imposes no sum rule."""
    return (
        isinstance(values, dict)
        and bool(values)
        and len(finite_rate_control(values)) == len(values)
    )


def finite_rate_control(values: Any) -> dict[str, float]:
    """Keep measured, finite sensitivities; booleans and nulls are not values."""
    if not isinstance(values, dict):
        return {}
    return {
        str(key): float(value)
        for key, value in values.items()
        if isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
    }


def rate_control_note(
    source: Any,
    values: Any = None,
    *,
    thermodynamic: bool = False,
    record: dict[str, Any] | None = None,
) -> str:
    """Label only usable control, with provenance from the same stored solve."""
    usable = (
        usable_thermodynamic_rate_control(values)
        if thermodynamic
        else usable_rate_control(values, record=record)
    )
    if not usable:
        if not thermodynamic and rate_control_near_noise(record):
            return "No usable rate control: the rate is at noise level."
        return "No usable rate control" + (" (X_TRC)." if thermodynamic else ".")
    note = rate_control_source_note(source, thermodynamic=thermodynamic)
    if not thermodynamic and any(
        abs(value) > RATE_CONTROL_MAX_ABS
        for value in finite_rate_control(values).values()
    ):
        note += " Caution: large offsetting X_RC are sensitive to small changes in the solve."
    return note


def rate_control_source_note(source: Any, *, thermodynamic: bool = False) -> str:
    """Name recorded provenance, including for optima stored without a map."""
    kind = "Thermodynamic rate control" if thermodynamic else "Rate control"
    if source == "mf":
        note = f"{kind}: mean-field estimate."
    elif source == "chain":
        note = f"{kind}: chain estimate."
    else:
        note = f"{kind} source was not recorded; the values are unreliable."
    return note

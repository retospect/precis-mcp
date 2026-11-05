"""Read-only rate-control provenance shared by pathway and web readers.

Equal numerical sensitivities do not establish which solve produced them.
Only the record's explicit source licenses a mean-field or chain label.
All readers gate control claims before dropping missing values. This module
also works without the optional engine; it never solves or mutates a record.
"""

from __future__ import annotations

import math
from typing import Any

# autocatpath/kinetics.py::degree_of_rate_control's ``sum_tol`` default.
# The engine exposes a function parameter, not an importable constant.
RATE_CONTROL_SUM_TOL = 0.05
# The verdict's existing ill-conditioned guard, retained for every X_RC reader.
RATE_CONTROL_MAX_ABS = 2.0


def usable_rate_control(values: Any) -> bool:
    """Require bounded, complete finite X_RC summing to one within tolerance."""
    if not isinstance(values, dict) or not values:
        return False
    finite = finite_rate_control(values)
    if len(finite) != len(values):
        return False
    if any(abs(value) > RATE_CONTROL_MAX_ABS for value in finite.values()):
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
    source: Any, values: Any = None, *, thermodynamic: bool = False
) -> str:
    """Label only usable control, with provenance from the same stored solve."""
    usable = (
        usable_thermodynamic_rate_control(values)
        if thermodynamic
        else usable_rate_control(values)
    )
    if not usable:
        return "No usable rate control" + (" (X_TRC)." if thermodynamic else ".")
    return rate_control_source_note(source, thermodynamic=thermodynamic)


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

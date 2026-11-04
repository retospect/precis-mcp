"""Human-set environment search bounds, resolved without writing quest meta.

The optimizer's later slices share this resolver so their bounds cannot
drift. Core owns the fallback rather than importing the optional pathway
plugin; a test pins the T/pressure box to its quest kinetics sweep.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

ENVIRONMENT_AXES = frozenset({"T", "p_total", "NO", "H2", "U_RHE", "pH", "ion_conc_M"})
ENVIRONMENT_TERMS = frozenset({"m_th", "m_kin", "m_HER", "s", "r", "m_Ea"})


def effective_environment_space(meta: dict[str, Any] | None) -> dict[str, Any]:
    """Return a detached search space from validated quest metadata.

    Defaults are T 350–900 K, p_total 0.1–100 bar logarithmic, TOF_floor
    1e-3 /s and tau_s 1 s. Operating-condition windows supply U_RHE/pH
    bounds; a point without a window becomes [point, point], including
    ion_conc_M when declared. Undeclared coordinates stay absent.
    Explicit environment axes override these bounds individually; other
    supplied fields override defaults. No weights means bottleneck, and
    no selectivity_floor means no additional selectivity mask.
    """
    meta = meta or {}
    axes: dict[str, Any] = {
        "T": [350.0, 900.0],
        "p_total": [0.1, 100.0, "log"],
    }
    conditions = meta.get("operating_conditions") or {}
    window = conditions.get("window") or {}
    for axis in ("U_RHE", "pH", "ion_conc_M"):
        if axis in window:
            axes[axis] = deepcopy(window[axis])
        elif axis in conditions:
            axes[axis] = [conditions[axis], conditions[axis]]
    space: dict[str, Any] = {"axes": axes, "TOF_floor": 1e-3, "tau_s": 1.0}
    declared = deepcopy(meta.get("environment_space") or {})
    axes.update(declared.pop("axes", {}))
    space.update(declared)
    return space

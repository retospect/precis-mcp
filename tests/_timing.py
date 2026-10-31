"""Coverage-aware wall-clock budgets for ``assert elapsed < N`` tests.

Every budget is tuned for a plain run; a coverage tracer makes the same
Python work cost roughly 2-4x (the 2026-10-30/31 coverage-db nightlies:
29.8 us/record vs the 20 us bound, 333 ms vs 200 ms, gr543576). Budget
asserts route through :func:`budget`, which returns the limit scaled x3
while a tracer measures the run — and the exact limit otherwise, so the
check never weakens in a normal run.
"""

from __future__ import annotations

import os
import sys

#: Set by check.yml's coverage-db job so detection needs no import probe.
COVERAGE_ENV = "PRECIS_COVERAGE_RUN"
SCALE = 3.0


def coverage_active() -> bool:
    """True while a coverage tracer measures this run."""
    if os.environ.get(COVERAGE_ENV):
        return True
    if sys.gettrace() is not None:
        return True
    mod = sys.modules.get("coverage")
    current = getattr(getattr(mod, "Coverage", None), "current", None)
    return bool(current is not None and current())


def budget(limit: float) -> float:
    """``limit`` scaled x3 under a coverage tracer; ``limit`` otherwise."""
    return limit * (SCALE if coverage_active() else 1.0)

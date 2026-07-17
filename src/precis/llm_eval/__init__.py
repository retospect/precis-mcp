"""LLM golden-eval harness (slice 11) — measure a model on precis's own tasks.

The middle rung of the catalog's trust ladder (observed-telemetry >
measured-eval > published-benchmark): run a candidate over a gold set, score
it deterministically, and write a ``measured-eval`` ordinal per capability axis
through :func:`precis.llm_catalog.record_eval` — before routing production work
to an unproven model.

Public surface: :func:`harness.run_eval`, :func:`harness.compare`,
:func:`tasks.load_gold_set`.
"""

from __future__ import annotations

from precis.llm_eval.harness import (
    AxisResult,
    EvalReport,
    TaskScore,
    compare,
    run_eval,
)
from precis.llm_eval.tasks import GoldTask, load_gold_set

__all__ = [
    "AxisResult",
    "EvalReport",
    "GoldTask",
    "TaskScore",
    "compare",
    "load_gold_set",
    "run_eval",
]

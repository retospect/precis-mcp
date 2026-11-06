"""LLM golden-eval harness (slice 11) — measure a model on precis's own tasks.

The middle rung of the catalog's trust ladder (observed-telemetry >
measured-eval > published-benchmark): run a candidate over a gold set, score
it deterministically, and write a ``measured-eval`` ordinal per capability axis
through :func:`precis.llm_catalog.record_eval` — before routing production work
to an unproven model.

Separate one-off experiment: :mod:`.notation` (quantity-notation A/B, gr336349;
run with ``python -m precis.llm_eval.notation``).

Public surface: :func:`harness.run_eval`, :func:`harness.compare`,
:func:`tasks.load_gold_set`.

Account-pinned dispatch contains pacing, scrubbing and telemetry failures as
error results. Its last-resort error is static because scrubbing can itself
fail; provider rate limits latch before any of these post-call operations so
telemetry failure cannot permit another request.
"""

from __future__ import annotations

from precis.llm_eval.harness import (
    AxisResult,
    EvalReport,
    PlacementMismatch,
    TaskScore,
    compare,
    run_eval,
)
from precis.llm_eval.tasks import GoldTask, load_gold_set

__all__ = [
    "AxisResult",
    "EvalReport",
    "GoldTask",
    "PlacementMismatch",
    "TaskScore",
    "compare",
    "load_gold_set",
    "run_eval",
]

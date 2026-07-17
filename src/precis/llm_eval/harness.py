"""The LLM eval harness — run a candidate model over a gold set, score, record.

Slice 11. Turns a gold set (:mod:`.tasks`) into per-axis ``measured-eval``
ordinals via the deterministic scorers (:mod:`.scorers`), reporting through the
catalog's existing write surface (:func:`llm_catalog.record_eval`). This is the
**middle rung of the trust ladder** — a ``measured-eval`` number from precis's
own tasks that de-risks routing to an unproven model *before* it runs
production work (observed-telemetry > measured-eval > published-benchmark).

The candidate model is exercised through the real router seam
(:func:`router.dispatch`) so an OSS variant runs on the exact transport +
booked endpoint it would in production; a booked ``endpoint`` dict pins the
variant (gripe 162624), so a fp8 golden run measures the fp8 endpoint.

A task whose ``scorer`` isn't wired (the heavy ``code`` / ``summarize-extract``
axes that need a test-runner or judge) is **skipped with a log**, never scored
0 — the harness reports what it did and did not measure (no silent caps).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from precis.llm_eval.scorers import SCORERS, bucket_to_ordinal
from precis.llm_eval.tasks import GoldTask, load_gold_set

if TYPE_CHECKING:
    from precis.store import Store
    from precis.utils.llm.router import Tier

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TaskScore:
    task_id: str
    score: float
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AxisResult:
    """The outcome of evaluating one capability axis over its gold tasks."""

    axis: str
    n: int
    mean_score: float
    ordinal: int
    per_task: list[TaskScore] = field(default_factory=list)
    recorded: bool = False


@dataclass(frozen=True, slots=True)
class EvalReport:
    model: str
    results: list[AxisResult]
    skipped: list[str]  # "task_id (axis): reason"

    @property
    def ordinals(self) -> dict[str, int]:
        return {r.axis: r.ordinal for r in self.results}


def run_axis(
    tasks: list[GoldTask],
    *,
    model: str,
    tier: Tier,
    dispatch_fn: Callable[[Any], Any],
    endpoint: dict[str, Any] | None = None,
    effort: str | None = None,
) -> AxisResult:
    """Run one axis's gold tasks through ``model`` and bucket the mean score.

    Every task in ``tasks`` must share an axis and a **wired** scorer (the
    caller filters). A dispatch error or a transport failure scores that task 0
    (a model that can't answer fails the axis) with the error retained for the
    report.
    """
    from precis.utils.llm.router import LlmRequest

    axis = tasks[0].axis
    scored: list[TaskScore] = []
    for t in tasks:
        try:
            res = dispatch_fn(
                LlmRequest(
                    tier=tier,
                    prompt=t.prompt,
                    model=model,
                    tools_needed=t.tools_needed,
                    endpoint=endpoint,
                    effort=effort,
                    source="llm_eval",
                )
            )
        except Exception as exc:  # transport blew up — that's a 0 for this task
            log.warning("llm eval: task %s dispatch raised: %s", t.task_id, exc)
            scored.append(TaskScore(t.task_id, 0.0, error=str(exc)))
            continue
        err = getattr(res, "error", None)
        if err:
            scored.append(TaskScore(t.task_id, 0.0, error=str(err)))
            continue
        scorer = SCORERS[t.scorer]
        score = scorer(
            getattr(res, "text", "") or "", getattr(res, "data", None), t.expect
        )
        scored.append(TaskScore(t.task_id, score))
    mean = sum(s.score for s in scored) / len(scored) if scored else 0.0
    return AxisResult(
        axis=axis,
        n=len(scored),
        mean_score=mean,
        ordinal=bucket_to_ordinal(mean),
        per_task=scored,
    )


def run_eval(
    store: Store,
    *,
    model: str,
    tier: Tier,
    gold_path: str | None = None,
    tasks: list[GoldTask] | None = None,
    dispatch_fn: Callable[[Any], Any] | None = None,
    endpoint: dict[str, Any] | None = None,
    effort: str | None = None,
    record: bool = True,
) -> EvalReport:
    """Evaluate ``model`` over a gold set and (optionally) record the ordinals.

    Groups the gold tasks by axis, runs each wired axis through
    :func:`run_axis`, and — when ``record`` — writes each axis ordinal to the
    model's card via :func:`llm_catalog.record_eval` (``measured-eval``
    provenance). Tasks naming an unwired scorer are collected into
    ``report.skipped`` and reported, not measured.

    ``dispatch_fn`` defaults to the live :func:`router.dispatch`; tests inject a
    stub so no real model runs.
    """
    if dispatch_fn is None:
        from precis.utils.llm.router import dispatch as _live_dispatch

        disp: Callable[[Any], Any] = _live_dispatch
    else:
        disp = dispatch_fn

    gold = tasks if tasks is not None else load_gold_set(gold_path)

    by_axis: dict[str, list[GoldTask]] = {}
    skipped: list[str] = []
    for t in gold:
        if t.scorer not in SCORERS:
            skipped.append(f"{t.task_id} ({t.axis}): scorer {t.scorer!r} not wired")
            log.info(
                "llm eval: skipping %s — scorer %r not wired (heavy axis)",
                t.task_id,
                t.scorer,
            )
            continue
        by_axis.setdefault(t.axis, []).append(t)

    results: list[AxisResult] = []
    for axis, axis_tasks in by_axis.items():
        res = run_axis(
            axis_tasks,
            model=model,
            tier=tier,
            dispatch_fn=disp,
            endpoint=endpoint,
            effort=effort,
        )
        recorded = False
        if record:
            from precis.llm_catalog import record_eval

            try:
                record_eval(
                    store,
                    model,
                    axis=axis,
                    ordinal=res.ordinal,
                    by="agent",
                    note=(
                        f"golden eval: {axis} = {res.ordinal} "
                        f"(mean {res.mean_score:.2f} over {res.n} tasks)"
                    ),
                )
                recorded = True
            except Exception:
                log.warning(
                    "llm eval: record_eval failed for %s/%s",
                    model,
                    axis,
                    exc_info=True,
                )
        results.append(
            AxisResult(
                axis=res.axis,
                n=res.n,
                mean_score=res.mean_score,
                ordinal=res.ordinal,
                per_task=res.per_task,
                recorded=recorded,
            )
        )

    return EvalReport(model=model, results=results, skipped=skipped)


def compare(
    store: Store,
    *,
    model_a: str,
    model_b: str,
    tier: Tier,
    gold_path: str | None = None,
    dispatch_fn: Callable[[Any], Any] | None = None,
    record: bool = False,
) -> dict[str, EvalReport]:
    """Run two models over the same gold set — the "compare A vs B" surface.

    Defaults to ``record=False`` (a comparison is exploratory; recording both
    would overwrite the card twice). Returns ``{model: EvalReport}`` so the
    caller renders a per-axis A-vs-B table.
    """
    return {
        model_a: run_eval(
            store,
            model=model_a,
            tier=tier,
            gold_path=gold_path,
            dispatch_fn=dispatch_fn,
            record=record,
        ),
        model_b: run_eval(
            store,
            model=model_b,
            tier=tier,
            gold_path=gold_path,
            dispatch_fn=dispatch_fn,
            record=record,
        ),
    }


__all__ = [
    "AxisResult",
    "EvalReport",
    "TaskScore",
    "compare",
    "run_axis",
    "run_eval",
]

"""The weekly tier-ladder regression run — every offering measured, pinned
one rung at a time (gr595546).

``precis llm eval`` measures a candidate on demand; this is the fixed-set
form the ``llm_tier_regression`` scheduler cadence fires weekly: the
packaged regression set (``precis.data``
``llm_eval/tier_regression_set.json`` — ~30 tasks with known-good
outcomes, the union of the curated ``scripts/llm_eval/gold_set`` sets plus
the packaged seed) is run through **every rung of every tier's resolved
chain** — each local and cloud offering the router could pick — via a
dispatch pinned to that single rung, so a weak offering can't hide behind
failover. Per offering the run records a pass rate: passed tasks over
**attempted** tasks at ``score >= pass_min`` — an errored task counts
against the rate (td663780), so a partial outage reads as a drop, not a
silently shrinking denominator; a run where nothing scored records
``rate=None`` (a void, not a 0). The ``llm-tier-regression`` condition row
compares it against the last recorded run. A provider silently swapping
the model behind an id reads as that offering's rate dropping.

Scorers in :data:`_BLOCKED_UNATTENDED` (``code`` — its callee executes
model-generated Python with worker credentials and network, no OS
sandbox) are refused on this unattended path: their tasks are skipped
with a reason, same as an unwired scorer. The attended ``precis llm
eval`` path still wires them.

Dispatch: a rung that isn't cloud (``LOCAL`` wire, or a hosted-OSS wire
with no hosted endpoint configured — i.e. the ``served_by`` slot path)
runs through :func:`pinned.pinned_dispatch` ``"local"``, which reserves
the serving slot and pins the endpoint exactly as production does. A
cloud rung runs through a one-rung :class:`FailoverProvider` — the same
ladder ``route()`` walks, so ``profile``/``bare``/``max_tokens``/
``timeout_s``/subscription gates apply unchanged — logged to the
route-log like :func:`router.route` does.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import replace
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from precis.llm_eval.pinned import pinned_dispatch

if TYPE_CHECKING:
    from precis.llm_eval.tasks import GoldTask
    from precis.store import Store
    from precis.utils.llm.router import Backend, LlmRequest, LlmResult, Rung, Tier

log = logging.getLogger(__name__)

#: A task "passes" at this score — the scorers are mostly 0/1, and the
#: fractional ones (keypoints/tool_json/summary) read a mostly-right reply
#: as a pass: the weekly number should move on real drift, not wording
#: noise. ``PRECIS_TIER_REGRESSION_PASS_MIN`` overrides.
PASS_MIN = 0.5

#: ``precis.data`` resource holding the fixed regression set.
_SET_RESOURCE = "llm_eval/tier_regression_set.json"

#: Scorers the scheduled run refuses even though they are wired
#: (td663780): ``score_code`` executes model-generated Python in a bare
#: ``python -I`` subprocess — timed but not jailed — inheriting the
#: worker's credentials and network. That is a controlled-eval
#: convenience for the attended ``precis llm eval`` path (the scorer's
#: docstring says so), never something an unattended weekly cadence may
#: run. A task naming a blocked scorer is skipped with a reason rather
#: than executed; the packaged set carries none.
_BLOCKED_UNATTENDED = frozenset({"code"})


def regression_set_path() -> Path:
    """Path to the packaged weekly regression set (~30 known-good tasks)."""
    return Path(str(resources.files("precis.data").joinpath(_SET_RESOURCE)))


def _env_f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _ladder_backend() -> Backend:
    """The effective cloud backend, demoted exactly as ``route()`` does:
    ``OPENAI`` without ``PRECIS_LLM_BASE_URL`` falls back to ``ANTHROPIC``."""
    from precis.utils.llm.router import Backend, resolve_backend

    backend = resolve_backend()
    if backend is Backend.OPENAI and not os.environ.get("PRECIS_LLM_BASE_URL"):
        backend = Backend.ANTHROPIC
    return backend


def ladder_rungs(backend: Backend | None = None) -> list[tuple[Tier, Rung]]:
    """Every offering in the ladder, in tier order.

    ``resolve_chain(tier, tools_needed=False)`` returns the tier's rung
    list as completion traffic walks it — the operator ``llm.chain.<tier>``
    chain when set (unfiltered for tool-less calls, so every authored rung
    shows), else the compiled default. One entry per rung, so a failover
    rung gets measured too, not only the rung production happens to land
    on.
    """
    from precis.utils.llm.router import Tier, resolve_chain

    backend = backend or _ladder_backend()
    out: list[tuple[Tier, Rung]] = []
    for tier in Tier:
        out.extend(
            (tier, rung)
            for rung in resolve_chain(tier, tools_needed=False, backend=backend)
        )
    return out


def _error(req: LlmRequest, model: str, msg: str, *, paused: bool = False) -> LlmResult:
    from precis.utils.llm.router import LlmResult

    return LlmResult(
        text="",
        cost_usd=None,
        turns_used=None,
        model=model,
        tier=req.tier,
        error=msg,
        paused=paused,
    )


def _apply_rung(req: LlmRequest, rung: Rung) -> LlmRequest:
    """The per-rung cap transforms ``FailoverProvider._walk`` applies —
    rung caps fill only a caller-unset field (the task's own ``max_tokens``
    is a correctness constraint). ``reasoning_effort`` is applied by the
    caller for the LOCAL wire; ``profile`` needn't be — a profiled rung
    classifies cloud, so the ladder applies it there."""
    call_req = req
    if rung.max_tokens is not None and req.max_tokens is None:
        call_req = replace(call_req, max_tokens=rung.max_tokens)
    if rung.timeout_s is not None and req.timeout_s is None:
        call_req = replace(call_req, timeout_s=rung.timeout_s)
    return call_req


def rung_dispatch(tier: Tier, rung: Rung) -> Callable[[Any], Any]:
    """A ``dispatch_fn(req) -> LlmResult`` pinned to one ladder rung.

    Non-cloud rungs (``_rung_is_cloud`` — the ``LOCAL`` wire, or a hosted
    wire with no hosted endpoint so only a ``served_by`` slot can carry
    it) dispatch through :func:`pinned_dispatch` ``"local"``: the slot
    reserve + endpoint pin is production-faithful for the served-by path,
    with the rung's caps applied by :func:`_apply_rung` (a local rung's
    ``max_tokens``/``reasoning_effort`` is what keeps a thinking model
    from spending the whole cap on its thinking channel).
    Cloud rungs run a one-rung :class:`FailoverProvider` — the ladder
    itself applies the rung's ``profile``/``bare``/``max_tokens``/
    ``timeout_s`` and the subscription gates — wrapped in the same
    breaker gate + ``record_dispatch`` logging :func:`pinned_dispatch`
    uses.
    """
    from precis.utils.llm.router import (
        FailoverProvider,
        Transport,
        _rung_is_cloud,
        record_dispatch,
        resolve_model,
    )

    model = rung.model or resolve_model(tier)
    if not _rung_is_cloud(rung):
        pinned = pinned_dispatch(model, "local")

        def dispatch_local(req: LlmRequest) -> LlmResult:
            call_req = _apply_rung(req, rung)
            if rung.reasoning_effort is not None and rung.transport is Transport.LOCAL:
                call_req = replace(
                    call_req, local_reasoning_effort=rung.reasoning_effort
                )
            return pinned(call_req)

        return dispatch_local
    ladder = FailoverProvider([rung])

    def dispatch(req: LlmRequest) -> LlmResult:
        from precis.budget import breaker

        trip = breaker.gate_tier(
            req.tier, transport=rung.transport.value, local=False, bare=rung.bare
        )
        if trip is not None:
            return _error(req, model, trip, paused=True)
        started = time.monotonic()
        try:
            result = ladder.run(req, model=model)
        except Exception as exc:  # provider raised — score it an error task
            log.warning(
                "tier regression: %s rung dispatch of %s raised: %s",
                rung.transport,
                model,
                exc,
            )
            result = _error(req, model, f"{type(exc).__name__}: {exc}")
        if result.placement is None:
            result = replace(result, placement="cloud")
        record_dispatch(
            req,
            result,
            transport=rung.transport,
            duration_ms=int((time.monotonic() - started) * 1000),
            routed="cloud",
        )
        return result

    return dispatch


def run_tier_regression(
    store: Store,
    *,
    tasks: list[GoldTask] | None = None,
    gold_path: str | None = None,
    dispatch_for: Callable[[Tier, Rung], Callable[[Any], Any]] | None = None,
    pass_min: float | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Run the fixed set through every ladder offering → the score doc.

    ``tasks`` defaults to the packaged regression set; ``dispatch_for``
    defaults to :func:`rung_dispatch` (tests inject a scripted map so no
    real model runs). The returned doc is what the pass writes to
    ``app_settings`` — per offering: its tier/rung label, the model +
    transport it dispatched on, the attempted/scored/passed/error counts
    and the pass rate: ``passed`` over **attempted** (scored + errored)
    tasks, so an errored task counts against the rate and a partial
    outage reads as a drop (td663780); ``None`` when nothing scored —
    a void run, not a 0. Tasks naming an unwired or unattended-blocked
    scorer (:data:`_BLOCKED_UNATTENDED`) are counted once in ``skipped``
    (the set is the same for every offering).
    """
    from datetime import UTC, datetime

    from precis.llm_eval import run_eval
    from precis.llm_eval.tasks import load_gold_set
    from precis.utils.llm.router import _rung_is_cloud, resolve_model

    now = now or datetime.now(UTC)
    threshold = (
        pass_min
        if pass_min is not None
        else _env_f("PRECIS_TIER_REGRESSION_PASS_MIN", PASS_MIN)
    )
    full: list[GoldTask] = (
        tasks
        if tasks is not None
        else load_gold_set(gold_path or regression_set_path())
    )
    skipped: list[str] = [
        f"{t.task_id} ({t.axis}): scorer {t.scorer!r} is blocked for "
        "unattended runs (executes model code unsandboxed)"
        for t in full
        if t.scorer in _BLOCKED_UNATTENDED
    ]
    gold = [t for t in full if t.scorer not in _BLOCKED_UNATTENDED]

    offerings: dict[str, Any] = {}
    for tier, rung in ladder_rungs():
        label = rung.label or rung.transport.value
        key = f"{tier.value}/{label}"
        n = 2
        while key in offerings:  # two same-labelled rungs in one chain
            key = f"{tier.value}/{label}#{n}"
            n += 1
        model = rung.model or resolve_model(tier)
        disp = dispatch_for(tier, rung) if dispatch_for else rung_dispatch(tier, rung)
        report = run_eval(
            store,
            model=model,
            tier=tier,
            tasks=gold,
            dispatch_fn=disp,
            record=False,
        )
        scored = passed = errors = 0
        total = 0.0
        for res in report.results:
            for ts in res.per_task:
                if ts.error is not None:
                    errors += 1
                    continue
                scored += 1
                total += ts.score
                if ts.score >= threshold:
                    passed += 1
        for s in report.skipped:
            if s not in skipped:
                skipped.append(s)
        offerings[key] = {
            "tier": tier.value,
            "rung": label,
            "model": model,
            "transport": rung.transport.value,
            "placement": "cloud" if _rung_is_cloud(rung) else "local",
            "attempted": scored + errors,
            "scored": scored,
            "errors": errors,
            "passed": passed,
            "rate": round(passed / (scored + errors), 4) if scored else None,
            "mean": round(total / scored, 4) if scored else None,
        }

    return {
        "computed_at": now.isoformat(),
        "set": _SET_RESOURCE,
        "n_tasks": len(gold),
        "pass_min": threshold,
        "offerings": offerings,
        "skipped": skipped,
    }


__all__ = [
    "PASS_MIN",
    "ladder_rungs",
    "regression_set_path",
    "run_tier_regression",
    "rung_dispatch",
]

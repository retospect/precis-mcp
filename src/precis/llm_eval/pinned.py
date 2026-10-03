"""Eval-only dispatcher that runs a candidate model on exactly one transport.

:func:`router.route` walks the operator chain (``llm.chain.<tier>``), whose
rungs always pin a model — so ``LlmRequest.model`` is ignored and an eval would
measure the chain's model, not the candidate. Production routing must stay as
it is; the eval instead builds its own single rung here (gripe gr464223).

``placement='local'`` reserves a ``served_by`` slot for the candidate and runs
it on the LOCAL wire at the slot's endpoint; ``'cloud'`` runs it on
``claude -p`` (``claude-*`` ids) or the OpenAI-compatible wire. Every call is
logged to the route-log like :func:`router.route` does; failures come back as
error results, never exceptions.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace

from precis.utils.llm.router import (
    LlmRequest,
    LlmResult,
    Transport,
    provider_for,
    record_dispatch,
)

log = logging.getLogger(__name__)


def _error(req: LlmRequest, model: str, msg: str, *, paused: bool = False) -> LlmResult:
    return LlmResult(
        text="",
        cost_usd=None,
        turns_used=None,
        model=model,
        tier=req.tier,
        error=msg,
        paused=paused,
    )


def cloud_transport(model: str) -> Transport:
    """``claude-*`` ids ride ``claude -p``; anything else the hosted OSS wire."""
    return Transport.CLAUDE_P if model.startswith("claude") else Transport.OPENAI_COMPAT


def pinned_dispatch(model: str, placement: str) -> Callable[[LlmRequest], LlmResult]:
    """A ``dispatch_fn(req) -> LlmResult`` pinned to ``model`` on one rung."""
    if placement not in ("local", "cloud"):
        raise ValueError(f"placement must be 'local' or 'cloud', got {placement!r}")

    def _run_local(req: LlmRequest) -> LlmResult:
        from precis.utils.llm import local_serving

        slot = local_serving.acquire(model)
        if slot is None:
            return _error(
                req, model, f"model {model} is not served on any host (no served_by)"
            )
        try:
            if slot.paused:
                return _error(
                    req, model, f"all local slots busy for {model}", paused=True
                )
            call_req = replace(req, local_url=slot.endpoint)
            started = time.monotonic()
            result = _call(Transport.LOCAL, call_req, slot.served_model or model)
            record_dispatch(
                req,
                result,
                transport=Transport.LOCAL,
                duration_ms=int((time.monotonic() - started) * 1000),
                routed="local",
            )
            return result
        finally:
            local_serving.release(slot)

    def _run_cloud(req: LlmRequest) -> LlmResult:
        from precis.budget import breaker

        transport = cloud_transport(model)
        trip = breaker.gate_tier(
            req.tier, transport=transport.value, local=False, bare=False
        )
        if trip is not None:
            return _error(req, model, trip, paused=True)
        started = time.monotonic()
        result = _call(transport, req, model)
        record_dispatch(
            req,
            result,
            transport=transport,
            duration_ms=int((time.monotonic() - started) * 1000),
            routed="cloud",
        )
        return result

    def _call(transport: Transport, req: LlmRequest, run_model: str) -> LlmResult:
        try:
            result = provider_for(transport).run(req, model=run_model)
        except Exception as exc:
            log.warning("llm eval: %s dispatch of %s raised: %s", transport, model, exc)
            result = _error(req, model, f"{type(exc).__name__}: {exc}")
        return replace(result, placement=placement)

    def dispatch(req: LlmRequest) -> LlmResult:
        return _run_local(req) if placement == "local" else _run_cloud(req)

    return dispatch

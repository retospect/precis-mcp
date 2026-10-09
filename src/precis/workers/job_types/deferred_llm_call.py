"""``deferred_llm_call`` — a synchronous surface's LLM call, retried later.

A web follow-up (``precis_web/routes/refs.py::_run_followup``) answers
inside one HTTP request. When the router returns a ``quota``- or
``budget``-class failure the right move is not to store the failure as
the answer turn (gr345578 stored a Claude session-limit 429 verbatim) and
not to block the request for hours: the human turn stays as the resume
point and the call is deferred to this job, which runs it once the
horizon passes and appends the real answer.

Coordinator lane (``REQUIRES = {claude_bin}``, gated per host at claim
time like ``quest_tick``): the job sleeps as a ``Yield`` with an
``at_time`` wake at ``retry_at``, runs the serialized
:class:`~precis.utils.llm.router.LlmRequest` through :func:`route`, and
delivers by ``surface``:

* ``followup`` — ``args`` carries ``request`` (the request's
  JSON-serializable fields), ``conv_slug`` / ``conv_ref_id`` (the thread
  to append to) and ``author`` (default ``asa``). Success appends the
  answer as a conv turn through
  :class:`~precis.handlers.conversation.ConversationHandler` and is
  ``Done``. Another ``quota``/``budget`` result defers again to the new
  ``retry_at`` (bounded by :data:`MAX_DEFERRALS`); any other failure, or
  the bound, appends a ``system`` turn naming the failure and fails the
  job — the thread never shows a failure in asa's voice.

Minted from the web through the canonical todo shape (``put(kind='todo',
executor='coordinator', job_type='deferred_llm_call', params=...)``) so
the dispatch worker owns the job's lifecycle and the todo auto-resolves
on success; :func:`deferral_params` builds the params dict.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from precis.utils.llm.router import LlmRequest, LlmResult, Tier, route
from precis.utils.timeutil import as_utc
from precis.workers.executors._yield import Done, WakeWhen, Yield
from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

NAME = "deferred_llm_call"
SURFACE_FOLLOWUP = "followup"

#: Failure classes that defer instead of failing: the window clears on its
#: own, so the same call later is the right call.
DEFERRABLE_CLASSES: frozenset[str] = frozenset({"quota", "budget"})

#: Successive deferrals before the job gives up and tells the thread. Six
#: five-hour windows is over a day of sustained exhaustion — past that the
#: human should know, not keep waiting.
MAX_DEFERRALS = 6

#: Horizon when a deferrable result names none (matches the quota
#: wording's own fallback).
_DEFAULT_HORIZON = timedelta(hours=2)

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "surface": {"type": "string"},
        "args": {"type": "object"},
        "retry_at": {"type": "string"},
    },
    "required": ["surface", "args"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS = frozenset({"coordinator"})
#: The follow-up runs the claude agent transport in-line; the coordinator
#: lane checks this per host at claim time (``check_job_type_requires``).
REQUIRES: frozenset[str] = frozenset({"claude_bin"})
DESCRIPTION = (
    "Run a synchronous surface's LLM call later (after a quota/budget "
    "window) and deliver the answer where the surface would have."
)

#: ``LlmRequest`` fields a surface may serialize into ``args.request``.
#: Paths travel as strings and are re-resolved on the running host.
_REQUEST_FIELDS: frozenset[str] = frozenset(
    {
        "tier",
        "prompt",
        "messages",
        "tools_needed",
        "model",
        "max_usd",
        "timeout_s",
        "max_tokens",
        "source",
        "system_prompt",
        "mcp_config",
        "max_turns",
        "output_format",
        "disallowed_tools",
        "extra_args",
    }
)


def serialize_request(req: LlmRequest) -> dict[str, Any]:
    """The JSON-serializable subset of ``req`` :func:`request_from_args`
    rebuilds — enough for a one-shot or agent call; the live-only knobs
    (``log_event``, ``on_event``, ``env_overlay``) are re-derived by the
    surface at run time."""
    out: dict[str, Any] = {}
    for name in _REQUEST_FIELDS:
        value = getattr(req, name)
        if name == "tier":
            value = req.tier.value
        elif isinstance(value, Path):
            value = str(value)
        elif isinstance(value, tuple):
            value = list(value)
        if value is not None and value != [] and value != "":
            out[name] = value
    return out


def _resolve_path(raw: Any, env_var: str) -> Path | None:
    """A path from the submitting host if it exists here, else this host's
    own env (the web host and the worker host need not share a tree)."""
    for candidate in (raw, os.environ.get(env_var)):
        if isinstance(candidate, str) and candidate and Path(candidate).exists():
            return Path(candidate)
    return None


def request_from_args(
    data: dict[str, Any], *, log_event: tuple[Any, int, str] | None = None
) -> LlmRequest:
    """Rebuild the :class:`LlmRequest` a surface serialized."""
    kwargs: dict[str, Any] = {k: v for k, v in data.items() if k in _REQUEST_FIELDS}
    kwargs["tier"] = Tier(str(kwargs.get("tier") or Tier.FRONTIER.value))
    kwargs["system_prompt"] = _resolve_path(
        kwargs.get("system_prompt"), "PRECIS_ASK_SOUL_PATH"
    )
    kwargs["mcp_config"] = _resolve_path(kwargs.get("mcp_config"), "PRECIS_MCP_CONFIG")
    for name in ("disallowed_tools", "extra_args"):
        if name in kwargs:
            kwargs[name] = tuple(str(x) for x in kwargs[name])
    return LlmRequest(log_event=log_event, **kwargs)


def deferral_params(
    *, surface: str, args: dict[str, Any], retry_at: datetime | None
) -> dict[str, Any]:
    """The ``params`` dict for a ``deferred_llm_call`` todo/job."""
    at = retry_at if retry_at is not None else datetime.now(UTC) + _DEFAULT_HORIZON
    return {"surface": surface, "args": args, "retry_at": at.isoformat()}


def _wait_until(state: dict[str, Any], at: datetime, *, deferrals: int) -> Yield:
    return Yield(
        state={**state, "retry_at": at.isoformat(), "deferrals": deferrals},
        wake_when=WakeWhen("at_time", {"ts": int(at.timestamp())}),
    )


def _append_turn(
    store: Store, *, slug: str, text: str, author: str, meta: dict[str, Any]
) -> None:
    from precis.dispatch import Hub

    # Workers never import handlers (import contract); the hub resolves
    # the conv handler lazily and caches it.
    Hub(store=store).sibling("conv").put(id=slug, text=text, author=author, meta=meta)


def _deliver_followup(
    ctx: Any, args: dict[str, Any], result: LlmResult, *, failed: bool
) -> None:
    slug = str(args.get("conv_slug") or "")
    if not slug:
        raise ValueError("followup args need conv_slug")
    if failed:
        _append_turn(
            ctx.store,
            slug=slug,
            text=f"⚠️ thinking failed: {result.error}",
            author="system",
            meta={"deferred_job": ctx.ref_id, "reason_class": result.reason_class},
        )
        return
    answer = (result.text or "").strip() or "(the model returned no text)"
    meta = {
        k: v
        for k, v in {
            "model": result.model,
            "cost_usd": result.cost_usd,
            "duration_s": round(result.duration_s, 1) if result.duration_s else None,
            "turns": result.turns_used,
            "deferred_job": ctx.ref_id,
        }.items()
        if v is not None
    }
    _append_turn(
        ctx.store,
        slug=slug,
        text=answer,
        author=str(args.get("author") or "asa"),
        meta=meta,
    )


def _dispatch(ctx: Any, spec: Any) -> Any:
    params = (ctx.meta or {}).get("params") or {}
    state: dict[str, Any] = dict((ctx.meta or {}).get("coordinator_state") or {})
    surface = str(params.get("surface") or "")
    args = params.get("args") or {}
    if surface != SURFACE_FOLLOWUP or not isinstance(args, dict):
        return Done(
            summary=f"deferred_llm_call: unknown surface {surface!r}", success=False
        )
    deferrals = int(state.get("deferrals") or 0)
    now = datetime.now(UTC)
    retry_at = as_utc(state.get("retry_at") or params.get("retry_at"))
    if retry_at is not None and retry_at > now + timedelta(seconds=30):
        # Horizon still ahead (first slice, or woken early by a manual kick
        # / clock skew): park until it passes. A few seconds early is fine.
        return _wait_until(state, retry_at, deferrals=deferrals)

    request = args.get("request")
    if not isinstance(request, dict):
        return Done(summary="deferred_llm_call: args.request missing", success=False)
    conv_ref_id = args.get("conv_ref_id")
    log_event = (
        (ctx.store, int(conv_ref_id), SURFACE_FOLLOWUP)
        if isinstance(conv_ref_id, int)
        else None
    )
    started = time.monotonic()
    result = route(request_from_args(request, log_event=log_event))
    elapsed = time.monotonic() - started

    if result.error is not None and result.reason_class in DEFERRABLE_CLASSES:
        if deferrals + 1 < MAX_DEFERRALS:
            at = result.retry_at or now + _DEFAULT_HORIZON
            ctx.append_chunk(
                "job_event",
                f"deferred_llm_call: {result.reason_class} failure, deferring "
                f"({deferrals + 1}/{MAX_DEFERRALS}) until {at.isoformat()}: "
                f"{result.error}",
            )
            return _wait_until(state, at, deferrals=deferrals + 1)
        log.warning(
            "deferred_llm_call %d: gave up after %d deferrals (%s)",
            ctx.ref_id,
            deferrals,
            result.error,
        )
    if result.error is not None:
        _deliver_followup(ctx, args, result, failed=True)
        return Done(
            summary=(
                f"deferred_llm_call: {surface} failed after {deferrals} "
                f"deferral(s) [{result.reason_class or 'unclassified'}]: "
                f"{result.error}"
            ),
            success=False,
            summary_meta={"reason_class": result.reason_class, "deferrals": deferrals},
        )
    _deliver_followup(ctx, args, result, failed=False)
    return Done(
        summary=(
            f"deferred_llm_call: {surface} answered after {deferrals} deferral(s) "
            f"in {elapsed:.0f}s (model={result.model})"
        ),
        summary_meta={
            "deferrals": deferrals,
            "cost_usd": result.cost_usd,
            "wall_seconds": round(elapsed, 1),
        },
    )


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "DEFERRABLE_CLASSES",
    "MAX_DEFERRALS",
    "NAME",
    "SPEC",
    "SURFACE_FOLLOWUP",
    "deferral_params",
    "load",
    "request_from_args",
    "serialize_request",
]

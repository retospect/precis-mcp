"""job_type registry — what kinds of work the `job` substrate runs.

Each job_type module exports:

- ``PARAMS_SCHEMA`` (jsonschema dict): validated at submit time.
- ``COMPATIBLE_EXECUTORS`` (frozenset[str]): which executors this
  type can run under. Dispatcher rejects the put if the requested
  executor isn't in the set.
- ``REQUIRES`` (frozenset[str]): what the executor host must
  provide. Dispatcher rejects if the executor's PROVIDES set
  doesn't cover this.
- ``DESCRIPTION`` (str): one-line summary; surfaced in
  ``precis-job-help``.
- ``run(...)``: the worker entry point invoked by the executor's
  runner once a row is claimed.
- ``dispatch(ctx, spec)`` (optional): the per-job dispatch
  wrapper. Plugin job_types ship their own; built-ins keep their
  dispatch logic inside the executor module for now.

The runtime registry below imports each built-in type and bundles
its metadata into a :class:`JobTypeSpec`. Third-party packages can
register additional job_types via the ``precis.job_types``
entry-point group; failure isolation mirrors
:func:`precis.dispatch._load_plugins` — one broken plugin must not
brick the worker.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobTypeSpec:
    """Per-job-type declaration. Matches the module exports."""

    name: str
    params_schema: dict[str, Any]
    compatible_executors: frozenset[str]
    requires: frozenset[str]
    description: str
    run: Callable[..., Any]
    #: Optional submit-time check. Signature ``(store, *,
    #: gripe_id, params) -> str | None``: return an error message
    #: when the submit can't actually be carried out, ``None``
    #: when OK. The MCP-side ``JobHandler.put`` surfaces non-None
    #: as ``BadInput`` so the LLM gets an immediate rejection
    #: rather than a queued zombie job. ``None`` here means "no
    #: extra validation beyond the static schema / executor /
    #: REQUIRES checks".
    validate_submit: Callable[..., str | None] | None = None
    #: Optional per-job dispatcher. When set, the executor
    #: (``claude_inproc._run_one`` / ``coordinator._run_one``) calls
    #: ``dispatch(ctx, spec)`` instead of going through the built-in
    #: ``if/elif`` switch. ``ctx`` is a
    #: :class:`precis.workers.executors._context.DispatchContext`
    #: carrying the store handle, ref_id, meta dict, and the
    #: closures over status / chunk / failure / meta helpers that
    #: the executor exposes — plugins use these instead of
    #: importing executor internals. Built-in job_types
    #: (fix_gripe, plan_tick) leave this ``None`` and the
    #: executor falls back to its in-tree dispatchers.
    #:
    #: Coordinator dispatchers MUST return a ``Done`` or ``Yield``
    #: (``precis.workers.executors._yield``); the coordinator persists
    #: that return (terminal status + summary, or checkpoint + wake
    #: condition). A resumed slice reads its prior checkpoint from
    #: ``ctx.meta['coordinator_state']``. Annotated loosely because the
    #: in-tree built-ins return ``None``.
    dispatch: Callable[..., Any] | None = None


JOB_TYPE_PLUGIN_GROUP = "precis.job_types"


def _load_fix_gripe() -> JobTypeSpec:
    # Lazy import keeps the heavy git/subprocess dependencies out
    # of the MCP dispatch path until the worker actually needs them.
    from precis.workers.job_types import fix_gripe

    return JobTypeSpec(
        name="fix_gripe",
        params_schema=fix_gripe.PARAMS_SCHEMA,
        compatible_executors=fix_gripe.COMPATIBLE_EXECUTORS,
        requires=fix_gripe.REQUIRES,
        description=fix_gripe.DESCRIPTION,
        run=fix_gripe.run,
        validate_submit=fix_gripe.validate_submit,
    )


def _load_plan_tick() -> JobTypeSpec:
    # The planner-coroutine tick. Synthesized at dispatch time from
    # an ``LLM:*`` tag on the parent todo (see workers/dispatch.py).
    from precis.workers.job_types import plan_tick

    return JobTypeSpec(
        name="plan_tick",
        params_schema=plan_tick.PARAMS_SCHEMA,
        compatible_executors=plan_tick.COMPATIBLE_EXECUTORS,
        requires=plan_tick.REQUIRES,
        description=plan_tick.DESCRIPTION,
        run=plan_tick.run,
        validate_submit=plan_tick.validate_submit,
    )


def _load_draft_export() -> JobTypeSpec:
    # Deterministic draft → LaTeX → PDF export (runs via its plugin
    # ``dispatch`` under claude_inproc; no claude subprocess).
    from precis.workers.job_types import draft_export

    return draft_export.SPEC


#: Name → spec. Populated lazily on first access so the import
#: graph stays cheap for the MCP server.
_REGISTRY: dict[str, JobTypeSpec] = {}

#: Cached plugin discovery. Populated on first call to
#: ``_discover_job_type_plugins``; entry-point loads only happen
#: once per worker process unless ``_reset_plugin_cache`` is
#: called (used by tests).
_PLUGIN_SPECS: dict[str, JobTypeSpec] | None = None


def _entry_points(group: str) -> list[Any]:
    """Indirection wrapper around ``importlib.metadata.entry_points``.

    Lets tests patch this function to inject fake entry points
    without setting up a real wheel install. Mirrors the pattern
    used in :mod:`precis.dispatch._entry_points`.
    """
    from importlib.metadata import entry_points

    return list(entry_points(group=group))


def _discover_job_type_plugins() -> dict[str, JobTypeSpec]:
    """Load JobTypeSpecs declared by third-party packages.

    Each entry under the ``precis.job_types`` group resolves to
    either a :class:`JobTypeSpec` instance directly or a
    zero-argument factory that produces one.

    Failure semantics match :func:`precis.dispatch._load_plugins`:
    every ``Exception`` raised during load is caught and logged.
    One broken plugin must not brick the worker.
    """
    out: dict[str, JobTypeSpec] = {}
    try:
        eps = _entry_points(JOB_TYPE_PLUGIN_GROUP)
    except Exception as exc:  # defensive — importlib surface is stable
        log.warning("precis.job_types discovery failed: %s", exc)
        return out

    for ep in eps:
        name = getattr(ep, "name", "<unknown>")
        try:
            obj = ep.load()
        except Exception as exc:
            log.warning(
                "precis.job_types plugin %r failed to load (%s): %s",
                name,
                type(exc).__name__,
                exc,
            )
            continue

        # Accept either a JobTypeSpec instance or a callable that
        # produces one (factory pattern, mirrors how plugin
        # packages can defer heavy imports).
        try:
            if isinstance(obj, JobTypeSpec):
                spec = obj
            elif callable(obj):
                spec = obj()
            else:
                log.warning(
                    "precis.job_types plugin %r did not produce a JobTypeSpec "
                    "(got %s); skipping",
                    name,
                    type(obj).__name__,
                )
                continue
        except Exception as exc:
            log.warning(
                "precis.job_types plugin %r factory raised %s: %s",
                name,
                type(exc).__name__,
                exc,
            )
            continue

        if not isinstance(spec, JobTypeSpec):
            log.warning(
                "precis.job_types plugin %r factory returned %s, "
                "not JobTypeSpec; skipping",
                name,
                type(spec).__name__,
            )
            continue

        # Built-ins win on a collision. fix_gripe and plan_tick are
        # the only built-ins today; any plugin claiming those
        # names is logged and skipped.
        if spec.name in ("fix_gripe", "plan_tick"):
            log.warning(
                "precis.job_types plugin %r claims built-in name %r; "
                "skipping (built-ins win)",
                name,
                spec.name,
            )
            continue

        out[spec.name] = spec
    return out


def _get_plugin_specs() -> dict[str, JobTypeSpec]:
    """Return the cached plugin discovery, populating on first call."""
    global _PLUGIN_SPECS
    if _PLUGIN_SPECS is None:
        _PLUGIN_SPECS = _discover_job_type_plugins()
    return _PLUGIN_SPECS


def _reset_plugin_cache() -> None:
    """Drop the plugin-discovery cache. Used by tests; not public."""
    global _PLUGIN_SPECS
    _PLUGIN_SPECS = None


def get_job_type(name: str) -> JobTypeSpec | None:
    if name in _REGISTRY:
        return _REGISTRY[name]
    if name == "fix_gripe":
        _REGISTRY["fix_gripe"] = _load_fix_gripe()
        return _REGISTRY["fix_gripe"]
    if name == "plan_tick":
        _REGISTRY["plan_tick"] = _load_plan_tick()
        return _REGISTRY["plan_tick"]
    if name == "draft_export":
        _REGISTRY["draft_export"] = _load_draft_export()
        return _REGISTRY["draft_export"]
    # Fall through to plugin-discovered specs. Cached on first
    # lookup so subsequent calls are cheap.
    plugins = _get_plugin_specs()
    spec = plugins.get(name)
    if spec is not None:
        _REGISTRY[name] = spec
        return spec
    return None


def known_job_types() -> list[str]:
    """List of registered job_type names (for error messages)."""
    builtins = ["fix_gripe", "plan_tick", "draft_export"]
    plugin_names = sorted(_get_plugin_specs())
    # Built-ins first so the error-message ordering is stable for
    # callers that have only ever seen the in-tree set.
    return builtins + [n for n in plugin_names if n not in builtins]


__all__ = [
    "JOB_TYPE_PLUGIN_GROUP",
    "JobTypeSpec",
    "_reset_plugin_cache",
    "get_job_type",
    "known_job_types",
]

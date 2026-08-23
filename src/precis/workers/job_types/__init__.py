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

#: Executor lanes that actually read ``JobTypeSpec.run`` / ``.dispatch`` /
#: ``.submit``+``.poll`` when they claim a job (see each module's own
#: guard: ``claude_inproc._run_one``'s ``spec.dispatch is not None`` /
#: hardcoded-name ``spec.run`` fallback, ``job_inproc._run_one``'s
#: ``spec.dispatch is None`` failure, ``ssh_node.run_ssh_node_pass``'s
#: ``submit``+``poll`` / ``dispatch`` gate, and ``coordinator``'s
#: ``spec.dispatch is None`` failure). ``claude_docker`` is deliberately
#: absent — ``run_claude_docker_pass`` claims ``sandbox_run`` rows and
#: calls the ``sandbox_run`` module's functions directly, keyed on
#: job_type name; it never reads any of these three fields, so a
#: job_type whose *only* compatible executor is ``claude_docker``
#: legitimately needs none of them set.
_DISPATCH_PROTOCOL_EXECUTORS: frozenset[str] = frozenset(
    {"claude_inproc", "job_inproc", "ssh_node", "coordinator"}
)


@dataclass(frozen=True)
class JobTypeSpec:
    """Per-job-type declaration. Matches the module exports."""

    name: str
    params_schema: dict[str, Any]
    compatible_executors: frozenset[str]
    requires: frozenset[str]
    description: str
    #: ``None`` for job_types driven entirely through ``dispatch`` (or
    #: the detached ``submit``/``poll`` pair) — see the class-level
    #: ``__post_init__`` invariant for what "driven" requires.
    run: Callable[..., Any] | None = None
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
    #: Optional DETACHED dispatch pair (the detached submit/poll
    #: protocol, gr187627) — the
    #: ``ssh_node`` executor's non-blocking protocol. When BOTH ``submit``
    #: and ``poll`` are set, ``ssh_node`` prefers them over ``dispatch``:
    #:
    #: * ``submit(ctx, spec) -> handle`` launches the compute DETACHED
    #:   (the plugin owns HOW — ``nohup``, ``docker run -d``, ``sbatch``,
    #:   …) and returns a JSON-serializable handle (typically a str/dict
    #:   naming the remote process/container/job). It must return quickly
    #:   — the whole point is the claiming worker's pass rotation never
    #:   blocks on the run. ``submit`` must NOT call ``ctx.set_status`` —
    #:   the job stays ``STATUS:running`` (already set by the claim) until
    #:   ``poll`` drives it terminal.
    #: * ``poll(ctx, handle) -> bool`` is called once per ``ssh_node`` pass
    #:   for every in-flight handle THIS host submitted (the handle is
    #:   round-tripped through ``meta.compute_handle``). Returns ``True``
    #:   once it has driven the job to a terminal ``STATUS`` via
    #:   ``ctx.set_status`` / ``ctx.record_failure`` (the executor does
    #:   nothing further); ``False`` while still running (the executor
    #:   renews the lease and polls again next pass — ``poll`` itself
    #:   should NOT touch the lease).
    #:
    #: A plugin that hasn't migrated (only ``dispatch`` set) keeps working
    #: — ``ssh_node`` falls back to the legacy blocking call, logging a
    #: deprecation warning naming gr187627 (the multi-hour block starves
    #: the claiming worker's whole pass rotation, which is exactly what
    #: this protocol exists to fix).
    submit: Callable[..., Any] | None = None
    poll: Callable[..., Any] | None = None
    #: Optional wall-clock kill hook — a detached submit/poll job_type has
    #: no termination path once the sweeper's own wall-clock retirement
    #: excludes its executor: ``ssh_node``'s ``_poll_one``
    #: renews the lease forever while ``poll`` returns falsy, so a run
    #: that never self-terminates would poll indefinitely. When
    #: ``_poll_one`` finds ``meta.deadline`` in the past, it calls
    #: ``kill(ctx, handle)`` (if the job_type declares one) BEFORE
    #: terminalizing the row ``STATUS:failed`` — the plugin's chance to
    #: cancel the remote compute (ssh + kill a PID, cancel a Slurm job,
    #: …) so the wall-clock kill isn't purely a DB-side fiction. Optional:
    #: a job_type with no ``kill`` still gets terminalized on schedule,
    #: it just can't proactively stop the remote side (mirrors
    #: ``claude_docker``'s ``_terminate``, which always CAN kill because
    #: it owns the container directly).
    kill: Callable[..., Any] | None = None

    def __post_init__(self) -> None:
        """Enforce "the executor has something to call".

        A job_type compatible with one of the dispatch-protocol
        executors (:data:`_DISPATCH_PROTOCOL_EXECUTORS`) must set
        ``run``, ``dispatch``, or the detached ``submit``+``poll``
        pair — otherwise every one of those executors fails the job
        at claim time with "no dispatch()" / "has no submit/poll or
        dispatch" (see their own guards). Catching that here, at
        registration time, turns a landmine that only detonates once a
        job of that type is actually claimed into an immediate,
        traceback-at-import error. ``claude_docker``-only job_types
        (``sandbox_run``) are exempt — that executor bypasses this
        protocol entirely.

        ``submit``/``poll`` are also required to travel together: a
        lone one of the pair satisfies neither ``ssh_node``'s ``if
        spec.submit is not None and spec.poll is not None`` detached
        gate nor its legacy ``dispatch`` fallback, so it would silently
        fall through to "no submit/poll or dispatch" too — just less
        obviously than an outright missing pair.
        """
        if (self.submit is None) != (self.poll is None):
            raise ValueError(
                f"job_type {self.name!r}: submit and poll must be set together "
                "(ssh_node's detached protocol requires both or neither)"
            )
        has_dispatchable = (
            self.run is not None
            or self.dispatch is not None
            or (self.submit is not None and self.poll is not None)
        )
        if not has_dispatchable and (
            self.compatible_executors & _DISPATCH_PROTOCOL_EXECUTORS
        ):
            raise ValueError(
                f"job_type {self.name!r}: compatible with a dispatch-protocol "
                f"executor ({sorted(self.compatible_executors)}) but sets none "
                "of run / dispatch / submit+poll"
            )


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
    # ``meta.llm_tier`` on the parent todo (see workers/dispatch.py).
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


def _load_doctor_tick() -> JobTypeSpec:
    # Self-healing spine Layer 3: the LLM judgment pass over the fleet's
    # own published health surfaces. Same hardcoded-run shape as
    # plan_tick (not the plugin dispatch protocol) — see
    # ``executors/claude_inproc.py::_run_doctor_tick``.
    from precis.workers.job_types import doctor_tick

    return JobTypeSpec(
        name="doctor_tick",
        params_schema=doctor_tick.PARAMS_SCHEMA,
        compatible_executors=doctor_tick.COMPATIBLE_EXECUTORS,
        requires=doctor_tick.REQUIRES,
        description=doctor_tick.DESCRIPTION,
        run=doctor_tick.run,
    )


def _load_draft_export() -> JobTypeSpec:
    # Deterministic draft → LaTeX → PDF export (runs via its plugin
    # ``dispatch`` under claude_inproc; no claude subprocess).
    from precis.workers.job_types import draft_export

    return draft_export.SPEC


def _load_taproot_backfill() -> JobTypeSpec:
    # Cluster replacement for the (removed) synchronous edit(taproot=True)
    # MCP door — the LLM-bearing canonicalizer cascade runs on the worker,
    # never in-process on the MCP server. Runs via plugin dispatch.
    from precis.workers.job_types import taproot_backfill

    return taproot_backfill.SPEC


def _load_reground_claim() -> JobTypeSpec:
    # Dispatch glue for the taproot reground pass (audit/prune +
    # deeper re-discovery + the add-first applier). Thin: the mechanism
    # lives in workers/hub_refine.py — docs/backlog/taproot-reground.md's
    # DRY invariant is that there is exactly one hub-improver.
    from precis.workers.job_types import reground_claim

    return reground_claim.SPEC


def _load_draft_refresh() -> JobTypeSpec:
    # One bounded, section-scoped refresh of a living draft (critique +
    # rewrite against corpus + research-arm evidence, growth-gated apply).
    # Runs via plugin dispatch under claude_inproc.
    from precis.workers.job_types import draft_refresh

    return draft_refresh.SPEC


def _load_remarkable_send() -> JobTypeSpec:
    # Deterministic reMarkable-mode export + upload (runs via plugin dispatch).
    from precis.workers.job_types import remarkable_send

    return remarkable_send.SPEC


def _load_news_poll() -> JobTypeSpec:
    # Deterministic RSS ingestion pass (runs via plugin dispatch).
    from precis.workers.job_types import news_poll

    return news_poll.SPEC


def _load_briefing() -> JobTypeSpec:
    # Deterministic morning-news digest (runs via plugin dispatch).
    from precis.workers.job_types import briefing

    return briefing.SPEC


def _load_reading_brief() -> JobTypeSpec:
    # Deterministic morning reading-brief cast producer (coordinator executor).
    from precis.workers.job_types import reading_brief

    return reading_brief.SPEC


def _load_meditation() -> JobTypeSpec:
    # Deterministic evening nidra cast producer (coordinator executor).
    from precis.workers.job_types import meditation

    return meditation.SPEC


def _load_card_forge() -> JobTypeSpec:
    # Deterministic morning card pass (mastery refresh + rework + mint).
    from precis.workers.job_types import card_forge

    return card_forge.SPEC


def _load_struct_relax() -> JobTypeSpec:
    # Energy-rung relax on the GPU node via ssh_node; sinks to the §23.16
    # run-cube. Runs via plugin dispatch.
    from precis.workers.job_types import struct_relax

    return struct_relax.SPEC


def _load_structure_propose() -> JobTypeSpec:
    # LLM turns an instruction into proposed structure ops (tool-less claude,
    # propose-only) under claude_inproc. Runs via plugin dispatch.
    from precis.workers.job_types import structure_propose

    return structure_propose.SPEC


def _load_cad_propose() -> JobTypeSpec:
    # LLM turns an instruction into a proposed CAD design source (tool-less
    # claude, propose-only) under claude_inproc. Runs via plugin dispatch.
    from precis.workers.job_types import cad_propose

    return cad_propose.SPEC


def _load_diagram_propose() -> JobTypeSpec:
    # One figure/mermaid draw-with-me turn against the model — builds/verifies
    # the diagram from seeds and reconciles node→chunk bindings.
    from precis.workers.job_types import diagram_propose

    return diagram_propose.SPEC


def _load_cad_discuss() -> JobTypeSpec:
    # LLM discusses a CAD design (tool-less claude, threaded, read-only) under
    # claude_inproc — answers questions, proposes nothing. Runs via dispatch.
    from precis.workers.job_types import cad_discuss

    return cad_discuss.SPEC


def _load_sandbox_run() -> JobTypeSpec:
    # Open-ended coding task in a throwaway container, run by the
    # claude_docker poll executor.
    # The executor pass is gated on PRECIS_SANDBOX_ENABLED, but the
    # job_type registers unconditionally so put/dispatch validation and
    # error messages work everywhere (a put on a non-sandbox host is
    # rejected by validate_submit, not by an unknown-job_type error).
    from precis.workers.job_types import sandbox_run

    # No run/dispatch/submit/poll: claude_docker (sandbox_run's sole
    # compatible executor) bypasses the dispatch protocol entirely — see
    # _DISPATCH_PROTOCOL_EXECUTORS and __post_init__'s exemption for it.
    return JobTypeSpec(
        name="sandbox_run",
        params_schema=sandbox_run.PARAMS_SCHEMA,
        compatible_executors=sandbox_run.COMPATIBLE_EXECUTORS,
        requires=sandbox_run.REQUIRES,
        description=sandbox_run.DESCRIPTION,
        validate_submit=sandbox_run.validate_submit,
    )


def _load_diagnose_gripe() -> JobTypeSpec:
    # Read-only root-cause diagnosis appended to a gripe as a
    # gripe_comment (see precis.workers.job_types.diagnose_gripe) — never fixes,
    # branches, pushes, or flips STATUS. Runs via plugin dispatch under
    # claude_inproc. Registers unconditionally (like sandbox_run): the
    # scan pass that mints these jobs (diagnose_scan) is the dark gate,
    # not the job_type itself, so put/dispatch validation works even on
    # a host where the scan is off.
    from precis.workers.job_types import diagnose_gripe

    return diagnose_gripe.SPEC


def _load_good_search() -> JobTypeSpec:
    # Deep-search coordinator campaign (fuse → triage children → merged
    # verdict). Runs via plugin dispatch under the coordinator executor.
    from precis.workers.job_types import good_search

    return good_search.SPEC


def _load_embed_batch() -> JobTypeSpec:
    # Bounded in-proc work order draining the derived embed queue (§F
    # cycle a). Runs via plugin dispatch under the job_inproc executor.
    from precis.workers.job_types import embed_batch

    return embed_batch.SPEC


def _load_derived_drain() -> JobTypeSpec:
    # Bounded in-proc work order draining a SMALL-tier derived LLM queue
    # (summarize/classify) — melchior-pinned, router-capped, never cloud.
    # Runs via plugin dispatch under the job_inproc executor.
    from precis.workers.job_types import derived_drain

    return derived_drain.SPEC


def _load_quest_tick() -> JobTypeSpec:
    # Perpetual catalyst-quest loop (harvest → review+propose → dispatch sims →
    # wait → repeat). Runs via plugin dispatch under the coordinator executor.
    from precis.workers.job_types import quest_tick

    return quest_tick.SPEC


def _load_good_search_triage() -> JobTypeSpec:
    # good_search's fan-out child: batched one-shot relevance triage
    # under claude_inproc. Runs via plugin dispatch.
    from precis.workers.job_types import good_search

    return good_search.TRIAGE_SPEC


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
    if name == "doctor_tick":
        _REGISTRY["doctor_tick"] = _load_doctor_tick()
        return _REGISTRY["doctor_tick"]
    if name == "draft_export":
        _REGISTRY["draft_export"] = _load_draft_export()
        return _REGISTRY["draft_export"]
    if name == "taproot_backfill":
        _REGISTRY["taproot_backfill"] = _load_taproot_backfill()
        return _REGISTRY["taproot_backfill"]
    if name == "reground_claim":
        _REGISTRY["reground_claim"] = _load_reground_claim()
        return _REGISTRY["reground_claim"]
    if name == "draft_refresh":
        _REGISTRY["draft_refresh"] = _load_draft_refresh()
        return _REGISTRY["draft_refresh"]
    if name == "remarkable_send":
        _REGISTRY["remarkable_send"] = _load_remarkable_send()
        return _REGISTRY["remarkable_send"]
    if name == "news_poll":
        _REGISTRY["news_poll"] = _load_news_poll()
        return _REGISTRY["news_poll"]
    if name == "briefing":
        _REGISTRY["briefing"] = _load_briefing()
        return _REGISTRY["briefing"]
    if name == "reading_brief":
        _REGISTRY["reading_brief"] = _load_reading_brief()
        return _REGISTRY["reading_brief"]
    if name == "meditation":
        _REGISTRY["meditation"] = _load_meditation()
        return _REGISTRY["meditation"]
    if name == "card_forge":
        _REGISTRY["card_forge"] = _load_card_forge()
        return _REGISTRY["card_forge"]
    if name == "struct_relax":
        _REGISTRY["struct_relax"] = _load_struct_relax()
        return _REGISTRY["struct_relax"]
    if name == "structure_propose":
        _REGISTRY["structure_propose"] = _load_structure_propose()
        return _REGISTRY["structure_propose"]
    if name == "cad_propose":
        _REGISTRY["cad_propose"] = _load_cad_propose()
        return _REGISTRY["cad_propose"]
    if name == "diagram_propose":
        _REGISTRY["diagram_propose"] = _load_diagram_propose()
        return _REGISTRY["diagram_propose"]
    if name == "cad_discuss":
        _REGISTRY["cad_discuss"] = _load_cad_discuss()
        return _REGISTRY["cad_discuss"]
    if name == "sandbox_run":
        _REGISTRY["sandbox_run"] = _load_sandbox_run()
        return _REGISTRY["sandbox_run"]
    if name == "diagnose_gripe":
        _REGISTRY["diagnose_gripe"] = _load_diagnose_gripe()
        return _REGISTRY["diagnose_gripe"]
    if name == "good_search":
        _REGISTRY["good_search"] = _load_good_search()
        return _REGISTRY["good_search"]
    if name == "good_search_triage":
        _REGISTRY["good_search_triage"] = _load_good_search_triage()
        return _REGISTRY["good_search_triage"]
    if name == "quest_tick":
        _REGISTRY["quest_tick"] = _load_quest_tick()
        return _REGISTRY["quest_tick"]
    if name == "embed_batch":
        _REGISTRY["embed_batch"] = _load_embed_batch()
        return _REGISTRY["embed_batch"]
    if name == "derived_drain":
        _REGISTRY["derived_drain"] = _load_derived_drain()
        return _REGISTRY["derived_drain"]
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
    builtins = [
        "fix_gripe",
        "plan_tick",
        "doctor_tick",
        "draft_export",
        "taproot_backfill",
        "reground_claim",
        "draft_refresh",
        "remarkable_send",
        "news_poll",
        "briefing",
        "reading_brief",
        "meditation",
        "card_forge",
        "struct_relax",
        "structure_propose",
        "cad_propose",
        "diagram_propose",
        "cad_discuss",
        "sandbox_run",
        "diagnose_gripe",
        "good_search",
        "good_search_triage",
        "quest_tick",
        "embed_batch",
        "derived_drain",
    ]
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

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

``elsevier_abstract_backfill`` is deterministic: explicit confirmed IDs,
expected-count guard and dry-run default re-arm existing fetch pins without
removing body/hash/event evidence or calling a provider/model.

``fix_gripe`` deployment + trust model: ``docs/runbooks/fix-gripe-ops.md``.
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
    #: Optional submit-time semantic check, ``validate(store, params)``
    #: (gr511724). ``JobHandler.put`` calls it right after the static
    #: ``params_schema`` check; the hook raises ``BadInput`` — with its
    #: own ``next=``/``options=`` — when the params type-check but
    #: cannot run (an unknown taxon, a retired subject ref), so the
    #: submit is refused instead of landing as a queued job that fails
    #: the same check at dispatch. Distinct from ``validate_submit``:
    #: it takes no link target and *raises* rather than returning a
    #: message, so the refusal carries the check's own recovery hint.
    #: ``None``: no semantic gate beyond the schema.
    validate: Callable[..., None] | None = None
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
    #: Optional post-terminal scratch reap, ``reap(ctx, handle)``. ``kill``
    #: runs BEFORE the row is terminalized, so a kill that also deleted the
    #: compute dir would destroy it before ``meta.finished_at`` / ``wall_s``
    #: / ``node`` landed (gr496403). ``ssh_node`` calls ``reap`` after the
    #: terminal ``STATUS:failed`` write; the hook deletes only once it sees
    #: the stamp.
    reap: Callable[..., Any] | None = None
    #: Optional job-type runtime cap for ``ssh_node``'s legacy blocking
    #: dispatch. Read on the claiming host (the same process that runs
    #: ``dispatch``) so a type's configured internal timeout cannot exceed
    #: the outer runner's bound or the initial lease.
    wall_seconds: Callable[[], float] | None = None
    #: Optional ``service_config`` *placement* pin (gr506045, Reto ruling
    #: 2026-10-23). When set, a host-scoped ``service_config`` row for this
    #: service with ``prio > 0`` pins the job type's claims to that host:
    #: ``claim_executor_jobs`` skips the row on every other host, so the
    #: job waits ``STATUS:queued`` (never fails) while its host is down.
    #: The identity is the *heartbeat* host (``reserve_host()`` /
    #: ``lease_host`` — the same key ``service_config.host`` rows are
    #: written under), deliberately distinct from the ``PRECIS_NODE``
    #: ``target_node`` capability-lane pin, whose "pin goes stale → reap
    #: as infra" lifecycle does not apply here. Several enabled host rows
    #: pin to the whole set; zero host-scoped enabled rows (a dark
    #: service or only a ``'*'`` row) means no pin — host-agnostic.
    service_pin: str | None = None
    #: Optional dispatch-time coalescing (gr506771). When ``True``, the
    #: dispatch pass refuses to mint a second job of this type while a
    #: non-terminal one (``queued`` / ``running`` / ``waiting_*``)
    #: already exists fleet-wide — a recurring tick whose earlier job is
    #: still pending is *skipped* (the spawned todo is closed with
    #: ``meta.skipped='previous pending'``) instead of enqueueing a
    #: duplicate that would re-run over the same state. For job types
    #: whose runs are whole-graph passes where a second concurrent run
    #: adds nothing.
    dedupe_pending: bool = False
    #: Optional post-failure hook, ``on_failed(ctx)``, invoked once by
    #: ``job_inproc`` after a job ends ``STATUS:failed`` *through the
    #: dispatch path* (a ``record_failure`` the dispatcher called itself,
    #: or an exception out of ``dispatch``). NOT called on lease-loss
    #: (the row belongs to the new claimant) or on the
    #: ``poison_guard``/sweeper terminal paths, which never reach
    #: dispatch — a crash-looping job must not side-effect here. The
    #: hook owns whatever bounded recovery the job_type wants; keep it
    #: cheap (it runs inside the claiming pass) and never raise.
    on_failed: Callable[[Any], None] | None = None
    #: Optional params-conditional capability hook,
    #: ``conditional_requires(params) -> frozenset[str]``. Folded into
    #: ``job_type_requires`` — the check-only CLAIM-time capability gate
    #: (``claim_executor_jobs(check_job_type_requires=…)`` /
    #: ``_coordinator_capability_ok``), NOT the submit-time
    #: ``requires - EXECUTOR_PROVIDES`` check: its tokens name a *host*
    #: capability advertised through ``resource_slots`` (the heartbeat's
    #: probes), not an executor's static PROVIDES. Use when only some
    #: param shapes need a host capability — ``paper_pull``'s
    #: ``upload_sha`` leg needs ``upload_staging`` on the claiming host;
    #: its ``url`` leg needs nothing (gr537770).
    conditional_requires: Callable[[dict[str, Any]], frozenset[str]] | None = None
    #: Optional queue TTL in seconds (gr506771). A job of this type
    #: still ``STATUS:queued`` past it is *cancelled* by the sweeper with
    #: a reason — a run whose input has rotted (a whole-graph pass
    #: re-scanning state from hours ago) is worse than a skipped tick.
    #: ``None`` (default): queued jobs wait indefinitely, as before.
    queue_ttl_s: int | None = None
    #: Stateless live-scan exemption from ``queue_ttl_s`` (Reto ruling
    #: 2026-10-23, gr508975 option (a)) — the explicit, named set of job
    #: types the sweeper's overqueued reap never touches. A type whose
    #: claim-time work scans live graph state has no enqueued input that
    #: rots while it waits, and ``dedupe_pending`` already bounds its
    #: backlog to one, so cancelling it past a TTL only starves the
    #: cadence. Declared *instead of* ``queue_ttl_s`` — ``__post_init__``
    #: refuses both together, so re-arming a TTL on an exempt type is a
    #: reviewed flag removal here, not silent drift. Setting this on a
    #: type whose enqueued input DOES rot is a correctness bug, not a
    #: tuning choice.
    queue_ttl_exempt: bool = False

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
        if self.queue_ttl_exempt and self.queue_ttl_s is not None:
            raise ValueError(
                f"job_type {self.name!r}: queue_ttl_exempt and queue_ttl_s "
                "are mutually exclusive — an exempt type declares no TTL "
                "(gr508975)"
            )
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


def defer_on_pause(ctx: Any, result: Any, *, label: str) -> bool:
    """Park the job on a window-scoped LLM pause instead of failing it
    (gr482938 — the plugin-dispatch analog of a coordinator ``Yield``).

    Call this first on any ``route()`` result (or ``DispatchError``)
    before falling into the ``record_failure`` path: a ``paused`` result
    — a breaker trip (claude quota taper / dollar cap), a
    quota-exhaustion notice, a saturated local slot — is a
    wait-for-window, not a defect. ``ctx.defer`` parks the SAME job as
    ``STATUS:waiting_time`` until the router's ``retry_at`` (the parsed
    window reset, else the executor's default backoff), so the work is
    retried when the window reopens instead of landing as a failed job
    that trips the all-failing watchdog.

    Returns ``True`` when the job parked — the dispatcher must then
    return with no terminal status (the finalize pass leaves a parked
    job alone). ``False`` on a non-pause result, when the ctx carries no
    ``defer`` surface, or once the job has already parked
    ``_common.MAX_PAUSE_PARKS`` times — the caller then handles
    ``result.error`` normally, so a never-lifting pause still fails.

    A ``timed_out`` result is deliberately NOT a park: it is
    ``paused=True`` too, but nothing rolls off — re-running the
    identical call re-burns the same wall-clock ceiling (quest_tick's
    ``pause_kind == "timeout"`` ruling), so it keeps failing.
    """
    if not getattr(result, "paused", False) or getattr(result, "timed_out", False):
        return False
    defer = getattr(ctx, "defer", None)
    if defer is None:
        return False
    wake_at = getattr(result, "retry_at", None)
    when = wake_at.isoformat() if wake_at is not None else "the default backoff"
    err = getattr(result, "error", None) or "paused"
    return bool(
        defer(
            wake_at,
            f"{label}: LLM dispatch paused (window-scoped) — {err}; "
            f"re-queued at {when} or on /budget resume",
        )
    )


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


def _load_conflict_sweep() -> JobTypeSpec:
    # Dispatch glue for one claim hub's conflict sweep (negated-paraphrase
    # ANN + budgeted verify + disputes edge + coverage ledger). Thin: the
    # mechanism lives in workers/conflict_search.py; this job and the
    # standing pass are two doors onto the same sweep.
    from precis.workers.job_types import conflict_sweep

    return conflict_sweep.SPEC


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


def _load_remarkable_papers_send() -> JobTypeSpec:
    # Deterministic cited-sources-PDFs → reMarkable upload (plugin dispatch).
    from precis.workers.job_types import remarkable_papers_send

    return remarkable_papers_send.SPEC


def _load_remarkable_reading_send() -> JobTypeSpec:
    # Deterministic per-source reading-edition typeset + upload (plugin dispatch).
    from precis.workers.job_types import remarkable_reading_send

    return remarkable_reading_send.SPEC


def _load_news_poll() -> JobTypeSpec:
    # Deterministic RSS ingestion pass (runs via plugin dispatch).
    from precis.workers.job_types import news_poll

    return news_poll.SPEC


def _load_money_refresh() -> JobTypeSpec:
    # Deterministic FX/CPI cache refresh (runs via plugin dispatch).
    from precis.workers.job_types import money_refresh

    return money_refresh.SPEC


def _load_pourbaix_bulk() -> JobTypeSpec:
    # Deterministic bulk Pourbaix verdict for a quest candidate (runs via
    # plugin dispatch under claude_inproc; needs the [pourbaix] extra).
    from precis.workers.job_types import pourbaix_bulk

    return pourbaix_bulk.SPEC


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


def _load_struct_search() -> JobTypeSpec:
    # AGOX/GOFEE surrogate search on the GPU node via ssh_node, in-process
    # (no container). Runs via plugin dispatch.
    from precis.workers.job_types import struct_search

    return struct_search.SPEC


def _load_struct_neb() -> JobTypeSpec:
    # One CI-NEB edge between two structure endpoints, in-process on the
    # GPU node via ssh_node (gr512126, slice 3). Runs via plugin dispatch.
    from precis.workers.job_types import struct_neb

    return struct_neb.SPEC


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


def _load_run_slice() -> JobTypeSpec:
    # Dark factory step 6: one slice headless on a lane (claude -p in its
    # own worktree), the detached submit/poll pair under ssh_node. Registers
    # unconditionally; PRECIS_FACTORY=off refuses at validate_submit.
    from precis.workers.job_types import run_slice

    return JobTypeSpec(
        name="run_slice",
        params_schema=run_slice.PARAMS_SCHEMA,
        compatible_executors=run_slice.COMPATIBLE_EXECUTORS,
        requires=run_slice.REQUIRES,
        description=run_slice.DESCRIPTION,
        validate_submit=run_slice.validate_submit,
        submit=run_slice.submit,
        poll=run_slice.poll,
        kill=run_slice.kill,
        wall_seconds=run_slice.wall_seconds,
    )


def _load_diagnose_gripe() -> JobTypeSpec:
    # Read-only root-cause diagnosis appended to a gripe as a
    # gripe_comment (see precis.workers.job_types.diagnose_gripe) — never fixes,
    # branches, pushes, or flips STATUS. Runs via plugin dispatch under
    # claude_inproc. Registers unconditionally (like sandbox_run): the
    # scan pass that mints these jobs (diagnose_scan) is the dark switch,
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


def _load_pcb_place() -> JobTypeSpec:
    # Placement-only anneal over a pcb design's graph (pcb-guided-place-
    # route Slice 10 tool surface) — the enqueued half of
    # put(kind='pcb', args={'op': 'place'}). Runs via plugin dispatch under
    # the job_inproc executor.
    from precis.workers.job_types import pcb_place

    return pcb_place.SPEC


def _load_datasheet_pull() -> JobTypeSpec:
    # Fetch + ingest + link one LCSC part's datasheet — the enqueued half of
    # the pcb datasheet auto-pull (minted by put(kind='pcb')). Runs via
    # plugin dispatch under the job_inproc executor.
    from precis.workers.job_types import datasheet_pull

    return datasheet_pull.SPEC


def _load_pcb_argue() -> JobTypeSpec:
    # docs/backlog/pcb-argue-with-design.md slice 2: answer a pcb design's
    # open argument notes in place (resolve handles, run the real
    # machinery, store origin='proposed' answer notes). Plugin dispatch
    # under job_inproc.
    from precis.workers.job_types import pcb_argue

    return pcb_argue.SPEC


def _load_pcb_review() -> JobTypeSpec:
    # docs/backlog/pcb-datasheet-review.md slice 7: work the pcb-tapeout
    # judgment items that are not checked or stale, one fresh-context
    # call each, machine verdicts anchored + quoted. Plugin dispatch
    # under job_inproc.
    from precis.workers.job_types import pcb_review

    return pcb_review.SPEC


def _load_paper_pull() -> JobTypeSpec:
    # Fetch an agent-supplied PDF URL into the watch inbox with a fetch
    # sidecar for a paper stub — the enqueued half of
    # put(kind='paper', args={'pdf_url': ...}). job_inproc plugin dispatch.
    from precis.workers.job_types import paper_pull

    return paper_pull.SPEC


def _load_paper_reextract() -> JobTypeSpec:
    # Re-extract a paper's body from its stored PDF — the enqueued half
    # of edit(kind='paper', args={'reextract': True}) (gr517423 slice 4).
    # job_inproc plugin dispatch.
    from precis.workers.job_types import paper_reextract

    return paper_reextract.SPEC


def _load_pdf_attach() -> JobTypeSpec:
    # Identity-gate a stored PDF against a bodiless target and move/
    # attach it — the enqueued half of edit(kind='paper',
    # args={'move_pdf_to': …} / {'attach_pdf': …}) (gr517423 slice 5).
    # job_inproc plugin dispatch.
    from precis.workers.job_types import pdf_attach

    return pdf_attach.SPEC


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
    if name == "elsevier_abstract_backfill":
        from precis.workers.job_types.elsevier_abstract_backfill import SPEC

        _REGISTRY[name] = SPEC
        return SPEC
    if name == "reground_claim":
        _REGISTRY["reground_claim"] = _load_reground_claim()
        return _REGISTRY["reground_claim"]
    if name == "conflict_sweep":
        _REGISTRY["conflict_sweep"] = _load_conflict_sweep()
        return _REGISTRY["conflict_sweep"]
    if name == "draft_refresh":
        _REGISTRY["draft_refresh"] = _load_draft_refresh()
        return _REGISTRY["draft_refresh"]
    if name == "remarkable_send":
        _REGISTRY["remarkable_send"] = _load_remarkable_send()
        return _REGISTRY["remarkable_send"]
    if name == "remarkable_papers_send":
        _REGISTRY["remarkable_papers_send"] = _load_remarkable_papers_send()
        return _REGISTRY["remarkable_papers_send"]
    if name == "remarkable_reading_send":
        _REGISTRY["remarkable_reading_send"] = _load_remarkable_reading_send()
        return _REGISTRY["remarkable_reading_send"]
    if name == "news_poll":
        _REGISTRY["news_poll"] = _load_news_poll()
        return _REGISTRY["news_poll"]
    if name == "money_refresh":
        _REGISTRY["money_refresh"] = _load_money_refresh()
        return _REGISTRY["money_refresh"]
    if name == "pourbaix_bulk":
        _REGISTRY["pourbaix_bulk"] = _load_pourbaix_bulk()
        return _REGISTRY["pourbaix_bulk"]
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
    if name == "struct_search":
        _REGISTRY["struct_search"] = _load_struct_search()
        return _REGISTRY["struct_search"]
    if name == "struct_neb":
        _REGISTRY["struct_neb"] = _load_struct_neb()
        return _REGISTRY["struct_neb"]
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
    if name == "run_slice":
        _REGISTRY["run_slice"] = _load_run_slice()
        return _REGISTRY["run_slice"]
    if name == "plan_dispatch":
        # Dark-factory plan tick (gates → loops → phases → dispatch →
        # hygiene). Plugin dispatch under job_inproc, minted every 5 min by
        # an operator-armed recurring todo (see the module docstring).
        from precis.workers.job_types.plan_dispatch import SPEC

        _REGISTRY[name] = SPEC
        return SPEC
    if name == "gripe_validate":
        # Gripe ladder's last rung: verify -> done once the resolved sha is in
        # the recorded prod head. Armed by a recurring todo (see its docstring).
        from precis.workers.job_types.gripe_validate import SPEC

        _REGISTRY[name] = SPEC
        return SPEC
    if name == "diagnose_gripe":
        _REGISTRY["diagnose_gripe"] = _load_diagnose_gripe()
        return _REGISTRY["diagnose_gripe"]
    if name == "graph_garden":
        from precis.workers.job_types.graph_garden import SPEC

        _REGISTRY[name] = SPEC
        return SPEC
    if name == "secret_status":
        # Deterministic credential check for the /secrets stored status —
        # plugin dispatch under job_inproc; no LLM, no secret in meta.
        from precis.workers.job_types.secret_status import SPEC

        _REGISTRY[name] = SPEC
        return SPEC
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
    if name == "pcb_place":
        _REGISTRY["pcb_place"] = _load_pcb_place()
        return _REGISTRY["pcb_place"]
    if name == "datasheet_pull":
        _REGISTRY["datasheet_pull"] = _load_datasheet_pull()
        return _REGISTRY["datasheet_pull"]
    if name == "datasheet_facts":
        # One BIG-tier read of a pulled datasheet -> the part ref's fact
        # block (datasheet-facts-mesh). Plugin dispatch under claude_inproc.
        from precis.workers.job_types.datasheet_facts import SPEC

        _REGISTRY[name] = SPEC
        return SPEC
    if name == "pcb_argue":
        _REGISTRY["pcb_argue"] = _load_pcb_argue()
        return _REGISTRY["pcb_argue"]
    if name == "pcb_review":
        _REGISTRY["pcb_review"] = _load_pcb_review()
        return _REGISTRY["pcb_review"]
    if name == "paper_pull":
        _REGISTRY["paper_pull"] = _load_paper_pull()
        return _REGISTRY["paper_pull"]
    if name == "paper_reextract":
        _REGISTRY["paper_reextract"] = _load_paper_reextract()
        return _REGISTRY["paper_reextract"]
    if name == "pdf_attach":
        _REGISTRY["pdf_attach"] = _load_pdf_attach()
        return _REGISTRY["pdf_attach"]
    if name == "deferred_llm_call":
        # A synchronous surface's LLM call deferred past a quota/budget
        # window (web follow-up first). Plugin dispatch under coordinator.
        from precis.workers.job_types.deferred_llm_call import SPEC

        _REGISTRY[name] = SPEC
        return SPEC
    if name == "survey_extract":
        # The survey/review numeric extraction pass — local reader over a
        # paper's chunks writing anchored measure runs, then a spawned
        # measure_accept child. Plugin dispatch under coordinator.
        from precis.workers.job_types.survey_extract import SPEC

        _REGISTRY[name] = SPEC
        return SPEC
    if name == "measure_accept":
        # The sampling-acceptance half of survey_extract — stratified
        # sample + frontier verdicts + Wilson rule. Also standalone.
        from precis.workers.job_types.survey_extract import ACCEPT_SPEC

        _REGISTRY[name] = ACCEPT_SPEC
        return ACCEPT_SPEC
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
        "elsevier_abstract_backfill",
        "reground_claim",
        "conflict_sweep",
        "draft_refresh",
        "remarkable_send",
        "remarkable_papers_send",
        "remarkable_reading_send",
        "news_poll",
        "money_refresh",
        "pourbaix_bulk",
        "briefing",
        "reading_brief",
        "meditation",
        "card_forge",
        "struct_relax",
        "struct_search",
        "struct_neb",
        "structure_propose",
        "cad_propose",
        "diagram_propose",
        "cad_discuss",
        "sandbox_run",
        "run_slice",
        "plan_dispatch",
        "gripe_validate",
        "diagnose_gripe",
        "graph_garden",
        "secret_status",
        "good_search",
        "good_search_triage",
        "quest_tick",
        "embed_batch",
        "derived_drain",
        "pcb_place",
        "datasheet_pull",
        "datasheet_facts",
        "pcb_argue",
        "pcb_review",
        "paper_pull",
        "paper_reextract",
        "pdf_attach",
        "deferred_llm_call",
        "survey_extract",
        "measure_accept",
    ]
    plugin_names = sorted(_get_plugin_specs())
    # Built-ins first so the error-message ordering is stable for
    # callers that have only ever seen the in-tree set.
    return builtins + [n for n in plugin_names if n not in builtins]


__all__ = [
    "JOB_TYPE_PLUGIN_GROUP",
    "JobTypeSpec",
    "_reset_plugin_cache",
    "defer_on_pause",
    "get_job_type",
    "known_job_types",
]

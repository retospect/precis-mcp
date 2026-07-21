"""``plan_tick`` job_type — one LLM tick of the planner coroutine.

The dispatcher mints a ``plan_tick`` job under every ``LLM:*``-tagged
todo that has no live job and no live open children. The transport is
the router's decision (``select_transport`` + ``resolve_backend``):

* under the default **ANTHROPIC** backend the tick runs as a real
  ``claude -p`` agent (MCP tools, OAuth Max subscription) *through the
  router* (the ``CLAUDE_AGENT`` transport, :func:`_run_claude_tick`) —
  ``bypassPermissions`` + env-back-door context;
* under a tools-capable OSS backend (``PRECIS_LLM_BACKEND=openai``) it
  drives the precis verbs in-process over the OSS ``tools=`` loop (the
  ``OPENAI_TOOLS`` transport, :func:`_run_oss_tick`, ADR 0024/0046),
  binding context in a ContextVar.

What the planner does during the tick is its own call (mint
children, yield to user, halt, or finish). The runner doesn't
interpret the output — it captures the final text as a
``job_summary`` chunk under the job ref, and lets the dispatcher's
next sweep notice whatever state the planner set.

Closed vocab: ``meta.params`` carries ``model`` (one of
``opus|sonnet|haiku``) plus an optional ``timeout_s``. The model is
synthesized from the parent's ``LLM:<value>`` tag at dispatch time;
callers normally don't write ``params`` directly.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from precis.utils.llm.router import (
    PLANNER_MODEL_ALIASES,
    Tier,
    Transport,
    resolve_backend,
    select_transport,
)
from precis.utils.llm.router import (
    PLANNER_TIER_BY_ALIAS as _TIER_BY_ALIAS,
)

log = logging.getLogger(__name__)


#: Per-tick ``--max-turns`` ceiling for the planner subprocess. A research +
#: write section tick (read chunks, search the corpus, mint citations, write
#: paragraphs) legitimately needs many tool-call turns; 30 was too tight for
#: citation-dense sections (the agent front-loaded research and hit the wall
#: before writing — bubbling as a resume-streak failure), so the default is
#: 60. A tick that still hits the ceiling is a *resumable exhaustion*, not a
#: hard failure (see ``executors/claude_inproc._resume_reason``). Override via
#: ``PRECIS_PLAN_TICK_MAX_TURNS``.
_DEFAULT_MAX_TURNS: int = 60


#: Per-tick cost ceiling for the claude tick (``--max-budget-usd``). Higher than
#: the ``claude_agent`` default ($2) because a plan_tick is a multi-turn agentic
#: run that legitimately spends more than a one-shot call, so a lower cap would
#: truncate normal ticks. It is a runaway-spend backstop, not a normal-tick
#: limit; a tick cut off by the cap is treated as a *resumable exhaustion* (the
#: same handling as ``--max-turns`` / timeout). Override via
#: ``PRECIS_PLAN_TICK_MAX_USD``.
_DEFAULT_MAX_USD: float = 5.00


DESCRIPTION: str = (
    "one LLM planner tick on an LLM:*-tagged todo — reads body + "
    "child summaries, mints children / yields / finishes"
)


PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "model": {
            "type": "string",
            "enum": list(PLANNER_MODEL_ALIASES),
            "description": "Which capability tier to run (the router maps it to "
            "a claude or served OSS model per the active backend). Synthesized "
            "from the parent's LLM:<value> tag at dispatch time.",
        },
        "timeout_s": {
            "type": "integer",
            "minimum": 30,
            "maximum": 3600,
            "description": "Wall-clock cap on the tick. Default 1800s (30 min).",
        },
    },
    "required": ["model"],
    "additionalProperties": False,
}


COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"claude_inproc"})


#: Under the ANTHROPIC backend the tick spawns ``claude -p`` (needs
#: ``claude_bin``) with an MCP config so the agent can call back via the precis
#: tools (needs ``mcp_config``); both are provided by the ``claude_inproc``
#: executor. The OSS-backend path uses neither (the loopback litellm proxy),
#: but requiring them is harmless — the executor provides them regardless.
REQUIRES: frozenset[str] = frozenset({"claude_bin", "mcp_config"})


@dataclass(frozen=True)
class PlanTickOutcome:
    """Result of one planner tick.

    ``stdout`` is captured for the ``job_summary`` chunk. ``exit_code``
    decides the job's STATUS (0 → succeeded, non-zero → failed).
    """

    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    #: Explicit resumable-exhaustion signal from a non-claude transport (the
    #: in-process OSS tools loop, which emits no stream-json for the executor's
    #: ``_resume_reason`` to parse). ``None`` on the claude path — its resume
    #: reason stays derived from the stream + exit code, unchanged/byte-identical.
    resume_reason: str | None = None


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    """Submit-time check. Today: only validates the model value.

    ``gripe_id`` is ignored — plan_tick parents are todos, not gripes.
    Kept in the signature for the registry's uniform interface.
    """
    del gripe_id
    model = params.get("model")
    if model not in {"opus", "sonnet", "haiku"}:
        return (
            f"plan_tick: params.model must be one of "
            f"[opus, sonnet, haiku], got {model!r}"
        )
    return None


def run(
    *,
    store: Any,
    job_ref_id: int,
    parent_ref_id: int,
    params: dict[str, Any],
    log_event: Any = None,
    **_kw: Any,
) -> PlanTickOutcome:
    """Run one planner tick under ``parent_ref_id`` and return the outcome.

    The runner builds the prompts via
    :func:`precis.workers.planner_prompt.build_planner_prompts`, then
    drives the precis verbs in-process over the OSS ``tools=`` loop
    (:func:`_run_oss_tick`). The transport + model are the router's
    decision (:func:`~precis.utils.llm.router.select_transport` +
    :func:`~precis.utils.llm.router.resolve_backend`): a served OSS
    model (``LOCAL_BIG``) by default, or the tag's cloud tier when the
    backend is flipped to a tools-capable cloud.
    """
    from precis.workers.planner_prompt import build_planner_prompts

    model = params["model"]
    timeout_s = int(params.get("timeout_s", 1800))
    started = time.monotonic()

    # Patent tick: refresh the freedom-to-operate claims digest onto this
    # tick's ``meta.working_set`` before the prompt is built, so the planner
    # injects the prior-art claims the tick must design around. Best-effort.
    _refresh_patent_claims_digest(store, parent_ref_id)

    prompts = build_planner_prompts(store, ref_id=parent_ref_id, model=model)

    workspace = _load_parent_workspace(store, parent_ref_id)

    # Open a run-attribution record (kind='agentlog') carrying the full
    # assembled prompt; the in-process tools loop threads its id via the tick
    # ContextVar (:func:`precis.utils.inproc_context.tick_context`) so every
    # draft chunk this tick writes/moves attributes back to this run (a
    # `touched` link). Best-effort: a failure here must never abort the tick.
    from precis import agentlog

    agentlog_id: int | None = None
    try:
        agentlog_id = agentlog.open_log(
            store,
            source="plan_tick",
            title=f"plan_tick #{parent_ref_id} ({model})",
            model=model,
            prompt=f"{prompts.system}\n\n──── USER ────\n\n{prompts.user}",
            parent_ref_id=parent_ref_id,
            job_ref_id=job_ref_id,
        )
    except Exception:
        log.warning("plan_tick: failed to open agentlog", exc_info=True)

    def _finalize(status: str) -> None:
        if agentlog_id is None:
            return
        try:
            agentlog.finalize_log(store, log_id=agentlog_id, status=status)
        except Exception:
            log.warning("plan_tick: failed to finalize agentlog", exc_info=True)

    transport = select_transport(
        _TIER_BY_ALIAS.get(model, Tier.CLOUD_SUPER),
        tools_needed=True,
        backend=resolve_backend(),
    )
    if transport is Transport.CLAUDE_AGENT:
        outcome = _run_claude_tick(
            store=store,
            prompts=prompts,
            model=model,
            parent_ref_id=parent_ref_id,
            agentlog_id=agentlog_id,
            workspace=workspace,
            timeout_s=timeout_s,
            started=started,
            job_ref_id=job_ref_id,
            log_event=log_event,
        )
    else:
        outcome = _run_oss_tick(
            store=store,
            prompts=prompts,
            model=model,
            parent_ref_id=parent_ref_id,
            agentlog_id=agentlog_id,
            workspace=workspace,
            timeout_s=timeout_s,
            started=started,
            job_ref_id=job_ref_id,
            log_event=log_event,
        )
    _finalize("ok" if outcome.exit_code == 0 else f"exit:{outcome.exit_code}")
    return outcome


def _run_oss_tick(
    *,
    store: Any,
    prompts: Any,
    model: str,
    parent_ref_id: int,
    agentlog_id: int | None,
    workspace: Any,
    timeout_s: int,
    started: float,
    job_ref_id: int,
    log_event: Any,
) -> PlanTickOutcome:
    """Run one planner tick over the in-process OSS ``tools=`` loop (the
    ``OPENAI_TOOLS`` transport), instead of spawning ``claude -p``.

    Goes *through* ``router.dispatch`` — so the OSS tick gains the same breaker
    gate + route-log the claude tick does — rather than calling the loop
    directly. ``dispatch`` runs the provider synchronously in this thread, so
    the loop still executes inside the ``tick_context`` block below.

    The spawned-claude tick hands its runtime context to the subprocess via env
    back-doors (``PRECIS_CURRENT_TODO`` &c.); the OSS loop drives
    ``runtime.dispatch`` *in this process*, so those env vars would resolve
    against the worker's environment — and clobber under thread concurrency.
    We bind the context in a thread-isolated ContextVar
    (:func:`precis.utils.inproc_context.tick_context`) instead, for the
    synchronous span of the loop; the verb readers consult it first. So
    children default under the right parent, file-kinds route by the workspace,
    draft writes attribute to this run's agentlog, and the draft-bound
    prose-file kind is prohibited for the tick (``disabled_kinds`` — the
    in-process twin of the claude path's ``PRECIS_KINDS_DISABLED``) — with no
    env mutation and no cross-tick bleed.

    Maps the result onto :class:`PlanTickOutcome` (:func:`_oss_exit_from_result`):
    a breaker/slot pause → resumable ``paused``; ``stop`` → clean exit 0;
    ``max_turns`` → a *resumable exhaustion* (exit 1 + ``resume_reason`` so the
    executor bumps the streak and re-mints, matching the claude ``--max-turns``
    path); a transport ``error`` → a real failure.
    """
    from precis.utils.inproc_context import TickContext, tick_context
    from precis.utils.llm.router import LlmRequest, dispatch

    tier = _resolve_oss_tier(model)
    # Tools are advertised iff mcp_config is non-None (router contract); the OSS
    # loop drives the verbs in-process so it never reads the file, but the tick
    # needs precis tools to act. PRECIS_MCP_CONFIG is a claude_inproc executor
    # capability present in the worker env under either backend.
    mcp_config = os.environ.get("PRECIS_MCP_CONFIG", "")
    if not mcp_config:
        log.warning(
            "plan_tick: PRECIS_MCP_CONFIG unset; the OSS tick advertises no "
            "precis tools — the planner can't mint children / write / finish"
        )
    gate = _draft_prose_kind_gate(store, parent_ref_id)
    ctx = TickContext(
        parent_todo=parent_ref_id,
        workspace=workspace.path if workspace is not None else None,
        model=model,
        agentlog_id=agentlog_id,
        disabled_kinds=(gate,) if gate is not None else (),
    )
    if log_event:
        log_event(
            "plan_tick.spawn",
            {
                "job_ref_id": job_ref_id,
                "parent_ref_id": parent_ref_id,
                "model": model,
                "transport": "openai_tools",
                "system_chars": len(prompts.system),
                "user_chars": len(prompts.user),
            },
        )
    try:
        with tick_context(ctx):
            result = dispatch(
                LlmRequest(
                    tier=tier,
                    prompt=prompts.user,
                    tools_needed=True,
                    system_prompt=prompts.system,
                    mcp_config=os.path.abspath(mcp_config) if mcp_config else None,
                    max_turns=_max_turns(),
                    timeout_s=timeout_s,
                    source="plan_tick",
                    ref_id=parent_ref_id,
                )
            )
    except (RuntimeError, OSError) as exc:
        duration = time.monotonic() - started
        log.warning(
            "plan_tick: OSS tick for parent #%d could not run: %s",
            parent_ref_id,
            exc,
        )
        return PlanTickOutcome(
            exit_code=1,
            stdout="",
            stderr=f"plan_tick OSS tick error: {exc}",
            duration_s=duration,
        )
    duration = time.monotonic() - started
    exit_code, resume_reason = _oss_exit_from_result(result)
    return PlanTickOutcome(
        exit_code=exit_code,
        stdout=result.text,
        stderr=result.error or "",
        duration_s=duration,
        resume_reason=resume_reason,
    )


def _oss_exit_from_result(result: Any) -> tuple[int, str | None]:
    """Map the OSS-loop router ``LlmResult`` → ``(exit_code, resume_reason)``.

    A breaker / local-slot pause → a resumable ``'paused'`` (retry when the
    window clears / a slot frees, bounded by the executor's per-parent streak
    cap). A transport / build / admission error → a hard failure. Otherwise
    defer to the loop's ``stop_reason`` (:func:`_oss_exit`).
    """
    if result.paused:
        return 1, "paused"
    if result.error:
        return 1, None
    return _oss_exit(result.stop_reason or "")


def _oss_exit(stop_reason: str) -> tuple[int, str | None]:
    """Map the OSS loop's ``stop_reason`` → ``(exit_code, resume_reason)`` for
    the ``claude_inproc`` executor.

    ``stop`` (model answered) → clean exit 0, no resume. ``max_turns`` (turn
    ceiling) → a resumable exhaustion: exit 1 + ``resume_reason='max_turns'`` so
    the executor bumps the per-parent streak and re-mints a fresh tick (bounded
    by the cap) rather than parking the parent — the same treatment as the
    claude ``--max-turns`` cutoff. ``error`` / anything else → a real failure:
    exit 1, no resume, so it bubbles.
    """
    if stop_reason == "stop":
        return 0, None
    if stop_reason == "max_turns":
        return 1, "max_turns"
    return 1, None


def _run_claude_tick(
    *,
    store: Any,
    prompts: Any,
    model: str,
    parent_ref_id: int,
    agentlog_id: int | None,
    workspace: Any,
    timeout_s: int,
    started: float,
    job_ref_id: int,
    log_event: Any,
) -> PlanTickOutcome:
    """Run one planner tick as a ``claude -p`` agent through the router.

    Selected under the ANTHROPIC backend (:func:`~precis.utils.llm.router.select_transport`
    → ``CLAUDE_AGENT``): the real Claude Code agent, MCP tools enabled, authed
    off the OAuth Max subscription. It goes *through* ``router.dispatch`` — so it
    gains the breaker gate + route-log — rather than hand-building a ``claude``
    command.

    The spawned subprocess can't read the in-process ContextVar the OSS loop
    binds, so the tick's runtime context (parent todo / model / workspace /
    agentlog id / draft kind-gate) is threaded via ``env_overlay`` the subprocess
    (and its MCP server) inherits, and the run happens from a CLAUDE.md-free
    neutral cwd (ADR 0051 §12) so no ambient project persona is prepended outside
    the assembler.

    ``call_claude_agent`` defaults to ``--permission-mode bypassPermissions`` —
    the fix for the prod incident where the bespoke spawn used ``acceptEdits``
    (auto-approve *edits* only) and every MCP *tool* call was denied, halting the
    tick. The outcome mapping (:func:`_claude_exit`) preserves the resumable-
    exhaustion semantics: ``max_turns`` / ``budget`` / timeout / a breaker pause
    become a resumable signal rather than a hard failure.
    """
    from precis.utils.llm.router import LlmRequest, dispatch

    tier = _TIER_BY_ALIAS.get(model, Tier.CLOUD_SUPER)
    mcp_config = os.environ.get("PRECIS_MCP_CONFIG", "")
    if not mcp_config:
        log.warning(
            "plan_tick: PRECIS_MCP_CONFIG unset; the claude tick can't call back "
            "via MCP — children/yield/done won't land"
        )
    # ADR 0051 §12 — run from a neutral cwd so `claude -p` discovers no project
    # CLAUDE.md, and surface any ambient one (user file or up the cwd tree) that
    # would still be prepended outside the assembler and bust the cache prefix.
    cwd = _neutral_cwd()
    ambient = _ambient_claude_md_paths(cwd)
    if ambient:
        log.warning(
            "plan_tick: ambient CLAUDE.md would contaminate the persona floor "
            "outside the assembler (ADR 0051 §12) — remove it on agent hosts: %s",
            ambient,
        )
    env_overlay = _tick_env_overlay(
        store=store,
        parent_ref_id=parent_ref_id,
        model=model,
        agentlog_id=agentlog_id,
        workspace=workspace,
    )
    if log_event:
        log_event(
            "plan_tick.spawn",
            {
                "job_ref_id": job_ref_id,
                "parent_ref_id": parent_ref_id,
                "model": model,
                "transport": "claude_agent",
                "system_chars": len(prompts.system),
                "user_chars": len(prompts.user),
                "cwd": cwd,
                "ambient_claude_md": ambient,
            },
        )
    result = dispatch(
        LlmRequest(
            tier=tier,
            prompt=prompts.user,
            tools_needed=True,
            system_prompt=prompts.system,
            # Absolute so the neutral cwd can't strand a relative path.
            mcp_config=os.path.abspath(mcp_config) if mcp_config else None,
            max_turns=_max_turns(),
            max_usd=_max_budget_usd(),
            timeout_s=timeout_s,
            # Full message stream (every turn + tool call/result) so the executor
            # can store a debuggable transcript and parse the terminal reason.
            # ``--verbose`` is required alongside ``stream-json`` in ``-p`` mode.
            output_format="stream-json",
            extra_args=("--verbose",),
            env_overlay=env_overlay,
            cwd=cwd,
            source="plan_tick",
            ref_id=parent_ref_id,
        )
    )
    duration = time.monotonic() - started
    exit_code, resume_reason = _claude_exit(result)
    return PlanTickOutcome(
        exit_code=exit_code,
        # The full stream-json stdout (``raw_text``) for the executor's transcript
        # + resume parse; a genuine error carries only the partial ``text``.
        stdout=result.raw_text or result.text or "",
        stderr=result.error or "",
        duration_s=duration,
        resume_reason=resume_reason,
    )


def _claude_exit(result: Any) -> tuple[int, str | None]:
    """Map a router ``LlmResult`` from the claude tick → ``(exit_code, resume_reason)``.

    A breaker / quota-window pause → a resumable ``'paused'`` (retry when the
    window clears, bounded by the executor's per-parent streak cap). A transport
    timeout → resumable ``'timeout'``. A recovered exhaustion (``max_turns`` / a
    ``budget``-class reason, surfaced on ``terminal_reason`` because
    ``call_claude_agent`` swallows the recoverable non-zero exit) → the matching
    resumable signal. A clean run (or a ``completed`` terminal reason on a
    process-teardown exit) → clean exit 0. Any other error → a hard failure.
    """
    if result.paused:
        return 1, "paused"
    if result.error is not None:
        if "timed out" in result.error.lower():
            return 1, "timeout"
        return 1, None
    tr = result.terminal_reason
    if tr == "max_turns":
        return 1, "max_turns"
    if tr is not None and "budget" in tr:
        return 1, "budget"
    if tr is None or tr == "completed":
        return 0, None
    return 1, None


def _tick_env_overlay(
    *,
    store: Any,
    parent_ref_id: int,
    model: str,
    agentlog_id: int | None,
    workspace: Any,
) -> dict[str, str]:
    """The env back-doors the spawned MCP server reads to bind this tick's
    runtime context — the claude path's equivalent of the OSS loop's ContextVar:

    * ``PRECIS_CURRENT_TODO`` — the parent todo, so ``put(kind='todo', ...)``
      auto-parents children under it (``utils/workspace.current_todo_from_env``).
    * ``PRECIS_CURRENT_MODEL`` — the tier the tick runs on, for the LLM's own
      degrade / escalate decisions.
    * ``PRECIS_WORKSPACE`` — the parent's workspace path, so file-kinds route by
      the layout convention.
    * the agentlog id (``agentlog.ENV_VAR``) — so draft chunks this tick writes /
      moves attribute back to the run (a ``touched`` link).
    * ``PRECIS_KINDS_DISABLED`` — the draft-bound prose-file kind-gate.
    """
    from precis import agentlog

    overlay: dict[str, str] = {
        "PRECIS_CURRENT_TODO": str(parent_ref_id),
        "PRECIS_CURRENT_MODEL": model,
    }
    if workspace is not None:
        overlay["PRECIS_WORKSPACE"] = workspace.path
    if agentlog_id is not None:
        overlay[agentlog.ENV_VAR] = str(agentlog_id)
    _disable_prose_file_kind(store, parent_ref_id, overlay)
    return overlay


def _draft_prose_kind_gate(store: Any, parent_ref_id: int) -> tuple[str, str] | None:
    """The ``(kind, hint)`` prose-file kind to prohibit for this tick, or ``None``.

    When a draft is bound to the tick, the colliding file kind — the one whose
    files duplicate the draft body: ``tex`` for a tex-format draft, ``markdown``
    for a md-format one — is prohibited so the agent can't write the section to a
    freestanding ``kind='tex'``/``'markdown'`` file the draft never renders (the
    canonical store is the draft's chunks; the file is export-only). The ``hint``
    tells the agent what to do instead and surfaces verbatim in the ``Unsupported``
    error the gated verb raises.

    Shared by both transports: the claude path folds it into the
    ``PRECIS_KINDS_DISABLED`` env overlay (:func:`_disable_prose_file_kind`);
    the OSS path folds it into the tick ContextVar's ``disabled_kinds``. Best-
    effort: any lookup failure returns ``None`` — the ``## Draft`` prompt block
    still steers the agent, the gate is just the belt to its suspenders.
    """
    from precis.workers.planner_prompt import bound_draft

    try:
        resolved = bound_draft(store, parent_ref_id)
    except Exception:
        log.warning("plan_tick: bound_draft lookup failed", exc_info=True)
        return None
    if resolved is None:
        return None
    ident, _title, fmt = resolved
    kind = "markdown" if fmt.lower() in ("md", "markdown") else "tex"
    hint = (
        f"this project's deliverable is draft '{ident}' — write prose with "
        f"put(kind='draft' ...) or edit(id='dc<id>') as the '## Draft' block "
        f"in your prompt describes; the {kind} file kind is export-only output "
        f"here"
    )
    return kind, hint


def _disable_prose_file_kind(
    store: Any, parent_ref_id: int, overlay: dict[str, str]
) -> None:
    """Fold the draft prose-file kind-gate into the claude subprocess's
    ``PRECIS_KINDS_DISABLED`` env overlay (the claude-path side of
    :func:`_draft_prose_kind_gate`).

    Merges with any operator-set ``PRECIS_KINDS_DISABLED`` in the worker env
    (the gate reads a comma list); the hint carries no comma. No-op when no
    draft is bound.
    """
    from precis.config import load_config

    gate = _draft_prose_kind_gate(store, parent_ref_id)
    if gate is None:
        return
    kind, hint = gate
    # Tier-1 config var → read through load_config(), not os.environ
    # (docs/conventions/env-vars.md); PRECIS_KINDS_DISABLED backs
    # PrecisConfig.kinds_disabled. The overlay we build here is still an env
    # entry the *subprocess* MCP server parses at construction.
    existing = (load_config().kinds_disabled or "").strip()
    entry = f"{kind}:{hint}"
    overlay["PRECIS_KINDS_DISABLED"] = f"{existing},{entry}" if existing else entry


#: Process-wide neutral cwd for the claude tick (ADR 0051 §12). Lazily created,
#: reused across ticks (an empty dir needs no per-tick churn).
_NEUTRAL_CWD: str | None = None


def _neutral_cwd() -> str:
    """A stable, empty working directory the claude tick runs in so ``claude
    -p``'s *project* ``CLAUDE.md`` auto-discovery finds nothing (ADR 0051 §12).

    The turn-taker must own the entire system prompt: a tick's rendered system
    prompt has to equal the assembler's bytes. Running from the daemon's cwd lets
    ``claude`` discover a project ``CLAUDE.md`` up the tree and prepend it
    *outside* the assembler — a competing uncontrolled persona that also silently
    busts the cache prefix. A fresh temp dir (ancestors ``/tmp`` → ``/``, none
    carrying a ``CLAUDE.md``) removes that discovery surface without ``--bare``
    (which would force API-key auth and break OAuth). The *user* file
    ``~/.claude/CLAUDE.md`` is discovered regardless of cwd —
    :func:`_ambient_claude_md_paths` guards that."""
    global _NEUTRAL_CWD
    if _NEUTRAL_CWD is not None and os.path.isdir(_NEUTRAL_CWD):
        return _NEUTRAL_CWD
    _NEUTRAL_CWD = tempfile.mkdtemp(prefix="precis-plan-tick-cwd-")
    return _NEUTRAL_CWD


def _ambient_claude_md_paths(cwd: str) -> list[str]:
    """Every ``CLAUDE.md`` ``claude -p`` could auto-discover for a run in ``cwd``
    and prepend outside the assembler (ADR 0051 §12): the user file
    ``~/.claude/CLAUDE.md`` plus any project ``CLAUDE.md`` from ``cwd`` up to the
    filesystem root. An empty list means a clean persona environment — the
    rendered system prompt is exactly the assembler's bytes."""
    found: list[str] = []
    try:
        home_md = Path.home() / ".claude" / "CLAUDE.md"
        if home_md.is_file():
            found.append(str(home_md))
        base = Path(cwd).resolve()
        for d in (base, *base.parents):
            md = d / "CLAUDE.md"
            if md.is_file():
                found.append(str(md))
    except OSError:  # a stat failure must not abort the tick
        log.warning("plan_tick: CLAUDE.md ambient-scan failed", exc_info=True)
    return found


def _refresh_patent_claims_digest(store: Any, parent_ref_id: int) -> None:
    """For a **patent** tick with a bound draft, stamp the freedom-to-operate
    claims digest onto the tick's ``meta.working_set`` so the planner injects
    the prior-art claims (``docs/design/patent-authoring-loop.md``). Discovers
    the draft's linked prior-art patents and writes one eye per claim chunk.
    Best-effort — a digest failure must never sink a tick."""
    try:
        ws = _load_parent_workspace(store, parent_ref_id)
        if ws is None or ws.doc_type != "patent":
            return
        from precis.workers.planner_prompt import bound_draft

        resolved = bound_draft(store, parent_ref_id)
        if not resolved:
            return
        draft = store.get_ref(kind="draft", id=resolved[0])
        if draft is None:
            return
        from precis.workers.patent_digest import refresh_claims_digest

        refresh_claims_digest(store, parent_ref_id, draft.id)
    except Exception:  # pragma: no cover — enhancement, never fatal
        log.warning("plan_tick: patent claims-digest refresh failed", exc_info=True)


def _load_parent_workspace(store: Any, parent_ref_id: int):
    """Read meta.workspace from the parent todo, parse it.

    Returns a :class:`Workspace` or None when the todo carries no
    workspace block. Validation is lenient: malformed dicts log a
    warning and return None rather than raising. Cascade resilience.
    """
    from precis.utils.workspace import Workspace

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s",
            (parent_ref_id,),
        ).fetchone()
    if row is None:
        return None
    return Workspace.from_meta(row[0])


def _resolve_oss_tier(model: str) -> Tier:
    """Pick the capability tier the in-process tools loop runs on.

    The ``LLM:<tag>`` maps to a cloud tier (:data:`_TIER_BY_ALIAS`); that tier
    is used only when the router would actually route it to the OSS ``tools=``
    loop under the active backend (a tools-capable cloud such as OpenRouter).
    Otherwise — the default ``ANTHROPIC`` backend, whose cloud tiers route to
    ``claude -p`` — fall to ``LOCAL_BIG`` (a served OSS model), the tier that
    always drives the tools loop, so the tick runs on the capability the
    cluster actually has. Model selection stays in the ADR 0046 resolver.
    """
    tag_tier = _TIER_BY_ALIAS.get(model, Tier.CLOUD_SUPER)
    transport = select_transport(tag_tier, tools_needed=True, backend=resolve_backend())
    return tag_tier if transport is Transport.OPENAI_TOOLS else Tier.LOCAL_BIG


def _max_turns() -> int:
    """The planner subprocess's ``--max-turns`` ceiling.

    Reads ``PRECIS_PLAN_TICK_MAX_TURNS`` (an int) or falls back to
    :data:`_DEFAULT_MAX_TURNS`. A malformed value logs and falls back rather
    than crashing the tick.
    """
    raw = os.environ.get("PRECIS_PLAN_TICK_MAX_TURNS")
    if not raw:
        return _DEFAULT_MAX_TURNS
    try:
        return int(raw)
    except ValueError:
        log.warning(
            "plan_tick: PRECIS_PLAN_TICK_MAX_TURNS=%r is not an int; using %d",
            raw,
            _DEFAULT_MAX_TURNS,
        )
        return _DEFAULT_MAX_TURNS


def _max_budget_usd() -> float:
    """The claude tick's ``--max-budget-usd`` cap.

    Reads ``PRECIS_PLAN_TICK_MAX_USD`` (a float) or falls back to
    :data:`_DEFAULT_MAX_USD`. A malformed value logs and falls back rather than
    crashing the tick.
    """
    raw = os.environ.get("PRECIS_PLAN_TICK_MAX_USD")
    if not raw:
        return _DEFAULT_MAX_USD
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "plan_tick: PRECIS_PLAN_TICK_MAX_USD=%r is not a float; using $%.2f",
            raw,
            _DEFAULT_MAX_USD,
        )
        return _DEFAULT_MAX_USD

"""Project-wide wrapper around ``claude -p`` for **agentic** worker calls.

Peer to :mod:`precis.utils.claude_p`. Where ``claude_p`` is the one-shot
JSON-out shape (no tools, no system prompt, parse the last JSON block from
stdout), ``claude_agent`` is the multi-turn shape used by the dream pass and
the structural/deep reviewers (MCP tools enabled, optional system prompt,
side effects on the precis DB are the output — the final stdout text is
logged for audit but isn't the "result").

The wrapper adds: cost cap, wall-clock timeout, optional structured
:func:`log_event` write to ``ref_events`` for per-host attribution,
stub-friendly ``PRECIS_CLAUDE_BIN`` override (so tests don't need a real
claude binary).

Auth: by default inherits the calling user's auth (OAuth via ``~/.claude``,
container-baked API key, whatever). Pass ``bare=True`` to add ``--bare`` so
claude reads ``ANTHROPIC_API_KEY`` only — used by the ``fix_gripe`` job
executor where OAuth state isn't reachable.

Knobs (all overridable per call, project defaults via env):

* ``PRECIS_CLAUDE_BIN``       — claude binary path (default ``claude``).
* ``PRECIS_CLAUDE_AGENT_MODEL`` — default model (falls back to the router's
  ``Tier.FRONTIER`` = ``claude-opus-4-8``).
* ``PRECIS_CLAUDE_AGENT_MAX_USD`` — per-call cost cap (default ``2.00``).
* ``PRECIS_CLAUDE_AGENT_TIMEOUT_S`` — wall-clock timeout (default ``600``).
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from precis.utils._claude_subprocess import (
    ClaudeProcessError,
    extract_cost_usd,
    resolve_binary,
    run_claude,
    run_claude_async,
    to_str,
)
from precis.utils.claude_oauth import ensure_oauth_token, prefer_oauth_over_api_key
from precis.utils.friction_reflect import append_friction_footer

if TYPE_CHECKING:
    from precis.store import Store
    from precis.workers.executors.agent_container import Mount

log = logging.getLogger(__name__)

# Back-compat aliases: tests import these private names from this module.
_to_str = to_str
_extract_cost_usd = extract_cost_usd


# Default model: the router's FRONTIER tier (opus-4.8). This is the
# agentic/reasoning shape — reviewers, dream, follow-up "ask & think" —
# so it consolidates on the strong model rather than sonnet (the reasoning
# tier is where the stronger model earns its keep,
# and 4-7/4-8 are the same price). A caller can still pin a model via the
# ``model=`` arg or ``PRECIS_CLAUDE_AGENT_MODEL``. Resolved lazily inside
# :func:`call_claude_agent` because ``router`` imports this module.
def _default_agent_model() -> str:
    from precis.utils.llm.router import Tier, resolve_model

    return resolve_model(Tier.FRONTIER)


# Default budget per call. Agentic passes spend more than one-shot
# JSON judges (the chase worker's claude_p default is $0.10); $2 is
# enough for a ~20-turn sonnet session + tools, narrow enough that
# a runaway loop is bounded.
_DEFAULT_MAX_USD = 2.00

# Default wall-clock timeout. 10 minutes covers a full dream pass
# (~30-60s) plus headroom for sonnet response variance. Reviewers
# with deeper analysis can bump this per call.
_DEFAULT_TIMEOUT_S = 600


class ClaudeAgentError(ClaudeProcessError):
    """Raised when ``claude -p`` fails (exit code, timeout, binary missing).

    Carries the stdout / stderr / returncode (from
    :class:`ClaudeProcessError`) so callers can surface diagnostics in
    their digest memory / log without re-running.
    """


class ContainerRequiredError(RuntimeError):
    """Raised by :func:`call_claude_agent` when ``require_container=True`` but
    the container path is unavailable — disabled, probe-failed, or an infra
    failure that would otherwise fall back in-process.

    A fail-closed chokepoint for a caller (e.g. fix_gripe's full-privilege,
    verbatim-gripe-text agent) that must never run unsandboxed without an
    operator ack: refuse rather than silently falling back, at any point the
    container turns out unavailable (pre-flight or mid-run), not just a
    pre-flight check a caller might race past.
    """


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Outcome of one :func:`call_claude_agent` invocation.

    ``final_text`` is the raw stdout from claude — useful for audit
    logs even when the "real" output is the side effects produced
    via MCP tools. ``cost_usd`` is best-effort (parsed from stderr).
    ``duration_s`` is the wall-clock duration of the subprocess
    call. ``turns_used`` is best-effort from the tool's stderr
    accounting; ``None`` when it can't be parsed. ``tool_calls`` is the
    count of ``tool_use`` blocks in the stream-json stream — positive
    evidence the pass *did something*; ``None`` on the text/stderr path
    (unknown, never confused with a real zero) so the review seam's
    empty-result assertion can't false-fire on a transport that doesn't
    report tool calls.
    """

    final_text: str
    cost_usd: float | None
    duration_s: float
    turns_used: int | None
    tool_calls: int | None = None
    #: The complete raw stdout stream (every stream-json event: turns + tool
    #: call/result), preserved verbatim for a caller (the planner tick) that
    #: stores a debuggable transcript or parses the terminal reason itself.
    #: ``final_text`` is the *lifted* answer; this is the whole stream. Empty
    #: on the text/stub path where there is no stream to keep.
    raw_stdout: str = ""
    #: How the run terminated *abnormally* (``stream_terminal_reason``):
    #: ``'max_turns'`` / a ``'budget'``-class reason / another ``error_*``
    #: subtype — ``None`` on a clean run. Lets a caller (plan_tick) map a
    #: recovered exhaustion onto a resumable outcome without re-parsing the
    #: stream, since the wrapper swallows a recoverable non-zero exit.
    terminal_reason: str | None = None
    #: Token telemetry from the stream-json trailing ``result`` event's
    #: ``usage`` dict. That event's ``usage`` is already a **cumulative
    #: total across every turn** of the run (same "only the final event
    #: carries the totals" shape ``_last_result_event`` documents for
    #: ``total_cost_usd``/``num_turns``) — read from the ONE trailing event,
    #: never summed across every ``assistant`` event (double-counting).
    #: ``None`` together, on the same text/stub path that leaves
    #: ``tool_calls`` ``None`` — never a false zero.
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None


def call_claude_agent(
    prompt: str,
    *,
    model: str | None = None,
    system_prompt: str | Path | None = None,
    mcp_config: str | Path | None = None,
    max_turns: int = 20,
    timeout_s: float | None = None,
    max_usd: float | None = None,
    permission_mode: str = "bypassPermissions",
    output_format: str = "text",
    bare: bool = False,
    disallowed_tools: tuple[str, ...] = (),
    envelope: Any | None = None,
    extra_args: tuple[str, ...] = (),
    log_event: tuple[Store, int, str] | None = None,
    env_overlay: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    env_base: Mapping[str, str] | None = None,
    require_container: bool = False,
    mounts: tuple[Mount, ...] = (),
    workdir: str | None = None,
) -> AgentResult:
    """Run an agentic ``claude -p`` session and return the audit result.

    Args:
        prompt: Directive prompt. Unlike :func:`claude_p.call_claude_p`, no
            JSON-shape hint needed — output is captured raw and the actual
            "result" is whatever tool calls it made.
        model: Override the default model (env ``PRECIS_CLAUDE_AGENT_MODEL``
            or the router's FRONTIER tier).
        system_prompt: Inject via ``--append-system-prompt``: a literal
            string or a :class:`Path` (read at call time).
        mcp_config: Path to an MCP config JSON enabling tools. ``None``
            skips MCP entirely.
        max_turns: Hard cap on agent turns.
        timeout_s: Wall-clock timeout (env ``PRECIS_CLAUDE_AGENT_TIMEOUT_S``
            or 600).
        max_usd: Per-call cost cap (env ``PRECIS_CLAUDE_AGENT_MAX_USD`` or
            2.00).
        permission_mode: ``--permission-mode`` flag. Default
            ``bypassPermissions`` — worker passes have no TTY.
        output_format: ``--output-format``. Default ``text``; ``json`` for
            JSON-shaped audit output.
        bare: Add ``--bare`` to force API-key auth (no OAuth/keychain/
            CLAUDE.md auto-discovery) — used where OAuth isn't reachable.
        disallowed_tools: Tuple passed to ``--disallowed-tools``.
        envelope: Optional per-todo permission box
            (:class:`precis.workers.envelope.Envelope`). Its tier-1 deny list
            merges into ``disallowed_tools``; its DB role exports as
            ``PRECIS_MCP_DB_ROLE`` for the spawned MCP server. ``None`` falls
            back to the executor-scoped active envelope
            (:func:`~precis.workers.envelope.active_envelope`).
        extra_args: Pass-through for niche flags. Use sparingly.
        log_event: Optional ``(store, ref_id, source)`` triple. On success,
            writes a ``ref_events`` row on ``ref_id`` (event ``agent:done``,
            payload carrying cost/model/duration_s).
        env_overlay: Extra env vars overlaid onto the subprocess env, after
            the OAuth bootstrap. The spawned MCP server inherits them — used
            to thread runtime context a subprocess can't receive as an
            in-process ContextVar (``PRECIS_CURRENT_TODO``/``_MODEL``/
            ``PRECIS_WORKSPACE``/the agentlog id/``PRECIS_KINDS_DISABLED``).
            Applies to the in-process subprocess only; the container path
            doesn't forward it.
        cwd: Working directory for the subprocess (threaded to
            :func:`run_claude`) — a CLAUDE.md-free neutral cwd avoids
            discovering an ambient project persona. ``None`` inherits the
            caller's cwd.
        env_base: When given, :func:`_prepare_agent_env` builds the
            subprocess env from a COPY of ``env_base`` instead of
            ``os.environ`` — for a DB-isolated/restricted-env caller
            supplying its own stripped env. The OAuth bootstrap is skipped
            in this mode (the caller supplies its own auth, typically
            ``bare=True`` + a baked ``ANTHROPIC_API_KEY``); ``env_overlay``
            still applies on top. ``None`` builds from ``os.environ``.
        require_container: When ``True``, the container path is REQUIRED —
            if unavailable (disabled, capability probe fails, or a
            containerized run dies at the infra level) raises
            :class:`ContainerRequiredError` instead of silently falling
            back in-process. Default ``False`` (container-preferred,
            in-proc fallback always allowed).
        mounts: Bind mounts
            (:class:`~precis.workers.executors.agent_container.Mount`)
            threaded to the container path only; ignored in-process.
        workdir: Container ``-w`` working directory, threaded the same way
            as ``mounts``; ignored in-process (``cwd`` covers that path).
            ``None`` leaves the image's default workdir.

    Returns:
        :class:`AgentResult` with the raw stdout + telemetry.

    Raises:
        ClaudeAgentError: subprocess exited non-zero, timed out, or the
            binary was missing.
        ContainerRequiredError: ``require_container=True`` and the container
            path is unavailable.
    """
    binary, args, model, timeout_s, max_usd, active_env = _resolve_agent_args(
        prompt,
        model=model,
        system_prompt=system_prompt,
        mcp_config=mcp_config,
        max_turns=max_turns,
        timeout_s=timeout_s,
        max_usd=max_usd,
        permission_mode=permission_mode,
        output_format=output_format,
        bare=bare,
        disallowed_tools=disallowed_tools,
        envelope=envelope,
        extra_args=extra_args,
    )
    proc_env = _prepare_agent_env(
        active_env=active_env, bare=bare, env_overlay=env_overlay, env_base=env_base
    )
    cwd_str = str(cwd) if cwd is not None else None

    started = time.monotonic()
    # ``stdin_devnull``: Claude Code reads stdin in non-interactive ``-p``
    # mode and waits up to 3s for data before proceeding; a CLI-spawned
    # worker's inherited stdin pipe can cause claude to read garbage/hang.
    # Direct ``-p`` callers want no stdin.
    #
    # Container executor: run the SAME claude -p in a throwaway container
    # instead of in-process, isolated by the envelope's DB role + network
    # tier. A foreground run whose stdout is captured exactly as the
    # in-proc subprocess's, so the parsing below is unchanged. Off ⇒
    # byte-identical. plan_tick (via router.dispatch → ClaudeAgentProvider)
    # and fix_gripe (via env_base/mounts/require_container) both route
    # through this one chokepoint.
    run_argv = args
    run_binary = binary
    from precis.workers.executors import agent_container as _container

    # Opt-in (``PRECIS_AGENT_CONTAINER``) is necessary but NOT sufficient:
    # gate on the verified-capability probe (runtime live ∧ image resident ∧
    # auth token resolvable ∧ not health-latched). An opted-in host that
    # can't actually containerize runs in-process — byte-identical —
    # instead of failing every agentic pass on a box it can't launch.
    # ``require_container`` flips that fallback into a hard refusal for a
    # caller that can't tolerate running unsandboxed.
    containerized = False
    container_enabled = _container.container_agent_enabled()
    container_ok = container_enabled and _container.container_capability_ok()
    if container_ok:
        import uuid as _uuid

        from precis import secrets as _secrets

        # ``adopt_process_store`` scrubs ``PRECIS_DATABASE_URL`` from
        # ``os.environ`` at worker boot so host ``claude -p`` spawns don't
        # inherit the DSN — ``proc_env`` no longer carries it. The container
        # is an isolation boundary that *does* need DB access: re-inject the
        # captured DSN so ``--env PRECIS_DATABASE_URL`` carries it in
        # (mirrors the OAuth re-injection above); without this the
        # container's entrypoint aborts "PRECIS_DATABASE_URL not set".
        #
        # When the caller supplied ``env_base`` (a DB-isolated env) there is,
        # by design, no DSN in ``proc_env`` to re-inject FROM — the
        # adopted-DSN fallback must not paper over that isolation, so it's
        # only consulted when the caller did NOT ask for env isolation.
        _dsn = proc_env.get("PRECIS_DATABASE_URL")
        if _dsn is None and env_base is None:
            _dsn = _secrets.get_adopted_dsn()
        if _dsn:
            # The worker DSN is password-free by design (libpq fills it from
            # the host's PGPASSFILE), but no pgpass exists inside the
            # container: complete the password on the host before the DSN
            # crosses the boundary, or an in-container call dies fe_sendauth.
            _dsn = _secrets.complete_dsn_password(_dsn)
            proc_env["PRECIS_DATABASE_URL"] = _dsn
        from precis.workers import envelope as _envelope

        # ``bare`` (API-key auth, no OAuth/keychain) implies the container's
        # secret-by-key channel must request ANTHROPIC_API_KEY, not
        # CLAUDE_CODE_OAUTH_TOKEN — else the container asks for a secret
        # ``env_base``/``proc_env`` never carries and auth fails inside it.
        container_mode = "api" if bare else _container.agent_run_mode()
        run_argv = _container.containerize_claude_argv(
            args,
            active_env if active_env is not None else _envelope.Envelope(),
            name=f"precis-agent-{_uuid.uuid4().hex[:12]}",
            model=model or "",
            dsn=_dsn,
            mode=container_mode,
            mounts=mounts,
            workdir=workdir,
        )
        run_binary = run_argv[0]
        containerized = True
    else:
        if container_enabled:
            # Opted in but this host can't be *verified* to run the container
            # (no runtime / image / token, or the health latch is tripped
            # after a recent infra failure).
            _warn_container_incapable_once()
        if require_container:
            raise ContainerRequiredError(
                "call_claude_agent: require_container=True but the container "
                "path is unavailable on this host "
                f"(container_agent_enabled={container_enabled}, "
                "capability probe failed or health-latched) — refusing the "
                "in-process fallback rather than running unsandboxed."
            )
        # Run in-process rather than failing every pass.

    # ``bootstrap_oauth=(env_base is None)``: an isolated-env caller built
    # its own auth already — re-injecting the worker's real OAuth token
    # into that isolated env would defeat the isolation
    # ``_prepare_agent_env`` deliberately skipped.
    res: Any
    try:
        res = run_claude(
            run_argv,
            binary=run_binary,
            label="claude -p (agent)",
            timeout_s=timeout_s,
            error_cls=ClaudeAgentError,
            env=proc_env,
            stdin_devnull=True,
            cwd=cwd_str,
            bootstrap_oauth=env_base is None,
        )
    except ClaudeAgentError as exc:
        if containerized and _container_infra_failure(exc):
            # The *container* failed to run (image missing, daemon unreachable,
            # socket perm, OOM 137) — NOT a claude/model error inside it. Latch
            # the host unhealthy so subsequent passes skip the container for
            # ~10 min. (A container OOM exits 137 ≥128, which the router would
            # otherwise mis-read as a signal 'interrupt' and *skip* — catching
            # it here routes it to the fallback/refusal below, not the skip.)
            _container.trip_container_unhealthy()
            if require_container:
                # This caller can't tolerate running unsandboxed — refuse
                # rather than silently degrade to in-proc.
                log.warning(
                    "claude_agent: containerized run failed at the "
                    "container-infra level (rc=%s) and require_container=True "
                    "— refusing the in-process fallback",
                    getattr(exc, "returncode", None),
                )
                raise ContainerRequiredError(
                    "call_claude_agent: containerized run failed at the "
                    f"container-infra level (rc={getattr(exc, 'returncode', None)}) "
                    "and require_container=True — refusing the in-process "
                    "fallback rather than running unsandboxed."
                ) from exc
            # Retry the SAME call in-process once: one bad box degrades to
            # in-proc instead of dropping the pass.
            log.warning(
                "claude_agent: containerized run failed at the container-infra "
                "level (rc=%s); latching host unhealthy and retrying in-process",
                getattr(exc, "returncode", None),
            )
            res = _run_inproc_fallback(
                binary,
                args,
                timeout_s,
                proc_env,
                cwd_str,
                bootstrap_oauth=env_base is None,
            )
        else:
            # A non-container failure (or a claude/model error inside the
            # container): recover a resumable exhaustion or re-raise enriched.
            res = _recover_exhaustion_or_raise(exc)
    duration_s = time.monotonic() - started

    result = _build_agent_result(res, duration_s=duration_s)

    if log_event is not None:
        _write_log_event(
            log_event,
            model=model,
            cost_usd=result.cost_usd,
            duration_s=duration_s,
            turns_used=result.turns_used,
        )

    return result


async def call_claude_agent_async(
    prompt: str,
    *,
    model: str | None = None,
    system_prompt: str | Path | None = None,
    mcp_config: str | Path | None = None,
    max_turns: int = 20,
    timeout_s: float | None = None,
    max_usd: float | None = None,
    permission_mode: str = "bypassPermissions",
    output_format: str = "text",
    bare: bool = False,
    disallowed_tools: tuple[str, ...] = (),
    envelope: Any | None = None,
    extra_args: tuple[str, ...] = (),
    log_event: tuple[Store, int, str] | None = None,
    env_overlay: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> AgentResult:
    """Streaming async twin of :func:`call_claude_agent`.

    Same keyword surface (see that function's docstring for every arg
    except ``on_event``) — used by a real-time caller (asa_bot's Discord
    bridge) instead of the blocking sync one. Argv building and env/OAuth/
    envelope setup are shared with the sync path via
    :func:`_resolve_agent_args`/:func:`_prepare_agent_env`; only the
    subprocess execution differs
    (:func:`~precis.utils._claude_subprocess.run_claude_async`).

    ``on_event``, when given, is awaited once per parsed ``stream-json``
    event, in arrival order, as the subprocess runs (e.g. live "calling
    tool X" progress). Omitting it still returns a complete, equivalent
    :class:`AgentResult` via the same :func:`_build_agent_result`
    post-processing the sync path uses.

    Container-executor support is deliberately OUT OF SCOPE here — this
    always runs the in-process subprocess path, unlike the sync function's
    container-or-in-proc branch.

    Raises:
        ClaudeAgentError: subprocess exited non-zero, timed out, or the
            binary was missing — same shape as :func:`call_claude_agent`.
    """
    binary, args, model, timeout_s, max_usd, active_env = _resolve_agent_args(
        prompt,
        model=model,
        system_prompt=system_prompt,
        mcp_config=mcp_config,
        max_turns=max_turns,
        timeout_s=timeout_s,
        max_usd=max_usd,
        permission_mode=permission_mode,
        output_format=output_format,
        bare=bare,
        disallowed_tools=disallowed_tools,
        envelope=envelope,
        extra_args=extra_args,
    )
    proc_env = _prepare_agent_env(
        active_env=active_env, bare=bare, env_overlay=env_overlay
    )
    cwd_str = str(cwd) if cwd is not None else None

    started = time.monotonic()
    res: Any
    try:
        res = await run_claude_async(
            args,
            binary=binary,
            label="claude -p (agent · async)",
            timeout_s=timeout_s,
            error_cls=ClaudeAgentError,
            env=proc_env,
            stdin_devnull=True,
            cwd=cwd_str,
            on_event=on_event,
        )
    except ClaudeAgentError as exc:
        # Same resumable-exhaustion recovery the sync path applies — no
        # container branch here, so there's no infra-failure leg to check.
        res = _recover_exhaustion_or_raise(exc)
    duration_s = time.monotonic() - started

    result = _build_agent_result(res, duration_s=duration_s)

    if log_event is not None:
        _write_log_event(
            log_event,
            model=model,
            cost_usd=result.cost_usd,
            duration_s=duration_s,
            turns_used=result.turns_used,
        )

    return result


# ── helpers ────────────────────────────────────────────────────────


def _resolve_agent_args(
    prompt: str,
    *,
    model: str | None,
    system_prompt: str | Path | None,
    mcp_config: str | Path | None,
    max_turns: int,
    timeout_s: float | None,
    max_usd: float | None,
    permission_mode: str,
    output_format: str,
    bare: bool,
    disallowed_tools: tuple[str, ...],
    envelope: Any | None,
    extra_args: tuple[str, ...],
) -> tuple[str, list[str], str, float, float, Any]:
    """Resolve model/timeout/budget and build the full ``claude -p`` argv.

    Shared by :func:`call_claude_agent` and :func:`call_claude_agent_async`
    so the flag-building logic (mcp-config / system-prompt+friction-footer /
    disallowed-tools-via-settings-JSON / extra-args / the prompt itself)
    lives in exactly one place. Returns ``(binary, args, model, timeout_s,
    max_usd, active_env)`` — ``active_env`` (the resolved envelope, if any)
    is threaded to :func:`_prepare_agent_env` so it isn't re-resolved.
    """
    binary = resolve_binary()
    model = (
        model or os.environ.get("PRECIS_CLAUDE_AGENT_MODEL") or _default_agent_model()
    )
    if timeout_s is None:
        env_timeout = os.environ.get("PRECIS_CLAUDE_AGENT_TIMEOUT_S")
        timeout_s = float(env_timeout) if env_timeout else _DEFAULT_TIMEOUT_S
    if max_usd is None:
        env_usd = os.environ.get("PRECIS_CLAUDE_AGENT_MAX_USD")
        max_usd = float(env_usd) if env_usd else _DEFAULT_MAX_USD

    # ``system_prompt`` accepts a literal or a path; resolve to a
    # string here so the subprocess sees only one shape.
    if isinstance(system_prompt, Path):
        system_prompt_text: str | None = system_prompt.read_text(encoding="utf-8")
    else:
        system_prompt_text = system_prompt

    # End-of-run tool-friction reflection (default-OFF, PRECIS_FRICTION_REFLECT).
    # Rides ``--append-system-prompt`` on eligible runs — MCP present (so the
    # agent can ``put`` a gripe) with turn headroom. One-shot JSON judges use
    # claude_p, not this path, so they are structurally excluded.
    system_prompt_text = append_friction_footer(
        system_prompt_text,
        has_mcp=mcp_config is not None,
        max_turns=max_turns,
    )

    args: list[str] = [
        binary,
        "-p",
        "--model",
        model,
        "--max-budget-usd",
        str(max_usd),
        "--max-turns",
        str(max_turns),
        "--permission-mode",
        permission_mode,
        "--output-format",
        output_format,
    ]
    if output_format == "stream-json" and "--verbose" not in extra_args:
        # ``claude -p`` (``--print``) rejects ``--output-format stream-json``
        # unless ``--verbose`` is also passed. Guarded so a caller that
        # already passes ``--verbose`` explicitly doesn't get a duplicate.
        args.append("--verbose")
    if bare:
        # ``--bare`` strips OAuth / keychain / CLAUDE.md auto-discovery
        # and forces ANTHROPIC_API_KEY. Used in containers where
        # OAuth isn't reachable. Same flag as workers/job_types/fix_gripe.
        args.append("--bare")
    if mcp_config is not None:
        args.extend(["--mcp-config", str(mcp_config)])
        # ``--strict-mcp-config`` rejects unknown MCP options at
        # parse time — same pattern as scripts/exercise-mcp/run.sh.
        # Without it a typo in mcp.json fails silently and the
        # agent runs without the tools it was supposed to have.
        args.append("--strict-mcp-config")
    if system_prompt_text:
        args.extend(["--append-system-prompt", system_prompt_text])
    # Per-todo envelope: resolve the explicit arg, else the executor-scoped
    # active envelope. Its tier-1 deny list is merged into
    # ``disallowed_tools`` below; its DB role is exported to the subprocess
    # env in ``_prepare_agent_env``.
    from precis.workers import envelope as _envelope

    active_env = envelope if envelope is not None else _envelope.active_envelope()
    effective_deny = list(disallowed_tools)
    if active_env is not None:
        for tool in _envelope.disallowed_tools(active_env):
            if tool not in effective_deny:
                effective_deny.append(tool)

    if effective_deny:
        # ``claude -p`` declares ``--disallowed-tools <tools...>`` as a
        # Commander.js *variadic* — it greedily consumes every subsequent
        # positional as another tool name, including the prompt itself
        # (the ``=VALUE`` form binds the first value but doesn't stop the
        # variadic eating the rest). Pass the deny list via ``--settings``
        # JSON instead: Claude Code's supported ``permissions.deny`` route,
        # taking a single JSON string with no variadic to fight.
        import json as _json

        settings_payload = {
            "permissions": {"deny": effective_deny},
        }
        args.extend(["--settings", _json.dumps(settings_payload)])
    args.extend(extra_args)
    # ``--`` end-of-options sentinel, then the prompt as the sole trailing
    # positional. The prompt is UNTRUSTED (e.g. raw Discord messages), and
    # claude's Commander.js CLI parses any argv token starting with ``-`` as
    # an option — without ``--``, a prompt beginning with a dash exits the
    # binary 1 with "unknown option". MUST stay after ``extra_args`` and
    # immediately precede the prompt.
    args.append("--")
    args.append(prompt)

    log.debug(
        "claude_agent: invoking model=%s max_turns=%d max_usd=%.4f "
        "timeout=%.0fs mcp=%s",
        model,
        max_turns,
        max_usd,
        timeout_s,
        "yes" if mcp_config else "no",
    )
    return binary, args, model, timeout_s, max_usd, active_env


def _prepare_agent_env(
    *,
    active_env: Any | None,
    bare: bool,
    env_overlay: dict[str, str] | None,
    env_base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the ``claude -p`` subprocess env: OAuth bootstrap, the
    envelope's DB-role export, the OAuth-over-API-key preference, and the
    caller's env overlay.

    Shared by :func:`call_claude_agent` and :func:`call_claude_agent_async`
    (the async twin never passes ``env_base``, so it always takes the
    ``os.environ`` branch below).

    ``env_base``, when given, replaces ``os.environ`` as the starting env —
    an isolated-env caller supplies its own already-stripped env (no
    PG*/PRECIS_* creds) rather than inheriting the worker's ambient one. The
    OAuth bootstrap is skipped in that mode — the caller is expected to have
    its own auth already in ``env_base`` (typically ``bare=True`` +
    ``ANTHROPIC_API_KEY``).
    """
    # CLAUDE_CODE_OAUTH_TOKEN bootstrap. Interactive shells source
    # ~/.zshrc / ~/.bash_profile which loads the long-lived token from
    # ~/.claude_oauth_token into the env; launchd-spawned daemons don't run
    # any such hook, so claude -p falls back to expired keychain
    # credentials and silently exits "Not logged in" (a clean-looking
    # cost=$0 turns=None success). Load the file ourselves when missing.
    proc_env = dict(env_base) if env_base is not None else dict(os.environ)
    # Process-level envelope enforcement: advertise the resolved Postgres
    # role so a per-call `precis serve` the container executor spawns
    # binds `agent_ro` for a read-only box. Harmless while the current MCP
    # config ignores it — ships dark ahead of that consumer.
    if active_env is not None:
        from precis.workers import envelope as _envelope

        proc_env["PRECIS_MCP_DB_ROLE"] = _envelope.db_role(active_env)
    if env_base is None:
        ensure_oauth_token(proc_env)
    if not bare:
        # Prefer the OAuth token (Max subscription) over ANTHROPIC_API_KEY
        # (per-token API billing): scrub the key when a token is present so the
        # CLI can't pick the billed path. ``bare`` deliberately keeps the key
        # (container auth). Warn when we're forced onto the billed fallback.
        if prefer_oauth_over_api_key(proc_env) == "api_key":
            log.warning(
                "claude_agent: no OAuth token (CLAUDE_CODE_OAUTH_TOKEN) — auth "
                "is falling back to ANTHROPIC_API_KEY, billed per token at API "
                "rates. Install ~/.claude_oauth_token to use the subscription."
            )
    if env_overlay:
        # Tick runtime context (parent todo / model / workspace / agentlog id /
        # kind-gate) for the spawned MCP server. Applied last so it wins over the
        # inherited env, but after the OAuth bootstrap so it can't clobber auth.
        # The subprocess can't read the in-process ContextVar the OSS loop uses,
        # so these env back-doors are how the claude tick propagates its context.
        proc_env.update(env_overlay)
    return proc_env


def _build_agent_result(res: Any, *, duration_s: float) -> AgentResult:
    """Post-process a completed run's stdout/stderr into an
    :class:`AgentResult`.

    Shared by :func:`call_claude_agent` and :func:`call_claude_agent_async`
    so the "Not logged in" guard, the stream-json result-event / assistant-
    text lift, and the token-telemetry read live in exactly one place.
    ``res`` is anything with ``.stdout`` / ``.stderr`` attributes — a
    ``subprocess.CompletedProcess``, ``run_claude_async``'s
    ``SimpleNamespace``, or the recovered-exhaustion
    ``SimpleNamespace`` from :func:`_recover_exhaustion_or_raise`.

    Raises:
        ClaudeAgentError: when the stdout carries the "Not logged in"
            silent-failure marker (see the guard below).
    """
    # "Not logged in" guard: claude -p exits 0 with "Not logged in · Please
    # run /login" on stdout when the OAuth state is bad, which otherwise
    # looks like a clean cost=$0 turns=None success. Detect and raise
    # explicitly so the operator sees the failure where it happened.
    stdout_text = (res.stdout or "").strip()
    if "Not logged in" in stdout_text or "Please run /login" in stdout_text:
        raise ClaudeAgentError(
            "claude -p (agent) returned but is not logged in. "
            "CLAUDE_CODE_OAUTH_TOKEN missing or stale — load it from "
            "~/.claude_oauth_token or re-run 'claude /login'.",
            stdout=res.stdout,
            stderr=res.stderr,
            returncode=0,
        )

    # Claude Code 2.1.x emits the final cost + turn count in the
    # trailing ``{"type":"result"}`` JSON event on stdout (stream-json
    # format), not on stderr. Try stdout's last JSON line first; if
    # nothing surfaces there (legacy text-format invocations, stub
    # tests), fall back to the stderr regex extractors.
    last_result = _last_result_event(res.stdout or "")
    cost_usd: float | None
    turns_used: int | None
    tool_calls: int | None
    if last_result is not None:
        # stream-json path: pull cost + turns from the event AND lift
        # the assistant's final text out of the ``result`` field so
        # callers don't have to grovel through the JSON stream.
        raw_cost = last_result.get("total_cost_usd")
        cost_usd = float(raw_cost) if isinstance(raw_cost, (int, float)) else None
        raw_turns = last_result.get("num_turns")
        turns_used = int(raw_turns) if isinstance(raw_turns, (int, float)) else None
        # Positive evidence the pass acted: count tool_use blocks in the
        # stream. The review seam's empty-result assertion (cost==0 ∧
        # turns 0/None ∧ tool_calls==0 ∧ no text) hinges on this being a
        # *definitive* zero — only the stream-json path can supply it.
        tool_calls = _count_tool_use_events(res.stdout or "")
        # Prefer the result event's ``result`` field; but on an exhaustion
        # cutoff (max_turns / budget) it is often null/empty, in which case
        # falling back to ``res.stdout`` would dump the entire JSON stream
        # as the "answer". Walk the stream for the last assistant text
        # instead, only dropping to raw stdout when there is none.
        result_text = last_result.get("result")
        if isinstance(result_text, str) and result_text.strip():
            final_text = result_text
        else:
            final_text = _last_assistant_text(res.stdout) or res.stdout
    else:
        # text-format path or stub-tests: regex over stderr for cost
        # (legacy Claude Code), final_text is the raw stdout.
        cost_usd = _extract_cost_usd(res.stderr or "")
        turns_used = _extract_turns_used(res.stderr or "")
        final_text = res.stdout
        # No stream to count — leave tool_calls unknown (never a false
        # zero) so the empty-result assertion stays inert on this path.
        tool_calls = None

    usage = _stream_usage(res.stdout or "")

    return AgentResult(
        final_text=final_text,
        cost_usd=cost_usd,
        duration_s=duration_s,
        turns_used=turns_used,
        tool_calls=tool_calls,
        raw_stdout=res.stdout or "",
        # How the run ended abnormally, when it did (max_turns / budget / other
        # error_* subtype). ``None`` on a clean run. Both the clean path and the
        # recovered-exhaustion path carry the full stream on ``res.stdout``, so
        # this is a definitive read for a caller that maps it to a resume signal.
        terminal_reason=stream_terminal_reason(res.stdout or ""),
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        cache_read_tokens=usage["cache_read_tokens"],
        cache_creation_tokens=usage["cache_creation_tokens"],
    )


def _write_log_event(
    log_event: tuple[Store, int, str],
    *,
    model: str,
    cost_usd: float | None,
    duration_s: float,
    turns_used: int | None,
) -> None:
    """Best-effort ``ref_events`` audit row on a successful agentic run.

    Shared by :func:`call_claude_agent` and :func:`call_claude_agent_async`.
    A failure here shouldn't lose the agent's work, but we want the
    operator to notice — the store's ``append_event`` itself is robust;
    this try/except catches the case where a stub Store doesn't implement
    it.
    """
    store, ref_id, source = log_event
    try:
        store.append_event(
            ref_id,
            source=source,
            event="agent:done",
            payload={
                "model": model,
                "cost_usd": cost_usd,
                "duration_s": round(duration_s, 2),
                "turns_used": turns_used,
            },
        )
    except Exception:
        log.exception("claude_agent: log_event append failed")


def _recover_exhaustion_or_raise(exc: ClaudeAgentError) -> Any:
    """A genuine ``ClaudeAgentError`` → either a recovered partial (resumable
    ``--max-turns``/``--max-budget-usd`` exhaustion, whose full ``stream-json``
    is on stdout with a partial answer in the trailing ``result`` event) or a
    re-raise enriched with the stream's terminal reason.

    Factored out so the primary (container-or-in-proc) run and the
    post-container-failure in-proc fallback share one recovery path — an
    exhaustion is resumable, not failed, and callers must not lose that work
    behind a bare undiagnosable ``exited 1:``.
    """
    reason = _recoverable_exhaustion(exc.stdout or "")
    if reason is None:
        # Genuine failure. The CLI's bare "exited N: " is undiagnosable when
        # stderr is empty (stream-json errors land on stdout), so enrich the
        # message with the terminal reason when one is present before re-raising.
        term = stream_terminal_reason(exc.stdout or "")
        if term is not None:
            raise ClaudeAgentError(
                f"{exc} (terminal_reason={term})",
                stdout=exc.stdout,
                stderr=exc.stderr,
                returncode=exc.returncode,
            ) from exc
        raise exc
    log.info(
        "claude_agent: exit %s recovered as resumable exhaustion (%s); "
        "returning partial result",
        exc.returncode,
        reason,
    )
    return SimpleNamespace(stdout=exc.stdout or "", stderr=exc.stderr or "")


def _run_inproc_fallback(
    binary: str,
    args: list[str],
    timeout_s: float,
    proc_env: dict[str, str],
    cwd: str | None = None,
    *,
    bootstrap_oauth: bool = True,
) -> Any:
    """Retry the SAME agentic call in-process after a container-infra failure.

    Runs the ORIGINAL host argv (``args``/``binary``, not the ``docker run``
    wrapper), so a host whose container can't launch still completes the
    pass. A ``ClaudeAgentError`` from the fallback is a real in-proc failure
    and goes through the shared exhaustion-recovery/enrich path.

    ``bootstrap_oauth`` threads the same isolation decision the primary run
    made — an env-isolated caller's fallback must not have the worker's
    real OAuth token re-injected here either.
    """
    try:
        return run_claude(
            args,
            binary=binary,
            label="claude -p (agent · in-proc fallback)",
            timeout_s=timeout_s,
            error_cls=ClaudeAgentError,
            env=proc_env,
            stdin_devnull=True,
            cwd=cwd,
            bootstrap_oauth=bootstrap_oauth,
        )
    except ClaudeAgentError as exc:
        return _recover_exhaustion_or_raise(exc)


#: Substrings in a failed ``docker/podman run``'s output that mean the
#: *container* couldn't run (vs. claude failing inside it). Lower-cased
#: substring match over stderr+stdout. OOM is caught by the 137 exit code below,
#: not a marker.
_CONTAINER_INFRA_MARKERS = (
    "cannot connect to the docker daemon",
    "cannot connect to podman",
    "is the docker daemon running",
    "no such image",
    "unable to find image",
    "manifest unknown",
    "image not known",
    "permission denied",
    "dial unix",
    "connection refused",
)


def _container_infra_failure(exc: ClaudeAgentError) -> bool:
    """Whether a containerized run's error is the *container* failing to run
    (image missing, daemon unreachable, socket perm, OOM) rather than a
    claude/model error inside it.

    Exit 137 = OOM/SIGKILLed (``docker run`` forwards the container's exit
    code); other infra failures surface a known runtime message. Deliberately
    narrow — a false positive costs only one in-proc retry, a false negative
    would hide a real failure behind a pointless retry — so this matches
    specific signatures, never any non-zero exit.
    """
    rc = getattr(exc, "returncode", None)
    if rc == 137:  # container OOM / SIGKILL (128 + 9)
        return True
    blob = (
        (getattr(exc, "stderr", "") or "") + "\n" + (getattr(exc, "stdout", "") or "")
    ).lower()
    return any(m in blob for m in _CONTAINER_INFRA_MARKERS)


#: Warn only once per process when opted-in-but-incapable (avoid per-pass spam).
_warned_container_incapable = False


def _warn_container_incapable_once() -> None:
    """Log (once per process) that ``PRECIS_AGENT_CONTAINER`` is set but this
    host can't be verified to run the container, so passes run in-process."""
    global _warned_container_incapable
    if _warned_container_incapable:
        return
    _warned_container_incapable = True
    log.warning(
        "claude_agent: PRECIS_AGENT_CONTAINER is set but this host can't be "
        "verified to run the agent container (runtime/image/token missing, or "
        "the health latch is tripped) — running agentic passes in-process. "
        "Warns once per process; /factory shows the host as degraded."
    )


# claude emits "turns: N" on stderr in some output formats; best-effort.
_TURNS_RE = re.compile(r"\bturns?\s*[:=]\s*([0-9]+)", re.IGNORECASE)


def _extract_turns_used(stderr: str) -> int | None:
    m = _TURNS_RE.search(stderr)
    if m is None:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def stream_final_text(stdout: str) -> str:
    """The assistant's final text from a ``stream-json`` stdout (the
    trailing ``result`` event's ``result`` field).

    Falls back to the **raw stdout** when there's no result event — i.e.
    text-format output or a stub test that prints canned text — so a
    caller that switched its invocation to ``stream-json`` stays correct
    on legacy / stub output (the conclusion parser still sees plain text).
    """
    ev = _last_result_event(stdout or "")
    if ev is not None:
        r = ev.get("result")
        if isinstance(r, str):
            return r
    return stdout or ""


def stream_terminal_reason(stdout: str) -> str | None:
    """How an agent run ended, when it ended *abnormally*.

    Returns ``'max_turns'`` when the agent hit ``--max-turns`` — a
    *resumable* exhaustion, not a real error: the run was cut off mid-flight,
    and a fresh invocation continues with a new turn budget. (The CLI
    surfaces this as a trailing ``result`` event with
    ``subtype='error_max_turns'`` and/or ``terminal_reason='max_turns'``,
    ``is_error=true``, exit 1.) Returns the raw ``error_*`` subtype for other
    abnormal terminations, ``None`` for a clean run, a non-error terminal
    reason, or stdout with no result event.
    """
    ev = _last_result_event(stdout or "")
    if ev is None:
        return None
    subtype = ev.get("subtype")
    reason = ev.get("terminal_reason")
    if subtype == "error_max_turns" or reason == "max_turns":
        return "max_turns"
    if isinstance(subtype, str) and subtype.startswith("error_"):
        return subtype
    if isinstance(reason, str) and reason not in ("", "end_turn", "stop"):
        return reason
    return None


def _recoverable_exhaustion(stdout: str) -> str | None:
    """Terminal reason for a non-zero exit that is a *resumable
    exhaustion* rather than a crash.

    An agent that hits the ``--max-turns`` ceiling or the
    ``--max-budget-usd`` cap exits 1 with a ``stream-json`` result event
    (``subtype='error_max_turns'`` / a budget subtype) — but it ran, did
    work (MCP side effects), and usually produced a partial answer. That
    is recoverable: return the reason string so the caller can surface
    the partial :class:`AgentResult` instead of discarding everything.

    Also recoverable: a non-zero exit whose result event reports the run
    **completed** its turn (``terminal_reason='completed'``) — the model
    finished and produced an answer; the exit code is a process/teardown
    artifact. Treat it like an exhaustion and surface the final text.

    Returns ``None`` for a genuine error (no result event, or an
    ``error_during_execution``-class subtype) so the caller re-raises.
    """
    reason = stream_terminal_reason(stdout)
    if reason is None:
        return None
    if reason == "max_turns" or "budget" in reason or reason == "completed":
        return reason
    return None


def _last_assistant_text(stdout: str) -> str | None:
    """Last assistant message text in a ``stream-json`` stream.

    Walks events from the end for the most recent ``assistant`` message
    and concatenates its text blocks. Used as the final-text fallback
    when the trailing ``result`` event carries no usable ``result``
    string (e.g. a max-turns cutoff), so callers get the model's actual
    last words instead of the raw JSON stream. Returns ``None`` when no
    assistant text is present.
    """
    import json as _json

    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if not isinstance(ev, dict) or ev.get("type") != "assistant":
            continue
        msg = ev.get("message")
        content = msg.get("content") if isinstance(msg, dict) else ev.get("content")
        if isinstance(content, str):
            return content.strip() or None
        if isinstance(content, list):
            joined = "".join(
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
            if joined:
                return joined
    return None


def count_tool_use_events(stdout: str, *, name_prefix: str | None = None) -> int:
    """Count ``tool_use`` blocks across all assistant events in a stream.

    Every MCP/built-in tool call surfaces as a ``tool_use`` content block
    inside an ``assistant`` message event. The total is the review seam's
    positive evidence the pass *did something*: a run reporting zero tool
    calls, no text, and $0 cost did nothing, and the empty-result assertion
    raises on it rather than logging a silent "$0 success". Only meaningful
    on the stream-json path — the caller leaves ``tool_calls`` ``None`` on
    the text/stderr path so a definitive zero is never confused with unknown.

    ``name_prefix`` narrows the count to tools whose name starts with it
    (e.g. ``"mcp__precis__"``). Scoping to genuine ``tool_use`` blocks (not a
    bare substring search) avoids false-matching the *init* event's
    available-tools list or prose naming the tool.
    """
    import json as _json

    count = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if not isinstance(ev, dict) or ev.get("type") != "assistant":
            continue
        msg = ev.get("message")
        content = msg.get("content") if isinstance(msg, dict) else ev.get("content")
        if isinstance(content, list):
            count += sum(
                1
                for b in content
                if isinstance(b, dict)
                and b.get("type") == "tool_use"
                and (
                    name_prefix is None
                    or str(b.get("name") or "").startswith(name_prefix)
                )
            )
    return count


#: Back-compat alias — the name was private before it gained a second caller.
_count_tool_use_events = count_tool_use_events


def count_successful_tool_results(
    stdout: str, *, name_prefix: str | None = None
) -> int:
    """Count tool calls that returned a NON-error result in a stream.

    :func:`count_tool_use_events` counts *invocations*, which reads a run
    whose every call errored as tool use — invocation-counting alone can't
    distinguish a run where every verb died (e.g. a broken DSN) from a real
    success. Success needs the other half of the wire: each ``tool_use``
    block's id must come back in a ``tool_result`` block (inside a ``user``
    event) whose ``is_error`` is not true.

    ``name_prefix`` narrows to tools whose name starts with it, matching the
    invocation counter's contract. Only meaningful on the stream-json path.
    """
    import json as _json

    matching_ids: set[str] = set()
    ok = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        msg = ev.get("message")
        content = msg.get("content") if isinstance(msg, dict) else ev.get("content")
        if not isinstance(content, list):
            continue
        if ev.get("type") == "assistant":
            for b in content:
                if (
                    isinstance(b, dict)
                    and b.get("type") == "tool_use"
                    and (
                        name_prefix is None
                        or str(b.get("name") or "").startswith(name_prefix)
                    )
                    and b.get("id")
                ):
                    matching_ids.add(str(b["id"]))
        elif ev.get("type") == "user":
            for b in content:
                if (
                    isinstance(b, dict)
                    and b.get("type") == "tool_result"
                    and str(b.get("tool_use_id") or "") in matching_ids
                    and b.get("is_error") is not True
                ):
                    ok += 1
    return ok


def _last_result_event(stdout: str) -> dict[str, Any] | None:
    """Find the trailing ``{"type":"result"}`` event in stream-json stdout.

    Each event lives on its own line. We walk from the end backwards to
    find the most recent ``result`` event (claude can emit interim
    ``result`` events; the final one carries the totals). Returns ``None``
    when stdout has no JSON or no result event — falls back to the
    stderr regex extractors.
    """
    import json as _json

    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            ev = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            return ev
    return None


def _stream_usage(stdout: str) -> dict[str, int | None]:
    """Token telemetry from the trailing stream-json ``result`` event.

    Reads ``usage.input_tokens``/``output_tokens``/
    ``cache_read_input_tokens``/``cache_creation_input_tokens`` off the SAME
    trailing event ``_last_result_event`` uses for ``total_cost_usd``/
    ``num_turns`` — that ``usage`` dict is already a cumulative total across
    every turn, not a per-turn delta, so summing any OTHER event's usage on
    top would double-count.

    Returns all four keys ``None`` (never a false ``0``) when there is no
    result event to parse — the text/stub path, same condition that gates
    ``tool_calls`` to ``None``.
    """
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
    )
    empty: dict[str, int | None] = dict.fromkeys(keys)
    ev = _last_result_event(stdout)
    if ev is None:
        return empty
    usage = ev.get("usage")
    if not isinstance(usage, dict):
        return empty

    def _int_or_none(v: Any) -> int | None:
        return int(v) if isinstance(v, (int, float)) else None

    return {
        "input_tokens": _int_or_none(usage.get("input_tokens")),
        "output_tokens": _int_or_none(usage.get("output_tokens")),
        "cache_read_tokens": _int_or_none(usage.get("cache_read_input_tokens")),
        "cache_creation_tokens": _int_or_none(usage.get("cache_creation_input_tokens")),
    }


def _cost_from_stdout_result(stdout: str) -> float | None:
    """Pull ``total_cost_usd`` from the trailing stream-json result event."""
    ev = _last_result_event(stdout)
    if ev is None:
        return None
    val = ev.get("total_cost_usd")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _turns_from_stdout_result(stdout: str) -> int | None:
    """Pull ``num_turns`` from the trailing stream-json result event."""
    ev = _last_result_event(stdout)
    if ev is None:
        return None
    val = ev.get("num_turns")
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


__all__ = [
    "AgentResult",
    "ClaudeAgentError",
    "ContainerRequiredError",
    "call_claude_agent",
    "call_claude_agent_async",
    "stream_final_text",
    "stream_terminal_reason",
]

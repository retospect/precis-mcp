"""Project-wide wrapper around ``claude -p`` for **agentic** worker calls.

Peer to :mod:`precis.utils.claude_p`. Where ``claude_p`` is the
one-shot JSON-out shape used by the chase verifier (no tools, no
system prompt, parse the last JSON block from stdout),
``claude_agent`` is the multi-turn shape used by the dream pass and
the Slice-3 reviewers (MCP tools enabled, optional system prompt,
side effects on the precis DB are the output — the final stdout
text is logged for audit but isn't the "result").

Three currently-deployed call sites that this surface unifies:

* ``cluster/roles/precis_dream/files/dream-pass.sh`` (active) — has
  ``--append-system-prompt $(cat SOUL.md)`` + ``--mcp-config`` +
  ``--max-turns 20`` + ``--permission-mode bypassPermissions``.
  Output disposition: agentic memory writes via the MCP precis
  tools.
* Slice-3 structural reviewer (Slice 3 of the todo-tree plan) — same
  shape, different prompt, opus model, 6h cadence, output is
  ``tier:structural`` memories.
* Slice-3 deep reviewer — same shape, weekly cadence, output is
  ``tier:deep`` memories + archive/prune recommendations.

The wrapper adds: cost cap, wall-clock timeout, optional structured
:func:`log_event` write to ``ref_events`` for per-host attribution,
stub-friendly ``PRECIS_CLAUDE_BIN`` override (so tests don't need a
real claude binary).

Auth: by default inherits the calling user's auth (OAuth via
``~/.claude``, container-baked API key, whatever). Pass
``bare=True`` to add ``--bare`` so claude reads ``ANTHROPIC_API_KEY``
only — used by the ``fix_gripe`` job executor where OAuth state
isn't reachable.

Knobs (all overridable per call, project defaults via env):

* ``PRECIS_CLAUDE_BIN``       — claude binary path (default ``claude``).
* ``PRECIS_CLAUDE_AGENT_MODEL`` — default model (default ``claude-sonnet-4-6``).
* ``PRECIS_CLAUDE_AGENT_MAX_USD`` — per-call cost cap (default ``2.00``).
* ``PRECIS_CLAUDE_AGENT_TIMEOUT_S`` — wall-clock timeout (default ``600``).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


# Default model: Sonnet is the right default for the agentic shape —
# strong enough for tree analysis, fast enough for 6h cadences,
# cheap enough that a single 5-turn pass stays under the default
# cap. Reviewers that need opus pass ``model='claude-opus-4-7'``
# explicitly.
_DEFAULT_MODEL = "claude-sonnet-4-6"

# Default budget per call. Agentic passes spend more than one-shot
# JSON judges (the chase worker's claude_p default is $0.10); $2 is
# enough for a ~20-turn sonnet session + tools, narrow enough that
# a runaway loop is bounded.
_DEFAULT_MAX_USD = 2.00

# Default wall-clock timeout. 10 minutes covers a full dream pass
# (~30-60s) plus headroom for sonnet response variance. Reviewers
# with deeper analysis can bump this per call.
_DEFAULT_TIMEOUT_S = 600

# Claude emits a one-liner on stderr like "Cost: $0.0123" — same as
# claude_p.py. Capture for budget telemetry. Best-effort; returns
# None if claude's format drifts.
_COST_RE = re.compile(r"\bcost\b[^$]*\$\s*([0-9]+\.[0-9]+)", re.IGNORECASE)


class ClaudeAgentError(RuntimeError):
    """Raised when ``claude -p`` fails (exit code, timeout, binary missing).

    Carries the stdout / stderr / returncode so callers can surface
    diagnostics in their digest memory / log without re-running.
    """

    def __init__(
        self,
        message: str,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Outcome of one :func:`call_claude_agent` invocation.

    ``final_text`` is the raw stdout from claude — useful for audit
    logs even when the "real" output is the side effects produced
    via MCP tools. ``cost_usd`` is best-effort (parsed from stderr).
    ``duration_s`` is the wall-clock duration of the subprocess
    call. ``turns_used`` is best-effort from the tool's stderr
    accounting; ``None`` when it can't be parsed.
    """

    final_text: str
    cost_usd: float | None
    duration_s: float
    turns_used: int | None


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
    extra_args: tuple[str, ...] = (),
    log_event: tuple[Store, int, str] | None = None,
) -> AgentResult:
    """Run an agentic ``claude -p`` session and return the audit result.

    Args:
        prompt: Directive prompt. Unlike :func:`claude_p.call_claude_p`,
            agentic prompts don't need a JSON-shape hint — the model's
            output is captured raw and the actual "result" is whatever
            tool calls it made (MCP precis writes, etc.).
        model: Override the default model (env
            ``PRECIS_CLAUDE_AGENT_MODEL`` or sonnet-4-6).
        system_prompt: Inject via ``--append-system-prompt``. Accepts
            a literal string OR a :class:`Path` (read at call time).
            Used by the dream pass to inject asa's SOUL.md.
        mcp_config: Path to an MCP config JSON enabling tools like
            the precis MCP server. ``None`` skips MCP entirely.
        max_turns: Hard cap on agent turns. Default 20 mirrors the
            existing dream-pass.sh.
        timeout_s: Wall-clock timeout (env
            ``PRECIS_CLAUDE_AGENT_TIMEOUT_S`` or 600).
        max_usd: Per-call cost cap (env
            ``PRECIS_CLAUDE_AGENT_MAX_USD`` or 2.00).
        permission_mode: ``--permission-mode`` flag. Defaults to
            ``bypassPermissions`` — worker passes have no TTY.
        output_format: ``--output-format``. Default ``text`` matches
            dream-pass.sh. Passes that want JSON-shaped audit output
            can use ``json``.
        bare: When True, add ``--bare`` to force API-key auth (no
            OAuth / keychain / CLAUDE.md auto-discovery). Used by
            in-container executors where OAuth isn't reachable.
        disallowed_tools: Tuple passed to ``--disallowed-tools``.
            Dream-pass disables ``WebFetch,WebSearch`` so dreams
            don't fan out beyond corpus state.
        extra_args: Pass-through for niche flags. Use sparingly.
        log_event: Optional ``(store, ref_id, source)`` triple. When
            provided and the call succeeds, writes a ``ref_events``
            row on ``ref_id`` with the source, event ``agent:done``,
            and a payload carrying cost / model / duration_s.

    Returns:
        :class:`AgentResult` with the raw stdout + telemetry.

    Raises:
        ClaudeAgentError: subprocess exited non-zero, timed out, or
            the binary was missing.
    """
    binary = os.environ.get("PRECIS_CLAUDE_BIN", "claude")
    model = model or os.environ.get(
        "PRECIS_CLAUDE_AGENT_MODEL", _DEFAULT_MODEL
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
        system_prompt_text: str | None = system_prompt.read_text()
    else:
        system_prompt_text = system_prompt

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
    if disallowed_tools:
        # ``claude -p`` declares ``--disallowed-tools <tools...>`` as
        # a Commander.js *variadic* — it greedily consumes every
        # subsequent positional as another tool name, including the
        # prompt itself. The ``=VALUE`` form binds the first value
        # but doesn't stop the variadic from eating the rest, so
        # ``--disallowed-tools=WebFetch,WebSearch <prompt>`` parsed
        # the prompt's words as additional deny rules and the binary
        # exited 1 with "Permission deny rule 'DREAM' matches no
        # known tool" (2026-06-17 dream incident).
        #
        # Workaround: pass the deny list via ``--settings`` JSON. The
        # ``permissions.deny`` channel is Claude Code's supported
        # per-project / per-call route, and ``--settings`` takes a
        # single JSON string value so there's no variadic to fight.
        import json as _json

        settings_payload = {
            "permissions": {"deny": list(disallowed_tools)},
        }
        args.extend(["--settings", _json.dumps(settings_payload)])
    args.extend(extra_args)
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

    # CLAUDE_CODE_OAUTH_TOKEN bootstrap. Interactive shells source
    # ~/.zshrc / ~/.bash_profile which loads the long-lived token
    # from ``~/.claude_oauth_token`` into the env. launchd-spawned
    # daemons (dream, worker-agent) don't run any such hook, so
    # ``claude -p`` falls back to the (expired) keychain credentials
    # and silently exits "Not logged in" — appearing as a clean
    # ``cost=$0 turns=None`` success in our logs (2026-06-17 dream
    # incident). Load the file ourselves when the var is missing.
    proc_env = dict(os.environ)
    if "CLAUDE_CODE_OAUTH_TOKEN" not in proc_env:
        token_path = Path.home() / ".claude_oauth_token"
        try:
            token = token_path.read_text().strip()
        except OSError:
            token = ""
        if token:
            proc_env["CLAUDE_CODE_OAUTH_TOKEN"] = token

    started = time.monotonic()
    try:
        res = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=proc_env,
            # ``stdin=DEVNULL`` because Claude Code 2.1.x reads stdin
            # in non-interactive ``-p`` mode and waits up to 3s for
            # data before proceeding. When this helper is called from
            # a CLI-spawned worker (precis worker --only dream_agent
            # --once), the parent's stdin pipe behaviour can cause
            # claude to read garbage / hang, ultimately producing the
            # "Not logged in" silent-success or zero-MCP-call pattern
            # observed 2026-06-17. Direct ``-p`` callers want no
            # stdin; force it.
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeAgentError(
            f"claude -p (agent) timed out after {timeout_s}s",
            stdout=_to_str(exc.stdout),
            stderr=_to_str(exc.stderr),
        ) from exc
    except FileNotFoundError as exc:
        raise ClaudeAgentError(
            f"claude binary not found ({binary!r}); "
            f"set PRECIS_CLAUDE_BIN or install Claude Code"
        ) from exc

    duration_s = time.monotonic() - started

    if res.returncode != 0:
        raise ClaudeAgentError(
            f"claude -p (agent) exited {res.returncode}: "
            f"{(res.stderr or '').strip()[:400]}",
            stdout=res.stdout,
            stderr=res.stderr,
            returncode=res.returncode,
        )

    # "Not logged in" guard. ``claude -p`` exits 0 with the message
    # "Not logged in · Please run /login" on stdout when the OAuth
    # state is bad — and ``call_claude_agent`` used to treat that as
    # a clean success, reporting ``cost=$0 turns=None`` with no
    # downstream signal. Detect it explicitly and raise so the
    # operator sees the failure where it actually happened.
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

    cost_usd = _extract_cost_usd(res.stderr or "")
    turns_used = _extract_turns_used(res.stderr or "")

    result = AgentResult(
        final_text=res.stdout,
        cost_usd=cost_usd,
        duration_s=duration_s,
        turns_used=turns_used,
    )

    if log_event is not None:
        store, ref_id, source = log_event
        # Best-effort event log. A failure here shouldn't lose the
        # agent's work, but we want the operator to notice — the
        # store's append_event itself is robust; this try/except
        # catches the case where a stub Store doesn't implement it.
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

    return result


# ── helpers ────────────────────────────────────────────────────────


def _to_str(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode(errors="replace")
    return raw


def _extract_cost_usd(stderr: str) -> float | None:
    m = _COST_RE.search(stderr)
    if m is None:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


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


__all__ = [
    "AgentResult",
    "ClaudeAgentError",
    "call_claude_agent",
]

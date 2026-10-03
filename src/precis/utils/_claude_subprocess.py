"""Shared subprocess plumbing for the two ``claude -p`` wrappers.

:mod:`precis.utils.claude_p` (one-shot JSON judge) and
:mod:`precis.utils.claude_agent` (multi-turn agentic) are deliberately
distinct *output* contracts, but they share the same *process* harness:
resolve the binary via ``PRECIS_CLAUDE_BIN``, run the subprocess with a
wall-clock timeout, map timeout / missing-binary / non-zero-exit into a
typed error carrying stdout/stderr/returncode, and best-effort-parse the
``Cost: $…`` line claude emits. That harness lives here so a change to
the invocation (new flag, auth tweak) lands in one place.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from precis.utils.claude_oauth import ensure_oauth_token

log = logging.getLogger(__name__)

# Claude emits a one-liner like "Cost: $0.0123" on stderr; capture it
# for budgeting telemetry. Best-effort — if claude's accounting format
# changes, this just returns None.
_COST_RE = re.compile(r"\bcost\b[^$]*\$\s*([0-9]+\.[0-9]+)", re.IGNORECASE)


class ClaudeProcessError(RuntimeError):
    """Base for ``claude -p`` failures (exit code, timeout, binary missing).

    Carries the stdout / stderr / returncode so callers can surface
    diagnostics without re-running. The two wrappers subclass this so
    callers can catch the wrapper-specific type while sharing one shape.
    """

    def __init__(
        self,
        message: str,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int | None = None,
        timed_out: bool = False,
        binary_missing: bool = False,
        mcp_not_ready: bool = False,
        mcp_status: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        #: True when this failure is a wall-clock timeout (vs a non-zero exit /
        #: missing binary) — a transient *unavailability* the router classifies
        #: as ``paused`` (retry), not a semantic error.
        self.timed_out = timed_out
        #: True when ``subprocess``/``asyncio`` couldn't find the binary at
        #: all (``FileNotFoundError`` — see :func:`run_claude`/
        #: :func:`run_claude_async`) — a HOST-CONFIGURATION defect (no
        #: ``claude`` on PATH, or a bad ``PRECIS_CLAUDE_BIN``), not a
        #: transient/semantic one: retrying the identical call on the SAME
        #: host can never self-heal. Threaded structurally through
        #: :class:`~precis.utils.llm.router.LlmResult` (``cli_unavailable``)
        #: to :class:`~precis.quest.tick.QuestTickOutcome` (``failure_kind``)
        #: so a caller can fail fast instead of grinding through a retry
        #: budget sized for transient faults (gr335087).
        self.binary_missing = binary_missing
        #: True when :func:`run_claude_gated` refused to start the pass because
        #: a required MCP server never reported ``connected`` (failed,
        #: needs-auth, still pending at the gate deadline, or the CLI never
        #: answered the status probe). No model turn ran, so the call cost $0
        #: and the failure is the host's MCP wiring, not the model (gr463517).
        #: Threaded to :class:`~precis.utils.llm.router.LlmResult`
        #: (``mcp_not_ready``) so a caller can raise a visible alert instead of
        #: retrying the identical call.
        self.mcp_not_ready = mcp_not_ready
        #: The last ``{server: status}`` map the gate saw for the servers it
        #: required (``None`` when the CLI never reported any).
        self.mcp_status = mcp_status


def to_str(raw: bytes | str | None) -> str:
    """Coerce subprocess stdout/stderr (bytes | str | None) to ``str``."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode(errors="replace")
    return raw


def extract_cost_usd(stderr: str) -> float | None:
    """Best-effort ``Cost: $N.NN`` extraction from claude's stderr."""
    m = _COST_RE.search(stderr)
    if m is None:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def resolve_binary() -> str:
    """The claude binary path — ``PRECIS_CLAUDE_BIN`` or ``claude``."""
    return os.environ.get("PRECIS_CLAUDE_BIN", "claude")


def exit_detail(stdout: str, stderr: str, *, limit: int = 400) -> str:
    """The cause blurb for a non-zero ``claude -p`` exit.

    ``claude -p`` prints *its own* errors — invalid API key, not logged
    in, unknown flag — to **stdout**, leaving stderr empty, so a
    stderr-only message renders as a bare ``exited 1:`` with no cause.
    That is what a dead ``ANTHROPIC_API_KEY`` looked like from the
    diagnose lane for an entire debugging session (gr211457). Prefer
    stderr when it carries anything; otherwise fall back to stdout's
    *tail* — under ``--output-format stream-json`` the terminal
    ``{"type":"result"}`` event (which holds the error text) is last.
    """
    err = (stderr or "").strip()
    if err:
        return err[:limit]
    out = (stdout or "").strip()
    if not out:
        return "(no output on stdout or stderr)"
    return out[-limit:] if len(out) > limit else out


#: Marks the subprocess env as a NESTED model call rather than a user session.
#:
#: A ``claude -p`` we spawn inherits the caller's cwd and therefore the
#: caller's project hook config, so when that one-shot session ends it fires
#: the project's **SessionEnd** hooks against the CALLER's tree. In this repo
#: that hook is ``scripts/hooks/session-end-reap.sh``: it asks
#: ``scripts/inflight`` for the worktree's bucket and runs ``git worktree
#: remove`` when the answer is ``safe_remove``. So a single big-tier LLM call
#: made from inside a clean, already-merged worktree DELETES that worktree
#: out from under the live session — 1497 files on 2026-08-25 (gripe 256469).
#: The hook's dirty/unmerged guards hold; its *live* guard does not, because
#: the nested session is not the one that took the SessionStart lock.
#:
#: A nested model call is not a user session and must never run session
#: lifecycle hooks. Set at this chokepoint rather than per call site for the
#: same reason the OAuth bootstrap is — so no caller can forget it.
_NESTED_SESSION_ENV: dict[str, str] = {"PRECIS_NO_AUTOREAP": "1"}


def disarm_session_hooks(env: dict[str, str]) -> None:
    """Mark ``env`` (in place) as a nested ``claude`` call — see
    :data:`_NESTED_SESSION_ENV`. Always applied to a COPY by the runners
    below, never to a caller's dict."""
    env.update(_NESTED_SESSION_ENV)


def run_claude(
    argv: list[str],
    *,
    binary: str,
    label: str,
    timeout_s: float,
    error_cls: type[ClaudeProcessError],
    env: dict[str, str] | None = None,
    stdin_devnull: bool = False,
    cwd: str | None = None,
    bootstrap_oauth: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``claude -p`` and return the completed process on success.

    Raises ``error_cls`` (a :class:`ClaudeProcessError` subclass) on
    timeout, missing binary, or non-zero exit. ``label`` prefixes the
    timeout / exit error messages (e.g. ``"claude -p"`` vs
    ``"claude -p (agent)"``).

    ``cwd`` runs the subprocess from a specific directory — used by the
    planner tick to spawn from a CLAUDE.md-free neutral cwd so ``claude -p``
    discovers no ambient project persona. ``None`` inherits
    the caller's working directory (today's behaviour).

    ``env`` is always COPIED before use, never mutated in place — a caller
    that passes an isolated/restricted dict (fix_gripe's ``env_base``, §H
    cycle a) must not see it silently gain vars this function injects.
    ``bootstrap_oauth`` (default True) controls whether the OAuth token is
    injected into that copy at all; a caller with an intentionally isolated
    env (``env`` given AND that env deliberately excludes the token) passes
    ``False`` so the real worker/daemon OAuth token — read from
    ``~/.claude_oauth_token`` or the vault — never leaks into a sandboxed run
    that's supposed to auth some other way (e.g. ``ANTHROPIC_API_KEY``
    already baked into ``env``). A caller inheriting ``os.environ`` wants the
    bootstrap and leaves this at the default.
    """
    # Bootstrap the long-lived OAuth token from ~/.claude_oauth_token so any
    # ``claude -p`` caller — call_claude_p (figure turn, web follow-up, run as
    # the ``deploy`` precis-web user) as well as call_claude_agent — auths off
    # the token file instead of the daemon user's empty/stale keychain and 401s
    # (2026-07-12 incident). Central chokepoint: every claude -p goes through
    # here. Idempotent + override-safe (an env token already set wins).
    env = dict(env) if env is not None else dict(os.environ)
    disarm_session_hooks(env)
    if bootstrap_oauth:
        ensure_oauth_token(env)
    try:
        res = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL if stdin_devnull else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise error_cls(
            f"{label} timed out after {timeout_s}s",
            stdout=to_str(exc.stdout),
            stderr=to_str(exc.stderr),
            timed_out=True,
        ) from exc
    except FileNotFoundError as exc:
        raise error_cls(
            f"claude binary not found ({binary!r}); "
            f"set PRECIS_CLAUDE_BIN or install Claude Code",
            binary_missing=True,
        ) from exc

    if res.returncode != 0:
        raise error_cls(
            f"{label} exited {res.returncode}: {exit_detail(res.stdout, res.stderr)}",
            stdout=res.stdout,
            stderr=res.stderr,
            returncode=res.returncode,
        )
    return res


#: Statuses ``mcp_status`` reports for a server that will not become
#: ``connected`` on its own — the gate fails at once instead of waiting out
#: the deadline.
_MCP_TERMINAL_BAD = frozenset({"failed", "needs-auth", "disabled"})

#: Seconds between ``mcp_status`` control requests while gating.
MCP_GATE_POLL_S: float = 1.0

#: How long a gate failure waits for the CLI to exit after stdin closes
#: before killing it.
_GATE_EXIT_GRACE_S: float = 5.0


def _mcp_status_request(n: int) -> str:
    return (
        json.dumps(
            {
                "type": "control_request",
                "request_id": f"mcp-status-{n}",
                "request": {"subtype": "mcp_status"},
            }
        )
        + "\n"
    )


def _gate_status_text(required: tuple[str, ...], status: dict[str, str] | None) -> str:
    """``status=failed`` for one required server, ``status=a=failed,b=pending``
    for several, ``status=unknown`` when the CLI never reported any."""
    if status is None:
        return "status=unknown"
    if len(required) == 1:
        return f"status={status.get(required[0], 'absent')}"
    return "status=" + ",".join(f"{k}={v}" for k, v in status.items())


class _GatedStream:
    """Reader threads + shared state for one :func:`run_claude_gated` run.

    ``cond`` guards ``out_lines`` and ``state``; waiters wake on every stdout
    line and on EOF.
    """

    def __init__(self, proc: subprocess.Popen[str], require: tuple[str, ...]) -> None:
        self.proc = proc
        self.require = require
        self.cond = threading.Condition()
        self.out_lines: list[str] = []
        self.err_chunks: list[str] = []
        #: Latest ``{server: status}`` from a successful mcp_status response.
        self.status: dict[str, str] | None = None
        self.responses = 0
        self.last_error: str | None = None
        self.result_seen = False
        self._t_out = threading.Thread(target=self._pump_stdout, daemon=True)
        self._t_err = threading.Thread(target=self._pump_stderr, daemon=True)
        self._t_out.start()
        self._t_err.start()

    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            with self.cond:
                self.out_lines.append(line)
                self._note(line.strip())
                self.cond.notify_all()
        with self.cond:
            self.cond.notify_all()

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        for chunk in self.proc.stderr:
            self.err_chunks.append(chunk)

    def _note(self, stripped: str) -> None:
        # Called with ``cond`` held.
        if not stripped.startswith("{"):
            return
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            return
        if not isinstance(ev, dict):
            return
        etype = ev.get("type")
        if etype == "control_response":
            self.responses += 1
            resp = ev.get("response")
            body = resp.get("response") if isinstance(resp, dict) else None
            servers = body.get("mcpServers") if isinstance(body, dict) else None
            if isinstance(servers, list):
                self.status = {
                    str(s.get("name")): str(s.get("status") or "?")
                    for s in servers
                    if isinstance(s, dict) and s.get("name")
                }
            elif isinstance(resp, dict) and resp.get("subtype") == "error":
                self.last_error = str(resp.get("error"))[:200]
        elif etype == "result":
            self.result_seen = True

    def required_status(self) -> dict[str, str] | None:
        """The last reported status narrowed to the required servers."""
        if self.status is None:
            return None
        return {n: self.status.get(n, "absent") for n in self.require}

    def verdict(self) -> str | None:
        """``'ready'`` / ``'bad'`` / ``None`` (undecided). ``cond`` held."""
        req = self.required_status()
        if req is None:
            return None
        if any(v in _MCP_TERMINAL_BAD for v in req.values()):
            return "bad"
        if all(v == "connected" for v in req.values()):
            return "ready"
        return None

    def collect(self) -> tuple[str, str]:
        self._t_out.join(timeout=5)
        self._t_err.join(timeout=5)
        with self.cond:
            return "".join(self.out_lines), "".join(self.err_chunks)

    def write(self, text: str) -> bool:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(text)
            self.proc.stdin.flush()
        except (OSError, ValueError):
            return False
        return True

    def close_stdin(self) -> None:
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass


def run_claude_gated(
    argv: list[str],
    *,
    prompt: str,
    require_mcp: tuple[str, ...],
    gate_deadline_s: float,
    binary: str,
    label: str,
    timeout_s: float,
    error_cls: type[ClaudeProcessError],
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    bootstrap_oauth: bool = True,
    poll_s: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``claude -p`` but send ``prompt`` only once every ``require_mcp``
    server reports ``connected`` — otherwise run NO model turn and raise.

    Why (gr463517): the CLI may start the model's first turn before a stdio MCP
    server has finished connecting, and the model then answers tool-less. The
    container image's CLI (2.1.143) caps that first-turn wait at 2 s, and even
    newer CLIs start the pass when a server is ``failed``. A pass that
    "succeeds" tool-less spends money on prose reasoned from the bare prompt,
    so readiness becomes a hard precondition: fail closed, $0, visibly.

    Protocol (``argv`` must carry ``--input-format stream-json`` and NO
    positional prompt): stdin stays open; every ``poll_s`` (default
    :data:`MCP_GATE_POLL_S`) we write a ``control_request`` with subtype
    ``mcp_status`` — the Agent SDK's control request behind
    ``mcpServerStatus()`` — and read the ``control_response``
    (``response.mcpServers: [{name, status}]``) off stdout. When every required
    server is ``connected`` we write the prompt as a stream-json ``user``
    message and let the run proceed; after the terminal ``result`` event we
    close stdin (the CLI waits for more input otherwise). The returned stdout
    is the full stream, ``control_response`` lines included; every stream
    parser in :mod:`precis.utils.claude_agent` filters on ``type`` and ignores
    them.

    Fails closed (``error_cls`` with ``mcp_not_ready=True``) when a required
    server is ``failed`` / ``needs-auth`` / ``disabled``, is not ``connected``
    within ``gate_deadline_s``, or the CLI never answers a control request at
    all — a CLI protocol change must break loudly, never silently skip the
    gate. Other error semantics match :func:`run_claude`: wall-clock
    ``timeout_s`` (covering gate + run) → ``timed_out``, missing binary →
    ``binary_missing``, non-zero exit → ``returncode``. Env handling (copy,
    :func:`disarm_session_hooks`, OAuth bootstrap) is identical.
    """
    env = dict(env) if env is not None else dict(os.environ)
    disarm_session_hooks(env)
    if bootstrap_oauth:
        ensure_oauth_token(env)
    poll = MCP_GATE_POLL_S if poll_s is None else poll_s
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise error_cls(
            f"claude binary not found ({binary!r}); "
            f"set PRECIS_CLAUDE_BIN or install Claude Code",
            binary_missing=True,
        ) from exc

    started = time.monotonic()
    gate_end = started + min(gate_deadline_s, timeout_s)
    run_end = started + timeout_s
    gs = _GatedStream(proc, require_mcp)

    def _gate_failure(detail: str, status: dict[str, str] | None) -> ClaudeProcessError:
        gs.close_stdin()
        try:
            proc.wait(timeout=_GATE_EXIT_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        stdout, stderr = gs.collect()
        waited = round(time.monotonic() - started)
        return error_cls(
            f"{'/'.join(require_mcp)} MCP not connected "
            f"({_gate_status_text(require_mcp, status)}) after {waited}s"
            f"{detail} — pass not started",
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode,
            mcp_not_ready=True,
            mcp_status=status,
        )

    def _timeout_failure() -> ClaudeProcessError:
        gs.close_stdin()
        proc.kill()
        proc.wait()
        stdout, stderr = gs.collect()
        return error_cls(
            f"{label} timed out after {timeout_s}s",
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
        )

    def _exit_failure(what: str) -> ClaudeProcessError:
        stdout, stderr = gs.collect()
        return error_cls(
            f"{label} exited {proc.returncode} {what}: {exit_detail(stdout, stderr)}",
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode,
        )

    try:
        # ── gate: poll mcp_status until every required server is up ──
        n = 0
        while True:
            n += 1
            gs.write(_mcp_status_request(n))
            with gs.cond:
                gs.cond.wait_for(
                    lambda: gs.verdict() is not None or proc.poll() is not None,
                    timeout=min(poll, max(gate_end - time.monotonic(), 0.0)),
                )
                verdict = gs.verdict()
                responses, last_error = gs.responses, gs.last_error
            exited = proc.poll() is not None
            if exited and verdict is None:
                # A final status line may have raced the exit: drain, re-read.
                gs.collect()
                with gs.cond:
                    verdict = gs.verdict()
                    responses, last_error = gs.responses, gs.last_error
            if verdict == "ready":
                # The one positive trace a gated run leaves: a clean pass
                # alone cannot show the gate ran (gr463517).
                log.info(
                    "claude gate: %s connected after %.1fs (%d mcp_status poll(s))",
                    "/".join(require_mcp),
                    time.monotonic() - started,
                    n,
                )
                break
            if verdict == "bad":
                raise _gate_failure("", gs.required_status())
            if exited:
                if proc.returncode != 0:
                    raise _exit_failure("before MCP readiness")
                raise _gate_failure(" (CLI exited)", gs.required_status())
            if time.monotonic() >= gate_end:
                if timeout_s <= gate_deadline_s:
                    raise _timeout_failure()
                if responses == 0:
                    why = " (CLI never answered the mcp_status control request"
                    why += f"; last error: {last_error})" if last_error else ")"
                    raise _gate_failure(why, None)
                raise _gate_failure("", gs.required_status())

        # ── gate passed: send the prompt, run to the result event ──
        user_msg = {"type": "user", "message": {"role": "user", "content": prompt}}
        if not gs.write(json.dumps(user_msg) + "\n"):
            proc.wait()
            raise _exit_failure("while the prompt was being sent")
        with gs.cond:
            finished = gs.cond.wait_for(
                lambda: gs.result_seen or proc.poll() is not None,
                timeout=max(run_end - time.monotonic(), 0.0),
            )
        if not finished:
            raise _timeout_failure()
        # The CLI waits for more input after the result event; let it exit.
        gs.close_stdin()
        try:
            proc.wait(timeout=max(run_end - time.monotonic(), 0.0))
        except subprocess.TimeoutExpired:
            raise _timeout_failure() from None
    except BaseException:
        # Never leak a live CLI on an unexpected error (KeyboardInterrupt, a
        # bug above); the gate's own errors have already reaped it.
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise
    stdout, stderr = gs.collect()
    if proc.returncode != 0:
        raise error_cls(
            f"{label} exited {proc.returncode}: {exit_detail(stdout, stderr)}",
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode,
        )
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


async def run_claude_async(
    argv: list[str],
    *,
    binary: str,
    label: str,
    timeout_s: float,
    error_cls: type[ClaudeProcessError],
    env: dict[str, str] | None = None,
    stdin_devnull: bool = False,
    cwd: str | None = None,
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> SimpleNamespace:
    """Async analog of :func:`run_claude` — spawns ``argv`` via
    ``asyncio.create_subprocess_exec`` instead of blocking
    ``subprocess.run``, reading stdout line-by-line as it arrives and
    forwarding each parsed ``stream-json`` event to ``on_event`` (if given)
    in arrival order. Ported from ``asa_bot.claude_invoke``'s proven
    ``_read_stream_json`` / ``_consume`` shape — the one real caller that
    already needed real-time streaming (Discord progress updates).

    Same success/failure *contract* as :func:`run_claude` so a caller's
    ``except error_cls`` handling (notably
    :func:`~precis.utils.claude_agent._recover_exhaustion_or_raise`'s
    resumable-exhaustion detection) works identically whether the call went
    through the sync or async runner:

    * Missing binary → ``error_cls`` with "claude binary not found".
    * Wall-clock ``timeout_s`` exceeded → the process is killed and
      ``error_cls`` is raised with "timed out after {timeout_s}s", carrying
      whatever stdout/stderr was captured before the kill.
    * Non-zero exit → ``error_cls`` raised with "exited {rc}: {detail}"
      (:func:`exit_detail` — stderr, else stdout's tail), carrying the
      full stdout/stderr + ``returncode``.
    * Clean (exit 0) run → returns an object with ``.stdout`` / ``.stderr``
      (mirrors ``subprocess.CompletedProcess`` closely enough for every
      downstream reader, which only touches those two attributes).
    """
    # COPY before injecting, matching run_claude's documented contract — a
    # caller that passes an isolated/restricted dict must not see it silently
    # gain vars this function sets (this path previously mutated it in place).
    env = dict(env) if env is not None else dict(os.environ)
    disarm_session_hooks(env)
    ensure_oauth_token(env)

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL if stdin_devnull else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
    except FileNotFoundError as exc:
        raise error_cls(
            f"claude binary not found ({binary!r}); "
            f"set PRECIS_CLAUDE_BIN or install Claude Code",
            binary_missing=True,
        ) from exc

    stdout_lines: list[str] = []

    async def _pump_stdout() -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace")
            stdout_lines.append(text)
            if on_event is None:
                continue
            stripped = text.strip()
            if not stripped.startswith("{"):
                continue
            try:
                evt = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            await on_event(evt)

    try:
        await asyncio.wait_for(_pump_stdout(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        stderr_partial = b""
        if proc.stderr is not None:
            try:
                stderr_partial = await asyncio.wait_for(proc.stderr.read(), timeout=1.0)
            except (TimeoutError, OSError):
                stderr_partial = b""
        raise error_cls(
            f"{label} timed out after {timeout_s}s",
            stdout="".join(stdout_lines),
            stderr=to_str(stderr_partial),
            timed_out=True,
        ) from None

    # Read stderr to EOF (closes when the process exits) *before* ``wait()``
    # so a chatty stderr can't deadlock against an unread pipe buffer.
    stderr_bytes = await proc.stderr.read() if proc.stderr is not None else b""
    returncode = await proc.wait()

    stdout = "".join(stdout_lines)
    stderr = to_str(stderr_bytes)
    if returncode != 0:
        raise error_cls(
            f"{label} exited {returncode}: {exit_detail(stdout, stderr)}",
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
        )
    return SimpleNamespace(stdout=stdout, stderr=stderr)


__all__ = [
    "ClaudeProcessError",
    "exit_detail",
    "extract_cost_usd",
    "resolve_binary",
    "run_claude",
    "run_claude_async",
    "run_claude_gated",
    "to_str",
]

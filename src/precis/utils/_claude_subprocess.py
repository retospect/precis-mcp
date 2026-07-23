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
import os
import re
import subprocess
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from precis.utils.claude_oauth import ensure_oauth_token

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
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


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
) -> subprocess.CompletedProcess[str]:
    """Run ``claude -p`` and return the completed process on success.

    Raises ``error_cls`` (a :class:`ClaudeProcessError` subclass) on
    timeout, missing binary, or non-zero exit. ``label`` prefixes the
    timeout / exit error messages (e.g. ``"claude -p"`` vs
    ``"claude -p (agent)"``).

    ``cwd`` runs the subprocess from a specific directory — used by the
    planner tick to spawn from a CLAUDE.md-free neutral cwd so ``claude -p``
    discovers no ambient project persona (ADR 0051 §12). ``None`` inherits
    the caller's working directory (today's behaviour).
    """
    # Bootstrap the long-lived OAuth token from ~/.claude_oauth_token so any
    # ``claude -p`` caller — call_claude_p (figure turn, web follow-up, run as
    # the ``deploy`` precis-web user) as well as call_claude_agent — auths off
    # the token file instead of the daemon user's empty/stale keychain and 401s
    # (2026-07-12 incident). Central chokepoint: every claude -p goes through
    # here. Idempotent + override-safe (an env token already set wins).
    if env is None:
        env = dict(os.environ)
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
        ) from exc
    except FileNotFoundError as exc:
        raise error_cls(
            f"claude binary not found ({binary!r}); "
            f"set PRECIS_CLAUDE_BIN or install Claude Code"
        ) from exc

    if res.returncode != 0:
        raise error_cls(
            f"{label} exited {res.returncode}: {(res.stderr or '').strip()[:400]}",
            stdout=res.stdout,
            stderr=res.stderr,
            returncode=res.returncode,
        )
    return res


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
    * Non-zero exit → ``error_cls`` raised with "exited {rc}: {stderr}",
      carrying the full stdout/stderr + ``returncode``.
    * Clean (exit 0) run → returns an object with ``.stdout`` / ``.stderr``
      (mirrors ``subprocess.CompletedProcess`` closely enough for every
      downstream reader, which only touches those two attributes).
    """
    if env is None:
        env = dict(os.environ)
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
            f"set PRECIS_CLAUDE_BIN or install Claude Code"
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
        ) from None

    # Read stderr to EOF (closes when the process exits) *before* ``wait()``
    # so a chatty stderr can't deadlock against an unread pipe buffer.
    stderr_bytes = await proc.stderr.read() if proc.stderr is not None else b""
    returncode = await proc.wait()

    stdout = "".join(stdout_lines)
    stderr = to_str(stderr_bytes)
    if returncode != 0:
        raise error_cls(
            f"{label} exited {returncode}: {stderr.strip()[:400]}",
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
        )
    return SimpleNamespace(stdout=stdout, stderr=stderr)


__all__ = [
    "ClaudeProcessError",
    "extract_cost_usd",
    "resolve_binary",
    "run_claude",
    "run_claude_async",
    "to_str",
]

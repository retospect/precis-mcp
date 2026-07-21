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

import os
import re
import subprocess

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


__all__ = [
    "ClaudeProcessError",
    "extract_cost_usd",
    "resolve_binary",
    "run_claude",
    "to_str",
]

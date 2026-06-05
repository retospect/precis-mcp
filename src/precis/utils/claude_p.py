"""Project-wide wrapper around ``claude -p`` for worker-side LLM calls.

Used by anything in precis that needs LLM judgment without an
Anthropic SDK dependency. The chase worker is the first consumer
(per ``docs/design/finding-chase.md``); ingest-time consumers
(see §"Discussion" at the bottom of that doc) can adopt the same
helper without code duplication.

Why ``claude -p`` rather than a Python SDK:

* **No new top-level dep.** ``claude`` is on the container PATH
  already (verified by ``scripts/exercise-mcp/run.sh``), and the
  user's auth / billing already flow through it.
* **Subprocess isolation.** A bad LLM call (OOM, timeout, parse
  failure) cannot crash the worker process.
* **Easy to mock for tests.** Set ``PRECIS_CLAUDE_BIN`` to a stub
  script that emits the expected JSON; no real claude required.
* **Bounded cost.** The wrapper enforces ``--max-budget-usd`` per
  call, so a runaway worker doesn't drain the budget.

Output contract: the caller passes a ``json_schema_hint`` block to
embed in the prompt (so the model knows the expected shape) and
this helper parses the *last* ``{ … }`` block in stdout.
Conservative on the parse: if no JSON block is present, raises
:class:`ClaudePError` rather than returning empty dict.

Knobs (all overridable per call, project defaults via env):

* ``PRECIS_CLAUDE_BIN``       — claude binary path (default ``claude``).
* ``PRECIS_CLAUDE_MODEL``     — model id (default ``claude-haiku-4-5``).
* ``PRECIS_CLAUDE_MAX_USD``   — per-call cost cap (default ``0.10``).
* ``PRECIS_CLAUDE_TIMEOUT_S`` — wall-clock timeout (default ``120``).

Concurrency: each call is a separate subprocess; no shared state.
Thread-safe by construction.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


# Default model: Haiku is fast + cheap for the verifier-shaped tasks
# the chase worker uses (one-shot JSON output, ≤ ~4 KB context). Bump
# to Sonnet/Opus per call when more judgment is needed.
_DEFAULT_MODEL = "claude-haiku-4-5"

# Default budget per call — wide enough for a single Haiku turn with
# a few KB of context, narrow enough that a runaway loop is bounded.
_DEFAULT_MAX_USD = 0.10

# Default wall-clock timeout. Haiku turns ≤ 30 s in practice; the
# extra headroom absorbs container-cold-start + retry latency.
_DEFAULT_TIMEOUT_S = 120

# Regex that finds the LAST balanced ``{ … }`` block in stdout — the
# model is instructed to emit JSON, sometimes prefixed by a sentence
# of prose. Grab the rightmost block.
_JSON_BLOCK_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)


class ClaudePError(RuntimeError):
    """Raised when ``claude -p`` fails or its output cannot be parsed.

    Carries the stdout / stderr / returncode so callers can
    surface diagnostics without re-running.
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


@dataclass(frozen=True)
class ClaudePResult:
    """Parsed result of a successful :func:`call_claude_p` invocation.

    ``data`` is the parsed JSON dict from stdout. ``raw_stdout`` is
    retained for audit / debug logging. ``cost_usd`` is best-effort
    (parsed from claude's stderr accounting line when present;
    ``None`` if the line wasn't found).
    """

    data: dict[str, Any]
    raw_stdout: str
    cost_usd: float | None


def call_claude_p(
    prompt: str,
    *,
    model: str | None = None,
    max_usd: float | None = None,
    timeout_s: float | None = None,
    extra_args: tuple[str, ...] = (),
) -> ClaudePResult:
    """Run ``claude -p <prompt>`` and parse the last JSON block from stdout.

    Args:
        prompt: The full prompt text. The caller is responsible for
            including a JSON-shape hint at the end so the model
            emits parseable output.
        model: Override the default model (``PRECIS_CLAUDE_MODEL`` or
            ``claude-haiku-4-5``). Pass a heavier model for harder
            judgment tasks.
        max_usd: Override the per-call cost cap
            (``PRECIS_CLAUDE_MAX_USD`` or ``0.10``).
        timeout_s: Override the wall-clock timeout
            (``PRECIS_CLAUDE_TIMEOUT_S`` or ``120``).
        extra_args: Additional CLI flags to pass through. Use
            sparingly — most callers should rely on the defaults.

    Returns:
        :class:`ClaudePResult` with the parsed dict.

    Raises:
        ClaudePError: when the subprocess exits non-zero, times out,
            or returns no parseable JSON block.
    """
    binary = os.environ.get("PRECIS_CLAUDE_BIN", "claude")
    model = model or os.environ.get("PRECIS_CLAUDE_MODEL", _DEFAULT_MODEL)
    if max_usd is None:
        max_usd_env = os.environ.get("PRECIS_CLAUDE_MAX_USD")
        max_usd = float(max_usd_env) if max_usd_env else _DEFAULT_MAX_USD
    if timeout_s is None:
        timeout_env = os.environ.get("PRECIS_CLAUDE_TIMEOUT_S")
        timeout_s = float(timeout_env) if timeout_env else _DEFAULT_TIMEOUT_S

    args = [
        binary,
        "-p",
        prompt,
        "--model",
        model,
        "--max-budget-usd",
        str(max_usd),
        # No persistent session per call — worker passes are stateless.
        "--no-session-persistence",
        # Bypass interactive permission prompts; the worker has no TTY.
        "--permission-mode",
        "bypassPermissions",
        *extra_args,
    ]

    log.debug("claude_p: invoking model=%s max_usd=%.4f", model, max_usd)
    try:
        res = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudePError(
            f"claude -p timed out after {timeout_s}s",
            stdout=exc.stdout.decode()
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or ""),
            stderr=exc.stderr.decode()
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or ""),
        ) from exc
    except FileNotFoundError as exc:
        raise ClaudePError(
            f"claude binary not found ({binary!r}); "
            f"set PRECIS_CLAUDE_BIN or install Claude Code"
        ) from exc

    if res.returncode != 0:
        raise ClaudePError(
            f"claude -p exited {res.returncode}: {(res.stderr or '').strip()[:400]}",
            stdout=res.stdout,
            stderr=res.stderr,
            returncode=res.returncode,
        )

    data = _parse_last_json_block(res.stdout)
    if data is None:
        raise ClaudePError(
            "claude -p returned no parseable JSON block",
            stdout=res.stdout,
            stderr=res.stderr,
        )

    cost = _extract_cost_usd(res.stderr or "")
    return ClaudePResult(data=data, raw_stdout=res.stdout, cost_usd=cost)


def _parse_last_json_block(text: str) -> dict[str, Any] | None:
    """Extract and parse the LAST ``{ … }`` block in ``text``.

    The model is told to emit JSON, but it sometimes prefixes the
    output with a sentence of explanation. We grab the rightmost
    balanced block to tolerate that. Returns ``None`` when no
    parseable block exists.
    """
    if not text:
        return None
    matches = _JSON_BLOCK_RE.findall(text)
    if not matches:
        return None
    # Try the rightmost block first; if it fails to parse, walk
    # backwards (some outputs nest braces in prose).
    for block in reversed(matches):
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


# Claude emits a one-liner like "Cost: $0.0123" on stderr; capture it
# for budgeting telemetry. Best-effort — if claude's accounting
# format changes, this just returns None.
_COST_RE = re.compile(r"\bcost\b[^$]*\$\s*([0-9]+\.[0-9]+)", re.IGNORECASE)


def _extract_cost_usd(stderr: str) -> float | None:
    m = _COST_RE.search(stderr)
    if m is None:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


__all__ = [
    "ClaudePError",
    "ClaudePResult",
    "call_claude_p",
]

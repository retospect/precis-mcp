"""Project-wide wrapper around ``claude -p`` for worker-side LLM calls.

Used by anything in precis that needs LLM judgment without an
Anthropic SDK dependency. The chase worker is the first consumer
(per ``docs/backlog/finding-chase.md``); ingest-time consumers
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
from dataclasses import dataclass
from typing import Any

from precis.utils._claude_subprocess import (
    ClaudeProcessError,
    extract_cost_usd,
    resolve_binary,
    run_claude,
)
from precis.utils.claude_oauth import ensure_oauth_token, prefer_oauth_over_api_key

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


class ClaudePError(ClaudeProcessError):
    """Raised when ``claude -p`` fails or its output cannot be parsed.

    Carries the stdout / stderr / returncode (from
    :class:`ClaudeProcessError`) so callers can surface diagnostics
    without re-running.
    """


class ClaudePUnparseableError(ClaudePError):
    """The subprocess ran fine but its reply contained no parseable JSON
    block anywhere — the *model* broke the output contract (prose reply,
    empty reply), not the infrastructure. Split out from
    :class:`ClaudePError` so a caller can retry a format flake without
    also retrying timeouts / non-zero exits / a missing binary
    (:func:`precis.taproot.canon.extract_claim_strict_haiku`'s
    format-flake guard is the motivating consumer). Catching plain
    :class:`ClaudePError` keeps working — this is a subclass.
    """


@dataclass(frozen=True)
class ClaudePResult:
    """Parsed result of a successful :func:`call_claude_p` invocation.

    ``data`` is the parsed JSON dict from stdout. ``raw_stdout`` is
    retained for audit / debug logging — the **full envelope**, not the
    unwrapped payload. ``cost_usd`` comes from the ``--output-format
    json`` envelope's ``total_cost_usd``, falling back to the legacy
    stderr accounting line; ``None`` only when neither is present.

    ``text`` is the assistant's own answer with the envelope stripped —
    what a caller means by "the model's reply". Keep these two apart:
    ``raw_stdout`` gained a JSON wrapper when metering was turned on, so
    anything user-facing (or anything re-parsing the reply, e.g.
    ``taproot.canon``'s ``res.data or _parse_json_object(res.text)``
    fallback) must read ``text`` or it will get the metadata envelope
    instead of the answer. Defaults to ``""`` for the legacy 3-field
    construction; readers should fall back to ``raw_stdout``.
    """

    data: dict[str, Any]
    raw_stdout: str
    cost_usd: float | None
    text: str = ""


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
    binary = resolve_binary()
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
        "--model",
        model,
        "--max-budget-usd",
        str(max_usd),
        # Metering, not formatting. Without this, ``claude -p`` prints bare
        # text and the only cost signal is a stderr ``Cost: $N.NN`` line that
        # modern Claude Code no longer emits — so ``cost_usd`` came back
        # ``None`` on *every* call this helper has ever made, and the whole
        # claude_p lane (quest_tick, figure) was invisible to
        # ``PRECIS_DAILY_COST_CEILING``, which sums ``llm_call_log.cost_usd``.
        # The ``json`` envelope carries ``total_cost_usd`` alongside the
        # assistant text. Sibling of ``LlmRequest.output_format`` defaulting to
        # ``stream-json`` on the claude_agent lane — same bug, same fix.
        "--output-format",
        "json",
        # No persistent session per call — worker passes are stateless.
        "--no-session-persistence",
        # Bypass interactive permission prompts; the worker has no TTY.
        "--permission-mode",
        "bypassPermissions",
        *extra_args,
        # ``--`` end-of-options sentinel, prompt last and positional: a prompt
        # that begins with ``-`` (tex/paper text, a template edge) must never
        # be parsed by claude's Commander.js CLI as a flag. Same hardening as
        # claude_agent._resolve_agent_args (the agentic lane). Keep the prompt
        # the final token, right after ``--``.
        "--",
        prompt,
    ]

    # Bootstrap the OAuth token into the subprocess env. call_claude_p is
    # spawned from launchd daemons (the figure web canvas via precis-web,
    # finding_chase on the system worker) that run no shell hook, so
    # ``claude -p`` would otherwise fall back to stale/absent keychain
    # credentials and 401. Mirrors claude_agent / plan_tick / claude_quota
    # (2026-07-12 incident) — see utils/claude_oauth. Override-safe: a token
    # already in the env (plist var / interactive shell / test) wins.
    proc_env = dict(os.environ)
    ensure_oauth_token(proc_env)
    # Prefer OAuth (subscription) over ANTHROPIC_API_KEY (billed per token);
    # call_claude_p has no ``bare`` mode, so it always prefers the token.
    if prefer_oauth_over_api_key(proc_env) == "api_key":
        log.warning(
            "claude_p: no OAuth token — auth is falling back to "
            "ANTHROPIC_API_KEY, billed per token. Install ~/.claude_oauth_token."
        )

    log.debug("claude_p: invoking model=%s max_usd=%.4f", model, max_usd)
    res = run_claude(
        args,
        binary=binary,
        label="claude -p",
        timeout_s=timeout_s,
        error_cls=ClaudePError,
        env=proc_env,
    )

    payload, cost = _unwrap_envelope(res.stdout or "")
    data = _parse_last_json_block(payload)
    if data is None:
        raise ClaudePUnparseableError(
            "claude -p returned no parseable JSON block",
            stdout=res.stdout,
            stderr=res.stderr,
        )

    if cost is None:
        # Legacy path: a stub binary (tests) or an older CLI that ignored
        # ``--output-format json`` and printed bare text. Fall back to the
        # stderr accounting line rather than losing the call entirely.
        cost = extract_cost_usd(res.stderr or "")
    return ClaudePResult(data=data, raw_stdout=res.stdout, cost_usd=cost, text=payload)


def _unwrap_envelope(stdout: str) -> tuple[str, float | None]:
    """Split ``--output-format json`` stdout into (assistant text, cost).

    ``claude -p --output-format json`` wraps the answer in a metadata envelope
    whose ``result`` field holds the assistant text and whose
    ``total_cost_usd`` is the only place the real dollar figure appears.

    Tolerant by design: test stubs (``PRECIS_CLAUDE_BIN``) and any CLI that
    ignores the flag emit the model's JSON directly, so anything that isn't a
    recognizable envelope is handed back unchanged with no cost — the caller
    then falls back to the stderr regex. That keeps the parse contract ("last
    balanced ``{ … }`` block") identical on both shapes.
    """
    text = stdout.strip()
    if not text.startswith("{"):
        return stdout, None
    try:
        env = json.loads(text)
    except json.JSONDecodeError:
        return stdout, None
    # Discriminate on ``type == "result"`` *and* a string ``result`` — the CLI
    # envelope's own self-identification. A string ``result`` alone is too
    # weak: no judge prompt in the tree currently defines a top-level string
    # field named ``result``, but one plausibly could, and the misread would
    # surface as "no parseable JSON block" pointing nowhere near the cause.
    if not isinstance(env, dict):
        return stdout, None
    if env.get("type") != "result" or not isinstance(env.get("result"), str):
        return stdout, None
    raw_cost = env.get("total_cost_usd")
    cost = float(raw_cost) if isinstance(raw_cost, int | float) else None
    return env["result"], cost


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


__all__ = [
    "ClaudePError",
    "ClaudePResult",
    "call_claude_p",
]

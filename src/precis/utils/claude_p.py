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
from dataclasses import dataclass
from typing import Any

from precis.utils._claude_subprocess import (
    ClaudeProcessError,
    extract_cost_usd,
    resolve_binary,
    run_claude,
)
from precis.utils.claude_oauth import (
    API_KEY_VAR,
    ENV_VAR,
    ensure_oauth_token,
    prefer_oauth_over_api_key,
)

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

# (The old regex approach here only matched braces nested ≤2 deep, so a
# payload with a deeper structure — e.g. a quest tick's
# ``proposals[].structure.ops[].site.anchors`` — could never match whole
# and ``data`` came back as the payload's LAST shallow *fragment*, which
# then shadowed every ``res.data or parse(res.text)`` fallback downstream:
# the tick "succeeded" as a silent no-op. Parsing now walks the text with
# ``json.JSONDecoder.raw_decode`` — string-aware, any depth.)


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
    also retrying timeouts / non-zero exits / a missing binary. Catching
    plain :class:`ClaudePError` keeps working — this is a subclass. Routed
    callers (:mod:`precis.utils.llm.router`'s ``ClaudePProvider``) catch
    the parent :class:`ClaudeProcessError` and surface it as
    ``LlmResult.error`` rather than letting it propagate — see
    :func:`precis.taproot.canon.extract_claim_strict_medium` for a
    dispatch-level format-flake guard built on that.
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

    The four token fields mirror the envelope's ``usage`` object — the same
    keys :func:`precis.utils.claude_agent._stream_usage` reads off the
    agentic lane's stream-json ``result`` event. ``None`` (never a false
    ``0``) when the envelope carries no ``usage`` block: a legacy CLI, a
    test stub, or the bare-text fallback path.
    """

    data: dict[str, Any]
    raw_stdout: str
    cost_usd: float | None
    text: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None


def _bare_auth_env(env: dict[str, str]) -> None:
    """Prepare ``env`` for ``--bare`` (API-key) auth. Mutates in place.

    Two moves, both required:

    * **Drop the OAuth token.** ``--bare`` skips keychain reads, so a token
      left in the env is dead weight that only muddies which credential the
      CLI actually resolved. Removing it makes the billed path unambiguous
      in a subprocess dump.
    * **Fill the key from the vault** when the ambient env lacks it. Same
      resolution as ``fix_gripe._restricted_env`` — ``get_secret`` is env →
      ``vault.reveal`` → ``~/.secrets/pw/<NAME>`` — so a launchd daemon that
      carries no ``ANTHROPIC_*`` env still authenticates. Best-effort by
      construction: ``get_secret`` never raises.

    Raises:
        ClaudePError: when no key resolves. Raising beats letting ``claude``
            exit 1 with an auth error the caller would have to
            reverse-engineer, and it keeps *this call* off the subscription
            it was configured to avoid. Note this is call-level, not
            ladder-level: :class:`~precis.utils.llm.router.FailoverProvider`
            treats the raise as a rung failure and moves to the next rung,
            which may well be a non-bare (subscription) one. That failover
            is deliberate — a missing key should degrade the billing path,
            not stop the pass — so read the warning it logs, don't assume a
            bare rung guarantees API-key billing.
    """
    env.pop(ENV_VAR, None)
    if not env.get(API_KEY_VAR):
        try:
            from precis import secrets as _secrets

            key = _secrets.get_secret(API_KEY_VAR) or ""
        except Exception:
            # Defensive only — get_secret swallows its own errors. An
            # import-time failure must not take down the subprocess spawn.
            log.warning("claude_p: vault lookup for %s raised", API_KEY_VAR)
            key = ""
        if key:
            env[API_KEY_VAR] = key
    if not env.get(API_KEY_VAR):
        raise ClaudePError(
            f"claude_p: bare mode needs {API_KEY_VAR} (billed per token) and "
            f"neither the environment nor the vault holds one. Set it with "
            f"`precis secret set {API_KEY_VAR}`, or drop `bare` from the "
            f"chain rung to use the subscription."
        )


def call_claude_p(
    prompt: str,
    *,
    model: str | None = None,
    max_usd: float | None = None,
    timeout_s: float | None = None,
    extra_args: tuple[str, ...] = (),
    bare: bool = False,
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
        bare: Force **API-key** auth (``ANTHROPIC_API_KEY``, billed per
            token) instead of the Max subscription's OAuth token. Adds
            ``--bare``, which skips keychain reads — so the token is
            unreachable inside the child no matter what the env holds —
            and sources the key env → vault, the same resolution
            ``fix_gripe._restricted_env`` uses on the agentic lane.
            Default ``False``: OAuth wins and the key is scrubbed, which
            is the cheap path and must stay the default. Set it per
            **chain rung** (``{"transport": "claude_p", "bare": true}``
            in ``llm.chain.<tier>``), so moving one tier's spend onto the
            API key is an operator config change, not a code change.

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
        # Strips keychain reads (plus hooks/LSP/plugin sync/CLAUDE.md
        # auto-discovery), so auth falls to ANTHROPIC_API_KEY — see the
        # ``bare`` arg and _bare_auth_env below.
        *(("--bare",) if bare else ()),
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
    if bare:
        # Deliberate opt-in to the billed path: do NOT call
        # prefer_oauth_over_api_key (it would scrub the key), and do not
        # bootstrap the token — ``--bare`` makes it unusable anyway.
        _bare_auth_env(proc_env)
    else:
        ensure_oauth_token(proc_env)
        # Prefer OAuth (subscription) over ANTHROPIC_API_KEY (billed per
        # token). Reached only on the non-bare path.
        if prefer_oauth_over_api_key(proc_env) == "api_key":
            log.warning(
                "claude_p: no OAuth token — auth is falling back to "
                "ANTHROPIC_API_KEY, billed per token. Install "
                "~/.claude_oauth_token."
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

    payload, cost, usage = _unwrap_envelope(res.stdout or "")
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
    return ClaudePResult(
        data=data,
        raw_stdout=res.stdout,
        cost_usd=cost,
        text=payload,
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        cache_read_tokens=usage["cache_read_tokens"],
        cache_creation_tokens=usage["cache_creation_tokens"],
    )


#: All-``None`` usage — the "no telemetry available" shape, never a false ``0``.
_EMPTY_USAGE: dict[str, int | None] = {
    "input_tokens": None,
    "output_tokens": None,
    "cache_read_tokens": None,
    "cache_creation_tokens": None,
}


def _unwrap_envelope(
    stdout: str,
) -> tuple[str, float | None, dict[str, int | None]]:
    """Split ``--output-format json`` stdout into (assistant text, cost, usage).

    ``claude -p --output-format json`` wraps the answer in a metadata envelope
    whose ``result`` field holds the assistant text, whose ``total_cost_usd``
    is the only place the real dollar figure appears, and whose ``usage``
    object carries the token telemetry (see :func:`_extract_usage`).

    Tolerant by design: test stubs (``PRECIS_CLAUDE_BIN``) and any CLI that
    ignores the flag emit the model's JSON directly, so anything that isn't a
    recognizable envelope is handed back unchanged with no cost/usage — the
    caller then falls back to the stderr regex for cost. That keeps the parse
    contract ("last balanced ``{ … }`` block") identical on both shapes.
    """
    text = stdout.strip()
    if not text.startswith("{"):
        return stdout, None, dict(_EMPTY_USAGE)
    try:
        env = json.loads(text)
    except json.JSONDecodeError:
        return stdout, None, dict(_EMPTY_USAGE)
    # Discriminate on ``type == "result"`` *and* a string ``result`` — the CLI
    # envelope's own self-identification. A string ``result`` alone is too
    # weak: no judge prompt in the tree currently defines a top-level string
    # field named ``result``, but one plausibly could, and the misread would
    # surface as "no parseable JSON block" pointing nowhere near the cause.
    if not isinstance(env, dict):
        return stdout, None, dict(_EMPTY_USAGE)
    if env.get("type") != "result" or not isinstance(env.get("result"), str):
        return stdout, None, dict(_EMPTY_USAGE)
    raw_cost = env.get("total_cost_usd")
    cost = float(raw_cost) if isinstance(raw_cost, int | float) else None
    return env["result"], cost, _extract_usage(env)


def _extract_usage(env: dict[str, Any]) -> dict[str, int | None]:
    """Token telemetry from the envelope's ``usage`` object.

    Same shape, same keys as the agentic lane's trailing stream-json
    ``result`` event — mirrors
    :func:`precis.utils.claude_agent._stream_usage`'s mapping:
    ``usage.input_tokens`` / ``usage.output_tokens`` /
    ``usage.cache_read_input_tokens`` / ``usage.cache_creation_input_tokens``.
    Tolerant: an absent or malformed ``usage`` block (or a non-numeric field
    within it) returns/leaves ``None`` — never a false ``0`` — matching the
    agent path's "never a false zero" discipline.
    """
    usage = env.get("usage")
    if not isinstance(usage, dict):
        return dict(_EMPTY_USAGE)

    def _int_or_none(v: Any) -> int | None:
        return int(v) if isinstance(v, int | float) else None

    return {
        "input_tokens": _int_or_none(usage.get("input_tokens")),
        "output_tokens": _int_or_none(usage.get("output_tokens")),
        "cache_read_tokens": _int_or_none(usage.get("cache_read_input_tokens")),
        "cache_creation_tokens": _int_or_none(usage.get("cache_creation_input_tokens")),
    }


def _parse_last_json_block(text: str) -> dict[str, Any] | None:
    """Extract and parse the LAST complete JSON object in ``text``.

    The model is told to emit JSON, but it sometimes prefixes the output
    with a sentence of explanation (or fences it). Scanning with
    :meth:`json.JSONDecoder.raw_decode` from each candidate ``{`` is
    string-aware and depth-unlimited, so a nested payload parses whole
    instead of surrendering its last shallow fragment; a successfully
    parsed object is skipped over, so its *nested* objects are never
    re-offered as candidates. Returns ``None`` when no parseable object
    exists.
    """
    if not text:
        return None
    decoder = json.JSONDecoder()
    last: dict[str, Any] | None = None
    i = 0
    while True:
        start = text.find("{", i)
        if start < 0:
            break
        try:
            parsed, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            i = start + 1
            continue
        if isinstance(parsed, dict):
            last = parsed
        i = max(end, start + 1)
    return last


__all__ = [
    "ClaudePError",
    "ClaudePResult",
    "call_claude_p",
]

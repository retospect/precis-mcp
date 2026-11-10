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
:class:`ClaudePError` rather than returning empty dict. Tolerances, each
only after the cheaper ones fail (gr570522): ``strict=False`` strings, a
comma directly before a closer, a whole-reply JSON string decoded once
more — never a truncation salvage (a cut object has no complete block).
One structured-output retry (gr622034): a clean reply that still yields
no block is resampled once with ``--json-schema {"type": "object"}`` —
the CLI validates the answer server-side and lands it on the envelope's
``structured_output`` field, so a format flake converts to a parseable
object instead of a dropped result. The retry is skipped when the caller
already constrained the call itself, and any retry failure surfaces the
original unparseable error — carrying BOTH runs' cost/usage, since a
retry that could not recover the answer still spent real money
(td623635).

Knobs (all overridable per call, project defaults via env):

* ``PRECIS_CLAUDE_BIN``       — claude binary path (default ``claude``).
* ``PRECIS_CLAUDE_MODEL``     — model id (default ``claude-haiku-4-5``).
* ``PRECIS_CLAUDE_MAX_USD``   — per-call cost cap (default ``0.10``).
* ``PRECIS_CLAUDE_TIMEOUT_S`` — wall-clock timeout (default ``120``).

The ``0.10`` default here is Haiku-sized — this module has no notion of
*tier*, so it cannot know a BIG/FRONTIER caller needs a bigger budget. A
router-dispatched call (:class:`precis.utils.llm.router.ClaudePProvider`)
never hits this default in practice: :func:`~precis.utils.llm.router._claude_p_max_usd`
resolves ``LlmRequest.max_usd`` > ``PRECIS_CLAUDE_MAX_USD`` > a
per-tier default (``precis.utils.llm.router._CLAUDE_P_TIER_MAX_USD``) and
always passes a concrete float down, so ``PRECIS_CLAUDE_MAX_USD`` still wins
over the *router's* tier default when set, but this module's own ``0.10``
constant is only reachable from a direct, non-router caller. See
gr255847.

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

#: The schema handed to the one structured-output retry (gr622034): an
#: OPEN object — the prompt's own hint still picks the keys; the
#: constraint only guarantees the resampled reply IS a JSON object, the
#: exact thing the last-block scan demands. ``--json-schema`` validates
#: the answer server-side and lands it on the envelope's
#: ``structured_output`` field (the ``result`` text stays a summary), so
#: a prose flake converts into a parseable object instead of a dropped
#: result.
_OBJECT_SCHEMA_JSON = '{"type": "object"}'


def _invoke_args(
    binary: str,
    model: str,
    max_usd: float,
    *,
    bare: bool,
    extra_args: tuple[str, ...],
    prompt: str,
) -> list[str]:
    """The ``claude -p`` argv — factored so the schema retry rebuilds the
    identical invocation with a modified ``extra_args``."""
    return [
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
        # ``PRECIS_DAILY_COST_CEILING``, which sums ``llm_call_log.billed_usd``
        # (migration 0272; ``cost_usd`` on a billed login feeds it).
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
        # This lane is one-shot text completion — no tool may ever run. The
        # bare "*" deny removes every tool definition from the system prompt
        # (not just execution), so the model cannot even attempt one. Without
        # it, newer CLI/model combos answered by *calling harness tools*
        # (ReportFindings, observed verbatim in salvaged output) instead of
        # emitting the requested text, which surfaced as the 88-100%
        # "unparseable model output" failure rate on every claude_p consumer
        # (quest_tick gr244061; finding_chase's taproot verdicts gr277659).
        "--disallowedTools",
        "*",
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


def _drop_max_turns(extra_args: tuple[str, ...]) -> tuple[str, ...]:
    """``--max-turns <n>``/``--max-turns=<n>`` pairs, removed — for the
    schema retry only.

    ``--json-schema`` extracts the answer through the CLI's own
    structured-output tool call, a turn the caller's one-shot cap does
    not budget (card_describe pins ``--max-turns 1`` — the retry would
    die ``error_max_turns`` before the validated object lands). The cap's
    safety purpose — keeping the rung one-shot — is already guaranteed by
    ``--disallowedTools "*"``, which stays.
    """
    out: list[str] = []
    skip_value = False
    for arg in extra_args:
        if skip_value:
            skip_value = False
            continue
        if arg == "--max-turns":
            skip_value = True
            continue
        if arg.startswith("--max-turns="):
            continue
        out.append(arg)
    return tuple(out)


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

    ``reply`` / ``cost_usd`` / ``usage`` / ``is_error`` / ``subtype`` keep
    the metering envelope's payload and telemetry that were parsed BEFORE
    the JSON-block extraction failed — the spend and the model's actual
    answer must stay visible on the error row even though the call failed
    (gr543828). When raised after a spent ``--json-schema`` retry,
    ``cost_usd``/``usage`` sum BOTH runs — the retry's spend is billed
    too (td623635). ``None``/empty only when raised by a caller that
    never unwrapped an envelope (tests, legacy constructions).
    """

    def __init__(
        self,
        message: str,
        *,
        reply: str | None = None,
        cost_usd: float | None = None,
        usage: dict[str, int | None] | None = None,
        is_error: bool | None = None,
        subtype: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        #: The assistant's answer with the ``--output-format json``
        #: envelope stripped (what ``result`` carried); ``None`` when the
        #: envelope was never parsed.
        self.reply = reply
        self.cost_usd = cost_usd
        #: The envelope ``usage`` mapping (the four token fields),
        #: ``None`` when the envelope carried none.
        self.usage = usage
        #: The envelope's own failure flag — ``True`` when the CLI
        #: returned an API/account-level refusal as a result envelope.
        self.is_error = is_error
        #: The envelope's ``subtype`` (``success``,
        #: ``error_max_budget_usd``, …) when one was stamped.
        self.subtype = subtype


class ClaudePEnvelopeError(ClaudePUnparseableError):
    """The subprocess ran fine but the ``type: "result"`` envelope itself
    declared the turn failed — ``is_error: true`` (an API/account-level
    refusal) or a non-``success`` ``subtype`` (``error_max_budget_usd``,
    ``error_during_execution``, ``error_max_turns``).

    Distinct from a plain format flake: the CLI says no completed answer
    exists even when its ``result`` happens to carry parseable JSON — a
    structured-output echo or a partial answer cut by the spend cap. Raised
    instead of returning a :class:`ClaudePResult` so the router's
    ``_error_result`` keeps its ``transport`` / ``budget`` / ``quota``
    classification (a dict ``result`` on an error envelope would otherwise
    land as a clean parse, gr570522). A subclass of
    :class:`ClaudePUnparseableError`, so ``except`` clauses and the
    router's envelope-telemetry branch need no new case.
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
    images: tuple[Any, ...] = (),
    thinking: bool | None = None,
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
        images: Raster images (``LlmImage``-shaped: ``.data`` bytes +
            ``.media_type``) to ground the call on — the FIRST image's
            bytes pipe on stdin (``cat img.png | claude -p "…"`` attaches
            piped image data as an image block); extras warn + drop.
            Vision support for card_describe's figure slice (gr497688 S3).
        thinking: ``False`` disables extended thinking in the child via
            ``MAX_THINKING_TOKENS=0`` — without it ``claude -p`` spends
            hidden thinking tokens anyway: gr506762's table_recipe calls
            billed 600-13,900 output tokens for sub-500-token replies
            (~$0.030/call where thinking-off is ~$0.008). ``True``/``None``
            leave the ambient env alone — an operator-set
            ``MAX_THINKING_TOKENS`` budget still applies.

    Returns:
        :class:`ClaudePResult` with the parsed dict. On a clean format
        flake (the reply carried no JSON block) the call is resampled
        once under ``--json-schema`` before the error path — see
        :func:`_schema_retry` — and the returned result's cost/usage then
        cover BOTH runs.

    Raises:
        ClaudePError: when the subprocess exits non-zero, times out,
            the result envelope declares failure (``is_error`` or a
            non-``success`` ``subtype`` —
            :class:`ClaudePEnvelopeError`), or the reply contains no
            parseable JSON block (:class:`ClaudePUnparseableError`). A
            format-flake error raised AFTER a spent ``--json-schema``
            retry still carries the combined cost/usage of both runs —
            the retry's spend is billed, never dropped (td623635).
    """
    binary = resolve_binary()
    model = model or os.environ.get("PRECIS_CLAUDE_MODEL", _DEFAULT_MODEL)
    if max_usd is None:
        max_usd_env = os.environ.get("PRECIS_CLAUDE_MAX_USD")
        max_usd = float(max_usd_env) if max_usd_env else _DEFAULT_MAX_USD
    if timeout_s is None:
        timeout_env = os.environ.get("PRECIS_CLAUDE_TIMEOUT_S")
        timeout_s = float(timeout_env) if timeout_env else _DEFAULT_TIMEOUT_S

    args = _invoke_args(
        binary, model, max_usd, bare=bare, extra_args=extra_args, prompt=prompt
    )

    # Bootstrap the OAuth token into the subprocess env. call_claude_p is
    # spawned from launchd daemons (the figure web canvas via precis-web,
    # finding_chase on the system worker) that run no shell hook, so
    # ``claude -p`` would otherwise fall back to stale/absent keychain
    # credentials and 401. Mirrors claude_agent / plan_tick / claude_quota
    # (2026-07-12 incident) — see utils/claude_oauth. Override-safe: a token
    # already in the env (plist var / interactive shell / test) wins.
    proc_env = dict(os.environ)
    if thinking is False:
        # The documented off switch (Claude Code env vars): 0 omits the
        # thinking parameter on the Anthropic API. An ambient cap the
        # operator set is overridden — the caller asked for no thinking.
        proc_env["MAX_THINKING_TOKENS"] = "0"
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
    stdin_kwargs: dict[str, Any] = {}
    if images:
        if len(images) > 1:
            log.warning(
                "claude_p: %d images on one call — the stdin lane carries "
                "one; dropping %d",
                len(images),
                len(images) - 1,
            )
        # Passed conditionally: run_claude stubs in tests predate the kwarg.
        stdin_kwargs["stdin_data"] = bytes(images[0].data)
    res = run_claude(
        args,
        binary=binary,
        label="claude -p",
        timeout_s=timeout_s,
        error_cls=ClaudePError,
        env=proc_env,
        **stdin_kwargs,
    )

    # Parse the envelope once — the subtype rides alongside
    # ``unwrap_envelope``'s four outputs so the failure gate and a failed
    # parse both see it without a second decode.
    env = _envelope_dict(res.stdout or "")
    if env is None:
        payload, cost, usage, is_error = (
            res.stdout or "",
            None,
            dict(_EMPTY_USAGE),
            False,
        )
    else:
        payload, cost, usage, is_error = _unwrap_envelope(env)
    subtype = _subtype_of(env)

    if is_error or (subtype is not None and subtype != "success"):
        # The CLI's own verdict beats a parseable ``result``: an
        # ``is_error``/``error_*`` envelope reports an API/account-level
        # refusal or a spend-cap stop — its ``result`` is the refusal text
        # or a partial answer, never a completed one. A dict ``result``
        # (a structured-output echo) or a lucky string fragment would
        # otherwise land here as a successful parse (gr570522). Route it
        # through the failure path so ``_error_result`` keeps the
        # transport/budget/quota classification.
        if cost is None:
            cost = extract_cost_usd(res.stderr or "")
        raise ClaudePEnvelopeError(
            "claude -p turn failed: the result envelope declared "
            f"is_error={is_error} subtype={subtype}",
            stdout=res.stdout,
            stderr=res.stderr,
            reply=payload,
            cost_usd=cost,
            usage=usage,
            is_error=is_error,
            subtype=subtype,
        )

    data = _parse_last_json_block(payload)
    if data is None:
        # One structured-output retry (gr622034): a clean ``success`` reply
        # with no JSON block is a format flake — resample the identical call
        # under ``--json-schema`` so the CLI validates the answer into the
        # envelope's ``structured_output`` instead of dropping the result.
        outcome = _schema_retry(
            prompt,
            binary=binary,
            model=model,
            max_usd=max_usd,
            timeout_s=timeout_s,
            extra_args=extra_args,
            bare=bare,
            proc_env=proc_env,
            stdin_kwargs=stdin_kwargs,
            first_cost=cost,
            first_usage=usage,
            first_stderr=res.stderr or "",
        )
        if outcome.result is not None:
            return outcome.result
        raise ClaudePUnparseableError(
            "claude -p returned no parseable JSON block",
            stdout=res.stdout,
            stderr=res.stderr,
            reply=payload,
            cost_usd=outcome.cost_usd,
            usage=outcome.usage,
            is_error=is_error,
            subtype=subtype,
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


@dataclass(frozen=True)
class _SchemaRetryOutcome:
    """What the one ``--json-schema`` resample left behind.

    ``result`` is the recovered :class:`ClaudePResult` — ``None`` on any
    retry failure, when the caller raises the FIRST call's unparseable
    error. ``cost_usd``/``usage`` merge BOTH runs' telemetry either way:
    a retry that could not recover the answer still spent real money, so
    it must ride the raised error into ``llm_call_log`` and the daily
    cost ceiling rather than going dark (td623635).
    """

    result: ClaudePResult | None
    cost_usd: float | None
    usage: dict[str, int | None]


def _merged_retry_outcome(
    result: ClaudePResult | None,
    *,
    first_cost: float | None,
    first_usage: dict[str, int | None],
    first_stderr: str,
    cost2: float | None,
    usage2: dict[str, int | None],
) -> _SchemaRetryOutcome:
    """Sum the flaked call's and the retry's telemetry into one outcome.

    ``None + None`` stays ``None`` — never a false ``0`` — matching the
    "no telemetry" shape the rest of this module and the router's
    ``_error_result`` rely on. ``first_cost`` falls back to the first
    call's stderr ``Cost:`` line, the same legacy accounting the main
    path applies.
    """
    cost1 = first_cost if first_cost is not None else extract_cost_usd(first_stderr)
    total = None if cost1 is None and cost2 is None else (cost1 or 0.0) + (cost2 or 0.0)
    merged = {
        key: (
            None
            if first_usage.get(key) is None and usage2.get(key) is None
            else (first_usage.get(key) or 0) + (usage2.get(key) or 0)
        )
        for key in _EMPTY_USAGE
    }
    return _SchemaRetryOutcome(result, total, merged)


def _salvage_retry_telemetry(
    stdout: str, stderr: str
) -> tuple[float | None, dict[str, int | None]]:
    """Best-effort ``(cost, usage)`` off a dead retry's captured streams.

    A ``claude -p`` that exits non-zero or times out can still leave a
    metering envelope on its partial stdout or a ``Cost:`` line on
    stderr — spend the caller is billed for whether or not the retry
    produced an answer.
    """
    env = _envelope_dict(stdout)
    if env is not None:
        _, cost, usage, _ = _unwrap_envelope(env)
    else:
        cost, usage = None, dict(_EMPTY_USAGE)
    if cost is None:
        cost = extract_cost_usd(stderr)
    return cost, usage


def _schema_retry(
    prompt: str,
    *,
    binary: str,
    model: str,
    max_usd: float,
    timeout_s: float,
    extra_args: tuple[str, ...],
    bare: bool,
    proc_env: dict[str, str],
    stdin_kwargs: dict[str, Any],
    first_cost: float | None,
    first_usage: dict[str, int | None],
    first_stderr: str,
) -> _SchemaRetryOutcome:
    """One ``--json-schema`` resample of the identical call (gr622034).

    Reached only from the parse-failure path — a *clean* reply whose text
    carried no JSON block anywhere (envelope failures raise before this).
    The retry constrains the answer to a JSON object via ``--json-schema``:
    the CLI validates the output itself and lands it on the envelope's
    ``structured_output`` field (the ``result`` text stays a summary), so
    the flake converts to a parseable object rather than a dropped result
    — card_describe / quest_tick / draft_refresh's "no parseable JSON
    block" rows. Caller pins that would break the extraction turn are
    lifted for this one run (:func:`_drop_max_turns`); the
    ``--disallowedTools "*"`` no-tools guarantee is untouched.

    Telemetry merges BOTH runs — the flaked call's spend/tokens are real
    and must stay billed to the caller, and so is the retry's: on any
    retry failure (subprocess raise, a retry envelope declaring error,
    still no object) the outcome's ``result`` is ``None`` but its
    ``cost_usd``/``usage`` still sum both runs, so the raised
    :class:`ClaudePUnparseableError` bills the retry's spend to
    ``llm_call_log`` and the daily ceiling instead of discarding it
    (td623635). A caller that already passed ``--json-schema`` gets no
    retry — it is already constrained, so its flake is a validation
    failure, not a format one.
    """
    if any(
        arg == "--json-schema" or arg.startswith("--json-schema=") for arg in extra_args
    ):
        return _merged_retry_outcome(
            None,
            first_cost=first_cost,
            first_usage=first_usage,
            first_stderr=first_stderr,
            cost2=None,
            usage2=dict(_EMPTY_USAGE),
        )
    args = _invoke_args(
        binary,
        model,
        max_usd,
        bare=bare,
        extra_args=_drop_max_turns(extra_args) + ("--json-schema", _OBJECT_SCHEMA_JSON),
        prompt=prompt,
    )
    try:
        res = run_claude(
            args,
            binary=binary,
            label="claude -p",
            timeout_s=timeout_s,
            error_cls=ClaudePError,
            env=proc_env,
            **stdin_kwargs,
        )
    except ClaudeProcessError as exc:
        # A retry-side transport failure (timeout, non-zero exit, an old CLI
        # that doesn't know --json-schema) is not the caller's flake —
        # surface the original error, with whatever spend the dead
        # subprocess still reported.
        log.warning(
            "claude_p: --json-schema retry raised (%s); "
            "surfacing the original parse failure",
            exc,
        )
        cost2, usage2 = _salvage_retry_telemetry(exc.stdout or "", exc.stderr or "")
        return _merged_retry_outcome(
            None,
            first_cost=first_cost,
            first_usage=first_usage,
            first_stderr=first_stderr,
            cost2=cost2,
            usage2=usage2,
        )
    env = _envelope_dict(res.stdout or "")
    structured: Any = None
    if env is None:
        payload, cost2, usage2 = res.stdout or "", None, dict(_EMPTY_USAGE)
    else:
        subtype = _subtype_of(env)
        payload, cost2, usage2, is_error2 = _unwrap_envelope(env)
        if is_error2 or (subtype is not None and subtype != "success"):
            log.warning(
                "claude_p: --json-schema retry's envelope declared failure "
                "(is_error=%s subtype=%s); surfacing the original parse failure",
                is_error2,
                subtype,
            )
            if cost2 is None:
                cost2 = extract_cost_usd(res.stderr or "")
            return _merged_retry_outcome(
                None,
                first_cost=first_cost,
                first_usage=first_usage,
                first_stderr=first_stderr,
                cost2=cost2,
                usage2=usage2,
            )
        structured = env.get("structured_output")
    data = (
        structured if isinstance(structured, dict) else _parse_last_json_block(payload)
    )
    if cost2 is None:
        cost2 = extract_cost_usd(res.stderr or "")
    if data is None:
        return _merged_retry_outcome(
            None,
            first_cost=first_cost,
            first_usage=first_usage,
            first_stderr=first_stderr,
            cost2=cost2,
            usage2=usage2,
        )
    outcome = _merged_retry_outcome(
        None,
        first_cost=first_cost,
        first_usage=first_usage,
        first_stderr=first_stderr,
        cost2=cost2,
        usage2=usage2,
    )
    log.warning(
        "claude_p: reply carried no parseable JSON block; "
        "--json-schema retry recovered the answer"
    )
    merged = outcome.usage
    return _SchemaRetryOutcome(
        ClaudePResult(
            data=data,
            raw_stdout=res.stdout,
            cost_usd=outcome.cost_usd,
            text=payload,
            input_tokens=merged["input_tokens"],
            output_tokens=merged["output_tokens"],
            cache_read_tokens=merged["cache_read_tokens"],
            cache_creation_tokens=merged["cache_creation_tokens"],
        ),
        outcome.cost_usd,
        merged,
    )


#: All-``None`` usage — the "no telemetry available" shape, never a false ``0``.
_EMPTY_USAGE: dict[str, int | None] = {
    "input_tokens": None,
    "output_tokens": None,
    "cache_read_tokens": None,
    "cache_creation_tokens": None,
}


#: Envelope-only fields a bare model answer never carries — the
#: discriminant for a ``type: "result"`` object whose ``result`` is NOT a
#: string. Requiring two keeps a judge schema that happens to name one of
#: these keys on the raw-stdout path.
_ENVELOPE_ONLY_KEYS: tuple[str, ...] = (
    "total_cost_usd",
    "num_turns",
    "session_id",
    "subtype",
    "is_error",
    "duration_ms",
    "duration_api_ms",
    "modelUsage",
    "permission_denied",
    "uuid",
)


def _looks_like_envelope(env: dict[str, Any]) -> bool:
    """Whether ``env`` is the CLI's ``type: "result"`` metering envelope.

    A string ``result`` self-identifies (the historical discriminant). When
    ``result`` is absent, ``null`` or structured (an ``is_error`` envelope's
    way of reporting a refusal with no model answer), the call is only the
    CLI's when the object also carries the envelope-only metering fields —
    a bare model answer dict of the same shape (``{"type": "result",
    "result": null}``) must still fall through to the raw-stdout path so
    the scanner can return it as data.
    """
    if env.get("type") != "result":
        return False
    if isinstance(env.get("result"), str):
        return True
    return sum(1 for key in _ENVELOPE_ONLY_KEYS if key in env) >= 2


def _envelope_dict(text: str) -> dict[str, Any] | None:
    """The ``type: "result"`` envelope dict in ``text``, or ``None``.

    Candidates: the whole text (the normal shape), then each non-empty
    line — a hook/plugin notice or CLI warning printed ahead of or behind
    the envelope must not strand the metering wrapper on the raw-stdout
    path, where the JSON-block scan would parse the *envelope itself* as
    the model's answer (the ``result`` payload swallowed silently).
    """
    candidates = [text]
    candidates.extend(ln for ln in text.splitlines() if ln.strip())
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate.startswith("{"):
            continue
        try:
            env = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(env, dict) and _looks_like_envelope(env):
            return env
    return None


def unwrap_envelope(
    stdout: str,
) -> tuple[str, float | None, dict[str, int | None], bool]:
    """Split ``--output-format json`` stdout into
    (assistant text, cost, usage, is_error).

    ``claude -p --output-format json`` wraps the answer in a metadata envelope
    whose ``result`` field holds the assistant text, whose ``total_cost_usd``
    is the only place the real dollar figure appears, and whose ``usage``
    object carries the token telemetry (see :func:`_extract_usage`).
    ``is_error`` is the envelope's own failure flag — newer CLIs stamp it on
    an API/account-level refusal returned as a result envelope (the router
    reads it to classify an unparseable reply, gr516941).

    Tolerant by design: test stubs (``PRECIS_CLAUDE_BIN``) and any CLI that
    ignores the flag emit the model's JSON directly, so anything that isn't a
    recognizable envelope is handed back unchanged with no cost/usage — the
    caller then falls back to the stderr regex for cost. That keeps the parse
    contract ("last balanced ``{ … }`` block") identical on both shapes.

    A recognized envelope whose ``result`` is not a string yields an EMPTY
    payload (a dict is dumped back to JSON text — a structured-output
    envelope's answer; ``null``/absent/a scalar is a no-answer error
    envelope): handing the raw stdout back instead would let the block
    scan return the metering wrapper as the answer (gr570522).
    """
    env = _envelope_dict(stdout.strip())
    if env is None:
        return stdout, None, dict(_EMPTY_USAGE), False
    return _unwrap_envelope(env)


def _unwrap_envelope(
    env: dict[str, Any],
) -> tuple[str, float | None, dict[str, int | None], bool]:
    """``(payload, cost, usage, is_error)`` off an already-parsed envelope —
    the tail of :func:`unwrap_envelope`, factored so a caller that also
    needs the envelope's ``subtype`` never parses the text twice."""
    raw_cost = env.get("total_cost_usd")
    cost = float(raw_cost) if isinstance(raw_cost, int | float) else None
    result = env.get("result")
    if isinstance(result, str):
        payload = result
    elif isinstance(result, dict):
        payload = json.dumps(result)
    else:
        payload = ""
    return payload, cost, _extract_usage(env), env.get("is_error") is True


def _subtype_of(env: dict[str, Any] | None) -> str | None:
    """The envelope's ``subtype`` string, or ``None`` (no envelope /
    non-string subtype)."""
    if env is None:
        return None
    subtype = env.get("subtype")
    return subtype if isinstance(subtype, str) else None


def _envelope_subtype(stdout: str) -> str | None:
    """The ``type: "result"`` envelope's ``subtype`` (``success``,
    ``error_max_budget_usd``, ``error_during_execution``, …) — read on the
    failure path only, so the normal one pays no second parse. ``None``
    when stdout isn't a recognizable envelope (stub binary, bare text)."""
    return _subtype_of(_envelope_dict(stdout.strip()))


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

    Tolerances, in order (gr570522):

    * :func:`_drop_trailing_commas` — a comma directly preceding a
      ``}``/``]`` closer (``{"a": 1,}``) is the classic model flake;
      dropping it provably cannot drop content, and a no-op on valid input,
      so it runs FIRST: a trailing-comma'd final object must still win the
      last-object contract over an earlier clean one;
    * strict, then ``strict=False`` — mirroring
      :func:`precis.utils.llm.json_reply.extract_json_object`'s documented
      tolerance: a reply otherwise well-formed JSON but carrying a raw
      (unescaped) control character inside a string value (a literal
      newline instead of ``\\n``, seen in prod quest-tick output,
      gr345336) is accepted on the second pass;
    * :func:`_double_encoded_text` — the whole reply as ONE JSON string
      carrying the object (``"{\\"a\\": 1}"``, a model that serialized
      its answer twice) gets exactly one inner decode.

    A truncated/unbalanced reply has no complete object under any of these
    and still returns ``None`` — closing open containers would launder a
    truncated generation into a "successful" parse (the same refusal
    ``extract_json_object`` documents).
    """
    if not text:
        return None
    found = _scan_tolerant(text)
    if found is not None:
        return found
    inner = _double_encoded_text(text)
    if inner is not None:
        return _scan_tolerant(inner)
    return None


def _scan_tolerant(text: str) -> dict[str, Any] | None:
    """The trailing-comma repair plus the strict/``strict=False`` scans —
    the full candidate-level tolerance, reused for a decoded inner
    string."""
    cleaned = _drop_trailing_commas(text)
    if cleaned is not None:
        text = cleaned
    for strict in (True, False):
        last = _scan_last_json_block(text, strict=strict)
        if last is not None:
            return last
    return None


def _drop_trailing_commas(text: str) -> str | None:
    """``text`` with every comma directly preceding a ``}``/``]`` removed —
    or ``None`` when nothing changed (the common case).

    String-aware: a ``,`` inside a quoted value is content, never a
    delimiter; whitespace between the comma and the closer is tolerated.
    A no-op on valid JSON (a comma before a closer is never legal), so it
    runs BEFORE the scans — a repaired trailing object must still win the
    last-object contract — and logs the count so the repair rate stays
    measurable (same discipline as
    :func:`precis.utils.llm.json_reply._repair_stray_closers`).
    """
    out: list[str] = []
    in_str = False
    esc = False
    dropped = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                dropped += 1
                i += 1
                continue
        out.append(ch)
        i += 1
    if dropped == 0:
        return None
    log.warning(
        "claude_p: dropped %d comma(s) directly before a closer to parse the reply",
        dropped,
    )
    return "".join(out)


def _double_encoded_text(text: str) -> str | None:
    """The inner payload when the WHOLE reply is one JSON string carrying
    JSON — a model that serialized its answer twice. ``None`` otherwise;
    bounded to exactly one extra decode level (a triple-encoded reply
    stays unparseable rather than unwrap recursively)."""
    try:
        inner = json.loads(text.strip())
    except Exception:
        return None
    if isinstance(inner, str) and "{" in inner:
        log.warning("claude_p: reply was a JSON-encoded string — decoding once more")
        return inner
    return None


def _scan_last_json_block(text: str, *, strict: bool) -> dict[str, Any] | None:
    """One :func:`_parse_last_json_block` pass under the given decoder
    strictness (``strict=False`` permits raw control chars inside
    strings, nothing else)."""
    decoder = json.JSONDecoder(strict=strict)
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
    "unwrap_envelope",
]

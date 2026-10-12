"""Invoke the LLM router per Discord turn — claude -p streaming, or the
BIG placement chain.

Two lanes, picked by the effective ``--model`` value:

* a concrete claude id (``/model opus``, a config override) → the original
  streaming lane: ``dispatch_async`` drives ``claude -p`` and this module
  parses its stream-json events into text + progress updates.
* the ``local`` sentinel (the deployed default) → :func:`_invoke_via_chain`:
  the sync ``route`` walks the operator-owned ``llm.chain.big`` placement
  chain (local/OSS rung first, cloud fallback) in a worker thread — no
  ``claude -p``, no event stream, the reply comes off the aggregated
  :class:`LlmResult`.

Budget degradation (gr479851): a turn whose tier pauses at the budget
breaker (the claude quota taper, a dollar cap) is re-asked on each smaller
claude tier, then on a free served local model, and answers with a one-line
``-#`` notice naming the tier that served it (:func:`_degrade`). A ``/model``
pin is not degraded — the pause is explained instead. Only this
conversational lane degrades; worker passes still pause.

asa_bot spawns a fresh turn per Discord message either way. Captures the
final assistant text + per-turn metadata (stop_reason, token counts).
On the streaming lane, progress events flow out so the Discord progress
indicator can update.

Phase 3 of the router-migration plan (follow-up): this used
to hand-roll ``asyncio.create_subprocess_exec`` directly; it now builds
a :class:`~precis.utils.llm.router.LlmRequest` and calls
:func:`~precis.utils.llm.router.dispatch_async`, which streams through
:func:`~precis.utils.claude_agent.call_claude_agent_async` the same
way asa_bot's own subprocess used to. The ``on_event`` callback below
still runs the exact :func:`_handle_event` parsing this module always
used, so every field on :class:`ClaudeResult` and every ``on_progress``
event shape is populated off the SAME per-line stream-json events as
before — ``dispatch_async``'s own aggregated
:class:`~precis.utils.llm.router.LlmResult` is consulted for only one
thing it alone knows: whether (and why) the call failed.

``llm_call_log`` now ALSO records every turn's cost/tokens via the
router (``LlmRequest.log_call=True``) — a second, independent record
from the Stop-hook capture shim
(``deploy/roles/asa_bot/files/capture_assistant_turn.py``, keyed off
``ASA_CONV_SLUG``, untouched by this migration). The two can be
reconciled, or one retired, later; for now they simply coexist.

Context hygiene: every turn is a fresh ``claude -p``, so whatever the CLI
advertises is paid for in every prompt. Three knobs trim it. ``--tools``
(:data:`_ASA_TOOLS`) restricts the advertised built-in tool set to the four
pre-approved tools (denied tools stay advertised, so the hard deny list is
kept as belt and braces); MCP tools are unaffected. ``autoMemoryEnabled:
false`` drops the auto-memory section and ``disableBundledSkills: true`` drops
the bundled skills list (code-review, loop, ...), both passed via ``--settings``
(:data:`_ASA_SETTINGS`) so the behaviour does not depend on the deployed user
settings file alone. The Agent roster is intentional and stays; the
environment block is not configurable.

Write attribution: ``acting_user`` (the speaker's account abbrev, ``None``
when unmapped) reaches the turn's precis writes on both lanes — as
``PRECIS_ACTING_USER`` in the env overlay, which the deployed mcp.json passes
to the ``precis serve`` that ``claude -p`` spawns, and as an enclosing
``revision_context`` around the chain lane's in-process tool loop
(:mod:`precis.acting_user`). Unmapped turns set it empty and write as
``agent``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from asa_bot.config import LLMConfig
from asa_bot.llm_runtime import warm_runtime
from asa_bot.oauth import ensure_oauth_token
from precis.acting_user import ENV_VAR as ACTING_USER_ENV
from precis.acting_user import actor_for
from precis.utils.llm.degrade import (
    degrade_steps,
    is_budget_pause,
    local_fallback_model,
    tier_for_model,
)
from precis.utils.llm.router import (
    LlmRequest,
    LlmResult,
    Tier,
    dispatch_async,
    resolve_model,
    route,
)

log = logging.getLogger(__name__)


# First-sentence detector: terminator (`.`, `!`, `?`) followed by
# whitespace, newline, or end-of-text. The 280 char ceiling guards
# against degenerate cases where asa generates a wall of text with no
# sentence break — in that case we'd rather skip the ack than send a
# paragraph in pieces.
_FIRST_SENTENCE_RE = re.compile(r"[.!?](?:\s|$|\n)")
_FIRST_SENTENCE_MAX_CHARS = 280

# asa_bot's hand-rolled subprocess never capped a turn's dollar cost. The
# router's claude_agent transport ALWAYS enforces one (``--max-budget-usd``
# is a fixed CLI flag, not optional) — its own default ($2, sized for a
# ~20-turn sonnet session, see claude_agent._DEFAULT_MAX_USD) is far too
# tight for a 100-turn opus Discord conversation, and unlike a worker pass a
# Discord user has no way to "resume" a turn cut off mid-answer. Pin a
# generous ceiling here so a turn stays effectively uncapped — this IS a
# real behavior change from "no cap at all" to "a $50 backstop"; flagged for
# review/smoke-test rather than silently inheriting the shared $2 default.
_MAX_USD_CEILING = 50.0

# The router's claude_agent transport ALWAYS passes
# ``--permission-mode bypassPermissions`` (no per-caller override wired
# through the router yet) — before this migration, asa_bot's hand-rolled
# subprocess never passed ``--permission-mode`` at all, so with no TTY to
# approve anything, only the tools pre-approved in its deployed
# ``~/.claude/settings.json`` (``deploy/roles/asa_bot/templates/
# claude_settings.json.j2``: ``mcp__precis``, ``Read``, ``Glob``, ``Grep``,
# ``Agent``) were ever reachable. ``bypassPermissions`` auto-approves
# everything NOT explicitly denied, so without this list the migration
# would silently hand Asa live ``Bash``/``Write``/``Edit`` access over
# Discord. This is a HARD deny (``--settings`` → ``permissions.deny``,
# built in ``claude_agent._resolve_agent_args``) that applies regardless of
# ``permission_mode`` — restores the pre-migration tool boundary exactly,
# rather than reducing anything Asa could actually do before.
_ASA_DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash",
    "Write",
    "Edit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
)

# Advertised built-in tool set (``--tools``): exactly the four pre-approved in
# the deployed settings. Passed in ``extra_args`` — the argv ends with ``--``
# before the prompt, so ``--tools``' variadic cannot swallow it.
_ASA_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep", "Agent")
_ASA_EXTRA_ARGS: tuple[str, ...] = ("--tools", ",".join(_ASA_TOOLS))

# Top-level ``--settings`` keys (context hygiene, see module docstring).
_ASA_SETTINGS: dict[str, Any] = {
    "autoMemoryEnabled": False,
    "disableBundledSkills": True,
}

# Chain-lane turns hold a thread for the WHOLE turn (sync route, up to
# turn_timeout_seconds each) — on asyncio's shared default executor that
# would let a handful of long conversations starve every other to_thread
# user in the process. A dedicated small pool bounds the chain lane's
# thread footprint instead; a 5th concurrent turn queues here (Discord
# shows its working indicator) rather than eating the shared pool.
#
# Built lazily (:func:`_get_chain_executor`) rather than at import time —
# no thread pool should spin up just from importing this module (e.g. in
# tests that never touch the chain lane) — and torn down explicitly via
# :func:`shutdown_chain_executor` from ``bot.run``'s own shutdown path
# (``bot.py``'s ``finally`` block, alongside the listener/precis-client
# teardown) rather than left to leak until process exit.
_chain_executor: ThreadPoolExecutor | None = None
_chain_executor_lock = threading.Lock()


def _get_chain_executor() -> ThreadPoolExecutor:
    """The lazily-created, process-wide chain-lane thread pool."""
    global _chain_executor
    with _chain_executor_lock:
        if _chain_executor is None:
            _chain_executor = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="asa-chain"
            )
        return _chain_executor


def shutdown_chain_executor(*, wait: bool = True) -> None:
    """Explicit shutdown hook for the chain-lane pool.

    Called from ``asa_bot.bot.run``'s own shutdown path so the pool's
    threads are joined on a clean bot exit instead of left dangling.
    Idempotent — safe to call when the pool was never built (no chain-lane
    turn ever ran) or a second time.
    """
    global _chain_executor
    with _chain_executor_lock:
        if _chain_executor is not None:
            _chain_executor.shutdown(wait=wait)
            _chain_executor = None


# The ``--model`` sentinel that routes a turn through the router's BIG
# *placement chain* (operator-owned ``llm.chain.big``: local/OSS rung first,
# cloud fallback) instead of pinning a claude id onto the ``claude -p``
# streaming lane. Matches the ``local`` alias the router's
# PLANNER_TIER_BY_ALIAS already maps to ``Tier.BIG`` ("local now pins BIG
# directly"), so `/model local` and the deployed config default both land
# here. Chosen over an ``llm.op.asa_bot`` registry row because
# ``dispatch_async`` resolves its streaming decision from ``req.tier``
# *before* the operations layer runs — an op row could never divert the
# claude streaming path.
_LOCAL_MODEL_SENTINEL = "local"


class ClaudeResult:
    """The aggregated result of one claude -p invocation."""

    def __init__(self) -> None:
        self.text: str = ""
        self.stop_reason: str | None = None
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.cache_read_tokens: int | None = None
        self.cache_creation_tokens: int | None = None
        self.duration_ms: int | None = None
        self.error: str | None = None
        self.tool_uses: list[str] = []
        self.session_id: str | None = None
        # The router refused the turn before any provider ran (budget breaker,
        # busy local slots) and its failure class — what the budget ladder
        # (:func:`_degrade`) keys on.
        self.paused: bool = False
        self.reason_class: str | None = None
        # True once we've emitted the first-sentence ack event so we
        # don't fire it again as more text streams in.
        self.first_sentence_emitted: bool = False
        # The exact ack sentence we streamed as its own Discord message.
        # The caller strips this leading span from the final reply so the
        # opening sentence isn't posted twice (gripe #48766).
        self.first_sentence: str = ""


def _flag_value(argv: list[str], flag: str, default: str) -> str:
    """Pull a ``--flag value`` pair out of ``cfg.command``, falling back to
    ``default`` when the flag isn't present.

    ``LLMConfig.command`` is the literal argv asa_bot used to hand-roll to
    the subprocess (including any live ``--model`` override
    ``bot._llm_with_override`` splices in per-guild); the router wants these
    as structured :class:`~precis.utils.llm.router.LlmRequest` fields
    instead of a buried CLI flag, so this recovers them without asa_bot
    needing a new config field.
    """
    for i, tok in enumerate(argv):
        if tok == flag and i + 1 < len(argv):
            return argv[i + 1]
    return default


def _turn_overlay(
    cfg: LLMConfig, conv_slug: str, acting_user: str | None
) -> dict[str, str]:
    """The per-turn env overlay: config env, conv slug, speaker account.

    :data:`~precis.acting_user.ENV_VAR` is always set, empty when unmapped,
    so a value in asa's own environment never attributes another turn.
    """
    overlay: dict[str, str] = dict(cfg.env)
    # Hooks read ASA_CONV_SLUG to attach a Stop-hook capture to the right
    # conv ref (see the module docstring — an independent mechanism from the
    # router's own llm_call_log write).
    overlay["ASA_CONV_SLUG"] = conv_slug
    overlay[ACTING_USER_ENV] = acting_user or ""
    return overlay


def _route_as(req: LlmRequest, acting_user: str | None) -> LlmResult:
    """``route(req)`` with in-process verb writes attributed to ``acting_user``.

    Runs on the chain executor's thread, so the revision context is entered
    here rather than in the coroutine (``run_in_executor`` does not carry
    contextvars). A ``claude_agent`` fallback rung gets the env overlay instead.
    """
    if not acting_user:
        return route(req)
    from precis.store.revision_context import revision_context

    with revision_context(actor=actor_for(acting_user)):
        return route(req)


async def invoke(
    cfg: LLMConfig,
    system_prompt: str,
    user_message: str,
    *,
    conv_slug: str,
    on_progress: Callable[..., Any] | None = None,
    acting_user: str | None = None,
    pinned: bool = False,
) -> ClaudeResult:
    """Run one claude -p turn via the router. Returns the aggregated ClaudeResult.

    ``on_progress`` (if provided) is awaited with each progress event:
      - ('tool_use', tool_name, tool_args) — claude is about to call a tool
      - ('first_sentence', sentence) — the SOUL-mandated opening ack
      - ('text_partial', accumulated_so_far) — periodic text update

    Caller posts these to Discord as a single edited "working..."
    message (don't spam new posts).

    A turn whose tier pauses at the budget breaker degrades down the ladder
    (:func:`_degrade`) unless ``pinned`` — the speaker chose the model with
    ``/model`` — in which case the pause is explained instead
    (:func:`_pinned_pause`).

    Never raises — a router/transport failure lands on
    :attr:`ClaudeResult.error` instead, exactly like the old hand-rolled
    subprocess path: bot.py's per-turn queue consumer has no fallback reply
    if this raised rather than returning a gracefully-errored result.
    """
    model = _flag_value(cfg.command, "--model", resolve_model(Tier.FRONTIER))
    max_turns_str = _flag_value(cfg.command, "--max-turns", "100")
    try:
        max_turns = int(max_turns_str)
    except ValueError:
        max_turns = 100

    # DEBUG: opt-in dump of the full prompt for inspection. Off unless
    # ``ASA_PROMPT_DUMP`` names a file path; last turn wins (overwritten each
    # invocation). Kept behind an env var so no host-specific path lives in source.
    dump_target = os.environ.get("ASA_PROMPT_DUMP")
    if dump_target:
        try:
            from datetime import UTC, datetime
            from pathlib import Path

            Path(dump_target).write_text(
                f"=== timestamp: {datetime.now(tz=UTC).isoformat()} ===\n"
                f"=== conv_slug: {conv_slug} ===\n"
                f"=== model: {model}  max_turns: {max_turns}  "
                f"mcp_config: {cfg.mcp_config_path} ===\n\n"
                f"=== SYSTEM PROMPT ({len(system_prompt)} chars) ===\n"
                f"{system_prompt}\n\n"
                f"=== USER MESSAGE ({len(user_message)} chars) ===\n"
                f"{user_message}\n",
                encoding="utf-8",
            )
        except Exception:
            log.exception("debug prompt dump failed; continuing")

    turn = _Turn(
        cfg=cfg,
        system_prompt=system_prompt,
        user_message=user_message,
        conv_slug=conv_slug,
        on_progress=on_progress,
        acting_user=acting_user,
        max_turns=max_turns,
    )
    is_local = model.strip().lower() == _LOCAL_MODEL_SENTINEL
    start = tier_for_model(model)
    if is_local:
        result = await _invoke_via_chain(turn)
    else:
        result = await _invoke_streaming(turn, tier=start, model=model)
    if not _budget_paused(result):
        return result
    # A pin of the ``local`` sentinel is the automatic lane, not a choice of tier.
    if pinned and not is_local:
        return _pinned_pause(result, model)
    return await _degrade(turn, start=start, first=result)


@dataclasses.dataclass(frozen=True, slots=True)
class _Turn:
    """The per-turn inputs every lane and ladder step shares."""

    cfg: LLMConfig
    system_prompt: str
    user_message: str
    conv_slug: str
    on_progress: Callable[..., Any] | None
    acting_user: str | None
    max_turns: int


def _budget_paused(result: ClaudeResult) -> bool:
    """True when the router refused the turn at the budget breaker."""
    return is_budget_pause(result.paused, result.reason_class)


def _pinned_pause(result: ClaudeResult, model: str) -> ClaudeResult:
    """Explain a pause on a ``/model`` pin instead of silently degrading it."""
    result.error = (
        f"{result.error} Your /model pin `{model}` keeps this conversation on "
        f"that tier, so the turn was not moved to a smaller one. `/model local` "
        f"returns to automatic routing (smaller tiers answer while the window "
        f"is low), or resume paid work now on the Budget page "
        f"(/status?tab=budget)."
    )
    return result


def _with_notice(result: ClaudeResult, label: str, first: ClaudeResult) -> ClaudeResult:
    """Append the one-line degraded-tier notice (Discord ``-#`` subtext)."""
    why = (
        "the claude window is low"
        if first.reason_class == "quota"
        else "the budget cap is reached"
    )
    if result.text:
        result.text = f"{result.text}\n\n-# answered on {label} because {why}"
    return result


async def _degrade(turn: _Turn, *, start: Tier, first: ClaudeResult) -> ClaudeResult:
    """Walk the budget ladder (:func:`~precis.utils.llm.degrade.degrade_steps`)
    after ``start`` paused (gr479851).

    Claude steps run on the streaming lane, the local step on the chain lane
    with the served model :func:`~precis.utils.llm.degrade.local_fallback_model`
    picks. A paused step costs nothing — the breaker refuses before any
    provider runs — so walking is cheap. The first step that is not a budget
    pause answers, with the notice line; a real failure there is surfaced as
    is. Refuses (the original pause reason, plus why local could not serve)
    only when no tier can.

    Only asa's conversational turn opts in: worker passes (taproot, agentic
    job runs, plan ticks) call the router directly and still pause.
    """
    degraded_from = start.value
    for step in degrade_steps(start, opt_in=True):
        if step.local:
            return await _degrade_local(turn, start=start, first=first)
        assert step.model is not None
        res = await _invoke_streaming(
            turn,
            tier=step.tier,
            model=step.model,
            placement=step.placement,
            degraded_from=degraded_from,
        )
        if not _budget_paused(res):
            log.info(
                "asa budget degrade: %s paused → answered on %s",
                start.value,
                step.model,
            )
            return _with_notice(res, f"{step.tier.value} (`{step.model}`)", first)
    return first


async def _degrade_local(
    turn: _Turn, *, start: Tier, first: ClaudeResult
) -> ClaudeResult:
    """The ladder's last step: a free served local model on the chain lane."""
    loop = asyncio.get_running_loop()
    local_model = await loop.run_in_executor(
        _get_chain_executor(), partial(local_fallback_model, tools_needed=True)
    )
    if local_model is None:
        first.error = (
            f"{first.error} No smaller tier could serve this turn "
            f"(no local model is served)."
        )
        return first
    res = await _invoke_via_chain(
        turn, placement="local", model=local_model, degraded_from=start.value
    )
    if res.text:
        log.info(
            "asa budget degrade: %s paused → answered on local %s",
            start.value,
            local_model,
        )
        return _with_notice(res, f"local (`{local_model}`)", first)
    first.error = (
        f"{first.error} No smaller tier could serve this turn "
        f"(local `{local_model}`: {res.error or 'empty response'})."
    )
    return first


async def _invoke_streaming(
    turn: _Turn,
    *,
    tier: Tier,
    model: str,
    placement: str | None = None,
    degraded_from: str | None = None,
) -> ClaudeResult:
    """One turn on the ``claude -p`` streaming lane (a concrete claude id)."""
    cfg = turn.cfg
    result = ClaudeResult()
    overlay = _turn_overlay(cfg, turn.conv_slug, turn.acting_user)
    # asa_bot can run as a bare `deploy` user with no ~/.claude state, in
    # which case CLAUDE_CODE_OAUTH_TOKEN comes from the vault over asa's OWN
    # PRECIS_DATABASE_URL connection (asa_bot.secrets.reveal_secret) — NOT
    # precis.secrets.get_secret's bound-Store vault path that
    # claude_agent._prepare_agent_env falls back to (asa_bot never binds a
    # Store in-process; it talks to precis over a subprocess MCP server, so
    # that path silently no-ops here). Resolve it with asa's own proven
    # helper and thread it through env_overlay, applied last, so it wins
    # regardless of whether the router's own vault attempt no-ops.
    ensure_oauth_token(overlay)

    async def on_event(evt: dict[str, Any]) -> None:
        await _handle_event(evt, result, turn.on_progress)

    req = LlmRequest(
        # The breaker gates (and tapers) on this tier, so it names the tier the
        # model belongs to rather than a blanket FRONTIER.
        tier=tier,
        prompt=turn.user_message,
        tools_needed=True,
        # Pin the model explicitly (rather than deferring to the tier
        # default) so a live ``--model`` override applied to ``cfg.command``
        # (bot._llm_with_override, a per-guild slash-command knob) still
        # takes effect — the tier default alone can't see that override.
        model=model,
        # A non-FRONTIER tier chain may lead with a local rung; dispatch_async
        # only streams a claude_agent rung 0, so drop local rungs. This assumes
        # the tier's cloud rungs are claude: a hosted-OSS rung (an OSS-first
        # chain with PRECIS_LLM_BASE_URL set) also counts as cloud and would
        # take this turn off the streaming lane.
        placement=placement or (None if tier is Tier.FRONTIER else "cloud"),
        degraded_from=degraded_from,
        system_prompt=turn.system_prompt,
        mcp_config=cfg.mcp_config_path,
        max_turns=turn.max_turns,
        max_usd=_MAX_USD_CEILING,
        timeout_s=float(cfg.turn_timeout_seconds),
        disallowed_tools=_ASA_DISALLOWED_TOOLS,
        extra_args=_ASA_EXTRA_ARGS,
        settings_extra=_ASA_SETTINGS,
        # Required for the on_event stream this whole module is built on —
        # not read from cfg.command since the parsing below hard-depends on
        # it (not a user-overridable knob the way --model/--max-turns are).
        output_format="stream-json",
        source="asa_bot",
        # A person is waiting on this turn: never tapered (gr347551).
        work_class="interactive",
        env_overlay=overlay,
        cwd=cfg.cwd,
        log_call=True,
        on_event=on_event,
    )

    try:
        llm_result: LlmResult = await dispatch_async(req)
    except Exception:
        # Never raise out of invoke() — see the docstring's contract note.
        log.exception("claude_invoke: dispatch_async raised unexpectedly")
        result.error = "internal error dispatching to the LLM router"
        return result

    result.paused = llm_result.paused
    result.reason_class = llm_result.reason_class
    if llm_result.error is not None:
        if "timed out after" in llm_result.error:
            # Preserve the exact message shape the old hand-rolled
            # asyncio.wait_for(...) timeout produced (some downstream
            # consumer may already grep logs for it), rather than surfacing
            # the router transport's own wording.
            log.error("claude turn timed out after %ds", cfg.turn_timeout_seconds)
            result.error = f"turn exceeded {cfg.turn_timeout_seconds}s timeout"
        elif not result.text:
            # A non-zero exit with SOME text already streamed (a recoverable
            # exhaustion, or a CLI-teardown quirk) is swallowed as a silent
            # success — mirrors the old code's identical
            # `rc != 0 and not result.text` gate. Only a failure with
            # nothing to show gets surfaced to the user.
            result.error = llm_result.error

    return result


async def _invoke_via_chain(
    turn: _Turn,
    *,
    placement: str | None = None,
    model: str | None = None,
    degraded_from: str | None = None,
) -> ClaudeResult:
    """One turn on the router's BIG placement chain (``--model local``).

    The chain's rungs are OSS transports (the in-process ``openai_tools``
    loop / a hosted OpenAI-compat endpoint), none of which stream
    ``on_event`` — so this lane trades the Discord progress ticker
    (tool_use / first_sentence / text_partial) for operator-owned routing,
    and the final text is lifted off the aggregated :class:`LlmResult`
    instead of accumulated per-event. ``route`` is synchronous and the
    OSS tool loop blocks for the whole turn — it runs in a worker thread so
    the Discord gateway heartbeat (and other queued turns' progress edits)
    keeps running.

    The claude-lane knobs (``disallowed_tools``, the OAuth ``env_overlay``,
    ``cwd``) still ride along: the OSS transports ignore them, but
    ``resolve_chain`` can legitimately fall back to a ``claude_agent`` rung
    (a tool-filtered or default chain), and that rung must arrive as fully
    equipped as the streaming lane's.

    Never raises — same contract as :func:`invoke`.
    """
    cfg = turn.cfg
    result = ClaudeResult()
    overlay = _turn_overlay(cfg, turn.conv_slug, turn.acting_user)
    ensure_oauth_token(overlay)

    req = LlmRequest(
        tier=Tier.BIG,
        prompt=turn.user_message,
        tools_needed=True,
        model=model,
        placement=placement,
        degraded_from=degraded_from,
        system_prompt=turn.system_prompt,
        mcp_config=cfg.mcp_config_path,
        max_turns=turn.max_turns,
        max_usd=_MAX_USD_CEILING,
        timeout_s=float(cfg.turn_timeout_seconds),
        disallowed_tools=_ASA_DISALLOWED_TOOLS,
        extra_args=_ASA_EXTRA_ARGS,
        settings_extra=_ASA_SETTINGS,
        source="asa_bot",
        # A person is waiting on this turn: never tapered (gr347551).
        work_class="interactive",
        env_overlay=overlay,
        cwd=cfg.cwd,
        log_call=True,
    )
    loop = asyncio.get_running_loop()
    executor = _get_chain_executor()
    try:
        await loop.run_in_executor(executor, warm_runtime)
        llm_result: LlmResult = await loop.run_in_executor(
            executor, partial(_route_as, req, turn.acting_user)
        )
    except Exception:
        # Never raise out of invoke() — see its docstring's contract note.
        log.exception("claude_invoke: chain dispatch raised unexpectedly")
        result.error = "internal error dispatching to the LLM router"
        return result

    result.text = llm_result.text or ""
    result.paused = llm_result.paused
    result.reason_class = llm_result.reason_class
    result.stop_reason = llm_result.stop_reason
    if llm_result.duration_s is not None:
        result.duration_ms = int(llm_result.duration_s * 1000)
    result.input_tokens = llm_result.input_tokens
    result.output_tokens = llm_result.output_tokens
    result.cache_read_tokens = llm_result.cache_read_tokens
    result.cache_creation_tokens = llm_result.cache_creation_tokens
    if llm_result.error is not None and not result.text:
        # Same gate as the streaming lane: a failure with SOME text is a
        # recoverable exhaustion, surfaced only when there is nothing to show.
        result.error = llm_result.error
    return result


async def _handle_event(
    evt: dict[str, Any],
    result: ClaudeResult,
    on_progress: Callable[..., Any] | None,
) -> None:
    etype = evt.get("type")
    # Claude Code stream-json shape: a series of "system" / "assistant"
    # / "user" / "result" events. Plus inner Anthropic stream events.
    if etype == "system" and evt.get("subtype") == "init":
        result.session_id = evt.get("session_id")
    elif etype == "assistant":
        msg = evt.get("message", {})
        # Accumulate text + record tool uses. Emit text_partial so the
        # Discord progress indicator can heartbeat between tool calls
        # (otherwise it sits frozen while claude is generating prose
        # and the user thinks the bot stalled). Tool inputs ride along
        # with tool_use so `mcp__precis__get(kind='paper', id='…')` can
        # render in the progress message instead of a bare tool name.
        text_added = False
        for block in msg.get("content", []) or []:
            btype = block.get("type")
            if btype == "text":
                chunk = block.get("text", "")
                if chunk:
                    result.text += chunk
                    text_added = True
            elif btype == "tool_use":
                tname = block.get("name", "?")
                targs = block.get("input") or {}
                result.tool_uses.append(tname)
                if on_progress:
                    await on_progress(("tool_use", tname, targs))
        if text_added and on_progress:
            await on_progress(("text_partial", len(result.text)))
            await _maybe_emit_first_sentence(result, on_progress)
        # Track usage if exposed mid-stream.
        usage = msg.get("usage") or {}
        if usage:
            _absorb_usage(result, usage)
    elif etype == "user":
        # Tool results echo back — ignore for text aggregation.
        pass
    elif etype == "result":
        result.stop_reason = evt.get("subtype") or evt.get("stop_reason")
        result.duration_ms = evt.get("duration_ms")
        _absorb_usage(result, evt.get("usage") or {})
        # The terminal result may carry the final text too.
        if not result.text and (rtext := evt.get("result")):
            result.text = str(rtext)
    elif etype == "error":
        result.error = str(evt.get("message") or evt)


async def _maybe_emit_first_sentence(
    result: ClaudeResult, on_progress: Callable[..., Any]
) -> None:
    """Fire the ``first_sentence`` event once, when we've seen one.

    The first text claude emits is the SOUL-mandated acknowledgement
    ("Asking researcher to dig into MOFs"). Streaming it as a message
    as soon as it's complete gives the Discord user an immediate "I
    heard you" rather than five minutes of working-indicator silence.
    Mid-stream edits and tool calls don't disturb it — the ack stays
    as its own message; the final reply lands separately.
    """
    if result.first_sentence_emitted:
        return
    snippet = result.text[:_FIRST_SENTENCE_MAX_CHARS]
    m = _FIRST_SENTENCE_RE.search(snippet)
    if not m:
        return
    sentence = result.text[: m.end()].strip()
    if not sentence:
        return
    result.first_sentence_emitted = True
    result.first_sentence = sentence
    await on_progress(("first_sentence", sentence))


def _absorb_usage(result: ClaudeResult, usage: dict[str, Any]) -> None:
    for k in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        v = usage.get(k)
        if v is None:
            continue
        if k == "input_tokens":
            result.input_tokens = int(v)
        elif k == "output_tokens":
            result.output_tokens = int(v)
        elif k == "cache_read_input_tokens":
            result.cache_read_tokens = int(v)
        elif k == "cache_creation_input_tokens":
            result.cache_creation_tokens = int(v)

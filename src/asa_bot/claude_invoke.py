"""Invoke claude -p via the LLM router and stream the result.

asa_bot spawns a fresh claude per Discord turn. Captures the final
assistant text + per-turn metadata (stop_reason, token counts).
Streams progress events out so the Discord progress indicator can
update.

Phase 3 of the router-migration plan (ADR 0046 follow-up): this used
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
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from typing import Any

from asa_bot.config import LLMConfig
from asa_bot.oauth import ensure_oauth_token
from precis.utils.llm.router import LlmRequest, LlmResult, Tier, dispatch_async

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


async def invoke(
    cfg: LLMConfig,
    system_prompt: str,
    user_message: str,
    *,
    conv_slug: str,
    on_progress: Callable[..., Any] | None = None,
) -> ClaudeResult:
    """Run one claude -p turn via the router. Returns the aggregated ClaudeResult.

    ``on_progress`` (if provided) is awaited with each progress event:
      - ('tool_use', tool_name, tool_args) — claude is about to call a tool
      - ('first_sentence', sentence) — the SOUL-mandated opening ack
      - ('text_partial', accumulated_so_far) — periodic text update

    Caller posts these to Discord as a single edited "working..."
    message (don't spam new posts).

    Never raises — a router/transport failure lands on
    :attr:`ClaudeResult.error` instead, exactly like the old hand-rolled
    subprocess path: bot.py's per-turn queue consumer has no fallback reply
    if this raised rather than returning a gracefully-errored result.
    """
    model = _flag_value(cfg.command, "--model", "claude-opus-4-8")
    max_turns_str = _flag_value(cfg.command, "--max-turns", "100")
    try:
        max_turns = int(max_turns_str)
    except ValueError:
        max_turns = 100

    # DEBUG: opt-in dump of the full prompt for inspection. Off unless
    # ``ASA_PROMPT_DUMP`` names a file path; last turn wins (overwritten each
    # invocation). Kept env-gated so no host-specific path lives in source.
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

    result = ClaudeResult()

    # Hooks read ASA_CONV_SLUG to attach a Stop-hook capture to the right
    # conv ref (see the module docstring — an independent mechanism from the
    # router's own llm_call_log write below).
    overlay: dict[str, str] = dict(cfg.env)
    overlay["ASA_CONV_SLUG"] = conv_slug
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
        await _handle_event(evt, result, on_progress)

    req = LlmRequest(
        tier=Tier.CLOUD_SUPER,
        prompt=user_message,
        tools_needed=True,
        # Pin the model explicitly (rather than deferring to the tier
        # default) so a live ``--model`` override applied to ``cfg.command``
        # (bot._llm_with_override, a per-guild slash-command knob) still
        # takes effect — the tier default alone can't see that override.
        model=model,
        system_prompt=system_prompt,
        mcp_config=cfg.mcp_config_path,
        max_turns=max_turns,
        max_usd=_MAX_USD_CEILING,
        timeout_s=float(cfg.turn_timeout_seconds),
        disallowed_tools=_ASA_DISALLOWED_TOOLS,
        # Required for the on_event stream this whole module is built on —
        # not read from cfg.command since the parsing below hard-depends on
        # it (not a user-overridable knob the way --model/--max-turns are).
        output_format="stream-json",
        source="asa_bot",
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

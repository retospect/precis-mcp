"""Invoke claude -p as a subprocess and stream the result.

asa_bot spawns a fresh claude per Discord turn. Captures the final
assistant text + per-turn metadata (stop_reason, token counts).
Streams progress events out so the Discord progress indicator can
update.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Callable
from typing import Any

from asa_bot.config import LLMConfig
from asa_bot.oauth import ensure_oauth_token

log = logging.getLogger(__name__)


# First-sentence detector: terminator (`.`, `!`, `?`) followed by
# whitespace, newline, or end-of-text. The 280 char ceiling guards
# against degenerate cases where asa generates a wall of text with no
# sentence break — in that case we'd rather skip the ack than send a
# paragraph in pieces.
_FIRST_SENTENCE_RE = re.compile(r"[.!?](?:\s|$|\n)")
_FIRST_SENTENCE_MAX_CHARS = 280


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


async def invoke(
    cfg: LLMConfig,
    system_prompt: str,
    user_message: str,
    *,
    conv_slug: str,
    on_progress: Callable[..., Any] | None = None,
) -> ClaudeResult:
    """Run one claude -p turn. Returns the aggregated ClaudeResult.

    ``on_progress`` (if provided) is awaited with each progress event:
      - ('tool_use', tool_name) — claude is about to call a tool
      - ('subagent', subagent_type) — Agent() spawned a sub-session
      - ('text_partial', accumulated_so_far) — periodic text update

    Caller posts these to Discord as a single edited "working..."
    message (don't spam new posts).
    """
    # IMPORTANT: --mcp-config is variadic (--mcp-config <configs...>) and
    # absorbs every following non-flag arg until the next --flag. So the
    # user_message must NOT come immediately after it, or it gets read as
    # a second config path. Order matters: put --mcp-config first, then
    # --append-system-prompt (single-value flag) which acts as the
    # terminator, then the positional user_message at the very end.
    cmd = [
        *cfg.command,
        cfg.mcp_config_flag,
        cfg.mcp_config_path,
        cfg.system_prompt_flag,
        system_prompt,
        user_message,
    ]
    # DEBUG: dump the full prompt for inspection. Last turn wins;
    # overwritten on each invocation. Remove this block once we've
    # confirmed the preamble is shaped as expected.
    try:
        from datetime import UTC, datetime
        from pathlib import Path

        dump_path = Path("/Users/hermes/claudebot/last-turn-prompt.txt")
        dump_path.write_text(
            f"=== timestamp: {datetime.now(tz=UTC).isoformat()} ===\n"
            f"=== conv_slug: {conv_slug} ===\n"
            f"=== cmd argv (system_prompt + user_message redacted): "
            f"{[a if i not in (cmd.index(cfg.system_prompt_flag) + 1, len(cmd) - 1) else f'<{len(a)} chars>' for i, a in enumerate(cmd)]} ===\n\n"
            f"=== SYSTEM PROMPT ({len(system_prompt)} chars) ===\n"
            f"{system_prompt}\n\n"
            f"=== USER MESSAGE ({len(user_message)} chars) ===\n"
            f"{user_message}\n",
            encoding="utf-8",
        )
    except Exception:
        log.exception("debug prompt dump failed; continuing")
    env = dict(os.environ)
    env.update(cfg.env)
    # Bootstrap the long-lived OAuth token from ~/.claude_oauth_token so the
    # launchd-spawned claude -p never falls back to the short-lived keychain
    # credentials (~/.claude/.credentials.json), which lapse in ~a day and
    # make every turn reply "Failed to authenticate." See asa_bot.oauth.
    ensure_oauth_token(env)
    # Hooks read this to attach captures to the right conv ref.
    env["ASA_CONV_SLUG"] = conv_slug

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cfg.cwd,
        env=env,
    )

    result = ClaudeResult()
    try:
        await asyncio.wait_for(
            _consume(proc, result, on_progress),
            timeout=cfg.turn_timeout_seconds,
        )
    except TimeoutError:
        log.error("claude turn timed out after %ds", cfg.turn_timeout_seconds)
        proc.kill()
        await proc.wait()
        result.error = f"turn exceeded {cfg.turn_timeout_seconds}s timeout"
        return result

    rc = await proc.wait()
    if rc != 0 and not result.text:
        stderr = (
            (await proc.stderr.read()).decode("utf-8", errors="replace")
            if proc.stderr
            else ""
        )
        result.error = f"claude exited {rc}: {stderr[:500]}"
    return result


async def _consume(
    proc: asyncio.subprocess.Process,
    result: ClaudeResult,
    on_progress: Callable[..., Any] | None,
) -> None:
    if proc.stdout is None:
        return
    async for evt in _read_stream_json(proc.stdout):
        await _handle_event(evt, result, on_progress)


async def _read_stream_json(
    stream: asyncio.StreamReader,
) -> AsyncIterator[dict[str, Any]]:
    while True:
        line = await stream.readline()
        if not line:
            return
        try:
            yield json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            log.debug("claude stream non-JSON line: %r", line[:200])
            continue


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

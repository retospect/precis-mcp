"""Run Ask chat turns in the web process and persist their transcripts."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import signal
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from secrets import token_urlsafe
from typing import TYPE_CHECKING, Any

from precis import agentlog
from precis.chat.context import WEB_CHAT, build
from precis.handlers.conversation import ConversationHandler
from precis.utils.llm.router import (
    LlmRequest,
    LlmResult,
    Rung,
    Tier,
    Transport,
    dispatch_async,
)

if TYPE_CHECKING:
    from precis.runtime import PrecisRuntime
    from precis.store import Store

log = logging.getLogger(__name__)

#: Env var naming the rendered MCP config the chat's ``claude -p`` loads.
MCP_CONFIG_ENV = "PRECIS_CHAT_MCP_CONFIG"


def _chat_api_key() -> str | None:
    from precis.secrets import get_secret

    try:
        return get_secret("ANTHROPIC_API_KEY")
    except UnicodeDecodeError:
        # A malformed fallback secret file must not prevent web startup.
        return None


def preflight() -> list[str]:
    """One line per missing piece of a chat turn; empty when all present."""
    problems: list[str] = []
    if shutil.which("claude") is None:
        problems.append("the `claude` binary is not on PATH")
    cfg = os.environ.get(MCP_CONFIG_ENV, "")
    if not cfg:
        problems.append(f"{MCP_CONFIG_ENV} is not set (no chat MCP config deployed)")
    elif not os.path.isfile(cfg):
        problems.append(f"{MCP_CONFIG_ENV} names {cfg}, which does not exist")
    if not _chat_api_key():
        problems.append(_KEY_ERROR)
    return problems


def log_preflight() -> None:
    """Log each :func:`preflight` line (warning), or one info line if clean."""
    lines = preflight()
    for line in lines:
        log.warning("ask chat preflight: %s", line)
    if not lines:
        log.info("ask chat preflight: ok")


@dataclass
class ChatTurn:
    token: str
    slug: str
    lane: str
    owner_abbrev: str
    model: str
    max_usd: float
    conv_id: int
    status: str = "running"
    text: str = ""
    error: str | None = None
    terminal_subtype: str | None = None
    cost_usd: float | None = None
    turns_used: int | None = None
    tool_chips: list[dict[str, str]] = field(default_factory=list)
    desired_deletes: list[dict[str, Any]] = field(default_factory=list)
    agentlog_id: int | None = None
    group_id: int | None = None
    started: float = field(default_factory=time.monotonic)
    finished: float = 0.0
    stopping: bool = False
    task: asyncio.Task[None] | None = None

    def kill_group(self) -> None:
        if self.group_id is not None:
            with suppress(ProcessLookupError):
                os.killpg(self.group_id, signal.SIGKILL)
            self.group_id = None

    def on_process_group(self, pid: int) -> None:
        self.group_id = pid
        if self.stopping:
            self.kill_group()

    async def on_event(self, event: dict[str, Any]) -> None:
        match event.get("type"):
            case "assistant":
                if any(
                    event.get(key)
                    for key in ("parent_tool_use_id", "isSidechain", "is_sidechain")
                ):
                    return
                message = event.get("message")
                if not isinstance(message, dict):
                    message = {}
                content = message.get("content", event.get("content"))
                if isinstance(content, str):
                    self.text += content
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            self.text += block.get("text") or ""
                        elif block.get("type") == "tool_use":
                            name = block.get("name") or "?"
                            args = block.get("input") or {}
                            self.tool_chips.append(
                                {"name": name, "arguments": json.dumps(args)[:120]}
                            )
                            if name.rsplit("__", 1)[-1] == "delete":
                                self.desired_deletes.append(
                                    {
                                        key: args.get(key)
                                        for key in ("kind", "id", "reason")
                                    }
                                )
            case "result":
                self.terminal_subtype = event.get("subtype") or event.get("stop_reason")
                final_text = event.get("result")
                if not self.text and isinstance(final_text, str):
                    self.text = final_text
                cost = event.get("total_cost_usd")
                if isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0:
                    self.cost_usd = cost
                turns = event.get("num_turns")
                if isinstance(turns, int):
                    self.turns_used = turns
            case "error":
                self.error = str(event.get("message") or event)


@dataclass(frozen=True)
class _ContextTools:
    runtime: PrecisRuntime

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        body, error = await asyncio.to_thread(
            self.runtime.dispatch_with_status, name, arguments, fold=False
        )
        return "" if error else body


_TURNS: dict[str, ChatTurn] = {}
_MAX_ACTIVE = 3
_TTL_S = 3600.0
_SOURCE = "web:chat"
_KEY_ERROR = "ANTHROPIC_API_KEY is not resolvable; store it on the /secrets page"


def get_turn(token: str, user_abbrev: str) -> ChatTurn | None:
    """Resolve a progress entry for its authenticated owner."""
    turn = _TURNS.get(token)
    return turn if turn is not None and turn.owner_abbrev == user_abbrev else None


def _cap(env: str, default: str) -> float:
    cap = float(os.environ.get(env, default))
    if not math.isfinite(cap) or cap <= 0:
        raise ValueError(f"{env} must be a positive finite dollar amount")
    return cap


def start_turn(
    sid: str, lane: str, user_abbrev: str, text: str, *, runtime: PrecisRuntime
) -> str:
    """Append the human message and schedule one turn on the web event loop.

    Call with the authenticated user's abbreviation. Admission and the human
    append share the loop with completion, so a second start cannot pass the
    lane or spend check while the first is still being registered.
    """
    loop = asyncio.get_running_loop()
    if lane != "a" or not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
        raise ValueError("invalid chat session or lane")
    if not user_abbrev or not text.strip():
        raise ValueError("the chat needs an authenticated user and a message")
    store = runtime.store
    if store is None:
        raise RuntimeError("Ask chat requires a store")
    slug = f"chat/{sid}/{lane}"
    now = time.monotonic()
    for token, turn in list(_TURNS.items()):
        if turn.finished and now - turn.finished > _TTL_S:
            del _TURNS[token]

    ref = store.get_ref(kind="conv", id=slug)
    if ref is not None and ref.meta.get("owner_abbrev") != user_abbrev:
        raise PermissionError("chat belongs to another owner")
    active = [turn for turn in _TURNS.values() if turn.status == "running"]
    if any(turn.slug == slug for turn in active):
        raise RuntimeError("a turn is already running in this lane")
    if len(active) >= _MAX_ACTIVE:
        raise RuntimeError("three chat turns are already running")
    if ref is not None:
        today = datetime.now(UTC).date()
        spent = math.fsum(
            chunk.meta.get("cost_usd") or 0
            for chunk in store.chunks.list_chunks_for_ref(ref.id)
            if chunk.meta.get("author") == "asa"
            and chunk.created_at.astimezone(UTC).date() == today
        )
        if spent >= _cap("PRECIS_CHAT_DAY_MAX_USD", "30"):
            raise RuntimeError("day cap reached")

    model = os.environ.get("PRECIS_CHAT_MODEL") or "opus"
    max_usd = _cap("PRECIS_CHAT_MAX_USD", "10")
    title = text[:80]
    appended = ConversationHandler(hub=runtime.hub).append_turn(
        slug=slug,
        body=text,
        author=user_abbrev,
        title=title,
        meta={"lane": lane},
        ref_meta={
            "chat_session": sid,
            "chat_lane": lane,
            "model": model,
            "max_usd": max_usd,
            "owner_abbrev": user_abbrev,
            "title": title,
        },
    )
    token = token_urlsafe(24)
    turn = ChatTurn(token, slug, lane, user_abbrev, model, max_usd, appended.ref.id)
    _TURNS[token] = turn
    turn.task = loop.create_task(_run(turn, text, runtime, store), name=f"chat:{slug}")
    return token


async def build_request(
    turn: ChatTurn, text: str, runtime: PrecisRuntime
) -> LlmRequest:
    """Build a neutral streaming request pinned to metered API-key auth."""
    if not _chat_api_key():
        raise RuntimeError(_KEY_ERROR)
    cfg = os.environ.get(MCP_CONFIG_ENV)
    if not cfg or not os.path.isfile(cfg):
        raise RuntimeError(f"{MCP_CONFIG_ENV} must name the deployed chat MCP config")
    context = await build(
        precis=_ContextTools(runtime),
        profile=WEB_CHAT,
        conv_slug=turn.slug,
        guild_name="Web",
        channel_name="Ask",
        thread_name=None,
        author_handle=turn.owner_abbrev,
    )
    return LlmRequest(
        tier=Tier.FRONTIER,
        prompt=text,
        model=turn.model,
        tools_needed=True,
        max_usd=turn.max_usd,
        source=_SOURCE,
        work_class="interactive",
        system_prompt=(
            context
            + "\nYou cannot delete. When something should be deleted, call delete "
            "with a reason; the call is refused and listed for Reto."
        ),
        mcp_config=cfg,
        max_turns=40,
        output_format="stream-json",
        extra_args=("--tools", ""),
        env_overlay={
            "PRECIS_ACTING_USER": turn.owner_abbrev,
            "PRECIS_MCP_DENY_VERBS": "delete",
            **agentlog.run_env(turn.agentlog_id),
        },
        on_event=turn.on_event,
        stream_rung=Rung(Transport.CLAUDE_AGENT, bare=True),
        on_process_group=turn.on_process_group,
    )


async def _run(turn: ChatTurn, text: str, runtime: PrecisRuntime, store: Store) -> None:
    result = None
    try:
        if not turn.stopping:
            turn.agentlog_id = agentlog.open_run(
                store,
                source=_SOURCE,
                title=turn.slug,
                model=turn.model,
                parent_ref_id=turn.conv_id,
                prompt=text,
            )
            request = await build_request(turn, text, runtime)
            if not turn.stopping:
                result = await dispatch_async(request)
    except Exception as exc:
        log.exception("Ask chat turn failed")
        turn.error = str(exc) or type(exc).__name__
    finally:
        turn.group_id = None
        _finish(turn, runtime, store, result)


def _finish(
    turn: ChatTurn, runtime: PrecisRuntime, store: Store, result: LlmResult | None
) -> None:
    subtype = turn.terminal_subtype or (result.terminal_reason if result else None)
    turn.terminal_subtype = subtype
    if turn.stopping:
        status = "stopped"
        turn.error = None
    elif subtype == "error_max_budget_usd":
        status = "capped"
        turn.error = None
    elif result is not None and result.paused:
        status = "paused"
        turn.error = result.error or "Model dispatch paused"
    elif (
        turn.error
        or result is None
        or result.error
        or (subtype and subtype.startswith("error"))
    ):
        status = "error"
        turn.error = (
            turn.error or (result.error if result else None) or subtype or "Turn failed"
        )
    else:
        status = "answer"

    # The stream can report spend even when the CLI exits nonzero and the
    # router's error result has no metering.
    if result is not None:
        if result.cost_usd is not None:
            turn.cost_usd = result.cost_usd
        if result.turns_used is not None:
            turn.turns_used = result.turns_used
    if status == "answer" or not turn.text:
        turn.text = (result.text if result else "") or turn.text
    body = turn.text
    if turn.error:
        body = f"{body}\n\n{turn.error}".strip()
    if not body:
        body = {"stopped": "Turn stopped.", "capped": "Turn budget reached."}.get(
            status, "The model returned no text."
        )

    try:
        ConversationHandler(hub=runtime.hub).append_turn(
            slug=turn.slug,
            body=body,
            author="asa",
            msg_id=turn.token,
            meta={
                "lane": turn.lane,
                "status": status,
                "cost_usd": turn.cost_usd,
                "duration_s": (
                    result.duration_s
                    if result is not None and result.duration_s is not None
                    else time.monotonic() - turn.started
                ),
                "turns": turn.turns_used,
                "tool_calls": len(turn.tool_chips),
                "agentlog_id": turn.agentlog_id,
                "desired_deletes": turn.desired_deletes,
            },
        )
    except Exception:
        log.exception("Ask chat answer append failed")
        status = "error"
        turn.error = "Could not save the chat answer."
    finally:
        turn.finished = time.monotonic()
        turn.status = status
        agentlog.close_run(store, turn.agentlog_id, status=status, res=result)


def stop_turn(token: str) -> None:
    """Stop a turn by token. Browser routes must verify ownership first."""
    turn = _TURNS.get(token)
    if turn is not None and turn.status == "running":
        turn.stopping = True
        turn.kill_group()


async def shutdown() -> None:
    """Stop active CLI groups and persist their partial turns before DB close."""
    active = [turn for turn in _TURNS.values() if turn.status == "running"]
    for turn in active:
        stop_turn(turn.token)
    await asyncio.gather(*(turn.task for turn in active if turn.task is not None))

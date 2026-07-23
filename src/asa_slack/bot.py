"""Slack bridge — receives messages, drives Asa per turn via the ADR-0046
router, posts back.

Single-instance daemon over Socket Mode (a persistent outbound WebSocket —
no public endpoint needed, same always-on-daemon shape as the Discord
bridge). Every conversation is a thread from message 1: asa never posts to
a channel root, always ``thread_ts = incoming.thread_ts or incoming.ts``.

Unlike the Discord bridge (which hand-rolls its own streaming ``claude -p``
subprocess), asa-slack calls ``precis.utils.llm.router.dispatch()`` forced
to ``Tier.CLOUD_MID`` (sonnet) with a hard kind-allowlist
(``asa_slack.kind_policy``) baked in via ``env_overlay`` — Slack is a
semi-trusted, multi-user surface, and this is the router's governance
(budget breaker, route-log, per-turn cost/turn caps) for free.

Flow per inbound message (human or bot, any channel the app is in):

  1. resolve sender identity (Slack ``users.info`` / ``bots.info``, cached)
  2. capture the observed turn -> precis put(kind='conv', ...) — every
     message asa sees is captured, not just the ones that trigger a reply
  3. post an in-thread "thinking..." placeholder
  4. build the system prompt (SOUL + Slack hints + who's talking, via
     ``asa_bot.preamble.build`` — its per-user ``memory`` note mechanism
     works unchanged, keyed on the identity's author_handle)
  5. router.dispatch(), off the event loop thread (asyncio.to_thread) —
     a single blocking call, same reliability class as Discord's own
     await-a-subprocess live-turn path (lost only on an asa-slack process
     crash mid-turn — an accepted v1 trade-off)
  6. edit the placeholder with the final reply (split if long)
  7. capture asa's own reply as a conv turn too
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.app.async_app import AsyncApp
from slack_sdk.errors import SlackApiError

from asa_bot import preamble
from asa_bot.precis_client import PrecisClient, health_loop
from asa_slack import identity as identity_check
from asa_slack.config import Config, load_slack_tokens
from asa_slack.conv_slug import compute_slug
from asa_slack.kind_policy import slack_kinds_disabled
from precis.utils.llm.router import LlmRequest, Tier, dispatch
from precis.utils.msgsplit import split_message

log = logging.getLogger(__name__)

# Message-event subtypes that aren't a chat turn — edits, deletes, channel
# housekeeping. Anything else (plain messages, and bot-posted messages,
# which carry no subtype or "bot_message") is treated as a turn.
_IGNORED_SUBTYPES = frozenset(
    {
        "message_changed",
        "message_deleted",
        "message_replied",
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "bot_add",
        "bot_remove",
        "thread_broadcast",
    }
)


@dataclasses.dataclass(frozen=True, slots=True)
class Identity:
    """A resolved Slack sender — human or bot."""

    slack_id: str
    real_name: str
    is_bot: bool

    @property
    def mention(self) -> str:
        return f"<@{self.slack_id}>"

    @property
    def author_handle(self) -> str:
        """The ``conv`` author + the per-person ``memory`` tag key
        (``asa_bot.preamble``'s ``user:<handle>`` mechanism, unchanged).

        Includes the Slack mention form so asa can @-mention this person
        or bot directly in a reply, and an explicit human/bot flag so
        identity is never ambiguous — the workspace has a few other bots
        (Rocky, Bullwinkle, Natasha, ...) alongside real people.
        """
        kind = "bot" if self.is_bot else "human"
        return f"{self.real_name} ({self.mention}, {kind})"


class AsaSlack:
    def __init__(self, *, cfg: Config, precis: PrecisClient, app: AsyncApp) -> None:
        self._cfg = cfg
        self._precis = precis
        self._app = app
        self._soul = _read_or_empty(cfg.soul_path)
        self._slack_hints = _read_or_empty(cfg.slack_hints_path)
        self._identity_cache: dict[str, Identity] = {}
        self._channel_name_cache: dict[str, str] = {}
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._team_id: str = ""
        self._team_name: str = ""
        self._own_user_id: str = ""
        self._own_bot_id: str = ""
        app.event("message")(self._on_message)

    async def start(self) -> None:
        auth = await self._app.client.auth_test()
        self._team_id = str(auth.get("team_id") or "")
        self._team_name = str(auth.get("team") or "Slack")
        self._own_bot_id = str(auth.get("bot_id") or "")
        self._own_user_id = await identity_check.check_identity(
            self._app.client,
            expected_bot_user_id=self._cfg.slack.expected_bot_user_id,
        )

    async def _on_message(self, event: dict[str, Any], client: Any) -> None:
        try:
            await self._handle_message(event, client)
        except Exception:
            log.exception("asa-slack: turn failed for event ts=%r", event.get("ts"))

    async def _handle_message(self, event: dict[str, Any], client: Any) -> None:
        if event.get("subtype") in _IGNORED_SUBTYPES:
            return
        text = (event.get("text") or "").strip()
        if not text:
            return
        channel_id = event.get("channel")
        ts = event.get("ts")
        if not channel_id or not ts:
            return
        bot_id = event.get("bot_id")
        user_id = event.get("user")
        # Self-loop guard only — every other sender (human or bot: Rocky,
        # Bullwinkle, Natasha, ...) is a valid interlocutor and gets a reply.
        if bot_id and bot_id == self._own_bot_id:
            return
        if user_id and user_id == self._own_user_id:
            return

        allowed = self._cfg.slack.allowed_channels
        if allowed and channel_id not in allowed:
            return

        thread_ts = event.get("thread_ts") or ts
        who = await self._resolve_identity(client, user_id=user_id, bot_id=bot_id)
        slug = compute_slug(
            team_id=self._team_id, channel_id=channel_id, thread_ts=thread_ts
        )

        # Serialize turns within one thread (cross-thread runs concurrently).
        lock = self._thread_locks.setdefault(slug, asyncio.Lock())
        async with lock:
            await self._handle_one(
                client=client,
                slug=slug,
                channel_id=channel_id,
                thread_ts=thread_ts,
                ts=ts,
                text=text,
                who=who,
            )

    async def _resolve_identity(
        self, client: Any, *, user_id: str | None, bot_id: str | None
    ) -> Identity:
        key = user_id or bot_id or "unknown"
        cached = self._identity_cache.get(key)
        if cached is not None:
            return cached
        real_name = key
        is_bot = bool(bot_id) and not user_id
        if user_id:
            try:
                info = (await client.users_info(user=user_id))["user"]
                real_name = info.get("real_name") or info.get("name") or user_id
                is_bot = bool(info.get("is_bot"))
            except SlackApiError:
                log.warning("asa-slack: users_info failed for %s", user_id)
        elif bot_id:
            try:
                info = (await client.bots_info(bot=bot_id))["bot"]
                real_name = info.get("name") or bot_id
            except SlackApiError:
                log.warning("asa-slack: bots_info failed for %s", bot_id)
        # Some of this workspace's other assistants may not set Slack's own
        # is_bot flag — a literal "[bot]" marker in the resolved name is an
        # OR'd fallback signal.
        if "[bot]" in real_name.lower():
            is_bot = True
        who = Identity(slack_id=key, real_name=real_name, is_bot=is_bot)
        self._identity_cache[key] = who
        return who

    async def _resolve_channel_name(self, client: Any, channel_id: str) -> str:
        cached = self._channel_name_cache.get(channel_id)
        if cached is not None:
            return cached
        name = channel_id
        try:
            info = (await client.conversations_info(channel=channel_id))["channel"]
            if info.get("is_im"):
                name = "DM"
            else:
                name = info.get("name") or channel_id
        except SlackApiError:
            log.warning("asa-slack: conversations_info failed for %s", channel_id)
        self._channel_name_cache[channel_id] = name
        return name

    async def _handle_one(
        self,
        *,
        client: Any,
        slug: str,
        channel_id: str,
        thread_ts: str,
        ts: str,
        text: str,
        who: Identity,
    ) -> None:
        # 1. Capture the observed turn unconditionally — every message asa
        # sees (human or bot) lands in the transcript, whether or not it's
        # the one triggering this reply.
        await self._capture(
            slug=slug,
            channel_id=channel_id,
            thread_ts=thread_ts,
            text=text,
            author=who.author_handle,
            msg_id=ts,
            slack_id=who.slack_id,
            is_bot=who.is_bot,
        )

        # 2. Placeholder — always in-thread, never the channel root.
        try:
            placeholder = await client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts, text="_thinking…_"
            )
        except SlackApiError:
            log.exception("asa-slack: placeholder post failed (slug=%s)", slug)
            return

        # 3. System prompt.
        channel_name = await self._resolve_channel_name(client, channel_id)
        system_prompt = await self._build_system_prompt(
            slug=slug, channel_name=channel_name, who=who
        )

        # 4. Router dispatch, off the event loop thread — a single blocking
        # call (no streaming progress ticker; see the plan doc trade-off).
        req = LlmRequest(
            tier=Tier.CLOUD_MID,
            tools_needed=True,
            prompt=text,
            system_prompt=system_prompt,
            mcp_config=self._cfg.mcp_config_path,
            max_turns=self._cfg.router.max_turns,
            max_usd=self._cfg.router.max_usd,
            timeout_s=self._cfg.router.timeout_s,
            env_overlay={"PRECIS_KINDS_DISABLED": slack_kinds_disabled()},
            source="asa-slack",
        )
        result = await asyncio.to_thread(dispatch, req)

        # 5. Reply.
        body = (result.text or "").strip()
        if result.error:
            body = f"(sorry — something went wrong: {result.error[:300]})"
        if not body:
            body = "(no reply)"
        chunks = split_message(body, limit=self._cfg.slack.max_message_chars) or [body]
        try:
            await client.chat_update(
                channel=channel_id, ts=placeholder["ts"], text=chunks[0]
            )
        except SlackApiError:
            log.exception(
                "asa-slack: placeholder edit failed, posting fresh (slug=%s)", slug
            )
            await client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts, text=chunks[0]
            )
        for chunk in chunks[1:]:
            try:
                await client.chat_postMessage(
                    channel=channel_id, thread_ts=thread_ts, text=chunk
                )
            except SlackApiError:
                log.exception("asa-slack: reply chunk post failed (slug=%s)", slug)

        # 6. Capture asa's own reply too.
        if not result.error:
            await self._capture(
                slug=slug,
                channel_id=channel_id,
                thread_ts=thread_ts,
                text=body,
                author="asa",
                msg_id=f"asa-slack:{placeholder['ts']}",
                slack_id=self._own_user_id,
                is_bot=True,
            )

    async def _capture(
        self,
        *,
        slug: str,
        channel_id: str,
        thread_ts: str,
        text: str,
        author: str,
        msg_id: str,
        slack_id: str,
        is_bot: bool,
    ) -> None:
        try:
            await self._precis.call_tool(
                "put",
                {
                    "kind": "conv",
                    "id": slug,
                    "text": text,
                    "author": author,
                    "msg_id": msg_id,
                    "meta": {
                        "ts": datetime.now(tz=UTC).isoformat(),
                        "platform": "slack",
                        "team_id": self._team_id,
                        "channel_id": channel_id,
                        "thread_ts": thread_ts,
                        "slack_user_id": slack_id,
                        "is_bot": is_bot,
                    },
                    "ref_meta": {
                        "platform": "slack",
                        "team_id": self._team_id,
                        "channel_id": channel_id,
                        "thread_ts": thread_ts,
                    },
                },
            )
        except Exception:
            log.exception("asa-slack: conv capture failed (slug=%s); continuing", slug)

    async def _build_system_prompt(
        self, *, slug: str, channel_name: str, who: Identity
    ) -> str:
        pre = await preamble.build(
            precis=self._precis,
            cfg=self._cfg.preamble,
            conv_slug=slug,
            guild_name=self._team_name,
            channel_name=channel_name,
            thread_name=None,  # Slack threads have no separate name
            author_handle=who.author_handle,
            soul=self._soul,
            tool_hints="",  # ignored by preamble.build; SOUL is the source of truth
            platform="Slack",
        )
        if self._slack_hints.strip():
            pre = pre.rstrip() + "\n\n" + self._slack_hints.strip() + "\n"
        return pre


def _read_or_empty(path: str) -> str:
    p = Path(path)
    if not p.exists():
        log.warning("file not found, using empty: %s", path)
        return ""
    return p.read_text(encoding="utf-8")


async def run(cfg: Config) -> None:
    """Bring up the bridge end-to-end. Blocks until Socket Mode disconnects."""
    bot_token, app_token = load_slack_tokens(cfg.slack)
    app = AsyncApp(token=bot_token)
    precis = PrecisClient(cfg.precis)
    await precis.start()
    health_task = asyncio.create_task(
        health_loop(precis, cfg.precis.health_check_interval_seconds)
    )
    try:
        asa = AsaSlack(cfg=cfg, precis=precis, app=app)
        await asa.start()
        handler = AsyncSocketModeHandler(app, app_token)
        await handler.start_async()
    finally:
        health_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await health_task
        await precis.stop()

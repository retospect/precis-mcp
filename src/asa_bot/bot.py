"""Discord bot — receives messages, drives claude per turn, posts back.

Single-instance daemon. Per-thread serial queue keeps conv-order
sane; cross-thread concurrency is parallel.

Flow per inbound user message:

  1. compute conv_slug from Discord ctx
  2. capture user msg → precis put(kind='conv', author=<user>, msg_id=<discord>)
  3. build preamble from precis (4 parallel calls)
  4. invoke claude -p with SOUL + preamble + msg
  5. stream stdout, post progress to Discord
  6. on completion: post final text (split for 2K limit, attach if huge)
  7. assistant capture happens via Claude Code's Stop hook → HTTP shim

NOTIFY handlers:
  - precis.cron: fetch the cron's payload, synthesize a user msg,
    invoke Asa with that as the prompt, post the result.
  - precis.messages: fetch the message ref, post text + attachments
    to the target, stamp meta.status='sent'.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import discord

from asa_bot import preamble, slash
from asa_bot.capture_shim import CaptureShim
from asa_bot.claude_invoke import invoke as claude_invoke
from asa_bot.config import Config, LLMConfig, load_discord_token
from asa_bot.conv_slug import compute_slug
from asa_bot.pg_listen import PgListener
from asa_bot.precis_client import PrecisClient, health_loop

log = logging.getLogger(__name__)


class AsaBot(discord.Client):
    def __init__(
        self,
        *,
        cfg: Config,
        precis: PrecisClient,
        capture: CaptureShim,
        listener: PgListener,
    ) -> None:
        discord.VoiceClient.warn_nacl = False
        discord.VoiceClient.warn_dave = False
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guild_messages = True
        intents.dm_messages = True
        intents.guilds = True
        super().__init__(intents=intents)
        self._cfg = cfg
        self._precis = precis
        self._capture = capture
        self._listener = listener
        self._queues: dict[str, asyncio.Queue[discord.Message]] = {}
        self._workers: dict[str, asyncio.Task[Any]] = {}
        self._soul: str = ""
        self._tool_hints: str = ""
        self._runtime = slash.Runtime()

    async def setup_hook(self) -> None:
        self._soul = _read_or_empty(self._cfg.preamble.soul_path)
        self._tool_hints = _read_or_empty(self._cfg.preamble.tool_hints_path)
        asyncio.create_task(self._consume_cron())
        asyncio.create_task(self._consume_messages())

    async def on_ready(self) -> None:
        log.info("asa-bot connected as %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        # Log every inbound so a silent drop is visible.
        log.info(
            "on_message: from=%s ch=%s thr=%s len=%d content=%r",
            message.author,
            message.channel.id,
            getattr(message.channel, "parent_id", None),
            len(message.content or ""),
            (message.content or "")[:80],
        )
        if message.author == self.user or message.author.bot:
            log.info("on_message: dropped (author is bot/self)")
            return
        if not (message.content or "").strip():
            # System messages (thread-created, pin-added, etc.) and
            # attachment-only messages without body text would trigger
            # a turn with an empty prompt → claude exits with "Input
            # must be provided through stdin or as a prompt argument".
            # Skip them.
            log.info(
                "on_message: dropped (empty content — system message or attachment-only)"
            )
            return
        if (
            self._cfg.discord.allowed_channels
            and str(message.channel.id) not in self._cfg.discord.allowed_channels
        ):
            log.info("on_message: dropped (channel not in allowed_channels)")
            return
        # Slash commands — text-prefix style. Intercepted before the
        # claude path so they cost zero LLM tokens and don't capture
        # into the conv history. See ``asa_bot.slash`` for the registry.
        if (message.content or "").startswith("/"):
            await slash.dispatch(
                message=message,
                precis=self._precis,
                config=self._cfg,
                runtime=self._runtime,
                soul=self._soul,
                tool_hints=self._tool_hints,
                ctx_from_message=_ctx_from_message,
                reply_target=_reply_target,
            )
            return
        # Per-conv queue keeps the same thread serial; different threads
        # run in parallel. Lazy-create the worker per slug.
        slug = compute_slug(_ctx_from_message(message))
        q = self._queues.get(slug)
        if q is None:
            q = asyncio.Queue()
            self._queues[slug] = q
            self._workers[slug] = asyncio.create_task(self._worker(slug, q))
        await q.put(message)
        if q.qsize() > 0:
            # If there's a queue forming, give the user a "got it"
            # signal so they don't think we missed it.
            try:
                await message.add_reaction("🔄")
            except discord.HTTPException:
                pass

    async def _worker(self, slug: str, q: asyncio.Queue[discord.Message]) -> None:
        while True:
            message = await q.get()
            try:
                await self._handle_one(slug, message)
            except Exception:
                log.exception("turn failed (slug=%s)", slug)
            finally:
                q.task_done()

    async def _handle_one(self, slug: str, message: discord.Message) -> None:
        # Discord delivers TWO MESSAGE_CREATE events when a thread auto-
        # spawns from a channel message: one for the channel message
        # (which becomes the thread starter), one for the thread message
        # itself. Processing both gives two parallel turns; the channel-
        # side turn finishes first and the user perceives "my thread
        # question got answered in the channel." Drop the channel-side
        # turn when message.thread is set — the thread worker handles it.
        if message.thread is not None:
            log.info(
                "on_message: dropped (slug=%s; message has spawned thread %s — thread worker owns the reply)",
                slug,
                message.thread.id,
            )
            return

        # 1. Capture user turn.
        author_handle = (
            f"{message.author.name}#{message.author.discriminator}"
            if message.author.discriminator != "0"
            else message.author.name
        )
        try:
            await self._precis.call_tool(
                "put",
                {
                    "kind": "conv",
                    "id": slug,
                    "text": message.content,
                    "author": author_handle,
                    "msg_id": str(message.id),
                    "meta": {
                        "ts": message.created_at.astimezone(UTC).isoformat(),
                        "platform": "discord",
                        "channel_id": str(message.channel.id),
                        "guild_id": str(message.guild.id) if message.guild else "dm",
                    },
                    "ref_meta": {
                        "platform": "discord",
                        "guild_id": str(message.guild.id) if message.guild else "dm",
                        "channel_id": str(message.channel.id),
                        "thread_id": _thread_id_of(message),
                    },
                },
            )
        except Exception:
            log.exception("user-turn capture failed; proceeding anyway")

        # 2. Build preamble.
        guild_name = message.guild.name if message.guild else "DM"
        channel_obj = message.channel
        channel_name = getattr(channel_obj, "name", None) or "channel"
        thread_name = None
        if isinstance(channel_obj, discord.Thread):
            thread_name = channel_obj.name
        try:
            pre = await preamble.build(
                precis=self._precis,
                cfg=self._cfg.preamble,
                conv_slug=slug,
                guild_name=guild_name,
                channel_name=channel_name,
                thread_name=thread_name,
                author_handle=author_handle,
                soul=self._soul,
                tool_hints=self._tool_hints,
            )
        except Exception:
            log.exception("preamble build failed; using SOUL only")
            pre = self._soul

        # 3. Invoke claude -p.
        progress_msg = None
        # The progress message gets edited as claude streams. We hold:
        # last_label = what's currently shown in the message
        # last_text_edit_at = monotonic timestamp of the last text_partial
        #   edit; text_partial events are throttled (every ~5s) to stay
        #   within Discord's edit budget. tool_use / subagent events bypass
        #   the throttle since they're rare and informative.
        state = {"label": None, "last_text_edit_at": 0.0}

        async def on_progress(evt: tuple) -> None:
            nonlocal progress_msg
            kind = evt[0]
            # first_sentence is special: it's the user-facing ack
            # ("Looking at the cluster status now"), not a widget update.
            # Send it as its own message so it doesn't get edited away
            # when the working indicator advances. The final reply lands
            # separately later.
            if kind == "first_sentence":
                try:
                    await _reply_target(message).send(evt[1])
                except discord.HTTPException:
                    pass
                return
            label = _format_progress(evt)
            if label is None:
                return
            now = time.monotonic()
            if kind == "text_partial":
                if now - state["last_text_edit_at"] < 5.0:
                    return
                state["last_text_edit_at"] = now
            if label == state["label"]:
                return
            state["label"] = label
            if progress_msg is None:
                try:
                    progress_msg = await _reply_target(message).send(label)
                except discord.HTTPException:
                    pass
            else:
                try:
                    await progress_msg.edit(content=label)
                except discord.HTTPException:
                    pass

        result = await claude_invoke(
            _llm_with_override(self._cfg.llm, self._runtime.model_override),
            system_prompt=pre,
            user_message=message.content,
            conv_slug=slug,
            on_progress=on_progress,
        )

        # 4. Post reply (replace progress msg with final).
        # The first sentence was already streamed as a standalone ack
        # message; the final reply text still opens with that same
        # sentence, so strip the duplicate leading span before posting
        # (gripe #48766). If the whole reply *was* that one sentence,
        # the ack already delivered it — just clear the progress widget.
        body = _strip_leading_ack(result.text.strip(), result.first_sentence)
        if not body:
            if result.first_sentence_emitted and not result.error:
                if progress_msg is not None:
                    try:
                        await progress_msg.delete()
                    except discord.HTTPException:
                        pass
                return
            body = f"(no reply — {result.error or 'empty response'})"
        await self._post_reply(message, progress_msg, body, result)

    async def _post_reply(
        self,
        original: discord.Message,
        progress_msg: discord.Message | None,
        body: str,
        result: Any,
    ) -> None:
        chunks = _split_for_discord(body, self._cfg.discord.max_message_chars)
        # If the body is large, upload as attachment so we don't spam.
        if len(body) >= self._cfg.discord.attachment_threshold_chars:
            buf = body.encode("utf-8")
            file = discord.File(fp=io.BytesIO(buf), filename="asa-reply.md")
            preface = chunks[0][:1800] + (
                "\n\n*(full reply attached)*" if len(body) > 1800 else ""
            )
            if progress_msg is not None:
                try:
                    await progress_msg.delete()
                except discord.HTTPException:
                    pass
            await _reply_target(original).send(content=preface, file=file)
            return
        target = _reply_target(original)
        # Edit the progress message with the first chunk; send the rest.
        if progress_msg is not None:
            try:
                await progress_msg.edit(content=chunks[0])
            except discord.HTTPException:
                await target.send(content=chunks[0])
        else:
            await target.send(content=chunks[0])
        for c in chunks[1:]:
            await target.send(content=c)

    async def _consume_cron(self) -> None:
        while True:
            data = await self._listener.cron_queue.get()
            try:
                await self._handle_cron(data)
            except Exception:
                log.exception("cron handler failed")

    async def _handle_cron(self, data: dict[str, Any]) -> None:
        target = data.get("target")
        payload = data.get("payload") or ""
        if not target or not payload:
            log.warning("cron notify missing target/payload: %r", data)
            return
        slug = target.replace("conv:", "", 1) if target.startswith("conv:") else target
        # Synthesise a Discord-shaped invocation: post the payload to
        # the channel as if from a cron-runner user, then drive Asa
        # against the resulting context.
        await self._deliver_cron_prompt(slug, payload, cron_id=data.get("cron_id"))

    async def _deliver_cron_prompt(
        self, conv_slug: str, payload: str, *, cron_id: Any
    ) -> None:
        """Drive Asa as if the user just said ``payload`` in this conv.

        We don't post the synthetic prompt to Discord (it would be
        confusing). Instead we capture it as a 'cron-runner' authored
        turn, build the preamble, run claude, and post the response
        as a standalone Discord message in the channel.
        """
        msg_id = f"cron:{cron_id}:{datetime.now(tz=UTC).timestamp()}"
        try:
            await self._precis.call_tool(
                "put",
                {
                    "kind": "conv",
                    "id": conv_slug,
                    "text": payload,
                    "author": "cron-runner",
                    "msg_id": msg_id,
                },
            )
        except Exception:
            log.exception("cron synth capture failed; continuing")

        try:
            pre = await preamble.build(
                precis=self._precis,
                cfg=self._cfg.preamble,
                conv_slug=conv_slug,
                guild_name="(cron)",
                channel_name="(cron)",
                thread_name=None,
                author_handle="cron-runner",
                soul=self._soul,
                tool_hints=self._tool_hints,
            )
        except Exception:
            log.exception("cron preamble failed; using SOUL only")
            pre = self._soul

        result = await claude_invoke(
            self._cfg.llm,
            system_prompt=pre,
            user_message=payload,
            conv_slug=conv_slug,
            on_progress=None,
        )

        # Find the Discord channel from the slug and post the result.
        channel = await self._channel_from_slug(conv_slug)
        if channel is None:
            log.warning("cron: no Discord channel resolved for slug %r", conv_slug)
            return
        body = result.text.strip() or "(cron produced no response)"
        for c in _split_for_discord(body, self._cfg.discord.max_message_chars):
            await channel.send(content=c)

    async def _consume_messages(self) -> None:
        while True:
            data = await self._listener.message_queue.get()
            try:
                await self._handle_outbound(data)
            except Exception:
                log.exception("outbound message handler failed")

    async def _handle_outbound(self, data: dict[str, Any]) -> None:
        ref_id = data.get("ref_id")
        target = data.get("target")
        if not ref_id or not target:
            log.warning("messages notify missing ref_id/target: %r", data)
            return
        # Read the full body from the message_body chunk directly — the
        # kind='message' rendering only carries the title (≤200 chars), so a
        # long message (e.g. a morning briefing) would otherwise post
        # truncated. Mirrors how cli/cron.py reads the cron_payload chunk.
        text = await self._fetch_message_body(ref_id)
        if not text:
            # Fallback for older messages / a fetch failure: the rendering's
            # title block, via the precis MCP.
            body = await self._precis.call_tool(
                "get", {"kind": "message", "id": ref_id}
            )
            text = _extract_message_body(body)
        if not text:
            log.warning("message %s: empty body, skipping", ref_id)
            return
        channel = await self._channel_from_slug(target)
        if channel is None:
            log.warning("message %s: no channel for target %r", ref_id, target)
            return
        for c in _split_for_discord(text, self._cfg.discord.max_message_chars):
            await channel.send(content=c)

        # Capture the posted body as an assistant conv turn so follow-up
        # questions see it in the thread history — else the bot "denies"
        # having sent its own proactive briefing (gripe #47321). Mirrors
        # the cron capture in _deliver_cron_prompt. Best-effort: a capture
        # failure must never break delivery (which already succeeded).
        slug = target.replace("conv:", "", 1) if target.startswith("conv:") else target
        if slug.startswith("discord/"):
            author = data.get("author") or "asa"
            msg_id = f"message:{ref_id}"
            try:
                await self._precis.call_tool(
                    "put",
                    {
                        "kind": "conv",
                        "id": slug,
                        "text": text,
                        "author": author,
                        "msg_id": msg_id,
                        "meta": {"proactive": True},
                    },
                )
            except Exception:
                log.exception("message %s: conv capture failed; delivered", ref_id)

    async def _fetch_message_body(self, ref_id: Any) -> str:
        """Full text of a message, from its ``message_body`` chunk(s).

        Queried directly (the get rendering only carries the title). Uses
        the pooled query DSN — a plain SELECT is fine through pgbouncer
        (only LISTEN needs the direct session). Best-effort: returns "" on
        any failure so the caller falls back to the rendering."""
        import psycopg

        dsn = self._cfg.precis.database_url
        if not dsn:
            return ""
        try:
            async with await psycopg.AsyncConnection.connect(
                dsn, autocommit=True
            ) as conn:
                cur = await conn.execute(
                    "SELECT text FROM chunks WHERE ref_id = %s "
                    "AND chunk_kind = 'message_body' ORDER BY ord",
                    (int(ref_id),),
                )
                rows = await cur.fetchall()
            return "\n\n".join(r[0] for r in rows if r and r[0]).strip()
        except Exception:
            log.exception("message %s: direct body fetch failed", ref_id)
            return ""

    async def _channel_from_slug(self, slug: str) -> Any:
        """Resolve a conv slug to a discord channel / thread object."""
        # Accept both `discord/G/C/T` and `conv:discord/G/C/T`.
        s = slug.replace("conv:", "", 1)
        parts = s.split("/")
        if len(parts) < 2 or parts[0] != "discord":
            return None
        if parts[1] == "dm" and len(parts) >= 3:
            try:
                user_id = int(parts[2])
            except ValueError:
                return None
            user = await self.fetch_user(user_id)
            return await user.create_dm()
        if len(parts) < 4:
            return None
        try:
            channel_id = int(parts[2])
            thread_id = int(parts[3])
        except ValueError:
            return None
        if thread_id == channel_id:
            return self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        try:
            return await self.fetch_channel(thread_id)
        except discord.NotFound:
            return None


def _strip_leading_ack(body: str, ack: str) -> str:
    """Drop the ack sentence from the front of the final reply.

    The first sentence is streamed as its own "I heard you" message
    (``first_sentence`` event); the model's full reply then opens with
    that very sentence, so posting the whole body duplicates it as a
    standalone message (gripe #48766). If ``body`` starts with ``ack``,
    remove that span and any following whitespace. No-op when the model
    restructured its opening (``body`` no longer starts with ``ack``),
    so we never mangle a reply that legitimately begins differently.
    """
    ack = (ack or "").strip()
    if not ack or not body.startswith(ack):
        return body
    return body[len(ack) :].lstrip()


def _split_for_discord(text: str, limit: int) -> list[str]:
    """Split a long body across Discord-safe chunks at paragraph boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        if len(buf) + len(para) + 2 > limit:
            if buf:
                chunks.append(buf.rstrip())
                buf = ""
            if len(para) > limit:
                # Hard split.
                while len(para) > limit:
                    chunks.append(para[:limit])
                    para = para[limit:]
        buf += para + "\n\n"
    if buf.strip():
        chunks.append(buf.rstrip())
    return chunks


def _extract_message_body(rendering: str) -> str:
    # Very simple parser — precis renders kind='message' as a markdown
    # blob with the title as the first non-header line, status etc.
    # below. We send the title (which is the message text) for v1.
    lines = rendering.splitlines()
    title_idx = -1
    for i, line in enumerate(lines):
        if line.startswith("# message "):
            title_idx = i
            break
    if title_idx == -1:
        return rendering.strip()[:1900]
    body_lines = []
    for line in lines[title_idx + 1 :]:
        if (
            line.startswith("status:")
            or line.startswith("target:")
            or line.startswith("reason:")
        ):
            break
        if line.strip():
            body_lines.append(line)
    return "\n".join(body_lines).strip()


def _read_or_empty(path: str) -> str:
    p = Path(path)
    if not p.exists():
        log.warning("file not found, using empty: %s", path)
        return ""
    return p.read_text(encoding="utf-8")


def _llm_with_override(cfg: LLMConfig, model_override: str | None) -> LLMConfig:
    """Return a copy of cfg with `--model <override>` if override is set.

    Replaces the value following the first `--model` token. If the
    flag is absent we append it — keeps `/model` working even when a
    user's config.yaml stripped the default.
    """
    if not model_override:
        return cfg
    cmd = list(cfg.command)
    for i, tok in enumerate(cmd):
        if tok == "--model" and i + 1 < len(cmd):
            cmd[i + 1] = model_override
            break
    else:
        cmd.extend(["--model", model_override])
    return dataclasses.replace(cfg, command=cmd)


def _ctx_from_message(message: discord.Message) -> Any:
    """Synthesise a DiscordContext from a discord.py Message."""
    return _MessageCtx(
        guild_id=message.guild.id if message.guild else None,
        channel_id=_root_channel_id(message),
        thread_id=_thread_id_of_int(message),
        is_dm=isinstance(message.channel, discord.DMChannel),
        author_id=message.author.id,
    )


def _reply_target(message: discord.Message) -> Any:
    """Where progress and reply messages should land.

    A user message in a channel that later spawns a thread (Discord
    auto-thread, "Create Thread" on the message) yields a
    ``message.thread`` attribute pointing at the new Thread. The
    conversation continues there, so the reply should land in the
    thread — not in the parent channel where the starter message
    sits. For ordinary channel and in-thread messages
    ``message.thread`` is None and we fall back to ``message.channel``.
    """
    return message.thread or message.channel


# Tools we want to expand in the progress indicator. ``mcp__precis__get``
# is the standout: knowing kind+id tells you "fetching the librarian's
# transcript", not the opaque "she's calling get". Other tools just
# render as their bare name.
_PROGRESS_ARG_KEYS = ("kind", "id", "view", "q", "scope")
_PROGRESS_VALUE_MAXLEN = 40


def _format_progress(evt: tuple) -> str | None:
    """Render a claude_invoke progress event for Discord.

    Supports three event shapes:
      - ("tool_use", tool_name, args_dict)
      - ("subagent", subagent_type)
      - ("text_partial", char_count)

    Returns None when the event isn't worth showing (e.g. a known but
    uninteresting event). Caller throttles ``text_partial`` separately.
    """
    kind = evt[0]
    if kind == "tool_use":
        name = evt[1]
        args = evt[2] if len(evt) >= 3 else {}
        return f"🔍 {_format_tool_call(name, args)}…"
    if kind == "subagent":
        return f"🤝 `Agent({evt[1]})`…"
    if kind == "text_partial":
        return f"💭 thinking… ({evt[1]:,} chars so far)"
    return None


def _format_tool_call(name: str, args: dict[str, Any] | None) -> str:
    """Build an inline-code rendering of a tool call.

    Tool names with double-underscores (``mcp__precis__get``) get bold-
    italic'd by Discord's markdown if shown raw, so the result is
    always backticked.

    For ``mcp__precis__*`` we surface the routing args (kind/id/view/q)
    so the user can see what's being fetched instead of just "she's
    calling get."
    """
    args = args or {}
    if name.startswith("mcp__precis__"):
        bits = []
        for key in _PROGRESS_ARG_KEYS:
            if key not in args:
                continue
            bits.append(f"{key}={_repr_short(args[key])}")
        if bits:
            return f"`{name}({', '.join(bits)})`"
    return f"`{name}`"


def _repr_short(value: Any) -> str:
    """Repr-ish stringification that truncates long IDs so the progress
    message stays under Discord's 2000-char ceiling on busy threads."""
    if isinstance(value, str):
        if len(value) > _PROGRESS_VALUE_MAXLEN:
            return repr(value[: _PROGRESS_VALUE_MAXLEN - 1] + "…")
        return repr(value)
    return repr(value)


def _root_channel_id(message: discord.Message) -> int:
    ch = message.channel
    if isinstance(ch, discord.Thread):
        return ch.parent_id if ch.parent_id is not None else ch.id
    return ch.id


def _thread_id_of(message: discord.Message) -> str | None:
    ch = message.channel
    if isinstance(ch, discord.Thread):
        return str(ch.id)
    return None


def _thread_id_of_int(message: discord.Message) -> int | None:
    ch = message.channel
    if isinstance(ch, discord.Thread):
        return ch.id
    return None


class _MessageCtx:
    def __init__(
        self,
        *,
        guild_id: int | None,
        channel_id: int,
        thread_id: int | None,
        is_dm: bool,
        author_id: int,
    ) -> None:
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.thread_id = thread_id
        self.is_dm = is_dm
        self.author_id = author_id


async def run(cfg: Config) -> None:
    """Bring up the bot end-to-end. Blocks until Discord disconnects."""
    token = load_discord_token(cfg.discord)
    precis = PrecisClient(cfg.precis)
    await precis.start()
    # LISTEN needs a direct postgres session (notify_database_url) — it does
    # not survive transaction-pooled pgbouncer. Fall back to database_url.
    listener = PgListener(cfg.precis.notify_database_url or cfg.precis.database_url)
    await listener.start()
    capture_shim, shim_runner = await _start_shim_inline(cfg, precis)
    # Monitor the long-lived precis subprocess and respawn it if it dies or
    # comes up degraded (see PrecisClient.ping). Without this the subprocess
    # is spawned once and never checked — a boot-race storeless server then
    # persists until asa itself restarts.
    health_task = asyncio.create_task(
        health_loop(precis, cfg.precis.health_check_interval_seconds)
    )
    try:
        # Replay any fallback captures from a previous outage.
        await capture_shim.replay_fallback()
        bot = AsaBot(
            cfg=cfg,
            precis=precis,
            capture=capture_shim,
            listener=listener,
        )
        await bot.start(token)
    finally:
        health_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await health_task
        await listener.stop()
        await shim_runner.cleanup()
        await precis.stop()


async def _start_shim_inline(cfg: Config, precis: PrecisClient) -> Any:
    """Avoid a circular import by binding the shim here."""
    from asa_bot.capture_shim import start_shim

    return await start_shim(cfg.capture, precis)

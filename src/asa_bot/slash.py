"""Text-prefix slash commands for asa-bot.

Intercepted in ``bot.on_message`` before the claude dispatch path, so
commands cost zero LLM tokens and don't write to the conv ref. Discord
treats these as ordinary messages (no native interaction/autocomplete);
the ``/`` prefix is just a convention this dispatcher recognises.

Parsing is permissive on purpose — the user types these on a phone
keyboard. ``shlex`` handles quoting; ``KEY=VALUE`` pairs unpack into a
dict; bare positional tokens become a positional list. Unknown commands
return a polite usage stub rather than a hard error.
"""

from __future__ import annotations

import dataclasses
import io
import json
import logging
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import discord
import yaml

from asa_bot import preamble as preamble_mod
from asa_bot.config import Config
from asa_bot.conv_slug import compute_slug
from asa_bot.precis_client import PrecisClient

log = logging.getLogger(__name__)


SlashHandler = Callable[["SlashContext"], Awaitable[None]]


# Short-name → canonical model id. Anything not in this map is passed
# through verbatim so a one-off custom model id still works.
MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


@dataclasses.dataclass(slots=True)
class Runtime:
    """Mutable per-process overrides toggled by slash commands.

    Distinct from `Config` (frozen, file-driven). The bot owns one
    instance and passes it into `slash.dispatch`; the worker reads
    `model_override` when building each turn's claude command.
    """

    model_override: str | None = None


class SlashContext:
    """Everything a slash handler needs in one bag.

    Keeps the call-site signature uniform so dispatch is data-driven.
    """

    def __init__(
        self,
        *,
        message: discord.Message,
        positional: list[str],
        kwargs: dict[str, str],
        precis: PrecisClient,
        config: Config,
        runtime: Runtime,
        soul: str,
        tool_hints: str,
        ctx_from_message: Callable[[discord.Message], Any],
        reply_target: Callable[[discord.Message], Any],
    ) -> None:
        self.message = message
        self.positional = positional
        self.kwargs = kwargs
        self.precis = precis
        self.config = config
        self.runtime = runtime
        self.soul = soul
        self.tool_hints = tool_hints
        self.ctx_from_message = ctx_from_message
        self.reply_target = reply_target

    @property
    def author_handle(self) -> str:
        m = self.message
        return (
            f"{m.author.name}#{m.author.discriminator}"
            if m.author.discriminator != "0"
            else m.author.name
        )

    async def send(
        self, content: str = "", *, file: discord.File | None = None
    ) -> None:
        """Send a reply to wherever the slash command came from."""
        try:
            if file is not None:
                await self.reply_target(self.message).send(content=content, file=file)
            else:
                # 2000-char Discord limit. Truncate with a note.
                if len(content) > 1950:
                    content = content[:1950] + "\n*(output truncated)*"
                await self.reply_target(self.message).send(content=content)
        except discord.HTTPException as e:
            log.warning("slash send failed: %s", e)


# ── parsing ────────────────────────────────────────────────────────


def parse_args(raw: str) -> tuple[list[str], dict[str, str]]:
    """Split ``raw`` into (positional, kwargs).

    ``shlex`` handles quoting (smart quotes normalised to ASCII first
    so phone keyboards don't break). Each token: if it contains ``=``
    it's a kwarg (``kind=paper``), otherwise positional. Bare values
    keep their order; kwargs go into a dict.
    """
    if not raw:
        return [], {}
    # Normalise smart quotes that phone keyboards emit.
    raw = raw.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    try:
        tokens = shlex.split(raw)
    except ValueError:
        # Mismatched quotes — fall back to whitespace split.
        tokens = raw.split()
    positional: list[str] = []
    kwargs: dict[str, str] = {}
    for tok in tokens:
        if "=" in tok:
            k, _, v = tok.partition("=")
            kwargs[k.strip()] = v.strip()
        else:
            positional.append(tok)
    return positional, kwargs


# ── commands ───────────────────────────────────────────────────────


async def cmd_help(ctx: SlashContext) -> None:
    lines = [
        "**asa slash commands** — text-prefix, no LLM cost, no conv capture.",
        "",
        "`/help` — this list",
        "`/show-prompt` — full preamble (system prompt + injected blocks) as attachment",
        "`/status` — record counts per kind + most-recent entry",
        "`/model [opus|sonnet|haiku|<id>]` — show or change the LLM driving each turn",
        "`/agents` — list available Claude Code agent definitions",
        "`/skill <query>` — semantic search over precis skill docs",
        "`/precis <verb> key=value [...]` — direct MCP call. e.g.:",
        "  `/precis search kind=memory tags=internal-thought page_size=5`",
        "  `/precis get kind=memory id=6134`",
        '  `/precis put kind=memory text="..." tags=user:asa`',
        "`/dreams` — recent DREAM:speculative + plain speculative memories",
        "`/diary` — recent internal-thought + internal-state",
    ]
    await ctx.send("\n".join(lines))


def current_model(cfg: Config, runtime: Runtime) -> str:
    """Effective model: runtime override wins, else the model baked into cfg."""
    if runtime.model_override:
        return runtime.model_override
    cmd = cfg.llm.command
    for i, tok in enumerate(cmd):
        if tok == "--model" and i + 1 < len(cmd):
            return cmd[i + 1]
    return "(unknown — no --model flag in cfg.llm.command)"


async def cmd_model(ctx: SlashContext) -> None:
    """Show or set the model used for subsequent turns."""
    if not ctx.positional:
        active = current_model(ctx.config, ctx.runtime)
        source = "override" if ctx.runtime.model_override else "config default"
        aliases = ", ".join(f"`{k}`" for k in MODEL_ALIASES)
        await ctx.send(
            f"**current model**: `{active}` ({source})\n"
            f"change with: `/model <id>` — aliases: {aliases}"
        )
        return
    target = ctx.positional[0].lower()
    resolved = MODEL_ALIASES.get(target, ctx.positional[0])
    ctx.runtime.model_override = resolved
    log.info("model override set: %s (from %r)", resolved, target)
    await ctx.send(f"model → `{resolved}` (active for next turn)")


async def cmd_agents(ctx: SlashContext) -> None:
    """List Claude Code agent definitions visible to the bot's cwd + ~/.claude."""
    cwd = Path(ctx.config.llm.cwd)
    locations = [
        ("user", Path.home() / ".claude" / "agents"),
        ("project", cwd / ".claude" / "agents"),
    ]
    entries: list[tuple[str, str, str, str]] = []  # (scope, name, desc, path)
    for scope, root in locations:
        if not root.is_dir():
            continue
        for p in sorted(root.glob("*.md")):
            name, desc = _parse_agent_frontmatter(p)
            entries.append((scope, name or p.stem, desc, str(p)))
    if not entries:
        searched = ", ".join(str(r) for _, r in locations)
        await ctx.send(f"**agents**: none found.\nlooked in: {searched}")
        return
    by_scope: dict[str, list[tuple[str, str, str]]] = {}
    for scope, name, desc, path in entries:
        by_scope.setdefault(scope, []).append((name, desc, path))
    parts = [f"**agents** ({len(entries)} total)"]
    for scope in ("project", "user"):
        rows = by_scope.get(scope)
        if not rows:
            continue
        parts.append(f"\n*{scope}*")
        for name, desc, _ in rows:
            head = desc.strip().split("\n", 1)[0] if desc else ""
            if len(head) > 120:
                head = head[:117] + "..."
            parts.append(f"  `{name}` — {head}" if head else f"  `{name}`")
    await ctx.send("\n".join(parts))


def _parse_agent_frontmatter(path: Path) -> tuple[str, str]:
    """Return (name, description) from a `.md` agent file's YAML frontmatter.

    Tolerates missing/malformed frontmatter — falls back to ("", "").
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "", ""
    if not text.startswith("---"):
        return "", ""
    end = text.find("\n---", 4)
    if end == -1:
        return "", ""
    try:
        fm = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        return "", ""
    if not isinstance(fm, dict):
        return "", ""
    return str(fm.get("name", "")), str(fm.get("description", ""))


async def cmd_show_prompt(ctx: SlashContext) -> None:
    """Build the same preamble a real turn would receive, dump as attachment."""
    m = ctx.message
    discord_ctx = ctx.ctx_from_message(m)
    slug = compute_slug(discord_ctx)
    guild_name = m.guild.name if m.guild else "DM"
    channel_obj = m.channel
    channel_name = getattr(channel_obj, "name", None) or "channel"
    thread_name = channel_obj.name if isinstance(channel_obj, discord.Thread) else None
    try:
        body = await preamble_mod.build(
            precis=ctx.precis,
            cfg=ctx.config.preamble,
            conv_slug=slug,
            guild_name=guild_name,
            channel_name=channel_name,
            thread_name=thread_name,
            author_handle=ctx.author_handle,
            soul=ctx.soul,
            tool_hints=ctx.tool_hints,
        )
    except Exception as e:
        log.exception("show-prompt build failed")
        await ctx.send(f"preamble build failed: {e!r}")
        return
    buf = body.encode("utf-8")
    fp = io.BytesIO(buf)
    file = discord.File(fp=fp, filename=f"preamble-{slug.replace('/', '-')}.md")
    await ctx.send(
        content=f"Preamble for this thread ({len(buf):,} bytes).",
        file=file,
    )


async def cmd_status(ctx: SlashContext) -> None:
    """Counts per kind + the most-recent entry per kind."""
    kinds = ctx.positional or ["paper", "patent", "memory", "pres", "conv"]
    parts = ["**precis status** — counts + latest"]
    for k in kinds:
        try:
            blob = await ctx.precis.call_tool(
                "search",
                {"kind": k, "page_size": 1, "q": "*"},
            )
            first_line = (blob or "").strip().split("\n", 1)[0] if blob else "(empty)"
            parts.append(f"`{k}`: {first_line}")
        except Exception as e:
            parts.append(f"`{k}`: error — {e}")
    await ctx.send("\n".join(parts))


async def cmd_precis(ctx: SlashContext) -> None:
    """Direct MCP passthrough. First positional is the verb, kwargs are the args."""
    if not ctx.positional:
        await ctx.send(
            "usage: `/precis <verb> key=value [...]`. "
            "verbs: get search put edit delete tag link.\n"
            "examples:\n"
            "  `/precis search kind=memory tags=internal-thought page_size=5`\n"
            "  `/precis get kind=memory id=6134`"
        )
        return
    verb = ctx.positional[0]
    args: dict[str, Any] = {}
    for k, v in ctx.kwargs.items():
        # Auto-typing: ints, lists (comma-separated), JSON, else string.
        if v.isdigit():
            args[k] = int(v)
        elif v.startswith("[") or v.startswith("{"):
            try:
                args[k] = json.loads(v)
            except json.JSONDecodeError:
                args[k] = v
        elif "," in v and k in ("tags", "add", "remove"):
            args[k] = [s.strip() for s in v.split(",")]
        else:
            args[k] = v
    # Trailing positionals (after verb) become positional MCP args — rare;
    # most callers should use kwargs. We just attach as a comment in the
    # display rather than passing them on, since the MCP shape doesn't
    # take positionals.
    extras = ctx.positional[1:]
    try:
        result = await ctx.precis.call_tool(verb, args)
    except Exception as e:
        await ctx.send(f"`precis {verb}({args})` failed: `{e!r}`")
        return
    rendered = result if isinstance(result, str) else json.dumps(result, default=str)
    header = f"**precis {verb}**({_format_args(args)})"
    if extras:
        header += f"\n*(ignored positionals: {extras})*"
    body = f"{header}\n```\n{rendered}\n```"
    if len(body) > 1900:
        # Send as attachment so nothing's lost.
        fp = io.BytesIO((header + "\n\n" + rendered).encode("utf-8"))
        file = discord.File(fp=fp, filename=f"precis-{verb}.txt")
        await ctx.send(content=header + " *(attached)*", file=file)
    else:
        await ctx.send(body)


async def cmd_dreams(ctx: SlashContext) -> None:
    """Recent dream-tagged memories.

    Precis tag filtering is AND-of-tags (intersection), and the closed
    axis `DREAM:` rejects bare-flag `speculative` collisions at write
    time. So we just query `DREAM:speculative`; any legacy bare-flag
    memories were normalised on the migration that introduced the
    closed axis.
    """
    try:
        blob = await ctx.precis.call_tool(
            "search",
            {
                "kind": "memory",
                "tags": ["DREAM:speculative"],
                "page_size": int(ctx.kwargs.get("n", "10")),
            },
        )
    except Exception as e:
        await ctx.send(f"`/dreams` failed: `{e!r}`")
        return
    body = f"**recent dreams**\n\n{blob}"
    if len(body) > 1900:
        fp = io.BytesIO(body.encode("utf-8"))
        file = discord.File(fp=fp, filename="dreams.md")
        await ctx.send(content="**recent dreams** *(attached)*", file=file)
    else:
        await ctx.send(body)


async def cmd_diary(ctx: SlashContext) -> None:
    """Recent internal-state self-doc + internal-thought trail.

    Precis tag filter is AND, so passing both tags returns nothing
    (memories carry one or the other, not both). Run them as two
    queries and stitch the output: ``internal-state`` is the singleton
    self-doc and goes on top; ``internal-thought`` is the recency trail
    underneath.
    """
    n = int(ctx.kwargs.get("n", "10"))
    sections: list[str] = []
    try:
        state_blob = await ctx.precis.call_tool(
            "search",
            {"kind": "memory", "tags": ["internal-state"], "page_size": 1},
        )
        sections.append(f"**self-doc** (internal-state)\n{state_blob}")
    except Exception as e:
        sections.append(f"**self-doc** lookup failed: `{e!r}`")
    try:
        thought_blob = await ctx.precis.call_tool(
            "search",
            {"kind": "memory", "tags": ["internal-thought"], "page_size": n},
        )
        sections.append(f"**diary** — last {n} internal-thought\n{thought_blob}")
    except Exception as e:
        sections.append(f"**diary** lookup failed: `{e!r}`")
    body = "\n\n".join(sections)
    if len(body) > 1900:
        fp = io.BytesIO(body.encode("utf-8"))
        file = discord.File(fp=fp, filename="diary.md")
        await ctx.send(content="**diary** *(attached)*", file=file)
    else:
        await ctx.send(body)


async def cmd_skill(ctx: SlashContext) -> None:
    """Semantic + lexical search over precis skills.

    Thin wrapper around ``search(kind='skill', q=...)``. The underlying
    handler already merges cosine over the skill-chunk embedding index
    with substring matching (precis-mcp src/precis/handlers/skill.py),
    so natural-language phrasing works — "how do I file a bug" surfaces
    the gripe skill, etc.
    """
    if not ctx.positional:
        await ctx.send(
            "usage: `/skill <query>` — semantic search over precis skill docs.\n"
            "examples:\n"
            "  `/skill how do I file a bug`\n"
            "  `/skill memory tag axes`"
        )
        return
    q = " ".join(ctx.positional)
    page_size = int(ctx.kwargs.get("n", "8"))
    try:
        blob = await ctx.precis.call_tool(
            "search",
            {"kind": "skill", "q": q, "page_size": page_size},
        )
    except Exception as e:
        await ctx.send(f"`/skill {q!r}` failed: `{e!r}`")
        return
    body = f"**skill matches for** `{q}`\n\n{blob}"
    if len(body) > 1900:
        fp = io.BytesIO(body.encode("utf-8"))
        file = discord.File(fp=fp, filename="skill-search.md")
        await ctx.send(content=f"**skill matches for** `{q}` *(attached)*", file=file)
    else:
        await ctx.send(body)


# ── registry ───────────────────────────────────────────────────────


REGISTRY: dict[str, SlashHandler] = {
    "help": cmd_help,
    "show-prompt": cmd_show_prompt,
    "showprompt": cmd_show_prompt,  # tolerated alias
    "prompt": cmd_show_prompt,  # tolerated alias
    "status": cmd_status,
    "model": cmd_model,
    "agents": cmd_agents,
    "skill": cmd_skill,
    "precis": cmd_precis,
    "dreams": cmd_dreams,
    "diary": cmd_diary,
}


def _format_args(args: dict[str, Any]) -> str:
    """Compact one-line render of an MCP args dict for the reply header."""
    parts = []
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 30:
            v = v[:27] + "..."
        parts.append(f"{k}={v!r}")
    return ", ".join(parts)


async def dispatch(
    *,
    message: discord.Message,
    precis: PrecisClient,
    config: Config,
    runtime: Runtime,
    soul: str,
    tool_hints: str,
    ctx_from_message: Callable[[discord.Message], Any],
    reply_target: Callable[[discord.Message], Any],
) -> None:
    """Parse + route. Called from ``bot.on_message`` when content starts with ``/``.

    Unknown commands fall through to a usage hint rather than silently
    consuming the message; the user typing ``/foo`` should learn ``foo``
    isn't a thing rather than wondering why nothing happened.
    """
    raw = (message.content or "").strip()
    if not raw.startswith("/"):
        return
    after = raw[1:]
    cmd, _, rest = after.partition(" ")
    cmd = cmd.lower()
    handler = REGISTRY.get(cmd)
    ctx_obj = None  # Allow ctx construction even if handler is None.
    if handler is None:
        known = ", ".join(f"`/{k}`" for k in REGISTRY if not k.endswith("prompt"))
        try:
            await reply_target(message).send(
                f"unknown command `/{cmd}`. known: {known}. `/help` for details."
            )
        except discord.HTTPException:
            pass
        return
    positional, kwargs = parse_args(rest)
    ctx_obj = SlashContext(
        message=message,
        positional=positional,
        kwargs=kwargs,
        precis=precis,
        config=config,
        runtime=runtime,
        soul=soul,
        tool_hints=tool_hints,
        ctx_from_message=ctx_from_message,
        reply_target=reply_target,
    )
    try:
        await handler(ctx_obj)
    except Exception:
        log.exception("slash handler %s crashed", cmd)
        try:
            await reply_target(message).send(f"`/{cmd}` crashed — check the bot log.")
        except discord.HTTPException:
            pass

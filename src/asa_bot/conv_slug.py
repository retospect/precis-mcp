"""Discord identifier → precis conv slug.

Slug shape:
    discord/<guild_id>/<channel_id>/<thread_or_root_id>
    discord/dm/<user_id>                                   (direct messages)

Stable across daemon restarts (no UUIDs, no DB lookup). One conv
ref per Discord thread; channel-root chats collapse to a slug where
thread_id == channel_id.
"""

from __future__ import annotations

from typing import Protocol


class DiscordContext(Protocol):
    guild_id: int | None
    channel_id: int
    thread_id: int | None
    is_dm: bool
    author_id: int


def compute_slug(ctx: DiscordContext) -> str:
    if ctx.is_dm:
        return f"discord/dm/{ctx.author_id}"
    if ctx.guild_id is None:
        # Shouldn't happen in normal Discord, but treat as DM-ish.
        return f"discord/dm/{ctx.author_id}"
    thread_or_root = ctx.thread_id if ctx.thread_id is not None else ctx.channel_id
    return f"discord/{ctx.guild_id}/{ctx.channel_id}/{thread_or_root}"

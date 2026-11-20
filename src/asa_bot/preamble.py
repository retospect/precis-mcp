"""Discord/Slack adapter for the transport-independent core context builder.

Defaults and the public call signature remain here so existing bridge callers
keep their exact prompt while web chat selects its own core profile.
"""

from dataclasses import asdict

from asa_bot.config import PreambleConfig
from precis.chat.context import ContextProfile, ToolCaller
from precis.chat.context import (
    _fetch_stickies as _fetch_stickies,
)
from precis.chat.context import (
    _fetch_user_note as _fetch_user_note,
)
from precis.chat.context import (
    _render_conv_pointer as _render_conv_pointer,
)
from precis.chat.context import (
    _render_inner_life as _render_inner_life,
)
from precis.chat.context import (
    _render_operator_prefs as _render_operator_prefs,
)
from precis.chat.context import (
    _render_stickies as _render_stickies,
)
from precis.chat.context import (
    _render_user_note as _render_user_note,
)
from precis.chat.context import (
    _user_note_tag as _user_note_tag,
)
from precis.chat.context import build as build_context


def discord_profile(cfg: PreambleConfig) -> ContextProfile:
    """Carry all configurable Discord budgets into core without new defaults."""
    values = asdict(cfg)
    values.pop("soul_path")
    values.pop("tool_hints_path")
    return ContextProfile(**values)


async def build(
    *,
    precis: ToolCaller,
    cfg: PreambleConfig,
    conv_slug: str,
    guild_name: str,
    channel_name: str,
    thread_name: str | None,
    author_handle: str,
    soul: str,
    tool_hints: str,
    platform: str = "Discord",
) -> str:
    """Build the bridge preamble with its legacy section order and budgets."""
    return await build_context(
        precis=precis,
        profile=discord_profile(cfg),
        conv_slug=conv_slug,
        guild_name=guild_name,
        channel_name=channel_name,
        thread_name=thread_name,
        author_handle=author_handle,
        soul=soul,
        tool_hints=tool_hints,
        platform=platform,
    )

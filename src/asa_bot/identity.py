"""Discord user id -> precis account.

Discord gives asa a display name; precis knows accounts. A user links the two
on ``/account`` (``web_users.discord_user_id``, a numeric snowflake). This
module resolves ``message.author.id`` to that account's ``abbrev`` so asa's
conv turns and per-user memory tag (``user:<handle>``) carry a stable account
handle instead of a mutable display name.

The abbrev also rides into the turn's LLM run so its precis writes are
attributed to that account (:mod:`precis.acting_user`).

Unmapped users, a missing DSN, or any DB failure fall back to the display
name asa always used -- identity lookup must never take a turn down -- and
their turns write as the generic ``agent``.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_LOOKUP_SQL = "SELECT abbrev FROM web_users WHERE discord_user_id = %s"


def display_handle(author: object) -> str:
    """The legacy handle: ``name#discriminator`` (``name`` for new-style)."""
    name = getattr(author, "name", "unknown")
    disc = getattr(author, "discriminator", "0")
    return f"{name}#{disc}" if disc not in ("0", None) else str(name)


async def resolve_account_abbrev(dsn: str, discord_user_id: int | str) -> str | None:
    """The mapped account's ``abbrev``, or None (unmapped / no DSN / error)."""
    if not dsn:
        return None
    try:
        import psycopg

        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            cur = await conn.execute(_LOOKUP_SQL, (str(discord_user_id),))
            row = await cur.fetchone()
    except Exception:
        log.exception("discord id %s: account lookup failed", discord_user_id)
        return None
    return str(row[0]) if row and row[0] else None


async def resolve_speaker(dsn: str, author: object) -> tuple[str, str | None]:
    """``(handle, account abbrev or None)`` for a Discord author.

    The handle is the abbrev when mapped, else the display handle. The
    abbrev alone decides write attribution (:mod:`precis.acting_user`), so an
    unmapped speaker never borrows an account through a lookalike name.
    """
    uid = getattr(author, "id", None)
    abbrev = await resolve_account_abbrev(dsn, uid) if uid is not None else None
    return (abbrev or display_handle(author)), abbrev


async def resolve_author_handle(dsn: str, author: object) -> str:
    """Account ``abbrev`` for a mapped Discord author, else the display handle."""
    handle, _abbrev = await resolve_speaker(dsn, author)
    return handle

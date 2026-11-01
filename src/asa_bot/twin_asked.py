"""``twin:asked`` capture — a user's "what is X?" queues the term for the
card forge (gr548277).

Runs fire-and-forget *after* a turn's reply is produced: the caller wraps it
in a task and every failure lands in the log, never in the reply. When the
message matches the what-is grammar (:func:`precis.reading.asked.
extract_asked_term`):

- a live concept/taxon named the term (case-insensitive, aliases included)
  gets tagged ``twin:asked`` and its ``meta.asked_count`` bumped;
- otherwise a ``twin:asked`` concept is minted via the ``put`` door —
  ``<term> — <first sentence of the assistant's reply>`` — ``related-to``
  the conv, owned by the asking user's login (``refs.owner_login``).

The verbs go through the precis MCP client (single-sourced handler
validation); the name match, ``asked_count`` bump and owner stamp are plain
SQL on the bot's DSN — the same read path ``identity.resolve_speaker`` and
``_fetch_message_body`` already use. Never fires on agent turns — the caller
only reaches this from the human-message path.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from precis.reading.asked import extract_asked_term, first_sentence

log = logging.getLogger(__name__)

_TAG = "twin:asked"
_CREATED_RE = re.compile(r"\bcn(\d+)\b")

_FIND_SQL = """
    SELECT kind, ref_id FROM refs
    WHERE kind IN ('concept', 'taxon') AND retired_at IS NULL
      AND (
            meta->>'norm_name' = %s
         OR lower(meta->>'name') = lower(%s)
         OR EXISTS (
                SELECT 1 FROM jsonb_array_elements_text(
                    COALESCE(meta->'aliases', '[]'::jsonb)) a
                WHERE lower(a) = lower(%s))
      )
    ORDER BY CASE WHEN kind = 'concept' THEN 0 ELSE 1 END, ref_id
    LIMIT 1
"""

_BUMP_SQL = (
    "UPDATE refs SET meta = meta || jsonb_build_object("
    "  'asked_count', COALESCE((meta->>'asked_count')::int, 0) + 1) "
    "WHERE ref_id = %s AND retired_at IS NULL"
)


async def _connect(dsn: str) -> Any:
    import psycopg

    return await psycopg.AsyncConnection.connect(dsn, autocommit=True)


async def _find_named(dsn: str, term: str) -> tuple[str, int] | None:
    """``(kind, ref_id)`` for the term's live concept/taxon — same predicate
    as :func:`precis.reading.asked.find_named_term`, async."""
    from precis.reading.concepts import normalize_name

    conn = await _connect(dsn)
    try:
        cur = await conn.execute(_FIND_SQL, (normalize_name(term), term, term))
        row = await cur.fetchone()
    finally:
        await conn.close()
    return (str(row[0]), int(row[1])) if row else None


async def _login_for(dsn: str, abbrev: str) -> str | None:
    """``web_users.login`` for a resolved speaker abbrev — ``refs.owner_login``
    FKs on login, not abbrev."""
    conn = await _connect(dsn)
    try:
        cur = await conn.execute(
            "SELECT login FROM web_users WHERE abbrev = %s", (abbrev,)
        )
        row = await cur.fetchone()
    finally:
        await conn.close()
    return str(row[0]) if row and row[0] else None


async def _stamp_created(dsn: str, ref_id: int, *, owner_login: str | None) -> None:
    """Owner + ``asked_count=1`` on a freshly-minted concept — the ``put``
    door owns meta shape; these two fields have no verb."""
    conn = await _connect(dsn)
    try:
        await conn.execute(
            "UPDATE refs SET owner_login = COALESCE(owner_login, %s), "
            "meta = meta || jsonb_build_object('asked_count', "
            "  COALESCE((meta->>'asked_count')::int, 0) + 1) "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (owner_login, ref_id),
        )
    finally:
        await conn.close()


async def _bump(dsn: str, ref_id: int) -> None:
    conn = await _connect(dsn)
    try:
        await conn.execute(_BUMP_SQL, (ref_id,))
    finally:
        await conn.close()


async def capture(
    *,
    precis: Any,
    dsn: str,
    message_text: str,
    reply_text: str,
    conv_slug: str,
    account_abbrev: str | None,
) -> str | None:
    """Tag/mint the asked term. Returns the captured term or ``None``.

    Every DB/MCP leg is best-effort at its own boundary: a failure that
    reaches the caller is the caller's ``log.exception``, never a turn
    failure.
    """
    term = extract_asked_term(message_text)
    if term is None:
        return None

    owner_login = (
        await _login_for(dsn, account_abbrev) if (account_abbrev and dsn) else None
    )

    found = await _find_named(dsn, term) if dsn else None
    if found is not None:
        kind, ref_id = found
        await precis.call_tool("tag", {"kind": kind, "id": ref_id, "add": [_TAG]})
        await _bump(dsn, ref_id)
        log.info("twin:asked: %s id=%s re-tagged for %r", kind, ref_id, term)
        return term

    definition = first_sentence(reply_text)
    text = f"{term} — {definition}" if definition else term
    args: dict[str, Any] = {"kind": "concept", "text": text, "tags": [_TAG]}
    if conv_slug:
        args["link"] = f"conv:{conv_slug}"
    ack = await precis.call_tool("put", args)
    m = _CREATED_RE.search(str(ack or ""))
    if m is not None and dsn:
        await _stamp_created(dsn, int(m.group(1)), owner_login=owner_login)
        log.info("twin:asked: minted concept cn%s for %r", m.group(1), term)
    else:
        log.warning(
            "twin:asked: put ack carried no cn handle, owner/asked_count "
            "unstamped for %r: %.120s",
            term,
            str(ack),
        )
    return term


__all__ = ["capture"]

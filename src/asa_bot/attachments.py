"""Discord PDF attachment -> paper ingest (gr434187).

A PDF attached to a Discord message in a channel asa serves is dropped into
the same watch inbox ``precis ingest --watch`` scans (``$PAPERS_INBOX``, else
``$PRECIS_WATCH_INBOX``), so the normal Marker pipeline ingests it and folds a
known file onto its existing ref. asa then polls ``refs.pdf_sha256`` for the
file's hash and replies with the ``pa<id>`` handle.

* **Who:** only a speaker mapped to a precis account (:mod:`asa_bot.identity`)
  may ingest; the account is recorded on the paper as a ``user:<abbrev>`` tag.
  An unmapped speaker is refused with a reply, not silently dropped.
* **Fetch:** the CDN URL is Discord-supplied but still external input, so it
  goes through the same bounded ``safe_stream`` download ``paper_pull`` uses
  (50 MB cap, ``%PDF`` magic, deadline); a pre-check on the declared size
  refuses an oversize file before any bytes move.
* **Drop:** staged in ``<inbox>/.staging/`` and renamed into place, so the
  watcher never sees a half-written PDF. A hash already on a ref is answered
  from the DB and not re-dropped.

Every failure becomes a reply string; nothing here raises into the bot.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Poll cadence / ceiling for the ingest to mint the ref (Marker is slow).
POLL_INTERVAL_S = 20.0
POLL_TIMEOUT_S = 30 * 60.0

_FIND_SQL = (
    "SELECT ref_id FROM refs WHERE pdf_sha256 = %s AND retired_at IS NULL "
    "ORDER BY ref_id LIMIT 1"
)

Send = Callable[[str], Awaitable[Any]]
CallTool = Callable[[str, dict[str, Any]], Awaitable[Any]]


def inbox_dir() -> Path | None:
    """The watch inbox this host can write, or None when unconfigured."""
    raw = (
        os.environ.get("PAPERS_INBOX") or os.environ.get("PRECIS_WATCH_INBOX") or ""
    ).strip()
    return Path(raw) if raw else None


def is_pdf_attachment(att: object) -> bool:
    """Name or declared type says PDF (the magic bytes are checked on fetch)."""
    name = str(getattr(att, "filename", "") or "").lower()
    ctype = str(getattr(att, "content_type", "") or "").lower()
    return name.endswith(".pdf") or ctype.startswith("application/pdf")


def _drop_name(att: object, message_id: int | str) -> str:
    stem = Path(str(getattr(att, "filename", "") or "attachment")).stem
    stem = re.sub(r"[^a-zA-Z0-9._-]", "_", stem.lower())[:60] or "attachment"
    return f"discord-{message_id}-{stem}.pdf"


async def find_ref_id(dsn: str, sha256: str) -> int | None:
    """Live ref holding this ``pdf_sha256``, or None (also on DB failure)."""
    if not dsn:
        return None
    try:
        import psycopg

        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            cur = await conn.execute(_FIND_SQL, (sha256,))
            row = await cur.fetchone()
    except Exception:
        log.exception("attachment ingest: ref lookup failed")
        return None
    return int(row[0]) if row else None


def _handle(ref_id: int) -> str:
    from precis.utils import handle_registry

    return handle_registry.try_format("paper", ref_id) or f"pa{ref_id}"


def _download_sync(url: str, staged: Path) -> str:
    """Bounded safe_stream fetch to ``staged``; returns sha256 or raises."""
    from precis.workers.job_types import datasheet_pull as dl

    _final, sha = dl._download(url, staged)
    return sha


def _refusal(reason: str, detail: str) -> str:
    if reason == "too_large":
        from precis.workers.job_types import datasheet_pull as dl

        return f"Refused: the file is over the {dl.MAX_BYTES // (1024 * 1024)} MB cap."
    if reason == "not_pdf":
        return "Refused: the file does not start like a PDF (no `%PDF` header)."
    return f"Refused: could not fetch the attachment ({reason}). {detail[:120]}".strip()


async def ingest_attachment(
    att: object,
    *,
    message_id: int | str,
    abbrev: str,
    dsn: str,
    inbox: Path,
    call_tool: CallTool,
    send: Send,
    poll_interval_s: float | None = None,
    poll_timeout_s: float | None = None,
) -> None:
    """Fetch one PDF attachment into the inbox and reply with its ``pa<id>``."""
    poll_interval_s = POLL_INTERVAL_S if poll_interval_s is None else poll_interval_s
    poll_timeout_s = POLL_TIMEOUT_S if poll_timeout_s is None else poll_timeout_s
    from precis.workers.job_types import datasheet_pull as dl

    name = str(getattr(att, "filename", "") or "attachment")
    if not is_pdf_attachment(att):
        await send(f"Refused `{name}`: only PDF attachments are ingested.")
        return
    size = int(getattr(att, "size", 0) or 0)
    if size > dl.MAX_BYTES:
        await send(f"`{name}`: {_refusal('too_large', '')}")
        return

    target = inbox / _drop_name(att, message_id)
    staging = inbox / ".staging"
    staged = staging / target.name
    try:
        staging.mkdir(parents=True, exist_ok=True)
        if target.exists():
            await send(f"`{name}` is already queued for ingest.")
            return
        try:
            url = str(getattr(att, "url", ""))
            sha = await asyncio.to_thread(_download_sync, url, staged)
        except dl._Failed as exc:
            await send(f"`{name}`: {_refusal(exc.reason, exc.detail or '')}")
            return
        existing = await find_ref_id(dsn, sha)
        if existing is not None:
            await send(f"`{name}` is already in the library as {_handle(existing)}.")
            return
        os.replace(staged, target)
    except OSError as exc:
        log.exception("attachment ingest: inbox write failed")
        await send(f"`{name}`: could not write to the ingest inbox ({exc.strerror}).")
        return
    finally:
        staged.unlink(missing_ok=True)

    await send(f"`{name}` queued for ingest; I will post the paper handle when done.")
    waited = 0.0
    ref_id = await find_ref_id(dsn, sha)
    while ref_id is None and waited < poll_timeout_s:
        await asyncio.sleep(poll_interval_s)
        waited += poll_interval_s
        ref_id = await find_ref_id(dsn, sha)
    if ref_id is None:
        await send(
            f"`{name}`: no paper after {int(poll_timeout_s // 60)} min. It may "
            "still be in the inbox or have landed in the ingest errors folder."
        )
        return
    handle = _handle(ref_id)
    note = ""
    try:
        await call_tool(
            "tag", {"kind": "paper", "id": handle, "add": [f"user:{abbrev}"]}
        )
    except Exception:
        log.exception("attachment ingest: attribution tag failed for %s", handle)
        note = f" (could not tag it user:{abbrev})"
    await send(f"`{name}` ingested as {handle}{note}.")


async def handle_message_attachments(
    message: Any,
    *,
    abbrev: str | None,
    dsn: str,
    call_tool: CallTool,
    send: Send,
    inbox: Path | None,
) -> bool:
    """Act on a message's attachments; True when any attachment was handled.

    Non-PDF attachments on a message that carries text are left to the normal
    chat turn (a pasted screenshot is not an ingest request); on an
    attachment-only message they get a refusal reply instead of silence.
    """
    atts = list(getattr(message, "attachments", None) or [])
    if not atts:
        return False
    has_text = bool((getattr(message, "content", "") or "").strip())
    pdfs = [a for a in atts if is_pdf_attachment(a)]
    if not pdfs:
        if has_text:
            return False
        await send("Refused: only PDF attachments are ingested.")
        return True
    if not abbrev:
        await send(
            "Refused: link your Discord id to a precis account on `/account` "
            "before attaching papers."
        )
        return True
    if inbox is None:
        await send("Refused: no ingest inbox is configured on this host.")
        return True
    for att in pdfs:
        await ingest_attachment(
            att,
            message_id=message.id,
            abbrev=abbrev,
            dsn=dsn,
            inbox=inbox,
            call_tool=call_tool,
            send=send,
        )
    return True

"""List + fetch messages over IMAP, and parse them into typed rows.

Read-only browse (slice 2): folder listings (headers only) and single-message
fetch. Every SELECT is ``readonly=True`` and every FETCH uses ``BODY.PEEK`` so
browsing NEVER sets the ``\\Seen`` flag — reading mail in precis must not mark
it read in the real mailbox.

No persistence: IMAP is the source of truth (docs/design/email-kind.md). These
helpers fetch live each call; the summarization path (later slice) is what
promotes a chosen body into the chunk pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as default_policy
from typing import TYPE_CHECKING

from precis.mail.account import Account
from precis.mail.imap import _quote, _status_int, connect

if TYPE_CHECKING:
    from precis.store import Store

#: Default number of recent messages a folder listing returns.
DEFAULT_LIST_LIMIT = 25

_UID_RE = re.compile(rb"UID (\d+)")
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True, slots=True)
class MessageHeader:
    """One row in a folder listing — headers only, no body fetched."""

    uid: int
    from_: str
    subject: str
    date: str


@dataclass(frozen=True, slots=True)
class Message:
    """A single fetched message: headers + best-effort plain-text body."""

    uid: int
    folder: str
    from_: str
    to: str
    subject: str
    date: str
    body_text: str
    truncated_html: bool  # True when body came from stripped HTML (no text/plain)


@dataclass(frozen=True, slots=True)
class PollBatch:
    """New messages past a UID high-water, plus the folder's current UIDVALIDITY.

    ``uidvalidity`` lets the poller detect a mid-flight resync; ``messages`` are
    fully parsed (bodies fetched), UID-ascending, and strictly ``> since_uid``.
    """

    uidvalidity: int | None
    messages: list[Message]


def _decode_uids(raw: bytes | None) -> list[int]:
    if not raw:
        return []
    return [int(tok) for tok in raw.split() if tok.isdigit()]


def _iter_fetch_payloads(data: list) -> list[tuple[int, bytes]]:
    """Pull ``(uid, payload_bytes)`` pairs out of an imaplib FETCH response.

    imaplib returns a list where each fetched message is a tuple
    ``(b'<seq> (UID <n> BODY[...] {len}', b'<payload>')`` interleaved with
    bare ``b')'`` separators. The UID lives in the envelope half; the header
    or body bytes are the payload half.
    """
    out: list[tuple[int, bytes]] = []
    for item in data:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        envelope, payload = item[0], item[1]
        m = _UID_RE.search(envelope or b"")
        if m is None or not isinstance(payload, (bytes, bytearray)):
            continue
        out.append((int(m.group(1)), bytes(payload)))
    return out


def _header_str(msg: EmailMessage, name: str) -> str:
    """Read one header, already RFC 2047-decoded by the default policy."""
    val = msg.get(name)
    return "" if val is None else str(val).strip()


def list_recent(
    account: Account,
    *,
    store: Store,
    folder: str,
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[MessageHeader]:
    """Newest ``limit`` messages in ``folder`` — headers only, newest first."""
    parser = BytesParser(policy=default_policy)
    with connect(account, store=store) as conn:
        conn.select(_quote(folder), readonly=True)
        typ, data = conn.uid("SEARCH", "ALL")
        if typ != "OK":
            return []
        uids = _decode_uids(data[0] if data else None)
        if not uids:
            return []
        window = uids[-limit:]
        typ, fetched = conn.uid(
            "FETCH",
            ",".join(str(u) for u in window),
            "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])",
        )
        if typ != "OK":
            return []

    headers: list[MessageHeader] = []
    for uid, payload in _iter_fetch_payloads(fetched):
        msg = parser.parsebytes(payload)
        headers.append(
            MessageHeader(
                uid=uid,
                from_=_header_str(msg, "From"),
                subject=_header_str(msg, "Subject"),
                date=_header_str(msg, "Date"),
            )
        )
    # Newest first (IMAP UID order is ascending).
    headers.sort(key=lambda h: h.uid, reverse=True)
    return headers


def fetch_one(
    account: Account, *, store: Store, folder: str, uid: int
) -> Message | None:
    """Fetch and parse a single message by UID. ``None`` if it doesn't exist."""
    with connect(account, store=store) as conn:
        conn.select(_quote(folder), readonly=True)
        typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK":
            return None
        payloads = _iter_fetch_payloads(data)
    if not payloads:
        return None
    raw = payloads[0][1]
    return parse_message(raw, folder=folder, uid=uid)


#: Hard ceiling on messages fetched in one poll — a large backlog drains
#: forward over several ticks rather than pulling thousands of bodies at once.
DEFAULT_POLL_BATCH = 200


def fetch_new(
    account: Account,
    *,
    store: Store,
    folder: str,
    since_uid: int,
    limit: int = DEFAULT_POLL_BATCH,
) -> PollBatch:
    """Fetch messages with ``UID > since_uid`` in ``folder`` (bodies included).

    Oldest-first and capped at ``limit`` so a big backlog drains across ticks.
    Read-only + ``BODY.PEEK`` (never sets ``\\Seen``). The ``UID n:*`` search
    always includes the mailbox's highest UID even when ``n`` exceeds it (an
    IMAP quirk), so the result is re-filtered to strictly ``> since_uid``.
    """
    with connect(account, store=store) as conn:
        typ, _sel = conn.select(_quote(folder), readonly=True)
        if typ != "OK":
            return PollBatch(uidvalidity=None, messages=[])
        uidvalidity = _status_int(conn, folder, "UIDVALIDITY")
        typ, data = conn.uid("SEARCH", "UID", f"{since_uid + 1}:*")
        if typ != "OK":
            return PollBatch(uidvalidity=uidvalidity, messages=[])
        uids = sorted(
            u for u in _decode_uids(data[0] if data else None) if u > since_uid
        )
        uids = uids[:limit]
        if not uids:
            return PollBatch(uidvalidity=uidvalidity, messages=[])
        typ, fetched = conn.uid(
            "FETCH", ",".join(str(u) for u in uids), "(BODY.PEEK[])"
        )
        if typ != "OK":
            return PollBatch(uidvalidity=uidvalidity, messages=[])

    messages = [
        parse_message(payload, folder=folder, uid=uid)
        for uid, payload in _iter_fetch_payloads(fetched)
        if uid > since_uid
    ]
    messages.sort(key=lambda m: m.uid)
    return PollBatch(uidvalidity=uidvalidity, messages=messages)


def parse_message(raw: bytes, *, folder: str, uid: int) -> Message:
    """Parse RFC822 bytes into a :class:`Message` (pure — no IMAP)."""
    msg = BytesParser(policy=default_policy).parsebytes(raw)
    body_text, from_html = _extract_body(msg)
    return Message(
        uid=uid,
        folder=folder,
        from_=_header_str(msg, "From"),
        to=_header_str(msg, "To"),
        subject=_header_str(msg, "Subject"),
        date=_header_str(msg, "Date"),
        body_text=body_text,
        truncated_html=from_html,
    )


def _extract_body(msg: EmailMessage) -> tuple[str, bool]:
    """Best-effort plain-text body. Prefers text/plain; strips HTML otherwise.

    Returns ``(text, from_html)`` — ``from_html`` marks that we fell back to
    tag-stripped HTML (lossy) so the renderer can flag it.
    """
    try:
        part = msg.get_body(preferencelist=("plain",))
        if part is not None:
            return str(part.get_content()).strip(), False
        html_part = msg.get_body(preferencelist=("html",))
        if html_part is not None:
            return _strip_html(str(html_part.get_content())), True
    except (LookupError, ValueError, KeyError):
        pass
    # Non-multipart or undecodable → fall back to the raw payload.
    try:
        content = msg.get_content()
        return (str(content).strip(), False) if content else ("", False)
    except (LookupError, ValueError, KeyError):
        return "", False


def _strip_html(html: str) -> str:
    """Crude HTML→text: drop script/style, strip tags, collapse whitespace.

    Deliberately minimal (no new dependency). Good enough for a browse
    preview; the summarization path does richer extraction downstream.
    """
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p>", "\n\n", html)
    text = _TAG_RE.sub("", html)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()

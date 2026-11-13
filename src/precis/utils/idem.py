"""Blind-retry idempotency keys for ``put`` (gr658432).

An MCP ``put`` that stalls client-side ("server isn't responding") can
have committed anyway — the client can't tell a dropped write from a
slow one, so a blind retry risks double-filing. ``kind='job'`` already
carries ``idem_key`` (``meta.idem_key`` + a transaction-scoped advisory
lock); this module is the shared machinery for the numeric-ref kinds
(todo / gripe / memory / quest / …) that now accept the same kwarg.

The contract: ``put(kind=<k>, text=…, idem_key='<key>')`` mints at most
one live ref of that kind per key — a retry returns the existing ref's
id instead of a duplicate. The key is caller-chosen and scoped per kind;
a soft-deleted ref does not block a fresh mint. Comment-appending puts
(``put(id=N, text=…)``) dedupe at the chunk level instead — see
:mod:`precis.handlers._comment_timeline`.
"""

from __future__ import annotations

import hashlib
from typing import Any

from precis.errors import BadInput

#: Cap on a caller-supplied key — generous headroom over the longest
#: in-tree keys (``reextract:<ref>:<sha256>`` ≈ 90 chars); a longer
#: string smells like a pasted body, not a key.
MAX_KEY_LEN = 200


def idem_lock_key(idem: str) -> int:
    """Hash an idem string to a 64-bit signed integer for advisory lock.

    ``pg_advisory_xact_lock`` takes a ``bigint``. We hash via BLAKE2b
    truncated to 8 bytes and reinterpret as a signed int so the
    value fits Postgres's ``bigint`` range. Stable across processes
    so two workers racing the same idem key serialize correctly.
    The hash is not security-sensitive: collisions just cause two
    unrelated puts to share a lock briefly.
    """
    digest = hashlib.blake2b(idem.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def normalize_key(idem_key: Any, *, kind: str) -> str:
    """Validate and normalise a caller-supplied ``idem_key``.

    Raises :class:`BadInput` on a non-string, empty, or over-long value —
    a malformed key must refuse loudly, never silently dedupe on a
    mangled form.
    """
    if not isinstance(idem_key, str):
        raise BadInput(
            f"idem_key must be a string, got {idem_key!r}",
            next=(
                f"put(kind={kind!r}, text='...', idem_key='<stable key>') — "
                "a retry with the same key returns the first ref instead of "
                "minting a duplicate"
            ),
        )
    key = idem_key.strip()
    if not key:
        raise BadInput(
            "idem_key must be a non-empty string",
            next=f"put(kind={kind!r}, text='...', idem_key='<stable key>')",
        )
    if len(key) > MAX_KEY_LEN:
        raise BadInput(
            f"idem_key is {len(key)} chars (max {MAX_KEY_LEN}) — "
            "a key names the write, it does not carry its payload",
            next="use a short stable key, e.g. '<session>:<serial>'",
        )
    return key


def ref_id_for_key(conn: Any, kind: str, idem: str) -> int | None:
    """Newest live ``kind`` ref stamped ``meta.idem_key = idem``, else None.

    Runs on the caller's ``conn`` — inside the
    ``pg_advisory_xact_lock`` window when serialised minting matters.
    """
    row = conn.execute(
        "SELECT ref_id FROM refs "
        "WHERE kind = %s AND retired_at IS NULL AND meta->>'idem_key' = %s "
        "ORDER BY ref_id DESC LIMIT 1",
        (kind, idem),
    ).fetchone()
    return int(row[0]) if row else None


def key_on_ref(conn: Any, ref_id: int, idem: str) -> bool:
    """Whether ``ref_id`` already carries ``meta.idem_key = idem``.

    A create that stamps the key inside its own write boundary (gripe's
    ``file_gripe_readonly`` definer — the only insert a no-UPDATE role
    can make, migration 0319) must not be followed by the post-hoc
    ``update_ref`` stamp: that UPDATE is exactly what the role refuses.
    Read-only, so every scope can ask.
    """
    row = conn.execute(
        "SELECT 1 FROM refs WHERE ref_id = %s AND meta->>'idem_key' = %s",
        (ref_id, idem),
    ).fetchone()
    return row is not None


__all__ = [
    "MAX_KEY_LEN",
    "idem_lock_key",
    "key_on_ref",
    "normalize_key",
    "ref_id_for_key",
]

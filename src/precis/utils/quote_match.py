"""Shared verbatim quote matcher for imported drafts and finding promotion.

The normalized fifty-character prefix and fifteen-character minimum preserve
existing draft-import matching; this is deliberately not a fuzzy match.
"""

from __future__ import annotations

import re
from contextlib import nullcontext
from typing import Any


def _norm(s: str) -> str:
    """Lowercase, collapse whitespace, drop non-alphanumerics — for matching a
    verbatim quote against ingested paper text despite hyphenation/OCR drift."""
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (s or "").lower()))


def _match_quote(
    store: Any, ref_id: int, quote: str, cache: dict[int, list], *, conn: Any = None
) -> int | None:
    """Find the paper chunk whose text contains the verbatim quote and return
    its chunk_id (-> ``[pc<id>]``), or None. Matches a distinctive normalized
    prefix; the paper's chunks are cached per ref_id (papers are cited often)."""
    needle = _norm(quote).strip()[:50]
    if len(needle) < 15:
        return None
    chunks = cache.get(ref_id)
    if chunks is None:
        with nullcontext(conn) if conn is not None else store.pool.connection() as c:
            rows = c.execute(
                "SELECT chunk_id, text FROM chunks WHERE ref_id = %s AND ord >= 0 "
                "AND retired_at IS NULL ORDER BY ord",
                (ref_id,),
            ).fetchall()
        chunks = [(int(cid), _norm(txt)) for cid, txt in rows]
        cache[ref_id] = chunks
    for cid, hay in chunks:
        if needle in hay:
            return cid
    return None

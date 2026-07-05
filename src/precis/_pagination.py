"""Process-local pagination cache for MCP frame-size chunking.

The MCP stdio transport limits a single tool result to a frame
that's roughly 32 KiB on common clients. A ``get(kind='material')``
or ``search`` response large enough to overflow that frame would
fail to round-trip — handlers had no good way to detect or react.

This module gives the runtime a "chunk + cache" affordance: when
``dispatch_with_status`` builds a response larger than
``PRECIS_MAX_BODY_BYTES`` (default 24 KiB to leave headroom for
JSON framing), it asks ``PaginationCache.split`` to return the
head plus a cursor. The tail lives in this cache, keyed by cursor,
TTL-pruned. The agent retrieves the rest via the ``more`` MCP tool
which calls back into ``PaginationCache.pop``.

Splitting is textual, not structural — the renderer already emits
GitHub-flavoured Markdown, so we split on ``\\n## `` (H2 section)
boundaries. Sections fit inside a single chunk; only the boundary
between sections moves to the next page. Each page ends with a
loud ``more(cursor='...')`` footer so the agent knows the body is
incomplete and pagination is in flight.

The cache is per-process: a worker restart drops all cursors. The
agent's recovery is to re-issue the original call. Acceptable for
v1; revisit if cursor reuse across restarts becomes a real need.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from threading import Lock

log = logging.getLogger(__name__)

#: Soft cap on rendered body bytes before chunking kicks in. Set
#: to a value safely under the MCP stdio frame ceiling (typically
#: ~32 KiB) so JSON framing + envelope have room. Tunable via the
#: ``PRECIS_MAX_BODY_BYTES`` env var for clients that tolerate
#: bigger frames.
DEFAULT_MAX_BODY_BYTES = 24576

#: How long a cursor lives in the cache. The agent that received
#: the head has 5 minutes to ask for the tail before it expires.
#: Tunable via ``PRECIS_PAGINATION_TTL_S``.
DEFAULT_TTL_SECONDS = 300

#: Hard ceiling on number of pending cursors. Prevents an
#: adversarial caller from filling RAM with truncated bodies.
DEFAULT_MAX_CURSORS = 256

#: Marker the agent sees at the bottom of a chunked head. The
#: footer is appended *inside* the body (it's not metadata) so
#: existing rendering / logging paths see the pagination hint
#: without protocol changes.
#:
#: Deliberately loud: a terse "Next: more(...)" hint reads as
#: trailing noise, and consumers were treating a first-frame head
#: as a complete result and acting on it (e.g. a long YouTube
#: transcript summarised as if it ended mid-sentence). The footer
#: now states, in order: that the body is *incomplete*, roughly how
#: much remains, the exact call to continue, and that the reader
#: must drain every page before acting. ``{cursor}`` and the literal
#: ``more(cursor='...')`` call are preserved for the ``more`` tool.
_FOOTER_TEMPLATE = (
    "\n\n---\n"
    "⚠️ **Truncated — this is NOT the complete result.** It was cut to fit the "
    "response frame; about {remaining} more follows on the next page. Call "
    "`more(cursor='{cursor}')` to fetch it, then keep following each page's "
    "cursor until no footer remains — do not summarise, quote, or act on this "
    "content until you have drained every page.\n"
)


def _human_bytes(n: int) -> str:
    """Render a byte count as a compact human-readable size.

    Bounded width by construction (it steps up to KB / MB), so it
    is safe to use in the footer-reserve upper bound below.
    """
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _max_body_bytes() -> int:
    """Resolve the soft cap from env at call time.

    Read on every check rather than once at import so operators
    can tune the cap without a process restart. The cost is a dict
    lookup per response — negligible against the rendering cost.
    """
    raw = os.environ.get("PRECIS_MAX_BODY_BYTES")
    if not raw:
        return DEFAULT_MAX_BODY_BYTES
    try:
        value = int(raw)
    except ValueError:
        log.warning(
            "PRECIS_MAX_BODY_BYTES=%r is not an int; using default",
            raw,
        )
        return DEFAULT_MAX_BODY_BYTES
    if value <= 0:
        return DEFAULT_MAX_BODY_BYTES
    return value


def _ttl_seconds() -> float:
    """Resolve cursor TTL from env at call time."""
    raw = os.environ.get("PRECIS_PAGINATION_TTL_S")
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "PRECIS_PAGINATION_TTL_S=%r is not a float; using default",
            raw,
        )
        return DEFAULT_TTL_SECONDS
    if value <= 0:
        return DEFAULT_TTL_SECONDS
    return value


@dataclass(frozen=True, slots=True)
class _CachedTail:
    """One pending tail keyed by cursor.

    Holds the remaining body plus expiry. Kept private — callers
    interact via :class:`PaginationCache`'s public methods.
    """

    body: str
    expires_at: float


class PaginationCache:
    """Thread-safe pagination cache for chunked responses.

    Constructed once per :class:`PrecisRuntime`; the runtime calls
    :meth:`split` on every outbound body. The ``more`` MCP tool
    calls :meth:`pop` to retrieve the next page.

    Eviction policy: TTL pruned on every operation. If the
    cursor-count ceiling is hit, the oldest entries (smallest
    ``expires_at``) drop first.
    """

    def __init__(
        self,
        *,
        max_cursors: int = DEFAULT_MAX_CURSORS,
    ) -> None:
        self._entries: dict[str, _CachedTail] = {}
        self._lock = Lock()
        self._max_cursors = max_cursors

    def _now(self) -> float:
        return time.monotonic()

    def _prune_expired(self) -> None:
        """Drop expired entries. Caller must hold ``self._lock``."""
        now = self._now()
        expired = [c for c, e in self._entries.items() if e.expires_at <= now]
        for cursor in expired:
            self._entries.pop(cursor, None)

    def _maybe_evict_oldest(self) -> None:
        """Drop the oldest entry if we're over the cursor ceiling.

        Caller must hold ``self._lock``. Cheap O(n) scan; n is at
        most :data:`DEFAULT_MAX_CURSORS`.
        """
        if len(self._entries) < self._max_cursors:
            return
        oldest = min(self._entries.items(), key=lambda item: item[1].expires_at)
        self._entries.pop(oldest[0], None)
        log.info(
            "pagination cache full; evicted oldest cursor %r",
            oldest[0],
        )

    def split(self, body: str) -> tuple[str, str | None]:
        """Split ``body`` into a head + cached tail if oversized.

        Returns ``(head, cursor)``. When ``body`` fits inside
        :func:`_max_body_bytes`, returns ``(body, None)`` and
        nothing is cached. When ``body`` is too large, returns
        ``(head_with_footer, cursor)`` and caches the remaining
        text under ``cursor``.

        Splitting is greedy: take the largest run of sections that
        fits the limit (with footer-space reserved), keep the rest
        for the next page. Sections shorter than the limit go to
        one page; a single section longer than the limit falls
        through to a paragraph split, then a hard byte split.
        """
        cap = _max_body_bytes()
        if len(body.encode("utf-8")) <= cap:
            return body, None

        head, tail = _greedy_split(body, cap)
        if not tail:
            # Body fits after all (multi-byte UTF-8 made the
            # initial check pessimistic). No cursor needed.
            return body, None

        cursor = uuid.uuid4().hex
        remaining = _human_bytes(len(tail.encode("utf-8")))
        footer = _FOOTER_TEMPLATE.format(cursor=cursor, remaining=remaining)
        head_with_footer = head + footer

        with self._lock:
            self._prune_expired()
            self._maybe_evict_oldest()
            self._entries[cursor] = _CachedTail(
                body=tail,
                expires_at=self._now() + _ttl_seconds(),
            )
        return head_with_footer, cursor

    def pop(self, cursor: str) -> str | None:
        """Retrieve and remove the tail for ``cursor``.

        Returns the cached tail, possibly re-split if it's still
        too big (recursive cursor — the new cursor is in the
        body's footer). Returns ``None`` when the cursor is
        unknown or expired; the ``more`` tool surfaces that as a
        clean error to the agent.

        Pops the entry: a cursor is single-use. The agent that
        needs to re-read must hold onto the body it received.
        """
        with self._lock:
            self._prune_expired()
            entry = self._entries.pop(cursor, None)
        if entry is None:
            return None
        if entry.expires_at <= self._now():
            return None
        # Recursive split: the tail may itself overflow.
        head, _maybe_next_cursor = self.split(entry.body)
        return head

    def __len__(self) -> int:
        """Number of pending cursors. Useful for tests/diagnostics."""
        with self._lock:
            self._prune_expired()
            return len(self._entries)


# ── Splitting helpers ──────────────────────────────────────────────


_SECTION_DELIMITER = "\n## "
_PARAGRAPH_DELIMITER = "\n\n"
#: Footer space we keep in reserve when picking the head's byte
#: budget so ``head + footer`` stays under the frame cap. Derived
#: from the template itself — rendered with a full-width cursor and
#: a generous ``remaining`` token — so it self-corrects whenever the
#: footer wording changes and can never silently under-reserve. The
#: ``remaining`` readout is bounded-width by ``_human_bytes`` (it
#: steps up to KB/MB), so ``"8888.8 MB"`` is a safe upper bound.
_FOOTER_RESERVE_BYTES = len(
    _FOOTER_TEMPLATE.format(cursor="f" * 32, remaining="8888.8 MB").encode("utf-8")
)


def _greedy_split(body: str, cap_bytes: int) -> tuple[str, str]:
    """Return ``(head, tail)`` such that head fits inside ``cap_bytes``.

    Strategy:
    1. Try ``\\n## `` (H2 section) boundaries first — preserves the
       rendered hierarchy.
    2. Fall back to paragraph boundaries (``\\n\\n``) when one
       section alone exceeds the cap.
    3. Last resort: hard-cut on a UTF-8 char boundary.
    """
    # Reserve some bytes for the ``more(cursor='...')`` footer;
    # the rest is available to the head. For very small caps the
    # reserve can dominate — clamp to a minimum of 1 byte for the
    # head budget so the chunker still makes forward progress.
    budget = max(cap_bytes - _FOOTER_RESERVE_BYTES, 1)

    head, tail = _split_on_delimiter(body, _SECTION_DELIMITER, budget)
    if head and tail:
        return head, tail

    head, tail = _split_on_delimiter(body, _PARAGRAPH_DELIMITER, budget)
    if head and tail:
        return head, tail

    return _hard_split(body, budget)


def _split_on_delimiter(
    body: str, delimiter: str, budget_bytes: int
) -> tuple[str, str]:
    """Greedy delimiter split. Returns ``("", "")`` when impossible.

    A successful split returns a non-empty head AND non-empty tail —
    that's the signal to the caller that this delimiter level
    worked. When no boundary fits inside ``budget_bytes`` (either
    the first piece is already too big, or there's no delimiter at
    all), returns two empty strings.
    """
    if not body or delimiter not in body:
        return "", ""

    parts = body.split(delimiter)
    head = parts[0]
    if len(head.encode("utf-8")) > budget_bytes:
        # First section already exceeds the budget — caller falls
        # through to a finer split.
        return "", ""

    accepted_parts = [head]
    consumed_bytes = len(head.encode("utf-8"))
    cursor = 1
    while cursor < len(parts):
        next_piece = delimiter + parts[cursor]
        next_size = len(next_piece.encode("utf-8"))
        if consumed_bytes + next_size > budget_bytes:
            break
        accepted_parts.append(next_piece)
        consumed_bytes += next_size
        cursor += 1

    if cursor >= len(parts):
        # The whole body fit. No tail — caller's outer check
        # already verified body exceeds the cap, so this is a
        # pathological case (multi-byte UTF-8 made the initial
        # encode-check pessimistic). Return empty tail; caller
        # treats it as "body fits after all".
        return body, ""

    head_text = "".join(accepted_parts)
    # The tail's first piece is ``parts[cursor]`` without the
    # leading delimiter — we want the delimiter at the START of
    # the tail so the next call re-finds the boundary.
    tail_pieces = [delimiter.lstrip("\n") + parts[cursor], *parts[cursor + 1 :]]
    tail_text = delimiter.join(tail_pieces)
    # Reinstate the leading newline so the tail starts with
    # ``## `` cleanly rather than mid-newline.
    if not tail_text.startswith("##"):
        tail_text = "## " + parts[cursor]
        if cursor + 1 < len(parts):
            tail_text += delimiter + delimiter.join(parts[cursor + 1 :])
    return head_text, tail_text


def _hard_split(body: str, budget_bytes: int) -> tuple[str, str]:
    """Byte-budget split that respects UTF-8 character boundaries.

    Last-resort fallback when no delimiter-based split works. We
    pick the largest valid UTF-8 prefix under ``budget_bytes`` and
    let the tail carry the rest.
    """
    raw = body.encode("utf-8")
    if len(raw) <= budget_bytes:
        return body, ""

    cut = budget_bytes
    # Walk back until ``raw[cut]`` is a start-of-codepoint byte.
    # In UTF-8 continuation bytes have the bit pattern 10xxxxxx
    # (i.e. byte & 0xC0 == 0x80); decoding partway through one of
    # those would raise UnicodeDecodeError.
    while cut > 0 and (raw[cut] & 0xC0) == 0x80:
        cut -= 1
    head = raw[:cut].decode("utf-8", errors="strict")
    tail = raw[cut:].decode("utf-8", errors="strict")
    return head, tail


__all__ = [
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_TTL_SECONDS",
    "PaginationCache",
]

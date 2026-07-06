"""Split a long outbound message into Discord-sized parts.

Discord rejects (or the delivery layer truncates) any single message body
over 2000 characters. asa_bot posts every ``message`` ref **verbatim** — it
does not chunk — so a long briefing was cut off mid-URL, losing the tail of
the digest (gr51155). The fix is upstream: split the body here, on safe
boundaries, into parts that each fit, and queue each part as its own
``message`` ref.

The split prefers, in order:

1. paragraph / line boundaries — never break a line (and therefore never a
   markdown link) across parts when the line itself fits a part;
2. word boundaries — for a single line longer than the whole budget, break
   on spaces so a URL (a space-free token) stays intact;
3. a hard character cut — only for a single token longer than the budget
   (pathological; a URL never reaches 2000 chars).
"""

from __future__ import annotations

#: Discord's hard per-message content limit.
DISCORD_MAX_CHARS = 2000

#: Default budget — a hair under the hard limit to leave room for any
#: transport-side framing and to stay clear of off-by-one truncation.
DEFAULT_LIMIT = 1990


def _split_long_line(line: str, limit: int) -> list[str]:
    """Break a single over-long line on word boundaries, hard-cutting only a
    single token that itself exceeds ``limit`` (keeps URLs whole)."""
    pieces: list[str] = []
    cur = ""
    for word in line.split(" "):
        if len(word) > limit:
            # Pathological token (never a real URL at these sizes) — hard-cut.
            if cur:
                pieces.append(cur)
                cur = ""
            for i in range(0, len(word), limit):
                pieces.append(word[i : i + limit])
            continue
        candidate = word if not cur else f"{cur} {word}"
        if len(candidate) > limit:
            pieces.append(cur)
            cur = word
        else:
            cur = candidate
    if cur:
        pieces.append(cur)
    return pieces


def split_message(text: str, limit: int = DEFAULT_LIMIT) -> list[str]:
    """Split ``text`` into parts each ``<= limit`` characters.

    Splits on line boundaries first (so markdown links and headings stay
    intact), falling back to word boundaries and finally a hard cut only for a
    single line/token longer than ``limit``. Returns ``[]`` for empty input and
    ``[text]`` when it already fits.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    cur: list[str] = []

    def flush() -> None:
        if cur:
            joined = "\n".join(cur).strip()
            if joined:
                parts.append(joined)
        cur.clear()

    for line in text.split("\n"):
        if len(line) > limit:
            # No line boundary will help — emit what we have, then break the
            # over-long line on words.
            flush()
            parts.extend(_split_long_line(line, limit))
            continue
        cur_len = sum(len(x) for x in cur) + len(cur)  # +1 join newline each
        if cur and cur_len + len(line) > limit:
            flush()
        cur.append(line)
    flush()
    return parts


__all__ = ["DEFAULT_LIMIT", "DISCORD_MAX_CHARS", "split_message"]

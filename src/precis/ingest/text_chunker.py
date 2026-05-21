"""Recursive character text splitter for document chunking.

Splits text into chunks of roughly ``chunk_size`` characters, preferring
to break at natural boundaries (paragraphs → newlines → sentences → words).
Adjacent chunks overlap by ``chunk_overlap`` characters to preserve context
across chunk boundaries.

Two structure-aware helpers sit on top:

- :func:`split_table` — markdown-table-aware splitter that keeps the
  header row(s) as context prepended to each chunk so each row group
  remains interpretable.
- :func:`enforce_hard_max` — fallback safety net: any chunk still over
  ``hard_max`` chars after type-specific splitting is force-split via
  :func:`split_text`. This guards downstream embedders (bge-m3 caps at
  8192 tokens, ~32K chars best-case; corrupted OCR can blow that with
  fragmented multi-page tables).
"""

from __future__ import annotations

import re

# Default separators, tried in order (prefer paragraph → line → sentence → word)
DEFAULT_SEPARATORS: list[str] = ["\n\n", "\n", ". ", ", ", " "]

# Reasonable defaults for academic papers
DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 150

# Hard ceiling: any block over this size *must* be split before being
# handed to an embedder. Sized to stay safely under bge-m3's 8192-token
# cap even for fragmented OCR (~2 chars/token worst case).
DEFAULT_HARD_MAX_CHARS = 16_000

# Tables are denser than prose — a single row is short, but keeping the
# header + ~10–20 rows together helps retrieval. Default to 2× the
# prose chunk size.
DEFAULT_TABLE_CHUNK_SIZE = DEFAULT_CHUNK_SIZE * 2

# Markdown table separator row: `|---|:---:|---|` — at least one cell
# of dashes (with optional alignment colons).
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


def split_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    separators: list[str] | None = None,
) -> list[str]:
    """Split *text* into chunks of approximately *chunk_size* characters.

    The algorithm tries each separator in order.  For the first separator
    that produces pieces, it keeps pieces that fit and recursively splits
    those that don't (using the remaining separators).  Adjacent chunks
    share *chunk_overlap* characters of context.

    Returns a list of non-empty strings, each ≤ ``chunk_size`` chars
    (unless a single word exceeds the limit, in which case it is kept
    whole to avoid mid-word splits).
    """
    if not text.strip():
        return []

    if len(text) <= chunk_size:
        return [text.strip()]

    seps = separators if separators is not None else list(DEFAULT_SEPARATORS)

    return _recursive_split(text, chunk_size, chunk_overlap, seps)


def _recursive_split(
    text: str,
    chunk_size: int,
    chunk_overlap: int,
    separators: list[str],
) -> list[str]:
    """Core recursive splitting logic."""
    # Base case: text fits
    if len(text) <= chunk_size:
        stripped = text.strip()
        return [stripped] if stripped else []

    # Try each separator
    for i, sep in enumerate(separators):
        pieces = _split_keeping_sep(text, sep)
        if len(pieces) <= 1:
            continue  # separator not found; try next

        # Merge small pieces back together up to chunk_size
        merged = _merge_pieces(pieces, chunk_size, chunk_overlap, sep)

        # Recursively split any chunk that's still too big
        remaining_seps = separators[i + 1 :]
        result: list[str] = []
        for chunk in merged:
            if len(chunk) <= chunk_size:
                stripped = chunk.strip()
                if stripped:
                    result.append(stripped)
            elif remaining_seps:
                result.extend(
                    _recursive_split(chunk, chunk_size, chunk_overlap, remaining_seps)
                )
            else:
                # No more separators — keep as-is (won't split mid-word)
                stripped = chunk.strip()
                if stripped:
                    result.append(stripped)
        return result

    # No separator worked — return text as-is
    stripped = text.strip()
    return [stripped] if stripped else []


def _split_keeping_sep(text: str, sep: str) -> list[str]:
    """Split text by *sep*, keeping the separator at the start of each piece
    (except the first)."""
    parts = text.split(sep)
    if len(parts) <= 1:
        return parts

    result = [parts[0]]
    for part in parts[1:]:
        result.append(sep + part)
    return result


def _merge_pieces(
    pieces: list[str],
    chunk_size: int,
    chunk_overlap: int,
    sep: str,
) -> list[str]:
    """Greedily merge adjacent pieces into chunks up to *chunk_size*.

    When starting a new chunk, includes up to *chunk_overlap* characters
    from the tail of the previous chunk.
    """
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for piece in pieces:
        piece_len = len(piece)

        if current and current_len + piece_len > chunk_size:
            # Flush current buffer
            chunk_text = "".join(current)
            if chunk_text.strip():
                chunks.append(chunk_text)

            # Build overlap from end of current buffer
            overlap_pieces: list[str] = []
            overlap_len = 0
            for prev in reversed(current):
                if overlap_len + len(prev) > chunk_overlap:
                    break
                overlap_pieces.insert(0, prev)
                overlap_len += len(prev)

            current = overlap_pieces
            current_len = overlap_len

        current.append(piece)
        current_len += piece_len

    # Flush remaining
    if current:
        chunk_text = "".join(current)
        if chunk_text.strip():
            chunks.append(chunk_text)

    return chunks


# ---------------------------------------------------------------------------
# Structure-aware splitters
# ---------------------------------------------------------------------------


def split_table(
    text: str,
    chunk_size: int = DEFAULT_TABLE_CHUNK_SIZE,
    *,
    hard_max: int = DEFAULT_HARD_MAX_CHARS,
) -> list[str]:
    """Split a markdown table into chunks, preserving the header row(s).

    A markdown table looks like::

        | col A | col B | col C |
        |-------|-------|-------|
        | r1a   | r1b   | r1c   |
        | r2a   | r2b   | r2c   |
        ...

    The header (typically rows 1–2: title row + alignment separator) is
    detected and **prepended to every output chunk** so each chunk is
    self-describing for retrieval.

    Falls back to :func:`split_text` if no header pattern is detected,
    or if the table is corrupted (single oversized row with no
    newlines, common in multi-page Marker-OCR'd tables).

    Always returns chunks ≤ ``hard_max`` chars by running every result
    through the hard-ceiling guard. This preserves the table-aware
    structure while still defending downstream embedders.
    """
    if not text.strip():
        return []
    if len(text) <= chunk_size:
        return [text.strip()]

    lines = text.splitlines()

    # Single corrupted "row" with no newlines (e.g., 192K-char OCR
    # garbage) — table-aware split can't help. Fall back to plain
    # splitter under hard_max.
    if len(lines) <= 1:
        return enforce_hard_max([text], hard_max=hard_max)

    # Detect header: row 0 looks like a table row, row 1 is the
    # alignment separator. If row 1 doesn't match, treat row 0 alone
    # as the header (some tables have no separator row).
    header_lines: list[str]
    body_start: int
    if len(lines) >= 2 and _TABLE_SEPARATOR_RE.match(lines[1]):
        header_lines = lines[:2]
        body_start = 2
    else:
        header_lines = lines[:1]
        body_start = 1

    header = "\n".join(header_lines)
    header_size = len(header) + 1  # +1 for trailing newline

    # If header alone is already over chunk_size, the table format
    # is degenerate — fall back to plain splitting under hard_max.
    if header_size >= chunk_size:
        return enforce_hard_max([text], hard_max=hard_max)

    body_rows = lines[body_start:]
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0

    for row in body_rows:
        row_size = len(row) + 1  # +1 for newline join
        # Single row larger than the chunk budget: emit current, then
        # emit this oversized row alone with the header (will be
        # caught by enforce_hard_max afterwards).
        if row_size > chunk_size - header_size:
            if current:
                chunks.append(header + "\n" + "\n".join(current))
                current = []
                current_size = 0
            chunks.append(header + "\n" + row)
            continue

        if current and (header_size + current_size + row_size) > chunk_size:
            chunks.append(header + "\n" + "\n".join(current))
            current = [row]
            current_size = row_size
        else:
            current.append(row)
            current_size += row_size

    if current:
        chunks.append(header + "\n" + "\n".join(current))

    # Apply hard-ceiling guard so any pathological row is force-split.
    return enforce_hard_max(chunks, hard_max=hard_max)


def enforce_hard_max(
    chunks: list[str],
    hard_max: int = DEFAULT_HARD_MAX_CHARS,
) -> list[str]:
    """Final safety net: force-split any chunk over ``hard_max`` chars.

    Runs each oversized chunk through :func:`split_text` with
    ``chunk_size=hard_max`` and a 5%-of-hard-max overlap. Always
    returns chunks ≤ ``hard_max`` chars (modulo single un-splittable
    long words, which is the same edge case :func:`split_text` has).

    This is the last line of defense for downstream embedders that
    can't tolerate arbitrary block sizes — any structure-aware
    splitter (text/list/table) should call this on its output.
    """
    if hard_max <= 0:
        raise ValueError("hard_max must be positive")
    overlap = max(1, hard_max // 20)
    out: list[str] = []
    for chunk in chunks:
        if len(chunk) <= hard_max:
            stripped = chunk.strip()
            if stripped:
                out.append(stripped)
        else:
            out.extend(split_text(chunk, chunk_size=hard_max, chunk_overlap=overlap))
    return out

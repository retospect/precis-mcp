"""``anchors`` / ``anchor_resolutions`` — the one relocatable-anchor store (gr739606).

Mixin on :class:`precis.store.Store`. Migration ``0360_anchors.sql`` defines
the tables. An anchor is a pointer into text (ref, chunk, char range),
code (repo, symbol path, line range), or a structured ref (kind, selector); the anchor
row is immutable and its current location lives in ``anchor_resolutions``.

Each target type uses the same resolve ladder:

1. the fingerprint still matches (text: chunk content sha; code: commit
   sha) -> the stored position, state ``ok``;
2. else the quote is searched verbatim in the ref or repository: exactly one
   hit -> ``ok`` if it sits at the stored position, else ``moved``;
   several hits -> ``stale`` (method ``ambiguous``), never a guess;
3. else ``stale``.

Embedding search may propose candidates elsewhere; nothing here re-points
an anchor from a similarity score.

Mint doors: a text anchor takes a caller-supplied range, or just a quote
that the server locates (it must be unique in the chunk); a supplied range
is checked against the quote. The handle is ``at<id>`` (text),
``ac<id>`` (code), or ``ap<id>`` (structured) — see :mod:`precis.utils.handle_registry`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from precis.errors import BadInput
from precis.utils.handle_registry import parse_anchor

TargetType = Literal["text", "code", "part"]
State = Literal["ok", "moved", "stale"]
Method = Literal["fingerprint", "verbatim", "ambiguous", "none"]


@dataclass(frozen=True)
class Anchor:
    """One immutable ``anchors`` row. Text anchors fill ref/chunk/char
    fields; code anchors fill repo/symbol/line fields."""

    anchor_id: int
    target_type: TargetType
    quote: str
    fingerprint: str
    observed_at: datetime | None = None
    note: str | None = None
    ref_id: int | None = None
    chunk_id: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    repo: str | None = None
    symbol_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    part_kind: str | None = None
    selector: str | None = None


@dataclass(frozen=True)
class Resolution:
    """Where an anchor is now. ``stale`` carries no position."""

    state: State
    method: Method
    chunk_id: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    symbol_path: str | None = None
    selector: str | None = None


@dataclass(frozen=True)
class ChunkText:
    """A chunk of the anchored ref as it is now."""

    chunk_id: int
    text: str
    content_sha: str | None = None


def text_fingerprint(text: str, content_sha: str | None) -> str:
    """A chunk's fingerprint: its ``content_sha``, else a sha256 of the text
    (the one fallback, used at mint and at resolve alike)."""
    return content_sha or hashlib.sha256(text.encode()).hexdigest()


def _find_all(haystack: str, needle: str) -> list[int]:
    out: list[int] = []
    i = haystack.find(needle)
    while i != -1:
        out.append(i)
        i = haystack.find(needle, i + 1)
    return out


_STALE = Resolution("stale", "none")


def resolve_text(anchor: Anchor, chunks: Sequence[ChunkText]) -> Resolution:
    """Run the ladder for a text anchor against the ref's current chunks."""
    assert anchor.chunk_id is not None
    assert anchor.char_start is not None and anchor.char_end is not None
    own = next((c for c in chunks if c.chunk_id == anchor.chunk_id), None)
    if own is not None and (
        text_fingerprint(own.text, own.content_sha) == anchor.fingerprint
    ):
        return Resolution(
            "ok",
            "fingerprint",
            anchor.chunk_id,
            anchor.char_start,
            anchor.char_end,
        )
    hits = [(c.chunk_id, i) for c in chunks for i in _find_all(c.text, anchor.quote)]
    if len(hits) > 1:
        return Resolution("stale", "ambiguous")
    if not hits:
        return _STALE
    chunk_id, start = hits[0]
    end = start + len(anchor.quote)
    same = (chunk_id, start, end) == (
        anchor.chunk_id,
        anchor.char_start,
        anchor.char_end,
    )
    return Resolution("ok" if same else "moved", "verbatim", chunk_id, start, end)


def resolve_code(
    anchor: Anchor,
    head_sha: str | None,
    sources: Mapping[str, Sequence[str]] | Sequence[str] | None,
    *,
    locate_symbol: Callable[[str, int], str] | None = None,
    other_locations: Sequence[tuple[str, int]] = (),
) -> Resolution:
    """Resolve against whole-line sources keyed by file or symbol path."""
    assert anchor.line_start is not None and anchor.line_end is not None
    assert anchor.symbol_path is not None
    if sources is None:
        return _STALE
    if not isinstance(sources, Mapping):
        sources = {anchor.symbol_path: sources}
    if head_sha == anchor.fingerprint and anchor.symbol_path in sources:
        return Resolution(
            "ok",
            "fingerprint",
            line_start=anchor.line_start,
            line_end=anchor.line_end,
            symbol_path=anchor.symbol_path,
        )
    want = anchor.quote.split("\n")
    length = len(want)
    if len(other_locations) > 1:
        return Resolution("stale", "ambiguous")
    hit = next(iter(other_locations), None)
    for path, lines in sources.items():
        for index in range(len(lines) - length + 1):
            if list(lines[index : index + length]) != want:
                continue
            if hit is not None:
                return Resolution("stale", "ambiguous")
            hit = (path, index + 1)
    if hit is None:
        return _STALE
    path, start = hit
    if locate_symbol is not None:
        path = locate_symbol(path, start)
    end = start + length - 1
    same = (path, start, end) == (
        anchor.symbol_path,
        anchor.line_start,
        anchor.line_end,
    )
    return Resolution(
        "ok" if same else "moved",
        "verbatim",
        line_start=start,
        line_end=end,
        symbol_path=path,
    )


_ANCHOR_COLS = (
    "anchor_id, target_type, quote, fingerprint, observed_at, note, ref_id, "
    "chunk_id, char_start, char_end, repo, symbol_path, line_start, line_end, "
    "part_kind, selector"
)


class AnchorsMixin:
    """Mixin: assumes the concrete Store provides ``self.pool``."""

    pool: Any

    def mint_text_anchor(
        self,
        ref_id: int,
        chunk_id: int,
        *,
        quote: str | None = None,
        char_start: int | None = None,
        char_end: int | None = None,
        note: str | None = None,
        conn: Any = None,
    ) -> Anchor:
        """Mint a text anchor on a chunk. Give the range, the quote, or both
        (a range is verified against the quote; a bare quote must occur
        exactly once in the chunk)."""
        with self.pool.connection() if conn is None else nullcontext(conn) as conn:
            row = conn.execute(
                "SELECT text, content_sha FROM chunks "
                "WHERE chunk_id = %s AND ref_id = %s AND retired_at IS NULL",
                (chunk_id, ref_id),
            ).fetchone()
            if row is None:
                raise BadInput(f"chunk {chunk_id} is not a live chunk of ref {ref_id}")
            text, content_sha = row
            if char_start is None and char_end is None:
                if not quote:
                    raise BadInput("give a quote, a char range, or both")
                hits = _find_all(text, quote)
                if len(hits) != 1:
                    raise BadInput(
                        f"quote occurs {len(hits)} times in the chunk; "
                        "give char_start/char_end to pick one"
                    )
                char_start = hits[0]
                char_end = char_start + len(quote)
            elif char_start is None or char_end is None:
                raise BadInput("char_start and char_end go together")
            if not 0 <= char_start < char_end <= len(text):
                raise BadInput(
                    f"char range [{char_start}, {char_end}) is outside the "
                    f"chunk text (length {len(text)})"
                )
            seen = text[char_start:char_end]
            if quote is not None and quote != seen:
                raise BadInput("quote does not match the text at that char range")
            got = conn.execute(
                f"INSERT INTO anchors (target_type, quote, fingerprint, note, "
                f"ref_id, chunk_id, char_start, char_end) "
                f"VALUES ('text', %s, %s, %s, %s, %s, %s, %s) "
                f"RETURNING {_ANCHOR_COLS}",
                (
                    seen,
                    text_fingerprint(text, content_sha),
                    note,
                    ref_id,
                    chunk_id,
                    char_start,
                    char_end,
                ),
            ).fetchone()
        return Anchor(*got)

    def mint_code_anchor(
        self,
        repo: str,
        symbol_path: str,
        line_start: int,
        line_end: int,
        *,
        quote: str,
        commit_sha: str,
        note: str | None = None,
        conn: Any = None,
    ) -> Anchor:
        """Mint a code anchor: the quoted lines as read at ``commit_sha``."""
        if not (1 <= line_start <= line_end):
            raise BadInput("line range must satisfy 1 <= line_start <= line_end")
        if len(quote.split("\n")) != line_end - line_start + 1:
            raise BadInput("quote must hold exactly the lines of the range")
        with self.pool.connection() if conn is None else nullcontext(conn) as conn:
            got = conn.execute(
                f"INSERT INTO anchors (target_type, quote, fingerprint, note, "
                f"repo, symbol_path, line_start, line_end) "
                f"VALUES ('code', %s, %s, %s, %s, %s, %s, %s) "
                f"RETURNING {_ANCHOR_COLS}",
                (quote, commit_sha, note, repo, symbol_path, line_start, line_end),
            ).fetchone()
        return Anchor(*got)

    def mint_part_anchor(
        self,
        ref_id: int,
        part_kind: str,
        selector: str,
        *,
        quote: str,
        fingerprint: str,
        note: str | None = None,
        conn: Any = None,
    ) -> Anchor:
        """Record a selector and exact quote observed by a structured-ref reader."""
        if part_kind not in {"se", "structure", "pathway", "pcb"}:
            raise BadInput(
                "unknown structured anchor kind",
                next="choose se, structure, pathway or pcb",
            )
        if not selector or not quote or not fingerprint:
            raise BadInput(
                "structured anchors need a selector, quote and fingerprint",
                next="supply the selector and exact observation from the structured ref",
            )
        with self.pool.connection() if conn is None else nullcontext(conn) as conn:
            row = conn.execute(
                f"INSERT INTO anchors (target_type, ref_id, part_kind, selector, "
                f"quote, fingerprint, note) VALUES ('part', %s, %s, %s, %s, %s, %s) "
                f"RETURNING {_ANCHOR_COLS}",
                (ref_id, part_kind, selector, quote, fingerprint, note),
            ).fetchone()
        return Anchor(*row)

    def relocate_part_anchor(
        self,
        anchor_id: int,
        fingerprint: str,
        parts: Mapping[str, str],
    ) -> Resolution:
        """Resolve against the kind adapter's current selectors and exact observations."""
        anchor = self.get_anchor(anchor_id)
        if anchor is None or anchor.target_type != "part":
            raise BadInput(
                f"ap{anchor_id} is not a structured anchor",
                next="use a structured anchor ID",
            )
        assert anchor.selector is not None
        if (
            fingerprint == anchor.fingerprint
            and parts.get(anchor.selector) == anchor.quote
        ):
            result = Resolution("ok", "fingerprint", selector=anchor.selector)
        else:
            hits = [
                selector for selector, quote in parts.items() if quote == anchor.quote
            ]
            if len(hits) > 1:
                result = Resolution("stale", "ambiguous")
            elif hits:
                state: State = "ok" if hits[0] == anchor.selector else "moved"
                result = Resolution(state, "verbatim", selector=hits[0])
            else:
                result = _STALE
        self.save_anchor_resolution(anchor_id, result)
        return result

    def get_anchor(self, anchor_id: int) -> Anchor | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_ANCHOR_COLS} FROM anchors WHERE anchor_id = %s",
                (anchor_id,),
            ).fetchone()
        return Anchor(*row) if row else None

    def get_anchor_by_handle(self, handle: str) -> Anchor | None:
        parsed = parse_anchor(handle)
        if parsed is None:
            return None
        target, anchor_id = parsed
        anchor = self.get_anchor(anchor_id)
        return anchor if anchor is not None and anchor.target_type == target else None

    def get_anchor_resolution(self, anchor_id: int) -> Resolution | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT state, method, chunk_id, char_start, char_end, "
                "line_start, line_end, symbol_path, selector FROM anchor_resolutions "
                "WHERE anchor_id = %s",
                (anchor_id,),
            ).fetchone()
        return Resolution(*row) if row else None

    def save_anchor_resolution(self, anchor_id: int, res: Resolution) -> None:
        """Upsert the one resolution row; the anchor row is never touched."""
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO anchor_resolutions (anchor_id, state, method, "
                "chunk_id, char_start, char_end, line_start, line_end, symbol_path, selector) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (anchor_id) DO UPDATE SET state = EXCLUDED.state, "
                "method = EXCLUDED.method, chunk_id = EXCLUDED.chunk_id, "
                "char_start = EXCLUDED.char_start, char_end = EXCLUDED.char_end, "
                "line_start = EXCLUDED.line_start, line_end = EXCLUDED.line_end, "
                "symbol_path = EXCLUDED.symbol_path, selector = EXCLUDED.selector, resolved_at = now()",
                (
                    anchor_id,
                    res.state,
                    res.method,
                    res.chunk_id,
                    res.char_start,
                    res.char_end,
                    res.line_start,
                    res.line_end,
                    res.symbol_path,
                    res.selector,
                ),
            )

    def relocate_text_anchor(self, anchor_id: int) -> Resolution:
        """Run the ladder for a text anchor against the ref's live chunks and
        record the result."""
        anchor = self.get_anchor(anchor_id)
        if anchor is None or anchor.target_type != "text":
            raise BadInput(f"at{anchor_id} is not a text anchor")
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id, text, content_sha FROM chunks "
                "WHERE ref_id = %s AND retired_at IS NULL ORDER BY ord",
                (anchor.ref_id,),
            ).fetchall()
        res = resolve_text(anchor, [ChunkText(*r) for r in rows])
        self.save_anchor_resolution(anchor_id, res)
        return res

    def relocate_code_anchor(
        self,
        anchor_id: int,
        head_sha: str | None,
        sources: Mapping[str, Sequence[str]] | Sequence[str] | None,
        *,
        locate_symbol: Callable[[str, int], str] | None = None,
        other_locations: Sequence[tuple[str, int]] = (),
    ) -> Resolution:
        """Run the ladder for a code anchor given the caller's read of the
        repository sources and record the result."""
        anchor = self.get_anchor(anchor_id)
        if anchor is None or anchor.target_type != "code":
            raise BadInput(f"ac{anchor_id} is not a code anchor")
        res = resolve_code(
            anchor,
            head_sha,
            sources,
            locate_symbol=locate_symbol,
            other_locations=other_locations,
        )
        self.save_anchor_resolution(anchor_id, res)
        return res

"""Long-yield tag redirect — shared across every tag write path.

``ask-user:<question>`` and ``halt:<reason>`` are the planner's
legitimate *yield* mechanism (see ``workers/planner_prompt.py``); their
values are LLM-authored prose that routinely carries spaces and runs
longer than a tag label should. The tag layer forbids whitespace in a
tag value (``Tag.parse_strict``) and hard-caps its length — so a raw
yield would be *rejected*, breaking the planner's yield.

The fix: before the ``parse_strict`` guard fires, rewrite a
whitespace-carrying or over-length yield value into a short space-free
``see-chunk-N`` handle and stash the full prose in a
``chunk_kind='tag_overflow'`` chunk on the ref. The guards downstream
see the short handle; ``get(kind=..., id=ref_id)`` reads the chunk for
the full explanation.

This is the single chokepoint that must run on *every* tag write path —
``TodoHandler.tag`` / ``TodoHandler._create`` (create-time tags) and the
generic ``NumericRefHandler.tag`` / ``NumericRefHandler._create`` used by
every other kind — so a yield can never leak prose into a tag row
regardless of entry point (gripe #39254). ORDERING IS LOAD-BEARING: call
this *before* ``Tag.parse_strict`` so a legitimate long/whitespace yield
is shortened to a space-free handle first, and only genuinely-malformed
(non-yield) whitespace tags reach the reject.

The implementation lives in the STORE layer (gr652333): the
``asks_triage`` worker rewrites ``ask-user:<question>`` yields through
the same redirect, and the workers→handlers import contract forbids it
reaching ``handlers._tag_redirect`` — which now re-exports these names
for the handler call sites.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from precis.errors import BadInput

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store.store import Store

#: Tag namespaces whose values are LLM-authored prose and routinely want
#: more than a tag's worth of room. A qualifying value gets redirected
#: into a ``chunk_kind='tag_overflow'`` chunk on the ref and the tag is
#: rewritten to a short ``see-chunk-N`` handle.
REDIRECTABLE_NAMESPACES: frozenset[str] = frozenset({"ask-user", "halt"})

#: A value already in redirect-handle form. Only this module may mint
#: one — a caller-supplied ``see-chunk-N`` is *forged* unless a
#: ``tag_overflow`` chunk already backs it on the same ref (the
#: idempotent re-add of a genuine handle). gr651179: an unbacked one
#: used to sail straight through — short and space-free, it trips
#: neither redirect test — and landed as a dangling pointer a human
#: reads as "see chunk N" with nothing there (e.g. a planner-tick
#: mimicking the handle form it saw on another todo, or a tag copied
#: across refs). The reader-side mirror is
#: :data:`precis.store._draft_ops._SEE_CHUNK_RE`.
_SEE_CHUNK_HANDLE_RE = re.compile(r"^see-chunk-(\d+)$")


#: Length threshold above which an ``ask-user:`` / ``halt:`` value gets
#: redirected to a chunk. Picked to comfortably accept the short
#: structured labels we expect ("missing-credentials",
#: "impossible-as-specified") while catching paragraph-style prose.
TAG_VALUE_REDIRECT_THRESHOLD: int = 80


def _value_needs_redirect(value: str) -> bool:
    """A yield value redirects when it is prose: too long OR whitespaced.

    The length trip catches paragraph-style dumps; the whitespace trip
    catches the sub-threshold single-line prose that would otherwise be
    rejected by ``Tag.parse_strict``'s whitespace guard (gripe #39254 —
    "ask-user:file writes disabled in sandbox …", 120–200 chars, no
    newline). A short space-free label ("missing-credentials") trips
    neither and stays a plain tag.
    """
    return len(value) > TAG_VALUE_REDIRECT_THRESHOLD or any(
        ch.isspace() for ch in value
    )


def redirect_long_tag_values(
    store: Store,
    *,
    ref_id: int,
    tags: list[str],
    conn: Connection | None = None,
) -> tuple[list[str], list[int]]:
    """Move long / whitespace ``ask-user:`` / ``halt:`` prose into chunks.

    For each tag ``ns:value`` where ``ns`` is in
    :data:`REDIRECTABLE_NAMESPACES` and ``value`` trips
    :func:`_value_needs_redirect`, write a ``chunk_kind='tag_overflow'``
    chunk carrying the full text on ``ref_id`` and replace the tag value
    with a short ``see-chunk-N`` handle (space-free, so it passes the
    downstream whitespace guard). Every other tag passes through
    untouched.

    ``conn`` — when given (the create paths, where the ref is minted in
    the same transaction), the overflow chunk is written on that
    connection so it lands atomically with the ref insert / tag writes.
    When ``None`` (the standalone ``tag`` paths, existing ref), a fresh
    connection is opened per chunk.

    Returns ``(rewritten_tags, chunk_ids)``. ``chunk_ids`` is empty when
    nothing tripped the redirect. Idempotent: a value already rewritten
    to ``see-chunk-N`` trips neither the length nor whitespace test, so a
    second pass is a no-op (no duplicate chunk) — provided a
    ``tag_overflow`` chunk still backs it on this ref. A ``see-chunk-N``
    with no backing chunk is a forged/copied pointer (gr651179) and is
    refused: the caller must write the question inline instead of a
    pointer at nothing.
    """
    from precis.store.types import ChunkInsert

    rewritten: list[str] = []
    chunk_ids: list[int] = []
    for raw in tags:
        # ``Tag.parse_strict`` (which runs *after* this redirect) strips a
        # leading ``OPEN:`` sentinel — the DB namespace column displays
        # leak into copy-pasted tag strings (gr192827). Mirror that strip
        # *here*, before the namespace/handle checks, or a
        # sentinel-carried yield (``OPEN:ask-user:see-chunk-2``) bypasses
        # the forged-handle refuse and the prose redirect, persisting the
        # same dangling pointer the checks exist to stop (review
        # td652026 of td652025). Pass-through appends ``raw`` untouched —
        # parse_strict strips the sentinel itself on the accepted forms.
        checked = raw
        if checked.startswith("OPEN:") and len(checked) > len("OPEN:"):
            checked = checked[len("OPEN:") :]
        if ":" not in checked:
            rewritten.append(raw)
            continue
        ns, _, value = checked.partition(":")
        if ns not in REDIRECTABLE_NAMESPACES:
            rewritten.append(raw)
            continue
        handle = _SEE_CHUNK_HANDLE_RE.match(value)
        if handle is not None:
            if _see_chunk_backed(store, ref_id, int(handle.group(1)), conn):
                rewritten.append(raw)
                continue
            raise BadInput(
                f"tag {raw!r} is a dangling pointer: no tag_overflow "
                f"chunk {handle.group(1)} exists on {ref_id} — "
                "see-chunk-N handles are minted by the yield redirect "
                "itself, never written by hand",
                next=(
                    f"pass the {ns} text itself in tags=/add= "
                    f"(e.g. add=['{ns}:<the question or reason>']) — a "
                    "long value lands in an overflow chunk the ask "
                    "views resolve"
                ),
            )
        if not _value_needs_redirect(value):
            rewritten.append(raw)
            continue
        text = f"{ns}: {value}"

        def _write(c: Connection, *, text: str = text, ns: str = ns) -> int:
            row = c.execute(
                "SELECT COALESCE(MAX(ord) + 1, 0) FROM chunks "
                "WHERE ref_id = %s AND ord >= 0",
                (ref_id,),
            ).fetchone()
            next_pos = int(row[0]) if row and row[0] is not None else 0
            store.chunks.insert_chunks(
                ref_id,
                [
                    ChunkInsert(
                        ord=next_pos,
                        text=text,
                        meta={"chunk_kind": "tag_overflow", "tag_namespace": ns},
                    )
                ],
                conn=c,
            )
            return next_pos

        if conn is not None:
            next_pos = _write(conn)
        else:
            with store.pool.connection() as c:
                next_pos = _write(c)
        chunk_ids.append(next_pos)
        rewritten.append(f"{ns}:see-chunk-{next_pos}")
    return rewritten, chunk_ids


def _see_chunk_backed(
    store: Store,
    ref_id: int,
    pos: int,
    conn: Connection | None,
) -> bool:
    """A ``tag_overflow`` chunk backs ``see-chunk-{pos}`` on ``ref_id``.

    Same kind filter :meth:`DraftStore.resolve_ask_question` resolves
    by — an unrelated chunk merely sharing the ord is not a backing
    chunk. Reads on the caller's ``conn`` when given (the create paths,
    where the overflow chunk may be uncommitted in the same tx)."""

    def _check(c: Connection) -> bool:
        row = c.execute(
            "SELECT 1 FROM chunks "
            "WHERE ref_id = %s AND ord = %s AND chunk_kind = 'tag_overflow'",
            (ref_id, pos),
        ).fetchone()
        return row is not None

    if conn is not None:
        return _check(conn)
    with store.pool.connection() as c:
        return _check(c)

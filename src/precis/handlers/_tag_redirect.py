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
"""

from __future__ import annotations

from typing import Any

#: Tag namespaces whose values are LLM-authored prose and routinely want
#: more than a tag's worth of room. A qualifying value gets redirected
#: into a ``chunk_kind='tag_overflow'`` chunk on the ref and the tag is
#: rewritten to a short ``see-chunk-N`` handle.
REDIRECTABLE_NAMESPACES: frozenset[str] = frozenset({"ask-user", "halt"})


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
    store: Any,
    *,
    ref_id: int,
    tags: list[str],
    conn: Any | None = None,
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
    second pass is a no-op (no duplicate chunk).
    """
    from precis.store.types import BlockInsert

    rewritten: list[str] = []
    chunk_ids: list[int] = []
    for raw in tags:
        if ":" not in raw:
            rewritten.append(raw)
            continue
        ns, _, value = raw.partition(":")
        if ns not in REDIRECTABLE_NAMESPACES or not _value_needs_redirect(value):
            rewritten.append(raw)
            continue
        text = f"{ns}: {value}"

        def _write(c: Any, *, text: str = text, ns: str = ns) -> int:
            row = c.execute(
                "SELECT COALESCE(MAX(ord) + 1, 0) FROM chunks "
                "WHERE ref_id = %s AND ord >= 0",
                (ref_id,),
            ).fetchone()
            next_pos = int(row[0]) if row and row[0] is not None else 0
            store.insert_blocks(
                ref_id,
                [
                    BlockInsert(
                        pos=next_pos,
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

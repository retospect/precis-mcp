"""Shared seed helpers for tests that previously redefined ``_seed_paper``.

Only byte-identical copies live here; the ~40 other ``_seed_paper`` defs
differ in signature or in what they seed (cite_key vs slug, chunks, meta,
identifiers) and stay local to their tests. ``FakeStore`` doubles are in
``tests/_fakes.py``. :func:`insert_gist` is the one door for hand-made
``chunk_summaries`` rows (gr515830).
"""

from __future__ import annotations

from typing import Any

from precis.store import ChunkInsert, Store


def seed_paper_with_body(store: Store, slug: str, title: str = "a paper") -> None:
    """A manual paper ref ``slug`` with one body chunk (``ord=0``, ``"body"``)."""
    store.insert_ref(kind="paper", slug=slug, title=title, provider="manual")
    paper_ref = store.get_ref(kind="paper", id=slug)
    assert paper_ref is not None
    store.chunks.insert_chunks(
        paper_ref.id, [ChunkInsert(ord=0, text="body", slug="b0")]
    )


def seed_paper_ref(store: Any, *, slug: str) -> int:
    """A bare paper ref titled ``P <slug>`` with empty meta; returns ref_id."""
    return int(store.insert_ref(kind="paper", slug=slug, title=f"P {slug}", meta={}).id)


def insert_gist(
    store: Store, chunk_id: int, text: str, *, sha: str | None = "current"
) -> None:
    """An ``llm-v1`` ``chunk_summaries`` row for ``chunk_id``.

    ``content_sha`` defaults to the chunk's own current sha — the
    hand-written ``INSERT``s this replaces kept forgetting the column,
    and a NULL or mismatched sha reads as a stale gist the readers must
    not serve (gr515830; the sha-match rule is gr514407's). Pass
    ``sha=None`` for a legacy sha-less row, or ``sha="<literal>"`` for a
    mismatched one.
    """
    if sha == "current":
        sql = (
            "INSERT INTO chunk_summaries (chunk_id, summarizer, text, "
            "content_sha) VALUES (%s, 'llm-v1', %s, "
            "(SELECT content_sha FROM chunks WHERE chunk_id = %s))"
        )
        params: tuple[Any, ...] = (chunk_id, text, chunk_id)
    else:
        sql = (
            "INSERT INTO chunk_summaries (chunk_id, summarizer, text, "
            "content_sha) VALUES (%s, 'llm-v1', %s, %s)"
        )
        params = (chunk_id, text, sha)
    with store.pool.connection() as conn:
        conn.execute(sql, params)

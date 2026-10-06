"""Shared seed helpers for tests that previously redefined ``_seed_paper``.

Only byte-identical copies live here; the ~40 other ``_seed_paper`` defs
differ in signature or in what they seed (cite_key vs slug, chunks, meta,
identifiers) and stay local to their tests. ``FakeStore`` doubles are in
``tests/_fakes.py``.
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

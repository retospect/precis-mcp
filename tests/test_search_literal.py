"""gr503537: ``search(mode='literal')`` — exact substring over chunk text.

``mode='literal'`` is the ILIKE leg: case-insensitive substring match
over ``chunks.text`` with ``%`` / ``_`` escaped, same kind/tag/retired
fences as the other legs. ``mode='verbatim'`` is keyword containment
(plus a phrase substring fallback only when ref-scoped, gr518172).
"""

from __future__ import annotations

from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.store import Store
from tests.conftest import id_of


def _memory(store: Store) -> MemoryHandler:
    return MemoryHandler(hub=Hub(store=store))


def test_literal_finds_exact_substring(store: Store) -> None:
    """The gripe's shape: a filename literal in a body is a literal hit.
    (Unscoped verbatim is keyword containment only since gr518172's
    ruling — no trigram index — so ``literal`` is the substring mode.)"""
    memory = _memory(store)
    path = "./src/precis/cli/memory_mirror.py"
    mid = id_of(memory.put(text=f"the mirror writes files under {path} daily").body)

    literal = {h.ref_id for h in memory.search_hits(q=path, mode="literal")}
    assert mid in literal
    verbatim = {h.ref_id for h in memory.search_hits(q=path, mode="verbatim")}
    assert mid not in verbatim


def test_literal_requires_the_phrase(store: Store) -> None:
    """literal requires the phrase verbatim; words apart don't match."""
    memory = _memory(store)
    mid = id_of(memory.put(text="alpha in the head … and beta in the tail").body)
    literal = {h.ref_id for h in memory.search_hits(q="alpha beta", mode="literal")}
    assert mid not in literal


def test_literal_percent_is_data(store: Store) -> None:
    memory = _memory(store)
    hit = id_of(memory.put(text="discount was 50% off list").body)
    other = id_of(memory.put(text="discount was 50000 off list").body)

    found = {h.ref_id for h in memory.search_hits(q="50% off", mode="literal")}
    assert hit in found
    assert other not in found


def test_literal_scoped_by_tags(store: Store) -> None:
    memory = _memory(store)
    tagged = id_of(memory.put(text="holds xyzzy-marker", tags=["topic:a"]).body)
    id_of(memory.put(text="holds xyzzy-marker", tags=["topic:b"]).body)

    hits = memory.search(q="xyzzy-marker", mode="literal", tags=["topic:a"])
    assert f"memory {tagged}" in hits.body
    assert "1 memory" in hits.body


def test_literal_blank_query_returns_nothing(store: Store) -> None:
    memory = _memory(store)
    memory.put(text="anything at all")
    assert memory.search_hits(q="   ", mode="literal") == []

"""gripe #39254 — whitespace-in-tag guard + long-yield redirect.

Agent prose (an ``ask-user:``/``halt:`` yield narrative) was leaking into
a tag *value* verbatim. Reto's rule: a tag value carries no whitespace.
The coupling is that ``ask-user:``/``halt:`` are the planner's legitimate
yield mechanism and their values are prose-with-spaces — so the long-yield
redirect (``handlers._tag_redirect``) must run on *every* tag write path
*before* ``Tag.parse_strict``'s whitespace guard, shortening a real yield
to a space-free ``see-chunk-N`` handle. Only a genuinely-malformed
(non-yield) whitespace tag then reaches the reject.

These tests exercise all four write paths:

* ``TodoHandler.put``  (create-time tags)  → redirected
* ``TodoHandler.tag``  (existing todo)     → redirected
* generic ``_create``  (memory put)        → redirected
* generic ``tag``      (memory tag)         → redirected

plus the reject for a non-yield whitespace tag on each family.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.memory import MemoryHandler
from precis.handlers.todo import TodoHandler
from precis.store import Store
from tests.conftest import id_of

# The real prod #39254 shape: single-line prose, no newline, 120–200 chars.
_PROSE = (
    "ask-user:file writes are disabled in the sandbox so I cannot create "
    "the file you asked for; please enable writes or tell me a different "
    "path to use"
)


@pytest.fixture
def todo(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


@pytest.fixture
def memory(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def _tag_values(store: Store, ref_id: int) -> list[str]:
    return [str(t) for t in store.tags_for(ref_id)]


# ── the #39254 prose is a single-line yield, not multi-line ──────────


def test_prose_shape_is_single_line() -> None:
    """Sanity: the motivating value is a single line (the first-pass
    newline guard would have missed it) but carries spaces (the broadened
    whitespace guard catches / the redirect handles it)."""
    assert "\n" not in _PROSE and "\r" not in _PROSE
    assert " " in _PROSE
    assert 80 < len(_PROSE) < 200  # slips past both the 200-cap and newline guard


# ── TodoHandler.put — create-time tags ───────────────────────────────


def test_put_redirects_long_yield_tag(todo: TodoHandler) -> None:
    """A space-carrying ``ask-user:`` yield supplied at create time is
    redirected to ``ask-user:see-chunk-N`` and the prose lands in a
    ``tag_overflow`` chunk — NOT rejected."""
    resp = todo.put(text="do the thing", tags=[_PROSE])
    ref_id = id_of(resp.body)
    values = _tag_values(todo.store, ref_id)
    handles = [v for v in values if v.startswith("ask-user:")]
    assert handles, values
    assert handles[0].startswith("ask-user:see-chunk-")
    assert " " not in handles[0]
    # The prose is recoverable from the tag_overflow chunk.
    slug = handles[0].split(":", 1)[1]
    resolved = todo.store.resolve_ask_question(ref_id, slug)
    assert "file writes are disabled" in resolved


def test_put_rejects_nonyield_whitespace_tag(todo: TodoHandler) -> None:
    """A non-yield (non ``ask-user:``/``halt:``) tag carrying whitespace is
    prose and must be rejected with a typed BadInput — the redirect only
    shortens the yield namespaces, so this reaches the guard."""
    with pytest.raises(BadInput, match="whitespace"):
        todo.put(text="do the thing", tags=["topic:carbon capture sweep"])


# ── TodoHandler.tag — existing todo ──────────────────────────────────


def test_tag_redirects_long_yield_tag(todo: TodoHandler) -> None:
    resp = todo.put(text="parent task")
    ref_id = id_of(resp.body)
    todo.tag(id=ref_id, add=[_PROSE])
    handles = [v for v in _tag_values(todo.store, ref_id) if v.startswith("ask-user:")]
    assert handles and handles[0].startswith("ask-user:see-chunk-")
    assert " " not in handles[0]


def test_tag_rejects_nonyield_whitespace_tag(todo: TodoHandler) -> None:
    resp = todo.put(text="parent task")
    ref_id = id_of(resp.body)
    with pytest.raises(BadInput, match="whitespace"):
        todo.tag(id=ref_id, add=["topic:carbon capture"])


# ── generic path (memory) — _create + tag ────────────────────────────


def test_memory_put_redirects_long_yield_tag(memory: MemoryHandler) -> None:
    """The generic ``NumericRefHandler._create`` path (every non-todo
    kind) redirects the yield too."""
    resp = memory.put(text="a note", tags=[_PROSE])
    ref_id = id_of(resp.body)
    handles = [
        v for v in _tag_values(memory.store, ref_id) if v.startswith("ask-user:")
    ]
    assert handles and handles[0].startswith("ask-user:see-chunk-")
    assert " " not in handles[0]
    slug = handles[0].split(":", 1)[1]
    assert "file writes are disabled" in memory.store.resolve_ask_question(ref_id, slug)


def test_memory_tag_redirects_long_yield_tag(memory: MemoryHandler) -> None:
    """The generic ``NumericRefHandler.tag`` path redirects the yield."""
    ref_id = id_of(memory.put(text="a note").body)
    memory.tag(id=ref_id, add=[_PROSE])
    handles = [
        v for v in _tag_values(memory.store, ref_id) if v.startswith("ask-user:")
    ]
    assert handles and handles[0].startswith("ask-user:see-chunk-")


def test_memory_put_rejects_nonyield_whitespace_tag(memory: MemoryHandler) -> None:
    with pytest.raises(BadInput, match="whitespace"):
        memory.put(text="a note", tags=["topic:carbon capture"])


def test_memory_create_rolls_back_on_rejected_tag(memory: MemoryHandler) -> None:
    """A rejected whitespace tag on create writes nothing — the ref insert
    rolls back with the tx (parse_strict now runs inside the create tx)."""
    before = len(memory.store.list_refs(kind="memory", limit=1000))
    with pytest.raises(BadInput, match="whitespace"):
        memory.put(text="ghost note", tags=["topic:carbon capture"])
    after = len(memory.store.list_refs(kind="memory", limit=1000))
    assert after == before


# ── short space-free yields stay plain tags (no redirect) ────────────


def test_short_spacefree_yield_is_not_redirected(todo: TodoHandler) -> None:
    """A short, space-free ``halt:`` handle is a legitimate tag label and
    must pass through untouched (no chunk, no see-chunk rewrite)."""
    ref_id = id_of(todo.put(text="task").body)
    todo.tag(id=ref_id, add=["halt:missing-credentials"])
    values = _tag_values(todo.store, ref_id)
    assert "halt:missing-credentials" in values
    assert not any("see-chunk" in v for v in values)

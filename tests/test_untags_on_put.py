"""Tests for the ``untags=`` parameter on numeric-ref ``put``.

Closed-prefix overwrite already exists on the ``tags=`` path
(``tags=['STATUS:done']`` replaces an existing ``STATUS:open`` row).
``untags=`` covers the gap where an agent needs to *remove* an open
or flag tag, or roll back a closed-prefix value without picking a
replacement.

Pinned semantics:
  * Same strict validation as ``tags=`` (canonical form, no bare
    flags that collide with a closed value).
  * Value-matched removal — ``untags=['STATUS:open']`` against a
    ``STATUS:done`` ref is a silent no-op, mirroring the
    closed-prefix overwrite behaviour.
  * Empty form ``STATUS:`` is rejected at parse time, so removing
    "any STATUS regardless of value" is not possible by accident.
  * On create (``id=None``), ``untags=`` raises ``BadInput`` —
    there's nothing to remove.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.memory import MemoryHandler
from precis.store import Store, Tag

# ── plumbing ────────────────────────────────────────────────────────


@pytest.fixture
def memory(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def _create_with_tags(h: MemoryHandler, *tags: str, text: str = "hello") -> int:
    """Create a memory carrying ``*tags`` and return its id."""
    out = h.put(text=text, tags=list(tags))
    # Response body looks like: ``created memory id=42``. Extract.
    body = out.body
    last = body.split("=")[-1].strip()
    return int(last.split()[0])


# ── basic untag flows ──────────────────────────────────────────────


def test_untag_removes_open_tag(memory: MemoryHandler, store: Store) -> None:
    rid = _create_with_tags(memory, "topic-co2-capture")
    assert any(t.value == "topic-co2-capture" for t in store.tags_for(rid))

    memory.tag(id=rid, remove=["topic-co2-capture"])

    assert not any(t.value == "topic-co2-capture" for t in store.tags_for(rid))


def test_untag_removes_flag(memory: MemoryHandler, store: Store) -> None:
    # 'star' is a flag (registered via flag_names); not closed-vocab.
    rid = _create_with_tags(memory, "star")
    assert any(t.value == "star" for t in store.tags_for(rid))

    memory.tag(id=rid, remove=["star"])

    assert not any(t.value == "star" for t in store.tags_for(rid))


def test_untag_closed_prefix_value_match(store: Store) -> None:
    """``STATUS:done`` removes the row only if STATUS is currently
    ``done`` — different value is a silent no-op.

    Exercised on ``todo`` because per-kind axis enforcement
    disallows STATUS: on ``memory`` (memories have no workflow
    state). The contract being tested is the *generic* closed-
    prefix value-match removal, which is identical across kinds.
    """
    from precis.handlers.todo import TodoHandler

    todo = TodoHandler(hub=Hub(store=store))
    out = todo.put(text="task")
    rid = int(out.body.split("id=")[1].split()[0].rstrip(",.()"))

    # Todos create with STATUS:open by default. Bump to STATUS:done.
    todo.tag(id=rid, add=["STATUS:done"])
    assert any(
        t.namespace == "closed" and t.prefix == "STATUS" and t.value == "done"
        for t in store.tags_for(rid)
    )

    # Wrong-value untag is a no-op — STATUS:done stays put.
    todo.tag(id=rid, remove=["STATUS:open"])
    assert any(
        t.namespace == "closed" and t.prefix == "STATUS" and t.value == "done"
        for t in store.tags_for(rid)
    )

    # Right-value untag removes it.
    todo.tag(id=rid, remove=["STATUS:done"])
    assert not any(
        t.namespace == "closed" and t.prefix == "STATUS" for t in store.tags_for(rid)
    )


def test_untag_idempotent(memory: MemoryHandler, store: Store) -> None:
    """Removing a tag that isn't there is a silent no-op (same as
    SQL DELETE finding zero rows)."""
    rid = _create_with_tags(memory)
    memory.tag(id=rid, remove=["topic-not-set"])  # no error
    memory.tag(id=rid, remove=["topic-not-set"])  # still no error


def test_untag_with_tags_combined(memory: MemoryHandler, store: Store) -> None:
    """Same put can both add and remove tags atomically (well, two
    DB calls, but one agent call)."""
    rid = _create_with_tags(memory, "topic-old")
    memory.tag(id=rid, add=["topic-new"], remove=["topic-old"])

    values = {t.value for t in store.tags_for(rid)}
    assert "topic-new" in values
    assert "topic-old" not in values


# ── validation: same shape as tags= ────────────────────────────────


def test_untag_rejects_bare_collision(store: Store) -> None:
    """Bare flag rejection is kind-scoped (per :data:`_KIND_ALLOWED_AXES`):
    a bare flag only shadows the closed form on kinds that allow the
    colliding axis. ``urgent`` collides with ``PRIO:urgent``, which is
    accepted by ``todo`` but not by ``memory``. So the rejection only
    fires on workflow kinds — exercise on ``todo``."""
    from precis.handlers.todo import TodoHandler

    todo = TodoHandler(hub=Hub(store=store))
    out = todo.put(text="task", tags=["topic-x"])
    rid = int(out.body.split("id=")[1].split()[0].rstrip(",.()"))
    with pytest.raises(BadInput, match="bare flag 'urgent'"):
        todo.tag(id=rid, remove=["urgent"])


def test_untag_rejects_unknown_status(store: Store) -> None:
    """STATUS axis enforcement now happens before value validation.
    Exercised on ``todo`` (which allows STATUS) so we reach the
    invalid-value branch — the rejection text is what we pin."""
    from precis.handlers.todo import TodoHandler

    todo = TodoHandler(hub=Hub(store=store))
    out = todo.put(text="task")
    rid = int(out.body.split("id=")[1].split()[0].rstrip(",.()"))
    with pytest.raises(BadInput, match="invalid STATUS value"):
        todo.tag(id=rid, remove=["STATUS:bogus"])


def test_untag_rejects_empty_value_form(memory: MemoryHandler) -> None:
    """``STATUS:`` (empty value) must not be accepted as 'remove all
    STATUS tags'. Closed-vocab values are exhaustively listed and
    the empty string is not among them."""
    rid = _create_with_tags(memory)
    with pytest.raises(BadInput):
        memory.tag(id=rid, remove=["STATUS:"])


# ── update path: at-least-one validation ────────────────────────────


def test_update_with_only_untags_is_valid(memory: MemoryHandler, store: Store) -> None:
    """``untags=`` alone is a sufficient update — no need to also
    pass text= or tags=."""
    rid = _create_with_tags(memory, "topic-x")
    out = memory.tag(id=rid, remove=["topic-x"])
    assert "tagged memory" in out.body


def test_put_on_existing_id_rejected(memory: MemoryHandler, store: Store) -> None:
    """After the seven-verb cutover, ``put`` is creation-only on
    numeric refs. Passing ``id=`` is a misuse and the error points
    at the right verb to reach instead of accepting a no-op."""
    rid = _create_with_tags(memory)
    with pytest.raises(BadInput, match="put on existing memory"):
        memory.put(id=rid)


# ── create path: untags rejected ───────────────────────────────────


def test_untags_kwarg_rejected_on_put(memory: MemoryHandler) -> None:
    """``untags=`` is no longer a put kwarg; the error points at
    the tag verb's ``remove=`` parameter so an agent stuck on the
    old shape gets a sharp hint."""
    with pytest.raises(BadInput, match="untags= is not accepted on put"):
        memory.put(text="hello", untags=["topic-x"])


# ── store-level removal still works (regression sanity) ────────────


def test_store_remove_tag_smoke(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="x")
    store.add_tag(ref.id, Tag.open("foo"))
    assert any(t.value == "foo" for t in store.tags_for(ref.id))
    store.remove_tag(ref.id, Tag.open("foo"))
    assert not any(t.value == "foo" for t in store.tags_for(ref.id))

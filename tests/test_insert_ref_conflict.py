"""``insert_ref`` refuses a slug a live ref already holds (gr517405).

The cite_key claim inside ``insert_ref`` only steals from a RETIRED
owner; a live owner (any kind — cite_key is a global namespace) used to
leave the new ref inserted with ``slug=None`` while the handler reported
success. Now it raises ``Conflict`` so the caller's transaction rolls
the orphan ``refs`` row back. ``pcb_apply``'s create path and
``store.pcb_fork`` are the covered doors; a concurrent create is
simulated by pre-inserting the cite_key identifier directly.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.errors import Conflict


def _live_paper(store: Any, slug: str) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title="A study")
    return int(ref.id)


def _preinsert_cite_key(store: Any, slug: str, holder_ref_id: int) -> None:
    """Simulate a concurrent create: the cite_key row a racing insert
    committed between the caller's check and its own insert."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('cite_key', %s, %s, 'test')",
            (slug, holder_ref_id),
        )
        conn.commit()


def _ref_count(store: Any, kind: str) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = %s", (kind,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_insert_ref_refuses_slug_held_by_another_kind(store: Any) -> None:
    holder_id = _live_paper(store, "held-slug")
    with pytest.raises(Conflict, match="held by a live paper:held-slug"):
        store.insert_ref(kind="draft", slug="held-slug", title="Draft")
    # no slugless orphan refs row survived the raise, and the holder's
    # claim is untouched.
    assert _ref_count(store, "draft") == 0
    with store.pool.connection() as conn:
        owner = conn.execute(
            "SELECT ref_id FROM ref_identifiers "
            "WHERE id_kind = 'cite_key' AND id_value = 'held-slug'"
        ).fetchone()
    assert owner is not None and int(owner[0]) == holder_id


def test_insert_ref_refuses_slug_held_by_same_kind(store: Any) -> None:
    _live_paper(store, "same-kind-slug")
    with pytest.raises(Conflict):
        store.insert_ref(kind="paper", slug="same-kind-slug", title="Dup")
    assert _ref_count(store, "paper") == 1


def test_insert_ref_conflict_rolls_back_caller_transaction(store: Any) -> None:
    holder_id = _live_paper(store, "raced-slug")
    _preinsert_cite_key(store, "raced-slug2", holder_id)
    with pytest.raises(Conflict):
        with store.tx() as conn:
            store.insert_ref(kind="draft", slug="raced-slug2", title="Draft", conn=conn)
            # anything after the raise never runs; the tx exits via the
            # exception so the orphan refs row rolls back with it.
    assert _ref_count(store, "draft") == 0


def test_insert_ref_reclaims_slug_from_retired_owner(store: Any) -> None:
    old = store.insert_ref(kind="paper", slug="free-again", title="Old")
    store.retire_ref(int(old.id))
    new = store.insert_ref(kind="draft", slug="free-again", title="New")
    assert new.slug == "free-again"


def test_pcb_apply_create_refuses_held_slug(store: Any) -> None:
    """The ``get_ref(kind='pcb')`` pre-check misses a paper's slug; the
    insert must raise, not mint a slugless board."""
    _live_paper(store, "board-slug")
    with pytest.raises(Conflict, match="paper:board-slug"):
        store.pcb_apply(
            slug="board-slug",
            title="Board",
            components=[],
            nets=[],
            connections=[],
        )
    assert _ref_count(store, "pcb") == 0


def test_pcb_apply_create_refuses_preinserted_slug(store: Any) -> None:
    """Concurrent-create shape: the cite_key landed after the caller's
    own check — ``store.pcb_apply`` still refuses and leaves nothing."""
    holder_id = _live_paper(store, "pcb-holder")
    _preinsert_cite_key(store, "raced-board", holder_id)
    with pytest.raises(Conflict):
        store.pcb_apply(
            slug="raced-board",
            title="Board",
            components=[],
            nets=[],
            connections=[],
        )
    assert _ref_count(store, "pcb") == 0


def test_pcb_fork_refuses_held_slug(store: Any) -> None:
    """Store-level ``pcb_fork`` (below the handler's live_slug_owner
    pre-check) refuses and rolls the whole copy back."""
    src_ref, _created, _counts = store.pcb_apply(
        slug="fork-src-board",
        title="Source",
        components=[],
        nets=[],
        connections=[],
    )
    holder_id = _live_paper(store, "fork-dst-slug")
    with pytest.raises(Conflict):
        with store.tx() as conn:
            store.pcb_fork(conn, src_ref, slug="fork-dst-slug", title="Fork")
    assert _ref_count(store, "pcb") == 1
    with store.pool.connection() as conn:
        owner = conn.execute(
            "SELECT ref_id FROM ref_identifiers "
            "WHERE id_kind = 'cite_key' AND id_value = 'fork-dst-slug'"
        ).fetchone()
    assert owner is not None and int(owner[0]) == holder_id


def test_pcb_fork_refuses_preinserted_slug(store: Any) -> None:
    src_ref, _created, _counts = store.pcb_apply(
        slug="fork-src2",
        title="Source",
        components=[],
        nets=[],
        connections=[],
    )
    holder_id = _live_paper(store, "fork-holder")
    _preinsert_cite_key(store, "raced-fork", holder_id)
    with pytest.raises(Conflict):
        with store.tx() as conn:
            store.pcb_fork(conn, src_ref, slug="raced-fork", title="Fork")
    assert _ref_count(store, "pcb") == 1

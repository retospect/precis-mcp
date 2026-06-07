"""Salience primitives for dreaming target selection.

Deterministic, in-process core (docs/design/dreaming.md, §Target
selection): a chunk's score is ``last_seen - last_dreamt`` and the seed
is the argmax over ``paper`` + ``memory``. These tests pin the four
behaviours the design calls out:

- ``bump_salience`` advances ``last_seen`` + ``accesses`` for a page;
- dream-actor reads are excluded (feedback guard);
- ``touch_last_dreamt`` resets the rotation;
- ``select_dream_seed`` picks the most-due chunk and rotates.
"""

from __future__ import annotations

import pytest

from precis.store import Store, as_dream_actor


def _mk_chunk(store: Store, ref_id: int, ord_: int, text: str) -> int:
    """Insert a body chunk and return its chunk_id."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, %s, 'paragraph', %s, '{}'::jsonb) RETURNING chunk_id",
            (ref_id, ord_, text),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _salience(store: Store, chunk_id: int) -> tuple:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT last_seen, last_dreamt, accesses FROM chunks WHERE chunk_id = %s",
            (chunk_id,),
        ).fetchone()
    assert row is not None
    return row


def test_bump_advances_last_seen_and_accesses(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="p1", title="P1", meta={})
    cid = _mk_chunk(store, ref.id, 0, "body")
    seen0, _, acc0 = _salience(store, cid)
    n = store.bump_salience([cid])
    assert n == 1
    seen1, _, acc1 = _salience(store, cid)
    assert seen1 > seen0
    assert acc1 == acc0 + 1


def test_bump_empty_is_noop(store: Store) -> None:
    assert store.bump_salience([]) == 0


def test_dream_actor_reads_do_not_bump(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="p2", title="P2", meta={})
    cid = _mk_chunk(store, ref.id, 0, "body")
    _, _, acc0 = _salience(store, cid)
    with as_dream_actor():
        assert store.bump_salience([cid]) == 0
    _, _, acc1 = _salience(store, cid)
    assert acc1 == acc0  # suppressed: the dreamer must not heat itself


def test_touch_last_dreamt_resets_rotation(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="m", meta={})
    cid = store.upsert_card_combined(ref.id, "m")
    # bump so last_seen > last_dreamt (score > 0)
    store.bump_salience([cid])
    seen, dreamt, _ = _salience(store, cid)
    assert seen > dreamt
    store.touch_last_dreamt([cid])
    seen2, dreamt2, _ = _salience(store, cid)
    assert dreamt2 >= seen2  # rotated out: score back to <= 0


def test_select_dream_seed_picks_argmax_and_rotates(store: Store) -> None:
    pa = store.insert_ref(kind="paper", slug="pa", title="A", meta={})
    pb = store.insert_ref(kind="paper", slug="pb", title="B", meta={})
    ca = _mk_chunk(store, pa.id, 0, "a")
    cb = _mk_chunk(store, pb.id, 0, "b")
    # ca is accessed → highest last_seen - last_dreamt → the seed
    store.bump_salience([ca])
    assert store.select_dream_seed() == ca
    # surfacing ca stamps last_dreamt → cb (untouched, score 0) now tops
    store.touch_last_dreamt([ca])
    assert store.select_dream_seed() == cb


def test_select_dream_seed_excludes_non_target_kinds(store: Store) -> None:
    todo = store.insert_ref(kind="todo", slug=None, title="t", meta={})
    ct = _mk_chunk(store, todo.id, 0, "t")
    store.bump_salience([ct])  # hot, but a todo is never a dream target
    assert store.select_dream_seed() != ct


def test_select_dream_seed_empty_corpus(store: Store) -> None:
    assert store.select_dream_seed() is None


def test_select_dream_seed_skips_deleted_refs(store: Store) -> None:
    pa = store.insert_ref(kind="paper", slug="pd", title="D", meta={})
    ca = _mk_chunk(store, pa.id, 0, "a")
    store.bump_salience([ca])
    store.soft_delete_ref(pa.id)
    assert store.select_dream_seed() is None


@pytest.mark.parametrize("kinds", [("paper",), ("memory",)])
def test_select_dream_seed_respects_kind_filter(
    store: Store, kinds: tuple[str, ...]
) -> None:
    pa = store.insert_ref(kind="paper", slug="pf", title="F", meta={})
    cp = _mk_chunk(store, pa.id, 0, "p")
    mem = store.insert_ref(kind="memory", slug=None, title="m", meta={})
    cm = store.upsert_card_combined(mem.id, "m")
    store.bump_salience([cp, cm])
    seed = store.select_dream_seed(kinds=kinds)
    assert seed == (cp if kinds == ("paper",) else cm)

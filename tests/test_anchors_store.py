"""The ``anchors`` store (gr739606, migration 0360): mint doors, the
exactly-one-position-set CHECK, immutability, relocation."""

from __future__ import annotations

import psycopg
import pytest

from precis.errors import BadInput
from precis.store import Store
from precis.store._anchors_ops import Anchor
from tests.workers._helpers import seed_chunk, seed_ref

TEXT = "The yield was 42 % at 300 K."


def _chunk(store: Store) -> tuple[int, int]:
    ref = seed_ref(store, title="a paper", kind="paper")
    return ref, seed_chunk(store, ref_id=ref, text=TEXT)


def test_mint_text_anchor_by_quote_server_locates_range(store: Store) -> None:
    ref, chunk = _chunk(store)
    a = store.mint_text_anchor(ref, chunk, quote="42 %")
    assert (a.char_start, a.char_end) == (14, 18)
    assert a.quote == "42 %" and a.target_type == "text" and a.fingerprint
    initial = store.get_anchor_resolution(a.anchor_id)
    assert initial is not None and (initial.state, initial.chunk_id) == ("ok", chunk)
    assert store.get_anchor_by_handle(f"at{a.anchor_id}") == a
    assert store.get_anchor_by_handle(f"ac{a.anchor_id}") is None


def test_mint_text_anchor_by_range_checks_quote(store: Store) -> None:
    ref, chunk = _chunk(store)
    a = store.mint_text_anchor(ref, chunk, char_start=14, char_end=18)
    assert a.quote == "42 %"
    with pytest.raises(BadInput):
        store.mint_text_anchor(ref, chunk, quote="43 %", char_start=14, char_end=18)
    with pytest.raises(BadInput):
        store.mint_text_anchor(ref, chunk, quote="not there")
    with pytest.raises(BadInput):
        store.mint_text_anchor(ref, chunk, char_start=0, char_end=999)


def test_mint_code_anchor_and_position_check(store: Store) -> None:
    a = store.mint_code_anchor(
        "repo", "m.py::f", 3, 4, quote="def f():\n    return 1", commit_sha="abc"
    )
    assert isinstance(a, Anchor) and a.target_type == "code"
    with pytest.raises(BadInput):
        store.mint_code_anchor(
            "repo", "m.py::f", 3, 5, quote="one line", commit_sha="x"
        )
    # both column sets filled violates the CHECK
    with store.pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO anchors (target_type, quote, fingerprint, repo, "
            "symbol_path, line_start, line_end, char_start, char_end) "
            "VALUES ('code', 'q', 'f', 'r', 's', 1, 1, 0, 1)"
        )


def test_anchor_row_is_immutable_but_resolution_moves(store: Store) -> None:
    ref, chunk = _chunk(store)
    a = store.mint_text_anchor(ref, chunk, quote="42 %")
    with store.pool.connection() as conn, pytest.raises(psycopg.errors.Error):
        conn.execute(
            "UPDATE anchors SET note = 'x' WHERE anchor_id = %s", (a.anchor_id,)
        )
    res = store.relocate_text_anchor(a.anchor_id)
    assert (res.state, res.method) == ("ok", "fingerprint")
    assert store.get_anchor_resolution(a.anchor_id) == res
    assert store.get_anchor(a.anchor_id) == a


def test_relocate_after_reingest_is_moved_then_stale(store: Store) -> None:
    ref, chunk = _chunk(store)
    a = store.mint_text_anchor(ref, chunk, quote="42 %")
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM chunks WHERE chunk_id = %s", (chunk,))
    new = seed_chunk(store, ref_id=ref, text="Intro. " + TEXT)
    res = store.relocate_text_anchor(a.anchor_id)
    assert (res.state, res.chunk_id) == ("moved", new)
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM chunks WHERE chunk_id = %s", (new,))
    assert store.relocate_text_anchor(a.anchor_id).state == "stale"
    assert store.get_anchor(a.anchor_id) == a


def test_structured_anchor_relocates_to_unique_selector(store: Store) -> None:
    ref, _ = _chunk(store)
    anchor = store.mint_part_anchor(
        ref, "pcb", "U1", quote="amplifier", fingerprint="old"
    )
    moved = store.relocate_part_anchor(anchor.anchor_id, "new", {"U2": "amplifier"})
    assert (moved.state, moved.selector) == ("moved", "U2")
    assert store.get_anchor_resolution(anchor.anchor_id) == moved
    assert store.get_anchor(anchor.anchor_id) == anchor

"""Capture and read-back of the private gtd store against real Postgres."""

from __future__ import annotations

import pytest

from precis.errors import BadInput
from precis.gtd import GtdStore
from precis.store import Store
from precis.user_context import user_scope
from precis.users import PasswordRecord

pytestmark = pytest.mark.db


@pytest.fixture
def owner(store: Store) -> int:
    password = PasswordRecord("unused", "unused", "scrypt-v1")
    return store.create_web_user(login="gtd-alice", abbrev="ga", password=password).id


def test_capture_then_get_item_round_trips_for_the_owner(
    store: Store, owner: int
) -> None:
    """me663435: a captured inbox item reads back for its owner with
    ``captured`` equal to the words given, proving the gtd-only tables work."""
    gtd = GtdStore(store)
    with user_scope(owner):
        item = gtd.capture("call the plumber about the boiler")
        again = gtd.get_item(item.id)
    assert again == item
    assert again.captured == "call the plumber about the boiler"
    assert again.text == again.captured
    assert (again.list, again.state) == ("inbox", "open")


def test_blank_capture_is_refused(store: Store, owner: int) -> None:
    """me663435: an inbox item needs words; a blank capture is BadInput and
    writes no row."""
    with user_scope(owner), pytest.raises(BadInput):
        GtdStore(store).capture("   ")


def _owner_row_count(store: Store, owner: int) -> int:
    with store.pool.connection() as conn, conn.transaction():
        conn.execute("SELECT set_config('precis.gtd_user_id', %s, true)", (str(owner),))
        row = conn.execute(
            "SELECT count(*) FROM gtd_items WHERE user_id = %s", (owner,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_oversize_capture_is_refused_and_writes_no_row(
    store: Store, owner: int
) -> None:
    """me663435, me663441: a capture over 4000 UTF-8 bytes raises BadInput
    naming the limit and leaves the owner's inbox empty."""
    gtd = GtdStore(store)
    with user_scope(owner):
        with pytest.raises(BadInput, match="4000"):
            gtd.capture("x" * 4001)
    assert _owner_row_count(store, owner) == 0


def test_control_characters_other_than_newline_and_tab_are_refused(
    store: Store, owner: int
) -> None:
    """me663435, me663441: capture accepts newline and tab and refuses other
    control characters without writing an inbox item."""
    gtd = GtdStore(store)
    with user_scope(owner):
        for bad in ("a\x00b", "a\x0bb", "a\x7fb"):
            with pytest.raises(BadInput):
                gtd.capture(bad)
        item = gtd.capture("line one\nline two\tindented")
    assert _owner_row_count(store, owner) == 1
    assert item.text == "line one\nline two\tindented"

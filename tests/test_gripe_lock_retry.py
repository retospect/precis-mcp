"""Gripe filing retries lock contention in-process (gr537784).

Prod roles run ``lock_timeout=5s``; a filing that waited on a sibling
writer surfaced as a bare ``Upstream LockNotAvailable`` and the gripe text
was lost. The create transaction now runs under ``retry_locked``; an
exhausted budget refuses with the draft text in ``next=``. The root cause —
a no-op UPDATE row-locking the shared tag row — is gone (0265,
``_upsert_tag``); the tail of this file pins that.
"""

from __future__ import annotations

from typing import Any

import pytest
from psycopg.errors import LockNotAvailable

from precis.dispatch import Hub
from precis.errors import Upstream
from precis.handlers.gripe import GripeHandler
from precis.store import Store
from precis.store._mappers import _upsert_tag
from precis.utils import db_retry


@pytest.fixture
def handler(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


def test_create_retries_through_lock_contention(
    handler: GripeHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_tx = store.tx
    attempts = {"n": 0}

    def contended_tx() -> Any:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise LockNotAvailable("could not obtain lock")
        return real_tx()

    monkeypatch.setattr(store, "tx", contended_tx)
    monkeypatch.setattr(db_retry.time, "sleep", lambda _s: None)
    out = handler.put(text="filed under contention")
    assert "created gripe" in out.body
    assert attempts["n"] == 3


def test_create_lock_exhaustion_refuses_with_draft(
    handler: GripeHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def always_locked() -> Any:
        raise LockNotAvailable("still locked")

    monkeypatch.setattr(store, "tx", always_locked)
    monkeypatch.setattr(db_retry.time, "sleep", lambda _s: None)
    with pytest.raises(Upstream) as excinfo:
        handler.put(text="the draft that must survive")
    assert excinfo.value.next is not None
    assert "the draft that must survive" in excinfo.value.next


# ── root cause: no row lock on the shared tag row (0265) ─────────────────
#
# A transaction that filed a gripe (so tagged STATUS:open) and is still
# open must not block a second filer or a second tagger. Before 0265 the
# tag "upsert" was a no-op UPDATE of the STATUS:open row — a FOR UPDATE
# lock held to commit — so the second caller waited out lock_timeout.
# Each test commits the tag first, as in prod: the very first insert of a
# tag must still be waited on (uncommitted unique-index tuple).


@pytest.fixture
def status_open(store: Store) -> int:
    with store.tx() as conn:
        return _upsert_tag(conn, "STATUS", "open")


def test_concurrent_filings_do_not_block_on_status_open(
    store: Store, status_open: int
) -> None:
    with store.tx() as holder:
        holder.execute("SELECT public.file_gripe_readonly(%s, NULL)", ("held open",))
        with store.tx() as second:
            second.execute("SET LOCAL lock_timeout = '500ms'")
            row = second.execute(
                "SELECT public.file_gripe_readonly(%s, NULL)", ("filed alongside",)
            ).fetchone()
            assert row is not None


def test_upsert_tag_takes_no_row_lock_on_existing_tag(
    store: Store, status_open: int
) -> None:
    with store.tx() as holder:
        assert _upsert_tag(holder, "STATUS", "open") == status_open
        with store.tx() as second:
            second.execute("SET LOCAL lock_timeout = '500ms'")
            assert _upsert_tag(second, "STATUS", "open") == status_open


def test_upsert_tag_inserts_a_new_tag_once(store: Store) -> None:
    with store.tx() as conn:
        tag_id = _upsert_tag(conn, "OPEN", "gr537784-fresh-tag")
        assert _upsert_tag(conn, "OPEN", "gr537784-fresh-tag") == tag_id

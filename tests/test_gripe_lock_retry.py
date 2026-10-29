"""Gripe filing retries lock contention in-process (gr537784).

Prod roles run ``lock_timeout=5s``; a filing that waited on a sibling
writer surfaced as a bare ``Upstream LockNotAvailable`` and the gripe text
was lost. The create transaction now runs under ``retry_locked``; an
exhausted budget refuses with the draft text in ``next=``.
"""

from __future__ import annotations

from typing import Any

import pytest
from psycopg.errors import LockNotAvailable

from precis.dispatch import Hub
from precis.errors import Upstream
from precis.handlers.gripe import GripeHandler
from precis.store import Store
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

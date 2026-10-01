"""gr458351: pools left open by one-shot CLIs are closed at interpreter exit."""

from __future__ import annotations

import atexit
import weakref
from unittest.mock import MagicMock

from precis.store import pool as pool_mod


def test_create_pool_registers_atexit_close(monkeypatch) -> None:
    fake = MagicMock()
    fake.closed = False
    monkeypatch.setattr(pool_mod, "ConnectionPool", MagicMock(return_value=fake))
    registered: list = []
    monkeypatch.setattr(atexit, "register", lambda f, *a: registered.append((f, a)))

    pool_mod.create_pool("postgresql:///nope")

    assert len(registered) == 1
    func, args = registered[0]
    func(*args)
    fake.close.assert_called_once()


def test_close_at_exit_skips_already_closed_pool() -> None:
    fake = MagicMock()
    fake.closed = True
    pool_mod._close_pool_ref(weakref.ref(fake))
    fake.close.assert_not_called()

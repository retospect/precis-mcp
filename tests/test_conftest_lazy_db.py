"""Lazy session-DB setup — gr514478 S3.

``_initialise_test_db`` (session autouse) must not probe, lock or clone
anything when the collected selection contains no ``db``-marked item:
a no-DB file pays zero session-DB cost. These drive the fixture body
directly with a fake session, so the guard is tested without needing
postgres at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest

import tests.conftest as conftest


def _item(db: bool) -> SimpleNamespace:
    return SimpleNamespace(
        get_closest_marker=lambda name: object() if (db and name == "db") else None
    )


def _request(items: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(session=SimpleNamespace(items=items))


# pytest wraps a fixture function in FixtureFunctionDefinition; the
# undecorated body lives on __wrapped__ at runtime but mypy's stubs don't
# expose it — cast first so the attribute access is typed Any.
def _fixture_gen(request: SimpleNamespace) -> Iterator[None]:
    return cast(Any, conftest._initialise_test_db).__wrapped__(request)


def test_no_db_items_skips_pg_probe_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        conftest,
        "_pg_available",
        lambda: pytest.fail("_pg_available probed for a no-DB selection"),
    )
    gen = _fixture_gen(_request([_item(False), _item(False)]))
    next(gen)
    with pytest.raises(StopIteration):
        next(gen)


def test_db_item_consults_pg(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []

    def _fake_pg_available() -> bool:
        called.append(True)
        return False

    monkeypatch.setattr(conftest, "_pg_available", _fake_pg_available)
    gen = _fixture_gen(_request([_item(False), _item(True)]))
    next(gen)
    with pytest.raises(StopIteration):
        next(gen)
    assert called, "_pg_available was never consulted for a db-marked selection"

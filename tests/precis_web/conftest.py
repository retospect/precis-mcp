"""Fixtures for the precis_web route tests.

The routes read structured data off a ``Store`` and route writes
through ``runtime.dispatch_with_status``. These fakes implement just
enough of both surfaces to exercise every route without a Postgres
connection: ``list_refs`` / ``search_refs_lexical`` / ``fetch_refs_by_ids``
return canned refs, and the fake pool's cursor returns empty result
sets (so the tag-join / status SQL degrades cleanly — exactly the
defensive path the ``_safe`` wrapper and tag defaults are built for).
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis_web.app import create_app
from precis_web.config import WebConfig


def make_ref(**kw: Any) -> SimpleNamespace:
    """A duck-typed ``Ref`` carrying the attrs the routes read."""
    base = {
        "id": 1,
        "kind": "todo",
        "slug": None,
        "title": "untitled",
        "year": None,
        "parent_id": None,
        "pdf_sha256": None,
        "authors": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeCursor:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchall(self) -> list[Any]:
        return self._rows

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeConn:
    def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        return _FakeCursor([])


class _FakePool:
    @contextmanager
    def connection(self):  # type: ignore[no-untyped-def]
        yield _FakeConn()


class FakeStore:
    def __init__(self) -> None:
        self.pool = _FakePool()
        self.todos = [
            make_ref(id=1, kind="todo", title="Build the thing", parent_id=None),
            make_ref(id=2, kind="todo", title="Draft the spec", parent_id=1),
        ]
        self.papers = [
            make_ref(
                id=10,
                kind="paper",
                slug="smith2024",
                title="A paper",
                year=2024,
                pdf_sha256="abc",
            ),
        ]

    def list_refs(self, *, kind: str | None = None, limit: int = 50, **kw: Any):
        if kind == "todo":
            return list(self.todos)
        if kind == "paper":
            return list(self.papers)
        return []

    def search_refs_lexical(self, *, q: str, kind: str | None = None, limit: int = 50):
        refs = self.list_refs(kind=kind, limit=limit)
        return [(r, 1.0) for r in refs]

    def fetch_refs_by_ids(self, ids, *, include_deleted: bool = False):
        pool = {r.id: r for r in self.todos + self.papers}
        return {i: pool[i] for i in ids if i in pool}


class FakeRuntime:
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def dispatch_with_status(self, verb: str, args: dict[str, Any]) -> tuple[str, bool]:
        self.calls.append((verb, dict(args)))
        return (f"[{verb}] ok", False)


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(FakeStore())


@pytest.fixture
def client(runtime: FakeRuntime, tmp_path) -> TestClient:
    cfg = WebConfig(corpus_dir=tmp_path)
    app = create_app(runtime=runtime, web_config=cfg)
    return TestClient(app)

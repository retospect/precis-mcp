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
        "updated_at": None,
        "meta": {},
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
                authors=[{"family": "Smith", "given": "Jane"}],
                meta={"abstract": "<jats:p>We study <b>X</b> in depth.</jats:p>"},
            ),
            make_ref(
                id=11,
                kind="paper",
                slug="jones2025",
                title="Another paper",
                year=2025,
                pdf_sha256="def",
                authors=[{"family": "Jones", "given": "Bob"}],
                meta={},  # no publisher abstract -> backfilled from chunks
            ),
        ]
        self.memories = [
            make_ref(id=20, kind="memory", title="A decision"),
            make_ref(id=21, kind="memory", title="An idea"),
        ]
        self.oracles = [
            make_ref(id=30, kind="oracle", slug="planck-constant", title="Planck"),
        ]
        self.convs = [
            make_ref(
                id=40,
                kind="conv",
                slug="discord/111/222/333",
                title="A thread",
            ),
        ]
        # Canned turns for conv id=40, keyed by ref_id. Blocks expose
        # pos / text / meta (author, ts) like the real Block dataclass.
        self._conv_blocks: dict[int, list[Any]] = {
            40: [
                SimpleNamespace(
                    pos=0,
                    text="hello there",
                    meta={"author": "alice", "ts": "2026-06-14T20:00:00Z"},
                ),
                SimpleNamespace(
                    pos=1,
                    text="general kenobi",
                    meta={"author": "bob", "ts": "2026-06-14T20:01:00Z"},
                ),
            ]
        }

    def _for_kind(self, kind: str | None) -> list[Any]:
        return {
            "todo": self.todos,
            "paper": self.papers,
            "memory": self.memories,
            "oracle": self.oracles,
            "conv": self.convs,
        }.get(kind or "", [])

    def list_blocks_for_ref(self, ref_id: int, **kw: Any) -> list[Any]:
        return list(self._conv_blocks.get(ref_id, []))

    def list_refs(
        self,
        *,
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
        **kw: Any,
    ):
        return list(self._for_kind(kind))[offset : offset + limit]

    def search_refs_lexical(
        self,
        *,
        q: str,
        kind: str | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
    ):
        return [(r, 1.0) for r in self._for_kind(kind)[:limit]]

    def fetch_refs_by_ids(self, ids, *, include_deleted: bool = False):
        pool = {
            r.id: r
            for r in self.todos
            + self.papers
            + self.memories
            + self.oracles
            + self.convs
        }
        return {i: pool[i] for i in ids if i in pool}

    def abstract_previews(self, ref_ids, *, max_chars: int = 900):
        # Stand in for the leading-chunk backfill: only paper 11 has a
        # body-derived abstract under the fake.
        canned = {11: "Body-derived abstract text for the second paper."}
        return {i: canned[i] for i in ref_ids if i in canned}

    def identifiers_for_refs(self, ref_ids):
        # Paper 10 carries a DOI; paper 11 an arXiv id — exercises both
        # hover-card link branches.
        canned = {
            10: {"doi": "10.1234/example.2024"},
            11: {"arxiv": "2501.01234"},
        }
        return {i: canned[i] for i in ref_ids if i in canned}

    def locked_ref_ids(self, ref_ids):
        # No live Postgres locks under the fake; the Tasks tab's
        # processing probe degrades to "nothing locked".
        return set()

    def events_for(self, ref_id, *, limit: int = 100, **kw: Any):
        # One canned status:done event so the history fragment has a
        # row to render; other refs return an empty log.
        if ref_id == 2:
            from datetime import UTC, datetime

            return [
                SimpleNamespace(
                    ts=datetime(2026, 6, 14, 20, 0, tzinfo=UTC),
                    event="status:done",
                    source="web:owner",
                )
            ]
        return []


class FakeRuntime:
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: Verbs the fake should report as failures (is_error=True), so
        #: the error-surfacing routes can be exercised without a real
        #: handler raising. The body mimics a handler BadInput message.
        self.error_verbs: set[str] = set()

    def dispatch_with_status(self, verb: str, args: dict[str, Any]) -> tuple[str, bool]:
        self.calls.append((verb, dict(args)))
        if verb in self.error_verbs:
            return (f"invalid {verb}: rejected by handler", True)
        return (f"[{verb}] ok", False)


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(FakeStore())


@pytest.fixture
def client(runtime: FakeRuntime, tmp_path) -> TestClient:
    cfg = WebConfig(corpus_dir=tmp_path)
    app = create_app(runtime=runtime, web_config=cfg)
    return TestClient(app)

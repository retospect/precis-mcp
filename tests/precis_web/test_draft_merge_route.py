"""``POST /drafts/{ident}/block/{handle}/merge-prev`` — real-Postgres
regression tests for the atomic backspace-merge (gr176088 part 2b).

Runs against the live ``store`` fixture (via ``runtime_with_store``, the
shared full-``boot()`` runtime — the route reaches
``hub.handler_for("draft")._sync_draft_links``, which needs a real
registered ``DraftHandler``, not the ``FakeRuntime``/``FakeStore`` pair the
rest of ``tests/precis_web`` uses). See CLAUDE.md "psycopg % LIKE /
fake-store gap" for why some routes need this real-store companion.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.store.store import Store
from precis_web.app import create_app
from precis_web.config import WebConfig


def _project(store: Store) -> int:
    return store.insert_ref(kind="todo", slug=None, title="Merge-route project").id


def _seed(store: Store, slug: str) -> tuple[Store, str, str]:
    """Draft ``slug`` with two mergeable paragraphs; returns (store, p1, p2)
    handles."""
    proj = _project(store)
    ref, title = store.drafts.create_draft(name=slug, title="T", project_ref_id=proj)
    p1 = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="Hello",
        at={"after": title.handle},
    )[0]
    # add_chunks trims a bare block's trailing space; edit_text preserves it
    store.drafts.edit_text(p1.handle, "Hello ")
    p2 = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="world",
        at={"after": "¶" + p1.handle},
    )[0]
    return store, p1.handle, p2.handle


def test_merge_prev_route_stale_base_sha_returns_409(
    runtime_with_store, store, monkeypatch
) -> None:
    """A concurrent edit to ``prev`` landing between the route's own read
    (``reading_order``, which resolves ``prev``) and its merge call must
    409 (the same conflict shape ``split_block`` returns), not silently
    drop the concurrent writer's text. The route reads and writes within a
    single handler with no natural pause, so — mirroring the
    ``_substitute`` race test's technique — the race is injected via a
    monkeypatch on ``reading_order`` that lands the concurrent edit right
    after the route's own read returns."""
    from precis.store._draft_ops import DraftStore

    _, p1, p2 = _seed(store, "mp1")
    real_reading_order = DraftStore.reading_order

    def _racing_reading_order(self, ref_id, **kw):
        order = real_reading_order(self, ref_id, **kw)
        store.drafts.edit_text(p1, "Hello there ")  # a concurrent writer lands
        return order

    monkeypatch.setattr(DraftStore, "reading_order", _racing_reading_order)

    app = create_app(runtime=runtime_with_store, web_config=WebConfig(corpus_dir=None))
    client = TestClient(app)
    resp = client.post(f"/drafts/mp1/block/{p2}/merge-prev", data={"text": "world"})

    assert resp.status_code == 409
    assert "changed since you read" in resp.json()["error"]
    # neither side was touched
    prev = store.drafts.get_draft_chunk(p1)
    assert prev is not None and prev.text == "Hello there "
    retiree = store.drafts.get_draft_chunk(p2)
    assert retiree is not None and retiree.text == "world" and not retiree.retired


def test_merge_prev_route_happy_path(runtime_with_store, store) -> None:
    _, p1, p2 = _seed(store, "mp2")

    app = create_app(runtime=runtime_with_store, web_config=WebConfig(corpus_dir=None))
    client = TestClient(app)
    resp = client.post(f"/drafts/mp2/block/{p2}/merge-prev", data={"text": "world"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["handle"] == p1
    assert body["caret"] == len("Hello ")
    merged = store.drafts.get_draft_chunk(p1)
    assert merged is not None and merged.text == "Hello world"
    retired = store.drafts.get_draft_chunk(p2)
    assert retired is not None and retired.retired

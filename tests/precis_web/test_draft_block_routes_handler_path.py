"""Web inline-editor mutations route through ``DraftHandler`` (gr343752).

The block routes used to call ``store.drafts.add_chunks`` / ``edit_text`` /
``retire_draft`` directly, skipping the handler's machine-owned-body
refusal (a quest dossier's ledger is a machine invariant). Each route must
now refuse a dossier-owned draft exactly as the MCP verbs do, and an
ordinary draft must behave as before.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.quest.dossier import ensure_dossier
from precis.store.store import Store
from precis_web.app import create_app
from precis_web.config import WebConfig


def _client(runtime_with_store: Any) -> TestClient:
    app = create_app(runtime=runtime_with_store, web_config=WebConfig(corpus_dir=None))
    return TestClient(app, raise_server_exceptions=False)


def _dossier(store: Store) -> tuple[int, str, str]:
    owner = store.insert_ref(kind="todo", slug=None, title="Owner process")
    ref_id = ensure_dossier(store, owner.id)
    ref = store.get_ref(kind="draft", id=ref_id)
    assert ref is not None and ref.slug is not None
    body = store.drafts.reading_order(ref_id)
    narrative = next(c for c in body if c.chunk_kind != "heading")
    return ref_id, ref.slug, narrative.handle


def _texts(store: Store, ref_id: int) -> list[str | None]:
    return [c.text for c in store.drafts.reading_order(ref_id)]


def test_add_block_refused_on_dossier(runtime_with_store: Any, store: Store) -> None:
    ref_id, slug, h = _dossier(store)
    before = _texts(store, ref_id)
    resp = _client(runtime_with_store).post(
        f"/drafts/{slug}/block", data={"after": h, "chunk_kind": "paragraph"}
    )
    assert resp.status_code == 400
    assert _texts(store, ref_id) == before


def test_split_block_refused_on_dossier(runtime_with_store: Any, store: Store) -> None:
    ref_id, slug, h = _dossier(store)
    before = _texts(store, ref_id)
    resp = _client(runtime_with_store).post(
        f"/drafts/{slug}/block/{h}/split", data={"before": "x", "after": "y"}
    )
    assert resp.status_code == 400
    assert _texts(store, ref_id) == before


def test_delete_draft_refused_on_dossier(runtime_with_store: Any, store: Store) -> None:
    ref_id, slug, _h = _dossier(store)
    before = _texts(store, ref_id)
    resp = _client(runtime_with_store).post(
        f"/drafts/{slug}/delete", data={"confirm": slug}, follow_redirects=False
    )
    assert resp.status_code == 400
    assert _texts(store, ref_id) == before


def test_ordinary_draft_block_flow_and_delete(
    runtime_with_store: Any, store: Store
) -> None:
    proj = store.insert_ref(kind="todo", slug=None, title="P").id
    ref, title = store.drafts.create_draft(
        name="plainweb", title="T", project_ref_id=proj
    )
    p1 = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="Hello world",
        at={"after": title.handle},
    )[0]
    c = _client(runtime_with_store)
    r = c.post("/drafts/plainweb/block", data={"after": p1.handle})
    assert r.status_code == 200 and r.json()["ok"]
    r = c.post(
        f"/drafts/plainweb/block/{p1.handle}/split",
        data={"before": "Hello", "after": "world"},
    )
    assert r.status_code == 200 and r.json()["ok"]
    new = r.json()["handle"]
    r = c.post(f"/drafts/plainweb/block/{new}/merge-prev", data={"text": "world"})
    assert r.status_code == 200 and r.json()["ok"]
    r = c.post(
        "/drafts/plainweb/delete",
        data={"confirm": "plainweb"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/drafts"
    assert store.drafts.reading_order(ref.id) == []

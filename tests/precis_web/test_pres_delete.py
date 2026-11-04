"""Presentation reader and Drive soft deletion, with the shared restore route."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from precis.dispatch import Hub
from precis.handlers.presentation import PresentationHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis_web.app import create_app
from precis_web.config import WebConfig

from .conftest import FakeRuntime


def test_pres_reader_has_confirmed_delete_form(client: TestClient) -> None:
    response = client.get("/pres/2001-lecture01")

    assert response.status_code == 200
    assert 'action="/pres/60/delete"' in response.text
    assert "Delete presentation" in response.text
    assert 'onsubmit="return confirm(' in response.text
    assert "Delete presentation #60?" in response.text
    assert "This soft-deletes it (removes it from listings)." in response.text


def test_pres_reader_delete_dispatches_and_returns_to_drive(
    client: TestClient, runtime: FakeRuntime
) -> None:
    response = client.post("/pres/60/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/drive"
    assert ("delete", {"kind": "pres", "id": 60}) in runtime.calls


@pytest.mark.parametrize("ref_id", [10, 999999])
def test_pres_reader_delete_rejects_wrong_kind_or_missing_ref(
    client: TestClient, runtime: FakeRuntime, ref_id: int
) -> None:
    response = client.post(f"/pres/{ref_id}/delete", follow_redirects=False)

    assert response.status_code == 404
    assert not any(verb == "delete" for verb, _args in runtime.calls)


@pytest.mark.parametrize("path", ["/pres/60/delete", "/drive/item/pres/60/delete"])
def test_pres_delete_surfaces_handler_errors(
    client: TestClient, runtime: FakeRuntime, path: str
) -> None:
    runtime.error_verbs.add("delete")

    response = client.post(path, follow_redirects=False)

    assert response.status_code == 400
    assert "rejected by handler" in response.text
    assert "location" not in response.headers


@pytest.mark.parametrize("entry", ["reader", "drive-slug", "drive-numeric"])
def test_pres_delete_and_undelete_round_trip(
    runtime_with_store: PrecisRuntime, store: Store, tmp_path: Path, entry: str
) -> None:
    """Exercise the HTTP door through real dispatch, storage and audit triggers."""
    handler = PresentationHandler(hub=Hub(store=store))
    slug = "presentation-delete-round-trip"
    handler.put(id=slug, title="Restore this deck", text="Keep this slide.", pos=0)
    ref = store.get_ref(kind="pres", id=slug)
    assert ref is not None
    handler.put(id=str(ref.id), title="Keep the numeric slug", text="Another deck.")
    other = store.get_ref(kind="pres", id=str(ref.id))
    assert other is not None and other.id != ref.id
    blocks = store.chunks.list_chunks_for_ref(ref.id)
    paths = {
        "reader": f"/pres/{ref.id}/delete",
        "drive-slug": f"/drive/item/pres/{slug}/delete",
        "drive-numeric": f"/drive/item/pres/{ref.id}/delete",
    }
    app = create_app(
        runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
    )
    with TestClient(app) as client:
        deleted = client.post(paths[entry], follow_redirects=False)
        assert deleted.status_code == 303, deleted.text
        assert deleted.headers["location"] == "/drive"
        assert store.get_ref(kind="pres", id=ref.id) is None
        assert store.get_ref(kind="pres", id=other.id) is not None
        assert len(store.events_for(ref.id, source="db", event="retired")) == 1

        tombstone = client.get(f"/refs/pres/{ref.id}")
        assert tombstone.status_code == 404
        assert f'action="/refs/pres/{ref.id}/undelete"' in tombstone.text

        restored = client.post(f"/refs/pres/{ref.id}/undelete", follow_redirects=False)
        assert restored.status_code == 303, restored.text
        assert restored.headers["location"] == f"/refs/pres/{ref.id}"

    live = store.get_ref(kind="pres", id=ref.id)
    assert live is not None and live.retired_at is None
    assert store.chunks.list_chunks_for_ref(ref.id) == blocks
    assert len(store.events_for(ref.id, source="db", event="restored")) == 1

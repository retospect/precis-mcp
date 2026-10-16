"""``/preview/<kind>/<id>`` must not leak another owner's profile (gr490266)."""

from __future__ import annotations

import base64
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.handlers.profile import ProfileHandler
from precis.users import hash_password
from precis_web.app import create_app
from precis_web.config import WebConfig

pytestmark = pytest.mark.db


@pytest.fixture
def authed_client(runtime_with_store: Any) -> TestClient:
    cfg = WebConfig(corpus_dir=None, auth_required=True)
    return TestClient(create_app(runtime=runtime_with_store, web_config=cfg))


def _basic(login: str, password: str) -> dict[str, str]:
    raw = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _seed_reto_profile(store: Any) -> Any:
    store.create_web_user(login="reto", abbrev="rs", password=hash_password("pw"))
    store.create_web_user(login="alice", abbrev="al", password=hash_password("pw"))
    store.upsert_kinds([ProfileHandler.spec])
    return store.insert_ref(
        kind="profile",
        slug=None,
        title="SecretPalette",
        meta={"profile": {"name": "SecretPalette"}},
        owner_login="reto",
    )


@pytest.mark.parametrize("url_kind", ["profile", "todo"])
def test_preview_hides_other_owners_profile(
    authed_client: TestClient, store: Any, url_kind: str
) -> None:
    prof = _seed_reto_profile(store)
    r = authed_client.get(
        f"/preview/{url_kind}/{prof.id}", headers=_basic("alice", "pw")
    )
    assert "SecretPalette" not in r.text


def test_preview_shows_profile_to_its_owner(
    authed_client: TestClient, store: Any
) -> None:
    prof = _seed_reto_profile(store)
    r = authed_client.get(f"/preview/profile/{prof.id}", headers=_basic("reto", "pw"))
    assert "SecretPalette" in r.text

"""``/refs/profile*`` is closed (gr490266): the generic refs browser has no
owner check, so kind='profile' is refused for everyone; owners use /account."""

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


def _seed(store: Any) -> Any:
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


@pytest.mark.parametrize("who", ["alice", "reto"])
def test_refs_profile_browser_is_closed(
    authed_client: TestClient, store: Any, who: str
) -> None:
    prof = _seed(store)
    h = _basic(who, "pw")
    for path in ("/refs/profile", f"/refs/profile/{prof.id}"):
        r = authed_client.get(path, headers=h)
        assert not r.is_success, path
        assert "SecretPalette" not in r.text, path


@pytest.mark.parametrize("who", ["alice", "reto"])
def test_refs_profile_undelete_is_refused(
    authed_client: TestClient, store: Any, who: str
) -> None:
    prof = _seed(store)
    store.retire_ref(prof.id)
    r = authed_client.post(
        f"/refs/profile/{prof.id}/undelete",
        headers={**_basic(who, "pw"), "Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert not r.is_success and not r.is_redirect
    assert "SecretPalette" not in r.text
    assert store.get_ref(kind="profile", id=prof.id, include_deleted=True).retired_at


def test_account_page_still_shows_owner_profile(
    authed_client: TestClient, store: Any
) -> None:
    _seed(store)
    r = authed_client.get("/account", headers=_basic("reto", "pw"))
    assert r.status_code == 200
    assert "SecretPalette" in r.text


@pytest.mark.parametrize("who", ["alice", "reto"])
def test_refs_consolidated_kinds_profile_lists_nothing(
    authed_client: TestClient, store: Any, who: str
) -> None:
    _seed(store)
    r = authed_client.get("/refs?kinds=profile", headers=_basic(who, "pw"))
    assert "SecretPalette" not in r.text


def _seed_path(store: Any) -> tuple[Any, int, int]:
    prof = _seed(store)
    t1 = store.insert_ref(kind="memory", slug=None, title="endpoint one").id
    t2 = store.insert_ref(kind="memory", slug=None, title="endpoint two").id
    store.add_link(src_ref_id=t1, dst_ref_id=prof.id, relation="related-to")
    store.add_link(src_ref_id=prof.id, dst_ref_id=t2, relation="related-to")
    return prof, t1, t2


def test_graph_path_through_other_owners_profile_is_no_path(
    authed_client: TestClient, store: Any
) -> None:
    prof, t1, t2 = _seed_path(store)
    r = authed_client.get(
        f"/graph/path?from=memory/{t1}&to=memory/{t2}&depth=4",
        headers=_basic("alice", "pw"),
    )
    assert r.status_code == 200
    assert "SecretPalette" not in r.text
    assert f"pf{prof.id}" not in r.text
    assert "No path within depth 4" in r.text


def test_graph_path_through_own_profile_is_shown(
    authed_client: TestClient, store: Any
) -> None:
    prof, t1, t2 = _seed_path(store)
    r = authed_client.get(
        f"/graph/path?from=memory/{t1}&to=memory/{t2}&depth=4",
        headers=_basic("reto", "pw"),
    )
    assert r.status_code == 200
    assert "SecretPalette" in r.text
    assert f"pf{prof.id}" in r.text
    assert "No path" not in r.text

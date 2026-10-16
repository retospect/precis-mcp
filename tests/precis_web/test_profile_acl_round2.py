"""Profile ACL round 2 (gr490266), web side: /tags/refs, /drive and
/favorites never expose another owner's (or any deleted) profile."""

from __future__ import annotations

import base64
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.handlers.profile import ProfileHandler
from precis.store.types import Tag
from precis.users import hash_password
from precis_web.app import create_app
from precis_web.config import WebConfig

pytestmark = pytest.mark.db


@pytest.fixture
def client(runtime_with_store: Any) -> TestClient:
    cfg = WebConfig(corpus_dir=None, auth_required=True)
    return TestClient(create_app(runtime=runtime_with_store, web_config=cfg))


def _as(login: str) -> dict[str, str]:
    raw = base64.b64encode(f"{login}:pw".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _world(store: Any) -> dict[str, Any]:
    for login, ab in (("alice", "al"), ("bob", "bo")):
        store.create_web_user(login=login, abbrev=ab, password=hash_password("pw"))
    store.upsert_kinds([ProfileHandler.spec])

    def mk(title: str, owner: str) -> Any:
        ref = store.insert_ref(
            kind="profile",
            slug=None,
            title=title,
            meta={"profile": {"name": title}},
            owner_login=owner,
        )
        store.add_tag(ref.id, Tag.open("acl-r2"))
        return ref

    w = {
        "mine": mk("AlicePalette", "alice"),
        "bobs": mk("BobPalette", "bob"),
        "dead": mk("AliceDeadPalette", "alice"),
    }
    store.retire_ref(w["dead"].id)
    return w


def _leaks(text: str, w: dict[str, Any]) -> list[str]:
    needles = (
        "BobPalette",
        "AliceDeadPalette",
        f"pf{w['bobs'].id}",
        f"pf{w['dead'].id}",
    )
    return [s for s in needles if s in text]


# ── /tags/refs ──


@pytest.mark.parametrize(
    "params",
    [
        {"namespace": "OPEN", "value": "acl-r2"},
        {"namespace": "OPEN", "value": "acl-r2", "kind": "profile"},
        {"kind": "profile"},
    ],
    ids=["tag", "tag+kind", "kind-only"],
)
def test_tags_refs_hides_foreign_and_deleted_profiles(
    client: TestClient, store: Any, params: dict[str, str]
) -> None:
    w = _world(store)
    r = client.get("/tags/refs", params=params, headers=_as("alice"))
    assert r.status_code == 200
    assert "AlicePalette" in r.text
    assert _leaks(r.text, w) == []


def test_tags_refs_is_per_viewer(client: TestClient, store: Any) -> None:
    _world(store)
    p = {"kind": "profile"}
    a = client.get("/tags/refs", params=p, headers=_as("alice"))
    b = client.get("/tags/refs", params=p, headers=_as("bob"))
    assert a.status_code == b.status_code == 200
    assert "AlicePalette" in a.text and "BobPalette" not in a.text
    assert "BobPalette" in b.text and "AlicePalette" not in b.text


# ── /drive ──


@pytest.mark.parametrize(
    "qs", ["?k=profile&folder=*", "?k=profile", ""], ids=["k+anywhere", "k", "discover"]
)
def test_drive_hides_foreign_profile(client: TestClient, store: Any, qs: str) -> None:
    w = _world(store)
    r = client.get("/drive" + qs, headers=_as("alice"))
    assert r.status_code == 200
    assert _leaks(r.text, w) == []


def test_drive_own_profile_control(client: TestClient, store: Any) -> None:
    _world(store)
    r = client.get("/drive?k=profile&folder=*", headers=_as("alice"))
    assert "AlicePalette" in r.text


def test_drive_trash_never_lists_profiles(client: TestClient, store: Any) -> None:
    w = _world(store)
    for qs in ("?state=deleted", "?k=profile&folder=*&state=deleted"):
        r = client.get("/drive" + qs, headers=_as("alice"))
        assert r.status_code == 200
        assert _leaks(r.text, w) == []


# ── /favorites ──


def test_favorite_toggle_on_foreign_profile_is_404(
    client: TestClient, store: Any
) -> None:
    w = _world(store)
    h = {**_as("alice"), "Origin": "http://testserver"}
    r = client.post(
        f"/favorites/profile/{w['bobs'].id}", headers=h, follow_redirects=False
    )
    assert r.status_code == 404
    assert store.list_favorites("alice") == []


def test_favorite_toggle_on_own_profile_works(client: TestClient, store: Any) -> None:
    w = _world(store)
    h = {**_as("alice"), "Origin": "http://testserver"}
    r = client.post(
        f"/favorites/profile/{w['mine'].id}", headers=h, follow_redirects=False
    )
    assert r.status_code in (200, 303)
    assert [f.ref_id for f in store.list_favorites("alice")] == [w["mine"].id]


def test_favorites_page_drops_directly_inserted_foreign_heart(
    client: TestClient, store: Any
) -> None:
    w = _world(store)
    store.set_favorite("alice", w["bobs"].id)
    store.set_favorite("alice", w["mine"].id)
    assert [f.ref_id for f in store.list_favorites("alice")] == [w["mine"].id]
    r = client.get("/favorites", headers=_as("alice"))
    assert r.status_code == 200
    assert "AlicePalette" in r.text
    assert "BobPalette" not in r.text


# ── /drive?q=pf<N> redirect, draft marks ──


def test_drive_profile_handle_query_does_not_redirect(
    client: TestClient, store: Any
) -> None:
    w = _world(store)
    for key in ("bobs", "mine"):
        r = client.get(
            f"/drive?q=pf{w[key].id}", headers=_as("alice"), follow_redirects=False
        )
        assert r.status_code == 200, key


def test_draft_marks_store_no_profile_eye(client: TestClient, store: Any) -> None:
    from precis_web import draft_eyes

    w = _world(store)
    draft = store.insert_ref(kind="draft", slug="acl-nt", title="ACL draft", meta={})
    h = {**_as("alice"), "Origin": "http://testserver"}
    r = client.post(
        f"/drafts/{draft.id}/marks",
        json={"op": "eye", "handles": [f"pf{w['bobs'].id}", f"pf{w['mine'].id}"]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert "Palette" not in r.text and f"pf{w['bobs'].id}" not in r.text
    assert f"pf{w['mine'].id}" not in str(draft_eyes.load_marks(store, draft.id))

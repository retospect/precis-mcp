"""``/favorites`` — per-user hearts on refs (gr486409).

Route-level tests over the fake store + fake runtime (``conftest.py``);
``ref_favorites`` itself is a plain table exercised here through the
``FavoritesMixin``-shaped fake. Auth is off in the test app
(``WebConfig.auth_required`` False) — ``current_user`` would return
``None`` — so ``_client_as`` wraps the app in a tiny ASGI middleware
that stamps ``scope["state"]["web_user"]`` exactly like ``auth.py``'s
gate does.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.users import WebUser
from precis_web.app import create_app
from precis_web.config import WebConfig
from tests.precis_web.conftest import FakeRuntime


def _user(login: str) -> WebUser:
    return WebUser(
        id=1,
        login=login,
        abbrev=login[:2],
        full_name=None,
        email=None,
        disabled_at=None,
        last_login_at=None,
        created_at=None,
        updated_at=None,
    )


class _AsUser:
    """Stamp ``scope["state"]["web_user"]`` — the same key the real
    ``BasicAuthMiddleware`` writes, so ``current_user`` resolves."""

    def __init__(self, app: Any, user: WebUser) -> None:
        self.app = app
        self.user = user

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope.setdefault("state", {})["web_user"] = self.user
        await self.app(scope, receive, send)


def _client_as(runtime: FakeRuntime, login: str, tmp_path) -> TestClient:
    cfg = WebConfig(corpus_dir=tmp_path)
    app = _AsUser(create_app(runtime=runtime, web_config=cfg), _user(login))
    return TestClient(app)


def test_toggle_htmx_adds_and_renders_filled(runtime: FakeRuntime, tmp_path) -> None:
    store = runtime.store
    c = _client_as(runtime, "reto", tmp_path)
    resp = c.post(
        "/favorites/paper/10",
        data={"op": "add"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert 'aria-pressed="true"' in resp.text
    assert "♥" in resp.text
    assert store.is_favorite("reto", 10)


def test_toggle_on_off_idempotent(runtime: FakeRuntime, tmp_path) -> None:
    store = runtime.store
    c = _client_as(runtime, "reto", tmp_path)
    for _ in range(2):
        c.post("/favorites/paper/10", data={"op": "add"})
    assert len(store.favorites["reto"]) == 1
    for _ in range(2):
        c.post("/favorites/paper/10", data={"op": "remove"})
    assert not store.is_favorite("reto", 10)


def test_toggle_plain_toggle_flips(runtime: FakeRuntime, tmp_path) -> None:
    store = runtime.store
    c = _client_as(runtime, "reto", tmp_path)
    c.post("/favorites/quest/97", data={"op": "toggle"})
    assert store.is_favorite("reto", 97)
    c.post("/favorites/quest/97", data={"op": "toggle"})
    assert not store.is_favorite("reto", 97)


def test_toggle_missing_or_wrong_kind_is_404(runtime: FakeRuntime, tmp_path) -> None:
    store = runtime.store
    c = _client_as(runtime, "reto", tmp_path)
    assert c.post("/favorites/paper/999999", data={"op": "add"}).status_code == 404
    assert c.post("/favorites/todo/10", data={"op": "add"}).status_code == 404
    assert not store.favorites.get("reto")


def test_per_user_isolation(runtime: FakeRuntime, tmp_path) -> None:
    store = runtime.store
    a = _client_as(runtime, "reto", tmp_path)
    b = _client_as(runtime, "alice", tmp_path)
    a.post("/favorites/paper/10", data={"op": "add"})
    assert store.is_favorite("reto", 10)
    assert not store.is_favorite("alice", 10)
    resp = b.get("/favorites")
    assert resp.status_code == 200
    assert "A paper" not in resp.text
    resp = a.get("/favorites")
    assert "A paper" in resp.text


def test_list_newest_first(runtime: FakeRuntime, tmp_path) -> None:
    store = runtime.store
    c = _client_as(runtime, "reto", tmp_path)
    c.post("/favorites/paper/10", data={"op": "add"})
    c.post("/favorites/paper/11", data={"op": "add"})
    resp = c.get("/favorites")
    assert resp.status_code == 200
    assert resp.text.index("Another paper") < resp.text.index("A paper")


def test_list_links_reader_and_graph(runtime: FakeRuntime, tmp_path) -> None:
    store = runtime.store
    c = _client_as(runtime, "reto", tmp_path)
    c.post("/favorites/paper/10", data={"op": "add"})
    resp = c.get("/favorites")
    assert 'href="/papers/10"' in resp.text
    assert 'href="/graph/paper/10"' in resp.text


def test_unheart_from_the_list(runtime: FakeRuntime, tmp_path) -> None:
    store = runtime.store
    c = _client_as(runtime, "reto", tmp_path)
    c.post("/favorites/paper/10", data={"op": "add"})
    resp = c.post(
        "/favorites/paper/10",
        data={"op": "remove"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert 'aria-pressed="false"' in resp.text
    assert not store.is_favorite("reto", 10)
    assert "A paper" not in c.get("/favorites").text


def test_retired_ref_renders_tombstone(runtime: FakeRuntime, tmp_path) -> None:
    store = runtime.store
    c = _client_as(runtime, "reto", tmp_path)
    c.post("/favorites/paper/10", data={"op": "add"})
    store.deleted_refs[10] = store.fetch_refs_by_ids([10])[10]
    resp = c.get("/favorites")
    assert resp.status_code == 200
    assert "deleted" in resp.text
    # The tombstone row links the generic detail route (which serves the
    # tombstone view), not the paper reader or the graph page.
    assert 'href="/refs/paper/10"' in resp.text
    assert 'href="/papers/10"' not in resp.text


def test_fav_slash_command_resolves(runtime: FakeRuntime, tmp_path) -> None:
    store = runtime.store
    """``/fav`` is a served GET (the drive box's served-path jump treats
    it as a command) and lands on ``/favorites``."""
    c = _client_as(runtime, "reto", tmp_path)
    resp = c.get("/fav", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/favorites"
    # Through the drive search box: "/fav" is a lone site path the app
    # serves, so the index 302s to it instead of searching literally.
    resp = c.get("/drive", params={"q": "/fav"}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].split("?")[0] == "/fav"


def test_graph_focus_renders_heart_state(runtime: FakeRuntime, tmp_path) -> None:
    store = runtime.store
    c = _client_as(runtime, "reto", tmp_path)
    resp = c.get("/graph/quest/97")
    assert resp.status_code == 200
    assert 'hx-post="/favorites/quest/97"' in resp.text
    assert 'aria-pressed="false"' in resp.text
    store.set_favorite("reto", 97)
    resp = c.get("/graph/quest/97")
    assert 'aria-pressed="true"' in resp.text


def test_ref_detail_renders_heart(runtime: FakeRuntime, tmp_path) -> None:
    store = runtime.store
    c = _client_as(runtime, "reto", tmp_path)
    resp = c.get("/refs/memory/20")
    assert resp.status_code == 200
    assert 'hx-post="/favorites/memory/20"' in resp.text


def test_paper_reader_renders_heart(runtime: FakeRuntime, tmp_path) -> None:
    store = runtime.store
    c = _client_as(runtime, "reto", tmp_path)
    resp = c.get("/papers/10")
    assert resp.status_code == 200
    assert 'hx-post="/favorites/paper/10"' in resp.text


def test_auth_off_hides_heart_and_refuses(client: TestClient) -> None:
    """No signed-in user → no heart rendered; the routes refuse (503)
    rather than writing to a guessed login."""
    resp = client.get("/refs/memory/20")
    assert resp.status_code == 200
    assert "/favorites/memory/20" not in resp.text
    assert client.get("/favorites").status_code == 503
    assert client.post("/favorites/paper/10", data={"op": "add"}).status_code == 503


# ── real Postgres: migration 0211 + FavoritesMixin SQL ────────────────
# The ``store`` fixture name auto-marks these ``db`` (tests/conftest.py)
# and binds the real Store — the FK, ON CONFLICT idempotency and the
# refs join only exist there, not in the fake above.

import psycopg

from precis.store import Store
from precis.users import hash_password


def _mkuser(store: Store, login: str) -> None:
    store.create_web_user(login=login, abbrev=login[:2], password=hash_password("pw"))


def test_store_roundtrip_and_isolation(store: Store) -> None:
    _mkuser(store, "reto")
    _mkuser(store, "alice")
    ref = store.insert_ref(kind="paper", slug="p1", title="p1")

    assert store.set_favorite("reto", ref.id) is True
    assert store.set_favorite("reto", ref.id) is False  # idempotent
    assert store.is_favorite("reto", ref.id)
    assert not store.is_favorite("alice", ref.id)
    assert [r.ref_id for r in store.list_favorites("alice")] == []

    assert store.unset_favorite("reto", ref.id) is True
    assert store.unset_favorite("reto", ref.id) is False  # idempotent
    assert not store.is_favorite("reto", ref.id)


def test_store_list_newest_first(store: Store) -> None:
    _mkuser(store, "reto")
    a = store.insert_ref(kind="paper", slug="first", title="first")
    b = store.insert_ref(kind="paper", slug="second", title="second")
    store.set_favorite("reto", a.id)
    store.set_favorite("reto", b.id)
    rows = store.list_favorites("reto")
    assert [r.ref_id for r in rows] == [b.id, a.id]
    assert rows[0].kind == "paper" and rows[0].title == "second"
    assert rows[0].slug == "second"  # cite_key identifier join


def test_store_favorite_missing_ref_fk_violation(store: Store) -> None:
    _mkuser(store, "reto")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        store.set_favorite("reto", 999_999_999)


def test_store_favorite_missing_login_fk_violation(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="orphan", title="orphan")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        store.set_favorite("nosuchuser", ref.id)


def test_store_retired_ref_lists_as_tombstone(store: Store) -> None:
    _mkuser(store, "reto")
    ref = store.insert_ref(kind="paper", slug="gone", title="gone")
    store.set_favorite("reto", ref.id)
    store.retire_ref(ref.id)
    rows = store.list_favorites("reto")
    assert [r.ref_id for r in rows] == [ref.id]
    assert rows[0].retired_at is not None

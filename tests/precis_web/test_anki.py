"""``/anki`` — the card-forge review page (:mod:`precis_web.routes.anki`).

Route-level tests with a fake store, like ``test_account.py``: the SQL
helpers degrade to empty on the non-parsing fake pool, and the write paths
(retire / mastery stamp / queue / cap) are pinned through recording fakes.
Real-PG coverage for the raw SQL lives in ``test_anki_sql.py``.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.users import WebUser, hash_password
from precis_web.app import create_app
from precis_web.config import WebConfig

GOOD = "correct-horse"


def _user() -> WebUser:
    return WebUser(
        id=1,
        login="reto",
        abbrev="rs",
        full_name="Reto Stamm",
        email="reto@example.com",
        disabled_at=None,
        last_login_at=None,
        created_at=None,
        updated_at=None,
        has_feed_token=False,
    )


class _FakeCursor:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchall(self) -> list[Any]:
        return self._rows

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeConn:
    """Empty result sets; UPDATE/INSERT statements are recorded."""

    def __init__(self, store: FakeStore) -> None:
        self._store = store

    def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            self._store.sql_writes.append((sql, params))
        return _FakeCursor([])


class _FakePool:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    @contextmanager
    def connection(self):
        yield _FakeConn(self._store)


class FakeStore:
    def __init__(self, *, password: str = GOOD) -> None:
        self.user = _user()
        self.record = hash_password(password)
        self.pool = _FakePool(self)
        self.retired: list[int] = []
        self.meta_writes: list[tuple[int, dict[str, Any]]] = []
        self.sql_writes: list[tuple[str, Any]] = []
        self.refs: dict[int, Any] = {}
        self.queued: list[dict[str, Any]] = []

    # ── the gate's reads ────────────────────────────────────────────
    def count_web_users(self, *, enabled_only: bool = True) -> int:
        return 1

    def get_web_user(self, login: str) -> WebUser | None:
        return self.user if login == self.user.login else None

    def get_web_user_credentials(self, login: str):
        if login != self.user.login:
            return None
        return self.user, self.record

    def get_web_user_by_feed_token(self, digest: str) -> WebUser | None:
        return None

    def touch_web_user_login(self, login: str) -> None:
        pass

    # ── the route's writes ─────────────────────────────────────────
    def get_ref(self, *, kind: str, id: Any, include_deleted: bool = False):
        ref = self.refs.get(id)
        if ref is not None and ref.kind == kind:
            return ref
        return None

    def retire_ref(self, ref_id: int, *, conn: Any = None) -> None:
        self.retired.append(ref_id)

    def update_ref(self, ref_id: int, *, meta_patch: dict[str, Any], **kw: Any):
        self.meta_writes.append((ref_id, meta_patch))
        ref = self.refs.get(ref_id)
        if ref is not None:
            ref.meta = {**(ref.meta or {}), **meta_patch}
            return ref
        return SimpleNamespace(id=ref_id, meta=meta_patch)


def _client(store: FakeStore) -> TestClient:
    cfg = WebConfig(corpus_dir=None, auth_required=True)
    app = create_app(runtime=SimpleNamespace(store=store), web_config=cfg)
    return TestClient(app)


def _auth(password: str = GOOD) -> dict[str, str]:
    raw = base64.b64encode(f"reto:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def test_page_is_behind_the_gate() -> None:
    assert _client(FakeStore()).get("/anki").status_code == 401


def test_page_renders_for_the_signed_in_user() -> None:
    r = _client(FakeStore()).get("/anki", headers=_auth())
    assert r.status_code == 200
    assert "concepts per morning" in r.text
    assert "term (one per line)" in r.text


def test_known_stamps_mastery_and_retires() -> None:
    """No forge cards exist under the fake pool, but the mastery fold must
    still fire for each selected concept."""
    store = FakeStore()
    r = _client(store).post(
        "/anki/known",
        data={"concept": ["11", "22"]},
        headers=_auth(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    stamped = {cid for cid, _ in store.meta_writes}
    assert stamped == {11, 22}
    for _cid, patch in store.meta_writes:
        assert patch["mastery"] == 1.0 and patch["state"] == "mastered"


def test_known_with_nothing_selected_shows_an_error() -> None:
    r = _client(FakeStore()).post("/anki/known", data={}, headers=_auth())
    assert r.status_code == 200
    assert "Nothing selected" in r.text


def test_drop_retires_the_users_own_card() -> None:
    store = FakeStore()
    store.refs[9] = SimpleNamespace(id=9, kind="anki", owner_login="reto", meta={})
    r = _client(store).post(
        "/anki/drop", data={"card": "9"}, headers=_auth(), follow_redirects=False
    )
    assert r.status_code == 303
    assert store.retired == [9]


def test_drop_refuses_another_owners_card() -> None:
    store = FakeStore()
    store.refs[9] = SimpleNamespace(id=9, kind="anki", owner_login="alice", meta={})
    r = _client(store).post("/anki/drop", data={"card": "9"}, headers=_auth())
    assert r.status_code == 404
    assert store.retired == []


def test_queue_mints_asked_terms(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str | None]] = []
    from precis_web.routes import anki as anki_routes

    def _queue(store: Any, term: str, *, owner_login: Any = None, **kw: Any) -> Any:
        seen.append((term, owner_login))
        return {"created": True}

    monkeypatch.setattr(anki_routes, "queue_asked_term", _queue)
    r = _client(FakeStore()).post(
        "/anki/queue",
        data={"terms": "entropy\nperovskite\n"},
        headers=_auth(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert seen == [("entropy", "reto"), ("perovskite", "reto")]


def test_cap_writes_the_dated_key_for_today_only() -> None:
    store = FakeStore()
    r = _client(store).post(
        "/anki/cap",
        data={"per_day": "12", "today_only": "1"},
        headers=_auth(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert store.sql_writes
    key_params = [p for sql, p in store.sql_writes if "app_settings" in sql]
    assert key_params and any(
        str(p[0]).startswith("reading.cards_per_day:2") for p in key_params
    )


def test_cap_rejects_a_non_number() -> None:
    r = _client(FakeStore()).post(
        "/anki/cap", data={"per_day": "many"}, headers=_auth()
    )
    assert r.status_code == 200
    assert "whole number" in r.text

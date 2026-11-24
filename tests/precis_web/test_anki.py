"""``/anki`` — the card-forge review page (:mod:`precis_web.routes.anki`).

Route-level tests with a fake store, like ``test_account.py``: the SQL
reads degrade to empty on the non-parsing fake pool. Route responses are
checked separately from the transactional known/undo implementation.
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

    @contextmanager
    def tx(self):
        with self.pool.connection() as conn:
            yield conn

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
    assert "new cards per day" in r.text
    assert "term (one per line)" in r.text


def test_known_submits_a_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis_web.routes import anki

    calls: list[tuple[str, list[int], bool]] = []

    def mark(
        store: FakeStore, user: WebUser, ids: list[int], *, require_cards: bool
    ) -> int:
        calls.append((user.login, ids, require_cards))
        return 3

    monkeypatch.setattr(anki, "mark_known", mark)
    r = _client(FakeStore()).post(
        "/anki/known",
        data={"concept": ["11", "22"]},
        headers=_auth(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/anki?retired=3"
    assert calls == [("reto", [11, 22], False)]


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


def test_learning_drop_htmx_refreshes_the_batch_counts() -> None:
    store = FakeStore()
    store.refs[9] = SimpleNamespace(id=9, kind="anki", owner_login="reto", meta={})
    r = _client(store).post(
        "/anki/drop",
        data={"card": "9", "return_to": "learning"},
        headers={**_auth(), "HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert r.headers["HX-Redirect"] == "/learning"
    assert store.retired == [9]


def test_drop_refuses_another_owners_card() -> None:
    store = FakeStore()
    store.refs[9] = SimpleNamespace(id=9, kind="anki", owner_login="alice", meta={})
    r = _client(store).post("/anki/drop", data={"card": "9"}, headers=_auth())
    assert r.status_code == 404
    assert store.retired == []


def test_boost_stamps_the_due_marker_for_the_users_own_card() -> None:
    """Boost = ``meta.anki_boost_due`` set to today; the next anki-sync tick
    brings the mirror card's due date forward and clears the marker."""
    store = FakeStore()
    store.refs[9] = SimpleNamespace(id=9, kind="anki", owner_login="reto", meta={})
    r = _client(store).post(
        "/anki/boost", data={"card": "9"}, headers=_auth(), follow_redirects=False
    )
    assert r.status_code == 303
    patches = {cid: patch for cid, patch in store.meta_writes}
    assert patches[9]["anki_boost_due"].count("-") == 2  # an ISO date


def test_boost_refuses_another_owners_card() -> None:
    store = FakeStore()
    store.refs[9] = SimpleNamespace(id=9, kind="anki", owner_login="alice", meta={})
    r = _client(store).post("/anki/boost", data={"card": "9"}, headers=_auth())
    assert r.status_code == 404
    assert store.meta_writes == []


def test_boost_htmx_swaps_the_queued_chip() -> None:
    store = FakeStore()
    store.refs[9] = SimpleNamespace(id=9, kind="anki", owner_login="reto", meta={})
    r = _client(store).post(
        "/anki/boost",
        data={"card": "9"},
        headers={**_auth(), "HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "boost queued" in r.text
    assert {cid for cid, _ in store.meta_writes} == {9}


def test_known_htmx_refreshes_learning_to_show_undo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis_web.routes import anki

    def mark(
        store: FakeStore, user: WebUser, ids: list[int], *, require_cards: bool
    ) -> int:
        assert require_cards and ids == [11]
        return 1

    monkeypatch.setattr(anki, "mark_known", mark)
    r = _client(FakeStore()).post(
        "/anki/known",
        data={"concept": "11", "return_to": "learning"},
        headers={**_auth(), "HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert r.headers["HX-Redirect"] == "/learning"


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


def test_highlight_queues_the_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chip's POST: the selection becomes the minted card's
    definition, source stamps 'highlight', and the page handle resolves
    to the related-to target (gr743234)."""
    store = FakeStore()
    store.refs[7] = SimpleNamespace(id=7, kind="paper", meta={})
    seen: list[dict[str, Any]] = []
    from precis_web.routes import anki as anki_routes

    def _queue(store: Any, term: str, **kw: Any) -> Any:
        seen.append({"term": term, **kw})
        return {"kind": "concept", "ref_id": 42, "name": term, "created": True}

    monkeypatch.setattr(anki_routes, "queue_asked_term", _queue)
    r = _client(store).post(
        "/anki/highlight",
        data={
            "term": "band gap",
            "context": "the band gap sets which photons a solid absorbs",
            "ref": "pa7",
        },
        headers={**_auth(), "Accept": "application/json"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["ref_id"] == 42
    assert seen == [
        {
            "term": "band gap",
            "owner_login": "reto",
            "conv_ref_id": 7,
            "definition": "the band gap sets which photons a solid absorbs",
            "source": "highlight",
        }
    ]


def test_highlight_without_a_page_ref_skips_the_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page with no record handle (or a malformed one) queues fine —
    the related-to edge is decoration, not a gate."""
    seen: list[dict[str, Any]] = []
    from precis_web.routes import anki as anki_routes

    def _queue(store: Any, term: str, **kw: Any) -> Any:
        seen.append(kw)
        return {"kind": "concept", "ref_id": 9, "name": term, "created": True}

    monkeypatch.setattr(anki_routes, "queue_asked_term", _queue)
    for handle in ("", "not-a-handle", "pa404"):
        r = _client(FakeStore()).post(
            "/anki/highlight",
            data={"term": "entropy", "context": "entropy again", "ref": handle},
            headers={**_auth(), "Accept": "application/json"},
        )
        assert r.status_code == 200 and r.json()["ok"] is True
    assert [kw["conv_ref_id"] for kw in seen] == [None, None, None]


def test_highlight_validates_the_bounds() -> None:
    """Term is required; context is the 2–2000 char selection window."""
    base = {"term": "entropy", "context": "some highlighted text", "ref": ""}
    heads = {**_auth(), "Accept": "application/json"}
    for data in (
        {**base, "term": ""},
        {**base, "term": "x" * 201},
        {**base, "context": ""},
        {**base, "context": "x"},
        {**base, "context": "x" * 2001},
    ):
        r = _client(FakeStore()).post("/anki/highlight", data=data, headers=heads)
        assert r.status_code == 400, data
        assert r.json()["ok"] is False


def test_highlight_plain_form_post_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Accept: application/json (a non-chip form post) falls back to
    the review page like the textarea queue."""
    from precis_web.routes import anki as anki_routes

    monkeypatch.setattr(
        anki_routes,
        "queue_asked_term",
        lambda *a, **k: {"kind": "concept", "ref_id": 1, "name": "x", "created": True},
    )
    r = _client(FakeStore()).post(
        "/anki/highlight",
        data={"term": "entropy", "context": "highlighted bit", "ref": ""},
        headers=_auth(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/anki?queued=1"


def test_highlight_is_behind_the_gate() -> None:
    r = _client(FakeStore()).post(
        "/anki/highlight",
        data={"term": "entropy", "context": "highlighted bit", "ref": ""},
    )
    assert r.status_code == 401


def test_highlight_undo_passes_the_owner_and_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The toast's Undo binds one submission: the route hands the ref id,
    the chip's submission token and the session login to
    unqueue_asked_term (gr743234)."""
    seen: list[dict[str, Any]] = []
    from precis_web.routes import anki as anki_routes

    def _unqueue(
        store: Any, ref_id: int, *, submission: str = "", owner_login: Any = None
    ) -> Any:
        seen.append({"ref_id": ref_id, "submission": submission, "owner": owner_login})
        return {"undone": True, "retired": True}

    monkeypatch.setattr(anki_routes, "unqueue_asked_term", _unqueue)
    r = _client(FakeStore()).post(
        "/anki/highlight/undo",
        data={"ref": "42", "submission": "a" * 32},
        headers={**_auth(), "Accept": "application/json"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "undone": True, "retired": True}
    assert seen == [{"ref_id": 42, "submission": "a" * 32, "owner": "reto"}]


def test_highlight_undo_surfaces_a_stale_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumed or foreign token answers ``undone: False`` — still a
    200, the toast just reports nothing left to undo."""
    from precis_web.routes import anki as anki_routes

    monkeypatch.setattr(
        anki_routes, "unqueue_asked_term", lambda *a, **k: {"undone": False}
    )
    r = _client(FakeStore()).post(
        "/anki/highlight/undo",
        data={"ref": "42", "submission": "a" * 32},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "undone": False}


def test_highlight_undo_404s_an_unknown_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis_web.routes import anki as anki_routes

    monkeypatch.setattr(anki_routes, "unqueue_asked_term", lambda *a, **k: None)
    r = _client(FakeStore()).post(
        "/anki/highlight/undo",
        data={"ref": "42"},
        headers=_auth(),
    )
    assert r.status_code == 404


def test_highlight_page_ref_needs_to_be_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The posted page handle only links what this login could see:
    another owner's per-login ref and an owner-private profile both
    resolve to no link (gr743234 review)."""
    store = FakeStore()
    store.refs[9] = SimpleNamespace(id=9, kind="anki", meta={}, owner_login="alice")
    store.refs[5] = SimpleNamespace(id=5, kind="profile", meta={})
    seen: list[dict[str, Any]] = []
    from precis_web.routes import anki as anki_routes

    def _queue(store: Any, term: str, **kw: Any) -> Any:
        seen.append(kw)
        return {"kind": "concept", "ref_id": 1, "name": term, "created": True}

    monkeypatch.setattr(anki_routes, "queue_asked_term", _queue)
    for handle in ("ak9", "pf5"):
        r = _client(store).post(
            "/anki/highlight",
            data={"term": "entropy", "context": "highlighted bit", "ref": handle},
            headers={**_auth(), "Accept": "application/json"},
        )
        assert r.status_code == 200 and r.json()["ok"] is True
    assert [kw["conv_ref_id"] for kw in seen] == [None, None]


def test_card_this_chip_is_on_the_signed_in_page() -> None:
    """The /anki page (like every authenticated page via base.html.j2)
    ships the chip, the popover and the toast scaffold (gr743234)."""
    r = _client(FakeStore()).get("/anki", headers=_auth())
    assert r.status_code == 200
    assert 'id="cardthis-chip"' in r.text
    assert 'id="cardthis-pop"' in r.text
    assert 'id="cardthis-toast"' in r.text
    assert 'data-shortcut="c"' in r.text


def test_card_this_chip_stays_off_a_logged_out_render() -> None:
    """base.html.j2 gates the whole chip block on nav_user — a logged-out
    (or auth-off) render ships none of it."""
    from precis_web.deps import templates

    tpl = templates.env.get_template("base.html.j2")
    off = tpl.render(nav_user=None)
    on = tpl.render(nav_user=SimpleNamespace(login="reto", abbrev="rs"))
    for marker in ("cardthis-chip", "cardthis-pop", "cardthis-toast"):
        assert f'id="{marker}"' not in off
        assert f'id="{marker}"' in on


def test_drop_source_rejects_unknown_source_and_bad_date() -> None:
    c = _client(FakeStore())
    bad_source = c.post(
        "/anki/drop-source",
        data={"source": "anki-foreign", "date": "2026-11-20"},
        headers=_auth(),
    )
    bad_date = c.post(
        "/anki/drop-source",
        data={"source": "walk", "date": "yesterday"},
        headers=_auth(),
    )
    assert bad_source.status_code == 422
    assert bad_date.status_code == 422

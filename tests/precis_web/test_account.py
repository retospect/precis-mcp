"""``/account`` — the signed-in user's own page
(:mod:`precis_web.routes.account`).

Driven through the real ``create_app`` with the gate on, because the
page's whole premise is the identity the gate parks on the request: a
test that stubbed that out would pass while the wiring was broken.

The fake store is a writable extension of the gate's read-only one — the
point of most of these is what happens to the *stored* row.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.users import (
    ALGO_PEPPERED,
    MIN_PASSWORD_LENGTH,
    PasswordRecord,
    WebUser,
    feed_token_digest,
    hash_password,
    verify_password,
)
from precis_web.app import create_app
from precis_web.config import WebConfig

GOOD = "correct-horse"
NEW = "battery-staple-9"


def _user(login: str = "reto") -> WebUser:
    return WebUser(
        id=1,
        login=login,
        abbrev="rs",
        full_name="Reto Stamm",
        email="reto@example.com",
        disabled_at=None,
        last_login_at=None,
        created_at=None,
        updated_at=None,
    )


class FakeStore:
    """The ``web_users`` surface the gate reads plus the writes /account does."""

    def __init__(self, *, password: str = GOOD, pepper: str | None = None) -> None:
        self.user = _user()
        self.record = hash_password(password, pepper=pepper)
        self.feed_digest: str | None = None
        self.password_writes = 0

    # ── reads (shared with the gate) ──────────────────────────────────
    def count_web_users(self, *, enabled_only: bool = True) -> int:
        return 1

    def get_web_user(self, login: str) -> WebUser | None:
        return self.user if login == self.user.login else None

    def get_web_user_credentials(self, login: str):
        if login != self.user.login:
            return None
        return self.user, self.record

    def get_web_user_by_feed_token(self, digest: str) -> WebUser | None:
        return self.user if digest and digest == self.feed_digest else None

    def touch_web_user_login(self, login: str) -> None:
        pass

    # ── writes ────────────────────────────────────────────────────────
    def set_web_user_password(self, login: str, password: PasswordRecord) -> bool:
        self.record = password
        self.password_writes += 1
        return True

    def update_web_user(self, login: str, **fields) -> bool:
        self.user = WebUser(
            **{
                **{f: getattr(self.user, f) for f in WebUser.__slots__},
                **{k: (v or None) for k, v in fields.items()},
            }
        )
        return True

    def set_web_user_feed_token(self, login: str, digest: str | None) -> bool:
        self.feed_digest = digest
        return True


def _client(store: FakeStore, **cfg_kw) -> TestClient:
    cfg = WebConfig(corpus_dir=None, auth_required=True, **cfg_kw)
    app = create_app(runtime=SimpleNamespace(store=store), web_config=cfg)
    return TestClient(app)


def _auth(password: str = GOOD, login: str = "reto") -> dict[str, str]:
    raw = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.fixture(autouse=True)
def _no_ambient_pepper(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("PRECIS_WEB_PASSWORD_PEPPER", raising=False)
    monkeypatch.setenv("PRECIS_SECRETS_FILE_DIR", str(tmp_path))


# ── the page ─────────────────────────────────────────────────────────


def test_page_shows_the_signed_in_identity() -> None:
    r = _client(FakeStore()).get("/account", headers=_auth())
    assert r.status_code == 200
    assert "reto@example.com" in r.text
    assert "Reto Stamm" in r.text


def test_the_top_bar_carries_the_abbrev_chip() -> None:
    """The nav is rendered from the middleware's identity — if that seam
    breaks, every page loses the Account link, not just this one."""
    r = _client(FakeStore()).get("/account", headers=_auth())
    assert 'href="/account"' in r.text
    assert ">RS</a>" in " ".join(r.text.split()).replace("> ", ">").replace(" <", "<")


def test_account_is_behind_the_gate() -> None:
    assert _client(FakeStore()).get("/account").status_code == 401


# ── changing the password ────────────────────────────────────────────


def _change(client: TestClient, current: str, new: str, confirm: str | None = None):
    return client.post(
        "/account/password",
        data={
            "current_password": current,
            "new_password": new,
            "confirm_password": new if confirm is None else confirm,
        },
        headers=_auth(current),
        follow_redirects=False,
    )


def test_change_password_replaces_the_stored_hash() -> None:
    store = FakeStore()
    r = _change(_client(store), GOOD, NEW)
    assert r.status_code == 303
    assert r.headers["location"] == "/account?changed=1"
    assert verify_password(NEW, store.record)
    assert not verify_password(GOOD, store.record)


def test_wrong_current_password_changes_nothing() -> None:
    """Authenticated is not the same as *at the keyboard* — browsers hold
    Basic credentials for the life of the tab."""
    store = FakeStore()
    client = _client(store)
    r = client.post(
        "/account/password",
        data={
            "current_password": "not-it-at-all",
            "new_password": NEW,
            "confirm_password": NEW,
        },
        headers=_auth(),
    )
    assert r.status_code == 200
    assert "Current password is not correct" in r.text
    assert store.password_writes == 0


def test_mismatched_confirmation_changes_nothing() -> None:
    store = FakeStore()
    r = _change(_client(store), GOOD, NEW, confirm="battery-staple-8")
    assert r.status_code == 200
    assert "don&#39;t match" in r.text or "don't match" in r.text
    assert store.password_writes == 0


def test_short_password_is_refused() -> None:
    store = FakeStore()
    r = _change(_client(store), GOOD, "a" * (MIN_PASSWORD_LENGTH - 1))
    assert r.status_code == 200
    assert str(MIN_PASSWORD_LENGTH) in r.text
    assert store.password_writes == 0


def test_the_old_password_stops_working_immediately() -> None:
    """The regression this feature is most likely to grow: the gate
    caches verified credentials for minutes, so a change that doesn't
    invalidate the cache leaves the retired password live."""
    store = FakeStore()
    client = _client(store)
    assert client.get("/account", headers=_auth(GOOD)).status_code == 200  # cache it
    assert _change(client, GOOD, NEW).status_code == 303
    assert client.get("/account", headers=_auth(GOOD)).status_code == 401
    assert client.get("/account", headers=_auth(NEW)).status_code == 200


def test_changing_the_password_adopts_the_deployment_pepper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An account created before the pepper existed upgrades on the next
    password change rather than needing a migration."""
    store = FakeStore()  # hashed plain
    monkeypatch.setenv("PRECIS_WEB_PASSWORD_PEPPER", "the-pepper")
    assert _change(_client(store), GOOD, NEW).status_code == 303
    assert store.record.password_algo == ALGO_PEPPERED
    assert verify_password(NEW, store.record, pepper="the-pepper")


# ── profile ──────────────────────────────────────────────────────────


def test_profile_save_and_clear() -> None:
    store = FakeStore()
    client = _client(store)
    r = client.post(
        "/account/profile",
        data={"full_name": "R. Stamm", "email": ""},
        headers=_auth(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert store.user.full_name == "R. Stamm"
    assert store.user.email is None


def test_profile_rejects_a_non_address() -> None:
    store = FakeStore()
    r = _client(store).post(
        "/account/profile",
        data={"full_name": "", "email": "reto-at-example"},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert "doesn" in r.text  # "doesn't look like an address"
    assert store.user.email == "reto@example.com"


# ── podcast token ────────────────────────────────────────────────────


def test_feed_token_is_shown_once_and_works() -> None:
    store = FakeStore()
    client = _client(store)
    r = client.post("/account/feed-token", data={"action": "rotate"}, headers=_auth())
    assert r.status_code == 200
    token = r.text.split("/podcast/feed.xml?t=")[1].split('"')[0]
    assert feed_token_digest(token) == store.feed_digest
    # The whole point: that URL alone reaches the feed, no Basic header.
    assert client.get(f"/podcast/feed.xml?t={token}").status_code == 200


def test_feed_token_revoke_clears_it() -> None:
    store = FakeStore()
    store.feed_digest = "deadbeef"
    r = _client(store).post(
        "/account/feed-token",
        data={"action": "revoke"},
        headers=_auth(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert store.feed_digest is None


# ── auth off ─────────────────────────────────────────────────────────


def test_with_auth_off_the_page_explains_itself_and_refuses_writes() -> None:
    """No gate means no identity — every mutation here would have to
    guess whose row to write."""
    store = FakeStore()
    app = create_app(
        runtime=SimpleNamespace(store=store),
        web_config=WebConfig(corpus_dir=None, auth_required=False),
    )
    client = TestClient(app)
    page = client.get("/account")
    assert page.status_code == 200
    assert "PRECIS_WEB_AUTH" in page.text
    assert (
        client.post(
            "/account/profile", data={"full_name": "x", "email": ""}
        ).status_code
        == 503
    )
    assert store.password_writes == 0

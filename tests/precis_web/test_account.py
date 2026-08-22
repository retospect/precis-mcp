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
import os
from dataclasses import replace
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


def _user(login: str = "reto", *, has_feed_token: bool = False) -> WebUser:
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
        has_feed_token=has_feed_token,
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
        # Mirror the real column: ``has_feed_token`` is derived from the
        # digest's presence, and /account keys its buttons off a re-read.
        self.user = replace(self.user, has_feed_token=digest is not None)
        return True


def _client(store: FakeStore, **cfg_kw) -> TestClient:
    cfg = WebConfig(corpus_dir=None, auth_required=True, **cfg_kw)
    app = create_app(runtime=SimpleNamespace(store=store), web_config=cfg)
    return TestClient(app)


def _auth(password: str = GOOD, login: str = "reto") -> dict[str, str]:
    raw = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.fixture(autouse=True)
def vault(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """An in-memory stand-in for :mod:`precis.secrets`.

    Patched at the module the helpers import, so the real
    ``remember/recall/forget_feed_token`` code runs — the thing under
    test is that wiring, not psycopg. Env wins first, exactly as
    ``get_secret`` does, so the pepper tests keep working.
    """
    from precis import secrets as vault_mod

    box: dict[str, str] = {}

    def _get(name, *, store=None, default=None):
        return os.environ.get(name) or box.get(name) or default

    monkeypatch.setattr(vault_mod, "get_secret", _get)
    monkeypatch.setattr(
        vault_mod, "set_secret", lambda n, v, *, store: box.__setitem__(n, v)
    )
    monkeypatch.setattr(
        vault_mod, "delete_secret", lambda n, *, store: box.pop(n, None)
    )
    return box


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


def test_feed_token_is_minted_and_works() -> None:
    store = FakeStore()
    client = _client(store)
    r = client.post("/account/feed-token", data={"action": "rotate"}, headers=_auth())
    assert r.status_code == 200
    token = r.text.split("/podcast/feed.xml?t=")[1].split('"')[0]
    assert feed_token_digest(token) == store.feed_digest
    # The whole point: that URL alone reaches the feed, no Basic header.
    assert client.get(f"/podcast/feed.xml?t={token}").status_code == 200


def test_the_feed_url_is_still_there_on_the_next_visit() -> None:
    """The reason the plaintext is vaulted at all.

    Show-once meant the only way to see your subscribe URL was to mint a
    new one — which unsubscribes the phone that was already working.
    """
    store = FakeStore()
    client = _client(store)
    minted = client.post(
        "/account/feed-token", data={"action": "rotate"}, headers=_auth()
    )
    token = minted.text.split("/podcast/feed.xml?t=")[1].split('"')[0]

    later = client.get("/account", headers=_auth())
    assert f"/podcast/feed.xml?t={token}" in later.text


def test_feed_token_revoke_clears_the_row_and_the_vault(vault) -> None:
    store = FakeStore()
    client = _client(store)
    client.post("/account/feed-token", data={"action": "rotate"}, headers=_auth())
    assert vault  # the plaintext was stored

    r = client.post(
        "/account/feed-token",
        data={"action": "revoke"},
        headers=_auth(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert store.feed_digest is None
    # A readable credential left behind for a link nobody can use is the
    # kind of leftover that outlives the person who forgot about it.
    assert vault == {}
    assert "/podcast/feed.xml?t=" not in client.get("/account", headers=_auth()).text


def test_a_live_link_the_vault_cant_read_says_so() -> None:
    """Minted before the vault (or on a deployment without one).

    The row still authenticates it, so the page must not claim there is
    no link — it has to offer the only fix there is, which is a new one.
    """
    store = FakeStore()
    store.user = _user(has_feed_token=True)
    store.feed_digest = "deadbeef"
    body = _client(store).get("/account", headers=_auth()).text
    assert "can&#39;t read it back" in body or "can't read it back" in body
    assert "/podcast/feed.xml?t=" not in body


def test_a_basic_authenticated_feed_still_carries_the_token() -> None:
    """Subscribing with the bare feed URL must not yield tokenless audio.

    A podcast app that *does* send Basic on the feed request often won't
    on the enclosure fetch, so the credential has to be inside the URLs
    the feed hands out either way.
    """
    store = FakeStore()
    client = _client(store)
    minted = client.post(
        "/account/feed-token", data={"action": "rotate"}, headers=_auth()
    )
    token = minted.text.split("/podcast/feed.xml?t=")[1].split('"')[0]

    feed = client.get("/podcast/feed.xml", headers=_auth())
    assert feed.status_code == 200
    assert f"?t={token}" in feed.text


def test_a_rejected_form_doesnt_read_as_a_lost_feed_link() -> None:
    """Mistyping a password must not look like "your podcast link is gone".

    The template reads *no URL on a user who has a token* as "this server
    can't show it — generate a new one". If an error re-render dropped
    the URL, a typo would talk the user into killing a subscription that
    was working fine.
    """
    store = FakeStore()
    client = _client(store)
    minted = client.post(
        "/account/feed-token", data={"action": "rotate"}, headers=_auth()
    )
    token = minted.text.split("/podcast/feed.xml?t=")[1].split('"')[0]

    rejected = client.post(
        "/account/password",
        data={
            "current_password": "not-the-password",
            "new_password": NEW,
            "confirm_password": NEW,
        },
        headers=_auth(),
    )
    assert rejected.status_code == 200
    assert "Current password is not correct." in rejected.text
    assert f"/podcast/feed.xml?t={token}" in rejected.text


def test_the_account_page_is_never_cached() -> None:
    """It carries a live credential in the body on every render now."""
    r = _client(FakeStore()).get("/account", headers=_auth())
    assert r.headers["cache-control"] == "no-store"


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

"""The precis-web Basic-auth gate — :mod:`precis_web.auth`.

Exercised through the real ``create_app`` (not a hand-built ASGI stack)
so these also pin the *wiring*: that the middleware sits outside every
router and the static mount, and that the served default is closed.

The routes themselves are irrelevant here — the gate answers before any
handler runs — so the fake store only implements the four ``web_users``
reads the gate touches.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from datetime import UTC

from fastapi.testclient import TestClient

from precis.users import PasswordRecord, WebUser, hash_password
from precis_web.app import create_app
from precis_web.config import WebConfig


def _user(login: str = "reto", *, disabled: bool = False) -> WebUser:
    return WebUser(
        id=1,
        login=login,
        abbrev="rs",
        full_name="Reto Stamm",
        email=None,
        disabled_at="2026-01-01" if disabled else None,  # type: ignore[arg-type]
        last_login_at=None,
        created_at=None,
        updated_at=None,
    )


class FakeUserStore:
    """Just the ``web_users`` surface the gate reads."""

    def __init__(
        self,
        *,
        user: WebUser | None = None,
        record: PasswordRecord | None = None,
        feed_digest: str | None = None,
    ) -> None:
        self.user = user
        self.record = record
        self.feed_digest = feed_digest
        self.credential_reads = 0
        self.touched: list[str] = []

    def count_web_users(self, *, enabled_only: bool = True) -> int:
        return 1 if self.user is not None else 0

    def get_web_user_credentials(self, login: str):
        self.credential_reads += 1
        if self.user is None or login != self.user.login:
            return None
        assert self.record is not None
        return self.user, self.record

    def get_web_user_by_feed_token(self, digest: str) -> WebUser | None:
        if self.feed_digest and digest == self.feed_digest:
            return self.user
        return None

    def touch_web_user_login(self, login: str) -> None:
        self.touched.append(login)


def _client(store: FakeUserStore, **cfg_kw) -> TestClient:
    cfg = WebConfig(corpus_dir=None, auth_required=True, **cfg_kw)
    app = create_app(runtime=SimpleNamespace(store=store), web_config=cfg)
    return TestClient(app)


def _basic(login: str, password: str) -> dict[str, str]:
    raw = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.fixture(autouse=True)
def _no_ambient_pepper(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Keep the vault/file layers of ``get_secret`` out of these tests —
    each test states its own pepper (or absence) explicitly."""
    monkeypatch.delenv("PRECIS_WEB_PASSWORD_PEPPER", raising=False)
    monkeypatch.setenv("PRECIS_SECRETS_FILE_DIR", str(tmp_path))


# ── the served default ───────────────────────────────────────────────


def test_from_env_defaults_to_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one that matters: a served process with no auth env var is
    authenticated. The bare-dataclass default is False for the test
    suite's benefit and must never become the served default."""
    monkeypatch.delenv("PRECIS_WEB_AUTH", raising=False)
    assert WebConfig.from_env().auth_required is True
    assert WebConfig().auth_required is False


@pytest.mark.parametrize("value", ["off", "0", "no", "false", "OFF"])
def test_explicit_off_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PRECIS_WEB_AUTH", value)
    assert WebConfig.from_env().auth_required is False


def test_typo_leaves_the_door_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_WEB_AUTH", "flase")
    assert WebConfig.from_env().auth_required is True


# ── the gate ─────────────────────────────────────────────────────────


def test_no_credentials_challenges() -> None:
    rec = hash_password("pw")
    client = _client(FakeUserStore(user=_user(), record=rec))
    resp = client.get("/drive", follow_redirects=False)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith("Basic realm=")


def test_good_credentials_pass_through() -> None:
    rec = hash_password("pw")
    store = FakeUserStore(user=_user(), record=rec)
    client = _client(store)
    resp = client.get("/", follow_redirects=False, headers=_basic("reto", "pw"))
    assert resp.status_code in (302, 303, 307)
    assert store.touched == ["reto"]


def test_login_is_case_folded() -> None:
    rec = hash_password("pw")
    client = _client(FakeUserStore(user=_user(), record=rec))
    resp = client.get("/", follow_redirects=False, headers=_basic("ReTo", "pw"))
    assert resp.status_code in (302, 303, 307)


def test_wrong_password_is_401() -> None:
    rec = hash_password("pw")
    client = _client(FakeUserStore(user=_user(), record=rec))
    resp = client.get("/", follow_redirects=False, headers=_basic("reto", "nope"))
    assert resp.status_code == 401


def test_unknown_user_is_401() -> None:
    rec = hash_password("pw")
    client = _client(FakeUserStore(user=_user(), record=rec))
    resp = client.get("/", follow_redirects=False, headers=_basic("mallory", "pw"))
    assert resp.status_code == 401


def test_disabled_user_is_401() -> None:
    rec = hash_password("pw")
    client = _client(FakeUserStore(user=_user(disabled=True), record=rec))
    resp = client.get("/", follow_redirects=False, headers=_basic("reto", "pw"))
    assert resp.status_code == 401


def test_garbled_header_challenges_rather_than_500s() -> None:
    rec = hash_password("pw")
    client = _client(FakeUserStore(user=_user(), record=rec))
    resp = client.get(
        "/", follow_redirects=False, headers={"Authorization": "Basic !!"}
    )
    assert resp.status_code == 401


def test_empty_roster_fails_closed_with_the_fix() -> None:
    client = _client(FakeUserStore())
    resp = client.get("/", follow_redirects=False, headers=_basic("reto", "pw"))
    assert resp.status_code == 503
    assert "precis users add" in resp.text


def test_empty_roster_explains_itself_before_prompting() -> None:
    """A fresh deploy is opened in a browser with no credentials to
    offer — a bare 401 would be a login prompt nobody can satisfy."""
    client = _client(FakeUserStore())
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 503
    assert "precis users add" in resp.text
    assert client.get("/podcast/feed.xml").status_code == 503


def test_missing_pepper_is_503_not_401() -> None:
    """A row that says it was peppered, and no pepper: an operator has to
    see a server error, not a login prompt."""
    rec = hash_password("pw", pepper="the-pepper")
    client = _client(FakeUserStore(user=_user(), record=rec))
    resp = client.get("/", follow_redirects=False, headers=_basic("reto", "pw"))
    assert resp.status_code == 503
    assert "pepper" in resp.text.lower()


def test_peppered_user_authenticates_with_the_pepper_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_WEB_PASSWORD_PEPPER", "the-pepper")
    rec = hash_password("pw", pepper="the-pepper")
    client = _client(FakeUserStore(user=_user(), record=rec))
    resp = client.get("/", follow_redirects=False, headers=_basic("reto", "pw"))
    assert resp.status_code in (302, 303, 307)


def test_healthz_stays_open() -> None:
    client = _client(FakeUserStore())
    assert client.get("/healthz").status_code == 200


def test_static_mount_is_covered() -> None:
    """A dependency-based gate would have missed the mount entirely."""
    rec = hash_password("pw")
    client = _client(FakeUserStore(user=_user(), record=rec))
    assert client.get("/static/tailwind.css").status_code == 401


def test_the_cache_skips_scrypt_but_not_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It caches the derivation, not the decision.

    Without the cache every request pays ~60 ms of scrypt — a page of
    assets would crawl and the gate would be a CPU-exhaustion lever. But
    the row still has to be read every time, or a revocation made in
    another process (``precis users disable``, over SSH) wouldn't be
    visible here until the TTL lapsed.
    """
    from precis_web import auth as auth_mod

    derivations = 0
    real = auth_mod.verify_password

    def counting(*a, **kw):
        nonlocal derivations
        derivations += 1
        return real(*a, **kw)

    monkeypatch.setattr(auth_mod, "verify_password", counting)

    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    client = _client(store)
    for _ in range(3):
        client.get("/", follow_redirects=False, headers=_basic("reto", "pw"))
    assert derivations == 1
    assert store.credential_reads == 3


def test_a_cli_password_change_locks_out_the_cached_credential() -> None:
    """The recovery path (``precis users passwd``) runs in a *different
    process* and cannot reach this process's cache. If a cache hit
    short-circuited the row read, a stolen password would keep working
    for the rest of the TTL after the operator believed they'd killed it.
    """
    store = FakeUserStore(user=_user(), record=hash_password("stolen-pw"))
    client = _client(store)
    ok = client.get("/", follow_redirects=False, headers=_basic("reto", "stolen-pw"))
    assert ok.status_code in (302, 303, 307)

    store.record = hash_password("rotated-pw")  # what the CLI just wrote

    denied = client.get(
        "/", follow_redirects=False, headers=_basic("reto", "stolen-pw")
    )
    assert denied.status_code == 401


def test_a_cli_disable_locks_out_the_cached_credential() -> None:
    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    client = _client(store)
    assert client.get(
        "/", follow_redirects=False, headers=_basic("reto", "pw")
    ).status_code in (302, 303, 307)

    store.user = _user(disabled=True)  # `precis users disable reto`

    # 401 rather than 503 because this fake keeps counting a disabled row
    # as a roster (the real store's count_web_users is enabled_only) —
    # which is what isolates the disabled branch here. The single-account
    # 503 case is covered end-to-end in tests/cli/test_users_live.py.
    assert (
        client.get(
            "/", follow_redirects=False, headers=_basic("reto", "pw")
        ).status_code
        == 401
    )


def test_auth_off_serves_open() -> None:
    app = create_app(
        runtime=SimpleNamespace(store=FakeUserStore()),
        web_config=WebConfig(corpus_dir=None, auth_required=False),
    )
    assert TestClient(app).get("/", follow_redirects=False).status_code in (
        302,
        303,
        307,
    )


# ── cross-site POSTs ─────────────────────────────────────────────────
#
# Basic auth is what creates this exposure: the browser attaches its
# cached Authorization header to a form on *any* page, so without a
# same-origin check an attacker's page could drive /console or /secrets
# with the victim's credentials. There are no cookies here to mark
# SameSite and no session to hang a token on.


def _post(client: TestClient, path: str, **headers: str):
    return client.post(
        path,
        data={"key": "x", "value": "y"},
        headers={**_basic("reto", "pw"), **headers},
        follow_redirects=False,
    )


def test_cross_site_post_is_refused() -> None:
    client = _client(FakeUserStore(user=_user(), record=hash_password("pw")))
    resp = _post(client, "/settings/set", Origin="https://evil.example")
    assert resp.status_code == 403
    assert "Cross-site" in resp.text


def test_cross_site_referer_is_refused_when_origin_is_absent() -> None:
    client = _client(FakeUserStore(user=_user(), record=hash_password("pw")))
    assert (
        _post(client, "/settings/set", Referer="https://evil.example/x").status_code
        == 403
    )


def test_same_origin_post_passes_the_check() -> None:
    client = _client(FakeUserStore(user=_user(), record=hash_password("pw")))
    resp = _post(client, "/settings/set", Origin="http://testserver")
    assert resp.status_code != 403


def test_a_headerless_post_is_allowed() -> None:
    """curl and the like send neither header and hold no ambient
    credential to be tricked into replaying — refusing them would break
    every script for no security gain."""
    client = _client(FakeUserStore(user=_user(), record=hash_password("pw")))
    assert _post(client, "/settings/set").status_code != 403


def test_cross_site_get_is_fine() -> None:
    """Only state-changing methods are checked — a link from anywhere
    should still open the app."""
    client = _client(FakeUserStore(user=_user(), record=hash_password("pw")))
    resp = client.get(
        "/settings",
        headers={**_basic("reto", "pw"), "Origin": "https://news.example"},
        follow_redirects=False,
    )
    assert resp.status_code != 403


# ── podcast: the self-authenticating prefix ──────────────────────────


def test_podcast_requires_something() -> None:
    rec = hash_password("pw")
    client = _client(FakeUserStore(user=_user(), record=rec))
    assert client.get("/podcast/feed.xml").status_code == 401


def test_podcast_accepts_basic() -> None:
    rec = hash_password("pw")
    client = _client(FakeUserStore(user=_user(), record=rec))
    resp = client.get("/podcast/feed.xml", headers=_basic("reto", "pw"))
    assert resp.status_code == 200


def test_podcast_feed_token_authorizes_and_is_threaded_into_urls(tmp_path) -> None:
    from datetime import datetime

    from precis import audio_feed
    from precis.users import feed_token_digest

    source = tmp_path / "src"
    source.mkdir()
    audio = source / "ep.mp3"
    audio.write_bytes(b"\x00" * 16)
    podcast_dir = tmp_path / "feed"
    audio_feed.publish_episode(
        podcast_dir,
        audio,
        episode_id="ep",
        title="Ep",
        description="",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    token = "s3cret-feed-token"
    store = FakeUserStore(
        user=_user(),
        record=hash_password("pw"),
        feed_digest=feed_token_digest(token),
    )
    client = _client(store, podcast_dir=podcast_dir, podcast_base_url="https://host")

    resp = client.get(f"/podcast/feed.xml?t={token}")
    assert resp.status_code == 200
    # The enclosure must carry the credential: the podcast app fetches it
    # later in a request that has no Basic header of its own.
    assert f"/podcast/audio/ep.mp3?t={token}" in resp.text

    assert client.get(f"/podcast/audio/ep.mp3?t={token}").status_code == 200
    assert client.get("/podcast/audio/ep.mp3?t=wrong").status_code == 401


# ── the whole seam, against real Postgres ────────────────────────────


def test_end_to_end_against_a_real_users_table(store) -> None:
    """CLI-shaped write → real SQL → real middleware → HTTP.

    The fakes above can't catch a mismatch between what
    ``create_web_user`` writes and what ``get_web_user_credentials``
    reads back, which is exactly the seam a first deploy would hit.
    """
    from precis.users import hash_password

    app = create_app(
        runtime=SimpleNamespace(store=store),
        web_config=WebConfig(corpus_dir=None, auth_required=True),
    )
    client = TestClient(app)

    assert client.get("/", follow_redirects=False).status_code == 503

    store.create_web_user(
        login="Reto", abbrev="RS", password=hash_password("hunter2", pepper=None)
    )
    assert client.get("/", follow_redirects=False).status_code == 401
    assert (
        client.get(
            "/", follow_redirects=False, headers=_basic("reto", "wrong")
        ).status_code
        == 401
    )
    assert client.get(
        "/", follow_redirects=False, headers=_basic("reto", "hunter2")
    ).status_code in (302, 303, 307)

    user = store.get_web_user("reto")
    assert user is not None and user.last_login_at is not None

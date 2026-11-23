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

from precis.store._secret_scope import viewer_handle
from precis.users import PasswordRecord, WebUser, hash_password
from precis_web.app import create_app
from precis_web.auth import SESSION_COOKIE, SessionTokens
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


def test_authenticated_browser_binds_secret_viewer() -> None:
    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    app = create_app(
        runtime=SimpleNamespace(store=store),
        web_config=WebConfig(corpus_dir=None, auth_required=True),
    )

    @app.get("/test-secret-viewer")
    def viewer() -> str | None:
        return viewer_handle()

    client = TestClient(app)
    assert client.get("/test-secret-viewer").status_code == 401
    assert (
        client.get("/test-secret-viewer", headers=_basic("reto", "pw")).json() == "rs"
    )
    assert client.get("/test-secret-viewer").json() == "rs"


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


def test_a_null_origin_post_is_refused() -> None:
    """``Origin: null`` is an attacker-reachable value — a sandboxed
    iframe POSTs with exactly that header — so it must stay refused.
    The legitimate-browser side of this is handled by never serving
    ``Referrer-Policy: no-referrer`` (which would null Origin even
    same-origin); see test_security_headers."""
    client = _client(FakeUserStore(user=_user(), record=hash_password("pw")))
    assert _post(client, "/settings/set", Origin="null").status_code == 403


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


@pytest.mark.parametrize(
    "path", ["/podcastfoo", "/podcasts", "/podcast-admin", "/podcastconsole"]
)
def test_a_lookalike_prefix_is_not_exempt(path: str) -> None:
    """The exemption names a subtree, not a string prefix.

    ``startswith("/podcast")`` also matched these, so they skipped auth
    and only 404'd because no such route happened to exist. Behind
    ``tailscale funnel`` that made the next route named anything like
    ``/podcasts`` unauthenticated on the public internet. 401 — the gate
    answers before routing, so a non-existent path is indistinguishable
    from a real one either way.
    """
    rec = hash_password("pw")
    client = _client(FakeUserStore(user=_user(), record=rec))
    assert client.get(path).status_code == 401


def test_the_exempt_subtree_itself_still_passes() -> None:
    """Guard the other side: don't fix the leak by closing the door.

    ``/podcast`` exactly, and anything under ``/podcast/``, must still
    reach the route's own token check rather than the middleware's 401 —
    but only when it carries a ``t`` (``/files`` always does).
    """
    from precis_web.auth import _bypasses_gate, _is_self_auth

    assert _is_self_auth("/podcast")
    assert _is_self_auth("/podcast/feed.xml")
    assert _is_self_auth("/podcast/audio/ep-1.mp3")
    assert not _is_self_auth("/podcastfoo")
    assert not _is_self_auth("/console")

    def scope(query: bytes) -> dict:
        return {"query_string": query}

    assert _bypasses_gate(scope(b"t=abc"), "/podcast/feed.xml")
    assert _bypasses_gate(scope(b"x=1&t=abc"), "/podcast/audio/ep-1.mp3")
    assert not _bypasses_gate(scope(b""), "/podcast/feed.xml")
    assert not _bypasses_gate(scope(b"t="), "/podcast/feed.xml")
    assert not _bypasses_gate(scope(b"x=t"), "/podcast")
    assert not _bypasses_gate(scope(b"t=abc"), "/podcastfoo")
    assert _bypasses_gate(scope(b""), "/files/pdf/x")


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


# ── the session cookie (Safari won't replay Basic into iframes) ──────


def test_basic_auth_mints_the_session_cookie() -> None:
    rec = hash_password("pw")
    client = _client(FakeUserStore(user=_user(), record=rec))
    resp = client.get("/", follow_redirects=False, headers=_basic("reto", "pw"))
    cookie = resp.headers.get("set-cookie", "")
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


def test_the_cookie_signs_in_without_basic() -> None:
    rec = hash_password("pw")
    store = FakeUserStore(user=_user(), record=rec)
    client = _client(store)
    client.get("/", follow_redirects=False, headers=_basic("reto", "pw"))
    # The TestClient jar replays the cookie; no Authorization header at
    # all — the iframe-subnavigation shape Safari actually sends.
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)


def test_a_still_valid_cookie_is_not_reissued() -> None:
    rec = hash_password("pw")
    client = _client(FakeUserStore(user=_user(), record=rec))
    client.get("/", follow_redirects=False, headers=_basic("reto", "pw"))
    resp = client.get("/", follow_redirects=False, headers=_basic("reto", "pw"))
    assert "set-cookie" not in resp.headers


def test_a_tampered_cookie_challenges() -> None:
    rec = hash_password("pw")
    client = _client(FakeUserStore(user=_user(), record=rec))
    client.cookies.set(SESSION_COOKIE, f"{2**33}.{'0' * 64}.reto")
    resp = client.get("/drive", follow_redirects=False)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith("Basic realm=")


def test_a_disabled_users_cookie_is_locked_out() -> None:
    """Same contract as the scrypt cache: the roster row decides on
    every request, so a disable bites the cookie holder immediately."""
    rec = hash_password("pw")
    store = FakeUserStore(user=_user(), record=rec)
    client = _client(store)
    client.get("/", follow_redirects=False, headers=_basic("reto", "pw"))
    store.user = _user(disabled=True)
    resp = client.get("/drive", follow_redirects=False)
    assert resp.status_code == 401


def test_logout_clears_the_cookie_and_never_reissues_on_401() -> None:
    rec = hash_password("pw")
    client = _client(FakeUserStore(user=_user(), record=rec))
    client.get("/", follow_redirects=False, headers=_basic("reto", "pw"))
    # Kill the jar so the logout POST carries Basic but no valid cookie —
    # the shape where a naive gate would mint a fresh session onto the
    # very response that revokes one.
    client.cookies.clear()
    resp = client.post("/account/logout", headers=_basic("reto", "pw"))
    assert resp.status_code == 401
    set_cookie = resp.headers.get("set-cookie", "")
    assert f"{SESSION_COOKIE}=" in set_cookie
    assert "Max-Age=0" in set_cookie


def test_expired_token_is_refused() -> None:
    tokens = SessionTokens(ttl=-1)
    assert tokens.verify(tokens.issue("reto")) is None


def test_token_survives_dotted_logins() -> None:
    tokens = SessionTokens()
    assert tokens.verify(tokens.issue("reto.stamm@example.com")) == (
        "reto.stamm@example.com"
    )
    assert tokens.verify(None) is None
    assert tokens.verify("garbage") is None


def test_a_bad_basic_header_denies_even_beside_a_valid_cookie() -> None:
    """Post-rotation shape: the browser replays a stale Basic credential
    while still holding a valid session cookie. The explicit credential
    is the one judged — a crisp 401 re-prompt now, not a mystery denial
    when the cookie expires hours later."""
    rec = hash_password("pw")
    client = _client(FakeUserStore(user=_user(), record=rec))
    client.get("/", follow_redirects=False, headers=_basic("reto", "pw"))
    resp = client.get("/", follow_redirects=False, headers=_basic("reto", "stale"))
    assert resp.status_code == 401


def test_cookie_requests_do_not_touch_last_login() -> None:
    """The touch write rides Basic only — cookie traffic is per-iframe
    frequency, and per-request UPDATEs are the amplification the
    credential cache exists to prevent."""
    rec = hash_password("pw")
    store = FakeUserStore(user=_user(), record=rec)
    client = _client(store)
    client.get("/", follow_redirects=False, headers=_basic("reto", "pw"))
    touched = len(store.touched)
    client.get("/", follow_redirects=False)
    client.get("/", follow_redirects=False)
    assert len(store.touched) == touched


def test_a_removed_users_valid_cookie_gets_a_challenge() -> None:
    """A cookie that verifies but names a login no longer on the roster
    must challenge (WWW-Authenticate), not just deny — the browser's
    re-prompt is the recovery path."""
    rec = hash_password("pw")
    store = FakeUserStore(user=_user(), record=rec)
    client = _client(store)
    client.get("/", follow_redirects=False, headers=_basic("reto", "pw"))
    store.user = _user(login="other")  # roster non-empty; reto is gone
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith("Basic realm=")


def test_malformed_tokens_are_refused_not_crashed() -> None:
    tokens = SessionTokens()
    assert tokens.verify("abc.x.y") is None  # non-numeric expiry
    assert tokens.verify(f"{2**33}.x.") is None  # empty login
    assert tokens.verify(f"{2**33}.x") is None  # missing login field


# ── failed attempts leave a trace, and repeat offenders get locked (gr343746)


def _ok(resp) -> bool:
    """Past the gate: ``/`` redirects to the landing page for a signed-in user."""
    return resp.status_code in (302, 303, 307)


def _failure_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if "precis-web auth: 401" in r.getMessage()
    ]


@pytest.mark.parametrize(
    ("login", "password", "disabled", "detail"),
    [
        ("mallory", "pw", False, "invalid credentials"),
        ("reto", "nope", False, "invalid credentials"),
        ("reto", "pw", True, "account disabled"),
    ],
)
def test_each_401_branch_logs_login_and_remote(
    caplog: pytest.LogCaptureFixture,
    login: str,
    password: str,
    disabled: bool,
    detail: str,
) -> None:
    """Unknown login, wrong password, disabled account: one warning each,
    naming the login and the peer address — the trace a stuffing run
    against the funnel used to leave nowhere."""
    rec = hash_password("pw")
    client = _client(FakeUserStore(user=_user(disabled=disabled), record=rec))
    with caplog.at_level("WARNING", logger="precis_web.auth"):
        assert client.get("/", headers=_basic(login, password)).status_code == 401
    (line,) = _failure_lines(caplog)
    assert detail in line
    assert f"login={login!r}" in line
    assert "from testclient" in line
    assert "(basic)" in line


def test_a_bare_challenge_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    """No credential presented is every first page load — not a failure."""
    client = _client(FakeUserStore(user=_user(), record=hash_password("pw")))
    with caplog.at_level("WARNING", logger="precis_web.auth"):
        assert client.get("/").status_code == 401
    assert _failure_lines(caplog) == []


def test_a_stale_cookie_for_a_disabled_user_is_logged_as_cookie(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rec = hash_password("pw")
    store = FakeUserStore(user=_user(), record=rec)
    client = _client(store)
    assert _ok(client.get("/", follow_redirects=False, headers=_basic("reto", "pw")))
    assert SESSION_COOKIE in client.cookies
    store.user = _user(disabled=True)  # `precis users disable`, another process
    with caplog.at_level("WARNING", logger="precis_web.auth"):
        resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 401
    (line,) = _failure_lines(caplog)
    assert "account disabled" in line and "(cookie)" in line


def test_repeated_failures_lock_the_login_before_scrypt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Past the threshold the gate answers 429 + Retry-After without
    reading the row or spending a scrypt; a locked login's correct
    password is refused too, until the window slides."""
    from precis_web.auth import _LOCKOUT_FAILURES

    rec = hash_password("pw")
    store = FakeUserStore(user=_user(), record=rec)
    client = _client(store)
    for _ in range(_LOCKOUT_FAILURES):
        assert client.get("/", headers=_basic("reto", "nope")).status_code == 401
    reads = store.credential_reads
    with caplog.at_level("WARNING", logger="precis_web.auth"):
        resp = client.get("/", headers=_basic("reto", "pw"))
    assert resp.status_code == 429
    assert int(resp.headers["retry-after"]) >= 1
    assert store.credential_reads == reads
    assert store.touched == []
    assert any("locked out login='reto'" in r.getMessage() for r in caplog.records)


def test_the_lock_is_per_address_too_at_a_higher_threshold() -> None:
    """Rotating the login on one address is the other half of a stuffing
    run — the address bucket catches it, but only past a threshold several
    times the per-login one: one person mistyping must never lock out
    everyone behind the same address."""
    from precis_web.auth import _LOCKOUT_ADDR_FAILURES, _LOCKOUT_FAILURES

    assert _LOCKOUT_ADDR_FAILURES >= 3 * _LOCKOUT_FAILURES
    rec = hash_password("pw")
    client = _client(FakeUserStore(user=_user(), record=rec))
    for i in range(_LOCKOUT_FAILURES):
        assert client.get("/", headers=_basic(f"user{i}", "pw")).status_code == 401
    assert _ok(client.get("/", follow_redirects=False, headers=_basic("reto", "pw")))
    for i in range(_LOCKOUT_FAILURES, _LOCKOUT_ADDR_FAILURES):
        assert client.get("/", headers=_basic(f"user{i}", "pw")).status_code == 401
    # (a browser without the session cookie the success above minted: with
    # one, the cookie would serve it through the lock)
    client.cookies.clear()
    assert client.get("/", headers=_basic("reto", "pw")).status_code == 429


def test_behind_the_loopback_proxy_the_address_is_the_last_forwarded_hop() -> None:
    """In production the socket peer is tailscale serve on loopback for
    EVERY caller; the address bucket keys on the hop the local proxy
    appended to X-Forwarded-For, and ignores the header entirely when the
    peer is not loopback (attacker-supplied)."""
    from precis_web.auth import _remote_addr

    loop = {"client": ("127.0.0.1", 5555)}
    xff = {b"x-forwarded-for": b"192.0.2.1, 198.51.100.7"}  # RFC 5737 TEST-NETs
    assert _remote_addr(loop, xff) == "198.51.100.7"
    assert _remote_addr(loop, {}) == "127.0.0.1"
    assert _remote_addr({"client": ("203.0.113.9", 1)}, xff) == "203.0.113.9"
    assert _remote_addr({}, xff) == "-"


def test_an_unresolvable_peer_gets_no_address_bucket() -> None:
    from precis_web.auth import BasicAuthMiddleware

    mw = BasicAuthMiddleware(lambda *a: None)  # type: ignore[arg-type]
    assert mw.failures.locked_for() == 0.0  # no keys → never locked


def test_tracker_memory_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every failed attempt inserts an attacker-chosen login key; the
    tracker evicts the stalest keys past maxsize, keeping a live lock."""
    from precis_web.auth import FailureTracker

    clock = [1000.0]
    monkeypatch.setattr("precis_web.auth.time.monotonic", lambda: clock[0])
    tracker = FailureTracker(login_failures=2, window=600.0, maxsize=8)
    for _ in range(2):
        tracker.record("login:target")  # locked, and the freshest key below
    for i in range(50):
        clock[0] += 1
        tracker.record(f"login:bogus{i}")
        tracker.record("login:target")
    assert len(tracker._hits) <= 8
    assert tracker.locked_for("login:target") > 0.0


def test_lockout_keys_on_the_canonical_login() -> None:
    """Case variants of one login share one bucket (the roster is
    lower-cased), so a run can't buy 3x the attempts by re-casing, and a
    success clears the bucket the failures were counted under."""
    from precis_web.auth import _LOCKOUT_FAILURES

    rec = hash_password("pw")
    client = _client(FakeUserStore(user=_user(), record=rec))
    variants = ["reto", "Reto", "RETO", " reto "]
    for i in range(_LOCKOUT_FAILURES):
        v = variants[i % len(variants)]
        assert client.get("/", headers=_basic(v, "nope")).status_code == 401
    assert client.get("/", headers=_basic("reto", "pw")).status_code == 429


def test_a_success_clears_the_pair_window_but_not_the_address() -> None:
    """A correct password forgets that pair's failures (a user who
    fumbled a few times is not locked out by their own success), while
    the address bucket keeps counting — a shared NAT address may carry
    both the legitimate user and the run targeting them."""
    from precis_web.auth import FailureTracker

    tracker = FailureTracker(failures=3, addr_failures=3, window=60.0)
    pair, addr = "pair:reto|1.2.3.4", "addr:1.2.3.4"
    for _ in range(2):
        tracker.record(pair, addr)
    tracker.clear(pair)
    tracker.record(pair, addr)
    assert tracker.locked_for(pair) == 0.0
    assert tracker.locked_for(addr) > 0.0


def test_the_window_slides(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis_web.auth import FailureTracker

    clock = [1000.0]
    monkeypatch.setattr("precis_web.auth.time.monotonic", lambda: clock[0])
    tracker = FailureTracker(login_failures=3, window=60.0)
    for _ in range(3):
        tracker.record("login:x")
        clock[0] += 10
    assert tracker.locked_for("login:x") == pytest.approx(30.0)
    clock[0] += 31
    assert tracker.locked_for("login:x") == 0.0
    assert tracker.locked_for("login:other") == 0.0


# ── gr730562: login+address lockout, /podcast through the gate, cookies ──
#
# Distinct client addresses are simulated the way production sees them:
# the socket peer is the loopback proxy and the real client is the last
# X-Forwarded-For hop (see ``_remote_addr``). RFC 5737 / 3849 documentation
# ranges throughout.

_A, _B, _C = "192.0.2.10", "192.0.2.20", "192.0.2.30"


def _app(store: FakeUserStore, **cfg_kw):
    cfg = WebConfig(corpus_dir=None, auth_required=True, **cfg_kw)
    return create_app(runtime=SimpleNamespace(store=store), web_config=cfg)


def _proxy(app) -> TestClient:
    """A client whose peer is the loopback proxy, with its own cookie jar."""
    return TestClient(app, client=("127.0.0.1", 50000))


def _via(addr: str, **headers: str) -> dict[str, str]:
    return {"X-Forwarded-For": addr, **headers}


def _guess(client: TestClient, addr: str, path: str = "/", login: str = "reto"):
    return client.get(
        path,
        headers=_via(addr, **_basic(login, "nope")),
        follow_redirects=False,
    )


@pytest.mark.parametrize(
    ("remote", "bucket"),
    [
        ("192.0.2.7", "192.0.2.7"),
        ("2001:db8:0:1::1", "2001:db8:0:1::/64"),
        ("2001:db8:0:1:ffff:ffff:ffff:ffff", "2001:db8:0:1::/64"),
        ("2001:db8:0:2::1", "2001:db8:0:2::/64"),
        ("::ffff:192.0.2.9", "192.0.2.9"),
        ("fe80::1%eth0", "fe80::/64"),
        ("testclient", "testclient"),
        ("-", None),
    ],
)
def test_the_address_bucket(remote: str, bucket: str | None) -> None:
    from precis_web.auth import _addr_bucket

    assert _addr_bucket(remote) == bucket


def test_podcast_basic_guessing_is_gated_logged_and_locked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The route's own Basic fallback was an unlimited guessing oracle at
    one scrypt per request. Now ``/podcast/feed.xml`` without a token is
    the gate's: logged, counted, and 429 before scrypt — against the same
    buckets as ``/``."""
    from precis_web.auth import _LOCKOUT_FAILURES

    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    client = _client(store)
    with caplog.at_level("WARNING", logger="precis_web.auth"):
        codes = [
            client.get("/podcast/feed.xml", headers=_basic("reto", "nope")).status_code
            for _ in range(30)
        ]
    assert codes == [401] * _LOCKOUT_FAILURES + [429] * (30 - _LOCKOUT_FAILURES)
    assert len(_failure_lines(caplog)) == _LOCKOUT_FAILURES
    reads = store.credential_reads
    # Same bucket as the rest of the app: the right password is refused on
    # ``/`` too, with no row read.
    assert client.get("/", headers=_basic("reto", "pw")).status_code == 429
    assert store.credential_reads == reads


def _seed_episode(tmp_path):
    from datetime import datetime

    from precis import audio_feed

    src = tmp_path / "src"
    src.mkdir()
    (src / "ep.mp3").write_bytes(b"\x00" * 16)
    podcast_dir = tmp_path / "feed"
    audio_feed.publish_episode(
        podcast_dir,
        src / "ep.mp3",
        episode_id="ep",
        title="Ep",
        description="",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    return podcast_dir


def test_a_token_request_needs_no_basic_and_a_wrong_token_never_touches_the_tracker(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    from precis.users import feed_token_digest

    token = "s3cret-feed-token"
    store = FakeUserStore(
        user=_user(),
        record=hash_password("pw"),
        feed_digest=feed_token_digest(token),
    )
    client = _client(store, podcast_dir=_seed_episode(tmp_path))
    assert client.get(f"/podcast/feed.xml?t={token}").status_code == 200
    with caplog.at_level("WARNING", logger="precis_web.auth"):
        codes = {client.get("/podcast/feed.xml?t=wrong").status_code for _ in range(30)}
    # Dead end every time, never a 429: nothing was counted, so nothing locks.
    assert codes == {401}
    assert _failure_lines(caplog) == []
    assert _ok(client.get("/", follow_redirects=False, headers=_basic("reto", "pw")))


def test_basic_and_cookie_callers_get_the_feed_with_their_own_token(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    monkeypatch.setattr(
        "precis_web.routes.podcast.recall_live_feed_token",
        lambda login, *, store: f"vaulted-for-{login}",
    )
    store = FakeUserStore(
        user=replace(_user(), has_feed_token=True), record=hash_password("pw")
    )
    client = _client(store, podcast_dir=_seed_episode(tmp_path))
    by_basic = client.get("/podcast/feed.xml", headers=_basic("reto", "pw"))
    assert by_basic.status_code == 200
    assert "/podcast/audio/ep.mp3?t=vaulted-for-reto" in by_basic.text
    # The gate minted a cookie on that response; the jar replays it.
    assert SESSION_COOKIE in client.cookies
    by_cookie = client.get("/podcast/feed.xml")
    assert by_cookie.status_code == 200
    assert "/podcast/audio/ep.mp3?t=vaulted-for-reto" in by_cookie.text
    assert client.get("/podcast/audio/ep.mp3").status_code == 200


def test_ten_wrong_guesses_from_one_address_do_not_lock_the_owner_elsewhere() -> None:
    from precis_web.auth import _LOCKOUT_FAILURES

    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    client = _proxy(_app(store))
    for _ in range(_LOCKOUT_FAILURES):
        assert _guess(client, _A).status_code == 401
    # Address A is locked for that login — even with the right password …
    right_a = client.get("/", headers=_via(_A, **_basic("reto", "pw")))
    assert right_a.status_code == 429
    # … but it is only the pair: another login from A is merely wrong,
    assert _guess(client, _A, login="someone").status_code == 401
    # and the owner at B signs in.
    right_b = client.get(
        "/", headers=_via(_B, **_basic("reto", "pw")), follow_redirects=False
    )
    assert _ok(right_b)


def test_addresses_in_one_ipv6_64_share_a_bucket() -> None:
    from precis_web.auth import _LOCKOUT_FAILURES

    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    client = _proxy(_app(store))
    in_64 = ["2001:db8:0:1::1", "2001:db8:0:1::2", "2001:db8:0:1:aaaa::3"]
    for i in range(_LOCKOUT_FAILURES):
        assert _guess(client, in_64[i % 3]).status_code == 401
    same = client.get("/", headers=_via("2001:db8:0:1:bbbb::9", **_basic("reto", "pw")))
    assert same.status_code == 429
    other = client.get(
        "/",
        headers=_via("2001:db8:0:2::1", **_basic("reto", "pw")),
        follow_redirects=False,
    )
    assert _ok(other)


@pytest.fixture
def _fast_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hundred real scrypts would be six seconds of nothing."""
    from precis_web import auth as auth_mod

    monkeypatch.setattr(
        auth_mod,
        "verify_password",
        lambda password, record, *, pepper: password == "pw",
    )


def _gate(app):
    """The live ``BasicAuthMiddleware`` instance of *app*, to read the
    tracker the gate actually wrote."""
    from precis_web.auth import BasicAuthMiddleware

    node = app.middleware_stack
    while not isinstance(node, BasicAuthMiddleware):
        node = node.app
    return node


def test_the_gate_stack_is_built_at_create_time() -> None:
    """Starlette builds ``app.middleware_stack`` lazily on first request,
    and the build races under concurrent first requests: each racing
    caller can serve its own stack — its own gate, its own failure
    tracker — splitting the lockout buckets a burst is counted against.
    ``create_app`` builds it eagerly so one tracker is shared from the
    first request on (gr778229)."""
    app = _app(FakeUserStore(user=_user(), record=hash_password("pw")))
    assert app.middleware_stack is not None
    _gate(app)  # reaches the gate without a request first


def _counting_scrypt(monkeypatch: pytest.MonkeyPatch, *, delay: float = 0.0):
    """Replace the derivation with a counter (and optional sleep)."""
    import time as time_mod

    from precis_web import auth as auth_mod

    calls = {"verify": 0, "burn": 0}

    def verify(password, record, *, pepper):
        calls["verify"] += 1
        time_mod.sleep(delay)
        return password == "pw"

    def burn():
        calls["burn"] += 1
        time_mod.sleep(delay)

    monkeypatch.setattr(auth_mod, "verify_password", verify)
    monkeypatch.setattr(auth_mod, "burn_verify", burn)
    return calls


def _snapshot(gate) -> dict[str, list[float]]:
    return {k: list(v) for k, v in gate.failures._hits.items()}


def _lock_pair_and_addr(client: TestClient, addr: str) -> None:
    """Someone else fills ``pair:reto|addr`` and ``addr:addr``."""
    from precis_web.auth import _LOCKOUT_ADDR_FAILURES, _LOCKOUT_FAILURES

    for _ in range(_LOCKOUT_FAILURES):
        assert _guess(client, addr).status_code == 401
    for i in range(_LOCKOUT_ADDR_FAILURES - _LOCKOUT_FAILURES):
        assert _guess(client, addr, login=f"u{i}").status_code == 401


def test_basic_plus_a_valid_cookie_is_served_in_a_bucket_someone_else_locked(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A browser sends its cached Basic header and the cookie together. The
    owner whose address bucket a neighbour filled keeps working — on the
    cookie: nothing verified, recorded, cleared or re-minted."""
    calls = _counting_scrypt(monkeypatch)
    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    app = _app(store)
    gate = _gate(app)
    owner = _proxy(app)
    assert _ok(
        owner.get("/", headers=_via(_A, **_basic("reto", "pw")), follow_redirects=False)
    )
    _lock_pair_and_addr(_proxy(app), _A)
    assert gate.failures.locked_for("pair:reto|192.0.2.10") > 0
    assert gate.failures.locked_for("addr:192.0.2.10") > 0
    before, verified = _snapshot(gate), calls["verify"] + calls["burn"]
    caplog.clear()

    with caplog.at_level("WARNING", logger="precis_web.auth"):
        for password in ("pw", "wrong"):  # right or wrong: the cookie serves it
            resp = owner.get(
                "/",
                headers=_via(_A, **_basic("reto", password)),
                follow_redirects=False,
            )
            assert _ok(resp), password
            assert "set-cookie" not in resp.headers
    assert _failure_lines(caplog) == []
    assert _snapshot(gate) == before  # nothing recorded, nothing cleared
    assert calls["verify"] + calls["burn"] == verified  # no scrypt


def test_a_locked_bucket_without_a_usable_cookie_is_still_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _counting_scrypt(monkeypatch)
    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    app = _app(store)
    owner = _proxy(app)
    assert _ok(
        owner.get("/", headers=_via(_A, **_basic("reto", "pw")), follow_redirects=False)
    )
    stale = owner.cookies.get(SESSION_COOKIE)
    assert stale
    _lock_pair_and_addr(_proxy(app), _A)
    right = _basic("reto", "pw")

    # No cookie: the owner's fresh browser waits out the window.
    assert _proxy(app).get("/", headers=_via(_A, **right)).status_code == 429
    # A cookie for another login does not vouch for this one.
    other = _proxy(app)
    other.cookies.set(SESSION_COOKIE, app.state.session_tokens.issue("someone"))
    assert other.get("/", headers=_via(_A, **right)).status_code == 429
    # A cookie minted before a password change: authorizes nothing.
    original = store.record
    store.record = hash_password("pw2")
    assert owner.get("/", headers=_via(_A, **right)).status_code == 429
    # A cookie for a since-disabled account likewise.
    store.record = original
    store.user = _user(disabled=True)
    assert owner.get("/", headers=_via(_A, **right)).status_code == 429


def test_a_cookie_session_is_not_refused_while_its_buckets_are_locked() -> None:
    from precis_web.auth import _LOCKOUT_FAILURES

    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    client = _client(store)
    assert _ok(client.get("/", follow_redirects=False, headers=_basic("reto", "pw")))
    # Wrong Basic beside the live cookie while NOT locked: judged (401)
    # and counted — a valid cookie buys no immunity from "Basic wins".
    for _ in range(_LOCKOUT_FAILURES):
        assert client.get("/", headers=_basic("reto", "nope")).status_code == 401
    # Now locked: cookie only, and Basic + cookie, are both served.
    assert _ok(client.get("/", follow_redirects=False))
    assert _ok(client.get("/", follow_redirects=False, headers=_basic("reto", "pw")))
    # A browser with no cookie is not.
    client.cookies.clear()
    assert client.get("/", headers=_basic("reto", "pw")).status_code == 429


def test_above_the_global_ceiling_a_cookie_is_a_fallback_not_a_trust_token(
    _fast_scrypt: None, caplog: pytest.LogCaptureFixture
) -> None:
    from precis_web.auth import _LOCKOUT_LOGIN_FAILURES

    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    app = _app(store)
    gate = _gate(app)
    owner = _proxy(app)
    # The owner signs in from A once: A is now known, and the jar holds a cookie.
    assert _ok(
        owner.get("/", headers=_via(_A, **_basic("reto", "pw")), follow_redirects=False)
    )
    # A distributed run: every guess from a fresh address, so no pair or
    # address bucket ever fills — only the login ceiling does.
    attacker = _proxy(app)
    for i in range(_LOCKOUT_LOGIN_FAILURES):
        assert _guess(attacker, f"198.51.100.{i + 1}").status_code == 401

    with caplog.at_level("WARNING", logger="precis_web.auth"):
        # An address never seen, no cookie: refused with the right password.
        stranger = attacker.get("/", headers=_via(_C, **_basic("reto", "pw")))
    assert stranger.status_code == 429
    assert int(stranger.headers["retry-after"]) >= 1
    assert any("locked out login='reto'" in r.getMessage() for r in caplog.records)
    # The known address is trusted: it signs in with a fresh browser.
    known = _proxy(app).get(
        "/", headers=_via(_A, **_basic("reto", "pw")), follow_redirects=False
    )
    assert _ok(known)
    # Never-seen address + a valid cookie: served on the cookie, recording
    # nothing (the ceiling's window is untouched) and minting nothing.
    before = _snapshot(gate)
    roaming = owner.get(
        "/", headers=_via(_C, **_basic("reto", "pw")), follow_redirects=False
    )
    assert _ok(roaming) and "set-cookie" not in roaming.headers
    assert _snapshot(gate) == before
    # The ceiling doesn't swallow other logins.
    assert _guess(attacker, _B, login="someone").status_code == 401
    # Never-seen address + a STALE cookie (password changed since): 429. The
    # old rule let any HMAC-valid cookie lift the ceiling.
    store.record = hash_password("pw2")
    stale = owner.get("/", headers=_via(_C, **_basic("reto", "pw2")))
    assert stale.status_code == 429


def test_a_burst_of_wrong_guesses_is_verified_only_up_to_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock was checked on arrival and the failure recorded after the
    scrypt, so a simultaneous burst all got verified. The guard checks
    inside the slot and records before releasing it, so for one bucket at
    most ``limit + slots - 1`` are ever derived, however many arrive."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from precis_web.auth import _LOCKOUT_FAILURES, _SCRYPT_SLOTS

    calls = _counting_scrypt(monkeypatch, delay=0.05)
    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    app = _app(store)
    n = 60
    barrier = threading.Barrier(n)

    def hit(_: int) -> int:
        client = _proxy(app)
        barrier.wait()
        return _guess(client, _A).status_code

    with ThreadPoolExecutor(n) as pool:
        codes = list(pool.map(hit, range(n)))
    verified = calls["verify"]
    bound = _LOCKOUT_FAILURES + _SCRYPT_SLOTS - 1
    print(f"pair burst: verified={verified} bound={bound} codes={sorted(set(codes))}")
    assert _LOCKOUT_FAILURES <= verified <= bound
    assert codes.count(401) == verified
    assert set(codes) <= {401, 429, 503}
    assert codes.count(429) + codes.count(503) == n - verified


def test_a_burst_across_logins_is_verified_only_up_to_the_address_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from precis_web.auth import _LOCKOUT_ADDR_FAILURES, _SCRYPT_SLOTS

    calls = _counting_scrypt(monkeypatch, delay=0.02)
    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    app = _app(store)
    n = 120
    barrier = threading.Barrier(n)

    def hit(i: int) -> int:
        client = _proxy(app)
        barrier.wait()
        return _guess(client, _A, login=f"u{i}").status_code

    with ThreadPoolExecutor(n) as pool:
        codes = list(pool.map(hit, range(n)))
    verified = calls["burn"]  # unknown logins burn, never verify
    bound = _LOCKOUT_ADDR_FAILURES + _SCRYPT_SLOTS - 1
    print(f"addr burst: verified={verified} bound={bound} codes={sorted(set(codes))}")
    assert _LOCKOUT_ADDR_FAILURES <= verified <= bound
    assert codes.count(401) == verified
    assert set(codes) <= {401, 429, 503}


def test_eviction_keeps_the_hot_keys_and_drops_the_single_hit_flood() -> None:
    """Evicting by staleness let ~2,000 junk logins reset a victim's count.
    Fewest-hits-first lets a flood of single-hit keys evict itself."""
    from precis_web.auth import FailureTracker

    tracker = FailureTracker()  # defaults: login 100, pair 10, maxsize 4096
    for _ in range(100):
        tracker.record("login:victim")  # the earliest hits of all
    for _ in range(10):
        tracker.record("pair:victim|192.0.2.10")
    for i in range(5000):
        tracker.record(f"login:junk{i}", f"pair:junk{i}|192.0.2.99")
    assert tracker.locked_for("login:victim") > 0.0
    assert tracker.locked_for("pair:victim|192.0.2.10") > 0.0
    assert len(tracker._hits) <= 4096


def test_the_tracker_survives_concurrent_writers() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from precis_web.auth import FailureTracker

    tracker = FailureTracker(login_failures=10_000, maxsize=64)

    def work(i: int) -> None:
        for j in range(300):
            tracker.record(f"login:k{(i * 7 + j) % 200}", "login:shared")
            tracker.locked_for("login:shared", f"login:k{j % 50}")

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(work, range(8)))
    assert len(tracker._hits) <= 64
    assert len(tracker._hits["login:shared"]) == 8 * 300


def test_cookie_only_401s_are_logged_but_not_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stale cookie replayed must not lock its own owner out."""
    from precis_web.auth import _LOCKOUT_FAILURES

    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    client = _client(store)
    assert _ok(client.get("/", follow_redirects=False, headers=_basic("reto", "pw")))
    store.user = _user(disabled=True)
    with caplog.at_level("WARNING", logger="precis_web.auth"):
        codes = {client.get("/").status_code for _ in range(3 * _LOCKOUT_FAILURES)}
    assert codes == {401}
    assert len(_failure_lines(caplog)) == 3 * _LOCKOUT_FAILURES
    store.user = _user()
    assert _ok(client.get("/", follow_redirects=False, headers=_basic("reto", "pw")))


def test_every_rejection_has_the_same_body_and_the_log_names_the_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    app = _app(store)
    bodies = {}
    with caplog.at_level("WARNING", logger="precis_web.auth"):
        client = _proxy(app)
        bodies["unknown"] = client.get("/", headers=_basic("mallory", "pw")).text
        bodies["wrong"] = client.get("/", headers=_basic("reto", "nope")).text
        store.user = _user(disabled=True)
        bodies["disabled"] = client.get("/", headers=_basic("reto", "pw")).text
        store.user = _user()
        assert _ok(
            client.get("/", follow_redirects=False, headers=_basic("reto", "pw"))
        )
        store.user = _user(disabled=True)
        bodies["cookie"] = client.get("/").text
    assert set(bodies.values()) == {"invalid credentials"}, bodies
    lines = _failure_lines(caplog)
    assert any("account disabled" in line and "(basic)" in line for line in lines)
    assert any("account disabled" in line and "(cookie)" in line for line in lines)


def test_a_saturated_scrypt_gate_sheds_load_with_503(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import threading

    from precis_web import auth as auth_mod
    from precis_web.auth import _LOCKOUT_FAILURES

    gate = threading.BoundedSemaphore(1)
    monkeypatch.setattr(auth_mod, "_SCRYPT_GATE", gate)
    monkeypatch.setattr(auth_mod, "_SCRYPT_WAIT", 0.05)
    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    client = _client(store)

    assert gate.acquire(timeout=1)  # every slot is taken
    with caplog.at_level("WARNING", logger="precis_web.auth"):
        # Past the pair limit: a counted failure would have locked by now.
        for login in ("reto", "mallory") * _LOCKOUT_FAILURES:
            resp = client.get("/", headers=_basic(login, "nope"))
            assert resp.status_code == 503
            assert int(resp.headers["retry-after"]) >= 1
    assert _failure_lines(caplog) == []
    gate.release()
    # Not recorded: the right password is accepted straight away.
    assert _ok(client.get("/", follow_redirects=False, headers=_basic("reto", "pw")))


def test_a_slot_is_released_to_the_gate_it_was_taken_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A swapped module gate must not receive a release for a slot taken
    from another: that would free a slot a different thread still holds
    and admit one more concurrent derivation than ``_SCRYPT_SLOTS``."""
    import threading

    from precis_web import auth as auth_mod

    real = threading.BoundedSemaphore(2)
    other = threading.BoundedSemaphore(2)
    assert real.acquire(timeout=1)  # another thread holds one real slot
    monkeypatch.setattr(auth_mod, "_SCRYPT_GATE", real)
    with auth_mod._scrypt_slot():
        monkeypatch.setattr(auth_mod, "_SCRYPT_GATE", other)
    assert real.acquire(timeout=0.05)  # only the one free slot came back
    assert not real.acquire(timeout=0.05)
    assert other.acquire(timeout=0.05) and other.acquire(timeout=0.05)


def test_a_shed_attempt_is_served_on_a_valid_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flood that holds every scrypt slot must not 503 a signed-in
    browser once its cached credential lapses: the cookie serves it, and
    a caller with no cookie is still shed."""
    import threading

    from precis_web import auth as auth_mod

    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    client = _client(store)
    assert _ok(client.get("/", follow_redirects=False, headers=_basic("reto", "pw")))
    _gate(client.app).cache.clear()  # the credential cache TTL lapsed
    gate = threading.BoundedSemaphore(1)
    assert gate.acquire(timeout=1)  # every slot is taken
    monkeypatch.setattr(auth_mod, "_SCRYPT_GATE", gate)
    monkeypatch.setattr(auth_mod, "_SCRYPT_WAIT", 0.05)

    assert _ok(client.get("/", follow_redirects=False, headers=_basic("reto", "pw")))
    client.cookies.clear()
    resp = client.get("/", follow_redirects=False, headers=_basic("reto", "pw"))
    assert resp.status_code == 503


def test_a_cache_hit_needs_no_scrypt_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from precis_web import auth as auth_mod

    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    client = _client(store)
    assert _ok(client.get("/", follow_redirects=False, headers=_basic("reto", "pw")))
    gate = threading.BoundedSemaphore(1)
    assert gate.acquire(timeout=1)
    monkeypatch.setattr(auth_mod, "_SCRYPT_GATE", gate)
    monkeypatch.setattr(auth_mod, "_SCRYPT_WAIT", 0.05)
    assert _ok(client.get("/", follow_redirects=False, headers=_basic("reto", "pw")))


def test_a_cookie_minted_before_a_password_change_is_refused_after_it() -> None:
    """``precis users passwd`` runs in another process and can't reach this
    one's state; the cookie's password tag is what makes it bite."""
    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    client = _client(store)
    assert _ok(client.get("/", follow_redirects=False, headers=_basic("reto", "pw")))
    assert _ok(client.get("/", follow_redirects=False))  # cookie alone works

    store.record = hash_password("pw2")  # what the CLI just wrote

    refused = client.get("/", follow_redirects=False)
    assert refused.status_code == 401
    assert refused.headers["www-authenticate"].startswith("Basic realm=")
    # The new password signs in and replaces the stale cookie, which then works.
    signed_in = client.get("/", follow_redirects=False, headers=_basic("reto", "pw2"))
    assert _ok(signed_in)
    assert SESSION_COOKIE in signed_in.headers.get("set-cookie", "")
    assert _ok(client.get("/", follow_redirects=False))


def test_a_cookie_minted_before_sign_out_is_refused_after_it() -> None:
    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    client = _client(store)
    assert _ok(client.get("/", follow_redirects=False, headers=_basic("reto", "pw")))
    old_cookie = client.cookies.get(SESSION_COOKIE)
    assert old_cookie

    out = client.post("/account/logout")  # the cookie alone vouches for it
    assert out.status_code == 401

    # A copy of the cookie kept elsewhere (the deletion is client-side only).
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, old_cookie)
    assert client.get("/", follow_redirects=False).status_code == 401

    # Signing straight back in works, within the same second.
    client.cookies.clear()
    assert _ok(client.get("/", follow_redirects=False, headers=_basic("reto", "pw")))
    assert _ok(client.get("/", follow_redirects=False))


def test_session_token_not_before_mark_and_tag() -> None:
    tokens = SessionTokens()
    before = tokens.issue("reto", "t1")
    claim = tokens.parse(before)
    assert claim is not None and claim.login == "reto" and claim.password_tag == "t1"
    tokens.sign_out("reto")
    other = tokens.issue("other")
    assert tokens.verify(before) is None
    after = tokens.issue("reto", "t1")  # same instant, as far as time() goes
    assert tokens.verify(after) == "reto"
    assert tokens.verify(other) == "other"  # the mark is per login
    # The tag is authenticated: editing it breaks the signature.
    expires, issued, _tag, rest = after.split(".", 3)
    assert tokens.verify(f"{expires}.{issued}.t2.{rest}") is None


def test_known_addresses_expire_and_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis_web.auth import KnownAddresses

    clock = [1000.0]
    monkeypatch.setattr("precis_web.auth.time.monotonic", lambda: clock[0])
    known = KnownAddresses(ttl=60.0, maxsize=3)
    known.add("reto", "a")
    assert ("reto", "a") in known and ("reto", "b") not in known
    assert ("reto", None) not in known
    clock[0] += 61
    assert ("reto", "a") not in known
    for b in "wxyz":
        clock[0] += 1
        known.add("reto", b)
    assert ("reto", "w") not in known  # stalest evicted
    assert all(("reto", b) in known for b in "xyz")

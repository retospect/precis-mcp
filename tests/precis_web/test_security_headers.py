"""Hardening headers ride on every response.

These went in when precis-web moved from tailnet-only to the public
internet behind ``tailscale funnel``. The framing headers are the ones
that matter: ``check_same_origin`` cannot stop a clickjack, because a
click inside a hostile ``<iframe>`` produces a request whose ``Origin``
is *ours*.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from precis.users import hash_password
from precis_web.app import create_app
from precis_web.config import WebConfig

from .test_auth import FakeUserStore, _basic, _user


def _client(*, auth: bool = True) -> TestClient:
    cfg = WebConfig(auth_required=auth)
    store = FakeUserStore(user=_user(), record=hash_password("pw"))
    app = create_app(runtime=SimpleNamespace(store=store), web_config=cfg)
    return TestClient(app)


def test_the_401_challenge_carries_them_too() -> None:
    """Outermost layer, so an unauthenticated response is covered.

    A challenge page is exactly what an attacker frames — if the headers
    only rode on handler responses, the interesting case would be bare.
    """
    resp = _client().get("/", follow_redirects=False)
    assert resp.status_code == 401
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["content-security-policy"] == "frame-ancestors 'none'"


def test_an_authenticated_page_carries_them() -> None:
    resp = _client().get("/", follow_redirects=False, headers=_basic("reto", "pw"))
    assert resp.status_code in (200, 302, 303, 307)
    for header, value in (
        ("x-frame-options", "DENY"),
        ("content-security-policy", "frame-ancestors 'none'"),
        ("x-content-type-options", "nosniff"),
        ("referrer-policy", "no-referrer"),
    ):
        assert resp.headers[header] == value
    assert "max-age=" in resp.headers["strict-transport-security"]


def test_they_are_present_with_auth_off() -> None:
    """Auth-off is when the app is *most* exposed, not least."""
    resp = _client(auth=False).get("/", follow_redirects=False)
    assert resp.headers["x-frame-options"] == "DENY"


def test_the_csp_does_not_restrict_scripts_or_styles() -> None:
    """Deliberately frame-ancestors only.

    The templates carry inline scripts and styles, so a broad
    ``default-src``/``script-src`` policy would silently break pages. If
    someone widens this, it needs the templates fixed first — this test
    is the tripwire, not a statement that a narrow CSP is ideal.
    """
    csp = _client().get("/", follow_redirects=False).headers["content-security-policy"]
    assert csp == "frame-ancestors 'none'"
    assert "script-src" not in csp
    assert "default-src" not in csp

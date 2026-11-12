"""RFC 9116 security.txt served unauthenticated by the MCP ASGI app."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from precis import security_txt, server


def _client() -> TestClient:
    app = Starlette()
    server._install_token_auth(app, token="install")
    return TestClient(app)


def test_render_rolling_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_SECURITY_CONTACT", "mailto:sec@example.org")
    body = security_txt.render(datetime(2026, 11, 12, 15, 30, tzinfo=UTC))
    assert body == (
        "Contact: mailto:sec@example.org\n"
        "Expires: 2027-11-11T00:00:00Z\n"
        "Preferred-Languages: en\n"
    )


def test_render_default_contact_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_SECURITY_CONTACT", raising=False)
    assert "Contact: mailto:reto@retostamm.com\n" in security_txt.render()


def test_served_by_default_without_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_SECURITY_CONTACT", raising=False)
    resp = _client().get(security_txt.PATH)
    assert resp.status_code == 200
    assert "Contact: mailto:reto@retostamm.com" in resp.text


def test_other_paths_keep_bearer_gate() -> None:
    assert _client().get("/mcp").status_code == 401


def _without_canonical(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if not ln.startswith("Canonical:")]


def test_web_and_mcp_bodies_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both hosts serve the same Contact/Expires/Languages lines."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient as WebClient

    from precis_web.app import create_app
    from precis_web.config import WebConfig

    monkeypatch.setenv("PRECIS_SECURITY_CONTACT", "mailto:sec@example.org")
    web = WebClient(create_app(web_config=WebConfig.from_env()))
    web_text = web.get(security_txt.PATH).text
    mcp_text = _client().get(security_txt.PATH).text
    assert _without_canonical(web_text) == _without_canonical(mcp_text)

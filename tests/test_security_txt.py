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
        "Expires: 2027-05-11T00:00:00Z\n"
        "Preferred-Languages: en\n"
    )


def test_render_none_without_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_SECURITY_CONTACT", raising=False)
    assert security_txt.render() is None


def test_served_without_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_SECURITY_CONTACT", "mailto:sec@example.org")
    resp = _client().get(security_txt.PATH)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "Contact: mailto:sec@example.org" in resp.text


def test_unconfigured_keeps_bearer_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_SECURITY_CONTACT", raising=False)
    assert _client().get(security_txt.PATH).status_code == 401

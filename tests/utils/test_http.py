"""Tests for the shared outbound-HTTP seam (``precis.utils.http``)."""

from __future__ import annotations

import pytest

from precis.utils.http import (
    DEFAULT_USER_AGENT,
    HTTPX_EXTRA,
    http_client,
    require_httpx,
)

httpx = pytest.importorskip("httpx", reason="needs the [external] extra")


def test_http_client_sets_default_user_agent() -> None:
    with http_client(timeout=5.0) as client:
        assert client.headers["User-Agent"] == DEFAULT_USER_AGENT


def test_http_client_merges_extra_headers_over_default_ua() -> None:
    with http_client(timeout=5.0, headers={"Accept": "application/json"}) as client:
        assert client.headers["Accept"] == "application/json"
        assert client.headers["User-Agent"] == DEFAULT_USER_AGENT


def test_http_client_custom_user_agent_overrides_default() -> None:
    with http_client(timeout=5.0, user_agent="precis-mcp/1.0 (+contact)") as client:
        assert client.headers["User-Agent"] == "precis-mcp/1.0 (+contact)"


def test_http_client_header_user_agent_wins_over_kwarg() -> None:
    # Explicit header beats the user_agent kwarg (headers merged last).
    with http_client(
        timeout=5.0, headers={"User-Agent": "custom/9"}, user_agent="default/1"
    ) as client:
        assert client.headers["User-Agent"] == "custom/9"


def test_http_client_defaults_to_no_redirect_following() -> None:
    # Security default: the SSRF guard relies on manual redirect walking.
    with http_client(timeout=5.0) as client:
        assert client.follow_redirects is False


def test_http_client_can_opt_into_redirects() -> None:
    with http_client(timeout=5.0, follow_redirects=True) as client:
        assert client.follow_redirects is True


def test_require_httpx_returns_module() -> None:
    assert require_httpx() is httpx


def test_httpx_extra_is_external() -> None:
    assert HTTPX_EXTRA == "external"

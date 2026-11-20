"""gr633600: the precis brand icon.

One packaged mark (:mod:`precis.icons` over ``precis.data.icons``)
declared on ``serverInfo.icons``/``websiteUrl`` and served
unauthenticated on every public host, so connector pickers and
browsers stop falling back to the infrastructure host's icon
(Tailscale). The web host half lives in
``tests/precis_web/test_icons.py``.
"""

from __future__ import annotations

import asyncio
import base64
from typing import cast

from precis import icons
from precis.store import Store


def test_svg_is_the_packaged_mark() -> None:
    body = icons.svg_bytes()
    assert body.lstrip().startswith(b"<svg")
    assert b"#34d399" in body  # the emerald wordmark colour


def test_ico_is_a_real_icon_pack() -> None:
    body = icons.ico_bytes()
    assert body[:4] == b"\x00\x00\x01\x00"  # ICO magic


def test_for_path_maps_the_two_public_paths() -> None:
    assert icons.for_path(icons.SVG_PATH) == (icons.svg_bytes(), icons.SVG_MEDIA_TYPE)
    assert icons.for_path(icons.ICO_PATH) == (icons.ico_bytes(), icons.ICO_MEDIA_TYPE)
    assert icons.for_path("/favicon.png") is None
    assert icons.for_path("/mcp") is None


def test_public_paths_cover_both_fetches() -> None:
    assert {"/favicon.ico", "/favicon.svg"} == icons.PUBLIC_PATHS


def test_data_uri_roundtrips_the_svg() -> None:
    prefix, _, payload = icons.data_uri().partition(",")
    assert prefix == f"data:{icons.SVG_MEDIA_TYPE};base64"
    assert base64.b64decode(payload) == icons.svg_bytes()


def test_https_url_joins_origin_and_path() -> None:
    assert icons.https_url("https://mcp.example") == "https://mcp.example/favicon.svg"
    assert icons.https_url("https://mcp.example/") == "https://mcp.example/favicon.svg"


def test_mcp_icons_without_origin_is_inline_only() -> None:
    entries = icons.mcp_icons()
    assert len(entries) == 1
    assert entries[0].src.startswith("data:image/svg+xml")
    assert entries[0].mimeType == icons.SVG_MEDIA_TYPE


def test_mcp_icons_with_origin_prefers_fetchable_url() -> None:
    entries = icons.mcp_icons("https://mcp.example")
    assert [e.src for e in entries] == [
        "https://mcp.example/favicon.svg",
        icons.data_uri(),
    ]


# ── serverInfo wiring ────────────────────────────────────────────────


def test_fastmcp_declares_icons_and_website() -> None:
    from precis import server

    assert server.mcp.icons is server._MCP_ICONS
    assert server.mcp.icons == icons.mcp_icons()
    assert server.mcp.website_url == icons.WEBSITE_URL


def test_token_gate_serves_icons_unauthenticated() -> None:
    """The connector's icon fetch happens before any credential exists —
    both paths answer on the bearer-gated app like security.txt does."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from precis import server

    async def _ok(request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/probe", _ok)])
    server._install_token_auth(app, token="s3cr3t")
    client = TestClient(app)
    resp = client.get("/favicon.ico")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(icons.ICO_MEDIA_TYPE)
    assert resp.content == icons.ico_bytes()
    resp = client.get("/favicon.svg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(icons.SVG_MEDIA_TYPE)
    assert resp.content == icons.svg_bytes()
    # The gate itself is unchanged: content paths still want the bearer.
    assert client.get("/probe").status_code == 401


def test_oauth_client_record_declares_the_mark() -> None:
    """``logo_uri`` on the operator-owned client metadata (gr633600):
    connector UIs that read it get the issuer-hosted precis icon."""
    from precis.oauth.config import OAuthClient, OAuthConfig
    from precis.oauth.provider import OAuthProvider

    config = OAuthConfig(
        issuer="https://auth.example",
        resource="https://mcp.example/mcp",
        clients={"app": OAuthClient("app", "App", ("https://cb.example/x",))},
    )
    provider = OAuthProvider(cast(Store, None), config)
    info = asyncio.run(provider.get_client("app"))
    assert info is not None
    assert str(info.logo_uri) == "https://auth.example/favicon.svg"

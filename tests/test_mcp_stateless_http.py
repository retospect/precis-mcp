"""``PRECIS_MCP_STATELESS_HTTP`` — the opt-in per-request transport (gr660588).

The SDK's ``stateless`` mode mints a fresh session object per request and
keeps no session map at all, so a restart costs only in-flight calls and
there is no stale-session 404/400 to recover from. What makes it viable
is the durable state this gripe added: ``read_receipts`` rows key on the
authenticated owner (the bearer the token gate stamps), not the
transport session, and the tool ledger groups by owner+hour. These tests
pin the flag's reach (settings → session manager → recover-middleware
skip) and that per-request authentication still holds with no
``_session_owners`` map underneath.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from precis import principal, server, slice_tokens
from precis.store import ChunkInsert, Store
from precis.tools import core
from precis.utils import handle_registry


def test_stateless_setting_reaches_the_session_manager() -> None:
    """``stateless_http=True`` on construction lands on the SDK's session
    manager via ``streamable_http_app()`` — the same conduit the serve
    path's ``mcp.settings.stateless_http = True`` writes through."""
    mcp = FastMCP("stateless-flag-test", stateless_http=True)
    mcp.streamable_http_app()
    assert mcp.session_manager.stateless is True


def test_stateless_flag_skips_recover_and_sets_sdk_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serve-time wiring: the env flag flips ``stateless_http`` on and
    drops ``_install_session_recover`` — a missing session id is the
    mode's shape, not a failure — while the failure logger and the
    bearer gate stay installed."""
    from starlette.applications import Starlette

    calls: list[str] = []
    monkeypatch.setenv(server._STATELESS_HTTP_ENV, "1")
    monkeypatch.setattr(server.mcp.settings, "stateless_http", False)
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: Starlette(routes=[]))
    monkeypatch.setattr(
        server,
        "_install_session_recover",
        lambda app, *, mcp_path: calls.append("recover"),
    )
    monkeypatch.setattr(
        server,
        "_install_session_log",
        lambda app, *, mcp_path: calls.append("log"),
    )
    monkeypatch.setattr(
        server, "_install_token_auth", lambda app, **kw: calls.append("auth")
    )
    monkeypatch.setattr("anyio.run", lambda *a, **k: None)
    monkeypatch.setattr(
        "precis.oauth.config.OAuthConfig.from_env", staticmethod(lambda: None)
    )

    server._run_network_transport(
        transport="streamable-http", host="127.0.0.1", port=0, token="t"
    )

    assert server.mcp.settings.stateless_http is True
    assert calls == ["log", "auth"]


def test_stateful_default_keeps_recover_and_sdk_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag unset: streamable-http stays stateful — recover middleware
    installed, ``stateless_http`` untouched."""
    from starlette.applications import Starlette

    calls: list[str] = []
    monkeypatch.delenv(server._STATELESS_HTTP_ENV, raising=False)
    monkeypatch.setattr(server.mcp.settings, "stateless_http", False)
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: Starlette(routes=[]))
    monkeypatch.setattr(
        server,
        "_install_session_recover",
        lambda app, *, mcp_path: calls.append("recover"),
    )
    monkeypatch.setattr(
        server,
        "_install_session_log",
        lambda app, *, mcp_path: calls.append("log"),
    )
    monkeypatch.setattr(
        server, "_install_token_auth", lambda app, **kw: calls.append("auth")
    )
    monkeypatch.setattr("anyio.run", lambda *a, **k: None)
    monkeypatch.setattr(
        "precis.oauth.config.OAuthConfig.from_env", staticmethod(lambda: None)
    )

    server._run_network_transport(
        transport="streamable-http", host="127.0.0.1", port=0, token="t"
    )

    assert server.mcp.settings.stateless_http is False
    assert calls == ["recover", "log", "auth"]


def test_stateless_flag_does_not_apply_to_sse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag names streamable-http only — SSE keeps its own app and
    never flips the SDK mode."""
    from starlette.applications import Starlette

    monkeypatch.setenv(server._STATELESS_HTTP_ENV, "1")
    monkeypatch.setattr(server.mcp.settings, "stateless_http", False)
    monkeypatch.setattr(server.mcp, "sse_app", lambda: Starlette(routes=[]))
    monkeypatch.setattr(
        server,
        "_install_session_log",
        lambda app, *, mcp_path: None,
    )
    monkeypatch.setattr(server, "_install_token_auth", lambda app, **kw: None)
    monkeypatch.setattr("anyio.run", lambda *a, **k: None)
    monkeypatch.setattr(
        "precis.oauth.config.OAuthConfig.from_env", staticmethod(lambda: None)
    )

    server._run_network_transport(transport="sse", host="127.0.0.1", port=0, token="t")

    assert server.mcp.settings.stateless_http is False


def _headers(raw: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {raw}",
        "Accept": "application/json, text/event-stream",
    }


async def _call(
    client: httpx.AsyncClient, raw: str, call_id: int, verb: str, arguments: dict
) -> httpx.Response:
    """One standalone stateless request — no ``initialize``, no
    ``Mcp-Session-Id``: the SDK answers each request on a fresh session."""
    return await client.post(
        "/mcp",
        headers=_headers(raw),
        json={
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": verb, "arguments": arguments},
        },
    )


def test_stateless_requests_authenticate_and_share_owner_receipts(
    runtime_with_store: Any,
    store: Store,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr660588 slice 4: with the SDK's ``_session_owners`` map bypassed,
    per-request bearer authentication is the only identity check — an
    unknown token 401s, one grant's read licenses its own ``$ref`` on a
    later standalone request (the durable receipt, keyed ``slice:td42``),
    and a different grant earns nothing."""
    monkeypatch.setenv(server._STATELESS_HTTP_ENV, "1")
    principal._mode_last_good = None  # observe default; no cross-test leak
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)

    from mcp.server.transport_security import TransportSecuritySettings

    mcp = FastMCP(
        "stateless-e2e",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    monkeypatch.setattr(server, "mcp", mcp)
    monkeypatch.setattr(server, "_mcp_profile", lambda: "typed")
    server._register_tools_from_registry()
    app = mcp.streamable_http_app()
    server._install_token_auth(
        app, token="install-secret", store=store, slice_dir=tmp_path
    )

    ref = store.insert_ref(kind="paper", slug="stateless-paper", title="S")
    block = store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text="t")])[0]
    handle = handle_registry.format_handle("paper", block.id, chunk=True)
    td42 = slice_tokens.mint(tmp_path, "td42")
    td43 = slice_tokens.mint(tmp_path, "td43")

    async def exercise() -> None:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost",
            ) as client,
        ):
            bad = await _call(client, "not-a-token", 1, "get", {"kind": "paper"})
            assert bad.status_code == 401

            read = await _call(client, td42, 2, "get", {"kind": "paper", "id": handle})
            assert read.status_code == 200, read.text

            put_args = {"kind": "memory", "text": {"$ref": handle}}
            own = await _call(client, td42, 3, "put", put_args)
            own_result = own.json()["result"]
            assert not own_result.get("isError"), own_result
            assert "dereferenced text" in own_result["content"][0]["text"]

            other = await _call(client, td43, 4, "put", put_args)
            other_result = other.json()["result"]
            assert other_result.get("isError")
            assert "[error:Denied]" in other_result["content"][0]["text"]

    asyncio.run(exercise())

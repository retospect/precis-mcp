"""gr632443: ``serverInfo.instructions`` is once-per-connection, not per-call.

The SDK's ``ServerSession._received_request`` answers every
``initialize`` on a live session with the full instructions block —
including a mid-session re-initialize on the same connection, which the
client then renders as ~250 tokens of banner attached to the next
ordinary tool result. ``precis.server`` strips the block from the
session's ``_init_options`` after the first successful response, so a
re-initialize replies ``instructions=None`` while a fresh connection —
a new ``InitializationOptions`` per ``Server.run`` — still gets it once.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import httpx
import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_client_server_memory_streams

from precis import server
from precis.users import WebUser

_BANNER = "precis: verbs … Kinds unavailable: patent (prohibited)."
_VOICE = "Talking by voice? get(kind='skill', id='precis-voice-chat-help')."


@asynccontextmanager
async def _connect(mcp: FastMCP) -> AsyncIterator[ClientSession]:
    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: mcp._mcp_server.run(
                    server_read,
                    server_write,
                    mcp._mcp_server.create_initialization_options(),
                )
            )
            try:
                async with ClientSession(
                    read_stream=client_read, write_stream=client_write
                ) as session:
                    yield session
            finally:
                tg.cancel_scope.cancel()


async def _init_results(mcp: FastMCP, inits: int) -> list[types.InitializeResult]:
    async with _connect(mcp) as session:
        return [await session.initialize() for _ in range(inits)]


def test_serverinfo_carries_title() -> None:
    first, second = anyio.run(_init_results, server.mcp, 2)

    for result in (first, second):
        assert result.serverInfo.name == "precis-mcp"
        assert result.serverInfo.title == "Precis"


def test_reinitialize_on_one_connection_drops_the_banner() -> None:
    """The mid-session sighting: the second ``initialize`` on a live
    connection must not re-send the block — that is what rode along on
    the ordinary tool result."""
    mcp = FastMCP("precis-test-once", instructions=_BANNER)

    first, second = anyio.run(_init_results, mcp, 2)

    assert first.instructions == _BANNER
    assert second.instructions is None
    assert first.serverInfo.title is None


def test_a_fresh_connection_still_gets_the_banner() -> None:
    """Once per connection, not once per process: each new connection's
    first ``initialize`` carries the full block."""
    mcp = FastMCP("precis-test-once", instructions=_BANNER)

    first_conn = anyio.run(_init_results, mcp, 1)[0]
    second_conn = anyio.run(_init_results, mcp, 1)[0]

    assert first_conn.instructions == _BANNER
    assert second_conn.instructions == _BANNER


@pytest.mark.parametrize("is_developer", [True, False])
def test_authenticated_banner_includes_one_live_voice_pointer(
    is_developer: bool,
) -> None:
    user = WebUser(
        1,
        "alice",
        "al",
        None,
        None,
        None,
        None,
        None,
        None,
        key_memory_id=648264,
        is_developer=is_developer,
    )
    assert server.mcp.instructions is not None
    assert server.mcp.instructions.count(_VOICE) == 1
    instructions = server._authenticated_instructions(user, server.mcp.instructions)
    assert instructions.count(_VOICE) == 1
    assert instructions.count("get(kind='skill', id='precis-key-memory-help')") == 1
    assert "precis-word-handles-help" not in instructions


def _voice_probe(*, stateless: bool = False) -> FastMCP:
    mcp = FastMCP("voice-probe", stateless_http=stateless, json_response=True)

    @mcp.tool(structured_output=False)
    def probe() -> str:
        return "original result"

    server._install_personal_scope_gate(mcp)
    return mcp


def test_voice_fallback_is_once_per_live_connection() -> None:
    mcp = _voice_probe()

    async def exercise() -> None:
        for _ in range(2):
            async with _connect(mcp) as session:
                await session.initialize()
                first = await session.call_tool("probe")
                second = await session.call_tool("probe")
                original = types.TextContent(type="text", text="original result")
                assert first.content == [
                    original,
                    types.TextContent(type="text", text=_VOICE),
                ]
                assert second.content == [original]

    anyio.run(exercise)


def test_stateless_connector_gets_voice_pointer_without_initialize() -> None:
    mcp = _voice_probe(stateless=True)
    app = mcp.streamable_http_app()

    async def exercise() -> None:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
            ) as client,
        ):
            for request_id in (1, 2):
                reply = await client.post(
                    "/mcp",
                    headers={"Accept": "application/json, text/event-stream"},
                    json={
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": "probe", "arguments": {}},
                    },
                )
                assert reply.status_code == 200, reply.text
                assert [
                    block["text"] for block in reply.json()["result"]["content"]
                ] == [
                    "original result",
                    _VOICE,
                ]

    anyio.run(exercise)

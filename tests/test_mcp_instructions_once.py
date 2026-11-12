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

import anyio
from mcp import types
from mcp.client.session import ClientSession
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_client_server_memory_streams

from precis import server  # noqa: F401 — importing installs the shim under test

_BANNER = "precis: verbs … Kinds unavailable: patent (prohibited)."


async def _init_results(mcp: FastMCP, inits: int) -> list[types.InitializeResult]:
    """Open one in-memory connection to ``mcp`` and run ``inits`` initializes."""
    results: list[types.InitializeResult] = []
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
                    for _ in range(inits):
                        results.append(await session.initialize())
            finally:
                tg.cancel_scope.cancel()
    return results


def test_reinitialize_on_one_connection_drops_the_banner() -> None:
    """The mid-session sighting: the second ``initialize`` on a live
    connection must not re-send the block — that is what rode along on
    the ordinary tool result."""
    mcp = FastMCP("precis-test-once", instructions=_BANNER)

    first, second = anyio.run(_init_results, mcp, 2)

    assert first.instructions == _BANNER
    assert second.instructions is None


def test_a_fresh_connection_still_gets_the_banner() -> None:
    """Once per connection, not once per process: each new connection's
    first ``initialize`` carries the full block."""
    mcp = FastMCP("precis-test-once", instructions=_BANNER)

    first_conn = anyio.run(_init_results, mcp, 1)[0]
    second_conn = anyio.run(_init_results, mcp, 1)[0]

    assert first_conn.instructions == _BANNER
    assert second_conn.instructions == _BANNER

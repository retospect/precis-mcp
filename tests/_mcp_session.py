"""In-memory MCP client/server sessions, for tests about *session* behaviour.

Every other test in this tree deliberately bypasses the transport: they
build a :class:`~precis.runtime.PrecisRuntime` from a fixture and call
``.dispatch(verb, args)`` directly, exactly as ``precis.server``'s module
docstring instructs ("Tests should not import this module"). That is the
right default — it keeps the suite fast and DB-shaped rather than
protocol-shaped.

The consequence is that **no test has ever created an MCP session**, so
no test can observe a property that only exists *between* sessions. That
matters now for one specific question: ``precis serve`` runs one process
per agent over stdio today, but the network transport
(``sse``/``streamable-http``, see ``server._NETWORK_TRANSPORTS``) serves
many sessions from one process — and several invariants that are free
under process-per-agent have to be re-proven under a shared process.

This module is the missing primitive. It stands up N real
:class:`~mcp.client.session.ClientSession` objects against **one**
``FastMCP`` instance over the SDK's in-memory streams — no sockets, no
subprocesses, no HTTP. Each session gets its own ``ServerSession`` (the
SDK's ``Server.run`` builds one per connection), so the shape is exactly
"one process, many agents", which is what the shared-transport question
turns on.

Tools are registered through the **real** production seam
(:func:`precis.server._offload_sync` plus ``server._TOOL_KW``), not a
hand-rolled stand-in, so anything proven here is a property of the
shipping wrapper rather than of the harness.

Deliberately DB-free: callers pass plain sync callables. A test that
needs a store should build a runtime the ordinary way; this harness is
for the protocol layer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import anyio
from mcp.client.session import ClientSession
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from precis import server


def build_test_mcp(
    tools: Sequence[Callable[..., Any]],
    *,
    semaphore: anyio.Semaphore | None = None,
    name: str = "precis-test-session",
) -> FastMCP:
    """A ``FastMCP`` exposing ``tools``, wrapped exactly as production does.

    ``semaphore=None`` (the default) leaves each wrapper resolving
    :func:`precis.server._get_tool_semaphore` at call time — i.e. the
    module-level singleton the real server uses. Pass an explicit
    semaphore only when a test needs a bound isolated from that global.
    """
    mcp = FastMCP(name)
    for fn in tools:
        mcp.tool(**server._TOOL_KW)(server._offload_sync(fn, semaphore=semaphore))
    return mcp


@asynccontextmanager
async def connected_sessions(
    mcp: FastMCP, count: int
) -> AsyncIterator[list[ClientSession]]:
    """Yield ``count`` initialized client sessions against one ``mcp``.

    Models a single shared server process with ``count`` concurrent
    agents attached. Sessions are torn down in reverse order on exit.
    """
    async with AsyncExitStack() as stack:
        sessions = [
            await stack.enter_async_context(
                create_connected_server_and_client_session(mcp)
            )
            for _ in range(count)
        ]
        yield sessions


def text_of(result: Any) -> str:
    """The concatenated text payload of a ``CallToolResult``."""
    return "".join(
        block.text for block in result.content if getattr(block, "type", None) == "text"
    )

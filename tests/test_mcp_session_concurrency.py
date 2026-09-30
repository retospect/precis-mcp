"""Properties that only exist *between* concurrent MCP sessions.

``tests/test_mcp_tool_offload.py`` pins gr330541's fix at the wrapper and
``Tool.run`` level — one tool, one caller, no session. These tests go one
layer out, through ``tests/_mcp_session.py``'s in-memory harness, and ask
what happens when **two agents share one server process**.

That IS today's deployment for the session MCP: it is one long-lived
``precis serve --transport streamable-http`` container that every Claude
Code session's ``precis`` tools share (see ``precis.server``'s module
docstring for the two-deployment split). Spawned callers — agent
containers, the sandbox sidecar, ``asa_bot`` — remain stdio and keep a
process per caller, so each bound below is a per-caller budget there and
a shared one on the session server. The difference is invisible until
something creates two sessions against one server; these tests create
them and hold the difference still.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator

import pytest
from mcp.server.fastmcp import Context

from precis import serve_ledger, server
from tests._mcp_session import build_test_mcp, connected_sessions, text_of


@pytest.fixture
def _reset_tool_semaphore() -> Iterator[None]:
    """Restore ``server._tool_semaphore`` so the module global stays clean.

    It is built lazily on first use, so a test that sets
    ``PRECIS_MCP_TOOL_CONCURRENCY`` must also clear the cached instance —
    both before (to pick the new limit up) and after (so an unrelated
    later test doesn't inherit this test's bound).
    """
    server._tool_semaphore = None
    try:
        yield
    finally:
        server._tool_semaphore = None


# ── cross-session head-of-line blocking ──────────────────────────────


def test_slow_call_in_one_session_does_not_block_another_session() -> None:
    """gr330541's regression, in its cross-agent form.

    The in-session version is already pinned
    (``test_offload_sync_prevents_head_of_line_blocking``). This is the
    version that decides whether a shared process is viable at all: if
    agent A's multi-minute ``perplexity-research`` stalled agent B's
    microsecond skill read, one shared server would be strictly worse
    than process-per-agent no matter how the pools were sized.
    """
    order: list[str] = []
    lock = threading.Lock()

    def slow() -> str:
        """Hold a worker thread for a beat."""
        time.sleep(0.3)
        with lock:
            order.append("slow")
        return "slow-done"

    def fast() -> str:
        """Return immediately."""
        with lock:
            order.append("fast")
        return "fast-done"

    mcp = build_test_mcp([slow, fast])

    async def _run() -> tuple[str, str]:
        async with connected_sessions(mcp, 2) as (session_a, session_b):
            a, b = await asyncio.gather(
                session_a.call_tool("slow", {}),
                session_b.call_tool("fast", {}),
            )
            return text_of(a), text_of(b)

    a_text, b_text = asyncio.run(_run())
    assert a_text == "slow-done"
    assert b_text == "fast-done"
    # ``fast`` recorded itself while ``slow``'s sleep was still running:
    # the two sessions progressed independently.
    assert order == ["fast", "slow"], order


# ── what the tool-concurrency cap is actually scoped to ──────────────


def test_tool_concurrency_cap_is_process_wide_not_per_session(
    monkeypatch: pytest.MonkeyPatch, _reset_tool_semaphore: None
) -> None:
    """``PRECIS_MCP_TOOL_CONCURRENCY`` bounds the PROCESS, not the session.

    ``server._tool_semaphore`` is a module-level singleton
    (``server.py``'s ``_get_tool_semaphore``), so every session attached
    to one server draws from one budget. Two sessions issuing two
    concurrent calls each — four in flight against a cap of two — must
    still peak at two.

    This is the load-bearing asymmetry between the two deployments:

    * **stdio** one process per spawned caller, so the default of 4
      (``server._DEFAULT_TOOL_CONCURRENCY``) is 4 *per caller* and the
      aggregate ceiling is N×4.
    * **the shared session server** the same number would be 4 for
      every attached session combined, which is why that deployment
      raises ``PRECIS_MCP_TOOL_CONCURRENCY`` in step with the pool
      instead of running the default.

    Sizing is therefore necessary but not sufficient: the singleton is
    first-come-first-served, so one session bursting N calls can hold
    every permit while another session's cheap read queues behind them.
    Nothing here asserts a fairness property — there isn't one to
    assert yet. This test pins the scope so that change is deliberate.
    """
    monkeypatch.setenv(server._TOOL_CONCURRENCY_ENV, "2")

    lock = threading.Lock()
    current = 0
    peak = 0

    def tracked() -> str:
        """Occupy a permit long enough for every contender to try for one."""
        nonlocal current, peak
        with lock:
            current += 1
            peak = max(peak, current)
        time.sleep(0.3)
        with lock:
            current -= 1
        return "ok"

    # semaphore=None → each wrapper resolves the module singleton at call
    # time, which is the production path.
    mcp = build_test_mcp([tracked], semaphore=None)

    async def _run() -> None:
        async with connected_sessions(mcp, 2) as (session_a, session_b):
            await asyncio.gather(
                session_a.call_tool("tracked", {}),
                session_a.call_tool("tracked", {}),
                session_b.call_tool("tracked", {}),
                session_b.call_tool("tracked", {}),
            )

    asyncio.run(_run())
    assert peak == 2, peak


# ── per-session state really is per-session ──────────────────────────


def test_serve_ledger_state_does_not_leak_between_sessions() -> None:
    """The serve ledger's central claim, proven against two real sessions.

    :mod:`precis.serve_ledger` keys its state on the FastMCP session
    object via a ``WeakKeyDictionary`` precisely so that "two concurrent
    agent sessions must never see each other's ledger, and the HTTP
    transport serves many sessions from one process" (that module's
    docstring). Until this harness existed there was no way to hand it
    two sessions, so the claim rested on reading the code.

    A leak here would not corrupt data — the ledger only ever decides
    whether to re-send an unchanged skill body — but it would silently
    starve one agent of a skill another agent had already read, which is
    close to undiagnosable from the affected agent's side.
    """

    def serve(slug: str, ctx: Context) -> str:
        """Record ``slug`` as served in the calling session's ledger."""
        with serve_ledger.session_scope(ctx.session):
            serve_ledger.record(slug, "sha-" + slug)
        return "recorded"

    def seen(slug: str, ctx: Context) -> str:
        """Report whether ``slug`` was already served in THIS session."""
        with serve_ledger.session_scope(ctx.session):
            return "yes" if serve_ledger.was_served(slug) else "no"

    mcp = build_test_mcp([serve, seen])

    async def _run() -> tuple[str, str]:
        async with connected_sessions(mcp, 2) as (session_a, session_b):
            await session_a.call_tool("serve", {"slug": "precis-overview"})
            a = await session_a.call_tool("seen", {"slug": "precis-overview"})
            b = await session_b.call_tool("seen", {"slug": "precis-overview"})
            return text_of(a), text_of(b)

    a_text, b_text = asyncio.run(_run())
    assert a_text == "yes", "the recording session must see its own entry"
    assert b_text == "no", "a sibling session must NOT inherit it"

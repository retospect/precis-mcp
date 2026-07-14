"""PrecisClient.ping must detect a *degraded* precis, not just a dead one.

Regression for the 2026-07-14 incident: melchior rebooted, asa's long-lived
``precis serve`` subprocess was spawned while the prod DB was unreachable, and
it came up **storeless** — it answered ``tools/list`` cleanly but reported
``unknown kind`` for memory/conv/gripe. asa's preamble filled with those
errors, the agent concluded precis was a file-only sandbox and could not file
gripes, and the liveness-only health check never restarted it (and in fact was
never even started). ping() now also probes a store-backed kind.
"""

from __future__ import annotations

import asyncio

from asa_bot.config import PrecisConfig
from asa_bot.precis_client import PrecisClient


def _client_with_fake_request(handler) -> PrecisClient:
    client = PrecisClient(PrecisConfig())

    async def _fake_request(method, params):
        return handler(method, params)

    # Bypass the real subprocess + MCP handshake entirely.
    client.request = _fake_request  # type: ignore[assignment]
    return client


def _tool_text(blob: str):
    return {"content": [{"type": "text", "text": blob}]}


def test_healthy_precis_pings_true():
    def handler(method, params):
        if method == "tools/list":
            return {"tools": [{"name": "get"}]}
        return _tool_text("# 3 gripe entries tagged ['STATUS:open']\n...")

    client = _client_with_fake_request(handler)
    assert asyncio.run(client.ping()) is True


def test_storeless_precis_pings_false():
    # The degraded-server signature: DB kind reported as unknown.
    def handler(method, params):
        if method == "tools/list":
            return {"tools": [{"name": "get"}]}
        return _tool_text(
            "[error:NotFound] unknown kind: gripe\n  options: calc, provenance"
        )

    client = _client_with_fake_request(handler)
    assert asyncio.run(client.ping()) is False


def test_no_search_kinds_pings_false():
    def handler(method, params):
        if method == "tools/list":
            return {"tools": [{"name": "get"}]}
        return _tool_text(
            "no kinds in this build support verb='search'; "
            "the most likely cause is a missing env var"
        )

    client = _client_with_fake_request(handler)
    assert asyncio.run(client.ping()) is False


def test_dead_precis_pings_false():
    def handler(method, params):
        raise ConnectionError("precis MCP closed stdout")

    client = _client_with_fake_request(handler)
    assert asyncio.run(client.ping()) is False

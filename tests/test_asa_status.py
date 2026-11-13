"""``/precis status`` and the ``isError`` contract it runs on (gr663484).

The command used to issue ``search(kind=K, page_size=1, q='*')`` — the
match-all fold turns that into a queryless call every kind refuses with
``[error:BadInput]``, and ``call_tool`` returned the refusal text (or
``""`` when the error envelope carried no content), so every kind
rendered "(empty)". Two fixes:

* ``cmd_status`` asks ``search(view='count')`` for the ref total and the
  bare ``get(kind=K)`` listing for the newest row — valid calls that
  work uniformly across paper/patent/memory/pres/conv.
* ``call_tool`` raises :class:`PrecisToolError` when the wire result
  carries ``isError``, so a refusal surfaces as an error line instead
  of a text blob a renderer can't tell from hits.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

# asa_bot.slash imports discord.py (the `[asa]` extra). Skip cleanly where
# it isn't installed — mirrors test_asa_slash.py.
pytest.importorskip("discord")

from asa_bot.precis_client import PrecisClient, PrecisToolError
from asa_bot.slash import Runtime, SlashContext, cmd_status


class _FakeSendTarget:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    async def send(self, content: str = "", *, file: Any = None) -> None:
        self._sink.append(content)


class _FakePrecis:
    """``PrecisClient`` stand-in returning canned blobs per (verb, kind)."""

    def __init__(
        self,
        blobs: dict[tuple[str, str], str],
        *,
        fail: dict[tuple[str, str], Exception] | None = None,
    ) -> None:
        self._blobs = blobs
        self._fail = fail or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, arguments))
        key = (name, str(arguments.get("kind")))
        if key in self._fail:
            raise self._fail[key]
        return self._blobs.get(key, "")


def _make_ctx(precis: Any, sink: list[str]) -> SlashContext:
    return SlashContext(
        message=object(),
        positional=[],
        kwargs={},
        precis=precis,
        config=object(),  # type: ignore[arg-type]
        runtime=Runtime(),
        soul="",
        tool_hints="",
        ctx_from_message=lambda m: m,
        reply_target=lambda m: _FakeSendTarget(sink),
    )


def test_cmd_status_renders_count_and_latest_row() -> None:
    blobs = {
        ("search", "memory"): "# 3 memory entries — live refs",
        ("get", "memory"): (
            "# recent memory (3)\n\n"
            "{id\tpreview\ttags}\n"
            "82\tMem 2\tSPACE:research\n"
            "81\tMem 1\tSPACE:research\n"
            "77\tMem 0\tSPACE:research\n"
            "Next:\n{...}"
        ),
    }
    precis = _FakePrecis(blobs)
    sink: list[str] = []
    asyncio.run(cmd_status(_make_ctx(precis, sink)))

    assert sink, "no reply sent"
    memory_line = next(line for line in sink[0].splitlines() if "`memory`" in line)
    assert "3 memory entries" in memory_line
    assert "82 — Mem 2" in memory_line
    assert "(empty)" not in sink[0]
    # The old q='*' call shape is gone; the count view + bare get are the asks.
    assert ("search", {"kind": "memory", "view": "count"}) in precis.calls
    assert ("get", {"kind": "memory"}) in precis.calls
    assert all(
        not (name == "search" and args.get("q") == "*") for name, args in precis.calls
    )


def test_cmd_status_empty_kind_is_none_not_error() -> None:
    blobs = {
        ("search", "pres"): "# 0 pres entries — live refs",
        ("get", "pres"): "no presentations recorded yet",
    }
    sink: list[str] = []
    asyncio.run(cmd_status(_make_ctx(_FakePrecis(blobs), sink)))

    pres_line = next(line for line in sink[0].splitlines() if "`pres`" in line)
    assert "0 pres entries" in pres_line
    assert "(none)" in pres_line
    assert "(empty)" not in pres_line


def test_cmd_status_tool_error_named_not_empty() -> None:
    """A refused call surfaces as ``error — <the refusal>``, never "(empty)"."""
    fail: dict[tuple[str, str], Exception] = {
        ("search", "paper"): PrecisToolError(
            "[error:BadInput] search requires q= or tags="
        )
    }
    blobs = {("get", "paper"): "# 1 paper\n{handle}\npa1"}
    sink: list[str] = []
    asyncio.run(cmd_status(_make_ctx(_FakePrecis(blobs, fail=fail), sink)))

    paper_line = next(line for line in sink[0].splitlines() if "`paper`" in line)
    assert "error —" in paper_line
    assert "search requires q= or tags=" in paper_line
    assert "(empty)" not in paper_line


class _StubClient(PrecisClient):
    """Wire-shape stub — ``call_tool`` sees exactly what the serve sent."""

    _result: dict[str, Any]

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        return self._result


def _stub(result: dict[str, Any]) -> _StubClient:
    client = _StubClient.__new__(_StubClient)
    client._result = result
    return client


def test_call_tool_raises_on_iserror() -> None:
    client = _stub(
        {
            "content": [
                {"type": "text", "text": "[error:BadInput] search requires q="}
            ],
            "isError": True,
        }
    )
    with pytest.raises(PrecisToolError, match="search requires q="):
        asyncio.run(client.call_tool("search", {"kind": "paper", "q": "*"}))


def test_call_tool_iserror_without_content_still_raises() -> None:
    """An error envelope with no text content must not collapse to "" —
    the silent-empty path that rendered every kind "(empty)"."""
    client = _stub({"isError": True})
    with pytest.raises(PrecisToolError):
        asyncio.run(client.call_tool("search", {"kind": "paper"}))


def test_call_tool_success_returns_text() -> None:
    client = _stub(
        {
            "content": [
                {"type": "text", "text": "one"},
                {"type": "text", "text": "two"},
            ],
            "isError": False,
        }
    )
    assert (
        asyncio.run(client.call_tool("get", {"kind": "gripe", "id": [1]})) == "one\ntwo"
    )

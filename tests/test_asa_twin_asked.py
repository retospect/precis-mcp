"""asa's twin:asked capture (gr548277): a user's "what is X?" tags or mints
the term for the card forge — always after the reply, never in its way.

The precis MCP calls go to a recording fake; the SQL legs go through a
monkeypatched ``psycopg.AsyncConnection`` double, so the test pins the
control flow (match → tag+bump / mint → owner+count stamp), not the wire.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("discord")

from asa_bot import twin_asked


class _Cursor:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeConn:
    """One canned row per execute, in order; writes get recorded."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = list(rows)
        self.writes: list[tuple[str, Any]] = []

    async def execute(self, sql: str, params: Any = None) -> _Cursor:
        if "UPDATE" in sql or "INSERT" in sql:
            self.writes.append((sql, params))
        rows = self._rows.pop(0) if self._rows else []
        return _Cursor([rows] if rows else [])

    async def close(self) -> None:
        pass


class _FakePrecis:
    def __init__(self, put_ack: str = "created concept cn777.") -> None:
        self.put_ack = put_ack
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, arguments))
        return self.put_ack if name == "put" else "ok"


def _patch_db(monkeypatch: pytest.MonkeyPatch, conns: list[_FakeConn]) -> None:
    """Serve the canned conns in order as twin_asked opens connections."""
    queue = list(conns)

    class _AC:
        @staticmethod
        async def connect(dsn: str, autocommit: bool = False) -> _FakeConn:
            return queue.pop(0) if queue else _FakeConn([])

    monkeypatch.setattr("psycopg.AsyncConnection.connect", _AC.connect)


def test_existing_concept_is_tagged_and_bumped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login_conn = _FakeConn([("reto",)])
    find_conn = _FakeConn([("concept", 55)])
    bump_conn = _FakeConn([])
    _patch_db(monkeypatch, [login_conn, find_conn, bump_conn])
    precis = _FakePrecis()

    term = asyncio.run(
        twin_asked.capture(
            precis=precis,
            dsn="postgresql://x",
            message_text="what is entropy?",
            reply_text="Entropy measures disorder. More later.",
            conv_slug="discord/1/2/3",
            account_abbrev="rs",
        )
    )
    assert term == "entropy"
    assert precis.calls == [
        ("tag", {"kind": "concept", "id": 55, "add": ["twin:asked"]})
    ]
    assert bump_conn.writes and bump_conn.writes[0][1] == (55,)


def test_unknown_term_mints_via_put_and_stamps_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login_conn = _FakeConn([("reto",)])
    find_conn = _FakeConn([])  # no match
    stamp_conn = _FakeConn([])
    _patch_db(monkeypatch, [login_conn, find_conn, stamp_conn])
    precis = _FakePrecis(put_ack="created concept cn777.\n\nNext: ...")

    term = asyncio.run(
        twin_asked.capture(
            precis=precis,
            dsn="postgresql://x",
            message_text="remind me what a perovskite is",
            reply_text="A perovskite is a crystal structure. More detail.",
            conv_slug="discord/1/2/3",
            account_abbrev="rs",
        )
    )
    assert term == "perovskite"
    ((verb, args),) = precis.calls
    assert verb == "put"
    assert args["kind"] == "concept"
    assert args["text"] == "perovskite — A perovskite is a crystal structure."
    assert args["tags"] == ["twin:asked"]
    assert args["link"] == "conv:discord/1/2/3"
    sql, params = stamp_conn.writes[0]
    assert "owner_login" in sql and params[0] == "reto" and params[1] == 777


def test_non_ask_message_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_db(monkeypatch, [])
    precis = _FakePrecis()
    term = asyncio.run(
        twin_asked.capture(
            precis=precis,
            dsn="postgresql://x",
            message_text="tell me about entropy",
            reply_text="Entropy is…",
            conv_slug="discord/1/2/3",
            account_abbrev="rs",
        )
    )
    assert term is None and precis.calls == []


def test_status_questions_are_not_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_db(monkeypatch, [])
    precis = _FakePrecis()
    term = asyncio.run(
        twin_asked.capture(
            precis=precis,
            dsn="postgresql://x",
            message_text="what is the status of the deploy?",
            reply_text="Green.",
            conv_slug="discord/1/2/3",
            account_abbrev="rs",
        )
    )
    assert term is None and precis.calls == []

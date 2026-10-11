"""gr461387: guide-web's structural read-only DSN rewrite + role refusal."""

from __future__ import annotations

from typing import Any

import pytest
from psycopg.conninfo import conninfo_to_dict

from precis_web.readonly_dsn import (
    ReadOnlyRefused,
    main,
    rewrite_dsn,
    verify_read_only,
)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://agent_rw@host.docker.internal:6432/precis",
        "postgresql://agent_rw:" + "pw" + "@host.docker.internal:6432/precis",
        "postgresql://other_user@host.docker.internal:6432/precis?options=-c%20x%3Dy",
        "host=host.docker.internal port=6432 dbname=precis user=agent_rw",
        "host=host.docker.internal port=6432 dbname=precis user=agent_rw password=pw",
        "postgresql://host.docker.internal:6432/precis",
    ],
)
def test_rewrite_forces_ro_user_and_loopback(dsn: str) -> None:
    d = conninfo_to_dict(rewrite_dsn(dsn))
    assert d["user"] == "agent_ro"
    assert d["host"] == "127.0.0.1"
    assert d["port"] == "6432"
    assert d["dbname"] == "precis"
    assert "password" not in d
    assert "options" not in d


def test_rewrite_rejects_garbage_without_echoing_it() -> None:
    with pytest.raises(ReadOnlyRefused) as ei:
        rewrite_dsn("not a dsn ===secretvalue")
    assert "secretvalue" not in str(ei.value)


def test_rewrite_rejects_missing_host() -> None:
    with pytest.raises(ReadOnlyRefused):
        rewrite_dsn("dbname=precis user=agent_rw")


class _Conn:
    def __init__(self, row: tuple[Any, ...]) -> None:
        self.row = row

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, sql: str) -> _Conn:
        return self

    def fetchone(self) -> tuple[Any, ...]:
        return self.row


def _connect(row: tuple[Any, ...]) -> Any:
    return lambda dsn, **kw: _Conn(row)


def test_verify_accepts_ro_without_insert() -> None:
    verify_read_only("x", connect=_connect(("agent_ro", False)))


@pytest.mark.parametrize(
    "row", [("agent_rw", True), ("agent_rw", False), ("agent_ro", True)]
)
def test_verify_refuses_writer(row: tuple[Any, ...]) -> None:
    with pytest.raises(ReadOnlyRefused):
        verify_read_only("x", connect=_connect(row))


def test_verify_refuses_when_connection_fails() -> None:
    def boom(dsn: str, **kw: Any) -> Any:
        raise OSError("nope")

    with pytest.raises(ReadOnlyRefused):
        verify_read_only("x", connect=boom)


def test_main_refuses_and_prints_no_dsn(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    f = tmp_path / "dsn"
    f.write_text(
        "postgresql://agent_rw:" + "topsecret" + "@host.docker.internal:6432/p",
        encoding="utf-8",
    )

    def refuse(dsn: str, connect: Any = None) -> None:
        raise ReadOnlyRefused("effective role is 'agent_rw'")

    monkeypatch.setattr("precis_web.readonly_dsn.verify_read_only", refuse)
    assert main(["m", str(f)]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "REFUSING" in out.err
    assert "topsecret" not in out.err


def test_lifespan_skips_nas_attest_when_read_only_env() -> None:
    from pathlib import Path

    src = (Path(__file__).parent.parent / "src/precis_web/app.py").read_text(
        encoding="utf-8"
    )
    assert "not _read_only_env_override()" in src

"""CLI surface — argument parsing and exit codes."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from precis import cli


def test_no_args_exits(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["precis"])
    with pytest.raises(SystemExit):
        cli.main()


def test_migrate_not_implemented(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["precis", "migrate"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "not yet implemented" in err


def test_jobs_not_implemented(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["precis", "jobs", "reembed"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "reembed" in err


def test_serve_invokes_server_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """`precis serve` must dispatch to precis.server.main()."""
    called: dict[str, Any] = {"hit": False}

    def fake_main() -> None:
        called["hit"] = True

    import precis.server

    monkeypatch.setattr(precis.server, "main", fake_main)
    monkeypatch.setattr(sys, "argv", ["precis", "serve"])

    cli.main()
    assert called["hit"] is True

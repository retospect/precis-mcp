"""Unit tests for the bare pytest/pip/mypy guard hook (pure ``evaluate`` + exit code)."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "hooks"
    / "guard-bare-python-tools.py"
)
_spec = importlib.util.spec_from_file_location("guard_bare_python_tools", _HOOK)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
evaluate = _mod.evaluate
main = _mod.main


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest tests/x.py",
        "rtk pytest tests/x.py",
        "pip install x",
        "pip3 install x",
        "mypy src",
        "python -m pytest",
        "python3 -m pip install x",
        "FOO=1 pytest -q",
        "git status && pytest",
        "echo hi | pip list",
        "ls\nmypy src",
    ],
)
def test_refused(cmd: str) -> None:
    assert evaluate(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "uv run pytest tests/x.py",
        "uv run mypy src",
        "uvx pytest",
        "scripts/test tests/x.py",
        "scripts/test --typecheck",
        "rtk proxy ssh host 'pytest'",
        "ssh host pip install x",
        "git log | grep pytest",
        "grep -r mypy docs",
        "echo 'pytest is bad'",
        "uv add pytest-foo",
        "cat <<EOF\npytest\nEOF",
        "echo 'unterminated",
        "",
    ],
)
def test_allowed(cmd: str) -> None:
    assert evaluate(cmd) is None


def test_main_exit_codes(monkeypatch, capsys) -> None:
    def run(cmd: str) -> int:
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps({"tool_input": {"command": cmd}}))
        )
        return main()

    assert run("pytest") == 2
    assert "scripts/test" in capsys.readouterr().err
    assert run("uv run pytest") == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert main() == 0

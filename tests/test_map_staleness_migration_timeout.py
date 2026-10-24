"""Unit tests for the map-staleness-reminder hook (matcher: Write).

Loads the hyphenated hook script by path like
``tests/test_bash_reflex_nudge.py`` and drives ``_migration_collision``
against a stub ``scripts/migration-check`` under a fake
``CLAUDE_PROJECT_DIR``. Covers the gr514201 follow-up: the advisory
subprocess used to take ~90s on a large fleet while the hook allowed 15s
and swallowed the timeout — a silent drop of exactly the warning the hook
exists to print.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the stub scripts are POSIX sh",
)

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "hooks"
    / "map-staleness-reminder.py"
)

_MIGRATION_PATH = "/wt/src/precis/migrations/0242_new_thing.sql"


def _load():
    spec = importlib.util.spec_from_file_location("map_staleness_reminder", _HOOK)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_checker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, timeout: str
):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    stub = scripts_dir / "migration-check"
    stub.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("PRECIS_MIGRATION_CHECK_TIMEOUT", timeout)
    return _load()


def test_migration_collision_timeout_warns_instead_of_swallowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _stub_checker(tmp_path, monkeypatch, "sleep 30", "0.5")

    note = mod._migration_collision(_MIGRATION_PATH)

    assert "timed out" in note, note
    assert "scripts/migration-check" in note, note


def test_migration_collision_surfaces_checker_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _stub_checker(
        tmp_path, monkeypatch, "echo '0011 collision: NEW x main y'; exit 1", "15"
    )

    note = mod._migration_collision(_MIGRATION_PATH)

    assert "0011 collision" in note, note


def test_migration_collision_quiet_when_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _stub_checker(tmp_path, monkeypatch, "exit 0", "15")

    assert mod._migration_collision(_MIGRATION_PATH) == ""


def test_migration_collision_ignores_non_migration_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stub that would fail proves the path filter short-circuits first.
    mod = _stub_checker(tmp_path, monkeypatch, "exit 1; echo never", "15")

    assert mod._migration_collision("/wt/src/precis/handlers/foo.py") == ""

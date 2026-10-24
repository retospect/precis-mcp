"""Tests for ``scripts/hooks/map-staleness-reminder.py`` (gr514353).

The PostToolUse Write hook feeds one-line "keep the map true" reminders back
to the session for paths under its TRIGGERS fragments. The ``docs/backlog/``
reminder ("delete the item's file when the ship completes") applies to an
EDIT of an existing item — a freshly written, still git-untracked spec earns
no reminder. Loading the hyphenated-name hook by path mirrors
``test_bash_reflex_nudge.py``; ``main()`` is driven with fake hook-input JSON
like ``test_read_dedup.py``'s pure-function style (no subprocess — the hook's
own ``git ls-files`` runs for real inside a tmp git repo).
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "hooks"
    / "map-staleness-reminder.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("map_staleness", HOOK)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load()


def _run(monkeypatch, capsys, file_path: Path) -> dict | None:
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(file_path)}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert hook.main() == 0
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"] if out.strip() else None


def _git(repo: Path, *args: str) -> None:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    return tmp_path


def test_new_backlog_spec_is_silent(monkeypatch, capsys, repo: Path) -> None:
    """gr514353: a Write CREATING a docs/backlog item gets no reminder —
    nothing is being completed, so the delete-on-ship note is noise."""
    spec = repo / "docs" / "backlog" / "new-item.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# spec\n", encoding="utf-8")
    assert _run(monkeypatch, capsys, spec) is None


def test_edit_of_tracked_backlog_item_reminds(monkeypatch, capsys, repo: Path) -> None:
    item = repo / "docs" / "backlog" / "old-item.md"
    item.parent.mkdir(parents=True)
    item.write_text("# old\n", encoding="utf-8")
    _git(repo, "add", "docs/backlog/old-item.md")
    out = _run(monkeypatch, capsys, item)
    assert out is not None
    assert "DELETE" in out["additionalContext"]


def test_untracked_migration_still_reminds(monkeypatch, capsys, repo: Path) -> None:
    """Only the backlog trigger is create-silent — a NEW (untracked)
    migration must still get its reminder/collision check."""
    mig = repo / "src" / "precis" / "migrations" / "9999_x.sql"
    mig.parent.mkdir(parents=True)
    mig.write_text("select 1;\n", encoding="utf-8")
    out = _run(monkeypatch, capsys, mig)
    assert out is not None
    assert "migration" in out["additionalContext"].lower()


def test_path_outside_git_is_silent(monkeypatch, capsys, tmp_path: Path) -> None:
    """Outside a repo ``ls-files`` fails → counted as new/untracked → no
    reminder. Only a broken git (exception → can't tell) keeps the note."""
    spec = tmp_path / "docs" / "backlog" / "loose.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# spec\n", encoding="utf-8")
    assert _run(monkeypatch, capsys, spec) is None


def test_non_trigger_path_is_silent(monkeypatch, capsys, tmp_path: Path) -> None:
    f = tmp_path / "readme.txt"
    f.write_text("x\n", encoding="utf-8")
    assert _run(monkeypatch, capsys, f) is None

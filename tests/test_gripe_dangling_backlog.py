"""Gripe comments remain auditable when delete-on-ship removes a spec."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/gripe-dangling-backlog"


@pytest.fixture
def comment_snapshot(tmp_path: Path) -> Path:
    """Full comment-view fixture includes history, nested links and punctuation."""
    snapshot = tmp_path / "comments.jsonl"
    rows = [
        {
            "id": "gr347551",
            "text": "# gripe 347551\ntags: STATUS:triaged\n"
            "## comment 1 · 2026-10-11 15:22Z\n"
            "Folded into `docs/backlog/spend-governance.md`.\n"
            "See [plan](docs/backlog/spend-governance.md#checks), "
            "docs/backlog/still-open.md and docs/backlog/threads/retired.md.\n",
        },
        {
            "id": 42,
            "text": "# gripe 42\ntags: STATUS:done\n"
            "## comment 1 · 2026-10-11 15:22Z\n"
            "Folded into docs/backlog/spend-governance.md.\n"
            "## comment 2 · 2026-10-12 10:00Z\nShipped on main as abcdef1234.",
        },
    ]
    snapshot.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    return snapshot


def _lint(snapshot: Path, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--comments", str(snapshot), "--root", str(root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_reports_missing_comment_paths(comment_snapshot: Path, tmp_path: Path) -> None:
    backlog = tmp_path / "docs/backlog"
    backlog.mkdir(parents=True)
    (backlog / "still-open.md").write_text("# Active item\n", encoding="utf-8")
    result = _lint(comment_snapshot, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "gr42 docs/backlog/spend-governance.md",
        "gr347551 docs/backlog/spend-governance.md",
        "gr347551 docs/backlog/threads/retired.md",
    ]
    assert "3 dangling citation(s)" in result.stderr

    # Restoring every cited file makes the same historical comments clean.
    (backlog / "spend-governance.md").write_text("# Restored\n", encoding="utf-8")
    (backlog / "threads").mkdir()
    (backlog / "threads/retired.md").write_text("# Restored\n", encoding="utf-8")
    clean = _lint(comment_snapshot, tmp_path)
    assert clean.returncode == 0, clean.stderr
    assert clean.stdout == ""
    assert "0 dangling citation(s)" in clean.stderr


@pytest.mark.parametrize(
    "content", ["not JSON", "[]", '{"id":"not-a-gripe","text":"x"}', '{"id":42}']
)
def test_invalid_snapshot_fails(tmp_path: Path, content: str) -> None:
    (tmp_path / "docs/backlog").mkdir(parents=True)
    snapshot = tmp_path / "comments.jsonl"
    snapshot.write_text(content, encoding="utf-8")
    result = _lint(snapshot, tmp_path)
    assert result.returncode == 2
    assert "line 1:" in result.stderr
    assert "dangling citation(s)" not in result.stderr


def test_wrong_checkout_fails(comment_snapshot: Path, tmp_path: Path) -> None:
    result = _lint(comment_snapshot, tmp_path)
    assert result.returncode == 2
    assert "--root must contain docs/backlog/" in result.stderr

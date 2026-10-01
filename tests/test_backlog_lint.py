"""scripts/backlog-lint: done-marked sub-parts of open specs must not be loud (gr458459)."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "backlog-lint"


def _lint(d: Path) -> str:
    out = subprocess.run(
        ["bash", str(SCRIPT), str(d)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return out.stdout


def _fm(status: str) -> str:
    return f"---\nstatus: {status}\npillar: platform\n---\n"


def test_checked_subitem_in_open_status_spec_is_quiet(tmp_path: Path) -> None:
    for st in ("idea", "draft", "ready", "in-progress"):
        (tmp_path / f"{st}.md").write_text(
            _fm(st) + "# Spec\n\n- [x] a sub-part shipped\n", encoding="utf-8"
        )
    out = _lint(tmp_path)
    assert "marked done but still in" not in out
    assert "4 done-marked sub-part(s)" in out


def test_marker_without_open_signal_is_loud(tmp_path: Path) -> None:
    (tmp_path / "thread.md").write_text(
        "# thread\n\n- **x** — SHIPPED 2026-09-30 as foo\n", encoding="utf-8"
    )
    out = _lint(tmp_path)
    assert "1 item(s) marked done" in out
    assert "thread.md:3" in out


def test_h1_done_marker_is_loud_even_in_open_status(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text(_fm("draft") + "# Spec — DONE\n", encoding="utf-8")
    assert "1 item(s) marked done" in _lint(tmp_path)

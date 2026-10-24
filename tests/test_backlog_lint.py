"""scripts/backlog-lint: done-marked sub-parts of open specs must not be loud (gr458459)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: execs the bash script scripts/backlog-lint",
)

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


def _crowded(d: Path, prefix: str, n: int) -> None:
    for i in range(n):
        (d / f"{prefix}-{i}.md").write_text(
            _fm("idea") + f"# {prefix} {i}\n", encoding="utf-8"
        )


def test_parked_is_a_valid_status(tmp_path: Path) -> None:
    """`parked` (a deliberately shelved item) is in the closed status set —
    Reto parked hexfold-multicell-prism 2026-10-24 and the lint went red on
    a legitimate state."""
    (tmp_path / "x.md").write_text(_fm("parked") + "# Spec\n", encoding="utf-8")
    assert "status must be one of" not in _lint(tmp_path)


def test_crowded_prefix_within_baseline_is_quiet(tmp_path: Path) -> None:
    _crowded(tmp_path, "foo", 10)
    (tmp_path / "lint-baseline.txt").write_text("foo 10\n", encoding="utf-8")
    assert "crowded prefixes" not in _lint(tmp_path)


def test_crowded_prefix_over_baseline_warns(tmp_path: Path) -> None:
    _crowded(tmp_path, "foo", 10)
    (tmp_path / "lint-baseline.txt").write_text(
        "# reviewed families\nfoo 9\n", encoding="utf-8"
    )
    out = _lint(tmp_path)
    assert "crowded prefixes" in out
    assert "foo-*  10 files" in out


def test_crowded_prefix_without_baseline_still_warns(tmp_path: Path) -> None:
    _crowded(tmp_path, "foo", 9)
    assert "foo-*  9 files" in _lint(tmp_path)


def test_relative_arg_resolves_against_caller_cwd(tmp_path: Path) -> None:
    """A relative arg names a dir under the caller's cwd, not the script's
    own root — the script can be invoked through another checkout's
    scripts/ on PATH (gr514451)."""
    d = tmp_path / "mybacklog"
    d.mkdir()
    (d / "x.md").write_text("# t\n\n- **x** — SHIPPED 2026-01-01\n", encoding="utf-8")
    out = subprocess.run(
        ["bash", str(SCRIPT), "mybacklog"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    assert "mybacklog/x.md:3" in out

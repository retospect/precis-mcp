"""``scripts/ship --quick`` type-checks the changed python files (gr579772).

Three quick lands reddened main on ``mypy src tests`` on 2026-11-09/10
because the quick lane skipped mypy. Two halves:

* scripts/ship builds the pre-qland lint command with a
  ``scripts/lib/mypy_changed.py <changed src/ tests/ .py files>`` leg, and
  ``PRECIS_SHIP_QUICK_MYPY=0`` drops it. Driven through the real ship script
  on the test_ship_narrow_lock rig; the fake gate records the command ship
  would have run (``PRECIS_SHIP_TEST_GATE_ARG``).
* ``scripts/lib/mypy_changed.py`` fails on a type error in a changed file
  and ignores one that only surfaces in an unchanged imported module. Real
  mypy on a throwaway tree.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_ship_narrow_lock import REPO, Rig, _finish

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)

_HELPER = REPO / "scripts" / "lib" / "mypy_changed.py"
_BAD = "def f() -> int:\n    return 'not an int'\n"
_GOOD = "def g() -> int:\n    return 1\n"


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def _gate_cmd(rig: Rig, name: str, files: dict[str, str], quick_mypy: str = "1") -> str:
    """Ship ``files`` --quick; return the pre-qland command the gate got."""
    wt = rig.worktree(name, files)
    out_file = rig.root / f"{name}.gatecmd"
    p, log = rig.ship(
        wt,
        "--quick",
        sleep=0,
        PRECIS_SHIP_TEST_GATE_CMD=f'printf %s "$PRECIS_SHIP_TEST_GATE_ARG" > {out_file}',
        PRECIS_SHIP_QUICK_MYPY=quick_mypy,
    )
    out = _finish(p, log)
    assert out_file.exists(), out
    return out_file.read_text(encoding="utf-8")


def test_quick_lane_runs_mypy_on_the_changed_python_files_only(rig: Rig) -> None:
    cmd = _gate_cmd(
        rig,
        "mypyon",
        {
            "src/precis/newmod.py": _GOOD,
            "tests/test_newmod.py": _GOOD,
            "docs/notes.md": "# notes\n",
            "scripts/tool.py": _GOOD,  # outside src/ and tests/: not typed here
        },
    )
    assert (
        "uv run python scripts/lib/mypy_changed.py"
        " src/precis/newmod.py tests/test_newmod.py && "
    ) in cmd, cmd
    assert "docs/notes.md" not in cmd and "scripts/tool.py" not in cmd, cmd
    # Plain --quick must not widen to the whole tree.
    assert "mypy src tests" not in cmd, cmd


def test_env_escape_skips_the_mypy_leg(rig: Rig) -> None:
    cmd = _gate_cmd(
        rig,
        "mypyoff",
        {"src/precis/newmod.py": _GOOD},
        quick_mypy="0",
    )
    assert "mypy_changed" not in cmd and "mypy" not in cmd, cmd
    assert "uv run ruff check" in cmd, cmd  # the rest of the lint still runs


def test_no_python_in_the_diff_skips_the_mypy_leg(rig: Rig) -> None:
    cmd = _gate_cmd(
        rig,
        "nopy",
        {"src/precis/data.txt": "x\n", "tests/fixture.json": "{}\n"},
    )
    assert "mypy" not in cmd, cmd


def _run_helper(cwd: Path, *files: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_HELPER), *files],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_changed_file_with_a_type_error_fails(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(_BAD, encoding="utf-8")
    r = _run_helper(tmp_path, "bad.py")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "bad.py:2: error:" in r.stdout, r.stdout
    assert "reproduce: uv run mypy bad.py" in r.stdout, r.stdout


def test_error_only_in_an_unchanged_imported_file_passes(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(_BAD, encoding="utf-8")
    (tmp_path / "good.py").write_text(
        "import bad\n\n\ndef h() -> int:\n    return bad.f()\n", encoding="utf-8"
    )
    # Sanity: the full run really is red on bad.py (the filter is not vacuous).
    full = subprocess.run(
        [sys.executable, "-m", "mypy", "good.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert "bad.py:2: error:" in full.stdout, full.stdout
    r = _run_helper(tmp_path, "good.py")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 error(s) in unchanged imported files ignored" in r.stdout, r.stdout


def test_clean_changed_file_passes(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text(_GOOD, encoding="utf-8")
    r = _run_helper(tmp_path, "ok.py")
    assert r.returncode == 0, r.stdout + r.stderr


def test_filter_keeps_notes_and_context_of_changed_files_only() -> None:
    spec = importlib.util.spec_from_file_location("mypy_changed", _HELPER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    filter_diagnostics = mod.filter_diagnostics

    out = "\n".join(
        [
            "src/a.py:3: error: Incompatible return value type  [return-value]",
            "src/a.py:3: note: Expected something",
            "    detail line",
            "src/b.py:9: error: unrelated  [misc]",
            "src/b.py:9: note: b's note",
            "Found 2 errors in 2 files (checked 2 source files)",
        ]
    )
    got = filter_diagnostics(out, ["./src/a.py"])
    assert got == out.splitlines()[:3], got
    assert filter_diagnostics(out, ["src/c.py"]) == []
    # A lone note in a changed file is not a red.
    assert filter_diagnostics("src/a.py:1: note: hi", ["src/a.py"]) == []

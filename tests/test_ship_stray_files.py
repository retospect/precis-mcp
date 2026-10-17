"""scripts/ship refuses a branch that ADDS scratch files or a bare assert-False probe.

Leaks onto main: ship-pgbouncer-tag.log, tests/test_zz_*.py probes (one with
`assert False` turned main red), a sed `-e` backup. Reuses the real-git rig of
test_ship_narrow_lock; see that module's docstring.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.test_ship_narrow_lock import Rig, _finish, _tree_files

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)

# Built by concatenation so this file itself never holds a bare probe line.
_BARE = "    assert " + "False\n"


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("tests/test_zz_scratch_rc.py", "x = 1\n"),
        ("tests/test_zz_tmp_measure.py", "x = 1\n"),
        ("ship-pgbouncer-tag.log", "noise\n"),
        ("tests/test_message_delivery_stamp.py-e", "x\n"),
        ("src/mod.py-e", "x\n"),
        ("src/mod.py.orig", "x\n"),
        ("tests/test_ok.py", "def test_x():\n" + _BARE),
        ("tests/test_ok.py", "def test_x():\n" + _BARE.rstrip() + "  # probe\n"),
    ],
)
def test_added_stray_is_refused_and_named(rig: Rig, path: str, body: str) -> None:
    wt = rig.worktree("s", {"ok.txt": "fine\n", path: body})
    main_before = rig.origin_ref("main")
    p, log = rig.ship(wt, "--quick", sleep=1)
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "stray files or debug probes" in out, out
    assert path in out, out
    assert rig.origin_ref("main") == main_before


def test_normal_diff_passes(rig: Rig) -> None:
    wt = rig.worktree(
        "n",
        {
            "pkg/real.py": "def f():\n    assert False, 'message form is fine'\n",
            "pkg/sub/run.log": "nested logs are not root-level\n",
            "tests/test_real.py": "def test_a():\n    assert True\n",
        },
    )
    out = _finish(*rig.ship(wt, "--quick", sleep=1))
    assert "stray files" not in out, out
    assert "tests/test_real.py" in _tree_files(rig, "main")


def test_existing_tracked_stray_is_not_flagged(rig: Rig) -> None:
    rig.race(
        {
            "tests/test_zz_old.py": "x = 1\n",
            "old.log": "legacy\n",
            "bare.py": "def f():\n" + _BARE,
        }
    )
    wt = rig.worktree("e", {"ok.txt": "fine\n"})
    out = _finish(*rig.ship(wt, "--quick", sleep=1))
    assert "stray files" not in out, out
    assert "ok.txt" in _tree_files(rig, "main")

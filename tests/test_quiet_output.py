"""Quiet-by-default transcripts for scripts/rebase and scripts/ship (gr497896).

The default run writes its full transcript to a /tmp log (path printed) and
the terminal sees only the epilogue: ✖ lines, a conflict's file list, the
log path and the final status token. ``--verbose``/``PRECIS_SHIP_VERBOSE=1``
is the old full stream. These tests build temp git repos — no network: a
local ``main`` ref is the rebase base and the ship gate is the fake lint.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_ship_narrow_lock import Rig, _finish

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash scripts are POSIX-only"
)

REPO = Path(__file__).resolve().parent.parent
_BASH = "/bin/bash"


def _g(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _commit(repo: Path, rel: str, body: str, msg: str) -> str:
    (repo / rel).write_text(body, encoding="utf-8")
    _g(repo, "add", rel)
    _g(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", msg)
    return _g(repo, "rev-parse", "HEAD")


@pytest.fixture
def rebase_repo(tmp_path: Path) -> Path:
    """A plain repo on main carrying scripts/rebase + the quiet lib."""
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    (scripts / "lib").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "rebase", scripts / "rebase")
    shutil.copy2(REPO / "scripts" / "lib" / "quiet-log.sh", scripts / "lib")
    _g(repo, "init", "-q", "-b", "main")
    # Local identity: the container's git has no global user.*, and rebase's
    # inner commits need one too.
    _g(repo, "config", "user.email", "t@example.invalid")
    _g(repo, "config", "user.name", "t")
    _commit(repo, "shared.txt", "base\n", "base")
    return repo


def _rebase(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_BASH, "scripts/rebase", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )


def _log_path(out: str) -> Path:
    m = re.search(r"^full log: (\S+)$", out, re.M)
    assert m, f"no 'full log:' line in {out!r}"
    return Path(m.group(1))


def test_rebase_quiet_uptodate(rebase_repo: Path) -> None:
    proc = _rebase(rebase_repo, "main")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.rstrip().endswith("REBASE_STATUS: uptodate")
    assert "▶" not in proc.stdout
    log = _log_path(proc.stdout)
    assert log.exists() and "already contains" in log.read_text(encoding="utf-8")


def test_rebase_quiet_clean(rebase_repo: Path) -> None:
    _g(rebase_repo, "checkout", "-q", "-b", "feat")
    _commit(rebase_repo, "f.txt", "f\n", "feat")
    _g(rebase_repo, "checkout", "-q", "main")
    _commit(rebase_repo, "m.txt", "m\n", "main moved")
    _g(rebase_repo, "checkout", "-q", "feat")
    proc = _rebase(rebase_repo, "main")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.rstrip().endswith("REBASE_STATUS: clean")
    assert "rebasing feat" not in proc.stdout
    assert "rebasing feat" in _log_path(proc.stdout).read_text(encoding="utf-8")


def test_rebase_quiet_conflict_lists_files_no_diffs(rebase_repo: Path) -> None:
    _g(rebase_repo, "checkout", "-q", "-b", "feat")
    _commit(rebase_repo, "shared.txt", "mine\n", "feat edit")
    _g(rebase_repo, "checkout", "-q", "main")
    _commit(rebase_repo, "shared.txt", "theirs\n", "main edit")
    _g(rebase_repo, "checkout", "-q", "feat")
    proc = _rebase(rebase_repo, "main")
    assert proc.returncode == 10, proc.stdout + proc.stderr
    assert proc.stdout.rstrip().endswith("REBASE_STATUS: conflict")
    assert "shared.txt" in proc.stderr  # the conflicted file list
    assert "<<<<<<<" not in proc.stdout + proc.stderr  # no diffs/markers
    assert "✖" not in proc.stdout + proc.stderr  # a conflict is not a failure
    gitdir = Path(_g(rebase_repo, "rev-parse", "--absolute-git-dir"))
    assert (gitdir / "rebase-merge").is_dir() or (gitdir / "rebase-apply").is_dir()
    _g(rebase_repo, "rebase", "--abort")


def test_rebase_verbose_is_the_old_stream(rebase_repo: Path) -> None:
    _g(rebase_repo, "checkout", "-q", "-b", "feat")
    _commit(rebase_repo, "f.txt", "f\n", "feat")
    _g(rebase_repo, "checkout", "-q", "main")
    _commit(rebase_repo, "m.txt", "m\n", "main moved")
    _g(rebase_repo, "checkout", "-q", "feat")
    proc = _rebase(rebase_repo, "main", "--verbose")
    assert proc.returncode == 0
    assert "rebasing feat" in proc.stdout
    assert proc.stdout.rstrip().endswith("REBASE_STATUS: clean")
    assert "full log:" not in proc.stdout


def test_epilogue_surfaces_crashes_without_cross(tmp_path: Path) -> None:
    """A bare crash (no ✖) must still be readable: Traceback blocks, error
    lines and the log tail all reach stderr (gr497896 review)."""
    log = tmp_path / "crash.log"
    log.write_text(
        "▶ step one\n"
        "noise line\n"
        "Traceback (most recent call last):\n"
        '  File "x.py", line 3, in f\n'
        "    boom()\n"
        "RuntimeError: blew up\n"
        "some PASSED test\n"
        "FAILED tests/test_x.py::test_y - assert 1 == 2\n"
        "tail-4\ntail-3\ntail-2\ntail-1\ntail-0\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            _BASH,
            "-c",
            f'. "{REPO}/scripts/lib/quiet-log.sh"; quiet_epilogue "{log}" 1 SHIP_STATUS shipped',
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    err = proc.stderr
    for want in ("Traceback (most recent call last):", "RuntimeError: blew up",
                 "FAILED tests/test_x.py::test_y", "tail-0", "tail-4"):  # fmt: skip
        assert want in err, err
    assert "▶ step one" not in err  # the tail is the LAST lines, not the log
    assert proc.stdout.rstrip().endswith("SHIP_STATUS: error")
    # rc 0: no tail dump, no error lines.
    log2 = tmp_path / "ok.log"
    log2.write_text("noise line\ndone\n", encoding="utf-8")
    ok = subprocess.run(
        [
            _BASH,
            "-c",
            f'. "{REPO}/scripts/lib/quiet-log.sh"; quiet_epilogue "{log2}" 0 SHIP_STATUS shipped',
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert ok.stdout.rstrip().endswith("SHIP_STATUS: shipped")
    assert "noise line" not in ok.stderr


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_ship_quiet_success(rig: Rig) -> None:
    wt = rig.worktree("q", {"q.txt": "q\n"})
    p, log = rig.ship(wt, "--quick", sleep=1, PRECIS_SHIP_VERBOSE="0")
    out = _finish(p, log)
    assert out.rstrip().endswith("SHIP_STATUS: shipped"), out
    assert "▶" not in out and "syncing origin/main" not in out
    full = _log_path(out)
    assert full.exists()
    text = full.read_text(encoding="utf-8")
    assert "shipped — landed on main" in text
    assert "q.txt" in set(
        rig.g(
            rig.origin, "ls-tree", "-r", "--name-only", "refs/heads/main"
        ).splitlines()
    )


def test_ship_quiet_red_gate(rig: Rig) -> None:
    wt = rig.worktree("red", {"r.txt": "r\n"})
    p, log = rig.ship(
        wt,
        "--quick",
        sleep=1,
        PRECIS_SHIP_VERBOSE="0",
        PRECIS_FAKE_LINT_FAIL="1",
    )
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc == 1, out
    assert "✖" in out  # the failure line still reaches the terminal
    assert out.rstrip().endswith("SHIP_STATUS: error"), out
    # Only the epilogue reaches the terminal: early-step banners are gone,
    # and even the failure tail is a handful of lines.
    assert "syncing origin/main" not in out
    assert len(out.splitlines()) < 15, out


def test_ship_quiet_conflict_lists_files(rig: Rig) -> None:
    wt = rig.worktree("c", {"shared.txt": "mine\n"})
    rig.race({"shared.txt": "theirs\n"})
    p, log = rig.ship(wt, "--quick", sleep=1, PRECIS_SHIP_VERBOSE="0")
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc == 1, out
    assert out.rstrip().endswith("SHIP_STATUS: conflict"), out
    assert "conflicted files:" in out and "shared.txt" in out
    assert "<<<<<<<" not in out  # names, never diffs
    gitdir = Path(rig.g(wt, "rev-parse", "--absolute-git-dir"))
    assert (gitdir / "rebase-merge").is_dir() or (gitdir / "rebase-apply").is_dir()
    rig.g(wt, "rebase", "--abort")

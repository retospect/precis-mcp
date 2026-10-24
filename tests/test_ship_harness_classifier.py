"""scripts/ship's release-train harness classifier (docs/backlog/release-train.md §1).

Harness — any path under ``scripts/ship``, ``scripts/test``, ``scripts/lib/``
or ``.github/`` — keeps a pre-merge gate: a harness diff is refused by the
ungated lanes (``--quick``, ``--checked``) and named to ``--remote``, whose
``ci/<branch>`` check.yml run is that gate. Everything else — ``uv.lock``,
``pyproject.toml``, migrations and schema included — lands ``--quick``, so a
non-harness diff on ``--remote`` is refused and named to ``--quick``.

Reuses the real-git rig of test_ship_narrow_lock (origin.git, primary,
worktrees); see that module's docstring. The ``gh`` stub drives the remote
lane: ``api`` fails (an unreadable in-flight count proceeds), ``run list``
answers one run, ``run view`` says it completed green — so a harness diff
under ``--remote`` exercises the whole lane and lands.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.test_ship_narrow_lock import Rig, _finish, _tree_files

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)

_GH_STUB = """\
#!/bin/sh
case "$1" in
  api) exit 1 ;;  # unreadable in-flight count -> admit
  run)
    case "$2" in
      list) echo 1; exit 0 ;;
      view)
        for a in "$@"; do
          case "$a" in
            status,conclusion) echo "completed success"; exit 0 ;;
            url) echo "https://example.invalid/run/1"; exit 0 ;;
          esac
        done
        exit 0 ;;
      *) exit 0 ;;
    esac ;;
  *) exit 0 ;;
esac
"""


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def _gh_on_path(rig: Rig) -> None:
    gh = rig.root / "fakebin" / "gh"
    gh.write_text(_GH_STUB, encoding="utf-8")
    gh.chmod(0o755)


def _harness_wt(rig: Rig, name: str, path: str) -> Path:
    """A worktree whose diff touches ``path`` — appended to when the base
    already has it (scripts/ship), created otherwise."""
    wt = rig.worktree(name, {f"{name}.txt": f"{name}\n"})
    p = wt / path
    if p.exists():
        with p.open("a", encoding="utf-8") as fh:
            fh.write("\n# harness touch\n")
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# harness\n", encoding="utf-8")
    rig.g(wt, "add", "-A")
    rig.g(wt, "commit", "-q", "-m", "harness")
    return wt


@pytest.mark.parametrize(
    "path",
    [
        "scripts/test",
        "scripts/lib/new-helper.sh",
        ".github/workflows/check.yml",
        # A modified scripts/ship is the diff this file itself will ship.
        "scripts/ship",
    ],
)
def test_harness_diff_refused_by_quick(rig: Rig, path: str) -> None:
    wt = _harness_wt(rig, "h", path)
    main0 = rig.origin_ref("main")

    p, log = rig.ship(wt, "--quick", sleep=1)
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")

    assert rc != 0, out
    assert "touches the landing harness" in out, out
    assert "--remote" in out, out
    assert not (rig.lintdir / "h.started").exists(), "refused after a gate ran"
    assert rig.origin_ref("main") == main0
    assert not rig.lockdir().exists()


@pytest.mark.parametrize("path", ["scripts/test", ".github/workflows/check.yml"])
def test_harness_diff_refused_by_checked(rig: Rig, path: str) -> None:
    wt = rig.worktree("hc", {path: "# harness\n", "hc.txt": "c\n"})
    p, log = rig.ship(wt, "--checked", sleep=1)
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "touches the landing harness" in out, out
    assert "--remote" in out, out
    assert not (rig.lintdir / "hc.started").exists()


@pytest.mark.parametrize(
    "files",
    [
        {"nh.txt": "n\n", "src/precis/x.py": "X = 1\n"},
        # uv.lock and pyproject.toml are explicitly NOT harness (Reto
        # 2026-10-23): the env they shape is gated by the release cut.
        {"uv.lock": 'version = 1\nrequires-python = ">=3.12"\n'},
        {"pyproject.toml": '[project]\nname = "x"\n'},
        # scripts/round and scripts/release are tooling, not the landing
        # harness: not in the ruling's list.
        {"scripts/round": "#!/bin/sh\nexit 0\n"},
        {"scripts/release": "#!/bin/sh\nexit 0\n"},
    ],
    ids=["plain-src", "uv-lock", "pyproject", "scripts-round", "scripts-release"],
)
def test_non_harness_diff_refused_by_remote(rig: Rig, files: dict) -> None:
    _gh_on_path(rig)
    wt = rig.worktree("nh", files)
    main0 = rig.origin_ref("main")
    p, log = rig.ship(wt, "--remote", sleep=1)
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "--remote is for harness diffs" in out, out
    assert "--quick" in out, out
    assert rig.origin_ref("main") == main0
    assert not rig.lockdir().exists()


@pytest.mark.parametrize(
    "files",
    [
        {"uv.lock": 'version = 1\nrequires-python = ">=3.12"\n'},
        {"pyproject.toml": '[project]\nname = "x"\n'},
    ],
    ids=["uv-lock", "pyproject"],
)
def test_env_files_land_under_quick(rig: Rig, files: dict) -> None:
    """uv.lock/pyproject.toml change the test env for every run, but the
    ruling's harness list leaves them out (and migrations and schema): they
    qland."""
    wt = rig.worktree("lk", files)
    p, log = rig.ship(wt, "--quick", sleep=1)
    out = _finish(p, log)
    assert set(files) <= _tree_files(rig), out


def test_harness_diff_lands_under_remote(rig: Rig) -> None:
    """The positive control: a harness diff IS admitted to the remote lane —
    the gh stub's green run lands it on main."""
    _gh_on_path(rig)
    wt = rig.worktree("hr", {"scripts/lib/new-helper.sh": "#!/bin/sh\nexit 0\n"})
    p, log = rig.ship(wt, "--remote", sleep=1)
    out = _finish(p, log)
    assert "remote gate GREEN" in out, out
    assert "scripts/lib/new-helper.sh" in _tree_files(rig), out


def test_mixed_diff_with_a_harness_path_is_harness(rig: Rig) -> None:
    """One harness path makes the whole diff harness: a mixed change cannot
    smuggle scripts/lib/ onto main through the ungated lane."""
    wt = rig.worktree("mx", {"mx.txt": "m\n", "scripts/lib/another.sh": "#!/bin/sh\n"})
    p, log = rig.ship(wt, "--quick", sleep=1)
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "touches the landing harness" in out, out

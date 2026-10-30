"""gr514898 — scripts/inflight was O(several git subprocesses per worktree),
serial, so a fleet-size box (>140 trees) took >10 s before ANY output and
then dumped the full 140-row table (gr514784, gr514947 were the same
report).

The shipped shape pins: one batched `git for-each-ref` pass for the
per-branch facts, the remaining per-tree probe (one `git status`, plus
`git cherry`/`git merge-tree` only for branches ahead of main) fanned out
to bounded parallel subshells, and a default listing ordered
self → live/overlapping → quiet with the quiet tail capped (`--all` to opt
out).

Exercises the REAL `scripts/inflight` copied byte-for-byte into a
throwaway repo, mirroring tests/test_inflight_branchless_branches.py.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the shipped bash scripts are POSIX-only",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
INFLIGHT_SRC = REPO_ROOT / "scripts" / "inflight"

CAP = 40  # INFLIGHT_MAX_ROWS default in scripts/inflight


def _test_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    return env


def _run(
    cmd: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env or _test_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"cmd failed: {cmd}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd)


@pytest.fixture
def fleet_repo(tmp_path: Path) -> Path:
    """Primary on `main` plus CAP+8 merged+clean 'quiet' worktrees — enough
    to overflow the default cap — staged with the real scripts/inflight."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "main")

    scripts_dir = primary / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(INFLIGHT_SRC, scripts_dir / "inflight")
    (scripts_dir / "inflight").chmod(0o755)

    (primary / "README.md").write_text("root\n", encoding="utf-8")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "initial")

    for i in range(CAP + 8):
        _git(
            primary,
            "worktree",
            "add",
            "-q",
            "-b",
            f"worktree-quiet-{i:02d}",
            str(tmp_path / f"quiet-{i:02d}"),
        )
    return primary


def _table_lines(out: str) -> list[str]:
    """Header + data rows only — drop the cap note, the Removable footer
    block, and the cached gripe digest line."""
    skip = ("…", "Removable", "→", "gripes:")
    return [
        ln for ln in out.splitlines() if ln.strip() and not ln.strip().startswith(skip)
    ]


def test_default_listing_is_capped_with_overlap_first(fleet_repo: Path) -> None:
    """A listing bigger than the cap keeps every overlap row (live, dirty,
    ahead) and hides only the quiet tail behind a '+N more' line."""
    tmp_path = fleet_repo.parent
    # Two overlap trees created AFTER the quiet block, so porcelain order
    # alone would bury them — ordering, not luck, must surface them.
    dirty_tree = tmp_path / "zzz-dirty"
    _git(fleet_repo, "worktree", "add", "-q", "-b", "worktree-zdirty", str(dirty_tree))
    (dirty_tree / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
    ahead_tree = tmp_path / "zzz-ahead"
    _git(fleet_repo, "worktree", "add", "-q", "-b", "worktree-zahead", str(ahead_tree))
    (ahead_tree / "feature.txt").write_text("shipped?\n", encoding="utf-8")
    _git(ahead_tree, "add", "-A")
    _git(ahead_tree, "commit", "-q", "-m", "zahead work")

    out = _run([str(fleet_repo / "scripts" / "inflight")], fleet_repo).stdout
    rows = _table_lines(out)

    # header + capped rows
    assert len(rows) <= CAP + 1, f"{len(rows)} lines — cap failed:\n{out}"
    # the cap note names how many were hidden
    m = re.search(r"… \+(\d+) more quiet trees — scripts/inflight --all", out)
    assert m, out
    total_rows = CAP + 8 + 2 + 1  # quiet + overlap + primary
    assert int(m.group(1)) == total_rows - (len(rows) - 1), out

    # overlap rows must appear, and before every surviving quiet row
    shown_before_note = out.split("… +")[0]
    assert "zzz-dirty" in shown_before_note, out
    assert "zzz-ahead" in shown_before_note, out
    quiet_idx = [i for i, ln in enumerate(rows) if "quiet-" in ln]
    assert quiet_idx, out
    assert shown_before_note.index("zzz-dirty") < shown_before_note.index(
        rows[quiet_idx[-1]].split()[0]
    ), out
    assert shown_before_note.index("zzz-ahead") < shown_before_note.index(
        rows[quiet_idx[-1]].split()[0]
    ), out


def test_inflight_all_lists_everything(fleet_repo: Path) -> None:
    out = _run([str(fleet_repo / "scripts" / "inflight"), "--all"], fleet_repo).stdout
    rows = _table_lines(out)
    for i in range(CAP + 8):
        assert f"quiet-{i:02d}" in out, out
    assert "more quiet trees" not in out, out
    # header + primary + all worktrees
    assert len(rows) == 1 + 1 + (CAP + 8), out


def test_small_listing_is_untouched(tmp_path: Path) -> None:
    """Below the cap the table is the whole roster — no 'more' line, no
    dropped rows."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "main")
    scripts_dir = primary / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(INFLIGHT_SRC, scripts_dir / "inflight")
    (scripts_dir / "inflight").chmod(0o755)
    (primary / "README.md").write_text("root\n", encoding="utf-8")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "initial")
    for i in range(3):
        _git(
            primary,
            "worktree",
            "add",
            "-q",
            "-b",
            f"worktree-w{i}",
            str(tmp_path / f"w{i}"),
        )

    out = _run([str(primary / "scripts" / "inflight")], primary).stdout
    rows = _table_lines(out)
    assert len(rows) == 1 + 1 + 3, out
    assert "more quiet trees" not in out, out
    for i in range(3):
        assert f"w{i}" in out


def test_first_output_arrives_before_the_scan(tmp_path: Path) -> None:
    """The gr514898 UX ask: something must print in well under a second —
    the scan announces itself on stderr before the parallel probes run, so
    stdout stays machine-clean."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "main")
    scripts_dir = primary / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(INFLIGHT_SRC, scripts_dir / "inflight")
    (scripts_dir / "inflight").chmod(0o755)
    (primary / "README.md").write_text("root\n", encoding="utf-8")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "initial")

    result = _run([str(primary / "scripts" / "inflight")], primary)
    assert "scanning" in result.stderr, result.stderr
    assert result.stdout.startswith("WORKTREE"), result.stdout


def test_git_calls_do_not_scale_with_tree_count(tmp_path: Path) -> None:
    """The old shape ran ~8 git subprocesses per worktree serially — the
    regression this fixes. A `git` shim on PATH counts every invocation;
    merged+clean trees must cost one probe call each plus a constant
    (worktree list, for-each-ref, a few rev-parse)."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "main")
    scripts_dir = primary / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(INFLIGHT_SRC, scripts_dir / "inflight")
    (scripts_dir / "inflight").chmod(0o755)
    (primary / "README.md").write_text("root\n", encoding="utf-8")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "initial")
    n_trees = 6
    for i in range(n_trees):
        _git(
            primary,
            "worktree",
            "add",
            "-q",
            "-b",
            f"worktree-w{i}",
            str(tmp_path / f"w{i}"),
        )

    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    git_bin = shutil.which("git")
    assert git_bin is not None
    (shim_dir / "git").write_text(
        f'#!/bin/sh\necho x >> "$GIT_CALL_LOG"\nexec "{git_bin}" "$@"\n',
        encoding="utf-8",
    )
    (shim_dir / "git").chmod(0o755)

    log = tmp_path / "git-calls.log"
    env = _test_env()
    env.update(
        {
            "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
            "GIT_CALL_LOG": str(log),
        }
    )
    result = _run([str(primary / "scripts" / "inflight")], primary, env=env)
    assert result.returncode == 0, result.stderr
    n_calls = len(log.read_text(encoding="utf-8").splitlines())
    # ~9 parent calls + 1-2 probe calls per merged+clean tree (status, plus
    # a merge-base when the git predates %(ahead-behind:) and takes the
    # fallback — the dev container's git exercises exactly that). The old
    # shape costs ~8/tree — ~60 on this rig — so the bound has headroom
    # and still fails against it.
    assert n_calls <= 2 * n_trees + 15, (
        f"{n_calls} git calls for {n_trees} clean trees — per-tree spawning is back"
    )


def test_status_walk_never_takes_index_lock(tmp_path: Path) -> None:
    """gr541144 — the per-tree `git status` (and `diff --stat` on a dirty
    tree) refreshes the index, so the probe took index.lock in every
    worktree it walked; a SessionStart hook killed mid-scan left stale
    0-byte locks behind that then blocked each tree's own writes (13 piled
    up under .git/worktrees). With --no-optional-locks the probe must run
    fine while another process holds that tree's index.lock — never
    failing on, removing or replacing it — and leave no lock of its own."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "main")
    scripts_dir = primary / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(INFLIGHT_SRC, scripts_dir / "inflight")
    (scripts_dir / "inflight").chmod(0o755)
    (primary / "README.md").write_text("root\n", encoding="utf-8")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "initial")

    held_tree = tmp_path / "held"
    _git(primary, "worktree", "add", "-q", "-b", "worktree-held", str(held_tree))
    clean_tree = tmp_path / "clean"
    _git(primary, "worktree", "add", "-q", "-b", "worktree-clean", str(clean_tree))
    # dirty + untracked so the --json walk also pays the `diff --stat`
    # probe on the lock-held tree.
    (held_tree / "wip.txt").write_text("uncommitted\n", encoding="utf-8")

    def git_dir(tree: Path) -> Path:
        out = _git(
            tree, "rev-parse", "--path-format=absolute", "--git-dir"
        ).stdout.strip()
        return Path(out)

    # A git shim on PATH pins the mechanism, not just the outcome: every
    # status/diff the walk spawns must carry --no-optional-locks.
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    git_bin = shutil.which("git")
    assert git_bin is not None
    (shim_dir / "git").write_text(
        f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$GIT_ARGV_LOG"\nexec "{git_bin}" "$@"\n',
        encoding="utf-8",
    )
    (shim_dir / "git").chmod(0o755)
    argv_log = tmp_path / "git-argv.log"
    env = _test_env()
    env.update(
        {
            "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
            "GIT_ARGV_LOG": str(argv_log),
        }
    )

    sentinel = b"sentinel - held by another process\n"
    lock = git_dir(held_tree) / "index.lock"
    lock.write_bytes(sentinel)

    inflight = primary / "scripts" / "inflight"
    _run([str(inflight), "--json"], primary, env=env)
    assert lock.read_bytes() == sentinel, (
        "the walk removed or replaced a lock it does not own"
    )

    calls = argv_log.read_text(encoding="utf-8").splitlines()
    probes = [c for c in calls if re.search(r"(?:^| )(?:status|diff)(?: |$)", c)]
    assert probes, f"expected the walk to run status/diff probes:\n{calls}"
    for c in probes:
        assert "--no-optional-locks" in c, c

    lock.unlink()
    _run([str(inflight), "--json"], primary, env=env)
    for tree in (primary, held_tree, clean_tree):
        assert not (git_dir(tree) / "index.lock").exists(), f"index.lock left in {tree}"

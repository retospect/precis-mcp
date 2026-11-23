"""``scripts/reap-factory-mirrors`` — retention for factory mirror set-asides.

gr762499: the gr762423 fix renames a corrupt per-launch mirror or tips
record aside (``<name>.broken-<stamp>`` / later ``<name>.dropped-<stamp>``)
instead of deleting it, and nothing bounded the pile — a mirror aside is a
full bare repo, so every corruption event parked ~130 MiB under the main
checkout's ``scratch/factory-mirror/`` forever. The sweep prunes an aside
once it is past the age retention (default 14 days) and logs each deletion.

These run the real script against a throwaway ``--root`` — no git needed,
the sweep never shells out when a root is named.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "reap-factory-mirrors"

DAY = 24 * 3600

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: chmod-based unremovable-path case",
)


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PRECIS_")}
    env.update(extra)
    return env


def _run(root: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(**env),
        timeout=60,
        check=False,
    )


def _mirror_root(root: Path) -> Path:
    mirror = root / "scratch" / "factory-mirror"
    mirror.mkdir(parents=True)
    return mirror


def _aged(path: Path, age_s: float, *, dir: bool = False) -> Path:
    """A set-aside at ``path`` with its mtime ``age_s`` in the past."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if dir:
        path.mkdir(exist_ok=True)
    else:
        path.write_text("wreckage\n", encoding="utf-8")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


def test_old_set_asides_are_pruned_and_everything_else_is_untouched(
    tmp_path: Path,
) -> None:
    mirror = _mirror_root(tmp_path)
    live_mirror = mirror / "td1.git"
    live_mirror.mkdir()
    live_tips = mirror / "td1.tips.json"
    live_tips.write_text("{}\n", encoding="utf-8")
    lock = mirror / "cache.git.lock"
    lock.write_text("", encoding="utf-8")
    unrelated = mirror / "source-mirror.git"
    unrelated.mkdir()
    old_broken = _aged(mirror / "td2.git.broken-20261101T000000Z", 21 * DAY, dir=True)
    old_tips = _aged(mirror / "td2.tips.json.broken-20261101T000000Z", 21 * DAY)
    old_dropped = _aged(mirror / "td3.tips.json.dropped-20261101T000000Z", 21 * DAY)
    young = _aged(mirror / "td4.git.broken-20261122T000000Z", DAY, dir=True)

    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert not old_broken.exists()
    assert not old_tips.exists()
    assert not old_dropped.exists()
    assert young.exists()
    assert live_mirror.exists()
    assert live_tips.exists()
    assert lock.exists()
    assert unrelated.exists()
    assert "pruned td2.git.broken-20261101T000000Z" in result.stdout
    assert "pruned td2.tips.json.broken-20261101T000000Z" in result.stdout
    assert "pruned td3.tips.json.dropped-20261101T000000Z" in result.stdout
    assert "3 factory mirror set-aside(s)" in result.stdout


def test_every_old_aside_of_a_base_is_pruned(tmp_path: Path) -> None:
    """A pure age rule, not keep-newest: every launch owns a base name, so
    per-launch retention could never bound the disk (gr762499)."""
    mirror = _mirror_root(tmp_path)
    asides = [
        _aged(mirror / f"td1.git.broken-2026100{i}T000000Z", 30 * DAY, dir=True)
        for i in range(3)
    ]

    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    for aside in asides:
        assert not aside.exists()
    assert "pruned 3 factory mirror set-aside(s)" in result.stdout


def test_dry_run_reports_without_removing(tmp_path: Path) -> None:
    mirror = _mirror_root(tmp_path)
    old = _aged(mirror / "td1.git.broken-20261101T000000Z", 21 * DAY, dir=True)

    result = _run(tmp_path, "--dry-run")

    assert result.returncode == 0, result.stderr
    assert old.exists()
    assert "would prune td1.git.broken-20261101T000000Z" in result.stdout


def test_for_hook_is_silent_when_nothing_is_past_retention(tmp_path: Path) -> None:
    mirror = _mirror_root(tmp_path)
    _aged(mirror / "td1.git.broken-20261122T000000Z", DAY, dir=True)

    quiet = _run(tmp_path, "--for-hook")
    human = _run(tmp_path)

    assert quiet.returncode == 0 and quiet.stdout == ""
    assert "nothing to prune" in human.stdout


def test_no_autoreap_hatch_is_a_no_op(tmp_path: Path) -> None:
    mirror = _mirror_root(tmp_path)
    old = _aged(mirror / "td1.git.broken-20261101T000000Z", 21 * DAY, dir=True)

    result = _run(tmp_path, PRECIS_NO_AUTOREAP="1")

    assert result.returncode == 0
    assert result.stdout == ""
    assert old.exists()


def test_age_override_via_env(tmp_path: Path) -> None:
    mirror = _mirror_root(tmp_path)
    two_days = _aged(mirror / "td1.git.broken-20261121T000000Z", 2 * DAY, dir=True)

    result = _run(tmp_path, PRECIS_FACTORY_ASIDE_MAX_AGE_SECONDS="3600")

    assert result.returncode == 0, result.stderr
    assert not two_days.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the chmod guard")
def test_unremovable_path_is_reported_and_keeps_the_sweep_alive(
    tmp_path: Path,
) -> None:
    """chmod 0 on the aside itself makes the unlink fail: the sweep reports
    the survivor and still exits 0 (best-effort, never fatal)."""
    mirror = _mirror_root(tmp_path)
    stuck = _aged(mirror / "td1.git.broken-20261101T000000Z", 21 * DAY, dir=True)
    gone = _aged(mirror / "td2.git.broken-20261101T000000Z", 21 * DAY, dir=True)
    stuck.chmod(0)

    try:
        result = _run(tmp_path)
    finally:
        stuck.chmod(0o755)

    assert result.returncode == 0, result.stderr
    assert "td1.git.broken-20261101T000000Z past retention but not removed" in (
        result.stdout
    )
    assert stuck.exists()
    assert not gone.exists()


# ── the scripts/reap-worktrees wiring ──────────────────────────────


def _stage_repo(tmp_path: Path, log: Path) -> Path:
    """A minimal git repo holding copies of reap-worktrees (with the lib it
    sources), a stub inflight, and a STUB reap-factory-mirrors that records
    its argv — the wiring test asserts the sweep's invocation shape without
    depending on uv for the inner script's shebang."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "lib").mkdir(parents=True)
    for name in ("reap-worktrees",):
        shutil.copy2(REPO / "scripts" / name, repo / "scripts" / name)
    for name in ("git-network.sh", "git_network.py"):
        shutil.copy2(REPO / "scripts" / "lib" / name, repo / "scripts" / "lib" / name)
    stub = repo / "scripts" / "reap-factory-mirrors"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        "for last do :; done\n"
        'touch "$last/.stub-touched"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    inflight = repo / "scripts" / "inflight"
    inflight.write_text(
        '#!/bin/sh\n[ "$1" = "--json" ] && echo \'{"worktrees": []}\'\n',
        encoding="utf-8",
    )
    inflight.chmod(0o755)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "init",
            "-q",
            "-b",
            "main",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def test_reap_worktrees_invokes_the_sweep_against_the_primary_checkout(
    tmp_path: Path,
) -> None:
    log = tmp_path / "stub.log"
    repo = _stage_repo(tmp_path, log)
    primary = (
        subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        .stdout.splitlines()[0]
        .partition(" ")[2]
    )

    result = subprocess.run(
        ["bash", "scripts/reap-worktrees", "--dry-run"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(),
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines, "reap-worktrees never invoked the sweep"
    args = lines[0].split()
    assert "--dry-run" in args
    assert "--root" in args
    assert args[args.index("--root") + 1] == primary
    # The stub proved it ran against the named root.
    assert (Path(primary) / ".stub-touched").exists()

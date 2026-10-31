"""gr541365 — the SessionStart hooks ran the fleet scan (one `git status` per
worktree) seven times per session start, across dozens of concurrently
starting sessions: load average 90, hooks taking minutes.

scripts/lib/inflight_cache.py now serves `--for-hook` and
`--json --skip-branch-probes` from one shared, single-flight scan. Pinned
here: the cached answers are the live answers (hook counts line, JSON with the
caller's own tree bucketed `self`), a second call inside the TTL scans nothing,
and the shapes that must stay live (plain `--json`, `INFLIGHT_NO_CACHE=1`,
TTL 0) do.

Exercises the REAL scripts copied into a throwaway repo, mirroring
tests/test_inflight_speed.py.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the shipped bash scripts are POSIX-only",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CACHE_NAME = "precis-inflight-cache.json"


def _env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("INFLIGHT_NO_CACHE", None)
    env.pop("INFLIGHT_NEUTRAL", None)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    env.update(extra)
    return env


def _run(cmd: list[str], cwd: Path, **env: str) -> str:
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=_env(**env),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"cmd failed: {cmd}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result.stdout


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", *args], cwd)


@pytest.fixture
def fleet(tmp_path: Path) -> dict[str, Path]:
    """Primary on main plus: a clean/merged tree, a dirty tree, an ahead tree.
    The real scripts are committed so every worktree carries its own copy."""
    primary = tmp_path / "primary"
    (primary / "scripts" / "lib").mkdir(parents=True)
    _git(primary, "init", "-q", "-b", "main")
    shutil.copy2(REPO_ROOT / "scripts" / "inflight", primary / "scripts" / "inflight")
    (primary / "scripts" / "inflight").chmod(0o755)
    shutil.copy2(
        REPO_ROOT / "scripts" / "lib" / "inflight_cache.py",
        primary / "scripts" / "lib" / "inflight_cache.py",
    )
    (primary / "README.md").write_text("root\n", encoding="utf-8")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "initial")

    trees = {"primary": primary}
    for name in ("clean", "dirty", "ahead"):
        tree = tmp_path / name
        _git(primary, "worktree", "add", "-q", "-b", f"worktree-{name}", str(tree))
        trees[name] = tree
    (trees["dirty"] / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
    (trees["ahead"] / "feature.txt").write_text("work\n", encoding="utf-8")
    _git(trees["ahead"], "add", "-A")
    _git(trees["ahead"], "commit", "-q", "-m", "ahead work")
    return trees


def _inflight(tree: Path, *args: str, **env: str) -> str:
    return _run([str(tree / "scripts" / "inflight"), *args], tree, **env)


def _cache(fleet: dict[str, Path]) -> Path:
    common = _git(
        fleet["primary"], "rev-parse", "--path-format=absolute", "--git-common-dir"
    ).strip()
    return Path(common) / CACHE_NAME


def _shimmed(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """PATH with a git shim logging argv; returns (env extras, log path)."""
    shim = tmp_path / "shim"
    shim.mkdir()
    git_bin = shutil.which("git")
    assert git_bin is not None
    (shim / "git").write_text(
        f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$GIT_ARGV_LOG"\nexec "{git_bin}" "$@"\n',
        encoding="utf-8",
    )
    (shim / "git").chmod(0o755)
    log = tmp_path / "git-argv.log"
    return {
        "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}",
        "GIT_ARGV_LOG": str(log),
    }, log


def _status_calls(log: Path) -> int:
    if not log.exists():
        return 0
    return sum(
        1
        for ln in log.read_text(encoding="utf-8").splitlines()
        if " status " in f" {ln} "
    )


def test_cached_hook_line_equals_live(fleet: dict[str, Path]) -> None:
    live = _inflight(fleet["clean"], "--for-hook", INFLIGHT_NO_CACHE="1")
    cached = _inflight(fleet["clean"], "--for-hook")
    assert cached == live
    assert "4 trees in flight" in cached, cached
    assert _cache(fleet).exists()


def test_cached_json_equals_live_with_caller_as_self(fleet: dict[str, Path]) -> None:
    args = ("--json", "--skip-branch-probes")
    for caller in ("clean", "dirty", "ahead", "primary"):
        tree = fleet[caller]
        live = json.loads(_inflight(tree, *args, INFLIGHT_NO_CACHE="1"))
        cached = json.loads(_inflight(tree, *args))
        assert cached == live, f"caller {caller}"
    # the cache was written by the first caller; later callers were re-bucketed
    by_name = {w["name"]: w["bucket"] for w in cached["worktrees"]}
    assert by_name["main"] == "base"
    assert "self" not in by_name.values()  # primary caller: `base` wins over `self`
    ahead = json.loads(_inflight(fleet["ahead"], *args))
    selves = [w["name"] for w in ahead["worktrees"] if w["bucket"] == "self"]
    assert selves == ["ahead"]


def test_second_call_inside_ttl_scans_nothing(
    fleet: dict[str, Path], tmp_path: Path
) -> None:
    extra, log = _shimmed(tmp_path)
    _inflight(fleet["clean"], "--for-hook", **extra)
    first = _status_calls(log)
    assert first >= 4, "the first call must have scanned every tree"
    _inflight(fleet["dirty"], "--for-hook", **extra)
    _inflight(fleet["ahead"], "--json", "--skip-branch-probes", **extra)
    assert _status_calls(log) == first, "a call inside the TTL rescanned"


def test_one_scan_serves_both_shapes(fleet: dict[str, Path], tmp_path: Path) -> None:
    extra, log = _shimmed(tmp_path)
    _inflight(fleet["clean"], "--json", "--skip-branch-probes", **extra)
    first = _status_calls(log)
    _inflight(fleet["clean"], "--for-hook", **extra)
    assert _status_calls(log) == first


def test_ttl_zero_rescans(fleet: dict[str, Path], tmp_path: Path) -> None:
    extra, log = _shimmed(tmp_path)
    _inflight(fleet["clean"], "--for-hook", INFLIGHT_CACHE_TTL="0", **extra)
    first = _status_calls(log)
    _inflight(fleet["clean"], "--for-hook", INFLIGHT_CACHE_TTL="0", **extra)
    assert _status_calls(log) > first


def test_no_cache_env_is_live_and_leaves_no_cache(
    fleet: dict[str, Path], tmp_path: Path
) -> None:
    extra, log = _shimmed(tmp_path)
    for _ in range(2):
        _inflight(fleet["clean"], "--for-hook", INFLIGHT_NO_CACHE="1", **extra)
    assert _status_calls(log) >= 8
    assert not _cache(fleet).exists()


def test_stale_cache_is_replaced(fleet: dict[str, Path]) -> None:
    assert "1 dirty" in _inflight(fleet["clean"], "--for-hook")
    cache = _cache(fleet)
    # age the file past the TTL, then change the world: a second dirty tree
    old = cache.stat().st_mtime - 3600
    os.utime(cache, (old, old))
    (fleet["clean"] / "now-dirty.txt").write_text("x\n", encoding="utf-8")
    out = _inflight(fleet["clean"], "--for-hook")
    assert "2 dirty" in out, out


def test_plain_json_and_table_stay_live(fleet: dict[str, Path]) -> None:
    _inflight(fleet["clean"], "--json")
    _inflight(fleet["clean"])
    _inflight(fleet["clean"], "--all")
    assert not _cache(fleet).exists()


def test_falls_back_to_live_when_helper_cannot_scan(fleet: dict[str, Path]) -> None:
    """A broken helper must never cost a session its answer."""
    helper = fleet["primary"] / "scripts" / "lib" / "inflight_cache.py"
    helper.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    live = _inflight(fleet["primary"], "--for-hook", INFLIGHT_NO_CACHE="1")
    assert _inflight(fleet["primary"], "--for-hook") == live
    got = json.loads(_inflight(fleet["primary"], "--json", "--skip-branch-probes"))
    assert len(got["worktrees"]) == 4


def _helper_module():
    spec = importlib.util.spec_from_file_location(
        "inflight_cache", REPO_ROOT / "scripts" / "lib" / "inflight_cache.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_probe_failed_stub_counts_as_a_dirty_tree_not_a_branch() -> None:
    """A probe that died leaves a stub row with no path and dirty "?"; the live
    counts line counts it as a dirty tree, never as a worktree-less branch."""
    rows = [
        {"path": "/p", "dirty": "clean", "session": "—", "verdict": "—"},
        {"path": "", "dirty": "?", "session": "—", "verdict": "?"},
        {"path": "", "dirty": "—", "session": "—", "verdict": "↑2↓0"},
    ]
    line = _helper_module()._hook_line({"base": "main", "worktrees": rows})
    assert line.startswith("🌳 3 trees in flight (0 live, 1 dirty, 0 ahead of main")
    assert "1 worktree-less branch)" in line, line


def test_waiter_serves_stale_counts_when_the_lock_is_held(
    fleet: dict[str, Path],
) -> None:
    """A scan that is slow or hung must not leave other starts empty-handed:
    the counts line comes from the old cache, the JSON shape scans live."""
    fresh = _inflight(fleet["clean"], "--for-hook")
    cache = _cache(fleet)
    old = cache.stat().st_mtime - 3600
    os.utime(cache, (old, old))
    lock_fd = os.open(cache.with_name("precis-inflight-cache.lock"), os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        out = _inflight(fleet["clean"], "--for-hook", INFLIGHT_CACHE_LOCK_WAIT="0.5")
        assert out == fresh
        got = json.loads(
            _inflight(
                fleet["clean"],
                "--json",
                "--skip-branch-probes",
                INFLIGHT_CACHE_LOCK_WAIT="0.5",
            )
        )
        assert len(got["worktrees"]) == 4
    finally:
        os.close(lock_fd)


def test_memory_nudge_launches_one_lint_per_window(tmp_path: Path) -> None:
    """A burst of session starts launches the lint once (atomic claim), and a
    claim older than the window is released so a later start launches again."""
    hooks = tmp_path / "repo" / "scripts" / "hooks"
    hooks.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "hooks" / "memory-nudge.sh", hooks)
    count = tmp_path / "lint-runs"
    lint = tmp_path / "repo" / "scripts" / "memory-lint"
    lint.write_text(f"#!/bin/sh\necho x >> {count}\n", encoding="utf-8")
    lint.chmod(0o755)
    cache_home = tmp_path / "cache"
    nudge = hooks / "memory-nudge.sh"

    def start() -> None:
        _run(["bash", str(nudge)], tmp_path, XDG_CACHE_HOME=str(cache_home))

    def runs() -> int:
        time.sleep(1.0)  # the lint is a background process: let it settle
        return (
            len(count.read_text(encoding="utf-8").splitlines()) if count.exists() else 0
        )

    for _ in range(4):
        start()
    assert runs() == 1

    claim = cache_home / "precis" / "memory-lint.log.started.d"
    assert claim.is_dir()
    old = claim.stat().st_mtime - 3600
    os.utime(claim, (old, old))
    start()
    assert runs() == 2

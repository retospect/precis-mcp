#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""Shared, single-flight cache for ``scripts/inflight``'s hook-mode scans (gr541365).

Every SessionStart ran the fleet scan (one ``git status`` per worktree) from
seven hooks, and dozens of sessions start at once: load average 90, hooks
taking minutes. This front serves the two hook-mode shapes from ONE scan:

    inflight --for-hook                   -> the counts line, derived here
    inflight --json --skip-branch-probes  -> the JSON, `self` applied here

Both come from a single cached ``--json --skip-branch-probes`` scan taken with
``INFLIGHT_NEUTRAL=1`` (no caller-specific `self` bucket). Callers are told
apart on read, so the cache is shared by every worktree of the repo.

Single flight: scans serialize on an ``flock``; a waiter finds the leader's
fresh result when it gets the lock and does not rescan. The cache is valid for
``INFLIGHT_CACHE_TTL`` seconds (default 45) after the scan FINISHED.

Not cached, always live: the human table, ``--all``, a plain ``--json`` (full
branch verdicts), and anything with ``INFLIGHT_NO_CACHE=1`` — the reapers'
post-grace-period recheck sets it, because a destructive decision needs a
fresh read. A reaper that tolerates less staleness than the default passes a
shorter ``INFLIGHT_CACHE_TTL``.

Any failure here falls back to running the real script uncached: a session
start is never blocked and never gets less than it did before. A waiter that
cannot get the lock within ``LOCK_WAIT`` (a scan is hanging, or very slow)
stops waiting: the counts line is served from whatever cache exists, any age —
it is only a number — while the JSON shape scans live, as it did before.

Usage (called by scripts/inflight, which resolves its own path):
    inflight_cache.py <hook|json> <path-to-inflight>
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_TTL = 45.0
LOCK_WAIT = 45.0  # inside the hook budget; past it a waiter stops queueing
SCAN_TIMEOUT = 300.0  # a hung scan must not hold the lock (and every waiter) forever
GIT_TIMEOUT = 60.0  # one rev-parse; a wedged git must not wedge a session start
SCAN_ARGS = ["--json", "--skip-branch-probes"]


def _git(*args: str) -> str:
    cp = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=GIT_TIMEOUT,
    )
    return cp.stdout.strip() if cp.returncode == 0 else ""


def _ttl() -> float:
    try:
        return max(0.0, float(os.environ.get("INFLIGHT_CACHE_TTL", DEFAULT_TTL)))
    except ValueError:
        return DEFAULT_TTL


def _read_fresh(cache: Path, ttl: float | None) -> str | None:
    """The cached scan text when it finished within ``ttl`` seconds (any age
    when ``ttl`` is None)."""
    try:
        age = time.time() - cache.stat().st_mtime
        if ttl is None or 0 <= age < ttl:
            text = cache.read_text(encoding="utf-8")
            json.loads(text)
            return text
    except (OSError, ValueError):
        pass
    return None


def _scan(inflight: str) -> str | None:
    env = dict(os.environ, INFLIGHT_NO_CACHE="1", INFLIGHT_NEUTRAL="1")
    try:
        cp = subprocess.run(
            ["bash", inflight, *SCAN_ARGS],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
            timeout=SCAN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    if cp.returncode != 0 or not cp.stdout.strip():
        return None
    try:
        data = json.loads(cp.stdout)
    except ValueError:
        return None
    return cp.stdout if isinstance(data, dict) and "worktrees" in data else None


def _store(cache: Path, text: str) -> None:
    tmp = cache.with_name(f"{cache.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, cache)
    except OSError:
        tmp.unlink(missing_ok=True)


def _locked_scan(cache: Path, lock: Path, inflight: str, ttl: float) -> str | None:
    """Fresh cache, or the scan this process ran under the lock, or None."""
    try:
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None
    try:
        try:
            wait = float(os.environ.get("INFLIGHT_CACHE_LOCK_WAIT", LOCK_WAIT))
        except ValueError:
            wait = LOCK_WAIT
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.25)
        text = _read_fresh(cache, ttl)  # the leader we waited on just wrote it
        if text is None:
            text = _scan(inflight)
            if text is not None:
                _store(cache, text)
        return text
    finally:
        os.close(fd)  # drops the flock


def _apply_self(data: dict, here: str) -> dict:
    """The caller's own tree is bucket `self` (base still wins), as a live scan says."""
    if not here:
        return data
    for wt in data.get("worktrees", []):
        path = wt.get("path")
        if path and wt.get("bucket") != "base" and os.path.realpath(path) == here:
            wt["bucket"] = "self"
    return data


def _hook_line(data: dict) -> str:
    """The `--for-hook` counts line, counted exactly as the live path counts it."""
    rows = data.get("worktrees", [])
    # A row with no path is a branchless branch — except a probe-failed stub,
    # which also has none but carries dirty "?" and counts as a (dirty) tree.
    trees = [wt for wt in rows if wt.get("path") or wt.get("dirty") == "?"]
    branchless = len(rows) - len(trees)
    live = sum(1 for wt in trees if str(wt.get("session", "")).startswith("live#"))
    if any(str(wt.get("session", "")).startswith("live-task#") for wt in trees):
        live += 1
    dirty = sum(1 for wt in trees if wt.get("dirty") != "clean")
    base = data.get("base", "main")
    ahead = sum(1 for wt in trees if str(wt.get("verdict", "")).startswith("↑"))
    extra = ""
    if branchless:
        word = "branch" if branchless == 1 else "branches"
        extra = f" · {branchless} worktree-less {word}"
    return (
        f"🌳 {len(rows)} trees in flight ({live} live, {dirty} dirty, "
        f"{ahead} ahead of {base}{extra}) — scripts/inflight for the overlap table"
    )


def _fallback(inflight: str, argv: list[str]) -> int:
    env = dict(os.environ, INFLIGHT_NO_CACHE="1")
    try:
        return subprocess.run(
            ["bash", inflight, *argv], env=env, check=False, timeout=SCAN_TIMEOUT
        ).returncode
    except (OSError, subprocess.SubprocessError):
        # `--for-hook` always exits 0; the json shape's callers read non-zero
        # as "inflight cannot answer".
        return 0 if "--for-hook" in argv else 1


def main(argv: list[str]) -> int:
    mode, inflight = argv[1], argv[2]
    orig = ["--for-hook"] if mode == "hook" else list(SCAN_ARGS)
    try:
        common = _git("rev-parse", "--path-format=absolute", "--git-common-dir")
        if not common:
            return _fallback(inflight, orig)
        cache = Path(common) / "precis-inflight-cache.json"
        lock = Path(common) / "precis-inflight-cache.lock"
        ttl = _ttl()
        text = _read_fresh(cache, ttl) or _locked_scan(cache, lock, inflight, ttl)
        if text is None and mode == "hook":
            text = _read_fresh(cache, None)  # a stale count beats a second scan
        if text is None:
            return _fallback(inflight, orig)
        data = json.loads(text)
        if mode == "hook":
            print(_hook_line(data))
        else:
            top = _git("rev-parse", "--show-toplevel")
            here = os.path.realpath(top) if top else ""
            print(json.dumps(_apply_self(data, here), indent=2))
        return 0
    except Exception:  # a hook cache must never break a session start
        return _fallback(inflight, orig)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

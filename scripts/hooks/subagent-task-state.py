#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""Track active Claude subagent ids in the shared git directory.

Marker shape (gr513174): {"agent_id", "pid", "cwd", "started_at"} — the
durable session pid, the session's worktree, and a UTC start time. Markers
that predate this shape, name a dead pid, or outlive _MAX_MARKER_AGE are
stale: readers ignore and unlink them, so a task whose SubagentStop never
fired (killed agent, crashed session) cannot freeze the reaper again.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_STATE_DIR = "precis-agent-tasks"
_LOCK_PID = re.compile(r"pid\s+([0-9]+)")
# Upper bound on a marker whose pid stays live (or unreadable) but whose
# SubagentStop never fired: six hours of protection, then the tree reaps.
_MAX_MARKER_AGE = timedelta(hours=6)


def _common_dir(path: Path) -> Path | None:
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=10,
    )
    if proc.returncode:
        return None
    return Path(proc.stdout.strip())


def _ps_field(pid: int, field: str) -> str:
    proc = subprocess.run(
        ["ps", "-o", f"{field}=", "-p", str(pid)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=5,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _session_pid() -> int | None:
    """The durable session pid behind this hook call. Same discriminators and
    25-hop bound as find_session_pid in scripts/lib/session-lock.sh — the
    walk exists because the hook's own parent can be a transient
    snapshot-shell wrapper that dies as soon as the hook returns, which would
    make a fresh marker read stale.
    """
    pid = os.getppid()
    for _ in range(25):
        if pid <= 1:
            return None
        base = _ps_field(pid, "comm").rsplit("/", 1)[-1]
        if base == "claude":
            return pid
        if base == "node" and "@anthropic-ai/claude-code" in _ps_field(pid, "args"):
            return pid
        try:
            pid = int(_ps_field(pid, "ppid"))
        except ValueError:
            return None
    return None


def _payload_task_id() -> str:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return ""
    task_id = payload.get("agent_id", "") if isinstance(payload, dict) else ""
    return task_id if isinstance(task_id, str) and _TASK_ID.fullmatch(task_id) else ""


def _record(action: str, root: Path) -> int:
    task_id = _payload_task_id()
    common = _common_dir(root)
    if not task_id or common is None:
        return 0
    state = common / _STATE_DIR
    marker = state / f"{task_id}.json"
    if action == "stop":
        marker.unlink(missing_ok=True)
        return 0
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    data: dict[str, object] = {
        "agent_id": task_id,
        "cwd": str(root.resolve()),
        "started_at": datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    if (pid := _session_pid()) is not None:
        data["pid"] = pid
    temporary = state / f".{task_id}.{os.getpid()}.tmp"
    try:
        temporary.write_text(json.dumps(data), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)
    return 0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass  # EPERM etc. — exists, owned by someone else
    return True


def _started_at(data: dict) -> datetime | None:
    raw = data.get("started_at")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _live_markers(state: Path) -> list[dict] | None:
    """Markers still describing a live task; everything else is unlinked.

    Stale = corrupt file, a task_id that doesn't parse, an old
    {"agent_id"}-only marker (no cwd/started_at), a dead recorded pid, or an
    age past _MAX_MARKER_AGE. None means the state dir could not be read —
    callers fail closed on that, never on a stale marker.
    """
    try:
        markers = sorted(state.glob("*.json"))
    except OSError:
        return None
    now = datetime.now(UTC)
    live = []
    for marker in markers:
        stale = False
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            stale = True
        else:
            task_id = data.get("agent_id", "") if isinstance(data, dict) else ""
            cwd = data.get("cwd") if isinstance(data, dict) else None
            started = _started_at(data) if isinstance(data, dict) else None
            pid = data.get("pid") if isinstance(data, dict) else None
            stale = (
                not isinstance(task_id, str)
                or not _TASK_ID.fullmatch(task_id)
                or not isinstance(cwd, str)
                or started is None
                or now - started > _MAX_MARKER_AGE
                or (isinstance(pid, int) and not _pid_alive(pid))
            )
        if stale:
            marker.unlink(missing_ok=True)
            continue
        live.append({"agent_id": data["agent_id"], "cwd": data["cwd"], "pid": pid})
    return live


def _tree_lock_pid(path: Path) -> int | None:
    """The pid <path>'s `git worktree lock` reason names — how the harness
    records which session owns an agent child tree ("claude agent <id> (pid
    <N>)"). Same `pid <N>` parse as scripts/inflight's session_field."""
    proc = subprocess.run(
        ["git", "-C", str(path), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=10,
    )
    if proc.returncode:
        return None
    real = os.path.realpath(path)
    for block in proc.stdout.split("\n\n"):
        lines = block.splitlines()
        if not lines or not lines[0].startswith("worktree "):
            continue
        if os.path.realpath(lines[0][len("worktree ") :]) != real:
            continue
        for ln in lines[1:]:
            if ln.startswith("locked"):
                m = _LOCK_PID.search(ln)
                return int(m.group(1)) if m else None
        return None
    return None


def _matches(marker: dict, real: str, lock_pid: int | None) -> bool:
    """The asked-about tree only: the marker's own worktree, or a tree the
    task's session holds (a worktree-isolated agent child is locked with the
    parent session's pid — gr513174 cause 1 was ignoring this scope)."""
    if os.path.realpath(marker["cwd"]) == real:
        return True
    pid = marker["pid"]
    return lock_pid is not None and isinstance(pid, int) and pid == lock_pid


def _check(path: Path) -> int:
    common = _common_dir(path)
    if common is None:
        return 2
    live = _live_markers(common / _STATE_DIR)
    if live is None:
        return 2
    real = os.path.realpath(path)
    lock_pid = (
        _tree_lock_pid(path) if any(isinstance(m["pid"], int) for m in live) else None
    )
    for marker in live:
        if _matches(marker, real, lock_pid):
            print(marker["agent_id"])
            return 0
    return 1


def _list(path: Path) -> int:
    """All live markers as `agent_id<TAB>cwd<TAB>pid` lines (pid may be
    empty) so a many-tree reader like scripts/inflight pays one call total
    and matches per row."""
    common = _common_dir(path)
    if common is None:
        return 2
    live = _live_markers(common / _STATE_DIR)
    if live is None:
        return 2
    for marker in live:
        pid = marker["pid"]
        print(
            f"{marker['agent_id']}\t{marker['cwd']}\t{pid if isinstance(pid, int) else ''}"
        )
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] in {"start", "stop"}:
        return _record(argv[1], Path(argv[2]))
    if len(argv) == 3 and argv[1] == "check":
        return _check(Path(argv[2]))
    if argv[1:] == ["list"]:
        return _list(Path.cwd())
    if len(argv) == 3 and argv[1] == "list":
        return _list(Path(argv[2]))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

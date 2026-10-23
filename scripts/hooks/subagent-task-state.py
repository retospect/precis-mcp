#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""Track active Claude subagent ids in the shared git directory."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_STATE_DIR = "precis-agent-tasks"


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
    )
    if proc.returncode:
        return None
    return Path(proc.stdout.strip())


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
    temporary = state / f".{task_id}.{os.getpid()}.tmp"
    try:
        temporary.write_text(
            json.dumps({"agent_id": task_id}),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)
    return 0


def _check(path: Path) -> int:
    common = _common_dir(path)
    if common is None:
        return 2
    state = common / _STATE_DIR
    try:
        markers = sorted(state.glob("*.json"))
    except OSError:
        return 2
    for marker in markers:
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print("unknown")
            return 0
        task_id = data.get("agent_id", "") if isinstance(data, dict) else ""
        if isinstance(task_id, str) and _TASK_ID.fullmatch(task_id):
            print(task_id)
            return 0
        print("unknown")
        return 0
    return 1


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] in {"start", "stop"}:
        return _record(argv[1], Path(argv[2]))
    if len(argv) == 3 and argv[1] == "check":
        return _check(Path(argv[2]))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""Render immutable compute-only runtime checks without workload/credential imports.

The checker is copied as standalone Python3.6-compatible source because early
failure can precede the target interpreter/venv. It emits bounded neutral facts;
the workload's separately hashed producer owns its persisted preflight envelope.
No dynamic module lookup, download, environment installation or scheduler submit.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any

from .ssh import RemoteError


def runtime_artifacts(plan: dict[str, Any]) -> dict[str, bytes]:
    """Freeze expected facts; missing identities/budgets cannot produce artifacts."""
    try:
        if set(plan) != {
            "schema",
            "resources",
            "python",
            "baseline_python",
            "uv",
            "platform",
            "storage",
            "module_policy",
            "min_remaining_seconds",
        }:
            raise ValueError
        if plan["schema"] != "remote.runtime.v1" or plan["module_policy"] != "none":
            raise ValueError
        resources = plan["resources"]
        if set(resources) != {
            "account",
            "partition",
            "qos",
            "nodes",
            "cpus",
            "gpus",
            "memory_mb",
            "wall_seconds",
        }:
            raise ValueError
        for name in ("account", "partition", "qos"):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", resources[name]):
                raise ValueError
        for name in ("nodes", "cpus", "gpus", "memory_mb", "wall_seconds"):
            if type(resources[name]) is not int or resources[name] < 1:
                raise ValueError
        if (
            resources["nodes"] != 1
            or resources["gpus"] > 4
            or resources["wall_seconds"] > 600
        ):
            raise ValueError
        if (
            type(plan["min_remaining_seconds"]) is not int
            or not 1 <= plan["min_remaining_seconds"] <= resources["wall_seconds"]
        ):
            raise ValueError
        for name in ("baseline_python", "python", "uv"):
            obj = plan[name]
            if set(obj) != {"path", "version", "sha256"} or not re.fullmatch(
                r"[0-9]+\.[0-9]+\.[0-9]+", obj["version"]
            ):
                raise ValueError
            if not re.fullmatch(r"[a-f0-9]{64}", obj["sha256"]):
                raise ValueError
            path = PurePosixPath(obj["path"])
            if (
                str(path) != obj["path"]
                or ".." in path.parts
                or not re.fullmatch(r"[A-Za-z0-9_./+-]+", obj["path"])
            ):
                raise ValueError
            if (name in {"baseline_python", "python"} and not path.is_absolute()) or (
                name == "uv" and (path.is_absolute() or len(path.parts) < 2)
            ):
                raise ValueError
        plat = plan["platform"]
        if (
            set(plat) != {"system", "machine", "glibc_min", "driver_min", "gpu_name"}
            or plat["system"] != "Linux"
            or plat["machine"] != "x86_64"
        ):
            raise ValueError
        if not re.fullmatch(r"[0-9]+\.[0-9]+", plat["glibc_min"]) or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+", plat["driver_min"]
        ):
            raise ValueError
        if not re.fullmatch(r"[A-Za-z0-9_-]+", plat["gpu_name"]):
            raise ValueError
        storage = plan["storage"]
        if set(storage) != {
            "root",
            "compute_peak_bytes",
            "compute_peak_inodes",
            "reserve_bytes",
            "reserve_inodes",
        }:
            raise ValueError
        root = PurePosixPath(storage["root"])
        if (
            not root.is_absolute()
            or str(root) != storage["root"]
            or ".." in root.parts
            or not re.fullmatch(r"/[A-Za-z0-9_./+-]+", storage["root"])
        ):
            raise ValueError
        for key in (
            "compute_peak_bytes",
            "compute_peak_inodes",
            "reserve_bytes",
            "reserve_inodes",
        ):
            if type(storage[key]) is not int or storage[key] < 1:
                raise ValueError
    except (KeyError, TypeError, ValueError):
        raise RemoteError("runtime_plan_invalid") from None
    # The caller inventories/hashes every file, including the workload report producer.
    config = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(config) > 8192:
        raise RemoteError("runtime_plan_invalid")
    script = (
        "#!/bin/sh\n"
        "# Sourced inside the same allocation; workload producer persists these neutral facts.\n"
        "REMOTE_RUNTIME_CHECK_STATUS=70\n"
        "REMOTE_RUNTIME_OBSERVATIONS= REMOTE_RUNTIME_PYTHON= REMOTE_RUNTIME_UV=\n"
        "export REMOTE_RUNTIME_CHECK_STATUS REMOTE_RUNTIME_OBSERVATIONS REMOTE_RUNTIME_PYTHON REMOTE_RUNTIME_UV\n"
        "unset TMPDIR PYTHONDONTWRITEBYTECODE\n"
        "umask 077\n"
        "unset PYTHONPATH PYTHONHOME LD_PRELOAD LD_LIBRARY_PATH BASH_ENV ENV SLURM_TIME_FORMAT\n"
        "export PATH=/usr/bin:/bin\n"
        "export TZ=UTC\n"
        'test -n "${SLURM_JOB_ID:-}" || return 70\n'
        'test -n "${pilot_stage:-}" && test -n "${pilot_run:-}" || return 70\n'
        f"printf '%s  %s\\n' {shlex.quote(plan['baseline_python']['sha256'])} {shlex.quote(plan['baseline_python']['path'])} "
        "| /usr/bin/sha256sum --check --status 2>/dev/null || return 70\n"
        f"if REMOTE_RUNTIME_OBSERVATIONS=$({shlex.quote(plan['baseline_python']['path'])} "
        '"$pilot_stage/runtime-check.py" "$pilot_stage/runtime-plan.json" "$pilot_stage" "$pilot_run" 2>/dev/null) '
        "; then\n"
        "  REMOTE_RUNTIME_CHECK_STATUS=0\n"
        "else\n"
        "  REMOTE_RUNTIME_CHECK_STATUS=$?\n"
        '  return "$REMOTE_RUNTIME_CHECK_STATUS"\n'
        "fi\n"
        f"REMOTE_RUNTIME_PYTHON={shlex.quote(plan['python']['path'])}\n"
        'REMOTE_RUNTIME_UV="$pilot_run/.runtime-tools/uv"\n'
        "export REMOTE_RUNTIME_PYTHON REMOTE_RUNTIME_UV\n"
        'TMPDIR="$pilot_run/.runtime-temp"\n'
        "export TMPDIR PYTHONDONTWRITEBYTECODE=1\n"
        "# Observations survive for the early producer/trap; failure stops installation.\n"
        'return "$REMOTE_RUNTIME_CHECK_STATUS"\n'
    ).encode()
    return {
        "runtime-plan.json": config,
        "runtime-check.py": Path(__file__).with_name("_runtime_check.py").read_bytes(),
        "runtime-bootstrap.sh": script,
    }

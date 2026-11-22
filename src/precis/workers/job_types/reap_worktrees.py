"""Run the local worktree reaper once and report its before/after inventory.

The script owns eligibility and its grace re-check. The job enables the
explicit run even in worker environments that suppress autoreap, and
disables remote ref cleanup to keep the run local to the checkout.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, TypedDict

from precis.workers import admission
from precis.workers.job_types import release_cut as _rc
from precis.workers.job_types import run_slice as _rs

NAME = "reap_worktrees"
PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_node": {"type": "string"},
        "resources": {
            "type": "object",
            "properties": {"wall_seconds": {"type": "integer"}},
        },
    },
    "additionalProperties": False,
}
COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"ssh_node"})
REQUIRES: frozenset[str] = frozenset()
DESCRIPTION = (
    "Reap eligible local worktrees and branches on demand and report their buckets."
)


_INVENTORY_TIMEOUT_S = 120


class _Entry(TypedDict):
    name: str
    path: str
    branch: str
    bucket: str
    worktree: str | None


def wall_seconds() -> float:
    return 20 * 60.0


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    del store, gripe_id, params
    return _rc.factory_refusal(NAME)


def _read_inflight(ctx: Any, root: Path) -> list[_Entry]:
    # The full inventory fans out git probes across the checkout's worktrees.
    footprint = admission.Footprint(
        cpu_milli=1000, mem_mb=256, wall_s=_INVENTORY_TIMEOUT_S
    )
    with admission.hold(
        ctx.store, admission.local_host(), footprint, f"{NAME}:{ctx.ref_id}"
    ):
        result = subprocess.run(
            [str(root / "scripts" / "inflight"), "--json"],
            cwd=root,
            capture_output=True,
            encoding="utf-8",
            check=True,
            timeout=_INVENTORY_TIMEOUT_S,
        )
    entries = []
    for row in json.loads(result.stdout)["worktrees"]:
        if row.get("purpose") == "(probe failed)":
            raise ValueError(f"incomplete inventory: probe failed for {row['name']}")
        entries.append(
            _Entry(
                name=row["name"],
                path=row["path"],
                branch=row["branch"],
                bucket=row["bucket"],
                worktree=row["worktree"],
            )
        )
    return entries


def _inventory(ctx: Any, root: Path) -> list[_Entry] | None:
    try:
        return _read_inflight(ctx, root)
    except (
        admission.Refused,
        OSError,
        subprocess.SubprocessError,
        ValueError,
        TypeError,
        KeyError,
    ) as exc:
        ctx.record_failure(f"{NAME}: scripts/inflight --json failed: {exc}")
        return None


def submit(ctx: Any, spec: Any) -> dict[str, Any] | None:
    del spec
    if (reason := _rc.factory_refusal(NAME)) is not None:
        ctx.record_failure(reason)
        return None
    snapshot = _inventory(ctx, _rs.slice_root())
    if snapshot is None:
        return None
    handle = _rc.launch_script(
        ctx,
        NAME,
        [],
        script="reap-worktrees",
        env={"PRECIS_NO_AUTOREAP": "", "PRECIS_NO_CI_REF_REAP": "1"},
    )
    if handle is not None:
        handle["snapshot"] = snapshot
    return handle


def poll(ctx: Any, handle: Any) -> bool:
    done = _rc.finish_run(ctx, handle, NAME)
    if done is None:
        return not isinstance(handle, dict)
    code, log = done
    if code != 0:
        _rc.fail_with_tail(ctx, f"{NAME}: scripts/reap-worktrees exit {code}", log)
        return True
    current = _inventory(ctx, Path(handle["root"]))
    if current is None:
        return True

    paths = {row["path"] for row in current if row["worktree"] is not None}
    # A previously unchecked branch may now be attached to a worktree.
    branches = {row["branch"] for row in current}
    removed = [
        {"name": row["name"], "bucket": row["bucket"]}
        for row in handle["snapshot"]
        if row["bucket"] != "base"
        and (
            row["path"] not in paths
            if row["worktree"] is not None
            else row["branch"] not in branches
        )
    ]
    kept = [
        {"name": row["name"], "bucket": row["bucket"]}
        for row in current
        if row["bucket"] != "base"
    ]
    ctx.set_meta(reap_worktrees={"removed": removed, "kept": kept})
    lines = [
        f"{row['name']}: {outcome} ({row['bucket']})"
        for outcome, rows in (("removed", removed), ("kept", kept))
        for row in rows
    ]
    ctx.append_chunk(
        "job_summary", "\n".join(lines) or "No non-base worktrees or branches."
    )
    ctx.set_status("succeeded")
    return True


kill = _rc.kill

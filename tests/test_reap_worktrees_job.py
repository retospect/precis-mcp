"""Exercise the on-demand worktree reaper without a checkout or grace sleep."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

from precis.store import Store
from precis.workers.job_types import get_job_type, known_job_types, reap_worktrees
from precis.workers.registry import SERVICES_BY_NAME
from tests.test_ci_rerun import Ctx


def _row(name: str, bucket: str, *, checked_out: bool = True) -> dict[str, Any]:
    path = f"/checkout/{name}" if checked_out else ""
    return {
        "name": name,
        "path": path,
        "branch": name,
        "bucket": bucket,
        "worktree": path or None,
    }


@pytest.fixture
def ctx(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Ctx:
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    monkeypatch.setattr(reap_worktrees._rs, "slice_root", lambda: tmp_path)
    return Ctx(cast(Store, None), {})


def test_registered() -> None:
    spec = get_job_type("reap_worktrees")
    assert spec is not None
    assert spec.compatible_executors == frozenset({"ssh_node"})
    assert spec.submit is not None and spec.poll is not None
    assert "reap_worktrees" in known_job_types()
    assert "repo_checkout" in SERVICES_BY_NAME["reap_worktrees"].placement


def test_reports_removed_and_kept(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    base = _row("main", "base")
    snapshot = [
        base,
        _row("wt-safe", "safe_remove"),
        _row("wt-work", "has_unmerged_work"),
        _row("old-branch", "safe_remove", checked_out=False),
        _row("moved-branch", "stranded", checked_out=False),
    ]
    current = [
        base,
        _row("wt-work", "has_unmerged_work"),
        _row("moved-branch", "has_unmerged_work"),
    ]
    read = Mock(side_effect=[snapshot, current])
    monkeypatch.setattr(reap_worktrees, "_read_inflight", read)
    root = reap_worktrees._rs.slice_root()
    launch = Mock(return_value={"root": str(root)})
    monkeypatch.setattr(reap_worktrees._rc, "launch_script", launch)
    monkeypatch.setattr(reap_worktrees._rc, "finish_run", Mock(return_value=(0, "")))

    handle = reap_worktrees.submit(ctx, None)
    read.assert_called_once_with(ctx, root)
    assert handle is not None and handle["snapshot"] == snapshot
    launch.assert_called_once_with(
        ctx,
        "reap_worktrees",
        [],
        script="reap-worktrees",
        env={"PRECIS_NO_AUTOREAP": "", "PRECIS_NO_CI_REF_REAP": "1"},
    )
    assert reap_worktrees.poll(ctx, handle)
    assert read.call_count == 2
    assert ctx.stamped["reap_worktrees"] == {
        "removed": [
            {"name": "wt-safe", "bucket": "safe_remove"},
            {"name": "old-branch", "bucket": "safe_remove"},
        ],
        "kept": [
            {"name": "wt-work", "bucket": "has_unmerged_work"},
            {"name": "moved-branch", "bucket": "has_unmerged_work"},
        ],
    }
    assert ctx.chunks == [
        (
            "job_summary",
            "wt-safe: removed (safe_remove)\n"
            "old-branch: removed (safe_remove)\n"
            "wt-work: kept (has_unmerged_work)\n"
            "moved-branch: kept (has_unmerged_work)",
        )
    ]
    assert ctx.status == "succeeded"


@pytest.mark.parametrize("code", [1, None])
def test_exit_failure(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch, code: int | None
) -> None:
    monkeypatch.setattr(
        reap_worktrees._rc, "finish_run", Mock(return_value=(code, "log"))
    )
    read = Mock()
    monkeypatch.setattr(reap_worktrees, "_read_inflight", read)
    assert reap_worktrees.poll(ctx, {})
    read.assert_not_called()
    assert f"scripts/reap-worktrees exit {code}" in ctx.failures[0]
    assert ctx.stamped == {}
    assert ctx.status is None


@pytest.mark.parametrize("phase", ["submit", "poll"])
def test_inflight_failure(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    read = Mock(side_effect=subprocess.CalledProcessError(2, "inflight"))
    launch = Mock()
    monkeypatch.setattr(reap_worktrees, "_read_inflight", read)
    monkeypatch.setattr(reap_worktrees._rc, "launch_script", launch)
    monkeypatch.setattr(reap_worktrees._rc, "finish_run", Mock(return_value=(0, "")))

    if phase == "submit":
        assert reap_worktrees.submit(ctx, None) is None
    else:
        assert reap_worktrees.poll(ctx, {"root": str(reap_worktrees._rs.slice_root())})
    launch.assert_not_called()
    assert "scripts/inflight --json failed" in ctx.failures[0]
    assert ctx.stamped == {}
    assert ctx.chunks == []


def test_factory_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    refusal = reap_worktrees.validate_submit(None, params={})
    assert refusal is not None and "PRECIS_FACTORY=off" in refusal

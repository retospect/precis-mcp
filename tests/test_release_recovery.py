"""Release recovery preserves ownership and confirms transient CI failures."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.test_release_script import _load_release

HEAD = "1" * 40


def _args(mod: ModuleType, *, cut_only: bool = False) -> Any:
    return mod.argparse.Namespace(
        deploy_only="", sha=HEAD, adopt=False, dry_run=False, cut_only=cut_only
    )


def _live_setup(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, deployment: dict[str, Any]
) -> dict[str, Any]:
    rel = {
        "branch": "release/r1",
        "base": HEAD,
        "state": "deploying",
        "deployment": {"sha": HEAD, **deployment},
    }
    monkeypatch.setattr(mod, "warn_hook_drift", lambda: None)
    monkeypatch.setattr(mod, "fetch", lambda: None)
    monkeypatch.setattr(mod, "rev", lambda name: HEAD)
    monkeypatch.setattr(mod, "git", lambda *args: HEAD)
    monkeypatch.setattr(mod, "round_state", lambda: {"open": True, "releases": [rel]})
    monkeypatch.setattr(mod, "remote", lambda ref: HEAD)
    monkeypatch.setattr(mod, "ancestor", lambda *args: True)
    return rel


@pytest.mark.parametrize("cut_only", [False, True])
@pytest.mark.parametrize("owner", [False, True])
def test_already_live_finish_requires_owner_and_not_cut_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: bool, cut_only: bool
) -> None:
    mod = _load_release(tmp_path, monkeypatch)
    _live_setup(mod, monkeypatch, {})
    if owner:
        mod.write_owner("release/r1", HEAD, "t")
    calls: list[tuple[str, bool]] = []

    def finish(args: object, sha: str, *, finish_pointer: bool = False) -> int:
        calls.append((sha, finish_pointer))
        return 3

    monkeypatch.setattr(mod, "deploy_and_close", finish)
    expected = owner and not cut_only
    assert mod.run(_args(mod, cut_only=cut_only)) == (3 if expected else 0)
    assert calls == ([(HEAD, True)] if expected else [])


@pytest.mark.parametrize(
    "deployment",
    [{}, {"converged": True}, {"rollout_at": "t"}],
    ids=["no-receipt", "converged", "confirmation-pending"],
)
def test_already_live_sibling_never_takes_busy_lifecycle_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    deployment: dict[str, Any],
) -> None:
    mod = _load_release(tmp_path, monkeypatch)
    _live_setup(mod, monkeypatch, deployment)
    monkeypatch.setattr(
        mod, "rnd", lambda *args, **kwargs: pytest.fail("busy lifecycle lock taken")
    )
    for name in ("deploy_and_close", "warn_post_migration", "record_prod_head"):
        monkeypatch.setattr(
            mod, name, lambda *a, **k: pytest.fail("deploy side effect")
        )
    assert mod.run(_args(mod)) == 0
    output = capsys.readouterr()
    assert f"already live: {HEAD[:9]} is in origin/prod" in output.out
    assert "deploy failed" not in output.err


def test_owner_without_receipt_preserves_finish_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load_release(tmp_path, monkeypatch)
    _live_setup(mod, monkeypatch, {})
    mod.write_owner("release/r1", HEAD, "t")
    calls: list[tuple[str, ...]] = []

    def refuse(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        print("receipt missing: verify the served clone")
        return subprocess.CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(mod, "rnd", refuse)
    assert mod.run(_args(mod)) == 1
    output = capsys.readouterr()
    assert calls == [("deploy", "--finish-pointer")]
    assert "receipt missing: verify the served clone" in output.out
    assert "next:" in output.err
    assert "deploy failed" not in output.err


@pytest.mark.parametrize(
    ("branch", "base", "preserved"),
    [
        ("release/r1", HEAD, False),
        ("release/r2", HEAD, True),
        ("release/r1", "2" * 40, True),
    ],
)
@pytest.mark.parametrize("close_rc", [0, 1])
def test_finished_release_clears_only_its_own_marker_after_successful_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    base: str,
    preserved: bool,
    close_rc: int,
) -> None:
    mod = _load_release(tmp_path, monkeypatch)
    rel = {"branch": "release/r1", "base": HEAD, "state": "deploying"}
    before = {"open": True, "releases": [rel]}
    after = {"open": True, "deployed": rel, "releases": []}
    states = iter([before, after, after])
    monkeypatch.setattr(mod, "round_state", lambda: next(states))
    monkeypatch.setattr(mod, "rev", lambda name: HEAD)
    monkeypatch.setattr(mod, "warn_post_migration", lambda: None)
    monkeypatch.setattr(mod, "record_prod_head", lambda *a, **k: "recorded")
    monkeypatch.setattr(mod, "validate_gripes", lambda: None)
    monkeypatch.setattr(
        mod,
        "rnd",
        lambda *a, **k: subprocess.CompletedProcess(
            a, close_rc if a == ("close",) else 0, "", ""
        ),
    )
    mod.write_owner(branch, base, "t")
    marker = mod.marker_path().read_text(encoding="utf-8")
    assert mod.deploy_and_close(_args(mod), HEAD, finish_pointer=True) == 0
    assert mod.marker_path().exists() is (preserved or bool(close_rc))
    if preserved or close_rc:
        assert mod.marker_path().read_text(encoding="utf-8") == marker


@pytest.mark.parametrize("owner", [False, True])
def test_closing_retry_requires_owner_and_keeps_marker_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: bool
) -> None:
    mod = _load_release(tmp_path, monkeypatch)
    _live_setup(mod, monkeypatch, {})
    monkeypatch.setattr(
        mod,
        "round_state",
        lambda: {
            "open": True,
            "deployed": {"branch": "release/r1", "base": HEAD},
            "releases": [],
        },
    )
    if owner:
        mod.write_owner("release/r1", HEAD, "t")
    calls: list[tuple[str, ...]] = []

    def close(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "", "close unavailable")

    monkeypatch.setattr(mod, "rnd", close)
    assert mod.run(_args(mod)) == 0
    assert calls == ([("close",)] if owner else [])
    assert mod.marker_path().exists() is owner


@pytest.mark.parametrize("queued_gate", [False, True])
def test_terminal_failed_workflow_only_counts_gate_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queued_gate: bool
) -> None:
    mod = _load_release(tmp_path, monkeypatch)
    jobs = [
        {"name": "lint", "status": "completed", "conclusion": "success"},
        {"name": "publish", "status": "completed", "conclusion": "failure"},
    ]
    if queued_gate:
        jobs.append({"name": "test-linux (2)", "status": "queued", "conclusion": ""})
    workflow = {"status": "completed", "conclusion": "failure", "jobs": jobs}
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, json.dumps(workflow), ""
        ),
    )
    result = mod.completed_run_red("4242")
    if queued_gate:
        assert isinstance(result, dict)
        assert result["jobs"] == ["test-linux (2)"]
        assert any("test-linux (2)" in line for line in result["diagnostics"])
    else:
        assert result is None


def test_terminal_red_cleared_next_poll_records_only_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_release(tmp_path, monkeypatch)
    facts = {"release_head": HEAD, "release_branch": "release/r1"}
    observations = iter([facts, {**facts, "candidate": HEAD}])
    monkeypatch.setattr(mod, "gate_facts", lambda: next(observations))
    red = {"jobs": ["lint"], "urls": [], "run_id": "4242", "completed": True}
    checks = iter([red, None])
    monkeypatch.setattr(mod, "ci_red", lambda sha: next(checks))
    monkeypatch.setattr(mod, "_regate_run_id", lambda sha: "4242")
    monkeypatch.setattr(mod.time, "sleep", lambda delay: None)
    verdicts: list[tuple[str, str, str]] = []
    monkeypatch.setattr(mod, "note_ci_verdict", lambda *args: verdicts.append(args))
    args = mod.argparse.Namespace(ci_timeout=1.0, poll=0.0)
    assert mod.wait_ci("release/r1", args) == 0
    assert verdicts == [(HEAD, "green", "4242")]

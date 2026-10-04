"""Scheduler protocol failures and durable controller state, not live acceptance."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from precis.remote.slurm import Limits, SlurmRunner
from precis.remote.ssh import CommandResult, RemoteError

TOKEN = "a" * 32


class FakeTransport:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.submit_count = 0
        self.accept_then_disconnect = False
        self.state = "COMPLETED"
        self.user = "fixture"
        self.exit_code = "0:0"

    def run(
        self,
        remote_argv: Sequence[str],
        *,
        timeout_s: float = 30,
        input_data: bytes = b"",
        max_output: int = 16777216,
    ) -> CommandResult:
        argv = remote_argv
        kwargs = {"input_data": input_data}
        if argv[0] in {"mkdir", "cp", "scancel"}:
            return CommandResult(0, b"", b"")
        if argv[0] == "sh" and argv[3] == "stage":
            self.files[argv[4]] = kwargs["input_data"]
            return CommandResult(0, b"", b"")
        if argv[0] == "sha256sum":
            return CommandResult(
                0,
                hashlib.sha256(self.files[argv[1]]).hexdigest().encode() + b"  file\n",
                b"",
            )
        if argv[0] == "sh" and argv[3] == "submit":
            self.submit_count += 1
            self.files[argv[4] + "/receipt"] = b"42\n"
            if self.accept_then_disconnect:
                raise RemoteError("transport_timeout")
            return CommandResult(0, b"42\n", b"")
        if argv[0] == "cat":
            if argv[1] in self.files:
                return CommandResult(0, self.files[argv[1]], b"")
            return CommandResult(1, b"", b"missing")
        if argv[0] == "squeue":
            return CommandResult(0, b"", b"")
        if argv[0] == "sacct":
            if "--format=JobIDRaw,JobName%128" in argv:
                return CommandResult(0, ("42|mpx-" + TOKEN + "\n").encode(), b"")
            return CommandResult(
                0,
                f"42|{self.state}|{self.exit_code}|4|1|cpu=4,gres/gpu=1|pfixture|gpu|test|start|end|{self.user}|mpx-{TOKEN}\n".encode(),
                b"",
            )
        raise AssertionError(argv)


def make_runner(tmp_path: Path, transport: FakeTransport) -> SlurmRunner:
    return SlurmRunner(
        transport,
        remote_root="/project/fixture",
        journal_root=tmp_path,
        user="fixture",
        profile_id="fixture",
        limits=Limits(submission_interval=0, status_interval=0),
    )


def spec(stage: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage_id": stage["stage_id"],
        "script": "job.sh",
        "task_ids": ["task0"],
        "resources": {
            "account": "pfixture",
            "partition": "gpu",
            "qos": "test",
            "nodes": 1,
            "cpus": 4,
            "gpus": 1,
            "memory_mb": 16384,
            "wall_seconds": 600,
        },
    }


def staged_job(runner: SlurmRunner) -> dict[str, Any]:
    data = b"#!/bin/bash\ntrue\n"
    return spec(
        runner.stage({"job.sh": data}, {"job.sh": hashlib.sha256(data).hexdigest()})
    )


def test_reconnect_collect_and_lost_ack_never_resubmit(tmp_path: Path) -> None:
    transport = FakeTransport()
    runner = make_runner(tmp_path, transport)
    script = b"#!/bin/bash\ntrue\n"
    stage = runner.stage(
        {"job.sh": script}, {"job.sh": hashlib.sha256(script).hexdigest()}
    )
    transport.accept_then_disconnect = True
    handle = runner.submit(spec(stage), TOKEN)
    assert handle["phase"] == "submission_unknown"
    assert (tmp_path / (TOKEN + ".json")).exists()
    resumed = make_runner(tmp_path, transport)
    assert resumed.submit(spec(stage), TOKEN)["phase"] == "submission_unknown"
    adopted = resumed.recover(handle)
    assert adopted["job_id"] == "42" and transport.submit_count == 1
    output = adopted["remote_dir"] + "/result.json"
    transport.files[output] = b'{"energy": 1}'
    collected = resumed.collect(adopted, ["result.json"])
    assert collected["files"]["result.json"] == transport.files[output]
    assert resumed.collect(adopted, ["result.json"]) == collected


@pytest.mark.parametrize(
    "scheduler, expected",
    [
        ("COMPLETED", "missing_output"),
        ("TIMEOUT", "timeout"),
        ("FAILED", "failed"),
        ("CANCELLED", "cancelled"),
    ],
)
def test_every_task_gets_outcome(tmp_path: Path, scheduler: str, expected: str) -> None:
    transport = FakeTransport()
    transport.state = scheduler
    runner = make_runner(tmp_path, transport)
    job = staged_job(runner)
    job["task_ids"] = ["task0", "task1"]
    handle = runner.submit(job, TOKEN)
    assert runner.collect(handle, ["result.json"])["outcomes"] == {
        "task0": expected,
        "task1": expected,
    }


def test_hash_budget_and_intent_conflict(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, FakeTransport())
    with pytest.raises(RemoteError, match="artifact_hash_mismatch"):
        runner.stage({"job.sh": b"script"}, {"job.sh": "0" * 64})
    job = staged_job(runner)
    job["resources"]["wall_seconds"] = 601
    with pytest.raises(RemoteError, match="budget_exceeded"):
        runner.submit(job, TOKEN)
    job["resources"]["wall_seconds"] = 600
    runner.submit(job, TOKEN)
    job["task_ids"] = ["different"]
    with pytest.raises(RemoteError, match="intent_conflict"):
        runner.submit(job, TOKEN)


def test_scheduler_identity_and_exit_status_fail_closed(tmp_path: Path) -> None:
    transport = FakeTransport()
    runner = make_runner(tmp_path, transport)
    handle = runner.submit(staged_job(runner), TOKEN)
    transport.user = "somebody_else"
    with pytest.raises(RemoteError, match="scheduler_identity_mismatch"):
        runner.status(handle)
    transport.user = "fixture"
    transport.exit_code = "1:0"
    state = runner.status(handle)
    assert state["state"] == "failed" and state["exit_code"] == 1
    assert runner.collect(handle, ["result.json"])["outcomes"] == {"task0": "failed"}


def test_unknown_submission_counts_against_budget_and_cancel_is_request(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()
    transport.accept_then_disconnect = True
    runner = make_runner(tmp_path, transport)
    job = staged_job(runner)
    handle = runner.submit(job, TOKEN)
    with pytest.raises(RemoteError, match="inflight_limit"):
        runner.submit(job, "c" * 32)
    cancelled = runner.cancel(handle)
    assert cancelled["cancel_requested"] and cancelled["phase"] == "submitted"
    assert transport.submit_count == 1


def test_stage_ready_and_journal_survive_reconnect(tmp_path: Path) -> None:
    transport = FakeTransport()
    runner = make_runner(tmp_path, transport)
    data = b"fixture"
    stage = runner.stage(
        {"nested/input": data}, {"nested/input": hashlib.sha256(data).hexdigest()}
    )
    assert stage["remote_dir"] + "/ready.json" in transport.files
    assert (tmp_path / ("stage-" + stage["stage_id"] + ".manifest")).exists()
    with pytest.raises(RemoteError, match="path_invalid"):
        runner.stage(
            {"../escape": data}, {"../escape": hashlib.sha256(data).hexdigest()}
        )


def test_submit_requires_hash_verified_stage(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, FakeTransport())
    with pytest.raises(RemoteError, match="stage_unverified"):
        runner.submit(spec({"stage_id": "b" * 64}), TOKEN)
    assert not (tmp_path / (TOKEN + ".json")).exists()

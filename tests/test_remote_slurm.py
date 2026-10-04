"""Scheduler protocol failures and durable controller state, not live acceptance."""

from __future__ import annotations

import hashlib
import json
import posixpath
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
        self.calls: list[list[str]] = []

    def run(
        self,
        remote_argv: Sequence[str],
        *,
        timeout_s: float = 30,
        input_data: bytes = b"",
        max_output: int = 16777216,
    ) -> CommandResult:
        argv = remote_argv
        self.calls.append(list(argv))
        kwargs = {"input_data": input_data}
        if argv[0] in {"mkdir", "cp", "scancel"}:
            return CommandResult(0, b"", b"")
        if argv[0] == "rm":
            for path in argv[3:]:
                self.files.pop(posixpath.normpath(path), None)
            return CommandResult(0, b"", b"")
        if argv[0] == "sh" and argv[3] == "stage":
            path = posixpath.normpath(argv[4])
            self.files[path + ".tmp"] = kwargs["input_data"]
            self.files[path] = self.files.pop(path + ".tmp")
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


def test_local_version_wheel_name_is_safe_and_budgets_explicit(tmp_path: Path) -> None:
    import json

    from precis.remote.slurm import _name

    name = "wheelhouse/autocatpath-0.24.0+meluxina.pilot1-py3-none-any.whl"
    assert _name(name) == name
    for bad in [
        "../escape+name",
        "/absolute+name",
        "-option+name",
        "name+$(id)",
        "name+;id",
        "name+\nline",
    ]:
        with pytest.raises(RemoteError, match="path_invalid"):
            _name(bad)
    transport = FakeTransport()
    data = b"12345"
    hashes = {name: hashlib.sha256(data).hexdigest()}
    runner = SlurmRunner(
        transport,
        remote_root="/project/fixture",
        journal_root=tmp_path / "default",
        user="fixture",
        profile_id="fixture",
        limits=Limits(max_bundle_bytes=4),
    )
    with pytest.raises(RemoteError, match="bundle_invalid"):
        runner.stage({name: data}, hashes)
    assert not transport.files
    reviewed = SlurmRunner(
        transport,
        remote_root="/project/fixture",
        journal_root=tmp_path / "explicit",
        user="fixture",
        profile_id="reviewed",
        limits=Limits(max_bundle_bytes=5),
    )
    stage = reviewed.stage({name: data}, hashes)
    assert stage["bundle_bytes"] == 5 and stage["max_bundle_bytes"] == 5
    persisted = json.loads(
        (
            tmp_path / "explicit" / ("stage-" + stage["stage_id"] + ".manifest")
        ).read_text()
    )
    assert persisted["bundle_bytes"] == 5 and persisted["max_bundle_bytes"] == 5
    assert Limits().max_bundle_bytes == 2 * 1024**3  # Generic default unchanged.


def test_submitted_intent_records_explicit_staging_budget(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, FakeTransport())
    job = staged_job(runner)
    handle = runner.submit(job, TOKEN)
    assert handle["bundle_bytes"] == len(b"#!/bin/bash\ntrue\n")
    assert handle["max_bundle_bytes"] == 2 * 1024**3


def test_submit_cannot_silently_adopt_larger_staging_cap(tmp_path: Path) -> None:
    transport = FakeTransport()
    high = SlurmRunner(
        transport,
        remote_root="/project/fixture",
        journal_root=tmp_path,
        user="fixture",
        profile_id="fixture",
        limits=Limits(max_bundle_bytes=64),
    )
    job = staged_job(high)
    low = SlurmRunner(
        transport,
        remote_root="/project/fixture",
        journal_root=tmp_path,
        user="fixture",
        profile_id="fixture",
        limits=Limits(max_bundle_bytes=32),
    )
    with pytest.raises(RemoteError, match="bundle_invalid"):
        low.submit(job, TOKEN)
    assert transport.submit_count == 0 and not (tmp_path / (TOKEN + ".json")).exists()


@pytest.mark.parametrize(
    "bundle",
    [
        {"job.sh": b"A", "./job.sh": b"B"},
        {"job.sh.tmp": b"B", "job.sh": b"A"},
        {"ready.json": b"caller metadata"},
        {"ready.json.tmp": b"caller temp"},
        {"ready.json/child": b"file vs generated directory"},
        {"job.sh": b"A", "job.sh.tmp/child": b"temp vs directory"},
        {"input": b"A", "input/child": b"file vs directory"},
        {"nested//input": b"noncanonical"},
    ],
)
def test_stage_path_conflicts_refused_before_remote_writes(
    tmp_path: Path, bundle: dict[str, bytes]
) -> None:
    transport = FakeTransport()
    runner = make_runner(tmp_path, transport)
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in bundle.items()}
    with pytest.raises(RemoteError, match="bundle_paths_invalid"):
        runner.stage(bundle, hashes)
    assert transport.calls == [] and transport.files == {}
    assert not list(tmp_path.glob("*.manifest"))
    stage_id = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    with pytest.raises(RemoteError, match="stage_unverified"):
        runner.submit(spec({"stage_id": stage_id}), TOKEN)
    assert transport.submit_count == 0 and not (tmp_path / (TOKEN + ".json")).exists()


@pytest.mark.parametrize("restage", [False, True])
def test_final_inventory_drift_prevents_readiness_and_submit(
    tmp_path: Path, restage: bool
) -> None:
    class DriftTransport(FakeTransport):
        corrupt = False

        def run(self, remote_argv: Sequence[str], **kwargs: Any) -> CommandResult:
            result = super().run(remote_argv, **kwargs)
            if (
                self.corrupt
                and remote_argv[0] == "sh"
                and remote_argv[3] == "stage"
                and remote_argv[4].endswith("/input")
            ):
                self.files[remote_argv[4].removesuffix("input") + "job.sh"] = b"drift"
            return result

    transport = DriftTransport()
    runner = make_runner(tmp_path, transport)
    bundle = {"job.sh": b"script", "input": b"input"}
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in bundle.items()}
    if restage:
        first = runner.stage(bundle, hashes)
        assert first["remote_dir"] + "/ready.json" in transport.files
    transport.corrupt = True
    with pytest.raises(RemoteError, match="artifact_hash_mismatch"):
        runner.stage(bundle, hashes)
    assert not any(path.endswith("/ready.json") for path in transport.files)
    manifest = json.loads(next(tmp_path.glob("*.manifest")).read_text())
    assert "hashes" not in manifest
    with pytest.raises(RemoteError, match="stage_unverified"):
        runner.submit(spec(manifest), TOKEN)
    assert transport.submit_count == 0 and not (tmp_path / (TOKEN + ".json")).exists()


@pytest.mark.parametrize(
    "fault, code, outcome",
    [
        ("transport", "transport_timeout", "collection_pending"),
        ("checksum_transport", "transport_unavailable", "collection_pending"),
        ("checksum_command", "output_checksum_failed", "collection_pending"),
        ("checksum_mismatch", "output_hash_mismatch", "invalid_output"),
        ("checksum_malformed", "checksum_invalid", "invalid_output"),
        ("output_cap", "output_limit", "invalid_output"),
        ("oversize_response", "output_limit", "invalid_output"),
        ("unclassified", "collection_transport_failed", "collection_pending"),
        ("status_transport", "transport_timeout", "collection_pending"),
    ],
)
def test_collection_failure_journals_every_task_and_retries_same_intent(
    tmp_path: Path, fault: str, code: str, outcome: str
) -> None:
    class FailingTransport(FakeTransport):
        active = False

        def run(self, remote_argv: Sequence[str], **kwargs: Any) -> CommandResult:
            if self.active:
                command = remote_argv[0]
                if command == "squeue" and fault == "status_transport":
                    raise RemoteError("transport_timeout")
                if remote_argv[1].endswith("/second.json"):
                    if command == "cat":
                        if fault == "transport":
                            raise RemoteError("transport_timeout")
                        if fault == "output_cap":
                            raise RemoteError("output_limit")
                        if fault == "oversize_response":
                            return CommandResult(0, b"x" * 65, b"")
                        if fault == "unclassified":
                            raise RemoteError("SYNTHETIC_PRIVATE_SENTINEL")
                    if command == "sha256sum":
                        if fault == "checksum_transport":
                            raise RemoteError("transport_unavailable")
                        if fault == "checksum_command":
                            return CommandResult(1, b"", b"SYNTHETIC_PRIVATE_SENTINEL")
                        if fault == "checksum_mismatch":
                            return CommandResult(0, b"0" * 64 + b"  file\n", b"")
                        if fault == "checksum_malformed":
                            return CommandResult(0, b"", b"")
            return super().run(remote_argv, **kwargs)

    transport = FailingTransport()
    runner = SlurmRunner(
        transport,
        remote_root="/project/fixture",
        journal_root=tmp_path,
        user="fixture",
        profile_id="fixture",
        limits=Limits(submission_interval=0, status_interval=0, max_output_bytes=64),
    )
    job = staged_job(runner)
    job["task_ids"] = ["task0", "task1"]
    handle = runner.submit(job, TOKEN)
    known = runner.status(handle)
    transport.files[handle["remote_dir"] + "/first.json"] = b"first"
    transport.files[handle["remote_dir"] + "/second.json"] = b"second"
    transport.active = True
    with pytest.raises(RemoteError, match=code) as caught:
        runner.collect(handle, ["first.json", "second.json"])
    assert str(caught.value) == code and caught.value.__cause__ is None
    journal_text = (tmp_path / (TOKEN + ".json")).read_text()
    saved = json.loads(journal_text)
    assert "SYNTHETIC_PRIVATE_SENTINEL" not in journal_text
    assert saved["outcomes"] == {"task0": outcome, "task1": outcome}
    assert saved["collection"]["error"] == code
    assert saved["collection"]["status"] == (
        "failed" if outcome == "invalid_output" else "pending"
    )
    for key in [
        "phase",
        "state",
        "scheduler_state",
        "accounting",
        "job_id",
        "job_hash",
        "resources",
    ]:
        assert saved[key] == known[key]
    assert saved["collection"]["attempts"] == 1
    assert saved["collection"]["failed_attempts"] == 1
    if fault != "status_transport":
        assert saved["output_hashes"] == {
            "first.json": hashlib.sha256(b"first").hexdigest()
        }
    # A new controller adopts the journal and retries collection, never submit.
    resumed = SlurmRunner(
        transport,
        remote_root="/project/fixture",
        journal_root=tmp_path,
        user="fixture",
        profile_id="fixture",
        limits=runner.limits,
    )
    assert resumed.submit(job, TOKEN)["collection"] == saved["collection"]
    assert transport.submit_count == 1
    transport.active = False
    collected = resumed.collect(handle, ["first.json", "second.json"])
    assert collected["files"] == {"first.json": b"first", "second.json": b"second"}
    assert collected["outcomes"] == {"task0": "collected", "task1": "collected"}
    saved = json.loads((tmp_path / (TOKEN + ".json")).read_text())
    assert saved["collection"] == {
        "status": "complete",
        "error": None,
        "last_error": code,
        "attempts": 2,
        "failed_attempts": 1,
        "at": saved["collection"]["at"],
    }
    assert saved["job_id"] == handle["job_id"] and transport.submit_count == 1


@pytest.mark.parametrize("scheduler", ["FAILED", "TIMEOUT", "CANCELLED"])
def test_collection_transport_failure_preserves_terminal_scheduler_evidence(
    tmp_path: Path, scheduler: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = FakeTransport()
    transport.state = scheduler
    runner = make_runner(tmp_path, transport)
    job = staged_job(runner)
    job["task_ids"] = ["task0", "task1"]
    handle = runner.submit(job, TOKEN)
    known = runner.status(handle)
    original = transport.run

    def fail(remote_argv: Sequence[str], **kwargs: Any) -> CommandResult:
        if remote_argv[0] == "cat":
            raise RemoteError("transport_unavailable")
        return original(remote_argv, **kwargs)

    monkeypatch.setattr(transport, "run", fail)
    with pytest.raises(RemoteError, match="transport_unavailable"):
        runner.collect(handle, ["result.json"])
    saved = json.loads((tmp_path / (TOKEN + ".json")).read_text())
    assert saved["outcomes"] == {
        "task0": "collection_pending",
        "task1": "collection_pending",
    }
    assert saved["scheduler_state"] == scheduler and saved["state"] == known["state"]
    assert saved["accounting"] == known["accounting"] and saved["phase"] == "terminal"
    assert transport.submit_count == 1

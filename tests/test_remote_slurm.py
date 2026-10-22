"""Scheduler protocol failures and durable controller state, not live acceptance."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

if sys.platform == "win32":
    pytest.skip(
        "POSIX-only: precis.remote.slurm needs fcntl at import",
        allow_module_level=True,
    )

from precis.remote.slurm import Limits, SlurmRunner
from precis.remote.ssh import CommandResult, RemoteError

TOKEN = "a" * 32


class FakeTransport:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.mtimes: dict[str, float] = {}
        self.dirs: set[str] = set()
        self.now = 1_800_000_000.0
        self.submit_count = 0
        self.job_names: dict[str, str] = {}
        self.stream_calls = 0
        self.accept_then_disconnect = False
        self.identity_deadline: float | None = None
        self.seconds_per_chunk = 0.0
        self.state = "COMPLETED"
        self.user = "fixture"
        self.exit_code = "0:0"
        self.calls: list[list[str]] = []

    def _write(self, path: str, data: bytes) -> None:
        path = posixpath.normpath(path)
        self.files[path] = data
        self.mtimes[path] = self.now

    def stream(
        self,
        remote_argv: Sequence[str],
        *,
        input_path: Path,
        chunk_bytes: int = 1048576,
        timeout_s: float = 3600,
        max_output: int = 16777216,
    ) -> CommandResult:
        argv = list(remote_argv)
        self.calls.append(argv)
        self.stream_calls += 1
        assert argv[0] == "sh" and argv[3] == "image"
        target = posixpath.normpath(argv[4])
        written = bytearray()
        with open(input_path, "rb") as handle:
            while chunk := handle.read(chunk_bytes):
                self.now += self.seconds_per_chunk
                if (
                    self.identity_deadline is not None
                    and self.now > self.identity_deadline
                ):
                    self._write(target, bytes(written))
                    raise RemoteError("transport_unavailable")
                written += chunk
        self._write(target, bytes(written))
        return CommandResult(0, b"", b"")

    def run(
        self,
        remote_argv: Sequence[str],
        *,
        timeout_s: float = 30,
        input_data: bytes = b"",
        max_output: int = 16777216,
    ) -> CommandResult:
        argv = list(remote_argv)
        self.calls.append(argv)
        if argv[0] == "env" and argv[1].startswith("TZ="):
            argv = argv[2:]
        if argv[0] == "mkdir":
            self.dirs.add(posixpath.normpath(argv[-1]))
            return CommandResult(0, b"", b"")
        if argv[0] == "cp" or argv[0] == "scancel":
            return CommandResult(0, b"", b"")
        if argv[0] == "mv":
            source, target = argv[3], argv[4]
            if source not in self.files:
                return CommandResult(1, b"", b"missing")
            self.files[target] = self.files.pop(source)
            self.mtimes[target] = self.mtimes.pop(source)
            return CommandResult(0, b"", b"")
        if argv[0] == "rm":
            for path in argv[3:]:
                path = posixpath.normpath(path)
                if argv[1] in {"-r", "-rf", "-fr", "-R"}:
                    for key in list(self.files):
                        if key == path or key.startswith(path + "/"):
                            self.files.pop(key)
                            self.mtimes.pop(key, None)
                    self.dirs.discard(path)
                else:
                    self.files.pop(path, None)
                    self.mtimes.pop(path, None)
            return CommandResult(0, b"", b"")
        if argv[0] == "find":
            directory = posixpath.normpath(argv[1])
            days = int(argv[7].lstrip("+"))
            cutoff = self.now - days * 86400
            names = sorted(
                posixpath.basename(path)
                for path in self.files
                if posixpath.dirname(path) == directory
                and self.mtimes.get(path, self.now) < cutoff
            )
            if directory not in self.dirs and not names:
                return CommandResult(1, b"", b"no such directory")
            return CommandResult(
                0, ("\n".join(names) + "\n").encode() if names else b"", b""
            )
        if argv[0] == "sh" and argv[3] == "stage":
            self._write(argv[4], input_data)
            return CommandResult(0, b"", b"")
        if argv[0] == "sh" and argv[3] == "cleanup":
            self._write(argv[4] + ".done", input_data)
            return CommandResult(0, b"", b"")
        if argv[0] == "sha256sum":
            if argv[1] not in self.files:
                return CommandResult(1, b"", b"missing")
            return CommandResult(
                0,
                hashlib.sha256(self.files[argv[1]]).hexdigest().encode() + b"  file\n",
                b"",
            )
        if argv[0] == "sh" and argv[3] == "submit":
            if argv[4] + ".done" in self.files:
                return CommandResult(73, b"", b"consumed")
            self.submit_count += 1
            job_id = str(41 + self.submit_count)
            job_name = re.search(r"mpx-[a-f0-9]+", argv[2])
            self.job_names[job_id] = job_name.group(0) if job_name else ""
            self.files[argv[4] + "/receipt"] = (job_id + "\n").encode()
            if self.accept_then_disconnect:
                raise RemoteError("transport_timeout")
            return CommandResult(0, (job_id + "\n").encode(), b"")
        if argv[0] == "cat":
            if argv[1] in self.files:
                return CommandResult(0, self.files[argv[1]], b"")
            return CommandResult(1, b"", b"missing")
        if argv[0] == "squeue":
            return CommandResult(0, b"", b"")
        if argv[0] == "sacct":
            if "--format=JobIDRaw,JobName%128" in argv:
                return CommandResult(0, ("42|mpx-" + TOKEN + "\n").encode(), b"")
            job_id = argv[argv.index("-j") + 1]
            name = self.job_names.get(job_id, "mpx-" + TOKEN)
            return CommandResult(
                0,
                f"{job_id}|{self.state}|{self.exit_code}|4|1|cpu=4,gres/gpu=1|pfixture|gpu|test|start|end|{self.user}|{name}\n".encode(),
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


def spec(stage: dict[str, Any], task_ids: Sequence[str] = ("task0",)) -> dict[str, Any]:
    return {
        "stage_id": stage["stage_id"],
        "script": "job.sh",
        "task_ids": list(task_ids),
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


def staged_job(
    runner: SlurmRunner, task_ids: Sequence[str] = ("task0",)
) -> dict[str, Any]:
    script = b"#!/bin/bash\ntrue\n"
    tasks = json.dumps(list(task_ids)).encode()
    stage = runner.stage(
        {"job.sh": script, "tasks.json": tasks},
        {
            "job.sh": hashlib.sha256(script).hexdigest(),
            "tasks.json": hashlib.sha256(tasks).hexdigest(),
        },
    )
    return spec(stage, task_ids)


def outputs(task_ids: Sequence[str], name: str = "result.json") -> dict[str, list[str]]:
    return {task: [name] for task in task_ids}


def test_reconnect_collect_and_lost_ack_never_resubmit(tmp_path: Path) -> None:
    transport = FakeTransport()
    runner = make_runner(tmp_path, transport)
    job = staged_job(runner)
    transport.accept_then_disconnect = True
    handle = runner.submit(job, TOKEN)
    assert handle["phase"] == "submission_unknown"
    assert (tmp_path / (TOKEN + ".json")).exists()
    resumed = make_runner(tmp_path, transport)
    assert resumed.submit(job, TOKEN)["phase"] == "submission_unknown"
    adopted = resumed.recover(handle)
    assert adopted["job_id"] == "42" and transport.submit_count == 1
    output = adopted["remote_dir"] + "/out/task0/result.json"
    transport.files[output] = b'{"energy": 1}'
    collected = resumed.collect(adopted, outputs(["task0"]))
    assert collected["files"]["task0/result.json"] == transport.files[output]
    assert resumed.collect(adopted, outputs(["task0"])) == collected


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
    job = staged_job(runner, ["task0", "task1"])
    handle = runner.submit(job, TOKEN)
    assert runner.collect(handle, outputs(["task0", "task1"]))["outcomes"] == {
        "task0": expected,
        "task1": expected,
    }
    saved = json.loads((tmp_path / (TOKEN + ".json")).read_text(encoding="utf-8"))
    assert saved["collection"]["status"] == "complete"
    assert saved["collection"]["error"] is None
    assert saved["missing"] == ["task0/result.json", "task1/result.json"]


@pytest.mark.parametrize("scheduler", ["PREEMPTED", "NODE_FAIL", "BOOT_FAIL"])
def test_preempted_is_terminal_but_not_failed(tmp_path: Path, scheduler: str) -> None:
    transport = FakeTransport()
    transport.state = scheduler
    runner = make_runner(tmp_path, transport)
    handle = runner.submit(staged_job(runner), TOKEN)
    state = runner.status(handle)
    assert state["state"] == "preempted" and state["phase"] == "terminal"
    collected = runner.collect(handle, outputs(["task0"]))
    assert collected["outcomes"] == {"task0": "preempted"}


def test_sbatch_disables_slurm_requeue(tmp_path: Path) -> None:
    transport = FakeTransport()
    runner = make_runner(tmp_path, transport)
    runner.submit(staged_job(runner), TOKEN)
    submit = next(
        call for call in transport.calls if call[0] == "sh" and call[3] == "submit"
    )
    assert "sbatch" in submit[2] and "--no-requeue" in submit[2]


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
    assert runner.collect(handle, outputs(["task0"]))["outcomes"] == {"task0": "failed"}


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
        ).read_text(encoding="utf-8")
    )
    assert persisted["bundle_bytes"] == 5 and persisted["max_bundle_bytes"] == 5
    assert Limits().max_bundle_bytes == 2 * 1024**3  # Generic default unchanged.


def test_submitted_intent_records_explicit_staging_budget(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, FakeTransport())
    job = staged_job(runner)
    handle = runner.submit(job, TOKEN)
    assert handle["bundle_bytes"] == len(b"#!/bin/bash\ntrue\n") + len(b'["task0"]')
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
    manifest = json.loads(next(tmp_path.glob("*.manifest")).read_text(encoding="utf-8"))
    assert "hashes" not in manifest
    with pytest.raises(RemoteError, match="stage_unverified"):
        runner.submit(spec(manifest), TOKEN)
    assert transport.submit_count == 0 and not (tmp_path / (TOKEN + ".json")).exists()


@pytest.mark.parametrize(
    "fault, code, outcome",
    [
        ("transport", "transport_timeout", "collection_pending"),
        ("ssh_return255", "transport_unavailable", "collection_pending"),
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
                        if fault == "ssh_return255":
                            return CommandResult(
                                255, b"", b"SYNTHETIC_PRIVATE_SENTINEL"
                            )
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
    job = staged_job(runner, ["task0", "task1"])
    handle = runner.submit(job, TOKEN)
    known = runner.status(handle)
    transport.files[handle["remote_dir"] + "/out/task0/first.json"] = b"first"
    transport.files[handle["remote_dir"] + "/out/task1/second.json"] = b"second"
    transport.active = True
    wanted = {"task0": ["first.json"], "task1": ["second.json"]}
    with pytest.raises(RemoteError, match=code) as caught:
        runner.collect(handle, wanted)
    assert str(caught.value) == code and caught.value.__cause__ is None
    journal_text = (tmp_path / (TOKEN + ".json")).read_text(encoding="utf-8")
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
        "token",
        "user",
        "profile_id",
    ]:
        assert saved[key] == known[key]
    assert saved["collection"]["attempts"] == 1
    assert saved["collection"]["failed_attempts"] == 1
    assert saved["missing"] == []
    if fault != "status_transport":
        assert saved["output_hashes"] == {
            "task0/first.json": hashlib.sha256(b"first").hexdigest()
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
    collected = resumed.collect(handle, wanted)
    assert collected["files"] == {
        "task0/first.json": b"first",
        "task1/second.json": b"second",
    }
    assert collected["outcomes"] == {"task0": "collected", "task1": "collected"}
    saved = json.loads((tmp_path / (TOKEN + ".json")).read_text(encoding="utf-8"))
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
    job = staged_job(runner, ["task0", "task1"])
    handle = runner.submit(job, TOKEN)
    known = runner.status(handle)
    original = transport.run

    def fail(remote_argv: Sequence[str], **kwargs: Any) -> CommandResult:
        if remote_argv[0] == "cat":
            raise RemoteError("transport_unavailable")
        return original(remote_argv, **kwargs)

    monkeypatch.setattr(transport, "run", fail)
    with pytest.raises(RemoteError, match="transport_unavailable"):
        runner.collect(handle, outputs(["task0", "task1"]))
    saved = json.loads((tmp_path / (TOKEN + ".json")).read_text(encoding="utf-8"))
    assert saved["outcomes"] == {
        "task0": "collection_pending",
        "task1": "collection_pending",
    }
    assert saved["scheduler_state"] == scheduler and saved["state"] == known["state"]
    assert saved["accounting"] == known["accounting"] and saved["phase"] == "terminal"
    assert transport.submit_count == 1


def test_tasks_json_must_match_intent_task_ids(tmp_path: Path) -> None:
    transport = FakeTransport()
    runner = make_runner(tmp_path, transport)
    script = b"#!/bin/bash\ntrue\n"
    tasks = json.dumps(["other"]).encode()
    stage = runner.stage(
        {"job.sh": script, "tasks.json": tasks},
        {
            "job.sh": hashlib.sha256(script).hexdigest(),
            "tasks.json": hashlib.sha256(tasks).hexdigest(),
        },
    )
    with pytest.raises(RemoteError, match="tasks_invalid"):
        runner.submit(spec(stage, ["task0"]), TOKEN)
    assert transport.submit_count == 0 and not (tmp_path / (TOKEN + ".json")).exists()
    # Staged tasks.json that vanished or drifted is unverified stage evidence.
    stage = runner.stage(
        {"job.sh": script, "tasks.json": json.dumps(["task0"]).encode()},
        {
            "job.sh": hashlib.sha256(script).hexdigest(),
            "tasks.json": hashlib.sha256(b'["task0"]').hexdigest(),
        },
    )
    del transport.files[stage["remote_dir"] + "/tasks.json"]
    with pytest.raises(RemoteError, match="stage_unverified"):
        runner.submit(spec(stage), "b" * 32)
    # Task ids become out/<task>/ path components remotely.
    with pytest.raises(RemoteError, match="tasks_invalid"):
        runner.submit(staged_job(runner, ["../escape"]), "c" * 32)


def test_missing_output_is_per_task_not_per_job(tmp_path: Path) -> None:
    transport = FakeTransport()
    runner = make_runner(tmp_path, transport)
    handle = runner.submit(staged_job(runner, ["task0", "task1"]), TOKEN)
    transport.files[handle["remote_dir"] + "/out/task0/result.json"] = b"done"
    collected = runner.collect(handle, outputs(["task0", "task1"]), logs=["slurm.out"])
    assert collected["outcomes"] == {
        "task0": "collected",
        "task1": "missing_output",
    }
    assert collected["files"] == {"task0/result.json": b"done"}
    saved = json.loads((tmp_path / (TOKEN + ".json")).read_text(encoding="utf-8"))
    assert saved["missing"] == ["task1/result.json", "slurm.out"]
    with pytest.raises(RemoteError, match="tasks_invalid"):
        runner.collect(handle, {"unknown": ["result.json"]})


def test_preempted_collects_complete_task_results(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.state = "PREEMPTED"
    runner = make_runner(tmp_path, transport)
    handle = runner.submit(staged_job(runner, ["task0", "task1"]), TOKEN)
    transport.files[handle["remote_dir"] + "/out/task0/result.json"] = b"done"
    transport.files[handle["remote_dir"] + "/slurm.out"] = b"log tail"
    collected = runner.collect(handle, outputs(["task0", "task1"]), logs=["slurm.out"])
    assert collected["outcomes"] == {"task0": "collected", "task1": "preempted"}
    assert collected["files"]["slurm.out"] == b"log tail"


def test_recover_pins_utc_for_sacct_starttime(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.accept_then_disconnect = True
    runner = make_runner(tmp_path, transport)
    handle = runner.submit(staged_job(runner), TOKEN)
    recovered = runner.recover(handle)
    assert recovered["job_id"] == "42"
    assert any(call[:3] == ["env", "TZ=UTC", "sacct"] for call in transport.calls)


def write_image(directory: Path, data: bytes = b"sif-bytes" * 100) -> tuple[Path, str]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "workload.sif"
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


def test_stage_image_streams_once_then_noops(tmp_path: Path) -> None:
    transport = FakeTransport()
    runner = make_runner(tmp_path, transport)
    path, sha = write_image(tmp_path)
    staged = runner.stage_image(path, sha)
    remote = f"/project/fixture/images/{sha}.sif"
    assert staged == {
        "image_sha256": sha,
        "remote_path": remote,
        "image_bytes": len(path.read_bytes()),
        "max_image_bytes": runner.limits.max_image_bytes,
        "staged": True,
    }
    assert transport.stream_calls == 1
    assert transport.files[remote] == path.read_bytes()
    assert remote + ".tmp" not in transport.files
    manifest = json.loads(
        (tmp_path / ("image-" + sha + ".manifest")).read_text(encoding="utf-8")
    )
    assert manifest["image_sha256"] == sha
    # Verified remote image makes a restage a no-op (plus stale tmp cleanup).
    transport.files[remote + ".tmp"] = b"stale"
    again = runner.stage_image(path, sha)
    assert again["staged"] is False and transport.stream_calls == 1
    assert remote + ".tmp" not in transport.files
    transport.files[remote] = b"corrupt"
    with pytest.raises(RemoteError, match="image_hash_mismatch"):
        runner.stage_image(path, sha)


def test_stage_image_refuses_bad_source_before_remote_calls(tmp_path: Path) -> None:
    transport = FakeTransport()
    runner = SlurmRunner(
        transport,
        remote_root="/project/fixture",
        journal_root=tmp_path,
        user="fixture",
        profile_id="fixture",
        limits=Limits(max_image_bytes=4),
    )
    path, sha = write_image(tmp_path)
    with pytest.raises(RemoteError, match="image_invalid"):
        runner.stage_image(path, "nothex")
    with pytest.raises(RemoteError, match="image_too_large"):
        runner.stage_image(path, sha)
    with pytest.raises(RemoteError, match="image_invalid"):
        runner.stage_image(tmp_path / "missing.sif", sha)
    with pytest.raises(RemoteError, match="image_hash_mismatch"):
        SlurmRunner(
            transport,
            remote_root="/project/fixture",
            journal_root=tmp_path / "other",
            user="fixture",
            profile_id="fixture",
        ).stage_image(path, "0" * 64)
    assert transport.calls == [] and transport.stream_calls == 0


def test_stage_image_cleans_tmp_when_identity_dies_mid_upload(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()
    runner = SlurmRunner(
        transport,
        remote_root="/project/fixture",
        journal_root=tmp_path,
        user="fixture",
        profile_id="fixture",
        limits=Limits(image_chunk_bytes=128),
    )
    path, sha = write_image(tmp_path, b"x" * 2048)
    transport.seconds_per_chunk = 5
    transport.identity_deadline = transport.now + 60  # ~60 s vault identity
    with pytest.raises(RemoteError, match="transport_unavailable"):
        runner.stage_image(path, sha)
    assert transport.stream_calls == 1  # one process, not per-chunk sessions
    assert not any(p.endswith(".tmp") for p in transport.files)
    assert not (tmp_path / ("image-" + sha + ".manifest")).exists()


def test_stage_image_cleans_tmp_on_remote_hash_mismatch(tmp_path: Path) -> None:
    class CorruptTransport(FakeTransport):
        def stream(self, remote_argv: Sequence[str], **kwargs: Any) -> CommandResult:
            result = super().stream(remote_argv, **kwargs)
            target = posixpath.normpath(remote_argv[4])
            self._write(target, b"corrupted in flight")
            return result

    transport = CorruptTransport()
    runner = make_runner(tmp_path, transport)
    path, sha = write_image(tmp_path)
    with pytest.raises(RemoteError, match="image_hash_mismatch"):
        runner.stage_image(path, sha)
    assert not any(p.endswith(".tmp") for p in transport.files)


def test_submit_requires_verified_image_and_passes_its_path(tmp_path: Path) -> None:
    transport = FakeTransport()
    runner = make_runner(tmp_path, transport)
    path, sha = write_image(tmp_path)
    job = staged_job(runner)
    job["image_sha256"] = sha
    with pytest.raises(RemoteError, match="image_unverified"):
        runner.submit(job, TOKEN)
    assert transport.submit_count == 0 and not (tmp_path / (TOKEN + ".json")).exists()
    runner.stage_image(path, sha)
    handle = runner.submit(job, TOKEN)
    assert handle["image_sha256"] == sha
    submit = next(
        call for call in transport.calls if call[0] == "sh" and call[3] == "submit"
    )
    assert f"/project/fixture/images/{sha}.sif" in submit[2]


def collected_handle(
    runner: SlurmRunner, transport: FakeTransport, token: str = TOKEN
) -> dict[str, Any]:
    handle = runner.submit(staged_job(runner), token)
    transport.files[handle["remote_dir"] + "/out/task0/result.json"] = b"done"
    runner.collect(handle, outputs(["task0"]))
    return handle


def test_cleanup_tombstones_and_derives_paths(tmp_path: Path) -> None:
    transport = FakeTransport()
    runner = make_runner(tmp_path, transport)
    handle = collected_handle(runner, transport)
    run_dir = f"/project/fixture/run-{TOKEN}"
    stage_dir = f"/project/fixture/stage-{handle['stage_id']}"
    # A re-collected but poisoned handle cannot redirect the deletes.
    state = json.loads((tmp_path / (TOKEN + ".json")).read_text(encoding="utf-8"))
    state["remote_dir"] = "/elsewhere/poisoned"
    (tmp_path / (TOKEN + ".json")).write_text(json.dumps(state), encoding="utf-8")
    cleaned = runner.cleanup({"token": TOKEN})
    assert cleaned["phase"] == "cleaned"
    assert transport.files[run_dir + ".done"]
    assert not any(p.startswith(run_dir + "/") for p in transport.files)
    assert not any(p.startswith(stage_dir) for p in transport.files)
    assert not any(
        "/elsewhere" in str(part) for call in transport.calls for part in call
    )
    assert not (tmp_path / ("stage-" + handle["stage_id"] + ".manifest")).exists()


def test_cleanup_requires_complete_collection(tmp_path: Path) -> None:
    transport = FakeTransport()
    runner = make_runner(tmp_path, transport)
    handle = runner.submit(staged_job(runner), TOKEN)
    with pytest.raises(RemoteError, match="not_collected"):
        runner.cleanup(handle)
    assert transport.files[handle["remote_dir"] + "/receipt"]


def test_cleanup_keeps_stage_named_by_uncollected_intent(tmp_path: Path) -> None:
    transport = FakeTransport()
    runner = SlurmRunner(
        transport,
        remote_root="/project/fixture",
        journal_root=tmp_path,
        user="fixture",
        profile_id="fixture",
        limits=Limits(submission_interval=0, status_interval=0, max_inflight=2),
    )
    job = staged_job(runner)
    first = runner.submit(job, TOKEN)
    second = runner.submit(job, "b" * 32)
    stage_dir = f"/project/fixture/stage-{first['stage_id']}"
    for handle in (first, second):
        transport.files[handle["remote_dir"] + "/out/task0/result.json"] = b"done"
    runner.collect(first, outputs(["task0"]))
    runner.cleanup(first)
    assert any(p.startswith(stage_dir + "/") for p in transport.files)
    assert (tmp_path / ("stage-" + first["stage_id"] + ".manifest")).exists()
    runner.collect(second, outputs(["task0"]))
    runner.cleanup(second)
    assert not any(p.startswith(stage_dir) for p in transport.files)
    assert not (tmp_path / ("stage-" + first["stage_id"] + ".manifest")).exists()


def test_tombstone_refuses_resubmit_after_journal_loss(tmp_path: Path) -> None:
    transport = FakeTransport()
    runner = SlurmRunner(
        transport,
        remote_root="/project/fixture",
        journal_root=tmp_path,
        user="fixture",
        profile_id="fixture",
        limits=Limits(submission_interval=0, status_interval=0, max_inflight=2),
    )
    job = staged_job(runner)
    first = collected_handle(runner, transport, TOKEN)
    runner.submit(job, "b" * 32)  # keeps the shared stage alive
    runner.cleanup(first)
    assert transport.files[f"/project/fixture/run-{TOKEN}.done"]
    (tmp_path / (TOKEN + ".json")).unlink()  # controller loses its journal
    with pytest.raises(RemoteError, match="intent_consumed"):
        runner.submit(job, TOKEN)
    assert transport.submit_count == 2
    assert (
        json.loads((tmp_path / (TOKEN + ".json")).read_text(encoding="utf-8"))["phase"]
        == "consumed"
    )


def test_prune_images_keep_age_and_active_references(tmp_path: Path) -> None:
    transport = FakeTransport()
    runner = SlurmRunner(
        transport,
        remote_root="/project/fixture",
        journal_root=tmp_path,
        user="fixture",
        profile_id="fixture",
        limits=Limits(submission_interval=0, status_interval=0, max_inflight=2),
    )
    keep_path, keep_sha = write_image(tmp_path / "a", b"keep" * 100)
    drop_path, drop_sha = write_image(tmp_path / "b", b"drop" * 100)
    live_path, live_sha = write_image(tmp_path / "c", b"live" * 100)
    for path, sha in (
        (keep_path, keep_sha),
        (drop_path, drop_sha),
        (live_path, live_sha),
    ):
        runner.stage_image(path, sha)
    job = staged_job(runner)
    job["image_sha256"] = live_sha
    runner.submit(job, TOKEN)  # uncollected intent names live_sha
    images = "/project/fixture/images"
    fresh = "d" * 64
    transport._write(f"{images}/{fresh}.sif", b"fresh")
    transport._write(f"{images}/{drop_sha}.sif.tmp", b"stale upload")
    young_tmp = f"{images}/{'e' * 64}.sif.tmp"
    transport._write(young_tmp, b"in flight")
    # Age everything except the fresh image and the in-flight tmp.
    for mpath in list(transport.mtimes):
        if mpath in {f"{images}/{fresh}.sif", young_tmp}:
            continue
        transport.mtimes[mpath] = transport.now - 9 * 86400
    result = runner.prune_images(keep=[keep_sha], older_than_days=7)
    assert sorted(result["removed"]) == [
        f"{drop_sha}.sif",
        f"{drop_sha}.sif.tmp",
    ]
    assert sorted(result["kept"]) == [f"{keep_sha}.sif", f"{live_sha}.sif"]
    assert f"{images}/{drop_sha}.sif" not in transport.files
    assert f"{images}/{live_sha}.sif" in transport.files
    assert f"{images}/{fresh}.sif" in transport.files
    assert not (tmp_path / ("image-" + drop_sha + ".manifest")).exists()
    assert (tmp_path / ("image-" + keep_sha + ".manifest")).exists()
    with pytest.raises(RemoteError, match="image_invalid"):
        runner.prune_images(["nothex"])

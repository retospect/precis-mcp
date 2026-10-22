"""Injected SSH batch lifecycle with durable intent and uncertain-ack recovery.

No vault/workload imports. A receipt or unique scheduler token can establish
acceptance; missing evidence never permits automatic resubmission. Journal
locks serialize controller budgets across processes sharing its directory.

Literal plus signs preserve artifact local-version names. The default staging
cap stays2GiB; callers must supply reviewed overrides explicitly, and actual
bytes/selected cap accompany ready markers and durable submission intents.

Artifact names must be canonical and disjoint from generated/temp paths;
the full inventory is rechecked before readiness, since per-upload checks
alone miss later overwrites. Collection errors journal every task before
raising, preserving scheduler evidence and the same intent for collection retry.

Jobs submit with --no-requeue: a preempted or node-failed row is terminal
evidence for a caller-decided fresh attempt, never an in-place scheduler rerun
over a half-written run directory. One sbatch carries many tasks; the job
script iterates a staged tasks.json and writes out/<task_id>/, and collection
scores each task on its own verified outputs. Images stage once per content
hash into images/ via a single long-lived stream and are garbage-collected
only by an explicit prune; cleanup tombstones a consumed token so its run
directory can never host a second sbatch.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .ssh import CommandResult, RemoteError


class Transport(Protocol):
    def run(
        self,
        remote_argv: Sequence[str],
        *,
        timeout_s: float = 30,
        input_data: bytes = b"",
        max_output: int = 16777216,
    ) -> CommandResult: ...

    def stream(
        self,
        remote_argv: Sequence[str],
        *,
        input_path: Path,
        chunk_bytes: int = 1048576,
        timeout_s: float = 3600,
        max_output: int = 16777216,
    ) -> CommandResult: ...


@dataclass(frozen=True)
class Limits:
    max_nodes: int = 1
    max_wall_seconds: int = 600
    max_inflight: int = 1
    submission_interval: float = 10
    status_interval: float = 30
    max_bundle_bytes: int = 2 * 1024**3
    max_output_bytes: int = 16 * 1024**2
    max_image_bytes: int = 3 * 1024**3
    image_chunk_bytes: int = 1024**2
    image_stream_seconds: float = 3600


def _name(value: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or not re.fullmatch(r"[A-Za-z0-9_./+-]+", value)
    ):
        raise RemoteError("path_invalid")
    if not path.parts or value.startswith("-"):
        raise RemoteError("path_invalid")
    return value


def _token(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{16,64}", value):
        raise RemoteError("token_invalid")
    return value


def _stage_paths(names: Sequence[str]) -> None:
    """Reject filesystem aliases and file/directory or temporary collisions."""
    for name in names:
        if _name(name) != str(PurePosixPath(name)):
            raise RemoteError("bundle_paths_invalid")
    paths = [*names, *(name + ".tmp" for name in names), "ready.json", "ready.json.tmp"]
    occupied = set(paths)
    if len(paths) != len(occupied) or any(
        str(parent) in occupied
        for name in paths
        for parent in PurePosixPath(name).parents
    ):
        raise RemoteError("bundle_paths_invalid")


def _checksum(output: bytes) -> str:
    try:
        value = output.decode("ascii").split()[0]
    except (UnicodeError, IndexError):
        raise RemoteError("checksum_invalid") from None
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        raise RemoteError("checksum_invalid")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(fd, "w") as out:
        json.dump(value, out, sort_keys=True)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class SlurmRunner:
    """Stage/submit/recover/status/cancel/collect plus image stage and cleanup."""

    def __init__(
        self,
        transport: Transport,
        *,
        remote_root: str,
        journal_root: Path,
        user: str,
        profile_id: str,
        limits: Limits = Limits(),
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not remote_root.startswith("/") or ".." in PurePosixPath(remote_root).parts:
            raise RemoteError("path_invalid")
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", remote_root):
            raise RemoteError("path_invalid")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,63}", user) or not profile_id:
            raise RemoteError("profile_invalid")
        self.transport, self.remote_root = transport, remote_root.rstrip("/")
        self.journal_root, self.user, self.profile_id = journal_root, user, profile_id
        self.limits, self.clock = limits, clock
        if (
            limits.max_nodes < 1
            or limits.max_wall_seconds < 1
            or limits.max_inflight < 1
            or limits.status_interval < 0
            or limits.submission_interval < 0
            or limits.max_bundle_bytes < 1
            or limits.max_output_bytes < 1
            or limits.max_image_bytes < 1
            or limits.image_chunk_bytes < 1
            or limits.image_stream_seconds < 1
        ):
            raise RemoteError("limits_invalid")
        journal_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (
            journal_root.is_symlink()
            or journal_root.resolve() != journal_root.absolute()
            or journal_root.stat().st_uid != os.getuid()
            or stat.S_IMODE(journal_root.stat().st_mode) != 0o700
        ):
            raise RemoteError("journal_invalid")

    @contextmanager
    def _lock(self, name: str = "lock") -> Iterator[None]:
        fd = os.open(
            self.journal_root / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _load(self, token: str) -> dict[str, Any]:
        state = json.loads(
            (self.journal_root / (_token(token) + ".json")).read_text(encoding="utf-8")
        )
        if state.get("profile_id") != self.profile_id or state.get("user") != self.user:
            raise RemoteError("handle_invalid")
        return dict(state)

    def _save(self, state: dict[str, Any]) -> None:
        _atomic_json(self.journal_root / (_token(state["token"]) + ".json"), state)

    def _run(self, argv: Sequence[str], **kwargs: Any) -> bytes:
        result = self.transport.run(argv, **kwargs)
        if result.returncode:
            raise RemoteError("remote_command_failed")
        return result.stdout

    def stage(
        self, bundle: Mapping[str, bytes], hashes: Mapping[str, str]
    ) -> dict[str, Any]:
        bundle_bytes = sum(map(len, bundle.values()))
        if (
            not bundle
            or set(bundle) != set(hashes)
            or bundle_bytes > self.limits.max_bundle_bytes
        ):
            raise RemoteError("bundle_invalid")
        _stage_paths(list(bundle))
        for name, data in bundle.items():
            if hashlib.sha256(data).hexdigest() != hashes[name]:
                raise RemoteError("artifact_hash_mismatch")
        stage_id = hashlib.sha256(
            json.dumps(dict(hashes), sort_keys=True).encode()
        ).hexdigest()
        directory = f"{self.remote_root}/stage-{stage_id}"
        manifest = self.journal_root / ("stage-" + stage_id + ".manifest")
        with self._lock():
            # A failed restage cannot retain an earlier local verified inventory.
            _atomic_json(manifest, {"stage_id": stage_id})
        self._run(["mkdir", "-p", "-m", "700", directory])
        self._run(
            ["rm", "-f", "--", f"{directory}/ready.json", f"{directory}/ready.json.tmp"]
        )
        for name, data in bundle.items():
            path = f"{directory}/{name}"
            self._run(["mkdir", "-p", "-m", "700", str(PurePosixPath(path).parent)])
            script = 'umask 077; cat > "$1.tmp" && mv "$1.tmp" "$1"'
            self._run(
                ["sh", "-c", script, "stage", path], input_data=data, timeout_s=120
            )
            actual = _checksum(self._run(["sha256sum", path]))
            if actual != hashes[name]:
                raise RemoteError("artifact_hash_mismatch")
        # Verify the complete inventory after the last upload, not just each
        # artifact immediately after its own write.
        for name, expected in hashes.items():
            if _checksum(self._run(["sha256sum", f"{directory}/{name}"])) != expected:
                raise RemoteError("artifact_hash_mismatch")
        stage = {
            "stage_id": stage_id,
            "remote_dir": directory,
            "hashes": dict(hashes),
            "bundle_bytes": bundle_bytes,
            "max_bundle_bytes": self.limits.max_bundle_bytes,
        }
        # Ready marker is written only after every upload was hash verified.
        self._run(
            [
                "sh",
                "-c",
                'umask 077; cat > "$1.tmp" && mv "$1.tmp" "$1"',
                "stage",
                f"{directory}/ready.json",
            ],
            input_data=json.dumps(stage, sort_keys=True).encode(),
        )
        with self._lock():
            _atomic_json(manifest, stage)
        return stage

    def _resources(self, resources: Mapping[str, Any]) -> list[str]:
        for key in ("account", "partition", "qos"):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", str(resources.get(key, ""))):
                raise RemoteError("resources_invalid")
        try:
            numbers = {
                key: int(resources.get(key, 0))
                for key in ("nodes", "cpus", "gpus", "memory_mb", "wall_seconds")
            }
        except (ValueError, TypeError):
            raise RemoteError("resources_invalid") from None
        if (
            not 1 <= numbers["nodes"] <= self.limits.max_nodes
            or not 1 <= numbers["wall_seconds"] <= self.limits.max_wall_seconds
        ):
            raise RemoteError("budget_exceeded")
        if (
            any(numbers[key] < 1 for key in ("cpus", "memory_mb"))
            or not 0 <= numbers["gpus"] <= 4
        ):
            raise RemoteError("resources_invalid")
        seconds = numbers["wall_seconds"]
        flags = [f"--{key}={resources[key]}" for key in ("account", "partition", "qos")]
        flags += [
            f"--nodes={numbers['nodes']}",
            "--exclusive",
            "--ntasks=1",
            f"--cpus-per-task={numbers['cpus']}",
            f"--mem={numbers['memory_mb']}M",
            f"--time={seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}",
        ]
        if numbers["gpus"]:
            flags.append(f"--gpus-per-task={numbers['gpus']}")
        return flags

    def submit(self, job_spec: Mapping[str, Any], token: str) -> dict[str, Any]:
        token = _token(token)
        stage = _token(str(job_spec["stage_id"]))
        script = _name(str(job_spec["script"]))
        # Task ids name per-task output directories on the remote side.
        task_ids = [str(task) for task in job_spec.get("task_ids", [token])]
        if (
            not task_ids
            or len(set(task_ids)) != len(task_ids)
            or not all(
                re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", task)
                for task in task_ids
            )
        ):
            raise RemoteError("tasks_invalid")
        image = job_spec.get("image_sha256")
        if image is not None:
            image = str(image)
            if not re.fullmatch(r"[a-f0-9]{64}", image):
                raise RemoteError("image_invalid")
        resources = dict(job_spec["resources"])
        flags = self._resources(resources)
        job_hash = hashlib.sha256(
            json.dumps(dict(job_spec), sort_keys=True).encode()
        ).hexdigest()
        with self._lock():
            path = self.journal_root / (token + ".json")
            if path.exists():
                state = self._load(token)
                if state["job_hash"] != job_hash:
                    raise RemoteError("intent_conflict")
                return state  # Existing intent is NEVER a second submission.
            stage_path = self.journal_root / ("stage-" + stage + ".manifest")
            if not stage_path.exists():
                raise RemoteError("stage_unverified")
            staged_manifest = json.loads(stage_path.read_text(encoding="utf-8"))
            if (
                staged_manifest.get("stage_id") != stage
                or script not in staged_manifest.get("hashes", {})
                or "tasks.json" not in staged_manifest.get("hashes", {})
            ):
                raise RemoteError("stage_unverified")
            actual_bytes, staged_cap = (
                staged_manifest.get("bundle_bytes"),
                staged_manifest.get("max_bundle_bytes"),
            )
            if (
                not isinstance(actual_bytes, int)
                or not isinstance(staged_cap, int)
                or actual_bytes < 1
                or actual_bytes > staged_cap
            ):
                raise RemoteError("stage_unverified")
            if (
                actual_bytes > self.limits.max_bundle_bytes
                or staged_cap > self.limits.max_bundle_bytes
            ):
                raise RemoteError("bundle_invalid")
            # The verified stage must carry the same task set as the intent;
            # the job script iterates out/<task>/ from staged tasks.json.
            listing = self.transport.run(
                ["cat", f"{self.remote_root}/stage-{stage}/tasks.json"]
            )
            try:
                staged_tasks = json.loads(listing.stdout)
            except (json.JSONDecodeError, UnicodeError):
                staged_tasks = None
            if listing.returncode or not isinstance(staged_tasks, list):
                raise RemoteError("stage_unverified")
            if [str(task) for task in staged_tasks] != task_ids:
                raise RemoteError("tasks_invalid")
            if image is not None:
                image_manifest = self.journal_root / ("image-" + image + ".manifest")
                staged_image = (
                    json.loads(image_manifest.read_text(encoding="utf-8"))
                    if image_manifest.exists()
                    else {}
                )
                if staged_image.get("image_sha256") != image:
                    raise RemoteError("image_unverified")
            active = [
                json.loads(p.read_text(encoding="utf-8"))
                for p in self.journal_root.glob("*.json")
                if p.name != "limits.json"
            ]
            if (
                sum(
                    s.get("phase")
                    not in {"terminal", "collected", "cleaned", "consumed"}
                    for s in active
                )
                >= self.limits.max_inflight
            ):
                raise RemoteError("inflight_limit")
            limits_path = self.journal_root / "limits.json"
            controls = (
                json.loads(limits_path.read_text(encoding="utf-8"))
                if limits_path.exists()
                else {}
            )
            now = self.clock()
            if now - controls.get("last_submit", 0) < self.limits.submission_interval:
                raise RemoteError("submission_paced")
            remote_dir = f"{self.remote_root}/run-{token}"
            state = {
                "token": token,
                "profile_id": self.profile_id,
                "user": self.user,
                "job_hash": job_hash,
                "stage_id": stage,
                "remote_dir": remote_dir,
                "phase": "submitting",
                "state": "submission_unknown",
                "exit_code": None,
                "signal": None,
                "job_id": None,
                "submitted_at": now,
                "resources": resources,
                "bundle_bytes": staged_manifest["bundle_bytes"],
                "max_bundle_bytes": staged_manifest["max_bundle_bytes"],
                "task_ids": task_ids,
                "image_sha256": image,
            }
            self._save(state)  # FSYNC BEFORE ANY external acceptance is possible.
            _atomic_json(limits_path, {**controls, "last_submit": now})
        try:
            staged = f"{self.remote_root}/stage-{stage}/{script}"
            command = [
                "sbatch",
                "--parsable",
                "--no-requeue",
                "--export=NIL",
                *flags,
                f"--job-name=mpx-{token}",
                f"--comment=mpx-{token}",
                f"--chdir={remote_dir}",
                f"--output={remote_dir}/slurm.out",
                f"--error={remote_dir}/slurm.err",
                f"{remote_dir}/job.sh",
                f"{self.remote_root}/stage-{stage}",
            ]
            if image is not None:
                command.append(f"{self.remote_root}/images/{image}.sif")
            remote_script = (
                # A cleanup tombstone outlives the run dir: a re-polled
                # (job, attempt) token must never reach sbatch again.
                'test -e "$1.done" && exit 73; '
                'mkdir -p -m 700 "$1" && cd "$1" || exit 1; '
                "if test -s receipt; then cat receipt; exit 0; fi; "
                "mkdir submitted.once 2>/dev/null || exit 75; "
                'cp "$2" job.sh || exit 76; '
                + shlex.join(command)
                + " > receipt.tmp; r=$?; "
                'if test "$r" -eq 0; then mv receipt.tmp receipt; cat receipt; fi; exit "$r"'
            )
            reply = self.transport.run(
                ["sh", "-c", remote_script, "submit", remote_dir, staged]
            )
            if reply.returncode == 73:
                raise RemoteError("intent_consumed")
            if reply.returncode:
                raise RemoteError("remote_command_failed")
            receipt = reply.stdout.decode().strip()
            match = re.fullmatch(r"([0-9]+)(?:;[A-Za-z0-9_-]+)?", receipt)
            if not match:
                raise RemoteError("submission_unknown")
            with self._lock():
                state = self._load(token)
                state.update(job_id=match[1], phase="submitted", state="pending")
                self._save(state)
        except (RemoteError, UnicodeError) as error:
            consumed = str(error) == "intent_consumed"
            with self._lock():
                state = self._load(token)
                state["phase"] = "consumed" if consumed else "submission_unknown"
                state["state"] = "consumed" if consumed else "submission_unknown"
                self._save(state)
            if consumed:
                raise
        return state

    def recover(self, intent: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock():
            state = self._load(str(intent["token"]))
        if state.get("job_id"):
            return state
        with self._lock():
            state = self._load(state["token"])
            if (
                self.clock() - state.get("last_recover", 0)
                < self.limits.status_interval
            ):
                return state
            state["last_recover"] = self.clock()
            self._save(state)
        directory = state["remote_dir"]
        result = self.transport.run(["cat", f"{directory}/receipt"])
        matches: set[str] = set()
        if result.returncode == 0:
            match = re.fullmatch(rb"([0-9]+)(?:;[A-Za-z0-9_-]+)?\s*", result.stdout)
            if match:
                matches.add(match[1].decode())
        name = "mpx-" + state["token"]
        queue = self._run(
            [
                "squeue",
                "-h",
                "-u",
                self.user,
                "-A",
                state["resources"]["account"],
                "-n",
                name,
                "-o",
                "%i|%j",
            ]
        )
        for line in queue.decode().splitlines():
            identifier, _, actual_name = line.partition("|")
            if actual_name == name and identifier.isdigit():
                matches.add(identifier)
        from datetime import UTC, datetime

        start = datetime.fromtimestamp(state["submitted_at"] - 60, UTC).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        # The naive --starttime is computed in UTC; sacct reads it in its own
        # TZ, so pin TZ=UTC or the search window slides with remote local time.
        history = self._run(
            [
                "env",
                "TZ=UTC",
                "sacct",
                "-n",
                "-P",
                "-u",
                self.user,
                "--starttime",
                start,
                "-A",
                state["resources"]["account"],
                "--format=JobIDRaw,JobName%128",
            ]
        )
        for line in history.decode().splitlines():
            identifier, _, actual_name = line.partition("|")
            if actual_name == name and identifier.isdigit():
                matches.add(identifier)
        if len(matches) > 1:
            raise RemoteError("submission_conflict")
        with self._lock():
            state = self._load(state["token"])
            if matches:
                state.update(job_id=matches.pop(), phase="submitted", state="pending")
            else:
                state["phase"] = "submission_unknown"
                state["state"] = "submission_unknown"
            self._save(state)
        return state

    def status(self, handle: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock():
            state = self._load(str(handle["token"]))
            if not state.get("job_id"):
                return state
            if self.clock() - state.get("last_status", 0) < self.limits.status_interval:
                return state
            state["last_status"] = self.clock()
            self._save(state)
        queue = self._run(
            ["squeue", "-h", "-j", state["job_id"], "-u", self.user, "-o", "%i|%T"]
        )
        fields = [
            "JobIDRaw",
            "State",
            "ExitCode",
            "ElapsedRaw",
            "AllocNodes",
            "AllocTRES%1024",
            "Account",
            "Partition",
            "QOS",
            "Start",
            "End",
            "User",
            "JobName%128",
        ]
        history = self._run(
            ["sacct", "-n", "-P", "-j", state["job_id"], "--format=" + ",".join(fields)]
        )
        rows = [
            dict(zip([f.split("%")[0] for f in fields], line.split("|"), strict=True))
            for line in history.decode().splitlines()
            if line
        ]
        root = next((row for row in rows if row["JobIDRaw"] == state["job_id"]), None)
        if root and (
            root["User"] != self.user
            or root["Account"] != state["resources"]["account"]
            or root["JobName"] != "mpx-" + state["token"]
        ):
            raise RemoteError("scheduler_identity_mismatch")
        with self._lock():
            state = self._load(state["token"])
            state["accounting"] = rows
            if root:
                state["scheduler_state"] = root["State"].split()[0].rstrip("+")
                try:
                    code, signal = root["ExitCode"].split(":")
                    state["exit_code"], state["signal"] = int(code), int(signal)
                except (ValueError, TypeError):
                    raise RemoteError("accounting_invalid") from None
                state["state"] = {
                    "COMPLETED": "succeeded",
                    "TIMEOUT": "timeout",
                    "CANCELLED": "cancelled",
                    "PENDING": "pending",
                    "RUNNING": "running",
                    "PREEMPTED": "preempted",
                    "NODE_FAIL": "preempted",
                    "BOOT_FAIL": "preempted",
                }.get(state["scheduler_state"], "failed")
                if state["state"] == "succeeded" and (
                    state["exit_code"] or state["signal"]
                ):
                    state["state"] = "failed"
                if state["scheduler_state"] in {
                    "COMPLETED",
                    "FAILED",
                    "TIMEOUT",
                    "CANCELLED",
                    "OUT_OF_MEMORY",
                    "NODE_FAIL",
                    "PREEMPTED",
                    "BOOT_FAIL",
                    "DEADLINE",
                    "REVOKED",
                }:
                    state["phase"] = "terminal"
            elif queue.strip():
                for line in queue.decode().splitlines():
                    identifier, _, scheduler = line.partition("|")
                    if identifier == state["job_id"]:
                        state["scheduler_state"] = scheduler
                        state["state"] = (
                            "running" if scheduler == "RUNNING" else "pending"
                        )
            else:
                state["scheduler_state"] = "accounting_pending"
                state["state"] = "pending"
            self._save(state)
        return state

    def cancel(self, handle: Mapping[str, Any]) -> dict[str, Any]:
        state = self.recover(handle)
        if not state.get("job_id"):
            raise RemoteError("submission_unknown")
        self._run(
            [
                "scancel",
                "--user",
                self.user,
                "--account",
                state["resources"]["account"],
                "--name",
                "mpx-" + state["token"],
                state["job_id"],
            ]
        )
        with self._lock():
            state = self._load(state["token"])
            state["cancel_requested"] = True
            self._save(state)
        return state  # A request is not proof of terminal cancellation.

    def collect(
        self,
        handle: Mapping[str, Any],
        outputs: Mapping[str, Sequence[str]],
        logs: Sequence[str] = (),
    ) -> dict[str, Any]:
        # One output name set per task; a missing output fails only its task.
        requested = {
            str(task): [_name(name) for name in names]
            for task, names in outputs.items()
        }
        log_names = [_name(name) for name in logs]
        with self._lock():
            state = self._load(str(handle["token"]))
        if set(requested) != set(state.get("task_ids", [])):
            raise RemoteError("tasks_invalid")
        files: dict[str, bytes] = {}
        missing: list[str] = []
        missing_tasks: set[str] = set()

        def fetch(path: str) -> bytes | None:
            result = self.transport.run(
                ["cat", path], max_output=self.limits.max_output_bytes
            )
            if result.returncode == 255:
                raise RemoteError("transport_unavailable")
            if result.returncode:
                return None
            if len(result.stdout) > self.limits.max_output_bytes:
                raise RemoteError("output_limit")
            try:
                checksum = _checksum(self._run(["sha256sum", path]))
            except RemoteError as error:
                if str(error) == "remote_command_failed":
                    raise RemoteError("output_checksum_failed") from None
                raise
            if hashlib.sha256(result.stdout).hexdigest() != checksum:
                raise RemoteError("output_hash_mismatch")
            return result.stdout

        try:
            state = self.status(handle)
            if state["phase"] not in {"terminal", "collected"}:
                raise RemoteError("job_not_terminal")
            for task, names in requested.items():
                for name in names:
                    key = f"{task}/{name}"
                    data = fetch(f"{state['remote_dir']}/out/{task}/{name}")
                    if data is None:
                        missing.append(key)
                        missing_tasks.add(task)
                    else:
                        files[key] = data
            for name in log_names:
                data = fetch(f"{state['remote_dir']}/{name}")
                if data is None:
                    missing.append(name)
                else:
                    files[name] = data
        except (RemoteError, UnicodeError) as error:
            if str(error) == "job_not_terminal":
                raise
            code = str(error)
            if code not in {
                "transport_timeout",
                "transport_unavailable",
                "output_limit",
                "output_checksum_failed",
                "output_hash_mismatch",
                "checksum_invalid",
                "remote_command_failed",
                "scheduler_identity_mismatch",
                "accounting_invalid",
            }:
                code = "collection_transport_failed"
            invalid = code in {
                "output_limit",
                "output_hash_mismatch",
                "checksum_invalid",
            }
            with self._lock():
                state = self._load(state["token"])
                previous = state.get("collection", {})
                state["collection"] = {
                    "status": "failed" if invalid else "pending",
                    "error": code,
                    "last_error": code,
                    "attempts": previous.get("attempts", 0) + 1,
                    "failed_attempts": previous.get("failed_attempts", 0) + 1,
                    "at": self.clock(),
                }
                state["outcomes"] = {
                    task: "invalid_output" if invalid else "collection_pending"
                    for task in state["task_ids"]
                }
                state["missing"] = missing
                state["output_hashes"] = {
                    **state.get("output_hashes", {}),
                    **{
                        name: hashlib.sha256(data).hexdigest()
                        for name, data in files.items()
                    },
                }
                self._save(state)
            raise RemoteError(code) from None
        scheduler = str(state.get("scheduler_state", "unknown"))
        classification = {
            "TIMEOUT": "timeout",
            "CANCELLED": "cancelled",
            "PREEMPTED": "preempted",
            "NODE_FAIL": "preempted",
            "BOOT_FAIL": "preempted",
        }.get(scheduler, "failed")
        completed = scheduler == "COMPLETED" and not (
            state.get("exit_code") or state.get("signal")
        )
        # A task with every declared output hash-verified is collected even when
        # the job ended preempted or failed; only the remainder reruns (B2).
        outcomes = {}
        for task in state["task_ids"]:
            if task in missing_tasks:
                outcomes[task] = "missing_output" if completed else classification
            elif requested[task] or completed:
                outcomes[task] = "collected"
            else:
                outcomes[task] = classification
        with self._lock():
            state = self._load(state["token"])
            previous = state.get("collection", {})
            state.update(
                phase="collected",
                outcomes=outcomes,
                missing=missing,
                output_hashes={
                    name: hashlib.sha256(data).hexdigest()
                    for name, data in files.items()
                },
                collection={
                    "status": "complete",
                    "error": None,
                    "last_error": previous.get("last_error"),
                    "attempts": previous.get("attempts", 0) + 1,
                    "failed_attempts": previous.get("failed_attempts", 0),
                    "at": self.clock(),
                },
            )
            self._save(state)
        return {
            "files": files,
            "outcomes": outcomes,
            "accounting": state.get("accounting", []),
        }

    def stage_image(self, path: Path, sha256: str) -> dict[str, Any]:
        """Stream one local image to images/<sha256>.sif; kept between jobs.

        One long-lived transport process carries the file, so a ~60 s upload
        identity only has to be alive when that process connects; chunked
        per-call appends would re-authenticate mid-upload. The .tmp upload
        target is removed on any failure.
        """
        if not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise RemoteError("image_invalid")
        source = Path(path)
        try:
            if source.is_symlink() or not source.is_file():
                raise OSError("not a regular file")
            size = source.stat().st_size
        except OSError:
            raise RemoteError("image_invalid") from None
        if size < 1:
            raise RemoteError("image_invalid")
        if size > self.limits.max_image_bytes:
            raise RemoteError("image_too_large")
        digest = hashlib.sha256()
        with open(source, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024**2), b""):
                digest.update(chunk)
        if digest.hexdigest() != sha256:
            raise RemoteError("image_hash_mismatch")
        images = f"{self.remote_root}/images"
        final = f"{images}/{sha256}.sif"
        temporary = final + ".tmp"
        record = {
            "image_sha256": sha256,
            "remote_path": final,
            "image_bytes": size,
            "max_image_bytes": self.limits.max_image_bytes,
        }
        manifest = self.journal_root / ("image-" + sha256 + ".manifest")
        with self._lock("image-" + sha256 + ".lock"):
            probe = self.transport.run(["sha256sum", final])
            if probe.returncode == 0:
                if _checksum(probe.stdout) != sha256:
                    raise RemoteError("image_hash_mismatch")
                self._run(["rm", "-f", "--", temporary])
                with self._lock():
                    _atomic_json(manifest, record)
                return {**record, "staged": False}
            self._run(["mkdir", "-p", "-m", "700", images])
            try:
                self.transport.stream(
                    ["sh", "-c", 'umask 077; cat > "$1"', "image", temporary],
                    input_path=source,
                    chunk_bytes=self.limits.image_chunk_bytes,
                    timeout_s=self.limits.image_stream_seconds,
                )
                if _checksum(self._run(["sha256sum", temporary])) != sha256:
                    raise RemoteError("image_hash_mismatch")
                self._run(["mv", "-f", "--", temporary, final])
            except Exception:
                try:
                    self.transport.run(["rm", "-f", "--", temporary])
                except RemoteError:
                    pass
                raise
        with self._lock():
            _atomic_json(manifest, record)
        return {**record, "staged": True}

    def cleanup(self, handle: Mapping[str, Any]) -> dict[str, Any]:
        """Tombstone and remove a collected run dir; drop a shared stage last.

        Removal paths are always derived from remote_root plus validated ids;
        journaled or handle-supplied paths are never trusted for deletion.
        """
        token = _token(str(handle["token"]))
        remote_dir = f"{self.remote_root}/run-{token}"
        with self._lock():
            state = self._load(token)
            if state.get("collection", {}).get("status") != "complete":
                raise RemoteError("not_collected")
            stage_id = _token(str(state["stage_id"]))
        # The tombstone is written before the run dir goes away: a resubmit of
        # this token must still refuse even with the run dir and journal gone.
        marker = json.dumps(
            {"token": token, "job_id": state.get("job_id"), "at": self.clock()},
            sort_keys=True,
        ).encode()
        self._run(
            ["sh", "-c", 'umask 077; cat > "$1.done"', "cleanup", remote_dir],
            input_data=marker,
        )
        self._run(["rm", "-rf", "--", remote_dir])
        with self._lock():
            keep_stage = False
            for entry in self.journal_root.glob("*.json"):
                if entry.name in {"limits.json", token + ".json"}:
                    continue
                try:
                    other = json.loads(entry.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    keep_stage = True
                    break
                if (
                    other.get("stage_id") == stage_id
                    and other.get("collection", {}).get("status") != "complete"
                ):
                    keep_stage = True
                    break
        if not keep_stage:
            self._run(["rm", "-rf", "--", f"{self.remote_root}/stage-{stage_id}"])
        with self._lock():
            state = self._load(token)
            state["phase"] = "cleaned"
            state["cleanup"] = {
                "at": self.clock(),
                "stage_removed": not keep_stage,
            }
            self._save(state)
            if not keep_stage:
                (self.journal_root / ("stage-" + stage_id + ".manifest")).unlink(
                    missing_ok=True
                )
        return state

    def prune_images(
        self, keep: Sequence[str], *, older_than_days: int = 7
    ) -> dict[str, Any]:
        """Explicit image GC; cleanup never touches images/.

        Removes only images/<sha>.sif that are absent from `keep`, older than
        `older_than_days`, and named by no journal entry whose collection has
        not completed; expired .sif.tmp partials go too.
        """
        keep_set = set()
        for item in keep:
            item = str(item)
            if not re.fullmatch(r"[a-f0-9]{64}", item):
                raise RemoteError("image_invalid")
            keep_set.add(item)
        if type(older_than_days) is not int or older_than_days < 0:
            raise RemoteError("image_invalid")
        images = f"{self.remote_root}/images"
        with self._lock():
            referenced = set()
            for entry in self.journal_root.glob("*.json"):
                if entry.name == "limits.json":
                    continue
                try:
                    state = json.loads(entry.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    raise RemoteError("journal_invalid") from None
                if state.get("collection", {}).get("status") == "complete":
                    continue
                image = state.get("image_sha256")
                if image:
                    referenced.add(str(image))
        listing = self.transport.run(
            [
                "find",
                images,
                "-maxdepth",
                "1",
                "-type",
                "f",
                "-mtime",
                f"+{older_than_days}",
                "-printf",
                "%f\\n",
            ]
        )
        removed: list[str] = []
        kept: list[str] = []
        if listing.returncode == 0:
            for name in listing.stdout.decode().splitlines():
                match = re.fullmatch(r"([a-f0-9]{64})\.sif(\.tmp)?", name)
                if not match:
                    continue
                sha, is_tmp = match[1], bool(match[2])
                if not is_tmp and (sha in keep_set or sha in referenced):
                    kept.append(name)
                    continue
                self._run(["rm", "-f", "--", f"{images}/{name}"])
                removed.append(name)
                if not is_tmp:
                    (self.journal_root / ("image-" + sha + ".manifest")).unlink(
                        missing_ok=True
                    )
        return {"removed": removed, "kept": kept}

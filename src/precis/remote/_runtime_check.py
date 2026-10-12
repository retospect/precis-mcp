"""Standalone Python3.6-compatible compute metadata checks, no Precis imports.

Only classified errors and selected neutral observations reach stdout. The
workload producer owns the durable report. This checker never installs packages,
loads a model, submits a job or creates another allocation. A matched immutable
uv binary is copied to the private run path only after every precondition passes.
"""

import calendar
import hashlib
import json
import os
import platform
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Dict  # noqa: UP035 - standalone baseline Python3.6


class CheckError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise CheckError(code)


def version(value):
    require(bool(re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", value)), "version_invalid")
    return tuple(int(part) for part in value.split("."))


def bounded_command(argv):
    """Selected metadata commands only; stderr discarded, stdout<=16KiB/4s."""
    try:
        p = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        raise CheckError("command_unavailable") from None
    data = bytearray()
    deadline = time.monotonic() + 4
    try:
        assert p.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(p.stdout, selectors.EVENT_READ)
            while selector.get_map():
                require(time.monotonic() < deadline, "command_timeout")
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    data.extend(chunk)
                    require(len(data) <= 16384, "command_output_limit")
        try:
            p.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise CheckError("command_timeout") from None
        require(p.returncode == 0, "command_failed")
        return data.decode("utf-8")
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
        if p.stdout is not None:
            p.stdout.close()


def duration(value):
    days, sep, clock = value.partition("-")
    if not sep:
        days, clock = "0", days
    fields = clock.split(":")
    require(
        len(fields) == 3 and days.isdigit() and all(x.isdigit() for x in fields),
        "scheduler_time_invalid",
    )
    return (
        int(days) * 86400 + int(fields[0]) * 3600 + int(fields[1]) * 60 + int(fields[2])
    )


def checksum(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def timestamp(value):
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        raise CheckError("scheduler_time_invalid") from None


def evaluate(plan, stage, run, *, env=None, command=None):
    """Return neutral observed-vs-expected evidence; no raw exceptions/outputs."""
    observed: Dict[str, Any] = {}  # noqa: UP006 - baseline Python3.6
    error = None
    env = os.environ if env is None else env
    command = bounded_command if command is None else command
    started = time.monotonic()
    try:
        job_id = env.get("SLURM_JOB_ID", "")
        require(bool(re.fullmatch(r"[0-9]+", job_id)), "allocation_required")
        require(
            all(
                os.access(path, os.X_OK)
                for path in ("/bin/bash", "/bin/mv", "/bin/chmod", "/bin/mkdir")
            ),
            "runtime_primitive_missing",
        )
        require(plan["module_policy"] == "none", "module_policy_invalid")
        output = command(["/usr/bin/scontrol", "-o", "show", "job", job_id])
        jobs = [line for line in output.splitlines() if line.strip()]
        require(len(jobs) == 1, "scheduler_identity_invalid")
        job = dict(part.split("=", 1) for part in jobs[0].split() if "=" in part)
        resources = plan["resources"]
        require(job.get("JobId") == job_id, "scheduler_identity_invalid")
        owner = re.fullmatch(r"[A-Za-z0-9_.-]+\(([0-9]+)\)", job.get("UserId", ""))
        require(
            owner is not None and int(owner.group(1)) == os.getuid(),
            "scheduler_identity_invalid",
        )
        require(
            job.get("JobState") == "RUNNING"
            and job.get("NodeList") == platform.node().split(".")[0],
            "scheduler_identity_invalid",
        )
        for name, key in (
            ("account", "Account"),
            ("partition", "Partition"),
            ("qos", "QOS"),
        ):
            require(job.get(key) == resources[name], "scheduler_resources_mismatch")
        require(
            int(job["NumNodes"]) == resources["nodes"] == 1
            and int(job["NumCPUs"]) == resources["cpus"],
            "scheduler_resources_mismatch",
        )
        tres = dict(
            part.split("=", 1) for part in job["AllocTRES"].split(",") if "=" in part
        )
        memory = tres.get("mem", "")
        memory_match = re.fullmatch(r"([0-9]+)([MG])", memory)
        require(memory_match is not None, "scheduler_resources_mismatch")
        assert memory_match is not None
        memory_mb = int(memory_match.group(1)) * (
            1024 if memory_match.group(2) == "G" else 1
        )
        require(
            memory_mb == resources["memory_mb"]
            and int(tres.get("gres/gpu", "0")) == resources["gpus"],
            "scheduler_resources_mismatch",
        )
        wall = duration(job["TimeLimit"])
        require(
            wall == resources["wall_seconds"] and wall <= 600,
            "scheduler_resources_mismatch",
        )
        remaining = wall - duration(job["RunTime"])
        # The sourced hook freezes TZ=UTC and removes Slurm's formatting override.
        # These are scheduler timestamps, never values copied from the plan.
        start_unix = timestamp(job["StartTime"])
        end_unix = timestamp(job["EndTime"])
        require(
            0 < end_unix - start_unix <= wall and start_unix <= time.time(),
            "scheduler_time_invalid",
        )
        remaining = min(remaining, int(end_unix - time.time()))
        require(remaining >= plan["min_remaining_seconds"], "insufficient_time")
        observed["allocation"] = {
            "job_id": job_id,
            "resources": {
                "account": job["Account"],
                "partition": job["Partition"],
                "qos": job["QOS"],
                "nodes": int(job["NumNodes"]),
                "cpus": int(job["NumCPUs"]),
                "gpus": int(tres["gres/gpu"]),
                "memory_mb": memory_mb,
                "wall_seconds": wall,
            },
            "start_unix": start_unix,
            "end_unix": end_unix,
            "remaining_seconds": remaining,
        }
        storage = plan["storage"]
        run = os.path.abspath(run)
        root = os.path.realpath(storage["root"])
        require(
            os.path.commonpath([os.path.realpath(run), root]) == root
            and os.path.realpath(run) != root,
            "run_path_invalid",
        )
        # The root can contain a documented provider symlink; private descendants cannot.
        relative = os.path.relpath(run, storage["root"])
        require(not relative.startswith(".."), "run_path_invalid")
        cursor = storage["root"]
        for part in relative.split(os.sep):
            cursor = os.path.join(cursor, part)
            require(not os.path.islink(cursor), "run_path_invalid")
        info = os.stat(run)
        require(
            stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid(),
            "run_path_invalid",
        )
        require(stat.S_IMODE(info.st_mode) == 0o700, "run_permissions_invalid")
        filesystem = os.statvfs(run)
        free_bytes, free_inodes = (
            filesystem.f_bavail * filesystem.f_frsize,
            filesystem.f_favail,
        )
        observed["storage"] = {
            "available_bytes": free_bytes,
            "available_inodes": free_inodes,
            "filesystem_observation_not_new_entitlement": True,
        }
        require(
            free_bytes >= storage["compute_peak_bytes"] + storage["reserve_bytes"]
            and free_inodes
            >= storage["compute_peak_inodes"] + storage["reserve_inodes"],
            "storage_headroom_insufficient",
        )
        python = plan["python"]
        require(
            os.path.isfile(python["path"])
            and checksum(python["path"]) == python["sha256"],
            "python_hash_mismatch",
        )
        script = (
            "import json,os,platform,importlib.util; print(json.dumps({'python':platform.python_version(),"
            "'system':platform.system(),'machine':platform.machine(),'glibc':os.confstr('CS_GNU_LIBC_VERSION'),"
            "'venv':importlib.util.find_spec('venv') is not None}))"
        )
        metadata = json.loads(command([python["path"], "-I", "-c", script]))
        plat = plan["platform"]
        require(
            set(metadata) == {"python", "system", "machine", "glibc", "venv"},
            "python_abi_mismatch",
        )
        require(
            metadata["python"] == python["version"]
            and metadata["system"] == plat["system"]
            and metadata["machine"] == plat["machine"]
            and metadata["venv"] is True,
            "python_abi_mismatch",
        )
        require(
            isinstance(metadata["glibc"], str)
            and metadata["glibc"].startswith("glibc ")
            and version(metadata["glibc"].split()[1]) >= version(plat["glibc_min"]),
            "glibc_mismatch",
        )
        observed["python"] = metadata
        gpu_lines = (
            command(
                [
                    "/usr/bin/nvidia-smi",
                    "--query-gpu=name,driver_version",
                    "--format=csv,noheader",
                ]
            )
            .strip()
            .splitlines()
        )
        require(len(gpu_lines) == resources["gpus"], "gpu_visibility_mismatch")
        gpus = []
        for line in gpu_lines:
            name, driver = (piece.strip() for piece in line.split(","))
            require(
                len(name) <= 160
                and plat["gpu_name"] in name
                and version(driver) >= version(plat["driver_min"]),
                "gpu_driver_mismatch",
            )
            gpus.append({"name": name, "driver": driver})
        observed["gpus"] = gpus
        uv = plan["uv"]
        source = os.path.join(stage, uv["path"])
        require(
            os.path.commonpath([os.path.realpath(source), os.path.realpath(stage)])
            == os.path.realpath(stage)
            and os.path.isfile(source)
            and not os.path.islink(source),
            "uv_path_invalid",
        )
        require(checksum(source) == uv["sha256"], "uv_hash_mismatch")
        destination = os.path.join(run, ".runtime-tools")
        require(not os.path.lexists(destination), "runtime_tools_conflict")
        os.mkdir(destination, 0o700)
        executable = os.path.join(destination, "uv")
        # Exclusive destination in a newly-created private directory, no symlink overwrite.
        with open(source, "rb") as incoming, open(executable, "xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, 65536)
        os.chmod(executable, 0o700)
        require(checksum(executable) == uv["sha256"], "uv_hash_mismatch")
        uv_version = command([executable, "--version"]).strip()
        require(
            re.fullmatch(
                r"uv " + re.escape(uv["version"]) + r"(?: \([^\r\n]{1,160}\))?",
                uv_version,
            )
            is not None,
            "uv_version_mismatch",
        )
        temporary = os.path.join(run, ".runtime-temp")
        require(not os.path.lexists(temporary), "runtime_temp_conflict")
        os.mkdir(temporary, 0o700)
        observed["uv"] = {
            "version": uv_version.split()[1],
            "sha256": checksum(executable),
        }
        observed["allocation"]["remaining_seconds_before_install"] = (
            remaining - int(time.monotonic() - started) - 1
        )
        require(
            observed["allocation"]["remaining_seconds_before_install"]
            >= plan["min_remaining_seconds"],
            "insufficient_time",
        )
    except CheckError as exc:
        error = str(exc)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        error = "runtime_metadata_invalid"
    return {
        "schema": "remote.runtime.observations.v1",
        "status": "passed" if error is None else "failed",
        "error": error,
        "observed": observed,
    }


def main():
    try:
        with open(sys.argv[1], "rb") as stream:
            payload = stream.read(32769)
        require(len(payload) <= 32768, "runtime_plan_invalid")
        plan = json.loads(payload)
        require(
            platform.python_version() == plan["baseline_python"]["version"],
            "baseline_python_mismatch",
        )
        report = evaluate(plan, sys.argv[2], sys.argv[3])
    except CheckError as exc:
        report = {
            "schema": "remote.runtime.observations.v1",
            "status": "failed",
            "error": str(exc),
            "observed": {},
        }
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        report = {
            "schema": "remote.runtime.observations.v1",
            "status": "failed",
            "error": "runtime_plan_invalid",
            "observed": {},
        }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["status"] == "passed" else 70


if __name__ == "__main__":
    sys.exit(main())

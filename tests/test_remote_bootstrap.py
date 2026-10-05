"""Offline allocation/identity/storage failures and deterministic artifact binding."""

from __future__ import annotations

import copy
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from precis.remote import _runtime_check as checker
from precis.remote.bootstrap import runtime_artifacts
from precis.remote.ssh import RemoteError


@pytest.fixture
def fixture(tmp_path: Path) -> tuple[dict[str, Any], Path, Path]:
    root = tmp_path / "owned"
    stage, run = root / "stage", root / "run"
    stage.mkdir(parents=True)
    run.mkdir(mode=0o700)
    (stage / "tools").mkdir()
    uv = stage / "tools/uv"
    uv.write_bytes(b"synthetic-not-an-executable")
    plan = {
        "schema": "remote.runtime.v1",
        "module_policy": "none",
        "min_remaining_seconds": 540,
        "resources": {
            "account": "p200916",
            "partition": "gpu",
            "qos": "test",
            "nodes": 1,
            "cpus": 128,
            "gpus": 4,
            "memory_mb": 491520,
            "wall_seconds": 600,
        },
        "python": {
            "path": sys.executable,
            "version": "3.12.14",
            "sha256": checker.checksum(sys.executable),
        },
        "uv": {
            "path": "tools/uv",
            "version": "0.12.22",
            "sha256": checker.checksum(str(uv)),
        },
        "baseline_python": {
            "path": sys.executable,
            "version": "3.6.8",
            "sha256": checker.checksum(sys.executable),
        },
        "platform": {
            "system": "Linux",
            "machine": "x86_64",
            "glibc_min": "2.28",
            "driver_min": "580.65.06",
            "gpu_name": "A100",
        },
        "storage": {
            "root": str(root),
            "compute_peak_bytes": 1024,
            "compute_peak_inodes": 4,
            "reserve_bytes": 1024,
            "reserve_inodes": 10,
        },
    }
    return plan, stage, run


def command(argv: list[str]) -> str:
    if "scontrol" in argv[0]:
        now = int(time.time())
        start = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 3))
        end = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now + 597))
        return f"JobId=1 UserId=fixture({os.getuid()}) JobState=RUNNING NodeList={platform.node().split('.')[0]} Account=p200916 Partition=gpu QOS=test NumNodes=1 NumCPUs=128 AllocTRES=cpu=128,mem=480G,node=1,gres/gpu=4 TimeLimit=00:10:00 RunTime=00:00:03 StartTime={start} EndTime={end}"
    if "nvidia-smi" in argv[0]:
        return "NVIDIA A100-SXM4-40GB, 580.65.06\n" * 4
    if argv[-1] == "--version":
        return "uv 0.12.22\n"
    return json.dumps(
        {
            "python": "3.12.14",
            "system": "Linux",
            "machine": "x86_64",
            "glibc": "glibc 2.28",
            "venv": True,
        }
    )


def test_deterministic_source_config_and_compute_gate(fixture: Any) -> None:
    plan, stage, run = fixture
    artifacts = runtime_artifacts(plan)
    assert artifacts == runtime_artifacts(copy.deepcopy(plan))
    assert json.loads(artifacts["runtime-plan.json"]) == plan
    assert b"SBATCH" not in artifacts["runtime-bootstrap.sh"]
    assert b"REMOTE_RUNTIME_CHECK_STATUS" in artifacts["runtime-bootstrap.sh"]
    assert b"REMOTE_RUNTIME_OBSERVATIONS" in artifacts["runtime-bootstrap.sh"]
    assert b'return "$REMOTE_RUNTIME_CHECK_STATUS"' in artifacts["runtime-bootstrap.sh"]
    # Early standalone program remains parseable by the baseline Python3.6.
    import ast

    ast.parse(artifacts["runtime-check.py"], feature_version=(3, 6))
    result = checker.evaluate(
        plan, str(stage), str(run), env={"SLURM_JOB_ID": "1"}, command=command
    )
    assert result["status"] == "passed"
    allocation = result["observed"]["allocation"]
    assert 595 <= allocation["remaining_seconds"] <= 597
    assert allocation["end_unix"] - allocation["start_unix"] == 600
    assert checker.checksum(str(run / ".runtime-tools/uv")) == plan["uv"]["sha256"]
    assert (run / ".runtime-tools/uv").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "change", ["hash", "path", "budget", "wall", "module", "extra"]
)
def test_incomplete_or_unsafe_plan_cannot_render(fixture: Any, change: str) -> None:
    plan, _, _ = fixture
    if change == "hash":
        plan["uv"]["sha256"] = None
    if change == "path":
        plan["uv"]["path"] = "tools/../escape"
    if change == "budget":
        plan["storage"]["compute_peak_inodes"] = None
    if change == "wall":
        plan["resources"]["wall_seconds"] = 601
    if change == "module":
        plan["module_policy"] = "load-default"
    if change == "extra":
        plan["secret"] = "not-accepted"
    with pytest.raises(RemoteError, match="runtime_plan_invalid"):
        runtime_artifacts(plan)


@pytest.mark.parametrize(
    "failure,code",
    [
        ("allocation", "allocation_required"),
        ("account", "scheduler_resources_mismatch"),
        ("node", "scheduler_identity_invalid"),
        ("owner", "scheduler_identity_invalid"),
        ("wall", "insufficient_time"),
        ("timestamps", "scheduler_time_invalid"),
        ("python_hash", "python_hash_mismatch"),
        ("python_version", "python_abi_mismatch"),
        ("glibc", "glibc_mismatch"),
        ("driver", "gpu_driver_mismatch"),
        ("uv_hash", "uv_hash_mismatch"),
        ("storage_bytes", "storage_headroom_insufficient"),
        ("storage_inodes", "storage_headroom_insufficient"),
        ("permissions", "run_permissions_invalid"),
        ("symlink", "run_path_invalid"),
    ],
)
def test_fail_closed_before_runtime_tool_install(
    fixture: Any, failure: str, code: str
) -> None:
    plan, stage, run = fixture
    environment = {"SLURM_JOB_ID": "1"}
    if failure == "allocation":
        environment.clear()
    if failure == "python_hash":
        plan["python"]["sha256"] = "0" * 64
    if failure == "uv_hash":
        plan["uv"]["sha256"] = "0" * 64
    if failure == "storage_bytes":
        plan["storage"]["compute_peak_bytes"] = 10**30
    if failure == "storage_inodes":
        plan["storage"]["compute_peak_inodes"] = 10**30
    if failure == "permissions":
        run.chmod(0o755)
    if failure == "symlink":
        alias = run.parent / "alias"
        alias.symlink_to(run, target_is_directory=True)
        run = alias

    def fail(argv: list[str]) -> str:
        value = command(argv)
        replacements = {
            "account": ("Account=p200916", "Account=other"),
            "node": (
                f"NodeList={platform.node().split('.')[0]}",
                "NodeList=other-node",
            ),
            "owner": (
                f"UserId=fixture({os.getuid()})",
                f"UserId=fixture({os.getuid() + 1})",
            ),
            "wall": ("RunTime=00:00:03", "RunTime=00:02:00"),
            "python_version": ("3.12.14", "3.12.13"),
            "glibc": ("glibc 2.28", "glibc 2.17"),
            "driver": ("580.65.06", "550.10.00"),
        }
        if failure in replacements:
            value = value.replace(*replacements[failure])
        if failure == "timestamps" and "scontrol" in argv[0]:
            value = value.replace("StartTime=", "StartTime=invalid")
        return value

    result = checker.evaluate(plan, str(stage), str(run), env=environment, command=fail)
    assert result["status"] == "failed" and result["error"] == code
    assert not (run / ".runtime-tools").exists()
    assert "synthetic-not-an-executable" not in json.dumps(result)


def test_bounded_metadata_errors_never_echo_stderr() -> None:
    with pytest.raises(checker.CheckError, match="command_failed"):
        checker.bounded_command(
            [
                sys.executable,
                "-c",
                "import sys; print('PRIVATE-CANARY', file=sys.stderr); sys.exit(1)",
            ]
        )
    with pytest.raises(checker.CheckError, match="command_output_limit"):
        checker.bounded_command([sys.executable, "-c", "print('x'*17000)"])


def test_missing_early_report_primitive_stops_before_install(
    fixture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, stage, run = fixture
    original = os.access
    monkeypatch.setattr(
        os, "access", lambda path, mode: path != "/bin/mv" and original(path, mode)
    )
    result = checker.evaluate(
        plan, str(stage), str(run), env={"SLURM_JOB_ID": "1"}, command=command
    )
    assert result["status"] == "failed"
    assert result["error"] == "runtime_primitive_missing"
    assert not (run / ".runtime-tools").exists()


def test_real_sourced_bootstrap_stops_before_model_and_preserves_facts(
    fixture: Any,
) -> None:
    plan, stage, run = fixture
    for name, body in runtime_artifacts(plan).items():
        (stage / name).write_bytes(body)
    script = (
        "set -e; pilot_stage=$1; pilot_run=$2; "
        'if source "$pilot_stage/runtime-bootstrap.sh"; then echo MODEL_STARTED; '
        'else printf "%s" "$REMOTE_RUNTIME_OBSERVATIONS"; exit "$REMOTE_RUNTIME_CHECK_STATUS"; fi'
    )
    result = subprocess.run(
        ["bash", "-c", script, "bootstrap-test", str(stage), str(run)],
        env={**os.environ, "SLURM_JOB_ID": "1"},
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 70
    report = json.loads(result.stdout)
    assert report["error"] == "baseline_python_mismatch"
    assert b"MODEL_STARTED" not in result.stdout
    assert not (run / ".runtime-tools").exists()


@pytest.mark.parametrize("failure", ["job", "stage", "run", "baseline_hash"])
def test_early_sourced_guard_clears_seeded_success_without_running_checker(
    fixture: Any, failure: str
) -> None:
    plan, stage, run = fixture
    if failure == "baseline_hash":
        plan["baseline_python"]["sha256"] = "0" * 64
    for name, body in runtime_artifacts(plan).items():
        (stage / name).write_bytes(body)
    (stage / "runtime-check.py").write_text(
        "from pathlib import Path\nimport sys\n"
        "Path(sys.argv[3], 'CHECKER_STARTED').touch()\n"
    )
    result = _source_seeded_hook(stage, run, failure)
    assert result.returncode == 70
    assert result.stdout.splitlines() == [b"70", b"", b"", b"", b"", b""]
    assert result.stderr == b""
    assert not (run / "CHECKER_STARTED").exists()
    assert not (run / ".runtime-tools").exists()


@pytest.mark.parametrize("checker_rc", [0, 23])
def test_sourced_checker_retains_actual_status_and_replaces_seeded_facts(
    fixture: Any, checker_rc: int
) -> None:
    plan, stage, run = fixture
    for name, body in runtime_artifacts(plan).items():
        (stage / name).write_bytes(body)
    observations = json.dumps({"status": "passed" if checker_rc == 0 else "failed"})
    (stage / "runtime-check.py").write_text(
        f"import sys\nprint({observations!r})\nsys.exit({checker_rc})\n"
    )
    result = _source_seeded_hook(stage, run)
    assert result.returncode == checker_rc
    values = result.stdout.splitlines()
    assert values[:2] == [str(checker_rc).encode(), observations.encode()]
    if checker_rc == 0:
        assert values[2:] == [
            plan["python"]["path"].encode(),
            str(run / ".runtime-tools/uv").encode(),
            str(run / ".runtime-temp").encode(),
            b"1",
        ]
    else:
        assert values[2:] == [b"", b"", b"", b""]
    assert result.stderr == b""


def _source_seeded_hook(
    stage: Path, run: Path, missing: str | None = None
) -> subprocess.CompletedProcess[bytes]:
    # Consumer saves the real source rc before any reporter command can replace it.
    script = (
        "set -e; pilot_stage=$1; pilot_run=$2; "
        'if source "$3/runtime-bootstrap.sh"; then source_rc=0; else source_rc=$?; fi; '
        'printf "%s\\n" "$REMOTE_RUNTIME_CHECK_STATUS" "$REMOTE_RUNTIME_OBSERVATIONS" '
        '"$REMOTE_RUNTIME_PYTHON" "$REMOTE_RUNTIME_UV" "${TMPDIR:-}" '
        '"${PYTHONDONTWRITEBYTECODE:-}"; exit "$source_rc"'
    )
    environment = {
        **os.environ,
        "SLURM_JOB_ID": "1" if missing != "job" else "",
        "REMOTE_RUNTIME_CHECK_STATUS": "0",
        "REMOTE_RUNTIME_OBSERVATIONS": "STALE_SUCCESS_FACTS",
        "REMOTE_RUNTIME_PYTHON": "/stale/python",
        "REMOTE_RUNTIME_UV": "/stale/uv",
        "TMPDIR": "/stale/temp",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [
            "bash",
            "-c",
            script,
            "seeded-bootstrap-test",
            "" if missing == "stage" else str(stage),
            "" if missing == "run" else str(run),
            str(stage),
        ],
        env=environment,
        capture_output=True,
        timeout=10,
    )


def test_metadata_timeout_is_classified_and_reaped() -> None:
    with pytest.raises(checker.CheckError, match="command_timeout"):
        checker.bounded_command([sys.executable, "-c", "import time; time.sleep(10)"])

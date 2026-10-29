"""Dependency preparation must succeed before a replacement MCP child starts.

Run the ensure script's own PREP body and readiness functions with fake
commands under tmp_path. No Docker daemon, server, image or real uv is used.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_ENSURE = (
    Path(__file__).resolve().parents[1] / "deploy/mcp-http/precis-mcp-http-ensure.sh"
)
_REBUILD = "rebuild the image with scripts/mcp-image-refresh"
_LOCK_HASH = "a" * 64
_PROJECT_HASH = "b" * 64

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: executes Bash fragments"
)


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _run(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )


def _prep_rig(tmp_path: Path, mode: str) -> tuple[Path, dict[str, str]]:
    source, app, venv, fakebin = (
        tmp_path / p for p in ("source", "app", "venv", "bin")
    )
    for path in (source, app, venv, fakebin):
        path.mkdir()
    (source / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (source / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    match = re.search(
        r'^cat > /tmp/precis-prepare\.sh <<"PREP"\n(.*?)^PREP$',
        _ENSURE.read_text(encoding="utf-8"),
        re.S | re.M,
    )
    assert match, "PREP heredoc missing from ensure script"
    body = match.group(1)
    for old, new in (("/opt/venv", venv), ("/app", app), ("/src", source)):
        body = body.replace(old, str(new))
    prep = tmp_path / "prepare.sh"
    prep.write_text(body, encoding="utf-8")
    _write_executable(
        fakebin / "tar",
        'case " $* " in\n'
        '  *" -cf "*) printf "archive\\n" ;;\n'
        '  *" -xf "*) cat >/dev/null; cp "$RIG_SOURCE/uv.lock" '
        '"$RIG_SOURCE/pyproject.toml" "$2/" ;;\n'
        "  *) exit 99 ;;\n"
        "esac\n",
    )
    _write_executable(
        fakebin / "sha256sum",
        'case "$1" in\n'
        f'  */uv.lock) printf "{_LOCK_HASH}  %s\\n" "$1" ;;\n'
        f'  */pyproject.toml) printf "{_PROJECT_HASH}  %s\\n" "$1" ;;\n'
        "  *) exit 99 ;;\n"
        "esac\n",
    )
    _write_executable(
        fakebin / "uv",
        'printf "%s\\n" "$*" >> "$RIG_UV_CALLS"\n'
        'case " $* " in\n'
        '  *" --dry-run "*)\n'
        '    if [ "$RIG_MODE" = dry-run-failure ]; then\n'
        '      echo "fake dependency resolution failed" >&2; exit 17\n'
        "    fi\n"
        '    if [ "$RIG_MODE" = noop ]; then\n'
        '      printf " - unchanged==1\\n + unchanged==1\\n"\n'
        "    else\n"
        '      printf " + added==2\\n"\n'
        "    fi ;;\n"
        '  " sync "*)\n'
        '    if [ "$RIG_MODE" = sync-failure ]; then\n'
        '      echo "fake dependency installation failed" >&2; exit 18\n'
        "    fi ;;\n"
        '  " pip install "*) : ;;\n'
        "  *) exit 99 ;;\n"
        "esac\n",
    )
    launcher = tmp_path / "launch.sh"
    _write_executable(
        launcher,
        'bash "$RIG_PREP"\nprintf "started\\n" >> "$RIG_CHILD"\n',
    )
    return launcher, {
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "RIG_SOURCE": str(source),
        "RIG_PREP": str(prep),
        "RIG_CHILD": str(tmp_path / "child-started"),
        "RIG_UV_CALLS": str(tmp_path / "uv-calls"),
        "RIG_MODE": mode,
    }


@pytest.mark.parametrize("mode", ["dry-run-failure", "sync-failure"])
def test_dependency_failure_blocks_child_without_stamping_lock(
    tmp_path: Path, mode: str
) -> None:
    launcher, env = _prep_rig(tmp_path, mode)
    result = _run(launcher, env)
    assert result.returncode == 1, result.stderr
    assert "precis-prepare:" in result.stderr
    assert _REBUILD in result.stderr
    assert not Path(env["RIG_CHILD"]).exists()
    assert not (tmp_path / "venv/.precis-uvlock.sha256").exists()
    assert not (tmp_path / "venv/.precis-pyproject.sha256").exists()
    calls = Path(env["RIG_UV_CALLS"]).read_text(encoding="utf-8").splitlines()
    assert len(calls) == (1 if mode == "dry-run-failure" else 2)
    assert all(call.startswith("sync ") for call in calls)


@pytest.mark.parametrize("mode", ["noop", "sync-success"])
def test_success_stamps_lock_starts_child_and_skips_unchanged_sync(
    tmp_path: Path, mode: str
) -> None:
    launcher, env = _prep_rig(tmp_path, mode)
    result = _run(launcher, env)
    assert result.returncode == 0, result.stderr
    assert (
        (tmp_path / "venv/.precis-uvlock.sha256").read_text(encoding="utf-8").strip()
    ) == _LOCK_HASH
    assert (tmp_path / "venv/.precis-pyproject.sha256").read_text(
        encoding="utf-8"
    ).strip() == _PROJECT_HASH
    calls_file = Path(env["RIG_UV_CALLS"])
    first_calls = calls_file.read_text(encoding="utf-8")
    assert len(first_calls.splitlines()) == (2 if mode == "noop" else 3)
    assert Path(env["RIG_CHILD"]).read_text(encoding="utf-8") == "started\n"
    again = _run(launcher, env)
    assert again.returncode == 0, again.stderr
    assert calls_file.read_text(encoding="utf-8") == first_calls
    assert Path(env["RIG_CHILD"]).read_text(encoding="utf-8") == "started\nstarted\n"


def _function(name: str) -> str:
    match = re.search(
        rf"^{name}\(\) \{{\n.*?^\}}\n",
        _ENSURE.read_text(encoding="utf-8"),
        re.S | re.M,
    )
    assert match, f"{name} function missing from ensure script"
    return match.group(0)


@pytest.mark.parametrize("prepare_output", [True, False])
def test_ready_backend_forwards_only_prepare_logs_to_stderr(
    tmp_path: Path, prepare_output: bool
) -> None:
    runner = tmp_path / "wait-ready.sh"
    _write_executable(
        runner,
        "set -o pipefail\nREADY_TIMEOUT=10\n"
        "running() { return 0; }\n"
        "probe_backend() { printf '401\\n'; }\n"
        "check_python_root_mounts() { return 0; }\n"
        "docker() {\n"
        '  [ "$1" = logs ] || return 99\n'
        "  printf 'unrelated server stdout\\n'\n"
        "  printf 'unrelated server stderr\\n' >&2\n"
        '  if [ "$RIG_PREPARE_OUTPUT" = yes ]; then\n'
        "    printf 'precis-prepare: dependency sync complete\\n'\n"
        "    printf 'precis-prepare: autocatpath SKEW: rebuild image\\n' >&2\n"
        "  fi\n"
        "}\n"
        + _function("prepare_logs")
        + _function("wait_backend_ready")
        + 'wait_backend_ready "test-backend"\n',
    )
    result = _run(
        runner,
        {
            "PATH": os.environ["PATH"],
            "RIG_PREPARE_OUTPUT": "yes" if prepare_output else "no",
        },
    )
    assert result.returncode == 0, result.stderr
    assert not result.stdout
    assert result.stderr.splitlines() == (
        [
            "precis-prepare: dependency sync complete",
            "precis-prepare: autocatpath SKEW: rebuild image",
        ]
        if prepare_output
        else []
    )

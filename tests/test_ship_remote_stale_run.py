"""A cancelled check.yml run must not wedge ship retries for the same sha."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SHIP = Path(__file__).resolve().parents[1] / "scripts" / "ship"
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)


def _remote_gate_function() -> str:
    source = SHIP.read_text(encoding="utf-8")
    match = re.search(r"^remote_gate_wait\(\) \{\n.*?^\}\n", source, re.S | re.M)
    assert match, "remote_gate_wait not found in scripts/ship"
    return match.group(0)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def _repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    origin.mkdir()
    repo.mkdir()
    _git(origin, "init", "--bare", "-q")
    _git(repo, "init", "-q", "-b", "test")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "README").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/ci/test")
    return repo


def _gh_stub(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "state = Path(os.environ['GH_STATE'])\n"
        "count = int(state.read_text(encoding='utf-8')) if state.exists() else 0\n"
        "if args[:2] == ['run', 'list']:\n"
        "    print('123')\n"
        "elif args[:2] == ['run', 'view']:\n"
        "    field = args[args.index('--json') + 1]\n"
        "    if field == 'url':\n"
        "        print('https://example.invalid/run/123')\n"
        "    elif field == 'status,conclusion':\n"
        "        if os.environ.get('GH_ALWAYS_CANCELLED') == '1' or count == 0:\n"
        "            print('completed cancelled')\n"
        "        else:\n"
        "            print('completed success')\n"
        "elif args[:2] == ['run', 'rerun']:\n"
        "    state.write_text(str(count + 1), encoding='utf-8')\n"
        "else:\n"
        "    raise SystemExit(f'unexpected gh args: {args!r}')\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    return bin_dir


def _run_gate(
    tmp_path: Path, *, always_cancelled: bool = False
) -> tuple[subprocess.CompletedProcess[str], int]:
    repo = _repo(tmp_path)
    bin_dir = _gh_stub(tmp_path)
    state = tmp_path / "reruns"
    shell = (
        "set -euo pipefail\n"
        "BRANCH=test\n"
        'say() { printf "%s\\n" "$*"; }\n'
        "sleep() { :; }\n" + _remote_gate_function() + "remote_gate_wait\n"
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_STATE": str(state),
        "PRECIS_REMOTE_CI_TIMEOUT_MIN": "1",
    }
    if always_cancelled:
        env["GH_ALWAYS_CANCELLED"] = "1"
    proc = subprocess.run(
        ["bash", "-c", shell],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    reruns = int(state.read_text(encoding="utf-8")) if state.exists() else 0
    return proc, reruns


def test_cancelled_run_is_rerun_for_the_same_sha(tmp_path: Path) -> None:
    proc, reruns = _run_gate(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert reruns == 1
    assert "remote gate GREEN" in proc.stdout
    assert "remote gate RED" not in proc.stderr


def test_failed_gate_is_not_retried(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    bin_dir = _gh_stub(tmp_path)
    state = tmp_path / "reruns"
    gh = bin_dir / "gh"
    source = gh.read_text(encoding="utf-8").replace(
        "print('completed cancelled')", "print('completed failure')"
    )
    gh.write_text(source, encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_STATE": str(state),
        "PRECIS_REMOTE_CI_TIMEOUT_MIN": "1",
    }
    shell = (
        "set -euo pipefail\n"
        "BRANCH=test\n"
        'say() { printf "%s\\n" "$*"; }\n'
        "sleep() { :; }\n" + _remote_gate_function() + "remote_gate_wait\n"
    )
    proc = subprocess.run(
        ["bash", "-c", shell],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode != 0
    assert "remote gate RED: failure" in proc.stderr
    assert not state.exists()


def test_repeated_cancellation_has_a_bounded_retry_count(tmp_path: Path) -> None:
    proc, reruns = _run_gate(tmp_path, always_cancelled=True)
    assert proc.returncode != 0
    assert reruns == 2
    assert "stayed cancelled after 2 rerun attempts" in proc.stderr

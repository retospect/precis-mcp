"""``scripts/nightly`` status classification (gr508691).

A nightly run whose ``scripts/test`` refuses BEFORE pytest runs — the disk
preflight's exit 3, a missing compose file, the in-container stale-image
checks — must record an environment failure, not a RED that reads as suite
breakage. A RED still means real, reproducible test failures.

Both run the REAL ``scripts/nightly``, staged into a throwaway git repo,
against synthetic ``docker`` and ``scripts/test`` on PATH. No daemon and no
suite are contacted.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the shipped bash scripts + fake docker are POSIX-only",
)

REPO = Path(__file__).resolve().parent.parent

_FAKE_DOCKER = r"""#!/usr/bin/env bash
# Only `docker info` is called by scripts/nightly's preflight.
# FAKE_DOCKER_INFO_FAIL=1 makes the daemon read as unavailable.
if [ "${1:-}" = info ]; then
    [ -n "${FAKE_DOCKER_INFO_FAIL:-}" ] && exit 1
    exit 0
fi
exit 0
"""

# scripts/test refusing at the disk preflight — the gripe's exact failure
# (its exit 3 reaches nightly before any pytest output exists).
_TEST_DISK_REFUSAL = r"""#!/usr/bin/env bash
echo "scripts/test: disk probe failed: docker run timed out after 30s; this is not a disk-full verdict. Re-run, or PRECIS_DOCKER_DISK_CHECK=0 to skip the probe" >&2
exit 3
"""

_TEST_STALE_IMAGE = r"""#!/usr/bin/env bash
echo "stale precis-dev image — missing core dependency import(s): rdkit" >&2
exit 1
"""

# pytest itself exiting 3 (internal error): also a failure before test ids
# exist, but it is NOT a wrapper refusal — dep drift breaking collection is
# the drift signal nightly exists to catch, so it must stay RED.
_TEST_PYTEST_INTERNAL = r"""#!/usr/bin/env bash
echo "INTERNALERROR> Traceback (most recent call last):"
echo "INTERNALERROR>   File \"conftest.py\", line 9, in <module>"
echo "INTERNALERROR> ImportError: cannot import name 'gone'"
exit 3
"""

_TEST_RED = r"""#!/usr/bin/env bash
echo "FAILED tests/test_x.py::test_a - AssertionError: nope"
echo "===== 1 failed in 0.05s ====="
exit 1
"""

# The non-fatal lock-pin warning also says "stale precis-dev image" but the
# run continued — a later real failure must still record RED.
_TEST_WARNING_THEN_RED = r"""#!/usr/bin/env bash
echo "WARNING: stale precis-dev image — installed package(s) differ from uv.lock: foo" >&2
echo "FAILED tests/test_x.py::test_a - AssertionError: nope"
echo "===== 1 failed in 0.05s ====="
exit 1
"""


def _stage(tmp_path: Path, test_stub: str) -> Path:
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True, exist_ok=True)
    for name in ("nightly", "test"):
        (tree / "scripts" / name).write_text(
            (REPO / "scripts" / name).read_text(encoding="utf-8")
            if name == "nightly"
            else test_stub,
            encoding="utf-8",
        )
        (tree / "scripts" / name).chmod(0o755)
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    return tree


def _run(tmp_path: Path, tree: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    f = fakebin / "docker"
    if not f.exists():
        f.write_text(_FAKE_DOCKER, encoding="utf-8")
        f.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
    }
    env.pop("FAKE_DOCKER_INFO_FAIL", None)
    env.update(extra)
    return subprocess.run(
        ["bash", str(tree / "scripts" / "nightly")],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def _top_result(tree: Path) -> str:
    status = (tree / ".nightly-status.md").read_text(encoding="utf-8")
    return next(ln for ln in status.splitlines() if ln.startswith("- **"))


def test_wrapper_disk_refusal_records_environment_failure_not_red(
    tmp_path: Path,
) -> None:
    """The gripe's exact event: exit 3 disk-probe refusal read as RED."""
    tree = _stage(tmp_path, _TEST_DISK_REFUSAL)
    proc = _run(tmp_path, tree)
    assert proc.returncode == 0
    top = _top_result(tree)
    assert "⚠ environment failure" in top
    assert "disk probe failed" in top
    assert "✗ RED" not in top


def test_stale_image_refusal_is_environment_not_red(tmp_path: Path) -> None:
    tree = _stage(tmp_path, _TEST_STALE_IMAGE)
    proc = _run(tmp_path, tree)
    assert proc.returncode == 0
    top = _top_result(tree)
    assert "⚠ environment failure" in top
    assert "stale precis-dev image" in top


def test_pytest_internal_error_stays_red(tmp_path: Path) -> None:
    """A pytest-level crash with no wrapper refusal is still suite signal."""
    tree = _stage(tmp_path, _TEST_PYTEST_INTERNAL)
    proc = _run(tmp_path, tree)
    assert proc.returncode == 0
    top = _top_result(tree)
    assert "✗ RED" in top
    assert "environment failure" not in top


def test_real_failures_still_record_red_after_isolated_retries(
    tmp_path: Path,
) -> None:
    """The contention filter re-runs suspects; a stub failing every round is
    a real RED — the env classification must not swallow it."""
    tree = _stage(tmp_path, _TEST_RED)
    proc = _run(tmp_path, tree)
    assert proc.returncode == 0
    top = _top_result(tree)
    assert "✗ RED" in top
    assert "persistent after isolated retry" in top


def test_nonfatal_stale_image_warning_does_not_reclassify_red(
    tmp_path: Path,
) -> None:
    tree = _stage(tmp_path, _TEST_WARNING_THEN_RED)
    proc = _run(tmp_path, tree)
    assert proc.returncode == 0
    assert "✗ RED" in _top_result(tree)


def test_docker_unavailable_still_records_skip(tmp_path: Path) -> None:
    tree = _stage(tmp_path, _TEST_DISK_REFUSAL)
    proc = _run(tmp_path, tree, FAKE_DOCKER_INFO_FAIL="1")
    assert proc.returncode == 0
    top = _top_result(tree)
    assert "⚠ skipped" in top
    assert "Docker unavailable" in top

"""Preflight stamp cache — gr514478.

The three stale-baked-venv checks (check-core-deps, check-lock-pins,
check-entry-points) run inside ``scripts/lib/pytest-inner.sh`` in the test
container. These tests run the real inner script against a fake ``python3``
on PATH that logs invocations, so the stamp behaviour — write on pass, skip
on hit, never stamp a failure or a heal — is checked against the shipped
bash, not a reimplementation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the shipped bash script + fake python3 are POSIX-only",
)

REPO = Path(__file__).resolve().parent.parent

_FAKE_PY = r"""#!/usr/bin/env bash
set -u
printf '%s\n' "$*" >> "$FAKE_PY_LOG"
case "${1:-}" in
  scripts/lib/check-core-deps.py)     exit "${FAKE_FAIL_CORE:-0}" ;;
  scripts/lib/check-lock-pins.py)     exit "${FAKE_FAIL_LOCK:-0}" ;;
  scripts/lib/check-entry-points.py)
    [ "${FAKE_HEAL:-0}" = "1" ] && echo "precis-preflight: healed"
    exit "${FAKE_FAIL_EP:-0}"
    ;;
  scripts/lib/collect_count.py) echo 3; exit 0 ;;
esac
exit 0
"""


def _stage(tmp_path: Path) -> tuple[Path, Path]:
    tree = tmp_path / "tree"
    (tree / "scripts" / "lib").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "lib" / "pytest-inner.sh", tree / "scripts" / "lib")
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    py = fakebin / "python3"
    py.write_text(_FAKE_PY, encoding="utf-8")
    py.chmod(0o755)
    return tree, fakebin


def _run(
    tmp_path: Path,
    *,
    key: str | None = "key1",
    stamp: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, list[str], Path]:
    """Run the staged inner script; return (rc, python3 arg log, stamp path)."""
    tree, fakebin = _stage(tmp_path)
    log = tmp_path / "py.log"
    log.write_text("", encoding="utf-8")
    stamp_path = tmp_path / "stamp"
    if stamp is not None:
        stamp_path.write_text(stamp, encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = f"{fakebin}{os.pathsep}{os.environ['PATH']}"
    env["FAKE_PY_LOG"] = str(log)
    env["PRECIS_PREFLIGHT_STAMP"] = str(stamp_path)
    env["PRECIS_AUTO_N"] = "0"
    if key is not None:
        env["PRECIS_PREFLIGHT_KEY"] = key
    else:
        env.pop("PRECIS_PREFLIGHT_KEY", None)
    env.update(extra_env or {})
    proc = subprocess.run(
        ["bash", str(tree / "scripts" / "lib" / "pytest-inner.sh"), "tests/x.py"],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    return proc.returncode, log.read_text(encoding="utf-8").splitlines(), stamp_path


def _checks(log: list[str]) -> list[str]:
    return [line for line in log if line.startswith("scripts/lib/check-")]


def test_miss_runs_checks_and_writes_stamp(tmp_path: Path) -> None:
    rc, log, stamp_path = _run(tmp_path)
    assert rc == 0
    assert _checks(log) == [
        "scripts/lib/check-core-deps.py",
        "scripts/lib/check-lock-pins.py",
        "scripts/lib/check-entry-points.py --heal",
    ]
    assert log[-1].startswith("-P -m pytest")
    assert stamp_path.read_text(encoding="utf-8").strip() == "key1"


def test_hit_skips_checks(tmp_path: Path) -> None:
    rc, log, _ = _run(tmp_path, stamp="key1\n")
    assert rc == 0
    assert _checks(log) == []
    assert log[-1].startswith("-P -m pytest")


def test_key_change_rechecks_and_restamps(tmp_path: Path) -> None:
    rc, log, stamp_path = _run(tmp_path, key="key2", stamp="key1\n")
    assert rc == 0
    assert len(_checks(log)) == 3
    assert stamp_path.read_text(encoding="utf-8").strip() == "key2"


def test_failed_check_leaves_no_stamp_and_no_pytest(tmp_path: Path) -> None:
    rc, log, stamp_path = _run(tmp_path, extra_env={"FAKE_FAIL_CORE": "1"})
    assert rc == 1
    assert _checks(log) == ["scripts/lib/check-core-deps.py"]
    assert not any(line.startswith("-P -m pytest") for line in log)
    assert not stamp_path.exists()


def test_heal_writes_no_stamp(tmp_path: Path) -> None:
    """A heal patched only the throwaway container — the next run must pay
    the checks again, so no stamp is written."""
    rc, log, stamp_path = _run(tmp_path, extra_env={"FAKE_HEAL": "1"})
    assert rc == 0
    assert len(_checks(log)) == 3
    assert log[-1].startswith("-P -m pytest")
    assert not stamp_path.exists()


def test_no_key_runs_checks_without_stamping(tmp_path: Path) -> None:
    rc, log, stamp_path = _run(tmp_path, key=None)
    assert rc == 0
    assert len(_checks(log)) == 3
    assert not stamp_path.exists()

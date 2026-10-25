"""pytest-inner.sh's bind-mount race retry (gr515829).

A host-side worktree edit (`git checkout -- <file>`, an agent Write)
landing while the test container collects shows up as a
``FileNotFoundError`` mid-collection — a flake, not a verdict.
``pytest-inner.sh`` streams the run through a capture file and re-runs it
once when the failure is exactly that; anything else stays a single run.

Staging: the real script plus a synthetic ``python3`` on PATH that passes
the preflight scripts and replays canned pytest output per call.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)

REPO = Path(__file__).resolve().parent.parent
INNER = REPO / "scripts" / "lib" / "pytest-inner.sh"

_FAKE_PYTHON = r"""#!/usr/bin/env bash
# Synthetic python3 -- passes the preflight .py scripts; for '-m pytest'
# consumes one "<rc>|<output (\n-escaped)>" line per call from
# $FAKE_PY_SEQ; beyond it exits $FAKE_PY_RC (default 0).
set -u
case " $* " in
  *" -m pytest "*)
    if [ -n "${FAKE_PY_CALLS:-}" ]; then
      n=0; [ -f "$FAKE_PY_CALLS" ] && n="$(cat "$FAKE_PY_CALLS")"
      echo "$((n + 1))" > "$FAKE_PY_CALLS"
      line="$(sed -n "$((n + 1))p" "${FAKE_PY_SEQ:-/dev/null}" 2>/dev/null || true)"
      if [ -n "$line" ]; then
        rc="${line%%|*}"
        printf '%b\n' "${line#*|}"
        exit "${rc:-0}"
      fi
      exit "${FAKE_PY_RC:-0}"
    fi
    exit "${FAKE_PY_RC:-0}"
    ;;
esac
exit 0
"""


def _stage(tmp_path: Path) -> tuple[Path, Path]:
    tree = tmp_path / "inner"
    (tree / "scripts" / "lib").mkdir(parents=True)
    shutil.copy2(INNER, tree / "scripts" / "lib" / "pytest-inner.sh")
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    py = fakebin / "python3"
    py.write_text(_FAKE_PYTHON, encoding="utf-8")
    py.chmod(0o755)
    return tree, fakebin


def _run_inner(
    tmp_path: Path, seq: str, args: tuple[str, ...] = ("-q",)
) -> tuple[int, str, str, int]:
    """(rc, stdout, stderr, pytest call count)."""
    tree, fakebin = _stage(tmp_path)
    seq_file = tmp_path / "pyseq"
    seq_file.write_text(seq, encoding="utf-8")
    calls = tmp_path / "pycalls"
    env = {
        **os.environ,
        "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_PY_SEQ": str(seq_file),
        "FAKE_PY_CALLS": str(calls),
    }
    proc = subprocess.run(
        ["bash", str(tree / "scripts" / "lib" / "pytest-inner.sh"), *args],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    n_calls = int(calls.read_text(encoding="utf-8").strip()) if calls.exists() else 0
    return proc.returncode, proc.stdout, proc.stderr, n_calls


def test_collect_filenotfound_retries_once(tmp_path: Path) -> None:
    """The gripe verbatim: FileNotFoundError while collecting → one quiet
    re-run, then the real result stands."""
    rc, out, err, calls = _run_inner(
        tmp_path,
        "2|ERROR collecting tests/test_x.py\\n"
        "E   FileNotFoundError: [Errno 2] No such file or directory\n"
        "0|1 passed in 0.01s\n",
    )
    assert rc == 0, err
    assert calls == 2
    assert "retrying once" in err
    assert "1 passed in 0.01s" in out


def test_plain_failure_does_not_retry(tmp_path: Path) -> None:
    """A real red run must not double: only the collect race retries."""
    rc, _, err, calls = _run_inner(
        tmp_path, "1|tests/test_x.py F\\n1 failed in 0.01s\n"
    )
    assert rc == 1
    assert calls == 1
    assert "retrying once" not in err


def test_filenotfound_outside_collection_does_not_retry(tmp_path: Path) -> None:
    """A FileNotFoundError inside a test body is a real failure, not the
    bind-mount race — no retry."""
    rc, _, _, calls = _run_inner(
        tmp_path,
        "1|tests/test_x.py F\\n"
        "FileNotFoundError: [Errno 2] No such file or directory\\n"
        "1 failed in 0.01s\n",
    )
    assert rc == 1
    assert calls == 1

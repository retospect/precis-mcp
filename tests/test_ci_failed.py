"""``scripts/ci-failed`` — the compact digest behind the red-gate pointer.

gr514904: `gh run view <id> --log-failed` returns megabytes; the script
parses its ``job <TAB> step <TAB> <ISO ts> <text>`` stream into a
per-failed-job signal digest (pytest FAILED/ERROR/E lines, error/timeout
markers) with a tail fallback for jobs that log nothing matching.
``gh`` is stubbed on PATH; nothing touches the network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci-failed"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the gh stub is a POSIX script"
)

_GH_STUB = """\
#!/usr/bin/env python3
import os
import sys
from pathlib import Path

if os.environ.get("GH_STUB_ERROR"):
    sys.stderr.write(os.environ["GH_STUB_ERROR"])
if "--json" in sys.argv:
    sys.stdout.write(os.environ.get("GH_STUB_RUN", '{"status":"completed","conclusion":"failure","jobs":[]}'))
    sys.exit(int(os.environ.get("GH_STUB_META_RC", "0")))
sys.stdout.write(Path(os.environ["GH_STUB_STATE"]).read_text(encoding="utf-8"))
sys.exit(int(os.environ.get("GH_STUB_RC", "0")))
"""

TS = "2026-10-24T21:00:00.0000000Z"

LOG = f"""\
test-linux (3.13, 5)\tRun pytest\t{TS} ============================= test session starts ==============================
test-linux (3.13, 5)\tRun pytest\t{TS} tests/test_a.py ....F....
test-linux (3.13, 5)\tRun pytest\t{TS} ____________________________ test_broke ____________________________
test-linux (3.13, 5)\tRun pytest\t{TS} E       assert 1 == 2
test-linux (3.13, 5)\tRun pytest\t{TS} ================ 1 failed, 5 passed in 3.21s =================
test-linux (3.13, 5)\tRun pytest\t{TS} FAILED tests/test_a.py::test_broke - assert 1 == 2
test-linux (3.13, 5)\tRun pytest\t{TS} FAILED tests/test_a.py::test_broke - assert 1 == 2
test-linux (3.13, 5)\tRun pytest\t{TS} Error: Process completed with exit code 1.
lint\tRun ruff\t{TS} src/x.py:1:1: F401 unused import
lint\tRun ruff\t{TS} Found 1 error.
docs\tBuild docs\t{TS} building pages
{"".join(f"docs\tBuild docs\t{TS} layout noise line {i}\n" for i in range(20))}docs\tBuild docs\t{TS} mkdocs: aborting build
docs\tBuild docs\t{TS} done
"""


def _gh_stub(tmp_path: Path, log: str = LOG, rc: int = 0) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(_GH_STUB, encoding="utf-8")
    gh.chmod(0o755)
    (tmp_path / "ghstate").write_text(log, encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["GH_STUB_STATE"] = str(tmp_path / "ghstate")
    env["GH_STUB_RC"] = str(rc)
    return env


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=60,
    )


def test_signal_lines_per_failed_job(tmp_path: Path) -> None:
    cp = _run(["223355"], _gh_stub(tmp_path))
    assert cp.returncode == 0, cp.stderr
    assert "== test-linux (3.13, 5) ==" in cp.stdout
    assert "== lint ==" in cp.stdout
    assert "FAILED tests/test_a.py::test_broke - assert 1 == 2" in cp.stdout
    assert "E       assert 1 == 2" in cp.stdout
    assert "Error: Process completed with exit code 1." in cp.stdout
    # the duplicated FAILED line is deduped
    assert cp.stdout.count("test_broke - assert 1 == 2") == 1
    # noise is filtered, the ISO timestamp is stripped
    assert "test session starts" not in cp.stdout
    assert "....F...." not in cp.stdout
    assert TS not in cp.stdout


def test_silent_job_falls_back_to_its_tail(tmp_path: Path) -> None:
    cp = _run(["223355"], _gh_stub(tmp_path))
    assert cp.returncode == 0, cp.stderr
    assert "== docs ==" in cp.stdout
    assert "no signal lines" in cp.stdout
    assert "mkdocs: aborting build" in cp.stdout
    assert "building pages" not in cp.stdout  # older than the tail window


def test_lines_cap_reports_the_rest(tmp_path: Path) -> None:
    cp = _run(["223355", "-n", "2"], _gh_stub(tmp_path))
    assert cp.returncode == 0, cp.stderr
    assert "more signal lines" in cp.stdout


def test_gh_failure_exits_one_with_reason(tmp_path: Path) -> None:
    cp = _run(["223355"], _gh_stub(tmp_path, log="", rc=3))
    assert cp.returncode == 1
    assert "ci-failed: gh run view 223355 --log-failed failed" in cp.stderr


def test_non_numeric_run_id_is_usage_error(tmp_path: Path) -> None:
    cp = _run(["abc"], _gh_stub(tmp_path))
    assert cp.returncode == 2


def test_completed_run_reports_jobs_that_never_concluded(tmp_path: Path) -> None:
    env = _gh_stub(tmp_path, log="")
    env["GH_STUB_RUN"] = json.dumps(
        {
            "status": "completed",
            "conclusion": "failure",
            "jobs": [
                {"name": "shard 2", "status": "queued", "conclusion": ""},
                {"name": "shard 7", "status": "in_progress", "conclusion": ""},
            ],
        }
    )
    cp = _run(["223355"], env)
    assert cp.returncode == 0, cp.stderr
    assert "never concluded: shard 2 (queued)" in cp.stdout
    assert "never concluded: shard 7 (in_progress)" in cp.stdout
    assert "full rerun needed: gh run rerun 223355" in cp.stdout
    assert "--failed cannot rerun jobs that never concluded" in cp.stdout


def test_metadata_unreadable_is_reported(tmp_path: Path) -> None:
    env = _gh_stub(tmp_path)
    env["GH_STUB_META_RC"] = "1"
    env["GH_STUB_ERROR"] = "HTTP 503: service unavailable"
    cp = _run(["223355"], env)
    assert cp.returncode == 1
    assert "--json status,conclusion,jobs unreadable: exit 1" in cp.stderr
    assert "HTTP 503: service unavailable" in cp.stderr

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
import re
import sys
from pathlib import Path

if os.environ.get("GH_STUB_ERROR"):
    sys.stderr.write(os.environ["GH_STUB_ERROR"])
argv_log = os.environ.get("GH_STUB_ARGV")
if argv_log:
    with open(argv_log, "a", encoding="utf-8") as f:
        f.write(" ".join(sys.argv[1:]) + "\\n")
if len(sys.argv) > 1 and sys.argv[1] == "api":
    reject = os.environ.get("GH_STUB_REJECT_FLAG")
    if reject and reject in sys.argv:
        sys.stderr.write("unknown flag: %s\\n" % reject)
        sys.exit(1)
    api = Path(os.environ["GH_STUB_API"])
    if api.is_dir():
        m = re.search(r"/jobs/(\\d+)/logs", " ".join(sys.argv))
        api = api / (m.group(1) if m else "_")
    if api.exists():
        sys.stdout.buffer.write(api.read_bytes())
    sys.exit(int(os.environ.get("GH_STUB_API_RC", "0")))
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


FAILED_FIRST_LOG = f"""\
failed-first\tRe-run failed ids\t{TS} failed-first: tests/test_a.py::test_one
failed-first\tRe-run failed ids\t{TS} failed-first: tests/test_a.py::test_two
failed-first\tRe-run failed ids\t{TS} E       assert 1 == 2
failed-first\tRe-run failed ids\t{TS} FAILED tests/test_a.py::test_one - assert 1 == 2
failed-first\tRe-run failed ids\t{TS} FAILED tests/test_a.py::test_two - assert x
failed-first\tRe-run failed ids\t{TS} FAILED tests/test_a.py::test_two - assert x
failed-first\tRe-run failed ids\t{TS} ================ 2 failed, 17 passed in 19.88s =================
failed-first\tRe-run failed ids\t{TS} Error: Process completed with exit code 1.
"""


def test_failed_ids_rollup_names_the_failing_nodeids(tmp_path: Path) -> None:
    """gr580432: a red failed-first named only its job — the reader had to
    pull the job log for the ids. The roll-up carries them, deduped and
    outside the -n cap."""
    cp = _run(["223355"], _gh_stub(tmp_path, log=FAILED_FIRST_LOG))
    assert cp.returncode == 0, cp.stderr
    assert "== failed-first ==" in cp.stdout
    assert "failed ids (2):" in cp.stdout
    assert "    tests/test_a.py::test_one\n" in cp.stdout
    assert "    tests/test_a.py::test_two\n" in cp.stdout
    # the re-run input lines are not ids and carry no signal
    assert "failed-first: tests/test_a.py" not in cp.stdout


def test_red_failed_first_marks_the_matrix_skip_as_by_design(
    tmp_path: Path,
) -> None:
    """gr580432: skipped test-linux shards read as a hole in the run; the
    note names the skip as failed-first's designed consequence."""
    env = _gh_stub(tmp_path, log=FAILED_FIRST_LOG)
    env["GH_STUB_RUN"] = json.dumps(
        {
            "status": "completed",
            "conclusion": "failure",
            "jobs": [
                {"name": "plan", "status": "completed", "conclusion": "success"},
                {"name": "lint", "status": "completed", "conclusion": "success"},
                {
                    "name": "failed-first",
                    "status": "completed",
                    "conclusion": "failure",
                },
                {
                    "name": "test-linux (3.13, 1)",
                    "status": "completed",
                    "conclusion": "skipped",
                },
                {
                    "name": "test-linux (3.13, 2)",
                    "status": "completed",
                    "conclusion": "skipped",
                },
                # full-shape jobs skip on every gate run — not counted
                {
                    "name": "test-other (macos-latest, 3.13, 1)",
                    "status": "completed",
                    "conclusion": "skipped",
                },
                {
                    "name": "coverage-db",
                    "status": "completed",
                    "conclusion": "skipped",
                },
            ],
        }
    )
    cp = _run(["223355"], env)
    assert cp.returncode == 0, cp.stderr
    assert "2 test-linux shard job(s) skipped by design" in cp.stdout
    assert "green failed-first" in cp.stdout
    assert "test-other" not in cp.stdout.split("note:")[-1]


def test_green_failed_first_prints_no_skip_note(tmp_path: Path) -> None:
    env = _gh_stub(tmp_path)
    env["GH_STUB_RUN"] = json.dumps(
        {
            "status": "completed",
            "conclusion": "failure",
            "jobs": [
                {
                    "name": "failed-first",
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "name": "test-linux (3.13, 1)",
                    "status": "completed",
                    "conclusion": "failure",
                },
            ],
        }
    )
    cp = _run(["223355"], env)
    assert cp.returncode == 0, cp.stderr
    assert "skipped by design" not in cp.stdout


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


def _api_stub(tmp_path: Path, head: bytes, rc: int = 0) -> dict[str, str]:
    env = _gh_stub(tmp_path)
    (tmp_path / "ghapi").write_bytes(head)
    env["GH_STUB_API"] = str(tmp_path / "ghapi")
    env["GH_STUB_API_RC"] = str(rc)
    return env


def test_probe_reports_status_and_accepted_permissions(tmp_path: Path) -> None:
    head = (
        b"HTTP/2 403\n"
        b"server: github.com\n"
        b"x-accepted-github-permissions: contents=read\n"
        b"x-github-request-id: ABCD:1234\n"
        b"\n"
        b'{"message":"Forbidden"}'
    )
    cp = _run(["223355", "--probe"], _api_stub(tmp_path, head))
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout == (
        "probe: status=403 accepted-permissions=contents=read run=223355\n"
    )
    # the body and the other headers are never echoed
    assert "Forbidden" not in cp.stdout
    assert "ABCD" not in cp.stdout


def test_probe_without_the_header_prints_absent(tmp_path: Path) -> None:
    head = b"HTTP/2 200\nserver: github.com\ncontent-type: application/zip\n\n"
    cp = _run(["223355", "--probe"], _api_stub(tmp_path, head))
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout == ("probe: status=200 accepted-permissions=absent run=223355\n")


def test_probe_odd_permission_value_prints_unprintable(tmp_path: Path) -> None:
    head = b"HTTP/2 403\nx-accepted-github-permissions: contents=read;admin\n\n"
    cp = _run(["223355", "--probe"], _api_stub(tmp_path, head))
    assert cp.returncode == 0, cp.stderr
    assert "accepted-permissions=unprintable" in cp.stdout
    assert "contents=read;admin" not in cp.stdout


def test_probe_without_gh_exits_one(tmp_path: Path) -> None:
    env = dict(os.environ)
    (tmp_path / "bin").mkdir()
    env["PATH"] = str(tmp_path / "bin")
    cp = _run(["223355", "--probe"], env)
    assert cp.returncode == 1
    assert "gh api actions/runs/223355/logs" in cp.stderr


def test_probe_gh_failure_without_a_head_exits_one(tmp_path: Path) -> None:
    env = _api_stub(tmp_path, b"", rc=1)
    env["GH_STUB_ERROR"] = "gh: api call failed"
    cp = _run(["223355", "--probe"], env)
    assert cp.returncode == 1
    assert "failed (1)" in cp.stderr


def _api_dir(tmp_path: Path, logs: dict[str, str]) -> dict[str, str]:
    """A gh stub whose `api` answers per job id: GH_STUB_API is a dir,
    the job's log at <dir>/<databaseId>."""
    env = _gh_stub(tmp_path, log="")
    api = tmp_path / "api"
    api.mkdir()
    for jid, log in logs.items():
        (api / jid).write_text(log, encoding="utf-8")
    env["GH_STUB_API"] = str(api)
    argv_file = tmp_path / "argv"
    env["GH_STUB_ARGV"] = str(argv_file)
    env["_argv_file"] = str(argv_file)
    return env


def test_in_progress_run_digests_the_failed_jobs_per_job(tmp_path: Path) -> None:
    env = _api_dir(
        tmp_path,
        {
            "111": (
                f"{TS} ============================= test session starts\n"
                f"{TS} FAILED tests/test_a.py::test_broke - assert 1 == 2\n"
            ),
        },
    )
    env["GH_STUB_RUN"] = json.dumps(
        {
            "status": "in_progress",
            "conclusion": None,
            "jobs": [
                {
                    "name": "test-linux",
                    "databaseId": 111,
                    "status": "completed",
                    "conclusion": "failure",
                },
                {
                    "name": "lint",
                    "databaseId": 222,
                    "status": "in_progress",
                    "conclusion": None,
                },
            ],
        }
    )
    cp = _run(["223355"], env)
    assert cp.returncode == 0, cp.stderr
    assert "== test-linux ==" in cp.stdout
    assert "FAILED tests/test_a.py::test_broke - assert 1 == 2" in cp.stdout
    assert "== lint ==" not in cp.stdout  # still running, not failed
    calls = Path(env["_argv_file"]).read_text(encoding="utf-8")
    assert "/actions/jobs/111/logs" in calls
    assert "--log-failed" not in calls  # never waits on the whole run


def test_per_job_log_call_allows_escape_sequences(tmp_path: Path) -> None:
    """gr574350: gh ≥2.97 aborts a logs response carrying terminal escape
    sequences unless --allow-escape-sequences is passed."""
    env = _api_dir(
        tmp_path,
        {"111": f"{TS} Error: Process completed with exit code 1.\n"},
    )
    env["GH_STUB_RUN"] = json.dumps(
        {
            "status": "in_progress",
            "conclusion": None,
            "jobs": [
                {
                    "name": "build",
                    "databaseId": 111,
                    "status": "completed",
                    "conclusion": "failure",
                },
            ],
        }
    )
    cp = _run(["223355"], env)
    assert cp.returncode == 0, cp.stderr
    calls = Path(env["_argv_file"]).read_text(encoding="utf-8")
    assert (
        "api --allow-escape-sequences "
        "repos/{owner}/{repo}/actions/jobs/111/logs" in calls
    )


def test_old_gh_without_the_flag_retries_plain(tmp_path: Path) -> None:
    """gh <2.97 has no --allow-escape-sequences: one retry without it
    keeps the per-job digest working (same shape as ci_watch._gh_log)."""
    env = _api_dir(
        tmp_path,
        {"111": f"{TS} FAILED tests/test_a.py::test_broke - assert 1 == 2\n"},
    )
    env["GH_STUB_REJECT_FLAG"] = "--allow-escape-sequences"
    env["GH_STUB_RUN"] = json.dumps(
        {
            "status": "in_progress",
            "conclusion": None,
            "jobs": [
                {
                    "name": "build",
                    "databaseId": 111,
                    "status": "completed",
                    "conclusion": "failure",
                },
            ],
        }
    )
    cp = _run(["223355"], env)
    assert cp.returncode == 0, cp.stderr
    assert "FAILED tests/test_a.py::test_broke - assert 1 == 2" in cp.stdout
    calls = Path(env["_argv_file"]).read_text(encoding="utf-8")
    job_calls = [l for l in calls.splitlines() if "/actions/jobs/111/logs" in l]
    assert job_calls == [
        "api --allow-escape-sequences repos/{owner}/{repo}/actions/jobs/111/logs",
        "api repos/{owner}/{repo}/actions/jobs/111/logs",
    ]


def test_in_progress_run_with_no_failed_jobs_reports_no_lines(tmp_path: Path) -> None:
    env = _api_dir(tmp_path, {})
    env["GH_STUB_RUN"] = json.dumps(
        {
            "status": "in_progress",
            "conclusion": None,
            "jobs": [
                {
                    "name": "lint",
                    "databaseId": 222,
                    "status": "in_progress",
                    "conclusion": None,
                },
            ],
        }
    )
    cp = _run(["223355"], env)
    assert cp.returncode == 0, cp.stderr
    assert "no failed-job log lines yet" in cp.stdout


def test_log_failed_in_progress_refusal_falls_back_to_per_job(tmp_path: Path) -> None:
    """The metadata read said completed but --log-failed still refuses —
    the race reads the failed jobs per job instead of failing."""
    env = _api_dir(
        tmp_path,
        {
            "333": f"{TS} Error: Process completed with exit code 1.\n",
        },
    )
    env["GH_STUB_RUN"] = json.dumps(
        {
            "status": "completed",
            "conclusion": "failure",
            "jobs": [
                {
                    "name": "build",
                    "databaseId": 333,
                    "status": "completed",
                    "conclusion": "failure",
                },
            ],
        }
    )
    env["GH_STUB_RC"] = "1"
    env["GH_STUB_ERROR"] = (
        "run 223355 is still in progress; logs will be available when it is complete"
    )
    cp = _run(["223355"], env)
    assert cp.returncode == 0, cp.stderr
    assert "== build ==" in cp.stdout
    assert "Error: Process completed with exit code 1." in cp.stdout
    calls = Path(env["_argv_file"]).read_text(encoding="utf-8")
    assert "--log-failed" in calls  # attempted first, then the per-job path
    assert "/actions/jobs/333/logs" in calls


_COMPLETED_BLIND_RUN = {
    "status": "completed",
    "conclusion": "failure",
    "jobs": [
        {
            "name": "gate-fast (3.13, 1)",
            "databaseId": 555,
            "status": "completed",
            "conclusion": "failure",
        }
    ],
}


def test_blind_shared_log_is_backfilled_from_the_job_log(tmp_path: Path) -> None:
    """gr587536: the shared ``--log-failed`` stream can carry only the
    step header for a failed shard — the digest re-reads the job's own
    log through the jobs-logs endpoint and finds the pytest failure
    lines (FAILED ids, the ``= N failed`` count, the E-lines) after
    stripping the log's escape sequences."""
    env = _api_dir(
        tmp_path,
        {
            "555": (
                f"{TS} \x1b[36m+ pytest -q\x1b[0m\n"
                f"{TS} E       assert [] == ['GROQ_API_KEY']\n"
                f"{TS} \x1b[31mFAILED\x1b[0m tests/x.py::t - assert [] == ['GROQ_API_KEY']\n"
                f"{TS} = \x1b[31m1 failed\x1b[0m, 9 passed in 3.2s =\n"
            ),
        },
    )
    (tmp_path / "ghstate").write_text(
        f"gate-fast (3.13, 1)\tRun pytest\t{TS} + pytest -q\n"
        f"gate-fast (3.13, 1)\tRun pytest\t{TS} env:\n"
        f"gate-fast (3.13, 1)\tRun pytest\t{TS}   GROQ_API_KEY: ***\n",
        encoding="utf-8",
    )
    env["GH_STUB_RUN"] = json.dumps(_COMPLETED_BLIND_RUN)
    cp = _run(["223355"], env)
    assert cp.returncode == 0, cp.stderr
    assert "== gate-fast (3.13, 1) ==" in cp.stdout
    assert "FAILED tests/x.py::t - assert [] == ['GROQ_API_KEY']" in cp.stdout
    assert "E       assert [] == ['GROQ_API_KEY']" in cp.stdout
    assert "1 failed, 9 passed in 3.2s" in cp.stdout
    assert "no signal lines" not in cp.stdout
    assert "\x1b" not in cp.stdout
    calls = Path(env["_argv_file"]).read_text(encoding="utf-8")
    assert "--log-failed" in calls  # the shared stream came first
    assert "/actions/jobs/555/logs" in calls  # then the job's own log


def test_pytest_final_summary_is_named_per_job(tmp_path: Path) -> None:
    """td650667: pytest's final ``= … in Ns =`` line is the digest's proof
    that a job's log reached the end of the run; a job without one gets
    no ``pytest summary:`` line."""
    cp = _run(["223355"], _gh_stub(tmp_path))
    assert cp.returncode == 0, cp.stderr
    section = cp.stdout.split("== lint ==")[0]
    assert "  pytest summary: 1 failed, 5 passed in 3.21s\n" in section
    assert cp.stdout.count("pytest summary:") == 1


_SOAK_RUN = {
    "status": "completed",
    "conclusion": "failure",
    "jobs": [
        {
            "name": "soak (3.13, 4)",
            "databaseId": 444,
            "status": "completed",
            "conclusion": "failure",
        },
        {
            "name": "gate-fast (3.13, 1)",
            "databaseId": 555,
            "status": "completed",
            "conclusion": "failure",
        },
    ],
}


def test_per_job_reads_each_failed_jobs_full_log_on_a_completed_run(
    tmp_path: Path,
) -> None:
    """td650667: `--log-failed` carries only the failed step, so a leg
    whose upload step failed after a green pytest step shows no summary.
    --per-job reads the job's whole log (every step) instead."""
    env = _api_dir(
        tmp_path,
        {
            "444": (
                f"{TS} + pytest --shard 4/10\n"
                f"{TS} \x1b[32m===== 3456 passed, 12 skipped in 290.12s "
                f"(0:04:50) =====\x1b[0m\n"
                f"{TS} Attempt 5 of 5 failed with error: Unexpected token '<'\n"
            ),
        },
    )
    env["GH_STUB_RUN"] = json.dumps(_SOAK_RUN)
    cp = _run(["223355", "--job-prefix", "soak", "--per-job", "-n", "0"], env)
    assert cp.returncode == 0, cp.stderr
    assert "== soak (3.13, 4) ==" in cp.stdout
    assert "  pytest summary: 3456 passed, 12 skipped in 290.12s (0:04:50)" in cp.stdout
    assert "gate-fast" not in cp.stdout
    calls = Path(env["_argv_file"]).read_text(encoding="utf-8")
    assert "--log-failed" not in calls
    assert "/actions/jobs/444/logs" in calls
    assert "/actions/jobs/555/logs" not in calls


def test_per_job_with_unreadable_metadata_exits_one(tmp_path: Path) -> None:
    """Without the run's job list there is no job log to read: --per-job
    never falls back to the step-only shared stream."""
    env = _api_dir(tmp_path, {})
    env["GH_STUB_META_RC"] = "1"
    cp = _run(["223355", "--per-job"], env)
    assert cp.returncode == 1
    calls = Path(env["_argv_file"]).read_text(encoding="utf-8")
    assert "--log-failed" not in calls


def test_blind_shared_and_job_log_fall_back_to_the_tail(tmp_path: Path) -> None:
    """A job log that is itself just the step header keeps the tail
    fallback — now the fuller per-job log's tail — plus the jobs-logs
    next-step line."""
    env = _api_dir(
        tmp_path,
        {
            "555": (
                f"{TS} + pytest -q\n"
                f"{TS} env:\n"
                f"{TS}   GROQ_API_KEY: ***\n"
                f"{TS} step aborted before pytest ran\n"
            ),
        },
    )
    (tmp_path / "ghstate").write_text(
        f"gate-fast (3.13, 1)\tRun pytest\t{TS} + pytest -q\n",
        encoding="utf-8",
    )
    env["GH_STUB_RUN"] = json.dumps(_COMPLETED_BLIND_RUN)
    cp = _run(["223355"], env)
    assert cp.returncode == 0, cp.stderr
    assert "no signal lines" in cp.stdout
    assert "actions/jobs/<id>/logs" in cp.stdout  # the next-step hint
    assert "step aborted before pytest ran" in cp.stdout  # the job-log tail

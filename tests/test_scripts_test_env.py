"""scripts/test env forwarding — gr513868.

Only a fixed allowlist of env vars crosses into the test container
(``ARTIFACT_ENV``); ``NANO_SEEDS`` was exported on the host, silently
dropped, and a seed-iteration run measured seed 1 for four minutes.
``PRECIS_TEST_ENV="NAME=value …"`` is the opt-in for anything not on the
list, and a ``NANO_*``/``ESP32C3_*``/``PRECIS_PCB_*`` variable that is
exported but not forwarded is now named once on stderr.

These tests run the REAL ``scripts/test`` (staged byte-for-byte, with its
sourced libs, into a throwaway git repo) against a synthetic ``docker``
on PATH that logs every ``compose run`` arg vector verbatim. The real
daemon is never contacted — same harness as
``test_scripts_test_agent_teardown.py``.
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
    reason="the shipped bash script + fake docker are POSIX-only",
)

REPO = Path(__file__).resolve().parent.parent
_LIBS = (
    "compose-project.sh",
    "docker-disk.sh",
    "gate-slot.sh",
    "lock-holder.sh",
    "pytest-inner.sh",
    "collect_count.py",
)

_FAKE_DOCKER = r"""#!/usr/bin/env bash
# Synthetic docker -- logs compose invocations; never touches a daemon.
#   FAKE_DOCKER_LOG   appended: up|<proj> / run|<proj>|<all args> / down|<proj>
#   FAKE_PS_SERVICES  newline-separated service names `docker ps` reports
# A plain `docker run ... df` is the free-disk preflight probe: it answers
# with 100 GB free so the preflight passes and never reaches the log.
set -u
args=("$@")
case "${args[0]:-}" in
  run)
    printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n'
    printf 'overlay 209715200 1048576 104857600 1%% /\n'
    exit 0
    ;;
  ps)
    printf '%s\n' "${FAKE_PS_SERVICES:-}"
    exit 0
    ;;
  compose)
    proj=""
    verb=""
    for i in "${!args[@]}"; do
        a="${args[$i]}"
        [ "$a" = "-p" ] && proj="${args[$((i+1))]}"
        if [ -z "$verb" ]; then
            case "$a" in up|run|down) verb="$a" ;; esac
        fi
    done
    case "$verb" in
      up)   echo "up|$proj" >> "$FAKE_DOCKER_LOG" ;;
      run)
        echo "run|$proj|${args[*]}" >> "$FAKE_DOCKER_LOG"
        sleep "${FAKE_RUN_SLEEP:-0}"
        ;;
      down) echo "down|$proj" >> "$FAKE_DOCKER_LOG" ;;
    esac
    exit 0
    ;;
esac
exit 0
"""

_SCRUB_PREFIXES = (
    "NANO_",
    "ESP32C3_",
    "PRECIS_PCB_",
    "PRECIS_TEST_",
    "PRECIS_COMPOSE",
    "PRECIS_STRICT_LOCK_CHECK",
    "PRECIS_RANDOM",
    "UV_WITH",
)


def _stage(tmp_path: Path, name: str = "envtest") -> Path:
    """A throwaway repo directory holding the real script."""
    tree = tmp_path / name
    (tree / "scripts" / "lib").mkdir(parents=True)
    (tree / "docker" / "dev").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "test", tree / "scripts" / "test")
    for lib in _LIBS:
        shutil.copy2(REPO / "scripts" / "lib" / lib, tree / "scripts" / "lib" / lib)
    (tree / "docker" / "dev" / "compose.yaml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    # scripts/test refuses a missing test path before any docker call
    # (gr514948) — the staged tree needs the default arg's file to exist.
    (tree / "tests").mkdir(exist_ok=True)
    (tree / "tests" / "x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    return tree


def _run(
    tmp_path: Path,
    *,
    args: tuple[str, ...] = ("-n0", "tests/x.py"),
    extra_env: dict[str, str] | None = None,
    files: tuple[str, ...] = (),
    selector: str | None = None,
) -> tuple[int, list[str], str]:
    """Run staged scripts/test; return (rc, docker log lines, stderr)."""
    tree = _stage(tmp_path)
    for rel in files:
        p = tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
    if selector is not None:
        # --selected computes the selection host-side via scripts/impacted-tests
        sel = tree / "scripts" / "impacted-tests"
        sel.write_text(selector, encoding="utf-8")
        sel.chmod(0o755)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    docker = fakebin / "docker"
    docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SCRUB_PREFIXES)}
    env["PATH"] = f"{fakebin}{os.pathsep}{os.environ['PATH']}"
    env["FAKE_DOCKER_LOG"] = str(log)
    env["FAKE_PS_SERVICES"] = "precis-test-db"
    env["TMPDIR"] = str(tmp_path)
    env.update(extra_env or {})
    proc = subprocess.run(
        ["bash", str(tree / "scripts" / "test"), *args],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    return proc.returncode, log.read_text(encoding="utf-8").splitlines(), proc.stderr


def _run_line(log: list[str]) -> str:
    runs = [line for line in log if line.startswith("run|")]
    assert len(runs) == 1, f"expected exactly one compose run, got {log}"
    return runs[0]


def test_precis_test_env_forwards_pairs(tmp_path: Path) -> None:
    rc, log, stderr = _run(
        tmp_path, extra_env={"PRECIS_TEST_ENV": "NANO_SEEDS=2 FOO=bar"}
    )
    assert rc == 0, stderr
    line = _run_line(log)
    assert " -e NANO_SEEDS=2 " in f" {line} " or "-e NANO_SEEDS=2 " in line
    assert " -e FOO=bar " in f" {line} "
    assert "not forwarded" not in stderr


def test_set_but_unforwarded_tuning_var_warns_once(tmp_path: Path) -> None:
    """The gripe's failure mode verbatim: NANO_SEEDS exported, not in the
    allowlist — one stderr line naming it and the fix, and the container
    still does not see it."""
    rc, log, stderr = _run(tmp_path, extra_env={"NANO_SEEDS": "2"})
    assert rc == 0, stderr
    assert (
        "scripts/test: NANO_SEEDS is set but not forwarded into the "
        'container; use PRECIS_TEST_ENV="NANO_SEEDS=2"' in stderr
    )
    assert "NANO_SEEDS" not in _run_line(log)


def test_allowlisted_var_forwards_without_warning(tmp_path: Path) -> None:
    rc, log, stderr = _run(tmp_path, extra_env={"PRECIS_PCB_BENCH_OUT": "/tmp/x"})
    assert rc == 0, stderr
    assert " -e PRECIS_PCB_BENCH_OUT=/tmp/x " in f" {_run_line(log)} "
    assert "not forwarded" not in stderr


def test_forwarded_via_precis_test_env_suppresses_the_warning(tmp_path: Path) -> None:
    rc, log, stderr = _run(
        tmp_path,
        extra_env={"NANO_SEEDS": "3", "PRECIS_TEST_ENV": "NANO_SEEDS=3"},
    )
    assert rc == 0, stderr
    assert " -e NANO_SEEDS=3 " in f" {_run_line(log)} "
    assert "not forwarded" not in stderr


@pytest.mark.parametrize(
    "bad", ["PATH=/x", "HOME=/x", "PYTHONPATH=/x", "DATABASE_URL=p", "PGHOST=h"]
)
def test_precis_test_env_refuses_runtime_internals(tmp_path: Path, bad: str) -> None:
    rc, log, stderr = _run(tmp_path, extra_env={"PRECIS_TEST_ENV": bad})
    assert rc != 0
    assert "may not forward" in stderr
    assert not [line for line in log if line.startswith("run|")]


@pytest.mark.parametrize("bad", ["nano_seeds=2", "2FOO=x", "NOEQUALS"])
def test_precis_test_env_refuses_malformed_entries(tmp_path: Path, bad: str) -> None:
    rc, log, stderr = _run(tmp_path, extra_env={"PRECIS_TEST_ENV": bad})
    assert rc != 0
    assert "is not NAME=value" in stderr
    assert not [line for line in log if line.startswith("run|")]


# --- auto -n0 for small selections (test-loop-speed Decision 1) -------------
# The host side only decides WHETHER the container counts (-e PRECIS_AUTO_N):
# the count itself runs inside the container (scripts/lib/collect_count.py,
# covered by tests/test_collect_count.py).


def test_single_file_selection_counts_for_auto_n(tmp_path: Path) -> None:
    rc, log, stderr = _run(tmp_path, args=("tests/x.py",), files=("tests/x.py",))
    assert rc == 0, stderr
    line = _run_line(log)
    assert " -e PRECIS_AUTO_N=1 " in f" {line} "
    assert " -n6 " not in f" {line} "


def test_node_id_selection_counts_for_auto_n(tmp_path: Path) -> None:
    rc, log, stderr = _run(
        tmp_path, args=("tests/x.py::test_a",), files=("tests/x.py",)
    )
    assert rc == 0, stderr
    assert " -e PRECIS_AUTO_N=1 " in f" {_run_line(log)} "


def test_directory_selection_keeps_n6_without_count(tmp_path: Path) -> None:
    rc, log, stderr = _run(tmp_path, args=("tests/",), files=("tests/x.py",))
    assert rc == 0, stderr
    line = _run_line(log)
    assert " -e PRECIS_AUTO_N=0 " in f" {line} "
    assert " -n6 " in f" {line} "


def test_missing_path_argument_is_refused(tmp_path: Path) -> None:
    """A positional arg that names no file is refused before any container
    (gr514948) - the auto -n0 heuristic never sees a mistyped path."""
    rc, log, stderr = _run(
        tmp_path, args=("tests/does_not_exist.py",), files=("tests/x.py",)
    )
    assert rc == 2, stderr
    assert "test path not found: tests/does_not_exist.py" in stderr
    assert not log


def test_caller_n_passes_through_unchanged(tmp_path: Path) -> None:
    rc, log, stderr = _run(tmp_path, args=("-n2", "tests/x.py"), files=("tests/x.py",))
    assert rc == 0, stderr
    line = _run_line(log)
    assert " -e PRECIS_AUTO_N=0 " in f" {line} "
    assert " -n2 " in f" {line} "


def test_heartbeat_during_silent_phase_and_none_after_exit(tmp_path: Path) -> None:
    """gr514502: a run that goes quiet gets backgrounded by the harness —
    heartbeat lines name the phase every interval, and stop at exit."""
    rc, log, stderr = _run(
        tmp_path,
        extra_env={"FAKE_RUN_SLEEP": "3", "PRECIS_TEST_HEARTBEAT_S": "1"},
        args=("-n0", "tests/x.py"),
        files=("tests/x.py",),
    )
    assert rc == 0, stderr
    beats = [
        line for line in stderr.splitlines() if "… " in line and "scripts/test:" in line
    ]
    assert len(beats) >= 2, f"expected >=2 heartbeat lines, got: {stderr!r}"
    assert any("pytest" in b for b in beats)


# --- --random order probe (test-loop-speed Decision 6) ----------------------
# pytest-randomly is not a baked dep — it bridges through UV_WITH the same
# way testmon does. PRECIS_RANDOM_SEED is a pytest arg (--randomly-seed),
# not a forwarded env var, and implies --random on its own.


def test_random_flag_bridges_pytest_randomly(tmp_path: Path) -> None:
    rc, log, stderr = _run(
        tmp_path, args=("--random", "tests/x.py"), files=("tests/x.py",)
    )
    assert rc == 0, stderr
    assert "-e UV_WITH=--with pytest-randomly==3.16.0" in _run_line(log)


def test_random_seed_implies_random_and_becomes_seed_arg(tmp_path: Path) -> None:
    rc, log, stderr = _run(
        tmp_path,
        args=("tests/x.py",),
        files=("tests/x.py",),
        extra_env={"PRECIS_RANDOM_SEED": "1234"},
    )
    assert rc == 0, stderr
    line = _run_line(log)
    assert "pytest-randomly==3.16.0" in line
    assert "--randomly-seed=1234" in line
    assert "-e PRECIS_RANDOM_SEED" not in line


def test_random_combines_with_later_flags(tmp_path: Path) -> None:
    """Flag parsing is a loop: --random before --fast must not strand --fast
    as a pytest arg (single-pass parsing would)."""
    rc, log, stderr = _run(
        tmp_path, args=("--random", "--fast", "tests/x.py"), files=("tests/x.py",)
    )
    assert rc == 0, stderr
    line = _run_line(log)
    assert "pytest-randomly==3.16.0" in line
    assert "not db and not slow" in line


# --- --selected --list: host-side preview, nothing runs (gr515640) ---------
# --selected RUNS the computed selection; --list prints it and exits before
# any docker call, so a harness-diff "FULL" answer is cheap information, not
# an accidental 70-minute suite.


def test_selected_list_prints_selection_without_running(tmp_path: Path) -> None:
    rc, log, stderr = _run(
        tmp_path,
        args=("--selected", "--list"),
        selector="#!/usr/bin/env bash\nprintf 'testmon=0\\ntests/test_a.py\\ntests/test_b.py\\n'\n",
    )
    assert rc == 0, stderr
    assert "checked selection" in stderr
    assert not log  # no container, no gate slot — the preview is host-side


def test_selected_list_surfaces_full_without_running(tmp_path: Path) -> None:
    """A harness/schema diff answers FULL — the gripe's worst case was that
    'previewing' ran the whole suite. --list prints FULL and exits."""
    rc, log, stderr = _run(
        tmp_path,
        args=("--selected", "--list"),
        selector="#!/usr/bin/env bash\nprintf 'FULL\\n'\n",
    )
    assert rc == 0, stderr
    assert not log


def test_list_without_selected_is_a_named_error(tmp_path: Path) -> None:
    rc, log, stderr = _run(
        tmp_path, args=("--list", "tests/x.py"), files=("tests/x.py",)
    )
    assert rc == 2
    assert "--selected --list" in stderr
    assert not log


# --- --co/--collect-only fast path (gr515705) ------------------------------
# A collection probe runs no tests: the run used to pay the gate-slot wait,
# the test-db bring-up AND the auto -n0 count pass (a second, hidden
# collection) before the list the caller asked for ever printed.


def test_collect_only_skips_db_and_runs_serial(tmp_path: Path) -> None:
    rc, log, stderr = _run(tmp_path, args=("--co", "tests/x.py"), files=("tests/x.py",))
    assert rc == 0, stderr
    line = _run_line(log)
    assert not [l for l in log if l.startswith("up|")], f"db was started: {log}"
    assert " --co " in f" {line} "
    assert " -n0 " in f" {line} "
    assert " -n6 " not in f" {line} "
    # the auto -n0 count pass is off — it would repeat the collection --co IS
    assert " -e PRECIS_AUTO_N=0 " in f" {line} "


def test_collect_only_long_form_matches(tmp_path: Path) -> None:
    rc, log, _ = _run(
        tmp_path, args=("--collect-only", "tests/x.py"), files=("tests/x.py",)
    )
    assert rc == 0
    assert not [l for l in log if l.startswith("up|")]


def test_normal_run_still_brings_db_up(tmp_path: Path) -> None:
    """Guard against over-matching: a plain run keeps its test DB."""
    rc, log, _ = _run(tmp_path, args=("-n0", "tests/x.py"), files=("tests/x.py",))
    assert rc == 0
    assert [l for l in log if l.startswith("up|")]


# --- --typecheck names its scope (gr515825) ---------------------------------


def test_typecheck_says_tests_are_included(tmp_path: Path) -> None:
    """The gripe: the run reported only "N source files" — a builder could
    not tell whether tests/ were checked and re-ran host mypy to be sure.
    The run now names the target set before mypy starts."""
    rc, _, stderr = _run(tmp_path, args=("--typecheck",))
    assert rc == 0, stderr
    assert "uv run mypy src tests" in stderr
    assert "BOTH src/ and tests/" in stderr


def test_typecheck_custom_targets_are_named(tmp_path: Path) -> None:
    rc, _, stderr = _run(tmp_path, args=("--typecheck", "src/precis"))
    assert rc == 0, stderr
    assert "uv run mypy src/precis" in stderr


# --- full-suite runs point at --bg (gr515646) -------------------------------
# A bare `scripts/test` (or a --selected that resolved FULL) is the run that
# outlives the ~10 min foreground shell cap; the run names the --bg/--await
# escape once, before the gate/db wait starts.


def test_full_suite_run_points_at_bg(tmp_path: Path) -> None:
    rc, _, stderr = _run(tmp_path, args=())
    assert rc == 0, stderr
    assert "scripts/test --bg" in stderr
    assert "--await" in stderr


def test_targeted_run_has_no_bg_hint(tmp_path: Path) -> None:
    rc, _, stderr = _run(tmp_path, args=("-n0", "tests/x.py"), files=("tests/x.py",))
    assert rc == 0, stderr
    assert "--bg" not in stderr

"""scripts/test output filter — gr514615, gr514718.

gr514615: every run printed pysbd ``SyntaxWarning: invalid escape
sequence`` lines (the image sets PYTHONDONTWRITEBYTECODE=1, so nothing is
ever cached — controller and each xdist worker recompile it) plus xdist's
``bringing up nodes...`` status, all above the result. The fix pre-compiles
pysbd's .pyc in the same ``uv run`` env (``prewarm-pycache.py``) and drops
the xdist status line in run_pytest's output filter.

gr514718: ``scripts/test tests/x.py -q`` stacked the caller's -q on the
script's own → -qq → pytest drops the ``N passed`` line entirely: bare
dots, nothing confirming the tests ran. A caller-supplied -q/-v now
replaces the script's -q instead of compounding.

Same staging harness as test_scripts_test_env.py: the real ``scripts/test``
+ its sourced libs in a throwaway git repo, a synthetic ``docker`` on PATH
that logs the ``compose run`` arg vector and can emit canned container
stdout. The real daemon is never contacted.
"""

from __future__ import annotations

import importlib.util
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
    "prewarm-pycache.py",
)

_FAKE_DOCKER = r"""#!/usr/bin/env bash
# Synthetic docker -- logs compose invocations; never touches a daemon.
#   FAKE_DOCKER_LOG   appended: up|<proj> / run|<proj>|<all args> / down|<proj>
#   FAKE_PS_SERVICES  newline-separated service names `docker ps` reports
#   FAKE_RUN_STDOUT   file whose contents `compose run` prints to stdout
#   FAKE_RUN_RC       exit status of `compose run` (default 0)
#   FAKE_RUN_RC_FILE  file of exit statuses, one per line — each `compose
#                     run` pops the first; overrides FAKE_RUN_RC
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
        [ -n "${FAKE_RUN_STDOUT:-}" ] && cat "$FAKE_RUN_STDOUT"
        if [ -n "${FAKE_RUN_RC_FILE:-}" ] && [ -s "$FAKE_RUN_RC_FILE" ]; then
          rc="$(head -n1 "$FAKE_RUN_RC_FILE")"
          tail -n +2 "$FAKE_RUN_RC_FILE" > "$FAKE_RUN_RC_FILE.tmp"
          mv "$FAKE_RUN_RC_FILE.tmp" "$FAKE_RUN_RC_FILE"
          exit "$rc"
        fi
        exit "${FAKE_RUN_RC:-0}"
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
    "UV_WITH",
    "FAKE_",
)


def _stage(tmp_path: Path) -> Path:
    """A throwaway repo directory holding the real script."""
    tree = tmp_path / "outtest"
    (tree / "scripts" / "lib").mkdir(parents=True)
    (tree / "docker" / "dev").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "test", tree / "scripts" / "test")
    for lib in _LIBS:
        shutil.copy2(REPO / "scripts" / "lib" / lib, tree / "scripts" / "lib" / lib)
    (tree / "docker" / "dev" / "compose.yaml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    # scripts/test refuses a positional path that does not exist (gr514948).
    (tree / "tests").mkdir()
    (tree / "tests" / "x.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    return tree


def _run(
    tmp_path: Path,
    *,
    args: tuple[str, ...] = ("-n0", "tests/x.py"),
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, list[str], str]:
    """Run staged scripts/test; return (rc, stdout, docker log, stderr)."""
    tree = _stage(tmp_path)
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
    env["TMPDIR"] = str(tmp_path)
    env.pop("FAKE_RUN_RC_FILE", None)
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
    return (
        proc.returncode,
        proc.stdout,
        log.read_text(encoding="utf-8").splitlines(),
        proc.stderr,
    )


def _run_line(log: list[str]) -> str:
    runs = [line for line in log if line.startswith("run|")]
    assert len(runs) == 1, f"expected exactly one compose run, got {log}"
    return runs[0]


def _pytest_args(run_line: str) -> str:
    """The arg vector tail after bash -lc's ``_`` — what pytest sees."""
    return run_line.rsplit(" _ ", 1)[1]


# --- gr514615: pysbd prewarm + xdist status filter ----------------------


def test_prewarm_is_chained_before_pytest(tmp_path: Path) -> None:
    """The pyc prewarm runs inside the same `uv run` env, first in the
    chain — check-core-deps.py's own `import pysbd` would otherwise emit
    the warnings before pytest ever started."""
    inner = (REPO / "scripts" / "lib" / "pytest-inner.sh").read_text(encoding="utf-8")
    assert inner.index("prewarm-pycache.py pysbd") < inner.index("check-core-deps.py")
    assert "scripts/lib/pytest-inner.sh" in (REPO / "scripts" / "test").read_text(
        encoding="utf-8"
    )


def test_xdist_status_line_is_filtered(tmp_path: Path) -> None:
    """The gripe verbatim: 'bringing up nodes...' never reaches the caller,
    while the real result output does."""
    noise = tmp_path / "noise.txt"
    noise.write_text(
        "bringing up nodes...\nbringing up nodes...\n..  [100%]\n2 passed in 1.00s\n",
        encoding="utf-8",
    )
    rc, out, log, stderr = _run(tmp_path, extra_env={"FAKE_RUN_STDOUT": str(noise)})
    assert rc == 0, stderr
    assert "bringing up nodes" not in out
    assert "2 passed in 1.00s" in out


def test_filter_preserves_nonzero_run_rc(tmp_path: Path) -> None:
    """The sed filter must not mask a red run (grep -v would also exit 1 on
    an all-filtered stream — sed exits 0, so the code stays compose's)."""
    noise = tmp_path / "noise.txt"
    noise.write_text("bringing up nodes...\n1 failed\n", encoding="utf-8")
    rc, out, _, _ = _run(
        tmp_path,
        extra_env={"FAKE_RUN_STDOUT": str(noise), "FAKE_RUN_RC": "5"},
    )
    assert rc == 5
    assert "bringing up nodes" not in out


def test_prewarm_silences_pysbd_import() -> None:
    """End-to-end in the dev env: after the prewarm, importing pysbd emits
    no SyntaxWarning — the .pyc it wrote is what the import reads."""
    if not importlib.util.find_spec("pysbd"):
        pytest.skip("pysbd not installed in this environment")
    prewarm = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "lib" / "prewarm-pycache.py"), "pysbd"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert prewarm.returncode == 0, prewarm.stderr
    assert "SyntaxWarning" not in prewarm.stderr
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pysbd, pathlib; "
            "assert any((pathlib.Path(pysbd.__path__[0]) / '__pycache__').glob('*.pyc'))",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert "SyntaxWarning" not in probe.stderr


def test_prewarm_missing_package_is_quiet_and_nonfatal() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "lib" / "prewarm-pycache.py"),
            "definitely-not-a-real-pkg-xyz",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stderr == ""


# --- gr514718: caller verbosity replaces, never compounds, script -q ----


def test_default_run_gets_one_q(tmp_path: Path) -> None:
    rc, _, log, stderr = _run(tmp_path)
    assert rc == 0, stderr
    assert _pytest_args(_run_line(log)).split().count("-q") == 1


def test_caller_q_does_not_compound(tmp_path: Path) -> None:
    """The gripe verbatim: `scripts/test x.py -q` used to pass -q twice →
    -qq → pytest drops the "N passed" summary (bare dots only)."""
    rc, _, log, stderr = _run(tmp_path, args=("-n0", "-q", "tests/x.py"))
    assert rc == 0, stderr
    assert _pytest_args(_run_line(log)).split().count("-q") == 1


def test_caller_verbose_replaces_q(tmp_path: Path) -> None:
    rc, _, log, stderr = _run(tmp_path, args=("-n0", "-v", "tests/x.py"))
    assert rc == 0, stderr
    tail = _pytest_args(_run_line(log)).split()
    assert "-v" in tail
    assert "-q" not in tail


def test_caller_qq_is_respected(tmp_path: Path) -> None:
    """An explicit -qq is the caller's own choice — passed through as-is,
    not silently clamped back to -q."""
    rc, _, log, stderr = _run(tmp_path, args=("-n0", "-qq", "tests/x.py"))
    assert rc == 0, stderr
    tail = _pytest_args(_run_line(log)).split()
    assert tail.count("-qq") == 1
    assert "-q" not in tail


# --- gr515155 / gr515188 / gr515811: compose quiet, git mount, OOM retry --


def test_compose_run_uses_quiet_progress(tmp_path: Path) -> None:
    """gr515155: compose's "Container … Creating/Started" lifecycle chatter
    is switched off at the source — --progress quiet on the compose call,
    not another output filter."""
    rc, _, log, stderr = _run(tmp_path)
    assert rc == 0, stderr
    assert "--progress quiet" in _run_line(log)


def test_git_common_dir_is_mounted(tmp_path: Path) -> None:
    """gr515188: the worktree's .git file points at the host git-common
    dir, which /app doesn't cover — it must be mounted at its own absolute
    path so repo-git tests run in-container."""
    rc, _, log, stderr = _run(tmp_path)
    assert rc == 0, stderr
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=tmp_path / "outtest",
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()
    assert f"-v {common}:{common}" in _run_line(log)


def test_typecheck_retries_once_on_oom_137(tmp_path: Path) -> None:
    """gr515811: the first --typecheck run OOM-killed (137) under sibling
    pressure — retry once in-call rather than making the caller re-run."""
    rc_file = tmp_path / "rcs"
    rc_file.write_text("137\n0\n", encoding="utf-8")
    rc, _, log, stderr = _run(
        tmp_path,
        args=("--typecheck",),
        extra_env={"FAKE_RUN_RC_FILE": str(rc_file)},
    )
    assert rc == 0
    runs = [line for line in log if line.startswith("run|")]
    assert len(runs) == 2, f"expected the one retry, got {runs}"
    assert "mypy" in runs[0]
    assert "retrying --typecheck once" in stderr


def test_typecheck_no_retry_on_plain_failure(tmp_path: Path) -> None:
    """Only 137 retries — a real mypy red must not double its run."""
    rc_file = tmp_path / "rcs"
    rc_file.write_text("1\n0\n", encoding="utf-8")
    rc, _, log, _ = _run(
        tmp_path,
        args=("--typecheck",),
        extra_env={"FAKE_RUN_RC_FILE": str(rc_file)},
    )
    assert rc == 1
    runs = [line for line in log if line.startswith("run|")]
    assert len(runs) == 1

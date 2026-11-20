"""``scripts/ship --remote`` lands past a red gate only on the helper's word
(gr739541).

``remote_gate_wait`` and ``_red_carried`` are cut out of the real
``scripts/ship`` and run under bash with a stubbed ``gh`` on PATH and a
shim in place of ``scripts/lib/red_carried.py`` — no network, no GitHub.
The verdict logic itself is ``tests/test_red_carried.py``; this file pins
the wiring: what the helper is asked, that only its exit 0 with ids turns
the red gate into a land, and that such a land says so in its trailers and
writes no deploy pin.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)

REPO = Path(__file__).resolve().parents[1]
SHIP = REPO / "scripts" / "ship"
SUBJECT_LIB = REPO / "scripts" / "lib" / "ship-subject.sh"


def _function(name: str) -> str:
    source = SHIP.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", source, re.S | re.M)
    assert match, f"{name} not found in scripts/ship"
    return match.group(0)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()


_GH_STUB = """\
#!/bin/sh
case "$1 $2" in
  "run list") echo 123 ;;
  "run view")
    case "$*" in
      *"--json url"*) echo https://example.invalid/run/123 ;;
      *"--json jobs"*) echo "  gate-fast (3.13, 3): failure" ;;
      *) echo "completed failure" ;;
    esac ;;
  *) echo "unexpected gh args: $*" >&2; exit 2 ;;
esac
"""

_SHIM = """\
#!/bin/sh
printf '%s\\n' "$*" > "$SHIM_ARGS"
[ -n "$SHIM_OUT" ] && printf '%s\\n' "$SHIM_OUT"
exit "$SHIM_RC"
"""


def _gate(
    tmp_path: Path,
    *,
    rc: int,
    out: str = "",
    prep: Callable[[Path], object] | None = None,
    **env: str,
):
    """Run ``remote_gate_wait`` on a red run; returns the process, the
    helper's argv (``None`` when it never ran) and the repo's two shas.
    ``prep`` gets the scratch repo just before the gate runs."""
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
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    shim = repo / "scripts" / "lib" / "red_carried.py"
    shim.parent.mkdir(parents=True)
    shim.write_text(_SHIM, encoding="utf-8")
    shim.chmod(0o755)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the diff")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "remote", "add", "origin", str(origin))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(_GH_STUB, encoding="utf-8")
    gh.chmod(0o755)
    args_file = tmp_path / "shim-args"
    if prep is not None:
        prep(repo)
    shell = (
        "set -euo pipefail\n"
        'BRANCH=test\nWORKTREE="$PWD"\nRED_CARRIED=stale\n'
        'say() { printf "%s\\n" "$*"; }\n'
        "sleep() { :; }\n"
        "_remote_gate_admit() { return 0; }\n"
        'git_network() { git "$@"; }\n'
        + _function("remote_gate_wait")
        + _function("_red_carried")
        + "rc=0; remote_gate_wait || rc=$?\n"
        'printf "rc=%s carried=[%s]\\n" "$rc" "$RED_CARRIED"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", shell],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "SHIM_ARGS": str(args_file),
            "SHIM_RC": str(rc),
            "SHIM_OUT": out,
            **env,
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    asked = (
        args_file.read_text(encoding="utf-8").strip() if args_file.exists() else None
    )
    return proc, asked, (base, head)


IDS = "tests/test_a.py::test_x tests/test_b.py::test_z[p 1]; base run 341"


def test_a_carried_red_turns_the_gate_into_a_land(tmp_path: Path) -> None:
    proc, asked, (base, head) = _gate(tmp_path, rc=0, out=IDS)
    assert f"rc=0 carried=[{IDS}]" in proc.stdout, proc.stderr
    assert "remote gate RED" in proc.stderr  # the red is still reported
    assert "Gate: red-carried" in proc.stdout
    assert asked is not None
    assert f"--run 123 --head {head} --base {base}" in asked


def test_a_red_the_helper_refuses_stays_a_red_gate(tmp_path: Path) -> None:
    proc, asked, _ = _gate(tmp_path, rc=1, out=IDS)
    assert "rc=1 carried=[]" in proc.stdout, proc.stderr
    assert asked is not None


def test_a_helper_that_names_no_id_stays_a_red_gate(tmp_path: Path) -> None:
    proc, _, _ = _gate(tmp_path, rc=0, out="")
    assert "rc=1 carried=[]" in proc.stdout, proc.stderr


def test_the_rule_can_be_turned_off(tmp_path: Path) -> None:
    proc, asked, _ = _gate(tmp_path, rc=0, out=IDS, PRECIS_RED_CARRIED="0")
    assert "rc=1 carried=[]" in proc.stdout, proc.stderr
    assert asked is None


def test_a_helper_that_cannot_run_stays_a_red_gate(tmp_path: Path) -> None:
    def no_exec_bit(repo: Path) -> None:
        (repo / "scripts" / "lib" / "red_carried.py").chmod(0o644)

    proc, asked, _ = _gate(tmp_path, rc=0, out=IDS, prep=no_exec_bit)
    assert "rc=1 carried=[]" in proc.stdout, proc.stderr
    assert asked is None


def test_a_base_that_cannot_be_found_stays_a_red_gate(tmp_path: Path) -> None:
    def no_origin_main(repo: Path) -> None:
        _git(repo, "update-ref", "-d", "refs/remotes/origin/main")

    proc, asked, _ = _gate(tmp_path, rc=0, out=IDS, prep=no_origin_main)
    assert "rc=1 carried=[]" in proc.stdout, proc.stderr
    assert asked is None


def test_the_land_carries_the_red_carried_trailer_above_the_others() -> None:
    """The in-lock section's own lines, run with the real trailer minter:
    the carried ids ride first, a forward-merge note keeps its own line."""
    source = SHIP.read_text(encoding="utf-8")
    match = re.search(
        r'^ +_gate_lines="\$GATE_TRAILER"\n.*?^ +TRAILERS="\$\(.*?\)"\n',
        source,
        re.S | re.M,
    )
    assert match, "the trailer composition is gone from scripts/ship"
    shell = (
        f'. "{SUBJECT_LIB}"\n'
        'MSG="fix(ship): x\n\nCo-Authored-By: A <a@example.invalid>"\n'
        'CO_AUTHOR=""; BRANCH_ATTRS=""\n'
        'RED_CARRIED="$1"; GATE_TRAILER="$2"\n'
        + match.group(0)
        + 'printf "%s\\n" "$TRAILERS"\n'
    )

    def trailers(carried: str, gate: str) -> list[str]:
        return subprocess.run(
            ["bash", "-c", shell, "_", carried, gate],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.splitlines()

    fwd = "Gate: forward-merged over 2 commits; tested base abc"
    author = "Co-Authored-By: A <a@example.invalid>"
    assert trailers(IDS, "") == [f"Gate: red-carried {IDS}", author]
    assert trailers(IDS, fwd) == [f"Gate: red-carried {IDS}", fwd, author]
    assert trailers("", fwd) == [fwd, author]
    assert trailers("", "") == [author]


def test_a_red_carried_land_writes_no_deploy_pin() -> None:
    """The pin is a deploy warrant; a tree whose gate was red is not one."""
    source = SHIP.read_text(encoding="utf-8")
    pin = next(
        line
        for line in source.splitlines()
        if line.startswith("if [[")
        and '-n "$GATED_SHA"' in line
        and 'DOCS_ONLY" != 1' in line
    )
    assert '-z "$RED_CARRIED"' in pin


def test_each_attempt_starts_with_no_carried_red() -> None:
    """A re-gate after a lost race must not inherit the last gate's ids."""
    source = SHIP.read_text(encoding="utf-8")
    loop = source.index("for (( attempt = 1; attempt <= MAX_ATTEMPTS; attempt++ )); do")
    reset = source.index('RED_CARRIED=""', loop)
    assert reset < source.index('say "syncing origin/main"', loop)

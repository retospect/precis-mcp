"""The repo-wide ship lock covers only the final merge section.

ship-gate-ci design note 4: lint, gates and the CI wait run UNLOCKED; the
lock is held for the seconds of fetch -> ancestor check -> [forward merge] ->
commit-tree -> CAS push -> local-main ff, then released explicitly.

These tests run the REAL ``scripts/ship`` (committed, with its libs, into a
throwaway repo) against REAL git: a bare ``origin.git``, a primary clone on
``main`` and linked worktrees on feature branches under
``<primary>/.claude/worktrees/``. The container gate is replaced through the
test-only ``PRECIS_SHIP_TEST_GATE_CMD`` seam by a fake lint that sleeps and
probes the lock dir; ``npx`` is a stub on PATH. Nothing reaches a real
remote: every clone's ``origin`` is the temp bare repo and no ``gh`` is
needed (no ``--remote``).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)

REPO = Path(__file__).resolve().parent.parent
_SHIP_SRC = REPO / "scripts" / "ship"
_BASH = "/bin/bash"  # macOS ships 3.2: the version scripts/ship must run on

_FAKE_LINT = r"""#!/usr/bin/env bash
# Fake lint/gate. PRECIS_FAKE_LINT_SLEEP seconds; marks start/finish in
# PRECIS_FAKE_LINT_DIR; with PRECIS_FAKE_LINT_PROBE=1 fails if the repo-wide
# ship lock dir exists at any probe. PRECIS_FAKE_LINT_NETFAIL=1 dies with
# uv's unreachable-index signature; PRECIS_FAKE_LINT_FAIL=1 is a plain red.
set -u
d="$PRECIS_FAKE_LINT_DIR"
name="$(basename "$PWD")"
common="$(cd "$(git rev-parse --git-common-dir)" && pwd)"
: > "$d/$name.started"
echo run >> "$d/$name.runs"
# PRECIS_FAKE_LINT_NETFAIL=1: die with uv's verbatim signature for a dead
# package-index route (gr496615) — ship must report a NETWORK failure, never
# "lint is RED". pypi.invalid is a reserved TLD, never a real host.
if [ "${PRECIS_FAKE_LINT_NETFAIL:-0}" = 1 ]; then
    echo 'error: Failed to build `precis-mcp @ file:///app`'
    echo '  cause: Failed to resolve requirements from `build-system.requires`'
    echo '  cause: Failed to fetch: https://pypi.invalid/simple/hatchling/'
    echo '  cause: error sending request for url (https://pypi.invalid/simple/hatchling/)'
    echo '  cause: client error (Connect)'
    echo '  cause: Connect timeout'
    exit 1
fi
# PRECIS_FAKE_LINT_FAIL=1: a plain red with no fetch signature — the control.
if [ "${PRECIS_FAKE_LINT_FAIL:-0}" = 1 ]; then
    echo "ruff: found 1 error"
    exit 1
fi
# PRECIS_FAKE_LINT_HOLD=1: run until the test creates $d/release (load-proof;
# the 300 s cap only bounds a hung test) instead of a fixed sleep.
end=$((SECONDS + ${PRECIS_FAKE_LINT_SLEEP:-4}))
if [ "${PRECIS_FAKE_LINT_HOLD:-0}" = 1 ]; then end=$((SECONDS + 300)); fi
while [ "$SECONDS" -lt "$end" ]; do
    if [ "${PRECIS_FAKE_LINT_HOLD:-0}" = 1 ] && { [ -e "$d/release" ] || [ -e "$d/$name.release" ]; }; then break; fi
    if [ "${PRECIS_FAKE_LINT_PROBE:-0}" = 1 ]; then
        if [ -d "$common/precis-ship.lock.d" ]; then
            echo present >> "$d/$name.lockseen"
            exit 1
        fi
        echo probe >> "$d/$name.probes"
    fi
    sleep 0.2
done
: > "$d/$name.finished"
"""


class Rig:
    """origin.git + primary + a racer clone, with worktrees added on demand."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.origin = tmp_path / "origin.git"
        self.primary = tmp_path / "primary"
        self.racer = tmp_path / "racer"
        self.lintdir = tmp_path / "lint"
        self.lintdir.mkdir()
        fakebin = tmp_path / "fakebin"
        fakebin.mkdir()
        # tailwind rebuild is best-effort: a failing npx is the WARN path.
        (fakebin / "npx").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        (fakebin / "npx").chmod(0o755)
        self.fake_lint = tmp_path / "fake_lint.sh"
        self.fake_lint.write_text(_FAKE_LINT, encoding="utf-8")
        gitcfg = tmp_path / "gitconfig"
        gitcfg.write_text(
            "[user]\n\tname = T\n\temail = t@example.invalid\n"
            "[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n"
            '[protocol "file"]\n\tallow = always\n',
            encoding="utf-8",
        )
        self.env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("PRECIS_")},
            "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
            "GIT_CONFIG_GLOBAL": str(gitcfg),
            "GIT_CONFIG_NOSYSTEM": "1",
            "PRECIS_NO_AUTOREAP": "1",
            "PRECIS_SQUAWK": "0",
            # Ship is quiet by default (gr497896): the transcript goes to a
            # /tmp log and only the epilogue reaches captured stdout. These
            # tests assert on the transcript, so they run it verbose;
            # PRECIS_SHIP_VERBOSE=0 in a call re-enables the quiet path.
            "PRECIS_SHIP_VERBOSE": "1",
            "PRECIS_CO_AUTHOR": "Test <t@example.invalid>",
            "PRECIS_SHIP_TEST_GATE_CMD": f"{_BASH} {self.fake_lint}",
            "PRECIS_FAKE_LINT_DIR": str(self.lintdir),
        }
        self._git_env = {**os.environ, **self.env}
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(self.origin)],
            check=True,
            env=self._git_env,
        )
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.primary)],
            check=True,
            capture_output=True,
            env=self._git_env,
        )
        self._stage_repo()
        self.g(self.primary, "add", "-A")
        self.g(self.primary, "commit", "-q", "-m", "base")
        self.g(self.primary, "push", "-q", "origin", "main")
        # A `gated` pointer already on origin, to prove a land leaves it alone.
        self.g(self.primary, "push", "-q", "origin", "main:refs/heads/gated")
        self.gated0 = self.origin_ref("gated")
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.racer)],
            check=True,
            capture_output=True,
            env=self._git_env,
        )

    def g(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self._git_env,
        ).stdout.strip()

    def _stage_repo(self) -> None:
        scripts = self.primary / "scripts"
        (scripts / "lib").mkdir(parents=True)
        shutil.copy2(_SHIP_SRC, scripts / "ship")
        for lib in (REPO / "scripts" / "lib").iterdir():
            if lib.is_file() and lib.suffix in (".sh", ".py"):
                shutil.copy2(lib, scripts / "lib" / lib.name)
        shutil.copy2(REPO / "scripts" / "migration-check", scripts / "migration-check")
        # Drift guard: "age unknown" never refuses (no gh in the rig).
        stub = scripts / "last-gated-main-sha"
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
        # --checked computes its selection via scripts/impacted-tests. The
        # real selector needs the project env + python index, which the rig
        # repo does not carry — stub it: emit $PRECIS_IMPACTED_STUB verbatim
        # when set (the test drives the full protocol: 'testmon=N' + targets,
        # or 'FULL', or a missing file for selector failure), else a minimal
        # 'testmon=0' + one target.
        stub = scripts / "impacted-tests"
        stub.write_text(
            "#!/bin/sh\n"
            # Drain stdin: ship pipes the changed list in, and a stub that exits
            # first makes printf die of SIGPIPE (pipefail => selector "failed").
            "cat >/dev/null\n"
            'if [ -n "${PRECIS_IMPACTED_STUB:-}" ]; then cat "$PRECIS_IMPACTED_STUB";'
            ' else printf "testmon=0\\ntests/test_stub.py\\n"; fi\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
        (self.primary / ".gitignore").write_text(
            ".claude/\n.ship-sha\ncoverage.xml\n", encoding="utf-8"
        )
        (self.primary / "src/precis/migrations").mkdir(parents=True)
        (self.primary / "src/precis/migrations/0001_init.sql").write_text(
            "select 1;\n", encoding="utf-8"
        )
        (self.primary / "src/precis/utils").mkdir(parents=True)
        (self.primary / "src/precis/utils/safe_fetch.py").write_text(
            "X = 0\n", encoding="utf-8"
        )

    def origin_ref(self, name: str) -> str:
        return self.g(self.origin, "rev-parse", f"refs/heads/{name}")

    def worktree(self, name: str, files: dict[str, str]) -> Path:
        wt = self.primary / ".claude" / "worktrees" / name
        self.g(self.primary, "worktree", "add", "-q", "-b", f"feat-{name}", str(wt))
        for rel, body in files.items():
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(body, encoding="utf-8")
        self.g(wt, "add", "-A")
        self.g(wt, "commit", "-q", "-m", f"work {name}")
        return wt

    def race(self, files: dict[str, str]) -> str:
        """A third party pushes to origin main; returns the new main sha."""
        self.g(self.racer, "pull", "-q", "--ff-only", "origin", "main")
        for rel, body in files.items():
            (self.racer / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.racer / rel).write_text(body, encoding="utf-8")
        self.g(self.racer, "add", "-A")
        self.g(self.racer, "commit", "-q", "-m", "racer")
        self.g(self.racer, "push", "-q", "origin", "HEAD:main")
        return self.origin_ref("main")

    def ship(
        self,
        wt: Path,
        *flags: str,
        sleep: float = 4,
        probe: bool = False,
        hold: bool = False,
        **env: str,
    ) -> tuple[subprocess.Popen[str], Path]:
        """Start scripts/ship. ``hold=True`` makes the fake gate wait for
        ``release()`` rather than for ``sleep`` seconds."""
        log = self.root / f"{wt.name}.log"
        full_env = {
            **self.env,
            "PRECIS_FAKE_LINT_SLEEP": str(sleep),
            "PRECIS_FAKE_LINT_PROBE": "1" if probe else "0",
            "PRECIS_FAKE_LINT_HOLD": "1" if hold else "0",
            **env,
        }
        fh = log.open("w", encoding="utf-8")
        proc = subprocess.Popen(
            [_BASH, "scripts/ship", *flags, "-m", f"chore(test): ship {wt.name}"],
            cwd=wt,
            env=full_env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        return proc, log

    def wait_lint_started(self, wt: Path, timeout: float = 60) -> None:
        marker = self.lintdir / f"{wt.name}.started"
        end = time.monotonic() + timeout
        while not marker.exists():
            assert time.monotonic() < end, f"{wt.name}: lint never started"
            time.sleep(0.1)

    def release(self, wt: Path | None = None) -> None:
        """Let held fake gates (``wt``'s only, else all; later legs too) finish."""
        (self.lintdir / (f"{wt.name}.release" if wt else "release")).touch()

    def lockdir(self) -> Path:
        return self.primary / ".git" / "precis-ship.lock.d"


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    r = Rig(tmp_path)
    yield r
    r.release()  # never leave a held fake gate running after a failed test


def _finish(proc: subprocess.Popen[str], log: Path, timeout: float = 120) -> str:
    rc = proc.wait(timeout=timeout)
    out = log.read_text(encoding="utf-8")
    assert rc == 0, f"ship exited {rc}:\n{out[-3000:]}"
    return out


def _tree_files(rig: Rig, ref: str = "origin/main") -> set[str]:
    return set(rig.g(rig.primary, "ls-tree", "-r", "--name-only", ref).splitlines())


def test_two_quick_ships_overlap_their_lint(rig: Rig) -> None:
    """Both land; both lints are in flight at once (lint is unlocked).

    Held gates make the overlap structural rather than a wall-time ratio: both
    lints have started before either is allowed to finish, which could not
    happen if one ship held the lock across its lint.
    """
    a = rig.worktree("a", {"a.txt": "a\n"})
    b = rig.worktree("b", {"b.txt": "b\n"})

    pa, la = rig.ship(a, "--quick", hold=True)
    pb, lb = rig.ship(b, "--quick", hold=True)
    rig.wait_lint_started(a)
    rig.wait_lint_started(b)
    assert not (rig.lintdir / "a.finished").exists()
    assert not (rig.lintdir / "b.finished").exists()
    # Staggered so the two final sections cannot meet: a lock wait in either
    # log could then only mean the lock was held across a lint.
    rig.release(a)
    out_a = _finish(pa, la)
    rig.release(b)
    out_b = _finish(pb, lb)

    assert {"a.txt", "b.txt"} <= _tree_files(rig)
    assert "waiting for the ship lock" not in out_a + out_b


def test_lock_dir_absent_while_lint_runs(rig: Rig) -> None:
    wt = rig.worktree("probe", {"p.txt": "p\n"})
    p, log = rig.ship(wt, "--quick", hold=True, probe=True)
    probes_file = rig.lintdir / "probe.probes"
    end = time.monotonic() + 60
    lockseen = rig.lintdir / "probe.lockseen"
    while not lockseen.exists() and not (
        probes_file.exists()
        and len(probes_file.read_text(encoding="utf-8").splitlines()) >= 5
    ):
        assert time.monotonic() < end, "the probe never ran (vacuous pass)"
        time.sleep(0.1)
    rig.release()
    out = _finish(p, log)
    probes = (rig.lintdir / "probe.probes").read_text(encoding="utf-8").splitlines()
    assert len(probes) >= 5, "the probe never ran (vacuous pass)"
    assert not (rig.lintdir / "probe.lockseen").exists(), out
    assert "p.txt" in _tree_files(rig)
    assert not rig.lockdir().exists()  # released after the ship, too


def test_quick_lost_race_forward_merges_and_lands(rig: Rig) -> None:
    wt = rig.worktree("q", {"q.txt": "q\n"})
    p, log = rig.ship(wt, "--quick", hold=True)
    rig.wait_lint_started(wt)
    racer_sha = rig.race({"racer.txt": "r\n"})
    rig.release()
    out = _finish(p, log)
    assert "forward-merged over 1 commits (lint not re-run)" in out
    assert {"q.txt", "racer.txt"} <= _tree_files(rig)
    msg = rig.g(rig.origin, "log", "-1", "--format=%B", "refs/heads/main")
    assert re.search(
        r"^Gate: lint only; forward-merged over 1 commits; linted base [0-9a-f]{40}$",
        msg,
        re.M,
    ), msg
    assert rig.g(rig.origin, "rev-parse", "refs/heads/main^") == racer_sha
    assert not rig.lockdir().exists()


def test_checked_ship_runs_lint_and_selection(rig: Rig) -> None:
    """--checked = pre-qland lint + the impacted-tests selection in one extra
    gate leg (the fake gate counts two runs), then the --quick merge
    machinery and the PARTIALLY TESTED banner."""
    wt = rig.worktree("chk", {"chk.txt": "c\n"})
    p, log = rig.ship(wt, "--checked", sleep=1)
    out = _finish(p, log)
    runs = (rig.lintdir / "chk.runs").read_text(encoding="utf-8").count("run")
    assert runs == 2, out  # lint leg + selected-pytest leg
    assert "checked gate" in out and "PARTIALLY TESTED" in out
    assert "chk.txt" in _tree_files(rig)


def test_checked_lost_race_trailer_names_the_selection(rig: Rig) -> None:
    """A forward-merged --checked land must not read as 'lint only' in the
    squash trailer — git log is how a reader tells what actually gated."""
    wt = rig.worktree("qc", {"qc.txt": "q\n"})
    p, log = rig.ship(wt, "--checked", hold=True)
    rig.wait_lint_started(wt)
    rig.race({"racer.txt": "r\n"})
    rig.release()
    out = _finish(p, log)
    assert "forward-merged over 1 commits (lint not re-run)" in out
    msg = rig.g(rig.origin, "log", "-1", "--format=%B", "refs/heads/main")
    assert re.search(
        r"^Gate: lint \+ selected pytest; forward-merged over 1 commits; linted base [0-9a-f]{40}$",
        msg,
        re.M,
    ), msg


def _ship_no_m(rig: Rig, wt: Path, *flags: str) -> tuple[int, str]:
    """A --quick-family ship that resolves its message from the branch
    commit (rig.ship always passes -m)."""
    log = rig.root / f"{wt.name}.log"
    fh = log.open("w", encoding="utf-8")
    proc = subprocess.run(
        [_BASH, "scripts/ship", *flags],
        cwd=wt,
        env={**rig.env, "PRECIS_FAKE_LINT_SLEEP": "1"},
        stdout=fh,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    fh.close()
    return proc.returncode, log.read_text(encoding="utf-8")


def test_quick_ship_keeps_the_branch_commit_footer(rig: Rig) -> None:
    """gr517803: the squash used to drop a non-Claude builder's trailers and
    substitute a Claude footer (Codex's 98aee0098 landed as d3c1217f5 with
    "Co-Authored-By: Claude"). Kept trailers land verbatim."""
    wt = rig.worktree("trail", {"t.txt": "t\n"})
    rig.g(
        wt,
        "commit",
        "-q",
        "--amend",
        "-m",
        "fix(ship): keep the author footer\n\n"
        "Co-Authored-By: Devin SWE-2 <noreply@cognition.ai>\n"
        "Claude-Session: none (Devin builder, GRIPE_FIX fleet)",
    )
    rc, out = _ship_no_m(rig, wt, "--quick")
    assert rc == 0, out
    msg = rig.g(rig.origin, "log", "-1", "--format=%B", "refs/heads/main")
    assert "Co-Authored-By: Devin SWE-2 <noreply@cognition.ai>" in msg
    assert "Claude-Session: none (Devin builder, GRIPE_FIX fleet)" in msg
    # and no substitute footer was minted on top (rig sets PRECIS_CO_AUTHOR)
    assert "Co-Authored-By: Test <t@example.invalid>" not in msg


def test_quick_ship_mints_the_fallback_footer_only_when_absent(rig: Rig) -> None:
    """The control: a trailerless branch commit still gets the session
    footer (PRECIS_CO_AUTHOR in the rig is Test <t@example.invalid>)."""
    wt = rig.worktree("notrail", {"n.txt": "n\n"})
    rig.g(wt, "commit", "-q", "--amend", "-m", "fix(ship): no trailer at all")
    rc, out = _ship_no_m(rig, wt, "--quick")
    assert rc == 0, out
    msg = rig.g(rig.origin, "log", "-1", "--format=%B", "refs/heads/main")
    assert re.search(r"^Co-Authored-By: Test <t@example\.invalid>$", msg, re.M), msg


def _stage_hooks_mirror(wt: Path, stub_rc: int) -> None:
    """The .claude/.devin mirror pair as the real repo tracks it, with a
    stub gen-devin-hooks whose exit code is the verdict."""
    (wt / ".claude").mkdir(exist_ok=True)
    (wt / ".claude" / "settings.json").write_text('{"hooks": {}}\n', encoding="utf-8")
    (wt / ".devin").mkdir(exist_ok=True)
    (wt / ".devin" / "hooks.v1.json").write_text("{}\n", encoding="utf-8")
    stub = wt / "scripts" / "gen-devin-hooks"
    stub.write_text(f"#!/bin/sh\nexit {stub_rc}\n", encoding="utf-8")
    stub.chmod(0o755)


def test_quick_ship_refuses_a_stale_devin_hooks_mirror(rig: Rig) -> None:
    """gr517403: render --check belongs to the --quick lint on EVERY diff
    class — .claude/settings.json is in the docs-only set, so the check runs
    before the docs/code lint split, or the mirror drift keeps slipping."""
    wt = rig.worktree("hook", {"h.txt": "h\n"})
    _stage_hooks_mirror(wt, 1)
    rig.g(wt, "add", "-f", ".claude/settings.json")
    rig.g(wt, "add", ".devin/hooks.v1.json", "scripts/gen-devin-hooks")
    # amend, not a second commit: a 2-commit branch dies earlier at
    # message resolution ("different type(scope) prefixes") and never
    # reaches the lint step under test.
    rig.g(wt, "commit", "-q", "--amend", "-m", "chore(test): stale mirror")
    rc, out = _ship_no_m(rig, wt, "--quick")
    assert rc != 0, out
    assert "hooks.v1.json is stale" in out
    assert "next=scripts/gen-devin-hooks render" in out
    # died before any gate leg ran
    assert not (rig.lintdir / "hook.started").exists()


def test_quick_ship_passes_a_fresh_devin_hooks_mirror(rig: Rig) -> None:
    """The control: an exit-0 check never blocks the lint."""
    wt = rig.worktree("hookok", {"h.txt": "h\n"})
    _stage_hooks_mirror(wt, 0)
    rig.g(wt, "add", "-f", ".claude/settings.json")
    rig.g(wt, "add", ".devin/hooks.v1.json", "scripts/gen-devin-hooks")
    rig.g(wt, "commit", "-q", "--amend", "-m", "chore(test): fresh mirror")
    rc, out = _ship_no_m(rig, wt, "--quick")
    assert rc == 0, out
    assert "h.txt" in _tree_files(rig)


@pytest.mark.parametrize(
    "flags",
    [["--checked", "--impacted"], ["--checked", "--remote"], ["--checked", "--full"]],
)
def test_checked_refuses_other_gates(rig: Rig, flags: list[str]) -> None:
    wt = rig.worktree("badcombo", {"b.txt": "b\n"})
    proc = subprocess.run(
        [_BASH, "scripts/ship", *flags, "-m", "chore(test): x"],
        cwd=wt,
        env=rig.env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 2
    assert "--checked" in proc.stdout + proc.stderr


def test_budget_exhausted_full_gate_lands_without_pin(rig: Rig) -> None:
    wt = rig.worktree("f", {"f.txt": "f\n"})
    p, log = rig.ship(wt, hold=True, PRECIS_SHIP_MAX_ATTEMPTS="1")
    rig.wait_lint_started(wt)
    racer_sha = rig.race({"racer.txt": "r\n"})
    rig.release()
    out = _finish(p, log)
    assert "not a deploy warrant" in out
    assert "merged forward over 1 commits" in out
    assert not (wt / ".ship-sha").exists()
    assert rig.origin_ref("gated") == rig.gated0, "origin/gated moved"
    assert {"f.txt", "racer.txt"} <= _tree_files(rig)
    msg = rig.g(rig.origin, "log", "-1", "--format=%B", "refs/heads/main")
    assert re.search(
        r"^Gate: forward-merged over 1 commits; tested base [0-9a-f]{40}$", msg, re.M
    ), msg
    assert rig.g(rig.origin, "rev-parse", "refs/heads/main^") == racer_sha
    assert not rig.lockdir().exists()


def test_no_tests_flag_is_rejected(rig: Rig) -> None:
    """--no-tests is gone: --quick IS the lint-only lane, --checked adds the
    impacted selection — an unknown flag must die at parse time."""
    wt = rig.worktree("nt", {"nt.py": "x = 1\n"})
    proc = subprocess.run(
        [_BASH, "scripts/ship", "--quick", "--no-tests", "-m", "chore(test): x"],
        cwd=wt,
        env=rig.env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 2
    assert "unknown flag --no-tests" in proc.stdout + proc.stderr


def test_checked_empty_selection_refuses(rig: Rig) -> None:
    """A selector that returns nothing on a non-empty diff is broken — the
    checked lane refuses rather than shipping an untested tree."""
    sel = rig.root / "sel.txt"
    sel.write_text("testmon=0\n", encoding="utf-8")
    wt = rig.worktree("emp", {"emp.py": "x = 1\n"})
    p, log = rig.ship(wt, "--checked", sleep=1, PRECIS_IMPACTED_STUB=str(sel))
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "selected nothing" in out
    assert not rig.lockdir().exists()


def test_checked_full_selection_refuses(rig: Rig) -> None:
    """A harness/schema diff has no honest subset: the selector answers
    FULL and --checked points at the heavier lanes."""
    sel = rig.root / "sel.txt"
    sel.write_text("FULL\n", encoding="utf-8")
    wt = rig.worktree("ful", {"ful.py": "x = 1\n"})
    p, log = rig.ship(wt, "--checked", sleep=1, PRECIS_IMPACTED_STUB=str(sel))
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "selector answers FULL" in out
    assert "--full" in out
    assert not rig.lockdir().exists()


def test_checked_selector_failure_refuses(rig: Rig) -> None:
    sel = rig.root / "missing.txt"  # the stub cats this; absent -> cat exits 1
    wt = rig.worktree("sf", {"sf.py": "x = 1\n"})
    p, log = rig.ship(wt, "--checked", sleep=1, PRECIS_IMPACTED_STUB=str(sel))
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "cannot compute the checked selection" in out
    assert not rig.lockdir().exists()


def test_gate_network_failure_is_not_a_lint_red(rig: Rig) -> None:
    """gr496615: a dead package-index route inside the gate is a NETWORK
    failure on a distinct exit path (rc 68, the unreachable host named) —
    never "pre-qland lint is RED"."""
    wt = rig.worktree("net", {"net.txt": "n\n"})
    p, log = rig.ship(wt, "--quick", sleep=1, PRECIS_FAKE_LINT_NETFAIL="1")
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc == 68, out
    assert "NETWORK failure" in out
    assert "pypi.invalid" in out
    assert "lint is RED" not in out
    assert not rig.lockdir().exists()


def test_gate_plain_failure_stays_a_lint_red(rig: Rig) -> None:
    """Control for the split: a red with no fetch signature keeps the
    ordinary die path — the network check must not blanket-relabel."""
    wt = rig.worktree("red", {"red.txt": "r\n"})
    p, log = rig.ship(wt, "--quick", sleep=1, PRECIS_FAKE_LINT_FAIL="1")
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc == 1, out
    assert "pre-qland lint is RED" in out
    assert "NETWORK failure" not in out


def test_exact_land_full_gate_pins_and_has_no_trailer(rig: Rig) -> None:
    """Control for the test above: with no race the pin IS written."""
    wt = rig.worktree("c", {"c.txt": "c\n"})
    p, log = rig.ship(wt, sleep=1, PRECIS_SHIP_MAX_ATTEMPTS="1")
    out = _finish(p, log)
    sha = rig.origin_ref("main")
    assert (wt / ".ship-sha").read_text(encoding="utf-8").strip() == sha
    assert rig.origin_ref("gated") == sha, out
    msg = rig.g(rig.origin, "log", "-1", "--format=%B", "refs/heads/main")
    assert "Gate:" not in msg
    assert "not a deploy warrant" not in out


def test_race_retry_prints_budget_and_elapsed(rig: Rig) -> None:
    """A full-gate loss with budget left re-gates (unlocked) and says what's left."""
    wt = rig.worktree("r", {"r.txt": "r\n"})
    p, log = rig.ship(wt, hold=True, PRECIS_SHIP_MAX_ATTEMPTS="2")
    rig.wait_lint_started(wt)
    rig.race({"racer.txt": "r\n"})
    rig.release()
    out = _finish(p, log)
    assert re.search(r"race budget: 1 of 2 attempts left; elapsed \d+m ?\d+s", out), out
    assert "not a deploy warrant" not in out  # the re-gate landed it exactly
    assert (wt / ".ship-sha").exists()


def test_migration_number_collision_with_merged_range_dies(rig: Rig) -> None:
    wt = rig.worktree(
        "m", {"src/precis/migrations/0002_ours.sql": "select 2;\n", "m.txt": "m\n"}
    )
    p, log = rig.ship(wt, hold=True, PRECIS_SHIP_MAX_ATTEMPTS="1")
    rig.wait_lint_started(wt)
    racer_sha = rig.race({"src/precis/migrations/0002_theirs.sql": "select 3;\n"})
    rig.release()
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "migration number collision" in out, out
    assert "0002_ours.sql" in out and "0002_theirs.sql" in out
    assert rig.origin_ref("main") == racer_sha, "main must be unchanged"
    assert not rig.lockdir().exists(), "the lock must be released after the die"


def test_migrations_are_numbered_per_plugin_dir(rig: Rig) -> None:
    """0016 in precis_se vs 0016 in precis_chem is two ledgers, not a collision."""
    wt = rig.worktree("plug", {"src/precis_se/migrations/0016_a.sql": "select 1;\n"})
    p, log = rig.ship(wt, hold=True, PRECIS_SHIP_MAX_ATTEMPTS="1")
    rig.wait_lint_started(wt)
    rig.race({"src/precis_chem/migrations/0016_b.sql": "select 2;\n"})
    rig.release()
    out = _finish(p, log)
    assert "migration number collision" not in out
    assert {
        "src/precis_se/migrations/0016_a.sql",
        "src/precis_chem/migrations/0016_b.sql",
    } <= _tree_files(rig)


def test_stale_migration_mint_dies_naming_the_next_free_number(rig: Rig) -> None:
    """gr489414: the 0216 double-mint. The sibling's 0002 lands BEFORE our
    ship runs — by sync time the collision is inside our merge-base, outside
    the in-lock guard's window; the stale-mint check must refuse and name
    origin/main's next free number."""
    wt = rig.worktree(
        "m", {"src/precis/migrations/0002_ours.sql": "select 2;\n", "m.txt": "m\n"}
    )
    racer_sha = rig.race({"src/precis/migrations/0002_theirs.sql": "select 3;\n"})
    p, log = rig.ship(wt, "--quick", sleep=1)
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "migration number collision" in out, out
    assert "0002_ours.sql" in out and "0002_theirs.sql" in out
    assert "0003" in out, "the refusal must print origin/main's next free number"
    assert rig.origin_ref("main") == racer_sha, "main must be unchanged"
    assert not rig.lockdir().exists()


def test_migration_number_above_next_free_dies(rig: Rig) -> None:
    """A gap mint is the same stale-base tell: 0003 against main's 0001 means
    the branch minted off a tree that predates main's current tip — or typed
    a number by hand. Either way it renumbers to 0002 before it ships."""
    wt = rig.worktree("m", {"src/precis/migrations/0003_gap.sql": "select 2;\n"})
    p, log = rig.ship(wt, "--quick", sleep=1)
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "next free number" in out and "0002" in out, out
    assert "0003_gap.sql" not in _tree_files(rig)  # never landed


def test_contiguous_migrations_at_next_free_land(rig: Rig) -> None:
    """Control: numbers that ARE main's next free (a two-migration branch
    minted off current main) must not trip the guard."""
    wt = rig.worktree(
        "m",
        {
            "src/precis/migrations/0002_a.sql": "select 2;\n",
            "src/precis/migrations/0003_b.sql": "select 3;\n",
        },
    )
    p, log = rig.ship(wt, "--quick", sleep=1)
    _finish(p, log)
    assert {
        "src/precis/migrations/0002_a.sql",
        "src/precis/migrations/0003_b.sql",
    } <= _tree_files(rig)


def test_new_plugin_ledger_mints_at_any_start(rig: Rig) -> None:
    """A migrations/ dir main has never seen has no number to fork — the
    branch's own mint starts the ledger (keeps the 0016-style plugin-dir
    convention of test_migrations_are_numbered_per_plugin_dir legal)."""
    wt = rig.worktree(
        "newplug", {"src/precis_new/migrations/0016_a.sql": "select 1;\n"}
    )
    p, log = rig.ship(wt, "--quick", sleep=1)
    _finish(p, log)
    assert "src/precis_new/migrations/0016_a.sql" in _tree_files(rig)


def test_shared_origin_main_ref_moved_after_the_in_lock_fetch_cannot_revert(
    rig: Rig,
) -> None:
    """A sibling's fetch moves the SHARED refs/remotes/origin/main right after
    our in-lock ancestor check. The section must stay keyed to the tip it
    fetched: landing on top of the sibling-fetched M2 (which HEAD lacks) would
    revert M2. (The shim fires after the first `merge-base --is-ancestor` under
    the lock; reading origin/main after that check reproduces the bug.)"""
    real_git = shutil.which("git")
    assert real_git
    shimdir = rig.root / "gitshim"
    shimdir.mkdir()
    mark = rig.root / "shim.fired"
    shim = shimdir / "git"
    shim.write_text(
        f"""#!/bin/bash
"{real_git}" "$@"
rc=$?
if [ "${{1:-}}" = merge-base ] && [ "${{2:-}}" = --is-ancestor ] && [ ! -e "{mark}" ] && [ -d "{rig.primary}/.git/precis-ship.lock.d" ]; then
    : > "{mark}"
    (
        cd "{rig.racer}" || exit 0
        unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
        "{real_git}" fetch -q origin main && "{real_git}" reset -q --hard origin/main
        echo m2 > m2.txt
        "{real_git}" add -A && "{real_git}" commit -q -m "M2 (sibling push)" \\
            && "{real_git}" push -q origin HEAD:main
        # a sibling worktree's unlocked fetch: moves the shared tracking ref
        cd "{rig.primary}" && "{real_git}" fetch -q origin main
    )
fi
exit $rc
""",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    wt = rig.worktree("m2", {"m2_ours.txt": "o\n"})
    p, log = rig.ship(
        wt,
        "--quick",
        sleep=1,
        PATH=f"{shimdir}{os.pathsep}{rig.env['PATH']}",
    )
    out = _finish(p, log)
    assert mark.exists(), "the shim never fired (vacuous pass)"
    files = _tree_files(rig)
    assert {"m2_ours.txt", "m2.txt"} <= files, f"M2 was reverted: {sorted(files)}"
    assert "in-lock try 1/5" in out, out  # the stale lease lost, then it re-merged
    main = rig.origin_ref("main")
    assert rig.g(rig.origin, "log", "--format=%s", main).count("M2 (sibling push)") == 1


def test_non_cas_push_failure_dies_at_once(rig: Rig) -> None:
    wt = rig.worktree("hookfail", {"hf.txt": "h\n"})
    hook = rig.primary / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(
        "#!/bin/sh\nwhile read l ls r rs; do\n"
        '  [ "$r" = refs/heads/main ] && { echo "policy hook says no" >&2; exit 1; }\n'
        "done\nexit 0\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    p, log = rig.ship(wt, "--quick", sleep=1)
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "for a reason other than a lost race" in out, out
    assert "policy hook says no" in out
    assert "in-lock try" not in out, "a non-CAS failure must not be retried"
    assert "hf.txt" not in _tree_files(rig)
    assert not rig.lockdir().exists()


def test_safe_fetch_on_both_sides_refuses_the_forward_merge(rig: Rig) -> None:
    wt = rig.worktree("s", {"src/precis/utils/safe_fetch.py": "X = 1\n"})
    p, log = rig.ship(wt, "--quick", hold=True)
    rig.wait_lint_started(wt)
    racer_sha = rig.race({"src/precis/utils/safe_fetch.py": "X = 2\n"})
    rig.release()
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "safe_fetch.py changed on both sides" in out, out
    assert rig.origin_ref("main") == racer_sha
    assert not rig.lockdir().exists()


# ───────────── in-lock retry on a lost CAS + termination under arrivals ─────────────


def _install_racing_pre_push(rig: Rig, limit: int) -> Path:
    """A pre-push hook that makes the ship's push to main lose the CAS.

    Before each of the first ``limit`` pushes to ``refs/heads/main`` leaves the
    ship, the hook pushes a racer commit from the separate clone: "another
    host" landing inside the ship's lock. Returns the counter file.
    """
    counter = rig.root / "prepush.count"
    hook = rig.primary / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(
        f"""#!/bin/sh
hit=
while read lref lsha rref rsha; do
    [ "$rref" = refs/heads/main ] && hit=1
done
[ -n "$hit" ] || exit 0
n=$(cat "{counter}" 2>/dev/null || echo 0)
[ "$n" -ge {limit} ] && exit 0
n=$((n + 1))
echo "$n" > "{counter}"
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX
cd "{rig.racer}" || exit 0
git fetch -q origin main && git reset -q --hard origin/main
echo "$n" > "hook_$n.txt"
git add -A && git commit -q -m "hook racer $n" && git push -q origin HEAD:main
exit 0
""",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    return counter


def _runs(rig: Rig, wt: Path) -> int:
    f = rig.lintdir / f"{wt.name}.runs"
    return len(f.read_text(encoding="utf-8").splitlines()) if f.exists() else 0


def _landed_message(rig: Rig, subject: str) -> str:
    return rig.g(
        rig.origin, "log", "-1", "--format=%B", f"--grep=^{subject}$", "refs/heads/main"
    )


@contextmanager
def _arrivals(rig: Rig, interval: float) -> Iterator[list[int]]:
    """Push a new commit to origin main every ``interval`` s from the racer clone.

    The loop is "another host": it never takes the ship lock. Always stopped
    on exit. Yields the list of pushes that succeeded.
    """
    stop = threading.Event()
    pushed: list[int] = []

    def run(*args: str) -> bool:
        return (
            subprocess.run(
                ["git", *args],
                cwd=rig.racer,
                env=rig._git_env,
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )

    def loop() -> None:
        i = 0
        while not stop.is_set():
            i += 1
            if run("fetch", "-q", "origin", "main") and run(
                "reset", "-q", "--hard", "origin/main"
            ):
                (rig.racer / f"arrival_{i}.txt").write_text("x\n", encoding="utf-8")
                if (
                    run("add", "-A")
                    and run("commit", "-q", "-m", f"arrival {i}")
                    and run("push", "-q", "origin", "HEAD:main")
                ):
                    pushed.append(i)
            stop.wait(interval)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    try:
        yield pushed
    finally:
        stop.set()
        t.join(timeout=30)


def test_lost_cas_in_lock_retries_merge_and_push_without_regating(rig: Rig) -> None:
    wt = rig.worktree("retry", {"retry.txt": "r\n"})
    base = rig.origin_ref("main")
    counter = _install_racing_pre_push(rig, limit=2)
    p, log = rig.ship(wt, sleep=1, PRECIS_SHIP_MAX_ATTEMPTS="1")
    out = _finish(p, log)
    assert counter.read_text(encoding="utf-8").strip() == "2"
    assert "in-lock try 1/5" in out and "in-lock try 2/5" in out, out
    assert "could not ship after" not in out
    assert _runs(rig, wt) == 1, "the gate must not re-run on an in-lock retry"
    assert {"retry.txt", "hook_1.txt", "hook_2.txt"} <= _tree_files(rig)
    msg = _landed_message(rig, "chore(test): ship retry")
    # try 1 pushed the exact tree and lost; tries 2 and 3 merged one commit each
    assert f"Gate: forward-merged over 2 commits; tested base {base}" in msg, msg
    assert not (wt / ".ship-sha").exists()
    assert rig.origin_ref("gated") == rig.gated0
    assert not rig.lockdir().exists()


def test_in_lock_retries_are_bounded_then_named_die(rig: Rig) -> None:
    wt = rig.worktree("bound", {"bound.txt": "b\n"})
    counter = _install_racing_pre_push(rig, limit=1000)
    p, log = rig.ship(wt, sleep=1, PRECIS_SHIP_MAX_ATTEMPTS="1")
    rc = p.wait(timeout=180)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "CAS push rejected 5× inside the ship lock" in out, out
    assert counter.read_text(encoding="utf-8").strip() == "5"
    assert _runs(rig, wt) == 1
    assert "bound.txt" not in _tree_files(rig), "nothing may have landed"
    assert not (wt / ".ship-sha").exists()
    assert not rig.lockdir().exists(), "the die must release the lock"


def test_last_attempt_cas_loss_stays_in_lock_not_could_not_ship(rig: Rig) -> None:
    """Budget 2, CAS lost on attempt 1 (re-gate) then attempt 2 (last) is raced
    once more: in-lock retry, never "could not ship after N attempts"."""
    wt = rig.worktree("last", {"last.txt": "l\n"})
    _install_racing_pre_push(rig, limit=2)
    p, log = rig.ship(wt, sleep=1, PRECIS_SHIP_MAX_ATTEMPTS="2")
    out = _finish(p, log)
    assert "could not ship after" not in out
    assert "in-lock try 1/5" in out, out
    assert _runs(rig, wt) == 2
    assert "last.txt" in _tree_files(rig)


@pytest.mark.parametrize("mode", ["full", "quick"])
def test_termination_under_continuous_arrivals(rig: Rig, mode: str) -> None:
    """A push lands on main every ~1 s for the whole run (another host).

    The ship must land once the budget is spent, with the gate run once.
    """
    wt = rig.worktree(f"cont{mode}", {f"cont_{mode}.txt": "c\n"})
    with _arrivals(rig, 1.0) as pushed:
        time.sleep(1.5)  # arrivals are flowing before the ship starts
        if mode == "full":
            p, log = rig.ship(wt, sleep=4, PRECIS_SHIP_MAX_ATTEMPTS="1")
        else:
            p, log = rig.ship(wt, "--quick", sleep=4)
        out = _finish(p, log, timeout=240)
        n_pushed = len(pushed)
    tries = len(re.findall(r"in-lock try \d+/5", out))
    print(f"arrivals[{mode}]: {n_pushed} pushes; {tries} in-lock CAS losses")
    assert n_pushed >= 3, f"the arrivals loop barely ran ({n_pushed} pushes)"
    assert _runs(rig, wt) == 1, "budget 1 means at most one gate run"
    assert f"cont_{mode}.txt" in _tree_files(rig)
    msg = _landed_message(rig, f"chore(test): ship cont{mode}")
    if mode == "full":
        assert "not a deploy warrant" in out
        assert re.search(
            r"^Gate: forward-merged over \d+ commits; tested base [0-9a-f]{40}$",
            msg,
            re.M,
        ), msg
        assert not (wt / ".ship-sha").exists()
        assert rig.origin_ref("gated") == rig.gated0
    else:
        assert "forward-merged over" in out
        assert re.search(
            r"^Gate: lint only; forward-merged over \d+ commits", msg, re.M
        )
    assert not rig.lockdir().exists()


# ───────────────────────── steal threshold + timeout helper ─────────────────────────


def _acquire_fn() -> str:
    text = _SHIP_SRC.read_text(encoding="utf-8")
    m = re.search(r"^acquire_ship_lock\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert m, "acquire_ship_lock() not found in scripts/ship"
    return m.group(0)


def _age(path: Path, minutes: float) -> None:
    old = time.time() - minutes * 60
    os.utime(path, (old, old))


def _try_acquire(tmp_path: Path, age_min: float, env_min: str | None = None) -> str:
    """Run the REAL acquire_ship_lock against an other-host holder file.

    Prints STOLEN-AND-ACQUIRED, or WAITING if it would have slept.
    """
    lockdir = tmp_path / "precis-ship.lock.d"
    lockdir.mkdir()
    (lockdir / "holder").write_text(
        "/elsewhere pid=424242 host=some-other-host-x\n", encoding="utf-8"
    )
    _age(lockdir, age_min)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PRECIS_")}
    if env_min is not None:
        env["PRECIS_SHIP_LOCK_STEAL_MIN"] = env_min
    script = f"""
set -u
source "{REPO}/scripts/lib/lock-holder.sh"
say() {{ printf '%s\\n' "$*"; }}
sleep() {{ echo WAITING; exit 7; }}
LOCKDIR="{lockdir}"
{_acquire_fn()}
acquire_ship_lock
echo STOLEN-AND-ACQUIRED
"""
    proc = subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        env=env,
    )
    return proc.stdout


def test_other_host_holder_aged_six_minutes_is_stolen(tmp_path: Path) -> None:
    assert "STOLEN-AND-ACQUIRED" in _try_acquire(tmp_path, 6)


def test_other_host_holder_aged_four_minutes_is_waited_out(tmp_path: Path) -> None:
    out = _try_acquire(tmp_path, 4)
    assert "WAITING" in out and "STOLEN" not in out


def test_steal_threshold_is_configurable(tmp_path: Path) -> None:
    assert "STOLEN-AND-ACQUIRED" in _try_acquire(tmp_path, 2, env_min="1")


def test_locked_net_helper_bounds_a_hung_command() -> None:
    text = _SHIP_SRC.read_text(encoding="utf-8")
    m = re.search(r"^_locked_net\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert m
    script = f"LOCKED_NET_TIMEOUT=1\n{m.group(0)}\n_locked_net sleep 20; echo rc=$?\n"
    t0 = time.monotonic()
    proc = subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert "rc=142" in proc.stdout, proc.stdout + proc.stderr
    assert time.monotonic() - t0 < 10


_CI_EXTRAS = "--all-extras --no-extra catalyst --no-extra catalyst-gpu"


def test_gate_env_sync_carries_the_ci_extras_set() -> None:
    """A bare `uv sync` is exact and extras-less: it pruned the web extra
    (fastapi) from every fresh tree and mypy reported import-not-found
    across the repo. Both sync legs must carry CI's extras flags, and those
    flags must be the ones check.yml and the Dockerfile use."""
    ship = _SHIP_SRC.read_text(encoding="utf-8")
    m = re.search(r"^GATE_SYNC_EXTRAS='([^']+)'", ship, re.MULTILINE)
    assert m, "scripts/ship lost GATE_SYNC_EXTRAS"
    flags = m.group(1)
    assert flags.startswith(_CI_EXTRAS)
    assert "--inexact" in flags.split()  # never prune what the image bakes
    assert 'gate_sh "uv sync --offline ${GATE_SYNC_EXTRAS}"' in ship
    assert 'gate_sh "uv sync ${GATE_SYNC_EXTRAS}"' in ship
    assert "gate_sh 'uv sync" not in ship  # no bare, extras-less sync
    check = (REPO / ".github/workflows/check.yml").read_text(encoding="utf-8")
    assert f"uv run {_CI_EXTRAS} mypy src tests" in check
    dockerfile = (REPO / "docker/Dockerfile").read_text(encoding="utf-8")
    assert f'ARG PRECIS_DEPS_EXTRAS="{_CI_EXTRAS}"' in dockerfile


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_gate_extras_keep_the_web_extra_in_the_locked_env() -> None:
    """The set the gate syncs resolves fastapi (the web extra) from uv.lock —
    the package the bare sync uninstalled."""
    r = subprocess.run(
        ["uv", "export", "--frozen", "--no-hashes", "--offline", *_CI_EXTRAS.split()],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert re.search(r"^fastapi==", r.stdout, re.MULTILINE)


def test_qland_lint_cmd_runs_mypy_on_src_and_tests() -> None:
    """gr514402: a func-returns-value slip reached a --quick land because the
    builder trusted the lint's green; the lane has run `mypy src tests` since
    9485ab4da. Pin the command so a lint slimming cannot drop it silently —
    --quick's promise is that the failures that cost hours (ruff-format
    drift, mypy clusters) are caught locally."""
    ship = _SHIP_SRC.read_text(encoding="utf-8")
    cmds = re.findall(r'QLAND_CMD="([^"]+)"', ship)
    assert cmds, "scripts/ship lost its QLAND_CMD lint commands"
    # the docs-only command is $HYGIENE alone; a diff carrying .py must pay mypy
    assert any("uv run mypy src tests" in c for c in cmds), cmds


def _expanded_sync_flags(machine: str, tmp_path: Path) -> list[str]:
    """GATE_SYNC_EXTRAS as the gate container's shell expands it, with `uname -m`
    faked to ``machine``."""
    ship = _SHIP_SRC.read_text(encoding="utf-8")
    m = re.search(r"^GATE_SYNC_EXTRAS='([^']+)'", ship, re.MULTILINE)
    assert m
    fake = tmp_path / "uname"
    fake.write_text(f"#!{_BASH}\necho {machine}\n", encoding="utf-8")
    fake.chmod(0o755)
    proc = subprocess.run(
        [_BASH, "-c", f'PATH="{tmp_path}:$PATH"; echo {m.group(1)}'],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    return proc.stdout.split()


@pytest.mark.parametrize("machine", ["aarch64", "arm64"])
def test_gate_sync_drops_no_wheel_extras_on_aarch64(
    machine: str, tmp_path: Path
) -> None:
    """struct-search -> agox -> dscribe has no aarch64 Linux wheel and the gate
    image has no compiler: the sync must not request it there."""
    flags = _expanded_sync_flags(machine, tmp_path)
    assert flags[:2] == ["--all-extras", "--no-extra"]
    assert "struct-search" in flags
    assert flags[flags.index("struct-search") - 1] == "--no-extra"
    assert "--inexact" in flags


def test_gate_sync_keeps_every_extra_on_x86(tmp_path: Path) -> None:
    flags = _expanded_sync_flags("x86_64", tmp_path)
    assert "struct-search" not in flags
    assert "--all-extras" in flags


def _net_host(log_text: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    text = _SHIP_SRC.read_text(encoding="utf-8")
    m = re.search(r"^gate_net_host\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert m
    log = tmp_path / "gate.log"
    log.write_text(log_text, encoding="utf-8")
    return subprocess.run(
        [_BASH, "-c", f'{m.group(0)}\ngate_net_host "{log}"'],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


def test_source_build_failure_is_not_classified_as_network(tmp_path: Path) -> None:
    """uv words a missing-compiler sdist build "Failed to download and build";
    that must not map to the NETWORK rc 68 path."""
    r = _net_host(
        "error: Failed to download and build `dscribe==2.1.2`\n"
        "  Caused by: Failed to build `dscribe==2.1.2`\n"
        "  error: command 'g++' failed: No such file or directory\n",
        tmp_path,
    )
    assert r.returncode != 0 and not r.stdout.strip()


def test_real_fetch_failure_still_names_the_host(tmp_path: Path) -> None:
    r = _net_host(
        "error: Failed to fetch: `https://pypi.example.org/simple/fastapi/`\n",
        tmp_path,
    )
    assert r.returncode == 0 and r.stdout.strip() == "pypi.example.org"

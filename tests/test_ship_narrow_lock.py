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
# ship lock dir exists at any probe.
set -u
d="$PRECIS_FAKE_LINT_DIR"
name="$(basename "$PWD")"
common="$(cd "$(git rev-parse --git-common-dir)" && pwd)"
: > "$d/$name.started"
echo run >> "$d/$name.runs"
end=$((SECONDS + ${PRECIS_FAKE_LINT_SLEEP:-4}))
while [ "$SECONDS" -lt "$end" ]; do
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
        self, wt: Path, *flags: str, sleep: float = 4, probe: bool = False, **env: str
    ) -> tuple[subprocess.Popen[str], Path]:
        log = self.root / f"{wt.name}.log"
        full_env = {
            **self.env,
            "PRECIS_FAKE_LINT_SLEEP": str(sleep),
            "PRECIS_FAKE_LINT_PROBE": "1" if probe else "0",
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

    def lockdir(self) -> Path:
        return self.primary / ".git" / "precis-ship.lock.d"


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def _finish(proc: subprocess.Popen[str], log: Path, timeout: float = 120) -> str:
    rc = proc.wait(timeout=timeout)
    out = log.read_text(encoding="utf-8")
    assert rc == 0, f"ship exited {rc}:\n{out[-3000:]}"
    return out


def _tree_files(rig: Rig, ref: str = "origin/main") -> set[str]:
    return set(rig.g(rig.primary, "ls-tree", "-r", "--name-only", ref).splitlines())


def test_two_quick_ships_overlap_their_lint(rig: Rig) -> None:
    """Both land; wall time ~ one lint, not two (lint is unlocked)."""
    solo = rig.worktree("solo", {"solo.txt": "s\n"})
    a = rig.worktree("a", {"a.txt": "a\n"})
    b = rig.worktree("b", {"b.txt": "b\n"})

    t0 = time.monotonic()
    p, log = rig.ship(solo, "--quick", sleep=7)
    _finish(p, log)
    t_solo = time.monotonic() - t0

    # Different lint lengths keep the two final sections apart, so a lock
    # wait in either log can only mean the lock was held across a lint.
    t0 = time.monotonic()
    pa, la = rig.ship(a, "--quick", sleep=5)
    pb, lb = rig.ship(b, "--quick", sleep=7)
    out_a = _finish(pa, la)
    out_b = _finish(pb, lb)
    t_pair = time.monotonic() - t0
    print(f"measured wall: solo={t_solo:.2f}s pair={t_pair:.2f}s")

    assert {"solo.txt", "a.txt", "b.txt"} <= _tree_files(rig)
    assert "waiting for the ship lock" not in out_a + out_b
    # Serial would be ~ (5 + 7) + 2x overhead; overlapped is ~ 7 + overhead.
    assert t_pair < 1.5 * t_solo, (t_solo, t_pair)


def test_lock_dir_absent_while_lint_runs(rig: Rig) -> None:
    wt = rig.worktree("probe", {"p.txt": "p\n"})
    p, log = rig.ship(wt, "--quick", sleep=3, probe=True)
    out = _finish(p, log)
    probes = (rig.lintdir / "probe.probes").read_text(encoding="utf-8").splitlines()
    assert len(probes) >= 5, "the probe never ran (vacuous pass)"
    assert not (rig.lintdir / "probe.lockseen").exists(), out
    assert "p.txt" in _tree_files(rig)
    assert not rig.lockdir().exists()  # released after the ship, too


def test_quick_lost_race_forward_merges_and_lands(rig: Rig) -> None:
    wt = rig.worktree("q", {"q.txt": "q\n"})
    p, log = rig.ship(wt, "--quick", sleep=4)
    rig.wait_lint_started(wt)
    racer_sha = rig.race({"racer.txt": "r\n"})
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


def test_budget_exhausted_full_gate_lands_without_pin(rig: Rig) -> None:
    wt = rig.worktree("f", {"f.txt": "f\n"})
    p, log = rig.ship(wt, sleep=4, PRECIS_SHIP_MAX_ATTEMPTS="1")
    rig.wait_lint_started(wt)
    racer_sha = rig.race({"racer.txt": "r\n"})
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
    p, log = rig.ship(wt, sleep=3, PRECIS_SHIP_MAX_ATTEMPTS="2")
    rig.wait_lint_started(wt)
    rig.race({"racer.txt": "r\n"})
    out = _finish(p, log)
    assert re.search(r"race budget: 1 of 2 attempts left; elapsed \d+m ?\d+s", out), out
    assert "not a deploy warrant" not in out  # the re-gate landed it exactly
    assert (wt / ".ship-sha").exists()


def test_migration_number_collision_with_merged_range_dies(rig: Rig) -> None:
    wt = rig.worktree(
        "m", {"src/precis/migrations/0002_ours.sql": "select 2;\n", "m.txt": "m\n"}
    )
    p, log = rig.ship(wt, sleep=3, PRECIS_SHIP_MAX_ATTEMPTS="1")
    rig.wait_lint_started(wt)
    racer_sha = rig.race({"src/precis/migrations/0002_theirs.sql": "select 3;\n"})
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
    p, log = rig.ship(wt, sleep=3, PRECIS_SHIP_MAX_ATTEMPTS="1")
    rig.wait_lint_started(wt)
    rig.race({"src/precis_chem/migrations/0016_b.sql": "select 2;\n"})
    out = _finish(p, log)
    assert "migration number collision" not in out
    assert {
        "src/precis_se/migrations/0016_a.sql",
        "src/precis_chem/migrations/0016_b.sql",
    } <= _tree_files(rig)


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
    p, log = rig.ship(wt, "--quick", sleep=3)
    rig.wait_lint_started(wt)
    racer_sha = rig.race({"src/precis/utils/safe_fetch.py": "X = 2\n"})
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

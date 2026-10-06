"""``scripts/ship --release``: a round fix applied onto the open release branch
and merged forward into main in one narrow-lock section.

ship-gate-ci design note 5. Same rig as ``tests/test_ship_narrow_lock.py``: the
REAL ``scripts/ship`` against REAL git (a bare ``origin.git``, a primary clone,
linked worktrees on feature branches), the container gate replaced by the
``PRECIS_SHIP_TEST_GATE_CMD`` fake lint, ``npx``/``gh``/``uvx`` stubs on PATH.
Nothing reaches a real remote: every clone's ``origin`` is the temp bare repo
and ``gh`` is a stub that prints a fixed URL.

Layout of the throwaway origin: ``main`` carries ``app.py`` (eight lines); the
rig cuts ``release/r1`` at that sha, as ``scripts/round cut`` would.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_ship_narrow_lock import Rig, _arrivals, _finish


def _git_has_merge_base_option() -> bool:
    """`ship --release` needs `git merge-tree --merge-base` (git >= 2.40). The
    host and the CI runners have it; the precis-dev image's git (2.39) does
    not, and there ship --release refuses with a version hint instead."""
    out = subprocess.run(
        ["git", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    ).stdout
    m = re.search(r"(\d+)\.(\d+)", out)
    if m is None:
        return False
    return (int(m.group(1)), int(m.group(2))) >= (2, 40)


pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32", reason="the shipped bash script is POSIX-only"
    ),
    pytest.mark.skipif(
        not _git_has_merge_base_option(),
        reason="git merge-tree has no --merge-base (git < 2.40)",
    ),
]

APP = "".join(f"l{i}\n" for i in range(1, 9))
REL = "release/r1"
_GH_URL = "https://example.invalid/runs/4242"


def _app(**edits: str) -> str:
    """APP with ``l<N>`` lines replaced: ``_app(l1="L1")``."""
    out = APP
    for old, new in edits.items():
        out = out.replace(f"{old}\n", f"{new}\n")
    return out


class ReleaseRig(Rig):
    """Rig + an open ``release/r1`` cut from a main that carries ``app.py``."""

    def __init__(self, tmp_path: Path, *, drift_rc: int = 0, cut: bool = True) -> None:
        self._drift_rc = drift_rc
        super().__init__(tmp_path)
        gh = tmp_path / "fakebin" / "gh"
        gh.write_text(
            f'#!/bin/sh\necho "$*" >> "{tmp_path}/gh.calls"\necho {_GH_URL}\n',
            encoding="utf-8",
        )
        gh.chmod(0o755)
        self.relracer = tmp_path / "relracer"
        self.race({"app.py": APP})
        self.g(self.primary, "pull", "-q", "--ff-only", "origin", "main")
        self.cut_sha = self.origin_ref("main")
        if cut:
            self.g(
                self.primary, "push", "-q", "origin", f"{self.cut_sha}:refs/heads/{REL}"
            )
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.relracer)],
            check=True,
            capture_output=True,
            env=self._git_env,
        )

    def _stage_repo(self) -> None:
        super()._stage_repo()
        stub = self.primary / "scripts" / "last-gated-main-sha"
        stub.write_text(f"#!/bin/sh\nexit {self._drift_rc}\n", encoding="utf-8")

    def worktree_from(self, name: str, base: str, files: dict[str, str]) -> Path:
        """A thread tree cut from ``base`` (e.g. ``origin/release/r1``)."""
        wt = self.primary / ".claude" / "worktrees" / name
        self.g(
            self.primary, "worktree", "add", "-q", "-b", f"feat-{name}", str(wt), base
        )
        for rel, body in files.items():
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(body, encoding="utf-8")
        self.g(wt, "add", "-A")
        self.g(wt, "commit", "-q", "-m", f"work {name}")
        return wt

    def release_push(self, files: dict[str, str], msg: str = "relracer") -> str:
        """Another host pushes a commit to origin ``release/r1``."""
        self.g(self.relracer, "fetch", "-q", "origin", REL)
        self.g(self.relracer, "checkout", "-q", "-B", "w", "FETCH_HEAD")
        for rel, body in files.items():
            (self.relracer / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.relracer / rel).write_text(body, encoding="utf-8")
        self.g(self.relracer, "add", "-A")
        self.g(self.relracer, "commit", "-q", "-m", msg)
        self.g(self.relracer, "push", "-q", "origin", f"HEAD:refs/heads/{REL}")
        return self.origin_ref(REL)

    def refs(self) -> tuple[str, str]:
        return self.origin_ref("main"), self.origin_ref(REL)

    def parents(self, rev: str) -> list[str]:
        return self.g(self.origin, "rev-list", "--parents", "-n1", rev).split()[1:]

    def msg(self, rev: str) -> str:
        return self.g(self.origin, "log", "-1", "--format=%B", rev)

    def show(self, rev: str, path: str) -> str:
        return self.g(self.origin, "show", f"{rev}:{path}")

    def release_ship(self, wt: Path, *flags: str) -> tuple[int, str]:
        proc, log = self.ship(wt, "--release", *flags, sleep=1)
        rc = proc.wait(timeout=180)
        return rc, log.read_text(encoding="utf-8")


@pytest.fixture
def rr(tmp_path: Path) -> ReleaseRig:
    return ReleaseRig(tmp_path)


def _install_pre_push(
    rr: ReleaseRig,
    target: str,
    clone: Path,
    base: str,
    limit: int,
    payload: str = 'echo "$n" > "hook_$n.txt"',
) -> Path:
    """Before each of the first ``limit`` pushes to ``target`` leaves the ship,
    push a racer commit to it from ``clone`` ("another host" inside the lock)."""
    counter = rr.root / f"prepush_{target.replace('/', '_')}.count"
    hook = rr.primary / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(
        f"""#!/bin/sh
hit=
while read lref lsha rref rsha; do
    [ "$rref" = {target} ] && hit=1
done
[ -n "$hit" ] || exit 0
n=$(cat "{counter}" 2>/dev/null || echo 0)
[ "$n" -ge {limit} ] && exit 0
n=$((n + 1))
echo "$n" > "{counter}"
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX
cd "{clone}" || exit 0
git fetch -q origin && git checkout -q -B w origin/{base}
{payload}
git add -A && git commit -q -m "hook racer $n" && git push -q origin HEAD:{target}
exit 0
""",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    return counter


def _no_pin_no_gated_move(rr: ReleaseRig, wt: Path) -> None:
    assert not (wt / ".ship-sha").exists()
    assert rr.origin_ref("gated") == rr.gated0, "origin/gated moved"
    assert not rr.lockdir().exists()


# ───────────────────────────── clean apply ─────────────────────────────


def test_clean_apply_release_commit_forward_merge_and_branch_reset(
    rr: ReleaseRig,
) -> None:
    wt = rr.worktree("fix1", {"app.py": _app(l1="L1")})
    head = rr.g(wt, "rev-parse", "HEAD")
    old_main, old_rel = rr.refs()
    assert old_main == old_rel == rr.cut_sha

    p, log = rr.ship(wt, "--release", sleep=1)
    out = _finish(p, log)

    main, rel = rr.refs()
    # the release: one new commit on the old head, with the trailer
    assert rr.parents(rel) == [old_rel]
    assert re.search(rf"^Release: r1; from feat-fix1 {head[:8]}$", rr.msg(rel), re.M), (
        rr.msg(rel)
    )
    assert rr.g(rr.origin, "diff", "--name-only", old_rel, rel) == "app.py"
    assert "L1" in rr.show(rel, "app.py")
    # main: a real merge, parents (OLD_MAIN, R1), the Gate trailer, the fix
    assert rr.parents(main) == [old_main, rel]
    m_msg = rr.msg(main)
    assert m_msg.startswith(f"Merge {REL} fix forward: chore(test): ship fix1"), m_msg
    assert re.search(
        rf"^Gate: release-forward; lint on feat-fix1 {head[:8]}$", m_msg, re.M
    ), m_msg
    assert "L1" in rr.show(main, "app.py")
    # branch reset to main
    assert rr.g(wt, "rev-parse", "HEAD") == main
    _no_pin_no_gated_move(rr, wt)
    # report
    assert f"scripts/round in {rel}" in out
    assert _GH_URL in out
    assert rel[:8] in out and main[:8] in out
    calls = (rr.root / "gh.calls").read_text(encoding="utf-8")
    assert f"--branch {REL}" in calls
    assert "UNGATED like a qland" in out


def test_second_fix_cut_from_the_release_head(rr: ReleaseRig) -> None:
    """Two fixes in a row; the second tree is cut from the RELEASE head, so B is
    the release head and the forward merge's base is the previous release head."""
    wt1 = rr.worktree("a1", {"app.py": _app(l1="L1")})
    _finish(*rr.ship(wt1, "--release", sleep=1))
    main1, rel1 = rr.refs()

    wt2 = rr.worktree_from("a2", f"origin/{REL}", {"app.py": _app(l1="L1", l8="L8")})
    p, log = rr.ship(wt2, "--release", sleep=1)
    out = _finish(p, log)
    assert f"B = merge-base(HEAD, origin/main) = {rel1}" in out
    main2, rel2 = rr.refs()
    assert rr.parents(rel2) == [rel1]
    assert rr.parents(main2) == [main1, rel2]
    final = rr.show(rel2, "app.py")
    assert "L1" in final and "L8" in final
    assert rr.g(rr.origin, "rev-list", "--count", f"{rr.cut_sha}..{rel2}") == "2"
    _no_pin_no_gated_move(rr, wt2)


def test_tree_synced_to_a_main_newer_than_the_cut_applies_only_the_fix(
    rr: ReleaseRig,
) -> None:
    """Main moved after the cut (l8 edited, x.txt added) and the thread synced
    to it; the fix touches only its own line. The release must get the fix and
    NOT main's late work; main gets everything."""
    rr.race({"app.py": _app(l8="l8-main"), "x.txt": "late\n"})
    rr.g(rr.primary, "pull", "-q", "--ff-only", "origin", "main")
    wt = rr.worktree("sync", {"app.py": _app(l1="L1", l8="l8-main")})
    old_main = rr.origin_ref("main")

    p, log = rr.ship(wt, "--release", sleep=1)
    out = _finish(p, log)
    main, rel = rr.refs()
    assert "L1" in rr.show(rel, "app.py")
    assert "l8-main" not in rr.show(rel, "app.py"), "main's late edit leaked in"
    assert rr.g(rr.origin, "ls-tree", "--name-only", rel, "x.txt") == ""
    assert rr.parents(main) == [old_main, rel]
    merged = rr.show(main, "app.py")
    assert "L1" in merged and "l8-main" in merged
    assert rr.show(main, "x.txt") == "late"
    assert rr.g(wt, "rev-parse", "HEAD") == main, out
    _no_pin_no_gated_move(rr, wt)


# ─────────────────────────────── conflicts ───────────────────────────────


def test_conflict_onto_the_release_stops_before_any_push(rr: ReleaseRig) -> None:
    """The fix edits a line main changed after the cut: three-way conflict on
    the release side (base main-now, ours release, theirs the fix)."""
    rr.race({"app.py": _app(l8="l8-main")})
    rr.g(rr.primary, "pull", "-q", "--ff-only", "origin", "main")
    wt = rr.worktree("c1", {"app.py": _app(l8="l8-fix")})
    before = rr.refs()
    rc, out = rr.release_ship(wt)
    assert rc != 0, out
    assert (
        "release conflict — ask the coordinator (`fleet say`), nothing was pushed"
        in out
    )
    assert f'step "apply onto {REL}"' in out
    assert "app.py" in out.split("conflicted paths", 1)[1]
    assert rr.refs() == before
    assert not rr.lockdir().exists()
    assert not (wt / ".ship-sha").exists()


def test_conflict_on_the_forward_merge_stops_before_any_push(rr: ReleaseRig) -> None:
    """The tree is cut from the release head (clean onto it), but main edited the
    same line after the cut: the release side applies, the forward merge cannot."""
    wt = rr.worktree("c2", {"app.py": _app(l5="l5-fix")})
    rr.race({"app.py": _app(l5="l5-main")})
    before = rr.refs()
    rc, out = rr.release_ship(wt)
    assert rc != 0, out
    assert (
        "release conflict — ask the coordinator (`fleet say`), nothing was pushed"
        in out
    )
    assert f'step "forward merge of {REL} into main"' in out
    assert "app.py" in out.split("conflicted paths", 1)[1]
    assert rr.refs() == before, "the release must not be pushed alone"
    assert not rr.lockdir().exists()


# ───────────────────────── lost CAS / partial / no-op ─────────────────────────


def test_lost_cas_on_the_release_ref_retries_in_the_lock_and_lands(
    rr: ReleaseRig,
) -> None:
    wt = rr.worktree("cas", {"app.py": _app(l1="L1")})
    counter = _install_pre_push(rr, f"refs/heads/{REL}", rr.relracer, REL, limit=1)
    p, log = rr.ship(wt, "--release", sleep=1)
    out = _finish(p, log)
    assert counter.read_text(encoding="utf-8").strip() == "1"
    assert f"CAS push to {REL} rejected" in out and "in-lock try 1/5" in out, out
    main, rel = rr.refs()
    # the racer's commit sits under the ship's: cut -> racer -> R1
    log_msgs = rr.g(
        rr.origin, "log", "--format=%s", "--first-parent", f"{rr.cut_sha}..{rel}"
    ).splitlines()
    assert log_msgs == ["chore(test): ship cas", "hook racer 1"], log_msgs
    assert "L1" in rr.show(rel, "app.py")
    assert rr.parents(main)[1] == rel
    assert rr.show(main, "hook_1.txt") == "1"  # the forward merge carries it
    _no_pin_no_gated_move(rr, wt)


def test_in_lock_retry_after_the_release_landed_mints_no_second_release_commit(
    rr: ReleaseRig,
) -> None:
    wt = rr.worktree("lease", {"app.py": _app(l1="L1")})
    # the release push passes; the main push loses its lease to a racer
    counter = _install_pre_push(rr, "refs/heads/main", rr.racer, "main", limit=1)
    p, log = rr.ship(wt, "--release", sleep=1)
    out = _finish(p, log)
    assert counter.read_text(encoding="utf-8").strip() == "1"
    assert "CAS push to main rejected" in out and "in-lock try 1/5" in out, out
    assert "no second release commit" in out
    assert "already carries this change" in out, out
    main, rel = rr.refs()
    assert rr.g(rr.origin, "rev-list", "--count", f"{rr.cut_sha}..{rel}") == "1"
    assert (
        rr.g(rr.origin, "log", "--format=%s", f"{rr.cut_sha}..main").count(
            "hook racer 1"
        )
        == 1
    )
    racer_main = rr.g(rr.origin, "rev-parse", "main^1")
    assert rr.parents(main) == [racer_main, rel]
    assert rr.g(rr.origin, "log", "-1", "--format=%s", racer_main) == "hook racer 1"
    assert rr.g(wt, "rev-parse", "HEAD") == main
    _no_pin_no_gated_move(rr, wt)


def test_rerun_after_a_partial_does_only_the_forward_merge(rr: ReleaseRig) -> None:
    """The release already has an R1-equivalent (a partial earlier run): no
    second release commit, only the forward merge."""
    pre = rr.release_push({"app.py": _app(l1="L1")}, msg="earlier release push")
    old_main = rr.origin_ref("main")
    wt = rr.worktree("part", {"app.py": _app(l1="L1")})
    p, log = rr.ship(wt, "--release", sleep=1)
    out = _finish(p, log)
    main, rel = rr.refs()
    assert rel == pre, "the release ref must not move"
    assert "already carries this change" in out
    assert rr.parents(main) == [old_main, pre]
    assert not re.search(r"^Release: ", rr.msg(rel), re.M)
    assert re.search(r"^Gate: release-forward; lint on feat-part ", rr.msg(main), re.M)
    assert "L1" in rr.show(main, "app.py")
    assert rr.g(wt, "rev-parse", "HEAD") == main
    _no_pin_no_gated_move(rr, wt)


def test_full_noop_when_the_release_is_already_in_main(rr: ReleaseRig) -> None:
    wt = rr.worktree("noop", {"app.py": _app(l1="L1")})
    old_head = rr.g(wt, "rev-parse", "HEAD")
    _finish(*rr.ship(wt, "--release", sleep=1))
    landed = rr.refs()
    # the thread is back on its pre-ship commit: the same fix, already released
    rr.g(wt, "reset", "-q", "--hard", old_head)
    p, log = rr.ship(wt, "--release", sleep=1)
    out = _finish(p, log)
    assert "nothing to do" in out, out
    assert rr.refs() == landed, "no empty merge, no release push"
    assert rr.g(wt, "rev-parse", "HEAD") == landed[0]
    _no_pin_no_gated_move(rr, wt)


# ─────────────────────────────── refusals ───────────────────────────────


def test_empty_change_is_refused_before_any_lock(rr: ReleaseRig) -> None:
    wt = rr.primary / ".claude" / "worktrees" / "empty"
    rr.g(rr.primary, "worktree", "add", "-q", "-b", "feat-empty", str(wt))
    before = rr.refs()
    rc, out = rr.release_ship(wt)
    assert rc != 0, out
    assert "nothing to release" in out
    assert "fresh tree cut from the RELEASE head" in out
    assert "waiting for the ship lock" not in out
    assert not (rr.lintdir / "empty.started").exists(), "refused after the lint ran"
    assert rr.refs() == before


def test_no_open_release_is_refused(tmp_path: Path) -> None:
    rr = ReleaseRig(tmp_path, cut=False)
    wt = rr.worktree("none", {"app.py": _app(l1="L1")})
    main = rr.origin_ref("main")
    rc, out = rr.release_ship(wt)
    assert rc != 0, out
    assert "no open release branch" in out
    assert rr.origin_ref("main") == main


def test_two_open_releases_are_refused(rr: ReleaseRig) -> None:
    rr.g(rr.primary, "push", "-q", "origin", f"{rr.cut_sha}:refs/heads/release/r2")
    wt = rr.worktree("two", {"app.py": _app(l1="L1")})
    before = rr.refs()
    rc, out = rr.release_ship(wt)
    assert rc != 0, out
    assert "2 release branches are open" in out
    assert "release/r1" in out and "release/r2" in out
    assert rr.refs() == before
    assert rr.origin_ref("release/r2") == rr.cut_sha


@pytest.mark.parametrize(
    "path,body",
    [
        ("src/precis/migrations/0002_x.sql", "select 2;\n"),
        ("src/precis_se/migrations/0001_a.sql", "select 1;\n"),
        ("src/precis/utils/safe_fetch.py", "X = 1\n"),
    ],
)
def test_migration_and_safe_fetch_are_refused_without_the_flag(
    rr: ReleaseRig, path: str, body: str
) -> None:
    wt = rr.worktree("mig", {path: body})
    before = rr.refs()
    rc, out = rr.release_ship(wt)
    assert rc != 0, out
    assert "a migration on a frozen release goes to the coordinator" in out
    assert path in out
    assert "--allow-migration" in out
    assert "fresh tree cut from the RELEASE head" in out
    assert not (rr.lintdir / "mig.started").exists(), "refused after the lint ran"
    assert rr.refs() == before
    assert not rr.lockdir().exists()


def test_allow_migration_lands_and_squawk_runs_before_the_lock(
    rr: ReleaseRig,
) -> None:
    fakebin = rr.root / "fakebin"
    uvx = fakebin / "uvx"
    uvx.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{rr.root}/uvx.calls"\n'
        f'[ -d "{rr.lockdir()}" ] && echo held >> "{rr.root}/uvx.lockheld"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    uvx.chmod(0o755)
    wt = rr.worktree("sq", {"src/precis/migrations/0002_ours.sql": "select 2;\n"})
    p, log = rr.ship(wt, "--release", "--allow-migration", sleep=1, PRECIS_SQUAWK="1")
    out = _finish(p, log)
    calls = (rr.root / "uvx.calls").read_text(encoding="utf-8")
    assert "squawk src/precis/migrations/0002_ours.sql" in calls, calls
    assert not (rr.root / "uvx.lockheld").exists(), "squawk ran inside the lock"
    assert "--allow-migration" in out
    main, rel = rr.refs()
    assert rr.show(rel, "src/precis/migrations/0002_ours.sql") == "select 2;"
    assert rr.parents(main)[1] == rel


def test_allow_migration_number_collision_with_the_release_head_dies(
    rr: ReleaseRig,
) -> None:
    rr.release_push({"src/precis/migrations/0002_rel.sql": "select 9;\n"})
    wt = rr.worktree("colrel", {"src/precis/migrations/0002_ours.sql": "select 2;\n"})
    before = rr.refs()
    rc, out = rr.release_ship(wt, "--allow-migration")
    assert rc != 0, out
    assert "migration number collision" in out
    assert "the release head release/r1" in out
    assert "0002_ours.sql" in out and "0002_rel.sql" in out
    assert rr.refs() == before
    assert not rr.lockdir().exists()


def test_allow_migration_number_collision_with_main_dies(rr: ReleaseRig) -> None:
    rr.race({"src/precis/migrations/0002_main.sql": "select 8;\n"})
    wt = rr.worktree("colmain", {"src/precis/migrations/0002_ours.sql": "select 2;\n"})
    before = rr.refs()
    rc, out = rr.release_ship(wt, "--allow-migration")
    assert rc != 0, out
    assert "migration number collision" in out
    assert "origin/main carries 0002_main.sql" in out
    assert rr.refs() == before
    assert not rr.lockdir().exists()


def test_allow_migration_numbers_are_per_plugin_dir(rr: ReleaseRig) -> None:
    rr.release_push({"src/precis_chem/migrations/0016_b.sql": "select 2;\n"})
    wt = rr.worktree("plug", {"src/precis_se/migrations/0016_a.sql": "select 1;\n"})
    rc, out = rr.release_ship(wt, "--allow-migration")
    assert rc == 0, out
    assert "migration number collision:" not in out


# ───────────────────── drift guard / contention / flags ─────────────────────


def test_release_skips_the_drift_guard(tmp_path: Path) -> None:
    """`last-gated-main-sha` says "looked, none" (exit 2): a plain --quick ship
    is refused by the drift guard, --release is not."""
    rr = ReleaseRig(tmp_path, drift_rc=2)
    ctl = rr.worktree("ctl", {"ctl.txt": "c\n"})
    p, log = rr.ship(ctl, "--quick", sleep=1)
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0 and "no all-green shard matrix" in out, "control: guard is live"
    assert "ctl.txt" not in rr.g(rr.origin, "ls-tree", "-r", "--name-only", "main")

    wt = rr.worktree("drift", {"app.py": _app(l1="L1")})
    p, log = rr.ship(wt, "--release", sleep=1)
    out = _finish(p, log)
    assert "qland drift guard skipped" in out
    assert "L1" in rr.show("main", "app.py")


def test_main_moving_during_the_section_still_lands(rr: ReleaseRig) -> None:
    wt = rr.worktree("move", {"app.py": _app(l1="L1")})
    with _arrivals(rr, 1.0) as pushed:
        time.sleep(1.5)  # arrivals are flowing before the ship starts
        p, log = rr.ship(wt, "--release", sleep=4)
        out = _finish(p, log, timeout=240)
        n_pushed = len(pushed)
    tries = len(re.findall(r"in-lock try \d+/5", out))
    print(f"arrivals[release]: {n_pushed} pushes; {tries} in-lock CAS losses")
    assert n_pushed >= 3, f"the arrivals loop barely ran ({n_pushed} pushes)"
    main, rel = rr.refs()
    # main's tip may be a later arrival: find OUR merge M by its subject
    m = rr.g(
        rr.origin,
        "log",
        "-1",
        "--format=%H",
        f"--grep=^Merge {REL} fix forward: chore(test): ship move",
        "main",
    )
    assert m, "the forward merge is not on main"
    assert rr.parents(m)[1] == rel
    assert re.search(r"^Gate: release-forward; ", rr.msg(m), re.M)
    assert rr.g(rr.origin, "merge-base", "--is-ancestor", m, "main") == ""
    assert "L1" in rr.show(main, "app.py")
    assert "L1" in rr.show(rel, "app.py")
    _no_pin_no_gated_move(rr, wt)


def test_flag_combinations(rr: ReleaseRig) -> None:
    wt = rr.worktree("flags", {"app.py": _app(l1="L1")})
    for flags, needle in (
        (("--allow-migration",), "only means something with --release"),
        (("--release", "--remote"), "contradictory"),
    ):
        proc, log = rr.ship(wt, *flags, sleep=1)
        assert proc.wait(timeout=60) == 2
        assert needle in log.read_text(encoding="utf-8")
    assert rr.refs() == (rr.cut_sha, rr.cut_sha)


def test_usage_header_documents_the_flags() -> None:
    text = (Path(__file__).resolve().parent.parent / "scripts" / "ship").read_text(
        encoding="utf-8"
    )
    header = text.split("set -euo pipefail", 1)[0]
    assert "scripts/ship --release" in header
    assert "--allow-migration" in header


def test_no_git_surprises_in_the_release_rig(rr: ReleaseRig) -> None:
    """Every clone points at the temp bare origin, never a real remote."""
    for clone in (rr.primary, rr.racer, rr.relracer):
        url = rr.g(clone, "remote", "get-url", "origin")
        assert url == str(rr.origin), url


# ───────────── partial state, renames, B older than the cut, hooks ─────────────


def test_partial_state_die_past_the_bound_names_r1(rr: ReleaseRig) -> None:
    """The release lands, the main lease is lost, and the bound is 1: the die
    must say the release has R1 and main lacks the forward merge."""
    wt = rr.worktree("part1", {"app.py": _app(l1="L1")})
    _install_pre_push(rr, "refs/heads/main", rr.racer, "main", limit=1)
    p, log = rr.ship(wt, "--release", sleep=1, PRECIS_SHIP_INLOCK_TRIES="1")
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    main, rel = rr.refs()
    assert rel != rr.cut_sha and re.search(r"^Release: r1; ", rr.msg(rel), re.M)
    assert f"release r1 has {rel}; main lacks the forward merge" in out, out
    assert rr.g(rr.origin, "log", "-1", "--format=%s", main) == "hook racer 1"
    assert not rr.lockdir().exists()
    assert not (wt / ".ship-sha").exists()


def test_retry_time_forward_merge_conflict_after_r1_landed(rr: ReleaseRig) -> None:
    """R1 lands, then a racer edits the fix's own line on main (the main lease is
    lost); the retry's forward merge conflicts. The die must not say "nothing
    was pushed"."""
    theirs = rr.root / "app_theirs.py"
    theirs.write_text(_app(l1="l1-main"), encoding="utf-8")
    wt = rr.worktree("retryc", {"app.py": _app(l1="L1")})
    _install_pre_push(
        rr,
        "refs/heads/main",
        rr.racer,
        "main",
        limit=1,
        payload=f'cp "{theirs}" app.py',
    )
    p, log = rr.ship(wt, "--release", sleep=1)
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    main, rel = rr.refs()
    assert 'step "forward merge of release/r1 into main"' in out, out
    assert f"release r1 has {rel}; main lacks the forward merge" in out, out
    assert "nothing was pushed" not in out
    assert "are untouched" not in out
    assert rr.g(rr.origin, "rev-list", "--count", f"{rr.cut_sha}..{rel}") == "1"
    assert rr.g(rr.origin, "log", "-1", "--format=%s", main) == "hook racer 1"
    assert not rr.lockdir().exists()


def _renamed_tree(rr: ReleaseRig, name: str, old: str, new: str) -> Path:
    wt = rr.primary / ".claude" / "worktrees" / name
    rr.g(rr.primary, "worktree", "add", "-q", "-b", f"feat-{name}", str(wt))
    rr.g(wt, "mv", old, new)
    rr.g(wt, "commit", "-q", "-m", f"rename {name}")
    return wt


@pytest.mark.parametrize(
    "old,new",
    [
        ("src/precis/utils/safe_fetch.py", "src/precis/utils/safe_fetch_old.py"),
        ("src/precis/migrations/0001_init.sql", "src/precis/migrations/0001_moved.sql"),
    ],
)
def test_renames_of_safe_fetch_and_migrations_are_refused(
    rr: ReleaseRig, old: str, new: str
) -> None:
    wt = _renamed_tree(rr, "ren", old, new)
    before = rr.refs()
    rc, out = rr.release_ship(wt)
    assert rc != 0, out
    assert "a migration on a frozen release goes to the coordinator" in out
    assert old in out, "the deleted side of the rename must be named"
    assert not (rr.lintdir / "ren.started").exists()
    assert rr.refs() == before


def test_allow_migration_renamed_migration_is_new_for_squawk(rr: ReleaseRig) -> None:
    uvx = rr.root / "fakebin" / "uvx"
    uvx.write_text(
        f'#!/bin/sh\necho "$*" >> "{rr.root}/uvx.calls"\nexit 0\n', encoding="utf-8"
    )
    uvx.chmod(0o755)
    wt = _renamed_tree(
        rr,
        "ren2",
        "src/precis/migrations/0001_init.sql",
        "src/precis/migrations/0001_moved.sql",
    )
    p, log = rr.ship(wt, "--release", "--allow-migration", sleep=1, PRECIS_SQUAWK="1")
    out = _finish(p, log)  # the old name is deleted by the fix: no collision with it
    calls = (rr.root / "uvx.calls").read_text(encoding="utf-8")
    assert "squawk src/precis/migrations/0001_moved.sql" in calls, calls
    main, rel = rr.refs()
    assert rr.show(rel, "src/precis/migrations/0001_moved.sql") == "select 1;"
    assert "migration number collision:" not in out


def test_thread_branched_before_the_cut_applies_cleanly(rr: ReleaseRig) -> None:
    """B is OLDER than the cut S: the thread branched from the main before S, main
    moved to S, the release was cut at S, and main moved on again. The release
    ends as S plus the fix; main's later work stays out of it."""
    rr.race({"later.txt": "after the cut\n"})
    wt = rr.worktree_from(
        "older",
        f"{rr.cut_sha}~1",
        {".gitignore": ".claude/\n.ship-sha\ncoverage.xml\nextra\n"},
    )
    p, log = rr.ship(wt, "--release", sleep=1)
    out = _finish(p, log)
    old_b = rr.g(rr.origin, "rev-parse", f"{rr.cut_sha}~1")
    assert f"B = merge-base(HEAD, origin/main) = {old_b}" in out
    main, rel = rr.refs()
    assert rr.parents(rel) == [rr.cut_sha]
    assert rr.g(rr.origin, "diff", "--name-only", rr.cut_sha, rel) == ".gitignore"
    assert "extra" in rr.show(rel, ".gitignore")
    assert "l1" in rr.show(rel, "app.py")  # S's own file is there
    assert rr.g(rr.origin, "ls-tree", "--name-only", rel, "later.txt") == ""
    assert rr.g(rr.origin, "ls-tree", "--name-only", main, "later.txt") == "later.txt"
    assert rr.parents(main)[1] == rel
    _no_pin_no_gated_move(rr, wt)


def _install_origin_hook(rr: ReleaseRig, ref: str) -> None:
    hook = rr.origin / "hooks" / "pre-receive"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(
        "#!/bin/sh\nwhile read old new ref; do\n"
        f'  [ "$ref" = {ref} ] && {{ echo "protected branch: no pushes" >&2; exit 1; }}\n'
        "done\nexit 0\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)


def test_release_hook_rejection_dies_at_once_and_names_the_partial(
    rr: ReleaseRig,
) -> None:
    """A server-side rejection prints "[remote rejected]" too: not a race."""
    _install_origin_hook(rr, "refs/heads/main")
    wt = rr.worktree("hook1", {"app.py": _app(l1="L1")})
    p, log = rr.ship(wt, "--release", sleep=1)
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "remote rejected" in out and "protected branch" in out
    assert "for a reason other than a lost race" in out, out
    assert "in-lock try" not in out, "a hook rejection must not be retried"
    main, rel = rr.refs()
    assert main == rr.cut_sha
    assert f"release r1 has {rel}; main lacks the forward merge" in out, out
    assert not rr.lockdir().exists()


def test_narrow_lock_hook_rejection_dies_at_once(rr: ReleaseRig) -> None:
    """Same classification in the shared narrow-lock push."""
    _install_origin_hook(rr, "refs/heads/main")
    wt = rr.worktree("hook2", {"h.txt": "h\n"})
    p, log = rr.ship(wt, "--quick", sleep=1)
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "remote rejected" in out
    assert "for a reason other than a lost race" in out, out
    assert "in-lock try" not in out and "CAS push rejected" not in out
    assert rr.origin_ref("main") == rr.cut_sha
    assert not rr.lockdir().exists()


def test_lint_autofix_amend_is_rescanned_for_risky_paths(rr: ReleaseRig) -> None:
    """The lint's auto-fix amend lands after the preflight scan: a fix that only
    touched app.py but whose lint rewrote safe_fetch.py is refused before the lock."""
    wt = rr.worktree("amend", {"app.py": _app(l1="L1")})
    before = rr.refs()
    gate = (
        "echo '# autofix' >> src/precis/utils/safe_fetch.py; "
        f"echo run >> {rr.lintdir}/amend.runs"
    )
    p, log = rr.ship(wt, "--release", sleep=1, PRECIS_SHIP_TEST_GATE_CMD=gate)
    rc = p.wait(timeout=120)
    out = log.read_text(encoding="utf-8")
    assert rc != 0, out
    assert "amended ruff auto-fixes" in out
    assert "a migration on a frozen release goes to the coordinator" in out
    assert "safe_fetch.py" in out.split("this change touches", 1)[1]
    assert "shipping release fix" not in out, "refused only after taking the lock path"
    assert rr.refs() == before
    assert not rr.lockdir().exists()

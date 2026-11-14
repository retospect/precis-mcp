"""`scripts/release` — cut at S, wait for CI, deploy, over the real `scripts/round`.

Same rig as the round tests: a throwaway repo with a bare `origin`, fake CI
(`LGM_EXACT`, canned gh check-runs) and a fake `scripts/deploy`. Polls are
sub-second via flags.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from precis.workers.job_types.release_cut import (
    REFUSAL_IN_FLIGHT,
    classify_cut_refusal,
)
from tests._script import load_script
from tests.test_round_gate import (
    REPO_ROOT,
    Rig,
    _child,
    _commit,
    _git,
    _live,
    _push_branch,
    _rel,
    _round_json,
)
from tests.test_round_gate import rig as rig
from tests.test_round_release import _hook, _verdict

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: execs shebang'd scripts"
)

RELEASE = REPO_ROOT / "scripts" / "release"
FAST = ("--poll", "0.01", "--ci-timeout", "0.02")


def _release(rig: Rig, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    env = dict(rig.env)
    env.update(extra)
    return subprocess.run(
        [sys.executable, str(RELEASE), *args],
        cwd=rig.root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )


def _green(rig: Rig, key: str = "c3") -> dict[str, str]:
    return {**_verdict(rig.shas[key]), "DEPLOY_STUB_VERIFY": "1"}


def _next_line(sha: str) -> str:
    return (
        f"next: verify runtime, then scripts/round deploy --confirm-runtime {sha} "
        "--runtime-evidence '<observed services/boot SHAs/readiness/session MCP>' "
        "&& scripts/round close"
    )


def test_already_live_exits_zero(rig: Rig) -> None:
    _push_branch(rig, "prod", rig.shas["c3"])
    out = _release(rig, *FAST)
    assert out.returncode == 0, out.stderr
    assert f"already live: {rig.shas['c3'][:9]} is in origin/prod" in out.stdout
    assert rig.deploy_log() == []


def test_sha_off_first_parent_is_refused(rig: Rig) -> None:
    side = _git(
        rig.root,
        "commit-tree",
        f"{rig.shas['c0']}^{{tree}}",
        "-p",
        rig.shas["c0"],
        "-m",
        "side",
    )
    out = _release(rig, "--sha", side, *FAST)
    assert out.returncode == 1
    assert "first-parent" in out.stderr
    assert rig.origin_ref("release/r1") == ""


def test_rides_an_unrecorded_release_that_contains_s(rig: Rig) -> None:
    # A foreign branch carrying commits main lacks is in flight; one whose
    # head main already contains is a stale leftover (gr544044).
    work = _child(rig, rig.shas["c3"], "unrecorded release work")
    _push_branch(rig, "release/r7", work)
    out = _release(rig, "--sha", rig.shas["c2"], *FAST)
    assert out.returncode == 0, out.stderr
    assert (
        f"riding release release/r7 at {work[:9]}: it ships "
        f"{rig.shas['c2'][:9]}" in out.stdout
    )
    assert rig.deploy_log() == []


def test_release_not_containing_s_rides_the_next_cut(rig: Rig) -> None:
    """No queueing (release-train §5): an in-flight release that lacks S
    reports and exits 0 — the driver cuts N+1 at deploy start."""
    work = _child(rig, rig.shas["c0"], "unshipped release work")
    _push_branch(rig, "release/r7", work)
    out = _release(rig, *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"{rig.shas['c3'][:9]} rides the next cut" in out.stdout
    assert "queued" not in out.stdout
    assert rig.deploy_log() == []


def test_superseded_release_kept_on_origin_does_not_block_a_new_cut(
    rig: Rig,
) -> None:
    """gr544044: a superseded release's branch stays on origin for Reto to
    prune; the in-flight check must read the terminal record as finished."""
    assert _release(rig, "--cut-only", *FAST).returncode == 0  # r1 at c3
    path = rig.root / ".git" / "precis-round" / "round.json"
    cur = _round_json(rig)
    rel = _rel(cur)
    rel["state"] = "superseded"
    rel["superseded_by"] = 2
    path.write_text(json.dumps(cur), encoding="utf-8")
    c4 = _commit(rig.root, "src/c4.py", "c4 code")
    _git(rig.root, "push", "-q", "origin", "main")
    _push_branch(rig, "prod", rig.shas["c3"])  # deployed: prod holds r1's head
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "rides the next cut" not in out.stdout
    assert (
        "release/r1: head on prod, superseded — ignored; "
        "delete with: git push origin --delete release/r1" in out.stdout
    )
    assert f"RELEASE_CUT: r2 {c4}" in out.stdout.splitlines()
    assert rig.origin_ref("release/r1") == rig.shas["c3"]  # never deleted
    assert rig.origin_ref("release/r2") == c4


def test_unrecorded_release_branch_on_prod_is_retired(rig: Rig) -> None:
    """gr544044/gr675506 (e): a release/* left on origin whose head
    origin/prod contains is stale — the cut proceeds and the line names the
    delete command. (A head merely on main no longer retires it.)"""
    _push_branch(rig, "release/r7", rig.shas["c0"])
    _push_branch(rig, "prod", rig.shas["c0"])
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert (
        "release/r7: head on prod, stale — ignored; "
        "delete with: git push origin --delete release/r7" in out.stdout
    )
    # `open` numbers past the stale branch, so the cut mints r8.
    assert f"RELEASE_CUT: r8 {rig.shas['c3']}" in out.stdout.splitlines()
    assert rig.origin_ref("release/r7") == rig.shas["c0"]  # never deleted
    assert rig.origin_ref("release/r8") == rig.shas["c3"]


def test_foreign_branch_with_head_on_main_is_in_flight(rig: Rig) -> None:
    """gr675506 (b): another cutter's live release/r313 sits on main (its
    head is main's tip when cut, an ancestor once main moves) and prod does
    not contain it: the cut reports "release in flight" and creates no
    branch. The old "head on main, stale — ignored" rule hid it."""
    for head_key in ("c3", "c2"):  # main's tip, then an ancestor of it
        _push_branch(rig, "release/r313", rig.shas[head_key])
        out = _release(rig, "--cut-only", *FAST)
        assert out.returncode == 0, out.stdout + out.stderr
        assert "release in flight: release/r313" in out.stdout
        assert "RELEASE_CUT:" not in out.stdout
        assert "head on main" not in out.stdout
        heads = _git(rig.root, "ls-remote", "--heads", "origin", "release/*")
        assert [ln.split("\t")[1] for ln in heads.splitlines()] == [
            "refs/heads/release/r313"
        ]  # nothing was cut beside it
        _git(rig.root, "push", "-q", "origin", "--delete", "release/r313")


def test_hand_cut_without_the_release_pass_marker_warns_and_still_cuts(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr675506 (d), first release: `scripts/release` without the factory
    pass marker still cuts and warns on stderr; with it, the cut log names
    the pass. A dry-run says nothing."""
    monkeypatch.delitem(rig.env, "PRECIS_RELEASE_PASS")
    dry = _release(rig, "--dry-run", *FAST)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "would cut release/r1" in dry.stdout and "gr675506" not in dry.stderr
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "PRECIS_RELEASE_PASS is unset" in out.stderr and "gr675506" in out.stderr
    assert "cutting under release pass" not in out.stdout
    assert rig.origin_ref("release/r1") == rig.shas["c3"]
    assert _release(rig, "--deploy-only", "r1", "--dry-run", *FAST).returncode == 0
    assert _round_json(rig)["releases"][0]["state"] == "cut"
    assert rig.round("cut", "--abandon").returncode == 0
    out = _release(rig, "--cut-only", *FAST, PRECIS_RELEASE_PASS="td5")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "cutting under release pass td5" in out.stdout
    assert "PRECIS_RELEASE_PASS is unset" not in out.stderr
    assert any(ln.startswith("RELEASE_CUT: r") for ln in out.stdout.splitlines())


def test_cut_and_finish_warns_once_per_cut_path_without_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The re-cut (`supersede=True`) and the pending-cut reconcile go
    through `cut_and_finish`: one warning each, then `round cut` runs."""
    mod = _load_release(tmp_path, monkeypatch)
    monkeypatch.delenv("PRECIS_RELEASE_PASS")
    called: list[tuple[str, ...]] = []

    def _rnd(*a: str, **k: object) -> subprocess.CompletedProcess[str]:
        called.append(a)
        return subprocess.CompletedProcess(a, 1, "", "boom")

    monkeypatch.setattr(mod, "rnd", _rnd)
    monkeypatch.setattr(mod, "remote", lambda ref: "")
    args = mod.argparse.Namespace(adopt=False)
    assert mod.cut_and_finish(args, "a" * 40, "release/r2") == 1
    assert mod.cut_and_finish(args, "a" * 40, "release/r2", supersede=True) == 1
    assert [c[0] for c in called] == ["cut", "cut"]
    err = capsys.readouterr().err
    assert err.count("PRECIS_RELEASE_PASS is unset") == 2 and "gr675506" in err


def test_live_releases_adds_the_shared_rows_branch_when_origin_has_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr675506: a non-terminal row naming a branch on origin is live
    whatever the local record says; a terminal row, a branch gone from
    origin, or a branch a live local record already covers adds nothing."""
    mod = _load_release(tmp_path, monkeypatch)
    row = {"n": 313, "branch": "release/r313", "base": "c" * 40, "state": "cut"}
    on_origin = ["release/r313"]
    live = mod.live_releases({"releases": []}, row, on_origin)
    assert [(r["branch"], r["state"], r.get("from_row")) for r in live] == [
        ("release/r313", "cut", True)
    ]
    # a stale local record of an older release does not hide it
    stale = {"releases": [{"n": 309, "branch": "release/r309", "state": "abandoned"}]}
    assert [r["n"] for r in mod.live_releases(stale, row, on_origin)] == [313]
    assert mod.live_releases({"releases": []}, row, []) == []  # gone from origin
    assert mod.live_releases({"releases": []}, row) == []  # no branch list: as before
    done = {**row, "state": "confirmed"}
    assert mod.live_releases({"releases": []}, done, on_origin) == []
    mine = {"releases": [{"n": 313, "branch": "release/r313", "state": "cut"}]}
    live = mod.live_releases(mine, row, on_origin)
    assert [r.get("from_row") for r in live] == [None]  # the local one, not doubled


def test_cut_only_refuses_when_the_shared_row_names_a_branch_on_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """gr675506: this clone's round.json is empty, the shared row holds a
    live r313 and origin has release/r313 (prod lacks it): the cut says
    "release in flight: release/r313" and cuts nothing."""
    mod = _load_release(tmp_path, monkeypatch)
    prod, head = "b" * 40, "c" * 40
    r313 = "e" * 40
    cur = {"open": False, "n": 0, "releases": []}
    monkeypatch.setattr(mod, "warn_hook_drift", lambda: None)
    monkeypatch.setattr(mod, "fetch", lambda: None)
    monkeypatch.setattr(mod, "rev", lambda name: {"origin/main": head}.get(name, name))
    monkeypatch.setattr(mod, "git", lambda *a: head)
    monkeypatch.setattr(
        mod,
        "remote",
        lambda ref: {"refs/heads/prod": prod, "refs/heads/release/r313": r313}.get(
            ref, ""
        ),
    )
    monkeypatch.setattr(mod, "ancestor", lambda older, newer: False)
    monkeypatch.setattr(mod, "round_state", lambda: cur)
    monkeypatch.setattr(mod, "read_owner", lambda: {})
    monkeypatch.setattr(mod, "release_branches", lambda: ["release/r313"])
    monkeypatch.setattr(mod, "head_containing", lambda *a: ("", False))
    monkeypatch.setattr(
        mod,
        "read_round_record",
        lambda: {"n": 313, "branch": "release/r313", "base": head, "state": "cut"},
    )

    def _no_rnd(*a: str, **k: object) -> subprocess.CompletedProcess[str]:
        pytest.fail(f"round must not run: {a}")

    monkeypatch.setattr(mod, "rnd", _no_rnd)
    cuts: list[str] = []

    def _cut_and_finish(args: object, s: str, branch: str = "") -> int:
        cuts.append(s)
        return 0

    monkeypatch.setattr(mod, "cut_and_finish", _cut_and_finish)
    args = mod.argparse.Namespace(
        deploy_only="",
        sha="",
        adopt=False,
        dry_run=False,
        cut_only=True,
        ci_timeout=0.0,
        poll=0.0,
    )
    assert mod.run(args) == 0
    assert "release in flight: release/r313" in capsys.readouterr().out
    assert cuts == []


def test_a_stuck_row_whose_branch_prod_holds_does_not_block_the_cut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """gr675506 review: the row adds a branch to `live` only while prod
    lacks its head. A row stuck non-terminal (`deployed`) on a release
    origin/prod already contains is a stale row — warned about, never a
    blocker."""
    mod = _load_release(tmp_path, monkeypatch)
    prod, head, r313 = "b" * 40, "c" * 40, "e" * 40
    cur = {"open": False, "n": 0, "releases": []}
    monkeypatch.setattr(mod, "warn_hook_drift", lambda: None)
    monkeypatch.setattr(mod, "fetch", lambda: None)
    monkeypatch.setattr(mod, "rev", lambda name: {"origin/main": head}.get(name, name))
    monkeypatch.setattr(mod, "git", lambda *a: head)
    monkeypatch.setattr(
        mod,
        "remote",
        lambda ref: {"refs/heads/prod": prod, "refs/heads/release/r313": r313}.get(
            ref, ""
        ),
    )
    monkeypatch.setattr(
        mod, "ancestor", lambda older, newer: (older, newer) == (r313, prod)
    )
    monkeypatch.setattr(mod, "round_state", lambda: cur)
    monkeypatch.setattr(mod, "read_owner", lambda: {})
    monkeypatch.setattr(mod, "release_branches", lambda: ["release/r313"])
    monkeypatch.setattr(mod, "head_containing", lambda *a: ("", False))
    monkeypatch.setattr(
        mod,
        "read_round_record",
        lambda: {"n": 313, "branch": "release/r313", "base": r313, "state": "deployed"},
    )
    monkeypatch.setattr(
        mod,
        "rnd",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""),
    )
    cuts: list[str] = []

    def _cut_and_finish(args: object, s: str, branch: str = "") -> int:
        cuts.append(s)
        return 0

    monkeypatch.setattr(mod, "cut_and_finish", _cut_and_finish)
    args = mod.argparse.Namespace(
        deploy_only="",
        sha="",
        adopt=False,
        dry_run=False,
        cut_only=True,
        ci_timeout=0.0,
        poll=0.0,
    )
    assert mod.run(args) == 0
    out = capsys.readouterr().out
    assert "release in flight" not in out
    assert "release/r313: head on prod, shared row says deployed" in out
    assert cuts == [head]


def test_unrecorded_release_branch_off_main_still_blocks(rig: Rig) -> None:
    """A foreign release/* carrying commits main lacks is still in flight."""
    work = _child(rig, rig.shas["c0"], "unshipped release work")
    _push_branch(rig, "release/r7", work)
    out = _release(rig, *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"{rig.shas['c3'][:9]} rides the next cut" in out.stdout
    assert "RELEASE_CUT:" not in out.stdout
    assert rig.origin_ref("release/r1") == ""


def _stale_deployed(rig: Rig, *, drop_marker: bool = True) -> str:
    """A deployed-unconfirmed r1 that origin/prod moved past — the
    dead-clone record a second worktree trips on (gr573151). With
    drop_marker=False this tree keeps owning it. Returns the new
    origin/main tip."""
    assert _release(rig, "--cut-only", *FAST).returncode == 0  # r1 at c3
    out = rig.round("deploy", DEPLOY_STUB_VERIFY="1", **_verdict(rig.shas["c3"]))
    assert out.returncode == 3, out.stdout + out.stderr  # pinned, unconfirmed
    c4 = _commit(rig.root, "src/c4.py", "c4 code")
    _git(rig.root, "push", "-q", "origin", "main")
    _push_branch(rig, "prod", c4)  # a later deploy moved prod past the pin
    tip = _commit(rig.root, "src/c5.py", "c5 code")
    _git(rig.root, "push", "-q", "origin", "main")
    if drop_marker:
        (rig.root / ".release-owner").unlink()
    return tip


def test_stale_deployed_record_exits_four_and_names_the_clear_command(
    rig: Rig,
) -> None:
    """gr573151: the only in-flight record is a corpse prod already moved
    past — a blocker, not a ride: exit 4 naming `round supersede r<N>`."""
    _stale_deployed(rig)
    out = _release(rig, *FAST)
    assert out.returncode == 4, out.stdout + out.stderr
    assert "release/r1" in out.stderr
    assert "scripts/round supersede r1" in out.stderr
    assert "rides the next cut" not in out.stdout
    assert rig.deploy_log() == [f"{rig.shas['c3']} --pinned"]  # only the setup's


def test_stale_record_with_a_live_foreign_branch_rides(rig: Rig) -> None:
    """A live owner still in flight keeps the ride — exit 0 naming the
    branch; only an all-stale in-flight set is exit 4."""
    tip = _stale_deployed(rig)
    work = _child(rig, rig.shas["c0"], "unshipped release work")
    _push_branch(rig, "release/r7", work)
    out = _release(rig, *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "release in flight: release/r7" in out.stdout
    assert f"{tip[:9]} rides the next cut" in out.stdout
    assert "RELEASE_CUT:" not in out.stdout


def test_stale_owned_record_exits_four_not_three(rig: Rig) -> None:
    """gr573151 review: ownership does not revive a corpse — an owned
    record prod moved past is exit 4 like a foreign one; exit 3 stays for
    a live rollout that genuinely awaits runtime confirmation."""
    _stale_deployed(rig, drop_marker=False)
    out = _release(rig, *FAST)
    assert out.returncode == 4, out.stdout + out.stderr
    assert "release/r1" in out.stderr
    assert "scripts/round supersede r1" in out.stderr
    assert "confirm-runtime" not in out.stdout


def test_fresh_path_opens_cuts_waits_and_deploys(rig: Rig) -> None:
    out = _release(rig, *FAST, **_green(rig))
    assert out.returncode == 3, out.stdout + out.stderr
    s = rig.shas["c3"]
    assert "round 1 open" in out.stdout
    assert f"release/r1 cut at {s[:9]}" in out.stdout
    assert "fleet say -m" in out.stdout
    assert _next_line(s) in out.stdout
    assert rig.origin_ref("release/r1") == s
    assert rig.deploy_log() == [f"{s} --pinned"]
    assert _rel(_round_json(rig))["deployment"]["sha"] == s


def test_red_check_run_exits_two_with_job_and_url(rig: Rig) -> None:
    s = rig.shas["c3"]
    url = "https://github.com/o/r/actions/runs/4242/job/9"
    (rig.gh_dir / s).write_text(
        f"lint\tcompleted\tsuccess\t{url}\n"
        f"test-linux (2)\tcompleted\tfailure\t{url}\n"
        f"test-linux (1)\tin_progress\t\t{url}\n"
        f"other\tcompleted\tfailure\t{url}\n",
        encoding="utf-8",
    )
    out = _release(rig, "--poll", "0.01", "--ci-timeout", "1")
    assert out.returncode == 2
    assert "test-linux (2)" in out.stderr
    assert "other" not in out.stderr
    assert url in out.stderr
    assert "scripts/ci-failed 4242" in out.stderr
    assert rig.origin_ref("release/r1") == s  # left in place
    assert rig.deploy_log() == []


def test_red_failed_first_exits_two(rig: Rig) -> None:
    """release-train §3: a red failed-first is the release's red — the
    shards it gated read `skipped`, which must not mask it."""
    s = rig.shas["c3"]
    url = "https://github.com/o/r/actions/runs/4242/job/9"
    (rig.gh_dir / s).write_text(
        f"lint\tcompleted\tsuccess\t{url}\n"
        f"failed-first\tcompleted\tfailure\t{url}\n"
        f"test-linux (1)\tcompleted\tskipped\t{url}\n"
        f"test-linux (2)\tcompleted\tskipped\t{url}\n",
        encoding="utf-8",
    )
    out = _release(rig, "--poll", "0.01", "--ci-timeout", "1")
    assert out.returncode == 2
    assert "failed-first" in out.stderr
    assert rig.origin_ref("release/r1") == s  # left in place
    assert rig.deploy_log() == []


def test_red_gate_fast_check_run_exits_two(rig: Rig) -> None:
    """me580555: `gate-fast` is the blocking leg of the new gate shape — a
    red one is the release's verdict exactly as test-linux was."""
    s = rig.shas["c3"]
    url = "https://github.com/o/r/actions/runs/4242/job/9"
    (rig.gh_dir / s).write_text(
        f"lint\tcompleted\tsuccess\t{url}\n"
        f"gate-fast (3.13, 2)\tcompleted\tfailure\t{url}\n"
        f"soak (3.13, 1)\tcompleted\tfailure\t{url}\n",
        encoding="utf-8",
    )
    out = _release(rig, "--poll", "0.01", "--ci-timeout", "1")
    assert out.returncode == 2
    assert "gate-fast (3.13, 2)" in out.stderr
    assert "soak" not in out.stderr
    # The soak is skipped when the blocking leg is red — the debt read
    # moved to `round cut`/`open`, so no soak verdict is noted here.
    assert "soak red" not in out.stdout
    assert rig.origin_ref("release/r1") == s  # left in place
    assert rig.deploy_log() == []


def test_red_soak_leg_is_recorded_debt_not_a_veto(rig: Rig) -> None:
    """me580555: a red `soak*` leg never blocks the release — the
    continue-on-error legs leave the run green, so wait_ci surfaces the
    debt itself (release.soak_red; skipped under PRECIS_ROUND_ROOT) and
    deploys anyway."""
    s = rig.shas["c3"]
    url = "https://github.com/o/r/actions/runs/4242/job/9"
    (rig.gh_dir / s).write_text(
        f"lint\tcompleted\tsuccess\t{url}\n"
        f"soak (3.13, 1)\tcompleted\tfailure\t{url}\n"
        f"soak (3.13, 2)\tcompleted\tsuccess\t{url}\n"
        f"verdict-soak\tcompleted\tfailure\t{url}\n",
        encoding="utf-8",
    )
    out = _release(rig, "--poll", "0.01", "--ci-timeout", "1", **_green(rig))
    assert out.returncode == 3, out.stdout + out.stderr
    assert (
        f"soak red on {s[:9]} (non-blocking): soak (3.13, 1), verdict-soak"
        in out.stdout
    )
    assert "soak red not recorded: PRECIS_ROUND_ROOT" in out.stdout
    assert rig.deploy_log() == [f"{s} --pinned"]


def test_ci_timeout_exits_two_and_names_branch(rig: Rig) -> None:
    out = _release(rig, *FAST)
    assert out.returncode == 2
    assert "CI not green after" in out.stderr
    assert "release/r1" in out.stderr
    assert "gh unreadable" in out.stdout


def test_rerun_after_cut_resumes_without_recutting(rig: Rig) -> None:
    first = _release(rig, *FAST)
    assert first.returncode == 2
    s = rig.shas["c3"]
    before = _round_json(rig)
    out = _release(rig, *FAST, **_green(rig))
    assert out.returncode == 3, out.stdout + out.stderr
    assert "resuming release release/r1" in out.stdout
    assert " cut at " not in out.stdout
    assert _rel(_round_json(rig))["base"] == _rel(before)["base"] == s
    assert _round_json(rig)["n"] == 1
    assert rig.deploy_log() == [f"{s} --pinned"]
    assert _next_line(s) in out.stdout


def test_rerun_with_runtime_pending_prints_next_line_again(rig: Rig) -> None:
    assert _release(rig, *FAST, **_green(rig)).returncode == 3
    s = rig.shas["c3"]
    out = _release(rig, *FAST, **_green(rig))
    assert out.returncode == 3, out.stdout + out.stderr
    assert _next_line(s) in out.stdout
    assert rig.deploy_log() == [f"{s} --pinned"]  # no second install


def test_deployed_round_is_closed_and_reopened_before_cutting(rig: Rig) -> None:
    s1 = rig.shas["c3"]
    assert _release(rig, *FAST, **_green(rig)).returncode == 3
    confirm = rig.round(
        "deploy",
        "--confirm-runtime",
        s1,
        "--runtime-evidence",
        "fake runtime: all required daemons ready",
    )
    assert confirm.returncode == 0, confirm.stderr
    state = _round_json(rig)
    assert state["open"] and state["deployed"] and not _live(state)
    s2 = _commit(rig.root, "src/c.py", "c4 code")
    _git(rig.root, "push", "-q", "origin", "main")
    verdict = {**_verdict(s2), "DEPLOY_STUB_VERIFY": "1"}
    out = _release(rig, *FAST, **verdict)
    assert out.returncode == 3, out.stdout + out.stderr
    assert "round 1 closed" in out.stdout
    assert "round 2 open" in out.stdout
    assert f"release/r2 cut at {s2[:9]}" in out.stdout
    assert _round_json(rig)["n"] == 2
    assert rig.origin_ref("release/r2") == s2
    assert rig.deploy_log() == [f"{s1} --pinned", f"{s2} --pinned"]


def test_cut_only_cuts_and_returns_without_waiting(rig: Rig) -> None:
    """td543733: --cut-only stops after the cut — no CI wait, no deploy."""
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    s = rig.shas["c3"]
    assert f"release/r1 cut at {s[:9]}" in out.stdout
    assert f"RELEASE_CUT: r1 {s}" in out.stdout.splitlines()
    assert any(ln.startswith("RELEASE_RANGE: ") for ln in out.stdout.splitlines())
    assert "waiting for CI" not in out.stdout
    assert rig.origin_ref("release/r1") == s
    assert rig.deploy_log() == []
    assert _marker(rig).exists()  # the later --deploy-only owns it


def test_cut_only_rerun_reports_the_own_release_without_recutting(rig: Rig) -> None:
    assert _release(rig, "--cut-only", *FAST).returncode == 0
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert " cut at " not in out.stdout
    assert f"RELEASE_CUT: r1 {rig.shas['c3']}" in out.stdout.splitlines()
    assert _round_json(rig)["n"] == 1


def test_cut_only_on_a_foreign_release_prints_no_cut_line(rig: Rig) -> None:
    work = _child(rig, rig.shas["c3"], "foreign release work")
    _push_branch(rig, "release/r7", work)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stderr
    assert "riding release release/r7" in out.stdout
    assert "RELEASE_CUT:" not in out.stdout


def test_cut_only_dry_run_stops_after_the_cut(rig: Rig) -> None:
    out = _release(rig, "--cut-only", "--dry-run", *FAST)
    assert out.returncode == 0, out.stderr
    assert "would stop after the cut (--cut-only)" in out.stdout
    assert "would deploy" not in out.stdout
    assert rig.origin_ref("release/r1") == ""


# 4-column canned check-runs satisfy both readers: release::ci_red needs
# >=3 tabs, round::_red_verdict needs exactly 3 (gr573151).
_RED_RUNS = (
    "lint\tcompleted\tfailure\thttps://ci.invalid/actions/runs/42\n"
    "test-linux (1)\tcompleted\tskipped\thttps://ci.invalid/actions/runs/42\n"
)


def test_cut_only_recuts_a_red_owned_release_once_the_fix_is_on_main(
    rig: Rig,
) -> None:
    """gr573151: --cut-only resuming our own release that went red with its
    head fully on main supersedes it and re-cuts at S — the fix landed.
    gr585785: the superseded head, an ancestor of main, is deleted on
    origin — a leftover would read in flight to release_poll."""
    c2 = rig.shas["c2"]
    out = _release(rig, "--cut-only", "--sha", c2, *FAST)
    assert out.returncode == 0, out.stdout + out.stderr  # r1 at c2
    (rig.gh_dir / c2).write_text(_RED_RUNS, encoding="utf-8")
    c3 = rig.shas["c3"]
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert (
        f"release release/r1 is red and its head is on main: "
        f"re-cutting at {c3[:9]}" in out.stdout
    )
    assert f"RELEASE_CUT: r2 {c3}" in out.stdout.splitlines()
    rel1 = _rel(_round_json(rig), 1)
    assert rel1["state"] == "superseded"
    assert rel1["superseded_by"] == 2
    assert rig.origin_ref("release/r1") == ""  # superseded head on main: deleted
    assert rig.origin_ref("release/r2") == c3
    mark = json.loads(_marker(rig).read_text(encoding="utf-8"))
    assert (mark["branch"], mark["base"]) == ("release/r2", c3)


def test_cut_only_recuts_a_red_release_while_its_rerun_is_in_flight(
    rig: Rig,
) -> None:
    """gr648267: release::ci_red counts a completed red leg while a rerun
    still runs; round::_red_verdict waits for every leg and reads it
    undecided. The red re-cut passes --supersede, so round's refusal
    ("already records an open release") never fires."""
    c2 = rig.shas["c2"]
    assert _release(rig, "--cut-only", "--sha", c2, *FAST).returncode == 0
    (rig.gh_dir / c2).write_text(
        "lint\tcompleted\tfailure\thttps://ci.invalid/actions/runs/42\n"
        "test-linux (1)\tin_progress\t\thttps://ci.invalid/actions/runs/42\n",
        encoding="utf-8",
    )
    c3 = rig.shas["c3"]
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "already records an open release" not in out.stderr
    assert f"RELEASE_CUT: r2 {c3}" in out.stdout.splitlines()
    assert _rel(_round_json(rig), 1)["state"] == "superseded"
    assert rig.origin_ref("release/r2") == c3


def test_cut_only_skips_a_sha_another_checkout_already_cut(rig: Rig) -> None:
    """gr646224: round.json is per checkout. An unrecorded release/* whose
    head holds S is another checkout's cut of S — in flight even with its
    head on main, so no second branch gates S; the job reads in-flight."""
    c3 = rig.shas["c3"]
    _push_branch(rig, "release/r5", c3)
    out = _release(rig, "--cut-only", "--sha", c3, *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "RELEASE_CUT:" not in out.stdout
    assert f"release in flight: release/r5 at {c3[:9]}" in out.stdout
    assert f"riding release release/r5 at {c3[:9]}" in out.stdout
    assert rig.origin_ref("release/r6") == ""
    assert classify_cut_refusal(out.stdout)["class"] == REFUSAL_IN_FLIGHT
    # A cut at an ancestor of that head rides it too.
    out = _release(rig, "--cut-only", "--sha", rig.shas["c2"], *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "RELEASE_CUT:" not in out.stdout
    assert rig.origin_ref("release/r6") == ""


def test_cut_only_reports_an_owned_green_release_without_recutting(
    rig: Rig,
) -> None:
    """Only a RED head-on-main release re-cuts; a green one reports."""
    c2 = rig.shas["c2"]
    assert _release(rig, "--cut-only", "--sha", c2, *FAST).returncode == 0
    (rig.gh_dir / c2).write_text(
        "lint\tcompleted\tsuccess\thttps://ci.invalid/actions/runs/42\n",
        encoding="utf-8",
    )
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "re-cutting" not in out.stdout
    assert f"RELEASE_CUT: r1 {c2}" in out.stdout.splitlines()
    assert _rel(_round_json(rig), 1)["state"] == "cut"
    assert rig.origin_ref("release/r2") == ""


def test_cut_only_a_failed_recut_keeps_the_pending_cut_owned(rig: Rig) -> None:
    """A refused re-cut push leaves r1 superseded + r2 cut_pending: the
    marker stays r2 (the pending cut's owner); the re-run reconciles."""
    c2 = rig.shas["c2"]
    assert _release(rig, "--cut-only", "--sha", c2, *FAST).returncode == 0
    (rig.gh_dir / c2).write_text(_RED_RUNS, encoding="utf-8")
    hook = _hook(
        rig,
        "while read old new ref; do\n"
        '  if [ "$ref" = refs/heads/release/r2 ]; then\n'
        '    echo "push of release/r2 refused" >&2\n'
        "    exit 1\n"
        "  fi\n"
        "done\n",
    )
    out = _release(rig, "--cut-only", *FAST)
    hook.unlink()
    assert out.returncode == 1
    assert _rel(_round_json(rig), 1)["state"] == "superseded"
    assert _rel(_round_json(rig), 2).get("cut_pending")
    mark = json.loads(_marker(rig).read_text(encoding="utf-8"))
    assert mark["branch"] == "release/r2"
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    c3 = rig.shas["c3"]
    assert f"RELEASE_CUT: r2 {c3}" in out.stdout.splitlines()
    assert rig.origin_ref("release/r2") == c3


def test_cut_only_a_refused_recut_restores_the_r1_marker(rig: Rig) -> None:
    """gr573151 review: ANY non-zero re-cut restores the marker — one left
    naming a branch that was never recorded makes our red release look
    foreign, and the next run rides it instead of reporting it."""
    c2 = rig.shas["c2"]
    assert _release(rig, "--cut-only", "--sha", c2, *FAST).returncode == 0
    marker_before = _marker(rig).read_bytes()
    round_before = (rig.root / ".git/precis-round/round.json").read_bytes()
    (rig.gh_dir / c2).write_text(_RED_RUNS, encoding="utf-8")
    # origin/prod sits off main's line: the re-cut's fast-forward guard
    # refuses before any record is written (a die(1), not a race -1).
    side = _child(rig, rig.shas["c0"], "prod side")
    _push_branch(rig, "prod", side)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 1
    assert "fast-forward" in out.stderr
    assert _marker(rig).read_bytes() == marker_before
    assert (rig.root / ".git/precis-round/round.json").read_bytes() == round_before
    # The re-run still owns r1: it attempts the re-cut again — never a
    # foreign "release in flight" ride.
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 1
    assert "release release/r1 is red" in out.stdout
    assert "release in flight" not in out.stdout


def test_cut_only_retires_a_dead_record_and_recuts(rig: Rig) -> None:
    """gr582590: the marker still names release/r1 after its branch was
    deleted on origin and prod moved past its base — the stale record is
    retired, the stale marker discarded, and --cut-only cuts r2 at main."""
    c2 = rig.shas["c2"]
    assert _release(rig, "--cut-only", "--sha", c2, *FAST).returncode == 0
    # A hand-cut release elsewhere superseded r1: the branch is deleted on
    # origin and prod already holds its base.
    _git(rig.root, "push", "-q", "origin", ":refs/heads/release/r1")
    _push_branch(rig, "prod", c2)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "retiring the stale record" in out.stdout
    assert "discarded" in out.stdout
    assert f"RELEASE_CUT: r2 {rig.shas['c3']}" in out.stdout.splitlines()
    assert rig.origin_ref("release/r2") == rig.shas["c3"]
    assert _rel(_round_json(rig), 1)["state"] == "abandoned"
    mark = json.loads(_marker(rig).read_text(encoding="utf-8"))
    assert (mark["branch"], mark["base"]) == ("release/r2", rig.shas["c3"])


def test_cut_only_adopts_an_unowned_pending_cut_and_pushes_it(rig: Rig) -> None:
    """gr593418: a hand-run `round cut` whose push had no credentials leaves
    cut_pending in this clone's record and no marker. --cut-only adopts it
    and finishes the push at the recorded base; a dry run only reports."""
    c2 = rig.shas["c2"]
    hook = _reject_release_pushes(rig)
    assert _release(rig, "--cut-only", "--sha", c2, *FAST).returncode == 1
    assert _rel(_round_json(rig))["cut_pending"] is True
    _marker(rig).unlink()  # the hand-run cut writes no owner marker
    hook.unlink()
    dry = _release(rig, "--cut-only", "--dry-run", *FAST)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "release in flight: release/r1" in dry.stdout
    assert rig.origin_ref("release/r1") == ""
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "adopting pending cut release/r1" in out.stdout
    assert "release in flight" not in out.stdout
    assert f"RELEASE_CUT: r1 {c2}" in out.stdout.splitlines()
    assert rig.origin_ref("release/r1") == c2
    assert "cut_pending" not in _rel(_round_json(rig))
    assert json.loads(_marker(rig).read_text(encoding="utf-8"))["base"] == c2


@pytest.mark.parametrize("owned", [True, False], ids=["owned", "unowned"])
def test_cut_only_supersedes_an_open_record_whose_branch_left_origin(
    rig: Rig, owned: bool
) -> None:
    """gr593418: an open record whose release/r<N> is gone from origin is
    dead, not in flight — --cut-only lets `round cut` retire it and cuts
    the next release at origin/main, owned marker or not. It never
    deployed, so it is recorded abandoned, not superseded (gr611217)."""
    c2 = rig.shas["c2"]
    assert _release(rig, "--cut-only", "--sha", c2, *FAST).returncode == 0
    if not owned:
        _marker(rig).unlink()
    _git(rig.origin, "update-ref", "-d", "refs/heads/release/r1")
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "abandoned r1: branch gone" in out.stdout
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "abandoned"
    assert rel["abandoned_why"] == "branch gone"
    assert "superseded_by" not in rel
    c3 = rig.shas["c3"]
    assert f"RELEASE_CUT: r2 {c3}" in out.stdout.splitlines()
    assert rig.origin_ref("release/r1") == ""
    assert rig.origin_ref("release/r2") == c3


def test_cut_only_an_unreadable_origin_keeps_the_open_record_in_flight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An ls-remote FAILURE is not absence: the open record stays live —
    in flight, never superseded, no cut attempted."""
    mod = _load_release(tmp_path, monkeypatch)
    rel = {
        "n": 1,
        "branch": "release/r1",
        "base": "a" * 40,
        "state": "cut",
        "cut_at": "2026-11-08T00:00:00Z",
    }
    monkeypatch.setattr(mod, "warn_hook_drift", lambda: None)
    monkeypatch.setattr(mod, "fetch", lambda: None)
    monkeypatch.setattr(mod, "rev", lambda name: "b" * 40)
    monkeypatch.setattr(mod, "git", lambda *a: "b" * 40)
    monkeypatch.setattr(mod, "remote", lambda ref: None)  # ls-remote failed
    monkeypatch.setattr(
        mod,
        "round_state",
        lambda: {"open": True, "n": 1, "releases": [dict(rel)]},
    )
    monkeypatch.setattr(mod, "read_owner", lambda: {})
    monkeypatch.setattr(mod, "release_branches", lambda: [])
    monkeypatch.setattr(mod, "head_containing", lambda *a: ("", False))
    calls: list[tuple[str, ...]] = []

    def _rnd(*a: str, **k: object) -> subprocess.CompletedProcess[str]:
        calls.append(a)
        return subprocess.CompletedProcess(a, 0, "", "")

    monkeypatch.setattr(mod, "rnd", _rnd)
    args = mod.argparse.Namespace(
        deploy_only="",
        sha="",
        adopt=False,
        dry_run=False,
        cut_only=True,
        ci_timeout=0.0,
        poll=0.0,
    )
    assert mod.run(args) == 0
    out = capsys.readouterr()
    assert "release in flight: release/r1" in out.out
    assert calls == []  # no cut, no supersede probe — the record stays live


def test_cut_only_retires_a_dead_pending_cut_the_shared_row_hides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """td597858 review: a newer shared row supersedes the stale cut_pending
    record — the row's filter hides it from `live`, but `round cut` still
    reads the local record and refuses the fresh main sha ("pending cut
    pins a different SHA"). The dead-open probe runs on the local records
    BEFORE the filter: r1 is abandoned and the run reaches the cut."""
    mod = _load_release(tmp_path, monkeypatch)
    base, prod, head = "a" * 40, "b" * 40, "c" * 40
    rel = {
        "n": 1,
        "branch": "release/r1",
        "base": base,
        "state": "cut",
        "cut_pending": True,
        "cut_at": "2026-11-08T00:00:00Z",
    }
    cur = {"open": True, "n": 1, "releases": [rel]}
    monkeypatch.setattr(mod, "warn_hook_drift", lambda: None)
    monkeypatch.setattr(mod, "fetch", lambda: None)
    monkeypatch.setattr(mod, "rev", lambda name: {"origin/main": head}.get(name, name))
    monkeypatch.setattr(mod, "git", lambda *a: head)
    monkeypatch.setattr(
        mod,
        "remote",
        lambda ref: {"refs/heads/prod": prod}.get(ref, ""),
    )
    monkeypatch.setattr(
        mod, "ancestor", lambda older, newer: (older, newer) == (base, prod)
    )
    monkeypatch.setattr(mod, "round_state", lambda: cur)
    monkeypatch.setattr(mod, "read_owner", lambda: {})
    monkeypatch.setattr(mod, "release_branches", lambda: [])
    monkeypatch.setattr(mod, "head_containing", lambda *a: ("", False))
    monkeypatch.setattr(
        mod,
        "read_round_record",
        lambda: {"n": 2, "branch": "release/r2", "state": "cut"},
    )
    calls: list[tuple[str, ...]] = []

    def _rnd(*a: str, **k: object) -> subprocess.CompletedProcess[str]:
        calls.append(a)
        if a == ("cut", "--abandon"):
            rel["state"] = "abandoned"
            rel.pop("cut_pending", None)
        return subprocess.CompletedProcess(a, 0, "", "")

    monkeypatch.setattr(mod, "rnd", _rnd)
    cuts: list[str] = []

    def _cut_and_finish(args: object, s: str, branch: str = "") -> int:
        cuts.append(s)
        return 0

    monkeypatch.setattr(mod, "cut_and_finish", _cut_and_finish)
    args = mod.argparse.Namespace(
        deploy_only="",
        sha="",
        adopt=False,
        dry_run=False,
        cut_only=True,
        ci_timeout=0.0,
        poll=0.0,
    )
    assert mod.run(args) == 0
    assert "retiring the stale record" in capsys.readouterr().out
    assert calls == [("cut", "--abandon")]
    assert cuts == [head]
    assert rel["state"] == "abandoned"


def test_cut_only_a_present_open_release_is_still_in_flight(rig: Rig) -> None:
    """The gone-branch rule does not weaken the in-flight guard: an open
    record whose branch IS on origin reports in flight exactly as today."""
    c2 = rig.shas["c2"]
    assert _release(rig, "--cut-only", "--sha", c2, *FAST).returncode == 0
    _marker(rig).unlink()
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "release in flight: release/r1" in out.stdout
    assert "superseded" not in out.stdout
    assert _rel(_round_json(rig), 1)["state"] == "cut"
    assert rig.origin_ref("release/r2") == ""


def test_deploy_only_deploys_the_cut_pinned(rig: Rig) -> None:
    s = rig.shas["c3"]
    assert _release(rig, "--cut-only", *FAST).returncode == 0
    out = _release(rig, "--deploy-only", "r1", *FAST, **_green(rig))
    assert out.returncode == 3, out.stdout + out.stderr
    assert " cut at " not in out.stdout
    assert any(
        ln.startswith(f"RELEASE_DEPLOYED: r1 {s} ") for ln in out.stdout.splitlines()
    )
    assert _next_line(s) in out.stdout
    assert rig.deploy_log() == [f"{s} --pinned"]


@pytest.mark.parametrize("which", ["r2", "bogus"])
def test_deploy_only_refuses_another_release_and_never_cuts(
    rig: Rig, which: str
) -> None:
    assert _release(rig, "--cut-only", *FAST).returncode == 0
    out = _release(rig, "--deploy-only", which, *FAST, **_green(rig))
    assert out.returncode == 1
    assert rig.deploy_log() == []
    assert rig.origin_ref("release/r2") == ""


def test_deploy_only_without_a_cut_refuses(rig: Rig) -> None:
    out = _release(rig, "--deploy-only", "r1", *FAST, **_green(rig))
    assert out.returncode == 1
    assert "not a live recorded release" in out.stderr
    assert rig.origin_ref("release/r1") == ""
    assert rig.deploy_log() == []


def test_deploy_only_refuses_a_release_this_tree_did_not_cut(rig: Rig) -> None:
    assert _release(rig, "--cut-only", *FAST).returncode == 0
    _marker(rig).unlink()
    out = _release(rig, "--deploy-only", "r1", *FAST, **_green(rig))
    assert out.returncode == 1
    assert "did not cut release/r1" in out.stderr
    assert rig.deploy_log() == []


def test_deploy_only_marks_the_release_failed_when_the_rollout_dies(
    rig: Rig,
) -> None:
    """gr593418 review 2: a deploy leg that ran and exited non-zero leaves
    a LIVE `failed` record — its journal carries `started_epoch`, so the
    partial rollout keeps the pin and the die names the resume, not the
    next cut; the journal and the branch stay."""
    s = rig.shas["c3"]
    assert _release(rig, "--cut-only", *FAST).returncode == 0
    out = _release(rig, "--deploy-only", "r1", *FAST, DEPLOY_STUB_RC="7", **_verdict(s))
    assert out.returncode == 7, out.stdout + out.stderr
    assert "the release pin is retained" in out.stderr
    assert "re-run scripts/release (it resumes)" in out.stderr
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "failed"
    assert rel["deployment"]["sha"] == s
    assert rel["deployment"]["started_epoch"]
    assert rig.origin_ref("release/r1") == s


def test_deploy_only_marks_a_prelaunch_deploy_death_terminal(rig: Rig) -> None:
    """gr593418 review 2: `round deploy` dying after its journal write but
    before `scripts/deploy` launched (the env pointer move refused) is
    `failed` AND terminal — the die names the next cut."""
    s = rig.shas["c3"]
    assert _release(rig, "--cut-only", *FAST).returncode == 0
    out = _release(
        rig,
        "--deploy-only",
        "r1",
        *FAST,
        PRECIS_ENV_POINTERS="0",
        **_verdict(s),
    )
    assert out.returncode == 1, out.stdout + out.stderr
    assert "the next `scripts/round cut` supersedes the record" in out.stderr
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "failed"
    assert "started_epoch" not in rel["deployment"]
    assert rig.deploy_log() == []  # scripts/deploy never ran
    assert rig.origin_ref("release/r1") == s


def test_deploy_only_resumes_a_failed_record_whose_rollout_launched(
    rig: Rig,
) -> None:
    """gr593418 review 2: `failed` + a launched rollout (`started_epoch`,
    no `rollout_at`) is a live partial rollout — --deploy-only still
    finds the record and resumes the pinned rollout."""
    s = rig.shas["c3"]
    assert _release(rig, "--cut-only", *FAST).returncode == 0
    assert rig.round("deploy", DEPLOY_STUB_RC="7", **_verdict(s)).returncode == 7
    assert rig.round("fail").returncode == 0
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "failed"
    assert rel["deployment"].get("started_epoch")
    assert not rel["deployment"].get("rollout_at")
    out = _release(rig, "--deploy-only", "r1", *FAST, **_green(rig))
    assert out.returncode == 3, out.stdout + out.stderr
    assert rig.deploy_log() == [f"{s} --pinned", f"{s} --pinned"]
    assert _rel(_round_json(rig), 1)["state"] == "deployed"


def test_deploy_only_refuses_a_terminal_failed_record(rig: Rig) -> None:
    """gr593418 review 2: `failed` whose journal shows `scripts/deploy`
    never launched is terminal — --deploy-only does not see it as a live
    release."""
    s = rig.shas["c3"]
    assert _release(rig, "--cut-only", *FAST).returncode == 0
    assert rig.round("deploy", PRECIS_ENV_POINTERS="0", **_verdict(s)).returncode == 1
    assert rig.round("fail").returncode == 0
    assert _rel(_round_json(rig), 1)["state"] == "failed"
    out = _release(rig, "--deploy-only", "r1", *FAST, **_green(rig))
    assert out.returncode == 1
    assert "not a live recorded release" in out.stderr
    assert rig.deploy_log() == []


def test_deploy_only_resume_names_the_release_not_the_round(rig: Rig) -> None:
    """gr593418 review 3: a live `failed` record counts as deploying in
    the resume lookups — else `release_label` falls back to the ROUND
    number and `RELEASE_DEPLOYED:` names the wrong release."""
    s = rig.shas["c3"]
    assert _release(rig, "--cut-only", *FAST).returncode == 0
    assert rig.round("deploy", DEPLOY_STUB_RC="7", **_verdict(s)).returncode == 7
    assert rig.round("fail").returncode == 0
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "failed" and rel["deployment"].get("started_epoch")
    # Release number and round number differ in general — make the round
    # 3 so a round-number fallback emits r3 while the release stays r1.
    path = rig.root / ".git" / "precis-round" / "round.json"
    cur = _round_json(rig)
    cur["n"] = 3
    path.write_text(json.dumps(cur), encoding="utf-8")
    out = _release(rig, "--deploy-only", "r1", *FAST, **_green(rig))
    assert out.returncode == 3, out.stdout + out.stderr
    lines = out.stdout.splitlines()
    assert any(ln.startswith(f"RELEASE_DEPLOYED: r1 {s} ") for ln in lines)
    assert not any(ln.startswith("RELEASE_DEPLOYED: r3") for ln in lines)


def test_deploy_and_close_labels_the_release_record_not_the_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """gr593418 review 3 (module seam): round 3 holding a live `failed`
    release r7 emits r7 — in `RELEASE_DEPLOYED:`, in the prod head's
    `release`, and in the branch `round fail` is given on a second
    failed leg (never a bare call)."""
    mod = _load_release(tmp_path, monkeypatch)
    rel = {
        "n": 7,
        "branch": "release/r7",
        "base": "c" * 40,
        "state": "failed",
        "deployment": {"sha": "b" * 40, "started_epoch": 1762000000},
    }
    monkeypatch.setattr(mod, "_regate_if_stale", lambda args: 0)
    monkeypatch.setattr(
        mod,
        "round_state",
        lambda: {"n": 3, "open": True, "releases": [dict(rel)]},
    )
    monkeypatch.setattr(mod, "rev", lambda name: "2" * 40)
    monkeypatch.setattr(mod, "warn_post_migration", lambda: None)
    monkeypatch.setattr(mod, "note_gripes", lambda *args: None)
    monkeypatch.setattr(mod, "validate_gripes", lambda: None)
    released: list[str | None] = []

    def _prod_head(
        prev: str, head: str, *, release: str | None = None, **kw: object
    ) -> str:
        released.append(release)
        return "recorded"

    monkeypatch.setattr(mod, "record_prod_head", _prod_head)
    monkeypatch.setattr(
        mod,
        "rnd",
        lambda *a, **k: subprocess.CompletedProcess(a, 3, "", ""),
    )
    args = mod.argparse.Namespace(deploy_only="r7")
    assert mod.deploy_and_close(args, "b" * 40) == 3
    out = capsys.readouterr().out
    assert f"RELEASE_DEPLOYED: r7 {'b' * 40}" in out
    assert released == ["r7"]
    # A second failed leg on the same live record names its branch.
    calls: list[tuple[str, ...]] = []

    def _died(*a: str, **k: object) -> subprocess.CompletedProcess[str]:
        calls.append(a)
        return subprocess.CompletedProcess(a, 7, "", "")

    monkeypatch.setattr(mod, "rnd", _died)
    assert mod.deploy_and_close(args, "b" * 40) == 7
    assert ("fail", "release/r7") in calls


def test_run_rides_a_live_failed_record_instead_of_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """gr593418 review 3 (module seam): a non-owner `scripts/release`
    sees the live `failed` record as the deploying record — 'riding' it
    when it ships S, never the in-flight blocker."""
    mod = _load_release(tmp_path, monkeypatch)
    head = "1" * 40
    rel = {
        "n": 7,
        "branch": "release/r7",
        "base": "c" * 40,
        "state": "failed",
        "deployment": {"sha": "b" * 40, "started_epoch": 1762000000},
    }
    monkeypatch.setattr(mod, "warn_hook_drift", lambda: None)
    monkeypatch.setattr(mod, "fetch", lambda: None)
    monkeypatch.setattr(mod, "rev", lambda name: head)
    monkeypatch.setattr(mod, "git", lambda *args: head)
    monkeypatch.setattr(
        mod,
        "round_state",
        lambda: {"n": 3, "open": True, "releases": [dict(rel)]},
    )
    monkeypatch.setattr(mod, "owns", lambda r: False)
    monkeypatch.setattr(mod, "read_owner", lambda: {})
    monkeypatch.setattr(mod, "remote", lambda ref: "")
    monkeypatch.setattr(mod, "release_branches", lambda: ["release/r7"])
    monkeypatch.setattr(mod, "head_containing", lambda *a: ("b" * 40, True))
    args = mod.argparse.Namespace(
        deploy_only="",
        sha=head,
        adopt=False,
        dry_run=False,
        cut_only=False,
        ci_timeout=0.0,
        poll=0.0,
    )
    assert mod.run(args) == 0
    out = capsys.readouterr().out
    assert "riding release release/r7" in out
    assert "release in flight" not in out


def test_deploy_only_a_pre_rollout_refusal_marks_nothing(rig: Rig) -> None:
    """gr593418: a refusal before any work (round deploy refused before
    its journal write) is not a deploy failure — the record stays open."""
    assert _release(rig, "--cut-only", *FAST).returncode == 0
    # A foreign ship pin makes `round deploy` refuse before journaling.
    (rig.root / ".ship-sha").write_text(rig.shas["c0"] + "\n", encoding="utf-8")
    s = rig.shas["c3"]
    out = _release(rig, "--deploy-only", "r1", *FAST, **_verdict(s))
    assert out.returncode == 1, out.stdout
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "cut"
    assert "deployment" not in rel
    assert rig.deploy_log() == []


def test_deploy_only_refusal_before_the_deploy_marks_nothing(rig: Rig) -> None:
    """gr593418: --deploy-only refused before the deploy leg runs (here:
    not the cutting worktree) never marks the record failed."""
    assert _release(rig, "--cut-only", *FAST).returncode == 0
    _marker(rig).unlink()
    out = _release(rig, "--deploy-only", "r1", *FAST, **_green(rig))
    assert out.returncode == 1
    assert "did not cut release/r1" in out.stderr
    assert _rel(_round_json(rig), 1)["state"] == "cut"


def test_cut_only_and_deploy_only_are_exclusive(rig: Rig) -> None:
    out = _release(rig, "--cut-only", "--deploy-only", "r1", *FAST)
    assert out.returncode == 2
    assert "not allowed with" in out.stderr


def test_dry_run_only_reads(rig: Rig) -> None:
    out = _release(rig, "--dry-run", *FAST)
    assert out.returncode == 0, out.stderr
    for line in (
        "would open round 1",
        f"would cut release/r1 at {rig.shas['c3'][:9]}",
        "would wait for CI",
        "would deploy",
    ):
        assert line in out.stdout
    assert rig.origin_ref("release/r1") == ""
    assert not (Path(rig.root) / ".git/precis-round/round.json").exists()
    assert rig.deploy_log() == []


def _marker(rig: Rig) -> Path:
    return rig.root / ".release-owner"


def test_cut_writes_owner_marker(rig: Rig) -> None:
    assert _release(rig, *FAST).returncode == 2  # cut, CI never green
    mark = json.loads(_marker(rig).read_text(encoding="utf-8"))
    rel = _rel(_round_json(rig))
    assert (mark["branch"], mark["base"]) == (rel["branch"], rel["base"])
    assert mark["cut_at"] == rel["cut_at"]
    assert mark["worktree"] == str(rig.root)


def test_sibling_without_marker_rides_recorded_release(rig: Rig) -> None:
    assert _release(rig, *FAST).returncode == 2
    _marker(rig).unlink()  # a sibling worktree has no marker
    out = _release(rig, *FAST, **_green(rig))
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"riding release release/r1 at {rig.shas['c3'][:9]}" in out.stdout
    assert rig.deploy_log() == []
    assert not _marker(rig).exists()


def test_sibling_runtime_pending_rides_not_confirms(rig: Rig) -> None:
    assert _release(rig, *FAST, **_green(rig)).returncode == 3
    _marker(rig).unlink()
    out = _release(rig, *FAST, **_green(rig))
    assert out.returncode == 0, out.stdout + out.stderr
    assert "next:" not in out.stdout
    assert rig.deploy_log() == [f"{rig.shas['c3']} --pinned"]


def test_adopt_takes_ownership_and_resumes(rig: Rig) -> None:
    assert _release(rig, *FAST).returncode == 2
    _marker(rig).unlink()
    out = _release(rig, "--adopt", *FAST, **_green(rig))
    assert out.returncode == 3, out.stdout + out.stderr
    assert "adopting release release/r1" in out.stderr
    assert _marker(rig).exists()
    assert rig.deploy_log() == [f"{rig.shas['c3']} --pinned"]


def test_marker_removed_once_release_is_retired(rig: Rig) -> None:
    s = rig.shas["c3"]
    assert _release(rig, *FAST, **_green(rig)).returncode == 3
    assert _marker(rig).exists()
    confirm = rig.round(
        "deploy", "--confirm-runtime", s, "--runtime-evidence", "fake runtime ok"
    )
    assert confirm.returncode == 0, confirm.stderr
    out = _release(rig, *FAST)
    assert out.returncode == 0
    assert "already live" in out.stdout
    assert not _marker(rig).exists()


def _reject_release_pushes(rig: Rig) -> Path:
    hook = rig.origin / "hooks" / "pre-receive"
    hook.write_text(
        '#!/bin/sh\nwhile read o n ref; do case "$ref" in refs/heads/release/*) exit 1;; esac; done\n',
        encoding="utf-8",
    )
    hook.chmod(0o755)
    return hook


def test_failed_cut_push_keeps_ownership_and_never_rides(rig: Rig) -> None:
    c2 = rig.shas["c2"]
    hook = _reject_release_pushes(rig)
    out = _release(rig, "--sha", c2, *FAST)
    assert out.returncode == 1
    assert _rel(_round_json(rig))["cut_pending"] is True
    assert _marker(rig).exists()
    assert rig.origin_ref("release/r1") == ""
    # a sibling neither rides the unpushed record nor deploys: next cut
    saved = _marker(rig).read_text(encoding="utf-8")
    _marker(rig).unlink()
    sib = _release(rig, "--sha", c2, *FAST)
    assert sib.returncode == 0, sib.stdout + sib.stderr
    assert "riding release" not in sib.stdout
    assert "rides the next cut" in sib.stdout
    # the owner resumes with the recorded base (not the newer main tip)
    _marker(rig).write_text(saved, encoding="utf-8")
    hook.unlink()
    own = _release(rig, *FAST)
    assert own.returncode == 2, own.stdout + own.stderr
    assert rig.origin_ref("release/r1") == c2
    assert json.loads(_marker(rig).read_text(encoding="utf-8"))["base"] == c2


def test_owner_acts_on_own_release_when_s_is_newer(rig: Rig) -> None:
    c2 = rig.shas["c2"]
    assert _release(rig, "--sha", c2, *FAST).returncode == 2
    out = _release(rig, *FAST, **{**_verdict(c2), "DEPLOY_STUB_VERIFY": "1"})
    assert out.returncode == 3, out.stdout + out.stderr
    assert "queued" not in out.stdout
    assert "rides the next release" in out.stdout
    assert _next_line(c2) in out.stdout
    assert rig.deploy_log() == [f"{c2} --pinned"]


def test_failed_close_is_retried_before_already_live(rig: Rig) -> None:
    s = rig.shas["c3"]
    assert _release(rig, *FAST, **_green(rig)).returncode == 3
    done = rig.round("deploy", "--confirm-runtime", s, "--runtime-evidence", "ok")
    assert done.returncode == 0, done.stderr
    assert _round_json(rig)["open"] is True  # close never ran
    out = _release(rig, *FAST)
    assert out.returncode == 0
    assert "already live" in out.stdout
    assert _round_json(rig)["open"] is False
    assert not _marker(rig).exists()


def test_already_live_leaves_completed_round_open_without_owner_marker(
    rig: Rig,
) -> None:
    sha = rig.shas["c3"]
    assert _release(rig, *FAST, **_green(rig)).returncode == 3
    confirmed = rig.round(
        "deploy", "--confirm-runtime", sha, "--runtime-evidence", "ok"
    )
    assert confirmed.returncode == 0, confirmed.stderr
    _marker(rig).unlink()
    previous = rig.deploy_log()
    result = _release(rig, *FAST)
    assert result.returncode == 0, result.stderr
    assert _round_json(rig)["open"] is True
    assert rig.deploy_log() == previous
    assert not _marker(rig).exists()


def test_rerun_failure_superseded_by_newer_check_run_is_not_red(rig: Rig) -> None:
    s = rig.shas["c3"]
    url = "https://github.com/o/r/actions/runs/4242/job/9"
    (rig.gh_dir / s).write_text(
        f"lint\tcompleted\tsuccess\t{url}\t{url}\t5\n"
        f"test-linux (2)\tcompleted\tfailure\t{url}\t{url}\t7\n"
        f"test-linux (2)\tin_progress\t\t{url}\t{url}\t9\n",
        encoding="utf-8",
    )
    out = _release(rig, *FAST)
    assert out.returncode == 2
    assert "CI not green after" in out.stderr  # timeout, not a red verdict
    assert "CI red" not in out.stderr


def test_stale_run_cancelled_shard_does_not_poison_fresh_run(rig: Rig) -> None:
    """gr504329: check-runs are sha-keyed, so a cancelled shard from a
    superseded workflow run used to read as this run's red. The verdict
    scopes to the current run — a job name absent from it is pending."""
    s = rig.shas["c3"]
    old = "https://github.com/o/r/actions/runs/1111/job/1"
    new = "https://github.com/o/r/actions/runs/2222/job/2"
    lines = [f"test-linux (12)\tcompleted\tcancelled\t{old}\t{old}\t1"]
    lines += [
        f"test-linux ({i})\tin_progress\t\t{new}\t{new}\t{100 + i}"
        for i in range(1, 12)
    ]
    (rig.gh_dir / s).write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = _release(rig, *FAST)
    assert out.returncode == 2
    assert "CI not green after" in out.stderr  # timeout, not a red verdict
    assert "CI red" not in out.stderr


def test_red_names_job_and_run_of_the_current_run(rig: Rig) -> None:
    """Same scoping, red case: the current run's own failed shard turns
    the verdict red, naming the job and the *newer* run id — never the
    older run's cancelled row."""
    s = rig.shas["c3"]
    (rig.gh_dir / "run-id").write_text("2222\n", encoding="utf-8")
    old = "https://github.com/o/r/actions/runs/1111/job/1"
    new = "https://github.com/o/r/actions/runs/2222/job/2"
    (rig.gh_dir / s).write_text(
        f"test-linux (12)\tcompleted\tcancelled\t{old}\t{old}\t1\n"
        f"test-linux (3)\tcompleted\tfailure\t{new}\t{new}\t103\n"
        f"test-linux (1)\tin_progress\t\t{new}\t{new}\t101\n",
        encoding="utf-8",
    )
    out = _release(rig, "--poll", "0.01", "--ci-timeout", "1")
    assert out.returncode == 2
    assert "test-linux (3)" in out.stderr
    assert "test-linux (12)" not in out.stderr
    assert "scripts/ci-failed 2222" in out.stderr


def _load_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    copy = tmp_path / "release_script.py"
    copy.write_text(RELEASE.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "lib").mkdir(exist_ok=True)
    for name in ("ci_run.py", "git_network.py", "soak_verdict.py", "round_state.py"):
        (tmp_path / "lib" / name).write_text(
            (RELEASE.parent / "lib" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    mod = load_script("release_script", copy)
    monkeypatch.setattr(mod, "marker_path", lambda: tmp_path / ".release-owner")
    # In-process runs stand in for the factory release pass (gr675506).
    monkeypatch.setenv("PRECIS_RELEASE_PASS", "td1")
    return mod


def test_completed_non_green_run_fails_fast_with_queued_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load_release(tmp_path, monkeypatch)
    head = "1" * 40
    run = {
        "status": "completed",
        "conclusion": "failure",
        "jobs": [
            {"name": "shard 2", "status": "queued", "conclusion": ""},
            {"name": "test-linux (2)", "status": "queued", "conclusion": ""},
        ],
    }

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[1] == "api":
            return subprocess.CompletedProcess(command, 0, "", "")
        assert command[1:4] == ["run", "view", "4242"]
        return subprocess.CompletedProcess(command, 0, json.dumps(run), "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod, "_regate_run_id", lambda sha: "4242")
    monkeypatch.setattr(
        mod,
        "gate_facts",
        lambda: {"release_head": head, "release_branch": "release/r1"},
    )
    monkeypatch.setattr(mod, "note_ci_verdict", lambda *args: None)
    sleeps: list[float] = []
    monkeypatch.setattr(mod.time, "sleep", sleeps.append)
    args = mod.argparse.Namespace(ci_timeout=90.0, poll=60.0)
    assert mod.wait_ci("release/r1", args) == 2
    assert sleeps == [60.0]  # Confirm rerun lag once, then fail fast.
    err = capsys.readouterr().err
    assert "never concluded: shard 2 (queued)" in err
    assert "test-linux (2)" in err
    assert "full rerun needed: gh run rerun 4242" in err


def test_ci_red_gate_fast_leg_is_the_verdict_soak_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """me580555: `_blocking_leg` counts `gate-fast` (and still test-linux);
    a red `soak*` row alone is never the CI verdict."""
    mod = _load_release(tmp_path, monkeypatch)
    url = "https://github.com/o/r/actions/runs/4242/job/9"
    rows = [
        ["lint", "completed", "success", url],
        ["gate-fast (3.13, 2)", "completed", "failure", url],
        ["soak (3.13, 1)", "completed", "failure", url],
    ]
    monkeypatch.setattr(mod, "_check_run_rows", lambda h: (rows, 0))
    red = mod.ci_red("1" * 40)
    assert isinstance(red, dict)
    assert red["jobs"] == ["gate-fast (3.13, 2)"]
    assert red["urls"] == [url]
    assert red["run_id"] == "4242"
    soak_only = [rows[0], rows[2]]
    monkeypatch.setattr(mod, "_check_run_rows", lambda h: (soak_only, 0))
    assert mod.ci_red("1" * 40) is None


def test_soak_red_reports_only_red_soak_legs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`soak_red` sees only completed non-OK `soak*` legs — a red blocking
    leg is ci_red's business, and unreadable `gh` is False."""
    mod = _load_release(tmp_path, monkeypatch)
    url = "https://github.com/o/r/actions/runs/4242/job/9"
    rows = [
        ["soak (3.13, 1)", "completed", "failure", url],
        ["soak (3.13, 2)", "completed", "success", url],
        ["soak (3.13, 3)", "in_progress", "", url],
        ["gate-fast (3.13, 1)", "completed", "failure", url],
    ]
    monkeypatch.setattr(mod, "_check_run_rows", lambda h: (rows, 0))
    red = mod.soak_red("1" * 40)
    assert isinstance(red, dict)
    assert red["jobs"] == ["soak (3.13, 1)"]
    assert red["run_id"] == "4242"
    monkeypatch.setattr(mod, "_check_run_rows", lambda h: ([rows[1]], 0))
    assert mod.soak_red("1" * 40) is None
    monkeypatch.setattr(mod, "_check_run_rows", lambda h: False)
    assert mod.soak_red("1" * 40) is False


def test_record_soak_red_writes_the_full_sha_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The debt row carries the FULL sha (round cut's ancestor test needs
    it), run_id, jobs and a Z stamp under `release.soak_red`."""
    mod = _load_release(tmp_path, monkeypatch)
    sent: list[list[str]] = []

    def transport(args: list[str]) -> tuple[int, str, str]:
        sent.append(args)
        return 0, "", ""

    sha = "c0ffee" + "1" * 34
    stamp = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
    line = mod.record_soak_red(
        sha, "4242", ["soak (3.13, 1)"], transport=transport, now=stamp
    )
    assert line == f"soak red recorded: {sha[:9]} run 4242"
    assert sent[0][:3] == ["settings", "set", mod.SOAK_RED_KEY]
    assert json.loads(sent[0][3]) == {
        "sha": sha,
        "run_id": "4242",
        "jobs": ["soak (3.13, 1)"],
        "at": "2026-11-07T12:00:00Z",
    }
    monkeypatch.setattr(mod, "_record_skip_reason", lambda: "no prod DSN on this host")
    assert (
        mod.record_soak_red(sha, "4242", ["x"])
        == "soak red not recorded: no prod DSN on this host"
    )
    assert (
        mod.record_soak_red("", "4242", ["x"], transport=transport)
        == "soak red not recorded: no sha"
    )
    assert len(sent) == 1  # neither refusal contacted the transport


@pytest.mark.parametrize("verdict", ["red", "unreadable", "clean"])
def test_note_soak_says_and_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verdict: str,
) -> None:
    """`note_soak` is best-effort: a red verdict is said and recorded, an
    unreadable one is said once, a clean one is silent — none raises."""
    mod = _load_release(tmp_path, monkeypatch)
    head = "c0ffee" + "1" * 34
    verdicts = {
        "red": {
            "jobs": ["soak (3.13, 1)"],
            "urls": ["https://u/1"],
            "run_id": "4242",
        },
        "unreadable": False,
        "clean": None,
    }
    monkeypatch.setattr(mod, "soak_red", lambda h: verdicts[verdict])
    recorded: list[tuple[str, str, list[str]]] = []

    def _record(sha: str, rid: str, jobs: list[str]) -> str:
        recorded.append((sha, rid, jobs))
        return "rec"

    monkeypatch.setattr(mod, "record_soak_red", _record)
    mod.note_soak(head)
    out = capsys.readouterr().out
    if verdict == "red":
        assert f"soak red on {head[:9]} (non-blocking): soak (3.13, 1)" in out
        assert "https://u/1" in out
        assert recorded == [(head, "4242", ["soak (3.13, 1)"])]
    elif verdict == "unreadable":
        assert "soak legs unreadable" in out
        assert recorded == []
    else:
        assert out == ""
        assert recorded == []


def test_ci_unreadable_reports_original_gh_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load_release(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, "", "HTTP 503: service unavailable"
        ),
    )
    assert mod.ci_red("1" * 40) is False
    assert "HTTP 503: service unavailable" in capsys.readouterr().err


def test_finish_pointer_never_regates_or_starts_rollout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_release(tmp_path, monkeypatch)
    calls: list[tuple[str, ...]] = []

    def fake_round(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 3, "", "")

    monkeypatch.setattr(mod, "rnd", fake_round)
    monkeypatch.setattr(mod, "_regate_if_stale", lambda args: pytest.fail("regated"))
    monkeypatch.setattr(mod, "round_state", lambda: {"n": 1})
    monkeypatch.setattr(mod, "rev", lambda name: "2" * 40)
    monkeypatch.setattr(mod, "record_prod_head", lambda *args, **kwargs: "recorded")
    monkeypatch.setattr(mod, "warn_post_migration", lambda: None)
    monkeypatch.setattr(mod, "note_gripes", lambda *args: None)
    monkeypatch.setattr(mod, "validate_gripes", lambda: None)
    args = mod.argparse.Namespace(deploy_only="")
    assert mod.deploy_and_close(args, "1" * 40, finish_pointer=True) == 3
    assert calls == [("deploy", "--finish-pointer")]


@pytest.mark.parametrize(
    ("already_live", "pipelined"), [(False, False), (True, False), (True, True)]
)
def test_release_resumes_only_pointer_for_converged_or_already_live_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, already_live: bool, pipelined: bool
) -> None:
    mod = _load_release(tmp_path, monkeypatch)
    head = "1" * 40
    rel = {
        "branch": "release/r1",
        "base": head,
        "state": "deploying",
        "deployment": {"sha": head, "converged": not already_live},
    }
    calls: list[tuple[str, bool]] = []
    releases = [rel]
    if pipelined:
        releases.append({"branch": "release/r2", "base": "2" * 40, "state": "cut"})

    def finish(args: object, sha: str, *, finish_pointer: bool = False) -> int:
        calls.append((sha, finish_pointer))
        return 3

    monkeypatch.setattr(mod, "warn_hook_drift", lambda: None)
    monkeypatch.setattr(mod, "fetch", lambda: None)
    monkeypatch.setattr(mod, "rev", lambda name: head)
    monkeypatch.setattr(mod, "git", lambda *args: head)
    monkeypatch.setattr(
        mod, "round_state", lambda: {"open": True, "releases": releases}
    )
    monkeypatch.setattr(mod, "owns", lambda release: release is rel)
    monkeypatch.setattr(mod, "read_owner", lambda: {})
    monkeypatch.setattr(mod, "remote", lambda ref: head if already_live else "")
    monkeypatch.setattr(mod, "ancestor", lambda *args: True)
    monkeypatch.setattr(mod, "deploy_and_close", finish)
    args = mod.argparse.Namespace(
        deploy_only="", sha=head, adopt=False, dry_run=False, cut_only=False
    )
    assert mod.run(args) == 3
    assert calls == [(head, True)]


@pytest.mark.parametrize("method", ["round_state", "gate_facts"])
def test_round_read_failure_preserves_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    method: str,
) -> None:
    mod = _load_release(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mod,
        "rnd",
        lambda *args: subprocess.CompletedProcess(
            args, 1, "", "git remote error: upstream unavailable"
        ),
    )
    assert not getattr(mod, method)()
    err = capsys.readouterr().err
    assert "exit 1" in err
    assert "git remote error: upstream unavailable" in err


def test_gh_timeout_preserves_captured_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load_release(tmp_path, monkeypatch)

    def timed_out(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(
            ["gh", "api"], 120, stderr=b"connection reset by peer"
        )

    monkeypatch.setattr(mod.subprocess, "run", timed_out)
    assert mod.ci_red("1" * 40) is False
    err = capsys.readouterr().err
    assert "timed out after 120 seconds" in err
    assert "connection reset by peer" in err


SKIP_LINE = "ci verdict not recorded: PRECIS_ROUND_ROOT names another checkout"


def test_green_gate_reaches_the_verdict_recorder(rig: Rig) -> None:
    """The rig names another checkout, so the real recorder refuses with its
    skip reason — the line proves wait_ci called it on the green path."""
    out = _release(rig, *FAST, **_green(rig))
    assert out.returncode == 3, out.stdout + out.stderr
    assert SKIP_LINE in out.stdout


def test_red_gate_reaches_the_verdict_recorder(rig: Rig) -> None:
    s = rig.shas["c3"]
    url = "https://github.com/o/r/actions/runs/4242/job/9"
    (rig.gh_dir / s).write_text(
        f"lint\tcompleted\tsuccess\t{url}\ntest-linux (1)\tcompleted\tfailure\t{url}\n",
        encoding="utf-8",
    )
    out = _release(rig, "--poll", "0.01", "--ci-timeout", "1")
    assert out.returncode == 2
    assert SKIP_LINE in out.stdout


@pytest.mark.parametrize("verdict", ["green", "red"])
def test_wait_ci_records_the_verdict_with_range_and_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str
) -> None:
    mod = _load_release(tmp_path, monkeypatch)
    head = "c0ffee" + "1" * 34
    prod = "9" * 40
    calls: list[tuple[str, str, str, str]] = []
    facts = {
        "release_head": head,
        "release_branch": "release/r1",
        "candidate": head if verdict == "green" else "",
        "candidate_state": "found",
    }
    monkeypatch.setattr(mod, "gate_facts", lambda: facts)
    monkeypatch.setattr(mod, "rev", lambda name: prod)
    monkeypatch.setattr(mod, "_regate_run_id", lambda sha: "555")
    monkeypatch.setattr(
        mod, "ci_red", lambda h: {"jobs": ["lint"], "urls": [], "run_id": "666"}
    )
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)

    def _record(sha: str, rng: str, v: str, run_id: str = "") -> str:
        calls.append((sha, rng, v, run_id))
        return "ok"

    monkeypatch.setattr(mod, "record_ci_verdict", _record)
    args = mod.argparse.Namespace(ci_timeout=1.0, poll=0.0)
    rc = mod.wait_ci("release/r1", args)
    assert rc == (0 if verdict == "green" else 2)
    n = mod.LANDED_SHA_LEN
    run_id = "555" if verdict == "green" else "666"
    assert calls == [(head, f"{prod[:n]}..{head[:n]}", verdict, run_id)]


def test_wait_ci_proceeds_on_a_waived_head_without_re_recording(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The red-test waiver (`precis.plans.red_waiver`): `round gate` names
    the head the candidate with state `waived`. wait_ci proceeds, logs the
    gripe, and records NOTHING — a green re-record would overwrite the
    waiver row the store-side gates read."""
    mod = _load_release(tmp_path, monkeypatch)
    head = "c0ffee" + "1" * 34
    facts = {
        "release_head": head,
        "release_branch": "release/r1",
        "candidate": head,
        "candidate_state": "waived",
        "certificate": {
            "sha": head,
            "waived": {"gripe": 77, "tests": ["tests/test_w.py::test_a"]},
        },
    }
    monkeypatch.setattr(mod, "gate_facts", lambda: facts)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    recorded: list[Any] = []
    monkeypatch.setattr(mod, "note_ci_verdict", lambda *a: recorded.append(a))
    monkeypatch.setattr(mod, "note_soak", lambda *a: recorded.append(a))
    monkeypatch.setattr(mod, "ci_red", lambda h: recorded.append((h,)))
    args = mod.argparse.Namespace(ci_timeout=1.0, poll=0.0)
    assert mod.wait_ci("release/r1", args) == 0
    assert recorded == []
    out = capsys.readouterr().out
    assert "WAIVED by gr77" in out and "tests/test_w.py::test_a" in out


def test_a_red_never_overwrites_a_waived_row_but_a_green_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The waiver row is a standing decision: wait_ci's red record (or a
    retried poll) on the same sha leaves it; a fresh green replaces it;
    other shas and plain rows behave as before."""
    mod = _load_release(tmp_path, monkeypatch)
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    head = "c0ffee" + "1" * 34
    waived = {
        "sha": head[:12],
        "range": "a..b",
        "verdict": "green",
        "run_id": "31",
        "at": "2026-11-10T11:00:00Z",
        "waived": {
            "gripe": 77,
            "tests": ["tests/t.py::a"],
            "run_id": "31",
            "attempt": 1,
        },
    }
    other = {"sha": "1" * 12, "verdict": "red", "run_id": "1", "at": "x"}
    prev = [other, waived]
    # a red from the SAME run (31) leaves the waiver
    assert mod.ci_verdict_rows(head, "a..b", "red", "31", prev, now) == prev
    # a red from a NEWER run on the head is that run's own verdict
    newer = mod.ci_verdict_rows(head, "a..b", "red", "99", prev, now)
    assert [(r["verdict"], r["run_id"]) for r in newer] == [("red", "1"), ("red", "99")]
    assert all("waived" not in r for r in newer)
    # a waiver marker with no run id never protects the row
    loose = {**waived, "waived": {"gripe": 77}}
    rows = mod.ci_verdict_rows(head, "a..b", "red", "", [loose], now)
    assert [r["verdict"] for r in rows] == ["red"] and "waived" not in rows[0]
    rows = mod.ci_verdict_rows(head, "a..b", "green", "99", prev, now)
    assert [r["verdict"] for r in rows] == ["red", "green"]
    assert "waived" not in rows[-1] and rows[-1]["run_id"] == "99"
    # a red on a different sha, or over a plain row, records as ever
    rows = mod.ci_verdict_rows("2" * 40, "a..b", "red", "5", prev, now)
    assert rows[:2] == prev and rows[-1]["sha"] == "2" * 12
    plain = {**waived}
    del plain["waived"]
    rows = mod.ci_verdict_rows(head, "a..b", "red", "99", [plain], now)
    assert [(r["verdict"], r["run_id"]) for r in rows] == [("red", "99")]


def test_wait_ci_green_path_is_unchanged_when_not_waived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_release(tmp_path, monkeypatch)
    head = "c0ffee" + "1" * 34
    facts = {
        "release_head": head,
        "release_branch": "release/r1",
        "candidate": head,
        "candidate_state": "found",
        "certificate": {"sha": head, "waived": {"gripe": 1}},  # not state=waived
    }
    monkeypatch.setattr(mod, "gate_facts", lambda: facts)
    monkeypatch.setattr(mod, "_regate_run_id", lambda sha: "555")
    seen: list[tuple[Any, ...]] = []
    monkeypatch.setattr(mod, "note_ci_verdict", lambda *a: seen.append(a))
    monkeypatch.setattr(mod, "note_soak", lambda *a: None)
    args = mod.argparse.Namespace(ci_timeout=1.0, poll=0.0)
    assert mod.wait_ci("release/r1", args) == 0
    assert seen == [(head, "green", "555")]


@pytest.mark.parametrize("verdict", ["green", "red"])
def test_wait_ci_notes_soak_on_green_and_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str
) -> None:
    """me580555 round 1: wait_ci notes the soak only on the green path —
    a red gate-fast run skipped the soak outright, so there is nothing
    to record there (the debt read moved to `round cut`/`open`)."""
    mod = _load_release(tmp_path, monkeypatch)
    head = "c0ffee" + "1" * 34
    facts = {
        "release_head": head,
        "release_branch": "release/r1",
        "candidate": head if verdict == "green" else "",
        "candidate_state": "found",
    }
    monkeypatch.setattr(mod, "gate_facts", lambda: facts)
    monkeypatch.setattr(mod, "rev", lambda name: "9" * 40)
    monkeypatch.setattr(mod, "_regate_run_id", lambda sha: "555")
    red = {"jobs": ["gate-fast (3.13, 1)"], "urls": [], "run_id": "666"}
    monkeypatch.setattr(mod, "ci_red", lambda h: red)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(mod, "note_ci_verdict", lambda *args: None)
    noted: list[str] = []
    monkeypatch.setattr(mod, "note_soak", noted.append)
    args = mod.argparse.Namespace(ci_timeout=1.0, poll=0.0)
    rc = mod.wait_ci("release/r1", args)
    assert rc == (0 if verdict == "green" else 2)
    assert noted == ([head] if verdict == "green" else [])


@pytest.mark.parametrize(
    ("cut_out", "claims"),
    [
        ("release/r1 cut at abcdef123 — late work", True),
        ("release/r1 pending cut reconciled at abcdef123", False),
    ],
)
def test_reconciling_anothers_pending_cut_does_not_claim_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cut_out: str, claims: bool
) -> None:
    mod = _load_release(tmp_path, monkeypatch)
    sha = "abcdef123" + "0" * 31
    rel = {"branch": "release/r1", "base": sha, "cut_at": "t"}
    monkeypatch.setattr(
        mod,
        "rnd",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=cut_out, stderr=""),
    )
    monkeypatch.setattr(
        mod,
        "round_state",
        lambda: {"n": 1, "releases": [{**rel, "n": 1, "state": "cut"}]},
    )
    monkeypatch.setattr(mod, "finish", lambda args, s: 77)
    args = mod.argparse.Namespace(adopt=False)
    assert mod.cut_and_finish(args, sha) == (77 if claims else -1)
    assert (tmp_path / ".release-owner").exists() is claims
    if not claims:  # an owner whose marker pre-existed keeps it on reconcile
        mod.write_owner("release/r1", sha, "t")
        assert mod.cut_and_finish(args, sha) == 77


def test_cut_and_finish_refuses_a_sha_already_on_prod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """gr582590: a cut whose base prod already holds arms a rollback —
    cut_and_finish refuses before `round cut` runs or a marker lands."""
    mod = _load_release(tmp_path, monkeypatch)
    sha, prod = "a" * 40, "b" * 40
    monkeypatch.setattr(
        mod, "remote", lambda ref: prod if ref == "refs/heads/prod" else ""
    )
    monkeypatch.setattr(mod, "rev", lambda name: name)
    monkeypatch.setattr(mod, "ancestor", lambda a, b: True)

    def boom(*a: str, **k: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("round cut must not run")

    monkeypatch.setattr(mod, "rnd", boom)
    assert mod.cut_and_finish(mod.argparse.Namespace(), sha, "release/r1") == 1
    assert "already on origin/prod" in capsys.readouterr().err
    assert not (tmp_path / ".release-owner").exists()


def test_live_releases_prefers_the_shared_row_over_a_forked_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr582590: the shared release.round row adjudicates open records —
    a forked clone's cut record is not live; a deploying journal is."""
    mod = _load_release(tmp_path, monkeypatch)
    state = {
        "releases": [
            {"n": 9, "branch": "release/r9", "state": "cut"},
            {"n": 8, "branch": "release/r8", "state": "confirmed"},
            {"n": 10, "branch": "release/r10", "state": "deploying"},
        ]
    }
    # The shared row moved past the local open record: a fork, not live.
    live = mod.live_releases(state, {"n": 10, "branch": "release/r10", "state": "cut"})
    assert [r["n"] for r in live] == [10]
    # Same release terminal in the shared row: retired elsewhere.
    live = mod.live_releases(
        state, {"n": 9, "branch": "release/r9", "state": "confirmed"}
    )
    assert [r["n"] for r in live] == [10]
    # An older shared row: the local record is newer — it stands.
    live = mod.live_releases(state, {"n": 9, "branch": "release/r9", "state": "cut"})
    assert [r["n"] for r in live] == [9, 10]
    # No row: the local record stands alone.
    assert [r["n"] for r in mod.live_releases(state)] == [9, 10]


def test_stale_round_number_mints_the_origin_number(rig: Rig) -> None:
    """gr573151: a stale local record used to refuse a deployed/r<N> it did
    not take; the cut now numbers past origin and the marker names r2."""
    assert rig.round("open").returncode == 0
    _git(rig.root, "push", "-q", "origin", f"{rig.shas['c0']}:refs/tags/deployed/r1")
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"RELEASE_CUT: r2 {rig.shas['c3']}" in out.stdout.splitlines()
    mark = json.loads(_marker(rig).read_text(encoding="utf-8"))
    assert mark["branch"] == "release/r2"
    assert rig.origin_ref("release/r1") == ""
    assert rig.origin_ref("release/r2") == rig.shas["c3"]


def test_dry_run_names_the_origin_numbered_release(rig: Rig) -> None:
    assert rig.round("open").returncode == 0
    _git(rig.root, "push", "-q", "origin", f"{rig.shas['c0']}:refs/tags/deployed/r1")
    out = _release(rig, "--dry-run", *FAST)
    assert out.returncode == 0, out.stderr
    assert "would cut release/r2" in out.stdout
    assert rig.origin_ref("release/r1") == ""
    assert rig.origin_ref("release/r2") == ""


# ── stale-verdict re-gate (release re-runs CI instead of refusing) ──────────


def _verdict_file(
    rig: Rig, name: str, sha: str, age: float, state: str = "found"
) -> Path:
    data = json.loads(_verdict(sha, age, state)["LGM_EXACT"])
    p = rig.gh_dir / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


_FLIP_HOOK = '[ -f "$GH_STUB_DIR/reran" ] && cat "$GH_STUB_DIR/v-fresh" || cat "$GH_STUB_DIR/v-stale"'


def test_stale_verdict_reruns_ci_on_the_same_sha_and_deploys(rig: Rig) -> None:
    s = rig.shas["c3"]
    _verdict_file(rig, "v-stale", s, 9.0)
    _verdict_file(rig, "v-fresh", s, 0.1)
    (rig.gh_dir / "run-id").write_text("4242\n", encoding="utf-8")
    out = _release(
        rig, "--sha", s, *FAST, LGM_EXACT_HOOK=_FLIP_HOOK, DEPLOY_STUB_VERIFY="1"
    )
    assert out.returncode == 3, out.stdout + out.stderr
    assert "re-running CI on the same sha" in out.stdout
    assert "re-ran check.yml run 4242" in out.stdout
    assert (rig.gh_dir / "reran").exists()
    assert rig.deploy_log() == [f"{s} --pinned"]


def test_partial_deploy_resumes_after_fresh_same_sha_verdict(rig: Rig) -> None:
    s = rig.shas["c3"]
    failed = _release(rig, "--sha", s, *FAST, **_verdict(s), DEPLOY_STUB_RC="9")
    assert failed.returncode == 9, failed.stdout + failed.stderr
    journal = _rel(_round_json(rig))["deployment"]
    assert journal["sha"] == s and "rollout_at" not in journal
    _verdict_file(rig, "v-stale", s, 9.0)
    _verdict_file(rig, "v-fresh", s, 0.1)
    (rig.gh_dir / "run-id").write_text("4242\n", encoding="utf-8")
    stale = rig.round("deploy", LGM_EXACT_HOOK=_FLIP_HOOK)
    assert stale.returncode == 1 and "re-gate the same release head" in stale.stderr
    assert rig.deploy_log() == [f"{s} --pinned"]
    resumed = _release(
        rig, "--sha", s, *FAST, LGM_EXACT_HOOK=_FLIP_HOOK, DEPLOY_STUB_VERIFY="1"
    )
    assert resumed.returncode == 3, resumed.stdout + resumed.stderr
    assert "re-ran check.yml run 4242" in resumed.stdout
    assert _rel(_round_json(rig))["deployment"]["sha"] == s
    assert rig.deploy_log() == [f"{s} --pinned", f"{s} --pinned"]
    assert rig.origin_ref("release/r1") == s


def test_stale_verdict_rerun_going_red_still_refuses(rig: Rig) -> None:
    s = rig.shas["c3"]
    _verdict_file(rig, "v-stale", s, 9.0)
    _verdict_file(rig, "v-fresh", s, 0.1, state="release_not_green")
    (rig.gh_dir / "run-id").write_text("4242\n", encoding="utf-8")
    url = "https://github.com/o/r/actions/runs/4242/job/9"
    (rig.gh_dir / "on-rerun").write_text(
        f"cat > \"$GH_STUB_DIR/{s}\" <<'ROWS'\n"
        f"lint\tcompleted\tsuccess\t{url}\t{url}\t4242\n"
        f"test-linux (2)\tcompleted\tfailure\t{url}\t{url}\t4242\n"
        "ROWS\n",
        encoding="utf-8",
    )
    out = _release(
        rig, "--sha", s, *FAST, LGM_EXACT_HOOK=_FLIP_HOOK, DEPLOY_STUB_VERIFY="1"
    )
    assert out.returncode == 2, out.stdout + out.stderr
    assert "went red" in out.stderr
    assert "test-linux (2)" in out.stderr
    assert rig.deploy_log() == []


def test_stale_verdict_without_a_runnable_workflow_keeps_the_refusal(
    rig: Rig,
) -> None:
    s = rig.shas["c3"]
    _verdict_file(rig, "v-stale", s, 9.0)  # no run-id file -> no run to rerun
    out = _release(rig, "--sha", s, *FAST, LGM_EXACT_HOOK=_FLIP_HOOK)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "deploy failed" in out.stderr
    assert "old" in out.stderr  # the round deploy age refusal survives verbatim
    assert not (rig.gh_dir / "reran").exists()
    assert rig.deploy_log() == []


def _prod_precis_stub(rig: Rig, body: str) -> None:
    """Plant a fake `scripts/prod-precis` in the rig checkout (the release
    script resolves the wrapper under PRECIS_ROUND_ROOT, not its own dir)."""
    p = rig.root / "scripts" / "prod-precis"
    p.write_text(body, encoding="utf-8")
    p.chmod(0o755)


def test_deploy_warns_pending_post_migration_steps(rig: Rig) -> None:
    """gr514955: after the deploy's migrate play, release names whatever
    post-migration steps prod still has pending."""
    _prod_precis_stub(
        rig,
        "#!/usr/bin/env bash\n"
        "echo 'post-migration check: 1 pending post-migration step(s):'\n"
        "echo '  - chunk_embeddings_ref_kind_filled on chunk_embeddings "
        "(from precis/0226_chunk_embeddings_kind_ann) — run: "
        "precis backfill-embedding-kinds --apply'\n",
    )
    out = _release(rig, *FAST, **_green(rig))
    assert out.returncode == 3, out.stdout + out.stderr
    assert "post-migration check: 1 pending" in out.stdout
    assert "precis backfill-embedding-kinds --apply" in out.stdout


def test_deploy_warn_survives_an_unreadable_probe(rig: Rig) -> None:
    """The probe is best-effort: a failing prod-precis warns, never fails."""
    _prod_precis_stub(rig, "#!/usr/bin/env bash\nexit 1\n")
    out = _release(rig, *FAST, **_green(rig))
    assert out.returncode == 3, out.stdout + out.stderr
    assert "post-migration check unreadable (rc 1)" in out.stdout


def test_deploy_warn_skips_without_the_wrapper(rig: Rig) -> None:
    """No prod-precis in the checkout -> skip note, deploy unaffected."""
    out = _release(rig, *FAST, **_green(rig))
    assert out.returncode == 3, out.stdout + out.stderr
    assert "post-migration check skipped" in out.stdout


@pytest.mark.parametrize("dry_run", [True, False])
def test_record_prod_head_mode_honours_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    """td674307: `--record-prod-head` alone writes prod's record and exits;
    with `--dry-run` it only says what it would write."""
    mod = _load_release(tmp_path, monkeypatch)
    head = "1" * 40
    written: list[str] = []
    said: list[str] = []
    monkeypatch.setattr(mod, "warn_hook_drift", lambda: None)
    monkeypatch.setattr(mod, "rev", lambda name: head)
    monkeypatch.setattr(mod, "say", said.append)

    def record(prev: str, h: str, **kw: object) -> str:
        written.append(h)
        return f"prod head recorded: {h[:9]}"

    monkeypatch.setattr(mod, "record_prod_head", record)
    args = mod.argparse.Namespace(
        deploy_only="",
        sha="",
        adopt=False,
        dry_run=dry_run,
        cut_only=False,
        record_prod_head=head,
    )
    assert mod.run(args) == 0
    assert written == ([] if dry_run else [head])
    assert said[-1].startswith(
        "dry-run: would record" if dry_run else "prod head recorded"
    )

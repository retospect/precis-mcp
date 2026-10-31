"""`scripts/release` — cut at S, wait for CI, deploy, over the real `scripts/round`.

Same rig as the round tests: a throwaway repo with a bare `origin`, fake CI
(`LGM_EXACT`, canned gh check-runs) and a fake `scripts/deploy`. Polls are
sub-second via flags.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests._script import load_script
from tests.test_round_gate import (
    REPO_ROOT,
    Rig,
    _commit,
    _git,
    _live,
    _push_branch,
    _rel,
    _round_json,
)
from tests.test_round_gate import rig as rig
from tests.test_round_release import _verdict

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
    _push_branch(rig, "release/r7", rig.shas["c3"])
    out = _release(rig, "--sha", rig.shas["c2"], *FAST)
    assert out.returncode == 0, out.stderr
    assert (
        f"riding release release/r7 at {rig.shas['c3'][:9]}: it ships "
        f"{rig.shas['c2'][:9]}" in out.stdout
    )
    assert rig.deploy_log() == []


def test_release_not_containing_s_rides_the_next_cut(rig: Rig) -> None:
    """No queueing (release-train §5): an in-flight release that lacks S
    reports and exits 0 — the driver cuts N+1 at deploy start."""
    _push_branch(rig, "release/r7", rig.shas["c0"])
    out = _release(rig, *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"{rig.shas['c3'][:9]} rides the next cut" in out.stdout
    assert "queued" not in out.stdout
    assert rig.deploy_log() == []


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
    mod = load_script("release_script", copy)
    monkeypatch.setattr(mod, "marker_path", lambda: tmp_path / ".release-owner")
    return mod


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


def test_stale_round_number_cut_refusal_exits_one_and_clears_marker(rig: Rig) -> None:
    assert rig.round("open").returncode == 0
    _git(rig.root, "push", "-q", "origin", f"{rig.shas['c0']}:refs/tags/deployed/r1")
    out = _release(rig, *FAST)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "deployed/r1 is already tagged on origin" in out.stderr
    assert not _marker(rig).exists()
    assert rig.origin_ref("release/r1") == ""
    assert not _live(_round_json(rig))


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

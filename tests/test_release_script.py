"""`scripts/release` — cut at S, wait for CI, deploy, over the real `scripts/round`.

Same rig as the round tests: a throwaway repo with a bare `origin`, fake CI
(`LGM_EXACT`, canned gh check-runs) and a fake `scripts/deploy`. Polls are
sub-second via flags.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pytest

from tests.test_round_gate import (
    REPO_ROOT,
    Rig,
    _commit,
    _git,
    _push_branch,
    _round_json,
)
from tests.test_round_gate import rig as rig
from tests.test_round_release import _verdict

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: execs shebang'd scripts"
)

RELEASE = REPO_ROOT / "scripts" / "release"
FAST = ("--poll", "0.01", "--ci-timeout", "0.02", "--queue-timeout", "0.02")


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


def test_queue_timeout_exits_four(rig: Rig) -> None:
    _push_branch(rig, "release/r7", rig.shas["c0"])
    out = _release(rig, *FAST)
    assert out.returncode == 4
    assert out.stdout.count("queued: release/r7 in flight") == 1
    assert f"{rig.shas['c3'][:9]} is on main" in out.stderr
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
    assert _round_json(rig)["release"]["deployment"]["sha"] == s


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
    assert "gh run view 4242 --log-failed" in out.stderr
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
    assert _round_json(rig)["release"]["base"] == before["release"]["base"] == s
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
    assert state["open"] and state["deployed"] and "release" not in state
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
    rel = _round_json(rig)["release"]
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
    assert _round_json(rig)["release"]["cut_pending"] is True
    assert _marker(rig).exists()
    assert rig.origin_ref("release/r1") == ""
    # a sibling must neither ride the unpushed record nor deploy: it queues
    saved = _marker(rig).read_text(encoding="utf-8")
    _marker(rig).unlink()
    sib = _release(rig, "--sha", c2, *FAST)
    assert sib.returncode == 4, sib.stdout + sib.stderr
    assert "riding" not in sib.stdout
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


def _load_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    copy = tmp_path / "release_script.py"
    copy.write_text(RELEASE.read_text(encoding="utf-8"), encoding="utf-8")
    loader = SourceFileLoader("release_script", str(copy))
    spec = importlib.util.spec_from_loader("release_script", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    monkeypatch.setattr(mod, "marker_path", lambda: tmp_path / ".release-owner")
    return mod


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
    monkeypatch.setattr(mod, "round_state", lambda: {"n": 1, "release": rel})
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
    assert "release" not in _round_json(rig)

"""Frozen release gate/deploy: real bare origin, fake CI and deploy only."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests._script import load_script

if sys.platform != "win32":
    import fcntl

from tests.test_round_gate import (
    REPO_ROOT,
    Rig,
    _child,
    _commit,
    _fresh,
    _git,
    _live,
    _open_round,
    _push_branch,
    _rel,
    _rels,
    _round_json,
)
from tests.test_round_gate import (
    rig as rig,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX release tooling")


def _cut(rig: Rig) -> str:
    _open_round(rig)
    out = rig.round("cut", **_fresh(rig))
    assert out.returncode == 0, out.stderr
    return rig.shas["c2"]


def _verdict(sha: str, age: float | None = 1.0, state: str = "found") -> dict[str, str]:
    return {
        "LGM_EXACT": json.dumps(
            {
                "candidate": sha if state == "found" else "",
                "candidate_state": state,
                "age_hours": age,
                "certificate": {
                    "sha": sha,
                    "workflow": ".github/workflows/check.yml",
                    "run_id": 42,
                    "attempt": 1,
                },
            }
        )
    }


def _deploy(rig: Rig, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    out = rig.round("deploy", *args, **extra)
    if out.returncode == 3 and extra.get("DEPLOY_STUB_VERIFY") == "1":
        sha = _rel(_round_json(rig))["deployment"]["sha"]
        return rig.round(
            "deploy",
            "--confirm-runtime",
            sha,
            "--runtime-evidence",
            "fake runtime: all required daemons and session MCP ready on pinned SHA",
            **extra,
        )
    return out


def _tag(rig: Rig) -> str:
    return _git(rig.origin, "rev-parse", "refs/tags/deployed/r1")


def _hook(rig: Rig, body: str) -> Path:
    path = rig.origin / "hooks" / "pre-receive"
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _load_round(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    script = Path(__file__).resolve().parents[1] / "scripts/round"
    copy = rig.root / "round_script.py"
    copy.write_text(script.read_text(encoding="utf-8"), encoding="utf-8")
    (rig.root / "lib").mkdir(exist_ok=True)
    for name in ("round_state.py", "git_network.py", "soak_verdict.py"):
        (rig.root / "lib" / name).write_text(
            (script.parent / "lib" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    mod = load_script("round_script", copy)
    monkeypatch.chdir(rig.root)
    for key, val in rig.env.items():
        monkeypatch.setenv(key, val)
    return mod


def test_completed_round_retry_never_deploys_newer_green_main(rig: Rig) -> None:
    head = _cut(rig)
    assert _deploy(rig, DEPLOY_STUB_VERIFY="1", **_verdict(head)).returncode == 0
    before = {name: rig.origin_ref(name) for name in ("main", "gated", "prod")}
    out = rig.round("deploy", **_fresh(rig, "c3"))
    assert out.returncode == 0
    assert "already completed" in out.stdout
    assert rig.deploy_log() == [f"{head} --pinned"]
    assert {name: rig.origin_ref(name) for name in before} == before


def _converged_without_prod(rig: Rig) -> str:
    head = _cut(rig)
    hook = _hook(
        rig,
        "while read old new ref; do\n"
        '  if [ "$ref" = refs/heads/prod ]; then\n'
        '    echo "remote transport unavailable after convergence" >&2\n'
        "    exit 1\n"
        "  fi\n"
        "done\n",
    )
    out = rig.round("deploy", DEPLOY_STUB_VERIFY="1", **_verdict(head))
    assert out.returncode == 1
    assert "transport unavailable" in out.stderr
    assert "scripts/round deploy --finish-pointer" in out.stderr
    assert "deploy returned zero" in out.stderr
    assert _rel(_round_json(rig))["state"] == "deploying"
    assert not _rel(_round_json(rig))["deployment"].get("rollout_at")
    hook.unlink()
    return head


def test_finish_pointer_recovers_converged_deploy_without_rollout(rig: Rig) -> None:
    head = _converged_without_prod(rig)
    out = rig.round("deploy", "--finish-pointer")
    assert out.returncode == 3, out.stderr
    assert rig.origin_ref("prod") == head
    assert rig.deploy_log() == [f"{head} --pinned"]
    rel = _rel(_round_json(rig))
    assert rel["state"] == "deployed"
    assert rel["deployment"]["rollout_at"]
    assert not rel["deployment"].get("runtime_confirmed_at")
    out = _deploy(
        rig,
        "--finish-pointer",
        "--confirm-runtime",
        head,
        "--runtime-evidence",
        "fixture daemon boot SHA and readiness observed",
    )
    assert out.returncode == 0, out.stderr
    assert _rel(_round_json(rig), 1)["state"] == "confirmed"
    assert rig.deploy_log() == [f"{head} --pinned"]


@pytest.mark.parametrize("invalid", ["missing", "wrong-sha", "stale", "in-progress"])
def test_finish_pointer_requires_current_convergence_receipt(
    rig: Rig, invalid: str
) -> None:
    head = _converged_without_prod(rig)
    common = rig.root / ".git"
    marker = common / "precis-deploy-state"
    if invalid == "missing":
        marker.unlink()
    elif invalid == "wrong-sha":
        marker.write_text(
            f"{rig.shas['c0']} {int(time.time())} success\n", encoding="utf-8"
        )
    elif invalid == "stale":
        marker.write_text(f"{head} 1 success\n", encoding="utf-8")
    else:
        (common / "precis-deploy-attempt").write_text(head, encoding="utf-8")
    before = _round_json(rig)
    out = rig.round("deploy", "--finish-pointer")
    assert out.returncode == 1
    assert rig.origin_ref("prod") != head
    assert _round_json(rig) == before
    assert rig.deploy_log() == [f"{head} --pinned"]


def test_finish_pointer_never_starts_an_unstarted_release(rig: Rig) -> None:
    _cut(rig)
    before = _round_json(rig)
    out = rig.round("deploy", "--finish-pointer")
    assert out.returncode == 1
    assert _round_json(rig) == before
    assert rig.deploy_log() == []


@pytest.mark.parametrize("already_published", [False, True])
def test_finish_pointer_syncs_served_clone_before_recording_deployed(
    rig: Rig, already_published: bool
) -> None:
    head = _converged_without_prod(rig)
    clone = rig.root.parent / "served"
    _git(rig.root.parent, "clone", "-q", str(rig.origin), str(clone))
    _git(clone, "checkout", "-q", "--detach", rig.shas["c0"])
    if already_published:
        _push_branch(rig, "prod", head)
    out = rig.round("deploy", "--finish-pointer", PRECIS_PROD_CHECKOUT=str(clone))
    assert out.returncode == 3, out.stderr
    assert _git(clone, "rev-parse", "HEAD") == head
    assert _rel(_round_json(rig))["state"] == "deployed"
    assert rig.deploy_log() == [f"{head} --pinned"]


@pytest.mark.parametrize("already_published", [False, True])
def test_finish_pointer_refuses_stale_clone_and_retries_without_rollout(
    rig: Rig, already_published: bool
) -> None:
    head = _converged_without_prod(rig)
    clone = rig.root.parent / "served"
    _git(rig.root.parent, "clone", "-q", str(rig.origin), str(clone))
    old = rig.shas["c0"]
    _git(clone, "checkout", "-q", "--detach", old)
    # The target adds README.md: preserve this untracked local edit and refuse.
    (clone / "README.md").write_text("local clone edit\n", encoding="utf-8")
    if already_published:
        _push_branch(rig, "prod", head)
    before = _round_json(rig)
    out = rig.round("deploy", "--finish-pointer", PRECIS_PROD_CHECKOUT=str(clone))
    assert out.returncode == 1
    assert "session MCP keeps serving the old sha" in out.stderr
    assert old in out.stderr and head in out.stderr
    assert "scripts/round deploy --finish-pointer" in out.stderr
    assert _git(clone, "rev-parse", "HEAD") == old
    assert _round_json(rig) == before
    assert rig.origin_ref("prod") == head
    assert rig.deploy_log() == [f"{head} --pinned"]
    (clone / "README.md").unlink()
    retry = rig.round("deploy", "--finish-pointer", PRECIS_PROD_CHECKOUT=str(clone))
    assert retry.returncode == 3, retry.stderr
    assert _git(clone, "rev-parse", "HEAD") == head
    assert _rel(_round_json(rig))["state"] == "deployed"
    assert rig.deploy_log() == [f"{head} --pinned"]


def test_history_ci_lookup_keeps_time_for_multiple_gh_calls(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_round(rig, monkeypatch)

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == 900
        return subprocess.CompletedProcess(command, 0, rig.shas["c2"], "")

    monkeypatch.setitem(mod._NETWORK, "run_command", run)
    assert mod._lgm_run(rig.root) == (rig.shas["c2"], 0)


def test_round_receipt_survives_close_and_open_immutably(rig: Rig) -> None:
    head = _cut(rig)
    assert _deploy(rig, DEPLOY_STUB_VERIFY="1", **_verdict(head)).returncode == 0
    path = rig.root / ".git/precis-round/receipts/r1.json"
    before = path.read_bytes()
    receipt = json.loads(before)
    assert receipt["deployed"]["ci"]["sha"] == head
    assert receipt["deployed"]["runtime_evidence"]
    assert receipt["deployed"]["runtime_receipt"]
    assert rig.round("close").returncode == 0
    assert rig.round("open").returncode == 0
    assert _round_json(rig)["n"] == 2
    assert path.read_bytes() == before


def test_conflicting_archived_receipt_cannot_be_overwritten_on_open(rig: Rig) -> None:
    head = _cut(rig)
    assert _deploy(rig, DEPLOY_STUB_VERIFY="1", **_verdict(head)).returncode == 0
    assert rig.round("close").returncode == 0
    path = rig.root / ".git/precis-round/receipts/r1.json"
    path.write_text('{"conflicting":true}', encoding="utf-8")
    previous = _round_json(rig)
    out = rig.round("open")
    assert out.returncode == 1
    assert "immutable receipt" in out.stderr
    assert path.read_text(encoding="utf-8") == '{"conflicting":true}'
    assert _round_json(rig) == previous


@pytest.mark.parametrize(
    "content",
    [
        "{broken",
        "[]",
        "null",
        "{}",
        '{"n":1,"open":"yes"}',
        '{"n":1,"open":true,"release":{}}',
        '{"n":1,"open":false,"deployed":{}}',
        '{"n":1,"open":true,"release":{"branch":"release/r1","base":"'
        + "a" * 40
        + '","deployment":{}}}',
    ],
)
@pytest.mark.parametrize("command", ["deploy", "open"])
def test_invalid_present_state_refuses_without_main_fallback(
    rig: Rig, content: str, command: str
) -> None:
    path = rig.root / ".git/precis-round/round.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    out = rig.round(command, **_fresh(rig))
    assert out.returncode == 1
    assert "invalid round state" in out.stderr
    assert path.read_text(encoding="utf-8") == content
    assert rig.deploy_log() == []
    assert rig.origin_ref("gated") == ""


@pytest.mark.parametrize("mode", ["publication", "journal"])
@pytest.mark.parametrize(
    "key", ["release", "deployed", "deployment", "dangling", "malformed"]
)
def test_shell_readers_refuse_invalid_lifecycle_records(
    rig: Rig, mode: str, key: str
) -> None:
    path = rig.root / ".git/precis-round/round.json"
    path.parent.mkdir(parents=True)
    data = {"n": 1, "open": True, key: {}}
    if key == "deployment":
        data = {
            "n": 1,
            "open": True,
            "release": {
                "branch": "release/r1",
                "base": rig.shas["c2"],
                "deployment": {},
            },
        }
    if key == "dangling":
        path.symlink_to(path.parent / "missing-state")
    else:
        path.write_text(
            "{broken" if key == "malformed" else json.dumps(data), encoding="utf-8"
        )
    lib = Path(__file__).resolve().parents[1] / "scripts/lib/round-lock.sh"
    function = (
        "acquire_main_gated_publication"
        if mode == "publication"
        else "release_journal_clear"
    )
    target = "gated" if mode == "publication" else "release/r1"
    before = rig.origin_ref(target)
    out = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{lib}"; {function} && git push -q origin "{rig.shas["c3"]}:refs/heads/{target}"',
        ],
        cwd=rig.root,
        env=rig.env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert out.returncode == 1
    assert rig.origin_ref(target) == before
    if key == "dangling":
        assert path.is_symlink() and not path.exists()
    elif key == "malformed":
        assert path.read_text(encoding="utf-8") == "{broken"
    else:
        assert json.loads(path.read_text(encoding="utf-8")) == data


@pytest.mark.parametrize("command", ["deploy", "open"])
def test_read_failed_state_refuses_without_overwrite(rig: Rig, command: str) -> None:
    path = rig.root / ".git/precis-round/round.json"
    path.mkdir(parents=True)
    out = rig.round(command, **_fresh(rig))
    assert out.returncode == 1
    assert "unreadable/invalid round state" in out.stderr
    assert path.is_dir()
    assert rig.deploy_log() == []


def test_dangling_state_link_refuses_main_fallback(rig: Rig) -> None:
    path = rig.root / ".git/precis-round/round.json"
    path.parent.mkdir(parents=True)
    path.symlink_to(path.parent / "missing-receipt")
    out = rig.round("deploy", **_fresh(rig))
    assert out.returncode == 1
    assert "dangling state link" in out.stderr
    assert path.is_symlink()
    assert rig.deploy_log() == []


@pytest.mark.parametrize("same_content", [False, True])
def test_new_same_sha_receipt_requires_fresh_runtime_observation_without_reinstall(
    rig: Rig, same_content: bool
) -> None:
    head = _cut(rig)
    hook = _hook(
        rig,
        'while read old new ref; do case "$ref" in refs/tags/*) exit 1;; esac; done\n',
    )
    assert _deploy(rig, DEPLOY_STUB_VERIFY="1", **_verdict(head)).returncode == 1
    old = _rel(_round_json(rig))["deployment"]["runtime_receipt"]
    marker = rig.root / ".git/precis-deploy-state"
    # Even second-resolution deploy timestamps can repeat: file generation matters.
    marker.write_text(
        old["content"] if same_content else f"{head} {int(time.time()) + 1} success\n",
        encoding="utf-8",
    )
    hook.unlink()
    out = rig.round("deploy")
    assert out.returncode == 3
    assert "runtime verification pending" in out.stdout
    journal = _rel(_round_json(rig))["deployment"]
    assert "runtime_confirmed_at" not in journal
    assert journal["superseded_runtime"][0]["runtime_receipt"] == old
    assert rig.origin_ref("release/r1") == head
    assert _git(rig.origin, "tag", "--list", "deployed/r1") == ""
    out = rig.round(
        "deploy",
        "--confirm-runtime",
        head,
        "--runtime-evidence",
        "new fake service observation after receipt replacement",
    )
    assert out.returncode == 0, out.stderr
    assert _round_json(rig)["deployed"]["runtime_receipt"] != old
    assert rig.deploy_log() == [f"{head} --pinned"]


@pytest.mark.parametrize("phase", ["tag", "main", "retire"])
def test_uncertain_success_acknowledgement_resumes_without_reinstall(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    base = _cut(rig)
    head = _child(rig, base, "unforwarded release")
    _push_branch(rig, "release/r1", head)
    assert rig.round("deploy", DEPLOY_STUB_VERIFY="1", **_verdict(head)).returncode == 3
    mod = _load_round(rig, monkeypatch)
    real_git = mod._rgit
    lost_ack = False

    def uncertain(root: Path, *args: str) -> str | None:
        nonlocal lost_ack
        result = real_git(root, *args)
        target = {
            "tag": f"{head}:refs/tags/deployed/r1",
            "main": "refs/heads/main",
            "retire": ":refs/heads/release/r1",
        }[phase]
        if not lost_ack and args[0] == "push" and any(a.endswith(target) for a in args):
            assert (
                result is not None
            )  # remote succeeded; client lost its acknowledgement
            lost_ack = True
            return None
        return result

    monkeypatch.setattr(mod, "_rgit", uncertain)
    args = mod.argparse.Namespace(
        dry_run=False,
        confirm_runtime=head,
        runtime_evidence="observed fake required-service boot SHA/readiness and session MCP",
    )
    with mod._lifecycle(rig.root) as fd:
        rc = mod.cmd_deploy(args, fd)
    assert lost_ack
    if phase != "main":
        assert rc == 1
    args.confirm_runtime, args.runtime_evidence = "", ""
    if rc != 0:
        with mod._lifecycle(rig.root) as fd:
            assert mod.cmd_deploy(args, fd) == 0
    assert rig.origin_ref("prod") == _tag(rig) == head
    assert rig.origin_ref("release/r1") == ""
    assert rig.deploy_log() == [f"{head} --pinned"]


def _lost_retirement_ack(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> str:
    head = _cut(rig)
    assert rig.round("deploy", DEPLOY_STUB_VERIFY="1", **_verdict(head)).returncode == 3
    mod = _load_round(rig, monkeypatch)
    real_git = mod._rgit

    def lost(root: Path, *args: str) -> str | None:
        out = real_git(root, *args)
        if args[0] == "push" and args[-1] == ":refs/heads/release/r1":
            assert out is not None
            return None
        return out

    monkeypatch.setattr(mod, "_rgit", lost)
    args = mod.argparse.Namespace(
        dry_run=False,
        confirm_runtime=head,
        runtime_evidence="first fake runtime observation",
    )
    with mod._lifecycle(rig.root) as fd:
        assert mod.cmd_deploy(args, fd) == 1
    assert rig.origin_ref("release/r1") == ""
    assert _rel(_round_json(rig))["deployment"]["runtime_confirmed_at"]
    return head


def _interrupt_final_state_write(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> str:
    head = _cut(rig)
    assert rig.round("deploy", DEPLOY_STUB_VERIFY="1", **_verdict(head)).returncode == 3
    mod = _load_round(rig, monkeypatch)
    real_write = mod._write

    def interrupt(path: Path, data: dict) -> None:
        if "deployed" in data and "release" not in data:
            raise OSError("interrupted final state persistence after archive")
        real_write(path, data)

    monkeypatch.setattr(mod, "_write", interrupt)
    args = mod.argparse.Namespace(
        dry_run=False,
        confirm_runtime=head,
        runtime_evidence="original fake runtime observation before archive",
    )
    with mod._lifecycle(rig.root) as fd, pytest.raises(OSError, match="after archive"):
        mod.cmd_deploy(args, fd)
    assert rig.origin_ref("release/r1") == ""
    assert _tag(rig) == head
    assert _live(_round_json(rig))
    assert (rig.root / ".git/precis-round/receipts/r1.json").is_file()
    return head


@pytest.mark.parametrize("same_content", [False, True])
@pytest.mark.parametrize("fresh_observation", [False, True])
def test_published_archive_is_terminal_after_interrupted_state_write_and_receipt_repair(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    same_content: bool,
    fresh_observation: bool,
) -> None:
    head = _interrupt_final_state_write(rig, monkeypatch)
    archive = rig.root / ".git/precis-round/receipts/r1.json"
    original_bytes = archive.read_bytes()
    original = json.loads(original_bytes)
    state_before = _round_json(rig)
    marker = rig.root / ".git/precis-deploy-state"
    marker.write_text(
        marker.read_text(encoding="utf-8")
        if same_content
        else f"{head} {int(time.time()) + 1} success\n",
        encoding="utf-8",
    )
    refs_before = {
        name: rig.origin_ref(name) for name in ("main", "gated", "prod", "release/r1")
    }
    assert rig.round("deploy", "--dry-run").returncode == 0
    assert _round_json(rig) == state_before
    flags = (
        (
            "--confirm-runtime",
            head,
            "--runtime-evidence",
            "new repair observation must not replace archived evidence",
        )
        if fresh_observation
        else ()
    )
    out = rig.round("deploy", *flags)
    assert out.returncode == 0, out.stderr
    assert "completion already archived" in out.stdout
    assert not _live(_round_json(rig))
    assert _round_json(rig)["deployed"] == original["deployed"]
    assert archive.read_bytes() == original_bytes
    assert {name: rig.origin_ref(name) for name in refs_before} == refs_before
    assert rig.deploy_log() == [f"{head} --pinned"]
    assert rig.round("deploy", **_fresh(rig, "c3")).returncode == 0
    assert rig.deploy_log() == [f"{head} --pinned"]


@pytest.mark.parametrize("damage", ["proof", "round", "malformed", "dangling"])
def test_archive_reconciliation_refuses_conflict_or_unreadable_receipt(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    head = _interrupt_final_state_write(rig, monkeypatch)
    archive = rig.root / ".git/precis-round/receipts/r1.json"
    if damage == "dangling":
        archive.unlink()
        archive.symlink_to(archive.parent / "missing-receipt")
    elif damage == "malformed":
        archive.write_text("{broken", encoding="utf-8")
    else:
        data = json.loads(archive.read_bytes())
        if damage == "proof":
            data["deployed"]["runtime_evidence"] = "conflicting runtime evidence"
        else:
            data["n"] = 2
        archive.write_text(json.dumps(data), encoding="utf-8")
    state_before = _round_json(rig)
    marker = rig.root / ".git/precis-deploy-state"
    marker.write_text(marker.read_text(encoding="utf-8"), encoding="utf-8")
    out = rig.round("deploy")
    assert out.returncode == 1
    assert "immutable completion archive" in out.stderr
    assert _round_json(rig) == state_before
    assert rig.deploy_log() == [f"{head} --pinned"]


@pytest.mark.parametrize("same_content", [True, False])
def test_receipt_refresh_after_lost_retirement_ack_completes_without_reinstall(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, same_content: bool
) -> None:
    head = _lost_retirement_ack(rig, monkeypatch)
    old = _rel(_round_json(rig))["deployment"]["runtime_receipt"]
    marker = rig.root / ".git/precis-deploy-state"
    marker.write_text(
        old["content"] if same_content else f"{head} {int(time.time()) + 1} success\n",
        encoding="utf-8",
    )
    assert rig.round("deploy").returncode == 3
    out = rig.round(
        "deploy",
        "--confirm-runtime",
        head,
        "--runtime-evidence",
        "fresh fake service observation after repair and remote retirement",
    )
    assert out.returncode == 0, out.stderr
    assert _round_json(rig)["deployed"]["runtime_receipt"] != old
    assert (
        _round_json(rig)["deployed"]["superseded_runtime"][0]["runtime_receipt"] == old
    )
    assert rig.deploy_log() == [f"{head} --pinned"]
    assert rig.origin_ref("release/r1") == ""


@pytest.mark.parametrize(
    "missing", ["tag", "conflicting-tag", "main", "prod", "prior-proof"]
)
def test_receipt_refresh_after_remote_delete_requires_all_recovery_evidence(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    head = _lost_retirement_ack(rig, monkeypatch)
    marker = rig.root / ".git/precis-deploy-state"
    marker.write_text(marker.read_text(encoding="utf-8"), encoding="utf-8")
    assert rig.round("deploy").returncode == 3
    if missing == "tag":
        _git(rig.origin, "update-ref", "-d", "refs/tags/deployed/r1")
    elif missing == "conflicting-tag":
        _git(rig.origin, "update-ref", "refs/tags/deployed/r1", rig.shas["c0"])
    elif missing in ("main", "prod"):
        _git(rig.origin, "update-ref", f"refs/heads/{missing}", rig.shas["c0"])
    else:
        path = rig.root / ".git/precis-round/round.json"
        cur = _round_json(rig)
        _rel(cur)["deployment"]["superseded_runtime"] = []
        path.write_text(json.dumps(cur), encoding="utf-8")
    out = rig.round(
        "deploy",
        "--confirm-runtime",
        head,
        "--runtime-evidence",
        "fake fresh observation",
    )
    assert out.returncode == 1
    assert _live(_round_json(rig))
    assert "runtime_confirmed_at" not in _rel(_round_json(rig))["deployment"]
    assert rig.deploy_log() == [f"{head} --pinned"]


def test_final_forward_merge_cas_retries_main_arrival_without_losing_it(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _cut(rig)
    head = _child(rig, base, "release awaiting forward merge")
    _push_branch(rig, "release/r1", head)
    assert rig.round("deploy", DEPLOY_STUB_VERIFY="1", **_verdict(head)).returncode == 3
    arrival = _child(rig, rig.shas["c3"], "main CAS racer")
    mod = _load_round(rig, monkeypatch)
    real_git, raced = mod._rgit, False

    def race(root: Path, *args: str) -> str | None:
        nonlocal raced
        if not raced and args[0] == "push" and args[-1].endswith(":refs/heads/main"):
            assert (
                real_git(root, "push", "-q", "origin", f"{arrival}:refs/heads/main")
                is not None
            )
            raced = True
        return real_git(root, *args)

    monkeypatch.setattr(mod, "_rgit", race)
    args = mod.argparse.Namespace(
        dry_run=False,
        confirm_runtime=head,
        runtime_evidence="fake required daemons/session MCP observed ready at SHA",
    )
    with mod._lifecycle(rig.root) as fd:
        assert mod.cmd_deploy(args, fd) == 0
    assert raced
    assert _git(
        rig.root, "show", "-s", "--format=%P", rig.origin_ref("main")
    ).split() == [arrival, head]
    assert rig.origin_ref("prod") == head
    assert rig.deploy_log() == [f"{head} --pinned"]


def test_release_gate_ignores_newer_green_main_and_selects_exact_head(rig: Rig) -> None:
    base = _cut(rig)
    fix = _child(rig, base, "frozen fix")
    _push_branch(rig, "release/r1", fix)
    out = rig.round("gate", "--json", **_fresh(rig, "c3"), **_verdict(fix))
    assert out.returncode == 0, out.stderr
    facts = json.loads(out.stdout)
    assert facts["candidate"] == facts["release_head"] == fix
    assert facts["release_branch"] == "release/r1"
    assert facts["age_hours"] == 1
    assert not facts["candidate_in_main"]  # deploy forwards it at the end
    status = rig.round("status")
    assert f"release release/r1 at {fix[:9]}" in status.stdout


def test_release_gate_json_on_a_not_green_head_is_facts_not_errors(
    rig: Rig,
) -> None:
    """gr573219: an empty candidate must not reach merge-base, and --json on
    a not-yet-green head is a fact dump (exit 0), not a refusal."""
    head = _cut(rig)
    _push_branch(rig, "gated", rig.shas["c0"])
    _push_branch(rig, "prod", rig.shas["c0"])
    out = rig.round("gate", "--json", **_verdict(head, state="release_not_green"))
    assert out.returncode == 0, out.stderr
    assert "merge-base" not in out.stderr
    facts = json.loads(out.stdout)
    assert facts["candidate"] == ""
    assert facts["candidate_state"] == "release_not_green"
    out = rig.round("gate", **_verdict(head, state="release_not_green"))
    assert out.returncode == 1
    assert "no full green CI verdict" in out.stdout


@pytest.mark.parametrize("state", ["release_not_green", "unreadable"])
def test_release_never_falls_back_to_green_main(rig: Rig, state: str) -> None:
    head = _cut(rig)
    out = _deploy(rig, **_fresh(rig, "c3"), **_verdict(head, state=state))
    assert out.returncode == 1
    assert rig.deploy_log() == []
    assert rig.origin_ref("gated") == ""


@pytest.mark.parametrize("age", [None, 7.0, float("nan"), -1.0])
def test_release_deploy_requires_fresh_known_verdict_age(
    rig: Rig, age: float | None
) -> None:
    head = _cut(rig)
    out = _deploy(rig, **_verdict(head, age))
    assert out.returncode == 1
    assert rig.deploy_log() == []


def test_missing_release_and_unrecorded_release_do_not_deploy_main(rig: Rig) -> None:
    head = _cut(rig)
    _git(rig.root, "push", "-q", "origin", "--delete", "release/r1")
    assert _deploy(rig, **_fresh(rig, "c3")).returncode == 1
    state = rig.root / ".git/precis-round/round.json"
    cur = _round_json(rig)
    cur["releases"] = []
    state.write_text(json.dumps(cur), encoding="utf-8")
    _push_branch(rig, "release/r1", head)
    out = _deploy(rig, **_fresh(rig, "c3"))
    assert "unrecorded release" in out.stderr
    assert rig.deploy_log() == []


def test_release_deploy_ff_checks_prod_before_gated_move(rig: Rig) -> None:
    head = _cut(rig)
    side = _child(rig, rig.shas["c0"], "prod side")
    _push_branch(rig, "prod", side)
    out = _deploy(rig, **_verdict(head))
    assert out.returncode == 1
    assert "prod moves fast-forward only" in out.stderr
    assert rig.origin_ref("gated") == ""
    assert rig.deploy_log() == []


def test_release_deploy_second_parent_tag_merge_and_retirement(rig: Rig) -> None:
    base = _cut(rig)
    fix = _child(rig, base, "frozen fix")
    _push_branch(rig, "release/r1", fix)
    _push_branch(rig, "gated", base)
    _push_branch(rig, "prod", base)
    out = _deploy(rig, DEPLOY_STUB_VERIFY="1", **_fresh(rig, "c3"), **_verdict(fix))
    assert out.returncode == 0, out.stderr
    main = rig.origin_ref("main")
    assert _git(rig.root, "show", "-s", "--format=%P", main).split() == [
        rig.shas["c3"],
        fix,
    ]
    assert rig.origin_ref("gated") == rig.origin_ref("prod") == _tag(rig) == fix
    assert rig.origin_ref("release/r1") == ""
    assert not _live(_round_json(rig))
    assert _round_json(rig)["deployed"]["sha"] == fix
    assert rig.deploy_log() == [f"{fix} --pinned"]
    assert _git(rig.root, "rev-list", "--count", f"{fix}..{main}") == "2"
    fleet = Path(__file__).resolve().parents[1] / "scripts/fleet"
    refs = subprocess.run(
        ["bash", str(fleet), "refs"],
        cwd=rig.root,
        env=rig.env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert refs.returncode == 0, refs.stderr
    assert any(
        line.startswith(f"prod {fix[:9]}") and line.endswith("2 behind main")
        for line in refs.stdout.splitlines()
    )
    status = rig.round("status", "--json")
    assert json.loads(status.stdout)["round"]["deployed"]["sha"] == fix
    assert (
        _git(rig.root, "diff", "--name-only", base, fix) == ""
    )  # same-tree fix fixture
    assert not rig.lock.exists()
    assert rig.round("close").returncode == 0


def test_release_deploy_dry_run_is_read_only(rig: Rig) -> None:
    head = _cut(rig)
    before = _round_json(rig)
    out = _deploy(rig, "--dry-run", **_verdict(head))
    assert out.returncode == 0, out.stderr
    assert "tag deployed/r1" in out.stdout
    assert rig.deploy_log() == []
    assert _round_json(rig) == before
    assert rig.origin_ref("gated") == ""


def test_release_zero_exit_without_health_or_prod_never_retires(rig: Rig) -> None:
    head = _cut(rig)
    out = _deploy(rig, **_verdict(head))
    assert out.returncode == 1
    assert "matching fresh success marker and origin/prod" in out.stderr
    assert rig.origin_ref("release/r1") == head
    assert not _rel(_round_json(rig))["deployment"].get("runtime_confirmed_at")
    assert rig.round("cut", "--abandon").returncode == 1
    assert rig.round("close").returncode == 1
    retry = _deploy(rig, DEPLOY_STUB_VERIFY="1", **_verdict(head))
    assert retry.returncode == 0, retry.stderr


def test_release_failed_deploy_retains_pin_and_returns_exit_code(rig: Rig) -> None:
    head = _cut(rig)
    out = _deploy(rig, DEPLOY_STUB_RC="7", **_verdict(head))
    assert out.returncode == 7
    assert _rel(_round_json(rig))["deployment"]["sha"] == head
    assert rig.origin_ref("release/r1") == head


def test_release_head_race_during_ci_lookup_stops_before_rollout(rig: Rig) -> None:
    head = _cut(rig)
    newer = _child(rig, head, "release arrival")
    out = _deploy(
        rig,
        LGM_EXACT_HOOK=f"git push -q origin {newer}:refs/heads/release/r1",
        **_verdict(head),
    )
    assert out.returncode == 1
    assert "moved during the CI lookup" in out.stderr
    assert rig.deploy_log() == []
    assert "deployment" not in _rel(_round_json(rig))


def test_failed_deploy_hook_cannot_be_falsely_runtime_confirmed(rig: Rig) -> None:
    head = _cut(rig)
    out = _deploy(
        rig,
        DEPLOY_STUB_VERIFY="1",
        DEPLOY_STUB_HOOK="printf 'HOOK_FAILURE_SENTINEL\\n'; exit 9",
        **_verdict(head),
    )
    assert out.returncode == 9, f"stdout={out.stdout!r} stderr={out.stderr!r}"
    assert "HOOK_FAILURE_SENTINEL" in out.stdout
    assert "release/r1 deployed" not in out.stdout
    assert rig.origin_ref("release/r1") == head
    assert rig.origin_ref("prod") == ""
    assert rig.origin_ref("main") == rig.shas["c3"]
    assert _git(rig.origin, "tag", "--list", "deployed/r1") == ""
    journal = _rel(_round_json(rig))["deployment"]
    assert journal["sha"] == head
    assert "rollout_at" not in journal
    assert "runtime_confirmed_at" not in journal
    assert len(rig.deploy_log()) == 1


def test_release_head_race_during_rollout_retains_pin_without_tag(rig: Rig) -> None:
    head = _cut(rig)
    newer = _child(rig, head, "external release arrival")
    out = rig.round(
        "deploy",
        DEPLOY_STUB_VERIFY="1",
        DEPLOY_STUB_HOOK=f"git push -q origin {newer}:refs/heads/release/r1",
        **_verdict(head),
    )
    assert rig.origin_ref("release/r1") == newer, (
        "race injection did not move isolated origin; "
        f"rc={out.returncode} stdout={out.stdout!r} stderr={out.stderr!r}"
    )
    assert out.returncode == 1
    assert "changed during rollout" in out.stderr
    assert _rel(_round_json(rig))["deployment"]["sha"] == head
    assert "rollout_at" not in _rel(_round_json(rig))["deployment"]
    assert rig.origin_ref("prod") == head
    assert rig.origin_ref("gated") == head
    assert _git(rig.origin, "tag", "--list", "deployed/r1") == ""
    assert "deployed" not in _round_json(rig)
    retry = _deploy(rig, **_verdict(newer))
    assert "partial deployment pins" in retry.stderr
    assert len(rig.deploy_log()) == 1


def test_immutable_tag_conflict_stops_before_rollout(rig: Rig) -> None:
    head = _cut(rig)
    _git(rig.root, "push", "-q", "origin", f"{rig.shas['c0']}:refs/tags/deployed/r1")
    out = _deploy(rig, **_verdict(head))
    assert out.returncode == 1
    assert "immutable deployed tag" in out.stderr
    assert rig.deploy_log() == []
    assert _tag(rig) == rig.shas["c0"]


def test_failed_retirement_resumes_at_same_sha(rig: Rig) -> None:
    head = _cut(rig)
    hook = _hook(
        rig,
        'while read old new ref; do\n [ "$ref" = refs/heads/release/r1 ] && [ "$new" = "0000000000000000000000000000000000000000" ] && exit 1\ndone\nexit 0\n',
    )
    out = _deploy(rig, DEPLOY_STUB_VERIFY="1", **_verdict(head))
    assert out.returncode == 1
    assert "leased release retirement failed" in out.stderr
    assert _rel(_round_json(rig))["deployment"]["runtime_confirmed_at"]
    assert _tag(rig) == head
    hook.unlink()
    out = _deploy(rig, DEPLOY_STUB_VERIFY="1", **_fresh(rig, "c3"), **_verdict(head))
    assert out.returncode == 0, out.stderr
    assert rig.deploy_log() == [f"{head} --pinned"]
    assert rig.origin_ref("release/r1") == ""


def test_resume_after_remote_deleted_before_state_clear(rig: Rig) -> None:
    head = _cut(rig)
    hook = _hook(
        rig,
        'while read old new ref; do\n [ "$new" = "0000000000000000000000000000000000000000" ] && exit 1\ndone\nexit 0\n',
    )
    assert _deploy(rig, DEPLOY_STUB_VERIFY="1", **_verdict(head)).returncode == 1
    hook.unlink()
    _git(rig.root, "push", "-q", "origin", "--delete", "release/r1")
    out = _deploy(rig, **_fresh(rig, "c3"))
    assert out.returncode == 0, out.stderr
    assert rig.deploy_log() == [f"{head} --pinned"]
    assert not _live(_round_json(rig))


@pytest.mark.parametrize("phase", ["tag", "main"])
def test_failed_tag_or_main_publish_preserves_verified_pin_and_resumes(
    rig: Rig, phase: str
) -> None:
    base = _cut(rig)
    head = _child(rig, base, "unforwarded fix")
    _push_branch(rig, "release/r1", head)
    ref = "refs/tags/deployed/r1" if phase == "tag" else "refs/heads/main"
    hook = _hook(
        rig,
        f'while read old new ref; do\n [ "$ref" = {ref} ] && exit 1\ndone\nexit 0\n',
    )
    out = _deploy(rig, DEPLOY_STUB_VERIFY="1", **_verdict(head))
    assert out.returncode == 1
    assert _rel(_round_json(rig))["deployment"]["runtime_confirmed_at"]
    assert rig.origin_ref("release/r1") == head
    assert rig.origin_ref("prod") == head
    assert rig.origin_ref("main") == rig.shas["c3"]
    hook.unlink()
    out = _deploy(rig, DEPLOY_STUB_VERIFY="1", **_verdict(head))
    assert out.returncode == 0, out.stderr
    assert _tag(rig) == head
    assert rig.origin_ref("release/r1") == ""


def test_main_arrival_during_rollout_is_preserved_and_not_deployed(rig: Rig) -> None:
    base = _cut(rig)
    head = _child(rig, base, "unforwarded release fix")
    _push_branch(rig, "release/r1", head)
    arrival = _child(rig, rig.shas["c3"], "late main arrival")
    out = _deploy(
        rig,
        DEPLOY_STUB_VERIFY="1",
        DEPLOY_STUB_HOOK=f"git push -q origin {arrival}:refs/heads/main",
        **_verdict(head),
    )
    assert out.returncode == 0, out.stderr
    main = rig.origin_ref("main")
    assert _git(rig.root, "show", "-s", "--format=%P", main).split() == [arrival, head]
    assert rig.origin_ref("prod") == _tag(rig) == head
    assert rig.deploy_log() == [f"{head} --pinned"]


def test_conflicting_final_forward_merge_preserves_release_and_tag(rig: Rig) -> None:
    base = _cut(rig)
    _git(rig.root, "checkout", "-q", "-b", "fix", base)
    (rig.root / "src/a.py").write_text("release edit\n", encoding="utf-8")
    _git(rig.root, "commit", "-q", "-am", "release edit")
    head = _git(rig.root, "rev-parse", "HEAD")
    _push_branch(rig, "release/r1", head)
    _git(rig.root, "checkout", "-q", "main")
    (rig.root / "src/a.py").write_text("main edit\n", encoding="utf-8")
    _git(rig.root, "commit", "-q", "-am", "main edit")
    main = _git(rig.root, "rev-parse", "HEAD")
    _push_branch(rig, "main", main)
    out = _deploy(rig, DEPLOY_STUB_VERIFY="1", **_verdict(head))
    assert out.returncode == 1
    assert "final forward merge conflicts" in out.stderr
    assert rig.origin_ref("main") == main
    assert rig.origin_ref("release/r1") == _tag(rig) == rig.origin_ref("prod") == head
    assert _git(rig.root, "diff", "--name-only") == ""


def test_retirement_lease_preserves_a_release_push_after_last_head_read(
    rig: Rig,
) -> None:
    head = _cut(rig)
    newer = _child(rig, head, "external fix at retirement")
    _push_branch(rig, "spare", newer)
    hook = _hook(
        rig,
        f'while read old new ref; do\n if [ "$ref" = refs/heads/release/r1 ] && [ "$new" = "0000000000000000000000000000000000000000" ]; then\n  unset GIT_QUARANTINE_PATH GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES; git update-ref refs/heads/release/r1 {newer} {head} || exit 9\n fi\ndone\nexit 0\n',
    )
    out = _deploy(rig, DEPLOY_STUB_VERIFY="1", **_verdict(head))
    assert out.returncode == 1
    assert rig.origin_ref("release/r1") == newer
    assert _rel(_round_json(rig))["deployment"]["sha"] == head
    assert _tag(rig) == head
    hook.unlink()


def test_lifecycle_serializes_round_changes_and_shell_release_fix(rig: Rig) -> None:
    head = _cut(rig)
    path = rig.root / ".git/precis-round-lifecycle.lock"
    with path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for args in (("deploy",), ("cut", "--abandon"), ("close",), ("open",)):
            out = rig.round(*args, **_verdict(head))
            assert out.returncode == 1
            assert "lifecycle busy" in out.stderr
        lib = Path(__file__).resolve().parents[1] / "scripts/lib/round-lock.sh"
        out = subprocess.run(
            ["bash", "-c", f'. "{lib}"; acquire_round_lock'],
            cwd=rig.root,
            env=rig.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert out.returncode == 1
        assert "lifecycle busy" in out.stderr
    assert _deploy(rig, "--dry-run", **_verdict(head)).returncode == 0
    assert rig.deploy_log() == []


def test_shell_keeps_lifecycle_lock_after_python_child_exits(rig: Rig) -> None:
    lib = Path(__file__).resolve().parents[1] / "scripts/lib/round-lock.sh"
    # Two helper calls in the SAME shell: the second must see the first
    # child's lock still held by fd9; shell exit then releases it.
    code = f'. "{lib}"; acquire_round_lock || exit 9; "{sys.executable}" -c \'import fcntl; f=open(".git/precis-round-lifecycle.lock","a"); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)\''
    out = subprocess.run(
        ["bash", "-c", code],
        cwd=rig.root,
        env=rig.env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert out.returncode == 1
    assert "BlockingIOError" in out.stderr
    with (rig.root / ".git/precis-round-lifecycle.lock").open(
        "a", encoding="utf-8"
    ) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_successful_rollout_waits_for_current_coordinator_runtime_confirmation(
    rig: Rig,
) -> None:
    head = _cut(rig)
    out = rig.round("deploy", DEPLOY_STUB_VERIFY="1", **_verdict(head))
    assert out.returncode == 3
    assert "runtime verification pending" in out.stdout
    assert rig.origin_ref("release/r1") == head
    assert not _rel(_round_json(rig))["deployment"].get("runtime_confirmed_at")
    out = rig.round(
        "deploy",
        "--confirm-runtime",
        rig.shas["c3"],
        "--runtime-evidence",
        "wrong code",
    )
    assert out.returncode == 3
    assert rig.origin_ref("release/r1") == head
    out = rig.round("deploy", "--confirm-runtime", head)
    assert out.returncode == 3
    out = rig.round(
        "deploy",
        "--confirm-runtime",
        head,
        "--runtime-evidence",
        "live services and session MCP observed ready on this SHA",
        LGM_EXACT="unreadable",
    )
    assert out.returncode == 0, out.stderr
    assert rig.deploy_log() == [f"{head} --pinned"]
    assert _tag(rig) == head


def test_confirm_runtime_cannot_preapprove_a_rollout(rig: Rig) -> None:
    head = _cut(rig)
    out = rig.round(
        "deploy",
        "--confirm-runtime",
        head,
        "--runtime-evidence",
        "old evidence",
        **_verdict(head),
    )
    assert out.returncode == 1
    assert rig.deploy_log() == []


def test_zero_exit_with_old_same_sha_receipt_does_not_confirm_rollout(rig: Rig) -> None:
    head = _cut(rig)
    hook = f"git push -q origin {head}:refs/heads/prod; printf '%s 1 success\\n' {head} > .git/precis-deploy-state"
    out = rig.round("deploy", DEPLOY_STUB_HOOK=hook, **_verdict(head))
    assert out.returncode == 1
    assert "matching fresh success marker" in out.stderr
    assert rig.origin_ref("prod") == rig.origin_ref("release/r1") == head
    assert not _rel(_round_json(rig))["deployment"].get("rollout_at")


def test_verified_closeout_ignores_expired_ci_and_never_reinstalls(rig: Rig) -> None:
    head = _cut(rig)
    hook = _hook(
        rig,
        'while read old new ref; do\n [ "$ref" = refs/tags/deployed/r1 ] && exit 1\ndone\nexit 0\n',
    )
    assert _deploy(rig, DEPLOY_STUB_VERIFY="1", **_verdict(head)).returncode == 1
    hook.unlink()
    out = rig.round("deploy", **_verdict(head, age=99))
    assert out.returncode == 0, out.stderr
    assert rig.deploy_log() == [f"{head} --pinned"]


def test_newer_prod_stops_old_journal_closeout(rig: Rig) -> None:
    head = _cut(rig)
    assert rig.round("deploy", DEPLOY_STUB_VERIFY="1", **_verdict(head)).returncode == 3
    _push_branch(rig, "prod", rig.shas["c3"])
    out = rig.round(
        "deploy", "--confirm-runtime", head, "--runtime-evidence", "stale evidence"
    )
    assert out.returncode == 1
    assert rig.origin_ref("prod") == rig.shas["c3"]
    assert rig.origin_ref("release/r1") == head
    assert len(rig.deploy_log()) == 1


def test_cut_intent_recovers_remote_creation_without_local_ack(rig: Rig) -> None:
    head = _cut(rig)
    state = rig.root / ".git/precis-round/round.json"
    cur = _round_json(rig)
    _rel(cur)["cut_pending"] = True
    state.write_text(json.dumps(cur), encoding="utf-8")
    assert rig.round("deploy", **_verdict(head)).returncode == 1
    out = rig.round("cut", **_fresh(rig, "c3"))
    assert out.returncode == 0, out.stderr
    assert "pending cut reconciled" in out.stdout
    assert "cut_pending" not in _rel(_round_json(rig))
    assert rig.origin_ref("release/r1") == head


def test_cut_race_loser_drops_its_pending_record(rig: Rig) -> None:
    """gr573151: when the expect-absent lease loses the cut push, the loser's
    own record must go — the next run sees a foreign branch, not a wedge."""
    _open_round(rig)
    foreign = _child(rig, rig.shas["c0"], "foreign winner")
    # Quarantine forbids update-ref inside pre-receive, so the hook pushes
    # the winner's ref from this clone instead (marker file stops recursion).
    mark = rig.origin.parent / "hook-once"
    _hook(
        rig,
        f'[ -f "{mark}" ] && exit 0\n'
        f'touch "{mark}"\n'
        "env -u GIT_DIR -u GIT_OBJECT_DIRECTORY"
        " -u GIT_ALTERNATE_OBJECT_DIRECTORIES -u GIT_QUARANTINE_PATH"
        f' git -C "{rig.root}" push -q origin'
        f" {foreign}:refs/heads/release/r1 >&2 || true\n"
        "exit 1\n",
    )
    out = rig.round("cut", **_fresh(rig))
    assert out.returncode == 1
    assert "exists on origin" in out.stderr
    assert rig.origin_ref("release/r1") == foreign
    assert not _live(_round_json(rig))
    out = rig.round("cut", **_fresh(rig))
    assert out.returncode == 1
    assert "one open release at a time" in out.stderr


def _publish_main_gated(rig: Rig, sha: str) -> subprocess.CompletedProcess[str]:
    repo = Path(__file__).resolve().parents[1]
    code = f'. "{repo}/scripts/lib/round-lock.sh"; . "{repo}/scripts/lib/env-pointers.sh"; if acquire_main_gated_publication; then env_pointer_move "$PWD" gated "$1"; release_round_lock; fi'
    return subprocess.run(
        ["bash", "-c", code, "_", sha],
        cwd=rig.root,
        env=rig.env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_main_gate_keeps_local_warrant_but_defers_global_gated_after_cut(
    rig: Rig,
) -> None:
    _cut(rig)
    out = _publish_main_gated(rig, rig.shas["c3"])
    assert out.returncode == 0
    assert "publication deferred" in out.stderr
    assert rig.origin_ref("gated") == ""
    assert rig.round("cut", "--abandon").returncode == 0
    assert _publish_main_gated(rig, rig.shas["c3"]).returncode == 0
    assert rig.origin_ref("gated") == rig.shas["c3"]


def test_main_gate_before_cut_requires_compatible_cut_sha(rig: Rig) -> None:
    assert _publish_main_gated(rig, rig.shas["c3"]).returncode == 0
    _open_round(rig)
    out = rig.round("cut", **_fresh(rig, "c2"))
    assert out.returncode == 1
    assert "gated cannot fast-forward" in out.stderr
    assert rig.round("cut", **_fresh(rig, "c3")).returncode == 0


def test_main_gated_publication_never_waits_on_busy_lifecycle(rig: Rig) -> None:
    with (rig.root / ".git/precis-round-lifecycle.lock").open(
        "a", encoding="utf-8"
    ) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        out = _publish_main_gated(rig, rig.shas["c3"])
        assert out.returncode == 0
        assert "lifecycle busy" in out.stderr
        assert rig.origin_ref("gated") == ""


def test_unresolved_journal_blocks_shell_release_mutation_after_wrapper_exit(
    rig: Rig,
) -> None:
    head = _cut(rig)
    assert rig.round("deploy", DEPLOY_STUB_RC="7", **_verdict(head)).returncode == 7
    lib = Path(__file__).resolve().parents[1] / "scripts/lib/round-lock.sh"
    out = subprocess.run(
        ["bash", "-c", f'. "{lib}"; acquire_round_lock && release_journal_clear'],
        cwd=rig.root,
        env=rig.env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert out.returncode == 1
    assert rig.origin_ref("release/r1") == head


def test_killed_round_wrapper_keeps_lock_until_active_deploy_child_exits(
    rig: Rig,
) -> None:
    head = _cut(rig)
    script = Path(__file__).resolve().parents[1] / "scripts/round"
    started, finish = rig.root / "child-started", rig.root / "child-finish"
    env = {
        **rig.env,
        **_verdict(head),
        "DEPLOY_STUB_HOOK": "touch child-started; while [ ! -f child-finish ]; do sleep 0.05; done",
    }
    p = subprocess.Popen(
        [sys.executable, str(script), "deploy"],
        cwd=rig.root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert started.exists()
        p.kill()
        p.wait(timeout=5)
        out = rig.round("cut", "--abandon")
        assert out.returncode == 1
        assert "lifecycle busy" in out.stderr
    finally:
        finish.touch()
        if p.poll() is None:
            p.kill()
        p.wait(timeout=5)
    # Kernel release when the child exits, not an age-based stale steal.
    deadline = time.monotonic() + 5
    with (rig.root / ".git/precis-round-lifecycle.lock").open(
        "a", encoding="utf-8"
    ) as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                assert time.monotonic() < deadline
                time.sleep(0.05)


def _foreign_tag(rig: Rig, n: int) -> None:
    _git(rig.root, "push", "-q", "origin", f"{rig.shas['c0']}:refs/tags/deployed/r{n}")


def test_open_numbers_past_foreign_deployed_tags(rig: Rig) -> None:
    _foreign_tag(rig, 12)
    out = rig.round("open")
    assert out.returncode == 0, out.stderr
    assert "round 13 open" in out.stdout
    assert _round_json(rig)["n"] == 13


def test_open_numbers_past_foreign_release_branch(rig: Rig) -> None:
    _push_branch(rig, "release/r20", rig.shas["c0"])
    assert "round 21 open" in rig.round("open").stdout


def test_cut_numbers_past_a_foreign_deployed_tag(rig: Rig) -> None:
    """gr573151: the round record is per clone — a deployed tag another clone
    pushed must move the next cut's number, never refuse the cut."""
    _open_round(rig)
    _foreign_tag(rig, 1)
    status = rig.round("status", "--json")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["next_n"] == 2
    out = rig.round("cut", **_fresh(rig))
    assert out.returncode == 0, out.stderr
    assert rig.origin_ref("release/r2") == rig.shas["c2"]
    assert rig.origin_ref("release/r1") == ""
    assert _rel(_round_json(rig), 2)["n"] == 2
    status = rig.round("status", "--json")
    assert json.loads(status.stdout)["next_n"] == 3


def test_cut_refuses_when_origin_is_unreadable(rig: Rig) -> None:
    _open_round(rig)
    _git(rig.root, "remote", "set-url", "origin", str(rig.root / "nonexistent.git"))
    out = rig.round("cut", **_fresh(rig))
    assert out.returncode == 1
    assert "ls-remote failed" in out.stderr
    assert not _live(_round_json(rig))
    status = rig.round("status", "--json")
    assert json.loads(status.stdout)["next_n"] is None


def test_open_refuses_when_origin_unreadable(rig: Rig) -> None:
    _git(rig.root, "remote", "set-url", "origin", str(rig.root / "nonexistent.git"))
    out = rig.round("open")
    assert out.returncode == 1
    assert "ls-remote failed" in out.stderr
    assert not (rig.root / ".git/precis-round/round.json").exists()


# ── skipped hosts (auto-skip; scripts/deploy writes precis-deploy-skipped) ──


def _skipped_json(sha: str, hosts: dict[str, str]) -> str:
    return json.dumps({"sha": sha, "at": "t", "hosts": hosts})


def test_skipped_hosts_are_journaled_and_shown_in_status(rig: Rig) -> None:
    head = _cut(rig)
    hosts = {"serving": rig.shas["c0"], "scheduler": rig.shas["c0"]}
    out = rig.round(
        "deploy",
        DEPLOY_STUB_VERIFY="1",
        DEPLOY_STUB_SKIPPED=_skipped_json(head, hosts),
        **_verdict(head),
    )
    assert out.returncode == 3, out.stdout + out.stderr
    assert "WARNING: skipped hosts" in out.stdout
    journal = _rel(_round_json(rig))["deployment"]
    assert journal["skipped_hosts"] == hosts
    status = rig.round("status").stdout
    assert "lagging hosts" in status and "serving" in status
    assert "deploy --catch-up" in status


def test_a_stale_or_absent_skipped_record_journals_nothing(rig: Rig) -> None:
    head = _cut(rig)
    out = rig.round(
        "deploy",
        DEPLOY_STUB_VERIFY="1",
        DEPLOY_STUB_SKIPPED=_skipped_json(rig.shas["c0"], {"serving": "old"}),
        **_verdict(head),
    )
    assert out.returncode == 3, out.stdout + out.stderr
    assert "skipped_hosts" not in _rel(_round_json(rig))["deployment"]


def test_catch_up_redeploys_the_pinned_sha_and_clears_the_journal(rig: Rig) -> None:
    head = _cut(rig)
    hosts = {"serving": rig.shas["c0"]}
    assert (
        rig.round(
            "deploy",
            DEPLOY_STUB_VERIFY="1",
            DEPLOY_STUB_SKIPPED=_skipped_json(head, hosts),
            **_verdict(head),
        ).returncode
        == 3
    )
    # The host is back: the re-run records no skips and the journal clears.
    out = rig.round("deploy", "--catch-up", DEPLOY_STUB_VERIFY="1")
    assert out.returncode == 3, out.stdout + out.stderr  # runtime still pending
    assert "every skipped host" in out.stdout
    assert rig.deploy_log() == [f"{head} --pinned", f"{head} --pinned"]
    assert "skipped_hosts" not in _rel(_round_json(rig))["deployment"]


def test_catch_up_keeps_hosts_still_down_in_the_journal(rig: Rig) -> None:
    head = _cut(rig)
    hosts = {"serving": rig.shas["c0"]}
    assert (
        rig.round(
            "deploy",
            DEPLOY_STUB_VERIFY="1",
            DEPLOY_STUB_SKIPPED=_skipped_json(head, hosts),
            **_verdict(head),
        ).returncode
        == 3
    )
    out = rig.round(
        "deploy",
        "--catch-up",
        DEPLOY_STUB_VERIFY="1",
        DEPLOY_STUB_SKIPPED=_skipped_json(head, hosts),
    )
    assert out.returncode == 3, out.stdout + out.stderr
    assert "still lagging" in out.stdout
    journal = _rel(_round_json(rig))["deployment"]
    assert journal["skipped_hosts"] == hosts


def test_catch_up_refuses_when_nothing_is_lagging(rig: Rig) -> None:
    head = _cut(rig)
    assert rig.round("deploy", DEPLOY_STUB_VERIFY="1", **_verdict(head)).returncode == 3
    out = rig.round("deploy", "--catch-up")
    assert out.returncode == 1
    assert "no skipped hosts" in out.stderr
    assert rig.deploy_log() == [f"{head} --pinned"]


def test_catch_up_refuses_before_any_rollout(rig: Rig) -> None:
    head = _cut(rig)
    out = rig.round("deploy", "--catch-up", **_verdict(head))
    assert out.returncode == 1
    assert "requires a recorded rollout" in out.stderr
    assert rig.deploy_log() == []


def test_catch_up_named_host_must_be_lagging_on_the_open_rollout(rig: Rig) -> None:
    head = _cut(rig)
    hosts = {"serving": rig.shas["c0"]}
    assert (
        rig.round(
            "deploy",
            DEPLOY_STUB_VERIFY="1",
            DEPLOY_STUB_SKIPPED=_skipped_json(head, hosts),
            **_verdict(head),
        ).returncode
        == 3
    )
    out = rig.round("deploy", "--catch-up", "gateway", DEPLOY_STUB_VERIFY="1")
    assert out.returncode == 1
    assert "gateway" in out.stderr and "lagging" in out.stderr
    assert rig.deploy_log() == [f"{head} --pinned"]


# ── catch-up after the round completed (gr513362 part 2) ───────────────


def _complete_with_lagging(rig: Rig, hosts: dict[str, str]) -> str:
    """Deploy+confirm a release whose rollout left `hosts` behind.

    Returns the deployed (pinned) sha; afterwards the round is completed:
    ``cur["deployed"]`` holds the journal, ``skipped_hosts`` included.
    """
    head = _cut(rig)
    assert (
        _deploy(
            rig,
            DEPLOY_STUB_VERIFY="1",
            DEPLOY_STUB_SKIPPED=_skipped_json(head, hosts),
            **_verdict(head),
        ).returncode
        == 0
    )
    return head


def test_catch_up_on_a_completed_round_retargets_named_lagging_hosts(
    rig: Rig,
) -> None:
    hosts = {"serving": rig.shas["c0"], "scheduler": rig.shas["c0"]}
    head = _complete_with_lagging(rig, hosts)
    out = rig.round("deploy", "--catch-up", "serving", DEPLOY_STUB_VERIFY="1")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "serving" in out.stdout and head[:9] in out.stdout
    # The rollout re-ran at the SAME pin — the completed sha never moves.
    assert rig.deploy_log() == [f"{head} --pinned", f"{head} --pinned"]
    # …and the archived journal is evidence, not a cursor the catch-up edits.
    assert _round_json(rig)["deployed"]["skipped_hosts"] == hosts


def test_catch_up_on_a_completed_round_refuses_a_non_lagging_host(rig: Rig) -> None:
    head = _complete_with_lagging(rig, {"serving": rig.shas["c0"]})
    out = rig.round("deploy", "--catch-up", "gateway", DEPLOY_STUB_VERIFY="1")
    assert out.returncode == 1
    assert "gateway" in out.stderr and "lagging" in out.stderr
    assert rig.deploy_log() == [f"{head} --pinned"]


def test_catch_up_on_a_completed_round_defaults_to_every_lagging_host(
    rig: Rig,
) -> None:
    hosts = {"serving": rig.shas["c0"], "scheduler": rig.shas["c0"]}
    head = _complete_with_lagging(rig, hosts)
    out = rig.round("deploy", "--catch-up", DEPLOY_STUB_VERIFY="1")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "serving" in out.stdout and "scheduler" in out.stdout
    assert rig.deploy_log() == [f"{head} --pinned", f"{head} --pinned"]


def test_catch_up_on_a_completed_round_refuses_when_nothing_lagged(rig: Rig) -> None:
    head = _cut(rig)
    assert _deploy(rig, DEPLOY_STUB_VERIFY="1", **_verdict(head)).returncode == 0
    out = rig.round("deploy", "--catch-up", "serving", DEPLOY_STUB_VERIFY="1")
    assert out.returncode == 1
    assert "lagging" in out.stderr
    out = rig.round("deploy", "--catch-up", DEPLOY_STUB_VERIFY="1")
    assert out.returncode == 1
    assert "nothing to catch up" in out.stderr
    assert rig.deploy_log() == [f"{head} --pinned"]


# ── release-train §5: pipelined releases (records carry state) ──────────


def _journal(rig: Rig, sha: str) -> dict[str, object]:
    return {
        "sha": sha,
        "started_at": "2026-01-01T00:00:00Z",
        "started_epoch": 1,
        "ci": {"sha": sha},
    }


def _record(
    n: int, base: str, state: str, journal: dict[str, object] | None = None
) -> dict[str, object]:
    rec: dict[str, object] = {
        "n": n,
        "branch": f"release/r{n}",
        "base": base,
        "cut_at": "2026-01-01T00:00:00Z",
        "state": state,
    }
    if journal is not None:
        rec["deployment"] = journal
    return rec


def _state_write(rig: Rig, *recs: dict[str, object], n: int = 1) -> None:
    path = rig.root / ".git" / "precis-round" / "round.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"n": n, "open": True, "releases": list(recs)}),
        encoding="utf-8",
    )


def _deploying(rig: Rig, n: int = 1) -> str:
    """A recorded release in `deploying`: cut it, then journal a failed deploy."""
    head = _cut(rig)
    return _journal_deploy(rig, head, n)


def _journal_deploy(rig: Rig, head: str, n: int = 1) -> str:
    """Journal a failed deploy on the already-cut release → `deploying`."""
    assert rig.round("deploy", DEPLOY_STUB_RC="7", **_verdict(head)).returncode == 7
    assert _rel(_round_json(rig), n)["state"] == "deploying"
    return head


def _journal_deploy_unlaunched(rig: Rig, head: str, n: int = 1) -> str:
    """Journal a deploy that died BEFORE `scripts/deploy` ran — the env
    pointer move refused after the journal write, so the record is
    `deploying` with no `started_epoch`: the never-launched shape."""
    out = rig.round("deploy", PRECIS_ENV_POINTERS="0", **_verdict(head))
    assert out.returncode == 1, out.stdout + out.stderr
    rel = _rel(_round_json(rig), n)
    assert rel["state"] == "deploying"
    assert "started_epoch" not in rel["deployment"]
    assert rig.deploy_log() == []
    return head


@pytest.mark.parametrize("state", ["cut", "gating", "fixing", "green"])
def test_cut_refused_while_a_release_is_open(rig: Rig, state: str) -> None:
    _cut(rig)
    _state_write(rig, _record(1, rig.shas["c2"], state))
    out = rig.round("cut", **_fresh(rig, "c3"))
    assert out.returncode == 1
    assert "already records an open release (release/r1)" in out.stderr
    assert rig.origin_ref("release/r2") == ""


def test_cut_n1_allowed_while_n_is_deploying_with_main_moved(rig: Rig) -> None:
    head = _deploying(rig)
    newer = _commit(rig.root, "src/c4.py", "c4 code")
    _git(rig.root, "push", "-q", "origin", "main")
    out = rig.round("cut")
    assert out.returncode == 0, out.stderr
    assert rig.origin_ref("release/r2") == newer
    rels = _rels(_round_json(rig))
    assert [r["state"] for r in rels] == ["deploying", "cut"]
    assert _rel(_round_json(rig), 2)["base"] == newer
    assert rig.origin_ref("release/r1") == head


def test_cut_n1_refused_when_mains_tree_equals_n_head(rig: Rig) -> None:
    head = _deploying(rig)
    # main's tip carries exactly the release head's tree: nothing new to ship.
    same = _git(
        rig.root,
        "commit-tree",
        f"{head}^{{tree}}",
        "-p",
        rig.shas["c3"],
        "-m",
        "same tree",
    )
    _push_branch(rig, "main", same)
    out = rig.round("cut")
    assert out.returncode == 1
    assert "tree equals release/r1's head" in out.stderr
    assert rig.origin_ref("release/r2") == ""


def test_cut_n1_refused_when_main_lacks_n_head(rig: Rig) -> None:
    head = _deploying(rig)
    fix = _child(rig, head, "unforwarded release fix")
    _push_branch(rig, "release/r1", fix)
    newer = _commit(rig.root, "src/c4.py", "c4 code")
    _git(rig.root, "push", "-q", "origin", "main")
    out = rig.round("cut")
    assert out.returncode == 1
    assert "does not contain release/r1's head" in out.stderr
    assert rig.origin_ref("release/r2") == ""


def test_cut_n1_refuses_a_sha_other_than_origin_main(rig: Rig) -> None:
    _deploying(rig)
    newer = _commit(rig.root, "src/c4.py", "c4 code")
    _git(rig.root, "push", "-q", "origin", "main")
    out = rig.round("cut", "--sha", rig.shas["c2"])
    assert out.returncode == 1
    assert f"cuts at origin/main ({newer[:9]})" in out.stderr
    assert rig.origin_ref("release/r2") == ""


def test_old_single_release_shape_is_read_as_the_open_release(rig: Rig) -> None:
    path = rig.root / ".git" / "precis-round" / "round.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "n": 1,
                "open": True,
                "release": {"branch": "release/r1", "base": rig.shas["c2"]},
            }
        ),
        encoding="utf-8",
    )
    _push_branch(rig, "release/r1", rig.shas["c2"])
    out = rig.round("status")
    assert out.returncode == 0, out.stderr
    assert "release release/r1 at" in out.stdout
    assert rig.round("cut", **_fresh(rig, "c3")).returncode == 1
    # The next write upgrades the file to the list shape.
    assert rig.round("cut", "--abandon").returncode == 0
    assert _rel(_round_json(rig), 1)["state"] == "abandoned"


@pytest.mark.parametrize(
    "content",
    [
        '{"n":1,"open":true,"releases":{}}',
        '{"n":1,"open":true,"release":{},"releases":[]}',
        '{"n":1,"open":true,"releases":[{"branch":"release/r1","base":"'
        + "a" * 40
        + '","state":"cut"},{"branch":"release/r2","base":"'
        + "b" * 40
        + '","state":"gating"}]}',
        '{"n":1,"open":true,"releases":[{"branch":"release/r1","base":"'
        + "a" * 40
        + '","state":"bogus"}]}',
        '{"n":1,"open":false,"releases":[{"branch":"release/r1","base":"'
        + "a" * 40
        + '","state":"cut"}]}',
    ],
)
@pytest.mark.parametrize("command", ["deploy", "open"])
def test_invalid_releases_list_refuses(rig: Rig, content: str, command: str) -> None:
    path = rig.root / ".git" / "precis-round" / "round.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    out = rig.round(command, **_fresh(rig))
    assert out.returncode == 1
    assert "invalid" in out.stderr
    assert path.read_text(encoding="utf-8") == content
    assert rig.deploy_log() == []


def test_peer_marks_attach_to_the_open_release(rig: Rig) -> None:
    head = _cut(rig)
    assert rig.round("in", rig.shas["c3"], PRECIS_ROUND_PEER="alice").returncode == 0
    marks = rig.root / ".git" / "precis-round" / "marks"
    assert (
        json.loads((marks / "alice.json").read_text(encoding="utf-8"))["release"] == 1
    )
    _journal_deploy(rig, head)
    newer = _commit(rig.root, "src/c4.py", "c4 code")
    _git(rig.root, "push", "-q", "origin", "main")
    assert rig.round("cut").returncode == 0
    # The tally resets for the new open release: alice's r1 mark is out of
    # scope — she is neither credited nor pending under r2.
    assert "alice" not in rig.round("status").stdout
    assert rig.round("in", newer, PRECIS_ROUND_PEER="bob").returncode == 0
    assert json.loads((marks / "bob.json").read_text(encoding="utf-8"))["release"] == 2
    status = rig.round("status").stdout
    assert any(
        cols[0] == "bob" and cols[1] == "in"
        for line in status.splitlines()
        if len(cols := line.split()) > 1
    )
    assert head == rig.origin_ref("release/r1")
    # Abandon the open release: no open release remains, so the next marks
    # attach to the release the NEXT cut would mint (max record n + 1) —
    # never back to r1's scope.
    assert rig.round("cut", "--abandon").returncode == 0
    assert rig.round("in", newer, PRECIS_ROUND_PEER="alice").returncode == 0
    assert (
        json.loads((marks / "alice.json").read_text(encoding="utf-8"))["release"] == 3
    )


def test_supersede_retires_open_release_without_remote_delete(rig: Rig) -> None:
    """gr531127: a red release whose commits are all on main is retired in the
    record only; the next cut lands at origin/main and origin keeps r1."""
    head = _cut(rig)
    newer = _commit(rig.root, "src/c4.py", "c4 code")
    _git(rig.root, "push", "-q", "origin", "main")
    refused = rig.round("cut")
    assert refused.returncode == 1
    assert "--supersede" in refused.stderr
    dry = rig.round("cut", "--supersede", "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert "would mark release/r1 superseded" in dry.stdout
    assert _rel(_round_json(rig), 1)["state"] == "cut"
    both = rig.round("cut", "--supersede", "--abandon")
    assert both.returncode == 2
    assert "not allowed with" in both.stderr
    out = rig.round("cut", "--supersede")
    assert out.returncode == 0, out.stderr
    assert _rel(_round_json(rig), 1)["state"] == "superseded"
    assert _rel(_round_json(rig), 2)["state"] == "cut"
    assert rig.origin_ref("release/r1") == head
    assert rig.origin_ref("release/r2") == newer


def test_supersede_refuses_without_an_open_release(rig: Rig) -> None:
    _open_round(rig)
    out = rig.round("cut", "--supersede")
    assert out.returncode == 1
    assert "nothing to supersede" in out.stderr


def _gate_rows(rig: Rig, sha: str, *verdicts: str) -> None:
    """Canned `gh …/check-runs --jq` rows for `sha`: name/status/conclusion."""
    url = "https://github.com/o/r/actions/runs/4242/job/9"
    (rig.gh_dir / sha).write_text(
        "".join(f"{v}\t{url}\n" for v in verdicts), encoding="utf-8"
    )


def test_cut_supersedes_a_red_release_contained_in_main(rig: Rig) -> None:
    """gr531127: plain `cut` retires a red release whose head is an ancestor
    of origin/main and cuts r<N+1> at main; the red branch is not deleted."""
    head = _cut(rig)
    _gate_rows(
        rig,
        head,
        "lint\tcompleted\tsuccess",
        "test-linux (1)\tcompleted\tsuccess",
        "test-linux (2)\tcompleted\tfailure",
    )
    out = rig.round("cut")
    assert out.returncode == 0, out.stderr
    assert "r1 superseded by r2 (red, contained in main)" in out.stdout
    old = _rel(_round_json(rig), 1)
    assert old["state"] == "superseded"
    assert old["superseded_by"] == 2
    assert _rel(_round_json(rig), 2)["state"] == "cut"
    assert rig.origin_ref("release/r1") == head
    assert rig.origin_ref("release/r2") == rig.shas["c3"]
    status = rig.round("status")
    assert status.returncode == 0, status.stderr
    assert "release/r1" in status.stdout
    assert "superseded" in status.stdout


def test_cut_refuses_a_red_release_with_commits_not_on_main(rig: Rig) -> None:
    """A red release head that is not an ancestor of origin/main keeps the
    refusal, naming the unforwarded commits."""
    _cut(rig)
    fix = _child(rig, rig.shas["c2"], "unforwarded release fix")
    _push_branch(rig, "release/r1", fix)
    _gate_rows(
        rig,
        fix,
        "lint\tcompleted\tsuccess",
        "test-linux (1)\tcompleted\tfailure",
    )
    out = rig.round("cut")
    assert out.returncode == 1
    assert "carries commits not on origin/main" in out.stderr
    assert rig.origin_ref("release/r2") == ""
    assert _rel(_round_json(rig), 1)["state"] == "cut"


@pytest.mark.parametrize(
    "verdicts",
    [
        pytest.param(
            ["lint\tcompleted\tsuccess", "test-linux (1)\tcompleted\tsuccess"],
            id="green",
        ),
        pytest.param(
            ["lint\tcompleted\tsuccess", "test-linux (1)\tin_progress\t"],
            id="gating",
        ),
    ],
)
def test_cut_keeps_refusing_a_green_or_gating_release(
    rig: Rig, verdicts: list[str]
) -> None:
    """Supersede applies only to a red release; green and still-gating heads
    get the same refusal as before."""
    head = _cut(rig)
    _gate_rows(rig, head, *verdicts)
    out = rig.round("cut")
    assert out.returncode == 1
    assert "already records an open release" in out.stderr
    assert rig.origin_ref("release/r2") == ""
    assert _rel(_round_json(rig), 1)["state"] == "cut"


def test_cut_supersedes_an_open_release_whose_branch_left_origin(rig: Rig) -> None:
    """gr593418: the record says open but release/r<N> is gone from origin
    (the pass was abandoned elsewhere) — the dead record is superseded and
    the next cut proceeds at origin/main."""
    _cut(rig)
    _git(rig.origin, "update-ref", "-d", "refs/heads/release/r1")
    out = rig.round("cut")
    assert out.returncode == 0, out.stderr
    assert "superseded r1: branch gone" in out.stdout
    old = _rel(_round_json(rig), 1)
    assert old["state"] == "superseded"
    assert old["superseded_by"] == 2
    assert old["superseded_why"] == "branch gone"
    assert _rel(_round_json(rig), 2)["state"] == "cut"
    assert rig.origin_ref("release/r1") == ""
    assert rig.origin_ref("release/r2") == rig.shas["c3"]


def test_cut_supersedes_a_failed_release_record(rig: Rig) -> None:
    """gr593418 review 2: a record the deploy leg marked `failed` is dead
    the same way — but only while its journal shows `scripts/deploy` never
    launched (no `started_epoch`): superseded in the record, next cut at
    origin/main, branch kept."""
    head = _cut(rig)
    _journal_deploy_unlaunched(rig, head)  # died pre-launch → `deploying`
    assert rig.round("fail").returncode == 0
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "failed"
    assert "started_epoch" not in rel["deployment"]
    out = rig.round("cut")
    assert out.returncode == 0, out.stderr
    assert "superseded r1: state failed" in out.stdout
    old = _rel(_round_json(rig), 1)
    assert old["state"] == "superseded"
    assert old["superseded_by"] == 2
    assert old["superseded_why"] == "state failed"
    assert old["deployment"]["sha"] == head  # the journal is kept
    assert rig.origin_ref("release/r1") == head  # the dead branch is not deleted
    assert _rel(_round_json(rig), 2)["state"] == "cut"
    assert rig.origin_ref("release/r2") == rig.shas["c3"]


def test_cut_pending_with_a_gone_branch_still_reconciles(rig: Rig) -> None:
    """Scope guard: the pending-cut path is untouched — a `cut_pending`
    record whose branch left origin reconciles (re-pushes the recorded
    base), never auto-supersedes."""
    head = _cut(rig)
    path = rig.root / ".git" / "precis-round" / "round.json"
    cur = _round_json(rig)
    _rel(cur, 1)["cut_pending"] = True
    path.write_text(json.dumps(cur), encoding="utf-8")
    _git(rig.origin, "update-ref", "-d", "refs/heads/release/r1")
    out = rig.round("cut")
    assert out.returncode == 0, out.stderr
    assert "pending cut reconciled" in out.stdout
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "cut"
    assert "superseded_by" not in rel
    assert rig.origin_ref("release/r1") == head  # re-pushed at the recorded base
    assert rig.origin_ref("release/r2") == ""


def test_fail_marks_the_deploying_record_failed(rig: Rig) -> None:
    """`round fail` is the deploy leg's terminal verdict: the record moves
    to `failed` keeping its journal; the branch is untouched."""
    head = _deploying(rig)
    out = rig.round("fail", "--dry-run")
    assert out.returncode == 0, out.stderr
    assert "would mark release/r1 failed" in out.stdout
    assert _rel(_round_json(rig), 1)["state"] == "deploying"
    out = rig.round("fail")
    assert out.returncode == 0, out.stderr
    assert "partial rollout stays live" in out.stdout  # the rollout launched
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "failed"
    assert rel["deployment"]["sha"] == head
    assert rig.origin_ref("release/r1") == head
    # Idempotent: a second call is a no-op, not a refusal.
    assert rig.round("fail").returncode == 0


@pytest.mark.parametrize("verb_args", [["fail"], ["fail", "r1"]])
def test_fail_refuses_a_release_that_never_rolled_out(
    rig: Rig, verb_args: list[str]
) -> None:
    """A refusal before any work — the record is still open and carries no
    deployment journal — is never a `failed` verdict."""
    _cut(rig)
    out = rig.round(*verb_args)
    assert out.returncode == 1
    assert "never started a rollout" in out.stderr
    assert _rel(_round_json(rig), 1)["state"] == "cut"


def test_fail_refuses_a_finished_rollout_and_a_terminal_record(rig: Rig) -> None:
    head = _cut(rig)
    assert _deploy(rig, DEPLOY_STUB_VERIFY="1", **_verdict(head)).returncode == 0
    # `confirmed` (terminal) — refusing protects the completed rollout.
    out = rig.round("fail")
    assert out.returncode == 1
    assert _rel(_round_json(rig), 1)["state"] == "confirmed"


def test_fail_refuses_an_unknown_release_name(rig: Rig) -> None:
    _deploying(rig)
    out = rig.round("fail", "release/r9")
    assert out.returncode == 1
    assert "no release r9" in out.stderr
    assert _rel(_round_json(rig), 1)["state"] == "deploying"


def _failed_deploy(rig: Rig) -> str:
    """A `failed` record whose journal shows the rollout launched —
    `scripts/deploy` ran and died mid-rollout (`started_epoch`, no
    `rollout_at`): the live partial rollout a post-launch death leaves."""
    head = _cut(rig)
    _journal_deploy(rig, head)  # scripts/deploy ran, exited 7 → `deploying`
    out = rig.round("fail")
    assert out.returncode == 0, out.stderr
    assert "partial rollout stays live" in out.stdout
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "failed"
    assert rel["deployment"].get("started_epoch")
    assert not rel["deployment"].get("rollout_at")
    return head


def test_failed_with_a_launched_rollout_stays_live(rig: Rig) -> None:
    """gr593418 review 2: `failed` is terminal only while `scripts/deploy`
    never launched. A failed record whose journal carries `started_epoch`
    is a live partial rollout — `cut` refuses it (supersede --partial is
    the only door), `deploy`/`--finish-pointer` find it and resume it, and
    `fail` stays idempotent on it."""
    head = _failed_deploy(rig)
    rel = _rel(_round_json(rig), 1)
    assert rel["deployment"]["started_epoch"]
    out = rig.round("fail")
    assert out.returncode == 0 and "already failed" in out.stdout
    out = rig.round("status")
    assert "failed mid-rollout" in out.stdout
    out = rig.round("cut", **_fresh(rig, "c3"))
    assert out.returncode == 1
    assert "partial rollout" in out.stderr
    assert _rel(_round_json(rig), 1)["state"] == "failed"
    assert rig.origin_ref("release/r2") == ""
    # --finish-pointer finds the journal (its refusal is the receipt's,
    # not "a recorded deployment").
    out = rig.round("deploy", "--finish-pointer")
    assert out.returncode == 1
    assert "convergence receipt" in out.stderr
    out = rig.round("deploy", DEPLOY_STUB_VERIFY="1", **_verdict(head))
    assert out.returncode == 3, out.stdout + out.stderr
    assert "runtime verification pending" in out.stdout
    out = rig.round(
        "deploy",
        "--confirm-runtime",
        head,
        "--runtime-evidence",
        "all required daemons and session MCP ready on the pinned SHA",
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert _rel(_round_json(rig), 1)["state"] == "confirmed"


def test_failed_with_a_launched_rollout_is_retired_only_by_partial_supersede(
    rig: Rig,
) -> None:
    """A live `failed` record takes the same door a deploying one does:
    `supersede --partial` retires it keeping the journal. A terminal
    `failed` record (deploy never launched) refuses supersede — the next
    cut owns it."""
    head = _failed_deploy(rig)
    journal = dict(_rel(_round_json(rig), 1)["deployment"])
    # prod backed off the pin: what rolled out is partial.
    _git(rig.origin, "update-ref", "-d", "refs/heads/prod")
    _push_branch(rig, "prod", rig.shas["c0"])
    out = rig.round("supersede", "--partial", "--reason", "rollout stalled")
    assert out.returncode == 0, out.stderr
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "superseded"
    assert rel["superseded_partial"] is True
    assert rel["deployment"] == journal
    assert rig.origin_ref("prod") != head


def test_a_resumed_failed_leg_keeps_started_epoch(rig: Rig) -> None:
    """gr593418 review 3: resuming a live `failed` record rewrites the
    deployment journal — the rewrite must carry the prior `started_epoch`,
    so a second death before `scripts/deploy` relaunches can never read
    terminal over hosts that already took leg one's rollout."""
    head = _failed_deploy(rig)
    started = _rel(_round_json(rig), 1)["deployment"]["started_epoch"]
    # The resumed leg dies before `scripts/deploy` relaunches — the
    # env-pointer move refuses after the journal rewrite.
    out = rig.round("deploy", PRECIS_ENV_POINTERS="0", **_verdict(head))
    assert out.returncode == 1, out.stdout + out.stderr
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "deploying"
    assert rel["deployment"]["started_epoch"] == started  # carried, not lost
    assert rig.deploy_log() == [f"{head} --pinned"]  # leg one only
    assert rig.round("fail").returncode == 0
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "failed"
    assert rel["deployment"]["started_epoch"] == started  # still a live partial
    out = rig.round("cut", **_fresh(rig, "c3"))
    assert out.returncode == 1
    assert "partial rollout" in out.stderr
    assert rig.origin_ref("release/r2") == ""


def test_a_resumed_failed_leg_keeps_started_epoch_on_a_refusal(rig: Rig) -> None:
    """Same carry on a `_deploy_refuse` after the rewrite — the retain leg
    (a certified descendant of the release head already sits in main)
    refuses with pointer moves disabled, post-rewrite."""
    head = _failed_deploy(rig)
    started = _rel(_round_json(rig), 1)["deployment"]["started_epoch"]
    # gated = main's tip: a descendant of the release head already in
    # main → the resumed deploy retains it instead of moving it.
    _push_branch(rig, "gated", rig.shas["c3"])
    out = rig.round("deploy", PRECIS_ENV_POINTERS="0", **_verdict(head))
    assert out.returncode == 1
    assert "env_pointer_move gated disabled" in out.stderr
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "deploying"
    assert rel["deployment"]["started_epoch"] == started
    assert rig.round("fail").returncode == 0
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "failed"
    assert rel["deployment"]["started_epoch"] == started
    out = rig.round("cut", **_fresh(rig, "c3"))
    assert out.returncode == 1 and "partial rollout" in out.stderr


def test_supersede_refuses_a_terminal_failed_record(rig: Rig) -> None:
    """`failed` whose rollout never launched is already terminal: the next
    `cut` supersedes it — `supersede` reports nothing to do."""
    head = _cut(rig)
    _journal_deploy_unlaunched(rig, head)
    assert rig.round("fail").returncode == 0
    assert _rel(_round_json(rig), 1)["state"] == "failed"
    out = rig.round("supersede", "r1")
    assert out.returncode == 1
    assert "nothing to supersede" in out.stderr
    assert _rel(_round_json(rig), 1)["state"] == "failed"


def test_deploy_resumes_the_journal_record_with_an_open_release(rig: Rig) -> None:
    """`round deploy` acts on the deploying record, not the open one."""
    head = _deploying(rig)
    newer = _commit(rig.root, "src/c4.py", "c4 code")
    _git(rig.root, "push", "-q", "origin", "main")
    assert rig.round("cut").returncode == 0
    out = rig.round("deploy", DEPLOY_STUB_VERIFY="1", **_verdict(head))
    assert out.returncode == 3, out.stdout + out.stderr
    assert rig.deploy_log() == [f"{head} --pinned", f"{head} --pinned"]
    assert _rel(_round_json(rig), 1)["state"] == "deployed"
    assert _rel(_round_json(rig), 2)["state"] == "cut"


# ── supersede (gr573151): a record prod already moved past is stale ────────


def _prod_moved_past(rig: Rig) -> str:
    """A deployed-unconfirmed record whose prod already moved past the pin."""
    head = _cut(rig)
    out = rig.round("deploy", DEPLOY_STUB_VERIFY="1", **_verdict(head))
    assert out.returncode == 3, out.stdout + out.stderr
    _push_branch(rig, "prod", rig.shas["c3"])
    return head


def test_supersede_marks_the_record_prod_moved_past(rig: Rig) -> None:
    """gr573151: a deployed-unconfirmed record prod moved past is stale —
    supersede retires it and keeps the journal; gr580767: it also deletes
    the remote branch whose head is on main, so release_poll stops reading
    it in flight."""
    head = _prod_moved_past(rig)
    out = rig.round("supersede")
    assert out.returncode == 0, out.stderr
    assert (
        f"r1 superseded (origin/prod {rig.shas['c3'][:9]} is past {head[:9]})"
        in out.stdout
    )
    assert f"release/r1 deleted on origin ({head[:9]} is on main)" in out.stdout
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "superseded"
    assert rel["superseded_why"] == "origin/prod moved past it"
    assert rel["deployment"]["sha"] == head
    assert rig.origin_ref("release/r1") == ""
    assert rig.origin_ref("prod") == rig.shas["c3"]
    assert rig.round("close").returncode == 0
    assert rig.round("open").returncode == 0
    # prod sits at c3: only a cut at or past it can fast-forward prod.
    assert rig.round("cut", **_fresh(rig, "c3")).returncode == 0


def test_supersede_dry_run_leaves_the_record_untouched(rig: Rig) -> None:
    head = _prod_moved_past(rig)
    path = rig.root / ".git/precis-round/round.json"
    before = path.read_bytes()
    out = rig.round("supersede", "--dry-run")
    assert out.returncode == 0, out.stderr
    assert "dry-run: would supersede r1" in out.stdout
    assert (
        f"dry-run: would delete release/r1 on origin ({head[:9]} is on main)"
        in out.stdout
    )
    assert path.read_bytes() == before
    assert rig.origin_ref("release/r1") == head


def test_supersede_refuses_when_prod_sits_at_the_pin(rig: Rig) -> None:
    head = _cut(rig)
    out = rig.round("deploy", DEPLOY_STUB_VERIFY="1", **_verdict(head))
    assert out.returncode == 3
    out = rig.round("supersede")
    assert out.returncode == 1
    assert "--confirm-runtime" in out.stderr
    assert _rel(_round_json(rig), 1)["state"] == "deployed"


def test_supersede_refuses_a_runtime_confirmed_record(rig: Rig) -> None:
    head = _cut(rig)
    assert _deploy(rig, DEPLOY_STUB_VERIFY="1", **_verdict(head)).returncode == 0
    out = rig.round("supersede", "r1")
    assert out.returncode == 1
    assert "round deploy" in out.stderr


def test_supersede_refuses_an_open_record_and_an_unknown_one(rig: Rig) -> None:
    _cut(rig)
    out = rig.round("supersede", "r1")
    assert out.returncode == 1
    assert "cut --supersede" in out.stderr
    assert _rel(_round_json(rig), 1)["state"] == "cut"
    out = rig.round("supersede", "release/r9")
    assert out.returncode == 1
    assert "r9" in out.stderr


def test_supersede_refuses_a_partial_rollout(rig: Rig) -> None:
    _deploying(rig)
    out = rig.round("supersede")
    assert out.returncode == 1
    assert "partial" in out.stderr
    assert "round deploy" in out.stderr
    assert _rel(_round_json(rig), 1)["state"] == "deploying"


def test_supersede_needs_a_live_record_or_a_name(rig: Rig) -> None:
    _open_round(rig)
    out = rig.round("supersede")
    assert out.returncode == 1
    assert "no live deploying/deployed" in out.stderr


def test_supersede_deletes_only_the_inspected_head_on_main(rig: Rig) -> None:
    """gr580767: a superseded branch carrying commits main lacks is kept —
    the lease names the inspected head and the verb lists what must be
    forwarded by hand."""
    _prod_moved_past(rig)
    fix = _child(rig, rig.shas["c2"], "unforwarded release fix")
    _push_branch(rig, "release/r1", fix)
    out = rig.round("supersede", "r1")
    assert out.returncode == 0, out.stderr
    assert _rel(_round_json(rig), 1)["state"] == "superseded"
    assert rig.origin_ref("release/r1") == fix
    assert "deleted on origin" not in out.stdout
    assert "forward" in out.stdout and "by hand" in out.stdout
    assert fix[:7] in out.stdout


def test_supersede_retries_a_failed_branch_delete(rig: Rig) -> None:
    """A refused delete leaves the record terminal but the branch in
    flight — naming the superseded record retries the cleanup and never
    rewrites round.json."""
    head = _prod_moved_past(rig)
    hook = _hook(
        rig,
        "while read old new ref; do\n"
        '  if [ "$ref" = refs/heads/release/r1 ]; then\n'
        '    echo "remote rejects the release/r1 delete" >&2\n'
        "    exit 1\n"
        "  fi\n"
        "done\n",
    )
    out = rig.round("supersede")
    assert out.returncode == 0, out.stderr
    assert "could not delete release/r1 on origin" in out.stderr
    assert rig.origin_ref("release/r1") == head
    assert _rel(_round_json(rig), 1)["state"] == "superseded"
    path = rig.root / ".git/precis-round/round.json"
    before = path.read_bytes()
    # While origin still refuses, the retry warns again but does not fail.
    out = rig.round("supersede", "r1")
    assert out.returncode == 0, out.stderr
    assert "already superseded" in out.stdout
    assert "could not delete release/r1 on origin" in out.stderr
    assert rig.origin_ref("release/r1") == head
    out = rig.round("supersede", "r1", "--dry-run")
    assert out.returncode == 0, out.stderr
    assert "already superseded" in out.stdout
    assert f"dry-run: would delete release/r1 on origin ({head[:9]}" in out.stdout
    assert rig.origin_ref("release/r1") == head
    hook.unlink()
    out = rig.round("supersede", "r1")
    assert out.returncode == 0, out.stderr
    assert f"release/r1 deleted on origin ({head[:9]} is on main)" in out.stdout
    assert rig.origin_ref("release/r1") == ""
    assert path.read_bytes() == before


def test_supersede_retries_a_branch_left_by_cut_supersede(rig: Rig) -> None:
    """`cut --supersede` retires the record only — release/r1 stays on
    origin with no deployed/r1 tag, so release_poll reads it in flight
    forever. Naming the superseded record retires the branch."""
    head = _cut(rig)
    newer = _commit(rig.root, "src/c4.py", "c4 code")
    _git(rig.root, "push", "-q", "origin", "main")
    out = rig.round("cut", "--supersede")
    assert out.returncode == 0, out.stderr
    assert _rel(_round_json(rig), 1)["state"] == "superseded"
    assert rig.origin_ref("release/r1") == head
    out = rig.round("supersede", "r1")
    assert out.returncode == 0, out.stderr
    assert "already superseded" in out.stdout
    assert f"release/r1 deleted on origin ({head[:9]} is on main)" in out.stdout
    assert rig.origin_ref("release/r1") == ""
    assert rig.origin_ref("release/r2") == newer


def test_supersede_retry_deletes_once_the_head_reaches_main(rig: Rig) -> None:
    """A kept branch is retried cheaply: the unforwarded note is reprinted
    while main lacks the head, and the delete lands once main holds it."""
    _prod_moved_past(rig)
    fix = _child(rig, rig.shas["c2"], "unforwarded release fix")
    _push_branch(rig, "release/r1", fix)
    assert rig.round("supersede", "r1").returncode == 0
    assert rig.origin_ref("release/r1") == fix
    out = rig.round("supersede", "r1")
    assert out.returncode == 0, out.stderr
    assert "already superseded" in out.stdout
    assert fix[:7] in out.stdout
    assert rig.origin_ref("release/r1") == fix
    merged = _git(
        rig.root,
        "commit-tree",
        f"{fix}^{{tree}}",
        "-p",
        rig.shas["c3"],
        "-p",
        fix,
        "-m",
        "forward the release fix",
    )
    _push_branch(rig, "main", merged)
    out = rig.round("supersede", "r1")
    assert out.returncode == 0, out.stderr
    assert f"release/r1 deleted on origin ({fix[:9]} is on main)" in out.stdout
    assert rig.origin_ref("release/r1") == ""


# ── supersede --partial (td574370): a rollout that never reached prod ────


def test_supersede_partial_retires_a_partial_rollout_keeping_the_journal(
    rig: Rig,
) -> None:
    """A deploying record whose pinned sha never reached origin/prod is a
    partial rollout: --partial retires the record, stores the reason and
    keeps the deployment journal byte-for-byte; a branch whose head is on
    main is stale and deleted."""
    _deploying(rig)
    journal = dict(_rel(_round_json(rig), 1)["deployment"])
    out = rig.round("supersede", "--partial", "--reason", "rollout stalled")
    assert out.returncode == 0, out.stderr
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "superseded"
    assert rel["superseded_why"] == "rollout stalled"
    assert rel["superseded_partial"] is True
    assert rel["deployment"] == journal
    assert rig.origin_ref("release/r1") == ""
    assert rig.origin_ref("prod") == ""


@pytest.mark.parametrize("reason", [[], [""], ["   "]])
def test_supersede_partial_requires_a_reason(rig: Rig, reason: list[str]) -> None:
    _deploying(rig)
    out = rig.round(
        "supersede", "--partial", *(["--reason", *reason] if reason else [])
    )
    assert out.returncode == 2
    assert "reason" in out.stderr
    assert _rel(_round_json(rig), 1)["state"] == "deploying"


def test_reason_without_partial_is_refused(rig: Rig) -> None:
    _deploying(rig)
    out = rig.round("supersede", "--reason", "rollout stalled")
    assert out.returncode == 2
    assert "reason" in out.stderr


def test_supersede_partial_refuses_when_prod_has_the_pin(rig: Rig) -> None:
    head = _cut(rig)
    out = rig.round("deploy", DEPLOY_STUB_VERIFY="1", **_verdict(head))
    assert out.returncode == 3
    # prod sits AT the pin: the rollout completed — confirm runtime instead.
    out = rig.round("supersede", "--partial", "--reason", "x")
    assert out.returncode == 1
    assert "--confirm-runtime" in out.stderr
    # prod moved PAST the pin: the record is stale — plain supersede retires it.
    _push_branch(rig, "prod", rig.shas["c3"])
    out = rig.round("supersede", "--partial", "--reason", "x")
    assert out.returncode == 1
    assert "plain `scripts/round supersede`" in out.stderr
    assert _rel(_round_json(rig), 1)["state"] == "deployed"


def test_supersede_partial_refuses_on_open_terminal_or_confirmed_record(
    rig: Rig,
) -> None:
    head = _cut(rig)
    out = rig.round("supersede", "r1", "--partial", "--reason", "x")
    assert out.returncode == 1
    assert "still open" in out.stderr
    # terminal: a record --partial already retired is refused again.
    _journal_deploy(rig, head)
    assert rig.round("supersede", "--partial", "--reason", "x").returncode == 0
    out = rig.round("supersede", "r1", "--partial", "--reason", "x")
    assert out.returncode == 1
    assert "nothing to supersede" in out.stderr
    # runtime-confirmed: the deploy path finished it; nothing to retire.
    assert rig.round("cut").returncode == 0
    _journal_deploy(rig, rig.shas["c3"], n=2)
    assert (
        _deploy(rig, DEPLOY_STUB_VERIFY="1", **_verdict(rig.shas["c3"])).returncode == 0
    )
    out = rig.round("supersede", "r2", "--partial", "--reason", "x")
    assert out.returncode == 1
    assert "nothing to supersede" in out.stderr


def test_supersede_partial_refuses_when_prod_unreadable(rig: Rig) -> None:
    _deploying(rig)
    _git(rig.root, "remote", "set-url", "origin", str(rig.root / "nonexistent.git"))
    out = rig.round("supersede", "--partial", "--reason", "x")
    assert out.returncode == 1
    assert "could not read origin/prod" in out.stderr
    assert _rel(_round_json(rig), 1)["state"] == "deploying"


def test_supersede_partial_dry_run_writes_nothing(rig: Rig) -> None:
    head = _deploying(rig)
    path = rig.root / ".git/precis-round/round.json"
    before = path.read_bytes()
    out = rig.round("supersede", "--partial", "--reason", "x", "--dry-run")
    assert out.returncode == 0, out.stderr
    assert "dry-run: would supersede r1" in out.stdout
    assert "dry-run: would delete release/r1 on origin" in out.stdout
    assert path.read_bytes() == before
    assert rig.origin_ref("release/r1") == head


def test_cut_accepts_a_partial_superseded_record_with_its_journal(rig: Rig) -> None:
    """After --partial retires a partial rollout the next cut pipelines:
    main contains the pin with a differing tree, so r<N+1> mints at main
    while the superseded record keeps its journal. Both readers accept it."""
    _deploying(rig)
    out = rig.round("supersede", "--partial", "--reason", "rollout stalled")
    assert out.returncode == 0, out.stderr
    cut = rig.round("cut")
    assert cut.returncode == 0, cut.stderr
    assert rig.origin_ref("release/r2") == rig.shas["c3"]
    rel1 = _rel(_round_json(rig), 1)
    assert rel1["state"] == "superseded"
    assert rel1["deployment"]["sha"] == rig.shas["c2"]
    assert _rel(_round_json(rig), 2)["state"] == "cut"
    status = rig.round("status", "--json")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["next_n"] == 3
    reader = load_script(
        "round_state_partial", REPO_ROOT / "scripts" / "lib" / "round_state.py"
    )
    data = reader.read_round(rig.root / ".git" / "precis-round" / "round.json")
    rels = {r["n"]: r for r in data["releases"]}
    assert rels[1]["state"] == "superseded" and rels[1]["deployment"]
    assert rels[2]["state"] == "cut"


def test_supersede_partial_prints_the_unforwarded_commits(rig: Rig) -> None:
    """A partial rollout's release-branch fixes are unforwarded: after
    --partial the operator must merge them into main by hand before the
    next cut — the verb says so and names them (output only)."""
    _deploying(rig)
    fix = _child(rig, rig.shas["c2"], "release fix not yet on main")
    _push_branch(rig, "release/r1", fix)
    out = rig.round("supersede", "--partial", "--reason", "x")
    assert out.returncode == 0, out.stderr
    assert "forward" in out.stdout and "by hand" in out.stdout
    assert fix[:7] in out.stdout
    assert "release fix not yet on main" in out.stdout


def test_supersede_partial_prints_nothing_when_main_covers_the_branch(
    rig: Rig,
) -> None:
    _deploying(rig)
    out = rig.round("supersede", "--partial", "--reason", "x")
    assert out.returncode == 0, out.stderr
    assert "forward" not in out.stdout


# ── state_at (td574370): every transition stamps the record's clock ──────


def test_state_at_marks_every_transition(rig: Rig) -> None:
    head = _cut(rig)
    cut_at = _rel(_round_json(rig), 1)["state_at"]
    assert str(cut_at).endswith("Z")
    _journal_deploy(rig, head)
    assert _rel(_round_json(rig), 1)["state"] == "deploying"
    assert str(_rel(_round_json(rig), 1)["state_at"]).endswith("Z")
    out = rig.round("deploy", DEPLOY_STUB_VERIFY="1", **_verdict(head))
    assert out.returncode == 3
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "deployed" and str(rel["state_at"]).endswith("Z")
    out = rig.round(
        "deploy",
        "--confirm-runtime",
        head,
        "--runtime-evidence",
        "fake runtime evidence",
    )
    assert out.returncode == 0, out.stderr
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "confirmed" and str(rel["state_at"]).endswith("Z")


def test_state_at_on_superseded_and_abandoned(rig: Rig) -> None:
    head = _cut(rig)
    _journal_deploy(rig, head)
    assert rig.round("supersede", "--partial", "--reason", "x").returncode == 0
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "superseded" and str(rel["state_at"]).endswith("Z")
    assert rig.round("cut").returncode == 0
    assert rig.round("cut", "--abandon").returncode == 0
    rel = _rel(_round_json(rig), 2)
    assert rel["state"] == "abandoned" and str(rel["state_at"]).endswith("Z")


def test_lost_cut_race_restores_state_and_state_at(rig: Rig) -> None:
    """The lease loser's revert restores the open release's state AND its
    state_at — an in-memory supersede must not leak a fresh clock."""
    _cut(rig)
    path = rig.root / ".git/precis-round/round.json"
    cur = _round_json(rig)
    _rel(cur)["state_at"] = "2026-01-01T00:00:00Z"
    path.write_text(json.dumps(cur), encoding="utf-8")
    foreign = _child(rig, rig.shas["c0"], "foreign winner")
    mark = rig.origin.parent / "hook-once"
    _hook(
        rig,
        f'[ -f "{mark}" ] && exit 0\n'
        f'touch "{mark}"\n'
        "env -u GIT_DIR -u GIT_OBJECT_DIRECTORY"
        " -u GIT_ALTERNATE_OBJECT_DIRECTORIES -u GIT_QUARANTINE_PATH"
        f' git -C "{rig.root}" push -q origin'
        f" {foreign}:refs/heads/release/r2 >&2 || true\n"
        "exit 1\n",
    )
    out = rig.round("cut", "--supersede")
    assert out.returncode == 1
    assert "lost the cut race" in out.stderr
    rel = _rel(_round_json(rig), 1)
    assert rel["state"] == "cut"
    assert rel["state_at"] == "2026-01-01T00:00:00Z"
    assert "superseded_by" not in rel and "superseded_why" not in rel


# ── good/r<N> (gr517170) ──────────────────────────────────────────────────
# A deployed release earns an immutable good/r<N> tag after ~24h of clean
# runtime; the newest one is the rollback target. The verb enforces the
# 24h floor from the round receipt's deploy stamp.


def _good_tag(rig: Rig, n: int) -> str:
    cp = subprocess.run(
        [
            "git",
            "-C",
            str(rig.origin),
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/tags/good/r{n}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return cp.stdout.strip()


def _deployed_tag(rig: Rig, n: int, sha: str) -> None:
    _git(rig.root, "push", "-q", "origin", f"{sha}:refs/tags/deployed/r{n}")


def _good_receipt(rig: Rig, n: int, sha: str, hours_ago: float) -> None:
    at = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours_ago * 3600)
    )
    path = rig.root / ".git" / "precis-round" / "receipts" / f"r{n}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "n": n,
                "base": "",
                "deployed": {
                    "sha": sha,
                    "n": n,
                    "tag": f"deployed/r{n}",
                    "branch": f"release/r{n}",
                    "at": at,
                },
            }
        ),
        encoding="utf-8",
    )


def test_good_marks_the_newest_deployed_tag_after_24h(rig: Rig) -> None:
    _deployed_tag(rig, 1, rig.shas["c2"])
    _deployed_tag(rig, 2, rig.shas["c3"])
    _good_receipt(rig, 2, rig.shas["c3"], hours_ago=30)
    out = rig.round("good")
    assert out.returncode == 0, out.stderr
    assert _good_tag(rig, 2) == rig.shas["c3"]
    assert _good_tag(rig, 1) == ""
    # The dry-run and the resolve/status surfaces name the mark.
    out = rig.round("good", "--dry-run")
    assert out.returncode == 0 and "already marks" in out.stdout
    out = rig.round("good", "--resolve")
    assert out.returncode == 0
    assert out.stdout.split() == ["good/r2", rig.shas["c3"]]
    assert "good tag: good/r2" in rig.round("status").stdout


def test_good_refuses_under_24h_and_without_a_deploy_stamp(rig: Rig) -> None:
    _deployed_tag(rig, 1, rig.shas["c2"])
    _good_receipt(rig, 1, rig.shas["c2"], hours_ago=2)
    out = rig.round("good")
    assert out.returncode == 1
    assert "only" in out.stderr and "24h" in out.stderr
    assert _good_tag(rig, 1) == ""
    # No receipt: the verb cannot prove runtime and refuses, naming --at.
    (rig.root / ".git" / "precis-round" / "receipts" / "r1.json").unlink()
    out = rig.round("good")
    assert out.returncode == 1 and "--at" in out.stderr
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 26 * 3600))
    out = rig.round("good", "--at", at)
    assert out.returncode == 0, out.stderr
    assert _good_tag(rig, 1) == rig.shas["c2"]


def test_good_tag_is_immutable_and_an_unknown_release_refuses(rig: Rig) -> None:
    _deployed_tag(rig, 1, rig.shas["c2"])
    _good_receipt(rig, 1, rig.shas["c2"], hours_ago=48)
    assert rig.round("good").returncode == 0
    out = rig.round("good")
    assert out.returncode == 0 and "already marks" in out.stdout
    out = rig.round("good", "--n", "9")
    assert out.returncode == 1 and "no deployed/r9" in out.stderr


def test_good_resolve_with_no_good_tag(rig: Rig) -> None:
    out = rig.round("good", "--resolve")
    assert out.returncode == 0 and "no good/r" in out.stdout


# ── release.round publish + status owner (td573429) ────────────────


class _Prod:
    """Fake ``scripts/prod-precis`` transport for the round record: records
    calls, serves a fixed row for ``settings get``, fails on demand."""

    def __init__(
        self, row: dict[str, Any] | None = None, *, fail: bool = False
    ) -> None:
        self.row = row
        self.fail = fail
        self.calls: list[list[str]] = []
        self.timeouts: list[float | None] = []

    def __call__(self, args: list[str], **kw: Any) -> tuple[int, str, str]:
        self.calls.append(args)
        self.timeouts.append(kw.get("timeout"))
        if self.fail:
            return 1, "", "refused: https://example.invalid/x boom"
        if args[:2] == ["settings", "get"]:
            if self.row is None:
                return 0, f"{args[2]} = None (layer=default)\n", ""
            return 0, f"{args[2]} = {json.dumps(self.row)} (layer=db)\n", ""
        if args[:2] == ["settings", "set"]:
            self.row = json.loads(args[3])
            return 0, "", ""
        if args[:2] == ["settings", "clear"]:
            self.row = None
            return 0, "", ""
        raise AssertionError(f"unexpected transport args {args}")


def _published(rig: Rig, monkeypatch: pytest.MonkeyPatch, prod: Any) -> ModuleType:
    """``scripts/round`` in-process with prod faked: the rig's
    PRECIS_ROUND_ROOT skip reason is lifted and PRECIS_NODE gives
    owner.host a stable fake."""
    mod = _load_round(rig, monkeypatch)
    monkeypatch.setattr(mod, "_prod_precis", prod)
    monkeypatch.setattr(mod, "_record_skip_reason", lambda: None)
    monkeypatch.setenv("PRECIS_NODE", "fake-node")
    return mod


def _round_data(rig: Rig, *recs: dict[str, object]) -> dict[str, Any]:
    return {"n": 1, "open": True, "releases": list(recs)}


def _db_row(
    rig: Rig, *, host: str, checkout: str, state: str = "cut"
) -> dict[str, Any]:
    return {
        "n": 1,
        "branch": "release/r1",
        "base": rig.shas["c2"],
        "state": state,
        "owner": {"host": host, "checkout": checkout},
        "cut_at": "2026-11-06T11:00:00Z",
    }


def _status_json(mod: ModuleType, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    rc = mod.cmd_status(mod._state_dir(), mod.argparse.Namespace(json=True))
    assert rc == 0
    out = capsys.readouterr().out
    payload: dict[str, Any] = json.loads(out[out.index("{") :])
    return payload


def test_round_json_write_publishes_the_release_record(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    prod = _Prod()
    mod = _published(rig, monkeypatch, prod)
    for key, val in _fresh(rig).items():
        monkeypatch.setenv(key, val)
    assert mod.main(["open"]) == 0
    assert prod.calls == []  # no release recorded: nothing to publish
    assert mod.main(["cut"]) == 0
    sets = [c for c in prod.calls if c[:2] == ["settings", "set"]]
    assert len(sets) == 1  # several round.json writes: one publish, at the end
    row = json.loads(sets[-1][3])
    assert row["n"] == 1
    assert row["branch"] == "release/r1"
    assert row["base"] == rig.shas["c2"]
    assert row["state"] == "cut"
    assert row["owner"] == {"host": "fake-node", "checkout": str(rig.root)}


def test_unchanged_record_publishes_once_even_seconds_apart(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    prod = _Prod()
    mod = _published(rig, monkeypatch, prod)
    data = _round_data(rig, _record(1, rig.shas["c2"], "cut"))
    mod._publish_round(data)
    time.sleep(1.05)
    mod._publish_round(data)
    assert [c[:2] for c in prod.calls] == [["settings", "set"]]


def test_publish_failure_reports_once_then_stops(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prod = _Prod(fail=True)
    mod = _published(rig, monkeypatch, prod)
    data = _round_data(rig, _record(1, rig.shas["c2"], "cut"))
    mod._publish_round(data)
    mod._publish_round(data)
    assert len(prod.calls) == 1  # _publish_dead: no retry for the process
    err = capsys.readouterr().err
    assert "round record not published (exit 1):" in err
    assert "https://example.invalid" not in err


def test_raising_transport_reports_once_then_stops(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []

    def boom(args: list[str]) -> tuple[int, str, str]:
        calls.append(args)
        raise subprocess.TimeoutExpired(["prod-precis"], 15)

    mod = _published(rig, monkeypatch, boom)
    data = _round_data(rig, _record(1, rig.shas["c2"], "cut"))
    mod._publish_round(data)
    mod._publish_round(data)
    assert len(calls) == 1
    assert "round record not published: TimeoutExpired" in capsys.readouterr().err


def test_a_malformed_record_only_prints_a_line(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prod = _Prod()
    mod = _published(rig, monkeypatch, prod)
    data = {"n": 1, "open": True, "releases": [{"n": 1, "state": "cut"}]}
    mod._publish_round(data)  # must not raise
    assert prod.calls == []
    assert "round record not published: KeyError" in capsys.readouterr().err


def test_a_verb_publishes_its_final_write_once(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    prod = _Prod()
    mod = _published(rig, monkeypatch, prod)
    path = rig.root / ".git" / "precis-round" / "round.json"
    mod._write(path, _round_data(rig, _record(1, rig.shas["c2"], "cut")))
    mod._write(path, _round_data(rig, _record(1, rig.shas["c2"], "gating")))
    assert prod.calls == []  # nothing publishes until the verb ends
    mod._publish_pending()
    assert len(prod.calls) == 1
    assert json.loads(prod.calls[0][3])["state"] == "gating"
    mod._publish_pending()
    assert len(prod.calls) == 1  # the flush consumes its stash


def test_open_and_close_with_no_release_publish_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    prod = _Prod()
    mod = _published(rig, monkeypatch, prod)
    assert mod.main(["open"]) == 0
    assert mod.main(["eta", "later"]) == 0  # a marks write is not a round write
    assert mod.main(["close"]) == 0
    assert prod.calls == []


def test_status_names_a_foreign_record_owner(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    row = _db_row(rig, host="other-node", checkout=str(rig.root.parent / "clone-b"))
    prod = _Prod(row=row)
    mod = _published(rig, monkeypatch, prod)
    assert mod.main(["open"]) == 0
    rc = mod.cmd_status(mod._state_dir(), mod.argparse.Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert (
        f"record owner: other-node {rig.root.parent / 'clone-b'} says release/r1 [cut]"
        in out
    )
    assert prod.timeouts == [mod.STATUS_TIMEOUT_S]


def test_status_json_skips_the_record_read(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    row = _db_row(rig, host="other-node", checkout=str(rig.root.parent / "clone-b"))
    prod = _Prod(row=row)
    mod = _published(rig, monkeypatch, prod)
    assert mod.main(["open"]) == 0
    payload = _status_json(mod, capsys)
    assert "db_owner" not in payload
    assert prod.calls == []  # the machine path pays no prod round trip


def test_status_omits_the_record_owner_when_this_clone_owns_it(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prod = _Prod(row=_db_row(rig, host="fake-node", checkout=str(rig.root)))
    mod = _published(rig, monkeypatch, prod)
    assert mod.main(["open"]) == 0
    rc = mod.cmd_status(mod._state_dir(), mod.argparse.Namespace(json=False))
    assert rc == 0
    assert "record owner" not in capsys.readouterr().out


def test_status_names_the_owner_on_a_closed_round(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    row = _db_row(rig, host="other-node", checkout=str(rig.root.parent / "clone-b"))
    prod = _Prod(row=row)
    mod = _published(rig, monkeypatch, prod)
    assert mod.main(["open"]) == 0
    assert mod.main(["close"]) == 0
    rc = mod.cmd_status(mod._state_dir(), mod.argparse.Namespace(json=False))
    assert rc == 0
    assert "record owner: other-node" in capsys.readouterr().out


def test_status_ignores_a_terminal_record(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    row = _db_row(
        rig,
        host="other-node",
        checkout=str(rig.root.parent / "clone-b"),
        state="confirmed",
    )
    prod = _Prod(row=row)
    mod = _published(rig, monkeypatch, prod)
    assert mod.main(["open"]) == 0
    rc = mod.cmd_status(mod._state_dir(), mod.argparse.Namespace(json=False))
    assert rc == 0
    assert "record owner" not in capsys.readouterr().out


def test_rig_run_never_contacts_the_transport(rig: Rig, tmp_path: Path) -> None:
    marker = tmp_path / "contacted"
    stub = tmp_path / "fake-prod-cli"
    stub.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 0\n', encoding="utf-8")
    stub.chmod(0o755)
    env = {"PRECIS_GRIPE_NOTE_CLI": str(stub)}
    assert rig.round("open", **env).returncode == 0
    assert rig.round("cut", **env, **_fresh(rig)).returncode == 0
    assert rig.round("status", **env).returncode == 0
    assert not marker.exists()


def test_a_skipped_transport_prints_the_reason_once(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``_record_skip_reason`` set (this rig: the script copy's parent is
    not ``PRECIS_ROUND_ROOT``): one stderr line, then the process stops
    trying — the publish is best-effort, never silent (gr575362)."""
    mod = _load_round(rig, monkeypatch)
    reason = mod._record_skip_reason()
    assert reason is not None
    data = _round_data(rig, _record(1, rig.shas["c2"], "cut"))
    mod._publish_round(data)
    mod._publish_round(data)
    err = capsys.readouterr().err
    assert err.count("round record not published:") == 1
    assert reason in err


# ── soak debt gate (me580555, review round 1) ────────────────────


def _soak_rows(*legs: tuple[str, str, str], run: str = "4242") -> str:
    """4-field check-run rows (name/status/conclusion/url), as the
    paginated ``--jq`` read emits them."""
    url = f"https://github.com/o/r/actions/runs/{run}/job/9"
    return "".join(f"{n}\t{s}\t{c}\t{url}\n" for n, s, c in legs)


def _soak_row(sha: str, run_id: str = "4242", **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "sha": sha,
        "run_id": run_id,
        "jobs": ["soak (3.13, 2)"],
        "at": "2026-11-06T12:00:00Z",
    }
    row.update(kw)
    return row


def test_soak_legs_paginates_past_a_full_first_page(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sha carrying a gate run (~23 rows) plus a nightly can push the
    soak legs onto page 2 — the read must keep paging while a page
    returns 100 rows, and `classify` sees the late red leg."""
    mod = _load_round(rig, monkeypatch)
    pages: list[int] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        page = int(next(a[5:] for a in argv if a.startswith("page=")))
        pages.append(page)
        body = "".join(
            f"job{i}\tcompleted\tsuccess\thttps://o/r/actions/runs/1/job/{i}\n"
            for i in range(100)
        )
        if page == 2:
            body = (
                "soak (3.13, 1)\tcompleted\tfailure\t"
                "https://o/r/actions/runs/4242/job/9\n"
            )
        return subprocess.CompletedProcess(argv, 0, body, "")

    rows = mod._soak()["soak_legs"](fake_run, "0" * 40, rig.root)
    assert pages == [1, 2]
    assert rows is not None and len(rows) == 101
    assert mod._soak()["classify"](rows) == "red"
    assert mod._soak()["red_jobs"](rows) == (["soak (3.13, 1)"], "4242")


def test_soak_debt_check_refuses_on_a_red_soak_and_caches_the_row(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed red `soak*` leg on the last gated main sha is the debt
    — the refusal names sha/run/jobs and the soak-clear way out, and the
    read is cached into release.soak_red."""
    prod = _Prod()
    mod = _published(rig, monkeypatch, prod)
    monkeypatch.setenv("LGM_SHA", rig.shas["c2"])
    (rig.gh_dir / rig.shas["c2"]).write_text(
        _soak_rows(
            ("soak (3.13, 1)", "completed", "success"),
            ("soak (3.13, 2)", "completed", "failure"),
            ("verdict-soak", "completed", "failure"),
        ),
        encoding="utf-8",
    )
    block = mod._soak_debt_check(rig.root)
    assert rig.shas["c2"][:9] in block
    assert "run 4242" in block
    assert "soak (3.13, 2)" in block
    assert f"soak-clear {rig.shas['c2'][:12]} --reason" in block
    assert prod.row is not None
    assert prod.row["sha"] == rig.shas["c2"]
    assert prod.row["run_id"] == "4242"
    assert prod.row["jobs"] == ["soak (3.13, 2)", "verdict-soak"]
    assert "waived_by" not in prod.row


def test_a_waived_row_lets_the_check_pass_with_a_note(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`soak-clear` amended the cached row — the same sha's still-red soak
    is then a one-line waiver note, not a veto."""
    prod = _Prod(
        row=_soak_row(
            rig.shas["c2"],
            waived_by="peer-x",
            waived_at="2026-11-06T13:00:00Z",
            reason="known flaky leg",
        )
    )
    mod = _published(rig, monkeypatch, prod)
    monkeypatch.setenv("LGM_SHA", rig.shas["c2"])
    (rig.gh_dir / rig.shas["c2"]).write_text(
        _soak_rows(("soak (3.13, 1)", "completed", "failure")),
        encoding="utf-8",
    )
    assert mod._soak_debt_check(rig.root) == ""
    assert (
        f"soak waived: {rig.shas['c2'][:9]} by peer-x "
        f"at 2026-11-06T13:00:00Z: known flaky leg" in capsys.readouterr().out
    )


def test_a_green_soak_read_clears_the_cached_debt_row(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A later read that finds every `soak*` leg completed green resolves
    the debt in place — the row is cleared and the cut goes on."""
    prod = _Prod(row=_soak_row(rig.shas["c2"]))
    mod = _published(rig, monkeypatch, prod)
    monkeypatch.setenv("LGM_SHA", rig.shas["c2"])
    (rig.gh_dir / rig.shas["c2"]).write_text(
        _soak_rows(
            ("soak (3.13, 1)", "completed", "success"),
            ("soak (3.13, 2)", "completed", "success"),
            ("verdict-soak", "completed", "success"),
        ),
        encoding="utf-8",
    )
    assert mod._soak_debt_check(rig.root) == ""
    out = capsys.readouterr().out
    assert f"soak debt on {rig.shas['c2'][:9]} resolved by a green soak" in out
    assert "row cleared" in out
    assert prod.calls[-1] == ["settings", "clear", mod.SOAK_RED_KEY]
    assert prod.row is None


def test_a_still_running_soak_neither_refuses_nor_clears(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An in-flight soak is a note, not a veto — and it must not clear
    recorded debt either (the verdict is not in yet)."""
    prod = _Prod(row=_soak_row(rig.shas["c2"]))
    mod = _published(rig, monkeypatch, prod)
    monkeypatch.setenv("LGM_SHA", rig.shas["c2"])
    (rig.gh_dir / rig.shas["c2"]).write_text(
        _soak_rows(("soak (3.13, 1)", "in_progress", "")),
        encoding="utf-8",
    )
    assert mod._soak_debt_check(rig.root) == ""
    assert "soak still running" in capsys.readouterr().out
    assert prod.row is not None  # the cached debt row survives
    assert ["settings", "clear"] not in [c[:2] for c in prod.calls]


def test_no_last_gated_sha_skips_the_read(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prod = _Prod(row=_soak_row(rig.shas["c2"]))
    mod = _published(rig, monkeypatch, prod)
    # LGM_SHA unset: the stub answers empty — nothing to read a verdict on.
    assert mod._soak_debt_check(rig.root) == ""
    assert "no last-gated main sha" in capsys.readouterr().out
    assert prod.row is not None  # an unanswerable read never clears


def test_a_verdict_soak_only_failure_is_the_red_soak(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `verdict-soak` row alone (no `soak (PY, K)` legs — the matrix was
    renamed under an old run) is judged by its own conclusion: red is the
    debt, cached like any red soak."""
    prod = _Prod()
    mod = _published(rig, monkeypatch, prod)
    monkeypatch.setenv("LGM_SHA", rig.shas["c2"])
    (rig.gh_dir / rig.shas["c2"]).write_text(
        _soak_rows(("verdict-soak", "completed", "failure")),
        encoding="utf-8",
    )
    block = mod._soak_debt_check(rig.root)
    assert "soak red on last gated main" in block
    assert "verdict-soak" in block
    assert prod.row is not None
    assert prod.row["jobs"] == ["verdict-soak"]


def test_a_verdict_soak_only_success_reads_green(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A green `verdict-soak` alone clears cached debt — it is the soak's
    own verdict."""
    prod = _Prod(row=_soak_row(rig.shas["c2"]))
    mod = _published(rig, monkeypatch, prod)
    monkeypatch.setenv("LGM_SHA", rig.shas["c2"])
    (rig.gh_dir / rig.shas["c2"]).write_text(
        _soak_rows(("verdict-soak", "completed", "success")),
        encoding="utf-8",
    )
    assert mod._soak_debt_check(rig.root) == ""
    assert prod.calls[-1] == ["settings", "clear", mod.SOAK_RED_KEY]
    assert prod.row is None


def test_a_skipped_verdict_soak_means_no_soak_ran(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A skipped `verdict-soak` (the job did not run — a shape without the
    soak matrix) is neither a refusal nor a clear: 'none'."""
    prod = _Prod(row=_soak_row(rig.shas["c2"]))
    mod = _published(rig, monkeypatch, prod)
    monkeypatch.setenv("LGM_SHA", rig.shas["c2"])
    (rig.gh_dir / rig.shas["c2"]).write_text(
        _soak_rows(("verdict-soak", "completed", "skipped")),
        encoding="utf-8",
    )
    assert mod._soak_debt_check(rig.root) == ""
    assert "no soak legs" in capsys.readouterr().out
    assert prod.row is not None
    assert ["settings", "clear"] not in [c[:2] for c in prod.calls]


def test_cut_refuses_on_a_soak_that_went_red_after_the_verdict(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real order: the blocking verdict went green while the soak was
    still running, the soak turned red afterwards, and only now does
    `cut` read it — refuse and cache the debt row."""
    prod = _Prod()
    mod = _published(rig, monkeypatch, prod)
    for key, val in _fresh(rig).items():
        monkeypatch.setenv(key, val)
    # gate-fast verdict green; the soak legs are still in flight.
    (rig.gh_dir / rig.shas["c2"]).write_text(
        _soak_rows(("soak (3.13, 1)", "in_progress", "")),
        encoding="utf-8",
    )
    assert mod.main(["open"]) == 0
    # ... the soak finishes red afterwards.
    (rig.gh_dir / rig.shas["c2"]).write_text(
        _soak_rows(
            ("soak (3.13, 1)", "completed", "failure"),
            ("verdict-soak", "completed", "failure"),
        ),
        encoding="utf-8",
    )
    assert mod.main(["cut"]) == 1
    err = capsys.readouterr().err
    assert "cut refused — soak red on last gated main" in err
    assert "soak-clear" in err
    assert prod.row is not None and prod.row["sha"] == rig.shas["c2"]


def test_open_also_refuses_on_a_red_soak(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prod = _Prod()
    mod = _published(rig, monkeypatch, prod)
    monkeypatch.setenv("LGM_SHA", rig.shas["c2"])
    (rig.gh_dir / rig.shas["c2"]).write_text(
        _soak_rows(("soak (3.13, 1)", "completed", "failure")),
        encoding="utf-8",
    )
    assert mod.main(["open"]) == 1
    assert "open refused — soak red" in capsys.readouterr().err


def test_open_prints_the_waiver_line_on_a_waived_red_soak(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`open` shares `_soak_debt_check` with `cut`: a waived red soak is
    the printed waiver note, not a refusal — pin it on `open` too."""
    prod = _Prod(
        row=_soak_row(
            rig.shas["c2"],
            waived_by="peer-x",
            waived_at="2026-11-06T13:00:00Z",
            reason="known flaky leg",
        )
    )
    mod = _published(rig, monkeypatch, prod)
    monkeypatch.setenv("LGM_SHA", rig.shas["c2"])
    (rig.gh_dir / rig.shas["c2"]).write_text(
        _soak_rows(("soak (3.13, 1)", "completed", "failure")),
        encoding="utf-8",
    )
    assert mod.main(["open"]) == 0
    assert (
        f"soak waived: {rig.shas['c2'][:9]} by peer-x "
        f"at 2026-11-06T13:00:00Z: known flaky leg" in capsys.readouterr().out
    )


def test_soak_legs_pages_through_the_gh_stub(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gh stub sees `-f page=N`, not only fake_run: a full page 1
    (junk rows) pushes the red leg to page 2 and the debt read still
    finds it through the real stub on PATH."""
    prod = _Prod()
    mod = _published(rig, monkeypatch, prod)
    monkeypatch.setenv("LGM_SHA", rig.shas["c2"])
    (rig.gh_dir / f"{rig.shas['c2']}.page1").write_text(
        "".join(
            f"job{i}\tcompleted\tsuccess\thttps://o/r/actions/runs/1/job/{i}\n"
            for i in range(100)
        ),
        encoding="utf-8",
    )
    (rig.gh_dir / f"{rig.shas['c2']}.page2").write_text(
        _soak_rows(("soak (3.13, 1)", "completed", "failure")),
        encoding="utf-8",
    )
    block = mod._soak_debt_check(rig.root)
    assert "soak red on last gated main" in block
    assert "soak (3.13, 1)" in block


def test_a_waived_debt_lets_the_cut_through(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end: red soak → cut refuses → soak-clear records the waiver
    → cut proceeds (the same sha's red verdict is waived, not cleared)."""
    prod = _Prod()
    mod = _published(rig, monkeypatch, prod)
    for key, val in _fresh(rig).items():
        monkeypatch.setenv(key, val)
    # open while the soak is still in flight — it turns red afterwards,
    # the real order (a completed red soak refuses `open` too).
    (rig.gh_dir / rig.shas["c2"]).write_text(
        _soak_rows(("soak (3.13, 1)", "in_progress", "")),
        encoding="utf-8",
    )
    assert mod.main(["open"]) == 0
    (rig.gh_dir / rig.shas["c2"]).write_text(
        _soak_rows(("soak (3.13, 1)", "completed", "failure")),
        encoding="utf-8",
    )
    assert mod.main(["cut"]) == 1
    assert mod.main(["soak-clear", rig.shas["c2"][:12], "known", "flake"]) == 0
    assert prod.row is not None
    assert prod.row["waived_by"] == rig.root.name
    assert prod.row["reason"] == "known flake"
    waived_at = prod.row["waived_at"]  # the cut republishes release.round
    assert mod.main(["cut"]) == 0
    out = capsys.readouterr().out
    assert (
        f"soak waived: {rig.shas['c2'][:9]} by {rig.root.name} "
        f"at {waived_at}: known flake" in out
    )


def test_soak_clear_records_who_and_why(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prod = _Prod(row=_soak_row(rig.shas["c1"]))
    mod = _published(rig, monkeypatch, prod)
    assert mod.main(["soak-clear", rig.shas["c1"][:12], "--reason", "flaky shard"]) == 0
    out = capsys.readouterr().out
    assert (
        f"release.soak_red cleared (was {rig.shas['c1'][:12]}) "
        f"by {rig.root.name}: flaky shard" in out
    )
    assert prod.row is not None
    assert prod.row["waived_by"] == rig.root.name
    assert prod.row["reason"] == "flaky shard"
    assert prod.row["waived_at"]
    # The waiver is also journaled into the round soak log.
    log = rig.root / ".git" / "precis-round" / "soak.log"
    assert (
        f"soak waived: {rig.shas['c1'][:9]} by {rig.root.name} "
        f"at {prod.row['waived_at']}: flaky shard" in log.read_text(encoding="utf-8")
    )


def test_soak_clear_without_a_reason_is_refused(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The waiver IS the audit trail for a refused cut — "cleared, no
    idea why" is refused, and nothing is written."""
    prod = _Prod(row=_soak_row(rig.shas["c1"]))
    mod = _published(rig, monkeypatch, prod)
    assert mod.main(["soak-clear", rig.shas["c1"][:12]]) == 1
    err = capsys.readouterr().err
    assert "soak-clear refused" in err
    assert "--reason" in err
    assert "waived_by" not in (prod.row or {})
    assert ["settings", "set"] not in [c[:2] for c in prod.calls]


def test_soak_clear_with_a_blank_reason_is_refused(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prod = _Prod(row=_soak_row(rig.shas["c1"]))
    mod = _published(rig, monkeypatch, prod)
    assert mod.main(["soak-clear", rig.shas["c1"][:12], "--reason", "  ", " "]) == 1
    assert "--reason" in capsys.readouterr().err
    assert "waived_by" not in (prod.row or {})


def test_soak_clear_with_no_row_records_a_waiver(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No cached row (the red read could not write it, prod was down)
    must not dead-end the waiver: the named sha resolves and a fresh row
    carries who/why — otherwise a refusal has no way out."""
    prod = _Prod(row=None)
    mod = _published(rig, monkeypatch, prod)
    assert mod.main(["soak-clear", rig.shas["c1"], "--reason", "prod was down"]) == 0
    assert prod.row is not None
    assert prod.row["sha"] == rig.shas["c1"]
    assert prod.row["run_id"] == ""
    assert prod.row["jobs"] == []
    assert prod.row["waived_by"] == rig.root.name
    assert prod.row["reason"] == "prod was down"


def test_soak_clear_with_no_row_refuses_an_unresolvable_sha(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    prod = _Prod(row=None)
    mod = _published(rig, monkeypatch, prod)
    assert mod.main(["soak-clear", "0" * 40, "--reason", "why"]) == 1
    assert prod.row is None


def test_a_waiver_names_one_sha_not_another(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waiver is for ONE sha: a red soak on a different last-gated sha
    is fresh debt, not waived."""
    prod = _Prod(
        row=_soak_row(
            rig.shas["c1"],
            waived_by="peer-x",
            waived_at="2026-11-06T13:00:00Z",
            reason="known flaky leg",
        )
    )
    mod = _published(rig, monkeypatch, prod)
    monkeypatch.setenv("LGM_SHA", rig.shas["c2"])
    (rig.gh_dir / rig.shas["c2"]).write_text(
        _soak_rows(("soak (3.13, 1)", "completed", "failure")),
        encoding="utf-8",
    )
    block = mod._soak_debt_check(rig.root)
    assert f"soak red on last gated main {rig.shas['c2'][:9]}" in block
    assert prod.row is not None and prod.row["sha"] == rig.shas["c2"]


def test_an_unreadable_soak_read_leaves_a_log_line(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A gh failure proceeds (an outage must not wedge every cut) but is
    loud: stderr and a soak.log line — a red hidden behind the failure
    leaves a trace."""
    prod = _Prod()
    mod = _published(rig, monkeypatch, prod)
    monkeypatch.setenv("LGM_SHA", rig.shas["c2"])
    # No canned rows for the sha: the gh stub exits 1.
    assert mod._soak_debt_check(rig.root) == ""
    err = capsys.readouterr().err
    assert f"soak verdict on {rig.shas['c2'][:9]} unreadable" in err
    log = rig.root / ".git" / "precis-round" / "soak.log"
    assert "unreadable" in log.read_text(encoding="utf-8")


def test_soak_clear_wrong_prefix_refuses_and_keeps_the_row(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Waiving must name the recorded sha — a different prefix refuses so
    one sha's debt is never waived silently for another's."""
    prod = _Prod(row=_soak_row(rig.shas["c1"]))
    mod = _published(rig, monkeypatch, prod)
    wrong = ("0" if rig.shas["c1"][0] != "0" else "1") * 12
    assert mod.main(["soak-clear", wrong, "--reason", "why"]) == 1
    err = capsys.readouterr().err
    assert f"release.soak_red names {rig.shas['c1'][:12]}, not {wrong}" in err
    assert "waived_by" not in (prod.row or {})
    assert ["settings", "set"] not in [c[:2] for c in prod.calls]
    assert ["settings", "clear"] not in [c[:2] for c in prod.calls]


def test_soak_clear_with_no_row_and_no_reason_is_refused(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prod = _Prod(row=None)
    mod = _published(rig, monkeypatch, prod)
    assert mod.main(["soak-clear", rig.shas["c1"][:12]]) == 1
    assert "--reason" in capsys.readouterr().err


def test_red_verdict_counts_gate_fast_and_ignores_soak(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """me580555: `gate-fast` rows are verdict legs like test-linux (a
    completed failed one makes the sha red); `soak*` rows never do."""
    mod = _load_round(rig, monkeypatch)
    sha = rig.shas["c2"]
    url = "https://github.com/o/r/actions/runs/4242/job/9"
    (rig.gh_dir / sha).write_text(
        f"lint\tcompleted\tsuccess\t{url}\n"
        f"gate-fast (3.13, 1)\tcompleted\tfailure\t{url}\n"
        f"soak (3.13, 1)\tcompleted\tfailure\t{url}\n",
        encoding="utf-8",
    )
    looked, red = mod._red_verdict(rig.root, sha)
    assert looked
    assert red is not None
    assert red["jobs"] == ["gate-fast (3.13, 1)"]
    assert red["run_id"] == "4242"
    (rig.gh_dir / sha).write_text(
        f"lint\tcompleted\tsuccess\t{url}\n"
        f"soak (3.13, 1)\tcompleted\tfailure\t{url}\n"
        f"verdict-soak\tcompleted\tfailure\t{url}\n",
        encoding="utf-8",
    )
    looked, red = mod._red_verdict(rig.root, sha)
    assert looked and red is None

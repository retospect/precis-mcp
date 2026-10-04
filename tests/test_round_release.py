"""Frozen release gate/deploy: real bare origin, fake CI and deploy only."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pytest

if sys.platform != "win32":
    import fcntl

from tests.test_round_gate import (
    Rig,
    _child,
    _fresh,
    _git,
    _open_round,
    _push_branch,
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
        sha = _round_json(rig)["release"]["deployment"]["sha"]
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
    (rig.root / "lib/round_state.py").write_text(
        (script.parent / "lib/round_state.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    loader = SourceFileLoader("round_script", str(copy))
    spec = importlib.util.spec_from_loader("round_script", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
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
    old = _round_json(rig)["release"]["deployment"]["runtime_receipt"]
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
    journal = _round_json(rig)["release"]["deployment"]
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
    assert _round_json(rig)["release"]["deployment"]["runtime_confirmed_at"]
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
    assert "release" in _round_json(rig)
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
    assert "release" not in _round_json(rig)
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
    old = _round_json(rig)["release"]["deployment"]["runtime_receipt"]
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
        cur["release"]["deployment"]["superseded_runtime"] = []
        path.write_text(json.dumps(cur), encoding="utf-8")
    out = rig.round(
        "deploy",
        "--confirm-runtime",
        head,
        "--runtime-evidence",
        "fake fresh observation",
    )
    assert out.returncode == 1
    assert "release" in _round_json(rig)
    assert "runtime_confirmed_at" not in _round_json(rig)["release"]["deployment"]
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
    del cur["release"]
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
    assert "release" not in _round_json(rig)
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
    assert not _round_json(rig)["release"]["deployment"].get("runtime_confirmed_at")
    assert rig.round("cut", "--abandon").returncode == 1
    assert rig.round("close").returncode == 1
    retry = _deploy(rig, DEPLOY_STUB_VERIFY="1", **_verdict(head))
    assert retry.returncode == 0, retry.stderr


def test_release_failed_deploy_retains_pin_and_returns_exit_code(rig: Rig) -> None:
    head = _cut(rig)
    out = _deploy(rig, DEPLOY_STUB_RC="7", **_verdict(head))
    assert out.returncode == 7
    assert _round_json(rig)["release"]["deployment"]["sha"] == head
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
    assert "deployment" not in _round_json(rig)["release"]


def test_release_head_race_during_rollout_retains_pin_without_tag(rig: Rig) -> None:
    head = _cut(rig)
    newer = _child(rig, head, "external release arrival")
    out = _deploy(
        rig,
        DEPLOY_STUB_VERIFY="1",
        DEPLOY_STUB_HOOK=f"git push -q origin {newer}:refs/heads/release/r1",
        **_verdict(head),
    )
    assert out.returncode == 1
    assert "changed during rollout" in out.stderr
    assert rig.origin_ref("release/r1") == newer
    assert _round_json(rig)["release"]["deployment"]["sha"] == head
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
    assert _round_json(rig)["release"]["deployment"]["runtime_confirmed_at"]
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
    assert "release" not in _round_json(rig)


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
    assert _round_json(rig)["release"]["deployment"]["runtime_confirmed_at"]
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
    assert _round_json(rig)["release"]["deployment"]["sha"] == head
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
    assert not _round_json(rig)["release"]["deployment"].get("runtime_confirmed_at")
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
    assert not _round_json(rig)["release"]["deployment"].get("rollout_at")


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
    cur["release"]["cut_pending"] = True
    state.write_text(json.dumps(cur), encoding="utf-8")
    assert rig.round("deploy", **_verdict(head)).returncode == 1
    out = rig.round("cut", **_fresh(rig, "c3"))
    assert out.returncode == 0, out.stderr
    assert "pending cut reconciled" in out.stdout
    assert "cut_pending" not in _round_json(rig)["release"]
    assert rig.origin_ref("release/r1") == head


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

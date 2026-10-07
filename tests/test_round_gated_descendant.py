"""Reviewed maintenance controller with a frozen payload: fake origin/deploy only."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.test_round_gate import Rig, _child, _git, _push_branch, _round_json
from tests.test_round_gate import rig as rig
from tests.test_round_release import _cut, _deploy, _load_round, _verdict

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX release tooling")


@pytest.mark.parametrize("gated", ["absent", "ancestor", "equal", "descendant"])
def test_certified_release_preserves_monotone_gated_and_exact_payload(
    rig: Rig, gated: str
) -> None:
    head = _cut(rig)
    prior = {
        "absent": "",
        "ancestor": rig.shas["c0"],
        "equal": head,
        "descendant": rig.shas["c3"],
    }[gated]
    if prior:
        _push_branch(rig, "gated", prior)
    _push_branch(rig, "prod", rig.shas["c0"])
    out = rig.round("deploy", DEPLOY_STUB_VERIFY="1", **_verdict(head))
    assert out.returncode == 3, out.stderr
    expected = prior if gated == "descendant" else head
    assert rig.origin_ref("gated") == expected
    assert rig.origin_ref("prod") == head
    assert rig.origin_ref("release/r1") == head
    assert rig.deploy_log() == [f"{head} --pinned"]
    cur = _round_json(rig)
    assert (
        cur["release"]["deployment"]["ci"]
        == json.loads(_verdict(head)["LGM_EXACT"])["certificate"]
    )
    journal = cur["release"]["deployment"]
    assert journal["gated_before"] == prior
    assert journal["gated_after"] == expected
    assert journal["gated_action"] == ("retain" if gated == "descendant" else "publish")
    assert "runtime_confirmed_at" not in journal
    out = _deploy(rig, DEPLOY_STUB_VERIFY="1", **_verdict(head))
    assert out.returncode == 0, out.stderr
    assert rig.origin_ref("gated") == expected
    assert rig.origin_ref("prod") == head
    assert rig.origin_ref("release/r1") == ""
    assert _round_json(rig)["deployed"]["sha"] == head
    assert rig.deploy_log() == [f"{head} --pinned"]


def test_descendant_gated_dry_run_retains_ref_without_journal(rig: Rig) -> None:
    head = _cut(rig)
    _push_branch(rig, "gated", rig.shas["c3"])
    before = _round_json(rig)
    out = rig.round("deploy", "--dry-run", **_verdict(head))
    assert out.returncode == 0, out.stderr
    assert "retain origin/gated" in out.stdout
    assert _round_json(rig) == before
    assert rig.deploy_log() == []
    assert rig.origin_ref("gated") == rig.shas["c3"]


@pytest.mark.parametrize("invocation_cwd", ["controller", "payload"])
def test_split_controller_targets_frozen_scripts_refs_pin_and_state(
    rig: Rig, tmp_path: Path, invocation_cwd: str
) -> None:
    head = _cut(rig)
    _push_branch(rig, "gated", rig.shas["c3"])
    repo = Path(__file__).resolve().parents[1]
    controller = tmp_path / "maintenance-controller"
    _git(tmp_path, "init", "-q", "-b", "maintenance", str(controller))
    cwd = controller if invocation_cwd == "controller" else rig.root
    (controller / "scripts/lib").mkdir(parents=True)
    shutil.copy(repo / "scripts/round", controller / "scripts/round")
    shutil.copy(repo / "scripts/lib/round_state.py", controller / "scripts/lib")
    for name in ("deploy", "last-gated-main-sha"):
        poison = controller / "scripts" / name
        poison.write_text(
            "#!/bin/sh\necho wrong-controller-payload >&2\nexit 93\n", encoding="utf-8"
        )
        poison.chmod(0o755)
    (controller / ".ship-sha").write_text(rig.shas["c3"], encoding="utf-8")
    (rig.root / "scripts/round").write_text(
        "raise RuntimeError('wrong frozen controller')\n", encoding="utf-8"
    )
    for name, log in (
        ("deploy", "deploy-cwd.log"),
        ("last-gated-main-sha", "ci-cwd.log"),
    ):
        script = rig.root / "scripts" / name
        script.write_text(
            script.read_text(encoding="utf-8").replace(
                "#!/usr/bin/env bash\n",
                f"""#!/usr/bin/env bash
printf '%s | %s\\n' "$PWD" "$*" >> {log}
""",
                1,
            ),
            encoding="utf-8",
        )
    target_files = [p for p in (rig.root / "scripts").rglob("*") if p.is_file()]
    before = {p: p.read_bytes() for p in target_files}
    env = dict(rig.env, DEPLOY_STUB_VERIFY="1", **_verdict(head))
    command = [sys.executable, str(controller / "scripts/round"), "deploy"]
    # Both helper selection and pin checking must come from the frozen target.
    pin = rig.root / ".ship-sha"
    pin.write_text(rig.shas["c3"], encoding="utf-8")
    refused = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, check=False
    )
    assert refused.returncode == 1
    assert ".ship-sha here pins" in refused.stderr
    assert "deployment" not in _round_json(rig)["release"]
    assert rig.deploy_log() == []
    pin.write_text(head, encoding="utf-8")
    out = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, check=False
    )
    assert out.returncode == 3, out.stderr
    assert rig.deploy_log() == [f"{head} --pinned"]
    assert (rig.root / "deploy-cwd.log").read_text(
        encoding="utf-8"
    ).strip() == f"{rig.root} | {head} --pinned"
    ci_calls = (rig.root / "ci-cwd.log").read_text(encoding="utf-8").splitlines()
    assert all(line == f"{rig.root} | --exact {head}" for line in ci_calls)
    assert rig.origin_ref("gated") == rig.shas["c3"]
    assert rig.origin_ref("prod") == head
    assert _round_json(rig)["release"]["deployment"]["sha"] == head
    assert not (controller / ".git/precis-round").exists()
    assert not (controller / "deploy.log").exists()
    confirmed = subprocess.run(
        command
        + [
            "--confirm-runtime",
            head,
            "--runtime-evidence",
            "fake target daemon+MCP boot SHA/readiness observed",
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert confirmed.returncode == 0, confirmed.stderr
    assert rig.deploy_log() == [f"{head} --pinned"]
    assert _round_json(rig)["deployed"]["sha"] == head
    assert _round_json(rig)["deployed"]["confirmed_by"] == cwd.name
    assert not (controller / ".git/precis-round").exists()
    assert (rig.root / ".git/precis-round/receipts/r1.json").is_file()
    assert rig.origin_ref("release/r1") == ""
    assert rig.origin_ref("gated") == rig.shas["c3"]
    assert {p: p.read_bytes() for p in target_files} == before


@pytest.mark.parametrize("relation", ["divergent", "outside_main"])
def test_release_rejects_incompatible_gated_before_journal(
    rig: Rig, relation: str
) -> None:
    head = _cut(rig)
    parent = rig.shas["c0"] if relation == "divergent" else rig.shas["c3"]
    other = _child(rig, parent, relation)
    _push_branch(rig, "gated", other)
    out = rig.round("deploy", **_verdict(head))
    assert out.returncode == 1
    assert "gated moves fast-forward only" in out.stderr
    assert "deployment" not in _round_json(rig)["release"]
    assert rig.origin_ref("gated") == other
    assert rig.deploy_log() == []


def test_descendant_gated_is_release_only(rig: Rig) -> None:
    _push_branch(rig, "gated", rig.shas["c3"])
    out = rig.round("deploy", LGM_SHA=rig.shas["c2"], LGM_AGE="1")
    assert out.returncode == 1
    assert "gated moves fast-forward only" in out.stderr
    assert rig.origin_ref("gated") == rig.shas["c3"]
    assert rig.deploy_log() == []


@pytest.mark.parametrize("age", [None, 7, -1, float("nan")])
def test_descendant_gated_does_not_relax_freshness(rig: Rig, age: float | None) -> None:
    head = _cut(rig)
    _push_branch(rig, "gated", rig.shas["c3"])
    out = rig.round("deploy", **_verdict(head, age))
    assert out.returncode == 1
    assert "deployment" not in _round_json(rig)["release"]
    assert rig.deploy_log() == []


@pytest.mark.parametrize(
    "bad", ["missing", "sha", "workflow", "run_id", "attempt", "not_green"]
)
def test_descendant_gated_requires_exact_certificate(rig: Rig, bad: str) -> None:
    head = _cut(rig)
    _push_branch(rig, "gated", rig.shas["c3"])
    verdict = json.loads(_verdict(head)["LGM_EXACT"])
    if bad == "missing":
        del verdict["certificate"]
    elif bad == "not_green":
        verdict.update(candidate="", candidate_state="release_not_green")
    else:
        verdict["certificate"][bad] = {
            "sha": rig.shas["c3"],
            "workflow": "other.yml",
        }.get(bad, 0)
    out = rig.round("deploy", LGM_EXACT=json.dumps(verdict))
    assert out.returncode == 1
    assert "deployment" not in _round_json(rig)["release"]
    assert rig.deploy_log() == []


def test_descendant_gated_cannot_authorize_prod_rollback(rig: Rig) -> None:
    head = _cut(rig)
    _push_branch(rig, "gated", rig.shas["c3"])
    _push_branch(rig, "prod", rig.shas["c3"])
    out = rig.round("deploy", **_verdict(head))
    assert out.returncode == 1
    assert "prod moves fast-forward only" in out.stderr
    assert "deployment" not in _round_json(rig)["release"]
    assert rig.deploy_log() == []


def test_descendant_gated_release_head_race_still_refuses(rig: Rig) -> None:
    head = _cut(rig)
    _push_branch(rig, "gated", rig.shas["c3"])
    out = rig.round(
        "deploy",
        LGM_EXACT_HOOK=f"git push -q origin {rig.shas['c3']}:refs/heads/release/r1",
        **_verdict(head),
    )
    assert out.returncode == 1
    assert "moved during the CI lookup" in out.stderr
    assert "deployment" not in _round_json(rig)["release"]
    assert rig.deploy_log() == []


def test_descendant_gated_uses_full_dag_not_first_parent(rig: Rig) -> None:
    head = _cut(rig)
    fix = _child(rig, head, "release second parent")
    merged = _git(
        rig.root,
        "commit-tree",
        f"{rig.shas['c3']}^{{tree}}",
        "-p",
        rig.shas["c3"],
        "-p",
        fix,
        "-m",
        "main contains release via second parent",
    )
    _push_branch(rig, "release/r1", fix)
    _push_branch(rig, "main", merged)
    _push_branch(rig, "gated", merged)
    out = _deploy(rig, DEPLOY_STUB_VERIFY="1", **_verdict(fix))
    assert out.returncode == 0, out.stderr
    assert rig.origin_ref("gated") == rig.origin_ref("main") == merged
    assert rig.origin_ref("prod") == fix
    assert rig.deploy_log() == [f"{fix} --pinned"]


@pytest.mark.parametrize("stage", [1, 2, 3])
@pytest.mark.parametrize("ref", ["main", "gated", "prod"])
def test_unreadable_release_pointers_never_use_stale_tracking_refs(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, stage: int, ref: str
) -> None:
    head = _cut(rig)
    _push_branch(rig, "gated", rig.shas["c3"])
    mod = _load_round(rig, monkeypatch)
    monkeypatch.setenv("LGM_EXACT", _verdict(head)["LGM_EXACT"])
    original = mod._remote
    seen = 0

    def unreadable(root: Path, name: str) -> str | None:
        nonlocal seen
        if name == f"refs/heads/{ref}":
            seen += 1
            if seen == stage:
                return None
        return original(root, name)

    monkeypatch.setattr(mod, "_remote", unreadable)
    assert mod.main(["deploy"]) == 1
    assert seen == stage
    assert rig.deploy_log() == []
    assert rig.origin_ref("gated") == rig.shas["c3"]
    if stage < 3:
        assert "deployment" not in _round_json(rig)["release"]


@pytest.mark.parametrize("stage", [2, 3])
@pytest.mark.parametrize(
    "change",
    [
        "gated_diverges",
        "gated_outside_main",
        "prod_ahead",
        "main_loses_gated",
        "gated_deleted",
    ],
)
def test_release_rechecks_pointer_races_before_install(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, stage: int, change: str
) -> None:
    head = _cut(rig)
    _push_branch(rig, "gated", rig.shas["c3"])
    other = (
        _child(rig, rig.shas["c0"], "divergent race")
        if change == "gated_diverges"
        else _child(rig, rig.shas["c3"], "descendant outside main")
    )
    # Publish the race commit object into the bare test origin, without moving a guarded pointer yet.
    _push_branch(rig, "race-fixture", other)
    mod = _load_round(rig, monkeypatch)
    monkeypatch.setenv("LGM_EXACT", _verdict(head)["LGM_EXACT"])
    original = mod._remote
    seen = 0

    def race(root: Path, name: str) -> str | None:
        nonlocal seen
        if name == "refs/heads/main":
            seen += 1
            if seen == stage:
                if change.startswith("gated_"):
                    if change == "gated_deleted":
                        _git(rig.origin, "update-ref", "-d", "refs/heads/gated")
                    else:
                        _git(rig.origin, "update-ref", "refs/heads/gated", other)
                elif change == "prod_ahead":
                    _git(rig.origin, "update-ref", "refs/heads/prod", rig.shas["c3"])
                else:
                    _git(rig.origin, "update-ref", "refs/heads/main", head)
        return original(root, name)

    monkeypatch.setattr(mod, "_remote", race)
    result = mod.main(["deploy"])
    assert result == 1
    assert rig.deploy_log() == []
    if change == "gated_deleted":
        assert rig.origin_ref("gated") == ""  # no controller recreation at an older SHA


def test_rejected_gated_push_never_turns_into_descendant_retention(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _cut(rig)
    _push_branch(rig, "gated", rig.shas["c0"])
    mod = _load_round(rig, monkeypatch)
    monkeypatch.setenv("LGM_EXACT", _verdict(head)["LGM_EXACT"])
    original = mod.subprocess.run
    raced = False

    def race(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal raced
        command = args[0]
        if (
            isinstance(command, list)
            and command[:2] == ["bash", "-c"]
            and "env_pointer_move" in command[2]
        ):
            raced = True
            _push_branch(rig, "gated", rig.shas["c3"])
        return original(*args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", race)
    assert mod.main(["deploy"]) == 1
    assert raced
    assert rig.origin_ref("gated") == rig.shas["c3"]
    assert rig.deploy_log() == []


def test_descendant_retention_honors_disabled_environment_pointers(rig: Rig) -> None:
    head = _cut(rig)
    _push_branch(rig, "gated", rig.shas["c3"])
    out = rig.round("deploy", PRECIS_ENV_POINTERS="0", **_verdict(head))
    assert out.returncode == 1
    assert "disabled" in out.stderr
    assert rig.deploy_log() == []


@pytest.mark.parametrize("stage", [2, 3])
@pytest.mark.parametrize("prior", ["ancestor", "descendant"])
def test_compatible_gated_advance_is_rechecked_and_preserved(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, stage: int, prior: str
) -> None:
    head = _cut(rig)
    initial = rig.shas["c0"] if prior == "ancestor" else rig.shas["c3"]
    _push_branch(rig, "gated", initial)
    newer = _child(rig, rig.shas["c3"], "compatible newer main")
    _push_branch(rig, "race-fixture", newer)
    mod = _load_round(rig, monkeypatch)
    monkeypatch.setenv("LGM_EXACT", _verdict(head)["LGM_EXACT"])
    monkeypatch.setenv("DEPLOY_STUB_VERIFY", "1")
    original = mod._remote
    seen = 0

    def race(root: Path, name: str) -> str | None:
        nonlocal seen
        if name == "refs/heads/main":
            seen += 1
            if seen == stage:
                _git(rig.origin, "update-ref", "refs/heads/main", newer)
                _git(rig.origin, "update-ref", "refs/heads/gated", newer)
        return original(root, name)

    monkeypatch.setattr(mod, "_remote", race)
    assert mod.main(["deploy"]) == 3
    assert rig.deploy_log() == [f"{head} --pinned"]
    assert rig.origin_ref("gated") == newer
    assert rig.origin_ref("prod") == head
    journal = _round_json(rig)["release"]["deployment"]
    assert journal["gated_after"] == newer
    assert "runtime_confirmed_at" not in journal


@pytest.mark.parametrize("ref", ["main", "gated", "prod"])
def test_successful_fetch_must_contain_the_exact_observed_pointer(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, ref: str
) -> None:
    head = _cut(rig)
    _push_branch(rig, "gated", rig.shas["c3"])
    mod = _load_round(rig, monkeypatch)
    monkeypatch.setenv("LGM_EXACT", _verdict(head)["LGM_EXACT"])
    original_remote, original_git = mod._remote, mod._rgit

    def remote(root: Path, name: str) -> str | None:
        return "f" * 40 if name == f"refs/heads/{ref}" else original_remote(root, name)

    def fetch_other_head(root: Path, *args: str) -> str | None:
        if args == ("fetch", "-q", "origin", ref):
            return ""  # success after the branch advanced; observed object remains unreadable
        return original_git(root, *args)

    monkeypatch.setattr(mod, "_remote", remote)
    monkeypatch.setattr(mod, "_rgit", fetch_other_head)
    assert mod.main(["deploy"]) == 1
    assert "deployment" not in _round_json(rig)["release"]
    assert rig.deploy_log() == []


@pytest.mark.parametrize("stage", [2, 3])
@pytest.mark.parametrize("regression", ["deleted", "candidate", "earlier_descendant"])
def test_authoritative_gated_observation_cannot_be_forgotten(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, stage: int, regression: str
) -> None:
    head = _cut(rig)
    earlier = rig.shas["c3"]
    observed = _child(rig, earlier, "newest authoritative gated")
    _push_branch(rig, "main", observed)
    _push_branch(rig, "gated", observed)
    mod = _load_round(rig, monkeypatch)
    monkeypatch.setenv("LGM_EXACT", _verdict(head)["LGM_EXACT"])
    monkeypatch.setenv("DEPLOY_STUB_VERIFY", "1")
    original = mod._remote
    seen = 0
    regressed = (
        ""
        if regression == "deleted"
        else head
        if regression == "candidate"
        else earlier
    )

    def race(root: Path, name: str) -> str | None:
        nonlocal seen
        if name == "refs/heads/main":
            seen += 1
            if seen == stage:
                if regressed:
                    _git(rig.origin, "update-ref", "refs/heads/gated", regressed)
                else:
                    _git(rig.origin, "update-ref", "-d", "refs/heads/gated")
        return original(root, name)

    monkeypatch.setattr(mod, "_remote", race)
    assert mod.main(["deploy"]) == 1
    assert seen == stage
    assert rig.origin_ref("gated") == regressed
    assert rig.deploy_log() == []
    if stage == 2:
        assert "deployment" not in _round_json(rig)["release"]
    else:
        assert _round_json(rig)["release"]["deployment"]["gated_before"] == observed


@pytest.mark.parametrize("initial_remote", ["absent", "ancestor"])
def test_cached_gated_never_establishes_authoritative_lower_bound(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, initial_remote: str
) -> None:
    head = _cut(rig)
    _git(rig.root, "update-ref", "refs/remotes/origin/gated", rig.shas["c3"])
    if initial_remote == "ancestor":
        _push_branch(rig, "gated", rig.shas["c0"])
    # Preserve a known-stale tracking ref through first discovery.
    _git(rig.root, "update-ref", "refs/remotes/origin/gated", rig.shas["c3"])
    mod = _load_round(rig, monkeypatch)
    monkeypatch.setenv("LGM_EXACT", _verdict(head)["LGM_EXACT"])
    monkeypatch.setenv("DEPLOY_STUB_VERIFY", "1")
    monkeypatch.setattr(mod, "_fetch", lambda root: None)
    assert mod.main(["deploy"]) == 3
    assert rig.origin_ref("gated") == head
    assert rig.deploy_log() == [f"{head} --pinned"]
    expected_before = "" if initial_remote == "absent" else rig.shas["c0"]
    assert _round_json(rig)["release"]["deployment"]["gated_before"] == expected_before


@pytest.mark.parametrize("journal_source", ["before", "after"])
@pytest.mark.parametrize(
    "regression", ["deleted", "candidate", "earlier_descendant", "compatible_forward"]
)
def test_unfinished_install_journal_retains_authoritative_gated_evidence(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, journal_source: str, regression: str
) -> None:
    head = _cut(rig)
    earlier = rig.shas["c3"]
    observed = _child(rig, earlier, "persisted newest gate")
    _push_branch(rig, "main", observed)
    if journal_source == "before":
        _push_branch(rig, "gated", observed)
        first = rig.round("deploy", PRECIS_ENV_POINTERS="0", **_verdict(head))
        assert first.returncode == 1
        assert rig.deploy_log() == []
    else:
        _push_branch(rig, "gated", earlier)
        mod = _load_round(rig, monkeypatch)
        monkeypatch.setenv("LGM_EXACT", _verdict(head)["LGM_EXACT"])
        monkeypatch.setenv("DEPLOY_STUB_RC", "7")
        original = mod._remote
        seen = 0

        def advance(root: Path, name: str) -> str | None:
            nonlocal seen
            if name == "refs/heads/main":
                seen += 1
                if seen == 3:
                    _git(rig.origin, "update-ref", "refs/heads/gated", observed)
            return original(root, name)

        monkeypatch.setattr(mod, "_remote", advance)
        assert mod.main(["deploy"]) == 7
        assert rig.deploy_log() == [f"{head} --pinned"]
    journal = _round_json(rig)["release"]["deployment"]
    assert journal[f"gated_{journal_source}"] == observed
    before_log = rig.deploy_log()
    if regression == "compatible_forward":
        current = _child(rig, observed, "journal resume compatible forward")
        _push_branch(rig, "main", current)
        _push_branch(rig, "gated", current)
    elif regression == "deleted":
        current = ""
        _git(rig.origin, "update-ref", "-d", "refs/heads/gated")
    else:
        current = head if regression == "candidate" else earlier
        _git(rig.origin, "update-ref", "refs/heads/gated", current)
    retry = rig.round("deploy", DEPLOY_STUB_VERIFY="1", **_verdict(head))
    if regression == "compatible_forward":
        assert retry.returncode == 3, retry.stderr
        assert rig.deploy_log() == before_log + [f"{head} --pinned"]
    else:
        assert retry.returncode == 1
        assert rig.deploy_log() == before_log
        assert _round_json(rig)["release"]["deployment"] == journal
    assert rig.origin_ref("gated") == current


@pytest.mark.parametrize("stage", [2, 3])
def test_incomparable_gates_contained_in_main_still_violate_observed_bound(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, stage: int
) -> None:
    head = _cut(rig)
    observed = _child(rig, rig.shas["c3"], "observed branch gate")
    other = _child(rig, rig.shas["c3"], "incomparable branch gate")
    main = _git(
        rig.root,
        "commit-tree",
        f"{observed}^{{tree}}",
        "-p",
        observed,
        "-p",
        other,
        "-m",
        "main contains both gates",
    )
    _push_branch(rig, "main", main)
    _push_branch(rig, "gated", observed)
    mod = _load_round(rig, monkeypatch)
    monkeypatch.setenv("LGM_EXACT", _verdict(head)["LGM_EXACT"])
    original = mod._remote
    seen = 0

    def race(root: Path, name: str) -> str | None:
        nonlocal seen
        if name == "refs/heads/main":
            seen += 1
            if seen == stage:
                _git(rig.origin, "update-ref", "refs/heads/gated", other)
        return original(root, name)

    monkeypatch.setattr(mod, "_remote", race)
    assert mod.main(["deploy"]) == 1
    assert seen == stage
    assert rig.origin_ref("gated") == other
    assert rig.deploy_log() == []


def test_older_journal_after_observation_cannot_erase_stronger_before_bound(
    rig: Rig,
) -> None:
    head = _cut(rig)
    observed = rig.shas["c3"]
    _push_branch(rig, "gated", observed)
    first = rig.round("deploy", DEPLOY_STUB_RC="7", **_verdict(head))
    assert first.returncode == 7
    cur = _round_json(rig)
    assert cur["release"]["deployment"]["gated_before"] == observed
    # Reproduce a historical1017076 journal that accepted a later backward observation.
    cur["release"]["deployment"]["gated_after"] = head
    (rig.root / ".git/precis-round/round.json").write_text(
        json.dumps(cur), encoding="utf-8"
    )
    _git(rig.origin, "update-ref", "refs/heads/gated", head)
    retry = rig.round("deploy", **_verdict(head))
    assert retry.returncode == 1
    assert rig.deploy_log() == [f"{head} --pinned"]
    assert _round_json(rig) == cur
    assert rig.origin_ref("gated") == head

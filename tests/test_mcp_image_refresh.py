"""scripts/mcp-image-refresh — version compare, install decision, dry-run plan.

docker, git and every subprocess are mocked; nothing here touches a container
or the live install.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests._script import load_script

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: loads a shebang'd script"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PIN = "29039071c77a245aee4c0bd66d17ae03ed885061"
LOCK = (
    '[[package]]\nname = "other"\nversion = "1"\nsource = { registry = "x" }\n\n'
    '[[package]]\nname = "autocatpath"\nversion = "0.30.0"\n'
    f'source = {{ git = "https://github.com/retospect/catpath?rev={PIN}#{PIN}" }}\n'
    "dependencies = [\n]\n"
)


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture()
def mod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    m = load_script("mcp_image_refresh", REPO_ROOT / "scripts" / "mcp-image-refresh")
    (tmp_path / "uv.lock").write_text(LOCK, encoding="utf-8")
    repo = tmp_path / "repo-ensure.sh"
    repo.write_text("new\n", encoding="utf-8")
    monkeypatch.setattr(m, "ROOT", tmp_path)
    monkeypatch.setattr(m, "REPO_ENSURE", repo)
    # git history: the repo has shipped "old\n" and "new\n" at some point
    monkeypatch.setattr(m, "history_hashes", lambda *_a: {_h("old\n"), _h("new\n")})
    monkeypatch.setattr(m, "repo_copy_clean", lambda *_a: True)
    monkeypatch.setenv("PRECIS_MCP_ENSURE", str(tmp_path / "inst-ensure.sh"))
    return m


def _installed(mod: ModuleType, text: str | None) -> Path:
    inst = mod.installed_ensure()
    if text is not None:
        inst.write_text(text, encoding="utf-8")
    return inst


def _labels(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, rev: str, sha: str = "a" * 40
) -> None:
    monkeypatch.setattr(
        mod, "image_label", lambda _i, k: rev if k == mod.REV_LABEL else sha
    )


def _stale_then_built(mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    # stale label, sha label, then the label after the build carries the pin
    revs = iter(["deadbeef", "", PIN])
    monkeypatch.setattr(mod, "image_label", lambda _i, _k: next(revs))


def _no_run(mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake(cmd: list[str]) -> int:
        calls.append(cmd)
        return 0

    monkeypatch.setattr(mod, "run", fake)
    return calls


def test_locked_sha_reads_autocatpath_stanza(mod: ModuleType) -> None:
    assert mod.locked_sha(mod.ROOT / "uv.lock") == PIN


def test_real_lock_has_a_pin() -> None:
    m = load_script("mir2", REPO_ROOT / "scripts" / "mcp-image-refresh")
    assert len(m.locked_sha(REPO_ROOT / "uv.lock")) >= 7


def test_image_label_uses_narrow_format(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    def fake(cmd: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="abc\n", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake)
    assert mod.image_label("precis-mcp:dev", "precis.autocatpath_rev") == "abc"
    assert seen[0][:4] == ["docker", "image", "inspect", "--format"]
    assert "Config.Labels" in seen[0][4] and "Env" not in seen[0][4]


@pytest.mark.parametrize("out,rc", [("<no value>\n", 0), ("", 0), ("x", 1)])
def test_image_label_missing_is_empty(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, out: str, rc: int
) -> None:
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda cmd, **_k: subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=""),
    )
    assert mod.image_label("i", "k") == ""


def test_history_hashes_reads_every_revision_via_git(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs = {"r1": b"one\n", "r2": b"two\n"}

    def fake(cmd: list[str], **_k: object) -> subprocess.CompletedProcess[bytes]:
        if cmd[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="r2\nr1\nr0\n", stderr="")  # type: ignore[arg-type]
        rev = cmd[2].split(":")[0]
        if rev in blobs:
            return subprocess.CompletedProcess(cmd, 0, stdout=blobs[rev], stderr=b"")
        return subprocess.CompletedProcess(cmd, 128, stdout=b"", stderr=b"gone")

    m = load_script("mir3", REPO_ROOT / "scripts" / "mcp-image-refresh")
    monkeypatch.setattr(m.subprocess, "run", fake)
    assert m.history_hashes() == {_h("one\n"), _h("two\n")}


def test_noop_when_image_current_and_script_identical(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _labels(mod, monkeypatch, PIN)
    _installed(mod, "new\n")
    calls = _no_run(mod, monkeypatch)
    assert mod.main([]) == 0
    assert "up to date" in capsys.readouterr().out
    assert calls == []


def test_repo_revision_diff_auto_applies_then_recreates(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stale_then_built(mod, monkeypatch)
    inst = _installed(mod, "old\n")  # an older repo revision: no local edits
    calls = _no_run(mod, monkeypatch)
    monkeypatch.setattr(mod, "wait_serving", lambda *_a, **_k: True)
    assert mod.main([]) == 0
    assert calls == [
        ["scripts/build-image", "precis-dev"],
        ["docker", "tag", "precis-dev", "precis-mcp:dev"],
        ["scripts/mcp-http-install", "--apply"],
        [str(inst), "--recreate"],
    ]


def test_image_current_but_script_older_still_installs_and_recreates(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _labels(mod, monkeypatch, PIN)
    inst = _installed(mod, "old\n")
    calls = _no_run(mod, monkeypatch)
    monkeypatch.setattr(mod, "wait_serving", lambda *_a, **_k: True)
    assert mod.main([]) == 0
    assert calls == [["scripts/mcp-http-install", "--apply"], [str(inst), "--recreate"]]


def test_in_sync_stale_image_does_not_apply(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stale_then_built(mod, monkeypatch)
    inst = _installed(mod, "new\n")
    calls = _no_run(mod, monkeypatch)
    monkeypatch.setattr(mod, "wait_serving", lambda *_a, **_k: True)
    assert mod.main([]) == 0
    assert ["scripts/mcp-http-install", "--apply"] not in calls
    assert calls[-1] == [str(inst), "--recreate"]


def test_local_edit_refuses_and_changes_nothing(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _labels(mod, monkeypatch, "deadbeef")
    _installed(mod, "SECRET_TOKEN=hunter2\nlocal tweak\n")
    calls = _no_run(mod, monkeypatch)
    assert mod.main([]) == mod.EXIT_DIFF
    assert calls == []  # no build, no install, no recreate
    cap = capsys.readouterr()
    assert "STOP" in cap.err and "local edits" in cap.err
    assert "hunter2" not in cap.out + cap.err  # diff content is never printed


def test_dirty_repo_copy_refuses(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _labels(mod, monkeypatch, "deadbeef")
    _installed(mod, "old\n")
    monkeypatch.setattr(mod, "repo_copy_clean", lambda *_a: False)
    calls = _no_run(mod, monkeypatch)
    assert mod.main([]) == mod.EXIT_DIFF
    assert calls == []


def test_unreadable_git_history_refuses_with_its_own_reason(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _labels(mod, monkeypatch, "deadbeef")
    _installed(mod, "old\n")
    monkeypatch.setattr(mod, "history_hashes", lambda *_a: set())
    calls = _no_run(mod, monkeypatch)
    assert mod.main([]) == mod.EXIT_DIFF
    assert calls == []
    assert "unreadable" in capsys.readouterr().err


def test_installed_edited_during_build_is_not_overwritten(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stale_then_built(mod, monkeypatch)
    inst = _installed(mod, "old\n")
    calls: list[list[str]] = []

    def fake(cmd: list[str]) -> int:
        calls.append(cmd)
        if cmd[0] == "scripts/build-image":
            inst.write_text("edited meanwhile\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(mod, "run", fake)
    assert mod.main([]) == mod.EXIT_DIFF
    assert ["scripts/mcp-http-install", "--apply"] not in calls
    assert all("--recreate" not in c for c in calls)


def test_apply_failure_stops_before_recreate(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stale_then_built(mod, monkeypatch)
    _installed(mod, "old\n")
    calls: list[list[str]] = []

    def fake(cmd: list[str]) -> int:
        calls.append(cmd)
        return 1 if "--apply" in cmd else 0

    monkeypatch.setattr(mod, "run", fake)
    assert mod.main([]) == 1
    assert all("--recreate" not in c for c in calls)


def test_diff_summary_counts_without_content(mod: ModuleType, tmp_path: Path) -> None:
    inst = tmp_path / "i.sh"
    inst.write_text("TOKEN=abc\n", encoding="utf-8")
    s = mod.diff_summary(mod.REPO_ENSURE, inst)
    assert "+1/-1" in s and "abc" not in s


def test_missing_installed_script_refuses(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _labels(mod, monkeypatch, PIN)
    calls = _no_run(mod, monkeypatch)
    assert mod.main([]) == mod.EXIT_DIFF
    assert calls == []


def test_wait_serving_times_out_without_401(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [0.0]
    polls: list[list[str]] = []

    def fake_run(cmd: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
        polls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout="x (no token, via proxy) -> 502\n", stderr=""
        )

    def fake_sleep(s: float) -> None:
        clock[0] += s

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mod.time, "sleep", fake_sleep)
    assert mod.wait_serving(Path("/x/ensure.sh"), timeout=10.0, interval=3.0) is False
    assert 2 <= len(polls) <= 6  # bounded, did not spin forever


def test_unhealthy_after_recreate_fails_with_clear_message(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stale_then_built(mod, monkeypatch)
    _installed(mod, "old\n")
    _no_run(mod, monkeypatch)
    monkeypatch.setattr(mod, "wait_serving", lambda *_a, **_k: False)
    assert mod.main([]) == mod.EXIT_UNHEALTHY
    assert "not serving" in capsys.readouterr().err


def test_dry_run_auto_apply_prints_plan_and_changes_nothing(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _labels(mod, monkeypatch, "")  # unlabelled image = stale
    _installed(mod, "old\n")
    calls = _no_run(mod, monkeypatch)
    assert mod.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "scripts/build-image precis-dev" in out and "--recreate" in out
    assert "mcp-http-install --apply" in out and "AUTO-APPLY" in out
    assert PIN in out and "+1/-1 lines" in out
    assert calls == []


def test_dry_run_local_edit_prints_refusal(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _labels(mod, monkeypatch, "")
    _installed(mod, "edited\n")
    calls = _no_run(mod, monkeypatch)
    assert mod.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "REFUSE" in out and "--recreate" not in out
    assert calls == []


def test_serving_needs_both_probes_401(mod: ModuleType) -> None:
    ok = "x (no token, from the proxy) -> 401  (401 = serving)\ny (no token, via proxy) -> 401  (z)\n"
    assert mod.serving(ok)
    assert not mod.serving(ok.replace("via proxy) -> 401", "via proxy) -> 502"))
    assert not mod.serving("")


def test_build_that_misses_the_pin_does_not_recreate(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _labels(mod, monkeypatch, "deadbeef")  # still stale after the build
    _installed(mod, "old\n")
    calls = _no_run(mod, monkeypatch)
    assert mod.main([]) == 1
    assert all("--recreate" not in c for c in calls)
    assert ["scripts/mcp-http-install", "--apply"] not in calls

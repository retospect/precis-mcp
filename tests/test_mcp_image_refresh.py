"""scripts/mcp-image-refresh — version compare, install decision, dry-run plan.

Docker and live-install operations are mocked. The ancestry tests use a
throwaway Git repository under tmp_path; nothing touches a live container.
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
    monkeypatch.setattr(m, "build_refusal", lambda *_a: "")
    monkeypatch.setenv("PRECIS_MCP_ENSURE", str(tmp_path / "inst-ensure.sh"))
    monkeypatch.setenv("PRECIS_MCP_REPO", str(tmp_path))
    monkeypatch.setenv("PRECIS_MCP_HTTP_STATE", str(tmp_path / "state"))
    monkeypatch.delenv("PRECIS_MCP_SRC", raising=False)
    return m


def _installed(mod: ModuleType, text: str | None) -> Path:
    inst = mod.installed_ensure()
    if text is not None:
        inst.write_text(text, encoding="utf-8")
    return inst


def _labels(
    mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    rev: str,
    sha: str = "a" * 40,
    lock: str = _h(LOCK),
) -> None:
    labels = {mod.REV_LABEL: rev, mod.SHA_LABEL: sha, mod.LOCK_LABEL: lock}
    monkeypatch.setattr(mod, "image_label", lambda _i, k: labels[k])


def _stale_then_built(
    mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rev: str = "deadbeef",
    lock: str = "",
    rebuilt_lock: str = _h(LOCK),
) -> None:
    labels = {
        mod.REV_LABEL: iter([rev, PIN]),
        mod.SHA_LABEL: iter(["a" * 40]),
        mod.LOCK_LABEL: iter([lock, rebuilt_lock]),
    }
    queried: set[str] = set()

    def label(image: str, key: str) -> str:
        assert image == (mod.BUILD_SERVICE if key in queried else mod.IMAGE)
        queried.add(key)
        return next(labels[key])

    monkeypatch.setattr(mod, "image_label", label)


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


def test_canonical_mismatch_refuses_before_any_step(
    mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """gr515239: run from a tree whose main checkout is not the recorded
    canonical one, the script stops before the image check or any run()."""
    state = tmp_path / "state"
    state.mkdir()
    canon = tmp_path / "canon"
    canon.mkdir()
    (state / "main-checkout").write_text(str(canon) + "\n", encoding="utf-8")
    monkeypatch.setenv("PRECIS_MCP_HTTP_STATE", str(state))
    monkeypatch.setattr(
        mod, "own_main_checkout", lambda: (tmp_path / "elsewhere").resolve()
    )
    calls = _no_run(mod, monkeypatch)
    assert mod.main([]) == mod.EXIT_DIFF
    assert calls == []
    err = capsys.readouterr().err
    assert "STOP" in err and "elsewhere" in err


def test_canonical_mismatch_pass_cases(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # No marker yet -> pass: the ensure script owns recording it.
    monkeypatch.setenv("PRECIS_MCP_HTTP_STATE", str(tmp_path / "no-state"))
    assert mod.canonical_mismatch() == ""

    state = tmp_path / "state"
    state.mkdir()
    canon = (tmp_path / "canon").resolve()
    canon.mkdir()
    marker = state / "main-checkout"
    monkeypatch.setenv("PRECIS_MCP_HTTP_STATE", str(state))
    marker.write_text(str(canon) + "\n", encoding="utf-8")

    # Same main checkout (a canonical worktree passes) -> pass.
    monkeypatch.setattr(mod, "own_main_checkout", lambda: canon)
    assert mod.canonical_mismatch() == ""
    # A different checkout -> the refusal reason names both.
    monkeypatch.setattr(
        mod, "own_main_checkout", lambda: (tmp_path / "other").resolve()
    )
    assert "recorded as canonical" in mod.canonical_mismatch()
    # Git cannot answer (a copied-out script) -> pass.
    monkeypatch.setattr(mod, "own_main_checkout", lambda: None)
    assert mod.canonical_mismatch() == ""
    # A marker naming a deleted checkout -> pass; the ensure script
    # re-records it.
    marker.write_text(str(tmp_path / "gone") + "\n", encoding="utf-8")
    monkeypatch.setattr(
        mod, "own_main_checkout", lambda: (tmp_path / "other").resolve()
    )
    assert mod.canonical_mismatch() == ""


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
    assert calls == [["scripts/build-image", "precis-dev"]]


def test_lock_sha256_hashes_exact_bytes(mod: ModuleType, tmp_path: Path) -> None:
    lock = tmp_path / "bytes.lock"
    raw = b"version = 1\r\n# different newline bytes\r\n"
    lock.write_bytes(raw)
    assert mod.lock_sha256(lock) == hashlib.sha256(raw).hexdigest()


def test_same_catpath_pin_but_changed_served_lock_rebuilds(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    served = tmp_path / "served"
    served.mkdir()
    (served / ".git").write_text("gitdir: unused\n", encoding="utf-8")
    changed = LOCK + '\n[[package]]\nname = "added"\nversion = "2"\n'
    (served / "uv.lock").write_text(changed, encoding="utf-8")
    monkeypatch.setenv("PRECIS_MCP_SRC", str(served))
    _stale_then_built(
        mod, monkeypatch, rev=PIN, lock=_h(LOCK), rebuilt_lock=_h(changed)
    )
    inst = _installed(mod, "new\n")
    calls = _no_run(mod, monkeypatch)
    monkeypatch.setattr(mod, "wait_serving", lambda *_a, **_k: True)
    assert mod.main([]) == 0
    assert calls == [
        ["scripts/build-image", "precis-dev"],
        ["docker", "tag", "precis-dev", "precis-mcp:dev"],
        [str(inst), "--recreate"],
    ]


def test_missing_lock_label_is_stale_even_with_current_catpath(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stale_then_built(mod, monkeypatch, rev=PIN, lock="")
    _installed(mod, "new\n")
    calls = _no_run(mod, monkeypatch)
    monkeypatch.setattr(mod, "wait_serving", lambda *_a, **_k: True)
    assert mod.main([]) == 0
    assert calls[0] == ["scripts/build-image", "precis-dev"]


def test_image_current_for_served_pin_ignores_invoker_lock(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    served = tmp_path / "served"
    served.mkdir()
    (served / ".git").write_text("gitdir: unused\n", encoding="utf-8")
    changed = LOCK.replace(PIN, "f" * 40)
    (served / "uv.lock").write_text(changed, encoding="utf-8")
    monkeypatch.setenv("PRECIS_MCP_SRC", str(served))
    _labels(mod, monkeypatch, "f" * 40, lock=_h(changed))
    _installed(mod, "new\n")
    calls = _no_run(mod, monkeypatch)
    assert mod.main([]) == 0
    assert calls == []


def test_build_that_misses_lock_hash_does_not_recreate(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _labels(mod, monkeypatch, PIN, lock="c" * 64)
    _installed(mod, "old\n")
    calls = _no_run(mod, monkeypatch)
    assert mod.main([]) == 1
    assert calls == [["scripts/build-image", "precis-dev"]]


@pytest.mark.parametrize("change_pin", [False, True])
def test_served_lock_is_reread_after_build_before_recreate(
    mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    change_pin: bool,
) -> None:
    changed = LOCK.replace(PIN, "f" * 40) if change_pin else LOCK + "# new\n"
    _stale_then_built(
        mod,
        monkeypatch,
        rebuilt_lock=_h(changed) if change_pin else _h(LOCK),
    )
    _installed(mod, "old\n")
    calls: list[list[str]] = []

    def fake(cmd: list[str]) -> int:
        calls.append(cmd)
        if cmd[0] == "scripts/build-image":
            (mod.ROOT / "uv.lock").write_text(changed, encoding="utf-8")
        return 0

    monkeypatch.setattr(mod, "run", fake)
    assert mod.main([]) == 1
    assert calls == [["scripts/build-image", "precis-dev"]]


@pytest.fixture
def resolver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    m = load_script("mir_resolver", REPO_ROOT / "scripts/mcp-image-refresh")
    monkeypatch.delenv("PRECIS_MCP_REPO", raising=False)
    monkeypatch.delenv("PRECIS_MCP_SRC", raising=False)
    monkeypatch.setenv("PRECIS_MCP_HTTP_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return m


def test_served_repo_default_without_marker_or_prod_clone(
    resolver: ModuleType, tmp_path: Path
) -> None:
    assert resolver.served_repo() == tmp_path / "home/work/project/code/precis-mcp"


def test_served_repo_marker_then_environment_override(
    resolver: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    main = tmp_path / "canonical"
    (state / "main-checkout").write_text(str(main) + "\n", encoding="utf-8")
    assert resolver.served_repo() == main
    other = tmp_path / "override"
    monkeypatch.setenv("PRECIS_MCP_REPO", str(other))
    assert resolver.served_repo() == other


@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("git_marker", ["clone", "worktree", "absent"])
def test_served_repo_accepts_only_git_source_checkout(
    resolver: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: bool,
    git_marker: str,
) -> None:
    main = tmp_path / "main"
    source = tmp_path / ("custom-source" if override else "main-prod")
    source.mkdir()
    monkeypatch.setenv("PRECIS_MCP_REPO", str(main))
    if override:
        monkeypatch.setenv("PRECIS_MCP_SRC", str(source))
    if git_marker == "clone":
        (source / ".git").mkdir()
        (source / ".git/HEAD").write_text("ref: refs/heads/prod\n", encoding="utf-8")
    elif git_marker == "worktree":
        (source / ".git").write_text("gitdir: unused\n", encoding="utf-8")
    assert resolver.served_repo() == (main if git_marker == "absent" else source)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Refresh tests",
            "-c",
            "user.email=refresh@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=True,
    ).stdout.strip()


@pytest.mark.parametrize("ancestry", ["missing", "behind", "equal", "descendant"])
def test_build_refusal_checks_origin_prod_and_names_served_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ancestry: str
) -> None:
    m = load_script("mir_ancestry", REPO_ROOT / "scripts/mcp-image-refresh")
    build = tmp_path / "build"
    served = tmp_path / "served"
    for repo, message in ((build, "build base"), (served, "served revision")):
        repo.mkdir()
        (repo / "uv.lock").write_text(LOCK, encoding="utf-8")
        _git(repo, "init")
        _git(repo, "commit", "--allow-empty", "-m", message)
    base = _git(build, "rev-parse", "HEAD")
    _git(build, "commit", "--allow-empty", "-m", "build successor")
    head = _git(build, "rev-parse", "HEAD")
    if ancestry != "missing":
        _git(
            build,
            "update-ref",
            "refs/remotes/origin/prod",
            base if ancestry == "descendant" else head,
        )
    if ancestry == "behind":
        _git(build, "checkout", "--detach", base)
    monkeypatch.setattr(m, "ROOT", build)
    reason = m.build_refusal(served)
    if ancestry in ("equal", "descendant"):
        assert reason == ""
    else:
        assert "git fetch origin" in reason
        assert _git(served, "rev-parse", "HEAD") in reason
        assert str(served) in reason


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    "reason", ["origin/prod unavailable", "HEAD behind origin/prod"]
)
def test_ancestry_refusal_prevents_build_even_in_dry_run(
    mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
    reason: str,
) -> None:
    _labels(mod, monkeypatch, PIN, lock="")
    _installed(mod, "new\n")
    monkeypatch.setattr(mod, "build_refusal", lambda _served: reason)
    calls = _no_run(mod, monkeypatch)
    assert mod.main(["--dry-run"] if dry_run else []) == mod.EXIT_DIFF
    assert reason in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_different_build_and_served_locks_refuse_before_build(
    mod: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
) -> None:
    """A missing image label must not cause a build from the wrong lock.

    The autocatpath pin is identical, so only comparison of the complete
    build and served lockfiles can identify the mismatch before building.
    """
    served = tmp_path / "served"
    served.mkdir()
    (served / ".git").write_text("gitdir: unused\n", encoding="utf-8")
    changed = LOCK + '\n[[package]]\nname = "added"\nversion = "2"\n'
    (served / "uv.lock").write_text(changed, encoding="utf-8")
    monkeypatch.setenv("PRECIS_MCP_SRC", str(served))
    real = load_script("mir_lock_guard", REPO_ROOT / "scripts/mcp-image-refresh")
    monkeypatch.setattr(real, "ROOT", mod.ROOT)
    monkeypatch.setattr(mod, "build_refusal", real.build_refusal)
    _labels(mod, monkeypatch, PIN, lock="")
    _installed(mod, "new\n")
    calls = _no_run(mod, monkeypatch)
    assert mod.main(["--dry-run"] if dry_run else []) == mod.EXIT_DIFF
    assert calls == []
    err = capsys.readouterr().err
    assert _h(LOCK) in err and _h(changed) in err
    assert "deploy" in err.lower()
    assert "git fetch origin" in err
    assert "rebuild from the served tree" in err


@pytest.mark.parametrize("missing", ["build", "served"])
def test_build_refusal_reports_unreadable_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    real = load_script("mir_missing_lock", REPO_ROOT / "scripts/mcp-image-refresh")
    for name in ("build", "served"):
        directory = tmp_path / name
        directory.mkdir()
        if name != missing:
            (directory / "uv.lock").write_text(LOCK, encoding="utf-8")
    monkeypatch.setattr(real, "ROOT", tmp_path / "build")
    reason = real.build_refusal(tmp_path / "served")
    assert str(tmp_path / missing / "uv.lock") in reason


@pytest.mark.parametrize(
    "rev,lock,wanted,unwanted",
    [
        # Same lock hash, wrong engine: only the rev moved.
        ("deadbeef", _h(LOCK), "autocatpath rev", "uv.lock sha256"),
        # gr529876: same engine pin, a new dependency — only the hash moved.
        (PIN, "f" * 64, "uv.lock sha256", "autocatpath rev"),
        ("deadbeef", "f" * 64, None, None),
    ],
)
def test_stale_report_names_which_part_differs(
    mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rev: str,
    lock: str,
    wanted: str | None,
    unwanted: str | None,
) -> None:
    _labels(mod, monkeypatch, rev, lock=lock)
    _installed(mod, "new\n")
    _no_run(mod, monkeypatch)
    assert mod.main(["--dry-run"]) == 0
    line = next(
        ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("stale:")
    )
    if unwanted is None:
        assert "autocatpath rev" in line and "uv.lock sha256" in line
    else:
        assert wanted is not None
        assert wanted in line and unwanted not in line


def test_force_rebuilds_even_when_labels_match(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _labels(mod, monkeypatch, PIN)
    inst = _installed(mod, "new\n")
    calls = _no_run(mod, monkeypatch)
    monkeypatch.setattr(mod, "wait_serving", lambda *_a, **_k: True)
    assert mod.main(["--force"]) == 0
    assert calls == [
        ["scripts/build-image", "precis-dev"],
        ["docker", "tag", "precis-dev", "precis-mcp:dev"],
        [str(inst), "--recreate"],
    ]


def test_force_dry_run_plans_the_build_and_changes_nothing(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _labels(mod, monkeypatch, PIN)
    _installed(mod, "new\n")
    calls = _no_run(mod, monkeypatch)
    assert mod.main(["--force", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "--force rebuilds anyway" in out
    assert "scripts/build-image precis-dev" in out
    assert calls == []

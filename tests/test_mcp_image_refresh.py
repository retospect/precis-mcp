"""scripts/mcp-image-refresh — version compare, dry-run plan, stop-on-diff.

docker and every subprocess are mocked; nothing here touches a container.
"""

from __future__ import annotations

import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pytest

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


@pytest.fixture()
def mod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    m = SourceFileLoader(
        "mcp_image_refresh", str(REPO_ROOT / "scripts" / "mcp-image-refresh")
    ).load_module()
    (tmp_path / "uv.lock").write_text(LOCK, encoding="utf-8")
    repo = tmp_path / "repo-ensure.sh"
    repo.write_text("new\n", encoding="utf-8")
    inst = tmp_path / "inst-ensure.sh"
    monkeypatch.setattr(m, "ROOT", tmp_path)
    monkeypatch.setattr(m, "REPO_ENSURE", repo)
    monkeypatch.setenv("PRECIS_MCP_ENSURE", str(inst))
    return m


def _labels(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, rev: str, sha: str = "a" * 40
) -> None:
    monkeypatch.setattr(
        mod, "image_label", lambda _i, k: rev if k == mod.REV_LABEL else sha
    )


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
    m = SourceFileLoader(
        "mir2", str(REPO_ROOT / "scripts" / "mcp-image-refresh")
    ).load_module()
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


def test_up_to_date_exits_zero_without_running(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _labels(mod, monkeypatch, PIN)
    calls = _no_run(mod, monkeypatch)
    assert mod.main([]) == 0
    assert "up to date" in capsys.readouterr().out
    assert calls == []


def test_dry_run_prints_plan_and_diff_and_changes_nothing(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _labels(mod, monkeypatch, "")  # unlabelled image = stale
    calls = _no_run(mod, monkeypatch)
    assert mod.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "scripts/build-image precis-dev" in out and "--recreate" in out
    assert PIN in out and "DIFFERS" in out and "+new" in out
    assert calls == []


def test_stops_on_installed_script_diff_before_build(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _labels(mod, monkeypatch, "deadbeef")
    calls = _no_run(mod, monkeypatch)
    assert mod.main([]) == mod.EXIT_DIFF
    assert calls == []  # fail fast: no build, no install, no recreate
    assert "STOP" in capsys.readouterr().err


def test_full_refresh_order_when_in_sync(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    revs = iter(
        ["deadbeef", "", PIN]
    )  # before build: stale; sha label; after build: pinned
    monkeypatch.setattr(mod, "image_label", lambda _i, _k: next(revs))
    inst = Path(__import__("os").environ["PRECIS_MCP_ENSURE"])
    inst.write_text("new\n", encoding="utf-8")
    calls = _no_run(mod, monkeypatch)
    monkeypatch.setattr(mod, "wait_serving", lambda *_a, **_k: True)
    assert mod.main([]) == 0
    assert calls == [
        ["scripts/build-image", "precis-dev"],
        ["docker", "tag", "precis-dev", "precis-mcp:dev"],
        ["scripts/mcp-http-install"],
        [str(inst), "--recreate"],
    ]


def test_unhealthy_after_recreate_fails(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    revs = iter(["deadbeef", "", PIN])
    monkeypatch.setattr(mod, "image_label", lambda _i, _k: next(revs))
    Path(__import__("os").environ["PRECIS_MCP_ENSURE"]).write_text(
        "new\n", encoding="utf-8"
    )
    _no_run(mod, monkeypatch)
    monkeypatch.setattr(mod, "wait_serving", lambda *_a, **_k: False)
    assert mod.main([]) == mod.EXIT_UNHEALTHY


def test_serving_needs_both_probes_401(mod: ModuleType) -> None:
    ok = "x (no token, from the proxy) -> 401  (401 = serving)\ny (no token, via proxy) -> 401  (z)\n"
    assert mod.serving(ok)
    assert not mod.serving(ok.replace("via proxy) -> 401", "via proxy) -> 502"))
    assert not mod.serving("")


def test_build_that_misses_the_pin_does_not_recreate(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _labels(mod, monkeypatch, "deadbeef")  # still stale after the build
    Path(__import__("os").environ["PRECIS_MCP_ENSURE"]).write_text(
        "new\n", encoding="utf-8"
    )
    calls = _no_run(mod, monkeypatch)
    assert mod.main([]) == 1
    assert all("--recreate" not in c for c in calls)

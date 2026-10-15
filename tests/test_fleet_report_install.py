"""scripts/fleet-report-install: plist contents and lifecycle, fake launchctl."""

from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="launchd agent install is macOS-only"
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fleet-report-install"

FAKE_LAUNCHCTL = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state = Path(os.environ["FAKE_STATE"])
with (state / "launchctl.jsonl").open("a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
loaded = state / "loaded"
if sys.argv[1] == "print":
    if loaded.exists():
        print("    state = not running")  # real launchctl indents
        raise SystemExit(0)
    raise SystemExit(113)
if sys.argv[1] == "bootstrap":
    loaded.touch()
elif sys.argv[1] == "bootout":
    loaded.unlink(missing_ok=True)
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    launchctl = bindir / "launchctl"
    launchctl.write_text(FAKE_LAUNCHCTL, encoding="utf-8")
    launchctl.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FAKE_STATE", str(state))
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.delenv("PRECIS_MCP_TOKEN", raising=False)
    monkeypatch.delenv("PRECIS_FLEET_MCP_URL", raising=False)
    return tmp_path


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if check:
        assert r.returncode == 0, r.stderr
    return r


def calls(env: Path) -> list[list[str]]:
    log = env / "state" / "launchctl.jsonl"
    if not log.exists():
        return []
    return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]


def plist_path(env: Path) -> Path:
    return env / "home" / "Library" / "LaunchAgents" / "com.precis.fleet-report.plist"


def test_install_writes_plist_and_bootstraps(env):
    token_file = env / "token"
    token_file.write_text("tok-secret\n", encoding="utf-8")
    out = run("--token-file", str(token_file), "--remote", "reto@melchior")
    assert "every 60 s" in out.stdout
    plist = plist_path(env)
    assert stat.S_IMODE(plist.stat().st_mode) == 0o600
    d = plistlib.loads(plist.read_bytes())
    assert d["Label"] == "com.precis.fleet-report"
    assert d["StartInterval"] == 60 and d["RunAtLoad"] is True
    argv = d["ProgramArguments"]
    assert argv[:2] == [str(ROOT / "scripts" / "fleet-report"), "--push"]
    assert argv[2:] == ["--remote", "reto@melchior"]
    envs = d["EnvironmentVariables"]
    assert envs["PRECIS_MCP_TOKEN_FILE"] == str(token_file)
    assert "tok-secret" not in plist.read_text(encoding="utf-8")
    assert d["StandardOutPath"].endswith("Library/Logs/fleet-report.out.log")
    verbs = [c[0] for c in calls(env)]
    assert "bootout" not in verbs  # nothing was loaded
    assert calls(env)[-1] == ["bootstrap", f"gui/{os.getuid()}", str(plist)]


def test_install_embeds_env_token(env, monkeypatch):
    monkeypatch.setenv("PRECIS_MCP_TOKEN", "tok-secret")
    run()
    d = plistlib.loads(plist_path(env).read_bytes())
    assert d["EnvironmentVariables"]["PRECIS_MCP_TOKEN"] == "tok-secret"


def test_reinstall_bootouts_first_and_status_reports(env):
    run()
    (env / "state" / "launchctl.jsonl").unlink()
    run()
    verbs = [c[0] for c in calls(env)]
    # print probe -> bootout -> print poll -> bootstrap
    assert verbs.index("bootout") < verbs.index("bootstrap")
    out = run("--status")
    assert "installed:" in out.stdout and "state = not running" in out.stdout


def test_uninstall_bootouts_and_removes_plist(env):
    run()
    out = run("--uninstall")
    assert "uninstalled" in out.stdout
    assert not plist_path(env).exists()
    assert "bootout" in [c[0] for c in calls(env)]
    out = run("--status")
    assert "not installed" in out.stdout and "not loaded" in out.stdout


def test_missing_token_file_refused(env):
    r = run("--token-file", str(env / "nope"), check=False)
    assert r.returncode == 1 and "token file not found" in r.stderr
    assert not plist_path(env).exists()

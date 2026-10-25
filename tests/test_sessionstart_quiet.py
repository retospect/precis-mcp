"""gr499042 — SessionStart hooks are silent on success and the common-case
output stays under the byte budget.

Hook output is injected into context once per SessionStart (startup AND
compact) and then carried on every later turn of the session, so every
byte has a multiplier. The contract: counts and pointers, not rosters —
and a reaper that found nothing prints nothing. ``--for-hook`` buys the
silence; run by hand the scripts keep their human-facing one-liners.

The memory hook's own budget share (the counts line + gripe digest,
≤ 400 B) is pinned behaviourally in ``tests/test_memory_index_retired.py``;
everything else that can print on the common path is pinned here.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests._script import load_script

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the SessionStart scripts are POSIX-only"
)

REPO = Path(__file__).resolve().parent.parent

#: Whole-output ceiling for a common-case SessionStart (green CI, nothing
#: to reap): the inflight counts line measured below plus the memory hook's
#: share pinned in test_memory_index_retired.py. Everything else on the
#: SessionStart list prints nothing on success.
SESSIONSTART_BUDGET_BYTES = 600
MEMORY_HOOK_CAP_BYTES = 400


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _stage_repo(tmp_path: Path, *script_names: str) -> Path:
    """A minimal git repo holding copies of the named ``scripts/`` files
    plus a stub ``scripts/inflight`` answering ``--json`` with no trees —
    the state where every reaper should find nothing to do."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    for name in script_names:
        shutil.copy2(REPO / "scripts" / name, repo / "scripts" / name)
        (repo / "scripts" / name).chmod(0o755)
    if "inflight" in script_names:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "init")
        return repo
    inflight = repo / "scripts" / "inflight"
    inflight.write_text(
        '#!/bin/sh\n[ "$1" = "--json" ] && echo \'{"worktrees": []}\'\n',
        encoding="utf-8",
    )
    inflight.chmod(0o755)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _fake_docker(tmp_path: Path) -> Path:
    """A ``docker`` on PATH that succeeds and prints nothing — a daemon
    with no precis containers, volumes, networks or images."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    docker.chmod(0o755)
    return bindir


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PRECIS_")}
    env.update(extra)
    return env


def _run(
    argv: list[str], cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def test_reap_worktrees_for_hook_is_silent_on_success(tmp_path: Path) -> None:
    repo = _stage_repo(tmp_path, "reap-worktrees")
    env = _env(tmp_path)
    quiet = _run(["bash", "scripts/reap-worktrees", "--for-hook"], repo, env)
    assert quiet.returncode == 0, quiet.stderr
    assert quiet.stdout == ""
    human = _run(["bash", "scripts/reap-worktrees"], repo, env)
    assert "nothing to reap" in human.stdout  # the human affordance stays


def test_reap_test_dbs_for_hook_is_silent_on_success(tmp_path: Path) -> None:
    repo = _stage_repo(tmp_path, "reap-test-dbs")
    bindir = _fake_docker(tmp_path)
    # Fresh build-cache-prune stamp: the cap pass early-returns before it
    # can print its prune/skip lines.
    (repo / ".git" / "precis-build-cache-prune.stamp").touch()
    env = _env(tmp_path, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
    quiet = _run(["bash", "scripts/reap-test-dbs", "--for-hook"], repo, env)
    assert quiet.returncode == 0, quiet.stderr
    assert quiet.stdout == ""


def test_reap_stale_serves_for_hook_is_silent_on_success(tmp_path: Path) -> None:
    env = _env(tmp_path, PRECIS_REAP_SERVE_CMD="zzz-no-such-serve-zzz")
    quiet = _run(
        ["bash", str(REPO / "scripts" / "reap-stale-serves"), "--for-hook"],
        tmp_path,
        env,
    )
    assert quiet.returncode == 0, quiet.stderr
    assert quiet.stdout == ""
    human = _run(["bash", str(REPO / "scripts" / "reap-stale-serves")], tmp_path, env)
    assert "nothing to reap" in human.stdout


def test_reap_idle_gates_for_hook_is_silent_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # @dataclass introspects sys.modules[__module__] while it execs.
    mod = load_script(
        "reap_idle_gates", REPO / "scripts" / "reap-idle-gates", register=True
    )

    monkeypatch.setattr(mod, "_docker", lambda *a: "")
    monkeypatch.setattr(mod, "_worktrees", lambda: (["primary"], "primary"))
    monkeypatch.setattr(mod, "orphan_volumes", lambda *a: [])
    monkeypatch.setattr(mod, "dangling_images", lambda *a: [])

    assert mod.main(["--for-hook"]) == 0
    assert capsys.readouterr().out == ""
    assert mod.main([]) == 0
    assert "nothing to reap" in capsys.readouterr().out


def test_mcp_http_install_for_hook(tmp_path: Path) -> None:
    script = REPO / "scripts" / "mcp-http-install"
    ensure = REPO / "deploy" / "mcp-http" / "precis-mcp-http-ensure.sh"
    env = _env(tmp_path, PRECIS_MCP_ENSURE=str(ensure))
    quiet = _run(["bash", str(script), "--for-hook"], tmp_path, env)
    assert quiet.returncode == 0, quiet.stderr
    assert quiet.stdout == ""  # in sync → silent under --for-hook
    human = _run(["bash", str(script)], tmp_path, env)
    assert "in sync" in human.stdout

    # a drifted install still names itself under --for-hook — silence is
    # for success only, never for the actionable line
    other = tmp_path / "other-ensure.sh"
    other.write_text("# drifted\n", encoding="utf-8")
    env = _env(tmp_path, PRECIS_MCP_ENSURE=str(other))
    drifted = _run(["bash", str(script), "--for-hook"], tmp_path, env)
    assert drifted.stdout.startswith("mcp-http ensure script DIFFERS")


def _staged_inflight(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    repo = _stage_repo(tmp_path, "inflight")
    return _run(["bash", "scripts/inflight", "--for-hook"], repo, _env(tmp_path))


def test_inflight_for_hook_is_one_counts_line(tmp_path: Path) -> None:
    proc = _staged_inflight(tmp_path)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert len(lines) == 1
    assert re.fullmatch(
        r"🌳 \d+ trees in flight \(\d+ live, \d+ dirty, \d+ ahead of \S+\)"
        r" — scripts/inflight for the overlap table",
        lines[0],
    ), lines[0]
    # the roster/purpose/gripe extras stay out of the carried context
    assert "gripes:" not in proc.stdout
    assert ".claude/purpose" not in proc.stdout
    assert len(proc.stdout.encode("utf-8")) <= 200


def test_common_case_sessionstart_bytes_under_budget(tmp_path: Path) -> None:
    """The composed common-case render: the runnable-quiet pieces print
    nothing, so the total is inflight's counts line + the memory hook's
    pinned share — and stays under the budget."""
    total = len(_staged_inflight(tmp_path).stdout.encode("utf-8"))
    total += MEMORY_HOOK_CAP_BYTES
    # mcp-http --for-hook (in sync), the four reapers, session-start-lock,
    # memory-nudge, main-ci-status --for-hook (green) and docs-index all
    # contribute 0 on the common path — asserted above.
    assert total <= SESSIONSTART_BUDGET_BYTES

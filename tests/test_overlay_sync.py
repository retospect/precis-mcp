"""``scripts/lib/overlay-sync.sh`` + ``scripts/overlay-sync`` — the deploy
overlay's two copies cannot drift apart unnoticed (gr680328).

The private overlay (``deploy/inventory``) is its own git repo on the
controller, mirrored on the gateway as the ``deploy-helper`` remote (a
non-bare repo with ``receive.denyCurrentBranch=updateInstead``). On
2026-11-14 the two had forked for weeks and a controller deploy rendered
the pgbouncer userlist without a role (gr678654). Every ``scripts/deploy``
and ``scripts/host-ops/play`` now runs ``overlay_sync_preflight`` first:
fast-forward when behind, push when ahead, refuse on divergence or
uncommitted edits. These tests drive the library and the CLI against two
real throwaway repos shaped like the gateway pair; no network, no host.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only shell scripts"
)

_REPO = Path(__file__).resolve().parent.parent
_LIB = _REPO / "scripts" / "lib" / "overlay-sync.sh"
_CLI = _REPO / "scripts" / "overlay-sync"
_DEPLOY = _REPO / "scripts" / "deploy"
_PLAY = _REPO / "scripts" / "host-ops" / "play"


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """A git environment with no operator config, hooks or signing."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".gitconfig").write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        **extra,
    }
    # Ambient operator vars must not steer a fixture.
    for key in (
        "PRECIS_DEPLOY_ALLOW_OVERLAY_DRIFT",
        "PRECIS_OVERLAY_DIR",
        "PRECIS_OVERLAY_REMOTE",
        "PRECIS_OVERLAY_BRANCH",
        "GIT_SSH_COMMAND",
    ):
        if key not in extra:
            env.pop(key, None)
    return env


def _git(env: dict[str, str], repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _commit(env: dict[str, str], repo: Path, rel: str, body: str, msg: str) -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    _git(env, repo, "add", rel)
    _git(env, repo, "commit", "-qm", msg)
    return _git(env, repo, "rev-parse", "HEAD")


def _head(env: dict[str, str], repo: Path) -> str:
    return _git(env, repo, "rev-parse", "HEAD")


@pytest.fixture
def pair(tmp_path: Path) -> dict:
    """``helper``: the gateway's non-bare ``git_deploy_helper`` on ``main``
    with ``updateInstead``; ``local``: the controller's ``deploy/inventory``,
    a clone whose only remote is named ``deploy-helper``."""
    env = _env(tmp_path)
    helper = tmp_path / "git_deploy_helper"
    helper.mkdir()
    _git(env, helper, "init", "-q")
    _git(env, helper, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(env, helper, "config", "receive.denyCurrentBranch", "updateInstead")
    _commit(env, helper, "hosts.yml", "all:\n  hosts: {}\n", "init")
    _commit(env, helper, "group_vars/all/main.yml", "a: 1\n", "vars")
    local = tmp_path / "inventory"
    _git(env, tmp_path, "clone", "-q", "-o", "deploy-helper", str(helper), str(local))
    return {"env": env, "helper": helper, "local": local}


def _preflight(
    env: dict[str, str], overlay: Path, *, allow: bool = False
) -> subprocess.CompletedProcess[str]:
    e = dict(env)
    if allow:
        e["PRECIS_DEPLOY_ALLOW_OVERLAY_DRIFT"] = "1"
    return subprocess.run(
        [
            "bash",
            "-c",
            # The shell options scripts/deploy and play run under.
            'set -euo pipefail; . "$1"; overlay_sync_preflight "$2" overlay',
            "_",
            str(_LIB),
            str(overlay),
        ],
        env=e,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def _cli(
    env: dict[str, str], overlay: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_CLI), *args],
        env={**env, "PRECIS_OVERLAY_DIR": str(overlay)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


# ── the relation table ──────────────────────────────────────────────────────


def test_in_sync_is_a_noop(pair: dict) -> None:
    env, local, helper = pair["env"], pair["local"], pair["helper"]
    before = _head(env, local)
    result = _preflight(env, local)
    assert result.returncode == 0, result.stderr
    assert "in sync" in result.stdout
    assert _head(env, local) == before == _head(env, helper)


def test_behind_fast_forwards_the_local_copy(pair: dict) -> None:
    env, local, helper = pair["env"], pair["local"], pair["helper"]
    new = _commit(env, helper, "host_vars/gw.yml", "flag: true\n", "gateway flag")
    result = _preflight(env, local)
    assert result.returncode == 0, result.stderr
    assert "fast-forwarded" in result.stdout
    assert _head(env, local) == new
    assert (local / "host_vars" / "gw.yml").read_text(
        encoding="utf-8"
    ) == "flag: true\n"


def test_ahead_pushes_to_the_helper(pair: dict) -> None:
    env, local, helper = pair["env"], pair["local"], pair["helper"]
    new = _commit(env, local, "group_vars/all/main.yml", "a: 2\n", "bump a")
    result = _preflight(env, local)
    assert result.returncode == 0, result.stderr
    assert "pushed 1 commit" in result.stdout
    assert _head(env, helper) == new
    # updateInstead checked the push out into the helper's work tree.
    assert (helper / "group_vars/all/main.yml").read_text(encoding="utf-8") == "a: 2\n"


def test_diverged_refuses_and_touches_nothing(pair: dict) -> None:
    env, local, helper = pair["env"], pair["local"], pair["helper"]
    h = _commit(env, helper, "host_vars/gw.yml", "x: 1\n", "helper side")
    l = _commit(env, local, "host_vars/db.yml", "y: 1\n", "local side")
    result = _preflight(env, local)
    assert result.returncode == 1
    assert "DIVERGED" in result.stderr
    assert "1 commit(s) only here, 1 only there" in result.stderr
    assert "PRECIS_DEPLOY_ALLOW_OVERLAY_DRIFT" in result.stderr
    assert _head(env, local) == l and _head(env, helper) == h


def test_diverged_with_override_warns_and_proceeds(pair: dict) -> None:
    env, local, helper = pair["env"], pair["local"], pair["helper"]
    h = _commit(env, helper, "host_vars/gw.yml", "x: 1\n", "helper side")
    l = _commit(env, local, "host_vars/db.yml", "y: 1\n", "local side")
    result = _preflight(env, local, allow=True)
    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stderr and "DIVERGED" in result.stderr
    assert _head(env, local) == l and _head(env, helper) == h


# ── uncommitted state ───────────────────────────────────────────────────────


def test_dirty_tracked_file_refuses_before_any_fetch(pair: dict) -> None:
    env, local, helper = pair["env"], pair["local"], pair["helper"]
    _commit(env, helper, "host_vars/gw.yml", "x: 1\n", "helper side")
    (local / "hosts.yml").write_text("all:\n  hosts:\n    gw:\n", encoding="utf-8")
    result = _preflight(env, local)
    assert result.returncode == 1
    assert "uncommitted changes" in result.stderr
    assert " M hosts.yml" in result.stderr
    assert "scripts/overlay-sync commit" in result.stderr
    # Refused before fetching: the helper's new commit was not merged in.
    assert not (local / "host_vars" / "gw.yml").exists()


def test_dirty_with_override_warns_and_proceeds(pair: dict) -> None:
    env, local = pair["env"], pair["local"]
    (local / "hosts.yml").write_text("all:\n  hosts:\n    gw:\n", encoding="utf-8")
    result = _preflight(env, local, allow=True)
    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stderr and " M hosts.yml" in result.stderr


def test_untracked_loadable_file_refuses_backup_names_do_not(pair: dict) -> None:
    env, local = pair["env"], pair["local"]
    # Not loaded by ansible's vars plugin (extension is not yml/yaml/json).
    (local / "group_vars/all/main.yml.bak-20261114").write_text(
        "a: 1\n", encoding="utf-8"
    )
    (local / "group_vars/all/.DS_Store").write_bytes(b"\0")
    # A hidden directory is pruned at every level of ansible's walk (a hook's
    # `.scratch/` landed in the real overlay once and must not refuse deploys).
    (local / ".scratch" / "tool").mkdir(parents=True)
    (local / ".scratch" / "tool" / "note.json").write_text("{}\n", encoding="utf-8")
    ok = _preflight(env, local)
    assert ok.returncode == 0, ok.stderr
    # Loaded by ansible, reaches nobody else: refuse and name it.
    (local / "group_vars/all/vault_extra.yml").write_text("k: v\n", encoding="utf-8")
    bad = _preflight(env, local)
    assert bad.returncode == 1
    assert "?? group_vars/all/vault_extra.yml" in bad.stderr
    assert "main.yml.bak" not in bad.stderr


# ── skip paths: never act on something that is not the overlay repo ────────


def test_plain_directory_overlay_is_skipped(tmp_path: Path) -> None:
    env = _env(tmp_path)
    plain = tmp_path / "plain"
    (plain / "group_vars").mkdir(parents=True)
    (plain / "hosts.yml").write_text("all: {}\n", encoding="utf-8")
    result = _preflight(env, plain)
    assert result.returncode == 0, result.stderr
    assert "not its own git repo" in result.stdout


def test_directory_inside_another_repo_is_skipped(pair: dict) -> None:
    """A gitignored dir INSIDE the code checkout resolves to the checkout's
    top, not to itself — the library must never fetch/merge/push there."""
    env, helper = pair["env"], pair["helper"]
    inner = helper / "deploy" / "inventory"
    inner.mkdir(parents=True)
    (inner / "hosts.yml").write_text("all: {}\n", encoding="utf-8")
    result = _preflight(env, inner)
    assert result.returncode == 0, result.stderr
    assert "not its own git repo" in result.stdout
    cli = _cli(env, inner, "status")
    assert cli.returncode == 2
    assert "never acts on the code checkout" in cli.stderr


def test_repo_without_the_remote_is_skipped(tmp_path: Path) -> None:
    env = _env(tmp_path)
    lone = tmp_path / "lone"
    lone.mkdir()
    _git(env, lone, "init", "-q")
    _commit(env, lone, "hosts.yml", "all: {}\n", "init")
    result = _preflight(env, lone)
    assert result.returncode == 0, result.stderr
    assert "no 'deploy-helper' remote" in result.stdout


# ── the remote is unreachable or refuses ────────────────────────────────────


def test_fetch_failure_warns_and_never_pushes(pair: dict, tmp_path: Path) -> None:
    env, local = pair["env"], pair["local"]
    _git(env, local, "remote", "set-url", "deploy-helper", str(tmp_path / "gone"))
    ahead = _commit(env, local, "group_vars/all/main.yml", "a: 3\n", "local only")
    result = _preflight(env, local)
    assert result.returncode == 0, result.stderr
    assert "could not fetch" in result.stderr
    assert "not pushing" in result.stderr
    assert _head(env, local) == ahead


def test_rejected_push_refuses_with_the_runbook_pointer(pair: dict) -> None:
    env, local, helper = pair["env"], pair["local"], pair["helper"]
    # Someone edited the helper in place: updateInstead rejects any push.
    (helper / "hosts.yml").write_text("all:\n  hosts:\n    edited:\n", encoding="utf-8")
    _commit(env, local, "group_vars/all/main.yml", "a: 4\n", "local change")
    result = _preflight(env, local)
    assert result.returncode == 1
    assert "push was rejected" in result.stderr
    assert "remote rejected" in result.stderr


# ── the operator CLI ────────────────────────────────────────────────────────


def test_cli_help_and_usage(pair: dict) -> None:
    env, local = pair["env"], pair["local"]
    assert _cli(env, local, "--help").returncode == 0
    assert _cli(env, local).returncode == 2
    assert _cli(env, local, "bogus").returncode == 2


def test_cli_status_commit_push_round_trip(pair: dict) -> None:
    env, local, helper = pair["env"], pair["local"], pair["helper"]
    status = _cli(env, local, "status")
    assert status.returncode == 0, status.stderr
    assert "ahead 0, behind 0" in status.stdout and "clean" in status.stdout

    (local / "group_vars/all/main.yml").write_text("a: 5\n", encoding="utf-8")
    (local / "group_vars/all/new.yml").write_text("n: 1\n", encoding="utf-8")
    status = _cli(env, local, "status")
    assert " M group_vars/all/main.yml" in status.stdout
    assert "?? group_vars/all/new.yml" in status.stdout
    # Secrets never: the status shows names, not the edited values.
    assert "a: 5" not in status.stdout and "n: 1" not in status.stdout

    # Path form adds the untracked file and commits only the named paths.
    commit = _cli(
        env, local, "commit", "-m", "overlay: add new", "group_vars/all/new.yml"
    )
    assert commit.returncode == 0, commit.stderr
    assert re.search(r"committed [0-9a-f]+ overlay: add new", commit.stdout)
    assert " M group_vars/all/main.yml" in _cli(env, local, "status").stdout
    # -a form sweeps the remaining tracked edit.
    commit = _cli(env, local, "commit", "-m", "overlay: bump a")
    assert commit.returncode == 0, commit.stderr
    assert "clean" in _cli(env, local, "status").stdout
    assert "ahead 2, behind 0" in _cli(env, local, "status").stdout

    push = _cli(env, local, "push")
    assert push.returncode == 0, push.stderr
    assert _head(env, helper) == _head(env, local)
    assert (helper / "group_vars/all/new.yml").exists()
    assert _cli(env, local, "commit").returncode == 2  # -m is required


def test_cli_sync_log_diff(pair: dict) -> None:
    env, local, helper = pair["env"], pair["local"], pair["helper"]
    _commit(env, helper, "host_vars/gw.yml", "x: 1\n", "helper side")
    log = _cli(env, local, "log", "5")
    assert log.returncode == 0, log.stderr
    assert "> " in log.stdout and "helper side" in log.stdout
    diff = _cli(env, local, "diff")
    assert diff.returncode == 0, diff.stderr
    assert "host_vars/gw.yml" in diff.stdout
    sync = _cli(env, local, "sync")
    assert sync.returncode == 0, sync.stderr
    assert "fast-forwarded" in sync.stdout
    assert _head(env, local) == _head(env, helper)


# ── wiring and hygiene ──────────────────────────────────────────────────────


def test_deploy_and_play_run_the_preflight_before_rendering() -> None:
    deploy = _DEPLOY.read_text(encoding="utf-8")
    assert "scripts/lib/overlay-sync.sh" in deploy
    assert "overlay_sync_preflight" in deploy
    # Before the overlay hash (a fetch must not count as an overlay change)
    # and before the cd into the deploy tree.
    assert deploy.index("overlay_sync_preflight") < deploy.index(
        'OVERLAY_HASH="$(_overlay_hash'
    )
    assert deploy.index("overlay_sync_preflight") < deploy.index('cd "$CLUSTER_DIR"')
    # The hash skips .git: FETCH_HEAD moves on every preflight.
    assert "-name .git -prune" in deploy
    play = _PLAY.read_text(encoding="utf-8")
    assert "overlay_sync_preflight" in play
    assert play.index("overlay_sync_preflight") < play.index("exec ansible-playbook")
    for text in (deploy, play):
        assert "PRECIS_DEPLOY_ALLOW_OVERLAY_DRIFT" in text


def test_cli_is_executable_and_strict() -> None:
    assert os.access(_CLI, os.X_OK)
    assert "set -euo pipefail" in _CLI.read_text(encoding="utf-8")


def test_shellcheck() -> None:
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    result = subprocess.run(
        ["shellcheck", "-x", str(_CLI), str(_LIB)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(_REPO),
    )
    assert result.returncode == 0, result.stdout + result.stderr

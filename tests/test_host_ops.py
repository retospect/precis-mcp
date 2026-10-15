"""``scripts/host-ops/*`` — ported operator scripts (Reto "go host-ops", 2026-10-15).

Every script refuses to run outside its contract and reads host coordinates
from the gitignored inventory overlay (``CLUSTER_INVENTORY`` fixture here).
``ssh`` and ``ansible-playbook`` are PATH stubs that log argv — the tests
never touch a real host.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only shell scripts"
)

_REPO = Path(__file__).resolve().parent.parent
_DIR = _REPO / "scripts" / "host-ops"
_SCRIPTS = [
    "lanes-overlay",
    "nfs-keepalive",
    "nfs-recover",
    "os-update",
    "pin-prod",
    "play",
    "vllm-stop",
]

# Fixture overlay: RFC 5737 documentation addresses only — the no-secrets gate
# accepts them, and they can never be mistaken for a real node address.
_HOSTS_YML = """\
all:
  vars:
    nfs_server: caspar
    shared_mount: "/mnt/cluster"
  children:
    linux:
      hosts:
        spark:
        castor:
        pollux:
      vars:
        os_family: linux
    nfs_servers:
      hosts:
        caspar:
    vllm_serving:
      hosts:
        castor:
    macos:
      hosts:
        caspar:
          ansible_host: 203.0.113.10
          lan_ip: 203.0.113.11
"""

_NO_VLLM_YML = """\
all:
  children:
    linux:
      hosts:
        spark:
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _commit(repo: Path, body: str) -> str:
    (repo / "f.txt").write_text(body, encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", f"c {body}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def bin_dir(tmp_path: Path) -> dict[str, str]:
    """PATH stubs: ssh logs argv (rc 1 when the remote mentions sglang, so
    vllm-stop's refusal guard takes the proceed path); ansible-playbook logs
    argv and exits 0."""
    d = tmp_path / "bin"
    d.mkdir()
    ssh_calls = tmp_path / "ssh.calls"
    ansible_calls = tmp_path / "ansible.calls"
    (d / "ssh").write_text(
        "#!/bin/sh\n"
        # one log line per call: remote commands carry newlines
        f"{{ printf '%s' \"$*\" | tr '\\n' ' '; echo; }} >> \"{ssh_calls}\"\n"
        'case "$*" in *sglang*) exit 1;; esac\n'
        "exit 0\n",
        encoding="utf-8",
    )
    (d / "ansible-playbook").write_text(
        "#!/bin/sh\n"
        f"{{ printf '%s' \"$*\" | tr '\\n' ' '; echo; }} >> \"{ansible_calls}\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    for stub in d.iterdir():
        stub.chmod(0o755)
    return {
        "bin": str(d),
        "ssh_calls": str(ssh_calls),
        "ansible_calls": str(ansible_calls),
    }


def _env(tmp_path: Path, bin_dir: dict[str, str], **extra: str) -> dict[str, str]:
    inv = tmp_path / "inv" / "hosts.yml"
    inv.parent.mkdir(parents=True, exist_ok=True)
    inv.write_text(extra.pop("hosts_yml", _HOSTS_YML), encoding="utf-8")
    (tmp_path / ".vault-pass").write_text("fake\n", encoding="utf-8")
    return {
        **os.environ,
        "PATH": f"{bin_dir['bin']}:{os.environ['PATH']}",
        "CLUSTER_INVENTORY": str(inv),
        **extra,
    }


def _run(
    env: dict[str, str], script: str, *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_DIR / script), *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def _calls(path: str) -> list[str]:
    p = Path(path)
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


@pytest.fixture
def git_pair(tmp_path: Path) -> dict[str, str]:
    """bare origin + MAIN_CHECKOUT clone (source of origin/prod) + PROD_CHECKOUT
    clone; prod points at the second commit."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    _git(seed, "config", "user.email", "t@example.invalid")
    _git(seed, "config", "user.name", "t")
    old = _commit(seed, "one")
    prod_sha = _commit(seed, "two")
    _git(seed, "push", "-q", "origin", "HEAD:prod")
    main = tmp_path / "main"
    prod = tmp_path / "prod"
    _git(tmp_path, "clone", "-q", str(origin), str(main))
    _git(tmp_path, "clone", "-q", "--branch", "prod", str(origin), str(prod))
    return {
        "MAIN_CHECKOUT": str(main),
        "PROD_CHECKOUT": str(prod),
        "prod_sha": prod_sha,
        "old_sha": old,
    }


# ── every script: --help, executable, set -euo pipefail ────────────────────


@pytest.mark.parametrize("script", _SCRIPTS)
def test_help(script: str, tmp_path: Path, bin_dir: dict[str, str]) -> None:
    result = _run(_env(tmp_path, bin_dir), script, "--help")
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


@pytest.mark.parametrize("script", _SCRIPTS)
def test_executable_and_strict(script: str) -> None:
    path = _DIR / script
    assert os.access(path, os.X_OK), f"{script} not executable"
    assert "set -euo pipefail" in path.read_text(encoding="utf-8")


def test_shellcheck() -> None:
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    for script in _SCRIPTS:
        result = subprocess.run(
            ["shellcheck", str(_DIR / script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stdout + result.stderr


# ── --host contract ─────────────────────────────────────────────────────────


def test_os_update_requires_host(tmp_path: Path, bin_dir: dict[str, str]) -> None:
    result = _run(_env(tmp_path, bin_dir), "os-update")
    assert result.returncode == 2
    assert "--host" in result.stderr
    assert _calls(bin_dir["ssh_calls"]) == []


def test_os_update_dry_run(tmp_path: Path, bin_dir: dict[str, str]) -> None:
    env = _env(tmp_path, bin_dir)
    result = _run(env, "os-update", "--host", "spark", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "DRY: precis_cli reserve --host spark" in result.stdout
    assert "7/7 checks" in result.stdout
    # The stale apt/dpkg lock probe always runs; nothing else may ssh.
    calls = _calls(bin_dir["ssh_calls"])
    assert len(calls) == 1 and "fuser" in calls[0]


def test_vllm_stop_requires_host_or_group(
    tmp_path: Path, bin_dir: dict[str, str]
) -> None:
    env = _env(tmp_path, bin_dir, hosts_yml=_NO_VLLM_YML)
    result = _run(env, "vllm-stop")
    assert result.returncode == 2
    assert "vllm_serving" in result.stderr


def test_vllm_stop_resolves_group_host(tmp_path: Path, bin_dir: dict[str, str]) -> None:
    env = _env(tmp_path, bin_dir, VLLM_STOP_SLEEP_S="0")
    result = _run(env, "vllm-stop")
    assert result.returncode == 0, result.stderr
    calls = _calls(bin_dir["ssh_calls"])
    assert calls and all(" castor " in f" {c} " for c in calls)
    assert any("disable --now vllm" in c for c in calls)


def test_vllm_stop_host_override_and_start(
    tmp_path: Path, bin_dir: dict[str, str]
) -> None:
    env = _env(tmp_path, bin_dir, VLLM_STOP_SLEEP_S="0")
    result = _run(env, "vllm-stop", "--host", "pollux", "--start")
    assert result.returncode == 0, result.stderr
    calls = _calls(bin_dir["ssh_calls"])
    assert calls and all(" pollux " in f" {c} " for c in calls)
    assert any("enable --now vllm" in c for c in calls)


# ── NFS scripts read the overlay ─────────────────────────────────────────────


def test_nfs_keepalive_runs_on_linux_group(
    tmp_path: Path, bin_dir: dict[str, str]
) -> None:
    result = _run(_env(tmp_path, bin_dir), "nfs-keepalive")
    assert result.returncode == 0, result.stderr
    calls = _calls(bin_dir["ssh_calls"])
    assert len(calls) == 3
    hosts = {c.split()[8] for c in calls}  # four -o pairs, then <host> <remote>
    assert hosts == {"spark", "castor", "pollux"}
    assert all("systemd-run" in c and "/mnt/cluster" in c for c in calls)


def test_nfs_keepalive_stop(tmp_path: Path, bin_dir: dict[str, str]) -> None:
    result = _run(_env(tmp_path, bin_dir), "nfs-keepalive", "--stop")
    assert result.returncode == 0, result.stderr
    calls = _calls(bin_dir["ssh_calls"])
    assert len(calls) == 3
    assert all("systemctl stop nfs-keepalive" in c for c in calls)


def test_nfs_recover_overlay_driven(tmp_path: Path, bin_dir: dict[str, str]) -> None:
    env = _env(tmp_path, bin_dir, NFS_RECOVER_SLEEP_S="0")
    result = _run(env, "nfs-recover")
    assert result.returncode == 0, result.stderr
    calls = _calls(bin_dir["ssh_calls"])
    joined = "\n".join(calls)
    # nfsd restart on the resolved nfs_server host, then a pass over the
    # linux-group clients, then the recurrence probe.
    assert "caspar" in joined and "nfsd restart" in joined
    for h in ("spark", "castor", "pollux"):
        assert any(f" {h} " in f" {c} " for c in calls), h
    # the LAN address for `ss -K dst` came from the server's lan_ip, not a literal
    assert "dst 203.0.113.11" in joined
    assert "rpcinfo -T tcp 203.0.113.11" in joined


# ── play: the pinned-checkout gate ────────────────────────────────────────────


def test_play_refuses_unpinned_checkout(
    tmp_path: Path, bin_dir: dict[str, str], git_pair: dict[str, str]
) -> None:
    _git(
        Path(git_pair["PROD_CHECKOUT"]),
        "checkout",
        "-q",
        "--detach",
        git_pair["old_sha"],
    )
    env = _env(tmp_path, bin_dir, **git_pair)
    result = _run(env, "play", "playbooks/10-hardening.yml")
    assert result.returncode == 1
    assert "REFUSE" in result.stderr
    assert _calls(bin_dir["ansible_calls"]) == []


def test_play_refuses_dirty_checkout(
    tmp_path: Path, bin_dir: dict[str, str], git_pair: dict[str, str]
) -> None:
    _git(
        Path(git_pair["PROD_CHECKOUT"]),
        "checkout",
        "-q",
        "--detach",
        "origin/prod",
    )
    f = Path(git_pair["PROD_CHECKOUT"]) / "f.txt"
    f.write_text("dirty\n", encoding="utf-8")
    env = _env(tmp_path, bin_dir, HOST_LANES_SHA=git_pair["old_sha"], **git_pair)
    result = _run(env, "play", "playbooks/10-hardening.yml")
    assert result.returncode == 1
    assert "local edits" in result.stderr


def test_play_refuses_missing_lanes_sha(
    tmp_path: Path, bin_dir: dict[str, str], git_pair: dict[str, str]
) -> None:
    _git(
        Path(git_pair["PROD_CHECKOUT"]),
        "checkout",
        "-q",
        "--detach",
        "origin/prod",
    )
    env = _env(tmp_path, bin_dir, HOST_LANES_SHA="f" * 40, **git_pair)
    result = _run(env, "play", "playbooks/10-hardening.yml")
    assert result.returncode == 1
    assert "does not contain host-lanes" in result.stderr


def test_play_runs_pinned(
    tmp_path: Path, bin_dir: dict[str, str], git_pair: dict[str, str]
) -> None:
    prod = Path(git_pair["PROD_CHECKOUT"])
    (prod / "deploy").mkdir()
    _git(prod, "checkout", "-q", "--detach", "origin/prod")
    env = _env(tmp_path, bin_dir, HOST_LANES_SHA=git_pair["old_sha"], **git_pair)
    result = _run(env, "play", "playbooks/10-hardening.yml", "--tags", "oom-guard")
    assert result.returncode == 0, result.stderr
    calls = _calls(bin_dir["ansible_calls"])
    assert len(calls) == 1
    argv = calls[0]
    sha = git_pair["prod_sha"]
    assert "-i " in argv and "hosts.yml" in argv
    assert "--vault-password-file" in argv
    for ref in ("worker", "web", "embedder"):
        assert f"-e precis_{ref}_git_ref={sha}" in argv
    assert argv.endswith("playbooks/10-hardening.yml --tags oom-guard")


def test_play_usage(tmp_path: Path, bin_dir: dict[str, str]) -> None:
    result = _run(_env(tmp_path, bin_dir), "play")
    assert result.returncode == 2


# ── pin-prod ────────────────────────────────────────────────────────────────


def test_pin_prod_detaches_at_prod(
    tmp_path: Path, bin_dir: dict[str, str], git_pair: dict[str, str]
) -> None:
    _git(
        Path(git_pair["PROD_CHECKOUT"]),
        "checkout",
        "-q",
        "--detach",
        git_pair["old_sha"],
    )
    env = _env(tmp_path, bin_dir, PROD_CHECKOUT=git_pair["PROD_CHECKOUT"])
    result = _run(env, "pin-prod")
    assert result.returncode == 0, result.stderr
    assert git_pair["prod_sha"][:7] in result.stdout
    head = _git(Path(git_pair["PROD_CHECKOUT"]), "rev-parse", "HEAD")
    assert head == git_pair["prod_sha"]


# ── lanes-overlay: dry-run anchors ───────────────────────────────────────────


def test_lanes_overlay_dry_run_no_writes(
    tmp_path: Path, bin_dir: dict[str, str]
) -> None:
    # A fixture that matches none of the anchors must abort without writing.
    env = _env(tmp_path, bin_dir)
    overlay = tmp_path / "inv"
    before = (overlay / "hosts.yml").read_text(encoding="utf-8")
    env["PRECIS_OVERLAY_DIR"] = str(overlay)
    result = _run(env, "lanes-overlay")
    assert result.returncode != 0
    assert (overlay / "hosts.yml").read_text(encoding="utf-8") == before

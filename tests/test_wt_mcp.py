"""``scripts/wt-mcp`` — the per-worktree MCP server driver (dev-loop-scratch D1).

Exercises ``scripts/wt_mcp.py`` against a real linked git worktree in
``tmp_path``, a fake ensure script that records every invocation (and
prints a fake ``--config`` entry with a fake token), and a fake ``docker``
on PATH that records every call. The live ``precis-mcp-http`` server, the
real ensure script and real docker are never touched.

What is pinned:

* the target guard refuses anything outside ``precis-wt-<tree>``;
* ``up`` / ``status`` / ``down`` round-trip: the ensure script is called
  with the worktree overrides, the session config names the server
  ``precis`` on the chosen port, is mode 600, lives outside the checkout
  and its token never reaches stdout/stderr; ``down`` removes exactly this
  tree's containers, network, volume and state, and fails when a container
  survives;
* the watcher restarts a dead worktree server (probe ``down`` → plain
  ensure on the worktree name) and never names ``precis-mcp-http``;
* the migration guard fails closed: committed, staged or untracked
  migration files ``origin/prod`` lacks refuse ``up`` AND a watcher restart;
  a git failure refuses too;
* an ensure script without worktree mode is refused (it would serve the
  main checkout); rig/override env vars never reach the ensure script;
* one tree root per server name; port choice skips only this tree's own
  exact container names; ``down --root`` works from any checkout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: bash fakes on PATH, git worktrees"
)

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import wt_mcp

FAKE_TOKEN = "fake-token-0123456789abcdef0123456789abcdef"
#: The fake docker lists two foreign families on 8801 and 8802; the second
#: one's name starts with this tree's server name (``dl-x-extra``), which a
#: prefix match would wrongly treat as our own and free its port.
EXPECTED_PORT = 8803


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Path]]:
    """A main checkout + linked worktree, fake ensure, fake docker, own state."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.email", "t@example.com")
    _git(main, "config", "user.name", "t")
    (main / "src").mkdir()
    (main / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(main, "add", ".")
    _git(main, "commit", "-q", "-m", "init")
    _git(main, "update-ref", "refs/remotes/origin/prod", "HEAD")
    wt = tmp_path / "trees" / "dl-x"
    wt.parent.mkdir()
    _git(main, "worktree", "add", "-q", "-b", "wt-dl-x", str(wt))
    (wt / "scripts").mkdir()
    # The tree's own wt-mcp, as a session would run it.
    for f in ("wt-mcp", "wt_mcp.py"):
        shutil.copy2(REPO / "scripts" / f, wt / "scripts" / f)
    # The repo copy of the ensure script, where `up` looks by default; the
    # fake below is substituted through PRECIS_WT_ENSURE for the round trips.
    (wt / "deploy" / "mcp-http").mkdir(parents=True)
    shutil.copy2(
        REPO / "deploy" / "mcp-http" / "precis-mcp-http-ensure.sh",
        wt / "deploy" / "mcp-http" / "precis-mcp-http-ensure.sh",
    )

    calls = tmp_path / "ensure-calls"
    ensure = tmp_path / "fake-ensure.sh"
    ensure.write_text(
        "#!/bin/bash\n"
        "# fake ensure with PRECIS_MCP_WORKTREE handling (the marker wt-mcp checks)\n"
        f'printf "%s %s %s\\n" "$PRECIS_MCP_HTTP_NAME" "${{1:-ensure}}" "$PRECIS_MCP_HTTP_PORT" >> "{calls}"\n'
        f'printf "%s\\n" "$PRECIS_MCP_SRC" > "{tmp_path / "ensure-src"}"\n'
        f'printf "%s\\n" "$PRECIS_MCP_WORKTREE" > "{tmp_path / "ensure-wt"}"\n'
        f'printf "%s\\n" "$PRECIS_MCP_HTTP_STATE" > "{tmp_path / "ensure-state"}"\n'
        f'env | grep "^PRECIS_MCP_" | sort > "{tmp_path / "ensure-env"}"\n'
        'if [ "${1:-}" = --config ]; then\n'
        '  printf \'{"type":"http","url":"http://127.0.0.1:%s/mcp","headers":{"Authorization":"Bearer %s"}}\\n\' '
        f'"$PRECIS_MCP_HTTP_PORT" "{FAKE_TOKEN}"\n'
        "fi\n"
        'if [ "${1:-}" = --status ]; then echo "proxy: fake"; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    ensure.chmod(0o755)

    docker_calls = tmp_path / "docker-calls"
    keep = tmp_path / "docker-keep"  # exists → `rm -f` "fails": the proxy stays listed
    down = (
        tmp_path / "docker-down"
    )  # exists → the daemon is unreachable: every call fails
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    docker = fakebin / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> "{docker_calls}"\n'
        f'if [ -e "{down}" ]; then echo "Cannot connect to the Docker daemon" >&2; exit 1; fi\n'
        'case "$*" in\n'
        '  *"ps -a"*)\n'
        '    printf "precis-wt-other-proxy\\t127.0.0.1:8801->8765/tcp\\n"\n'
        '    printf "precis-wt-dl-x-extra-proxy\\t127.0.0.1:8802->8765/tcp\\n"\n'
        f'    [ -e "{keep}" ] && printf "precis-wt-dl-x-proxy\\t127.0.0.1:{EXPECTED_PORT}->8765/tcp\\n"\n'
        "    ;;\n"
        '  *"ps --filter"*) printf "precis-wt-other-proxy\\n" ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    monkeypatch.setenv("PATH", f"{fakebin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PRECIS_WT_ENSURE", str(ensure))
    monkeypatch.setenv("PRECIS_WT_STATE_HOME", str(tmp_path / "state-home"))
    live = tmp_path / "live-state"
    (live / "cache").mkdir(parents=True)
    (live / "cache" / "v.bin").write_bytes(b"\x00" * 16)
    (live / "secrets").mkdir()
    (live / "secrets" / "PRECIS_MCP_TOKEN").write_text(
        "live-secret\n", encoding="utf-8"
    )
    monkeypatch.setenv("PRECIS_WT_LIVE_STATE", str(live))
    # Port choice must not depend on what the host happens to have bound.
    monkeypatch.setattr(wt_mcp, "_port_free", lambda port: True)
    yield {
        "main": main,
        "wt": wt,
        "calls": calls,
        "docker_calls": docker_calls,
        "docker_keep": keep,
        "docker_down": down,
        "ensure": ensure,
        "tmp": tmp_path,
    }


def _calls(rig: dict[str, Path]) -> list[str]:
    return (
        rig["calls"].read_text(encoding="utf-8").splitlines()
        if rig["calls"].exists()
        else []
    )


# ── the guard ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "precis-mcp-http",
        "precis-mcp-http-proxy",
        "precis-mcp-http-a",
        "precis-mcp-httpx",
        "precis-wt-",
        "precis-wt",
        "foo",
        "",
        "precis-wt-x; docker rm -f precis-mcp-http-a",
    ],
)
def test_guard_refuses_non_worktree_targets(bad: str) -> None:
    with pytest.raises(wt_mcp.WtMcpError, match="refusing target"):
        wt_mcp.assert_wt_target(bad)


def test_guard_accepts_worktree_targets() -> None:
    for ok in ("precis-wt-dl-d1", "precis-wt-dl-d1-proxy", "precis-wt-a.b_c-net"):
        assert wt_mcp.assert_wt_target(ok) == ok


def test_tree_requires_a_linked_worktree(rig: dict[str, Path]) -> None:
    with pytest.raises(wt_mcp.WtMcpError, match="not a linked git worktree"):
        wt_mcp.Tree.at(rig["main"])
    tree = wt_mcp.Tree.at(rig["wt"])
    assert tree.name == "dl-x"
    assert tree.server == "precis-wt-dl-x"
    assert tree.main == rig["main"].resolve()


# ── up / status / down round-trip ──────────────────────────────────────────


def test_up_status_down_round_trip(
    rig: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    tree = wt_mcp.Tree.at(rig["wt"])
    assert wt_mcp.cmd_up(tree) == 0
    out = capsys.readouterr()
    assert FAKE_TOKEN not in out.out + out.err, "the token must never be printed"
    assert f"precis-wt-dl-x up on 127.0.0.1:{EXPECTED_PORT}" in out.out

    # The ensure script saw the worktree overrides.
    assert _calls(rig) == [
        f"precis-wt-dl-x ensure {EXPECTED_PORT}",
        f"precis-wt-dl-x --config {EXPECTED_PORT}",
    ]
    assert (rig["tmp"] / "ensure-src").read_text(encoding="utf-8").strip() == str(
        tree.root
    )
    assert (rig["tmp"] / "ensure-wt").read_text(encoding="utf-8").strip() == "dl-x"
    assert (rig["tmp"] / "ensure-state").read_text(encoding="utf-8").strip() == str(
        tree.ensure_state
    )
    assert (tree.ensure_state / "root").read_text(encoding="utf-8").strip() == str(
        tree.root
    )

    # The session config: server name `precis`, this port, mode 600, outside the checkout.
    cfg = tree.config_file
    assert cfg.is_relative_to(tree.ensure_state), (
        "the token file never lives in the checkout"
    )
    assert not cfg.is_relative_to(tree.root)
    assert wt_mcp.file_mode(cfg) == 0o600
    assert wt_mcp.file_mode(tree.ensure_state) == 0o700
    assert wt_mcp.file_mode(tree.state_dir) == 0o700
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert list(data["mcpServers"]) == ["precis"]
    assert (
        data["mcpServers"]["precis"]["url"] == f"http://127.0.0.1:{EXPECTED_PORT}/mcp"
    )
    assert tree.port_file.read_text(encoding="utf-8").strip() == str(EXPECTED_PORT)
    # Seeded from the live state: cache and nothing secret.
    assert (tree.ensure_state / "cache" / "v.bin").is_file()
    assert not (tree.ensure_state / "secrets").exists()

    assert wt_mcp.cmd_status(tree) == 0
    out = capsys.readouterr()
    assert "precis-wt-dl-x" in out.out and "ensure:   proxy: fake" in out.out
    assert FAKE_TOKEN not in out.out + out.err

    assert wt_mcp.cmd_down(tree) == 0
    docker = rig["docker_calls"].read_text(encoding="utf-8").splitlines()
    assert "rm -f precis-wt-dl-x-proxy" in docker
    assert "rm -f precis-wt-dl-x-a" in docker
    assert "rm -f precis-wt-dl-x-b" in docker
    assert "network rm precis-wt-dl-x-net" in docker
    assert "volume rm precis-wt-dl-x-uv-cache" in docker
    assert not any("precis-mcp-http" in line for line in docker)
    assert not tree.state_dir.exists()
    assert not tree.ensure_state.exists()


def test_down_fails_when_a_container_survives(
    rig: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """A docker refusal must surface, not read as "down": the reaper keeps
    the tree on a non-zero exit."""
    tree = wt_mcp.Tree.at(rig["wt"])
    wt_mcp.cmd_up(tree)
    rig["docker_keep"].touch()
    assert wt_mcp.cmd_down(tree) == 1
    assert "NOT fully down" in capsys.readouterr().err
    assert tree.ensure_state.exists(), "state is kept while the server exists"


def test_up_is_idempotent_on_port(
    rig: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    tree = wt_mcp.Tree.at(rig["wt"])
    wt_mcp.cmd_up(tree)
    wt_mcp.cmd_up(tree)
    assert {c.split()[-1] for c in _calls(rig)} == {str(EXPECTED_PORT)}
    capsys.readouterr()


def test_up_refuses_past_the_cap(
    rig: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = wt_mcp.Tree.at(rig["wt"])
    monkeypatch.setattr(
        wt_mcp, "live_wt_proxies", lambda: [f"precis-wt-t{i}-proxy" for i in range(9)]
    )
    with pytest.raises(wt_mcp.WtMcpError, match="cap 9"):
        wt_mcp.cmd_up(tree)
    assert _calls(rig) == [], "the ensure script must not run past the cap"


def test_down_refuses_when_docker_cannot_answer(
    rig: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """A daemon that is down is not "no survivors": `--restart unless-stopped`
    backends would come back after the reaper deleted the tree."""
    tree = wt_mcp.Tree.at(rig["wt"])
    wt_mcp.cmd_up(tree)
    rig["docker_down"].touch()
    with pytest.raises(wt_mcp.WtMcpError, match="docker ps exited 1"):
        wt_mcp.cmd_down(tree)
    assert tree.ensure_state.exists()
    res = _cli(["down", "--root", str(tree.root), "--if-present"], cwd=rig["tmp"])
    assert res.returncode == 1 and "docker ps exited 1" in res.stderr
    assert tree.ensure_state.exists(), "state kept: the reaper must keep the tree"
    capsys.readouterr()


def test_down_by_root_refuses_a_colliding_tree(rig: dict[str, Path]) -> None:
    """Reaping a same-named tree under another parent must not tear down the
    owner's server."""
    tree = wt_mcp.Tree.at(rig["wt"])
    wt_mcp.cmd_up(tree)
    other = rig["tmp"] / "elsewhere" / "dl-x"
    other.mkdir(parents=True)
    with pytest.raises(wt_mcp.WtMcpError, match=f"belongs to {tree.root}"):
        wt_mcp.cmd_down(wt_mcp.Tree.for_root(other, rig["main"]), if_present=True)
    docker = rig["docker_calls"].read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("rm -f") for line in docker)
    assert tree.ensure_state.exists()
    # The owner itself still can.
    assert wt_mcp.cmd_down(wt_mcp.Tree.for_root(tree.root, rig["main"])) == 0


def test_manual_restart_is_guarded(rig: dict[str, Path]) -> None:
    tree = wt_mcp.Tree.at(rig["wt"])
    wt_mcp.cmd_up(tree)
    rig["calls"].unlink()
    mig = tree.root / "src" / "precis" / "migrations"
    mig.mkdir(parents=True)
    (mig / "0999_new.sql").write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(wt_mcp.WtMcpError, match="0999_new.sql"):
        wt_mcp.cmd_restart(tree)
    assert _calls(rig) == []
    (mig / "0999_new.sql").unlink()
    assert wt_mcp.cmd_restart(tree) == 0
    assert _calls(rig) == [f"precis-wt-dl-x --restart {EXPECTED_PORT}"]


def test_up_refuses_a_second_tree_root_for_the_same_name(rig: dict[str, Path]) -> None:
    tree = wt_mcp.Tree.at(rig["wt"])
    tree.ensure_state.mkdir(parents=True)
    (tree.ensure_state / "root").write_text("/somewhere/else/dl-x\n", encoding="utf-8")
    with pytest.raises(
        wt_mcp.WtMcpError, match="already belongs to /somewhere/else/dl-x"
    ):
        wt_mcp.cmd_up(tree)
    assert _calls(rig) == []


def test_ensure_env_drops_rig_and_override_knobs(
    rig: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = wt_mcp.Tree.at(rig["wt"])
    for var in wt_mcp._ENV_DROP:
        monkeypatch.setenv(var, "leak")
    monkeypatch.setenv("PRECIS_MCP_HTTP_PORT", "9999")  # overridden, not dropped
    env = wt_mcp.ensure_env(tree, EXPECTED_PORT)
    assert not any(v in env for v in wt_mcp._ENV_DROP)
    assert env["PRECIS_MCP_HTTP_PORT"] == str(EXPECTED_PORT)
    wt_mcp.cmd_up(tree)
    seen = (rig["tmp"] / "ensure-env").read_text(encoding="utf-8")
    assert "leak" not in seen
    assert "PRECIS_MCP_WORKTREE=dl-x" in seen


# ── the ensure script must carry worktree mode ─────────────────────────────


def test_ensure_script_defaults_to_the_repo_copy(
    rig: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = wt_mcp.Tree.at(rig["wt"])
    monkeypatch.delenv("PRECIS_WT_ENSURE")
    own = tree.root / wt_mcp.ENSURE_REL
    assert wt_mcp.ensure_script(tree) == own
    # No copy in the tree → the main checkout's; none there either → refused.
    own.unlink()
    with pytest.raises(wt_mcp.WtMcpError, match="not found"):
        wt_mcp.ensure_script(tree)
    main_copy = tree.main / wt_mcp.ENSURE_REL
    main_copy.parent.mkdir(parents=True)
    shutil.copy2(REPO / wt_mcp.ENSURE_REL, main_copy)
    assert wt_mcp.ensure_script(tree) == main_copy


def test_ensure_script_without_worktree_mode_is_refused(
    rig: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installed copy predating D1 would serve the MAIN checkout with the
    gate at warn and report success. Refused, with both ways out named."""
    tree = wt_mcp.Tree.at(rig["wt"])
    old = rig["tmp"] / "old-ensure.sh"
    old.write_text("#!/bin/bash\necho no worktree mode here\n", encoding="utf-8")
    old.chmod(0o755)
    monkeypatch.setenv("PRECIS_WT_ENSURE", str(old))
    with pytest.raises(wt_mcp.WtMcpError) as ei:
        wt_mcp.cmd_up(tree)
    msg = str(ei.value)
    assert "no worktree mode" in msg
    assert "PRECIS_WT_ENSURE" in msg and "mcp-http-install" in msg
    assert _calls(rig) == []


# ── the migration guard (fail closed) ──────────────────────────────────────


def test_up_refuses_a_migration_prod_has_not_applied(rig: dict[str, Path]) -> None:
    tree = wt_mcp.Tree.at(rig["wt"])
    mig = tree.root / "src" / "precis" / "migrations"
    mig.mkdir(parents=True)
    (mig / "0999_new.sql").write_text("SELECT 1;\n", encoding="utf-8")
    # Untracked.
    with pytest.raises(wt_mcp.WtMcpError, match="scripts/dev") as ei:
        wt_mcp.cmd_up(tree)
    assert "0999_new.sql" in str(ei.value)
    assert _calls(rig) == []
    # Staged, not committed.
    _git(tree.root, "add", ".")
    with pytest.raises(wt_mcp.WtMcpError, match="0999_new.sql"):
        wt_mcp.cmd_up(tree)
    # Committed but not on origin/prod.
    _git(tree.root, "commit", "-q", "-m", "feat(db): 0999")
    with pytest.raises(wt_mcp.WtMcpError, match="0999_new.sql"):
        wt_mcp.cmd_up(tree)
    assert _calls(rig) == []
    # Once origin/prod carries it, the tree is allowed.
    _git(rig["main"], "update-ref", "refs/remotes/origin/prod", "wt-dl-x")
    assert wt_mcp.unapplied_migrations(tree.root) == []


def test_migration_guard_refuses_on_any_git_failure(
    rig: dict[str, Path], tmp_path: Path
) -> None:
    tree = wt_mcp.Tree.at(rig["wt"])
    _git(rig["main"], "update-ref", "-d", "refs/remotes/origin/prod")
    with pytest.raises(wt_mcp.WtMcpError, match="cannot be compared with origin/prod"):
        wt_mcp.unapplied_migrations(tree.root)
    with pytest.raises(wt_mcp.WtMcpError, match="migration guard"):
        wt_mcp.cmd_up(tree)
    assert _calls(rig) == []
    notrepo = tmp_path / "not-a-repo"
    notrepo.mkdir()
    with pytest.raises(wt_mcp.WtMcpError, match="migration guard"):
        wt_mcp.unapplied_migrations(notrepo)


def test_watcher_refuses_a_restart_once_a_migration_appears(
    rig: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration added while the server is up must not reach the running
    prod read-write server through the auto-restart."""
    tree = wt_mcp.Tree.at(rig["wt"])
    wt_mcp.cmd_up(tree)
    rig["calls"].unlink()
    monkeypatch.setattr(wt_mcp, "probe", lambda port, timeout=5.0: "up")
    ticks = {"n": 0}
    real_fp = wt_mcp.src_fingerprint

    def fp(root: Path) -> tuple[int, int]:
        ticks["n"] += 1
        if ticks["n"] == 2:
            mig = root / "src" / "precis" / "migrations"
            mig.mkdir(parents=True)
            (mig / "0999_new.sql").write_text("SELECT 1;\n", encoding="utf-8")
        return real_fp(root)

    monkeypatch.setattr(wt_mcp, "src_fingerprint", fp)
    wt_mcp.cmd_watch(
        tree, poll_s=0.01, debounce_s=0.05, health_every_s=1e9, iterations=12
    )
    assert _calls(rig) == [], "no --restart with an unapplied migration in the tree"
    log = tree.watch_log.read_text(encoding="utf-8")
    assert "NOT restarting" in log and "0999_new.sql" in log


def test_watcher_recovery_is_also_guarded(
    rig: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = wt_mcp.Tree.at(rig["wt"])
    wt_mcp.cmd_up(tree)
    rig["calls"].unlink()
    mig = tree.root / "src" / "precis" / "migrations"
    mig.mkdir(parents=True)
    (mig / "0999_new.sql").write_text("SELECT 1;\n", encoding="utf-8")
    monkeypatch.setattr(wt_mcp, "probe", lambda port, timeout=5.0: "down")
    wt_mcp.cmd_watch(
        tree, poll_s=0.01, debounce_s=1e9, health_every_s=0.0, iterations=3
    )
    assert _calls(rig) == []
    assert "stays down" in tree.watch_log.read_text(encoding="utf-8")


# ── the watcher ────────────────────────────────────────────────────────────


def test_watcher_restarts_a_dead_worktree_server_only(
    rig: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server is gone (probe ``down``) → one plain ensure on the
    worktree name; ``precis-mcp-http`` is never named."""
    tree = wt_mcp.Tree.at(rig["wt"])
    wt_mcp.cmd_up(tree)
    rig["calls"].unlink()
    monkeypatch.setattr(wt_mcp, "probe", lambda port, timeout=5.0: "down")
    rc = wt_mcp.cmd_watch(
        tree, poll_s=0.01, debounce_s=1e9, health_every_s=0.0, iterations=3
    )
    assert rc == 0
    calls = _calls(rig)
    assert calls == [f"precis-wt-dl-x ensure {EXPECTED_PORT}"], calls  # once per 30 s
    assert not any("precis-mcp-http" in c for c in calls)
    assert "is down: running ensure" in tree.watch_log.read_text(encoding="utf-8")


def test_watcher_respawns_after_a_src_edit(
    rig: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = wt_mcp.Tree.at(rig["wt"])
    wt_mcp.cmd_up(tree)
    rig["calls"].unlink()
    monkeypatch.setattr(wt_mcp, "probe", lambda port, timeout=5.0: "up")
    ticks = {"n": 0}
    real_fp = wt_mcp.src_fingerprint

    def fp(root: Path) -> tuple[int, int]:
        ticks["n"] += 1
        if ticks["n"] == 2:
            (root / "src" / "b.py").write_text("y = 2\n", encoding="utf-8")
        return real_fp(root)

    monkeypatch.setattr(wt_mcp, "src_fingerprint", fp)
    wt_mcp.cmd_watch(
        tree, poll_s=0.01, debounce_s=0.05, health_every_s=1e9, iterations=12
    )
    assert _calls(rig) == [f"precis-wt-dl-x --restart {EXPECTED_PORT}"]
    assert "served again" in tree.watch_log.read_text(encoding="utf-8")


def test_watcher_only_ever_calls_ensure_on_the_worktree_name(
    rig: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt and braces: even a Tree whose name is forged to the live server
    is refused before the ensure script runs."""
    tree = wt_mcp.Tree.at(rig["wt"])
    forged = wt_mcp.Tree(root=tree.root, name="x", main=tree.main)
    monkeypatch.setattr(wt_mcp, "WT_PREFIX", "precis-mcp-http-")
    with pytest.raises(wt_mcp.WtMcpError, match="refusing target"):
        wt_mcp.run_ensure(forged, EXPECTED_PORT, "--restart")
    assert _calls(rig) == []


# ── the CLI wrapper and the reaper hook ────────────────────────────────────


def _cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(REPO / "scripts" / "wt-mcp"), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def test_cli_down_by_root_from_another_checkout(rig: dict[str, Path]) -> None:
    """``scripts/wt-mcp down --root <tree>`` (the reaper's form) runs from
    this checkout, needs no git in the target, tears down by name."""
    tree = wt_mcp.Tree.at(rig["wt"])
    wt_mcp.cmd_up(tree)
    res = _cli(["down", "--root", str(tree.root), "--if-present"], cwd=rig["tmp"])
    assert res.returncode == 0, res.stderr
    assert "precis-wt-dl-x down" in res.stdout
    assert FAKE_TOKEN not in res.stdout + res.stderr
    assert not tree.state_dir.exists() and not tree.ensure_state.exists()
    # A tree that never ran a server is a quick no-op …
    res = _cli(["down", "--root", str(tree.root), "--if-present"], cwd=rig["tmp"])
    assert res.returncode == 0 and "nothing to tear down" in res.stdout
    # … and a survivor is a failure the reaper acts on.
    wt_mcp.cmd_up(tree)
    rig["docker_keep"].touch()
    res = _cli(["down", "--root", str(tree.root), "--if-present"], cwd=rig["tmp"])
    assert res.returncode == 1 and "NOT fully down" in res.stderr


def test_reap_worktrees_tears_down_by_name_and_keeps_the_tree_on_failure() -> None:
    src = (REPO / "scripts" / "reap-worktrees").read_text(encoding="utf-8")
    hook = src.index('scripts/wt-mcp down --root "$path" --if-present')
    assert hook < src.index('git worktree remove "$path"')
    assert '"$path/scripts/wt-mcp"' not in src, "never the tree's own script"
    assert "TIMEOUT_RUNNER=(timeout 240)" in src
    block = src[hook : src.index('git worktree remove "$path"')]
    assert "NOT removing the tree" in block and "continue" in block


def test_ensure_script_worktree_mode_is_opt_in() -> None:
    """The repo copy of the ensure script: worktree behaviour hangs off
    PRECIS_MCP_WORKTREE only, the live name refuses it, and the tree's own
    auto-migrate can never beat the refuse gate."""
    src = (REPO / "deploy" / "mcp-http" / "precis-mcp-http-ensure.sh").read_text(
        encoding="utf-8"
    )
    assert "PRECIS_MCP_DB_IP PRECIS_MCP_WORKTREE; do" in src
    assert '[ -z "${PRECIS_MCP_WORKTREE:-}" ] || return 0' in src
    assert (
        'if [ ! -f "${SRC_REPO}/.git/HEAD" ] && [ ! -f "${SRC_REPO}/.git" ]; then'
        in src
    )
    assert "SCHEMA_GATE=refuse" in src and "SCHEMA_GATE=warn" in src
    assert '${WT_ENVS[@]+"${WT_ENVS[@]}"}' in src
    assert "-e PRECIS_AUTO_MIGRATE=0" in src

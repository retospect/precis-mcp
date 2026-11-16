"""Deploy-lint: the dedicated builder user (`coder`) is least-privilege.

roles/precis_builder_host runs the builder worker, the lane CLIs, the
checkout and the MCP tunnel as `precis_builder_user`. For anything but
`deploy` the role creates the account (no password, no sudo, no extra
groups), keeps it OFF the root-equivalent docker group (rootless docker on
Linux, its own colima on macOS), gives it a `.pgpass` with only the DB login
its worker uses, and confines the tunnel key to the one forward. Pure
YAML/Jinja walks, no ansible needed (same approach as
test_deploy_orphan_worker_lanes.py). The deploy default must keep rendering
what it rendered before.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import jinja2
import pytest
import yaml

_DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
_ROLE = _DEPLOY / "roles" / "precis_builder_host"
_PGPASS = _DEPLOY / "roles" / "pgpass"


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _tasks(name: str = "main.yml") -> list[dict[str, Any]]:
    return _yaml(_ROLE / "tasks" / name)


def _task(tasks: list[dict[str, Any]], prefix: str) -> dict[str, Any]:
    found = [t for t in tasks if str(t.get("name", "")).startswith(prefix)]
    assert len(found) == 1, f"expected one task starting {prefix!r}, got {len(found)}"
    return found[0]


def _defaults() -> dict[str, Any]:
    return _yaml(_ROLE / "defaults" / "main.yml")


def _env() -> jinja2.Environment:
    """Jinja with the two ansible filters the role's expressions use."""
    env = jinja2.Environment(trim_blocks=True, keep_trailing_newline=True)
    env.filters["combine"] = _combine
    env.filters["bool"] = lambda v: v is True or str(v).lower() in ("true", "yes", "1")
    return env


def _eval(expr: str, **ctx: Any) -> str:
    return _env().from_string(expr).render(**ctx)


def _combine(base: dict[str, Any], *more: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for m in more:
        out.update(m)
    return out


# ── defaults: deploy unchanged, coder managed ──────────────────────────


def test_deploy_keeps_its_home_and_manages_nothing() -> None:
    d = _defaults()
    for os_family, home in (("darwin", "/Users/deploy"), ("linux", "/home/deploy")):
        ctx = {"os_family": os_family, "precis_builder_user": "deploy"}
        assert _eval(d["precis_builder_home"], **ctx) == home
        assert _eval(d["precis_builder_manage_user"], **ctx) == "False"
        assert _eval(d["precis_builder_rootless_docker"], **ctx) == "False"


def test_coder_home_follows_the_user_and_is_managed() -> None:
    d = _defaults()
    for os_family, home in (("darwin", "/Users/coder"), ("linux", "/home/coder")):
        ctx = {"os_family": os_family, "precis_builder_user": "coder"}
        assert _eval(d["precis_builder_home"], **ctx) == home
        assert _eval(d["precis_builder_manage_user"], **ctx) == "True"
    linux = {"os_family": "linux", "precis_builder_user": "coder"}
    mac = {"os_family": "darwin", "precis_builder_user": "coder"}
    rootless = d["precis_builder_rootless_docker"]
    assert _eval(rootless, precis_builder_manage_user=True, **linux) == "True"
    assert _eval(rootless, precis_builder_manage_user=True, **mac) == "False", (
        "macOS uses colima, never the Linux rootless path"
    )


def test_the_role_names_no_fixed_home() -> None:
    """No path in the role hardcodes deploy's home: the user is a variable."""
    for path in [*(_ROLE / "tasks").glob("*.yml"), _ROLE / "defaults" / "main.yml"]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"/(Users|home)/deploy", text), path.name


def test_pgpass_roles_follow_the_workers_dsn() -> None:
    roles = _defaults()["precis_builder_pg_roles"]
    assert len(roles) == 1
    assert _eval(roles[0]) == "agent_rw"
    assert _eval(roles[0], precis_db_own_cutover_done=True) == "precis_system"
    for forbidden in ("admin", "agent_ro", "precis_owner", "precis_ingress"):
        assert forbidden not in roles[0]


# ── the account ────────────────────────────────────────────────────────


def test_linux_account_is_locked_ungrouped_and_private() -> None:
    tasks = _tasks("user.yml")
    acct = _task(tasks, "Builder user account (Linux")
    user = acct["ansible.builtin.user"]
    assert user["password_lock"] is True
    assert user["groups"] == "" and user["append"] is False
    assert user["system"] is True and user["shell"] == "/bin/bash"
    assert "password" not in user
    home = _task(tasks, "Builder home is private")
    assert home["ansible.builtin.file"]["mode"] == "0700"
    text = (_ROLE / "tasks" / "user.yml").read_text(encoding="utf-8")
    assert "sudo" not in yaml.safe_dump(acct).lower()
    assert "docker" not in yaml.safe_dump(acct).lower()
    assert "NOPASSWD" not in text


def test_macos_account_is_a_standard_user_checked_against_admin() -> None:
    block = _task(_tasks("user.yml"), "Builder user account (macOS)")["block"]
    create = _task(block, "Create the builder account")
    cmd = " ".join(str(create["ansible.builtin.command"]).split())
    assert "sysadminctl -addUser" in cmd
    assert "-admin" not in cmd and "-password" not in cmd
    assert "-GID" in cmd, "own primary group, like the Linux account"
    guard = _task(block, "The builder account must not be an administrator")
    assert "_builder_admin_probe.rc != 0" in str(
        guard["ansible.builtin.assert"]["that"]
    )


def test_deploy_is_never_created_by_this_role() -> None:
    main = _tasks()
    for prefix in ("Builder user account", "Builder user's own .pgpass"):
        assert _task(main, prefix)["when"] == "precis_builder_manage_user | bool"
    refuse = _task(_tasks("user.yml"), "Refuse to manage the deploy account")
    assert "!= 'deploy'" in refuse["ansible.builtin.assert"]["that"]


# ── docker: never the docker group for coder ───────────────────────────


def test_docker_group_is_deploy_only() -> None:
    grp = _task(_tasks(), "Put the deploy user in the docker group")
    assert "not (precis_builder_manage_user | bool)" in grp["when"]
    assert grp["ansible.builtin.user"]["groups"] == "docker"
    # Nothing in the managed-user path adds a group.
    for name in ("user.yml", "docker_rootless.yml", "colima.yml"):
        text = (_ROLE / "tasks" / name).read_text(encoding="utf-8")
        assert not re.search(r"groups:\s*docker", text), name


def test_system_docker_install_is_skipped_for_the_rootless_user() -> None:
    main = _tasks()
    for prefix in ("Check for an existing docker daemon", "Install docker + compose"):
        assert (
            "not (precis_builder_rootless_docker | bool)" in _task(main, prefix)["when"]
        )
    rootless = _task(main, "Rootless docker for the builder user")
    assert rootless["when"] == "precis_builder_rootless_docker | bool"


def test_rootless_docker_setup_covers_the_moving_parts() -> None:
    tasks = _tasks("docker_rootless.yml")
    packages = {
        p
        for t in tasks
        if "ansible.builtin.apt" in t
        for p in (
            t["ansible.builtin.apt"]["name"]
            if isinstance(t["ansible.builtin.apt"]["name"], list)
            else [t["ansible.builtin.apt"]["name"]]
        )
    }
    assert {"uidmap", "docker-ce-rootless-extras"} <= packages
    text = (_ROLE / "tasks" / "docker_rootless.yml").read_text(encoding="utf-8")
    assert "/etc/subuid" in text and "/etc/subgid" in text
    install = _task(tasks, "Install the rootless docker daemon")
    assert install["ansible.builtin.command"].startswith(
        "dockerd-rootless-setuptool.sh install"
    )
    assert install["become_user"] == "{{ precis_builder_user }}"
    linger = _task(tasks, "Enable linger")
    assert linger["ansible.builtin.command"].startswith("loginctl enable-linger")
    sock = _task(tasks, "Record the rootless docker socket")
    assert (
        sock["ansible.builtin.set_fact"]["_builder_docker_host"]
        == "unix:///run/user/{{ _builder_uid }}/docker.sock"
    )


def test_macos_user_gets_its_own_colima_unit() -> None:
    main = _task(_tasks(), "The builder user's own colima VM")
    assert "precis_builder_manage_user | bool" in main["when"]
    unit = _tasks("colima.yml")[0]["vars"]
    assert unit["service_unit_run_as"] == "{{ precis_builder_user }}"
    assert unit["service_unit_schedule"]["mode"] == "interval"
    assert not re.search(r"[<>&]", unit["service_unit_exec"]), (
        "the plist embeds the command unescaped"
    )


# ── the worker unit's environment ──────────────────────────────────────


def _unit_env(**ctx: Any) -> dict[str, Any]:
    task = _task(_tasks(), "render + load the builder worker unit")
    expr = task["vars"]["service_unit_env"]
    env = _env()
    base: dict[str, Any] = {
        "precis_shared_env": {"SHARED": "1"},
        "precis_builder_venv": "/opt/precis/venv",
        "precis_builder_dsn": "postgresql://x@h:1/db",
        "precis_fix_repo_dir": ctx["precis_builder_home"] + "/precis-fix-repo",
        "precis_autocatpath_kinetics_seconds": 60,
        "precis_builder_rootless_docker": False,
    }
    rendered = env.from_string(expr).render(**{**base, **ctx})
    return eval(rendered, {})


def test_unit_env_points_docker_at_the_rootless_socket() -> None:
    env = _unit_env(
        os_family="linux",
        precis_builder_home="/home/coder",
        precis_builder_rootless_docker=True,
        _builder_uid="1234",
        _builder_docker_host="unix:///run/user/1234/docker.sock",
    )
    assert env["DOCKER_HOST"] == "unix:///run/user/1234/docker.sock"
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1234"
    assert env["HOME"] == "/home/coder"
    assert env["PGPASSFILE"] == "/home/coder/.pgpass"
    assert env["PRECIS_SLICE_ROOT"].startswith("/home/coder/")
    assert env["PRECIS_DEVIN_BIN"] == "/home/coder/.local/bin/devin"


def test_unit_env_for_deploy_and_macos_is_what_it_was() -> None:
    linux = _unit_env(os_family="linux", precis_builder_home="/home/deploy")
    assert "DOCKER_HOST" not in linux, "deploy on Linux uses the system socket"
    assert "XDG_RUNTIME_DIR" not in linux
    mac = _unit_env(os_family="darwin", precis_builder_home="/Users/coder")
    assert mac["DOCKER_HOST"] == "unix:///Users/coder/.colima/docker.sock"
    assert "XDG_RUNTIME_DIR" not in mac


def test_rootless_socket_parent_is_granted_to_the_codex_sandbox() -> None:
    """run_slice grants the DOCKER_HOST socket's parent as a writable root
    unless it is $HOME or an ancestor; /run/user/<uid> is neither."""
    from pathlib import PurePosixPath

    sock_dir = PurePosixPath(
        "unix:///run/user/1234/docker.sock".removeprefix("unix://")
    ).parent
    home = PurePosixPath("/home/coder")
    assert sock_dir != home and not home.is_relative_to(sock_dir)


# ── pgpass: only the worker's login, nothing copied into the repo ──────


def _render_pgpass(roles: list[str], secrets: dict[str, str]) -> str:
    env = _env()
    src = (_PGPASS / "templates" / "pgpass-user.j2").read_text(encoding="utf-8")
    return env.from_string(src).render(
        ansible_managed="managed",
        pgpass_user="coder",
        pgpass_user_roles=roles,
        pgpass_host="db.example",
        pgpass_port=6432,
        pgpass_database="precis_prod",
        lookup=lambda kind, name: secrets[name],
    )


def test_user_pgpass_carries_only_the_named_roles_lines() -> None:
    secrets = {
        "vault_pg_agent_rw_pass": "rw-secret",
        "vault_pg_admin_pass": "admin-secret",
        "vault_pg_agent_ro_pass": "ro-secret",
    }
    out = _render_pgpass(["agent_rw"], secrets)
    lines = [x for x in out.splitlines() if x and not x.startswith("#")]
    assert lines == [
        "db.example:6432:precis_prod:agent_rw:rw-secret",
        "db.example:6432:*:agent_rw:rw-secret",
    ]
    assert "admin" not in out and "ro-secret" not in out


def test_user_pgpass_task_is_private_and_no_log() -> None:
    tasks = _yaml(_PGPASS / "tasks" / "user.yml")
    place = _task(tasks, "Deploy .pgpass for")
    assert place["ansible.builtin.template"]["mode"] == "0600"
    assert place["ansible.builtin.template"]["owner"] == "{{ pgpass_user }}"
    assert place["no_log"] is True
    check = _task(tasks, "Assert the user's vault password")
    assert check["no_log"] is True
    # The variable name is assembled, never a literal credential name.
    assert not re.search(
        r"vault_[a-z]", (_PGPASS / "tasks" / "user.yml").read_text(encoding="utf-8")
    )


# ── the tunnel key is the user's own and confined ──────────────────────


def test_tunnel_key_is_confined_to_the_one_forward() -> None:
    block = _task(_tasks(), "Builder MCP tunnel")["block"]
    auth = _task(block, "Authorize the key on the MCP host")
    opts = auth["ansible.posix.authorized_key"]["key_options"]
    for needle in (
        "restrict",
        "port-forwarding",
        "permitopen=",
        'command="/bin/false"',
    ):
        assert needle in opts, needle
    gen = _task(block, "Generate the builder tunnel key")
    assert gen["become_user"] == "{{ precis_builder_user }}"
    assert _defaults()["precis_builder_tunnel_key"].startswith(
        "{{ precis_builder_home }}/"
    )


def test_checkout_is_owned_by_the_builder_user_and_home_stays_private() -> None:
    main = _tasks()
    parent = _task(main, "Ensure the slice checkout parent")
    f = parent["ansible.builtin.file"]
    assert f["owner"] == "{{ precis_builder_user }}"
    assert "'0700'" in f["mode"]
    clone = _task(main, "Clone/refresh the slice checkout")
    assert clone["become_user"] == "{{ precis_builder_user }}"
    unit = _task(main, "render + load the builder worker unit")
    assert unit["vars"]["service_unit_run_as"] == "{{ precis_builder_user }}"


def test_builder_user_gets_a_user_npm_prefix_and_a_first_devin_install() -> None:
    tasks = _tasks("user.yml")
    npmrc = _task(tasks, "Builder user's npm prefix")
    c = npmrc["ansible.builtin.copy"]
    assert c["dest"] == "{{ precis_builder_home }}/.npmrc"
    assert c["content"] == "prefix={{ precis_builder_home }}/.local\n"
    assert c["owner"] == "{{ precis_builder_user }}"
    assert c["mode"] == "0644"
    assert "when" not in npmrc, "npm prefix applies to Linux and macOS"
    bindir = _task(tasks, "Builder user's ~/.local/bin")
    f = bindir["ansible.builtin.file"]
    assert f["path"] == "{{ precis_builder_home }}/.local/bin"
    assert f["state"] == "directory" and f["owner"] == "{{ precis_builder_user }}"
    devin = _task(tasks, "First devin install")
    assert devin["become_user"] == "{{ precis_builder_user }}"
    assert devin["args"]["creates"] == "{{ precis_builder_home }}/.local/bin/devin"
    assert "https://cli.devin.ai/install.sh" in devin["ansible.builtin.shell"]
    assert devin["timeout"] > 0
    assert "failed_when" not in devin and "ignore_errors" not in devin


# ── the builder's own heartbeat identity (gr695674) ───────────────────
# A non-deploy builder user cannot ride the deploy unit's heartbeat: the
# probe sees a different home (/home/coder is 0700), so <host>'s
# resource_slots rows carry none of the builder's tokens and run_slice
# never places. Sharing the name is not an option either — two heartbeat
# passes under one host would retract each other's tokens every cycle.
# The unit therefore runs `heartbeat` itself under <host>-<user>, the
# same key the ssh_node claim reads (reserve_host == PRECIS_HOST_NAME).


def _exec(**ctx: Any) -> str:
    task = _task(_tasks(), "render + load the builder worker unit")
    return _eval(task["vars"]["service_unit_exec"], **ctx)


def _slots_host(**ctx: Any) -> str:
    return _eval(_defaults()["precis_builder_slots_host"], **ctx)


def test_slots_host_is_the_inventory_name_plus_the_user() -> None:
    assert (
        _slots_host(
            precis_builder_user="coder",
            precis_shared_env={},
            inventory_hostname="maple",
        )
        == "maple-coder"
    )
    # An overlay PRECIS_HOST_NAME is the heartbeat's real base — the
    # builder's identity follows it, never the bare inventory name.
    assert (
        _slots_host(
            precis_builder_user="coder",
            precis_shared_env={"PRECIS_HOST_NAME": "maple2"},
            inventory_hostname="maple",
        )
        == "maple2-coder"
    )


def test_managed_builder_heartbeats_under_its_own_host_name() -> None:
    """On a Linux host with precis_builder_user != deploy the unit runs
    the heartbeat pass and advertises under <host>-<user> — the key the
    claim's resource_slots read uses."""
    env = _unit_env(
        os_family="linux",
        precis_builder_home="/home/coder",
        precis_builder_manage_user=True,
        precis_builder_slots_host="maple-coder",
    )
    assert env["PRECIS_HOST_NAME"] == "maple-coder"
    assert env["PRECIS_NODE"] == "maple-coder", (
        "the claim-gate/target_node identity matches the slots host so a "
        "minted land_slice routes back to the worktree's host"
    )
    argv = _exec(
        precis_builder_venv="/opt/precis/venv",
        precis_builder_manage_user=True,
        precis_builder_batch_size=4,
        precis_builder_idle_seconds=5,
    ).split()
    assert "--only" in argv and "heartbeat" in argv
    assert argv.count("--only") == 2


def test_deploy_builder_keeps_the_host_heartbeat_and_plain_exec() -> None:
    """Deploy parity: no identity keys, no second heartbeat — the host's
    own heartbeat already probes the same home."""
    env = _unit_env(os_family="linux", precis_builder_home="/home/deploy")
    assert "PRECIS_HOST_NAME" not in env
    assert "PRECIS_NODE" not in env
    argv = _exec(
        precis_builder_venv="/opt/precis/venv",
        precis_builder_manage_user=False,
        precis_builder_batch_size=4,
        precis_builder_idle_seconds=5,
    ).split()
    assert "heartbeat" not in argv
    assert argv == [
        "/opt/precis/venv/bin/precis",
        "worker",
        "--only",
        "job_ssh_node",
        "--batch-size",
        "4",
        "--idle-seconds",
        "5",
    ]


def test_the_slots_key_is_the_one_the_ssh_node_claim_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same PRECIS_HOST_NAME drives both ends of gr695674: the
    heartbeat writes resource_slots[<name>] and claim_executor_jobs'
    default_host (liveness.reserve_host) reads advertised[<name>] — so
    tokens the builder's own probes find land exactly where
    _placement_ok looks."""
    from precis import liveness
    from precis.workers.executors._common import _placement_ok

    monkeypatch.setenv("PRECIS_HOST_NAME", "maple-coder")
    host = liveness.reserve_host()
    assert host == "maple-coder"
    advertised = {"maple-coder": {"repo_checkout", "devin_bin", "git"}}
    meta = {"job_type": "run_slice", "params": {}, "adapter": "devin_tree"}
    assert _placement_ok(meta, advertised, host)
    # The pre-fix shape: only <host> advertised (deploy's heartbeat, blind
    # to the builder's home) — the same job stays queued.
    assert not _placement_ok(meta, {"maple": set()}, host)

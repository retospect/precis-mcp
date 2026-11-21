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

import json
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
    env.filters["to_json"] = json.dumps
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


def test_macos_account_has_no_blank_password_and_is_hidden() -> None:
    block = _task(_tasks("user.yml"), "Builder user account (macOS)")["block"]
    names = [t["name"] for t in block]

    def idx(prefix: str) -> int:
        return names.index(_task(block, prefix)["name"])

    setpw = _task(block, "Set a random builder password")
    cmd = " ".join(str(setpw["ansible.builtin.shell"]).split())
    assert "dscl . -passwd /Users/{{ precis_builder_user }}" in cmd
    assert "openssl rand -base64" in cmd
    assert setpw["no_log"] is True, "the password must never be printed"
    cond = setpw["when"]
    assert "_builder_user_probe.rc != 0" in cond
    assert "_builder_blank_before.rc == 0" in cond
    assert _task(block, "Probe for a blank builder password")["no_log"] is True

    hide = _task(block, "Hide the builder account")
    assert (
        hide["ansible.builtin.command"]
        == "dscl . -create /Users/{{ precis_builder_user }} IsHidden 1"
    )

    guard = _task(block, "The builder account must not accept a blank password")
    assert "_builder_blank_after.rc != 0" in str(
        guard["ansible.builtin.assert"]["that"]
    )
    after = _task(block, "Probe the builder password again")
    assert after["ansible.builtin.command"] == (
        'dscl . -authonly {{ precis_builder_user }} ""'
    )
    assert after["register"] == "_builder_blank_after"
    assert after["no_log"] is True
    assert idx("Create the builder account") < idx("Probe for a blank")
    assert idx("Probe for a blank") < idx("Set a random builder password")
    assert idx("Set a random builder password") < idx(
        "Probe the builder password again"
    )
    assert idx("Probe the builder password again") < idx(
        "The builder account must not accept"
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
        "precis_builder_coverage_db_dir": (
            ctx["precis_builder_home"] + "/.cache/precis/coverage-db"
        ),
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
    script = devin["ansible.builtin.shell"]
    assert "https://cli.devin.ai/install.sh" in script
    assert devin["timeout"] > 0
    assert "failed_when" not in devin and "ignore_errors" not in devin


def test_devin_installer_gets_no_stdin_and_success_is_the_binary() -> None:
    """The installer's interactive login exits 1 without a TTY: it runs with
    stdin from /dev/null, its exit code is ignored, and the play instead fails
    when the binary is missing or does not run."""
    devin = _task(_tasks("user.yml"), "First devin install")
    lines = [ln.strip() for ln in devin["ansible.builtin.shell"].splitlines()]
    assert 'bash "$installer" < /dev/null || true' in lines
    assert "curl | bash" not in " ".join(lines)
    assert not any(ln.startswith("curl") and "| bash" in ln for ln in lines)
    binary = "{{ precis_builder_home }}/.local/bin/devin"
    assert f"test -x {binary}" in lines
    assert f"{binary} --version > /dev/null" in lines
    assert lines.index(f"test -x {binary}") > lines.index(
        'bash "$installer" < /dev/null || true'
    ), "the binary check must come after the installer"
    head = devin["ansible.builtin.shell"].splitlines()[0]
    assert "-e" in head and "pipefail" in head, "a failed check must fail the play"
    assert devin["args"]["creates"] == binary


# ── bwrap under Ubuntu 24.04's AppArmor userns restriction ────────────
# codex sandboxes with bwrap, which needs an unprivileged user namespace;
# with kernel.apparmor_restrict_unprivileged_userns=1 it fails for every
# user. The role installs an unconfined, userns-only profile on the bwrap
# binaries and proves the builder user can run bwrap.

_USERNS_GATE = "(_builder_userns_probe.stdout | default('') | trim) == \"1\""


def test_bwrap_profile_grants_only_userns_and_covers_the_bwrap_binaries() -> None:
    text = (_ROLE / "files" / "precis-bwrap").read_text(encoding="utf-8")
    body = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    assert "userns," in body
    m = re.search(r"^profile precis-bwrap (\S+) flags=\(unconfined\) \{", text, re.M)
    assert m, "an unconfined profile named precis-bwrap"
    attach = m.group(1)
    assert "usr/bin/bwrap" in attach
    assert "codex-resources/bwrap" in attach, "codex's vendored bwrap too"
    rules = [
        ln for ln in body if ln.endswith(",") and not ln.startswith(("abi", "include"))
    ]
    assert rules == ["userns,"], "the profile grants nothing but userns"


def test_bwrap_profile_is_installed_only_on_linux_behind_the_sysctl_probe() -> None:
    main = _tasks()
    probe = _task(main, "Probe the AppArmor unprivileged-userns restriction")
    assert probe["when"] == 'os_family == "linux"'
    assert (
        probe["ansible.builtin.command"]
        == "sysctl -n kernel.apparmor_restrict_unprivileged_userns"
    )
    assert probe["changed_when"] is False and probe["failed_when"] is False
    reg = probe["register"]
    assert reg == "_builder_userns_probe"

    install = _task(main, "Install the bwrap AppArmor profile")
    c = install["ansible.builtin.copy"]
    assert c["src"] == "precis-bwrap"
    assert c["dest"] == "/etc/apparmor.d/precis-bwrap"
    assert (c["owner"], c["group"], c["mode"]) == ("root", "root", "0644")
    assert install["become"] is True
    assert 'os_family == "linux"' in install["when"]
    assert _USERNS_GATE in install["when"]

    load = _task(main, "Load the bwrap AppArmor profile")
    assert load["ansible.builtin.command"] == (
        "apparmor_parser -r /etc/apparmor.d/precis-bwrap"
    )
    assert load["become"] is True
    assert f"{install['register']} is changed" in load["when"]
    assert 'os_family == "linux"' in load["when"] and _USERNS_GATE in load["when"]


def test_bwrap_verify_runs_as_the_builder_user_and_fails_the_play() -> None:
    verify = _task(_tasks(), "Verify the builder user can run bwrap")
    assert verify["ansible.builtin.command"] == (
        "/usr/bin/bwrap --unshare-net --dev-bind / / true"
    )
    assert verify["become"] is True
    assert verify["become_user"] == "{{ precis_builder_user }}"
    assert verify["args"]["chdir"] == "/tmp"
    assert "failed_when" not in verify and "ignore_errors" not in verify
    assert 'os_family == "linux"' in verify["when"]
    assert _USERNS_GATE in verify["when"]


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


# ── a second repository's clone (PRECIS_SLICE_REPOS) ──────────────────


def test_slice_repos_renders_as_json_only_when_the_var_is_set() -> None:
    """Mirrors 20g: the clone is the operator's to place, so there is no
    default; unset or empty, the key is omitted and the host takes no
    second-repo slice."""
    repos = {"catpath": "/Users/coder/catpath"}
    env = _unit_env(
        os_family="darwin",
        precis_builder_home="/Users/coder",
        precis_builder_slice_repos=repos,
    )
    assert json.loads(env["PRECIS_SLICE_REPOS"]) == repos
    base = {"os_family": "darwin", "precis_builder_home": "/Users/coder"}
    assert "PRECIS_SLICE_REPOS" not in _unit_env(**base)
    assert "PRECIS_SLICE_REPOS" not in _unit_env(**base, precis_builder_slice_repos={})
    assert "precis_builder_slice_repos" not in _defaults(), "no default"


def test_the_builder_does_not_inherit_the_gateways_slice_repos() -> None:
    """20g's precis_slice_repos names deploy's clones; the builder account
    has its own."""
    env = _unit_env(
        os_family="linux",
        precis_builder_home="/home/coder",
        precis_slice_repos={"catpath": "/Users/deploy/catpath"},
    )
    assert "PRECIS_SLICE_REPOS" not in env


# ── the builder's colima VM size ───────────────────────────────────────

_COLIMA_PLAIN = "/bin/bash -c 'colima status || { colima stop --force; colima start; }'"


def _colima_exec(**ctx: Any) -> str:
    """The unit's exec with the role defaults for any size not given."""
    d = _defaults()
    sizes = {
        f"precis_builder_colima_{k}": d[f"precis_builder_colima_{k}"]
        for k in ("cpu", "memory", "disk")
    }
    sizes.update(ctx)
    unit = _tasks("colima.yml")[0]["vars"]
    flags = _eval(d["precis_builder_colima_start_flags"], **sizes)
    return _eval(unit["service_unit_exec"], precis_builder_colima_start_flags=flags)


def test_colima_sizes_default_to_no_flags() -> None:
    """Default unchanged: no flag, so the VM takes colima's own defaults, as
    deploy's does (this tree sizes neither)."""
    d = _defaults()
    assert {d[f"precis_builder_colima_{k}"] for k in ("cpu", "memory", "disk")} == {""}
    assert _colima_exec() == _COLIMA_PLAIN


def test_colima_sizes_render_as_integer_flags() -> None:
    cmd = _colima_exec(
        precis_builder_colima_cpu=4,
        precis_builder_colima_memory="16",
        precis_builder_colima_disk=200,
    )
    assert cmd.endswith("colima start --cpu 4 --memory 16 --disk 200; }'")
    only = _colima_exec(precis_builder_colima_memory=8)
    assert "colima start --memory 8; }" in only
    assert "--cpu" not in only and "--disk" not in only


@pytest.mark.parametrize("bad", ["0", "-2", "lots", "4; touch x", "1 --x"])
def test_colima_sizes_never_embed_free_text(bad: str) -> None:
    """The plist embeds the command unescaped: only positive integers pass."""
    cmd = _colima_exec(
        precis_builder_colima_cpu=bad,
        precis_builder_colima_memory=bad,
        precis_builder_colima_disk=bad,
    )
    assert cmd == _COLIMA_PLAIN


# ── the lander's coverage map ──────────────────────────────────────────


def test_builder_unit_env_uses_the_role_variable_for_the_coverage_dir() -> None:
    env = _unit_env(
        os_family="darwin",
        precis_builder_home="/Users/coder",
        precis_builder_coverage_db_dir="/Users/coder/.cache/precis/coverage-db",
    )
    assert env["PRECIS_COVERAGE_DB_DIR"] == "/Users/coder/.cache/precis/coverage-db"
    d = _defaults()
    assert (
        _eval(d["precis_builder_coverage_db_dir"], precis_builder_home="/Users/coder")
        == "/Users/coder/.cache/precis/coverage-db"
    )


def test_a_managed_builder_gets_its_own_coverage_map_copy() -> None:
    """land_slice runs scripts/ship --checked as the builder account, which
    reads $PRECIS_COVERAGE_DB_DIR/.coverage; 20g copies only deploy's."""
    task = _task(_tasks(), "Coverage map for the builder's lander")
    assert task["when"] == "precis_builder_manage_user | bool"
    imp = task["ansible.builtin.import_role"]
    assert imp == {"name": "precis_worker", "tasks_from": "coverage_map"}
    v = task["vars"]
    assert v["precis_coverage_db_dir"] == "{{ precis_builder_coverage_db_dir }}"
    assert v["precis_coverage_db_owner"] == "{{ precis_builder_user }}"
    assert v["precis_coverage_db_group"] == "{{ precis_builder_user }}"
    assert v["precis_coverage_db_make_parents"] is True, (
        "a fresh home has no ~/.cache/precis; the file module would create "
        "the parents root-owned"
    )

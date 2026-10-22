"""Render/structure pins for cluster-fileserver-move Phase 0 step 3.

The NAS share squashes root and one ``deploy`` uid serves the fleet, so:
no script root runs lives on NFS (ruling 8), root-run jobs do not write the
share, and every new mount or hosts line ships dark until the overlay
defines it. Nothing here touches a host.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2
import yaml

_DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
_ROLES = _DEPLOY / "roles"


def _tasks(role: str, name: str = "main.yml") -> list[dict[str, Any]]:
    data = yaml.safe_load((_ROLES / role / "tasks" / name).read_text(encoding="utf-8"))
    assert isinstance(data, list)
    return data


def _task(tasks: list[dict[str, Any]], name: str) -> dict[str, Any]:
    hits = [t for t in tasks if t.get("name") == name]
    assert len(hits) == 1, f"expected one task named {name!r}"
    return hits[0]


def _render(role: str, template: str, **values: object) -> str:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = bool
    src = (_ROLES / role / "templates" / template).read_text(encoding="utf-8")
    return env.from_string(src).render(**values)


# ── root-run scripts live on local disk ──────────────────────────────────────


def test_ops_script_dir_is_local() -> None:
    gv = yaml.safe_load(
        (_DEPLOY / "group_vars" / "all.yml").read_text(encoding="utf-8")
    )
    assert gv["ops_script_dir"].startswith("/usr/local/")


def test_config_pull_and_pip_audit_scripts_install_to_ops_script_dir() -> None:
    for role, task in (
        ("config_pull", "Deploy config-pull script"),
        ("api_monitors", "Deploy pip-audit script"),
    ):
        tpl = _task(_tasks(role), task)["ansible.builtin.template"]
        assert tpl["dest"].startswith("{{ ops_script_dir }}/"), role
        assert tpl["owner"] == "root", role
        assert "shared_mount" not in tpl["dest"], role


def test_cron_jobs_run_the_local_scripts() -> None:
    cp = _task(_tasks("config_pull"), "Schedule nightly config pull")
    job = cp["ansible.builtin.cron"]["job"]
    assert job.startswith("{{ ops_script_dir }}/config_pull.sh >> ")
    assert "shared_mount" not in job and "host_local_root" not in job
    pa = _task(_tasks("api_monitors"), "Schedule weekly pip-audit (as deploy)")
    assert pa["ansible.builtin.cron"]["job"].startswith("{{ ops_script_dir }}/")


# ── root-run writers do not write the share ─────────────────────────────────


def test_pip_audit_runs_as_deploy_and_root_copy_is_removed() -> None:
    tasks = _tasks("api_monitors")
    cron = _task(tasks, "Schedule weekly pip-audit (as deploy)")["ansible.builtin.cron"]
    assert cron["user"] == "deploy"
    old = _task(tasks, "Remove the root-run weekly pip-audit")["ansible.builtin.cron"]
    assert old == {"name": cron["name"], "user": "root", "state": "absent"}


def test_share_chowns_stop_at_cutover() -> None:
    # Root is squashed on the NAS: a root chown there would fail the play.
    for role, task in (
        ("config_pull", "Hand the share-side config dir to deploy"),
        ("api_monitors", "Hand the pip-audit log dir to deploy"),
    ):
        when = _task(_tasks(role), task)["when"]
        whens = when if isinstance(when, list) else [when]
        assert any("cluster_share_on_nas" in w for w in whens), role


_PULL_VARS: dict[str, object] = {
    "ansible_managed": "managed",
    "inventory_hostname": "node-worker",
    "shared_mount": "/mnt/cluster",
    "config_pull_stage_dir": "/var/lib/precis-config-pull",
    "os_family": "linux",
}


def test_config_pull_stages_locally_and_publishes_as_deploy() -> None:
    out = _render(
        "config_pull",
        "config_pull.sh.j2",
        **_PULL_VARS,
        group_names=["scheduler"],
        config_pull_local_only=False,
    )
    assert 'STAGE="/var/lib/precis-config-pull/node-worker"' in out
    assert 'DEST="/mnt/cluster/backups/configs/node-worker"' in out
    # Every root rsync targets the stage, never the share.
    for line in out.splitlines():
        if line.startswith("rsync "):
            assert '"${STAGE}' in line and "${DEST}" not in line, line
    assert 'sudo -n -u deploy rsync -rlpt --delete "${STAGE}/" "${DEST}/"' in out
    assert 'chown -R deploy "${STAGE}"' in out


def test_config_pull_db_node_stays_host_local() -> None:
    out = _render(
        "config_pull",
        "config_pull.sh.j2",
        **_PULL_VARS,
        group_names=["data"],
        config_pull_local_only=True,
        host_local_root="/opt/local",
        postgres_data_dir="/pg",
    )
    assert 'DEST="/opt/local/backups/configs/node-worker"' in out
    assert 'STAGE="${DEST}"' in out
    assert "sudo -n -u deploy" not in out and "/mnt/cluster" not in out
    assert "/usr/local/etc/pgbouncer/" in out


def test_config_pull_local_only_defaults_to_the_data_group() -> None:
    d = yaml.safe_load(
        (_ROLES / "config_pull" / "defaults" / "main.yml").read_text(encoding="utf-8")
    )
    assert d["config_pull_local_only"] == "{{ 'data' in group_names }}"
    assert d["config_pull_log"].startswith("/var/log/precis-")


# ── host_local_root: the DB node's operational paths ────────────────────────


def _jinja_bool(expr: str, **values: object) -> bool:
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    env.filters["bool"] = bool
    return env.from_string("{{ (" + expr + ") }}").render(**values) == "True"


def test_host_local_root_assert_refuses_the_share_only_at_cutover() -> None:
    tasks = yaml.safe_load(
        (_DEPLOY / "tasks" / "assert_host_local_root.yml").read_text(encoding="utf-8")
    )
    (task,) = tasks
    assert "cluster_share_on_nas" in task["when"]
    that = task["ansible.builtin.assert"]["that"]

    def passes(**v: object) -> bool:
        return all(_jinja_bool(e, **v) for e in that)

    share = {"shared_mount": "/mnt/cluster"}
    assert not passes(**share)  # unset -> defaults onto the share
    assert not passes(**share, host_local_root="/mnt/cluster")
    assert not passes(**share, host_local_root="/mnt/cluster/local")
    assert passes(**share, host_local_root="/opt/local-ops")
    assert passes(**share, host_local_root="/mnt/cluster-local")


def test_backups_and_pgbouncer_include_the_assert() -> None:
    for role in ("backups", "pgbouncer"):
        first = _tasks(role)[0]
        assert first["ansible.builtin.include_tasks"].endswith(
            "/tasks/assert_host_local_root.yml"
        ), role


def test_backups_config_dir_is_host_local_and_logs_reachable_by_pgbouncer() -> None:
    tasks = _tasks("backups")
    loop = _task(tasks, "Ensure backup directories")["loop"]
    prefix = "{{ host_local_root | default(shared_mount) }}/"
    assert all(p.startswith(prefix) for p in loop)
    logs = _task(tasks, "Ensure operational log directory")["ansible.builtin.file"]
    assert logs["path"] == prefix + "logs"
    assert "staff" in logs["group"]


def test_pgbouncer_precreates_its_logfiles_as_pgboss() -> None:
    t = _task(_tasks("pgbouncer"), "Ensure PgBouncer log files (pgboss-owned)")
    f = t["ansible.builtin.file"]
    assert f["owner"] == "pgboss" and f["state"] == "touch"
    assert f["modification_time"] == "preserve"
    assert set(t["loop"]) == {"pgbouncer.log", "pgbouncer-error.log"}


def test_host_local_log_rotation_renders_and_ships_dark() -> None:
    out = _render(
        "logrotate",
        "host_local_logs.conf.j2",
        ansible_managed="managed",
        host_local_root="/opt/local-ops",
    )
    rows = [line for line in out.splitlines() if line and not line.startswith("#")]
    pgb = next(line for line in rows if "pgbouncer.log" in line)
    assert pgb.startswith("/opt/local-ops/logs/pgbouncer.log ")
    assert pgb.split()[-2].endswith("pgbouncer.pid") and pgb.split()[-1] == "1"
    assert any(r.startswith("/opt/local-ops/logs/backup-*.log ") for r in rows)
    assert not any("pgbouncer-error.log" in r for r in rows)
    task = _task(_tasks("logrotate"), "Deploy host-local log rotation (DB node, macOS)")
    assert "(host_local_root | default(shared_mount)) != shared_mount" in task["when"]

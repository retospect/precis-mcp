"""gr465126: nothing evaluated the Prometheus alert rules or routed them.

The rendered Prometheus config must load the rule file and name an
Alertmanager, Alertmanager must have one route to the precis receiver, and the
Linux tasks must be wired so a deploy ends with those facts true. No promtool
binary is assumed: the rules file is parsed as YAML and its shape checked.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import jinja2
import pytest
import yaml

_ROLE = Path(__file__).resolve().parent.parent / "deploy" / "roles" / "monitoring"
_DEPLOY = _ROLE.parent.parent

_VARS = {
    "ansible_managed": "x",
    "os_family": "linux",
    "group_names": ["monitoring"],
    "ansible_host": "203.0.113.11",
    "nas_restic_textfile_dir": "/var/lib/prometheus/node-exporter",
    "postgres_host": "pg",
    "groups": {"all": ["pg", "n1"]},
    "hostvars": {
        "pg": {"ansible_host": "203.0.113.10"},
        "n1": {"ansible_host": "203.0.113.11"},
    },
    "monitoring_alertmanager_enabled": True,
    "monitoring_db_exporters": True,
    "monitoring_alertmanager_port": 9093,
    "monitoring_alertmanager_group_by": ["alertname", "instance"],
    "monitoring_alertmanager_group_wait": "30s",
    "monitoring_alertmanager_group_interval": "5m",
    "monitoring_alertmanager_repeat_interval": "12h",
    "monitoring_receiver_host": "127.0.0.1",
    "monitoring_receiver_port": 9095,
    "monitoring_receiver_venv": "/opt/precis/venv",
    "monitoring_receiver_user": "deploy",
    "monitoring_receiver_pgpass": "/home/deploy/.pgpass",
    "monitoring_receiver_dsn": "postgresql://u@h/db",
    "precis_shared_env": {"PRECIS_OPS_ALERT_TARGET": "discord/1/2"},
}


def _render(name: str, **over: object) -> str:
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    # Ansible-only filters the templates use.
    env.filters["bool"] = bool
    env.filters["combine"] = lambda a, *rest: {
        **a,
        **{k: v for r in rest for k, v in r.items()},
    }
    src = (_ROLE / "templates" / name).read_text(encoding="utf-8")
    return env.from_string(src).render(**{**_VARS, **over})


def test_prometheus_loads_rules_and_names_alertmanager() -> None:
    doc = yaml.safe_load(_render("prometheus.yml.j2"))
    assert doc["rule_files"] == ["alert_rules.yml"]
    targets = doc["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"]
    assert targets == ["127.0.0.1:9093"]


def test_prometheus_without_alertmanager_has_no_alerting_block() -> None:
    doc = yaml.safe_load(
        _render("prometheus.yml.j2", monitoring_alertmanager_enabled=False)
    )
    assert "alerting" not in doc and doc["rule_files"] == ["alert_rules.yml"]


def test_alertmanager_routes_everything_to_the_precis_receiver() -> None:
    doc = yaml.safe_load(_render("alertmanager.yml.j2"))
    assert doc["route"]["receiver"] == "precis"
    assert "routes" not in doc["route"], "one route: nothing can fall outside it"
    assert doc["route"]["group_by"] == ["alertname", "instance"]
    (recv,) = doc["receivers"]
    assert recv["name"] == "precis"
    (hook,) = recv["webhook_configs"]
    assert hook["url"] == "http://127.0.0.1:9095/alerts"
    assert hook["send_resolved"] is True


def test_rules_template_parses_into_nonempty_valid_rules() -> None:
    doc = yaml.safe_load(_render("alert_rules.yml.j2"))
    rules = [r for g in doc["groups"] for r in g["rules"]]
    assert rules, "rule file is empty: nothing could fire"
    for r in rules:
        assert r["alert"] and r["expr"]
        assert r["labels"]["severity"] in {"critical", "warning"}


def test_restic_alert_uses_eight_day_success_marker_and_detects_probe_loss() -> None:
    rules = {
        r["alert"]: r
        for g in yaml.safe_load(_render("alert_rules.yml.j2"))["groups"]
        for r in g["rules"]
    }
    assert (
        rules["NASResticHeartbeatOld"]["expr"]
        == "time() - precis_nas_restic_last_ok_timestamp_seconds > 691200"
    )
    assert rules["NASResticHeartbeatOld"]["for"] == "5m"
    assert (
        "node_textfile_mtime_seconds" in rules["NASResticHeartbeatMetricStale"]["expr"]
    )
    assert "> 7200" in rules["NASResticHeartbeatMetricStale"]["expr"]
    assert (
        'instance="203.0.113.11:9100"'
        in rules["NASResticHeartbeatMetricAbsent"]["expr"]
    )
    assert (
        "precis_nas_restic_last_ok_timestamp_seconds"
        in rules["NASResticHeartbeatMetricAbsent"]["expr"]
    )
    assert "NASResticHeartbeatOld" not in _render("alert_rules.yml.j2", group_names=[])


def _gnu_tool_env(tmp_path: Path) -> dict[str, str]:
    """PATH for the rendered probe: the script is a Linux deploy artifact and
    assumes GNU coreutils — ``timeout`` and ``stat -c %Y``. macOS has neither
    (BSD stat, no timeout), so the probe's ``|| stamp=0`` turns a tool
    failure into a fake "missing marker" reading. Supply shims for exactly
    the tools the host lacks; on GNU hosts the real binaries run."""
    shims: dict[str, str] = {}
    if shutil.which("timeout") is None:
        shims["timeout"] = '#!/bin/sh\nshift\nexec "$@"\n'
    if subprocess.run(["stat", "-c", "%Y", "/"], capture_output=True).returncode != 0:
        # BSD stat: translate the one flag the probe uses.
        shims["stat"] = (
            "#!/bin/sh\n"
            'if [ "$1" = "-c" ] && [ "$2" = "%Y" ]; then\n'
            "  shift 2\n"
            '  exec /usr/bin/stat -f %m "$@"\n'
            "fi\n"
            'exec /usr/bin/stat "$@"\n'
        )
    env = dict(os.environ)
    if not shims:
        return env
    bindir = tmp_path / "gnu-tools"
    bindir.mkdir()
    for name, body in shims.items():
        tool = bindir / name
        tool.write_text(body, encoding="utf-8")
        tool.chmod(0o755)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    return env


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only shell probe")
def test_restic_probe_reports_marker_mtime_or_zero_when_missing(
    tmp_path: Path,
) -> None:
    heartbeat = tmp_path / "restic-last-ok"
    metric = tmp_path / "nas_restic.prom"
    script = tmp_path / "probe.sh"
    script.write_text(
        _render(
            "nas-restic-heartbeat-metrics.sh.j2",
            nas_restic_textfile_dir=str(tmp_path),
            monitoring_restic_heartbeat_file=str(heartbeat),
        ),
        encoding="utf-8",
    )
    env = _gnu_tool_env(tmp_path)
    for expected in (0, 1_700_000_000, 0):
        if expected:
            heartbeat.write_text("ok", encoding="utf-8")
            os.utime(heartbeat, (expected, expected))
        else:
            heartbeat.unlink(missing_ok=True)
        subprocess.run(["sh", str(script)], check=True, env=env)
        assert metric.read_text(encoding="utf-8") == (
            f"precis_nas_restic_last_ok_timestamp_seconds {expected}\n"
        )
    assert list(tmp_path.glob("nas_restic.prom.*")) == []


def test_node_exporter_collects_restic_metric_on_monitoring_host_only() -> None:
    template = (
        _ROLE.parent / "node_exporter/templates/prometheus-node-exporter.default.j2"
    ).read_text(encoding="utf-8")
    env = jinja2.Environment()
    settings = {
        "node_exporter_extra_args": [],
        "node_exporter_fs_mount_exclude": "^/(dev|proc)($|/)",
        "nas_restic_textfile_dir": _VARS["nas_restic_textfile_dir"],
    }
    rendered = env.from_string(template)
    assert "--collector.textfile.directory=/var/lib/prometheus/node-exporter" in (
        rendered.render(**settings, group_names=["monitoring"])
    )
    assert "--collector.textfile.directory" not in rendered.render(
        **settings, group_names=[]
    )


def test_receiver_unit_is_loopback_and_carries_the_ops_target() -> None:
    text = _render("precis-alertmanager-receiver.service.j2")
    assert "alertmanager-receiver \\\n  --host 127.0.0.1 --port 9095" in text
    assert "Environment=PRECIS_OPS_ALERT_TARGET=discord/1/2" in text
    assert "Environment=PGPASSFILE=/home/deploy/.pgpass" in text


def test_linux_tasks_deploy_every_piece_and_verify() -> None:
    main = (_ROLE / "tasks" / "main.yml").read_text(encoding="utf-8")
    linux = (_ROLE / "tasks" / "linux.yml").read_text(encoding="utf-8")
    verify = (_ROLE / "tasks" / "linux_verify.yml").read_text(encoding="utf-8")
    for doc in (main, linux, verify):
        yaml.safe_load(doc)
    assert "include_tasks: linux.yml" in main
    assert "linux_verify.yml" in main
    assert "validate: \"{{ 'promtool check rules %s'" in main
    assert "promtool check config" in main
    assert "src: alertmanager.yml.j2" in linux
    assert "validate: amtool check-config %s" in linux
    assert "precis-alertmanager-receiver.service.j2" in linux
    assert "nas-restic-heartbeat-metrics.sh.j2" in linux
    assert "Seed NAS restic heartbeat metric" in linux
    assert "Schedule NAS restic heartbeat probe" in linux
    assert "/api/v1/alertmanagers" in verify and "/api/v1/rules" in verify
    # WAL replay makes Prometheus 503 (no JSON) after a restart: wait on
    # /-/ready first, and never dereference .json unconditionally (gr495696).
    assert "/-/ready" in verify
    assert verify.count("is defined") >= 2
    # macOS-only work stays macOS-only (Grafana has no Linux path here).
    assert main.count('when: os_family == "darwin"') >= 5


def test_playbook_targets_the_monitoring_group_and_inventory_defines_it() -> None:
    play = yaml.safe_load(
        (_DEPLOY / "playbooks" / "08-monitoring.yml").read_text(encoding="utf-8")
    )
    assert play[0]["hosts"] == "monitoring"
    inv = yaml.safe_load(
        (_DEPLOY / "inventory.example" / "hosts.example.yml").read_text(
            encoding="utf-8"
        )
    )
    assert "monitoring" in inv["all"]["children"]


def test_restart_handlers_exist_for_each_linux_service() -> None:
    names = set()
    for h in yaml.safe_load(
        (_ROLE / "handlers" / "main.yml").read_text(encoding="utf-8")
    ):
        names |= {h["name"], h.get("listen", h["name"])}
    assert {
        "restart prometheus (linux)",
        "restart alertmanager",
        "restart alertmanager receiver",
    } <= names


# ── gr514256: the redeploy bounces a stale receiver on the monitoring host ──


def test_redeploy_bounces_a_stale_receiver_on_monitoring_hosts() -> None:
    """The receiver runs /opt/precis/venv but belongs to the monitoring role —
    no install notify covers it, and monitoring hosts sit outside step-2's
    bounce groups. redeploy-precis.yml restarts it only when the running
    process predates the installed wheel (process-vs-files staleness, so a
    resumed deploy still catches it and a fresh unit is never bounced)."""
    redeploy = yaml.safe_load(
        (_DEPLOY / "redeploy-precis.yml").read_text(encoding="utf-8")
    )
    names = [p["name"] for p in redeploy]
    tag = "Bounce a stale alertmanager receiver on the monitoring host (gr514256)"
    assert tag in names
    # After the bounce/sweep plays, before the convergence assert — restarts
    # land while the run is still mid-flight, same as every other daemon.
    at = names.index(tag)
    assert (
        names.index(
            "Bounce + flag orphaned precis worker lanes (no play owns them here)"
        )
        < at
    )
    assert (
        names.index("Verify every managed precis venv is on the deployed commit") > at
    )
    play = redeploy[at]
    hosts = play["hosts"] if isinstance(play["hosts"], list) else [play["hosts"]]
    assert "monitoring" in hosts and "gateway" in hosts
    assert play["pre_tasks"][0]["ansible.builtin.meta"] == "end_host"
    text = yaml.safe_dump(play)
    assert "direct_url.json" in text
    # gr514256/r101: ActiveEnterTimestampUSec does not exist on systemd 255.
    assert "TimestampUSec" not in text
    script = next(
        t["ansible.builtin.shell"]
        for t in play["tasks"]
        if t["name"].startswith("Classify the receiver process")
    )
    assert "-p ActiveState --value" in script
    assert "-p ActiveEnterTimestamp --value" in script
    assert "date -d" in script
    # an active unit with an unparseable start time must restart, not skip
    assert "echo stale; exit 0" in script
    assert "precis-alertmanager-receiver" in text

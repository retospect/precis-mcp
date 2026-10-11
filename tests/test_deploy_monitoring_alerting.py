"""gr465126: nothing evaluated the Prometheus alert rules or routed them.

The rendered Prometheus config must load the rule file and name an
Alertmanager, Alertmanager must have one route to the precis receiver, and the
Linux tasks must be wired so a deploy ends with those facts true. No promtool
binary is assumed: the rules file is parsed as YAML and its shape checked.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import yaml

_ROLE = Path(__file__).resolve().parent.parent / "deploy" / "roles" / "monitoring"
_DEPLOY = _ROLE.parent.parent

_VARS = {
    "ansible_managed": "x",
    "os_family": "linux",
    "postgres_host": "pg",
    "groups": {"all": ["pg", "n1"]},
    "hostvars": {
        "pg": {"ansible_host": "203.0.113.10"},
        "n1": {"ansible_host": "203.0.113.11"},
    },
    "monitoring_alertmanager_enabled": True,
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
    assert "/api/v1/alertmanagers" in verify and "/api/v1/rules" in verify
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
    names = {
        h["name"]
        for h in yaml.safe_load(
            (_ROLE / "handlers" / "main.yml").read_text(encoding="utf-8")
        )
    }
    assert {
        "restart prometheus",
        "restart alertmanager",
        "restart alertmanager receiver",
    } <= names

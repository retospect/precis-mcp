"""Mount alert rules must catch a *hung* mount, not only a full one.

2026-09-30: /mnt/cluster hung on every client and nothing alerted, because the
only rule was ``node_filesystem_avail_bytes{...} == 0`` -- a hung NFS statfs
times out, so node_exporter sets ``node_filesystem_device_error`` and drops the
avail series instead of reporting 0.
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import yaml

_TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "deploy"
    / "roles"
    / "monitoring"
    / "templates"
    / "alert_rules.yml.j2"
)

_HOSTVARS = {
    "n1": {"ansible_host": "203.0.113.10"},
    "n2": {"ansible_host": "203.0.113.11"},
    "n3": {"ansible_host": "203.0.113.12"},
}


def _rules(groups: dict[str, list[str]]) -> dict[str, dict]:
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    text = env.from_string(_TEMPLATE.read_text(encoding="utf-8")).render(
        groups=groups, hostvars=_HOSTVARS, ansible_managed="x"
    )
    doc = yaml.safe_load(text)
    return {r["alert"]: r for g in doc["groups"] for r in g["rules"]}


def _balanced(expr: str) -> bool:
    return all(expr.count(a) == expr.count(b) for a, b in ("{}", "()", '""'))


def test_hang_rules_read_device_error_for_both_mounts() -> None:
    rules = _rules({})
    for name, mount in (
        ("NFSMountHung", "/mnt/cluster"),
        ("NASMountHung", "/mnt/archive/botshome"),
    ):
        r = rules[name]
        assert r["expr"] == f'node_filesystem_device_error{{mountpoint="{mount}"}} == 1'
        assert r["for"] == "5m"
        assert r["labels"]["severity"] == "critical"
        assert "hung or erroring" in r["annotations"]["summary"]
        assert "$labels.instance" in r["annotations"]["summary"]


def test_full_rules_kept_and_no_longer_claim_missing() -> None:
    rules = _rules({})
    assert "NFSMountMissing" not in rules and "NASMountMissing" not in rules
    for name, mount in (
        ("NFSMountFullOrEmpty", "/mnt/cluster"),
        ("NASMountFullOrEmpty", "/mnt/archive/botshome"),
    ):
        assert rules[name]["expr"] == (
            f'node_filesystem_avail_bytes{{mountpoint="{mount}"}} == 0'
        )
        assert "missing" not in rules[name]["annotations"]["summary"]


def test_absence_rules_restricted_to_group_instances() -> None:
    rules = _rules(
        {
            "nfs_clients": ["n1", "n2"],
            "nfs_servers": ["n3"],
            "nfs_mount_hosts": ["n1", "n2"],
            "nas_mount_hosts": ["n2"],
        }
    )
    nfs = rules["NFSMountAbsent"]["expr"]
    assert nfs == (
        'up{job="node", instance=~"203[.]0[.]113[.]10:9100|203[.]0[.]113[.]11:9100'
        '"} == 1 unless on(instance) '
        'node_filesystem_avail_bytes{mountpoint="/mnt/cluster"}'
    )
    nas = rules["NASMountAbsent"]["expr"]
    assert 'instance=~"203[.]0[.]113[.]11:9100"' in nas
    assert "203[.]0[.]113[.]10" not in nas
    assert nas.endswith('{mountpoint="/mnt/archive/botshome"}')
    for r in (rules["NFSMountAbsent"], rules["NASMountAbsent"]):
        assert _balanced(r["expr"])
        assert re.search(r"== 1 unless on\(instance\)", r["expr"])
        assert r["for"] == "5m" and r["labels"]["severity"] == "critical"


def test_absence_rules_omitted_without_groups() -> None:
    rules = _rules({})
    assert "NFSMountAbsent" not in rules and "NASMountAbsent" not in rules


def test_lazy_clients_and_symlink_servers_do_not_imply_expected_mounts() -> None:
    rules = _rules({"nfs_clients": ["n1", "n2"], "nfs_servers": ["n3"]})
    assert "NFSMountAbsent" not in rules
    assert "NASMountAbsent" not in rules
    assert "NFSMountHung" in rules and "NASMountHung" in rules


def test_explicit_nfs_membership_excludes_servers_and_non_clients() -> None:
    rules = _rules(
        {
            "nfs_clients": ["n1", "n3"],
            "nfs_servers": ["n3"],
            "nfs_mount_hosts": ["n1", "n1", "n2", "n3"],
        }
    )
    expr = rules["NFSMountAbsent"]["expr"]
    assert 'instance=~"203[.]0[.]113[.]10:9100"' in expr
    assert "203[.]0[.]113[.]11" not in expr
    assert "203[.]0[.]113[.]12" not in expr
    assert "NFSMountAbsent" not in _rules(
        {"nfs_servers": ["n3"], "nfs_mount_hosts": ["n3"]}
    )

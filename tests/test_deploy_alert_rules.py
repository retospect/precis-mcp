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
    env.filters["bool"] = bool
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


def _disk_matchers(expr: str) -> dict[str, tuple[str, str]]:
    """``{label: (op, regex)}`` from the first selector of a disk rule."""
    sel = re.search(r"node_filesystem_avail_bytes\{(.*?)\} /", expr)
    assert sel, expr
    return {
        m.group(1): (m.group(2), m.group(3))
        for m in re.finditer(r'(\w+)(!~|=~)"([^"]*)"', sel.group(1))
    }


def _selected(expr: str, fstype: str, mountpoint: str) -> bool:
    """Whether a series with these labels passes the rule's selector.

    PromQL regex matchers are fully anchored, hence ``fullmatch``.
    """
    for label, value in (("fstype", fstype), ("mountpoint", mountpoint)):
        op, rx = _disk_matchers(expr)[label]
        hit = re.fullmatch(rx, value) is not None
        if hit == (op == "!~"):
            return False
    return True


def test_disk_rules_alert_on_local_filesystems_only() -> None:
    # gr738880: one APFS container fired once per helper volume on the DB host
    # and again on every NFS client of the share it exports.
    rules = _rules({})
    for name in ("HighDiskUsage", "DiskNearlyFull"):
        expr = rules[name]["expr"]
        assert _balanced(expr)
        # numerator and denominator carry the same selector, or the division
        # would match series on a different label set
        sel = re.findall(r"\{[^}]*\}", expr)
        assert len(sel) == 2 and sel[0] == sel[1]
        for fstype, mp in (
            ("apfs", "/"),
            ("apfs", "/System/Volumes/Data"),
            ("ext4", "/"),
            ("ext4", "/var/lib/docker"),
            ("xfs", "/data"),
        ):
            assert _selected(expr, fstype, mp), (fstype, mp)
        for fstype in (
            "nfs",
            "nfs4",
            "autofs",
            "devfs",
            "devtmpfs",
            "cifs",
            "smbfs",
            "afpfs",
            "overlay",
            "squashfs",
            "fuse.sshfs",
        ):
            assert not _selected(expr, fstype, "/mnt/cluster"), fstype
        # tmpfs, ramfs and generic fuse can be real disks or fill RAM: watched
        for fstype in ("tmpfs", "ramfs", "fuseblk", "fuse.mergerfs", "vfat", "zfs"):
            assert _selected(expr, fstype, "/data"), fstype
        for vol in ("VM", "Preboot", "Update", "xarts", "iSCPreboot", "Hardware"):
            assert not _selected(expr, "apfs", f"/System/Volumes/{vol}"), vol
        # an unrelated mount that merely starts alike stays watched
        assert _selected(expr, "apfs", "/System/Volumes/DataSomething")


def test_disk_rules_name_mountpoint_and_free_fraction() -> None:
    rules = _rules({})
    warn, crit = rules["HighDiskUsage"], rules["DiskNearlyFull"]
    assert warn["expr"].endswith("< 0.15") and warn["for"] == "5m"
    assert warn["labels"]["severity"] == "warning"
    assert crit["expr"].endswith("< 0.07") and crit["for"] == "2m"
    assert crit["labels"]["severity"] == "critical"
    for r, pct in ((warn, "85"), (crit, "93")):
        s = r["annotations"]["summary"]
        assert s.startswith(f"Disk usage above {pct}% on ")
        assert "{{ $labels.instance }} {{ $labels.mountpoint }}" in s
        assert "{{ $value | humanizePercentage }} free" in s


def test_mount_rules_keep_their_own_selectors() -> None:
    # The disk rules' filter must not leak into the mount-health rules, which
    # exist to see nfs mounts.
    rules = _rules({})
    assert "fstype" not in rules["NFSMountFullOrEmpty"]["expr"]
    assert "fstype" not in rules["NASMountFullOrEmpty"]["expr"]

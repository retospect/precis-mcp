"""nfs_client's move to the NAS is gated on ``cluster_share_on_nas``.

Off (the default), every source, option string and regex renders exactly
the pre-move values, so applying the role before the cutover window cannot
unmount or remount the live share. On, the source is the NAS's ``cluster``
share, the old server's line is replaced, and an old mount still up is
handled (macOS unmounts it, Linux defers to the reboot). Expressions are
rendered with plain Jinja plus shims for the Ansible filters/tests they use;
nothing here runs Ansible or touches a host.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import jinja2
import yaml

_DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
_ROLE = _DEPLOY / "roles" / "nfs_client"

_OLD_SRC = "192.0.2.12:/opt/nfs/shared"
_NAS_SRC = "fileserver:/Volume1/cluster"
_OLD_OPTS_DARWIN = "hard,intr,nosuid,nodev,noatime"
_OLD_OPTS_LINUX = (
    "hard,intr,nosuid,nodev,noatime,_netdev,x-systemd.automount,"
    "x-systemd.mount-timeout=30"
)


def _env() -> jinja2.Environment:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v) == "True"
    env.filters["regex_escape"] = re.escape
    env.filters["split"] = lambda s: s.split()
    env.tests["match"] = lambda v, p: re.match(p, v) is not None
    env.tests["search"] = lambda v, p: re.search(p, v) is not None
    return env


def _vars(**over: object) -> dict[str, Any]:
    defaults = yaml.safe_load(
        (_ROLE / "defaults" / "main.yml").read_text(encoding="utf-8")
    )
    v: dict[str, Any] = {
        **defaults,
        "hostvars": {"node-data": {"lan_ip": "192.0.2.12"}},
        "nfs_server": "node-data",
        "nfs_export_path": "/opt/nfs",
        "shared_mount": "/mnt/cluster",
        "nas_lan_name": "fileserver",
        "nas_lan_ip": "192.0.2.30",
        "nas_cluster_export": "/Volume1/cluster",
    }
    v.update(over)
    return v


def _r(expr: str, v: dict[str, Any]) -> str:
    return _env().from_string(expr).render(**v).strip()


def _tasks(name: str = "main.yml") -> list[dict[str, Any]]:
    data = yaml.safe_load((_ROLE / "tasks" / name).read_text(encoding="utf-8"))
    assert isinstance(data, list)
    return data


def _task(name: str, file: str = "main.yml") -> dict[str, Any]:
    hits = [t for t in _tasks(file) if t.get("name") == name]
    assert len(hits) == 1, name
    return hits[0]


def _facts(v: dict[str, Any]) -> dict[str, str]:
    sf = _task("Pick the cluster share's source and options")[
        "ansible.builtin.set_fact"
    ]
    return {k: _r(e, v) for k, e in sf.items()}


def test_defaults_keep_the_flag_off() -> None:
    d = yaml.safe_load((_ROLE / "defaults" / "main.yml").read_text(encoding="utf-8"))
    assert d["cluster_share_on_nas"] is False


def test_flag_off_renders_the_pre_move_mount_exactly() -> None:
    f = _facts(_vars(cluster_share_on_nas=False))
    assert f["nfs_client_src"] == _OLD_SRC
    assert f["nfs_client_opts_darwin"] == _OLD_OPTS_DARWIN
    assert f["nfs_client_opts_linux"] == _OLD_OPTS_LINUX


def test_flag_off_needs_no_nas_variables() -> None:
    v = _vars(cluster_share_on_nas=False)
    for k in ("nas_lan_name", "nas_lan_ip", "nas_cluster_export"):
        del v[k]
    assert _facts(v)["nfs_client_src"] == _OLD_SRC


def test_flag_on_mounts_the_nas_hard_on_first_use() -> None:
    f = _facts(_vars(cluster_share_on_nas=True))
    assert f["nfs_client_src"] == _NAS_SRC
    darwin = f["nfs_client_opts_darwin"].split(",")
    linux = f["nfs_client_opts_linux"].split(",")
    for opts in (darwin, linux):
        assert "hard" in opts and "soft" not in opts
    assert {"resvport", "vers=3"} <= set(darwin)
    assert {"nofail", "x-systemd.automount", "_netdev"} <= set(linux)


def test_cutover_asserts_the_overlay_names_the_nas() -> None:
    t = _task("Refuse a cutover the overlay cannot describe")
    assert t["when"] == "cluster_share_on_nas | bool"
    that = " ".join(t["ansible.builtin.assert"]["that"])
    for k in ("nas_lan_name", "nas_lan_ip", "nas_cluster_export"):
        assert k in that


def _stale_regex(v: dict[str, Any]) -> re.Pattern[str]:
    v = {**v, **_facts(v)}
    t = _task("Remove stale fstab entries for shared_mount from another server (macOS)")
    return re.compile(_r(t["ansible.builtin.lineinfile"]["regexp"], v))


def test_darwin_stale_line_removal_spares_the_current_source() -> None:
    old_line = f"{_OLD_SRC} /mnt/cluster nfs {_OLD_OPTS_DARWIN} 0 0"
    nas_line = f"{_NAS_SRC} /mnt/cluster nfs hard,resvport 0 0"
    before = _stale_regex(_vars(cluster_share_on_nas=False))
    assert not before.search(old_line)  # early apply keeps the live line
    after = _stale_regex(_vars(cluster_share_on_nas=True))
    assert after.search(old_line)  # cutover removes the old server's line
    assert not after.search(nas_line)


def test_darwin_fstab_line_follows_the_flag() -> None:
    t = _task("Add shared NFS entry to /etc/fstab (macOS)")[
        "ansible.builtin.lineinfile"
    ]
    for flag, src, opts in (
        (False, _OLD_SRC, _OLD_OPTS_DARWIN),
        (True, _NAS_SRC, None),
    ):
        v = _vars(cluster_share_on_nas=flag)
        v.update(_facts(v))
        line = _r(t["line"], v)
        assert line.startswith(f"{src} /mnt/cluster nfs ")
        if opts:
            assert line == f"{src} /mnt/cluster nfs {opts} 0 0"
        assert re.match(_r(t["regexp"], v), line)


def test_darwin_unmounts_only_a_share_from_another_source() -> None:
    t = _task("Find a share at shared_mount from another source (macOS, cutover)")
    assert "cluster_share_on_nas" in t["when"]
    expr = t["ansible.builtin.set_fact"]["nfs_client_stale_mounts"]
    v = _vars(cluster_share_on_nas=True)
    v.update(_facts(v))

    def stale(lines: list[str]) -> str:
        return _r(expr, {**v, "nfs_client_darwin_mounts": {"stdout_lines": lines}})

    old = f"{_OLD_SRC} on /mnt/cluster (nfs, nodev, nosuid, automounted)"
    new = f"{_NAS_SRC} on /mnt/cluster (nfs, nodev, nosuid, automounted)"
    other = "fileserver:/Volume1/botshome on /mnt/archive/botshome (nfs)"
    assert stale([old, other]) == repr([old])
    assert stale([new, other]) == "[]"
    umount = _task("Unmount the old server's share (macOS, cutover)")
    assert umount["ansible.builtin.command"] == "/sbin/umount {{ shared_mount }}"


def test_linux_early_apply_keeps_the_mounted_task_and_cutover_writes_fstab_only() -> (
    None
):
    pre = _task("Mount NFS share (Linux)")
    assert "not (cluster_share_on_nas | bool)" in pre["when"]
    assert pre["ansible.posix.mount"]["state"] == "mounted"
    cut = _task("Point fstab at the NAS (Linux, cutover)")
    assert "cluster_share_on_nas | bool" in cut["when"]
    assert cut["ansible.posix.mount"]["state"] == "present"
    assert cut["ansible.posix.mount"]["path"] == "{{ shared_mount }}"


def test_linux_reboot_pending_while_the_old_share_is_mounted() -> None:
    expr = _task(
        "Decide whether the old share still holds the mount point (Linux, cutover)"
    )["ansible.builtin.set_fact"]["nfs_client_reboot_pending"]
    v = _vars(cluster_share_on_nas=True)
    v.update(_facts(v))

    def pending(lines: list[str]) -> str:
        return _r(expr, {**v, "nfs_client_linux_mounts": {"stdout_lines": lines}})

    assert pending(["systemd-1 autofs", f"{_OLD_SRC} nfs"]) == "True"
    assert pending(["systemd-1 autofs", f"{_NAS_SRC} nfs4"]) == "False"
    assert pending([]) == "False"


def test_canary_is_skipped_while_a_reboot_is_pending() -> None:
    for name in ("Verify NFS canary", "Fail if NFS not working"):
        when = _task(name)["when"]
        whens = when if isinstance(when, list) else [when]
        assert any("nfs_client_reboot_pending" in w for w in whens), name


def test_hosts_line_is_overlay_driven_and_dark_when_unset() -> None:
    (t,) = yaml.safe_load(
        (_DEPLOY / "tasks" / "nas_hosts_entry.yml").read_text(encoding="utf-8")
    )
    li = t["ansible.builtin.lineinfile"]
    assert li["path"] == "/etc/hosts"
    assert any("nas_lan_ip" in w for w in t["when"])
    assert any("nas_lan_name" in w for w in t["when"])
    v = _vars()
    line = _r(li["line"], v)
    assert line.startswith("192.0.2.30\tfileserver\t")
    rx = re.compile(_r(li["regexp"], v))
    assert rx.search("198.51.100.7 fileserver")  # an address change rewrites it
    assert not rx.search("192.0.2.30 fileserver2")
    autofs = yaml.safe_load(
        (_DEPLOY / "roles" / "autofs_client" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
    )
    assert any(
        str(x.get("ansible.builtin.include_tasks", "")).endswith("/nas_hosts_entry.yml")
        for x in autofs
    )


def test_dbbackup_mounts_on_the_data_group_only_and_ships_dark() -> None:
    plays = yaml.safe_load(
        (_DEPLOY / "playbooks" / "01-nfs.yml").read_text(encoding="utf-8")
    )
    db = [p for p in plays if "dbbackup" in p.get("tags", [])]
    assert len(db) == 1 and db[0]["hosts"] == "data"
    inc = db[0]["tasks"][0]["ansible.builtin.include_role"]
    assert inc == {"name": "nfs_client", "tasks_from": "dbbackup"}
    (block,) = _tasks("dbbackup.yml")
    assert any("nas_dbbackup_export" in w for w in block["when"])
    names = {t["name"] for t in block["block"]}
    assert "Add the dbbackup entry to /etc/fstab (macOS)" in names
    assert "Start the dbbackup automount (Linux)" in names

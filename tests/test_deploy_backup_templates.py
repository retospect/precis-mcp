"""Render-level pins for the backups role's fileserver-move knobs.

Defaults must reproduce today's behaviour (flags off / today's source path);
each new flag has an on-state asserted too. Also pins the nfs_client macOS
stale-fstab cleanup task and that logrotate stays on shared_mount.
"""

from __future__ import annotations

from pathlib import Path

import jinja2

_ROLES = Path(__file__).resolve().parent.parent / "deploy" / "roles"
_VARS: dict[str, object] = {
    "ansible_managed": "managed",
    "shared_mount": "/mnt/cluster",
    "nas_root": "/nas",
    "os_family": "linux",
    "pg_user": "postgres",
    "vault_b2_application_key_id": "id",
    "vault_b2_application_key": "key",
    "vault_b2_bucket_name": "bucket",
}


def _render(name: str, **extra: object) -> str:
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    env.filters["bool"] = bool
    src = (_ROLES / "backups" / "templates" / name).read_text(encoding="utf-8")
    return env.from_string(src).render(**{**_VARS, **extra})


def test_pg_backup_default_keeps_local_dump() -> None:
    out = _render("pg_backup.sh.j2")
    assert "removed local dump" not in out
    assert "Keeping local dump" in out
    assert 'LOCAL_BACKUP_DIR="/mnt/cluster/backups/postgres"' in out


def test_pg_backup_verified_only_deletes_after_zstd_test() -> None:
    out = _render("pg_backup.sh.j2", pg_backup_local_keep_verified_only=True)
    assert 'if zstd -tq "${NAS_ARCHIVE}"; then' in out
    assert 'rm -rf "${LOCAL_DUMP_DIR}"' in out
    assert "Keeping local dump" not in out
    # existing safeguards untouched
    assert "trap on_exit EXIT" in out and "MIN_FREE_GB" in out


def test_pg_backup_uses_host_local_root_when_set() -> None:
    out = _render("pg_backup.sh.j2", host_local_root="/var/local")
    assert 'LOCAL_BACKUP_DIR="/var/local/backups/postgres"' in out


def test_b2_sync_default_and_override_source() -> None:
    d = _render("b2_sync.sh.j2")
    assert 'PG_SOURCE="$SHARED/backups/postgres/"' in d
    o = _render("b2_sync.sh.j2", b2_postgres_source="/nas/backups/postgres/")
    assert 'PG_SOURCE="/nas/backups/postgres/"' in o
    assert '"$PG_SOURCE" \\' in d
    assert d.rstrip().endswith('B2 offsite sync complete"')


def test_nfs_client_removes_stale_darwin_fstab_line() -> None:
    text = (_ROLES / "nfs_client" / "tasks" / "main.yml").read_text(encoding="utf-8")
    assert "Remove stale fstab entries for shared_mount" in text
    assert "state: absent" in text


def test_logrotate_unchanged_on_shared_mount() -> None:
    for p in (_ROLES / "logrotate" / "templates").glob("shared_logs*"):
        assert "host_local_root" not in p.read_text(encoding="utf-8")


def test_restore_test_default_uses_local_dump() -> None:
    out = _render("restore_test.sh.j2")
    assert "toc.dat" in out
    assert "tar.zst" not in out and "mktemp" not in out


def test_restore_test_verified_only_extracts_nas_archive() -> None:
    out = _render(
        "restore_test.sh.j2",
        pg_backup_local_keep_verified_only=True,
        host_local_root="/var/local",
    )
    assert 'NAS_BACKUP_DIR="/nas/backups/postgres"' in out
    assert "precis_prod_*.tar.zst" in out
    assert 'BACKUP_DIR="/var/local/backups/postgres"' in out
    assert "mktemp -d" in out and "trap 'rm -rf \"${SCRATCH_DIR}\"' EXIT" in out


def test_drill_pull_renders_in_both_states() -> None:
    # Deliberately flag-independent: the puller's protocol is local dump dirs.
    for flag in (False, True):
        out = _render("drill_pull.sh.j2", pg_backup_local_keep_verified_only=flag)
        assert 'DUMP_ROOT="/mnt/cluster/backups/postgres"' in out

"""Render-level pins for the backups role's fileserver-move knobs.

Defaults must reproduce today's behaviour (flags off / today's source path);
each new flag has an on-state asserted too. Also pins the nfs_client macOS
stale-fstab cleanup task and that logrotate stays on shared_mount.
"""

from __future__ import annotations

import re
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
    assert 'if zstd -tq < "${NAS_ARCHIVE}"; then' in out
    assert 'rm -rf "${LOCAL_DUMP_DIR}"' in out
    assert "Keeping local dump" not in out
    # existing safeguards untouched
    assert "trap on_exit EXIT" in out and "MIN_FREE_GB" in out


def test_pg_backup_zstd_never_opens_nas_paths() -> None:
    # gr496659: TCC denies Homebrew zstd on the network volume; the shell
    # (Full Disk Access) must do every NAS open via redirection.
    out = _render("pg_backup.sh.j2")
    assert '| zstd -T0 -q > "${NAS_TMP}"' in out
    assert 'zstd -tq < "${NAS_TMP}"' in out
    assert "--use-compress-program" not in out


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
    assert "tar.zst" not in out and "NAS_BACKUP_DIR" not in out


def test_restore_test_verified_only_extracts_nas_archive() -> None:
    out = _render(
        "restore_test.sh.j2",
        pg_backup_local_keep_verified_only=True,
        host_local_root="/var/local",
    )
    assert 'NAS_BACKUP_DIR="/nas/backups/postgres"' in out
    assert "precis_prod_*.tar.zst" in out
    assert 'BACKUP_DIR="/var/local/backups/postgres"' in out
    assert "mktemp -d" in out and "trap on_exit EXIT" in out


def test_drill_pull_renders_in_both_states() -> None:
    # Deliberately flag-independent: the puller's protocol is local dump dirs.
    for flag in (False, True):
        out = _render("drill_pull.sh.j2", pg_backup_local_keep_verified_only=flag)
        assert 'DUMP_ROOT="/mnt/cluster/backups/postgres"' in out


# ── Incremental leg (backup-incremental phase 1) ──


def test_pg_backup_dir_dump_flags_fixed_dir_and_vault_key() -> None:
    out = _render("pg_backup_dir.sh.j2")
    # -Fd -Z0 -j4 into a FIXED dir — no dated path, no tar anywhere.
    assert "--format=directory" in out
    assert "--compress=0" in out
    assert "--jobs=4" in out
    assert "precis_prod_current" in out
    assert "TIMESTAMP" not in out and "tar -c" not in out
    # gr505334 carried forward: the dump must never carry the vault key.
    assert "--exclude-table-data=vault._key" in out


def test_pg_backup_legacy_chain_also_excludes_vault_key() -> None:
    out = _render("pg_backup.sh.j2")
    assert "--exclude-table-data=vault._key" in out


def test_pg_backup_dir_rsync_sends_only_diffs() -> None:
    out = _render("pg_backup_dir.sh.j2")
    # --checksum is load-bearing: pg_dump rewrites every file's mtime, so a
    # plain rsync would re-send the whole ~47 GB nightly.
    assert "rsync -rt --delete --inplace --checksum" in out
    # The sync-ok stamp is written after the rsync, and the local copy is
    # deleted afterwards (no retained backups on the DB node).
    assert "precis_prod_current.sync-ok" in out
    assert 'rm -rf "${LOCAL_DUMP_DIR}"' in out


def test_pg_backup_dir_cannot_overlap_itself() -> None:
    out = _render("pg_backup_dir.sh.j2")
    # mkdir lock (macOS has no flock), pid liveness, stale-lock reclaim.
    assert 'mkdir "${LOCK_DIR}"' in out
    assert "kill -0" in out
    assert "-mmin -1200" in out
    assert "breaking stale/dead lock" in out


def test_pg_backup_dir_refuses_an_unmounted_dbbackup() -> None:
    # Without this guard rsync would write into the empty mount point on the
    # root disk — a backup that never leaves the node.
    out = _render("pg_backup_dir.sh.j2", dbbackup_mount="/mnt/dbbackup")
    assert 'mount | grep -q " on ${DBBACKUP_MOUNT} "' in out
    assert 'DBBACKUP_MOUNT="/mnt/dbbackup"' in out
    assert "nas_dbbackup_export" in out


def test_restic_dbdump_snapshot_copy_forget_and_stamps() -> None:
    out = _render("restic-dbdump.sh.j2")
    assert 'restic -r "${LOCAL_REPO}" backup "${SRC}" --tag precis-dbdump' in out
    assert 'restic -r "${B2_REPO}" copy --from-repo "${LOCAL_REPO}"' in out
    assert "--keep-daily 14 --keep-weekly 8 --keep-monthly 12" in out
    assert out.count("--prune") >= 2
    # Existing heartbeat file plus the leg-specific one.
    assert "restic-last-ok" in out and "restic-dbdump-last-ok" in out
    # Never snapshots a mid-rsync dir: quiescence + already-done checks.
    assert 'find "${SRC}" -newer "${SYNC_OK}"' in out
    assert "last-stamp" in out
    assert 'mkdir "${LOCK_DIR}"' in out
    # Secrets come from the NAS-local env file, never the repo.
    assert "/etc/restic/env" in out


def test_pg_backup_dir_slot_outside_the_backup_window() -> None:
    # The cron slot must stay clear of 03:00–04:20 UTC (backup-window guard)
    # and of the optional 03:00 reboot; the defaults file is the single
    # source the task consumes.
    defaults = (_ROLES / "backups/defaults/main.yml").read_text(encoding="utf-8")
    for key in (
        "pg_backup_dir_cron_hour",
        "pg_backup_dir_cron_minute",
        "pg_backup_dir_enabled",
    ):
        assert f"\n{key}:" in defaults, f"defaults/main.yml lacks {key}"

    tasks = (_ROLES / "backups/tasks/main.yml").read_text(encoding="utf-8")
    cron = tasks.split('name: "pg_dump dir backup to dbbackup"', 1)[1].split(
        "become:", 1
    )[0]
    assert "{{ pg_backup_dir_cron_minute }}" in cron
    assert "{{ pg_backup_dir_cron_hour }}" in cron

    hour_m = re.search(r"\npg_backup_dir_cron_hour: *\"?(\d+)", defaults)
    minute_m = re.search(r"\npg_backup_dir_cron_minute: *\"?(\d+)", defaults)
    assert hour_m and minute_m
    hour, minute = int(hour_m.group(1)), int(minute_m.group(1))
    assert not (hour == 3 or (hour == 4 and minute < 20)), (
        f"dir-dump slot {hour:02d}:{minute:02d} falls inside 03:00–04:20 UTC"
    )

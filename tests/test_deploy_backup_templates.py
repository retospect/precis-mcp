"""Render-level pins for the backups role's fileserver-move knobs.

Defaults must reproduce today's behaviour (flags off / today's source path);
each new flag has an on-state asserted too. Also pins the nfs_client macOS
stale-fstab cleanup task and that logrotate stays on shared_mount.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import jinja2
import pytest

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
    # mkdir lock (macOS has no flock), pid liveness, dead-pid reclaim. Age
    # alone must never steal a lock whose pid is still alive (the defect
    # scripts/lib/lock-holder.sh documents and fixed in four other
    # scripts) — there is no "-mmin -1200"-style wait-vs-steal gate on a
    # parseable pid.
    assert 'mkdir "${LOCK_DIR}"' in out
    assert "kill -0" in out
    assert "breaking dead-pid lock" in out
    assert "-mmin -1200" not in out


def test_pg_backup_dir_refuses_an_unmounted_dbbackup() -> None:
    # Without this guard rsync would write into the empty mount point on the
    # root disk — a backup that never leaves the node.
    out = _render("pg_backup_dir.sh.j2", dbbackup_mount="/mnt/dbbackup")
    assert 'mount | grep -q " on ${DBBACKUP_MOUNT} "' in out
    assert 'DBBACKUP_MOUNT="/mnt/dbbackup"' in out
    assert "nas_dbbackup_export" in out


def test_pg_backup_dir_reclaims_local_dump_on_sync_failure() -> None:
    # A dump that completed but whose rsync then failed must not survive
    # on the DB node (cluster-fileserver-move 4a) — the EXIT trap must
    # reclaim it while owning the lock, including a partial/aborted pg_dump.
    out = _render("pg_backup_dir.sh.j2")
    assert "DUMP_OK" not in out
    on_exit = out.split("on_exit() {", 1)[1].split("trap on_exit EXIT", 1)[0]
    assert 'if [ -d "${LOCAL_DUMP_DIR}" ]; then' in on_exit
    assert "Reclaiming unsynced dump dir" in on_exit


def _run_backup_dir(
    tmp_path: Path,
    *,
    held_lock: str | None = None,
    pidless_lock: bool = False,
    dump_exit: int = 0,
    rsync_exit: int = 0,
    replace_lock: bool = False,
    successor_on_release: bool = False,
    lock_age_minutes: float | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    if sys.platform == "win32":
        pytest.skip("POSIX-only: runs the rendered bash backup script")
    local_root = tmp_path / "local"
    mount = tmp_path / "nas"
    staging = local_root / "backups" / "postgres" / "precis_prod_current"
    lock = staging.parent / "pg_backup_dir.lock"
    staging.mkdir(parents=True)
    (staging / "original").write_text("another run", encoding="utf-8")
    if held_lock is not None or pidless_lock:
        lock.mkdir()
        if held_lock is not None:
            (lock / "pid").write_text(held_lock, encoding="utf-8")
        if lock_age_minutes is not None:
            old = time.time() - lock_age_minutes * 60
            os.utime(lock, (old, old))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_rm = shutil.which("rm")
    assert real_rm is not None
    commands = {
        "mount": 'printf "test on %s type test\\n" "$TEST_MOUNT"',
        "df": '''printf "Filesystem 1024-blocks Used Available Capacity Mounted\\n"
printf "test 209715200 0 209715200 0%% /\\n"''',
        "pg_dump": '''mkdir -p "$TEST_STAGING"
printf "dump\\n" > "$TEST_STAGING/toc.dat"
if [ "$TEST_REPLACE_LOCK" = 1 ]; then
    printf "successor\\n" > "$TEST_LOCK/pid"
    rm -rf "$TEST_STAGING"
    mkdir -p "$TEST_STAGING"
    printf "successor\\n" > "$TEST_STAGING/successor"
fi
exit "$TEST_DUMP_EXIT"''',
        "rsync": 'exit "$TEST_RSYNC_EXIT"',
        "rm": f"""{shlex.quote(real_rm)} "$@"
if [ "$TEST_SUCCESSOR_ON_RELEASE" = 1 ] && [ "${{@: -1}}" = "$TEST_LOCK" ]; then
    mkdir -p "$TEST_LOCK" "$TEST_STAGING"
    printf "successor\\n" > "$TEST_LOCK/pid"
    printf "successor\\n" > "$TEST_STAGING/successor"
fi""",
    }
    for name, body in commands.items():
        command = bin_dir / name
        command.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
        command.chmod(0o755)
    script = tmp_path / "pg_backup_dir.sh"
    script.write_text(
        _render(
            "pg_backup_dir.sh.j2",
            host_local_root=str(local_root),
            dbbackup_mount=str(mount),
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
            "TEST_MOUNT": str(mount),
            "TEST_STAGING": str(staging),
            "TEST_LOCK": str(lock),
            "TEST_DUMP_EXIT": str(dump_exit),
            "TEST_RSYNC_EXIT": str(rsync_exit),
            "TEST_REPLACE_LOCK": str(int(replace_lock)),
            "TEST_SUCCESSOR_ON_RELEASE": str(int(successor_on_release)),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    return result, staging, lock


@pytest.mark.parametrize("pidless", [False, True])
def test_pg_backup_dir_contender_preserves_holder_staging(
    tmp_path: Path, pidless: bool
) -> None:
    pid = None if pidless else str(os.getpid())
    result, staging, lock = _run_backup_dir(
        tmp_path, held_lock=pid, pidless_lock=pidless
    )
    assert result.returncode == 0, result.stderr
    assert (staging / "original").read_text(encoding="utf-8") == "another run"
    assert lock.is_dir()
    if pid is not None:
        assert (lock / "pid").read_text(encoding="utf-8") == pid
    else:
        assert not (lock / "pid").exists()


def test_pg_backup_dir_waits_on_an_old_but_alive_lock(tmp_path: Path) -> None:
    # The lock-holder.sh defect, ported here: a lock older than the old
    # 20 h age threshold must still be waited on, not stolen, as long as
    # its pid is alive. An NFS hang mid-rsync easily clears 20 h.
    result, staging, lock = _run_backup_dir(
        tmp_path, held_lock=str(os.getpid()), lock_age_minutes=2000
    )
    assert result.returncode == 0, result.stderr
    assert "another pg_backup_dir run holds" in result.stdout
    assert (staging / "original").read_text(encoding="utf-8") == "another run"
    assert lock.is_dir()
    assert (lock / "pid").read_text(encoding="utf-8") == str(os.getpid())


def test_pg_backup_dir_does_not_steal_a_lock_held_by_pid_1(tmp_path: Path) -> None:
    # pid 1 is always alive and, outside a root container, never signalable
    # by this process: a bare `kill -0` fails with EPERM, indistinguishable
    # from ESRCH (no such process) unless the error text is checked — the
    # same hole lock-holder.sh documents and scripts/deploy already fixed.
    # Misreading EPERM as dead would steal a live holder's lock and destroy
    # its in-flight staging dir.
    result, staging, lock = _run_backup_dir(tmp_path, held_lock="1")
    assert result.returncode == 0, result.stderr
    assert "another pg_backup_dir run holds" in result.stdout
    assert (staging / "original").read_text(encoding="utf-8") == "another run"
    assert lock.is_dir()
    assert (lock / "pid").read_text(encoding="utf-8") == "1"


@pytest.mark.parametrize(("dump_exit", "rsync_exit"), [(17, 0), (0, 23)])
def test_pg_backup_dir_failure_reclaims_owned_staging_and_keeps_status(
    tmp_path: Path, dump_exit: int, rsync_exit: int
) -> None:
    result, staging, lock = _run_backup_dir(
        tmp_path, dump_exit=dump_exit, rsync_exit=rsync_exit
    )
    assert result.returncode == (dump_exit or rsync_exit), result.stderr
    assert not staging.exists()
    assert not lock.exists()


def test_pg_backup_dir_replaced_lock_preserves_successor_staging(
    tmp_path: Path,
) -> None:
    result, staging, lock = _run_backup_dir(tmp_path, dump_exit=17, replace_lock=True)
    assert result.returncode == 17, result.stderr
    assert (staging / "successor").read_text(encoding="utf-8") == "successor\n"
    assert (lock / "pid").read_text(encoding="utf-8") == "successor\n"


def test_pg_backup_dir_releases_lock_after_staging_cleanup(tmp_path: Path) -> None:
    result, staging, lock = _run_backup_dir(
        tmp_path, dump_exit=17, successor_on_release=True
    )
    assert result.returncode == 17, result.stderr
    assert (staging / "successor").read_text(encoding="utf-8") == "successor\n"
    assert (lock / "pid").read_text(encoding="utf-8") == "successor\n"


def test_restic_dbdump_lock_check_handles_eperm() -> None:
    # Same EPERM hole as the DB-node script: a bare `kill -0` fails
    # identically for "no such process" and "process exists, not mine to
    # signal" (owned by another user) — lock-holder.sh's own header names
    # this as the fix already shipped in scripts/deploy. An unguarded
    # `kill -0 "${LOCK_PID}" 2>/dev/null` would steal a live holder's lock.
    out = _render("restic-dbdump.sh.j2")
    assert "lock_pid_alive" in out
    assert 'kill -0 "${LOCK_PID}" 2>/dev/null' not in out
    assert "not permitted" in out
    assert 'if lock_pid_alive "${LOCK_PID}"; then' in out


def test_restic_dbdump_snapshot_copy_forget_and_stamps() -> None:
    out = _render("restic-dbdump.sh.j2")
    assert 'restic -r "${LOCAL_REPO}" backup "${SRC}" --tag precis-dbdump' in out
    assert 'restic -r "${B2_REPO}" copy --from-repo "${LOCAL_REPO}"' in out
    assert "--keep-daily 14 --keep-weekly 8 --keep-monthly 12" in out
    assert out.count("--prune") >= 2
    # copy must run BEFORE either forget --prune: pruning the local repo
    # first could drop a snapshot a prior run's copy never reached before
    # it ever lands offsite.
    backup_at = out.index('restic -r "${LOCAL_REPO}" backup "${SRC}"')
    copy_at = out.index('restic -r "${B2_REPO}" copy --from-repo')
    local_forget_at = out.index('restic -r "${LOCAL_REPO}" forget')
    b2_forget_at = out.index('restic -r "${B2_REPO}" forget')
    assert backup_at < copy_at < local_forget_at < b2_forget_at
    # Existing heartbeat file plus the leg-specific one.
    assert "restic-last-ok" in out and "restic-dbdump-last-ok" in out
    # Never snapshots a mid-rsync dir: quiescence + already-done checks.
    assert 'find "${SRC}" -newer "${SYNC_OK}"' in out
    assert "last-stamp" in out
    assert 'mkdir "${LOCK_DIR}"' in out
    # Secrets come from the NAS-local env file, never the repo.
    assert "/etc/restic/env" in out


def test_pg_backup_dir_stamps_facts_backup_contract() -> None:
    # gr593053: `get(kind='job', id='/facts')` reads app_state backup.*
    # — the DB-node dir-dump leg is the only part of the chain that can
    # both measure the dump and reach the DB, so it writes the keys.
    out = _render("pg_backup_dir.sh.j2")
    assert "INSERT INTO app_state" in out
    for key in ("backup.last_run_at", "backup.last_size_bytes", "backup.last_kind"):
        assert key in out
    assert "ON CONFLICT (key) DO UPDATE" in out
    # A failed stamp must not fail a backup that already succeeded —
    # and it runs after the sync-ok write (never holds up the restic
    # leg) but before the local staging dir goes (it measures it).
    assert "|| echo" in out
    assert (
        out.index('> "${SYNC_OK}"')
        < out.index("INSERT INTO app_state")
        < out.rindex('rm -rf "${LOCAL_DUMP_DIR}"')
    )


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

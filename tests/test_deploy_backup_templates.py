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


def test_pg_backup_local_retention_is_a_role_variable_with_the_old_default() -> None:
    assert "LOCAL_RETENTION_DAYS=2\n" in _render("pg_backup.sh.j2")
    out = _render("pg_backup.sh.j2", pg_backup_local_retention_days=5)
    assert "LOCAL_RETENTION_DAYS=5\n" in out
    defaults = (_ROLES / "backups" / "defaults" / "main.yml").read_text(
        encoding="utf-8"
    )
    assert re.search(r"^pg_backup_local_retention_days: 2$", defaults, re.M)


_GB = 1024 * 1024  # KB per GB

# (name, size_gb, archived): archived is True (verified NAS archive for the
# dump's date), False (none) or "empty" (a zero-byte file under that name).
_Dump = tuple[str, int, bool | str]


def _run_pg_backup(
    tmp_path: Path,
    *,
    total_gb: int,
    dumps: list[_Dump],
    new_dump_gb: int = 1,
    reverse_mtimes: bool = False,
    df_frozen: bool = False,
    du_blank: tuple[str, ...] = (),
    zstd_fail: bool = False,
    psql_exit: int = 0,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Run the rendered nightly script against a fake volume.

    ``dumps`` is oldest first by name; every dir has a ``toc.dat`` (a killed
    dump has one too). ``df`` reports the volume's total minus the sizes the
    fake dump dirs declare (``.size_kb``), so a deletion really frees space
    (``df_frozen`` pins it instead: a deletion frees nothing); ``du`` reads the
    same file (prints nothing for ``du_blank`` names); ``pg_dump`` makes a dir.
    ``zstd_fail`` fails the NAS archive step; ``psql_exit`` is the stub psql's
    exit status (it logs ``psql <args>`` on one line). Returns the result, the
    backup dir and the stub-call log.
    """
    if sys.platform == "win32":
        pytest.skip("POSIX-only: runs the rendered bash backup script")
    local_root = tmp_path / "local"
    backup = local_root / "backups" / "postgres"
    backup.mkdir(parents=True)
    nas = tmp_path / "nas" / "backups" / "postgres"
    nas.mkdir(parents=True)
    now = time.time()
    for i, (name, size_gb, archived) in enumerate(dumps):
        d = backup / name
        d.mkdir()
        (d / ".size_kb").write_text(f"{size_gb * _GB}\n", encoding="utf-8")
        (d / "toc.dat").write_text("toc\n", encoding="utf-8")
        if name in du_blank:
            (d / ".no_du").write_text("", encoding="utf-8")
        if archived:
            ymd = name.split("_")[2]
            tag = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
            body = "" if archived == "empty" else "archive\n"
            (nas / f"precis_prod_{tag}.tar.zst").write_text(body, encoding="utf-8")
        # all inside the 2-day age prune, so only the free-space guard acts;
        # reversed, the oldest name carries the newest mtime
        age_rank = i if reverse_mtimes else len(dumps) - i
        stamp = now - age_rank * 3600
        os.utime(d, (stamp, stamp))

    log = tmp_path / "calls.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    commands = {
        "df": """if [ -n "$TEST_DF_FROZEN_KB" ]; then
    avail="$TEST_DF_FROZEN_KB"
else
    used=$(cat "$TEST_BACKUP"/precis_prod_*/.size_kb 2>/dev/null \\
      | awk '{s+=$1} END {print s+0}')
    avail=$((TEST_TOTAL_KB - used))
fi
printf "Filesystem 1024-blocks Used Available Capacity Mounted\\n"
printf "fake %s 0 %s 0%% /\\n" "$TEST_TOTAL_KB" "$avail\"""",
        "du": """for last; do :; done
if [ -f "$last/.no_du" ]; then
    exit 0
elif [ -f "$last/.size_kb" ]; then
    printf "%s\\t%s\\n" "$(cat "$last/.size_kb")" "$last"
else
    printf "1\\t%s\\n" "$last"
fi""",
        "pg_dump": """for a; do case "$a" in --file=*) out="${a#--file=}";; esac; done
mkdir -p "$out"
printf "toc\\n" > "$out/toc.dat"
printf "%s\\n" "$TEST_NEW_KB" > "$out/.size_kb"
echo "pg_dump $out" >> "$TEST_LOG\"""",
        "logger": 'echo "logger $*" >> "$TEST_LOG"',
        "tar": "exit 0",
        "zstd": 'exit "${TEST_ZSTD_EXIT:-0}"',
        "psql": """echo "psql $(printf '%s ' "$@" | tr '\\n' ' ')<<< $(tr '\\n' ' ')" >> "$TEST_LOG"
exit "${TEST_PSQL_EXIT:-0}\"""",
    }
    for name, body in commands.items():
        command = bin_dir / name
        command.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
        command.chmod(0o755)
    script = tmp_path / "pg_backup.sh"
    script.write_text(
        _render(
            "pg_backup.sh.j2",
            host_local_root=str(local_root),
            nas_root=str(tmp_path / "nas"),
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
            "TEST_BACKUP": str(backup),
            "TEST_TOTAL_KB": str(total_gb * _GB),
            "TEST_DF_FROZEN_KB": str(total_gb * _GB) if df_frozen else "",
            "TEST_NEW_KB": str(new_dump_gb * _GB),
            "TEST_LOG": str(log),
            "TEST_ZSTD_EXIT": "1" if zstd_fail else "0",
            "TEST_PSQL_EXIT": str(psql_exit),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    return result, backup, log


def _survivors(backup: Path) -> list[str]:
    """The fixture dirs still there (they are dated 2001; tonight's is not)."""
    return sorted(p.name for p in backup.glob("precis_prod_2001*"))


def _dump_ran(log: Path) -> bool:
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return any(line.startswith("pg_dump ") for line in lines)


def _logged(log: Path) -> str:
    return log.read_text(encoding="utf-8") if log.exists() else ""


def _tonights(backup: Path) -> list[str]:
    return sorted(p.name for p in backup.glob("precis_prod_*") if "_2001" not in p.name)


_D1, _D2, _D3, _D4 = (f"precis_prod_2001010{n}_033000" for n in (1, 2, 3, 4))


def test_pg_backup_guard_enough_room_deletes_nothing(tmp_path: Path) -> None:
    # 200 GB volume, 90 GB of dumps: 110 GB free >= 25 + 30 required.
    dumps: list[_Dump] = [(_D1, 30, True), (_D2, 30, True), (_D3, 30, True)]
    result, backup, log = _run_pg_backup(tmp_path, total_gb=200, dumps=dumps)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "deleting oldest" not in result.stdout
    assert _survivors(backup) == [_D1, _D2, _D3]
    assert len(_tonights(backup)) == 1
    assert _dump_ran(log)


def test_pg_backup_guard_drops_only_the_oldest_archived_dump_that_makes_room(
    tmp_path: Path,
) -> None:
    # 125 GB volume, 100 GB of dumps (40 + 30 + 30): 25 GB free, 65 needed
    # (25 + the largest, 40). Dropping the oldest frees 40 -> exactly enough.
    dumps: list[_Dump] = [(_D1, 40, True), (_D2, 30, True), (_D3, 30, True)]
    result, backup, log = _run_pg_backup(tmp_path, total_gb=125, dumps=dumps)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _survivors(backup) == [_D2, _D3]
    assert result.stdout.count("deleting oldest local dump") == 1
    assert f"dump {backup / _D1} (verified NAS archive " in result.stdout
    assert "precis_prod_2001-01-01.tar.zst" in result.stdout
    assert len(_tonights(backup)) == 1 and _dump_ran(log)


def test_pg_backup_guard_skips_an_unarchived_oldest_and_takes_the_next(
    tmp_path: Path,
) -> None:
    # The oldest has no NAS archive: it may be the only copy. The next archived
    # one goes instead; the newest archived one is never a candidate.
    dumps: list[_Dump] = [
        (_D1, 30, False),
        (_D2, 30, True),
        (_D3, 30, True),
        (_D4, 30, True),
    ]
    result, backup, log = _run_pg_backup(tmp_path, total_gb=150, dumps=dumps)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _survivors(backup) == [_D1, _D3, _D4]
    assert result.stdout.count("deleting oldest local dump") == 1
    assert _dump_ran(log)


def test_pg_backup_guard_never_deletes_the_newest_archived_dump(
    tmp_path: Path,
) -> None:
    # 60 GB volume: 20 (no archive) + 5 + 5 + 20 GB leaves 10; 45 needed. The
    # two small archived dumps go (-> 20 GB) and it is still short: the newest
    # archived dump and the unarchived one stay, the run skips.
    dumps: list[_Dump] = [
        (_D1, 20, False),
        (_D2, 5, True),
        (_D3, 5, True),
        (_D4, 20, True),
    ]
    result, backup, log = _run_pg_backup(tmp_path, total_gb=60, dumps=dumps)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _survivors(backup) == [_D1, _D4]
    assert result.stdout.count("deleting oldest local dump") == 2
    assert "[ERROR] only 20GB free on backup volume (< 45GB required" in result.stdout
    assert "logger -t pg-backup ABORT: 20GB free < 45GB required" in _logged(log)
    assert not _dump_ran(log)


def test_pg_backup_guard_with_nothing_archived_deletes_nothing_and_skips(
    tmp_path: Path,
) -> None:
    # toc.dat is no proof (pg_dump writes it first); only a NAS archive is.
    dumps: list[_Dump] = [(_D1, 30, False), (_D2, 30, False), (_D3, 30, False)]
    result, backup, log = _run_pg_backup(tmp_path, total_gb=100, dumps=dumps)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _survivors(backup) == [_D1, _D2, _D3]
    assert "deleting oldest" not in result.stdout
    assert "[ERROR] only 10GB free on backup volume (< 55GB required" in result.stdout
    assert not _dump_ran(log)


def test_pg_backup_guard_treats_an_empty_archive_as_no_archive(
    tmp_path: Path,
) -> None:
    dumps: list[_Dump] = [(_D1, 30, "empty"), (_D2, 30, True), (_D3, 30, True)]
    result, backup, _ = _run_pg_backup(tmp_path, total_gb=120, dumps=dumps)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _survivors(backup) == [_D1, _D3]


def test_pg_backup_guard_orders_by_name_not_mtime(tmp_path: Path) -> None:
    # The oldest name carries the NEWEST mtime (a touched or copied dump).
    dumps: list[_Dump] = [(_D1, 30, True), (_D2, 30, True), (_D3, 30, True)]
    result, backup, _ = _run_pg_backup(
        tmp_path, total_gb=120, dumps=dumps, reverse_mtimes=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _survivors(backup) == [_D2, _D3]


def test_pg_backup_guard_stops_when_a_deletion_frees_nothing(
    tmp_path: Path,
) -> None:
    # df reports the same free space whatever is deleted (APFS local snapshots
    # hold the blocks): one deletion, then stop and skip, not all but the newest.
    dumps: list[_Dump] = [(_D1, 30, True), (_D2, 30, True), (_D3, 30, True)]
    result, backup, log = _run_pg_backup(
        tmp_path, total_gb=30, dumps=dumps, df_frozen=True
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert _survivors(backup) == [_D2, _D3]
    assert result.stdout.count("deleting oldest local dump") == 1
    assert "freed no space" in result.stdout
    assert "SKIPPING dump" in result.stdout
    assert not _dump_ran(log)


def test_pg_backup_guard_skips_when_the_dump_size_cannot_be_read(
    tmp_path: Path,
) -> None:
    # du prints nothing: sizing would read 0 and wave the dump through.
    dumps: list[_Dump] = [(_D1, 30, True)]
    result, backup, log = _run_pg_backup(
        tmp_path, total_gb=500, dumps=dumps, du_blank=(_D1,)
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "could not read the size of the existing dumps" in result.stdout
    assert "logger -t pg-backup ABORT: could not read existing dump sizes" in _logged(
        log
    )
    assert _survivors(backup) == [_D1]
    assert not _dump_ran(log)


def test_pg_backup_guard_ignores_a_killed_dump_when_choosing_what_to_keep(
    tmp_path: Path,
) -> None:
    # A killed dump has toc.dat but no archive, and is the newest dir. It is
    # neither deletable nor the protected newest: the newest ARCHIVED dump is.
    # 133 GB volume, 130 GB used, 65 needed (25 + the 40 GB killed dir): both
    # older archived dumps go (-> 63 GB), the newest archived stays, run skips.
    killed = "precis_prod_20010105_033000"
    dumps: list[_Dump] = [
        (_D1, 30, True),
        (_D2, 30, True),
        (_D3, 30, True),
        (killed, 40, False),
    ]
    result, backup, log = _run_pg_backup(tmp_path, total_gb=133, dumps=dumps)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _survivors(backup) == [_D3, killed]
    assert not _dump_ran(log)


def test_pg_backup_guard_only_deletes_fixed_pattern_names(tmp_path: Path) -> None:
    # An archived-looking dir with a name outside precis_prod_YYYYMMDD_HHMMSS
    # is never a candidate, however old.
    odd = "precis_prod_2001-manual"
    dumps: list[_Dump] = [(odd, 30, True), (_D2, 30, True), (_D3, 30, True)]
    result, backup, _ = _run_pg_backup(tmp_path, total_gb=120, dumps=dumps)
    assert odd in _survivors(backup)
    assert _D2 not in _survivors(backup)


def test_pg_backup_guard_with_no_dumps_requires_only_the_floor(
    tmp_path: Path,
) -> None:
    result, _, _ = _run_pg_backup(tmp_path / "ok", total_gb=26, dumps=[])
    assert result.returncode == 0, result.stdout + result.stderr
    result, _, _ = _run_pg_backup(tmp_path / "short", total_gb=24, dumps=[])
    assert result.returncode == 1
    assert "(< 25GB required: 25GB + largest existing dump 0GB)" in result.stdout


def _psql_calls(log: Path) -> list[str]:
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return [line for line in lines if line.startswith("psql ")]


def test_pg_backup_success_stamps_the_in_db_backup_contract(tmp_path: Path) -> None:
    result, _, log = _run_pg_backup(tmp_path, total_gb=200, dumps=[], new_dump_gb=3)
    assert result.returncode == 0, result.stdout + result.stderr
    (call,) = _psql_calls(log)  # exactly once
    # same flags and user as pg_backup_dir.sh's stamp
    assert "-U postgres -d precis_prod -q -v ON_ERROR_STOP=1" in call
    assert re.search(r"-v run_at=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ", call)
    assert f"-v size_bytes={3 * _GB * 1024} " in call  # the stub dump, in bytes
    for key in ("backup.last_run_at", "backup.last_size_bytes", "backup.last_kind"):
        assert f"'{key}'" in call
    assert "'backup.last_kind', 'pg-dump-tar-zst'" in call
    assert "ON CONFLICT (key) DO UPDATE" in call
    assert "Stamped app_state backup.*" in result.stdout
    # the statement reaches psql on stdin, after the argv
    args, _, stdin = call.partition("<<< ")
    assert "INSERT INTO app_state" in stdin and "INSERT" not in args


@pytest.mark.parametrize("template", ["pg_backup.sh.j2", "pg_backup_dir.sh.j2"])
def test_backup_stamp_sql_is_never_a_dash_c_string(template: str) -> None:
    # psql expands :'var' only in input it lexes itself (stdin, -f). Inside a
    # -c string the server gets the colon and answers "syntax error at or near
    # :", so the dir leg's stamp failed on every run and /facts never saw a
    # backup record (gr738880). A stubbed psql cannot see this; the shape can.
    out = _render(template)
    assert ":'run_at'" in out and ":'size_bytes'" in out
    assert not re.search(r"""-c\s+"[^"]*:'""", out)
    stamp = out[out.index("-v run_at=") : out.index("INSERT INTO app_state")]
    assert "<<'SQL'" in stamp and "-c " not in stamp


def test_pg_backup_skip_by_the_guard_does_not_stamp(tmp_path: Path) -> None:
    result, _, log = _run_pg_backup(tmp_path, total_gb=24, dumps=[])
    assert result.returncode == 1 and "SKIPPING dump" in result.stdout
    assert _psql_calls(log) == []


def test_pg_backup_failed_nas_archive_does_not_stamp(tmp_path: Path) -> None:
    result, _, log = _run_pg_backup(tmp_path, total_gb=200, dumps=[], zstd_fail=True)
    assert result.returncode != 0
    assert _dump_ran(log)  # the dump ran; only the archive step failed
    assert _psql_calls(log) == []


def test_pg_backup_failing_stamp_warns_but_does_not_fail_the_backup(
    tmp_path: Path,
) -> None:
    result, _, log = _run_pg_backup(tmp_path, total_gb=200, dumps=[], psql_exit=2)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Backup done." in result.stdout
    assert "[WARN] app_state backup.* stamp failed" in result.stderr
    assert len(_psql_calls(log)) == 1


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
    assert "if ! psql" in out
    assert "[WARN] app_state backup.* stamp failed" in out
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

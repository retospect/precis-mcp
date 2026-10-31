"""``scripts/lib/backup-window.sh``: no migration deploy inside the nightly
backup window.

The prod DB node reboots at 03:00 UTC and the nightly pg_dump runs 03:30 to
about 04:18 UTC holding ACCESS SHARE on every table, so a migration's ALTER
TABLE waits behind the whole dump (2026-09-08: a /go at 03:22 UTC hung 17+
min with a silent log). ``scripts/deploy`` now refuses a deploy that carries
a pending migration while the window is open, warns-and-proceeds otherwise,
and takes ``--ignore-backup-window`` as the operator override. Since gr511981
the guard also probes ``pg_stat_activity`` on prod through the repo's own
``scripts/prod-psql --ro`` (stubbed in every test here — the tests never
touch prod): a running pg_dump/pg_basebackup refuses a migration deploy at
ANY hour, and the fixed window is the fallback for when the probe cannot
run.

Acceptance criteria (docs/backlog/deploy-no-deploy-window-guard.md), each a
test below:
  (a) clock faked to 03:40 UTC + a pending migration → non-zero before any
      ansible task runs, naming the window and its end;
  (b) 03:40 UTC + no pending migration → warns and proceeds;
  (c) outside the window → unchanged (silent, proceeds);
  (d) the bounds live in ONE place, next to the backup cron they mirror.

The decision function is driven directly with a faked clock
(``DEPLOY_NOW_UTC`` / explicit arguments) and a faked pending list; the
pending computation is driven in a throwaway git repo; and one case runs the
REAL ``scripts/deploy`` (copied byte-for-byte into a throwaway repo at its
real relative path, fake ansible on $PATH — the tests/test_deploy_pinned_sha.py
technique) to prove the refusal lands before ansible is ever invoked.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash scripts are POSIX-only"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "scripts" / "lib" / "backup-window.sh"
DEPLOY_SRC = REPO_ROOT / "scripts" / "deploy"
DEPLOY_STATE_LIB_SRC = REPO_ROOT / "scripts" / "lib" / "deploy-state.sh"
LOCK_HOLDER_LIB_SRC = REPO_ROOT / "scripts" / "lib" / "lock-holder.sh"
DEFAULTS_REL = Path("deploy/roles/backups/defaults/main.yml")
TASKS_REL = Path("deploy/roles/backups/tasks/main.yml")

MIG = "src/precis/migrations"


def _test_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "DEPLOY_NOW_UTC"}
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    env.update(extra)
    return env


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=_test_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


def _commit(repo: Path, rel: str, msg: str) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"-- {msg}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _lib(
    fn: str, *args: str, cwd: Path | None = None, **env: str
) -> subprocess.CompletedProcess[str]:
    quoted = " ".join("'" + a.replace("'", "'\\''") + "'" for a in args)
    return subprocess.run(
        ["bash", "-c", f'. "{LIB}"; {fn} {quoted}'],
        cwd=str(cwd or REPO_ROOT),
        env=_test_env(**env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def _decide(
    now: str,
    pending: str,
    ignore: str = "0",
    start: str = "03:00",
    end: str = "04:20",
    live: str = "unavailable",
    slot: str = "",
    detail: str = "",
) -> subprocess.CompletedProcess[str]:
    return _lib(
        "backup_window_decide", start, end, now, pending, ignore, live, slot, detail
    )


# ── (d) one place: the bounds file, the cron it mirrors, and what the lib reads ──


def test_bounds_live_next_to_the_backup_cron_and_contain_it() -> None:
    """The defaults file holds both the pg_dump cron slot and the window; the
    cron task consumes the slot from there (no literal hour/minute left in the
    task), and the slot sits inside the window — so moving the schedule
    without moving the window reddens here."""
    defaults = (REPO_ROOT / DEFAULTS_REL).read_text(encoding="utf-8")
    for key in (
        "pg_backup_cron_hour",
        "pg_backup_cron_minute",
        "backup_window_start_utc",
        "backup_window_end_utc",
    ):
        assert f"\n{key}:" in defaults, f"{DEFAULTS_REL} lacks {key}"

    tasks = (REPO_ROOT / TASKS_REL).read_text(encoding="utf-8")
    cron = tasks.split('name: "pg_dump nightly backup"', 1)[1].split("become:", 1)[0]
    assert "{{ pg_backup_cron_minute }}" in cron and "{{ pg_backup_cron_hour }}" in cron

    bounds = _lib("backup_window_bounds", str(REPO_ROOT))
    assert bounds.returncode == 0, bounds.stderr
    start, end = bounds.stdout.split()
    slot = _lib("backup_window_cron_slot", str(REPO_ROOT))
    assert slot.returncode == 0, slot.stderr
    inside = _lib("backup_window_contains", start, end, slot.stdout.strip())
    assert inside.returncode == 0, (
        f"pg_dump cron slot {slot.stdout.strip()} is outside the window {start}–{end}"
    )
    # The documented numbers; a deliberate schedule change updates this line.
    assert (start, end, slot.stdout.strip()) == ("03:00", "04:20", "03:30")


def test_bounds_file_missing_or_malformed_is_a_tree_error(tmp_path: Path) -> None:
    r = _lib("backup_window_bounds", str(tmp_path))
    assert r.returncode == 2 and "cannot read the window bounds" in r.stderr
    f = tmp_path / DEFAULTS_REL
    f.parent.mkdir(parents=True)
    f.write_text(
        "backup_window_start_utc: 3am\nbackup_window_end_utc: '04:20'\n",
        encoding="utf-8",
    )
    r = _lib("backup_window_bounds", str(tmp_path))
    assert r.returncode == 2 and "HH:MM" in r.stderr


def test_windows_lists_every_pair_with_its_cron_slot() -> None:
    """(d) extended (gr513885): the guard reads a LIST of <X>_window_*_utc
    pairs — one per backup leg — each paired with the pg_<X>_cron_* slot it
    mirrors, in file order (primary first)."""
    r = _lib("backup_window_windows", str(REPO_ROOT))
    assert r.returncode == 0, r.stderr
    rows = [line.split() for line in r.stdout.splitlines()]
    assert rows == [["03:00", "04:20", "03:30"], ["21:30", "23:30", "21:45"]]


def test_dir_dump_cron_slot_sits_inside_its_own_window() -> None:
    """Same containment contract as the primary leg: a deliberate schedule
    change updates the pair together or reddens here."""
    r = _lib("backup_window_windows", str(REPO_ROOT))
    assert r.returncode == 0, r.stderr
    rows = [line.split() for line in r.stdout.splitlines()]
    start, end, slot = rows[1]
    inside = _lib("backup_window_contains", start, end, slot)
    assert inside.returncode == 0, (
        f"dir-dump cron slot {slot} is outside its window {start}–{end}"
    )


# ── the decision, with the clock and pending set faked ─────────────────────


def test_inside_window_with_pending_migration_refuses_naming_window_and_end() -> None:
    """(a) at the decision layer."""
    r = _decide("03:40", f"{MIG}/0099_add_col.sql\n{MIG}/0100_index.sql")
    assert r.returncode == 1
    assert "REFUSING TO DEPLOY" in r.stdout
    assert "03:00–04:20 UTC" in r.stdout and "now 03:40 UTC" in r.stdout
    assert "ends at 04:20 UTC" in r.stdout
    assert "2 pending migration(s)" in r.stdout
    assert "0099_add_col.sql" in r.stdout and "0100_index.sql" in r.stdout
    assert "--ignore-backup-window" in r.stdout


def test_inside_window_without_pending_migration_warns_and_proceeds() -> None:
    """(b)."""
    r = _decide("03:40", "")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("WARNING:")
    assert "03:00–04:20 UTC" in r.stdout and "ends 04:20 UTC" in r.stdout


def test_outside_window_is_silent_regardless_of_pending() -> None:
    """(c): no output, no refusal — unchanged behaviour."""
    for now in ("02:59", "04:20", "12:00", "23:59", "00:00"):
        r = _decide(now, f"{MIG}/0099_add_col.sql")
        assert r.returncode == 0 and r.stdout == "" and r.stderr == "", (now, r)


def test_window_edges_are_half_open() -> None:
    assert _decide("03:00", f"{MIG}/0099.sql").returncode == 1
    assert _decide("04:19", f"{MIG}/0099.sql").returncode == 1
    assert _decide("04:20", f"{MIG}/0099.sql").returncode == 0


def test_override_flag_proceeds_with_a_note() -> None:
    r = _decide("03:40", f"{MIG}/0099_add_col.sql", ignore="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("NOTE: --ignore-backup-window")
    assert "0099_add_col.sql" in r.stdout


def test_unknown_pending_set_inside_window_fails_closed() -> None:
    r = _decide("03:40", "unknown")
    assert r.returncode == 1 and "UNKNOWN" in r.stdout and "fail closed" in r.stdout
    assert _decide("03:40", "unknown", ignore="1").returncode == 0


def test_clock_comes_from_DEPLOY_NOW_UTC_else_date() -> None:
    r = _lib("backup_window_now", DEPLOY_NOW_UTC="03:40")
    assert r.returncode == 0 and r.stdout.strip() == "03:40"
    r = _lib("backup_window_now", DEPLOY_NOW_UTC="3:40")
    assert r.returncode == 2 and "not HH:MM" in r.stderr
    r = _lib("backup_window_now")
    assert r.returncode == 0
    hh, mm = r.stdout.strip().split(":")
    assert 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59


# ── the live check (gr511981): the probe result is a decide input ─────────
# <live> is `running`/`clear`/`unavailable`; <slot> the pg_dump cron slot;
# <detail> the "pid|app|backend|state|age" lines the probe echoes.

_LIVE_DETAIL = "1234|pg_dump|client backend|active|00:52:10.331"


def test_live_running_backup_refuses_at_any_hour() -> None:
    """The r91 shape: 04:22Z is outside the window yet the dump still ran —
    the clock alone cannot see that."""
    for now in ("04:22", "12:00", "03:40"):
        r = _decide(
            now,
            f"{MIG}/0235_x.sql",
            live="running",
            slot="03:30",
            detail=_LIVE_DETAIL,
        )
        assert r.returncode == 1, (now, r)
        assert "REFUSING TO DEPLOY" in r.stdout
        assert "a backup is running on the prod DB right now" in r.stdout
        assert _LIVE_DETAIL.split("|")[0] in r.stdout and "pg_dump" in r.stdout


def test_live_running_backup_refuses_even_inside_the_window() -> None:
    r = _decide(
        "03:40",
        f"{MIG}/0235_x.sql",
        live="running",
        slot="03:30",
        detail=_LIVE_DETAIL,
    )
    assert r.returncode == 1
    # The live refusal names the running backend, not the clock guess.
    assert "pg_stat_activity shows" in r.stdout


def test_live_probe_unavailable_is_the_fixed_window_fallback() -> None:
    """(a) unchanged when the probe cannot run: inside+pending refuses."""
    r = _decide("03:40", f"{MIG}/0235_x.sql", live="unavailable", slot="03:30")
    assert r.returncode == 1
    assert "03:00–04:20 UTC" in r.stdout and "0235_x.sql" in r.stdout
    assert "could not run" in r.stdout and "fixed window decides" in r.stdout


def test_live_clear_inside_window_before_the_slot_still_refuses() -> None:
    """A clear probe at 03:10 cannot clear the scheduled 03:30 start."""
    r = _decide("03:10", f"{MIG}/0235_x.sql", live="clear", slot="03:30")
    assert r.returncode == 1
    assert "03:00–04:20 UTC" in r.stdout and "0235_x.sql" in r.stdout
    assert "still ahead" in r.stdout


def test_live_clear_inside_window_after_the_slot_proceeds() -> None:
    """The upside of the live check: the dump finished early (or never ran),
    and the probe — not the clock's 04:20 guess — says so."""
    r = _decide(
        "04:05",
        f"{MIG}/0235_x.sql\n{MIG}/0236_y.sql",
        live="clear",
        slot="03:30",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("NOTE: live check")
    assert "03:30 UTC cron slot has passed" in r.stdout
    assert "0235_x.sql" in r.stdout


def test_live_clear_unknown_pending_after_the_slot_proceeds() -> None:
    r = _decide("04:05", "unknown", live="clear", slot="03:30")
    assert r.returncode == 0 and r.stdout.startswith("NOTE: live check")


def test_live_clear_with_an_unreadable_slot_falls_back_to_the_window() -> None:
    r = _decide("04:05", f"{MIG}/0235_x.sql", live="clear", slot="")
    assert r.returncode == 1 and "03:00–04:20 UTC" in r.stdout


def test_live_clear_outside_window_is_silent() -> None:
    for live in ("clear", "unavailable"):
        r = _decide("12:00", f"{MIG}/0235_x.sql", live=live, slot="03:30")
        assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


def test_override_proceeds_on_a_live_backup_but_names_it() -> None:
    r = _decide(
        "04:22",
        f"{MIG}/0235_x.sql",
        ignore="1",
        live="running",
        slot="03:30",
        detail=_LIVE_DETAIL,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("NOTE: --ignore-backup-window")
    # The override is honoured, and the log stays honest about what runs.
    assert "IS running" in r.stdout and "1234" in r.stdout


# ── pending migrations: offline, from git, origin/prod..target ─────────────


@pytest.fixture
def mig_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """base(+0001) → prod(+0002) → code-only → mig(+0003, edits 0001)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    shas = {"base": _commit(repo, f"{MIG}/0001_base.sql", "base")}
    shas["prod"] = _commit(repo, f"{MIG}/0002_prod.sql", "prod")
    _git(repo, "update-ref", "refs/remotes/origin/prod", shas["prod"])
    shas["code"] = _commit(repo, "src/precis/thing.py", "code only")
    (repo / MIG / "0001_base.sql").write_text("-- edited\n", encoding="utf-8")
    shas["mig"] = _commit(repo, f"{MIG}/0003_new.sql", "new migration")
    return repo, shas


def test_pending_is_the_sql_added_between_origin_prod_and_target(
    mig_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, shas = mig_repo
    r = _lib("deploy_pending_migrations", str(repo), shas["mig"], cwd=repo)
    assert r.returncode == 0, r.stderr
    # Added only: the edit to 0001 and the already-deployed 0002 do not count.
    assert r.stdout.split() == [f"{MIG}/0003_new.sql"]

    r = _lib("deploy_pending_migrations", str(repo), shas["code"], cwd=repo)
    assert r.returncode == 0 and r.stdout == ""

    # Equal sha (no-op redeploy) and an ancestor (rollback) carry nothing.
    for target in (shas["prod"], shas["base"]):
        r = _lib("deploy_pending_migrations", str(repo), target, cwd=repo)
        assert r.returncode == 0 and r.stdout == "", target


def test_pending_is_unknown_when_an_end_does_not_resolve(
    mig_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, shas = mig_repo
    r = _lib("deploy_pending_migrations", str(repo), "no-such-ref", cwd=repo)
    assert r.returncode == 2 and "target no-such-ref does not resolve" in r.stderr
    _git(repo, "update-ref", "-d", "refs/remotes/origin/prod")
    r = _lib("deploy_pending_migrations", str(repo), shas["mig"], cwd=repo)
    assert r.returncode == 2 and "origin/prod does not resolve" in r.stderr


def test_guard_composes_bounds_clock_and_pending(
    mig_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, shas = mig_repo
    f = repo / DEFAULTS_REL
    f.parent.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / DEFAULTS_REL, f)

    r = _lib("backup_window_guard", str(repo), shas["mig"], "0", DEPLOY_NOW_UTC="03:40")
    assert r.returncode == 1 and "0003_new.sql" in r.stdout
    r = _lib(
        "backup_window_guard", str(repo), shas["code"], "0", DEPLOY_NOW_UTC="03:40"
    )
    assert r.returncode == 0 and r.stdout.startswith("WARNING:")
    r = _lib("backup_window_guard", str(repo), shas["mig"], "1", DEPLOY_NOW_UTC="03:40")
    assert r.returncode == 0 and r.stdout.startswith("NOTE:")
    r = _lib("backup_window_guard", str(repo), shas["mig"], "0", DEPLOY_NOW_UTC="12:00")
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    # No origin/prod → unknown → refused inside the window, silent outside.
    _git(repo, "update-ref", "-d", "refs/remotes/origin/prod")
    r = _lib("backup_window_guard", str(repo), shas["mig"], "0", DEPLOY_NOW_UTC="03:40")
    assert r.returncode == 1 and "UNKNOWN" in r.stdout and "origin/prod" in r.stderr
    r = _lib("backup_window_guard", str(repo), shas["mig"], "0", DEPLOY_NOW_UTC="12:00")
    assert r.returncode == 0 and r.stdout == ""


def test_dir_window_guard_refuses_and_proceeds(
    mig_repo: tuple[Path, dict[str, str]],
) -> None:
    """gr513885: the 21:45Z dir-dump leg is a second window — a migration
    deploy inside it refuses (the fixed window is the fallback; no prod-psql
    in the throwaway repo), outside both legs it is silent."""
    repo, shas = mig_repo
    f = repo / DEFAULTS_REL
    f.parent.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / DEFAULTS_REL, f)
    r = _lib("backup_window_guard", str(repo), shas["mig"], "0", DEPLOY_NOW_UTC="22:30")
    assert r.returncode == 1
    assert "21:30–23:30 UTC" in r.stdout and "ends at 23:30 UTC" in r.stdout
    assert "0003_new.sql" in r.stdout
    for now in ("21:29", "23:30"):
        r = _lib("backup_window_guard", str(repo), shas["mig"], "0", DEPLOY_NOW_UTC=now)
        assert r.returncode == 0 and r.stdout == "" and r.stderr == "", (now, r)


def test_dir_window_clear_probe_overrides_only_after_its_slot(
    mig_repo: tuple[Path, dict[str, str]],
) -> None:
    """The per-window cron slot matters: a clear probe at 21:40 cannot clear
    the 21:45 run still ahead; the same probe at 23:00 can."""
    repo, shas = mig_repo
    f = repo / DEFAULTS_REL
    f.parent.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / DEFAULTS_REL, f)
    _stub_prod_psql(repo, "#!/bin/sh\nprintf 'BEGIN\\nCOMMIT\\n'\n")
    r = _lib("backup_window_guard", str(repo), shas["mig"], "0", DEPLOY_NOW_UTC="21:40")
    assert r.returncode == 1
    assert "21:45 UTC" in r.stdout and "still ahead" in r.stdout
    r = _lib("backup_window_guard", str(repo), shas["mig"], "0", DEPLOY_NOW_UTC="23:00")
    assert r.returncode == 0 and r.stdout.startswith("NOTE: live check")


# ── the live probe through a stubbed scripts/prod-psql ─────────────────────


def _stub_prod_psql(repo: Path, body: str) -> Path:
    """A fake repo scripts/prod-psql: the probe calls it, never the real
    one — the tests never touch prod."""
    p = repo / "scripts" / "prod-psql"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    p.chmod(0o755)
    return p


def test_live_backups_probe_parses_only_backend_rows(
    mig_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, _ = mig_repo
    _stub_prod_psql(
        repo,
        "#!/bin/sh\n"
        "printf 'BEGIN\\n"
        "1234|pg_dump|client backend|active|00:52:10.331\\n"
        "1299|pg_basebackup|walsender|active|00:01:02.001\\n"
        "COMMIT\\n'\n",
    )
    r = _lib("backup_window_live_backups", str(repo), cwd=repo)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert len(lines) == 2 and lines[0].startswith("1234|pg_dump")


def test_live_backups_probe_clear_and_unrunnable(
    mig_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, _ = mig_repo
    r = _lib("backup_window_live_backups", str(repo), cwd=repo)
    assert r.returncode == 2  # no prod-psql in the tree at all
    _stub_prod_psql(repo, "#!/bin/sh\nexit 42\n")
    r = _lib("backup_window_live_backups", str(repo), cwd=repo)
    assert r.returncode == 2
    _stub_prod_psql(repo, "#!/bin/sh\nprintf 'BEGIN\\nCOMMIT\\n'\n")
    r = _lib("backup_window_live_backups", str(repo), cwd=repo)
    assert r.returncode == 0 and r.stdout == ""


def test_guard_refuses_on_a_live_dump_outside_the_window(
    mig_repo: tuple[Path, dict[str, str]],
) -> None:
    """The gr511981 incident, reproduced at the guard: 04:22Z, dump still
    running, pending migration — refused, no clock involved."""
    repo, shas = mig_repo
    f = repo / DEFAULTS_REL
    f.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / DEFAULTS_REL, f)
    _stub_prod_psql(
        repo,
        "#!/bin/sh\nprintf 'BEGIN\\n1234|pg_dump|client backend|active|00:52:10\\nCOMMIT\\n'\n",
    )
    r = _lib("backup_window_guard", str(repo), shas["mig"], "0", DEPLOY_NOW_UTC="04:22")
    assert r.returncode == 1
    assert "a backup is running on the prod DB right now" in r.stdout
    assert "pg_dump" in r.stdout


def test_guard_proceeds_on_a_clear_probe_after_the_slot(
    mig_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, shas = mig_repo
    f = repo / DEFAULTS_REL
    f.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / DEFAULTS_REL, f)
    _stub_prod_psql(repo, "#!/bin/sh\nprintf 'BEGIN\\nCOMMIT\\n'\n")
    r = _lib("backup_window_guard", str(repo), shas["mig"], "0", DEPLOY_NOW_UTC="04:05")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("NOTE: live check")


def test_guard_without_prod_psql_keeps_the_window_fallback(
    mig_repo: tuple[Path, dict[str, str]],
) -> None:
    """No prod-psql in the tree: live=unavailable, the fixed window decides
    exactly as it always did."""
    repo, shas = mig_repo
    f = repo / DEFAULTS_REL
    f.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / DEFAULTS_REL, f)
    r = _lib("backup_window_guard", str(repo), shas["mig"], "0", DEPLOY_NOW_UTC="03:40")
    assert r.returncode == 1 and "fixed window decides" in r.stdout


# ── (a) end to end: the REAL scripts/deploy stops before any ansible call ──

# Each fake binary records that it was called; the refusal must leave both
# files absent. The ping fake answers like the real one so the proceed cases
# get past reachability.
_FAKE_ANSIBLE = """#!/usr/bin/env bash
touch "$PRECIS_TEST_CALLED_DIR/ansible"
for h in gateway scheduler data inference serving; do
    printf '%s | SUCCESS => {\\n    "changed": false,\\n    "ping": "pong"\\n}\\n' "$h"
done
"""
_FAKE_PLAYBOOK = """#!/usr/bin/env bash
touch "$PRECIS_TEST_CALLED_DIR/ansible-playbook"
cat <<'RECAP'
PLAY RECAP *********************************************************
gateway                    : ok=1    changed=0    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0
scheduler                  : ok=1    changed=0    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0
data                       : ok=1    changed=0    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0
inference                  : ok=1    changed=0    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0
serving                    : ok=1    changed=0    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0
RECAP
"""
# The deploy's full-ansible preflight runs `ansible-doc -t module
# community.general.homebrew`; without a fake, a real ansible-core on PATH
# (the macos CI leg) fails the lookup and the deploy dies before the guard
# path under test (gr543576).
_FAKE_ANSIBLE_DOC = """#!/usr/bin/env bash
exit 0
"""


class DeployFixture:
    def __init__(
        self,
        repo: Path,
        cluster: Path,
        fakebin: Path,
        called: Path,
        shas: dict[str, str],
    ):
        self.repo, self.cluster, self.fakebin, self.called, self.shas = (
            repo,
            cluster,
            fakebin,
            called,
            shas,
        )

    def run(self, *args: str, now: str) -> subprocess.CompletedProcess[str]:
        env = _test_env(
            PRECIS_DEPLOY_SKIP_CATPATH_WHEEL="1",
            PRECIS_DEPLOY_SKIP_WHEEL_SMOKE="1",
            PRECIS_DEPLOY_FROM_TREE="",
            PRECIS_DEPLOY_ALLOW_STALE="1",
            PRECIS_CLUSTER_DIR=str(self.cluster),
            PRECIS_DEPLOY_NO_LOG="1",
            PRECIS_ENV_POINTERS="0",
            PRECIS_TEST_CALLED_DIR=str(self.called),
            DEPLOY_NOW_UTC=now,
        )
        env["PATH"] = f"{self.fakebin}:{env['PATH']}"
        return subprocess.run(
            ["bash", str(self.repo / "scripts" / "deploy"), *args],
            cwd=str(self.repo),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )

    def ansible_called(self) -> set[str]:
        return {p.name for p in self.called.iterdir()}


@pytest.fixture
def deploy_fx(tmp_path: Path) -> DeployFixture:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for rel, src in (
        ("scripts/deploy", DEPLOY_SRC),
        ("scripts/lib/deploy-state.sh", DEPLOY_STATE_LIB_SRC),
        ("scripts/lib/lock-holder.sh", LOCK_HOLDER_LIB_SRC),
        ("scripts/lib/backup-window.sh", LIB),
        (str(DEFAULTS_REL), REPO_ROOT / DEFAULTS_REL),
    ):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, repo / rel)
    (repo / "scripts" / "deploy").chmod(0o755)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "scripts")
    shas = {"prod": _commit(repo, f"{MIG}/0001_base.sql", "prod")}
    shas["mig"] = _commit(repo, f"{MIG}/0002_new.sql", "new migration")
    # The code-only target sits on a side branch: on main it would be an
    # ancestor of origin/main and the rollback guard (gr338201) would refuse
    # it before this guard ever saw it.
    _git(repo, "checkout", "-q", "-b", "code-only", shas["prod"])
    shas["code"] = _commit(repo, "src/precis/thing.py", "code only")
    _git(repo, "checkout", "-q", "main")
    # A real origin so `git fetch origin prod` inside scripts/deploy is live
    # and origin/prod is what it would be on an operator's machine.
    origin = tmp_path / "origin.git"
    _git(repo, "init", "-q", "--bare", str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    _git(
        repo,
        "push",
        "-q",
        "origin",
        "main",
        "code-only",
        f"{shas['prod']}:refs/heads/prod",
    )
    _git(repo, "fetch", "-q", "origin")

    cluster = tmp_path / "cluster"
    cluster.mkdir()
    (cluster / "redeploy-precis.yml").write_text("---\n", encoding="utf-8")
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    for name, body in (
        ("ansible", _FAKE_ANSIBLE),
        ("ansible-playbook", _FAKE_PLAYBOOK),
        ("ansible-doc", _FAKE_ANSIBLE_DOC),
    ):
        (fakebin / name).write_text(body, encoding="utf-8")
        (fakebin / name).chmod(0o755)
    called = tmp_path / "called"
    called.mkdir()
    return DeployFixture(repo, cluster, fakebin, called, shas)


def test_deploy_refuses_migration_inside_window_before_ansible(
    deploy_fx: DeployFixture,
) -> None:
    """(a) through the real script: non-zero, names the window and its end,
    and neither `ansible` (the reachability ping) nor `ansible-playbook` ran."""
    r = deploy_fx.run(deploy_fx.shas["mig"], now="03:40")
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "REFUSING TO DEPLOY" in out and "03:00–04:20 UTC" in out
    assert "ends at 04:20 UTC" in out and "0002_new.sql" in out
    assert "backup-window guard refused" in out
    assert deploy_fx.ansible_called() == set()


def test_deploy_warns_and_proceeds_without_migration_inside_window(
    deploy_fx: DeployFixture,
) -> None:
    """(b) through the real script."""
    r = deploy_fx.run(deploy_fx.shas["code"], now="03:40")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARNING: inside the 03:00–04:20 UTC backup window" in r.stdout
    assert deploy_fx.ansible_called() == {"ansible", "ansible-playbook"}


def test_deploy_outside_window_is_unchanged(deploy_fx: DeployFixture) -> None:
    """(c) through the real script: the guard says nothing."""
    r = deploy_fx.run(deploy_fx.shas["mig"], now="12:00")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "backup window" not in r.stdout and "backup-window" not in r.stdout
    assert deploy_fx.ansible_called() == {"ansible", "ansible-playbook"}


def test_deploy_override_flag_proceeds_inside_window(deploy_fx: DeployFixture) -> None:
    r = deploy_fx.run(deploy_fx.shas["mig"], "--ignore-backup-window", now="03:40")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOTE: --ignore-backup-window" in r.stdout
    assert deploy_fx.ansible_called() == {"ansible", "ansible-playbook"}


def test_deploy_refuses_migration_inside_dir_window_before_ansible(
    deploy_fx: DeployFixture,
) -> None:
    """gr513885 through the real script: the 21:45Z dir-dump leg refuses a
    migration deploy at 22:30Z before either ansible stub ran."""
    r = deploy_fx.run(deploy_fx.shas["mig"], now="22:30")
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "REFUSING TO DEPLOY" in out and "21:30–23:30 UTC" in out
    assert "ends at 23:30 UTC" in out and "backup-window guard refused" in out
    assert deploy_fx.ansible_called() == set()


def test_deploy_refuses_migration_on_a_live_dump_outside_the_window(
    deploy_fx: DeployFixture,
) -> None:
    """gr511981 through the real script: pg_stat_activity shows pg_dump at
    noon — refused before either ansible stub ran."""
    _stub_prod_psql(
        deploy_fx.repo,
        "#!/bin/sh\nprintf 'BEGIN\\n1234|pg_dump|client backend|active|00:52:10\\nCOMMIT\\n'\n",
    )
    r = deploy_fx.run(deploy_fx.shas["mig"], now="12:00")
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "a backup is running on the prod DB right now" in out
    assert "backup-window guard refused" in out
    assert deploy_fx.ansible_called() == set()


def test_prune_local_sweeps_incomplete_dump_dirs_and_nas_partials() -> None:
    """A killed pg_dump leaves a toc.dat-less dir; prune_local must reclaim it
    (and stale NAS partials) without touching a running or current dump
    (gripe 466216)."""
    tpl = (REPO_ROOT / "deploy/roles/backups/templates/pg_backup.sh.j2").read_text(
        encoding="utf-8"
    )
    body = tpl.split("prune_local() {", 1)[1].split("\n}\n", 1)[0]
    assert "-mmin +360" in body
    assert '! -path "${LOCAL_DUMP_DIR}"' in body
    assert "! -exec test -e {}/toc.dat" in body
    assert "precis_prod_*.tar.zst.partial.*" in body

"""``scripts/lib/backup-window.sh``: no migration deploy inside the nightly
backup window.

The prod DB node reboots at 03:00 UTC and the nightly pg_dump runs 03:30 to
about 04:18 UTC holding ACCESS SHARE on every table, so a migration's ALTER
TABLE waits behind the whole dump (2026-09-08: a /go at 03:22 UTC hung 17+
min with a silent log). ``scripts/deploy`` now refuses a deploy that carries
a pending migration while the window is open, warns-and-proceeds otherwise,
and takes ``--ignore-backup-window`` as the operator override.

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
    now: str, pending: str, ignore: str = "0", start: str = "03:00", end: str = "04:20"
) -> subprocess.CompletedProcess[str]:
    return _lib("backup_window_decide", start, end, now, pending, ignore)


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

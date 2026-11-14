"""scripts/deploy refuses a provably wrong deploy before it renders credentials.

2026-11-14 (gr680328, gr678404): a deploy launched from a Mac whose private
overlay had lagged its deploy-helper remote for two months rendered pgpass
and the pgbouncer userlist WITHOUT precis_system (the role lived only in the
helper's copy), then migrated as agent_rw after the db-own cutover had made
precis_owner the owner of public._migrations. Both facts were knowable before
any play ran.

Policy pinned here: a guard DIES on a provable mismatch (overlay diverged
from deploy-helper/main or carrying uncommitted edits; prod owner disagrees
with the overlay's precis_db_own_cutover_done), REPAIRS what is safely
repairable (a lagging overlay fast-forwards, an overlay ahead pushes) and
WARNS on anything it cannot read (no remote, fetch failure, unreadable prod
owner) -- an unreachable helper or a failed prod read never blocks a deploy
by itself.

Layers:
  A. the overlay sync preflight (``scripts/lib/overlay-sync.sh``, run at
     scripts/deploy step 0) against real git and local-path remotes — the
     wiring and the first guard's override spelling; every relation row is
     driven in tests/test_overlay_sync.py.
  B. ``_deploy_owner_preflight`` with stub ``scripts/prod-psql`` and
     ``ansible-inventory`` (same extraction pattern as
     tests/test_deploy_role_sync.py).
  C. Structural checks on the ansible refusal tasks in the pgbouncer and
     pgpass roles (no ansible run; the repo has no real render harness).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the shipped scripts under test are POSIX shell",
)

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "scripts" / "deploy"
PGB_TASKS = ROOT / "deploy" / "roles" / "pgbouncer" / "tasks" / "main.yml"
PGPASS_TASKS = ROOT / "deploy" / "roles" / "pgpass" / "tasks" / "main.yml"


def _funcs() -> str:
    m = re.search(
        r"# BEGIN _deploy_role_sync.*?\n(.*?)# END _deploy_role_sync",
        DEPLOY.read_text(encoding="utf-8"),
        re.S,
    )
    assert m, "_deploy_role_sync markers missing from scripts/deploy"
    return m.group(1)


_PRELUDE = """
set -euo pipefail
say() { printf 'say: %s\\n' "$*"; }
die() { printf 'DIE: %s\\n' "$*" >&2; exit 1; }
inventory_group_hosts() { echo node-gw; }
"""


def _bash(
    script: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    full_env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")}
    full_env.update(env or {})
    return subprocess.run(
        ["bash", "-c", _PRELUDE + _funcs() + script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env=full_env,
    )


def _out(r: subprocess.CompletedProcess[str]) -> str:
    return r.stdout + r.stderr


# -- A. overlay lag guard ---------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return r.stdout.strip()


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)


class Overlay:
    def __init__(self, tmp_path: Path) -> None:
        self.bare = tmp_path / "helper.git"
        self.overlay = tmp_path / "overlay"
        self.other = tmp_path / "other"  # second clone: advances the helper
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.bare))
        self.overlay.mkdir()
        _git(self.overlay, "init", "-q", "-b", "main")
        _commit(self.overlay, "base")
        _git(self.overlay, "remote", "add", "deploy-helper", str(self.bare))
        _git(self.overlay, "push", "-q", "deploy-helper", "main")
        _git(tmp_path, "clone", "-q", "-b", "main", str(self.bare), str(self.other))

    def helper_commit(self, name: str = "helper-extra") -> None:
        _commit(self.other, name)
        _git(self.other, "push", "-q", "origin", "main")

    def local_commit(self, name: str = "local-extra") -> None:
        _commit(self.overlay, name)


@pytest.fixture
def ov(tmp_path: Path) -> Overlay:
    return Overlay(tmp_path)


LIB = ROOT / "scripts" / "lib" / "overlay-sync.sh"


def _sync(
    overlay: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the preflight the way scripts/deploy does (set -euo pipefail)."""
    full_env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")}
    full_env.update(env or {})
    return subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail; . "$1"; overlay_sync_preflight "$2" overlay; echo RETURNED',
            "_",
            str(LIB),
            str(overlay),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env=full_env,
    )


def test_deploy_runs_the_sync_preflight_at_step_zero() -> None:
    """The die-only overlay-guard that first shipped in the role-sync block
    folded into the sync preflight: one mechanism, run before the overlay
    hash (a fetch must not count as an overlay change) and before the owner
    preflight."""
    text = DEPLOY.read_text(encoding="utf-8")
    assert "_deploy_overlay_lag_guard" not in text
    assert "scripts/lib/overlay-sync.sh" in text
    first = text.index("overlay_sync_preflight")
    assert first < text.index('OVERLAY_HASH="$(_overlay_hash')
    assert first < text.index("_deploy_owner_preflight")


def test_sync_in_sync_returns(ov: Overlay) -> None:
    r = _sync(ov.overlay)
    assert r.returncode == 0, _out(r)
    assert "in sync" in r.stdout and "RETURNED" in r.stdout, _out(r)


def test_sync_lagging_fast_forwards_instead_of_dying(ov: Overlay) -> None:
    ov.helper_commit()
    r = _sync(ov.overlay)
    assert r.returncode == 0, _out(r)
    assert "fast-forwarded" in r.stdout and "RETURNED" in r.stdout, _out(r)
    assert _git(ov.overlay, "rev-parse", "HEAD") == _git(ov.other, "rev-parse", "HEAD")


def test_sync_ahead_pushes_so_the_fix_lane_copy_matches(ov: Overlay) -> None:
    ov.local_commit()
    r = _sync(ov.overlay)
    assert r.returncode == 0, _out(r)
    assert "pushed" in r.stdout, _out(r)
    assert _git(ov.overlay, "rev-parse", "HEAD") == _git(
        ov.overlay, "rev-parse", "deploy-helper/main"
    )


def test_sync_first_guards_override_spelling_still_works(ov: Overlay) -> None:
    ov.helper_commit()
    before = _git(ov.overlay, "rev-parse", "HEAD")
    r = _sync(ov.overlay, {"PRECIS_DEPLOY_ALLOW_STALE_OVERLAY": "1"})
    assert r.returncode == 0, _out(r)
    assert "WARNING" in r.stderr and "behind" in r.stderr, _out(r)
    assert _git(ov.overlay, "rev-parse", "HEAD") == before  # reported, not moved


def test_sync_diverged_dies_and_override_warns(ov: Overlay) -> None:
    ov.helper_commit()
    ov.local_commit()
    r = _sync(ov.overlay)
    assert r.returncode != 0, _out(r)
    assert "DIVERGED" in r.stderr and "RETURNED" not in r.stdout, _out(r)
    r = _sync(ov.overlay, {"PRECIS_DEPLOY_ALLOW_STALE_OVERLAY": "1"})
    assert r.returncode == 0, _out(r)
    assert "WARNING" in r.stderr and "DIVERGED" in r.stderr, _out(r)


def test_sync_dirty_tracked_file_dies(ov: Overlay) -> None:
    ov.helper_commit()
    (ov.overlay / "base").write_text("edited", encoding="utf-8")
    r = _sync(ov.overlay)
    assert r.returncode != 0, _out(r)
    assert "uncommitted" in r.stderr and "RETURNED" not in r.stdout, _out(r)
    # Refused before the fetch: the lag was never acted on.
    assert "fast-forwarded" not in r.stdout


def test_sync_fetch_failure_warns_and_continues(ov: Overlay, tmp_path: Path) -> None:
    _git(
        ov.overlay, "remote", "set-url", "deploy-helper", str(tmp_path / "nonexistent")
    )
    r = _sync(ov.overlay)
    assert r.returncode == 0, _out(r)
    assert "WARNING" in r.stderr and "could not fetch" in r.stderr, _out(r)
    assert "RETURNED" in r.stdout


# -- B. owner preflight -----------------------------------------------------

_FAKE_ANSIBLE_INVENTORY = """#!/usr/bin/env bash
case "${FAKE_FLAG:-absent}" in
    absent) echo "unrelated_var: 1" ;;
    *) echo "precis_db_own_cutover_done: ${FAKE_FLAG}" ;;
esac
"""

# prod-psql stub: requires --ro and a SQL arg; prints $FAKE_PSQL_OUT.
_FAKE_PROD_PSQL = """#!/usr/bin/env bash
[ "$1" = "--ro" ] || { echo "stub: --ro missing" >&2; exit 2; }
[ -n "${2:-}" ] || { echo "stub: SQL missing" >&2; exit 2; }
printf '%s' "${FAKE_PSQL_OUT-}"
"""


def _preflight(
    tmp_path: Path,
    *,
    psql_out: str | None,
    flag: str = "absent",
    with_psql: bool = True,
) -> subprocess.CompletedProcess[str]:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ansible-inventory").write_text(_FAKE_ANSIBLE_INVENTORY, encoding="utf-8")
    (bindir / "ansible-inventory").chmod(0o755)
    if with_psql:
        (root / "scripts" / "prod-psql").write_text(_FAKE_PROD_PSQL, encoding="utf-8")
        (root / "scripts" / "prod-psql").chmod(0o755)
    env = {"PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_FLAG": flag}
    if psql_out is not None:
        env["FAKE_PSQL_OUT"] = psql_out
    return _bash(
        f"REPO_ROOT='{root}'\nINV_ARGS=()\n_deploy_owner_preflight\necho RETURNED\n",
        env,
    )


def test_preflight_without_prod_psql_is_silent(tmp_path: Path) -> None:
    r = _preflight(tmp_path, psql_out="precis_owner\n", with_psql=False)
    assert r.returncode == 0, _out(r)
    assert "say:" not in r.stdout and r.stderr == "", _out(r)


@pytest.mark.parametrize("garbage", ["", "ERROR: boom\n", "Owner Name With Spaces\n"])
def test_preflight_unreadable_owner_warns(tmp_path: Path, garbage: str) -> None:
    r = _preflight(tmp_path, psql_out=garbage, flag="true")
    assert r.returncode == 0, _out(r)
    assert "WARNING" in r.stdout and "could not read the owner" in r.stdout, _out(r)


def test_preflight_owner_and_flag_agree_post_cutover(tmp_path: Path) -> None:
    r = _preflight(tmp_path, psql_out="precis_owner\n", flag="true")
    assert r.returncode == 0, _out(r)
    assert "agrees" in r.stdout, _out(r)


@pytest.mark.parametrize("flag", ["false", "absent"])
def test_preflight_cutover_ran_but_overlay_flag_not_set_dies(
    tmp_path: Path, flag: str
) -> None:
    r = _preflight(tmp_path, psql_out="precis_owner\n", flag=flag)
    assert r.returncode != 0, _out(r)
    assert "precis_db_own_cutover_done: true" in r.stderr, _out(r)
    assert "gr678404" in r.stderr, _out(r)


def test_preflight_flag_true_but_prod_not_cut_over_dies(tmp_path: Path) -> None:
    r = _preflight(tmp_path, psql_out="agent_rw\n", flag="true")
    assert r.returncode != 0, _out(r)
    assert "cutover has not run" in r.stderr, _out(r)


def test_preflight_pre_cutover_agrees(tmp_path: Path) -> None:
    r = _preflight(tmp_path, psql_out="agent_rw\n", flag="false")
    assert r.returncode == 0, _out(r)
    assert "agrees" in r.stdout, _out(r)


# -- C. ansible refusal tasks (structural) ----------------------------------

_PB_USERLIST = "Deploy userlist.txt"
_PB_NOPW = (
    "Refuse to render userlist.txt while a role in postgres_roles has no password var"
)
_PB_NOROLES = (
    "Refuse to render userlist.txt while the db-own roles are missing "
    "from postgres_roles"
)
_PGPASS_DEPLOY = "Deploy .pgpass for deploy user"
_PGPASS_REFUSALS = [
    "Refuse to render pgpass while a role in postgres_roles has no password var",
    "Refuse to render pgpass while the db-own roles are missing from postgres_roles",
]


def _tasks(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _index(tasks: list[dict], name: str) -> int:
    names = [t.get("name") for t in tasks]
    assert names.count(name) == 1, f"{name!r} x{names.count(name)} in {names}"
    return names.index(name)


@pytest.mark.parametrize("name", [_PB_NOPW, _PB_NOROLES])
def test_pgbouncer_refusal_precedes_userlist_render(name: str) -> None:
    tasks = _tasks(PGB_TASKS)
    i = _index(tasks, name)
    assert i < _index(tasks, _PB_USERLIST)
    assert tasks[i]["tags"] == ["roles"]
    assert "ansible.builtin.assert" in tasks[i]


@pytest.mark.parametrize("name", _PGPASS_REFUSALS)
def test_pgpass_refusal_precedes_pgpass_render(name: str) -> None:
    tasks = _tasks(PGPASS_TASKS)
    i = _index(tasks, name)
    assert i < _index(tasks, _PGPASS_DEPLOY)
    assert "ansible.builtin.assert" in tasks[i]


@pytest.mark.parametrize(
    ("path", "names"),
    [(PGB_TASKS, [_PB_NOPW, _PB_NOROLES]), (PGPASS_TASKS, _PGPASS_REFUSALS)],
    ids=["pgbouncer", "pgpass"],
)
def test_refusal_tasks_never_spell_a_vault_variable(
    path: Path, names: list[str]
) -> None:
    # The variable name is assembled ('vault_' ~ password_var); the
    # lanes-deny scan flags literal credential variable names.
    found = 0
    for t in _tasks(path):
        if t.get("name") in names:
            found += 1
            assert "vault_pg_" not in yaml.safe_dump(t), t["name"]
    assert found == len(names)

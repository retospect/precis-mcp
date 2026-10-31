"""A literal-sha deploy renders `deploy/` from the TARGET sha, not from HEAD.

`scripts/deploy` had two sources of truth for one deploy: the venvs installed
``precis-mcp@<sha>`` from git, while ansible rendered ``deploy/`` roles and
templates from whatever working tree the caller happened to invoke it in.
Every rule around deploys followed from that split — sync your checkout first,
do not edit the tree mid-deploy, and (the one that actually cost time) a
sibling landing on ``main`` could invalidate a deploy that had already pinned
its sha, because the *templates* were still tied to a moving branch.

The fix is to check the pinned sha out into a throwaway detached worktree and
render from there, so HEAD == target by construction. These tests pin the
properties that buys:

  (a) templates come from the target sha even when HEAD is somewhere else
  (b) a dirty working tree no longer blocks a pinned deploy
  (c) the throwaway worktree is removed afterwards, including on failure
  (d) a ref-NAME target still renders from the checkout — the playbook
      resolves names itself via ls-remote, so resolving them twice could
      disagree, and the freshness guard still covers that path
  (e) the worktree is locked with a live-pid reason while ansible runs, so
      a sibling session's SessionStart `scripts/reap-worktrees` (which
      removes every clean, merged, sessionless tree — and a detached tree at
      a sha in main reads as merged) cannot delete it mid-deploy

Same technique as tests/test_deploy_pinned_sha.py: the real script is copied
byte-for-byte into a throwaway repo at its real relative path and driven with
fake ansible binaries on $PATH. The fake playbook records the tree it was
actually run against, which is the thing under test.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the shipped scripts under test are POSIX shell",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SRC = REPO_ROOT / "scripts" / "deploy"
DEPLOY_STATE_LIB_SRC = REPO_ROOT / "scripts" / "lib" / "deploy-state.sh"
LOCK_HOLDER_LIB_SRC = REPO_ROOT / "scripts" / "lib" / "lock-holder.sh"


def _test_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
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


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=_test_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _lib_call(fn: str, *args: str) -> str:
    quoted = " ".join(f"'{a}'" for a in args)
    result = subprocess.run(
        ["bash", "-c", f"source '{DEPLOY_STATE_LIB_SRC}'; {fn} {quoted}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


# FAKE_CRITICAL (whitespace list) answers the `ansible data:gateway
# --list-hosts` critical-set probe; FAKE_PING_DOWN (whitespace list) marks
# hosts | UNREACHABLE! in the -m ping output and exits non-zero, the shape a
# dropped Tailscale host produces.
_FAKE_ANSIBLE = """#!/usr/bin/env bash
for a in "$@"; do
    if [ "$a" = "--list-hosts" ]; then
        set -- ${FAKE_CRITICAL:-}
        printf '  hosts (%d):\\n' "$#"
        for h in "$@"; do printf '    %s\\n' "$h"; done
        exit 0
    fi
done
down=" ${FAKE_PING_DOWN:-} "
rc=0
for h in gateway scheduler data inference serving; do
    case "$down" in
        *" $h "*) printf '%s | UNREACHABLE! => {\n    "unreachable": true\n}\n' "$h"; rc=1 ;;
        *) printf '%s | SUCCESS => {\n    "changed": false,\n    "ping": "pong"\n}\n' "$h" ;;
    esac
done
exit "$rc"
"""

# Records the tree ansible was actually pointed at: its cwd, and the STAMP
# file that differs per commit. That pair is the whole assertion surface.
_FAKE_ANSIBLE_PLAYBOOK = """#!/usr/bin/env bash
{
  printf 'PWD=%s\n' "$PWD"
  printf 'STAMP=%s\n' "$(cat ./STAMP 2>/dev/null || echo MISSING)"
  printf 'LOCKED=%s\n' "$(git worktree list --porcelain 2>/dev/null | grep '^locked' | head -1)"
  printf 'ARGS=%s\n' "$*"
} >> "$RENDER_RECORD"
# Test seam: mid-run, overwrite the deploy lock's holder file with a
# DIFFERENT live pid — the state a deploy sees when a sibling stole and
# re-acquired the lock underneath it. The EXIT trap must leave it alone.
if [ -n "${FAKE_LOCK_HOLDER_PID:-}" ]; then
  printf '/x pid=%s host=%s\n' "$FAKE_LOCK_HOLDER_PID" "${FAKE_HOSTNAME:-$(hostname -s)}" > "$FAKE_LOCKDIR/holder"
fi
if [ -n "${FAKE_PLAYBOOK_OUT:-}" ]; then
  cat "$FAKE_PLAYBOOK_OUT"
  exit "${FAKE_PLAYBOOK_RC:-0}"
fi
cat <<'RECAP'
PLAY RECAP *********************************************************
gateway                    : ok=10   changed=2    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
scheduler                  : ok=10   changed=2    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
data                       : ok=8    changed=1    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
inference                  : ok=10   changed=2    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
serving                    : ok=5    changed=1    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
RECAP
exit 0
"""


# scripts/deploy's full-ansible preflight (`ansible-doc -t module
# community.general.homebrew`) must see a resolution too — on hosts where
# only ansible-core is installed the real lookup fails and the deploy dies
# before any play (the macos CI leg, gr543576).
_FAKE_ANSIBLE_DOC = """#!/usr/bin/env bash
exit 0
"""


def _make_fake_bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    (bindir / "ansible").write_text(_FAKE_ANSIBLE, encoding="utf-8")
    (bindir / "ansible-playbook").write_text(_FAKE_ANSIBLE_PLAYBOOK, encoding="utf-8")
    (bindir / "ansible-doc").write_text(_FAKE_ANSIBLE_DOC, encoding="utf-8")
    (bindir / "ansible").chmod(0o755)
    (bindir / "ansible-playbook").chmod(0o755)
    (bindir / "ansible-doc").chmod(0o755)
    return bindir


class Fixture:
    def __init__(self, repo: Path, shas: dict[str, str]) -> None:
        self.repo = repo
        self.gated = shas["gated"]
        self.sibling = shas["sibling"]

    def set_marker(self, sha: str) -> None:
        marker = Path(_lib_call("deploy_state_path", str(self.repo)))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{sha} 1700000000 success\n", encoding="utf-8")

    def _common_dir(self) -> Path:
        return Path(
            _git(
                self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"
            ).stdout.strip()
        )

    def render_tree_path(self) -> Path:
        return self._common_dir() / "precis-deploy-tree"

    def lock_path(self) -> Path:
        return self._common_dir() / "precis-deploy.lock"


def _stamped_commit(repo: Path, stamp: str) -> str:
    """One commit whose deploy/STAMP identifies it — the per-commit content
    that proves which tree ansible rendered from."""
    (repo / "deploy" / "STAMP").write_text(f"{stamp}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", stamp)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return build_fixture(tmp_path)


def build_fixture(tmp_path: Path) -> Fixture:
    """Fake repo + fake ansible for scripts/deploy; shared with sibling test files."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "lib").mkdir(parents=True)
    (repo / "deploy" / "inventory").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")

    shutil.copy2(DEPLOY_SRC, repo / "scripts" / "deploy")
    shutil.copy2(DEPLOY_STATE_LIB_SRC, repo / "scripts" / "lib" / "deploy-state.sh")
    shutil.copy2(LOCK_HOLDER_LIB_SRC, repo / "scripts" / "lib" / "lock-holder.sh")
    (repo / "scripts" / "deploy").chmod(0o755)
    # A local deploy/inventory makes the from-tree path take its "local
    # overlay" branch, so the test needs no private overlay on disk.
    (repo / "deploy" / "redeploy-precis.yml").write_text("---\n", encoding="utf-8")
    (repo / "deploy" / "inventory" / "hosts.yml").write_text("all:\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "scaffold")

    gated = _stamped_commit(repo, "GATED")
    sibling = _stamped_commit(repo, "SIBLING")

    origin = tmp_path / "origin.git"
    _git(repo, "init", "-q", "--bare", str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")

    return Fixture(repo, {"gated": gated, "sibling": sibling})


def _run_deploy(
    fx: Fixture,
    fakebin: Path,
    record: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = _test_env(
        PRECIS_DEPLOY_SKIP_CATPATH_WHEEL="1",
        PRECIS_DEPLOY_SKIP_WHEEL_SMOKE="1",
        PRECIS_DEPLOY_NO_LOG="1",
        RENDER_RECORD=str(record),
        **(extra_env or {}),
    )
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env.pop("PRECIS_CLUSTER_DIR", None)
    env.pop("PRECIS_DEPLOY_ALLOW_STALE", None)
    return subprocess.run(
        ["bash", str(fx.repo / "scripts" / "deploy"), *args],
        cwd=str(fx.repo),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def test_templates_come_from_the_target_sha_not_from_head(
    fx: Fixture, tmp_path: Path
) -> None:
    """(a) The property the whole change exists for.

    HEAD is at `sibling` — what a /qland burst would leave behind — while the
    deploy targets the earlier gated sha. Before this change ansible rendered
    SIBLING's templates alongside GATED's code. It must now render GATED's.
    """
    fakebin = _make_fake_bin(tmp_path)
    record = tmp_path / "render.txt"
    fx.set_marker(fx.gated)

    result = _run_deploy(fx, fakebin, record, fx.gated, "--pinned")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    rendered = record.read_text(encoding="utf-8")
    assert "STAMP=GATED" in rendered, (
        f"templates rendered from the wrong tree:\n{rendered}\n{result.stdout}"
    )
    assert "STAMP=SIBLING" not in rendered
    assert "precis-deploy-tree" in rendered, (
        f"expected the detached render worktree, got:\n{rendered}"
    )


def test_a_dirty_checkout_no_longer_blocks_a_pinned_deploy(
    fx: Fixture, tmp_path: Path
) -> None:
    """(b) The old guard refused when `deploy/` had uncommitted changes,
    because those changes would have been rendered. They cannot be now, so
    editing your tree during a deploy is no longer a hazard."""
    fakebin = _make_fake_bin(tmp_path)
    record = tmp_path / "render.txt"
    fx.set_marker(fx.gated)
    (fx.repo / "deploy" / "STAMP").write_text("LOCAL-EDIT\n", encoding="utf-8")

    result = _run_deploy(fx, fakebin, record, fx.gated, "--pinned")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    rendered = record.read_text(encoding="utf-8")
    assert "STAMP=GATED" in rendered
    assert "LOCAL-EDIT" not in rendered, (
        f"an uncommitted local edit reached the fleet:\n{rendered}"
    )


def test_the_render_worktree_is_cleaned_up(fx: Fixture, tmp_path: Path) -> None:
    """(c) It is a throwaway. A leftover would both waste disk and, worse,
    be silently reused by the next deploy if the re-add ever no-opped."""
    fakebin = _make_fake_bin(tmp_path)
    record = tmp_path / "render.txt"
    fx.set_marker(fx.gated)

    result = _run_deploy(fx, fakebin, record, fx.gated, "--pinned")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert not fx.render_tree_path().exists(), "render worktree left behind"
    listed = _git(fx.repo, "worktree", "list").stdout
    assert "precis-deploy-tree" not in listed, (
        f"stale worktree registration would block the next deploy:\n{listed}"
    )


def test_the_render_worktree_is_locked_with_a_live_pid_while_ansible_runs(
    fx: Fixture, tmp_path: Path
) -> None:
    """(e) The render tree is a clean detached worktree at a sha that is in
    main — exactly what scripts/inflight buckets `safe_remove` and every
    sibling session's SessionStart reaper deletes. Observed 2026-09-29: the
    tree vanished mid-deploy and ansible died with a bare `[Errno 2] No such
    file or directory` on the next template task. A `locked … pid N` reason
    with N alive is what inflight reads as a live session, so it is skipped.
    The lock must also come off again, or `worktree remove` refuses and a
    stale registration lingers.
    """
    fakebin = _make_fake_bin(tmp_path)
    record = tmp_path / "render.txt"
    fx.set_marker(fx.gated)

    result = _run_deploy(fx, fakebin, record, fx.gated, "--pinned")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    rendered = record.read_text(encoding="utf-8")
    assert re.search(
        r"^LOCKED=locked pid \d+ scripts/deploy render tree", rendered, re.M
    ), (
        f"render worktree was not locked with a live-pid reason while ansible ran:\n{rendered}"
    )
    assert "precis-deploy-tree" not in _git(fx.repo, "worktree", "list").stdout, (
        "locked render worktree was not unlocked+removed afterwards"
    )


def test_a_stale_render_worktree_from_a_dead_run_is_replaced(
    fx: Fixture, tmp_path: Path
) -> None:
    """(c cont.) A killed deploy leaves the worktree behind. The next run
    holds the deploy lock, so any leftover is dead by definition and must be
    cleared rather than refused — otherwise one SIGKILL wedges all deploys.

    The leftover is a genuinely *registered* worktree, not a bare directory:
    `git worktree add` completes before any ansible step can be interrupted,
    so that is the shape a real kill leaves, and it is the one that needs
    `worktree remove` rather than `rm -rf`. A stale `index.lock` inside it
    stands in for an interrupted git operation.
    """
    fakebin = _make_fake_bin(tmp_path)
    record = tmp_path / "render.txt"
    fx.set_marker(fx.gated)
    stale = fx.render_tree_path()
    _git(fx.repo, "worktree", "add", "--detach", "--quiet", str(stale), fx.sibling)
    (stale / "index.lock").write_text("", encoding="utf-8")
    assert "precis-deploy-tree" in _git(fx.repo, "worktree", "list").stdout

    result = _run_deploy(fx, fakebin, record, fx.gated, "--pinned")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    rendered = record.read_text(encoding="utf-8")
    assert "STAMP=GATED" in rendered, (
        f"the stale worktree's tree was rendered instead of the target's:\n{rendered}"
    )
    assert "STAMP=SIBLING" not in rendered


def test_a_render_tree_owned_by_another_users_live_pid_is_not_swept(
    fx: Fixture, tmp_path: Path
) -> None:
    """The startup sweep deletes `precis-deploy-tree-<pid>` when <pid> is
    dead. A bare `kill -0` also fails (EPERM) on a live pid owned by another
    user, so a deploy run as `reto` deleted a running `deploy`-user run's
    render tree — the 2026-09-27 `[Errno 2]` signature. pid 1 is always
    alive and, outside a root container, never ours."""
    fakebin = _make_fake_bin(tmp_path)
    record = tmp_path / "render.txt"
    fx.set_marker(fx.gated)
    foreign = fx._common_dir() / "precis-deploy-tree-1"
    _git(fx.repo, "worktree", "add", "--detach", "--quiet", str(foreign), fx.sibling)

    result = _run_deploy(fx, fakebin, record, fx.gated, "--pinned")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert (foreign / "deploy" / "STAMP").exists(), (
        "the sweep deleted a render tree whose owner pid is alive"
    )
    assert "STAMP=GATED" in record.read_text(encoding="utf-8")


def _this_host() -> str:
    return subprocess.run(
        ["hostname", "-s"], capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def _write_holder(lockdir: Path, pid: int) -> None:
    """The holder file ``lock_holder_write`` produces, for a chosen pid."""
    lockdir.mkdir(exist_ok=True)
    (lockdir / "holder").write_text(
        f"/fake/worktree pid={pid} host={_this_host()}\n", encoding="utf-8"
    )


def test_a_deploy_lock_held_by_another_users_live_pid_is_not_stolen(
    fx: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same EPERM hole in the dead-holder steal: the lock would be taken from
    a live run, and the sweep above would then delete that run's tree. pid 1
    is live but (on a dev host) signalable only by root — a bare `kill -0`
    reads EPERM as dead."""
    fakebin = _make_fake_bin(tmp_path)
    record = tmp_path / "render.txt"
    fx.set_marker(fx.gated)
    _write_holder(fx.lock_path(), 1)
    monkeypatch.setenv("PRECIS_DEPLOY_LOCK_WAIT", "0")

    result = _run_deploy(fx, fakebin, record, fx.gated, "--pinned")

    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "stole the deploy lock" not in out, out
    assert "still holds the lock" in out, out
    assert "pid=1" in (fx.lock_path() / "holder").read_text(encoding="utf-8")


def test_a_deploy_lock_held_by_a_live_same_user_pid_is_never_stolen(
    fx: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race-table fix: a live holder is never stolen, however stale the
    read that decided it was dead. Pytest's own pid is unambiguously alive."""
    fakebin = _make_fake_bin(tmp_path)
    record = tmp_path / "render.txt"
    fx.set_marker(fx.gated)
    _write_holder(fx.lock_path(), os.getpid())
    monkeypatch.setenv("PRECIS_DEPLOY_LOCK_WAIT", "0")

    result = _run_deploy(fx, fakebin, record, fx.gated, "--pinned")

    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "stole the deploy lock" not in out, out
    assert "still holds the lock" in out, out
    assert f"pid={os.getpid()}" in (fx.lock_path() / "holder").read_text(
        encoding="utf-8"
    )


def test_a_dead_holders_deploy_lock_is_stolen(fx: Fixture, tmp_path: Path) -> None:
    """The path that must keep working: a crashed deploy's lock is stolen —
    through lock_steal, which re-judges the exact dir it removes — and the
    deploy then proceeds normally and releases its own lock on exit."""
    fakebin = _make_fake_bin(tmp_path)
    record = tmp_path / "render.txt"
    fx.set_marker(fx.gated)
    dead = subprocess.Popen(["true"])
    dead.wait()
    _write_holder(fx.lock_path(), dead.pid)

    result = _run_deploy(fx, fakebin, record, fx.gated, "--pinned")

    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "stole the deploy lock" in out, out
    assert not fx.lock_path().exists(), "deploy lock stranded after a green deploy"


def test_the_exit_trap_does_not_release_a_lock_owned_by_another_pid(
    fx: Fixture, tmp_path: Path
) -> None:
    """Second half of the race table: the old trap's unconditional
    `rm -rf $_LOCK_DIR` deleted whatever dir was present — including a live
    sibling's. Here the fake playbook rewrites the holder to a foreign live
    pid mid-run (a sibling that stole and re-acquired); the EXIT trap must
    leave that dir alone."""
    fakebin = _make_fake_bin(tmp_path)
    record = tmp_path / "render.txt"
    fx.set_marker(fx.gated)
    foreign = subprocess.Popen(["sleep", "60"])
    try:
        result = _run_deploy(
            fx,
            fakebin,
            record,
            fx.gated,
            "--pinned",
            extra_env={
                "FAKE_LOCK_HOLDER_PID": str(foreign.pid),
                "FAKE_LOCKDIR": str(fx.lock_path()),
                "FAKE_HOSTNAME": _this_host(),
            },
        )
        out = result.stdout + result.stderr
        assert result.returncode == 0, out
        holder = (fx.lock_path() / "holder").read_text(encoding="utf-8")
        assert f"pid={foreign.pid}" in holder, (
            "the EXIT trap released a lock this deploy did not own:\n" + out
        )
    finally:
        foreign.kill()
        foreign.wait()


# `--version` answers harmlessly: scripts/deploy logs which ansible runs
# before the play, and that probe must not trigger the deletion.
_FAKE_PLAYBOOK_LOSES_ITS_TREE = """#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then echo 'ansible-playbook [core 2.0]'; exit 0; fi
rm -rf "$(dirname "$PWD")"
echo '[ERROR]: Task failed: [Errno 2] No such file or directory'
exit 2
"""


def test_a_render_tree_deleted_mid_run_is_named_as_the_cause(
    fx: Fixture, tmp_path: Path
) -> None:
    """Ansible losing its playbooks mid-run printed only `[Errno 2]`, with
    nothing saying the render tree had vanished. The deploy must say so."""
    fakebin = _make_fake_bin(tmp_path)
    (fakebin / "ansible-playbook").write_text(
        _FAKE_PLAYBOOK_LOSES_ITS_TREE, encoding="utf-8"
    )
    record = tmp_path / "render.txt"
    fx.set_marker(fx.gated)

    result = _run_deploy(fx, fakebin, record, fx.gated, "--pinned")

    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "render worktree" in out and "is GONE" in out, out


def test_the_lock_is_released_on_a_successful_deploy(
    fx: Fixture, tmp_path: Path
) -> None:
    """The render worktree's teardown runs in the same EXIT trap as the lock
    release, ahead of it. Under `set -e` a failing command in a trap aborts
    the rest of the trap, so a teardown that errored would silently strand
    the deploy lock and block every later deploy until the dead-pid steal."""
    fakebin = _make_fake_bin(tmp_path)
    record = tmp_path / "render.txt"
    fx.set_marker(fx.gated)

    result = _run_deploy(fx, fakebin, record, fx.gated, "--pinned")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert not fx.lock_path().exists(), "deploy lock stranded after a green deploy"


def test_a_refused_deploy_still_releases_the_lock_and_keeps_its_exit_code(
    fx: Fixture, tmp_path: Path
) -> None:
    """The other half: the trap must re-raise the status that triggered it.
    Capturing `$?` and `exit "$rc"` is what keeps a refusal reported as a
    refusal — and a *success* reported as success, which is the direction
    that would otherwise turn a completed deploy into a false red."""
    fakebin = _make_fake_bin(tmp_path)
    record = tmp_path / "render.txt"
    # Deployed marker ahead of the target → the rollback guard refuses.
    fx.set_marker(fx.sibling)

    result = _run_deploy(fx, fakebin, record, fx.gated, "--pinned")

    assert result.returncode != 0, (
        f"expected the rollback refusal:\n{result.stdout}\n{result.stderr}"
    )
    assert not fx.lock_path().exists(), "deploy lock stranded after a refusal"


def test_a_ref_name_target_still_renders_from_the_checkout(
    fx: Fixture, tmp_path: Path
) -> None:
    """(d) Only a literal sha gets the worktree. A ref NAME is resolved by the
    playbook itself at pin time via ls-remote; resolving it a second time here
    could pick a different commit, so that path is deliberately unchanged."""
    fakebin = _make_fake_bin(tmp_path)
    record = tmp_path / "render.txt"
    fx.set_marker(fx.sibling)

    result = _run_deploy(fx, fakebin, record, "main")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    rendered = record.read_text(encoding="utf-8")
    assert "precis-deploy-tree" not in rendered, (
        f"a ref-name deploy should render from the checkout:\n{rendered}"
    )


def test_the_wheel_smoke_builds_from_the_render_worktree() -> None:
    """The gr451360 preflight must smoke the artifact that ships: for a
    literal-sha target that is the detached render worktree, not the
    invoking checkout (the two-sources-of-truth split this file exists to
    kill). Static check — the runtime tests above skip the smoke."""
    text = DEPLOY_SRC.read_text(encoding="utf-8")
    assert 'scripts/wheel-smoke" "${_DEPLOY_TREE:-$REPO_ROOT}"' in text

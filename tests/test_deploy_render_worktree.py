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

Same technique as tests/test_deploy_pinned_sha.py: the real script is copied
byte-for-byte into a throwaway repo at its real relative path and driven with
fake ansible binaries on $PATH. The fake playbook records the tree it was
actually run against, which is the thing under test.
"""

from __future__ import annotations

import os
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
        ["git", *args], cwd=str(cwd), env=_test_env(), capture_output=True, text=True
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _lib_call(fn: str, *args: str) -> str:
    quoted = " ".join(f"'{a}'" for a in args)
    result = subprocess.run(
        ["bash", "-c", f"source '{DEPLOY_STATE_LIB_SRC}'; {fn} {quoted}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


_FAKE_ANSIBLE = """#!/usr/bin/env bash
for h in gateway scheduler data inference serving; do
    printf '%s | SUCCESS => {\n    "changed": false,\n    "ping": "pong"\n}\n' "$h"
done
exit 0
"""

# Records the tree ansible was actually pointed at: its cwd, and the STAMP
# file that differs per commit. That pair is the whole assertion surface.
_FAKE_ANSIBLE_PLAYBOOK = """#!/usr/bin/env bash
{
  printf 'PWD=%s\n' "$PWD"
  printf 'STAMP=%s\n' "$(cat ./STAMP 2>/dev/null || echo MISSING)"
} >> "$RENDER_RECORD"
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


def _make_fake_bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    (bindir / "ansible").write_text(_FAKE_ANSIBLE, encoding="utf-8")
    (bindir / "ansible-playbook").write_text(_FAKE_ANSIBLE_PLAYBOOK, encoding="utf-8")
    (bindir / "ansible").chmod(0o755)
    (bindir / "ansible-playbook").chmod(0o755)
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
    repo = tmp_path / "repo"
    (repo / "scripts" / "lib").mkdir(parents=True)
    (repo / "deploy" / "inventory").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")

    shutil.copy2(DEPLOY_SRC, repo / "scripts" / "deploy")
    shutil.copy2(DEPLOY_STATE_LIB_SRC, repo / "scripts" / "lib" / "deploy-state.sh")
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
    fx: Fixture, fakebin: Path, record: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    env = _test_env(
        PRECIS_DEPLOY_SKIP_CATPATH_WHEEL="1",
        PRECIS_DEPLOY_SKIP_WHEEL_SMOKE="1",
        PRECIS_DEPLOY_NO_LOG="1",
        RENDER_RECORD=str(record),
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

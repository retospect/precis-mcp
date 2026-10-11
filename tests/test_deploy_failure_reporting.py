"""scripts/deploy failure reporting (gr256065) and overlay change detection
(gr239588), driven through the fake-ansible harness of
tests/test_deploy_render_worktree.py.

gr256065: one host failing a prerequisite step used to print "redeploy failed"
plus the mixed-versions banner, though that host never reached the precis
install and still runs the old sha entirely. The banner now appears only when
the precis install task itself failed.

gr239588: edits to the gitignored overlay (deploy/inventory) move no sha and
touch no tracked path, so the deploy saw "nothing changed". The controller now
hashes the overlay, records the hash in the deploy-state marker, and passes
precis_overlay_changed=true when it moves.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.test_deploy_render_worktree import (
    Fixture,
    _git,
    _lib_call,
    _make_fake_bin,
    _run_deploy,
    build_fixture,
)


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return build_fixture(tmp_path)


_RECAP_FAILED = """
PLAY RECAP *********************************************************
gateway                    : ok=10   changed=2    unreachable=0    failed=0
scheduler                  : ok=10   changed=2    unreachable=0    failed=1
"""

_PREREQ_FAILURE = (
    """PLAY [Refresh /opt/precis/venv] ************************************

TASK [autocatpath : Install the CPython headers] *********************
fatal: [scheduler]: FAILED! => {"msg": "apt lock held"}
ok: [gateway]

TASK [precis_worker : Install precis-mcp[paper,pourbaix] into worker venv] ***
changed: [gateway]
"""
    + _RECAP_FAILED
)

_INSTALL_FAILURE = (
    """PLAY [Refresh /opt/precis/venv] ************************************

TASK [precis_worker : Install precis-mcp[paper,pourbaix] into worker venv] ***
changed: [gateway]
fatal: [scheduler]: FAILED! => {"msg": "uv pip install failed"}
"""
    + _RECAP_FAILED
)

_POST_INSTALL_FAILURE = (
    """PLAY [Refresh /opt/precis/venv] ************************************

TASK [precis_worker : Install precis-mcp[paper,pourbaix] into worker venv] ***
changed: [gateway]
changed: [scheduler]

PLAY [Rebuild the sandbox image] ***********************************

TASK [sandbox : podman pull] ***************************************
fatal: [scheduler]: FAILED! => {"msg": "pull failed"}
"""
    + _RECAP_FAILED
)

_IGNORED_FAILURE = (
    """PLAY [x] ******

TASK [probe : optional probe] ******
fatal: [scheduler]: FAILED! => {"msg": "meh"}
...ignoring
"""
    + _RECAP_FAILED
)


def _deploy(
    fx: Fixture, tmp_path: Path, sha: str, output: str | None = None, rc: int = 0
) -> tuple[subprocess.CompletedProcess[str], str]:
    """One deploy of `sha`; returns (result, the fake playbook's record)."""
    fakebin = (
        _make_fake_bin(tmp_path)
        if not (tmp_path / "fakebin").exists()
        else (tmp_path / "fakebin")
    )
    record = tmp_path / f"render-{len(list(tmp_path.glob('render-*.txt')))}.txt"
    extra: dict[str, str] = {}
    if output is not None:
        out = tmp_path / "playbook.out"
        out.write_text(output, encoding="utf-8")
        extra = {"FAKE_PLAYBOOK_OUT": str(out), "FAKE_PLAYBOOK_RC": str(rc)}
    result = _run_deploy(fx, fakebin, record, sha, "--pinned", extra_env=extra)
    rendered = record.read_text(encoding="utf-8") if record.exists() else ""
    return result, rendered


def _failing(fx: Fixture, tmp_path: Path, output: str) -> str:
    fx.set_marker(fx.gated)
    result, _ = _deploy(fx, tmp_path, fx.gated, output, rc=2)
    assert result.returncode != 0, f"{result.stdout}\n{result.stderr}"
    return result.stdout + result.stderr


def test_prereq_failure_names_host_play_task_and_skips_mixed_banner(
    fx: Fixture, tmp_path: Path
) -> None:
    text = _failing(fx, tmp_path, _PREREQ_FAILURE)
    assert "scheduler — prerequisite failed" in text
    assert "play: Refresh /opt/precis/venv" in text
    assert "task: autocatpath : Install the CPython headers" in text
    assert "gateway —" not in text, "a healthy host was listed as failed"
    assert "redeploy failed on scheduler" in text
    assert "may be on mixed versions" not in text, "mixed banner on a prereq failure"


def test_install_failure_keeps_the_mixed_versions_banner(
    fx: Fixture, tmp_path: Path
) -> None:
    text = _failing(fx, tmp_path, _INSTALL_FAILURE)
    assert "scheduler — precis install failed (possibly half-updated)" in text
    assert "redeploy failed on scheduler" in text
    assert "nodes may be on mixed versions" in text


def test_failure_after_install_landed_is_not_mixed(fx: Fixture, tmp_path: Path) -> None:
    text = _failing(fx, tmp_path, _POST_INSTALL_FAILURE)
    assert "scheduler — failed after the precis install landed" in text
    assert "may be on mixed versions" not in text


def test_ignored_failure_is_not_reported(fx: Fixture, tmp_path: Path) -> None:
    text = _failing(fx, tmp_path, _IGNORED_FAILURE)
    assert "failed hosts:" not in text
    # nothing parsed -> the old generic message stays as the fallback
    assert "redeploy failed — see the failing task above" in text


# ── overlay change detection (gr239588) ─────────────────────────────────────

_HASH_RE = re.compile(r" overlay=([0-9a-f]{64})\b")


def _untracked_overlay_sha(fx: Fixture) -> str:
    """Commit that stops tracking deploy/inventory, as in production where the
    overlay is gitignored: the render tree then has no inventory and the
    deploy resolves the canonical overlay in the main checkout."""
    (fx.repo / ".gitignore").write_text("deploy/inventory/\n", encoding="utf-8")
    _git(fx.repo, "rm", "-r", "-q", "--cached", "deploy/inventory")
    _git(fx.repo, "add", ".gitignore")
    _git(fx.repo, "commit", "-q", "-m", "untrack overlay")
    _git(fx.repo, "push", "-q", "origin", "main")
    (fx.repo / "deploy" / ".vault-pass").write_text("pw\n", encoding="utf-8")
    return _git(fx.repo, "rev-parse", "HEAD").stdout.strip()


def _marker(fx: Fixture) -> str:
    return Path(_lib_call("deploy_state_path", str(fx.repo))).read_text(
        encoding="utf-8"
    )


def test_overlay_edit_forces_changed_deploy_and_only_the_hash_is_recorded(
    fx: Fixture, tmp_path: Path
) -> None:
    sha = _untracked_overlay_sha(fx)
    inv = fx.repo / "deploy" / "inventory"
    (inv / "group_vars.yml").write_text("token: s3cr3t-one\n", encoding="utf-8")
    fx.set_marker(sha)  # marker without an overlay hash (pre-gr239588)

    # First deploy: nothing to compare against -> records a hash, forces nothing.
    result, rendered = _deploy(fx, tmp_path, sha)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "precis_overlay_changed" not in rendered
    first = _HASH_RE.search(_marker(fx))
    assert first, f"no overlay hash in marker: {_marker(fx)!r}"

    # Same sha, overlay untouched: still not a change.
    result, rendered = _deploy(fx, tmp_path, sha)
    assert "precis_overlay_changed" not in rendered
    assert "overlay changed" not in result.stdout

    # One overlay value edited at the same sha: counted as a deploy change.
    (inv / "group_vars.yml").write_text("token: s3cr3t-two\n", encoding="utf-8")
    result, rendered = _deploy(fx, tmp_path, sha)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "precis_overlay_changed=true" in rendered
    assert "overlay changed" in result.stdout
    assert "NOT re-rendered" in result.stdout
    second = _HASH_RE.search(_marker(fx))
    assert second and second.group(1) != first.group(1)

    # Neither overlay values nor file names' contents leak: only the hash.
    everything = result.stdout + result.stderr + _marker(fx) + rendered
    assert "s3cr3t" not in everything


def test_failed_deploy_keeps_the_old_overlay_hash(fx: Fixture, tmp_path: Path) -> None:
    """Failure direction: a red deploy must not record the new hash, or the
    retry would see the overlay as already deployed."""
    sha = _untracked_overlay_sha(fx)
    inv = fx.repo / "deploy" / "inventory"
    fx.set_marker(sha)
    result, _ = _deploy(fx, tmp_path, sha)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    before = _marker(fx)
    assert _HASH_RE.search(before)

    (inv / "hosts.yml").write_text("all:\n  vars: {x: 1}\n", encoding="utf-8")
    result, rendered = _deploy(fx, tmp_path, sha, _INSTALL_FAILURE, rc=2)

    assert result.returncode != 0
    assert "precis_overlay_changed=true" in rendered
    assert _marker(fx) == before

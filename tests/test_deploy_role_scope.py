"""scripts/deploy runs only the optional plays a marker→target diff touches.

r34 took ~25-30 min largely re-converging roles whose inputs had not moved
(pythonorg retries, image probes). redeploy-precis.yml's optional imports
carry component tags; the controller diffs the deploy-state marker sha
against the target and passes --skip-tags for components with no touched
input. Fail-safe directions: unmapped deploy paths and unknown inputs run
everything; so do a missing marker, an unreadable diff, an overlay change,
a fleet with lagging hosts, and PRECIS_DEPLOY_ROLE_SCOPE=0.

Same fake-ansible harness as tests/test_deploy_render_worktree.py; the fake
playbook's ARGS line is the assertion surface.
"""

from __future__ import annotations

import sys
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

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the shipped scripts under test are POSIX shell",
)

_ALL_TAGS = "pythonorg,codetask,sandbox,agentimg,tts,asa"


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return build_fixture(tmp_path)


def _commit_files(fx: Fixture, files: dict[str, str]) -> str:
    for name, body in files.items():
        p = fx.repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    _git(fx.repo, "add", "-A")
    _git(fx.repo, "commit", "-q", "-m", "scoped change")
    return _git(fx.repo, "rev-parse", "HEAD").stdout.strip()


def _deploy_scoped(
    fx: Fixture, tmp_path: Path, sha: str, *, extra_env: dict[str, str] | None = None
) -> tuple[str, str]:
    """Deploy `sha` with marker=sibling; return (recorded ARGS, output)."""
    fakebin = _make_fake_bin(tmp_path)
    record = tmp_path / "record.txt"
    env = {"FAKE_CRITICAL": "gateway data"}
    env.update(extra_env or {})
    fx.set_marker(fx.sibling)
    result = _run_deploy(fx, fakebin, record, sha, "--pinned", extra_env=env)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    return record.read_text(encoding="utf-8"), out


def test_precis_code_change_skips_pythonorg_and_codetask_only(
    fx: Fixture, tmp_path: Path
) -> None:
    sha = _commit_files(fx, {"src/precis/sub/x.py": "x = 1\n"})
    args, out = _deploy_scoped(fx, tmp_path, sha)
    assert "role scoping:" in out
    assert "--skip-tags pythonorg,codetask" in args


def test_web_only_change_skips_every_optional_play(fx: Fixture, tmp_path: Path) -> None:
    sha = _commit_files(fx, {"src/precis_web/app.py": "x = 1\n"})
    args, _ = _deploy_scoped(fx, tmp_path, sha)
    assert f"--skip-tags {_ALL_TAGS}" in args


def test_docs_only_change_skips_every_optional_play(
    fx: Fixture, tmp_path: Path
) -> None:
    sha = _commit_files(fx, {"docs/note.md": "x\n"})
    args, _ = _deploy_scoped(fx, tmp_path, sha)
    assert f"--skip-tags {_ALL_TAGS}" in args


def test_unmapped_deploy_path_runs_everything(fx: Fixture, tmp_path: Path) -> None:
    sha = _commit_files(fx, {"deploy/ansible.cfg": "[defaults]\n"})
    args, _ = _deploy_scoped(fx, tmp_path, sha)
    assert "--skip-tags" not in args


def test_unknown_repo_path_runs_everything(fx: Fixture, tmp_path: Path) -> None:
    sha = _commit_files(fx, {"src/strange.md": "x\n"})  # nested *.md: unmapped
    args, _ = _deploy_scoped(fx, tmp_path, sha)
    assert "--skip-tags" not in args


def test_pyproject_moves_pythonorg_plus_the_sha_baked_plays(
    fx: Fixture, tmp_path: Path
) -> None:
    sha = _commit_files(fx, {"pyproject.toml": "[project]\nname = 'x'\n"})
    args, _ = _deploy_scoped(fx, tmp_path, sha)
    assert "--skip-tags codetask" in args


def test_docker_change_runs_the_image_plays(fx: Fixture, tmp_path: Path) -> None:
    sha = _commit_files(fx, {"docker/Dockerfile": "FROM scratch\n"})
    args, _ = _deploy_scoped(fx, tmp_path, sha)
    assert "--skip-tags pythonorg" in args


def test_a_missing_marker_deploys_everything(fx: Fixture, tmp_path: Path) -> None:
    """No marker (no prior deploy on record) -> nothing to diff -> full run.
    `--pinned` itself requires a marker, so this deploys the branch."""
    sha = _commit_files(fx, {"src/precis/x.py": "x = 1\n"})
    _git(fx.repo, "push", "-q", "origin", "main")
    fakebin = _make_fake_bin(tmp_path)
    record = tmp_path / "record.txt"
    result = _run_deploy(
        fx,
        fakebin,
        record,
        "main",
        extra_env={"FAKE_CRITICAL": "gateway data"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--skip-tags" not in record.read_text(encoding="utf-8")


def test_lagging_hosts_force_a_full_run(fx: Fixture, tmp_path: Path) -> None:
    sha = _commit_files(fx, {"src/precis/x.py": "x = 1\n"})
    skipped = Path(_lib_call("deploy_skipped_path", str(fx.repo)))
    skipped.write_text('{"sha": "x", "hosts": {"serving": "y"}}\n', encoding="utf-8")
    args, _ = _deploy_scoped(fx, tmp_path, sha)
    assert "--skip-tags" not in args


def test_role_scope_env_disable_runs_everything(fx: Fixture, tmp_path: Path) -> None:
    sha = _commit_files(fx, {"src/precis/x.py": "x = 1\n"})
    args, _ = _deploy_scoped(
        fx, tmp_path, sha, extra_env={"PRECIS_DEPLOY_ROLE_SCOPE": "0"}
    )
    assert "--skip-tags" not in args


def test_same_sha_redeploy_skips_the_sha_baked_plays_too(
    fx: Fixture, tmp_path: Path
) -> None:
    """Equal shas: nothing moved, so every optional play is skippable."""
    args, _ = _deploy_scoped(fx, tmp_path, fx.sibling)
    assert f"--skip-tags {_ALL_TAGS}" in args

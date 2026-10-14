"""scripts/deploy warns when the deployed range touched roles it never renders.

deploy-renders-only-precis-roles.md option (i): the controller runs only
redeploy-precis.yml, so a merged fix to a role no imported playbook names
(backups, monitoring, pgbouncer, ...) ships without reaching a host — the
b2_sync template fix sat dark for seven weeks. After a deploy, the
marker→target diff is checked for deploy/roles/<role>/ and
deploy/playbooks/<pb> changes and each one the rollout did not render gets
an ADVISORY line naming the numbered playbook to run by hand (a role no
playbook names is reported as `no playbook`, never dropped). The marker
moves even on the convergence-graft path, so the advisory fires inside
_write_deploy_state_marker as well — exactly once per deploy.

Same fake-ansible harness as tests/test_deploy_render_worktree.py.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_deploy_render_worktree import (
    Fixture,
    _git,
    _make_fake_bin,
    _run_deploy,
    build_fixture,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the shipped scripts under test are POSIX shell",
)


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return build_fixture(tmp_path)


# The layout the target sha's render tree must carry: redeploy-precis.yml
# imports two playbooks covering pythonorg + precis_web; 11-backups and
# 31-asa-bot exist but are never imported; asa_common renders only through
# asa_bot's meta dependency.
_LAYOUT = {
    "deploy/redeploy-precis.yml": (
        "---\n"
        "- ansible.builtin.import_playbook: playbooks/06-pythonorg.yml\n"
        "- ansible.builtin.import_playbook: playbooks/38-precis-web.yml\n"
    ),
    "deploy/playbooks/06-pythonorg.yml": (
        "---\n- hosts: all\n  roles:\n    - pythonorg\n"
    ),
    "deploy/playbooks/38-precis-web.yml": (
        "---\n- hosts: gateway\n  roles:\n    - role: precis_web\n"
    ),
    "deploy/playbooks/11-backups.yml": "---\n- hosts: data\n  roles:\n    - backups\n",
    "deploy/playbooks/31-asa-bot.yml": "---\n- hosts: gateway\n  roles:\n    - asa_bot\n",
    "deploy/roles/asa_bot/meta/main.yml": (
        "---\ndependencies:\n  - role: asa_common\n"
    ),
    "deploy/roles/pythonorg/tasks/main.yml": "---\n[]\n",
    "deploy/roles/precis_web/tasks/main.yml": "---\n[]\n",
    "deploy/roles/backups/tasks/main.yml": "---\n[]\n",
    "deploy/roles/asa_common/defaults/main.yml": "---\n{}\n",
}


def _commit_files(fx: Fixture, files: dict[str, str]) -> str:
    for name, body in files.items():
        p = fx.repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    _git(fx.repo, "add", "-A")
    _git(fx.repo, "commit", "-q", "-m", "advisory fixture")
    return _git(fx.repo, "rev-parse", "HEAD").stdout.strip()


def _deploy(
    fx: Fixture,
    tmp_path: Path,
    sha: str,
    *args: str,
    output: str | None = None,
    rc: int = 0,
) -> subprocess.CompletedProcess[str]:
    fakebin = (
        tmp_path / "fakebin"
        if (tmp_path / "fakebin").exists()
        else _make_fake_bin(tmp_path)
    )
    env = {"FAKE_CRITICAL": "gateway data"}
    if output is not None:
        out = tmp_path / "playbook.out"
        out.write_text(output, encoding="utf-8")
        env["FAKE_PLAYBOOK_OUT"] = str(out)
        env["FAKE_PLAYBOOK_RC"] = str(rc)
    return _run_deploy(fx, fakebin, tmp_path / "record.txt", sha, *args, extra_env=env)


def _layout_at_marker(fx: Fixture) -> str:
    """Commit the deploy-tree layout and pin the marker to it; returns the
    sha a change commit can diff against."""
    sha = _commit_files(fx, _LAYOUT)
    fx.set_marker(sha)
    return sha


_MIDRUN_DROP = """TASK [precis_worker : Install precis-mcp into worker venv] ********
changed: [gateway]
fatal: [scheduler]: UNREACHABLE! => {"changed": false, "unreachable": true}
PLAY RECAP *********************************************************
gateway                    : ok=10   changed=2    unreachable=0    failed=0
scheduler                  : ok=3    changed=0    unreachable=1    failed=0
data                       : ok=8    changed=1    unreachable=0    failed=0
inference                  : ok=10   changed=2    unreachable=0    failed=0
serving                    : ok=5    changed=1    unreachable=0    failed=0
"""


def test_an_unrendered_role_names_its_playbook(fx: Fixture, tmp_path: Path) -> None:
    _layout_at_marker(fx)
    sha = _commit_files(
        fx,
        {
            "deploy/roles/backups/templates/b2_sync.sh.j2": "#!/bin/sh\n",
            "deploy/roles/backups/files/extra": "x\n",
        },
    )
    result = _deploy(fx, tmp_path, sha, "--pinned")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert (
        "ADVISORY: deploy/roles/backups changed since" in out
        and "ansible-playbook deploy/playbooks/11-backups.yml" in out
    ), out
    # two files under one role still produce a single advisory
    assert out.count("deploy/roles/backups changed since") == 1, out


def test_covered_roles_and_imported_playbooks_stay_quiet(
    fx: Fixture, tmp_path: Path
) -> None:
    _layout_at_marker(fx)
    sha = _commit_files(
        fx,
        {
            "deploy/roles/pythonorg/tasks/main.yml": "---\n- debug: msg=x\n",
            "deploy/roles/precis_web/tasks/main.yml": "---\n- debug: msg=x\n",
            "deploy/playbooks/06-pythonorg.yml": (
                "---\n- hosts: all\n  roles:\n    - pythonorg\n    - logrotate\n"
            ),
        },
    )
    result = _deploy(fx, tmp_path, sha, "--pinned")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "ADVISORY" not in out, out


def test_an_orphan_role_is_reported_as_no_playbook(fx: Fixture, tmp_path: Path) -> None:
    _layout_at_marker(fx)
    sha = _commit_files(fx, {"deploy/roles/zzz_orphan/tasks/main.yml": "---\n[]\n"})
    result = _deploy(fx, tmp_path, sha, "--pinned")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "ADVISORY: deploy/roles/zzz_orphan changed since" in out, out
    assert "no playbook under deploy/playbooks/ names it" in out, out


def test_a_meta_dependency_maps_to_the_parents_playbook(
    fx: Fixture, tmp_path: Path
) -> None:
    _layout_at_marker(fx)
    sha = _commit_files(
        fx, {"deploy/roles/asa_common/defaults/main.yml": "---\nx: 1\n"}
    )
    result = _deploy(fx, tmp_path, sha, "--pinned")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "ADVISORY: deploy/roles/asa_common changed since" in out, out
    assert "ansible-playbook deploy/playbooks/31-asa-bot.yml" in out, out


def test_a_changed_unimported_playbook_is_named(fx: Fixture, tmp_path: Path) -> None:
    _layout_at_marker(fx)
    sha = _commit_files(
        fx,
        {
            "deploy/playbooks/08-monitoring.yml": (
                "---\n- hosts: all\n  roles:\n    - monitoring\n"
            )
        },
    )
    result = _deploy(fx, tmp_path, sha, "--pinned")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "ADVISORY: deploy/playbooks/08-monitoring.yml changed since" in out, out
    assert "ansible-playbook deploy/playbooks/08-monitoring.yml" in out, out


def test_group_vars_moves_get_a_single_advisory(fx: Fixture, tmp_path: Path) -> None:
    _layout_at_marker(fx)
    sha = _commit_files(fx, {"deploy/playbooks/group_vars/all.yml": "x: 1\n"})
    result = _deploy(fx, tmp_path, sha, "--pinned")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "ADVISORY: deploy/playbooks group_vars/files changed since" in out, out


def test_no_marker_or_same_sha_means_no_advisory(fx: Fixture, tmp_path: Path) -> None:
    sha = _commit_files(fx, _LAYOUT)
    # no marker at all: nothing to diff against, and no marker means the
    # rollback guard is fine with a plain main deploy
    result = _run_deploy(
        fx,
        _make_fake_bin(tmp_path),
        tmp_path / "record.txt",
        "main",
        extra_env={"FAKE_CRITICAL": "gateway data"},
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "ADVISORY" not in out, out
    # same-sha redeploy: empty range
    fx.set_marker(sha)
    result = _deploy(fx, tmp_path, sha, "--pinned")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "ADVISORY" not in out, out


def test_the_advisory_fires_once_on_the_midrun_graft_path(
    fx: Fixture, tmp_path: Path
) -> None:
    _layout_at_marker(fx)
    sha = _commit_files(
        fx, {"deploy/roles/backups/templates/b2_sync.sh.j2": "#!/bin/sh\n"}
    )
    result = _deploy(fx, tmp_path, sha, "--pinned", output=_MIDRUN_DROP, rc=2)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "ADVISORY: deploy/roles/backups changed since" in out, out
    # printed inside _write_deploy_state_marker; the end-of-script call is
    # suppressed by the once-per-deploy flag
    assert out.count("deploy/roles/backups changed since") == 1, out

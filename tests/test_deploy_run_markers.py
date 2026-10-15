"""scripts/deploy moves origin/prod only when the migration ran and the run finished.

r36 (2026-10-15): play 43 failed on both compute hosts, so ansible ended the
whole playbook before play 38 ran `precis migrate`. Every host the run never
reached still read failed=0 in the PLAY RECAP, the two compute hosts were
auto-skipped as non-critical, and scripts/deploy moved origin/prod over a DB
two migrations behind the code. redeploy-precis.yml now prints
`PRECIS_DEPLOY_MIGRATED <sha>` from the migrate-first play and
`PRECIS_DEPLOY_COMPLETE <sha>` from its last play; scripts/deploy requires both
before either success writer runs (_deploy_markers_ok).

Same fake-ansible harness as tests/test_deploy_skip_hosts.py, plus the real
scripts/lib/env-pointers.sh so the origin/prod push itself is observed. The
target commit's deploy/redeploy-precis.yml mentions the markers, which is what
switches the requirement on (an older tree without them keeps the recap-only
behaviour, covered by the existing deploy tests).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_deploy_render_worktree import (
    REPO_ROOT,
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

ENV_POINTERS_LIB_SRC = REPO_ROOT / "scripts" / "lib" / "env-pointers.sh"


class MarkerFixture:
    def __init__(self, fx: Fixture, prev: str, target: str) -> None:
        self.fx = fx
        self.prev = prev
        self.target = target


@pytest.fixture
def mfx(tmp_path: Path) -> MarkerFixture:
    """`prev` is what the fleet runs (marker + origin/prod); `target` is a
    descendant whose redeploy-precis.yml declares the run markers."""
    fx = build_fixture(tmp_path)
    shutil.copy2(ENV_POINTERS_LIB_SRC, fx.repo / "scripts" / "lib" / "env-pointers.sh")
    (fx.repo / "deploy" / "redeploy-precis.yml").write_text(
        "---\n# prints PRECIS_DEPLOY_MIGRATED and PRECIS_DEPLOY_COMPLETE\n",
        encoding="utf-8",
    )
    _git(fx.repo, "add", "-A")
    _git(fx.repo, "commit", "-q", "-m", "declare run markers")
    target = _git(fx.repo, "rev-parse", "HEAD").stdout.strip()
    _git(fx.repo, "push", "-q", "origin", "main")
    prev = fx.sibling
    _git(fx.repo, "push", "-q", "origin", f"{prev}:refs/heads/prod")
    fx.set_marker(prev)
    return MarkerFixture(fx, prev, target)


def _deploy(
    mfx: MarkerFixture, tmp_path: Path, output: str, rc: int
) -> subprocess.CompletedProcess[str]:
    fakebin = (
        _make_fake_bin(tmp_path)
        if not (tmp_path / "fakebin").exists()
        else (tmp_path / "fakebin")
    )
    out = tmp_path / "playbook.out"
    out.write_text(output, encoding="utf-8")
    env = {
        "FAKE_PING_DOWN": "",
        "FAKE_CRITICAL": "gateway data",
        "FAKE_PLAYBOOK_OUT": str(out),
        "FAKE_PLAYBOOK_RC": str(rc),
        "PRECIS_ENV_POINTERS": "1",
        "PRECIS_PROD_CHECKOUT": str(tmp_path / "no-prod-clone"),
    }
    return _run_deploy(
        mfx.fx, fakebin, tmp_path / "record.txt", mfx.target, "--pinned", extra_env=env
    )


def _origin_prod(mfx: MarkerFixture) -> str:
    return _git(mfx.fx.repo, "ls-remote", "origin", "refs/heads/prod").stdout.split()[0]


def _marker_sha(mfx: MarkerFixture) -> str:
    path = Path(_lib_call("deploy_state_path", str(mfx.fx.repo)))
    return path.read_text(encoding="utf-8").split()[0]


def _skipped(mfx: MarkerFixture) -> dict[str, str] | None:
    path = Path(_lib_call("deploy_skipped_path", str(mfx.fx.repo)))
    if not path.exists():
        return None
    hosts: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))["hosts"]
    return hosts


def _recap(**bad: str) -> str:
    """A PLAY RECAP; `bad` maps a host to its replacement `unreachable=… failed=…`."""
    lines = ["PLAY RECAP *********************************************************"]
    for host in ("gateway", "scheduler", "data", "inference", "serving"):
        counts = bad.get(host, "unreachable=0    failed=0")
        lines.append(f"{host:<26} : ok=10   changed=2    {counts}    skipped=1")
    return "\n".join(lines) + "\n"


def _migrated(sha: str) -> str:
    return (
        "TASK [Report the migrate-first gate (scripts/deploy reads this line)] ***\n"
        f'ok: [gateway] => {{\n    "msg": "PRECIS_DEPLOY_MIGRATED {sha}"\n}}\n'
    )


def _complete(sha: str) -> str:
    return (
        "TASK [Report the run reached its last play (scripts/deploy reads this line)] ***\n"
        f'ok: [gateway] => {{\n    "msg": "PRECIS_DEPLOY_COMPLETE {sha}"\n}}\n'
    )


def test_full_success_moves_prod_and_the_marker(
    mfx: MarkerFixture, tmp_path: Path
) -> None:
    output = _migrated(mfx.target) + _complete(mfx.target) + _recap()
    result = _deploy(mfx, tmp_path, output, rc=0)

    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert _origin_prod(mfx) == mfx.target
    assert _marker_sha(mfx) == mfx.target
    assert _skipped(mfx) is None


def test_partial_failure_moves_prod_and_records_the_hosts_behind(
    mfx: MarkerFixture, tmp_path: Path
) -> None:
    """Per-host isolation: a lane host failed its build assert and another was
    unreachable, the gateway anchor kept the run going, and the gateway
    reached the last play. Every required play passed, so prod moves; the two
    hosts are recorded with the sha they still run, for
    `scripts/round deploy --catch-up`."""
    output = (
        _migrated(mfx.target)
        + 'fatal: [inference]: FAILED! => {"msg": "assert-unit-build: stale"}\n'
        + 'fatal: [serving]: UNREACHABLE! => {"unreachable": true}\n'
        + _complete(mfx.target)
        + _recap(
            inference="unreachable=0    failed=1",
            serving="unreachable=1    failed=0",
        )
    )
    result = _deploy(mfx, tmp_path, output, rc=4)

    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "WARNING" in out and "catch-up" in out
    assert _origin_prod(mfx) == mfx.target
    assert _marker_sha(mfx) == mfx.target
    assert _skipped(mfx) == {"inference": mfx.prev, "serving": mfx.prev}


def test_a_run_that_stopped_early_moves_nothing(
    mfx: MarkerFixture, tmp_path: Path
) -> None:
    """The r36 transcript: the migrate-first play passed, then the compute
    play failed on every one of its hosts and ansible ended the playbook. The
    gateway and data rows read clean because the run never reached them
    again. No COMPLETE line → no prod move, no marker, no skipped record."""
    output = (
        _migrated(mfx.target)
        + 'fatal: [inference]: FAILED! => {"msg": "assert-unit-build: stale"}\n'
        + 'fatal: [serving]: UNREACHABLE! => {"unreachable": true}\n'
        + "NO MORE HOSTS LEFT *************************************************\n"
        + _recap(
            inference="unreachable=0    failed=1",
            serving="unreachable=1    failed=0",
        )
    )
    result = _deploy(mfx, tmp_path, output, rc=4)

    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "PRECIS_DEPLOY_COMPLETE" in out and "NOT moved" in out
    assert _origin_prod(mfx) == mfx.prev
    assert _marker_sha(mfx) == mfx.prev
    assert _skipped(mfx) is None


def test_a_failed_migration_moves_nothing(mfx: MarkerFixture, tmp_path: Path) -> None:
    output = (
        'fatal: [gateway]: FAILED! => {"msg": "migrate: 0210 failed"}\n'
        + "NO MORE HOSTS LEFT *************************************************\n"
        + _recap(gateway="unreachable=0    failed=1")
    )
    result = _deploy(mfx, tmp_path, output, rc=2)

    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "DB migration did not pass" in out
    assert _origin_prod(mfx) == mfx.prev
    assert _marker_sha(mfx) == mfx.prev
    assert _skipped(mfx) is None


def test_a_green_exit_without_the_completion_line_moves_nothing(
    mfx: MarkerFixture, tmp_path: Path
) -> None:
    """ansible's exit status is not the evidence; the markers are."""
    result = _deploy(mfx, tmp_path, _migrated(mfx.target) + _recap(), rc=0)

    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert _origin_prod(mfx) == mfx.prev
    assert _marker_sha(mfx) == mfx.prev


def test_an_unrendered_marker_template_does_not_count(
    mfx: MarkerFixture, tmp_path: Path
) -> None:
    """A raw, unrendered msg (an error message echoing the task) is not the
    marker, even when everything else reads green."""
    output = (
        _migrated(mfx.target)
        + "[WARNING]: could not render 'PRECIS_DEPLOY_COMPLETE {{ precis_target_sha }}'\n"
        + _recap()
    )
    result = _deploy(mfx, tmp_path, output, rc=0)

    assert result.returncode != 0, result.stdout + result.stderr
    assert _origin_prod(mfx) == mfx.prev

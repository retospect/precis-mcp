"""scripts/deploy auto-skips a down non-critical host, never a critical one.

r33 (2026-10-14): one host dropping off Tailscale aborted the whole deploy
twice and the verdict-age guard then refused the hours-later resume. The
reachability ping now partitions its failures: hosts outside the
serving-critical set (the `data` + `gateway` inventory groups — DB and
web/MCP) are skipped with a WARNING and recorded in the shared
`precis-deploy-skipped` file with the sha they still run; a critical host
down still aborts the run. A non-critical host that drops DURING the
playbook is recorded the same way and the deploy completes.

Same fake-ansible harness as tests/test_deploy_render_worktree.py: FAKE_PING_DOWN
lists hosts that answer UNREACHABLE, FAKE_CRITICAL answers the
`data:gateway --list-hosts` probe, and FAKE_PLAYBOOK_OUT feeds a canned
playbook transcript.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_deploy_render_worktree import (
    Fixture,
    _lib_call,
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


_CRITICAL = "gateway data"


def _skipped_file(fx: Fixture) -> Path:
    return Path(_lib_call("deploy_skipped_path", str(fx.repo)))


def _skipped(fx: Fixture) -> dict[str, object]:
    path = _skipped_file(fx)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["hosts"]


def _deploy(
    fx: Fixture,
    tmp_path: Path,
    sha: str,
    *,
    down: str = "",
    critical: str = _CRITICAL,
    output: str | None = None,
    rc: int = 0,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    fakebin = (
        _make_fake_bin(tmp_path)
        if not (tmp_path / "fakebin").exists()
        else (tmp_path / "fakebin")
    )
    env = {"FAKE_PING_DOWN": down, "FAKE_CRITICAL": critical}
    if output is not None:
        out = tmp_path / "playbook.out"
        out.write_text(output, encoding="utf-8")
        env["FAKE_PLAYBOOK_OUT"] = str(out)
        env["FAKE_PLAYBOOK_RC"] = str(rc)
    env.update(extra_env or {})
    return _run_deploy(
        fx, fakebin, tmp_path / "record.txt", sha, "--pinned", extra_env=env
    )


_RECAP_ALL_GREEN = """PLAY RECAP *********************************************************
gateway                    : ok=10   changed=2    unreachable=0    failed=0
scheduler                  : ok=10   changed=2    unreachable=0    failed=0
data                       : ok=8    changed=1    unreachable=0    failed=0
inference                  : ok=10   changed=2    unreachable=0    failed=0
serving                    : ok=5    changed=1    unreachable=0    failed=0
"""

_MIDRUN_DROP = """TASK [precis_worker : Install precis-mcp into worker venv] ********
changed: [gateway]
fatal: [scheduler]: UNREACHABLE! => {"changed": false, "unreachable": true}
""" + _RECAP_ALL_GREEN.replace(
    "scheduler                  : ok=10   changed=2    unreachable=0    failed=0",
    "scheduler                  : ok=3    changed=0    unreachable=1    failed=0",
)


_RECAP_DATA_DOWN = _RECAP_ALL_GREEN.replace(
    "data                       : ok=8    changed=1    unreachable=0    failed=0",
    "data                       : ok=3    changed=0    unreachable=1    failed=0",
)


def test_a_down_noncritical_host_is_skipped_and_recorded(
    fx: Fixture, tmp_path: Path
) -> None:
    """The deploy proceeds, limits the host out of the rollout, warns loudly,
    and records it with the sha it still runs (the pre-deploy marker)."""
    fx.set_marker(fx.sibling)  # marker: fleet ran `sibling`; target is gated
    result = _deploy(fx, tmp_path, fx.sibling, down="serving scheduler")

    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "WARNING: skipping" in out and "serving" in out
    skipped = _skipped(fx)
    assert skipped == {"serving": fx.sibling, "scheduler": fx.sibling}, skipped
    # the rollout was limited away from the skipped hosts
    record = (tmp_path / "record.txt").read_text(encoding="utf-8")
    assert (
        "--limit all:!serving:!scheduler" in record
        or "--limit all:!scheduler:!serving" in record
    )


def test_a_down_critical_host_still_aborts(fx: Fixture, tmp_path: Path) -> None:
    fx.set_marker(fx.sibling)
    for host in ("gateway", "data"):
        result = _deploy(fx, tmp_path, fx.sibling, down=host)
        out = result.stdout + result.stderr
        assert result.returncode != 0, out
        assert "serving-critical" in out and host in out
        assert _skipped(fx) == {}


def test_unresolvable_critical_set_disables_auto_skip(
    fx: Fixture, tmp_path: Path
) -> None:
    """No data/gateway groups -> every host counts as critical -> abort,
    the pre-change behaviour (fail-safe)."""
    fx.set_marker(fx.sibling)
    result = _deploy(fx, tmp_path, fx.sibling, down="serving", critical="")
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "serving-critical" in out


def test_manual_skip_hosts_still_works_and_is_recorded(
    fx: Fixture, tmp_path: Path
) -> None:
    fx.set_marker(fx.sibling)
    result = _deploy(
        fx, tmp_path, fx.sibling, extra_env={"PRECIS_DEPLOY_SKIP_HOSTS": "serving"}
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "PRECIS_DEPLOY_SKIP_HOSTS" in out
    assert _skipped(fx) == {"serving": fx.sibling}
    record = (tmp_path / "record.txt").read_text(encoding="utf-8")
    assert ":!serving" in record


def test_a_clean_deploy_clears_the_skipped_record(fx: Fixture, tmp_path: Path) -> None:
    fx.set_marker(fx.sibling)
    assert _deploy(fx, tmp_path, fx.sibling, down="serving").returncode == 0
    assert _skipped(fx) == {"serving": fx.sibling}
    result = _deploy(fx, tmp_path, fx.sibling, down="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not _skipped_file(fx).exists()


def test_a_noncritical_host_dropping_mid_run_is_skipped_not_failed(
    fx: Fixture, tmp_path: Path
) -> None:
    fx.set_marker(fx.sibling)
    result = _deploy(fx, tmp_path, fx.sibling, output=_MIDRUN_DROP, rc=2)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "WARNING" in out and "scheduler" in out
    assert _skipped(fx) == {"scheduler": fx.sibling}


def test_a_critical_host_dropping_mid_run_still_fails(
    fx: Fixture, tmp_path: Path
) -> None:
    fx.set_marker(fx.sibling)
    result = _deploy(fx, tmp_path, fx.sibling, output=_RECAP_DATA_DOWN, rc=2)
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert _skipped(fx) == {}

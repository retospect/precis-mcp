"""Deploy-lint for the dedicated slice poll lane (gr548810).

Playbook 20g renders `com.precis.worker-slices` (`precis worker --only
job_ssh_node --only plan_tick`) on the gateway so detached run_slice / land_slice jobs are
polled in seconds instead of once per 40-50 min collapsed-worker lap. A
second claimant must carry env parity with 20b for the job types it can win;
these tests pin that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
_20G = _DEPLOY / "playbooks" / "20g-precis-worker-slices.yml"
_20E = "playbooks/20e-precis-worker-agentlane.yml"
_20G_REF = "playbooks/20g-precis-worker-slices.yml"


def _play() -> dict[str, Any]:
    plays = yaml.safe_load(_20G.read_text(encoding="utf-8"))
    assert isinstance(plays, list) and len(plays) == 1
    return plays[0]


def _unit_vars() -> dict[str, Any]:
    tasks = [t for t in _play()["tasks"] if "ansible.builtin.import_role" in t]
    assert len(tasks) == 1
    assert tasks[0]["ansible.builtin.import_role"]["name"] == "service_unit"
    return tasks[0]["vars"]


def test_targets_gateway_with_keepalive_unit() -> None:
    assert _play()["hosts"] == "gateway"
    spec = _unit_vars()
    assert spec["service_unit_name"] == "worker-slices"
    assert spec["service_unit_run_as"] == "deploy"
    assert spec["service_unit_schedule"]["mode"] == "keepalive"
    assert spec["service_unit_timeout_stop_sec"] == 60


def test_exec_is_only_job_ssh_node_and_plan_tick() -> None:
    exec_line = str(_unit_vars()["service_unit_exec"])
    assert exec_line.count("--only") == 2
    assert "--only job_ssh_node" in exec_line
    assert "--only plan_tick" in exec_line
    assert "--batch-size 1" in exec_line
    assert "precis_worker_slices_idle_seconds | default(5)" in exec_line
    assert "--profile" not in exec_line


def test_env_has_parity_keys_and_own_identity() -> None:
    env = str(_unit_vars()["service_unit_env"])
    for key in (
        "PRECIS_DATABASE_URL",
        "PGPASSFILE",
        "HOME",
        "PATH",
        "PRECIS_MAX_TICKS",
        "PRECIS_MAX_TODO_USD",
        "PRECIS_MAX_TREE_USD",
        "PRECIS_DAILY_COST_CEILING",
        "PRECIS_SUSPENDED_JOB_TYPES",
        "PRECIS_EMBEDDER",
        "PRECIS_EMBEDDER_URL",
        "PRECIS_DFT_NODE",
        "PRECIS_CLAUDE_BIN",
        # The checkout run_slice cuts worktrees under; a venv install has none
        # around its code. Same value as 20b's _l_b_slice_env (gripe 570787).
        "PRECIS_SLICE_ROOT",
        # The shared MCP's per-slice grant registry; rendered from the host
        # var precis_mcp_slice_token_dir when set, as 20b's
        # _l_b_slice_token_env (gripe 571617).
        "PRECIS_MCP_SLICE_TOKEN_DIR",
        "precis_shared_env",
        "precis_identity_env",
    ):
        assert key in env, key
    assert env.count("'PRECIS_PROCESS': 'precis-worker-slices'") == 1
    assert env.count("'PRECIS_SOURCE': 'precis-worker-slices'") == 1


def test_env_carries_what_the_plan_tick_reads() -> None:
    """The tick reads PRECIS_FACTORY (kill switch) and
    PRECIS_PLAN_TICK_INTERVAL_S from the unit env. Neither is a literal key:
    both ride ``precis_shared_env`` (the source the catch-all plist renders),
    which the lane combines in, so the operator sets them in one place."""
    env = str(_unit_vars()["service_unit_env"])
    assert "precis_shared_env" in env
    # The lane does not hard-code (and so cannot shadow) either knob.
    assert "PRECIS_FACTORY'" not in env
    assert "PRECIS_PLAN_TICK_INTERVAL_S'" not in env
    header = _20G.read_text(encoding="utf-8").split("\n- name:")[0]
    assert "PRECIS_FACTORY" in header
    assert "PRECIS_PLAN_TICK_INTERVAL_S" in header
    assert "td543894" in header


def test_site_bounce_task_skipped_under_redeploy() -> None:
    task = next(t for t in _play()["tasks"] if "command" in str(t.keys()))
    assert "com.precis.worker-slices" in str(task)
    assert task["when"] == "precis_deploy_changed is not defined"


def _import_order(path: Path) -> list[str]:
    plays = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        p["ansible.builtin.import_playbook"]
        for p in plays
        if "ansible.builtin.import_playbook" in p
    ]


def test_imported_by_site_and_redeploy_right_after_20e() -> None:
    for name in ("site.yml", "redeploy-precis.yml"):
        order = _import_order(_DEPLOY / name)
        assert order.count(_20G_REF) == 1, name
        assert order.index(_20G_REF) == order.index(_20E) + 1, name

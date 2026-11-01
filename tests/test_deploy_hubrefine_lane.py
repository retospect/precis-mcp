"""Deploy-lint for the dedicated hub_refine lane and the catch-all handover.

Playbook 20h renders `com.precis.worker-hubrefine` (`precis worker --only
hub_refine`) on the gateway so the ~11 min hub_refine pass stops taking ~27%
of the collapsed worker's 40-50 min lap. 20b stops claiming hub_refine and
job_claude_inproc (`--skip`), so 20h and 20e become the sole claimants and
must carry the env those passes read; these tests pin that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
_20H = _DEPLOY / "playbooks" / "20h-precis-worker-hubrefine.yml"
_20B = _DEPLOY / "playbooks" / "20b-precis-worker-collapsed.yml"
_20E = _DEPLOY / "playbooks" / "20e-precis-worker-agentlane.yml"
_20G_REF = "playbooks/20g-precis-worker-slices.yml"
_20H_REF = "playbooks/20h-precis-worker-hubrefine.yml"


def _play() -> dict[str, Any]:
    plays = yaml.safe_load(_20H.read_text(encoding="utf-8"))
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
    assert spec["service_unit_name"] == "worker-hubrefine"
    assert spec["service_unit_run_as"] == "deploy"
    assert spec["service_unit_schedule"]["mode"] == "keepalive"
    assert spec["service_unit_timeout_stop_sec"] == 60


def test_exec_is_only_hub_refine_once() -> None:
    exec_line = str(_unit_vars()["service_unit_exec"])
    assert exec_line.count("--only") == 1
    assert "--only hub_refine" in exec_line
    assert "precis_worker_hubrefine_idle_seconds | default(30)" in exec_line
    assert "--profile" not in exec_line
    assert "--skip" not in exec_line


def test_env_has_what_the_pass_reads_and_own_identity() -> None:
    env = str(_unit_vars()["service_unit_env"])
    for key in (
        "PRECIS_DATABASE_URL",
        "PGPASSFILE",
        "HOME",
        "PATH",
        "PRECIS_DAILY_COST_CEILING",
        "PRECIS_EMBEDDER",
        "PRECIS_EMBEDDER_URL",
        "PRECIS_CLAUDE_BIN",
        "precis_shared_env",
        "precis_identity_env",
    ):
        assert key in env, key
    assert env.count("'PRECIS_PROCESS': 'precis-worker-hubrefine'") == 1
    assert env.count("'PRECIS_SOURCE': 'precis-worker-hubrefine'") == 1


def test_site_bounce_task_skipped_under_redeploy() -> None:
    task = next(t for t in _play()["tasks"] if "command" in str(t.keys()))
    assert "com.precis.worker-hubrefine" in str(task)
    assert task["when"] == "precis_deploy_changed is not defined"


def _import_order(path: Path) -> list[str]:
    plays = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        p["ansible.builtin.import_playbook"]
        for p in plays
        if "ansible.builtin.import_playbook" in p
    ]


def test_imported_by_site_and_redeploy_right_after_20g() -> None:
    for name in ("site.yml", "redeploy-precis.yml"):
        order = _import_order(_DEPLOY / name)
        assert order.count(_20H_REF) == 1, name
        assert order.index(_20H_REF) == order.index(_20G_REF) + 1, name


def test_catchall_skips_both_dedicated_passes_on_the_gateway_only() -> None:
    plays = yaml.safe_load(_20B.read_text(encoding="utf-8"))
    play_vars = plays[0]["vars"]
    flags = str(play_vars["_l_b_skip_flags"])
    assert "--skip hub_refine" in flags
    assert "--skip job_claude_inproc" in flags
    assert "groups['gateway']" in flags
    task = next(
        t
        for t in plays[0]["tasks"]
        if "ansible.builtin.import_role" in t
        and t.get("vars", {}).get("service_unit_name") == "worker"
    )
    exec_line = str(task["vars"]["service_unit_exec"])
    assert "{{ _l_b_skip_flags }}" in exec_line
    assert "--profile all" in exec_line


def test_agent_lane_is_sole_claude_inproc_claimant_with_the_env_it_needs() -> None:
    plays = yaml.safe_load(_20E.read_text(encoding="utf-8"))
    task = next(
        t
        for t in plays[0]["tasks"]
        if "ansible.builtin.import_role" in t
        and t["vars"].get("service_unit_name") == "worker-agentlane"
    )
    env = str(task["vars"]["service_unit_env"])
    assert (
        "'PRECIS_INPROC_CONCURRENCY': "
        "(precis_worker_agentlane_inproc_concurrency | default(3)) | string"
    ) in env
    # card_forge reads it in the executor process; 20b's gateway extras
    # carried it ('act'), so the lane must too or the job drops to report mode.
    assert "'PRECIS_CARD_FORGE_AUTONOMY': precis_card_forge_autonomy" in env
    assert "PRECIS_LOCAL_SERVE_CONFIG" in env

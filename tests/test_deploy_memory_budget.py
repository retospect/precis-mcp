"""Per-host memory budget for the GB10 lane layout (host-lanes, 2026-10-15).

castor wedged twice on 2026-10-15: vLLM at 0.70 plus a 56G compute lane, a
24G sandbox lane and a 24G worker summed far past its 121 GiB. The hardening
role now derives every lane cap from the inventory group that places the lane
and renders one plan (templates/memory-budget.json.j2) that both writes the
drop-ins and asserts the budget. These tests render that same template for
every combination of lane-placing groups, using the role defaults, so a cap or
LLM-fraction change that would overcommit some host fails here.
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from typing import Any

import jinja2
import pytest
import yaml

_DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
_ROLES = _DEPLOY / "roles"
_HARDENING = _ROLES / "hardening"
_SANDBOX_EXECUTOR = (
    _DEPLOY.parent / "src" / "precis" / "workers" / "executors" / "claude_docker.py"
)

# Every inventory group that places a lane or an LLM on a Linux host.
_LANE_GROUPS = (
    "serving",
    "compute",
    "agent_sandbox_hosts",
    "inference",
    "vllm_serving",
    "sglang_serving",
)


def _yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def _defaults() -> dict[str, Any]:
    return _yaml(_HARDENING / "defaults" / "main.yml")


def _llm_lanes() -> list[dict[str, Any]]:
    """host_llm_lanes resolved the way memory_budget.yml resolves them."""
    return [
        {
            "group": "vllm_serving",
            "fraction": _yaml(_ROLES / "vllm" / "defaults" / "main.yml")[
                "vllm_gpu_memory_utilization"
            ],
        },
        {
            "group": "sglang_serving",
            "fraction": _yaml(_ROLES / "sglang" / "defaults" / "main.yml")[
                "sglang_mem_fraction_static"
            ],
        },
    ]


def _env() -> jinja2.Environment:
    env = jinja2.Environment(undefined=jinja2.Undefined)
    env.filters["regex_replace"] = lambda s, pat, rep="": re.sub(pat, rep, str(s))
    env.filters["intersect"] = lambda a, b: [x for x in a if x in b]
    return env


def _plan(group_names: list[str], **overrides: Any) -> dict[str, Any]:
    ctx = {k: v for k, v in _defaults().items() if k != "host_llm_lanes"}
    ctx["host_llm_lanes"] = _llm_lanes()
    ctx["group_names"] = group_names
    ctx.update(overrides)
    src = (_HARDENING / "templates" / "memory-budget.json.j2").read_text(
        encoding="utf-8"
    )
    plan: dict[str, Any] = json.loads(_env().from_string(src).render(**ctx))
    return plan


def _gib(size: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kmgt]?)", size.strip().lower())
    assert m, size
    scale = {"k": 1 / 1024**2, "m": 1 / 1024, "g": 1.0, "t": 1024.0, "": 1 / 1024**3}
    return float(m.group(1)) * scale[m.group(2)]


def _combos() -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    for n in range(len(_LANE_GROUPS) + 1):
        out.extend(itertools.combinations(_LANE_GROUPS, n))
    return out


def _excluded(groups: tuple[str, ...]) -> bool:
    pairs = _defaults()["host_lane_exclusive_groups"]
    return any(set(p) <= set(groups) for p in pairs)


@pytest.mark.parametrize("groups", _combos(), ids=lambda g: "+".join(g) or "none")
def test_every_allowed_lane_combination_fits(groups: tuple[str, ...]) -> None:
    plan = _plan(list(groups))
    if _excluded(groups):
        assert plan["exclusive"], plan
        return
    assert plan["exclusive"] == []
    if "inference" in groups:
        # 20b's collapsed worker carries no lane cap: uncapped by default, so
        # an inference host is refused until precis_worker_memory_max is set.
        assert plan["uncapped"] == ["precis-worker.service"], plan
        return
    assert plan["uncapped"] == []
    assert plan["total_mib"] <= plan["limit_mib"], plan["items"]


def test_castor_2026_10_15_layout_is_refused() -> None:
    """The layout that wedged castor: vLLM 0.70, compute 56G, sandbox 24G, worker 24G."""
    plan = _plan(
        ["serving", "compute", "agent_sandbox_hosts", "vllm_serving"],
        host_lane_exclusive_groups=[],
        precis_worker_memory_overrides={
            "precis-worker": {"memory_high": "20G", "memory_max": "24G"},
            "precis-worker-compute": {"memory_high": "40G", "memory_max": "56G"},
            "precis-worker-sandbox": {"memory_high": "20G", "memory_max": "24G"},
        },
        host_llm_lanes=[{"group": "vllm_serving", "fraction": 0.70}],
    )
    assert plan["total_mib"] > plan["limit_mib"]


def test_llm_host_takes_no_science_lane() -> None:
    plan = _plan(["serving", "compute", "vllm_serving"])
    assert plan["exclusive"] == ["vllm_serving+compute"]


def test_unknown_llm_fraction_counts_the_whole_box() -> None:
    plan = _plan(
        ["serving", "vllm_serving"],
        host_llm_lanes=[{"group": "vllm_serving", "fraction": 1.0}],
    )
    assert plan["total_mib"] > plan["limit_mib"]


def test_lane_caps_win_over_top_level_and_overrides_win_over_lanes() -> None:
    plan = _plan(
        ["serving", "compute"],
        precis_worker_memory_high="20G",
        precis_worker_memory_max="24G",
        precis_worker_memory_overrides={"precis-worker-compute": {"memory_max": "30G"}},
    )
    units = {u["name"]: u for u in plan["units"]}
    lanes = {lane["lane"]: lane for lane in _defaults()["precis_lane_memory"]}
    assert (
        units["precis-worker.service"]["memory_max"] == lanes["heartbeat"]["memory_max"]
    )
    assert units["precis-worker-compute.service"]["memory_max"] == "30G"
    assert (
        units["precis-worker-compute.service"]["memory_high"]
        == lanes["compute"]["memory_high"]
    )
    # Not placed here: the top-level values still apply, and nothing is counted.
    assert units["precis-worker-sandbox.service"]["placed"] is False
    assert units["precis-worker-sandbox.service"]["memory_max"] == "24G"
    assert not any("sandbox" in label for label, _ in plan["items"])


def test_extra_reserve_and_guard_units_count() -> None:
    base = _plan(["compute"])["total_mib"]
    plan = _plan(
        ["compute"],
        host_memory_extra_reserve="6G",
        host_memory_guard_units=[{"name": "x.service", "memory_max": "2G"}],
    )
    assert plan["total_mib"] == base + 8 * 1024


def test_size_parsing() -> None:
    plan = _plan(
        [],
        host_memory_guard_units=[
            {"name": "a.service", "memory_max": "1536M"},
            {"name": "b.service", "memory_max": "2g"},
            {"name": "c.service", "memory_max": "infinity"},
        ],
    )
    items = dict(plan["items"])
    assert items["unit a.service"] == 1536
    assert items["unit b.service"] == 2048
    assert items["unit c.service"] == 0
    assert plan["limit_mib"] == 121 * 1024


def test_gpu_container_reserve_covers_the_fold_cap() -> None:
    lanes = {lane["lane"]: lane for lane in _defaults()["precis_lane_memory"]}
    fold = _yaml(_ROLES / "alphafold" / "defaults" / "main.yml")["alphafold_mem_limit"]
    assert _gib(fold) <= _gib(lanes["compute"]["container_reserve"])


def test_sandbox_container_reserve_covers_the_executor_default() -> None:
    lanes = {lane["lane"]: lane for lane in _defaults()["precis_lane_memory"]}
    src = _SANDBOX_EXECUTOR.read_text(encoding="utf-8")
    m = re.search(r'os\.environ\.get\("PRECIS_SANDBOX_MEMORY"\) or "([^"]+)"', src)
    assert m, "PRECIS_SANDBOX_MEMORY default moved; update this test"
    assert _gib(m.group(1)) <= _gib(lanes["sandbox"]["container_reserve"])


def test_lane_plays_run_the_budget_check() -> None:
    for play in (
        "35-precis-worker-sandbox.yml",
        "43-precis-worker-compute.yml",
        "49-vllm.yml",
        "49a-sglang.yml",
    ):
        doc = yaml.safe_load((_DEPLOY / "playbooks" / play).read_text(encoding="utf-8"))
        roles = [
            t.get("ansible.builtin.import_role", {})
            for t in doc[0].get("pre_tasks", [])
        ]
        assert {"name": "hardening", "tasks_from": "memory_budget"} in roles, play


def test_drop_ins_follow_the_plan() -> None:
    tasks = yaml.safe_load(
        (_HARDENING / "tasks" / "main.yml").read_text(encoding="utf-8")
    )
    names = [t.get("name", "") for t in tasks]
    plan_at = names.index("Plan + budget-check this host's lane memory (Linux)")
    for name in (
        "Ensure precis worker memory-guard drop-in dirs (Linux)",
        "Memory-guard drop-in for precis worker units (Linux)",
    ):
        i = names.index(name)
        assert i > plan_at
        assert tasks[i]["loop"] == "{{ host_memory_plan.units | default([]) }}"

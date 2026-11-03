"""Deploy-lint for the dedicated chunk_keywords drain lane(s) (20g).

gr520236 / gr550973 follow-up: the shard space is GLOBAL — with H
hosts in `precis_capabilities.chunk_keywords_drain` each running
`precis_worker_kwdrain_lanes` units, `--shard-count` is H*L and host h
(its position in the capability list) runs shard indexes
h*L .. h*L+L-1. Lanes 0..L-1 on every host would otherwise share the
same residue classes and each shard would be worked twice — SKIP
LOCKED only arbitrates the claim instant, so the modulus is the only
partition. These tests render the playbook's Jinja expressions for a
simulated 2x2 fleet and pin the resize-retire tasks, the two-key gate,
and the capability declaration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2
import yaml

_REPO = Path(__file__).resolve().parent.parent
_DEPLOY = _REPO / "deploy"
_20G = _DEPLOY / "playbooks" / "20g-precis-worker-kwdrain.yml"
_TOPOLOGY = (
    _DEPLOY / "inventory.example" / "group_vars" / "all" / "topology.example.yml"
)
_DEFAULTS = _DEPLOY / "roles" / "precis_worker" / "defaults" / "main.yml"


def _play() -> dict[str, Any]:
    plays = yaml.safe_load(_20G.read_text(encoding="utf-8"))
    assert isinstance(plays, list) and len(plays) == 1
    return plays[0]


def _task_named(fragment: str) -> dict[str, Any]:
    found = [t for t in _play()["tasks"] if fragment in str(t.get("name", ""))]
    assert len(found) == 1, f"expected one task matching {fragment!r}"
    return found[0]


def _render(template: str, **ctx: Any) -> str:
    return jinja2.Template(template).render(**ctx)


# --------------------------------------------------------------------------
# Global shard math
# --------------------------------------------------------------------------


def test_shard_count_is_hosts_times_lanes() -> None:
    """`--shard-count` must cover the whole fleet's lanes — the value
    rendered for `--shard-count` is `hosts * lanes`, not lanes."""
    expr = str(_play()["vars"]["_kw_shard_count"])
    assert "_kw_hosts" in expr and "_kw_lanes" in expr
    assert _render(expr, _kw_hosts=["h-a", "h-b"], _kw_lanes="2") == "4"


def test_host_index_is_capability_list_position() -> None:
    """The shard base is the host's position in the capability list —
    the same ordered list on every host, so every host computes a
    consistent assignment without naming hostnames in the playbook."""
    expr = str(_play()["vars"]["_kw_host_idx"])
    assert ".index(" in expr and "_kw_hosts" in expr
    hosts = ["h-a", "h-b", "h-c"]
    assert _render(expr, _kw_hosts=hosts, inventory_hostname="h-a") == "0"
    assert _render(expr, _kw_hosts=hosts, inventory_hostname="h-c") == "2"
    # Not a member → the `-1` off-marker, never an index() ValueError.
    assert _render(expr, _kw_hosts=hosts, inventory_hostname="h-x") == "-1"


def test_2x2_fleet_renders_four_distinct_shards() -> None:
    """2 hosts x 2 lanes: the rendered argv gives each lane a distinct
    shard index 0..3 over count 4 — covering the whole space with no
    residue class worked twice."""
    exec_t = str(
        _task_named("render + load each chunk_keywords drain lane unit")["vars"][
            "service_unit_exec"
        ]
    )
    assert "--shard-count" in exec_t and "--shard-index" in exec_t
    rendered: dict[tuple[int, int], tuple[int, int]] = {}
    for host_idx in (0, 1):
        for lane in (1, 2):
            out = _render(
                exec_t,
                precis_worker_venv="/v",
                _kw_shard_count=4,
                _kw_host_idx=host_idx,
                _kw_lanes=2,
                lane=lane,
            )
            count = int(out.split("--shard-count")[1].split()[0])
            index = int(out.split("--shard-index")[1].split()[0])
            rendered[(host_idx, lane)] = (count, index)
    assert {c for c, _ in rendered.values()} == {4}
    assert sorted(i for _, i in rendered.values()) == [0, 1, 2, 3]


# --------------------------------------------------------------------------
# Gate + lifecycle
# --------------------------------------------------------------------------


def test_lane_is_gated_on_capability_membership() -> None:
    """Two-key opt-in, mirrors 20f: `hosts: all`, the render and bounce
    tasks fire only where `precis_worker_kwdrain_enabled` AND the host
    sits in `precis_capabilities.chunk_keywords_drain`."""
    play = _play()
    assert play["hosts"] == "all"
    gate = str(play["vars"]["_kw_lane_on"])
    assert "precis_worker_kwdrain_enabled" in gate
    assert "precis_capabilities.chunk_keywords_drain" in gate
    for fragment in (
        "provisioning only",
        "render + load each chunk_keywords drain lane unit",
        "Bounce the kwdrain lanes on a code refresh (macOS)",
        "Bounce the kwdrain lanes on a code refresh (Linux)",
    ):
        task = _task_named(fragment)
        assert "_kw_lane_on" in str(task.get("when", "")), fragment


def test_resize_retires_lanes_above_the_count() -> None:
    """Lowering `precis_worker_kwdrain_lanes` re-renders lanes 1..N but
    leaves units N+1.. loaded with the OLD shard space — an explicit
    retire task must bootout/disable any kwdrain unit above the new
    count on enabled hosts."""
    for fragment, needle in (
        ("retire kwdrain lanes above the current count (macOS)", "LaunchDaemons"),
        ("retire kwdrain lanes above the current count (Linux)", "systemd/system"),
    ):
        task = _task_named(fragment)
        when = str(task.get("when", ""))
        assert "_kw_lane_on | bool" in when, fragment
        shell = str(task["ansible.builtin.shell"]["cmd"])
        assert "worker-kwdrain-" in shell and needle in shell
        assert "_kw_lanes" in shell and "-gt" in shell, (
            f"{fragment} must compare the unit's index against the new lane count"
        )


def test_stale_units_still_retire_where_the_lane_is_off() -> None:
    """The flag-off / host-left-the-capability sweep is unchanged:
    every kwdrain unit retires where the lane must not exist."""
    for fragment in (
        "retire stale kwdrain LaunchDaemons (macOS)",
        "retire stale kwdrain systemd units (Linux)",
    ):
        task = _task_named(fragment)
        assert "not (_kw_lane_on | bool)" in str(task.get("when", ""))


def test_exec_runs_the_dedicated_chunk_keywords_loop() -> None:
    exec_t = str(
        _task_named("render + load each chunk_keywords drain lane unit")["vars"][
            "service_unit_exec"
        ]
    )
    assert "--only chunk_keywords" in exec_t
    assert "--dedicated" in exec_t


def test_capability_is_declared_and_ships_empty() -> None:
    """The capability exists in the example topology and ships EMPTY —
    naming a host here is the first half of the go switch."""
    topology = yaml.safe_load(_TOPOLOGY.read_text(encoding="utf-8"))
    assert "chunk_keywords_drain" in topology["precis_capabilities"]
    assert topology["precis_capabilities"]["chunk_keywords_drain"] == []
    defaults = yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))
    assert defaults["precis_worker_kwdrain_enabled"] is False
    assert defaults["precis_worker_kwdrain_lanes"] >= 1


def test_lane_uses_capability_refs_not_hostnames() -> None:
    """Lane hosts are named via `precis_capabilities`, never a literal —
    deploy/ is scanned for real node hostnames
    (test_deploy_tree_no_secrets.py)."""
    assert "precis_capabilities.chunk_keywords_drain" in _20G.read_text(
        encoding="utf-8"
    )

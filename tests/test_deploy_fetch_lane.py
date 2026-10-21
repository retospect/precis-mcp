"""Deploy-lint for the dedicated OA-fetch lane (Reto 2026-10-21, option A).

`fetch` (fetch_oa) used to run as one ~300 s slot inside the collapsed
`precis worker --profile all` serial rotation — prod measured ~0.05 req/s
(~30 papers/h) against a 25k-stub eligible backlog. Playbook 20b now renders
a second unit, `com.precis.worker-fetch` / `precis-worker-fetch.service`
(`precis worker --only fetch`), on the `precis_capabilities.oa_fetch` hosts.

The invariant these tests pin is the single-fetcher rule (two fetchers race
the shared watch inbox — docs/reference/config-variables.md): while
`precis_worker_fetch_dedicated` holds, EVERY non-lane unit renders
`PRECIS_OA_FETCH=0` — explicitly, after the shared-env combine, so a leftover
overlay `PRECIS_OA_FETCH=1` can't re-arm the pass inside a main unit.
"""

from __future__ import annotations

import plistlib
from pathlib import Path
from typing import Any

import jinja2
import yaml

_REPO = Path(__file__).resolve().parent.parent
_DEPLOY = _REPO / "deploy"
_20B = _DEPLOY / "playbooks" / "20b-precis-worker-collapsed.yml"
_DEFAULTS = _DEPLOY / "roles" / "precis_worker" / "defaults" / "main.yml"
_PLIST = _DEPLOY / "roles" / "precis_worker" / "templates" / "precis-worker.plist.j2"
_SERVICE = (
    _DEPLOY / "roles" / "precis_worker" / "templates" / "precis-worker.service.j2"
)


def _play() -> dict[str, Any]:
    plays = yaml.safe_load(_20B.read_text(encoding="utf-8"))
    assert isinstance(plays, list) and len(plays) == 1
    return plays[0]


def _var(name: str) -> str:
    return str(_play()["vars"][name])


def _task_named(fragment: str) -> dict[str, Any]:
    found = [t for t in _play()["tasks"] if fragment in str(t.get("name", ""))]
    assert len(found) == 1, f"expected one task matching {fragment!r}"
    return found[0]


# --------------------------------------------------------------------------
# The lane unit itself
# --------------------------------------------------------------------------


def test_20b_renders_a_dedicated_fetch_unit() -> None:
    task = _task_named("dedicated OA-fetch worker unit")
    assert task["ansible.builtin.import_role"]["name"] == "service_unit"
    spec = task["vars"]
    assert spec["service_unit_name"] == "worker-fetch"
    assert spec["service_unit_run_as"] == "deploy"
    assert spec["service_unit_schedule"]["mode"] == "keepalive"
    assert spec["service_unit_env"] == "{{ _l_b_fetch_env }}"


def test_lane_exec_is_only_fetch_with_a_short_idle() -> None:
    exec_line = str(
        _task_named("dedicated OA-fetch worker unit")["vars"]["service_unit_exec"]
    )
    assert "--only fetch" in exec_line
    assert "--batch-size {{ precis_worker_fetch_batch_size }}" in exec_line
    assert "--idle-seconds {{ precis_worker_fetch_idle_seconds }}" in exec_line
    # No embedder/summarizer flags — under --only fetch no handler is built
    # (cli/worker.py _build_handlers), so those knobs would be dead argv.
    assert "--embedder" not in exec_line and "--summarizer-model" not in exec_line


def test_lane_env_arms_fetch_and_carries_the_knobs() -> None:
    env = _var("_l_b_fetch_env")
    assert "'PRECIS_OA_FETCH': '1'" in env
    assert "'PRECIS_OA_FETCH_PASS_WALL_S': precis_worker_fetch_pass_wall_s" in env
    assert "'PRECIS_OA_FETCH_WORKERS': precis_worker_fetch_workers" in env
    assert "precis_worker_fetch_inflight" in env
    # Same inbox/credential/identity env the collapsed worker's fetch pass
    # resolved — the lane would otherwise fetch into an inbox nobody scans.
    assert env.index("precis_shared_env") < env.index("'PRECIS_OA_FETCH': '1'")
    assert "'PRECIS_FETCH_MARKUP': '1'" in env
    assert "'PRECIS_PROCESS': 'precis-worker-fetch'" in env


def test_lane_is_gated_on_capability_and_flag() -> None:
    on = _var("_l_b_fetch_lane_on")
    assert "precis_worker_fetch_dedicated" in on
    assert "precis_capabilities.oa_fetch" in on
    for fragment in (
        "dedicated OA-fetch worker unit",
        "Bounce the fetch lane on a code refresh (site.yml path, macOS)",
        "Bounce the fetch lane on a code refresh (site.yml path, Linux)",
    ):
        task = _task_named(fragment)
        assert "_l_b_fetch_lane_on" in str(task.get("when", ""))


def test_stale_lane_is_retired_where_it_must_not_exist() -> None:
    """Flag-off or host-left-oa_fetch: the unit must be removed, not left
    running PRECIS_OA_FETCH=1 next to a re-armed collapsed worker."""
    for fragment in (
        "retire a stale fetch-lane LaunchDaemon",
        "retire a stale fetch-lane systemd unit",
    ):
        task = _task_named(fragment)
        assert "not (_l_b_fetch_lane_on | bool)" in str(task.get("when", ""))
    darwin = str(_task_named("retire a stale fetch-lane LaunchDaemon"))
    assert "bootout" in darwin and "com.precis.worker-fetch" in darwin
    linux = str(_task_named("retire a stale fetch-lane systemd unit"))
    assert "precis-worker-fetch.service" in linux


# --------------------------------------------------------------------------
# The single-fetcher invariant on the main units
# --------------------------------------------------------------------------


def test_collapsed_unit_env_pins_fetch_off_under_dedicated() -> None:
    base = _var("_l_b_base_env")
    pin = "'PRECIS_OA_FETCH': '0'}"
    assert pin in base
    assert "precis_worker_fetch_dedicated" in base
    # The pin must land AFTER the shared/identity combine so it wins over an
    # overlay PRECIS_OA_FETCH=1, not before where it would be overridden.
    assert base.index(pin) > base.index("combine(precis_identity_env")


def test_role_defaults_carry_the_flag_and_knobs() -> None:
    defaults = yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))
    assert defaults["precis_worker_fetch_dedicated"] is True
    assert defaults["precis_worker_fetch_pass_wall_s"] == 600
    assert defaults["precis_worker_fetch_workers"] >= 1
    assert defaults["precis_worker_fetch_idle_seconds"] > 0


# --------------------------------------------------------------------------
# Rollback templates — the same invariant under the split-unit path
# --------------------------------------------------------------------------

_RENDER_VARS: dict[str, Any] = {
    "precis_worker_venv": "/opt/precis/venv",
    "precis_worker_dsn": "postgresql://agent_rw@db:6432/precis_prod",
    "precis_worker_batch_size": 32,
    "precis_worker_idle_seconds": 2,
    "precis_worker_embedder": "remote",
    "precis_worker_embedder_url": "http://127.0.0.1:8181",
    "precis_worker_summarizer": "rake-lemma",
    "precis_worker_hf_home": "/var/lib/precis/hf-cache",
    # The overlay shape that must never reach a split unit under dedicated
    # mode: an operator's leftover PRECIS_OA_FETCH=1 in the shared env.
    "precis_shared_env": {"PRECIS_OA_FETCH": "1", "PRECIS_WATCH_INBOX": "/inbox"},
    "precis_identity_env": {},
    "precis_capabilities": {},
    "groups": {},
    "inventory_hostname": "node-gateway",
}


def _combine(a: Any, b: Any) -> Any:
    """Stand-in for ansible's ``combine`` filter (shallow merge suffices —
    every rendered dict here is flat KEY: value env pairs)."""
    return {**(a or {}), **(b or {})}


def _render(path: Path, **extra: Any) -> str:
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    env.filters["combine"] = _combine
    return env.from_string(path.read_text(encoding="utf-8")).render(
        **_RENDER_VARS, **extra
    )


def test_rollback_plist_pins_fetch_off_when_dedicated() -> None:
    out = _render(_PLIST, precis_worker_fetch_dedicated=True)
    env = plistlib.loads(out.encode())["EnvironmentVariables"]
    assert env["PRECIS_OA_FETCH"] == "0", (
        "rollback plist kept the overlay's PRECIS_OA_FETCH=1 — the split "
        "unit would fetch alongside the dedicated lane"
    )
    assert out.count("<key>PRECIS_OA_FETCH</key>") == 1, "duplicate plist key"


def test_rollback_plist_passes_overlay_through_when_not_dedicated() -> None:
    out = _render(_PLIST, precis_worker_fetch_dedicated=False)
    env = plistlib.loads(out.encode())["EnvironmentVariables"]
    assert env["PRECIS_OA_FETCH"] == "1"


def test_rollback_service_pins_fetch_off_when_dedicated() -> None:
    out = _render(_SERVICE, precis_worker_fetch_dedicated=True)
    lines = [
        ln for ln in out.splitlines() if ln.startswith("Environment=PRECIS_OA_FETCH")
    ]
    assert lines == ["Environment=PRECIS_OA_FETCH=0"], lines


def test_rollback_service_passes_overlay_through_when_not_dedicated() -> None:
    out = _render(_SERVICE, precis_worker_fetch_dedicated=False)
    assert "Environment=PRECIS_OA_FETCH=1" in out


# --------------------------------------------------------------------------
# Secret gate: no literal hosts — capability refs only
# --------------------------------------------------------------------------


def test_lane_uses_capability_refs_not_hostnames() -> None:
    """The oa_fetch host must be named via precis_capabilities, never a
    literal — deploy/ is scanned for real node hostnames
    (test_deploy_tree_no_secrets.py)."""
    assert "precis_capabilities.oa_fetch" in _20B.read_text(encoding="utf-8")

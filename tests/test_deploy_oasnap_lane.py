"""Deploy-lint for the dedicated OpenAlex-snapshot worker lane (oa-cites
slice 3, docs/backlog/openalex-snapshot-cites.md).

The OpenAlex works-dump refresh reads 675 GB on a full release, so it can
never ride `worker_system` (every host) — playbook 20f renders
`com.precis.worker-oasnap` / `precis-worker-oasnap.service`
(`precis worker --only openalex_snapshot`) only where
`precis_capabilities.openalex_snapshot` names the host AND
`precis_worker_oasnap_enabled` is true (two-key opt-in: the capability
ships empty in the example and the flag defaults false), and retires the
unit everywhere else. These tests pin that gating so a later edit can't
turn the singleton into a fleet-wide multiply.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parent.parent
_DEPLOY = _REPO / "deploy"
_20F = _DEPLOY / "playbooks" / "20f-precis-worker-oasnap.yml"
_TOPOLOGY = (
    _DEPLOY / "inventory.example" / "group_vars" / "all" / "topology.example.yml"
)
_DEFAULTS = _DEPLOY / "roles" / "precis_worker" / "defaults" / "main.yml"
_SITE = _DEPLOY / "site.yml"


def _play() -> dict[str, Any]:
    plays = yaml.safe_load(_20F.read_text(encoding="utf-8"))
    assert isinstance(plays, list) and len(plays) == 1
    return plays[0]


def _task_named(fragment: str) -> dict[str, Any]:
    found = [t for t in _play()["tasks"] if fragment in str(t.get("name", ""))]
    assert len(found) == 1, f"expected one task matching {fragment!r}"
    return found[0]


def test_20f_renders_a_dedicated_oasnap_unit() -> None:
    task = _task_named("OpenAlex-snapshot worker unit")
    assert task["ansible.builtin.import_role"]["name"] == "service_unit"
    spec = task["vars"]
    assert spec["service_unit_name"] == "worker-oasnap"
    assert spec["service_unit_run_as"] == "deploy"
    assert spec["service_unit_schedule"]["mode"] == "keepalive"


def test_lane_exec_is_only_openalex_snapshot() -> None:
    exec_line = str(
        _task_named("OpenAlex-snapshot worker unit")["vars"]["service_unit_exec"]
    )
    assert "--only openalex_snapshot" in exec_line
    assert "--idle-seconds {{ precis_worker_oasnap_idle_seconds }}" in exec_line
    # No embedder/summarizer flags — under --only no other handler is built.
    assert "--embedder" not in exec_line and "--summarizer-model" not in exec_line


def test_lane_is_gated_on_capability_membership() -> None:
    play = _play()
    assert play["hosts"] == "all"
    gate = str(play["vars"]["_oasnap_lane_on"])
    assert "precis_capabilities.openalex_snapshot" in gate
    for fragment in (
        "provisioning only",
        "OpenAlex-snapshot worker unit",
        "Bounce the oasnap lane on a code refresh (site.yml path, macOS)",
        "Bounce the oasnap lane on a code refresh (site.yml path, Linux)",
    ):
        task = _task_named(fragment)
        assert "_oasnap_lane_on" in str(task.get("when", "")), fragment


def test_stale_lane_is_retired_where_it_must_not_exist() -> None:
    for fragment in (
        "retire a stale oasnap-lane LaunchDaemon",
        "retire a stale oasnap-lane systemd unit",
    ):
        task = _task_named(fragment)
        assert "not (_oasnap_lane_on | bool)" in str(task.get("when", ""))
    darwin = str(_task_named("retire a stale oasnap-lane LaunchDaemon"))
    assert "bootout" in darwin and "com.precis.worker-oasnap" in darwin
    linux = str(_task_named("retire a stale oasnap-lane systemd unit"))
    assert "precis-worker-oasnap.service" in linux


def test_capability_and_role_default_exist() -> None:
    topology = yaml.safe_load(_TOPOLOGY.read_text(encoding="utf-8"))
    assert "openalex_snapshot" in topology["precis_capabilities"]
    defaults = yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))
    assert defaults["precis_worker_oasnap_idle_seconds"] > 0


def test_capability_ships_empty_until_the_go() -> None:
    """Naming a host in the example topology is the first-pass go switch
    (a 675 GB works-dump read) — the example ships the capability empty
    so syncing it into an overlay cannot start the pass by accident."""
    topology = yaml.safe_load(_TOPOLOGY.read_text(encoding="utf-8"))
    assert topology["precis_capabilities"]["openalex_snapshot"] == []


def test_lane_also_needs_the_enabled_flag() -> None:
    """Two-key opt-in: the gate ANDs the capability list with
    `precis_worker_oasnap_enabled`, which defaults false."""
    gate = str(_play()["vars"]["_oasnap_lane_on"])
    assert "precis_worker_oasnap_enabled" in gate
    assert "precis_capabilities.openalex_snapshot" in gate
    defaults = yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))
    assert defaults["precis_worker_oasnap_enabled"] is False


def test_snapshot_pass_stays_off_default_profiles() -> None:
    """The property that keeps a 675 GB pass off the fleet: no
    default_profiles and no enable_env, so --profile system/agent/all
    never registers it — only an explicit --only openalex_snapshot."""
    from precis.workers.registry import (
        SERVICES_BY_NAME,
        service_names_for_profile,
    )

    spec = SERVICES_BY_NAME["openalex_snapshot"]
    assert not spec.default_profiles and spec.enable_env is None
    assert "openalex_snapshot" not in service_names_for_profile("all")


def test_site_yml_imports_20f() -> None:
    site = _SITE.read_text(encoding="utf-8")
    assert "playbooks/20f-precis-worker-oasnap.yml" in site


def test_lane_uses_capability_refs_not_hostnames() -> None:
    """The lane host is named via precis_capabilities, never a literal —
    deploy/ is scanned for real node hostnames
    (test_deploy_tree_no_secrets.py)."""
    assert "precis_capabilities.openalex_snapshot" in _20F.read_text(encoding="utf-8")

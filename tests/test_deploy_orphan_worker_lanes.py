"""Deploy-lint: every running precis-worker-*.service must get bounced (gr485861).

r35 upgraded /opt/precis/venv on the sandbox host while
precis-worker-sandbox.service kept running pre-r35 code. Two holes combined:

1. Playbooks/35 has no code-refresh bounce of its own, and the step-2
   "Bounce all precis daemons" play targets only gateway/scheduler/inference
   — never agent_sandbox_hosts — so even a grouped sandbox lane was never
   restarted on a venv move. (The compute lane survives the same topology
   only because 43's site.yml-path bounce fires: precis_deploy_changed is
   unset on hosts the pre-flight play doesn't target.)

2. agent_sandbox_hosts was emptied while the unit stayed installed+enabled
   on its former member — plays 34/35 logged "no hosts matched" and no play
   anywhere touched the still-running unit.

The fix is a self-bounce task in 35 plus a step-2f sweep play in
redeploy-precis.yml that restarts and loudly flags any loaded
precis-worker-*.service no lane play owns on the non-step-2 lane groups
(serving/compute/agent_sandbox_hosts). These tests pin that structure —
a pure YAML/text walk, no ansible needed (same approach as
test_deploy_watch_unit_reconverge.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEPLOY = _REPO_ROOT / "deploy"
_REDEPLOY = _DEPLOY / "redeploy-precis.yml"
_SANDBOX_PLAYBOOK = _DEPLOY / "playbooks" / "35-precis-worker-sandbox.yml"

_SWEEP_PLAY = "Bounce + flag orphaned precis worker lanes (no play owns them here)"
_SANDBOX_IMPORT = "playbooks/35-precis-worker-sandbox.yml"


def _plays(path: Path) -> list[dict[str, Any]]:
    plays = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(plays, list)
    return plays


def _find_play(plays: list[dict[str, Any]], name: str) -> dict[str, Any]:
    found = [p for p in plays if p.get("name") == name]
    assert len(found) == 1, f"expected exactly one play named {name!r}"
    return found[0]


def _task(play: dict[str, Any], name: str) -> dict[str, Any]:
    found = [t for t in play.get("tasks", []) if t.get("name") == name]
    assert len(found) == 1, f"expected exactly one task named {name!r}"
    return found[0]


def _when_text(task: dict[str, Any]) -> str:
    return str(task.get("when", ""))


# --------------------------------------------------------------------------
# Play 35: the sandbox lane bounces itself on a code refresh
# --------------------------------------------------------------------------


def test_sandbox_playbook_has_a_code_refresh_bounce() -> None:
    """gr485861 hole 1: without a self-bounce the lane restarts only on a
    unit-file change — step-2's host list never includes agent_sandbox_hosts,
    so a venv refresh left the unit on stale code."""
    plays = _plays(_SANDBOX_PLAYBOOK)
    tasks = [t for p in plays for t in p.get("tasks", [])]
    bounces = [
        t
        for t in tasks
        if (t.get("ansible.builtin.systemd") or {}).get("name")
        == "precis-worker-sandbox"
        and (t.get("ansible.builtin.systemd") or {}).get("state") == "restarted"
    ]
    assert bounces, (
        "35-precis-worker-sandbox.yml has no systemd restart of "
        "precis-worker-sandbox — the lane never picks up a venv refresh "
        "(the step-2 bounce play does not target agent_sandbox_hosts)"
    )


def test_sandbox_bounce_fires_on_a_code_deploy() -> None:
    """The bounce must fire when a redeploy moved code. `is not defined`
    alone would skip it the day the pre-flight play ever covers this group
    with a defined-true fact; `default(true)` covers both the undefined
    (today) and defined-true cases — only a proven no-op deploy skips."""
    plays = _plays(_SANDBOX_PLAYBOOK)
    tasks = [t for p in plays for t in p.get("tasks", [])]
    bounces = [
        t
        for t in tasks
        if (t.get("ansible.builtin.systemd") or {}).get("name")
        == "precis-worker-sandbox"
        and (t.get("ansible.builtin.systemd") or {}).get("state") == "restarted"
    ]
    assert bounces
    when = _when_text(bounces[0])
    assert "precis_deploy_changed | default(true) | bool" in when, (
        "the sandbox bounce must default to firing — defined-false is the "
        "only skip; a defined-true deploy must still bounce here because no "
        "other play covers agent_sandbox_hosts"
    )


# --------------------------------------------------------------------------
# redeploy §2f: the orphan sweep covers installed-but-unowned lane units
# --------------------------------------------------------------------------


def _sweep_play() -> dict[str, Any]:
    return _find_play(_plays(_REDEPLOY), _SWEEP_PLAY)


def test_sweep_targets_the_lane_groups_step_2_misses() -> None:
    """gateway/scheduler/inference already get a blanket `precis-*.service`
    bounce in step 2 — the sweep must cover exactly the lane-bearing groups
    that play does NOT reach."""
    hosts = set(_sweep_play()["hosts"])
    assert {"serving", "compute", "agent_sandbox_hosts"} <= hosts, (
        f"orphan sweep hosts {hosts} miss a lane-bearing group — units on "
        "those hosts have no bounce coverage outside their owning play"
    )
    assert not ({"gateway", "scheduler", "inference"} & hosts), (
        "step-2 hosts must stay out of the sweep — their precis-*.service "
        "units are already glob-bounced there, so sweeping them double-"
        "restarts every unit and flags owned units as orphans"
    )


def test_sweep_lists_worker_lane_units() -> None:
    task = _task(
        _sweep_play(),
        "List loaded precis-worker-* units with their active state (Linux)",
    )
    cmd = task["ansible.builtin.shell"]
    assert "precis-worker-*.service" in cmd
    assert "list-units" in cmd, "the sweep must see loaded units regardless of state"


def test_sweep_owned_set_mirrors_the_lane_plays() -> None:
    """The owned set is the exclusion list — a unit on it is already bounced
    by its owning play (43 on compute:inference, 35 on agent_sandbox_hosts).
    It must name every lane unit those plays install."""
    owned = _task(_sweep_play(), "Worker lane units a lane play owns on this host")
    expr = str(owned["ansible.builtin.set_fact"])
    assert "precis-worker-compute.service" in expr
    assert "precis-worker-sandbox.service" in expr
    assert "groups['compute']" in expr and "groups['inference']" in expr
    assert "groups['agent_sandbox_hosts']" in expr


def test_sweep_bounces_only_running_orphans() -> None:
    """Only `active` orphans get restarted — a stopped/disabled unit is
    drift to flag, not a stale-code process, and restarting it would
    resurrect something deliberately down."""
    play = _sweep_play()
    restart = _task(play, "Restart running orphaned lane units onto the deployed venv")
    assert restart["ansible.builtin.systemd"]["state"] == "restarted"
    assert restart["loop"] == "{{ _orphan_lanes_running | default([]) }}"
    owned = _task(
        play, "Orphaned lane units on this host (loaded but owned by no play)"
    )
    facts_expr = str(owned["ansible.builtin.set_fact"])
    assert "_orphan_lanes_running" in facts_expr
    assert "active" in facts_expr


def test_sweep_flags_orphans_loudly() -> None:
    play = _sweep_play()
    flag = _task(play, "Flag orphaned worker lanes (installed here, owned by no play)")
    msg = str(flag["ansible.builtin.debug"]["msg"])
    assert "WARNING" in msg


def test_sweep_runs_after_the_sandbox_lane_import() -> None:
    """The sweep is the backstop for the lane plays — it must run after the
    last lane render (35's import) so a managed unit's own bounce has
    already happened."""
    text = _REDEPLOY.read_text(encoding="utf-8")
    assert text.index(_SWEEP_PLAY) > text.index(_SANDBOX_IMPORT), (
        "the orphan sweep must follow the 35-precis-worker-sandbox.yml "
        "import in redeploy-precis.yml"
    )


def test_sweep_is_untagged() -> None:
    """Role scoping must never skip the sweep: a skipped lane play is exactly
    when an orphan needs the backstop."""
    assert "tags" not in _sweep_play(), (
        "the orphan sweep play must stay untagged — --skip-tags must not be "
        "able to remove it"
    )

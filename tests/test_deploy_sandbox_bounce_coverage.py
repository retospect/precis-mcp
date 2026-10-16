"""Deploy-lint: the sandbox unit bounces on whichever host the overlay names (gr485861).

The lane has two bounce paths, and each host the overlay names must reach one:

* play 35's own bounce — its selection play puts every declared
  ``agent_sandbox_hosts`` member plus any Linux host with the unit already
  installed (the adoption rule) into ``agent_sandbox_lane``, and the lane
  play restarts the unit there;
* redeploy's step-2f orphan sweep — the backstop for when role scoping
  skips play 35 (``--skip-tags sandbox`` on a web-only/docs-only/same-sha
  deploy). ``agent_sandbox_lane`` is then never populated, so the sweep's
  owned set keys on it alone: a declared member counts as UNOWNED and gets
  restarted, while the wider "known" set (declared ∪ adopted) keeps it out
  of the drift flag.

These tests evaluate the shipped ``when``/``set_fact`` Jinja expressions per
host under synthetic overlays — placeholder names only, no ansible — the
same approach as tests/test_deploy_sandbox_lane_selection.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2
import pytest
import yaml

_DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
_PLAYBOOK = _DEPLOY / "playbooks" / "35-precis-worker-sandbox.yml"
_REDEPLOY = _DEPLOY / "redeploy-precis.yml"

_UNIT = "precis-worker-sandbox.service"

_SELECT_PLAY = "Select the sandbox lane hosts (declared group + installed units)"
_LANE_PLAY = "Deploy the sandbox_run worker lane (agent_sandbox_lane)"
_JOIN = "Join the sandbox lane group"
_BOUNCE = "Bounce the sandbox lane on a code refresh (no step-2 coverage — gr485861)"

_SWEEP_PLAY = "Bounce + flag orphaned precis worker lanes (no play owns them here)"
_OWNED = "Worker lane units a lane play owns on this host"
_ORPHANS = "Orphaned lane units on this host (loaded but owned by no play)"
_FLAG = "Flag orphaned worker lanes (installed here, owned by no play)"


def _plays(path: Path) -> list[dict[str, Any]]:
    plays = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(plays, list)
    return plays


def _play(path: Path, name: str) -> dict[str, Any]:
    found = [p for p in _plays(path) if p.get("name") == name]
    assert len(found) == 1, f"expected exactly one play named {name!r}"
    return found[0]


def _task(play: dict[str, Any], name: str) -> dict[str, Any]:
    found = [t for t in play.get("tasks", []) if t.get("name") == name]
    assert len(found) == 1, f"expected exactly one task named {name!r}"
    return found[0]


def _env() -> jinja2.Environment:
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    # Ansible's `bool` filter — enough of it for the ship-tree `when`s here.
    env.filters["bool"] = lambda v: (
        v.strip().lower() in ("1", "true", "yes", "on")
        if isinstance(v, str)
        else bool(v)
    )
    return env


def _joins(host: str, groups: dict[str, list[str]], installed: bool) -> bool:
    """Evaluate the selection play's ``group_by`` ``when`` for one host."""
    when = _task(_play(_PLAYBOOK, _SELECT_PLAY), _JOIN)["when"]
    probe: dict[str, Any] = (
        {"stdout": f"{_UNIT} enabled enabled\n", "rc": 0}
        if installed
        else {"skipped": True}
    )
    rendered = (
        _env()
        .from_string("{{ (" + when + ") | string }}")
        .render(
            inventory_hostname=host,
            groups=groups,
            _sb_unit_probe=probe,
        )
    )
    return rendered == "True"


def _unit_set(fact: str, host: str, groups: dict[str, list[str]]) -> list[str]:
    """Render a lane-units ``set_fact`` expression for one host."""
    expr = str(
        _task(_play(_REDEPLOY, _SWEEP_PLAY), _OWNED)["ansible.builtin.set_fact"][fact]
    )
    rendered = (
        _env()
        .from_string(expr)
        .render(
            inventory_hostname=host,
            groups=groups,
        )
    )
    return yaml.safe_load(rendered)


def _flagged_expr() -> str:
    return str(
        _task(_play(_REDEPLOY, _SWEEP_PLAY), _ORPHANS)["ansible.builtin.set_fact"][
            "_orphan_lanes_flagged"
        ]
    )


def _sweep_members(groups: dict[str, list[str]]) -> set[str]:
    """Hosts the sweep play actually reaches (its groups minus the anchor)."""
    hosts = set(_play(_REDEPLOY, _SWEEP_PLAY)["hosts"])
    hosts.discard("gateway")  # isolation anchor — end_hosts itself out
    out: set[str] = set()
    for group in hosts:
        out.update(groups.get(group, []))
    return out


def _lane_bounce_when(host: str, groups: dict[str, list[str]]) -> bool:
    """Evaluate the lane play's bounce ``when`` (a YAML list) for a member."""
    os_family = "linux" if host in groups.get("linux", []) else "darwin"
    for cond in _task(_play(_PLAYBOOK, _LANE_PLAY), _BOUNCE)["when"]:
        rendered = (
            _env()
            .from_string("{{ (" + str(cond) + ") | string }}")
            .render(
                inventory_hostname=host,
                groups=groups,
                os_family=os_family,
            )
        )
        if rendered != "True":
            return False
    return True


def _bounces(
    host: str,
    groups: dict[str, list[str]],
    *,
    installed: bool,
    lane_play_ran: bool,
) -> tuple[bool, bool]:
    """(bounced, flagged) for ``_UNIT`` on ``host`` under this overlay.

    ``lane_play_ran=False`` is a role-scoped deploy that skipped play 35:
    ``agent_sandbox_lane`` is then absent from ``groups``, mirroring the
    in-memory inventory of such a run.
    """
    lane_members = {
        h
        for h in groups.get("linux", [])
        if _joins(h, groups, installed=installed and h == host)
    }
    live_groups = dict(groups)
    if lane_play_ran:
        live_groups["agent_sandbox_lane"] = sorted(lane_members)
    if lane_play_ran and host in lane_members:
        return _lane_bounce_when(host, live_groups), False
    owned = _unit_set("_orphan_lanes_owned", host, live_groups)
    known = _unit_set("_orphan_lanes_known", host, live_groups)
    # The sweep only sees units actually loaded on the host.
    swept = installed and host in _sweep_members(groups) and _UNIT not in owned
    flagged = installed and host in _sweep_members(groups) and _UNIT not in known
    return swept, flagged


# ── the overlay names a host ──────────────────────────────────────────────
# Placeholder layout: node-sbx is the declared sandbox host (also compute),
# node-old is a former member that kept the installed unit (serving only),
# node-idle carries neither declaration nor unit.

_OVERLAY: dict[str, list[str]] = {
    "linux": ["node-sbx", "node-old", "node-idle", "node-gw0"],
    "serving": ["node-old"],
    "compute": ["node-sbx", "node-idle"],
    "agent_sandbox_hosts": ["node-sbx"],
    "vllm_serving": ["node-old"],
    "gateway": ["node-gw0"],
}


@pytest.mark.parametrize("lane_play_ran", [True, False])
def test_declared_host_bounces_whether_or_not_the_lane_play_runs(
    lane_play_ran: bool,
) -> None:
    """The overlay-named host's unit bounces on every deploy: the lane play
    when it runs, the orphan sweep when role scoping skips it."""
    bounced, flagged = _bounces(
        "node-sbx", _OVERLAY, installed=True, lane_play_ran=lane_play_ran
    )
    assert bounced, (
        "node-sbx is declared in agent_sandbox_hosts — it must bounce even "
        "when --skip-tags sandbox kept play 35 out (its owned-set entry "
        "must come from agent_sandbox_lane alone)"
    )
    # A declared host is never drift, even on the sweep path.
    assert not flagged


def test_adopted_host_bounces_by_the_rule_and_flags_when_the_play_is_skipped() -> None:
    """Adoption (installed unit, undeclared): the lane play bounces it when
    it runs; when skipped, the sweep restarts AND flags it — a real orphan."""
    bounced, flagged = _bounces(
        "node-old", _OVERLAY, installed=True, lane_play_ran=True
    )
    assert bounced and not flagged
    bounced, flagged = _bounces(
        "node-old", _OVERLAY, installed=True, lane_play_ran=False
    )
    assert bounced and flagged


def test_declared_and_installed_is_bounced_once() -> None:
    """'or both': a declared member with the unit installed is one lane
    member — bounced by the lane play, never double-counted."""
    in_lane = _joins("node-sbx", _OVERLAY, installed=True)
    assert in_lane
    groups = dict(_OVERLAY, agent_sandbox_lane=["node-sbx"])
    assert _UNIT in _unit_set("_orphan_lanes_owned", "node-sbx", groups)
    assert _UNIT in _unit_set("_orphan_lanes_known", "node-sbx", groups)


# ── the overlay names none ────────────────────────────────────────────────


@pytest.mark.parametrize("declared", [[], None])
def test_overlay_naming_none_skips_cleanly(declared: list[str] | None) -> None:
    """agent_sandbox_hosts empty or absent: the lane is empty, the sweep
    finds nothing to own or flag — a clean no-op, not an error."""
    overlay: dict[str, list[str]] = {
        "linux": ["node-idle", "node-gw0"],
        "serving": ["node-idle"],
        "compute": ["node-idle"],
        "gateway": ["node-gw0"],
    }
    if declared is not None:
        overlay["agent_sandbox_hosts"] = declared
    for lane_play_ran in (True, False):
        bounced, flagged = _bounces(
            "node-idle", overlay, installed=False, lane_play_ran=lane_play_ran
        )
        assert not bounced and not flagged
        # nobody joins the lane — the lane play matches the anchor only
        assert not _joins("node-idle", overlay, installed=False)


def test_flagged_set_is_computed_from_known_not_owned() -> None:
    """The flag must key on _orphan_lanes_known (declared ∪ adopted), not
    _orphan_lanes_owned — or every scoped deploy would false-flag the
    overlay-named host as drift."""
    expr = _flagged_expr()
    assert "_orphan_lanes_known" in expr
    assert "_orphan_lanes_owned" not in expr
    flag = _task(_play(_REDEPLOY, _SWEEP_PLAY), _FLAG)
    assert "_orphan_lanes_flagged" in str(flag["when"])
    assert "_orphan_lanes_flagged" in str(flag["ansible.builtin.debug"]["msg"])

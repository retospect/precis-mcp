"""Deploy-lint: play 35's sandbox lane host selection (gr485861).

r35: ``agent_sandbox_hosts`` was empty (emptied 2026-09-10, lane dark), so
plays 34/35 logged "no hosts matched" while castor still ran an installed,
enabled ``precis-worker-sandbox.service`` on pre-deploy code. Play 35 now
opens with a selection play that puts every declared member, plus any Linux
host where the unit is installed, into ``agent_sandbox_lane``. The lane play
targets that group, bounces the unit and asserts the bounced process runs
the deployed build (the gr479965 check the compute lane uses).

The ``when`` expression of the ``group_by`` task is evaluated here with the
same Jinja the playbook ships, for each selection case.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2
import pytest
import yaml

_DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
_PLAYBOOK = _DEPLOY / "playbooks" / "35-precis-worker-sandbox.yml"
_SCRIPT = _DEPLOY / "playbooks" / "files" / "assert-unit-build.py"

_SELECT_PLAY = "Select the sandbox lane hosts (declared group + installed units)"
_LANE_PLAY = "Deploy the sandbox_run worker lane (agent_sandbox_lane)"
_PROBE = "Probe for an installed precis-worker-sandbox unit"
_JOIN = "Join the sandbox lane group"
_BOUNCE = "Bounce the sandbox lane on a code refresh (no step-2 coverage — gr485861)"
_RENDER = "gr485861: render the post-bounce build check"
_ASSERT = "gr485861: assert the bounced sandbox lane runs the deployed build"


def _plays() -> list[dict[str, Any]]:
    plays = yaml.safe_load(_PLAYBOOK.read_text(encoding="utf-8"))
    assert isinstance(plays, list)
    return plays


def _play(name: str) -> dict[str, Any]:
    found = [p for p in _plays() if p.get("name") == name]
    assert len(found) == 1, f"expected exactly one play named {name!r}"
    return found[0]


def _task(play: dict[str, Any], name: str) -> dict[str, Any]:
    found = [t for t in play.get("tasks", []) if t.get("name") == name]
    assert len(found) == 1, f"expected exactly one task named {name!r}"
    return found[0]


def _joins(
    *,
    host: str,
    declared: list[str],
    probe_stdout: str | None,
) -> bool:
    """Evaluate the ``group_by`` task's ``when`` for one host.

    ``probe_stdout=None`` is a skipped probe (darwin host, or adoption
    turned off): Ansible registers a result without ``stdout``.
    """
    when = _task(_play(_SELECT_PLAY), _JOIN)["when"]
    probe: dict[str, Any] = {"skipped": True}
    if probe_stdout is not None:
        probe = {"stdout": probe_stdout, "rc": 0}
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    rendered = env.from_string("{{ (" + when + ") | string }}").render(
        inventory_hostname=host,
        groups={"agent_sandbox_hosts": declared},
        _sb_unit_probe=probe,
    )
    return rendered == "True"


_INSTALLED = "precis-worker-sandbox.service enabled enabled\n"


@pytest.mark.parametrize(
    ("host", "declared", "probe_stdout", "expected"),
    [
        # r35: group emptied, unit still installed on castor -> adopted.
        ("castor", [], _INSTALLED, True),
        # Declared member joins whether or not the unit exists yet (first
        # provisioning run installs it).
        ("castor", ["castor"], "", True),
        ("node-gpu", ["node-gpu"], None, True),
        # Neither declared nor installed: never selected, so an
        # unprovisioned host cannot redden the deploy (the 2026-09-10
        # reason the group was emptied).
        ("pollux", [], "", False),
        # Adoption off / darwin: the probe is skipped, only the declared
        # group counts.
        ("castor", [], None, False),
    ],
)
def test_selection_cases(
    host: str, declared: list[str], probe_stdout: str | None, expected: bool
) -> None:
    assert _joins(host=host, declared=declared, probe_stdout=probe_stdout) is expected


def test_selection_play_runs_first_and_feeds_the_lane_play() -> None:
    names = [p.get("name") for p in _plays()]
    assert names.index(_SELECT_PLAY) < names.index(_LANE_PLAY)
    select = _play(_SELECT_PLAY)
    assert set(select["hosts"].split(":")) == {
        "linux",
        "agent_sandbox_hosts",
        "gateway",
    }
    assert _task(select, _JOIN)["ansible.builtin.group_by"]["key"] == (
        "agent_sandbox_lane"
    )
    assert set(_play(_LANE_PLAY)["hosts"].split(":")) == {
        "agent_sandbox_lane",
        "gateway",
    }


def test_both_plays_carry_the_gateway_isolation_anchor() -> None:
    """r36 (gr488897): a play whose whole batch fails ends the run, and
    neither batch here has a critical host — the selection play's hosts are
    all-linux (the gateway is macOS) and the lane can hold only adopted
    hosts. Each carries the gateway, which leaves in the first pre_task.
    The lane play keys end_host on agent_sandbox_lane — NOT the declared
    group — so an adopted host (installed unit, undeclared) stays in."""
    for name, lane_groups in (
        (_SELECT_PLAY, ("linux", "agent_sandbox_hosts")),
        (_LANE_PLAY, ("agent_sandbox_lane",)),
    ):
        play = _play(name)
        assert set(play["hosts"].split(":")) - {"gateway"} == set(lane_groups)
        anchor = play["pre_tasks"][0]
        assert anchor["ansible.builtin.meta"] == "end_host"
        assert anchor["name"].startswith("Isolation anchor")
        for group in lane_groups:
            assert f"groups['{group}']" in anchor["when"], (name, group)
        assert "groups['gateway']" not in anchor["when"]
    lane_anchor = _play(_LANE_PLAY)["pre_tasks"][0]
    assert "groups['agent_sandbox_hosts']" not in lane_anchor["when"], (
        "keying the lane play's anchor on the declared group would drop "
        "adopted hosts before their tasks run"
    )


def test_probe_checks_the_installed_unit_and_never_fails_the_play() -> None:
    probe = _task(_play(_SELECT_PLAY), _PROBE)
    assert probe["ansible.builtin.command"]["argv"] == [
        "systemctl",
        "list-unit-files",
        "--no-legend",
        "precis-worker-sandbox.service",
    ]
    assert probe["register"] == "_sb_unit_probe"
    assert probe["failed_when"] is False
    assert probe["changed_when"] is False
    assert probe["ignore_unreachable"] is True
    assert "agent_sandbox_adopt_installed | default(true) | bool" in probe["when"]


def test_lane_play_bounces_then_asserts_the_deployed_build() -> None:
    lane = _play(_LANE_PLAY)
    names = [t.get("name") for t in lane["tasks"]]
    assert names.index(_BOUNCE) < names.index(_RENDER) < names.index(_ASSERT)
    assert _task(lane, _BOUNCE)["register"] == "_sb_bounce"
    render = _task(lane, _RENDER)["ansible.builtin.copy"]
    assert render["src"] == _SCRIPT.name
    assert _SCRIPT.is_file()
    assert render["dest"] == "{{ _sb_build_check_script }}"
    check = _task(lane, _ASSERT)
    assert "_sb_bounce is not skipped" in check["when"]
    argv = check["ansible.builtin.command"]["argv"]
    assert argv[:3] == [
        "python3",
        "{{ _sb_build_check_script }}",
        "precis-worker-sandbox",
    ]
    assert "precis_worker_git_ref" in argv[3]
    assert check["until"] == "(_sb_build_check.rc | default(1)) == 0"
    assert int(check["retries"]) * int(check["delay"]) >= 120
    assert check["changed_when"] is False


def test_submit_allowlist_stays_on_the_declared_group() -> None:
    """Adopting an installed unit keeps it fresh; it must not add the host
    to PRECIS_SANDBOX_HOSTS (the submit allowlist), which stays Reto's
    declared group."""
    text = _PLAYBOOK.read_text(encoding="utf-8")
    env = text[text.index("'PRECIS_SANDBOX_HOSTS'") : text.index("'PRECIS_ROOT'")]
    assert "groups['agent_sandbox_hosts']" in env
    assert "groups['agent_sandbox_lane']" not in env

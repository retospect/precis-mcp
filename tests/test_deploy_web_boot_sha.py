"""Deploy-lint: a stale precis-web process fails or bounces the deploy (gr496849).

A resumed deploy once found every venv already on the target sha, bounced
nothing, and left com.precis.web serving the previous build's modules against
the new templates. The playbook now (1) probes the running web's boot sha in
the pre-flight and treats a mismatch as "changed" with at least web scope,
and (2) asserts the sha after the bounce. These tests pin that structure and
evaluate the stale-decision expression for real.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import jinja2
import pytest
import yaml

_REDEPLOY = Path(__file__).resolve().parents[1] / "deploy" / "redeploy-precis.yml"
_SHA = "a" * 40


def _plays() -> list[dict[str, Any]]:
    return yaml.safe_load(_REDEPLOY.read_text(encoding="utf-8"))


def _task(name_prefix: str) -> dict[str, Any]:
    found = [
        t
        for play in _plays()
        for t in play.get("tasks", [])
        if str(t.get("name", "")).startswith(name_prefix)
    ]
    assert len(found) == 1, f"expected exactly one task starting {name_prefix!r}"
    return found[0]


def _render(expr: str, **ctx: Any) -> str:
    env = jinja2.Environment()
    env.filters["bool"] = bool  # Ansible's filter; the inputs here are real bools
    env.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None
    return env.from_string(expr).render(**ctx).strip()


def _stale(probe: dict[str, Any], *, gateway: bool = True) -> bool:
    expr = _task("Decide whether the running precis-web is stale")[
        "ansible.builtin.set_fact"
    ]["precis_web_stale"]
    out = _render(
        expr,
        inventory_hostname="gw",
        groups={"gateway": ["gw"] if gateway else ["other"]},
        precis_target_sha=_SHA,
        precis_web_preflight_probe=probe,
    )
    return out == "True"


@pytest.mark.parametrize(
    ("probe", "gateway", "stale"),
    [
        ({"x_precis_boot_sha": _SHA}, True, False),
        ({"x_precis_boot_sha": "b" * 40}, True, True),
        ({"x_precis_boot_sha": "unknown"}, True, True),
        ({}, True, True),  # an older web that sends no header
        ({"failed": True, "status": -1}, True, True),  # no answer at all
        ({"x_precis_boot_sha": "b" * 40}, False, False),  # not a web host
    ],
)
def test_stale_decision(probe: dict[str, Any], gateway: bool, stale: bool) -> None:
    assert _stale(probe, gateway=gateway) is stale


def test_probe_is_gateway_only_and_never_fails_the_play() -> None:
    t = _task("Probe the running precis-web's boot sha")
    assert "groups['gateway']" in t["when"]
    assert t["failed_when"] is False
    assert "/healthz" in t["ansible.builtin.uri"]["url"]


def test_stale_web_forces_the_gate_and_raises_scope_none_to_web() -> None:
    gate = _task("Set the deploy-changed gate")["ansible.builtin.set_fact"][
        "precis_deploy_changed"
    ]
    assert "precis_web_stale" in gate
    # A stale web with every venv SAME: deploy_changed true despite no DIFF.
    out = _render(
        gate,
        precis_preflight={"results": [{"stdout": f"SAME {_SHA}"}]},
        precis_web_stale=True,
    )
    assert out == "True"
    out = _render(
        gate,
        precis_preflight={"results": [{"stdout": f"SAME {_SHA}"}]},
        precis_web_stale=False,
    )
    assert out == "False"

    raise_ = _task("Raise a 'none' bounce scope to 'web'")
    assert raise_["ansible.builtin.set_fact"] == {"precis_bounce_scope": "web"}
    conds = " ".join(raise_["when"])
    assert "precis_web_stale" in conds
    assert "== 'none'" in conds  # only 'none' is raised; 'full' is never narrowed


def test_post_bounce_assert_compares_the_pinned_sha_and_precedes_completion() -> None:
    plays = _plays()
    names = [p["name"] for p in plays]
    verify = next(
        i for i, n in enumerate(names) if n.startswith("Verify every managed")
    )
    done = next(i for i, n in enumerate(names) if n.startswith("Deploy complete"))
    assert verify < done
    tasks = [str(t.get("name", "")) for t in plays[verify]["tasks"]]
    assert any("Assert the running precis-web booted" in n for n in tasks)

    read = _task("Read the running precis-web's boot sha")
    assert read["retries"] >= 10  # rides out the launchd restart
    assert "precis_target_sha" in read["until"]
    assert read["failed_when"] is False  # the assert, not the poll, fails

    assert_ = _task("Assert the running precis-web booted")
    (that,) = assert_["ansible.builtin.assert"]["that"]
    assert "x_precis_boot_sha" in that and "precis_target_sha" in that

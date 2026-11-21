"""Deploy-lint: a host that is both the gateway and a builder host.

The gateway's deploy-run units (20b collapsed worker, 20g slice lane) and the
`<host>-coder` unit of 20i (roles/precis_builder_host) would both claim
`run_slice` there. Once the builder is proven, the host var
`precis_deploy_claims_run_slice: false` renders `run_slice` into the deploy
units' PRECIS_SUSPENDED_JOB_TYPES: the claim path skips a suspended type, and
the heartbeat probe stops advertising the `builder` floor. Every other
`repo_checkout` job type keeps claiming there. Pure YAML/Jinja walks, no
ansible needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2
import pytest
import yaml

_PLAYBOOKS = Path(__file__).resolve().parent.parent / "deploy" / "playbooks"
_20B = _PLAYBOOKS / "20b-precis-worker-collapsed.yml"
_20G = _PLAYBOOKS / "20g-precis-worker-slices.yml"

_HOLD_VARS = {"20b": "_l_b_suspended_job_types", "20g": "_slices_suspended_job_types"}


def _play(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))[0]


def _env() -> jinja2.Environment:
    env = jinja2.Environment()
    env.filters["bool"] = lambda v: v is True or str(v).lower() in ("true", "yes", "1")
    return env


def _hold(which: str, **ctx: Any) -> str:
    path = _20B if which == "20b" else _20G
    expr = _play(path)["vars"][_HOLD_VARS[which]]
    return _env().from_string(expr).render(**ctx).strip()


@pytest.mark.parametrize("which", ["20b", "20g"])
def test_default_renders_the_operator_hold_list_unchanged(which: str) -> None:
    assert _hold(which) == ""
    assert _hold(which, precis_suspended_job_types="") == ""
    assert (
        _hold(which, precis_suspended_job_types="autocatpath_seed, demo")
        == "autocatpath_seed,demo"
    )
    assert _hold(which, precis_deploy_claims_run_slice=True) == ""


@pytest.mark.parametrize("which", ["20b", "20g"])
def test_opting_out_adds_exactly_run_slice(which: str) -> None:
    assert _hold(which, precis_deploy_claims_run_slice=False) == "run_slice"
    assert _hold(which, precis_deploy_claims_run_slice="false") == "run_slice"
    assert (
        _hold(
            which,
            precis_suspended_job_types="autocatpath_seed",
            precis_deploy_claims_run_slice=False,
        )
        == "autocatpath_seed,run_slice"
    )
    # An operator hold that already names run_slice is not doubled.
    assert (
        _hold(
            which,
            precis_suspended_job_types="run_slice",
            precis_deploy_claims_run_slice=False,
        )
        == "run_slice"
    )


@pytest.mark.parametrize("which", ["20b", "20g"])
def test_the_hold_never_names_what_the_deploy_identity_must_keep(which: str) -> None:
    """release_*, ci_*, pin_bump, repo_release and land_slice rows pinned to
    the bare host keep draining on the deploy units."""
    held = _hold(which, precis_deploy_claims_run_slice=False).split(",")
    assert held == ["run_slice"]
    for kept in (
        "land_slice",
        "release_cut",
        "release_deploy",
        "release_confirm",
        "ci_red_digest",
        "ci_rerun",
        "pin_bump",
        "repo_release",
    ):
        assert kept not in held


def test_both_deploy_units_render_the_hold_into_their_env() -> None:
    b = _play(_20B)["vars"]["_l_b_base_env"]
    assert "'PRECIS_SUSPENDED_JOB_TYPES': _l_b_suspended_job_types" in b
    g_tasks = [
        t
        for t in _play(_20G)["tasks"]
        if t.get("ansible.builtin.import_role", {}).get("name") == "service_unit"
    ]
    env = g_tasks[0]["vars"]["service_unit_env"]
    assert "'PRECIS_SUSPENDED_JOB_TYPES': _slices_suspended_job_types" in env


def _guard(path: Path) -> dict[str, Any]:
    tasks = [t for t in _play(path)["tasks"] if "ansible.builtin.assert" in t]
    assert len(tasks) == 1
    return tasks[0]


@pytest.mark.parametrize("path", [_20B, _20G], ids=["20b", "20g"])
def test_opting_out_needs_a_builder_that_would_claim_the_builds(
    path: Path,
) -> None:
    """A managed builder user on this host, or another builder host: without
    either no unit claims run_slice anywhere, so the play is refused."""
    (cond,) = _guard(path)["ansible.builtin.assert"]["that"]
    check = _env().compile_expression(cond)
    groups = {"builder_hosts": ["gw"]}

    def ok(**ctx: Any) -> bool:
        return bool(check(inventory_hostname="gw", **ctx))

    assert ok(groups=groups)  # default: deploy keeps run_slice
    assert ok(groups={}, precis_deploy_claims_run_slice=True)
    assert ok(
        groups=groups, precis_deploy_claims_run_slice=False, precis_builder_user="coder"
    )
    assert not ok(groups=groups, precis_deploy_claims_run_slice=False)
    assert not ok(
        groups=groups,
        precis_deploy_claims_run_slice=False,
        precis_builder_user="deploy",
    )
    assert not ok(
        groups={},
        precis_deploy_claims_run_slice=False,
        precis_builder_user="coder",
    )
    # Another builder host takes the builds: this host needs no builder user.
    assert ok(groups={"builder_hosts": ["other"]}, precis_deploy_claims_run_slice=False)
    assert ok(
        groups={"builder_hosts": ["gw", "other"]},
        precis_deploy_claims_run_slice=False,
        precis_builder_user="deploy",
    )


def test_the_guard_runs_before_anything_is_rendered() -> None:
    for path in (_20B, _20G):
        first = _play(path)["tasks"][0]
        assert "ansible.builtin.assert" in first, path.name


def test_the_builder_unit_keeps_the_operator_hold_list_only() -> None:
    """The `<host>-coder` unit is the one that claims the builds: it must not
    inherit the deploy units' hold."""
    role = Path(__file__).resolve().parent.parent / "deploy" / "roles"
    text = (role / "precis_builder_host" / "tasks" / "main.yml").read_text(
        encoding="utf-8"
    )
    assert "'PRECIS_SUSPENDED_JOB_TYPES': precis_suspended_job_types | default('')" in (
        text
    )
    assert "precis_deploy_claims_run_slice" not in text

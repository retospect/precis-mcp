"""Deploy-lint: every lane that can claim `autocatpath_kinetics` carries the
same PRECIS_AUTOCATPATH_KINETICS_SECONDS, rendered from one group var.

The job is an ssh_node type with no node pin, so any `job_ssh_node` claimant
sizes its lease from the env at submit time (`kinetics_job._wall_seconds`)
and the detached child it spawns inherits the same env
(`runner._kinetics_timeout_s`). One lane missing the key falls back to the
900 s default and the lease and child budget disagree.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
_VAR = "precis_autocatpath_kinetics_seconds"
_ENV = "PRECIS_AUTOCATPATH_KINETICS_SECONDS"
_RENDER = f"'{_ENV}': {_VAR} | string"
_LANES = (
    "playbooks/20b-precis-worker-collapsed.yml",  # --profile all (carries ssh_node)
    "playbooks/20g-precis-worker-slices.yml",  # --only job_ssh_node, gateway
    "playbooks/43-precis-worker-compute.yml",  # --only job_ssh_node, compute
)


def test_group_var_is_the_single_source() -> None:
    gv = yaml.safe_load(
        (_DEPLOY / "group_vars" / "all.yml").read_text(encoding="utf-8")
    )
    assert gv[_VAR] == 6000


def test_every_claiming_lane_renders_env_from_var() -> None:
    for rel in _LANES:
        text = (_DEPLOY / rel).read_text(encoding="utf-8")
        assert text.count(_RENDER) == 1, rel


def test_no_other_deploy_file_hardcodes_the_env() -> None:
    """A new lane goes in `_LANES`, not a hand-set literal."""
    allowed = {_DEPLOY / rel for rel in _LANES}
    for path in _DEPLOY.rglob("*"):
        if not path.is_file() or path.suffix not in {".yml", ".yaml", ".j2"}:
            continue
        if path in allowed or path.name == "all.yml":
            continue
        assert _ENV not in path.read_text(encoding="utf-8"), path


def test_ssh_node_claimants_are_all_covered() -> None:
    """A playbook running `--only job_ssh_node` or `--profile all` is a lane."""
    for path in sorted((_DEPLOY / "playbooks").glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        execs = [ln for ln in text.splitlines() if "bin/precis worker" in ln]
        claims = any(
            "--only job_ssh_node" in ln or "--profile all" in ln for ln in execs
        )
        if claims:
            assert f"playbooks/{path.name}" in _LANES, path.name

"""The science lanes' env files carry PRECIS_CONTAINER_USER only when the
overlay sets ``science_container_user`` (cluster-fileserver-move, Phase 0
step 3): unset must render exactly today's env, set must reach the worker."""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest
import yaml

_ROLES = Path(__file__).resolve().parent.parent / "deploy" / "roles"
_SCIENCE_ROLES = ("dft", "alphafold", "aizynth")


def _render(role: str, **overrides: object) -> dict[str, str]:
    values = yaml.safe_load(
        (_ROLES / role / "defaults" / "main.yml").read_text(encoding="utf-8")
    )
    values.update(
        {f"{role}_node": "node-gpu", "ansible_managed": "test"},
        **overrides,
    )
    source = (_ROLES / role / "templates" / f"{role}.env.j2").read_text(
        encoding="utf-8"
    )
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    rendered = env.from_string(source).render(values)
    return dict(
        line.split("=", 1)
        for line in rendered.splitlines()
        if line and not line.startswith("#")
    )


@pytest.mark.parametrize("role", _SCIENCE_ROLES)
def test_unset_renders_no_container_user(role: str) -> None:
    assert "PRECIS_CONTAINER_USER" not in _render(role)
    assert "PRECIS_CONTAINER_USER" not in _render(role, science_container_user="")


@pytest.mark.parametrize("role", _SCIENCE_ROLES)
def test_set_reaches_the_worker_env(role: str) -> None:
    env = _render(role, science_container_user="1500:1500")
    assert env["PRECIS_CONTAINER_USER"] == "1500:1500"

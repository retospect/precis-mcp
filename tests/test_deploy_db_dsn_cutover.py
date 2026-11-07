"""Regression for the r235 deploy failure (td575694): commit b2383e574 (db-own)
pointed every pre-cutover DSN at ``precis_system``, a login role that does not
exist in prod until ``scripts/db-own-cutover`` runs and the overlay supplies its
password. The migrate-first play connected as it and died. Every DSN stays on
``agent_rw`` until ``precis_db_own_cutover_done`` is true.

The expressions are rendered (jinja2, ansible's ``bool`` filter stubbed) from the
real role defaults and playbook vars, so a rewrite that drops the guard fails here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jinja2
import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEPLOY = _REPO_ROOT / "deploy"

_HOSTVARS: dict[str, object] = {
    "postgres_host": "pg_primary",
    "hostvars": {"pg_primary": {"ansible_host": "203.0.113.10"}},
    "pgbouncer_port": 6432,
}

#: (file relative to deploy/, DSN variable) — each must follow the cutover flag.
_WORKER_DSNS = [
    ("roles/precis_worker/defaults/main.yml", "precis_worker_dsn"),
    ("roles/precis_worker_agent/defaults/main.yml", "precis_worker_agent_dsn"),
    ("playbooks/20d-precis-worker-drain.yml", "_drain_dsn"),
    ("playbooks/20e-precis-worker-agentlane.yml", "_agentlane_dsn"),
    ("playbooks/35-precis-worker-sandbox.yml", "_sb_dsn"),
    ("playbooks/43-precis-worker-compute.yml", "_compute_dsn"),
    ("run-fix-metadata.yml", "precis_worker_dsn"),
    ("run-migrate-refs.yml", "precis_worker_dsn"),
    ("run-reconcile.yml", "precis_worker_dsn"),
]


def _find_var(node: Any, name: str) -> str | None:
    """The first value bound to ``name`` anywhere in a parsed YAML tree."""
    if isinstance(node, dict):
        if name in node and isinstance(node[name], str):
            return node[name]
        for v in node.values():
            found = _find_var(v, name)
            if found is not None:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_var(v, name)
            if found is not None:
                return found
    return None


def _env() -> jinja2.Environment:
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    # ansible's `bool` filter (jinja2 core has none).
    env.filters["bool"] = lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"}
    env.filters["to_json"] = json.dumps
    return env


def _render_dsn(rel: str, name: str, **extra: object) -> str:
    """Render ``name`` from ``deploy/<rel>``; sibling defaults resolve by repeat."""
    tree = yaml.safe_load((_DEPLOY / rel).read_text(encoding="utf-8"))
    expr = _find_var(tree, name)
    assert expr is not None, f"{name} not found in {rel}"
    siblings = (
        {k: v for k, v in tree.items() if isinstance(v, (str, bool))}
        if (isinstance(tree, dict))
        else {}
    )
    ctx = {**siblings, **_HOSTVARS, **extra}
    out = expr
    for _ in range(5):  # nested defaults (precis_migrate_dsn -> *_db_user)
        out = _env().from_string(out).render(**ctx)
        if "{{" not in out:
            return out
    raise AssertionError(f"{name} did not render fully: {out!r}")


def _user(dsn: str) -> str:
    assert dsn.startswith("postgresql://"), dsn
    return dsn.removeprefix("postgresql://").split("@", 1)[0].split(":", 1)[0]


@pytest.mark.parametrize(("rel", "name"), _WORKER_DSNS)
def test_worker_dsns_stay_on_agent_rw_before_the_cutover(rel: str, name: str) -> None:
    # Flag unset (a play whose scope never loads the precis_web defaults) and
    # explicitly false must both give the pre-cutover role.
    assert _user(_render_dsn(rel, name)) == "agent_rw"
    assert _user(_render_dsn(rel, name, precis_db_own_cutover_done=False)) == "agent_rw"
    # Even with the vault var defined: the flag alone decides.
    assert (
        _user(
            _render_dsn(
                rel,
                name,
                precis_db_own_cutover_done=False,
                vault_pg_precis_system_pass="x",
            )
        )
        == "agent_rw"
    )


@pytest.mark.parametrize(("rel", "name"), _WORKER_DSNS)
def test_worker_dsns_switch_to_precis_system_after_the_cutover(
    rel: str, name: str
) -> None:
    got = _render_dsn(
        rel,
        name,
        precis_db_own_cutover_done=True,
        vault_pg_precis_system_pass="x",
    )
    assert _user(got) == "precis_system"


_WEB = "roles/precis_web/defaults/main.yml"


def test_migrate_and_web_dsns_stay_on_agent_rw_before_the_cutover() -> None:
    for extra in (
        {},
        {"precis_db_own_cutover_done": False},
        {
            "precis_db_own_cutover_done": False,
            "vault_pg_precis_owner_pass": "x",
            "vault_pg_precis_ingress_pass": "x",
        },
        # Flag true but the vault vars missing: still the pre-cutover role.
        {"precis_db_own_cutover_done": True},
    ):
        assert _user(_render_dsn(_WEB, "precis_migrate_dsn", **extra)) == "agent_rw"
        assert _user(_render_dsn(_WEB, "precis_web_dsn", **extra)) == "agent_rw"


def test_migrate_and_web_dsns_switch_after_the_cutover() -> None:
    extra = {
        "precis_db_own_cutover_done": True,
        "vault_pg_precis_owner_pass": "x",
        "vault_pg_precis_ingress_pass": "x",
    }
    assert _user(_render_dsn(_WEB, "precis_migrate_dsn", **extra)) == "precis_owner"
    assert _user(_render_dsn(_WEB, "precis_web_dsn", **extra)) == "precis_ingress"


_ASA_BOT = _DEPLOY / "roles" / "asa_bot" / "templates"


@pytest.mark.parametrize(
    "template", ["claude_mcp.json.j2", "config.yaml.j2", "com.asa.bot.plist.j2"]
)
def test_asa_bot_templates_stay_on_agent_rw_before_the_cutover(template: str) -> None:
    src = (_ASA_BOT / template).read_text(encoding="utf-8")
    ctx = {
        **_HOSTVARS,
        "postgres_port": 5432,
        "asa_common_env": {},
        "vault_pg_agent_rw_pass": "RW-PASS",
        "vault_pg_precis_system_pass": "SYS-PASS",
    }
    pre = _env().from_string(src).render(**ctx)
    assert "precis_system" not in pre
    assert "SYS-PASS" not in pre
    post = _env().from_string(src).render(**ctx, precis_db_own_cutover_done=True)
    if template != "com.asa.bot.plist.j2":
        assert "precis_system" in post
    assert "SYS-PASS" in post or template == "claude_mcp.json.j2"

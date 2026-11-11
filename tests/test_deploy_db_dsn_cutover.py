"""Regression for the r235 deploy failure (td575694): commit b2383e574 (db-own)
pointed every pre-cutover DSN at ``precis_system``, a login role that does not
exist in prod until ``scripts/db-own-cutover`` runs and the overlay supplies its
password. The migrate-first play connected as it and died. Default DSNs stay on
``agent_rw`` until ``precis_db_own_cutover_done`` is true. An explicit gateway
web identity survives narrower config-only plays; its credential-access probe
must pass before the working daemon configuration is replaced.

The expressions are rendered (jinja2, ansible's ``bool`` filter stubbed) from the
real role defaults and playbook vars, so a rewrite that drops the guard fails here.
"""

from __future__ import annotations

import ast
import json
import re
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


def _web_credential_preflight() -> dict[str, Any]:
    tasks = yaml.safe_load(
        (_DEPLOY / "roles/precis_web/tasks/main.yml").read_text(encoding="utf-8")
    )
    matches = [
        task
        for task in tasks
        if "PRECIS_WEB_PREFLIGHT_DSN" in task.get("environment", {})
    ]
    assert len(matches) == 1, "web deployment needs one credential-access preflight"
    return matches[0]


def _web_credential_preflight_sql() -> str:
    script = _web_credential_preflight()["ansible.builtin.command"]["argv"][2]
    statements = [
        ast.literal_eval(node.args[0])
        for node in ast.walk(ast.parse(script))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
    ]
    assert len(statements) == 1
    return str(statements[0])


@pytest.mark.parametrize("extra", [{}, {"precis_db_own_cutover_done": False}])
def test_explicit_gateway_web_identity_survives_missing_cutover_vars(
    extra: dict[str, object],
) -> None:
    from tests.test_deploy_public_mcp import _render

    # A gateway already using ingress must not silently fall back during a
    # config-only play that does not load the global cutover flag/passwords.
    web_dsn = _render_dsn(
        _WEB, "precis_web_dsn", precis_web_db_user="precis_ingress", **extra
    )
    public_dsn = _render_dsn(
        _WEB, "precis_mcp_public_dsn", precis_web_db_user="precis_ingress", **extra
    )
    assert _user(web_dsn) == "precis_ingress"
    assert _user(public_dsn) == "agent_rw"
    assert (
        _render(precis_web_dsn=web_dsn)["EnvironmentVariables"]["PRECIS_DATABASE_URL"]
        == web_dsn
    )
    public = _render(
        precis_web_dsn=web_dsn,
        precis_web_daemon_label="com.precis.mcp-public",
        precis_web_daemon_dsn=public_dsn,
    )
    assert public["EnvironmentVariables"]["PRECIS_DATABASE_URL"] == public_dsn


@pytest.mark.parametrize(
    "override", [None, "postgresql://precis_ingress@203.0.113.10:6432/custom_web"]
)
def test_credential_preflight_uses_the_same_dsn_as_the_web_daemon(
    override: str | None,
) -> None:
    from tests.test_deploy_public_mcp import _render

    context = {"precis_web_dsn": "postgresql://precis_ingress@db/precis_prod"}
    if override is not None:
        context["precis_web_daemon_dsn"] = override
    configured = _render(**context)["EnvironmentVariables"]["PRECIS_DATABASE_URL"]
    preflight = _web_credential_preflight()
    checked = (
        _env()
        .from_string(preflight["environment"]["PRECIS_WEB_PREFLIGHT_DSN"])
        .render(**context)
    )
    assert checked == configured


def test_web_auth_preflight_fails_closed_before_replacing_configuration() -> None:
    tasks = yaml.safe_load(
        (_DEPLOY / "roles/precis_web/tasks/main.yml").read_text(encoding="utf-8")
    )
    preflight = _web_credential_preflight()
    registered = preflight["register"]
    guard = next(
        task
        for task in tasks
        if registered in str(task.get("ansible.builtin.assert", {}).get("that", []))
    )
    template = next(
        task
        for task in tasks
        if task.get("ansible.builtin.template", {}).get("dest")
        == "/Library/LaunchDaemons/com.precis.web.plist"
    )
    for selected in (
        tasks,
        [task for task in tasks if "precis-web-config" in task.get("tags", [])],
    ):
        assert (
            selected.index(preflight) < selected.index(guard) < selected.index(template)
        )
    assert preflight["no_log"] is True
    assert preflight["become"] is True
    assert preflight["become_user"] == "deploy"
    assert preflight["changed_when"] is False
    assert preflight["failed_when"] is False
    assert preflight["check_mode"] is False
    assert preflight["environment"]["HOME"] == "/Users/deploy"
    for task in (preflight, guard):
        condition = _env().compile_expression(task["when"])
        assert condition(os_family="darwin")
        assert not condition(os_family="linux")
    conditions = guard["ansible.builtin.assert"]["that"]
    for result, expected in (({"rc": 0}, True), ({"rc": 1}, False), ({}, False)):
        assert (
            all(
                _env().compile_expression(expression)(**{registered: result})
                for expression in conditions
            )
            is expected
        )
    message = guard["ansible.builtin.assert"]["fail_msg"]
    assert "precis_web_db_user=precis_ingress" in message
    assert "Do not grant credential access to agent_rw" in message


def test_web_auth_preflight_reads_no_credential_rows_and_has_no_output() -> None:
    script = _web_credential_preflight()["ansible.builtin.command"]["argv"][2]
    tree = ast.parse(script)
    sql = " ".join(_web_credential_preflight_sql().split())
    assert sql == (
        "SELECT password_hash, password_salt, password_algo "
        "FROM public.web_users LIMIT 0"
    )
    connect = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "connect"
    )
    options = {
        keyword.arg: ast.literal_eval(keyword.value) for keyword in connect.keywords
    }
    assert "options" not in options  # PgBouncer rejects unsupported startup options.
    assert 0 < options["connect_timeout"] <= 10
    connection_block = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.With) and node.items[0].context_expr is connect
    )
    binding = connection_block.items[0].optional_vars
    assert isinstance(binding, ast.Name)
    read_only = connection_block.body[0]
    assert isinstance(read_only, ast.Assign)
    assert len(read_only.targets) == 1
    target = read_only.targets[0]
    assert isinstance(target, ast.Attribute)
    assert isinstance(target.value, ast.Name) and target.value.id == binding.id
    assert target.attr == "read_only"
    assert ast.literal_eval(read_only.value) is True
    # Even a successful credential-access check must never emit credential data.
    assert not any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "print")
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"fetchone", "fetchall", "fetchmany", "write"}
            )
        )
        for node in ast.walk(tree)
    )


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


def test_asa_common_env_dsn_user_follows_the_cutover() -> None:
    """gr605686: the launchd env DSN both chat bridges export is the
    last-resort fallback for every unthreaded in-process read
    (claude_invoke._attribution_store, secrets.reveal_secret) — its user
    must follow the db-own cutover like the worker DSNs, not stay on
    agent_rw, which cannot reveal infra-class vault secrets."""
    rel = "roles/asa_common/defaults/main.yml"
    assert _user(_render_dsn(rel, "PRECIS_DATABASE_URL")) == "agent_rw"
    assert (
        _user(_render_dsn(rel, "PRECIS_DATABASE_URL", precis_db_own_cutover_done=True))
        == "precis_system"
    )


def test_asa_bot_plist_notify_dsn_user_matches_its_password() -> None:
    src = (_ASA_BOT / "com.asa.bot.plist.j2").read_text(encoding="utf-8")
    ctx = {
        **_HOSTVARS,
        "postgres_port": 5432,
        "asa_common_env": {},
        "vault_pg_agent_rw_pass": "RW-PASS",
        "vault_pg_precis_system_pass": "SYS-PASS",
    }
    pat = re.compile(r"postgresql://([^:@]+):([^@]+)@127\.0\.0\.1:5433/")
    pre = pat.search(_env().from_string(src).render(**ctx))
    post = pat.search(
        _env().from_string(src).render(**ctx, precis_db_own_cutover_done=True)
    )
    assert pre is not None and pre.groups() == ("agent_rw", "RW-PASS")
    assert post is not None and post.groups() == ("precis_system", "SYS-PASS")

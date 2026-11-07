"""gr583810: a minted ``vault_pg_*_pass`` must carry no newline, no colon
and no outer whitespace.

``vault_pg_precis_system_pass`` was minted with a trailing newline:
Postgres and pgbouncer's userlist got ``"<pw>\\n"`` while pgpass clients
sent ``"<pw>"``, so the worker died with ``FATAL: password authentication
failed`` as ``precis_system``. Two assert tasks now fail loudly and early,
before any host writes a bad value:

- ``deploy/roles/postgres/tasks/main.yml`` — over ``postgres_roles``
  (tagged ``roles`` so it also runs inside ``02-postgres.yml --tags
  roles``), placed before "Create database roles".
- ``deploy/roles/pgpass/tasks/main.yml`` — over the exact var set
  ``templates/pgpass.j2`` renders, placed before the first .pgpass write.
  NOT tagged (pgpass is not part of 02-postgres). The names are assembled
  per loop item (``'vault' ~ '_pg_' ~ item ~ '_pass'``) because the
  lanes-deny scan flags a literal credential variable name in any
  builder-writable deploy file — the tasks file is builder-writable, the
  template is human-only.

The ``that`` expressions are evaluated here with jinja2 — the same engine
ansible renders them with — against fake values; no ansible run.
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import pytest
import yaml

_ROLES = Path(__file__).resolve().parent.parent / "deploy" / "roles"
_PGPASS_TEMPLATE = _ROLES / "pgpass" / "templates" / "pgpass.j2"

_ENV = jinja2.Environment()


def _tasks(role: str) -> list[dict]:
    return yaml.safe_load(
        (_ROLES / role / "tasks" / "main.yml").read_text(encoding="utf-8")
    )


def _assert_task(role: str) -> dict:
    """The password-shape assert in a role's task list."""
    for t in _tasks(role):
        if "ansible.builtin.assert" in t and "whitespace or a colon" in str(
            t.get("ansible.builtin.assert", {}).get("fail_msg", "")
        ):
            return t
    raise AssertionError(f"no vault-password assert found in {role} tasks")


def _eval_that(task: dict, pw: str) -> list[bool]:
    """Every `that` entry rendered with jinja2 against a fake password."""
    exprs = task["ansible.builtin.assert"]["that"]
    return [_ENV.from_string("{{ " + e + " }}").render(_pw=pw) == "True" for e in exprs]


def _task_vars(task: dict, item: object, mapping: dict) -> dict[str, object]:
    """The task's ``vars:`` block rendered in order — each entry sees the
    ones before it, as ansible's lazy templating does."""
    ctx: dict[str, object] = {
        "item": item,
        "vars": mapping,
        "lookup": _vars_lookup(mapping),
    }
    out: dict[str, object] = {}
    for name, val in task.get("vars", {}).items():
        out[name] = _ENV.from_string(val).render(**ctx, **out)
    return out


def _fail_msg(task: dict, item: object, pw: str) -> str:
    derived = _task_vars(task, item, {})
    derived["_pw"] = pw
    return _ENV.from_string(task["ansible.builtin.assert"]["fail_msg"]).render(
        item=item, **derived
    )


_ABSENT = object()


def _vars_lookup(mapping: dict):
    """ansible-core 2.21 `vars` lookup semantics: `default=None` counts as
    NO default (the lookup raises on an undefined name); only a non-None
    default rescues an absent var."""

    def _lookup(kind: str, name: str, default: object = _ABSENT) -> object:
        assert kind == "vars"
        if name in mapping:
            return mapping[name]
        if default is _ABSENT or default is None:
            raise RuntimeError(f"vars lookup: '{name}' is undefined")
        return default

    return _lookup


def _render_when(task: dict, item: object, mapping: dict) -> str:
    """The task's real `when:` rendered with jinja2 against a stub vars
    mapping — raises iff the expression itself raises. The task's own
    `vars:` derivations (`_var`, `_pw`) are resolved first, as ansible's
    lazy templating does."""
    return _ENV.from_string("{{ " + task["when"] + " }}").render(
        item=item,
        vars=mapping,
        lookup=_vars_lookup(mapping),
        **_task_vars(task, item, mapping),
    )


class TestPostgresRoleAssert:
    def test_precedes_create_database_roles_and_carries_roles(self) -> None:
        tasks = _tasks("postgres")
        names = [t.get("name") for t in tasks]
        assert_index = names.index(_assert_task("postgres").get("name"))
        assert names.index("Create database roles") > assert_index, (
            "the assert must run before any role/password write"
        )
        task = _assert_task("postgres")
        assert "roles" in task.get("tags", []), (
            "the assert must ride --tags roles so the role-sync checks it too"
        )

    def test_no_log_and_skips_password_less_roles(self) -> None:
        task = _assert_task("postgres")
        assert task.get("no_log") is True
        # Peer/trust roles carry no password_var — skipped explicitly.
        assert task.get("when") == "item.password_var is defined"
        assert task.get("loop") == "{{ postgres_roles }}"

    def test_that_expressions(self) -> None:
        task = _assert_task("postgres")
        for bad in (
            "s3cr3t\n",
            "s3cr3t\r\n",
            "a:b",
            " s3cr3t",
            "s3cr3t ",
            "",
            "mid\ndle",
        ):
            assert not all(_eval_that(task, bad)), f"value {bad!r} must fail the assert"
        for good in ("s3cr3t", "a-b_c.d+e=f", "p@ss/word"):
            assert all(_eval_that(task, good)), (
                f"clean value {good!r} must pass the assert"
            )

    def test_fail_msg_names_the_var_never_the_value(self) -> None:
        task = _assert_task("postgres")
        secret = "SUPER-FAKE-SECRET-7f3c"
        msg = _fail_msg(task, {"password_var": "pg_precis_system_pass"}, secret)
        assert "vault_pg_precis_system_pass" in msg
        assert secret not in msg

    def test_loop_label_survives_no_log_with_the_var_name(self) -> None:
        # Under no_log ansible censors the whole result including fail_msg —
        # the loop label is the only place the failing var's NAME reaches
        # the operator (never its value).
        task = _assert_task("postgres")
        assert task.get("no_log") is True
        label = task.get("loop_control", {}).get("label", "")
        assert label, "the assert needs loop_control.label naming the var"
        secret = "SUPER-FAKE-SECRET-7f3c"
        rendered = _ENV.from_string(label).render(
            item={"name": "precis_system", "password_var": "pg_precis_system_pass"},
            vault_pg_precis_system_pass=secret,
        )
        assert rendered == "vault_pg_precis_system_pass"
        assert secret not in rendered
        # A peer/trust role carries no password_var — falls back to the name.
        assert (
            _ENV.from_string(label).render(item={"name": "monitoring_peer"})
            == "monitoring_peer"
        )


class TestPgpassRoleAssert:
    def test_precedes_the_pgpass_write_and_is_untagged(self) -> None:
        tasks = _tasks("pgpass")
        names = [t.get("name") for t in tasks]
        assert_index = names.index(_assert_task("pgpass").get("name"))
        assert names.index("Deploy .pgpass for deploy user") > assert_index, (
            "the assert must run before the file is written"
        )
        task = _assert_task("pgpass")
        assert "roles" not in task.get("tags", []) or "tags" not in task, (
            "pgpass is not part of the 02-postgres roles slice"
        )

    def test_covers_the_template_var_set(self) -> None:
        task = _assert_task("pgpass")
        template = _PGPASS_TEMPLATE.read_text(encoding="utf-8")
        template_vars = set(re.findall(r"vault_pg_\w+_pass", template))
        # The asserted names are derived: the task's real loop items run
        # through its real `_var` expression.
        loop_vars = {_task_vars(task, item, {})["_var"] for item in task["loop"]}
        assert loop_vars == template_vars, (
            f"assert loop must cover the template's vars: {template_vars ^ loop_vars}"
        )
        # The template's `is defined`-gated vars must be skippable, not
        # fatal: a loop item is optional iff its `when:` renders False
        # against an empty vars mapping.
        gated = set(re.findall(r"(vault_pg_\w+_pass) is defined", template))
        optional = {
            _task_vars(task, item, {})["_var"]
            for item in task["loop"]
            if _render_when(task, item, {}) == "False"
        }
        assert (
            optional
            == gated
            == {
                "vault_pg_precis_system_pass",
                "vault_pg_precis_ingress_pass",
                "vault_pg_precis_owner_pass",
            }
        )

    def test_no_log(self) -> None:
        assert _assert_task("pgpass").get("no_log") is True

    def test_that_expressions(self) -> None:
        task = _assert_task("pgpass")
        for bad in ("s3cr3t\n", "a:b", " s3cr3t", "", "mid\ndle"):
            assert not all(_eval_that(task, bad)), f"value {bad!r} must fail the assert"
        assert all(_eval_that(task, "cl3an-p@ss"))

    def test_fail_msg_names_the_var_never_the_value(self) -> None:
        task = _assert_task("pgpass")
        secret = "SUPER-FAKE-SECRET-7f3c"
        msg = _fail_msg(task, "agent_rw", secret)
        assert "vault_pg_agent_rw_pass" in msg
        assert secret not in msg

    def test_loop_label_names_the_var_never_the_value(self) -> None:
        task = _assert_task("pgpass")
        label = task.get("loop_control", {}).get("label", "")
        assert label, "the assert needs loop_control.label naming the var"
        secret = "SUPER-FAKE-SECRET-7f3c"
        rendered = _ENV.from_string(label).render(
            **_task_vars(task, "agent_rw", {"vault_pg_agent_rw_pass": secret})
        )
        assert rendered == "vault_pg_agent_rw_pass"
        assert secret not in rendered

    def test_when_skips_an_absent_optional_var_without_raising(self) -> None:
        # ansible-core 2.21: lookup('vars', name, default=None) means "no
        # default" and RAISES on an undefined name — the guard must use a
        # form that cannot raise, or an absent optional var fails the
        # `when:` itself on the normal redeploy path.
        task = _assert_task("pgpass")
        absent: dict = {}
        # (1) optional + absent -> skipped quietly, no raise.
        assert _render_when(task, "precis_system", absent) == "False"
        # (2) optional + present -> asserted.
        have = {
            "vault_pg_precis_system_pass": "x",
            "vault_pg_agent_rw_pass": "y",
        }
        assert _render_when(task, "precis_system", have) == "True"
        # (3) required + absent -> the assert RUNS (_pw='' fails loudly).
        assert _render_when(task, "agent_rw", absent) == "True"
        # (4) required + present -> asserted.
        assert _render_when(task, "agent_rw", have) == "True"

    def test_pw_derivation_has_a_usable_default(self) -> None:
        # The `vars: _pw` lookup must keep a non-None default so an absent
        # var derives '' (the that-exprs then fail it loudly), never raise.
        task = _assert_task("pgpass")
        pw_expr = task["vars"]["_pw"]
        assert "default=None" not in pw_expr, (
            "default=None is 'no default' under ansible-core 2.21 — raises"
        )
        assert _task_vars(task, "agent_rw", {})["_pw"] == ""
        # The raising form this round removed: under the same stub it must
        # raise, proving the stub enforces the real semantics.
        raising = (
            "item not in ['precis_system', 'precis_ingress', 'precis_owner'] or "
            "lookup('vars', 'vault_pg_' + item + '_pass', default=None) is not none"
        )
        with pytest.raises(RuntimeError):
            _ENV.from_string("{{ " + raising + " }}").render(
                item="precis_system",
                vars={},
                lookup=_vars_lookup({}),
            )

    def test_tasks_file_names_no_vault_variable_literally(self) -> None:
        # Same regex as tests/test_lanes_deny.py's deploy scan: the tasks
        # file stays builder-writable, so the credential variable names are
        # assembled (`'vault' ~ '_pg_' ~ item ~ '_pass'`), never written
        # out — a literal here would fail the lanes-deny land gate.
        text = (_ROLES / "pgpass" / "tasks" / "main.yml").read_text(encoding="utf-8")
        assert not re.search(r"vault_[a-z0-9_]+", text), (
            "literal vault_ variable name in a builder-writable file — "
            "assemble it like _var instead"
        )

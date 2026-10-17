"""db_exporters (gr492114): the DSNs reach the DB node as a root-only env file.

The DB node has no precis CLI and gets none, so Ansible resolves the two DSNs
from vault vars on the control side and renders them into a 0600 root file the
launchers read. Nothing may read a DSN through the CLI, every task that
touches a DSN value is ``no_log``, and no secret is inlined in the launcher
or the launchd unit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import jinja2
import pytest
import yaml

_ROLE = Path(__file__).resolve().parent.parent / "deploy" / "roles" / "db_exporters"
_SENTINEL_PG = "postgresql://SENTINEL-PG@localhost:5432/postgres"
_SENTINEL_BOUNCER = "postgres://SENTINEL-BOUNCER@localhost:6432/pgbouncer"


def _read(*parts: str) -> str:
    return _ROLE.joinpath(*parts).read_text(encoding="utf-8")


def _defaults() -> dict:
    return yaml.safe_load(_read("defaults", "main.yml"))


def _tasks() -> list[dict]:
    return yaml.safe_load(_read("tasks", "main.yml"))


def _render(name: str, **over: object) -> str:
    d = _defaults()
    vars_ = {
        "ansible_managed": "x",
        **d,
        "db_exporters_env_file": d["db_exporters_env_dir"] + "/db-exporters.env",
        "vault_pg_exporter_dsn": _SENTINEL_PG,
        "vault_pgbouncer_exporter_dsn": _SENTINEL_BOUNCER,
        **over,
    }
    env = jinja2.Environment(
        undefined=jinja2.StrictUndefined, keep_trailing_newline=True
    )
    env.globals["lookup"] = lambda _plugin, key: vars_[key]
    return env.from_string(_read("templates", name)).render(**vars_)


def _env_task() -> dict:
    (task,) = [
        t
        for t in _tasks()
        if t.get("ansible.builtin.template", {}).get("src") == "db-exporters.env.j2"
    ]
    return task


def test_env_file_is_owner_only_0600() -> None:
    mod = _env_task()["ansible.builtin.template"]
    assert mod["owner"] == "{{ db_exporters_run_as }}"
    assert mod["mode"] == "0600"
    assert mod["dest"] == "{{ db_exporters_env_file }}"
    assert _defaults()["db_exporters_env_file"].startswith(
        "{{ db_exporters_env_dir }}/"
    )


def test_env_file_carries_both_dsns_from_vault_vars() -> None:
    d = _defaults()
    assert d["db_exporters_pg_dsn_var"].startswith("vault_")
    assert d["db_exporters_pgbouncer_dsn_var"].startswith("vault_")
    body = _render("db-exporters.env.j2")
    assert f"DATA_SOURCE_NAME={_SENTINEL_PG}\n" in body
    assert f"PGBOUNCER_EXPORTER_CONNECTION_STRING={_SENTINEL_BOUNCER}\n" in body


def test_dsn_tasks_are_no_log() -> None:
    assert _env_task().get("no_log") is True
    for task in _tasks():
        blob = yaml.safe_dump(task)
        if "vault_" in blob or "dsn_var" in blob or "db-exporters.env" in blob:
            assert task.get("no_log") is True, task["name"]


def test_no_dependency_on_the_precis_cli() -> None:
    for path in _ROLE.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert "precis secret" not in text, path
            assert "precis_bin" not in text, path
            assert "bin/precis" not in text, path


def test_launcher_reads_the_env_file_and_inlines_no_secret() -> None:
    d = _defaults()
    for dsn_env in ("DATA_SOURCE_NAME", "PGBOUNCER_EXPORTER_CONNECTION_STRING"):
        out = _render(
            "launcher.sh.j2",
            db_exporter={"binary": "x_exporter", "port": 1, "dsn_env": dsn_env},
        )
        assert d["db_exporters_env_dir"] + "/db-exporters.env" in out
        assert f'"{dsn_env}="*)' in out
        assert "SENTINEL" not in out
        assert "://" not in out


def test_launchd_unit_has_no_inline_secrets_and_runs_as_the_file_owner() -> None:
    d = _defaults()
    assert d["db_exporters_run_as"] == "deploy"  # owns the 0600 file
    (inc,) = [
        t
        for t in _tasks()
        if t.get("ansible.builtin.include_role", {}).get("name") == "service_unit"
    ]
    # The unit's environment is empty: no shared API keys, no DSN.
    assert inc["vars"]["service_unit_env"] == {}
    assert inc["vars"]["service_unit_exec"].endswith("-launch")
    assert "dsn" not in yaml.safe_dump(inc["vars"]).lower()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: runs bash")
def test_launcher_runs_and_keeps_dsn_with_equals_and_percent(tmp_path: Path) -> None:
    import subprocess

    dsn = "host=localhost dbname=pgbouncer sslmode=disable x=a%3Db"  # '=' and '%'
    fake = tmp_path / "fake_exporter"
    fake.write_text('#!/bin/bash\nprintf %s "$DATA_SOURCE_NAME"\n', encoding="utf-8")
    fake.chmod(0o755)
    envf = tmp_path / "db-exporters.env"
    envf.write_text(f"OTHER=1\nDATA_SOURCE_NAME={dsn}", encoding="utf-8")  # no newline
    script = _render(
        "launcher.sh.j2",
        db_exporter={
            "binary": "fake_exporter",
            "port": 1,
            "dsn_env": "DATA_SOURCE_NAME",
        },
        db_exporters_bin_dir=str(tmp_path),
        db_exporters_env_file=str(envf),
        db_exporters_listen_host="",
    )
    path = tmp_path / "launch.sh"
    path.write_text(script, encoding="utf-8")
    out = subprocess.run(
        ["/bin/bash", str(path)],
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    assert out.stdout == dsn


def test_dsn_precheck_precedes_env_render_and_is_no_log() -> None:
    names = [t["name"] for t in _tasks()]
    pre = names.index("DSN vault vars are set and not placeholders")
    assert pre < names.index(_env_task()["name"])
    assert _tasks()[pre]["no_log"] is True
    assert _env_task()["diff"] is False


def test_service_unit_darwin_plist_group_never_root() -> None:
    """macOS has no `root` group; a root-run unit must chgrp to wheel."""
    path = _ROLE.parent / "service_unit" / "tasks" / "main.yml"
    tasks = yaml.safe_load(path.read_text(encoding="utf-8"))
    (task,) = [t for t in tasks if "render LaunchDaemon" in t.get("name", "")]
    group = task["ansible.builtin.template"]["group"]
    env = jinja2.Environment()
    for run_as, want in (("root", "wheel"), ("precis", "precis")):
        assert env.from_string(group).render(service_unit_run_as=run_as) == want

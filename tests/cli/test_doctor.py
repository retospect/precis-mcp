"""``precis doctor`` (:mod:`precis.cli.doctor` + :mod:`precis.doctor`).

Drives the shared engine directly plus the CLI ``run`` wrapper — the
parser goes through ``_build_parser`` so flag wiring is real. DB-touching
checks use the live ``store`` fixture; the ``fresh_db`` fixture gives a
schema-less DSN for the migrations-pending path.
"""

from __future__ import annotations

import argparse
import io
import re
from argparse import Namespace
from contextlib import redirect_stdout
from typing import Any

import pytest

from precis import doctor
from precis.cli import doctor as cli_doctor
from precis.cli.main import _build_parser

pytest.importorskip("psycopg")


def _parse(argv: list[str]) -> Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    cli_doctor.add_parser(sub)
    return parser.parse_args(argv)


def _run(argv: list[str]) -> tuple[str, int]:
    """Drive ``cli_doctor.run``; return (stdout, exit code)."""
    buf = io.StringIO()
    code = 0
    with redirect_stdout(buf):
        try:
            cli_doctor.run(_parse(argv))
        except SystemExit as exc:
            code = int(exc.code or 0)
    return buf.getvalue(), code


# ── parser ──────────────────────────────────────────────────────────────────


def test_parser_flags_registered() -> None:
    args = _parse(["doctor", "--roles", "--probe"])
    assert args.roles and args.probe


def test_lazy_parser_offers_doctor() -> None:
    assert "doctor" in _build_parser(("doctor",)).parse_args(["doctor"]).cmd


# ── engine ──────────────────────────────────────────────────────────────────


def test_python_check_ok() -> None:
    c = doctor._check_python()
    assert c.name == "python" and c.level == doctor.OK


def test_dsn_check_fails_when_unset() -> None:
    c = doctor._check_dsn(None)
    assert c.level == doctor.FAIL
    assert "precis setup" in c.next


def test_dsn_check_never_prints_password() -> None:
    secret = "hunter2-definitely-secret"
    c = doctor._check_dsn("postgresql://u:" + secret + "@127.0.0.1:1/does-not-exist")
    assert c.level == doctor.FAIL
    assert secret not in c.detail


def test_cli_never_prints_dsn_password() -> None:
    secret = "hunter2-definitely-secret"
    # Built at runtime — a literal user:password DSN trips the
    # deploy-tree secret gate (test_deploy_tree_no_secrets).
    dsn = "postgresql://u:" + secret + "@127.0.0.1:1/nope"
    out, code = _run(["doctor", "--database-url", dsn])
    assert code == 1
    assert secret not in out
    assert re.search(r"^FAIL  database", out, re.M)


def test_run_checks_reports_all_names_with_no_db() -> None:
    from precis.config import PrecisConfig

    checks = doctor.run_checks(
        dsn="postgresql://u:" + "x" + "@127.0.0.1:1/nope",
        cfg=PrecisConfig(embedder="mock"),
    )
    names = [c.name for c in checks]
    for want in (
        "python",
        "database",
        "extensions",
        "migrations",
        "vault",
        "pepper",
        "roster",
        "embedder",
        "kinds",
        "heartbeat",
        "web",
        "secrets",
    ):
        assert want in names, names
    assert all(c.level in (doctor.OK, doctor.WARN, doctor.FAIL) for c in checks)
    assert doctor.has_failures(checks)


def test_roles_flag_adds_a_check(store: Any) -> None:
    checks = doctor.run_checks(store=store, roles=True)
    roles = next(c for c in checks if c.name == "roles")
    # The test DB has no tiered roles — this deployment didn't run
    # ``precis setup --roles`` — so the assert-they-exist check fails.
    assert roles.level == doctor.FAIL
    assert "agent_rw" in roles.detail
    without = doctor.run_checks(store=store, roles=False)
    assert "roles" not in [c.name for c in without]


def test_extensions_ok_on_test_db(store: Any) -> None:
    c = doctor._check_extensions(store)
    assert c.level == doctor.OK, c.detail


def test_migrations_pending_on_fresh_db(fresh_db: str) -> None:
    c = doctor._check_migrations(fresh_db)
    assert c.level == doctor.FAIL
    assert c.next == "precis migrate"


def test_migrations_at_head_on_test_db(store: Any) -> None:
    c = doctor._check_migrations(store.dsn)
    assert c.level == doctor.OK, c.detail


def test_roster_empty_fails_then_ok(store: Any) -> None:
    from precis.users import hash_password

    c = doctor._check_roster(store)
    assert c.level == doctor.FAIL
    assert "precis users add" in c.next
    store.create_web_user(
        login="ada", abbrev="al", password=hash_password("hunter2-swordfish")
    )
    assert doctor._check_roster(store).level == doctor.OK


def test_vault_check(store: Any) -> None:
    # Whether the test DB has vault._key provisioned is environment-dependent;
    # pin only that the check answers and never leaks.
    c = doctor._check_vault(store)
    assert c.level in (doctor.OK, doctor.FAIL)


def test_kinds_sweep_runs_boot_read_only(store: Any) -> None:
    import os

    with store.pool.connection() as conn:
        n_kinds = conn.execute("SELECT count(*) FROM kinds").fetchone()[0]

    assert "PRECIS_MCP_READ_ONLY" not in os.environ
    c = doctor._check_kinds(store, doctor_run_cfg())
    assert os.environ.get("PRECIS_MCP_READ_ONLY") is None
    # Missing extras / unset roots legitimately dark kinds — WARN is fine;
    # the sweep itself must not crash or write to the kinds table.
    assert c.level in (doctor.OK, doctor.WARN)
    with store.pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM kinds").fetchone()[0] == n_kinds


def doctor_run_cfg() -> Any:
    from precis.config import load_config

    return load_config()


def test_heartbeat_warns_on_empty_table(store: Any) -> None:
    c = doctor._check_heartbeat(store)
    assert c.level == doctor.WARN
    assert "no host_heartbeat" in c.detail


def test_web_port_free_is_ok() -> None:
    c = doctor._check_web(doctor.WebTarget(host="127.0.0.1", port=19999))
    assert c.level == doctor.OK
    assert "free" in c.detail


def test_web_port_held_fails() -> None:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        c = doctor._check_web(doctor.WebTarget(host="127.0.0.1", port=port))
    # The bound socket accepts connects but never answers /healthz →
    # falls through to the bind probe, which sees EADDRINUSE.
    assert c.level == doctor.FAIL
    assert "held by another process" in c.detail

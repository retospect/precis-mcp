"""db-own smoke — the daemons' real paths after the cutover, one per role.

Usage (any DB the cutover ran on; the host/db part of the DSNs is shared)::

    uv run --no-sync python scripts/db-own/smoke.py postgresql://HOST:PORT/DB

Connects as agent_rw, precis_ingress and precis_owner (``user=`` swapped into
the given DSN; the drill/dev servers use trust auth) and checks, on each, the
work that role does in production — MCP verbs through the dispatch path, the
vault through precis.secrets, the roster CLI, migrate, VACUUM — plus the
refusals the cutover exists for. Exit 0 = all green; prints one line per check
with its wall time. Writes a few rows (a memory, a vault name, a web user)
into the target DB: run it on a drill or dev DB, never on prod.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable

import psycopg
from psycopg.conninfo import make_conninfo

_FAILS: list[str] = []


def _dsn(base: str, user: str) -> str:
    return make_conninfo(base, user=user)


def _check(name: str, fn: Callable[[], object]) -> None:
    t0 = time.monotonic()
    try:
        fn()
    except Exception as exc:  # the smoke reports every failure
        _FAILS.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}"[:400])
        return
    print(f"ok   {name} ({(time.monotonic() - t0) * 1000:.0f} ms)")


def _cli(dsn: str, *argv: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["precis", *argv],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={"PRECIS_DATABASE_URL": dsn, "PATH": "/opt/venv/bin:/usr/bin:/bin"},
        check=False,
        timeout=300,
    )


def _expect_denied(dsn: str, stmt: str) -> None:
    with psycopg.connect(dsn, connect_timeout=15) as conn:
        try:
            conn.execute(stmt)
        except psycopg.errors.InsufficientPrivilege:
            return
    raise AssertionError(f"not refused: {stmt}")


def main(base: str) -> int:
    from precis import secrets
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.runtime import PrecisRuntime
    from precis.store import Store

    rw = _dsn(base, "agent_rw")
    ing = _dsn(base, "precis_ingress")
    own = _dsn(base, "precis_owner")

    store = Store.connect(rw)
    rt = PrecisRuntime(config=PrecisConfig(), hub=boot(store=store))

    def put_get_search() -> None:
        out = rt.dispatch("put", {"kind": "memory", "text": "db-own smoke probe"})
        assert "error" not in out.lower()[:80], out[:200]
        out = rt.dispatch("search", {"kind": "memory", "q": "db-own smoke probe"})
        assert "smoke" in out, out[:200]

    def vault_roundtrip() -> None:
        secrets.set_secret("DBOWN_SMOKE", "v-smoke-1234567890", store=store)
        secrets.invalidate("DBOWN_SMOKE")
        got = secrets.get_secret("DBOWN_SMOKE", store=store)
        assert got == "v-smoke-1234567890", got

    _check("agent_rw: put/search memory via dispatch", put_get_search)
    _check("agent_rw: vault set_secret + reveal via precis.secrets", vault_roundtrip)
    _check("agent_rw: list web users", lambda: store.list_web_users())
    _check(
        "agent_rw: SELECT vault.secrets refused",
        lambda: _expect_denied(rw, "SELECT ciphertext FROM vault.secrets"),
    )
    _check(
        "agent_rw: UPDATE web_users refused",
        lambda: _expect_denied(rw, "UPDATE web_users SET full_name = full_name"),
    )
    _check(
        "agent_rw: CREATE TABLE refused",
        lambda: _expect_denied(rw, "CREATE TABLE dbown_smoke (x int)"),
    )

    def users_add_refused() -> None:
        r = _cli(
            rw,
            "users",
            "add",
            "smokerw",
            "--abbrev",
            "srw",
            "--password-stdin",
            stdin="smoke-password-123456\n",
        )
        assert r.returncode != 0 and "precis_ingress" in r.stderr, r.stderr[-300:]

    def users_add_ingress() -> None:
        r = _cli(
            ing,
            "users",
            "add",
            "smokeing",
            "--abbrev",
            "sing",
            "--password-stdin",
            stdin="smoke-password-123456\n",
        )
        assert r.returncode == 0, r.stderr[-300:]

    def vacuum_rw() -> None:
        with psycopg.connect(rw, autocommit=True, connect_timeout=15) as conn:
            conn.execute("VACUUM ANALYZE refs")
            row = conn.execute(
                "SELECT last_vacuum IS NOT NULL OR last_autovacuum IS NOT NULL "
                "FROM pg_stat_user_tables WHERE relname = 'refs'"
            ).fetchone()
            assert row and row[0], "VACUUM did not run (no MAINTAIN?)"

    def migrate_owner() -> None:
        r = _cli(own, "migrate")
        assert r.returncode == 0 and "nothing to apply" in r.stdout, (
            r.stdout[-200:] + r.stderr[-300:]
        )

    def owner_ddl_grants() -> None:
        with psycopg.connect(own, connect_timeout=15) as conn:
            conn.execute("CREATE TABLE dbown_smoke_new (id serial, x int)")
            conn.commit()
        try:
            with psycopg.connect(rw, connect_timeout=15) as conn:
                conn.execute("INSERT INTO dbown_smoke_new (x) VALUES (1)")
                conn.commit()
        finally:
            with psycopg.connect(own, connect_timeout=15) as conn:
                conn.execute("DROP TABLE dbown_smoke_new")
                conn.commit()

    _check(
        "agent_rw: `precis users add` refused, hint names precis_ingress",
        users_add_refused,
    )
    _check("precis_ingress: `precis users add` writes the roster", users_add_ingress)
    _check("agent_rw: VACUUM ANALYZE refs (MAINTAIN)", vacuum_rw)
    _check("precis_owner: `precis migrate` (nothing pending)", migrate_owner)
    _check(
        "precis_owner: new table → agent_rw DML via default privileges",
        owner_ddl_grants,
    )
    store.close()
    print(f"db-own smoke: {'GREEN' if not _FAILS else 'RED: ' + ', '.join(_FAILS)}")
    return 1 if _FAILS else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))

"""db-own canary — agent_rw owns nothing and can't reach the vault or the roster.

Fable review findings 1 and 3 (Reto 2026-10-22): on prod agent_rw owned
``vault.secrets``, ``vault.events``, ``web_users`` and ``app_settings``, so
any agent_rw shell could decrypt the vault (the key is a readable GUC) and
UPDATE itself to web admin. ``scripts/db-own/verify.sql`` states the target
invariants; run read-only against prod before the cutover, it is red.

Each test reproduces prod's ownership on the session clone (every object in
public/vault owned by agent_rw, CREATE on public), runs the cutover exactly as
``scripts/db-own-cutover`` does (``cutover.sql`` + migration 0279), checks
``verify.sql`` and then the behaviour under ``SET LOCAL ROLE`` — all in ONE
transaction that is rolled back, so the clone and the cluster's roles end as
they started. Roles agent_rw/agent_ro/precis_system are created NOLOGIN once
(cluster-global, idempotent) as in tests/test_secrets_vault_class.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from tests.conftest import (
    PG_TEST_DSN,
    _active_dsn,
    _pg_available,
    _provision_vault_key,
)

_REPO = Path(__file__).resolve().parents[1]
_CUTOVER = _REPO / "scripts" / "db-own" / "cutover.sql"
_VERIFY = _REPO / "scripts" / "db-own" / "verify.sql"
_LAYOUT = _REPO / "src" / "precis" / "migrations" / "0279_db_ownership.sql"
_TEST_KEY = "test-vault-key-dbown-0123456789"

#: Roles prod has before the cutover; the cutover creates the other three.
_PREEXISTING = ("agent_rw", "agent_ro", "precis_system")

#: Prod's state today (verify.sql run read-only, 2026-10-22): checks that
#: are red before the cutover and the simulation must reproduce.
_RED_BEFORE = {
    "agent_rw owns nothing in this database (objects, schemas, db)",
    "protected tables owned by precis_owner",
    "agent_rw cannot SELECT vault.secrets",
    "agent_rw cannot INSERT/UPDATE/DELETE/TRUNCATE web_users",
    "agent_rw cannot CREATE in schemas public/vault",
    "agent_rw/agent_ro cannot SELECT web_users.password_hash",
    "vault.set_secret/delete_secret not executable by PUBLIC or agent_ro",
}

_SIMULATE_PROD = """
DO $sim$
DECLARE o record;
BEGIN
    EXECUTE format('ALTER DATABASE %I OWNER TO agent_rw', current_database());
    ALTER SCHEMA vault OWNER TO agent_rw;
    FOR o IN
        SELECT c.oid::regclass AS n, c.relkind
          FROM pg_class c
         WHERE c.relnamespace IN ('public'::regnamespace, 'vault'::regnamespace)
           AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
           AND NOT EXISTS (SELECT 1 FROM pg_depend d
                            WHERE d.classid = 'pg_class'::regclass
                              AND d.objid = c.oid AND d.deptype IN ('e', 'a', 'i'))
    LOOP
        EXECUTE format('ALTER %s %s OWNER TO agent_rw',
                       CASE o.relkind WHEN 'v' THEN 'VIEW'
                                      WHEN 'm' THEN 'MATERIALIZED VIEW'
                                      WHEN 'S' THEN 'SEQUENCE'
                                      ELSE 'TABLE' END, o.n);
    END LOOP;
    FOR o IN
        SELECT p.oid::regprocedure AS n
          FROM pg_proc p
         WHERE p.pronamespace IN ('public'::regnamespace, 'vault'::regnamespace)
           AND NOT EXISTS (SELECT 1 FROM pg_depend d
                            WHERE d.classid = 'pg_proc'::regclass
                              AND d.objid = p.oid AND d.deptype = 'e')
    LOOP
        EXECUTE format('ALTER ROUTINE %s OWNER TO agent_rw', o.n);
    END LOOP;
    GRANT CREATE ON SCHEMA public TO agent_rw;
    -- 0059's grant, which the template's own 0279 run revoked.
    GRANT EXECUTE ON FUNCTION vault.set_secret(text, text),
                              vault.delete_secret(text) TO PUBLIC;
    -- 0257's reveal grants (role-guarded: the template DB migrated before
    -- agent_rw existed here, so prod's grant never landed in it).
    GRANT EXECUTE ON FUNCTION vault.reveal(text),
                              vault.reveal(text, text, text, integer, integer, text),
                              vault.reveal(text, bigint),
                              vault.reveal(text, bigint, text, text, integer, integer, text)
                              TO agent_rw;
END
$sim$;
"""


def _ensure_roles(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        for role in _PREEXISTING:
            try:
                conn.execute(
                    sql.SQL(
                        "DO $$ BEGIN IF to_regrole({lit}) IS NULL THEN "
                        "CREATE ROLE {ident} NOLOGIN; END IF; END $$"
                    ).format(lit=sql.Literal(role), ident=sql.Identifier(role))
                )
            except (psycopg.errors.DuplicateObject, psycopg.errors.UniqueViolation):
                pass  # a sibling xdist worker created it first


@pytest.fixture(scope="module")
def owner_dsn() -> str:
    """Keyed superuser DSN on the session clone; skips without postgres."""
    if not _pg_available():
        pytest.skip(f"postgres unreachable at {PG_TEST_DSN}")
    dsn = _active_dsn()
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        if not (row and row[0]):
            pytest.skip("db-own cutover needs a superuser test role")
        conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    _ensure_roles(dsn)
    _provision_vault_key(dsn, _TEST_KEY)  # 0239: a vault._key row, not a GUC
    return dsn


@pytest.fixture
def prod_like(owner_dsn: str) -> Iterator[psycopg.Connection]:
    """One open transaction holding prod's pre-cutover ownership; rolled back."""
    conn = psycopg.connect(owner_dsn)
    try:
        conn.execute("SET LOCAL lock_timeout = '30s'")
        conn.execute(_SIMULATE_PROD)
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _verify(conn: psycopg.Connection) -> dict[str, bool]:
    rows = conn.execute(_VERIFY.read_text(encoding="utf-8")).fetchall()
    return {name: ok for _, name, ok in rows}


def _cutover(conn: psycopg.Connection) -> None:
    conn.execute(_CUTOVER.read_text(encoding="utf-8"))
    conn.execute(_LAYOUT.read_text(encoding="utf-8"))


def _as(
    conn: psycopg.Connection, role: str, stmt: str, *, session: bool = False
) -> list[tuple]:
    """Run ``stmt`` as ``role`` inside a savepoint; the role ends with it.

    ``session=True`` swaps session_user too (SET LOCAL SESSION AUTHORIZATION):
    SET ROLE and the vault ACL check the session user, which is otherwise the
    superuser test role and passes everything.
    """
    what = "SESSION AUTHORIZATION" if session else "ROLE"
    with conn.transaction():
        conn.execute(sql.SQL(f"SET LOCAL {what} {{}}").format(sql.Identifier(role)))
        cur = conn.execute(stmt)
        rows = cur.fetchall() if cur.description else []
        conn.execute(f"RESET {what}")
    return rows


def _denied(
    conn: psycopg.Connection, role: str, stmt: str, *, session: bool = False
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _as(conn, role, stmt, session=session)


def test_simulation_reproduces_prod_red_state(prod_like: psycopg.Connection) -> None:
    """Before the cutover the canary is red exactly where prod is."""
    checks = _verify(prod_like)
    red = {name for name, ok in checks.items() if not ok}
    assert red >= _RED_BEFORE, sorted(_RED_BEFORE - red)


def test_cutover_turns_every_check_green(prod_like: psycopg.Connection) -> None:
    _cutover(prod_like)
    checks = _verify(prod_like)
    assert all(checks.values()), sorted(n for n, ok in checks.items() if not ok)


def test_cutover_is_idempotent(prod_like: psycopg.Connection) -> None:
    _cutover(prod_like)
    _cutover(prod_like)
    assert all(_verify(prod_like).values())


def test_agent_rw_cannot_read_vault_or_write_roster(
    prod_like: psycopg.Connection,
) -> None:
    _cutover(prod_like)
    c = prod_like
    _denied(c, "agent_rw", "SELECT ciphertext FROM vault.secrets")
    _denied(c, "agent_rw", "SELECT * FROM vault.events")
    _denied(c, "agent_rw", "SELECT vault._key()")
    _denied(c, "agent_rw", "UPDATE web_users SET full_name = full_name")
    _denied(c, "agent_rw", "INSERT INTO web_users (login) SELECT 'x' WHERE false")
    _denied(c, "agent_rw", "DELETE FROM web_users")
    _denied(c, "agent_rw", "DELETE FROM _migrations")
    _denied(c, "agent_ro", "SELECT ciphertext FROM vault.secrets")
    for role in ("agent_rw", "agent_ro"):
        _denied(c, role, "SELECT password_hash FROM web_users")
        _denied(c, role, "SELECT * FROM web_users")


def test_vault_writes_class_gated(prod_like: psycopg.Connection) -> None:
    """db-own finding 2: agent_rw cannot overwrite or delete an infra-class
    secret; agent_ro writes nothing; the legitimate writers still can."""
    _cutover(prod_like)
    c = prod_like
    c.execute("SELECT vault.set_secret('CLAUDE_CODE_OAUTH_TOKEN', 'v-orig-1234567890')")
    for stmt in (
        "SELECT vault.set_secret('CLAUDE_CODE_OAUTH_TOKEN', 'v-evil-1234567890')",
        "SELECT vault.delete_secret('CLAUDE_CODE_OAUTH_TOKEN')",
        "SELECT vault.set_secret('svc/infra/dbown_new', 'v-evil-1234567890')",
    ):
        with pytest.raises(
            psycopg.errors.InsufficientPrivilege, match="not allowed to write"
        ):
            _as(c, "agent_rw", stmt, session=True)
    _denied(
        c,
        "agent_ro",
        "SELECT vault.set_secret('DBOWN_RO', 'v-1234567890')",
        session=True,
    )
    _denied(c, "agent_ro", "SELECT vault.delete_secret('DBOWN_RO')", session=True)
    row = c.execute("SELECT vault.reveal('CLAUDE_CODE_OAUTH_TOKEN')").fetchone()
    assert row == ("v-orig-1234567890",)
    # The writers: api names from agent_rw; the user's AnkiWeb password and the
    # admin vault editor from precis_ingress; operator infra writes on the
    # owner DSN; workers' infra names from precis_system.
    _as(
        c,
        "agent_rw",
        "SELECT vault.set_secret('DBOWN_API', 'v-api-1234567890')",
        session=True,
    )
    _as(c, "agent_rw", "SELECT vault.delete_secret('DBOWN_API')", session=True)
    _as(
        c,
        "precis_ingress",
        "SELECT vault.set_secret('ANKI_PASSWORD:t', 'v-ank-1234567890')",
        session=True,
    )
    _as(
        c,
        "precis_ingress",
        "SELECT vault.delete_secret('ANKI_PASSWORD:t')",
        session=True,
    )
    _as(
        c,
        "precis_owner",
        "SELECT vault.set_secret('CLAUDE_CODE_OAUTH_TOKEN', 'v-new-1234567890')",
        session=True,
    )
    c.execute("GRANT agent_rw TO precis_system")  # deploy's membership
    _as(
        c,
        "precis_system",
        "SELECT vault.set_secret('svc/infra/dbown_w', 'v-sys-1234567890')",
        session=True,
    )


def test_ingress_reads_the_hash_for_login(prod_like: psycopg.Connection) -> None:
    """Login (precis_web.auth → get_web_user_credentials) keeps working."""
    _cutover(prod_like)
    _as(
        prod_like,
        "precis_ingress",
        "SELECT login, password_hash, password_salt FROM web_users",
    )
    _as(
        prod_like,
        "agent_rw",
        "SELECT login, has_feed_token FROM (SELECT login, "
        "(feed_token_sha256 IS NOT NULL) AS has_feed_token FROM web_users) s",
    )
    _as(prod_like, "agent_ro", "SELECT count(*) FROM web_users")


def test_agent_rw_cannot_ddl_or_become_owner(prod_like: psycopg.Connection) -> None:
    _cutover(prod_like)
    c = prod_like
    _denied(c, "agent_rw", "CREATE TABLE public.t_dbown_probe (x int)")
    _denied(c, "agent_rw", "ALTER TABLE refs ADD COLUMN t_dbown_probe int")
    _denied(c, "agent_rw", "CREATE INDEX t_dbown_probe ON refs (ref_id)")
    for role in ("agent_rw", "precis_system", "precis_ingress"):
        _denied(c, role, "SET LOCAL ROLE precis_owner", session=True)
    _denied(c, "agent_rw", "GRANT SELECT ON vault.secrets TO agent_rw")


def test_agent_rw_keeps_what_it_needs(prod_like: psycopg.Connection) -> None:
    """DML everywhere in public, vault through its functions, VACUUM rights."""
    _cutover(prod_like)
    c = prod_like
    _as(c, "agent_rw", "SELECT count(*) FROM refs")
    _as(c, "agent_rw", "UPDATE refs SET title = title WHERE false")
    _as(c, "agent_rw", "SELECT login FROM web_users")
    _as(c, "agent_rw", "UPDATE app_settings SET value = value WHERE false")
    _as(c, "agent_rw", "SELECT version FROM _migrations LIMIT 1")
    _as(c, "agent_rw", "SELECT vault.set_secret('DBOWN_PLAIN', 'v-plain-1234567890')")
    rows = _as(c, "agent_rw", "SELECT vault.reveal('DBOWN_PLAIN')")
    assert rows == [("v-plain-1234567890",)]
    rows = _as(
        c, "agent_rw", "SELECT name FROM vault.list() WHERE name = 'DBOWN_PLAIN'"
    )
    assert rows == [("DBOWN_PLAIN",)]
    row = c.execute(
        "SELECT has_table_privilege('agent_rw', 'refs', 'MAINTAIN')"
    ).fetchone()
    assert row == (True,)


def test_reveal_classes_still_route_by_session_user(
    prod_like: psycopg.Connection,
) -> None:
    """The 0278 class gate holds after the owner change: infra stays
    precis_system-only, api stays agent_rw-only (no readers)."""
    _cutover(prod_like)
    c = prod_like
    c.execute("GRANT agent_rw TO precis_system")  # deploy's membership
    c.execute("SELECT vault.set_secret('svc/infra/dbown_key', 'v-infra-1234567890')")
    c.execute("SELECT vault.set_secret('DBOWN_API_KEY', 'v-api-1234567890')")
    infra = "SELECT vault.reveal('svc/infra/dbown_key')"
    api = "SELECT vault.reveal('DBOWN_API_KEY')"
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="not allowed"):
        _as(c, "agent_rw", infra, session=True)
    assert _as(c, "precis_system", infra, session=True) == [("v-infra-1234567890",)]
    assert _as(c, "agent_rw", api, session=True) == [("v-api-1234567890",)]
    with pytest.raises(
        psycopg.errors.InsufficientPrivilege, match="not allowed|permission denied"
    ):
        _as(c, "agent_ro", api, session=True)


def test_ingress_writes_roster(prod_like: psycopg.Connection) -> None:
    _cutover(prod_like)
    c = prod_like
    _as(c, "precis_ingress", "UPDATE web_users SET full_name = full_name WHERE false")
    _as(c, "precis_ingress", "INSERT INTO web_users (login) SELECT 'x' WHERE false")
    _as(c, "precis_ingress", "SELECT count(*) FROM refs")


def test_owner_objects_inherit_grants(prod_like: psycopg.Connection) -> None:
    """A table a later migration creates (as precis_owner) works for agent_rw."""
    _cutover(prod_like)
    c = prod_like
    _as(c, "precis_owner", "CREATE TABLE public.t_dbown_new (id serial, x int)")
    _as(c, "precis_owner", "CREATE VIEW public.t_dbown_view AS SELECT 1 AS one")
    _as(c, "agent_rw", "INSERT INTO t_dbown_new (x) VALUES (1)")
    _as(c, "agent_rw", "SELECT one FROM t_dbown_view")
    _as(c, "agent_ro", "SELECT x FROM t_dbown_new")
    row = c.execute(
        "SELECT relowner::regrole::text FROM pg_class WHERE relname = 't_dbown_new'"
    ).fetchone()
    assert row == ("precis_owner",)


def test_owner_login_runs_migration_ddl(prod_like: psycopg.Connection) -> None:
    """The owner DSN (session_user precis_owner) can do what migrations do."""
    _cutover(prod_like)
    c = prod_like
    for stmt in (
        "ALTER TABLE refs ADD COLUMN t_dbown_probe int",
        "CREATE INDEX t_dbown_probe_idx ON refs (t_dbown_probe)",
        "INSERT INTO _migrations (plugin, version, checksum) "
        "VALUES ('precis', '9999_t_dbown', 'x')",
        "SELECT name FROM vault.list()",
    ):
        _as(c, "precis_owner", stmt, session=True)


def test_layout_refuses_before_reassign(prod_like: psycopg.Connection) -> None:
    """precis_owner exists but agent_rw still owns relations → loud refusal."""
    c = prod_like
    c.execute(
        "DO $$ BEGIN IF to_regrole('precis_owner') IS NULL THEN "
        "CREATE ROLE precis_owner NOLOGIN; END IF; END $$"
    )
    with pytest.raises(psycopg.errors.RaiseException, match="still owns"):
        with c.transaction():
            c.execute(_LAYOUT.read_text(encoding="utf-8"))


def test_layout_noop_before_cutover(prod_like: psycopg.Connection) -> None:
    """agent_rw the owner and no precis_owner (prod pre-cutover, single-login
    installs): a deploy's migrate stamps 0279 without touching privileges."""
    c = prod_like
    c.execute(_LAYOUT.read_text(encoding="utf-8"))
    row = c.execute(
        "SELECT has_table_privilege('agent_rw', 'vault.secrets', 'SELECT')"
    ).fetchone()
    assert row == (True,)  # still the owner: nothing changed


def test_layout_on_setup_roles_install(owner_dsn: str) -> None:
    """`precis setup --roles` shape (out-of-box-setup.md O7): the invoking
    login owns everything, agent_rw/precis_ingress are plain logins."""
    with psycopg.connect(owner_dsn) as c:
        try:
            # The owner is an unprivileged login (a superuser owner is a dev
            # install; 0279 skips it): own _migrations as one, in this
            # rolled-back transaction.
            c.execute("CREATE ROLE t_dbown_setup NOLOGIN")
            c.execute("GRANT CREATE ON SCHEMA public TO t_dbown_setup")
            c.execute("ALTER TABLE public._migrations OWNER TO t_dbown_setup")
            c.execute(
                "DO $$ BEGIN IF to_regrole('precis_ingress') IS NULL THEN "
                "CREATE ROLE precis_ingress NOLOGIN; END IF; END $$"
            )
            c.execute("GRANT agent_rw TO precis_ingress")
            c.execute(_LAYOUT.read_text(encoding="utf-8"))
            _denied(c, "agent_rw", "SELECT ciphertext FROM vault.secrets")
            _denied(c, "agent_rw", "UPDATE web_users SET full_name = full_name")
            _denied(c, "agent_rw", "CREATE TABLE public.t_dbown_probe (x int)")
            _as(c, "agent_rw", "UPDATE refs SET title = title WHERE false")
            _as(c, "precis_ingress", "UPDATE web_users SET full_name = full_name")
            _as(c, "agent_rw", "SELECT vault.reveal('DBOWN_NONE')")
            with c.transaction():
                c.execute("SET LOCAL ROLE t_dbown_setup")
                c.execute("CREATE TABLE t_dbown_n (x int)")  # as the owner login
                c.execute("RESET ROLE")
            _as(c, "agent_rw", "INSERT INTO t_dbown_n VALUES (1)")
        finally:
            c.rollback()


def test_owner_refusal_names_the_owner_dsn(prod_like: psycopg.Connection) -> None:
    """Runtime DDL paths refuse up front on a non-owner DSN, naming the fix."""
    from precis.store.ownership import OwnerRequired, owner_refusal, require_owner

    c = prod_like
    _cutover(c)
    with c.transaction():
        c.execute("SET LOCAL ROLE agent_rw")
        msg = owner_refusal(c, "public.parts", purpose="parts_bulk_replace")
        with pytest.raises(OwnerRequired):
            require_owner(c, "public._migrations", purpose="precis migrate")
        c.execute("RESET ROLE")
    assert msg is not None
    assert "owned by precis_owner" in msg and "agent_rw" in msg
    assert "postgresql://precis_owner@" in msg
    with c.transaction():
        c.execute("SET LOCAL ROLE precis_owner")
        assert owner_refusal(c, "public.parts", purpose="x") is None
        assert owner_refusal(c, "public.no_such_table", purpose="x") is None
        c.execute("RESET ROLE")

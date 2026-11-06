"""Vault name classes — the reveal gate (migration 0285, multi-user S1).

Mechanics tested on the real test DB:
- ``vault.secrets.class`` is stamped from the name on every write
  (``svc/infra/`` and the legacy infra list → infra, ``svc/api/`` → api,
  ``user/<id>/`` → user, anything else → api) and is NOT NULL
- ``vault.reveal`` checks ``session_user``: infra → member of precis_system,
  api → member of agent_rw, user → member of precis_ingress AND the name is
  ``user/<p_user_id>/…``; denial raises ``insufficient_privilege``
- ``vault.set_secret``/``delete_secret`` gate on the written name's class:
  infra → precis_system or precis_ingress, api → agent_rw, user →
  precis_ingress; the owner of ``vault.secrets`` passes (db-own finding 2)
- the pattern table of the first S1 draft is gone

Simulating a foreign ``session_user`` needs real non-superuser roles: ``SET
SESSION AUTHORIZATION`` swaps it for the connection (the superuser test role
may issue it). Roles are created once, idempotently — they are cluster-global,
so tests must not race DROP/CREATE.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from precis.store import Store
from tests.conftest import (
    PG_TEST_DSN,
    _active_dsn,
    _pg_available,
    _provision_vault_key,
)

_TEST_KEY = "test-vault-key-0123456789"
_ROLES = ("precis_system", "agent_rw", "agent_ro", "precis_ingress", "t_vault_other")


@pytest.fixture(scope="module")
def vault_dsn() -> str:
    """Roles + a keyed DSN. Skips cleanly without postgres."""
    if not _pg_available():
        pytest.skip(f"postgres unreachable at {PG_TEST_DSN}")
    dsn = _active_dsn()
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            admin.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        except psycopg.errors.InsufficientPrivilege:
            pytest.skip("pgcrypto not installed")
        for role in _ROLES:
            try:
                admin.execute(
                    sql.SQL(
                        "DO $$ BEGIN IF to_regrole({lit}) IS NULL THEN "
                        "CREATE ROLE {ident} NOLOGIN; END IF; END $$"
                    ).format(lit=sql.Literal(role), ident=sql.Identifier(role))
                )
            except psycopg.errors.InsufficientPrivilege:
                pytest.skip("test role cannot CREATE ROLE — vault tests need it")
            except (psycopg.errors.DuplicateObject, psycopg.errors.UniqueViolation):
                pass  # a sibling xdist worker created it first
        # Prod's prerequisite for the per-transaction reader role (pool.py).
        try:
            admin.execute("GRANT agent_ro TO agent_rw")
        except psycopg.errors.UniqueViolation:
            pass  # a sibling xdist worker granted it first
        # Prod membership: the service roles reach 0257's agent_rw-only reveal
        # EXECUTE through it; the class gate then decides which names.
        try:
            admin.execute("GRANT agent_rw TO precis_system, precis_ingress")
        except psycopg.errors.UniqueViolation:
            pass  # a sibling xdist worker granted it first
        # 0257's agent_rw grant is role-guarded and the template DB migrates
        # before this role exists, so give it what prod's agent_rw has.
        admin.execute(
            "GRANT EXECUTE ON FUNCTION vault.reveal(text), "
            "vault.reveal(text, text, text, integer, integer, text), "
            "vault.reveal(text, bigint), "
            "vault.reveal(text, bigint, text, text, integer, integer, text) "
            "TO agent_rw"
        )
        admin.execute("DELETE FROM vault.secrets")
    _provision_vault_key(dsn, _TEST_KEY)  # 0239: a vault._key row, not a GUC
    return dsn


@pytest.fixture
def store(vault_dsn: str):
    s = Store.connect(vault_dsn)
    try:
        yield s
    finally:
        s.close()


def _set(store: Store, name: str, value: str) -> None:
    with store.pool.connection() as conn:
        conn.execute("SELECT vault.set_secret(%s, %s)", (name, value))
        conn.commit()


def _as(dsn: str, role: str, stmt: str, params: tuple = ()):
    """Run ``stmt`` under ``SET SESSION AUTHORIZATION <role>``; returns the row."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("SET SESSION AUTHORIZATION {}").format(sql.Identifier(role))
        )
        return conn.execute(stmt, params).fetchone()


@pytest.mark.parametrize(
    ("name", "cls"),
    [
        ("svc/infra/hpc_ssh_key", "infra"),
        ("svc/api/openalex", "api"),
        ("user/7/feed", "user"),
        ("CLAUDE_CODE_OAUTH_TOKEN", "infra"),
        ("MELUXINA_SSH_u104186", "infra"),
        ("ANKI_PASSWORD:reto", "infra"),
        ("email.rs@example.org.password", "infra"),
        ("ANKI_USER:reto", "api"),
        ("PERPLEXITY_API_KEY", "api"),
        ("user/notanid/x", "api"),
    ],
)
def test_class_stamped_from_name(store: Store, name: str, cls: str) -> None:
    _set(store, name, "v-class-1234567890")
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT class FROM vault.secrets WHERE name = %s", (name,)
        ).fetchone()
    assert row == (cls,)


def test_class_cannot_be_weakened(store: Store) -> None:
    """A direct UPDATE re-stamps: an infra name never stores as api."""
    _set(store, "svc/infra/pinned", "v-pin-1234567890")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE vault.secrets SET class = 'api' WHERE name = 'svc/infra/pinned'"
        )
        row = conn.execute(
            "SELECT class FROM vault.secrets WHERE name = 'svc/infra/pinned'"
        ).fetchone()
        conn.rollback()
    assert row == ("infra",)


def test_infra_only_for_precis_system(vault_dsn: str, store: Store) -> None:
    _set(store, "svc/infra/hpc_ssh_key", "v-hpc-1234567890")
    reveal = "SELECT vault.reveal('svc/infra/hpc_ssh_key')"
    assert _as(vault_dsn, "precis_system", reveal) == ("v-hpc-1234567890",)
    for role in ("agent_rw", "agent_ro", "precis_ingress", "t_vault_other"):
        with pytest.raises(
            psycopg.errors.InsufficientPrivilege, match="not allowed|permission denied"
        ):
            _as(vault_dsn, role, reveal)


def test_api_for_agent_rw_members_not_readers(vault_dsn: str, store: Store) -> None:
    _set(store, "PERPLEXITY_API_KEY", "v-api-1234567890")
    reveal = "SELECT vault.reveal('PERPLEXITY_API_KEY')"
    assert _as(vault_dsn, "agent_rw", reveal) == ("v-api-1234567890",)
    with pytest.raises(
        psycopg.errors.InsufficientPrivilege, match="not allowed|permission denied"
    ):
        _as(vault_dsn, "agent_ro", reveal)


def test_user_class_binds_p_user_id(vault_dsn: str, store: Store) -> None:
    """precis_ingress reveals user/<id>/… only for the id it passes."""
    _set(store, "user/7/feed", "v-user7-1234567890")
    ok = "SELECT vault.reveal('user/7/feed', 7::bigint)"
    assert _as(vault_dsn, "precis_ingress", ok) == ("v-user7-1234567890",)
    for stmt in (
        "SELECT vault.reveal('user/7/feed', 8::bigint)",
        "SELECT vault.reveal('user/7/feed')",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="not a secret"):
            _as(vault_dsn, "precis_ingress", stmt)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="not allowed"):
        _as(vault_dsn, "agent_rw", ok)


def test_set_local_role_does_not_change_the_gate(vault_dsn: str, store: Store) -> None:
    """An agent_rw session under SET LOCAL ROLE agent_ro is still agent_rw."""
    _set(store, "svc/infra/hpc_ssh_key", "v-hpc-1234567890")
    with psycopg.connect(vault_dsn) as conn:
        # 0257 leaves agent_ro no EXECUTE on reveal at all; grant it inside
        # this rolled-back transaction so the class gate (session_user) is
        # what answers, not the EXECUTE check (current_user).
        conn.execute("GRANT EXECUTE ON FUNCTION vault.reveal(text) TO agent_ro")
        conn.execute("SET SESSION AUTHORIZATION agent_rw")
        conn.execute("SET LOCAL ROLE agent_ro")
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='"agent_rw"'):
            conn.execute("SELECT vault.reveal('svc/infra/hpc_ssh_key')")
        conn.rollback()


def test_identity_overload_still_audits(vault_dsn: str, store: Store) -> None:
    """The 6-arg overload precis.secrets uses gates and writes vault.events."""
    _set(store, "svc/infra/audited", "v-aud-1234567890")
    row = _as(
        vault_dsn,
        "precis_system",
        "SELECT vault.reveal('svc/infra/audited', 'h', 'u', 1, 0, 'p')",
    )
    assert row == ("v-aud-1234567890",)
    with psycopg.connect(vault_dsn) as conn:
        n = conn.execute(
            "SELECT count(*) FROM vault.events "
            "WHERE name = 'svc/infra/audited' AND who = 'precis_system'"
        ).fetchone()
    assert n is not None and n[0] >= 1


def test_missing_name_is_null(vault_dsn: str) -> None:
    assert _as(vault_dsn, "agent_rw", "SELECT vault.reveal('NO_SUCH_NAME')") == (None,)


@pytest.fixture
def wtx(vault_dsn: str) -> Iterator[psycopg.Connection]:
    """One transaction, rolled back, with set_secret/delete_secret EXECUTE
    granted to PUBLIC — isolates the class gate from 0286's agent_rw-only
    EXECUTE grant (on prod precis_ingress/precis_system reach it through
    their agent_rw membership, which the shared test roles don't carry)."""
    conn = psycopg.connect(vault_dsn)
    try:
        conn.execute(
            "GRANT EXECUTE ON FUNCTION vault.set_secret(text, text), "
            "vault.delete_secret(text) TO PUBLIC"
        )
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _w(conn: psycopg.Connection, role: str, stmt: str, params: tuple = ()) -> None:
    """``stmt`` with session_user = ``role``, inside a savepoint."""
    with conn.transaction():
        conn.execute(
            sql.SQL("SET LOCAL SESSION AUTHORIZATION {}").format(sql.Identifier(role))
        )
        conn.execute(stmt, params)
        conn.execute("RESET SESSION AUTHORIZATION")


def _refused(
    conn: psycopg.Connection, role: str, stmt: str, params: tuple = ()
) -> None:
    with pytest.raises(
        psycopg.errors.InsufficientPrivilege, match="not allowed to write"
    ):
        _w(conn, role, stmt, params)


def _value(conn: psycopg.Connection, name: str) -> str | None:
    row = conn.execute("SELECT vault.reveal(%s)", (name,)).fetchone()
    return row[0] if row else None


_SET = "SELECT vault.set_secret(%s, %s)"
_DEL = "SELECT vault.delete_secret(%s)"


@pytest.mark.parametrize(
    "name", ["svc/infra/hpc_ssh_key", "CLAUDE_CODE_OAUTH_TOKEN", "ANKI_PASSWORD:reto"]
)
def test_agent_rw_cannot_overwrite_or_delete_infra(
    wtx: psycopg.Connection, name: str
) -> None:
    """db-own finding 2: agent_rw swapping the OAuth token or the HPC key for
    its own value (or deleting it) is refused; the stored value survives."""
    wtx.execute(_SET, (name, "v-orig-1234567890"))
    for role in ("agent_rw", "agent_ro", "t_vault_other"):
        _refused(wtx, role, _SET, (name, "v-evil-1234567890"))
        _refused(wtx, role, _DEL, (name,))
    assert _value(wtx, name) == "v-orig-1234567890"


def test_infra_writers_are_system_and_ingress(wtx: psycopg.Connection) -> None:
    """precis_ingress stores a user's AnkiWeb password (/account) and is the
    admin vault editor; precis_system writes infra names for the workers."""
    _w(wtx, "precis_ingress", _SET, ("ANKI_PASSWORD:t", "v-ing-1234567890"))
    assert _value(wtx, "ANKI_PASSWORD:t") == "v-ing-1234567890"
    _w(wtx, "precis_ingress", _DEL, ("ANKI_PASSWORD:t",))
    assert _value(wtx, "ANKI_PASSWORD:t") is None
    _w(wtx, "precis_system", _SET, ("svc/infra/w", "v-sys-1234567890"))
    assert _value(wtx, "svc/infra/w") == "v-sys-1234567890"


def test_api_writers_are_agent_rw_members(wtx: psycopg.Connection) -> None:
    _w(wtx, "agent_rw", _SET, ("ANKI_USER:t", "v-api-1234567890"))
    assert _value(wtx, "ANKI_USER:t") == "v-api-1234567890"
    _w(wtx, "agent_rw", _DEL, ("ANKI_USER:t",))
    assert _value(wtx, "ANKI_USER:t") is None
    for role in ("agent_ro", "t_vault_other"):
        _refused(wtx, role, _SET, ("ANKI_USER:t", "v-1234567890"))


def test_user_class_written_by_ingress_only(wtx: psycopg.Connection) -> None:
    _w(wtx, "precis_ingress", _SET, ("user/7/w", "v-u-1234567890"))
    for role in ("agent_rw", "precis_system"):
        _refused(wtx, role, _DEL, ("user/7/w",))
    row = wtx.execute("SELECT 1 FROM vault.secrets WHERE name = 'user/7/w'").fetchone()
    assert row == (1,)


def test_no_pattern_table(store: Store) -> None:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT to_regclass('vault.acl')").fetchone()
    assert row == (None,)

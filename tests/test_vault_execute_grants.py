"""gr523021: migration 0257 revokes PUBLIC EXECUTE on the vault verbs.

The carve-out tests (0079's ``test_gripe_carveout.py``) proved PUBLIC works
from a no-grants role; this is the mirror: a fresh scratch DB with real
``agent_ro`` / ``agent_rw`` NOLOGIN roles, the migrated schema, and
``has_function_privilege`` asserting the read-only role lost every vault
verb while ``agent_rw`` — the SHARED-WITH-AGENTS role every legitimate
caller connects as — kept them.

Roles are cluster-global, not per-DB, so the whole create → assert → drop
window sits under a cluster-wide advisory lock — two pytest *processes* on
the same test cluster would otherwise race the CREATE ROLE / DROP ROLE
pair. Cleanup is tolerant: a sibling worker's migration replay can grant
``agent_rw`` on *its* scratch DB while ours exists (0257's guarded DO
block), making the DROP fail with 2BP01 — the role then stays (harmless:
NOLOGIN, no password) and the next run adopts it.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from precis.store import Migrator, Store
from tests.conftest import _active_dsn, _dsn_with_db, _pg_available

ROOT = Path(__file__).parents[1]
MIGRATIONS = ROOT / "src/precis/migrations"
pytestmark = pytest.mark.db

#: Every signature the migration revokes from PUBLIC.
VAULT_FUNCS = (
    "vault.reveal(text)",
    "vault.reveal(text, text, text, integer, integer, text)",
    "vault.set_secret(text, text)",
    "vault.set_secret_if_absent(text, text)",
    "vault.delete_secret(text)",
    "vault.gc_events(integer)",  # 0258: audit-trail prune, agent_rw only
)
SECONDARY_FUNCS = (
    "public.refs_retire_event_trail()",
    "public.precis_revision_triggers_refresh()",
    "public.precis_kinds_refresh_guarded()",
)
#: Deliberate ro carve-outs / decided-PUBLIC verbs — must keep EXECUTE.
KEPT_FUNCS = (
    "public.bump_salience(bigint[])",
    "public.bump_ref_read(bigint, text, text)",
    "public.file_gripe_readonly(text)",
    "public.file_gripe_readonly(text, text)",
    "vault.list()",
    "vault.mask(text)",
)

#: The whole PUBLIC-EXECUTE allowlist for SECURITY DEFINER functions in the
#: vault/public schemas (post-0257 ground truth, pinned by the ratchet
#: below). Anything new here means a definer function just opened to every
#: connect-capable role — the exact hole gr523021 closed.
DEFINER_PUBLIC_EXEC_ALLOW = frozenset(KEPT_FUNCS)


def _acquire_role_lock(admin: psycopg.Connection) -> None:
    """Serialize the role window across pytest processes on this cluster."""
    for _ in range(150):
        got = admin.execute(
            "SELECT pg_try_advisory_lock(hashtext('precis:vault-grants-test'))"
        ).fetchone()
        if got is not None and got[0]:
            return
        time.sleep(1)
    pytest.skip("another vault-grants test holds the cluster role lock")


def _ensure_roles(admin: psycopg.Connection) -> list[str]:
    """CREATE ROLE for each app role the cluster lacks; return the ones made."""
    made = []
    for role in ("agent_ro", "agent_rw"):
        exists = admin.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone()
        if exists is None:
            admin.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
            made.append(role)
    return made


def test_vault_verbs_revoked_from_ro_granted_to_rw() -> None:
    if not _pg_available():
        pytest.skip("test PostgreSQL unavailable")
    name = "precis_vault_grants_" + uuid.uuid4().hex
    admin_dsn = _dsn_with_db(_active_dsn(), "postgres")
    dsn = _dsn_with_db(_active_dsn(), name)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        _acquire_role_lock(admin)
        made: list[str] = []
        try:
            made = _ensure_roles(admin)
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            runner = Migrator(
                dsn, MIGRATIONS, baseline=MIGRATIONS / "baseline/schema.sql"
            )
            runner.apply_all()
            assert runner.apply_all() == []  # idempotent
            with psycopg.connect(dsn) as conn:
                for func in (*VAULT_FUNCS, *SECONDARY_FUNCS):
                    ro = conn.execute(
                        "SELECT has_function_privilege('agent_ro', %s, 'EXECUTE')",
                        (func,),
                    ).fetchone()
                    rw = conn.execute(
                        "SELECT has_function_privilege('agent_rw', %s, 'EXECUTE')",
                        (func,),
                    ).fetchone()
                    assert ro == (False,), f"agent_ro still has EXECUTE on {func}"
                    assert rw == (True,), f"agent_rw lost EXECUTE on {func}"
                for func in KEPT_FUNCS:
                    ro = conn.execute(
                        "SELECT has_function_privilege('agent_ro', %s, 'EXECUTE')",
                        (func,),
                    ).fetchone()
                    assert ro == (True,), f"agent_ro lost decided-PUBLIC {func}"
                # No PUBLIC grantee rows remain on the revoked functions
                # either — agent_ro's only path was PUBLIC, but pin the ACL.
                for func in (*VAULT_FUNCS, *SECONDARY_FUNCS):
                    n_pub = conn.execute(
                        "SELECT count(*) FROM pg_proc p, aclexplode(p.proacl) a "
                        "WHERE p.oid = %s::regprocedure AND a.grantee = 0",
                        (func,),
                    ).fetchone()
                    assert n_pub == (0,), f"PUBLIC still in ACL of {func}"
        finally:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )
            for role in made:
                try:
                    admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
                except psycopg.Error as exc:
                    # 2BP01: a sibling worker's own scratch-DB replay granted
                    # the role mid-test; 42704: already gone. Either way the
                    # leftover NOLOGIN role is adoptable next run.
                    if exc.diag.sqlstate not in ("2BP01", "42704"):
                        raise
            admin.execute(
                "SELECT pg_advisory_unlock(hashtext('precis:vault-grants-test'))"
            )


def test_no_security_definer_function_grants_public_execute(store: Store) -> None:
    """Ratchet: a new SECURITY DEFINER function in vault/public may not take
    PUBLIC EXECUTE — that is how every connect-capable role, agent_ro
    included, gained vault writes (gr523021). The allowlist is the decided
    carve-outs: the read-path salience bump (0137), the ro gripe door
    (0079/0244), and the non-secret vault verbs."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT n.nspname || '.' || p.proname || '(' || "
            "  coalesce((SELECT string_agg(format_type(u.t, NULL), ', ' "
            "    ORDER BY u.ord) FROM unnest(p.proargtypes) "
            "    WITH ORDINALITY AS u(t, ord)), '') "
            "  || ')' "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE p.prosecdef AND n.nspname IN ('vault', 'public') "
            "AND has_function_privilege('public', p.oid, 'EXECUTE')"
        ).fetchall()
    offenders = {r[0] for r in rows} - DEFINER_PUBLIC_EXEC_ALLOW
    assert not offenders, (
        "SECURITY DEFINER function(s) with PUBLIC EXECUTE outside the "
        f"allowlist — an agent_ro-open hole (gr523021): {sorted(offenders)}"
    )


def test_gc_events_keeps_a_30_day_floor(store: Store) -> None:
    """0258: gc_events(0) must not erase recent audit rows (gr523021 c4).
    Rows inside the 30-day floor survive any p_keep_days; older rows go."""
    marker = "gc-floor-test-" + uuid.uuid4().hex
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO vault.events(at, who, verb, name) VALUES "
            "(now() - interval '2 days', 'test', 'reveal', %s), "
            "(now() - interval '40 days', 'test', 'reveal', %s)",
            (marker, marker),
        )
        conn.execute("SELECT vault.gc_events(0)")
        left = conn.execute(
            "SELECT count(*) FROM vault.events WHERE name = %s", (marker,)
        ).fetchone()
        conn.rollback()
    assert left == (1,), "gc_events(0) deleted a row inside the 30-day floor"

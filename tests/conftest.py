"""Pytest fixtures (sync, psycopg 3).

Phase 1 fixtures: hints, registry, runtime — stateless. Used by all
tests that don't touch the DB.

Phase 2 fixtures: fresh_db, store — postgres-backed. Tests that
exercise the store layer take `store` directly. Each `store` fixture
call yields a fresh ephemeral database with all migrations applied;
the database is dropped at fixture teardown.

Postgres connection comes from `PRECIS_TEST_PG_URL` (default:
`postgresql://localhost/postgres`). pgvector + pg_trgm extensions must
be available on the server; the migration installs them per database.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from precis.config import PrecisConfig
from precis.dispatch import Registry, boot
from precis.hints import HintBus
from precis.runtime import PrecisRuntime
from precis.store import Migrator, Store

PG_ADMIN_DSN = os.environ.get(
    "PRECIS_TEST_PG_URL",
    "postgresql://localhost/postgres",
)

MIGRATIONS_DIR = Path(__file__).parent.parent / "src" / "precis" / "migrations"


# ---------------------------------------------------------------------------
# Stateless fixtures (no DB)
# ---------------------------------------------------------------------------


@pytest.fixture
def hints() -> HintBus:
    return HintBus()


@pytest.fixture
def registry_stateless() -> Registry:
    """Stateless registry — calc only. Used by phase 1 tests."""
    return boot(store=None)


@pytest.fixture
def runtime_stateless(registry_stateless: Registry, hints: HintBus) -> PrecisRuntime:
    """Runtime with no store. Phase 1 tests use this."""
    return PrecisRuntime(
        config=PrecisConfig(),
        registry=registry_stateless,
        hints=hints,
    )


# Backwards-compat aliases for existing phase-1 tests.
@pytest.fixture
def registry(registry_stateless: Registry) -> Registry:
    return registry_stateless


@pytest.fixture
def runtime(runtime_stateless: PrecisRuntime) -> PrecisRuntime:
    return runtime_stateless


# ---------------------------------------------------------------------------
# Postgres fixtures (phase 2+)
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db() -> Iterator[str]:
    """Create an ephemeral postgres database, yield its DSN, drop on teardown.

    The created DB has no extensions installed yet — that's the migration's
    job. So this fixture mirrors what `precis migrate` sees on a real
    fresh deploy.
    """
    db_name = f"precis_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(PG_ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{db_name}"')

    test_url = _swap_database(PG_ADMIN_DSN, db_name)
    try:
        yield test_url
    finally:
        with psycopg.connect(PG_ADMIN_DSN, autocommit=True) as admin:
            # Terminate any leftover connections so DROP DATABASE succeeds.
            admin.execute(
                "SELECT pg_terminate_backend(pid) "
                "FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
            admin.execute(f'DROP DATABASE "{db_name}"')


@pytest.fixture
def store(fresh_db: str) -> Iterator[Store]:
    """Apply migrations against `fresh_db`, yield a connected Store."""
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()

    s = Store.connect(fresh_db)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def runtime_with_store(store: Store, hints: HintBus) -> PrecisRuntime:
    """Runtime backed by an ephemeral, migrated DB. Tests that need a full
    runtime+store stack take this fixture."""
    return PrecisRuntime(
        config=PrecisConfig(),
        registry=boot(store=store),
        hints=hints,
        store=store,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _swap_database(dsn: str, new_db: str) -> str:
    """Replace the database name in a postgres DSN.

    `postgresql://host/db` -> `postgresql://host/<new_db>`
    """
    base, sep, query = dsn.partition("?")
    last_slash = base.rfind("/")
    if last_slash < 0:
        return f"{base}/{new_db}{sep}{query}"
    return f"{base[: last_slash + 1]}{new_db}{sep}{query}"

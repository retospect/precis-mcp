"""Pytest fixtures (sync, psycopg 3).

Phase 1 fixtures: hub, runtime — stateless. Used by all tests that
don't touch the DB.

Phase 2 fixtures: store — postgres-backed. Tests that exercise the
store layer take ``store`` directly.

Test-DB lifecycle
-----------------

We share **one** postgres database across the whole test session:
``precis_test`` (owner ``precis_test``, password set in
``PRECIS_TEST_PG_URL``). At session start, an autouse fixture
**drops every table** in the test DB then applies all migrations
from ``0001`` so the schema is exactly what the source tree says
it is, no matter what shape the previous run left it in.

Per-test isolation is by ``TRUNCATE … CASCADE`` of every data
table at fixture entry — fast (ms scale), and preserves the
seeded vocabulary tables (``kinds``, ``chunk_kinds``,
``relations``, ``actors``, ``providers``, ``embedders``,
``summarizers``, ``artifact_kinds``) the migration installs and
that almost every handler test depends on.

Why not ephemeral DBs per test? Per-test CREATE DATABASE +
migrations costs ~1 s × hundreds of tests = unusable for tight
loops. TRUNCATE+CASCADE costs sub-millisecond and gives the same
isolation guarantee.

Setup (one-time, as the postgres superuser):

    CREATE ROLE precis_test WITH LOGIN PASSWORD '<pw>';
    CREATE DATABASE precis_test OWNER precis_test;
    \\c precis_test
    CREATE EXTENSION vector;
    CREATE EXTENSION btree_gist;
    GRANT ALL ON SCHEMA public TO precis_test;

Then export:

    PRECIS_TEST_PG_URL=postgresql://precis_test:<pw>@host.docker.internal:5432/precis_test

pgvector + btree_gist are pre-installed because they're "untrusted"
extensions only superuser can ``CREATE EXTENSION``. The migrations
use ``IF NOT EXISTS`` so they're no-ops once installed.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from precis.config import PrecisConfig
from precis.dispatch import Hub, boot
from precis.hints import HintBus
from precis.runtime import PrecisRuntime
from precis.store import Migrator, Store

# Test DSN. Default targets the local-dev convention
# (postgres at host.docker.internal:5432, role + db both
# ``precis_test``); override via env when running elsewhere.
PG_TEST_DSN = os.environ.get(
    "PRECIS_TEST_PG_URL",
    "postgresql://localhost/precis_test",
)

# Probed once per test session. Same gating as before: when the
# test DSN doesn't answer, every DB-tagged fixture skips so a
# DB-less environment (CI without postgres service, sandboxed
# shell) still runs the no-DB suite.
_PG_AVAILABLE: bool | None = None


def _pg_available() -> bool:
    """Cache and return whether ``PG_TEST_DSN`` answers a connect."""
    global _PG_AVAILABLE
    if _PG_AVAILABLE is None:
        try:
            with psycopg.connect(PG_TEST_DSN, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
            _PG_AVAILABLE = True
        except Exception:
            _PG_AVAILABLE = False
    return _PG_AVAILABLE


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "db: test requires a reachable PostgreSQL server at "
        "PRECIS_TEST_PG_URL (default postgresql://localhost/precis_test). "
        "Auto-skipped when unreachable.",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Auto-tag every test that uses ``store`` / ``hub`` / ``runtime_with_store``
    with the ``db`` marker. Lets ``-m 'not db'`` deselect the whole
    DB suite in environments that opt out."""
    db_fixtures = {
        "store",
        "hub",
        "hub_no_embedder",
        "runtime_with_store",
        "fresh_db",
    }
    for item in items:
        fixturenames = getattr(item, "fixturenames", ())
        if db_fixtures.intersection(fixturenames):
            item.add_marker(pytest.mark.db)


MIGRATIONS_DIR = Path(__file__).parent.parent / "src" / "precis" / "migrations"


# ---------------------------------------------------------------------------
# Stateless fixtures (no DB)
# ---------------------------------------------------------------------------


@pytest.fixture
def hints() -> HintBus:
    """Standalone HintBus for tests that exercise it directly."""
    return HintBus()


@pytest.fixture
def hub_stateless() -> Hub:
    """Stateless hub — calc only. Used by phase 1 tests."""
    return boot(store=None)


@pytest.fixture
def runtime_stateless(hub_stateless: Hub) -> PrecisRuntime:
    """Runtime with no store. Phase 1 tests use this."""
    return PrecisRuntime(config=PrecisConfig(), hub=hub_stateless)


# Backwards-compat aliases for existing phase-1 tests.
@pytest.fixture
def registry(hub_stateless: Hub) -> Hub:
    return hub_stateless


@pytest.fixture
def runtime(runtime_stateless: PrecisRuntime) -> PrecisRuntime:
    return runtime_stateless


# ---------------------------------------------------------------------------
# Postgres fixtures (shared precis_test DB; truncate-per-test isolation)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _initialise_test_db() -> None:
    """Drop all tables + re-apply migrations once per pytest session.

    Autouse so any test that touches the DB gets a known-good
    schema even without depending on ``store``. No-op when no
    postgres is reachable (the per-test fixtures will skip
    instead). Idempotent under re-runs.

    The drop is "everything in the public schema" — preserves the
    extensions (vector, btree_gist) which need superuser to
    re-install and are stable across runs.
    """
    if not _pg_available():
        return
    _drop_all_public_objects(PG_TEST_DSN)
    Migrator(PG_TEST_DSN, MIGRATIONS_DIR).apply_all()


@pytest.fixture
def store() -> Iterator[Store]:
    """Yield a Store backed by the shared test DB; truncate first.

    TRUNCATE every data table (CASCADE handles FKs) at fixture
    entry so each test starts from "schema present, vocabulary
    seeded, no data." Vocabulary tables (kinds / chunk_kinds /
    relations / actors / providers / embedders / summarizers /
    artifact_kinds) and the migrations ledger are preserved.

    Skips when no postgres reachable at ``PG_TEST_DSN``.
    """
    if not _pg_available():
        pytest.skip(
            f"postgres unreachable at {PG_TEST_DSN}; set PRECIS_TEST_PG_URL "
            "or start a server to run db-tagged tests"
        )
    _truncate_data_tables(PG_TEST_DSN)
    s = Store.connect(PG_TEST_DSN)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def fresh_db() -> Iterator[str]:
    """Yield a DSN pointing at a freshly-stripped test DB.

    Contract: schema is gone (every public table / view / sequence
    dropped) — caller is responsible for re-applying migrations.
    Migration-runner tests (``test_migrate.py``, ``test_initial_
    migration.py``) rely on this so they can assert "starting from
    nothing, the runner produces …".

    For handler / store tests that want a migrated DB with no data,
    use the ``store`` fixture instead — it's much cheaper (truncate
    vs. drop+rebuild).
    """
    if not _pg_available():
        pytest.skip(
            f"postgres unreachable at {PG_TEST_DSN}; set PRECIS_TEST_PG_URL "
            "or start a server to run db-tagged tests"
        )
    _drop_all_public_objects(PG_TEST_DSN)
    yield PG_TEST_DSN
    # Restore the schema for downstream tests in the same session.
    # The next ``store`` fixture call would otherwise fail because
    # the tables it wants to TRUNCATE don't exist.
    Migrator(PG_TEST_DSN, MIGRATIONS_DIR).apply_all()


@pytest.fixture
def runtime_with_store(store: Store) -> PrecisRuntime:
    """Runtime backed by the shared test DB."""
    return PrecisRuntime(config=PrecisConfig(), hub=boot(store=store))


@pytest.fixture
def hub(store: Store) -> Hub:
    """Hub bound to the test store + a MockEmbedder at the right dim."""
    from precis.embedder import MockEmbedder

    return Hub(store=store, embedder=MockEmbedder(dim=store.embedding_dim()))


@pytest.fixture
def hub_no_embedder(store: Store) -> Hub:
    """Hub with the test store but no embedder (lex-only code paths)."""
    return Hub(store=store)


# ---------------------------------------------------------------------------
# Schema lifecycle helpers
# ---------------------------------------------------------------------------

# Data tables that get TRUNCATEd between tests. Order doesn't
# matter (TRUNCATE … CASCADE handles FKs) but they're listed
# child-before-parent for readability. Vocabulary tables are
# explicitly NOT in this list — the migration seeds them and
# every test depends on the seed data.
_DATA_TABLES: tuple[str, ...] = (
    # Derived state — wiped first because their FKs cascade from
    # the parents below.
    "chunk_embeddings",
    "chunk_summaries",
    "ref_segment_sentences",
    "ref_segments",
    "ref_artifacts",
    "ref_events",
    # Tags + links + identifiers — secondary to refs/chunks.
    "chunk_tags",
    "ref_tags",
    "tags",
    "links",
    "ref_identifiers",
    # Core content.
    "chunks",
    "refs",
    "pdfs",
    # Cache.
    "cache_state",
    # Provenance — only the data tables (relation rows are vocab).
    "provenance_rw_cache",
    "provenance_rw_sync",
    # App-state KV.
    "app_state",
)


def _truncate_data_tables(dsn: str) -> None:
    """TRUNCATE every data table that exists. Idempotent."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        present = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        }
        targets = [t for t in _DATA_TABLES if t in present]
        if not targets:
            return
        # Single TRUNCATE so CASCADE is one round-trip; RESTART IDENTITY
        # so per-test ref_ids start from 1 (predictable across runs).
        conn.execute(
            f"TRUNCATE TABLE {', '.join(targets)} "
            "RESTART IDENTITY CASCADE"
        )


def _drop_all_public_objects(dsn: str) -> None:
    """Drop every table / view / sequence in the public schema.

    Preserves extensions (vector, btree_gist) since those need
    superuser to recreate and the test role doesn't have it. The
    next ``Migrator.apply_all`` rebuilds the schema from scratch.
    """
    with psycopg.connect(dsn, autocommit=True) as conn:
        # Drop views first (they may depend on tables we're about
        # to drop); then tables CASCADE; then orphan sequences.
        for kind, drop_kw in (
            ("VIEW",     "CASCADE"),
            ("TABLE",    "CASCADE"),
            ("SEQUENCE", "CASCADE"),
        ):
            rows = conn.execute(
                "SELECT relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' "
                "  AND c.relkind = %s",
                (
                    {"VIEW": "v", "TABLE": "r", "SEQUENCE": "S"}[kind],
                ),
            ).fetchall()
            for (name,) in rows:
                conn.execute(f'DROP {kind} IF EXISTS "{name}" {drop_kw}')
        # Drop leftover functions written by our migrations (e.g. the
        # future WORM trigger). EXCLUDE functions owned by extensions
        # — pgvector et al. install ``vector_in``, ``btree_gist_ops``,
        # etc. in public, and dropping those nukes the extension's
        # operators. The pg_depend EXISTS predicate filters them out.
        funcs = conn.execute(
            "SELECT proname FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM pg_depend d "
            "     WHERE d.objid = p.oid AND d.deptype = 'e'"
            "  )"
        ).fetchall()
        for (name,) in funcs:
            conn.execute(f'DROP FUNCTION IF EXISTS "{name}" CASCADE')

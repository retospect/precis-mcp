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
    CREATE EXTENSION pgcrypto;   -- secrets vault (migration 0059)
    GRANT ALL ON SCHEMA public TO precis_test;

Then export:

    PRECIS_TEST_PG_URL=postgresql://precis_test:<pw>@host.docker.internal:5432/precis_test

pgvector + btree_gist are pre-installed because they're "untrusted"
extensions only superuser can ``CREATE EXTENSION``. The migrations
use ``IF NOT EXISTS`` so they're no-ops once installed.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

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

log = logging.getLogger(__name__)

# ``PG_TEST_DSN`` names the *template*: the maintained ``precis_test`` DB
# (server + schema + pre-installed pgvector/btree_gist extensions). Each
# pytest session clones the template into a private ``precis_test_<uuid>``
# (see ``_initialise_test_db``) so sessions never share a physical DB.
# ``_ACTIVE_DSN`` — the clone — is what every per-test fixture connects to.
_TEMPLATE_DB = str(conninfo_to_dict(PG_TEST_DSN).get("dbname") or "postgres")
_ACTIVE_DSN = PG_TEST_DSN


def _active_dsn() -> str:
    """The DSN the current session's per-test fixtures use: the session's
    private clone once ``_initialise_test_db`` has run, else the template."""
    return _ACTIVE_DSN


def _dsn_with_db(dsn: str, dbname: str) -> str:
    """``dsn`` with its database swapped for ``dbname`` (server + creds kept)."""
    return make_conninfo(dsn, dbname=dbname)


def _drop_db(admin: psycopg.Connection, name: str) -> None:
    """Force-drop database ``name``, terminating its backends first."""
    admin.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid()",
        (name,),
    )
    admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _ensure_template_cloneable(admin: psycopg.Connection) -> None:
    """Best-effort startup auto-config: mark the template ``IS_TEMPLATE`` so
    a CREATEDB role can clone it. Succeeds when the connecting role owns the
    template (the local ``precis`` owns ``precis_test``) or is superuser;
    silently ignored otherwise (then cloning relies on plain ownership, or
    falls back). The CREATEDB privilege itself can only be granted by a
    superuser, so it is NOT auto-fixable here — see the fallback warning."""
    try:
        admin.execute(f'ALTER DATABASE "{_TEMPLATE_DB}" WITH IS_TEMPLATE true')
    except psycopg.Error:
        pass  # not owner/superuser — rely on ownership or pre-set template


def _sweep_orphan_clones(admin: psycopg.Connection) -> None:
    """Drop ephemeral ``precis_test_<uuid>`` clones left by crashed prior
    sessions. A clone in use by a *live* concurrent session still has
    connections, so its plain ``DROP`` raises ``ObjectInUse`` and we skip
    it — only truly idle orphans are reclaimed. The name regex (12 hex
    chars) won't match a hand-made ``precis_test_foo`` dev DB."""
    rows = admin.execute(
        "SELECT datname FROM pg_database WHERE datname ~ %s",
        (rf"^{_TEMPLATE_DB}_[0-9a-f]{{12}}$",),
    ).fetchall()
    for (name,) in rows:
        try:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        except psycopg.Error:
            pass  # in use by a live concurrent session — leave it


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
def _force_mock_embedder_for_tests() -> None:
    """Pin tests to :class:`MockEmbedder` regardless of the host env.

    The compose-driven dev container sets ``PRECIS_EMBEDDER=bge-m3``
    (the production value) — fine for the running service, but it
    forces in-process CLI tests (``cli.main()``) to attempt loading
    the bge-m3 weights and they fail with ``Upstream("embedder
    warming")`` when the weights aren't on disk. The ``hub`` fixture
    already binds :class:`MockEmbedder`, but the CLI tests bypass
    that path and re-resolve via :class:`PrecisConfig`, which reads
    ``PRECIS_EMBEDDER`` from env.

    Unset both knobs at session start so :class:`PrecisConfig` falls
    back to its ``"mock"`` default. Tests that genuinely want the
    hot embedder can ``monkeypatch.setenv("PRECIS_EMBEDDER",
    "remote")`` per-test. Run *before* ``_initialise_test_db`` purely
    by declaration order — autouse session fixtures fire in source
    order.
    """
    import os

    os.environ.pop("PRECIS_EMBEDDER", None)
    os.environ.pop("PRECIS_EMBEDDER_URL", None)


# Fixed 64-bit key for the session-wide advisory lock that serialises
# concurrent pytest sessions against the shared ``precis_test`` DB.
# Arbitrary but stable; ``0x70726563`` spells ``prec`` in ASCII.
_SESSION_DB_LOCK_KEY = 0x70726563


def _claim_template_maintenance(admin_dsn: str) -> bool:
    """True if THIS session should (re)prepare the shared template.

    Migrating + stripping the ``precis_test`` template to schema-only only
    needs to happen ONCE per test run — but under ``-n auto`` every xdist
    worker is its own session and was doing it under the advisory lock, so
    ~15 redundant maintenance passes serialised into the dominant gate cost.
    Claim the work via a marker keyed by the xdist run id
    (``PYTEST_XDIST_TESTRUNUID``, shared across a run's workers): the first
    worker to INSERT it does the maintenance, the rest skip straight to
    cloning the already-prepared template. The clone itself stays per-worker.

    Called under the advisory lock, so the claim is race-free. Falls back to
    ``True`` (do the maintenance) when there's no run id (``-n0`` / no xdist)
    or the marker table can't be maintained (restricted role) — correct,
    just back to the old per-worker behaviour.
    """
    runuid = os.environ.get("PYTEST_XDIST_TESTRUNUID")
    if not runuid:
        return True  # single-process run — nothing to coordinate
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as adm:
            adm.execute(
                "CREATE TABLE IF NOT EXISTS _precis_test_runs "
                "(runuid text PRIMARY KEY, at timestamptz NOT NULL DEFAULT now())"
            )
            adm.execute(
                "DELETE FROM _precis_test_runs WHERE at < now() - interval '1 hour'"
            )
            cur = adm.execute(
                "INSERT INTO _precis_test_runs(runuid) VALUES (%s) "
                "ON CONFLICT DO NOTHING RETURNING runuid",
                (runuid,),
            )
            return cur.fetchone() is not None
    except psycopg.Error:
        return True  # can't coordinate → prepare it ourselves


@pytest.fixture(scope="session", autouse=True)
def _initialise_test_db() -> Iterator[None]:
    """Give each pytest session its OWN ephemeral database — an instant
    clone of the maintained ``precis_test`` template — and drop it at
    session end.

    The whole suite used to share one physical ``precis_test``, so per-test
    ``TRUNCATE … CASCADE`` and ``fresh_db`` drop/rebuilds deadlocked across
    concurrent sessions (CI re-trigger, sibling agents) AND even within one
    run under ``pytest-randomly`` ordering. Now each session runs
    ``CREATE DATABASE precis_test_<uuid> TEMPLATE precis_test`` — a fast
    file-copy clone that inherits the schema + the pre-installed pgvector /
    btree_gist extensions (so no superuser ``CREATE EXTENSION`` is needed),
    works against its private copy, and ``DROP``s it on teardown. Sessions
    no longer contend: the only shared step is keeping the template migrated
    and taking the clone, serialised by a short advisory lock.

    Requires the connecting role to have CREATEDB AND to be able to copy
    the template — i.e. it owns ``precis_test`` or that DB is marked
    ``IS_TEMPLATE``. One-time server setup::

        ALTER ROLE <role> CREATEDB;
        ALTER DATABASE precis_test WITH IS_TEMPLATE true;

    If cloning isn't permitted we fall back to the shared template DB (the
    old serialise-by-advisory-lock behaviour) so a restricted environment
    still runs — without per-session isolation, and a warning is logged.
    No-op when no postgres is reachable (the per-test fixtures skip instead).
    """
    global _ACTIVE_DSN
    if not _pg_available():
        yield
        return

    admin_dsn = _dsn_with_db(PG_TEST_DSN, "postgres")
    clone_db = f"{_TEMPLATE_DB}_{uuid.uuid4().hex[:12]}"
    clone_dsn = _dsn_with_db(PG_TEST_DSN, clone_db)

    # Serialise template maintenance + the clone across sessions. Hold the
    # (server-global) advisory lock on the `postgres` admin DB, NOT the
    # template: `CREATE DATABASE … TEMPLATE` needs zero live connections to
    # the template, and the pre-clone terminate below would otherwise kill
    # the lock connection itself.
    lock_conn = psycopg.connect(admin_dsn, autocommit=True)
    lock_conn.execute("SELECT pg_advisory_lock(%s)", (_SESSION_DB_LOCK_KEY,))
    keepalive: psycopg.Connection | None = None
    shared_fallback = False
    try:
        # Keep the template current + data-free, so every clone starts from
        # "schema + vocab, no data". apply_all is an idempotent tail apply.
        # Do it once per run (first worker claims it), not once per worker —
        # see _claim_template_maintenance.
        if _claim_template_maintenance(admin_dsn):
            Migrator(PG_TEST_DSN, MIGRATIONS_DIR).apply_all()
            _truncate_data_tables(PG_TEST_DSN)
        try:
            with psycopg.connect(admin_dsn, autocommit=True) as adm:
                _ensure_template_cloneable(adm)
                _sweep_orphan_clones(adm)
                # No connections to the template may exist during the clone.
                adm.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (_TEMPLATE_DB,),
                )
                # STRATEGY=FILE_COPY (PG15+): checkpoint + filesystem copy
                # instead of the default WAL_LOG, which WAL-logs every copied
                # block — much slower for template cloning, especially over the
                # container→host DB hop that dominates the gate. Falls back to
                # the default for a server too old to know the keyword.
                try:
                    adm.execute(
                        f'CREATE DATABASE "{clone_db}" TEMPLATE "{_TEMPLATE_DB}" '
                        "STRATEGY = FILE_COPY"
                    )
                except psycopg.errors.SyntaxError:
                    adm.execute(
                        f'CREATE DATABASE "{clone_db}" TEMPLATE "{_TEMPLATE_DB}"'
                    )
            # Hold a connection to the clone for the whole session so a
            # concurrent session's orphan-sweep sees it as in-use. Autocommit
            # so it never sits idle-in-transaction.
            keepalive = psycopg.connect(clone_dsn, autocommit=True)
            _ACTIVE_DSN = clone_dsn
        except psycopg.errors.InsufficientPrivilege:
            msg = (
                "per-session test-DB isolation is DISABLED: the test role cannot "
                f"CREATE DATABASE, so the suite shares one physical {_TEMPLATE_DB!r} "
                "and can deadlock under concurrent / pytest-randomly runs. Fix once, "
                "as a superuser: 'ALTER ROLE <test-role> CREATEDB;' (the template "
                "IS_TEMPLATE bit is auto-set when the role owns it)."
            )
            log.warning("conftest: %s", msg)
            warnings.warn(msg, stacklevel=2)
            shared_fallback = True
            _ACTIVE_DSN = PG_TEST_DSN
    finally:
        # Clone path: the lock was only needed for the clone, release now.
        # Shared fallback: hold it for the whole session (old behaviour).
        if not shared_fallback:
            lock_conn.execute("SELECT pg_advisory_unlock(%s)", (_SESSION_DB_LOCK_KEY,))
            lock_conn.close()

    try:
        yield
    finally:
        _ACTIVE_DSN = PG_TEST_DSN
        if keepalive is not None:
            keepalive.close()
        if shared_fallback:
            lock_conn.execute("SELECT pg_advisory_unlock(%s)", (_SESSION_DB_LOCK_KEY,))
            lock_conn.close()
        else:
            with psycopg.connect(admin_dsn, autocommit=True) as adm:
                _drop_db(adm, clone_db)


@pytest.fixture
def store() -> Iterator[Store]:
    """Yield a Store backed by the shared test DB; truncate first.

    TRUNCATE every public data table (CASCADE handles FKs) at
    fixture entry so each test starts from "schema present,
    vocabulary seeded, no data." Only the seeded vocabulary and the
    migrations ledger survive — see :data:`_PRESERVE_TABLES`. This is
    a deny-list on purpose: any new data table is wiped automatically,
    so a forgotten table can't pollute the shared DB across tests.

    Skips when no postgres reachable at ``PG_TEST_DSN``.
    """
    if not _pg_available():
        pytest.skip(
            f"postgres unreachable at {PG_TEST_DSN}; set PRECIS_TEST_PG_URL "
            "or start a server to run db-tagged tests"
        )
    _truncate_data_tables(_active_dsn())
    s = Store.connect(_active_dsn())
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
    _drop_all_public_objects(_active_dsn())
    yield _active_dsn()
    # Restore the schema for downstream tests in the same session.
    # The next ``store`` fixture call would otherwise fail because
    # the tables it wants to TRUNCATE don't exist.
    Migrator(_active_dsn(), MIGRATIONS_DIR).apply_all()


@pytest.fixture
def drop_public_objects() -> Callable[[str], None]:
    """Return the drop-all-public-objects helper (preserves extensions).

    For tests that build the schema more than one way in a single body —
    e.g. the baseline-vs-replay convergence test, which drops between the
    two builds. Exposed as a fixture so tests don't import conftest
    internals directly.
    """
    return _drop_all_public_objects


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
# Cross-suite parse helpers
# ---------------------------------------------------------------------------


def id_of(body: str) -> int:
    """Parse ``id=N`` out of a create / update / tag ack response body.

    Robust against trailing punctuation in the TOON ``Next:`` trailer
    (``delete(kind='memory', id=42)`` would otherwise pollute a
    ``rsplit("=", 1)[1]`` parse with the close paren). Looks for the
    first occurrence of ``id=`` after the leading ``created <kind>``
    / ``updated <kind>`` / ``tagged <kind>`` clause and strips any
    trailing ``).,`` characters.

    Used by every per-handler test that needs the assigned numeric id
    of a freshly-put numeric ref. The same shape lives inline in
    test_todo_tree / test_schedule / test_nursery — moved here so a
    future trailer reword (Slice-3 ack changes, etc.) is a one-line
    fix instead of a sweep.

    ADR 0036: the create-ack now reads ``created <kind> <handle>.``
    (e.g. ``created memory me158.``). Prefer the first record handle in
    the ack's leading line; fall back to the legacy ``id=N`` form (still
    used by update/tag acks and code-less kinds).
    """
    from precis.utils import handle_registry

    head = body.split("\n", 1)[0]
    for tok in head.replace(",", " ").replace(".", " ").split():
        parsed = handle_registry.parse(tok)
        if parsed is not None and not parsed[1]:  # a record (non-chunk) handle
            return parsed[2]
    return int(body.split("id=", 1)[1].split()[0].rstrip(",.()"))


def record_handle(store: Store, slug: str, *, kind: str = "paper") -> str:
    """The ADR 0036 universal record handle (e.g. ``pa123``) for a
    slug-addressed ref — the form output now emits for the record itself."""
    from precis.utils import handle_registry

    ref = store.get_ref(kind=kind, id=slug)
    assert ref is not None, f"no live {kind} {slug!r}"
    return handle_registry.format_handle(kind, ref.id)


def chunk_handle(store: Store, slug: str, *, kind: str = "paper", ord: int = 0) -> str:
    """The ADR 0036 universal chunk handle (e.g. ``pc40``) for a
    slug-addressed ref's body chunk at ``ord``.

    The cutover replaced the legacy ``slug~pos`` in search/read output with
    this computed handle; tests that asserted the old form now resolve the
    chunk_id here and compute the handle the same way the emitters do.
    """
    from precis.utils import handle_registry

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT c.chunk_id FROM chunks c "
            "JOIN ref_identifiers ri ON ri.ref_id = c.ref_id "
            "WHERE ri.id_kind = 'cite_key' AND ri.id_value = %s AND c.ord = %s",
            (slug, ord),
        ).fetchone()
    assert row is not None, f"no chunk for {slug}~{ord}"
    return handle_registry.format_handle(kind, int(row[0]), chunk=True)


# ---------------------------------------------------------------------------
# Schema lifecycle helpers
# ---------------------------------------------------------------------------

# Data tables that get TRUNCATEd between tests. Order doesn't
# matter (TRUNCATE … CASCADE handles FKs) but they're listed
# child-before-parent for readability. Vocabulary tables are
# explicitly NOT in this list — the migration seeds them and
# every test depends on the seed data.
# Truncation is a DENY-list, not an allow-list: every public base table is
# wiped between tests EXCEPT the ones named here. This is deliberate — an
# allow-list silently rots (every feature that adds a data table — worker_logs,
# cad_nodes, cluster_*, host_heartbeat, … — has to remember to register it, and
# the forgotten ones accumulate across the shared ``precis_test`` DB and poison
# count-based assertions, e.g. test_status_sql's `assert 6 == 3`). The
# preserve-set is small and stable: the migrations ledger plus the seeded
# vocabulary every test relies on. CASCADE handles FK ordering in one shot.
_PRESERVE_TABLES: frozenset[str] = frozenset(
    {
        # Migrations ledger — never data.
        "_migrations",
        # Seeded vocabulary (populated by migrations; tests depend on it).
        "kinds",
        "chunk_kinds",
        "relations",
        "actors",
        "providers",
        "embedders",
        "summarizers",
        "artifact_kinds",
        "kind_provider",  # vocab mapping, seeded by 0022
        "news_sources",  # seeded reference rows, 0033
    }
)


def _run_with_lock_retry(conn: psycopg.Connection, stmt: str) -> None:
    """Run a lock-taking statement (TRUNCATE / DROP), retrying on deadlock
    or lock-timeout. A test that leaks a connection (a worker or pool not
    closed) can briefly hold a RowExclusiveLock that collides with the next
    fixture's AccessExclusiveLock under pytest-randomly ordering. The leak
    is transient — the writer finishes and releases — so a short retry
    succeeds. This is the privilege-free alternative to terminating the
    other backend (a non-superuser test role can't ``pg_terminate_backend``);
    ``lock_timeout`` keeps a single wait bounded so we retry rather than
    block, and a true deadlock cycle aborts us (not the leaker) so we retry
    too."""
    conn.execute("SET lock_timeout = '5s'")
    for attempt in range(10):
        try:
            conn.execute(stmt)
            return
        except (psycopg.errors.DeadlockDetected, psycopg.errors.LockNotAvailable):
            if attempt == 9:
                raise
            time.sleep(0.1 * (attempt + 1))


def _truncate_data_tables(dsn: str) -> None:
    """TRUNCATE every public data table — all of them except the seeded
    vocabulary / ledger in :data:`_PRESERVE_TABLES`. Idempotent."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        present = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        }
        targets = sorted(present - _PRESERVE_TABLES)
        if not targets:
            return
        # Single TRUNCATE so CASCADE is one round-trip; RESTART IDENTITY
        # so per-test ref_ids start from 1 (predictable across runs).
        _run_with_lock_retry(
            conn, f"TRUNCATE TABLE {', '.join(targets)} RESTART IDENTITY CASCADE"
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
            ("VIEW", "CASCADE"),
            ("TABLE", "CASCADE"),
            ("SEQUENCE", "CASCADE"),
        ):
            rows = conn.execute(
                "SELECT relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' "
                "  AND c.relkind = %s",
                ({"VIEW": "v", "TABLE": "r", "SEQUENCE": "S"}[kind],),
            ).fetchall()
            for (name,) in rows:
                _run_with_lock_retry(conn, f'DROP {kind} IF EXISTS "{name}" {drop_kw}')
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
            _run_with_lock_retry(conn, f'DROP FUNCTION IF EXISTS "{name}" CASCADE')

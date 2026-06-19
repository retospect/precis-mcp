"""Forward-only SQL migration runner (sync, psycopg 3).

Reads ``*.sql`` files from one or more migration sources, applies any
whose ``(plugin, version)`` pair isn't already in the ``_migrations``
ledger, records the version + checksum on success. Each migration
runs in its own transaction.

Versioning: filename without ``.sql`` extension. Filenames must sort
correctly (e.g. ``0001_initial.sql``, ``0002_add_xxx.sql``). The
``plugin`` namespace disambiguates: ``precis`` and ``precis_dft``
can both ship a ``0001_*.sql`` without collision.

Refuses to run if a previously-applied migration's checksum no
longer matches its file (someone edited a sealed migration).

Plugin migrations are discovered via the ``precis.migrations``
entry-point group. Each entry resolves to a directory containing
the plugin's ``*.sql`` files. Failure isolation mirrors
``precis.dispatch._load_plugins``: one broken plugin must not brick
``precis migrate``.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import psycopg
from psycopg.errors import UndefinedColumn, UndefinedTable

log = logging.getLogger(__name__)

#: Entry-point group third-party plugins use to advertise their
#: migrations. Each entry resolves to a string ``"pkg.subpkg"`` —
#: the plugin's migrations directory is the resolved package's
#: filesystem path.
MIGRATIONS_PLUGIN_GROUP = "precis.migrations"

#: The plugin name under which the in-tree precis migrations are
#: recorded. Old rows (predating the ``plugin`` column) are
#: backfilled with this value by the column DEFAULT in
#: ``0023_migrations_plugin.sql``.
PRECIS_PLUGIN_NAME = "precis"


class MigrationSource(NamedTuple):
    """One plugin's contribution to the migration ledger.

    ``plugin`` is the namespace recorded into ``_migrations.plugin``;
    ``dir`` is the directory whose ``*.sql`` files belong to that
    plugin. The in-tree source is
    ``MigrationSource("precis", <package dir>/migrations)``.
    """

    plugin: str
    dir: Path


@dataclass(frozen=True, slots=True)
class MigrationFile:
    plugin: str
    version: str
    path: Path
    sql: str
    checksum: str


def _entry_points(group: str) -> list[Any]:
    """Indirection wrapper around ``importlib.metadata.entry_points``.

    Mirrors the pattern in :mod:`precis.workers.job_types` and
    :mod:`precis.dispatch` so tests can patch this single function
    to inject fake plugin sources without setting up a real wheel
    install.
    """
    from importlib.metadata import entry_points

    return list(entry_points(group=group))


def _load_migrations(source: MigrationSource) -> list[MigrationFile]:
    """Read all ``*.sql`` files from one source, sorted by filename.

    Each file is tagged with the source's plugin so the apply loop
    knows which ``_migrations.plugin`` value to record.
    """
    if not source.dir.is_dir():
        raise FileNotFoundError(
            f"migrations directory not found: {source.dir} (plugin={source.plugin!r})"
        )

    files: list[MigrationFile] = []
    for path in sorted(source.dir.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        files.append(
            MigrationFile(
                plugin=source.plugin,
                version=path.stem,
                path=path,
                sql=sql,
                checksum=checksum,
            )
        )
    return files


def _has_plugin_column(conn: psycopg.Connection) -> bool:
    """Return True once 0023_migrations_plugin has applied.

    Used by the apply loop to switch INSERT shape mid-bootstrap
    on a fresh database. Pre-0023 the column doesn't exist and
    inserts must omit it; post-0023 the column is mandatory.
    """
    try:
        conn.execute("SELECT plugin FROM public._migrations LIMIT 0").fetchall()
    except (UndefinedColumn, UndefinedTable):
        conn.rollback()
        return False
    return True


def _applied_versions(
    conn: psycopg.Connection,
) -> dict[tuple[str, str], str]:
    """Return ``{(plugin, version): checksum}`` from ``_migrations``.

    Empty dict if the table doesn't exist (fresh database). Handles
    three states gracefully:

    1. No table at all — return ``{}``.
    2. Table exists but ``plugin`` column doesn't (pre-0023 schema):
       fall back to the old shape, tag every row with
       ``PRECIS_PLUGIN_NAME``.
    3. Both table and column exist (post-0023 — the common case):
       read directly.

    Uses the schema-qualified ``public._migrations`` — a pg_dump
    migration body can set ``search_path = ''`` mid-apply, which
    leaves later bare references unresolved.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT plugin, version, checksum FROM public._migrations")
            rows = cur.fetchall()
        return {(r[0], r[1]): r[2] for r in rows}
    except UndefinedColumn:
        conn.rollback()
        # Pre-0023 schema: table exists but plugin column doesn't.
        # Fall through to the legacy SELECT.
    except UndefinedTable:
        conn.rollback()
        return {}

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version, checksum FROM public._migrations")
            rows = cur.fetchall()
    except UndefinedTable:
        conn.rollback()
        return {}
    return {(PRECIS_PLUGIN_NAME, r[0]): r[1] for r in rows}


def _is_fresh_db(conn: psycopg.Connection) -> bool:
    """True when the ``public._migrations`` ledger table does not exist.

    A truly-fresh database (no schema yet) is the only state where
    loading a baseline snapshot is safe: a DB that already has the
    ledger has a migration history to advance, not replace.
    """
    row = conn.execute(
        "SELECT to_regclass('public._migrations') IS NOT NULL"
    ).fetchone()
    return not (row and row[0])


# Backcompat: callers built the runner with a single directory.
# Keep that path working so the CLI can land the plugin discovery
# change without ripple.
_LegacyDir = Path
_Sources = list[MigrationSource]


class Migrator:
    """Forward-only migration runner.

    Two construction shapes for backcompat:

    - ``Migrator(dsn, migrations_dir)`` — legacy single-source.
      Equivalent to ``Migrator(dsn, [MigrationSource("precis", migrations_dir)])``.
    - ``Migrator(dsn, sources=[MigrationSource(...)])`` — explicit
      multi-source path used by ``cli/migrate.py`` once plugin
      discovery lands.

    Use :meth:`discover_sources` to build the source list including
    plugin migrations advertised via the ``precis.migrations`` entry
    point group.
    """

    def __init__(
        self,
        dsn: str,
        sources_or_dir: _Sources | _LegacyDir,
        *,
        baseline: Path | None = None,
    ) -> None:
        self.dsn = dsn
        if isinstance(sources_or_dir, Path):
            self.sources: list[MigrationSource] = [
                MigrationSource(PRECIS_PLUGIN_NAME, sources_or_dir)
            ]
        else:
            self.sources = list(sources_or_dir)
        # Optional per-release baseline snapshot. When set and the
        # target DB is truly fresh, :meth:`apply_all` loads this single
        # file (which self-stamps the ``_migrations`` ledger) instead of
        # replaying the whole numbered chain, then applies any migrations
        # added since the snapshot as a normal tail. ``None`` (the
        # default, and what the test fixtures use) keeps the historical
        # full-replay behaviour, which is also the from-scratch reference
        # the baseline is validated against.
        self.baseline = baseline if (baseline and baseline.exists()) else None

    @classmethod
    def discover_sources(cls, builtin_dir: Path) -> list[MigrationSource]:
        """Compose the source list: built-in first, then plugins.

        ``builtin_dir`` is the precis-core migrations dir (the
        ``src/precis/migrations/`` shipped with this package).
        Plugin sources come from the ``precis.migrations``
        entry-point group; each entry resolves to a module name
        whose package directory is the plugin's migration root.

        Plugin discovery failures are logged, not raised. A broken
        plugin must not block ``precis migrate``.
        """
        sources: list[MigrationSource] = [
            MigrationSource(PRECIS_PLUGIN_NAME, builtin_dir)
        ]
        try:
            eps = _entry_points(MIGRATIONS_PLUGIN_GROUP)
        except Exception as exc:  # defensive
            log.warning("precis.migrations discovery failed: %s", exc)
            return sources

        for ep in eps:
            name = getattr(ep, "name", "<unknown>")
            try:
                resolved = ep.load()
            except Exception as exc:
                log.warning(
                    "precis.migrations plugin %r failed to load (%s): %s",
                    name,
                    type(exc).__name__,
                    exc,
                )
                continue
            try:
                dir_path = _resolve_to_dir(resolved)
            except Exception as exc:
                log.warning(
                    "precis.migrations plugin %r could not resolve "
                    "to a directory (%s): %s",
                    name,
                    type(exc).__name__,
                    exc,
                )
                continue
            if dir_path is None:
                log.warning(
                    "precis.migrations plugin %r resolved to a non-directory; skipping",
                    name,
                )
                continue
            # Use the EP name as the plugin namespace by default;
            # this matches the convention "the plugin name in
            # pyproject.toml's entry-point key is the migration
            # namespace".
            sources.append(MigrationSource(name, dir_path))
        return sources

    def applied_versions(self) -> list[tuple[str, str]]:
        """Return sorted ``(plugin, version)`` pairs for applied
        migrations. Stable order for diffing across runs."""
        with psycopg.connect(self.dsn) as conn:
            applied = _applied_versions(conn)
        return sorted(applied)

    def pending(self) -> list[tuple[str, str]]:
        """Return ``(plugin, version)`` pairs not yet applied.

        On a fresh DB with a baseline configured, the versions the
        baseline would self-stamp are treated as already applied, so the
        report reflects the post-snapshot tail :meth:`apply_all` will
        actually run — not the whole chain.
        """
        with psycopg.connect(self.dsn) as conn:
            applied = _applied_versions(conn)
            if self.baseline is not None and _is_fresh_db(conn):
                from precis.store.schema_dump import parse_baseline_ledger

                baked = parse_baseline_ledger(self.baseline.read_text(encoding="utf-8"))
                for version, checksum in baked:
                    applied.setdefault((PRECIS_PLUGIN_NAME, version), checksum)
        out: list[tuple[str, str]] = []
        for source in self.sources:
            for f in _load_migrations(source):
                if (f.plugin, f.version) not in applied:
                    out.append((f.plugin, f.version))
        return out

    def _load_baseline(self, conn: psycopg.Connection) -> None:
        """Load the baseline snapshot into a fresh DB in one transaction.

        The snapshot is a ``pg_dump``-shaped file (schema + seed data +
        a synthesised ``_migrations`` COPY block), so it goes through the
        same ``_execute_dump_sql`` driver the numbered migrations use.
        ``search_path`` is reset afterwards because the dump body sets it
        to ``''`` for the session.
        """
        assert self.baseline is not None
        sql = self.baseline.read_text(encoding="utf-8")
        log.info("bootstrapping fresh DB from baseline %s", self.baseline)
        with conn.transaction():
            with conn.cursor() as cur:
                _execute_dump_sql(cur, sql)
        conn.execute("RESET search_path")

    def apply_all(self) -> list[tuple[str, str]]:
        """Apply every pending migration. Returns the
        ``(plugin, version)`` pairs newly applied during this call.

        Within a single source, files apply in version-name order.
        Across sources, the built-in ``precis`` source runs first
        so its core schema is in place before any plugin touches
        it; remaining sources run in the order returned by
        :meth:`discover_sources` (which is the order plugins
        appear in ``importlib.metadata.entry_points``).
        """
        all_files: list[MigrationFile] = []
        for source in self.sources:
            try:
                all_files.extend(_load_migrations(source))
            except FileNotFoundError as exc:
                log.warning(
                    "precis.migrations plugin %r dir missing: %s",
                    source.plugin,
                    exc,
                )

        if not all_files:
            return []

        newly_applied: list[tuple[str, str]] = []

        # autocommit=True is load-bearing: under autocommit=False
        # the very first SELECT (or _applied_versions' fallback
        # rollback) puts us in an implicit transaction, and
        # ``with conn.transaction()`` then downgrades to a
        # SAVEPOINT. If the migration SQL aborts, the savepoint
        # disappears and the context-manager exit raises
        # ``InvalidSavepointSpecification`` — masking the real SQL
        # error. With autocommit=True, ``conn.transaction()`` issues
        # a real BEGIN/COMMIT and surfaces any inner exception
        # directly.
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            # Per-release baseline bootstrap: on a truly-fresh DB (no
            # ``_migrations`` table yet) load the snapshot in one shot
            # instead of replaying the chain. The snapshot ships a
            # ``_migrations`` COPY block, so after this load the ledger
            # is stamped to the baked-in head and the loop below applies
            # only the post-snapshot tail. A non-fresh DB skips this
            # entirely and migrates forward as always.
            if self.baseline is not None and _is_fresh_db(conn):
                self._load_baseline(conn)

            applied = _applied_versions(conn)
            has_plugin_col = _has_plugin_column(conn)

            # Integrity check on already-applied migrations.
            for f in all_files:
                key = (f.plugin, f.version)
                if key in applied and applied[key] != f.checksum:
                    raise RuntimeError(
                        f"checksum mismatch for already-applied migration "
                        f"{f.plugin}/{f.version!r}: file has "
                        f"{f.checksum[:12]}, DB has "
                        f"{applied[key][:12]}. Refusing to run — sealed "
                        "migrations must not be edited."
                    )

            for f in all_files:
                key = (f.plugin, f.version)
                if key in applied:
                    continue
                log.info("applying migration %s/%s", f.plugin, f.version)
                with conn.transaction():
                    with conn.cursor() as cur:
                        _execute_dump_sql(cur, f.sql)
                        # Choose INSERT shape based on whether the
                        # plugin column exists *as of this attempt*.
                        # On a fresh DB the 0023 migration creates the
                        # column mid-bootstrap; we re-check after each
                        # migration so the 0024+ inserts pick up the
                        # new shape automatically.
                        if has_plugin_col:
                            cur.execute(
                                "INSERT INTO public._migrations "
                                "(plugin, version, checksum) "
                                "VALUES (%s, %s, %s)",
                                (f.plugin, f.version, f.checksum),
                            )
                        else:
                            if f.plugin != PRECIS_PLUGIN_NAME:
                                # Sanity check: plugin migrations
                                # cannot precede 0023. A misordered
                                # source list would land here.
                                raise RuntimeError(
                                    f"plugin migration {f.plugin}/{f.version} "
                                    "ordered before 0023_migrations_plugin "
                                    "applied; cannot record without the "
                                    "plugin column."
                                )
                            cur.execute(
                                "INSERT INTO public._migrations "
                                "(version, checksum) "
                                "VALUES (%s, %s)",
                                (f.version, f.checksum),
                            )
                # A pg_dump-style migration body (0001) runs
                # ``set_config('search_path', '', false)`` which
                # persists on this shared connection for the SESSION,
                # not just the transaction. Subsequent hand-written
                # migrations use bare table names (``chunks``,
                # ``relations``, …) and would fail to resolve them
                # ("relation chunks does not exist") on a fresh full
                # apply. RESET restores the connection's startup
                # default (``"$user", public``) between migrations so
                # each forward migration starts from a sane
                # search_path.
                conn.execute("RESET search_path")
                # 0023 itself adds the plugin column. After it
                # applies, switch the INSERT shape so subsequent
                # rows carry an explicit plugin value.
                if not has_plugin_col:
                    has_plugin_col = _has_plugin_column(conn)
                newly_applied.append(key)
                log.info("  applied %s/%s ok", f.plugin, f.version)

        return newly_applied


def _resolve_to_dir(resolved: Any) -> Path | None:
    """Resolve an entry-point's loaded object to a migrations dir.

    Accepts:
    - A ``str`` or ``Path`` — used as a filesystem path directly.
    - A module — the directory containing the module's
      ``__init__.py`` is the migrations root.
    - A callable returning one of the above — useful for plugins
      that compose a path at discovery time.

    Returns ``None`` if the resolved value isn't a directory after
    coercion. Callers log and skip.
    """
    if callable(resolved) and not hasattr(resolved, "__path__"):
        resolved = resolved()

    if isinstance(resolved, (str, Path)):
        path = Path(resolved)
        return path if path.is_dir() else None

    # Module-like object: take its package directory.
    package_path = getattr(resolved, "__path__", None)
    if package_path is None:
        # Maybe a regular module with __file__.
        file_attr = getattr(resolved, "__file__", None)
        if file_attr is None:
            return None
        candidate = Path(file_attr).parent
        return candidate if candidate.is_dir() else None

    # __path__ is iterable; take the first entry. Plugins are
    # expected to be regular packages, not namespace packages with
    # multiple roots.
    paths: Iterable[str] = package_path
    for entry in paths:
        candidate = Path(entry)
        if candidate.is_dir():
            return candidate
    return None


# ---------------------------------------------------------------------------
# pg_dump-compatible SQL execution
# ---------------------------------------------------------------------------


def _execute_dump_sql(cur: psycopg.Cursor, sql: str) -> None:
    """Run a migration SQL body, handling pg_dump psql artefacts.

    A pg_dump-format file mixes real SQL with psql metacommands and
    ``COPY ... FROM stdin;`` data blocks. psycopg's simple-query
    ``execute()`` can't run either shape: ``\\restrict`` is a
    parser error, and a ``FROM stdin;`` COPY needs the explicit
    ``copy()`` API rather than an embedded ``\\.``-terminated block.

    This driver walks the SQL line-by-line:

    * ``\\restrict X`` / ``\\unrestrict X`` — silently skipped
      (these are PG 18+ dump markers that gate protocol features
      we don't use during a single-connection apply).
    * ``COPY table (cols) FROM stdin;`` — collect rows up to the
      ``\\.`` terminator and stream them through ``cur.copy()``
      with the same tab-separated payload the dump emitted.
    * Everything else — buffered and flushed to a single
      ``cur.execute()`` call between blocks so ordinary
      multi-statement SQL still runs in one parse.

    The intent is "pg_dump output Just Works"; clean hand-written
    migrations (the previous shape) pass through unchanged because
    they contain no ``\\``-prefixed lines.
    """
    buffer: list[str] = []

    def _flush() -> None:
        if not buffer:
            return
        chunk = "".join(buffer)
        buffer.clear()
        if chunk.strip():
            cur.execute(chunk)

    lines = sql.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith("\\restrict ") or stripped.startswith("\\unrestrict "):
            # Drop the dump marker; it isn't SQL.
            i += 1
            continue
        # COPY ... FROM stdin; — the next lines are tab-separated
        # row data terminated by a lone ``\.`` line.
        s_lower = stripped.lower()
        if s_lower.startswith("copy ") and "from stdin" in s_lower:
            _flush()
            copy_sql = line.rstrip()
            j = i + 1
            data: list[str] = []
            while j < len(lines):
                row = lines[j]
                if row.rstrip("\r\n") == "\\.":
                    j += 1
                    break
                data.append(row)
                j += 1
            payload = "".join(data).encode("utf-8")
            with cur.copy(copy_sql) as copy:
                copy.write(payload)
            i = j
            continue
        buffer.append(line)
        i += 1

    _flush()

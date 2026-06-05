"""Forward-only SQL migration runner (sync, psycopg 3).

Reads `*.sql` files from a migrations directory, applies any whose
version isn't already in the `_migrations` ledger, records the version
+ checksum on success. Each migration runs in its own transaction.

Versioning: filename without `.sql` extension. Filenames must sort
correctly (e.g. `0001_initial.sql`, `0002_add_xxx.sql`).

Refuses to run if a previously-applied migration's checksum no longer
matches its file (someone edited a sealed migration).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.errors import UndefinedTable

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MigrationFile:
    version: str
    path: Path
    sql: str
    checksum: str


def _load_migrations(migrations_dir: Path) -> list[MigrationFile]:
    """Read all *.sql files from `migrations_dir`, sorted by filename."""
    if not migrations_dir.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {migrations_dir}")

    files: list[MigrationFile] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        files.append(
            MigrationFile(version=path.stem, path=path, sql=sql, checksum=checksum)
        )
    return files


def _applied_versions(conn: psycopg.Connection) -> dict[str, str]:
    """Return {version: checksum} from `_migrations`. Empty dict if the
    table doesn't exist yet (fresh database).

    Uses the schema-qualified ``public._migrations`` — a pg_dump
    migration body can set ``search_path = ''`` mid-apply, which
    leaves later bare references unresolved.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version, checksum FROM public._migrations")
            rows = cur.fetchall()
    except UndefinedTable:
        conn.rollback()
        return {}
    return {row[0]: row[1] for row in rows}


class Migrator:
    """Forward-only migration runner.

    Usage::

        migrator = Migrator(dsn, migrations_dir)
        applied = migrator.apply_all()  # returns newly-applied versions
    """

    def __init__(self, dsn: str, migrations_dir: Path) -> None:
        self.dsn = dsn
        self.migrations_dir = Path(migrations_dir)

    def applied_versions(self) -> list[str]:
        with psycopg.connect(self.dsn) as conn:
            applied = _applied_versions(conn)
        return sorted(applied)

    def pending(self) -> list[str]:
        with psycopg.connect(self.dsn) as conn:
            applied = _applied_versions(conn)
        files = _load_migrations(self.migrations_dir)
        return [f.version for f in files if f.version not in applied]

    def apply_all(self) -> list[str]:
        """Apply every pending migration in version order. Returns the
        list of versions newly applied during this call."""
        files = _load_migrations(self.migrations_dir)
        if not files:
            return []

        newly_applied: list[str] = []
        # autocommit=True is load-bearing: under autocommit=False the very
        # first SELECT (or _applied_versions' fallback rollback) puts us in
        # an implicit transaction, and ``with conn.transaction()`` then
        # downgrades to a SAVEPOINT. If the migration SQL aborts, the
        # savepoint disappears and the context-manager exit raises
        # ``InvalidSavepointSpecification`` — masking the real SQL error.
        # With autocommit=True, ``conn.transaction()`` issues a real
        # BEGIN/COMMIT and surfaces any inner exception directly.
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            applied = _applied_versions(conn)

            # Integrity check on already-applied migrations
            for f in files:
                if f.version in applied and applied[f.version] != f.checksum:
                    raise RuntimeError(
                        f"checksum mismatch for already-applied migration "
                        f"{f.version!r}: file has {f.checksum[:12]}, "
                        f"DB has {applied[f.version][:12]}. "
                        f"Refusing to run - sealed migrations must not be edited."
                    )

            for f in files:
                if f.version in applied:
                    continue
                log.info("applying migration %s", f.version)
                with conn.transaction():
                    with conn.cursor() as cur:
                        _execute_dump_sql(cur, f.sql)
                        # _migrations may not exist yet on first migration;
                        # the migration creates it as part of its body.
                        # Fully qualified: pg_dump-style migrations
                        # set ``search_path = ''`` at the top of the
                        # body for DDL-safety (line ~45 of a 0001_initial
                        # dump). After the migration runs, the same
                        # search_path is still in effect on this
                        # connection, so a bare ``_migrations`` reference
                        # fails to resolve. Use the schema-qualified
                        # name so the ledger row lands regardless of
                        # what the migration file did to search_path.
                        cur.execute(
                            "INSERT INTO public._migrations (version, checksum) "
                            "VALUES (%s, %s)",
                            (f.version, f.checksum),
                        )
                newly_applied.append(f.version)
                log.info("  applied %s ok", f.version)

        return newly_applied


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

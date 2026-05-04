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
    table doesn't exist yet (fresh database)."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version, checksum FROM _migrations")
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
        with psycopg.connect(self.dsn, autocommit=False) as conn:
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
                        cur.execute(f.sql)
                        # _migrations may not exist yet on first migration;
                        # the migration creates it as part of its body.
                        cur.execute(
                            "INSERT INTO _migrations (version, checksum) "
                            "VALUES (%s, %s)",
                            (f.version, f.checksum),
                        )
                newly_applied.append(f.version)
                log.info("  applied %s ok", f.version)

        return newly_applied

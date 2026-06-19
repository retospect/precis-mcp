"""Generate the per-release baseline snapshot (``migrations/baseline/schema.sql``).

The baseline is the migration chain *compiled*: a throwaway database is
built by replaying every numbered migration from scratch, then
``pg_dump`` captures its schema + seed vocabulary. The ``_migrations``
ledger is *synthesised* from the migration files (deterministic — no
``applied_at`` churn) so the snapshot self-stamps the ledger when loaded.

A fresh ``precis migrate`` loads this one file instead of replaying the
chain (see :meth:`precis.store.Migrator.apply_all`). Because the loaded
ledger marks the baked-in versions as applied, any migration added
*after* the snapshot applies as a normal tail — this is the original
"install the snapshot, then migrate from there" model, pinned per
release.

Operationally this is a **container op** — it needs ``pg_dump`` on
PATH and a Postgres server it can create a scratch database on:

    precis db dump-schema                 # writes migrations/baseline/schema.sql

``scripts/bump`` regenerates it on every version bump, so the committed
baseline always matches the release it ships with. The text integrity
test in ``tests/test_schema_baseline.py`` guards that every version
baked into the snapshot maps to an unedited migration file (no DB
needed), and a DB-backed convergence test proves
``load-baseline + tail`` produces the same schema as a full from-scratch
replay.

This is a dual-track scheme (Rails ``schema.rb`` / Ecto
``structure.sql``), **not** a third greenfield: the numbered migrations
stay sealed in the tree as the upgrade path for existing databases. See
ADR 0031.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import psycopg
from psycopg import sql as _sql
from psycopg.conninfo import make_conninfo

from precis.store.migrate import (
    PRECIS_PLUGIN_NAME,
    MigrationSource,
    _load_migrations,
)

log = logging.getLogger(__name__)

#: Reference-vocabulary tables whose *data* is part of the schema
#: contract and must ride along in the baseline. Mirrors the set ADR
#: 0019 dumped by hand for the second-greenfield ``0001_initial.sql``.
SEED_TABLES: tuple[str, ...] = (
    "actors",
    "kinds",
    "relations",
    "providers",
    "chunk_kinds",
    "embedders",
    "summarizers",
    "artifact_kinds",
)

#: Extensions ``pg_dump --schema=public`` omits; prepended manually so a
#: truly-empty database can load the baseline in one shot.
EXTENSIONS: tuple[str, ...] = ("vector", "pg_trgm", "btree_gist")

#: Where the snapshot lives, relative to the migrations dir. A
#: subdirectory keeps it out of ``glob("*.sql")`` discovery (same trick
#: the ``archive/`` dir uses) so the runner never mistakes it for a
#: numbered migration.
BASELINE_SUBDIR = "baseline"
BASELINE_FILENAME = "schema.sql"

#: Fixed ``applied_at`` for synthesised ledger rows. The runner ignores
#: this column; a constant keeps the file byte-stable across regens.
_LEDGER_EPOCH = "1970-01-01 00:00:00+00"

#: Default scratch database name created on the maintenance server.
DEFAULT_SCRATCH_DB = "precis_schema_dump"


def baseline_path(migrations_dir: Path) -> Path:
    """Return the baseline snapshot path for a given migrations dir."""
    return migrations_dir / BASELINE_SUBDIR / BASELINE_FILENAME


def builtin_migrations_dir() -> Path:
    """Return the in-tree ``src/precis/migrations`` directory."""
    return Path(__file__).resolve().parent.parent / "migrations"


# ---------------------------------------------------------------------------
# Ledger synthesis + parsing
# ---------------------------------------------------------------------------


def _render_ledger_copy(migrations_dir: Path) -> str:
    """Build a deterministic ``COPY public._migrations`` block.

    Rows are derived from the migration files themselves, so the baked
    checksums are exactly the values :meth:`Migrator.apply_all`'s
    integrity gate compares against. Column order matches the table
    (``version, applied_at, checksum, plugin``).
    """
    files = _load_migrations(MigrationSource(PRECIS_PLUGIN_NAME, migrations_dir))
    lines = [
        "--",
        "-- Migration ledger (synthesised from the migration files, not",
        "-- pg_dump'd) so loading the baseline self-stamps every baked-in",
        "-- version as applied. applied_at is a fixed sentinel.",
        "--",
        "COPY public._migrations (version, applied_at, checksum, plugin) FROM stdin;",
    ]
    for f in files:
        lines.append(f"{f.version}\t{_LEDGER_EPOCH}\t{f.checksum}\t{f.plugin}")
    lines.append("\\.")
    lines.append("")
    return "\n".join(lines)


def parse_baseline_ledger(sql_text: str) -> list[tuple[str, str]]:
    """Extract ``[(version, checksum), ...]`` from a baseline's ledger block.

    Reads the ``COPY public._migrations (...) FROM stdin;`` block and
    maps columns by name, so it survives a column reorder. Returns an
    empty list if the block is absent.
    """
    lines = sql_text.splitlines()
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        low = line.lower()
        if low.startswith("copy public._migrations (") and "from stdin" in low:
            cols = [
                c.strip()
                for c in line[line.index("(") + 1 : line.index(")")].split(",")
            ]
            try:
                vi, ci = cols.index("version"), cols.index("checksum")
            except ValueError:
                return out
            j = i + 1
            while j < len(lines) and lines[j].rstrip() != "\\.":
                fields = lines[j].split("\t")
                if len(fields) > max(vi, ci):
                    out.append((fields[vi], fields[ci]))
                j += 1
            return out
        i += 1
    return out


# ---------------------------------------------------------------------------
# Baseline generation (needs pg_dump + a scratch DB)
# ---------------------------------------------------------------------------


def _run_pg_dump(pg_dump_bin: str, scratch_dsn: str, extra: list[str]) -> str:
    """Invoke ``pg_dump`` against ``scratch_dsn``; return its stdout."""
    cmd = [pg_dump_bin, "--no-owner", "--no-privileges", *extra, "-d", scratch_dsn]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{pg_dump_bin} failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def _clean_dump(text: str) -> str:
    """Strip psql-only artefacts pg_dump emits that we don't want baked.

    ``\\restrict`` / ``\\unrestrict`` are PG18 dump markers (the runner
    tolerates them, but they add noise) and a bare ``CREATE SCHEMA
    public`` is rewritten to ``IF NOT EXISTS`` so fresh databases that
    already have the default schema don't error.
    """
    out: list[str] = []
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith("\\restrict ") or s.startswith("\\unrestrict "):
            continue
        if s.rstrip() == "CREATE SCHEMA public;":
            out.append("CREATE SCHEMA IF NOT EXISTS public;")
            continue
        out.append(line)
    return "\n".join(out)


def _assemble(schema_ddl: str, seed_data: str, ledger: str, *, head: str) -> str:
    ext_lines = "\n".join(f"CREATE EXTENSION IF NOT EXISTS {e};" for e in EXTENSIONS)
    header = (
        "-- migrations/baseline/schema.sql — generated baseline snapshot.\n"
        "--\n"
        "-- DO NOT EDIT BY HAND. Regenerate with `precis db dump-schema`\n"
        "-- (or `scripts/bump`, which does it at every version bump).\n"
        "--\n"
        f"-- Baked-in migration head: {head}\n"
        "--\n"
        "-- This is the migration chain compiled to one file: a fresh\n"
        "-- `precis migrate` loads this instead of replaying every numbered\n"
        "-- migration, then applies any migrations added since this snapshot\n"
        "-- as a normal tail. The numbered migrations stay sealed in the tree\n"
        "-- as the upgrade path for existing databases (ADR 0031). This is NOT\n"
        "-- a greenfield — nothing is deleted.\n"
        "--\n"
        "-- Extensions (pg_dump --schema=public omits them):\n"
        f"{ext_lines}\n"
        "CREATE SCHEMA IF NOT EXISTS public;\n"
    )
    return f"{header}\n{schema_ddl.rstrip()}\n\n{seed_data.rstrip()}\n\n{ledger}"


def generate_baseline_sql(
    maintenance_dsn: str,
    migrations_dir: Path,
    *,
    scratch_db: str = DEFAULT_SCRATCH_DB,
    pg_dump_bin: str = "pg_dump",
) -> str:
    """Compile the migration chain into a single baseline SQL string.

    Creates ``scratch_db`` on ``maintenance_dsn``'s server, replays the
    chain into it from scratch (no baseline), ``pg_dump``s the schema +
    seed tables, synthesises the ledger, and drops the scratch DB.

    ``maintenance_dsn`` may point at any database on the target server;
    its dbname is swapped to ``postgres`` for the CREATE/DROP and to
    ``scratch_db`` for the replay + dump. The connecting role needs
    ``CREATEDB``.
    """
    from precis.store.migrate import Migrator

    admin_dsn = make_conninfo(maintenance_dsn, dbname="postgres")
    scratch_dsn = make_conninfo(maintenance_dsn, dbname=scratch_db)
    db_ident = _sql.Identifier(scratch_db)

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(_sql.SQL("DROP DATABASE IF EXISTS {}").format(db_ident))
        conn.execute(_sql.SQL("CREATE DATABASE {}").format(db_ident))
    try:
        # Full from-scratch replay — no baseline. This is the canonical
        # source of truth the snapshot must reproduce.
        applied = Migrator(scratch_dsn, migrations_dir).apply_all()
        head = applied[-1][1] if applied else "(empty)"

        schema_ddl = _clean_dump(
            _run_pg_dump(pg_dump_bin, scratch_dsn, ["--schema=public", "--schema-only"])
        )
        seed_extra = ["--data-only"]
        for t in SEED_TABLES:
            seed_extra += ["--table", f"public.{t}"]
        seed_data = _clean_dump(_run_pg_dump(pg_dump_bin, scratch_dsn, seed_extra))
        ledger = _render_ledger_copy(migrations_dir)
        return _assemble(schema_ddl, seed_data, ledger, head=head)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(_sql.SQL("DROP DATABASE IF EXISTS {}").format(db_ident))


def write_baseline(
    maintenance_dsn: str,
    migrations_dir: Path | None = None,
    *,
    output: Path | None = None,
    scratch_db: str = DEFAULT_SCRATCH_DB,
    pg_dump_bin: str = "pg_dump",
) -> Path:
    """Generate and write the baseline snapshot; return the path written."""
    migrations_dir = migrations_dir or builtin_migrations_dir()
    out = output or baseline_path(migrations_dir)
    sql_text = generate_baseline_sql(
        maintenance_dsn,
        migrations_dir,
        scratch_db=scratch_db,
        pg_dump_bin=pg_dump_bin,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(sql_text, encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Integrity checks (no DB / no pg_dump — safe in CI)
# ---------------------------------------------------------------------------


def baseline_integrity_errors(migrations_dir: Path | None = None) -> list[str]:
    """Return reasons the committed baseline is inconsistent with the files.

    Empty list == consistent (or no baseline yet — absence is not an
    error here; the runner falls back to full replay). Each baked-in
    version must map to an existing migration file with a matching
    checksum, and the baked-in versions must form a prefix (no
    file older than the head is missing from the ledger).
    """
    migrations_dir = migrations_dir or builtin_migrations_dir()
    path = baseline_path(migrations_dir)
    if not path.exists():
        return []
    ledger = parse_baseline_ledger(path.read_text(encoding="utf-8"))
    files = {
        f.version: f.checksum
        for f in _load_migrations(MigrationSource(PRECIS_PLUGIN_NAME, migrations_dir))
    }
    errs: list[str] = []
    baked = {v for v, _ in ledger}
    for version, checksum in ledger:
        if version not in files:
            errs.append(f"baseline references unknown migration {version!r}")
        elif files[version] != checksum:
            errs.append(
                f"baseline checksum drift for {version!r}: "
                f"baked {checksum[:12]}, file {files[version][:12]} "
                "(a sealed migration was edited)"
            )
    if baked:
        head = max(baked)
        for version in files:
            if version <= head and version not in baked:
                errs.append(
                    f"baseline is non-contiguous: file {version!r} predates the "
                    f"baked head {head!r} but is absent from the ledger"
                )
    return errs


def baseline_at_head_errors(migrations_dir: Path | None = None) -> list[str]:
    """Return reasons the baseline is not at the chain head.

    Stricter than :func:`baseline_integrity_errors`: used as the release
    gate. A released baseline must bake in *every* migration so a fresh
    install of the tagged version applies the snapshot and zero tail.
    """
    migrations_dir = migrations_dir or builtin_migrations_dir()
    path = baseline_path(migrations_dir)
    if not path.exists():
        return [f"no baseline snapshot at {path}"]
    errs = baseline_integrity_errors(migrations_dir)
    ledger = {v for v, _ in parse_baseline_ledger(path.read_text(encoding="utf-8"))}
    files = {
        f.version
        for f in _load_migrations(MigrationSource(PRECIS_PLUGIN_NAME, migrations_dir))
    }
    for version in sorted(files - ledger):
        errs.append(
            f"migration {version!r} is not baked into the baseline — "
            "regenerate with `precis db dump-schema` before releasing"
        )
    return errs


def assert_baseline_at_head(migrations_dir: Path | None = None) -> None:
    """Raise ``SystemExit`` with a clear message if the baseline is stale.

    Intended for the CI release (tag) job:
        python -c "from precis.store.schema_dump import assert_baseline_at_head as a; a()"
    """
    errs = baseline_at_head_errors(migrations_dir)
    if errs:
        raise SystemExit("baseline schema.sql is stale:\n  - " + "\n  - ".join(errs))


__all__ = [
    "BASELINE_FILENAME",
    "BASELINE_SUBDIR",
    "EXTENSIONS",
    "SEED_TABLES",
    "assert_baseline_at_head",
    "baseline_at_head_errors",
    "baseline_integrity_errors",
    "baseline_path",
    "builtin_migrations_dir",
    "generate_baseline_sql",
    "parse_baseline_ledger",
    "write_baseline",
]

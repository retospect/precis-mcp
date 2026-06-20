"""Baseline-snapshot integrity + convergence tests (ADR 0031).

Two tiers, matching the guarantee split:

* **Text tier (always runs, no DB / no pg_dump):** the ledger
  synth↔parse closure, and — once a baseline is committed — that every
  version baked into ``migrations/baseline/schema.sql`` maps to an
  unedited migration file. This is the gate that keeps the snapshot
  honest in CI, which has no Postgres.

* **DB tier (skips without Postgres + pg_dump):** the real convergence
  proof — ``load baseline + apply tail`` produces the *same* schema as
  a full from-scratch replay of the numbered chain, and the resulting
  ``_migrations`` ledgers match. This is the deep guarantee the
  /endsession container gate exercises.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from precis.store import Migrator
from precis.store.migrate import (
    PRECIS_PLUGIN_NAME,
    MigrationSource,
    _load_migrations,
)
from precis.store.schema_dump import (
    _render_ledger_copy,
    baseline_at_head_errors,
    baseline_integrity_errors,
    baseline_path,
    parse_baseline_ledger,
)

MIGRATIONS_DIR = Path(__file__).parent.parent / "src" / "precis" / "migrations"
BASELINE = baseline_path(MIGRATIONS_DIR)


# ---------------------------------------------------------------------------
# Text tier — no DB needed
# ---------------------------------------------------------------------------


def test_ledger_synth_parse_roundtrip() -> None:
    """The synthesised ledger parses back to exactly the migration files.

    This closes the loop the runner depends on: the checksums the
    baseline bakes in are the same ones :meth:`Migrator.apply_all`'s
    integrity gate recomputes from the files. Runs without a baseline
    file or a DB — it builds the ledger block from the live migrations.
    """
    block = _render_ledger_copy(MIGRATIONS_DIR)
    parsed = dict(parse_baseline_ledger(block))
    files = {
        f.version: f.checksum
        for f in _load_migrations(MigrationSource(PRECIS_PLUGIN_NAME, MIGRATIONS_DIR))
    }
    assert parsed == files
    assert files, "expected at least one migration file"


def test_baseline_integrity() -> None:
    """Committed baseline (if any) is consistent with the migration files.

    Absence is not a failure — the runner falls back to full replay
    when no snapshot exists, so this xfails-soft via skip until the
    first ``precis db dump-schema`` lands the file.
    """
    if not BASELINE.exists():
        pytest.skip("no baseline snapshot committed yet (run `precis db dump-schema`)")
    errs = baseline_integrity_errors(MIGRATIONS_DIR)
    assert not errs, "baseline inconsistent with migration files:\n" + "\n".join(errs)


def test_baseline_not_globbed_as_migration() -> None:
    """The snapshot must not be discovered as a numbered migration."""
    versions = {
        f.version
        for f in _load_migrations(MigrationSource(PRECIS_PLUGIN_NAME, MIGRATIONS_DIR))
    }
    assert "schema" not in versions
    assert BASELINE.parent.name == "baseline"  # lives in a subdir, out of glob


# ---------------------------------------------------------------------------
# DB tier — needs Postgres + pg_dump
# ---------------------------------------------------------------------------


def _pg_dump_bin() -> str | None:
    for cand in ("pg_dump", "/opt/homebrew/opt/libpq/bin/pg_dump"):
        if shutil.which(cand) or Path(cand).exists():
            return cand
    return None


def _dump_schema(pg_dump_bin: str, dsn: str) -> str:
    out = subprocess.run(
        [
            pg_dump_bin,
            "--schema=public",
            "--schema-only",
            "--no-owner",
            "--no-privileges",
            "-d",
            dsn,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # Drop comments / blank lines / SET preamble so the comparison is on
    # schema substance, not pg_dump's header chatter. ``\restrict`` /
    # ``\unrestrict`` are psql-only markers pg_dump >= 17 emits with a
    # *fresh random token every run* — left in, the two dumps could never
    # be equal. (The production baseline cleaner strips them too; see
    # precis.store.schema_dump._clean_dump.)
    keep = []
    for line in out.splitlines():
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        if s.startswith("\\restrict ") or s.startswith("\\unrestrict "):
            continue
        keep.append(s)
    return "\n".join(keep)


def _applied_ledger(dsn: str) -> set[tuple[str, str]]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute("SELECT plugin, version FROM public._migrations").fetchall()
    return {(r[0], r[1]) for r in rows}


@pytest.mark.db
def test_schema_convergence(
    fresh_db: str, drop_public_objects: Callable[[str], None]
) -> None:
    """load-baseline + tail == full from-scratch replay (schema + ledger).

    This is the install path exercised end-to-end: Path B is *exactly*
    what a fresh `precis migrate` does (load the snapshot, then apply any
    migrations added since). It must land the identical schema and ledger
    a full chain replay (Path A) produces. If they ever diverge, the
    snapshot is lying about the chain — or a tail migration fails to apply
    on top of the baseline. The `fresh_db` fixture handles the
    postgres-unreachable skip and restores a clean schema at teardown.
    """
    if not BASELINE.exists():
        pytest.skip("no baseline snapshot committed yet")
    pg_dump_bin = _pg_dump_bin()
    if pg_dump_bin is None:
        pytest.skip("pg_dump not available")
    dsn = fresh_db  # schema already stripped; teardown re-applies

    # Path A: full from-scratch replay (no baseline).
    Migrator(dsn, MIGRATIONS_DIR).apply_all()
    schema_a = _dump_schema(pg_dump_bin, dsn)
    ledger_a = _applied_ledger(dsn)

    # Path B: bootstrap from the snapshot, then apply any tail.
    drop_public_objects(dsn)
    Migrator(dsn, MIGRATIONS_DIR, baseline=BASELINE).apply_all()
    schema_b = _dump_schema(pg_dump_bin, dsn)
    ledger_b = _applied_ledger(dsn)

    assert ledger_a == ledger_b, "ledger diverges between replay and snapshot"
    assert schema_a == schema_b, "schema diverges between replay and snapshot"


def test_baseline_at_head_when_present() -> None:
    """A committed baseline should be at chain head (release-readiness).

    Pure text check (no DB). Not strictly required mid-cycle — a tail is
    allowed by design — but a committed snapshot behind head means a fresh
    install replays a tail it needn't, and the release tag-guard demands
    head. Soft via skip when absent or legitimately behind.
    """
    if not BASELINE.exists():
        pytest.skip("no baseline snapshot committed yet")
    errs = baseline_at_head_errors(MIGRATIONS_DIR)
    if errs:
        pytest.skip(
            "baseline is behind head (allowed mid-cycle; regenerate before "
            "release):\n" + "\n".join(errs)
        )

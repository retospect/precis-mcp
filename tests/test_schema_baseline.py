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
  /land container gate exercises.
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
    baseline_lag,
    baseline_path,
    parse_baseline_ledger,
)

MIGRATIONS_DIR = Path(__file__).parent.parent / "src" / "precis" / "migrations"
BASELINE = baseline_path(MIGRATIONS_DIR)

#: Bounded drift: a fresh install replays at most this many tail
#: migrations on top of the baseline; the exact-head assert
#: (`assert_baseline_at_head`) remains the release gate in publish.yml.
BASELINE_LAG_MAX = 25

#: Two historical migration-number collisions, grandfathered by EXACT
#: filename set (not just the shared number) so a THIRD file landing on
#: 0037 or 0039 still fails. Harmless at runtime — the ledger keys on the
#: full filename stem — but a past collision (0009, commit b88f57b4)
#: silently skipped a migration in prod, so new collisions are blocked.
#: These sealed files must NEVER be renamed to "fix" the collision — a
#: renamed applied migration looks unapplied to the ledger.
ALLOWED_PREFIX_COLLISIONS: tuple[frozenset[str], ...] = (
    frozenset({"0037_draft_list_chunks.sql", "0037_plots_relation.sql"}),
    frozenset({"0039_authored_relation.sql", "0039_orcid_kind.sql"}),
)


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


def test_baseline_lag_bounded() -> None:
    """Baseline must not drift too far behind head — a hard gate, not soft.

    ``test_baseline_at_head_when_present`` skips-soft when the baseline is
    behind head, because a tail is allowed by design mid-cycle. But that
    left the snapshot's only *hard* staleness check
    (``assert_baseline_at_head``) running solely in the tag-gated publish
    CI job — and tagging stopped, so the baseline drifted 73 migrations
    stale unnoticed before it was caught and regenerated. This bounds the
    drift instead of demanding the exact head, so it can run (and fail
    loudly) on every push and container gate.
    """
    if not BASELINE.exists():
        pytest.skip("no baseline snapshot committed yet (run `precis db dump-schema`)")
    lag = baseline_lag(MIGRATIONS_DIR)
    assert lag <= BASELINE_LAG_MAX, (
        f"baseline is {lag} migrations behind head (max {BASELINE_LAG_MAX}) — "
        "regenerate it: run `uv run precis db dump-schema` (in the dev "
        "container, against pg17) and commit the refreshed "
        "migrations/baseline/schema.sql (ADR 0031)"
    )


def test_migration_number_prefixes_unique() -> None:
    """No two migration files share a leading 4-digit number.

    Two historical collisions are grandfathered in by exact filename SET
    (see ``ALLOWED_PREFIX_COLLISIONS``) — any other duplicate, including a
    THIRD file landing on 0037 or 0039, fails this test.
    """
    files = sorted(p.name for p in MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    by_prefix: dict[str, list[str]] = {}
    for name in files:
        by_prefix.setdefault(name[:4], []).append(name)
    dupes = {prefix: names for prefix, names in by_prefix.items() if len(names) > 1}

    for prefix, names in dupes.items():
        assert frozenset(names) in ALLOWED_PREFIX_COLLISIONS, (
            f"migration number {prefix!r} is used by {len(names)} files: "
            f"{names} — pick the next free number for the new one; never "
            "renumber a sealed migration"
        )

    # Confirm each allowlisted pair still exists as-is — a rename that
    # "fixes" a collision on a sealed migration is exactly what's forbidden.
    for pair in ALLOWED_PREFIX_COLLISIONS:
        prefix = next(iter(pair))[:4]
        assert frozenset(dupes.get(prefix, [])) == pair, (
            f"expected legacy collision {sorted(pair)} at {prefix!r} not "
            "found — a sealed migration may have been renamed (forbidden)"
        )


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

"""gr454487 — every migration-seeded table must ride in the baseline.

``precis db dump-schema`` (see ``precis.store.schema_dump``) replays only
the *built-in* ``precis`` migration chain onto a scratch DB, then
``pg_dump --data-only``s ``SEED_TABLES`` into ``migrations/baseline/schema.sql``.
A fresh ``precis migrate`` loads that snapshot and self-stamps every baked
version as applied — so any tail migration's ``INSERT ... ON CONFLICT DO
NOTHING`` for a table *not* in ``SEED_TABLES`` never replays: the ledger
says it already ran, the ``INSERT`` never re-executes, and the table is
silently empty forever on a fresh install (the ``se`` scenario-preset
regression this test exists to catch — gr454487).

Plugin-source migrations (``precis_se``, ``precis_bio``, ...) are exempt
from that failure mode today: ``generate_baseline_sql`` only replays the
core ``precis`` dir (single-source ``Migrator``), so no plugin migration
is ever baked into the ledger — every plugin migration always applies as
a normal tail on a fresh install, so its seed ``INSERT``s always run for
real. This test still scans plugin chains too, in case that changes.

Two checks:

* **Inventory (no DB):** every ``INSERT INTO <table>`` target across
  every migration chain, with statements inside ``CREATE FUNCTION`` bodies
  stripped out first (those run at *call* time, e.g. the ``vault``/
  ``file_gripe_readonly`` SECURITY DEFINER functions — never at migration
  *apply* time, so they are not seed-data candidates at all) — must be
  either in :data:`precis.store.schema_dump.SEED_TABLES` or in this
  file's :data:`NOT_VOCAB`, with a reason.
* **Load (DB, ``fresh_db``):** loading the committed baseline into a
  truly empty database yields the three ``core`` design-scenario presets
  non-empty — the exact symptom gr454487 reported
  (``unknown scenario 'prototype' — known: ''``).
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg
import pytest

from precis.store import Migrator
from precis.store.schema_dump import SEED_TABLES, baseline_path

SRC_DIR = Path(__file__).parent.parent / "src"
MIGRATIONS_DIR = SRC_DIR / "precis" / "migrations"
BASELINE = baseline_path(MIGRATIONS_DIR)


#: Every migration chain in the tree — core plus every plugin package's
#: own ``migrations/`` dir. ``baseline/`` and ``archive/`` are excluded
#: the same way the runner excludes them (glob only ``NNNN_*.sql`` at the
#: top level).
def _migration_files() -> list[Path]:
    return sorted(SRC_DIR.glob("*/migrations/[0-9][0-9][0-9][0-9]_*.sql"))


#: Tables a migration's top-level ``INSERT INTO`` targets that are
#: deliberately *not* baked into ``SEED_TABLES`` — each is a one-time data
#: migration/backfill over rows that only exist on an already-populated
#: DB (a truly fresh install has none of those rows, so the statement is
#: a documented no-op there), not reference vocabulary the schema
#: contract promises to exist. Every entry names the migration(s) and the
#: reason, so a new unconditional vocabulary seed can't hide here by
#: mistake.
NOT_VOCAB: dict[str, str] = {
    "chunk_claims": (
        "0045_chunk_claims.sql: heals pre-lease chunk_summaries 'failed' "
        "rows into lease rows — SELECTs FROM chunk_summaries, which is "
        "empty on a fresh DB (docstring: 'Heal old-model failures')."
    ),
    "chunks": (
        "0050_memory_body_chunk.sql: backfills a memory_body chunk for "
        "existing kind='memory' refs — SELECTs FROM refs, empty on a "
        "fresh DB. (0079's chunks INSERT lives inside a CREATE FUNCTION "
        "body — file_gripe_readonly — so it's stripped before scanning "
        "and never reaches this list at all.)"
    ),
    "paper_authors": (
        "0168_paper_authors.sql: projects refs.authors jsonb into rows "
        "for existing papers — SELECTs FROM refs, empty on a fresh DB "
        "(docstring: 'The data backfill below projects every existing "
        "byline')."
    ),
    "pcb_boards": (
        "0138_pcb_boards_routes.sql: backfills one 'main' board per "
        "existing kind='pcb' ref — SELECTs FROM refs, empty on a fresh "
        "DB (comment: 'backfill: one main board per existing pcb-kind "
        "design ref')."
    ),
    "tags": (
        "0028/0102/0146: legacy-tag-value merge-then-delete migrations, "
        "each guarded by 'WHERE EXISTS (SELECT 1 FROM tags WHERE "
        "<old value>)' — every one is a documented no-op on a fresh DB "
        "('A fresh DB (no legacy rows) runs every statement as a "
        "no-op.', 0102's docstring). Ordinary tag rows are created "
        "on-demand at write time (get-or-create), not seeded."
    ),
}

#: Design-core presets (0162_design_core.sql) that must survive loading
#: the baseline into a truly empty DB — the exact gr454487 symptom.
_EXPECTED_SCENARIOS = {"prototype", "small_batch", "mass_production"}


def _strip_function_bodies(sql_text: str) -> str:
    """Remove every ``$$ ... $$`` dollar-quoted body.

    Every migration in this tree tags its dollar-quoting with a bare
    ``$$`` (verified: ``grep -rohE '\\$[A-Za-z_]*\\$' src/precis*/migrations``
    finds only that token) — ``CREATE FUNCTION ... AS $$ ... $$``. Those
    bodies run at *call* time (a later ``SELECT``/``INSERT`` against the
    function, or — for the ``vault``/``file_gripe_readonly`` cases — the
    app calling the function at runtime), never when the migration itself
    is applied, so an ``INSERT INTO`` inside one is not a baseline-seed
    candidate.
    """
    return re.sub(r"\$\$.*?\$\$", "", sql_text, flags=re.DOTALL)


def _strip_comment_only_lines(sql_text: str) -> str:
    """Drop every line whose stripped text starts with ``--``.

    Migration prose commonly narrates the SQL it's about to run (e.g.
    0012/0069's "the ... ingest insert into refs trips refs_provider_fkey"
    doc-comments) — a naive scan for ``insert into`` would misread that
    prose as a real statement. Only whole-comment lines are dropped (not
    inline trailing ``-- comment`` after real code) so this can't eat a
    string literal that happens to contain ``--``.
    """
    return "\n".join(
        line for line in sql_text.splitlines() if not line.strip().startswith("--")
    )


# Any schema qualifier is stripped (``public.``, ``vault.``, …) so a
# non-public seed INSERT is classified by its TABLE, not its schema name.
_INSERT_RE = re.compile(
    r"insert\s+into\s+(?:\"?[a-zA-Z_][a-zA-Z0-9_]*\"?\.)?\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?",
    re.IGNORECASE,
)


def _insert_targets(sql_text: str) -> set[str]:
    """Every top-level ``INSERT INTO <table>`` target, lower-cased."""
    stripped = _strip_comment_only_lines(_strip_function_bodies(sql_text))
    return {m.group(1).lower() for m in _INSERT_RE.finditer(stripped)}


def test_every_seeded_table_is_classified() -> None:
    """Every migration-chain ``INSERT INTO`` target is SEED_TABLES or NOT_VOCAB.

    Catches the gr454487 failure mode at the source: a new tail migration
    that unconditionally seeds a new table now fails CI immediately
    (forcing either ``SEED_TABLES`` or a justified ``NOT_VOCAB`` entry)
    instead of silently shipping a table that goes empty on every fresh
    install once the baseline bakes past it.
    """
    seed = set(SEED_TABLES)
    classified = seed | set(NOT_VOCAB)
    unclassified: dict[str, set[str]] = {}
    for path in _migration_files():
        targets = _insert_targets(path.read_text(encoding="utf-8"))
        unknown = targets - classified
        if unknown:
            unclassified[str(path.relative_to(SRC_DIR.parent))] = unknown

    assert not unclassified, (
        "migration(s) INSERT INTO table(s) neither in SEED_TABLES nor "
        "NOT_VOCAB — classify each as reference vocabulary (add to "
        "precis.store.schema_dump.SEED_TABLES) or a one-time data "
        "backfill (add to tests/test_baseline_seed_tables.py::NOT_VOCAB "
        "with a reason):\n"
        + "\n".join(
            f"  {path}: {sorted(tables)}" for path, tables in unclassified.items()
        )
    )


def test_seed_tables_and_not_vocab_disjoint() -> None:
    """A table can't be both baked-in vocabulary and a data-only backfill."""
    overlap = set(SEED_TABLES) & set(NOT_VOCAB)
    assert not overlap, f"tables classified as both vocab and data: {overlap}"


@pytest.mark.db
def test_baseline_load_seeds_design_scenarios(fresh_db: str) -> None:
    """Loading the baseline into an empty DB yields the three core presets.

    Reproduces gr454487 directly: before the fix, ``design_scenarios`` on
    a baseline-only install had zero rows (0162's seed migration is baked
    into the ledger, so its ``INSERT`` never replays), so
    ``put(kind='se', ..., scenario='prototype')`` failed with "unknown
    scenario 'prototype' — known: ''". ``fresh_db`` hands back a
    completely empty *public* schema; ``Migrator(..., baseline=BASELINE)
    .apply_all()`` is exactly what a real fresh ``precis migrate`` does
    (load the snapshot, then any tail) — the same "Path B"
    ``test_schema_convergence`` (tests/test_schema_baseline.py) exercises,
    scoped here to the one regression this gripe is about. The explicit
    ``vault`` schema drop mirrors that test too: a prior ``fresh_db``
    teardown in this worker may have left a full (non-baseline) replay's
    ``vault`` schema behind, and the baseline's ``CREATE FUNCTION
    vault.*`` isn't ``OR REPLACE`` — without dropping it first, loading
    the baseline would hit ``DuplicateFunction``.
    """
    if not BASELINE.exists():
        pytest.skip("no baseline snapshot committed yet")
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute('DROP SCHEMA IF EXISTS "vault" CASCADE')
    Migrator(fresh_db, MIGRATIONS_DIR, baseline=BASELINE).apply_all()
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT scenario_id FROM design_scenarios WHERE status = 'core'"
        ).fetchall()
    got = {r[0] for r in rows}
    assert got == _EXPECTED_SCENARIOS, (
        "expected the three core design-scenario presets after loading "
        f"the baseline into a fresh DB, got {sorted(got)!r} — SEED_TABLES "
        "in precis.store.schema_dump is missing design_scenarios/"
        "design_service_environments (gr454487)"
    )

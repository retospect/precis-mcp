"""Schema integrity tests for ``migrations/0001_initial.sql``.

These tests exercise the migration directly against an ephemeral
Postgres database. They do **not** touch the application-layer
``Store`` — that module still references the legacy v1 schema and will
be rewritten in the B2-B7 steps. Once the rewrite lands these tests
remain valid as the canonical structural invariants for the v2 schema.

Coverage:

- The migration applies cleanly and registers itself in ``_migrations``.
- All expected tables and views exist after applying.
- Seed counts and default-flag invariants match the locked design
  (storage-v2.md §"Schema v2", schema-v2.puml).
- CHECK constraints reject malformed rows (ord/chunk_kind, self-loop
  links).
- Partial-unique indices enforce "exactly one default" for embedders
  and summarizers.
- FK CASCADE deletes propagate from ``refs`` to identifiers, chunks,
  and chunk embeddings.
- Generated columns (``chunks.tsv``) populate automatically.
- The ``vector(1024)`` column accepts a 1024-dim vector.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import psycopg
import pytest

from precis.store import Migrator

MIGRATIONS_DIR = Path(__file__).parent.parent / "src" / "precis" / "migrations"
MIGRATION_FILE = MIGRATIONS_DIR / "0001_initial.sql"


# Expected entities. Updating these is part of the contract — change
# them only when changing the locked schema (which means a new
# migration, not an edit to 0001).

EXPECTED_TABLES = {
    "_migrations",
    "actors",
    "kinds",
    "relations",
    "providers",
    "chunk_kinds",
    "embedders",
    "summarizers",
    "pdfs",
    "refs",
    "ref_identifiers",
    "chunks",
    "chunk_embeddings",
    "chunk_summaries",
    "links",
    "tags",
    "ref_tags",
    "chunk_tags",
    "cache_state",
}

EXPECTED_VIEWS = {
    "v_refs",
    "v_ref_tags_all",
    "v_chunk_tags_all",
}

# Seed counts. These match the INSERT blocks in 0001_initial.sql.
EXPECTED_SEED_COUNTS = {
    "actors": 3,
    "kinds": 24,
    # 9 v1 relations + 7 Phase-7 vocabulary additions (derived-from /
    # derived-into / supports / supported-by / generalises / specialises /
    # see-also). See migration header for the rationale.
    "relations": 16,
    # 11 v1 providers + 'web' (added for the WebHandler cache provider).
    "providers": 12,
    "chunk_kinds": 57,
    "embedders": 1,
    "summarizers": 1,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _apply_migration(dsn: str) -> list[str]:
    """Apply ONLY ``0001_initial.sql`` (not the full bundle).

    These tests assert structural invariants of the *initial* migration —
    e.g. seed counts after only 0001 has run. Later migrations
    (0002+) extend the seed sets, so running the whole bundle
    breaks every count-asserting test in this file. To preserve the
    narrow contract, we point Migrator at a temp dir containing
    only ``0001_initial.sql``.
    """
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="precis_test_init_")
    try:
        shutil.copy(MIGRATION_FILE, Path(tmp) / MIGRATION_FILE.name)
        return Migrator(dsn, Path(tmp)).apply_all()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _query_one(dsn: str, sql: str, params: tuple = ()) -> tuple:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    assert row is not None, f"no row returned for: {sql}"
    return row


def _query_all(dsn: str, sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _one(cur: psycopg.Cursor) -> tuple:
    """Fetch one row that the surrounding INSERT...RETURNING / SELECT
    guarantees exists. Centralises the ``is not None`` assertion so the
    call sites stay readable and mypy's ``"None" object is not iterable``
    on the unpacking pattern no longer fires.
    """
    row = cur.fetchone()
    assert row is not None, "_one(cur) returned None unexpectedly"
    return row


# ---------------------------------------------------------------------------
# Migration runner
# ---------------------------------------------------------------------------


def test_migration_applies_cleanly(fresh_db: str) -> None:
    """Applying against an empty DB returns exactly the new migration's
    version. A second apply is a no-op (idempotency)."""
    applied = _apply_migration(fresh_db)
    assert applied == ["0001_initial"]

    again = _apply_migration(fresh_db)
    assert again == [], "second apply should be a no-op"


def test_migrations_ledger_records_correct_checksum(fresh_db: str) -> None:
    """The runner inserts (version, checksum) where checksum matches
    sha256 of the file. Editing a sealed migration changes the checksum
    and the next ``apply_all`` would refuse to run — verify that
    machinery is wired up correctly."""
    _apply_migration(fresh_db)

    expected = hashlib.sha256(MIGRATION_FILE.read_bytes()).hexdigest()
    version, checksum = _query_one(
        fresh_db,
        "SELECT version, checksum FROM _migrations WHERE version = %s",
        ("0001_initial",),
    )
    assert version == "0001_initial"
    assert checksum == expected


# ---------------------------------------------------------------------------
# Structural inventory
# ---------------------------------------------------------------------------


def test_all_expected_tables_present(fresh_db: str) -> None:
    _apply_migration(fresh_db)
    rows = _query_all(
        fresh_db,
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'",
    )
    actual = {r[0] for r in rows}
    missing = EXPECTED_TABLES - actual
    extra = actual - EXPECTED_TABLES
    assert not missing, f"missing tables: {sorted(missing)}"
    assert not extra, f"unexpected tables: {sorted(extra)}"


def test_all_expected_views_present(fresh_db: str) -> None:
    _apply_migration(fresh_db)
    rows = _query_all(
        fresh_db,
        "SELECT viewname FROM pg_views WHERE schemaname = 'public'",
    )
    actual = {r[0] for r in rows}
    missing = EXPECTED_VIEWS - actual
    assert not missing, f"missing views: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table, expected", sorted(EXPECTED_SEED_COUNTS.items()))
def test_seed_counts(fresh_db: str, table: str, expected: int) -> None:
    _apply_migration(fresh_db)
    (count,) = _query_one(fresh_db, f"SELECT count(*) FROM {table}")
    assert count == expected, f"{table}: expected {expected} seeds, got {count}"


def test_default_embedder_is_bge_m3(fresh_db: str) -> None:
    _apply_migration(fresh_db)
    name, dim, is_default = _query_one(
        fresh_db,
        "SELECT name, dim, is_default FROM embedders WHERE is_default = TRUE",
    )
    assert name == "bge-m3"
    assert dim == 1024
    assert is_default is True


def test_default_summarizer_is_rake_lemma(fresh_db: str) -> None:
    _apply_migration(fresh_db)
    name, is_default = _query_one(
        fresh_db,
        "SELECT name, is_default FROM summarizers WHERE is_default = TRUE",
    )
    assert name == "rake-lemma"
    assert is_default is True


# ---------------------------------------------------------------------------
# Partial-unique invariants ("exactly one default")
# ---------------------------------------------------------------------------


def test_only_one_default_embedder(fresh_db: str) -> None:
    _apply_migration(fresh_db)
    with psycopg.connect(fresh_db) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO embedders (name, dim, is_default) "
                    "VALUES ('dupe', 768, TRUE)"
                )


def test_only_one_default_summarizer(fresh_db: str) -> None:
    _apply_migration(fresh_db)
    with psycopg.connect(fresh_db) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO summarizers (name, is_default) VALUES ('dupe', TRUE)"
                )


# ---------------------------------------------------------------------------
# CHECK constraints
# ---------------------------------------------------------------------------


def test_chunks_card_requires_negative_ord(fresh_db: str) -> None:
    """A card chunk_kind with ord >= 0 must be rejected."""
    _apply_migration(fresh_db)
    with psycopg.connect(fresh_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO refs (kind, title) VALUES ('paper', 'T') RETURNING ref_id"
        )
        (ref_id,) = _one(cur)
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, 0, 'card_title', 'x')",
                (ref_id,),
            )


def test_chunks_body_requires_non_negative_ord(fresh_db: str) -> None:
    """A non-card chunk_kind with ord < 0 must be rejected."""
    _apply_migration(fresh_db)
    with psycopg.connect(fresh_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO refs (kind, title) VALUES ('paper', 'T') RETURNING ref_id"
        )
        (ref_id,) = _one(cur)
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, -1, 'paragraph', 'x')",
                (ref_id,),
            )


def test_links_reject_ref_level_self_loop(fresh_db: str) -> None:
    _apply_migration(fresh_db)
    with psycopg.connect(fresh_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO refs (kind, title) VALUES ('paper', 'T') RETURNING ref_id"
        )
        (ref_id,) = _one(cur)
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by) "
                "VALUES (%s, %s, 'cites', 'agent')",
                (ref_id, ref_id),
            )


def test_links_allow_chunk_level_self_link_across_chunks(
    fresh_db: str,
) -> None:
    """Same-ref links at chunk-level precision are allowed as long as the
    chunks differ (you can link a paper's intro chunk to its conclusion
    chunk)."""
    _apply_migration(fresh_db)
    with psycopg.connect(fresh_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO refs (kind, title) VALUES ('paper', 'T') RETURNING ref_id"
        )
        (ref_id,) = _one(cur)
        cur.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, 0, 'paragraph', 'a'), (%s, 1, 'paragraph', 'b') "
            "RETURNING chunk_id",
            (ref_id, ref_id),
        )
        chunk_a = _one(cur)[0]
        chunk_b = _one(cur)[0]
        cur.execute(
            "INSERT INTO links "
            "(src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, "
            " relation, set_by) "
            "VALUES (%s, %s, %s, %s, 'related-to', 'agent')",
            (ref_id, chunk_a, ref_id, chunk_b),
        )


# ---------------------------------------------------------------------------
# FK cascades and ergonomic view
# ---------------------------------------------------------------------------


def test_cascade_delete_from_refs_clears_dependents(fresh_db: str) -> None:
    _apply_migration(fresh_db)
    with psycopg.connect(fresh_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO refs (kind, title) VALUES ('paper', 'T') RETURNING ref_id"
        )
        (ref_id,) = _one(cur)
        cur.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id) "
            "VALUES ('pub_id', 'abc123', %s)",
            (ref_id,),
        )
        cur.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, -1, 'card_combined', 'card'), "
            "       (%s,  0, 'paragraph',     'body') "
            "RETURNING chunk_id",
            (ref_id, ref_id),
        )
        chunks = [r[0] for r in cur.fetchall()]
        cur.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector) "
            "SELECT chunk_id, 'bge-m3', "
            "       array_fill(0.01::real, ARRAY[1024])::vector "
            "FROM chunks WHERE ref_id = %s",
            (ref_id,),
        )

        cur.execute("DELETE FROM refs WHERE ref_id = %s", (ref_id,))

        for table in ("ref_identifiers", "chunks", "chunk_embeddings"):
            cur.execute(f"SELECT count(*) FROM {table}")
            (n,) = _one(cur)
            assert n == 0, f"{table} not cascaded: {n} rows remain"

        assert chunks  # quiet linter; chunks variable is referenced


def test_v_refs_exposes_identifier_columns(fresh_db: str) -> None:
    _apply_migration(fresh_db)
    with psycopg.connect(fresh_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO refs (kind, title, year) "
            "VALUES ('paper', 'Test', 2024) RETURNING ref_id"
        )
        (ref_id,) = _one(cur)
        cur.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id) VALUES "
            "('pub_id',   'a3f7k1',    %s), "
            "('cite_key', 'miller24a', %s)",
            (ref_id, ref_id),
        )
        cur.execute(
            "SELECT pub_id, cite_key, paper_id, title, year "
            "FROM v_refs WHERE ref_id = %s",
            (ref_id,),
        )
        pub_id, cite_key, paper_id, title, year = _one(cur)
        assert pub_id == "a3f7k1"
        assert cite_key == "miller24a"
        assert paper_id is None  # not inserted
        assert title == "Test"
        assert year == 2024


# ---------------------------------------------------------------------------
# Generated columns and vector type
# ---------------------------------------------------------------------------


def test_chunks_tsv_is_populated_on_insert(fresh_db: str) -> None:
    _apply_migration(fresh_db)
    with psycopg.connect(fresh_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO refs (kind, title) VALUES ('paper', 'T') RETURNING ref_id"
        )
        (ref_id,) = _one(cur)
        cur.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, 0, 'paragraph', 'the quick brown fox jumps over') "
            "RETURNING tsv::text",
            (ref_id,),
        )
        (tsv,) = _one(cur)
        # ts_vector for that text contains lemmatised tokens
        assert "quick" in tsv
        assert "brown" in tsv


def test_chunk_embeddings_accepts_1024_vector(fresh_db: str) -> None:
    _apply_migration(fresh_db)
    with psycopg.connect(fresh_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO refs (kind, title) VALUES ('paper', 'T') RETURNING ref_id"
        )
        (ref_id,) = _one(cur)
        cur.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, 0, 'paragraph', 'x') RETURNING chunk_id",
            (ref_id,),
        )
        (chunk_id,) = _one(cur)
        cur.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector) "
            "VALUES (%s, 'bge-m3', array_fill(0.01::real, ARRAY[1024])::vector)",
            (chunk_id,),
        )
        cur.execute(
            "SELECT vector_dims(vector) FROM chunk_embeddings WHERE chunk_id = %s",
            (chunk_id,),
        )
        (dims,) = _one(cur)
        assert dims == 1024


def test_chunk_embeddings_rejects_wrong_dim_vector(fresh_db: str) -> None:
    _apply_migration(fresh_db)
    with psycopg.connect(fresh_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO refs (kind, title) VALUES ('paper', 'T') RETURNING ref_id"
        )
        (ref_id,) = _one(cur)
        cur.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, 0, 'paragraph', 'x') RETURNING chunk_id",
            (ref_id,),
        )
        (chunk_id,) = _one(cur)
        with pytest.raises(psycopg.errors.DataException):
            cur.execute(
                "INSERT INTO chunk_embeddings (chunk_id, embedder, vector) "
                "VALUES (%s, 'bge-m3', array_fill(0.01::real, ARRAY[768])::vector)",
                (chunk_id,),
            )

"""Migration runner tests.

Each test gets an ephemeral postgres database via the `fresh_db`
fixture; the migration runner applies all bundled migrations against
it and we verify schema + seed contents."""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from precis.store import Migrator
from tests.conftest import MIGRATIONS_DIR


def _fetch(dsn: str, sql: str) -> list[dict]:
    """Connect, run sql, return rows as dicts."""
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(sql).fetchall()


def test_apply_creates_all_tables(fresh_db: str) -> None:
    migrator = Migrator(fresh_db, MIGRATIONS_DIR)
    applied = migrator.apply_all()
    # Initial migration must be first; everything that follows
    # extends it. Loose assertion on later versions — they all
    # follow the NNNN_<name> convention.
    assert applied[0] == "0001_initial"
    assert all(v[:4].isdigit() and v[4] == "_" for v in applied)

    rows = _fetch(
        fresh_db,
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
        "ORDER BY tablename",
    )
    names = {r["tablename"] for r in rows}

    # v2 schema. v1 tables (blocks/corpuses/density_levels/
    # flag_names/ref_closed_tags/ref_flags/ref_open_tags/system/
    # tag_prefixes) were folded or removed during the v2 redesign;
    # see ADR 0001 + storage-v2.md.
    expected = {
        "_migrations",
        "actors",
        "cache_state",
        "chunks",
        "chunk_embeddings",
        "chunk_summaries",
        "chunk_tags",
        "kinds",
        "links",
        "providers",
        "ref_identifiers",
        "ref_tags",
        "refs",
        "relations",
        "tags",
    }
    assert expected.issubset(names), f"missing tables: {expected - names}"


def test_seeds_populated(fresh_db: str) -> None:
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    actors = {r["slug"] for r in _fetch(fresh_db, "SELECT slug FROM actors")}
    kinds = {r["slug"] for r in _fetch(fresh_db, "SELECT slug FROM kinds")}
    relations = {r["slug"] for r in _fetch(fresh_db, "SELECT slug FROM relations")}

    # v2 unified the legacy tag_prefixes / flag_names / density_levels
    # vocabularies into the canonical ``tags`` table (namespace +
    # value); they're no longer separate tables. The remaining
    # vocab-table seed checks (actors / kinds / relations) still
    # apply.
    assert {"agent", "user", "system"}.issubset(actors)
    assert {"paper", "memory", "todo", "flashcard", "web", "youtube"}.issubset(kinds)
    assert {"related-to", "blocks", "contradicts"}.issubset(relations)


def test_extensions_installed(fresh_db: str) -> None:
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    rows = _fetch(fresh_db, "SELECT extname FROM pg_extension ORDER BY extname")
    names = {r["extname"] for r in rows}
    assert "vector" in names
    assert "pg_trgm" in names


def test_system_singleton_seeded(fresh_db: str) -> None:
    """v2 replaced the ``system`` key/value table with two surfaces:

    - ``app_state`` (migration 0003) — small KV for boot-time bookkeeping.
    - ``embedders.dim`` — the embedding dim now lives on the embedder
      registry row, where it belongs.

    The assertion shifted accordingly.
    """
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    rows = _fetch(
        fresh_db,
        "SELECT name, dim FROM embedders WHERE is_default = TRUE",
    )
    assert len(rows) == 1
    assert rows[0]["name"] == "bge-m3"
    assert rows[0]["dim"] == 1024


def test_apply_is_idempotent(fresh_db: str) -> None:
    migrator = Migrator(fresh_db, MIGRATIONS_DIR)
    first = migrator.apply_all()
    second = migrator.apply_all()
    assert first[0] == "0001_initial"
    assert len(first) >= 1
    assert second == [], "second run must be a no-op"


def test_applied_versions(fresh_db: str) -> None:
    migrator = Migrator(fresh_db, MIGRATIONS_DIR)
    assert migrator.applied_versions() == []
    pending_before = migrator.pending()
    assert pending_before[0] == "0001_initial"
    assert len(pending_before) >= 1

    migrator.apply_all()
    applied = migrator.applied_versions()
    assert applied == pending_before  # full set applied
    assert migrator.pending() == []


def test_checksum_drift_refuses(
    fresh_db: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a sealed migration's content changes, the runner refuses to run."""
    # Stage 1: apply against a snapshot of migrations
    snapshot_dir = tmp_path / "migrations"
    snapshot_dir.mkdir()
    (snapshot_dir / "0001_initial.sql").write_text(
        (MIGRATIONS_DIR / "0001_initial.sql").read_text()
    )
    Migrator(fresh_db, snapshot_dir).apply_all()

    # Stage 2: mutate the file and try to apply again
    (snapshot_dir / "0001_initial.sql").write_text("-- mutated content\nSELECT 1;\n")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        Migrator(fresh_db, snapshot_dir).apply_all()

"""Migration runner tests.

Each test gets an ephemeral postgres database via the `fresh_db`
fixture; the migration runner applies `0001_initial.sql` against it
and we verify schema + seed contents."""

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
    assert applied == ["0001_initial"]

    rows = _fetch(
        fresh_db,
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
        "ORDER BY tablename",
    )
    names = {r["tablename"] for r in rows}

    expected = {
        "_migrations",
        "actors",
        "blocks",
        "cache_state",
        "corpuses",
        "density_levels",
        "flag_names",
        "kinds",
        "links",
        "providers",
        "ref_closed_tags",
        "ref_flags",
        "ref_open_tags",
        "refs",
        "relations",
        "system",
        "tag_prefixes",
    }
    assert expected.issubset(names), f"missing tables: {expected - names}"


def test_seeds_populated(fresh_db: str) -> None:
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    actors = {r["slug"] for r in _fetch(fresh_db, "SELECT slug FROM actors")}
    kinds = {r["slug"] for r in _fetch(fresh_db, "SELECT slug FROM kinds")}
    relations = {r["slug"] for r in _fetch(fresh_db, "SELECT slug FROM relations")}
    prefixes = {
        r["prefix"] for r in _fetch(fresh_db, "SELECT prefix FROM tag_prefixes")
    }
    flags = {r["name"] for r in _fetch(fresh_db, "SELECT name FROM flag_names")}
    densities = {
        r["level"] for r in _fetch(fresh_db, "SELECT level FROM density_levels")
    }

    assert actors == {"agent", "user", "system"}
    # `kinds` table holds ref-backed kinds only. Stateless kinds (calc,
    # plot, clock, rng) live in the in-tree handler registry, not the DB.
    assert {"paper", "memory", "todo", "fc", "web", "youtube"}.issubset(kinds)
    assert {"related-to", "blocks", "contradicts"}.issubset(relations)
    assert {"SRC", "CACHE", "DENSITY", "STATUS", "PRIO", "CONFIDENCE"} == prefixes
    assert {"pinned", "urgent", "private"}.issubset(flags)
    assert densities == {"sparse", "medium", "dense"}


def test_extensions_installed(fresh_db: str) -> None:
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    rows = _fetch(fresh_db, "SELECT extname FROM pg_extension ORDER BY extname")
    names = {r["extname"] for r in rows}
    assert "vector" in names
    assert "pg_trgm" in names


def test_system_singleton_seeded(fresh_db: str) -> None:
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    rows = _fetch(fresh_db, "SELECT key, value FROM system")
    settings = {r["key"]: r["value"] for r in rows}
    assert settings["embedding_model"] == "BAAI/bge-m3"
    assert settings["embedding_dim"] == "1024"
    assert settings["schema_epoch"] == "1"


def test_apply_is_idempotent(fresh_db: str) -> None:
    migrator = Migrator(fresh_db, MIGRATIONS_DIR)
    first = migrator.apply_all()
    second = migrator.apply_all()
    assert first == ["0001_initial"]
    assert second == [], "second run must be a no-op"


def test_applied_versions(fresh_db: str) -> None:
    migrator = Migrator(fresh_db, MIGRATIONS_DIR)
    assert migrator.applied_versions() == []
    assert migrator.pending() == ["0001_initial"]

    migrator.apply_all()
    assert migrator.applied_versions() == ["0001_initial"]
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

"""Migration runner tests.

Each test gets an ephemeral postgres database via the `fresh_db`
fixture; the migration runner applies all bundled migrations against
it and we verify schema + seed contents."""

from __future__ import annotations

import hashlib

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
    # extends it. ``applied`` is a list of ``(plugin, version)``
    # tuples post-0023; every entry here lives under the built-in
    # ``precis`` plugin.
    assert applied[0] == ("precis", "0001_initial")
    assert all(p == "precis" for p, _ in applied)
    assert all(v[:4].isdigit() and v[4] == "_" for _, v in applied)

    rows = _fetch(
        fresh_db,
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
        "ORDER BY tablename",
    )
    names = {r["tablename"] for r in rows}

    # v2 schema. v1 tables (blocks/corpuses/density_levels/
    # flag_names/ref_closed_tags/ref_flags/ref_open_tags/system/
    # tag_prefixes) were folded or removed during the v2 redesign;
    # see the acatome merge + storage-v2.md.
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


def test_llm_call_log_hash_indexes_removed_but_foreign_keys_kept(fresh_db: str) -> None:
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    indexes = _fetch(
        fresh_db,
        "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
        "AND indexname IN ('llm_call_log_request_hash_idx', "
        "'llm_call_log_response_hash_idx')",
    )
    assert indexes == []
    foreign_keys = _fetch(
        fresh_db,
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'public.llm_call_log'::regclass AND contype = 'f'",
    )
    assert {
        "llm_call_log_request_hash_fkey",
        "llm_call_log_response_hash_fkey",
    }.issubset({row["conname"] for row in foreign_keys})


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
    assert {"paper", "memory", "todo", "anki", "web", "youtube"}.issubset(kinds)
    assert {"related-to", "blocks", "contradicts"}.issubset(relations)
    # Acquisition-mode findings / migration 0105 — links.relation FKs
    # to relations(slug), so a fresh DB must seed this alongside the rest
    # (taproot evidence relations's `establishes` precedent — the trap this AC guards).
    assert "awaits-evidence" in relations


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
    assert first[0] == ("precis", "0001_initial")
    assert len(first) >= 1
    assert second == [], "second run must be a no-op"


def test_applied_versions(fresh_db: str) -> None:
    migrator = Migrator(fresh_db, MIGRATIONS_DIR)
    assert migrator.applied_versions() == []
    pending_before = migrator.pending()
    assert pending_before[0] == ("precis", "0001_initial")
    assert len(pending_before) >= 1

    migrator.apply_all()
    applied = migrator.applied_versions()
    # Sorted ``applied`` matches the source-order ``pending_before``
    # when there's only one plugin (precis) and the filenames sort
    # in the same order they apply.
    assert applied == sorted(pending_before)
    assert migrator.pending() == []


def test_migrations_plugin_column_populated(fresh_db: str) -> None:
    """After 0023 applies every row in ``_migrations`` carries the
    ``plugin`` column, defaulting to ``'precis'`` for built-ins.

    Guards against a future refactor that forgets to populate the
    column for the precis plugin itself.
    """
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    rows = _fetch(
        fresh_db,
        "SELECT plugin, version FROM _migrations ORDER BY plugin, version",
    )
    assert rows, "_migrations should be non-empty after apply"
    assert all(r["plugin"] == "precis" for r in rows), (
        "every built-in migration must record plugin='precis'"
    )


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
        (MIGRATIONS_DIR / "0001_initial.sql").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    Migrator(fresh_db, snapshot_dir).apply_all()

    # Stage 2: mutate the file and try to apply again
    (snapshot_dir / "0001_initial.sql").write_text(
        "-- mutated content\nSELECT 1;\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        Migrator(fresh_db, snapshot_dir).apply_all()


def test_checksum_drift_rebuilds(
    fresh_db: str,
    tmp_path,
) -> None:
    """rebuild_on_checksum_mismatch replays from scratch instead of refusing.

    The disposable-test-DB escape hatch (gr479991): a redrafted migration
    drops every user object and replays the chain, restamping the ledger
    with the new checksum — the in-DB equivalent of recreating the
    test-db container.
    """
    snapshot_dir = tmp_path / "migrations"
    snapshot_dir.mkdir()
    original = (MIGRATIONS_DIR / "0001_initial.sql").read_text(encoding="utf-8")
    (snapshot_dir / "0001_initial.sql").write_text(original, encoding="utf-8")
    Migrator(fresh_db, snapshot_dir).apply_all()

    # An object no migration owns — the rebuild must remove it.
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute("CREATE TABLE stale_junk (x int)")

    # Mutate the sealed file (a "redrafted draft"): same version, new bytes.
    mutated = original + "\nCREATE TABLE public.drift_marker (x int);\n"
    (snapshot_dir / "0001_initial.sql").write_text(mutated, encoding="utf-8")

    # Strict callers still refuse.
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        Migrator(fresh_db, snapshot_dir).apply_all()

    # The disposable-DB path drops and replays.
    applied = Migrator(fresh_db, snapshot_dir).apply_all(
        rebuild_on_checksum_mismatch=True
    )
    assert applied == [("precis", "0001_initial")]
    tables = {
        r["tablename"]
        for r in _fetch(
            fresh_db,
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'",
        )
    }
    assert "drift_marker" in tables  # the mutated file ran
    assert "chunks" in tables  # the replayed chain is intact
    assert "stale_junk" not in tables  # the drop actually ran
    ledger = _fetch(fresh_db, "SELECT version, checksum FROM _migrations")
    assert ledger == [
        {
            "version": "0001_initial",
            "checksum": hashlib.sha256(mutated.encode("utf-8")).hexdigest(),
        }
    ]


def test_renumbered_draft_rebuilds(
    fresh_db: str,
    tmp_path,
) -> None:
    """A renumbered draft's orphan ledger row triggers the same rebuild.

    Renumbering leaves an applied ``(plugin, version)`` no file
    provides. Strict mode tolerates it — archived migrations leave the
    same orphans in real ledgers — but the test-DB rebuild treats it
    as drift and replays (gr479991).
    """
    snapshot_dir = tmp_path / "migrations"
    snapshot_dir.mkdir()
    (snapshot_dir / "0001_initial.sql").write_text(
        (MIGRATIONS_DIR / "0001_initial.sql").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (snapshot_dir / "9000_draft.sql").write_text(
        "CREATE TABLE public.draft_tbl (x int);\n", encoding="utf-8"
    )
    Migrator(fresh_db, snapshot_dir).apply_all()

    # Renumber the draft; idempotent SQL so the new version can apply
    # over the already-built table.
    (snapshot_dir / "9000_draft.sql").unlink()
    (snapshot_dir / "9001_draft.sql").write_text(
        "CREATE TABLE IF NOT EXISTS public.draft_tbl (x int);\n", encoding="utf-8"
    )
    applied = Migrator(fresh_db, snapshot_dir).apply_all()
    assert applied == [("precis", "9001_draft")]
    versions = {
        r["version"] for r in _fetch(fresh_db, "SELECT version FROM _migrations")
    }
    assert versions == {"0001_initial", "9000_draft", "9001_draft"}

    applied = Migrator(fresh_db, snapshot_dir).apply_all(
        rebuild_on_checksum_mismatch=True
    )
    assert applied == [("precis", "0001_initial"), ("precis", "9001_draft")]
    versions = {
        r["version"] for r in _fetch(fresh_db, "SELECT version FROM _migrations")
    }
    assert versions == {"0001_initial", "9001_draft"}
    tables = {
        r["tablename"]
        for r in _fetch(
            fresh_db,
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'",
        )
    }
    assert "draft_tbl" in tables

"""Post-migration pending-steps assert (gr514955, gr513663).

``precis.migration_postchecks`` is the one read-only probe shared by
``precis post-migration-check`` (which ``scripts/release`` runs after the
deploy's migrate play) and the health-digest ``post_migration_steps`` row.
Prod ran weeks on the global-index fallback because nothing noticed 0226's
NOT VALID constraints and unbuilt per-kind HNSW indexes; these tests pin
that the probe names each of them with the command that clears it.
"""

from __future__ import annotations

import argparse
from typing import cast

import pytest

from precis import migration_postchecks
from precis.cli import post_migration_check
from precis.migration_postchecks import (
    POST_MIGRATION_STEPS,
    PendingStep,
    format_pending,
    pending_post_steps,
)
from precis.store import Store
from precis.store._chunks_ops import _ANN_PARTIAL_KINDS, ann_partial_index_name
from tests.test_kind_scoped_ann import _autocommit, _legacy_nulls

_BACKFILL = "precis backfill-embedding-kinds --apply"


def _pending(store: Store) -> list[PendingStep]:
    with store.pool.connection() as conn:
        return pending_post_steps(conn)


def test_registry_maps_unique_constraints_and_one_ann_owner() -> None:
    names = [c for s in POST_MIGRATION_STEPS for c in s.constraints]
    assert len(names) == len(set(names))
    assert sum(1 for s in POST_MIGRATION_STEPS if s.ann_indexes) == 1
    assert all("/" in s.migration for s in POST_MIGRATION_STEPS)


def test_pending_names_each_missing_partial_index(store: Store) -> None:
    """The test DB's 0226 gate is validated but the backfill never ran —
    every expected partial index must be named with the backfill command."""
    items = _pending(store)
    by_name = {i.name: i for i in items}
    for kind in _ANN_PARTIAL_KINDS:
        name = ann_partial_index_name(kind)
        assert name in by_name, f"{name} not reported pending: {by_name}"
        assert by_name[name].table == "chunk_embeddings"
        assert by_name[name].migration == "precis/0226_chunk_embeddings_kind_ann"
        assert by_name[name].fix == _BACKFILL


def test_pending_constraint_under_unvalidated_gate(store: Store) -> None:
    """Prod's post-0226 shape: gate CHECK NOT VALID -> the constraint item
    names the backfill, and the indexes collapse to one summary line (they
    could not serve even if built)."""
    with store.pool.connection() as conn:
        _legacy_nulls(conn, [])
        items = pending_post_steps(conn)
        by_name = {i.name: i for i in items}
        gate = by_name["chunk_embeddings_ref_kind_filled"]
        assert gate.migration == "precis/0226_chunk_embeddings_kind_ann"
        assert gate.fix == _BACKFILL
        index_items = [i for i in items if "HNSW" in i.name]
        assert len(index_items) == 1
        assert index_items[0].fix == _BACKFILL
        conn.rollback()


def test_pending_unregistered_constraint_gets_validate_hint(store: Store) -> None:
    """A NOT VALID constraint with no registry entry still names itself and
    its own VALIDATE — the check cannot be ducked by skipping registration."""
    with store.pool.connection() as conn:
        conn.execute(
            "ALTER TABLE chunks ADD CONSTRAINT postcheck_scratch_check "
            "CHECK (ord IS NOT NULL) NOT VALID"
        )
        items = pending_post_steps(conn)
        conn.rollback()
    item = next(i for i in items if i.name == "postcheck_scratch_check")
    assert item.migration == ""
    assert "VALIDATE CONSTRAINT postcheck_scratch_check" in item.fix
    assert "unregistered" in item.fix


def test_built_partial_indexes_are_not_pending(store: Store) -> None:
    """Regression for the prod blind spot: once the per-kind indexes exist
    and are valid, the probe has nothing left to name."""
    from precis.cli.embedding_kind_backfill import build_kind_indexes

    with _autocommit(store) as conn:
        build_kind_indexes(conn)
    try:
        items = _pending(store)
        names = {i.name for i in items}
        for kind in _ANN_PARTIAL_KINDS:
            assert ann_partial_index_name(kind) not in names
        assert not any("HNSW" in n for n in names)
    finally:
        with _autocommit(store) as conn:
            for kind in _ANN_PARTIAL_KINDS:
                conn.execute(f"DROP INDEX IF EXISTS {ann_partial_index_name(kind)}")


def test_format_pending_names_items_and_clean() -> None:
    item = PendingStep("c1", "t1", "precis/0001_x", "run-me")
    out = format_pending([item])
    assert "1 pending" in out and "c1" in out and "run-me" in out
    assert "precis/0001_x" in out
    assert "clean" in format_pending([])


def test_cli_run_prints_pending(store: Store, capsys: pytest.CaptureFixture) -> None:
    args = argparse.Namespace(database_url=cast(str, store.pool.conninfo), strict=False)
    post_migration_check.run(args)
    out = capsys.readouterr().out
    assert "post-migration check:" in out
    assert "precis backfill-embedding-kinds --apply" in out


def test_cli_strict_exits_two_when_pending(store: Store) -> None:
    args = argparse.Namespace(database_url=cast(str, store.pool.conninfo), strict=True)
    with pytest.raises(SystemExit) as exc:
        post_migration_check.run(args)
    assert exc.value.code == 2


def test_cli_strict_clean_exits_zero(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(migration_postchecks, "pending_post_steps", lambda conn: [])
    args = argparse.Namespace(database_url=cast(str, store.pool.conninfo), strict=True)
    post_migration_check.run(args)  # no SystemExit


def test_health_digest_row_names_pending(store: Store) -> None:
    from precis.workers.health_digest import _check_post_migration_steps

    with store.pool.connection() as conn:
        result = _check_post_migration_steps(conn)
    assert result.name == "post_migration_steps"
    assert result.status == "stale"
    assert "post-migration steps pending:" in result.detail


def test_health_digest_row_ok_when_clean(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(migration_postchecks, "pending_post_steps", lambda conn: [])
    from precis.workers.health_digest import _check_post_migration_steps

    with store.pool.connection() as conn:
        result = _check_post_migration_steps(conn)
    assert result.status == "ok"

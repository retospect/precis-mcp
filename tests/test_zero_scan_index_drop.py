"""gr575086: migration 0312 drops the ten zero-scan indexes.

Pins the drop list itself (a typo'd or scope-creeping ``DROP INDEX``
lands on prod with the deploy), the
``_ANN_PARTIAL_KINDS`` / ``_ANN_PARTIAL_INDEX_KINDS`` split that keeps
the three dead per-kind partials routable but not rebuildable, and the
``struct_bonds_j_fkey`` grandfather the drop requires.
"""

from __future__ import annotations

import re

import pytest

from precis.cli.embedding_kind_backfill import _kind_index_ddl
from precis.migration_postchecks import pending_post_steps
from precis.store import Store
from precis.store._chunks_ops import (
    _ANN_DROPPED_PARTIAL_KINDS,
    _ANN_PARTIAL_INDEX_KINDS,
    _ANN_PARTIAL_KINDS,
    _ann_route,
    _reset_ann_state_cache,
    ann_partial_index_name,
)
from tests.conftest import MIGRATIONS_DIR
from tests.test_schema_design import UNINDEXED_FKS

_MIGRATION = MIGRATIONS_DIR / "0312_drop_zero_scan_indexes.sql"

#: The ten index names gr575086 reported at idx_scan=0 — the exact set
#: the migration must drop, no more, no fewer.
_DROPPED = frozenset(
    {
        "chunk_embeddings_vec_hnsw_draft_idx",
        "chunk_embeddings_vec_hnsw_quest_idx",
        "chunk_embeddings_vec_hnsw_tex_idx",
        "parts_params_idx",
        "paper_bib_entries_openalex_id_idx",
        "paper_authors_fullname_trgm_idx",
        "paper_authors_openalex_idx",
        "paper_authors_orcid_idx",
        "struct_bonds_ref_j_idx",
        "struct_bonds_j_idx",
    }
)

#: The gripe's own exclusions: constraint-backing, never droppable.
_KEPT = ("struct_atoms_ref_label_key", "chunk_citations_pkey")

_DROP_RE = re.compile(r"DROP\s+INDEX\s+IF\s+EXISTS\s+(?:public\.)?(\w+)", re.IGNORECASE)


def test_migration_drops_exactly_the_reported_ten() -> None:
    sql = _MIGRATION.read_text(encoding="utf-8")
    assert frozenset(_DROP_RE.findall(sql)) == _DROPPED
    # Every drop is IF EXISTS — parts_params_idx and the per-kind HNSW
    # partials exist only where prod built them; a bare DROP INDEX would
    # fail every other database.
    assert not re.search(r"DROP\s+INDEX\s+(?!IF\s+EXISTS)", sql, re.IGNORECASE)
    for kept in _KEPT:
        assert kept not in _DROP_RE.findall(sql)


def test_dropped_partial_kinds_split() -> None:
    """The dropped kinds stay routable (a missing partial falls back to
    the global route, a rebuilt index serves again) but are out of the
    expected/buildable index set."""
    assert {"draft", "quest", "tex"} == _ANN_DROPPED_PARTIAL_KINDS
    assert set(_ANN_PARTIAL_KINDS) >= _ANN_DROPPED_PARTIAL_KINDS
    assert (
        set(_ANN_PARTIAL_INDEX_KINDS)
        == set(_ANN_PARTIAL_KINDS) - _ANN_DROPPED_PARTIAL_KINDS
    )


def test_index_ddl_defaults_to_index_kinds() -> None:
    """``build_kind_indexes`` (the recorded post-step) must not rebuild
    the dropped partials."""
    names = {name for name, _ in _kind_index_ddl()}
    assert names == {ann_partial_index_name(k) for k in _ANN_PARTIAL_INDEX_KINDS}
    for kind in _ANN_DROPPED_PARTIAL_KINDS:
        assert ann_partial_index_name(kind) not in names


def test_j_fkey_uncoverage_is_grandfathered() -> None:
    """Dropping ``struct_bonds_j_idx`` leaves ``struct_bonds_j_fkey``
    uncovered — the schema-design ratchet carries it deliberately
    (dormant probe; see the migration header)."""
    assert "struct_bonds.struct_bonds_j_fkey" in UNINDEXED_FKS


@pytest.mark.db
def test_dropped_kinds_route_global_without_their_index(
    store: Store,
) -> None:
    """No index built: the designed degrade, not an error path."""
    _reset_ann_state_cache()
    with store.pool.connection() as conn:
        for kind in _ANN_DROPPED_PARTIAL_KINDS:
            assert _ann_route(conn, kind, None) == ("global", kind)


@pytest.mark.db
def test_postchecks_does_not_expect_dropped_indexes(store: Store) -> None:
    """``precis post-migration-check`` names every missing expected
    partial — and must not ask for the ones 0312 dropped."""
    with store.pool.connection() as conn:
        names = {i.name for i in pending_post_steps(conn)}
    for kind in _ANN_PARTIAL_INDEX_KINDS:
        assert ann_partial_index_name(kind) in names
    for kind in _ANN_DROPPED_PARTIAL_KINDS:
        assert ann_partial_index_name(kind) not in names

"""``Store.set_doi_validation`` + the ``refs.doi_status``/``doi_validated_at``
mapper round-trip (migration 0132, docs/backlog/draft-doi-completeness-check.md).

DB-backed on purpose: the migration adds two columns to ``refs`` and the
projection/mapper wiring (``_REFS_COLS`` / ``_REFS_COLS_ALIASED`` /
``_REFS_COLS_FOR_CACHE`` / ``_row_to_ref``) is exactly the kind of
column-count drift that silently ships a wrong tuple index — a unit test
against a hand-rolled fake would never catch it.
"""

from __future__ import annotations

from precis.store import Store


def test_new_ref_reads_as_never_validated(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="dv-fresh", title="Fresh")
    assert ref.doi_status is None
    assert ref.doi_validated_at is None

    reread = store.get_ref(kind="paper", id="dv-fresh")
    assert reread is not None
    assert reread.doi_status is None
    assert reread.doi_validated_at is None


def test_set_doi_validation_stamps_status_and_timestamp(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="dv-valid", title="Valid")

    store.set_doi_validation(ref.id, status="valid")

    reread = store.get_ref(kind="paper", id="dv-valid")
    assert reread is not None
    assert reread.doi_status == "valid"
    assert reread.doi_validated_at is not None


def test_set_doi_validation_writes_not_found_too(store: Store) -> None:
    """Unlike retraction's clean-read no-clobber rule, both outcomes are
    written here — there is no second source to preserve."""
    ref = store.insert_ref(kind="paper", slug="dv-dead", title="Dead DOI")

    store.set_doi_validation(ref.id, status="not_found")

    reread = store.get_ref(kind="paper", id="dv-dead")
    assert reread is not None
    assert reread.doi_status == "not_found"
    assert reread.doi_validated_at is not None


def test_fetch_refs_by_ids_carries_the_doi_columns(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="dv-batch", title="Batch")
    store.set_doi_validation(ref.id, status="valid")

    fetched = store.fetch_refs_by_ids([ref.id])[ref.id]
    assert fetched.doi_status == "valid"
    assert fetched.doi_validated_at is not None

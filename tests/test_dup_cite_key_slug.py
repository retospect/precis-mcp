"""A paper with >1 cite_key must not 500 the slug-resolving read paths.

Real-PG regression for a `CardinalityViolation` that took down `/papers`
in prod: the `slug` column is a correlated scalar subquery over
`ref_identifiers (id_kind='cite_key')`, and prod accumulated papers with
two or three cite_key rows (re-slugs / merges). Without a `LIMIT 1` the
scalar subquery returns multiple rows and Postgres raises. The web
FakeStore doesn't execute SQL, so this can only be caught against real
pgvector.
"""

from __future__ import annotations

from precis.store import Store


def _add_cite_key(store: Store, ref_id: int, value: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('cite_key', %s, %s, 'test')",
            (value, ref_id),
        )


def test_duplicate_cite_keys_resolve_to_one_slug(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="alpha2020", title="Dup")
    # A second (and third) cite_key on the same ref — the prod condition.
    _add_cite_key(store, ref.id, "beta2020")
    _add_cite_key(store, ref.id, "gamma2020")

    # None of the slug-resolving read paths may raise CardinalityViolation;
    # each must collapse the multiple cite_keys to a single slug.
    listed = [r for r in store.list_refs(kind="paper", limit=50) if r.id == ref.id]
    assert listed and listed[0].slug in {"alpha2020", "beta2020", "gamma2020"}

    fetched = store.fetch_refs_by_ids([ref.id]).get(ref.id)
    assert fetched is not None
    assert fetched.slug in {"alpha2020", "beta2020", "gamma2020"}

    hits = store.search_refs_lexical(q="Dup", kind="paper", limit=10)
    assert any(r.id == ref.id for r, _ in hits)


def test_soft_deleted_slug_is_reclaimed_by_new_ref(store: Store) -> None:
    """Soft-delete must not orphan a later same-slug re-creation (gr201814).

    Soft-delete only stamps ``deleted_at``; it does not release the
    ``ref_identifiers`` cite_key row. Before the fix, a re-created same-slug
    ref got NO cite_key (``ON CONFLICT DO NOTHING``) and ``get_ref(slug)``
    resolved the slug to the dead ref → filtered by ``deleted_at`` → ``None``,
    silently orphaning the new ref. ``insert_ref`` now reclaims a slug bound to
    a soft-deleted owner.
    """
    first = store.insert_ref(kind="paper", slug="recycle99", title="First")
    store.soft_delete_ref(first.id)
    # The slug now points at a soft-deleted ref → live lookup is None.
    assert store.get_ref(kind="paper", id="recycle99") is None

    # A new ref reusing the same (content-addressed) slug reclaims it.
    second = store.insert_ref(kind="paper", slug="recycle99", title="Second")
    assert second.id != first.id
    assert second.slug == "recycle99"

    got = store.get_ref(kind="paper", id="recycle99")
    assert got is not None
    assert got.id == second.id  # the live ref, not the dead one
    assert got.title == "Second"


def test_live_slug_is_not_stolen_by_a_second_insert(store: Store) -> None:
    """The reclaim steals only from *soft-deleted* owners — a slug held by a
    LIVE ref is never reassigned by a later same-slug insert (the
    one-live-ref-per-slug invariant; the conflict still no-ops)."""
    first = store.insert_ref(kind="paper", slug="held42", title="Holder")
    second = store.insert_ref(kind="paper", slug="held42", title="Interloper")

    # The interloper did not grab the slug; it still resolves to the holder.
    got = store.get_ref(kind="paper", id="held42")
    assert got is not None
    assert got.id == first.id
    assert got.title == "Holder"
    # The interloper row exists (reachable by numeric id) but carries no
    # cite_key — a live conflict is left untouched.
    assert second.id != first.id
    by_num = store.get_ref(kind="paper", id=second.id)
    assert by_num is not None and by_num.title == "Interloper"

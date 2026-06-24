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

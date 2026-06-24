"""``Store.chunk_pages`` — ord → page_first map for the paper sidebar nav.

Real-PG: validates the ``= ANY`` lookup and the NULL-page omission
(``page_first IS NOT NULL``) against pgvector rather than the FakeStore.
"""

from __future__ import annotations

from precis.store import BlockInsert, Store


def test_chunk_pages_maps_ords_and_omits_null_pages(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="cp-pages", title="CP")
    store.insert_blocks(
        ref.id,
        [
            BlockInsert(pos=0, text="alpha alpha"),
            BlockInsert(pos=1, text="beta beta"),
            BlockInsert(pos=2, text="gamma gamma"),
        ],
    )
    # page_first defaults to NULL on the BlockInsert path -> all omitted.
    assert store.chunk_pages(ref.id, [0, 1, 2]) == {}
    assert store.chunk_pages(ref.id, []) == {}

    # Stamp pages on two of the three chunks; the un-stamped one (ord 1)
    # must drop out of the map rather than surface a None page.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET page_first = %s WHERE ref_id = %s AND ord = %s",
            (3, ref.id, 0),
        )
        conn.execute(
            "UPDATE chunks SET page_first = %s WHERE ref_id = %s AND ord = %s",
            (5, ref.id, 2),
        )
    assert store.chunk_pages(ref.id, [0, 1, 2]) == {0: 3, 2: 5}

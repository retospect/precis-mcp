"""Supersede inherits title/authors the survivor lacks (gr353777).

Real-PG only. Fill-only: a survivor's own title/byline is never overwritten.
"""

from __future__ import annotations

from precis.ingest.add import _reconcile_orphan_stub
from precis.ingest.dedup import merge_duplicate

_TITLE = "Graphitic cones and the nucleation of curved carbon surfaces"
_AUTHORS = [
    {"given": "A.", "family": "Krishnan"},
    {"given": "E.", "family": "Dujardin"},
]


def _state(store, ref_id):
    with store.pool.connection() as conn:
        title, authors = conn.execute(
            "SELECT title, authors FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
        n = conn.execute(
            "SELECT count(*) FROM paper_authors WHERE ref_id = %s", (ref_id,)
        ).fetchone()[0]
    return title, authors, n


def _stub(store, slug="krishnan97"):
    stub = store.insert_ref(kind="paper", slug=slug, title=_TITLE)
    store.set_paper_authors(stub.id, _AUTHORS, source="crossref")
    return stub


def _merge(store, survivor, stub):
    with store.tx() as conn:
        merge_duplicate(
            store,
            survivor_ref_id=survivor.id,
            duplicate_ref_id=stub.id,
            source="test",
            reason="test",
            conn=conn,
        )


def test_orphan_stub_fold_fills_blank_title_and_authors(store):
    survivor = store.insert_ref(kind="paper", slug="pdfonly97", title="")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, 0, 'paragraph', %s)",
            (survivor.id, "Graphitic cones nucleation of curved carbon surfaces."),
        )
        conn.commit()
    stub = _stub(store)
    with store.pool.connection() as conn:
        merged = _reconcile_orphan_stub(
            store, survivor_ref_id=survivor.id, file_stem="krishnan97", conn=conn
        )
        conn.commit()
    assert merged == stub.id
    title, authors, n = _state(store, survivor.id)
    assert title == _TITLE
    assert n == 2 and len(authors) == 2


def test_merge_duplicate_never_overwrites_existing_fields(store):
    survivor = store.insert_ref(kind="paper", slug="own97", title="Own title")
    store.set_paper_authors(
        survivor.id, [{"given": "Z.", "family": "Own"}], source="human"
    )
    _merge(store, survivor, _stub(store))
    title, authors, n = _state(store, survivor.id)
    assert title == "Own title"
    assert n == 1 and authors[0]["family"] == "Own"


def test_merge_duplicate_fills_blank_title_and_authors(store):
    survivor = store.insert_ref(kind="paper", slug="blank97", title="")
    _merge(store, survivor, _stub(store))
    title, _authors, n = _state(store, survivor.id)
    assert title == _TITLE and n == 2


def test_merge_duplicate_skips_cite_key_title(store):
    survivor = store.insert_ref(kind="paper", slug="blank98", title="")
    stub = store.insert_ref(kind="paper", slug="krishnan98", title="krishnan98")
    _merge(store, survivor, stub)
    assert _state(store, survivor.id)[0] == ""


def test_merge_duplicate_copies_author_rows_verbatim(store):
    survivor = store.insert_ref(kind="paper", slug="blank99", title="")
    stub = _stub(store, "krishnan99")
    with store.tx() as conn:
        conn.execute(
            "UPDATE paper_authors SET source = 'orcid', "
            "verified_at = '2026-01-02T00:00:00Z' WHERE ref_id = %s AND position = 1",
            (stub.id,),
        )
    _merge(store, survivor, stub)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT position, source, verified_at IS NOT NULL FROM paper_authors "
            "WHERE ref_id = %s ORDER BY position",
            (survivor.id,),
        ).fetchall()
    assert rows == [(1, "orcid", True), (2, "crossref", False)]

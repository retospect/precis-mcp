"""``has_body_sql`` — the shared "has extracted text" predicate (gr453860)."""

from __future__ import annotations

from precis.store import Store
from precis.store._body_predicate import has_body_sql


def test_fragment_shape_is_parameter_safe() -> None:
    frag = has_body_sql("r")
    assert frag.startswith("EXISTS (SELECT 1 FROM chunks hb ")
    assert "hb.ref_id = r.ref_id" in frag and "hb.ord >= 0" in frag
    assert "%" not in frag and "?" not in frag
    assert "x.ref_id = p.ref_id" in has_body_sql("p", sub_alias="x")


def test_cards_do_not_count_as_body(store: Store) -> None:
    """A stub's ``ord < 0`` card is metadata; only an ``ord >= 0`` row is body."""
    card_only = store.insert_ref(kind="paper", slug="hb-card", title="Card only").id
    bodied = store.insert_ref(kind="paper", slug="hb-body", title="With body").id
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) VALUES "
            "(%s, -1, 'card_combined', 'Card only'), "
            "(%s, -1, 'card_combined', 'With body'), "
            "(%s, 0, 'paragraph', 'A paragraph.')",
            (card_only, bodied, bodied),
        )
    sql = (
        f"SELECT r.ref_id FROM refs r WHERE {has_body_sql('r')} AND r.ref_id = ANY(%s)"
    )
    with store.pool.connection() as conn:
        rows = conn.execute(sql, ([card_only, bodied],)).fetchall()
    assert [int(r[0]) for r in rows] == [bodied]


def _bodied(store: Store, slug: str, tag: str | None) -> int:
    rid = store.insert_ref(kind="paper", slug=slug, title=slug).id
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, 0, 'paragraph', 'A paragraph.')",
            (rid,),
        )
    if tag:
        from precis.store.types import Tag

        store.add_tag(rid, Tag.open(tag), set_by="system")
    return rid


def test_unusable_body_tags_make_a_bodied_ref_count_as_bodiless(store: Store) -> None:
    """body:partial / body:needs_ocr are read as "no usable body" by the
    gates (count_usable_chunks / usable_body_ref_ids) while the raw chunk
    checks are unchanged; an unrelated tag does not matter."""
    plain = _bodied(store, "ub-plain", None)
    other = _bodied(store, "ub-other", "topic:x")
    partial = _bodied(store, "ub-partial", "body:partial")
    ocr = _bodied(store, "ub-ocr", "body:needs_ocr")
    ids = [plain, other, partial, ocr]
    assert store.chunks.usable_body_ref_ids(ids) == {plain, other}
    assert store.chunks.ref_ids_with_chunks(ids) == set(ids)
    assert store.chunks.count_usable_chunks(partial) == 0
    assert store.chunks.count_usable_chunks(plain) == 1
    assert store.chunks.count_chunks(partial) == 1


def test_status_counts_both_tags(store: Store) -> None:
    from precis_web.routes.status import _paper_summary

    _bodied(store, "st-plain", None)
    _bodied(store, "st-p1", "body:partial")
    _bodied(store, "st-p2", "body:partial")
    _bodied(store, "st-o1", "body:needs_ocr")
    s = _paper_summary(store)
    assert (s["body_partial"], s["body_needs_ocr"]) == (2, 1)

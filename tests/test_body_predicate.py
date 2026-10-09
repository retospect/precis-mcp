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

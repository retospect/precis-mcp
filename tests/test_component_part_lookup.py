"""``component_find_by_part`` — the ``promote_to`` matching rule's store
half (bom-tables §2): ``(supplier, supplier_part_number)`` over
``meta.supplier_links`` with vendor-alias normalisation, then the
``(mpn, manufacturer)`` fallback. One hit / zero / ambiguous [ids].
"""

from __future__ import annotations

from typing import Any

_LINK = {
    "supplier": "jlcpcb",
    "supplier_part_number": "C25804",
    "manufacturer": "Samsung",
    "mpn": "CL05B104KO5NNNC",
    "product_url": "https://example.invalid/p",
    "datasheet_url": "https://example.invalid/d.pdf",
    "match_confidence": "confirmed_supplier_part_number",
    "first_linked_at": "2026-10-26T00:00:00+00:00",
}


def _component(
    store: Any, slug: str, *, link: dict[str, Any] | None = None, **meta: Any
) -> Any:
    ref, _ = store.component_entity_upsert(
        slug=slug, title=slug, meta_patch=meta or {"category": "resistor"}
    )
    if link is not None:
        store.component_supplier_link(ref.id, link)
    return ref


def test_one_hit_by_supplier_part_number(store: Any) -> None:
    ref = _component(store, "bom2a-one", link=dict(_LINK))
    assert store.component_find_by_part(
        supplier="jlcpcb", supplier_part_number="C25804"
    ) == [ref.id]


def test_zero_when_nothing_matches(store: Any) -> None:
    _component(store, "bom2a-zero", link=dict(_LINK))
    assert (
        store.component_find_by_part(
            supplier="jlcpcb", supplier_part_number="C99999999"
        )
        == []
    )
    assert store.component_find_by_part() == []


def test_two_hits_is_ambiguous_not_first_wins(store: Any) -> None:
    a = _component(store, "bom2a-amb-a", link=dict(_LINK))
    b = _component(store, "bom2a-amb-b", link=dict(_LINK))
    assert store.component_find_by_part(
        supplier="jlcpcb", supplier_part_number="C25804"
    ) == sorted([a.id, b.id])


def test_case_insensitive_on_both_halves(store: Any) -> None:
    ref = _component(store, "bom2a-case", link=dict(_LINK))
    assert store.component_find_by_part(
        supplier="JLCPCB", supplier_part_number="c25804"
    ) == [ref.id]


def test_vendor_alias_normalisation(store: Any) -> None:
    """``lcsc``/``LCSC.com`` resolve to the stored ``jlcpcb`` supplier."""
    ref = _component(store, "bom2a-alias", link=dict(_LINK))
    for typed in ("lcsc", "LCSC.com", "JLCPCB"):
        assert store.component_find_by_part(
            supplier=typed, supplier_part_number="C25804"
        ) == [ref.id]


def test_mpn_manufacturer_fallback(store: Any) -> None:
    ref = _component(
        store,
        "bom2a-mpn",
        mpn="CL05B104KO5NNNC",
        manufacturer="Samsung",
    )
    assert store.component_find_by_part(
        mpn="cl05b104ko5nnnc", manufacturer="samsung"
    ) == [ref.id]


def test_mpn_alone_is_not_a_fallback(store: Any) -> None:
    """The rule needs the pair — an MPN alone over-matches."""
    _component(store, "bom2a-mpnonly", mpn="X1", manufacturer="Maker")
    assert store.component_find_by_part(mpn="x1") == []
    # Same MPN, different maker → only the manufacturer's row hits.
    _component(store, "bom2a-mpnonly2", mpn="X1", manufacturer="Other")
    assert store.component_find_by_part(mpn="X1", manufacturer="Other") == [
        store.get_ref(kind="component", id="bom2a-mpnonly2").id
    ]


def test_link_match_wins_over_the_fallback(store: Any) -> None:
    """The mpn fallback fires only when the link match found nothing —
    a component matching both must not turn a unique hit ambiguous."""
    linked = _component(store, "bom2a-linked", link=dict(_LINK), mpn="CL05B104KO5NNNC")
    _component(store, "bom2a-mpnonly", mpn="cl05b104ko5nnnc", manufacturer="Samsung")
    assert store.component_find_by_part(
        supplier="jlcpcb",
        supplier_part_number="C25804",
        mpn="CL05B104KO5NNNC",
        manufacturer="Samsung",
    ) == [linked.id]


def test_a_non_array_supplier_links_is_not_an_error(store: Any) -> None:
    """A hand-edited ``supplier_links: {}`` or ``null`` reads as no links,
    not a jsonb_array_elements error."""
    _component(store, "bom2a-dict", supplier_links={"mouser": "SKU1"})
    _component(store, "bom2a-null", supplier_links=None)
    assert (
        store.component_find_by_part(supplier="jlcpcb", supplier_part_number="C25804")
        == []
    )


def test_retired_components_do_not_match(store: Any) -> None:
    ref = _component(store, "bom2a-ret", link=dict(_LINK))
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET retired_at=now() WHERE ref_id=%s", (ref.id,))
    assert (
        store.component_find_by_part(supplier="jlcpcb", supplier_part_number="C25804")
        == []
    )

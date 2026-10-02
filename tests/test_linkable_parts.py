"""Linkable parts, slice 1 (docs/backlog/linkable-parts.md).

A catalog part becomes a lazy ``part`` ref on its first add-mode link:
``Store.ensure_part_ref`` mints it, the two generic link doors call it,
``get(kind='part')`` shows the ref's ring, and a datasheet's
``part_lcsc`` dual-writes a ``datasheet-of`` edge.
"""

from __future__ import annotations

import threading

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.component import ComponentHandler
from precis.handlers.datasheet import DatasheetHandler
from precis.handlers.memory import MemoryHandler
from precis.handlers.part import PartHandler
from precis.pcb import catalog
from precis.utils import handle_registry


def _catalog_rows(*lcsc: int) -> list[dict]:
    rows = [
        catalog.normalize_jlcparts_row(
            {
                "lcsc": n,
                "manufacturer": "Samsung",
                "mfr_part": f"CL05B{n}",
                "description": "100nF 16V X7R 0402 capacitor",
                "basic": 1,
                "stock": 1000,
                "package": "0402",
            }
        )
        for n in lcsc
    ]
    return [r for r in rows if r]


@pytest.fixture
def seeded(store):
    store.parts_import(_catalog_rows(25804, 1525))
    return store


def _part_ref_count(store, lcsc: str) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs r JOIN ref_identifiers ri "
            "  ON ri.ref_id = r.ref_id AND ri.id_kind = 'lcsc' "
            "WHERE r.kind = 'part' AND ri.id_value = %s",
            (lcsc,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _memory(store) -> int:
    return store.insert_ref(kind="memory", slug=None, title="a thought").id


# ── ensure_part_ref ──────────────────────────────────────────────────


def test_ensure_part_ref_mints_once_and_reuses(seeded):
    first = seeded.ensure_part_ref("c25804")
    assert seeded.ensure_part_ref("C25804") == first
    ref = seeded.get_ref(kind="part", id="C25804")
    assert ref is not None and ref.id == first
    assert ref.title.startswith("CL05B25804 — 100nF")
    assert _part_ref_count(seeded, "C25804") == 1


def test_ensure_part_ref_refuses_a_c_number_not_in_the_catalog(seeded):
    with pytest.raises(NotFound):
        seeded.ensure_part_ref("C99999999")
    assert _part_ref_count(seeded, "C99999999") == 0


def test_concurrent_first_mints_converge_on_one_ref(seeded):
    ids: list[int] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def mint() -> None:
        try:
            barrier.wait()
            ids.append(seeded.ensure_part_ref("C25804"))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=mint) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(set(ids)) == 1
    assert _part_ref_count(seeded, "C25804") == 1


def test_a_retired_part_ref_is_replaced_by_a_fresh_mint(seeded):
    old = seeded.ensure_part_ref("C25804")
    seeded.retire_ref(old)
    new = seeded.ensure_part_ref("C25804")
    assert new != old
    assert seeded.part_ref_id("C25804") == new


# ── link doors ───────────────────────────────────────────────────────


def test_memory_link_to_a_part_mints_it_and_a_second_link_reuses_it(seeded):
    memory = MemoryHandler(hub=Hub(store=seeded))
    a, b = _memory(seeded), _memory(seeded)
    memory.link(id=a, target="part:C25804", rel="related-to")
    memory.link(id=b, target="part:C25804", rel="related-to")
    assert _part_ref_count(seeded, "C25804") == 1
    part_id = seeded.part_ref_id("C25804")
    assert {lk.src_ref_id for lk in seeded.links_for(part_id, direction="in")} == {
        a,
        b,
    }


def test_link_to_an_uncatalogued_part_mints_nothing(seeded):
    memory = MemoryHandler(hub=Hub(store=seeded))
    with pytest.raises(NotFound):
        memory.link(id=_memory(seeded), target="part:C99999999", rel="related-to")
    assert _part_ref_count(seeded, "C99999999") == 0


def test_unlink_and_a_bad_rel_never_mint(seeded):
    memory = MemoryHandler(hub=Hub(store=seeded))
    m = _memory(seeded)
    with pytest.raises(NotFound):
        memory.link(id=m, target="part:C25804", mode="remove")
    with pytest.raises(BadInput):
        memory.link(id=m, target="part:C25804", rel="no-such-relation")
    assert _part_ref_count(seeded, "C25804") == 0


def test_component_realized_by_part_shows_in_both_rings(seeded):
    seeded.insert_ref(kind="component", slug="decap-100n", title="decoupling cap")
    ComponentHandler(hub=Hub(store=seeded)).link(
        id="decap-100n", rel="realized-by", target="part:C25804"
    )
    part_body = PartHandler(hub=Hub(store=seeded)).get(id="C25804").body
    assert "decap-100n" in part_body or "decoupling cap" in part_body
    comp = seeded.get_ref(kind="component", id="decap-100n")
    out = seeded.links_for(comp.id, direction="out", relation="realized-by")
    assert [lk.dst_ref_id for lk in out] == [seeded.part_ref_id("C25804")]


def test_component_link_keeps_made_of_and_contains_on_put(seeded):
    seeded.insert_ref(kind="component", slug="bracket", title="bracket")
    with pytest.raises(BadInput, match="put"):
        ComponentHandler(hub=Hub(store=seeded)).link(
            id="bracket", rel="contains", target="component:bracket"
        )


# ── get + handle ─────────────────────────────────────────────────────


def test_get_part_shows_catalog_row_then_ring_once_linked(seeded):
    part = PartHandler(hub=Hub(store=seeded))
    before = part.get(id="C25804").body
    assert "CL05B25804" in before and "ref:" not in before
    MemoryHandler(hub=Hub(store=seeded)).link(id=_memory(seeded), target="part:C25804")
    after = part.get(id="C25804").body
    handle = handle_registry.format_handle("part", seeded.part_ref_id("C25804"))
    assert "CL05B25804" in after and f"ref: {handle}" in after
    assert "a thought" in after


def test_get_part_after_it_leaves_the_catalog_shows_the_ref(seeded):
    MemoryHandler(hub=Hub(store=seeded)).link(id=_memory(seeded), target="part:C25804")
    seeded.parts_bulk_replace(_catalog_rows(1525), force=True)  # swap drops C25804
    body = PartHandler(hub=Hub(store=seeded)).get(id="C25804").body
    assert "no longer in the catalog" in body
    assert "a thought" in body
    with pytest.raises(NotFound):
        PartHandler(hub=Hub(store=seeded)).get(id="C99999999")


def test_pn_handle_resolves_to_the_part_ref(seeded):
    ref_id = seeded.ensure_part_ref("C25804")
    handle = handle_registry.format_handle("part", ref_id)
    assert handle == f"pn{ref_id}"
    resolved = seeded.resolve_handle(handle)
    assert resolved is not None
    assert (resolved.kind, resolved.ref_id, resolved.public_id) == (
        "part",
        ref_id,
        "C25804",
    )


# ── datasheet dual-write ─────────────────────────────────────────────


def _datasheet_of(store, ds_id: int) -> list[int]:
    return [
        lk.dst_ref_id
        for lk in store.links_for(ds_id, direction="out", relation="datasheet-of")
    ]


def test_datasheet_part_lcsc_writes_meta_and_the_edge(seeded):
    ds = seeded.insert_ref(kind="datasheet", slug="cl05b-ds", title="CL05B datasheet")
    handler = DatasheetHandler(hub=Hub(store=seeded))
    handler.edit(id="cl05b-ds", part_lcsc="c25804")
    ref = seeded.get_ref(kind="datasheet", id=ds.id)
    assert ref.meta["part_lcsc"] == "C25804"
    assert _datasheet_of(seeded, ds.id) == [seeded.part_ref_id("C25804")]

    handler.edit(id="cl05b-ds", part_lcsc="C1525")
    assert _datasheet_of(seeded, ds.id) == [seeded.part_ref_id("C1525")]

    handler.edit(id="cl05b-ds", part_lcsc="")
    assert _datasheet_of(seeded, ds.id) == []
    assert seeded.get_ref(kind="datasheet", id=ds.id).meta["part_lcsc"] == ""


def test_datasheet_part_lcsc_outside_the_catalog_keeps_meta_only(seeded):
    ds = seeded.insert_ref(kind="datasheet", slug="odd-ds", title="odd datasheet")
    resp = DatasheetHandler(hub=Hub(store=seeded)).edit(
        id="odd-ds", part_lcsc="C99999999"
    )
    assert "not in the catalog" in resp.body
    assert seeded.get_ref(kind="datasheet", id=ds.id).meta["part_lcsc"] == "C99999999"
    assert _datasheet_of(seeded, ds.id) == []

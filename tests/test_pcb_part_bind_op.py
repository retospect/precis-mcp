"""op='part' — bind a SELECTED catalog C-number to an existing component.

The create-or-extend ruling makes a re-put never edit a stored component's
identity (``_reput_ignored_block``: "re-authoring anything else means a new
design — delete + put"), so a board authored with generic parts ("R 100k
0402 1%") had no MCP-facing way to become real parts after selection. This
op is that edit: it writes ``part_lcsc`` and re-stamps
footprint/height/courtyard from the catalog snapshot, under the judge, in
single or bulk (``moves=[...]``) form. Driven by the taz-leaf part-binding
pass (2026-10-30): 16 generics needed real parts after selection.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.pcb import PcbHandler
from precis.pcb import catalog as pcb_catalog
from tests.test_pcb_footprint_op import _fake_footprint


def _bind_footprint() -> dict:
    """The shared fake, but with pin names the generic design declares
    (pads_for_ir maps pads to pins through pin_map — 'A'/'B' would orphan
    every pad and fall back to synthesized geometry)."""
    fp = _fake_footprint()
    fp["pin_map"] = {
        "1": {"name": "1", "tags": []},
        "2": {"name": "2", "tags": []},
    }
    return fp


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _seed_part(store, lcsc: str, *, package: str = "0402") -> None:
    store.parts_import(
        [
            pcb_catalog.normalize_jlcparts_row(
                {
                    "lcsc": int(lcsc[1:]),
                    "description": f"test part {package}",
                    "basic": 1,
                    "stock": 50000,
                    "package": package,
                    "extra": {"Height": "0.35mm"},
                }
            )
        ]
    )


def _generic_design() -> dict:
    return {
        "components": [
            {
                "refdes": "R1",
                "label": "R 100k 0402 1%",
                "x": 0.0,
                "y": 0.0,
                "pins": [{"name": "1"}, {"name": "2"}],
            },
            {
                "refdes": "R2",
                "label": "R 10k 0402 1%",
                "x": 8.0,
                "y": 0.0,
                "pins": [{"name": "1"}, {"name": "2"}],
            },
        ],
        "nets": [{"name": "N1"}, {"name": "N2"}],
        "connections": [
            {"net": "N1", "refdes": "R1", "pin": "1"},
            {"net": "N2", "refdes": "R2", "pin": "1"},
        ],
    }


def _component_row(store, slug: str, refdes: str) -> dict:
    ref = store.get_ref(kind="pcb", id=slug)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT c.part_lcsc, c.footprint, c.height_mm, c.courtyard "
            "FROM pcb_components c "
            "JOIN pcb_instances i ON i.component_id = c.component_id "
            "WHERE i.ref_id = %s AND i.refdes = %s "
            "  AND i.retired_at IS NULL AND c.retired_at IS NULL",
            (ref.id, refdes),
        ).fetchone()
    assert row is not None
    return {
        "part_lcsc": row[0],
        "footprint": row[1],
        "height_mm": row[2],
        "courtyard": row[3],
    }


def test_op_part_binds_and_stamps_from_catalog(pcb, store):
    _seed_part(store, "C9001")
    store.part_footprint_put("C9001", _bind_footprint())
    pcb.put(id="bind1", args=_generic_design())

    resp = pcb.put(id="bind1", args={"op": "part", "refdes": "R1", "part": "C9001"})
    assert "R1" in resp.body and "C9001" in resp.body
    row = _component_row(store, "bind1", "R1")
    assert row["part_lcsc"] == "C9001"
    assert row["footprint"] == "0402"
    assert row["height_mm"] is not None
    assert row["courtyard"] == {"bbox": [-0.25, -0.25, 1.25, 0.25]}
    # the untouched neighbour stays generic
    assert _component_row(store, "bind1", "R2")["part_lcsc"] is None


def test_op_part_bulk_moves_form(pcb, store):
    _seed_part(store, "C9001")
    _seed_part(store, "C9002")
    pcb.put(id="bind2", args=_generic_design())
    resp = pcb.put(
        id="bind2",
        args={
            "op": "part",
            "moves": [
                {"refdes": "R1", "part": "C9001"},
                {"refdes": "R2", "part": "C9002"},
            ],
        },
    )
    assert "2 bound" in resp.body
    assert _component_row(store, "bind2", "R1")["part_lcsc"] == "C9001"
    assert _component_row(store, "bind2", "R2")["part_lcsc"] == "C9002"


def test_op_part_rebind_replaces_the_stamp(pcb, store):
    _seed_part(store, "C9001", package="0402")
    _seed_part(store, "C9002", package="0603")
    pcb.put(id="bind3", args=_generic_design())
    pcb.put(id="bind3", args={"op": "part", "refdes": "R1", "part": "C9001"})
    resp = pcb.put(id="bind3", args={"op": "part", "refdes": "R1", "part": "C9002"})
    assert "prev C9001" in resp.body
    row = _component_row(store, "bind3", "R1")
    assert row["part_lcsc"] == "C9002"
    assert row["footprint"] == "0603"


def test_op_part_unknown_refdes_notfound(pcb, store):
    _seed_part(store, "C9001")
    pcb.put(id="bind4", args=_generic_design())
    with pytest.raises(NotFound):
        pcb.put(id="bind4", args={"op": "part", "refdes": "ZZ9", "part": "C9001"})


def test_op_part_rejects_non_c_number(pcb, store):
    pcb.put(id="bind5", args=_generic_design())
    with pytest.raises(BadInput):
        pcb.put(
            id="bind5",
            args={"op": "part", "refdes": "R1", "part": "100k 0402"},
        )


def test_op_part_catalog_miss_names_get_first(pcb, store):
    pcb.put(id="bind6", args=_generic_design())
    with pytest.raises(BadInput) as exc:
        pcb.put(
            id="bind6",
            args={"op": "part", "refdes": "R1", "part": "C999999"},
        )
    assert "get(kind='part'" in str(exc.value.next or exc.value)


def test_op_part_judge_refuses_and_rolls_back_on_new_violation(pcb, store):
    """A bound footprint whose courtyard overlaps its neighbour's is the
    classic bind risk (the real part is bigger than the synthesized
    bound) — a new courtyard-overlap ERROR; the judge refuses and the
    bind rolls back."""
    _seed_part(store, "C9001")
    _seed_part(store, "C9002")
    # pads drive the checked courtyard (union of pad outlines, offset) —
    # wide pads on R1 reach across the 8 mm gap and overlap R2's.
    wide = _bind_footprint()
    for pad in wide["pads"]:
        pad["w"] = 16.0
    store.part_footprint_put("C9001", wide)
    store.part_footprint_put("C9002", _bind_footprint())
    pcb.put(id="bind7", args=_generic_design())
    pcb.put(id="bind7", args={"op": "part", "refdes": "R2", "part": "C9002"})

    with pytest.raises(BadInput):
        pcb.put(id="bind7", args={"op": "part", "refdes": "R1", "part": "C9001"})
    assert _component_row(store, "bind7", "R1")["part_lcsc"] is None
    # the earlier legal bind stands
    assert _component_row(store, "bind7", "R2")["part_lcsc"] == "C9002"

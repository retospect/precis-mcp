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
from precis.pcb import export as pcb_export
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


def _seed_part(
    store, lcsc: str, *, package: str = "0402", params: dict | None = None
) -> None:
    store.parts_import(
        [
            pcb_catalog.normalize_jlcparts_row(
                {
                    "lcsc": int(lcsc[1:]),
                    "description": f"test part {package}",
                    "basic": 1,
                    "stock": 50000,
                    "package": package,
                    "extra": {"Height": "0.35mm", **(params or {})},
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
            "SELECT c.part_lcsc, c.footprint, c.height_mm, c.courtyard, "
            "       c.label, c.meta "
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
        "label": row[4],
        "meta": dict(row[5] or {}),
    }


def _valued_design(label: str = "R 4.42M 0402 1%", value: str = "4.42M") -> dict:
    """A component carrying a stamped ``meta.part.value`` (the epro-import
    shape) and a generated label embedding it — the gr553735 taz-leaf
    repro."""
    return {
        "components": [
            {
                "refdes": "R1",
                "label": label,
                "part_meta": {"value": value},
                "x": 0.0,
                "y": 0.0,
                "pins": [{"name": "1"}, {"name": "2"}],
            }
        ],
        "nets": [{"name": "N1"}],
        "connections": [{"net": "N1", "refdes": "R1", "pin": "1"}],
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


# ── gr553735: value + label restamp ──────────────────────────────────


def test_op_part_restamps_auto_label_and_value(pcb, store):
    """A re-bind to a different-valued part restamps meta.part.value from
    the catalog row and regenerates the value-embedded label in the same
    format — and footprint stamping still applies."""
    _seed_part(store, "C9001", params={"Resistance": "2.7MΩ"})
    store.part_footprint_put("C9001", _bind_footprint())
    pcb.put(id="v1", args=_valued_design())

    resp = pcb.put(id="v1", args={"op": "part", "refdes": "R1", "part": "C9001"})
    row = _component_row(store, "v1", "R1")
    assert row["part_lcsc"] == "C9001"
    assert row["footprint"] == "0402"
    assert row["courtyard"] == {"bbox": [-0.25, -0.25, 1.25, 0.25]}
    assert row["meta"]["part"]["value"] == "2.7MΩ"
    assert row["label"] == "R 2.7MΩ 0402 1%"
    # the BOM reads the new value through the label
    ref = store.get_ref(kind="pcb", id="v1")
    model = pcb_export.export_model(store.pcb_load(ref.id), store.pcb_graph(ref.id))
    assert "R 2.7MΩ 0402 1%" in pcb_export.bom_csv(model)
    assert "4.42M" not in pcb_export.bom_csv(model)
    assert "2.7MΩ" in resp.body


def test_op_part_explicit_label_and_value_win(pcb, store):
    _seed_part(store, "C9001", params={"Resistance": "2.7MΩ"})
    pcb.put(id="v2", args=_valued_design())

    pcb.put(
        id="v2",
        args={
            "op": "part",
            "refdes": "R1",
            "part": "C9001",
            "label": "R 2.7M",
            "value": "2.7M",
        },
    )
    row = _component_row(store, "v2", "R1")
    assert row["label"] == "R 2.7M"
    assert row["meta"]["part"]["value"] == "2.7M"


def test_op_part_hand_written_label_kept_with_warning(pcb, store):
    """A label that doesn't embed the old value reads as hand-written:
    kept, and the reply warns it no longer names the bound value."""
    _seed_part(store, "C9001", params={"Resistance": "2.7MΩ"})
    pcb.put(id="v3", args=_valued_design(label="1/16W pull-up"))

    resp = pcb.put(id="v3", args={"op": "part", "refdes": "R1", "part": "C9001"})
    row = _component_row(store, "v3", "R1")
    assert row["label"] == "1/16W pull-up"
    assert row["meta"]["part"]["value"] == "2.7MΩ"
    assert "1/16W pull-up" in resp.body
    assert "2.7MΩ" in resp.body


def test_op_part_moves_form_takes_label_value_per_item(pcb, store):
    _seed_part(store, "C9001", params={"Resistance": "2.7MΩ"})
    pcb.put(id="v4", args=_valued_design())
    pcb.put(
        id="v4",
        args={
            "op": "part",
            "moves": [
                {"refdes": "R1", "part": "C9001", "label": "R 2.7MΩ", "value": "2.7MΩ"}
            ],
        },
    )
    row = _component_row(store, "v4", "R1")
    assert row["label"] == "R 2.7MΩ"
    assert row["meta"]["part"]["value"] == "2.7MΩ"


def test_reput_ignored_label_and_value_name_op_part(pcb, store):
    """The re-put 'ignored' block names op='part' with label=/value= as
    the edit for those fields instead of insert-only."""
    pcb.put(id="v5", args=_generic_design())
    resp = pcb.put(
        id="v5",
        args={
            "components": [{"refdes": "R1", "label": "R 4.7k 0402 1%", "value": "4.7k"}]
        },
    )
    assert "op='part'" in resp.body
    assert "insert-only" not in resp.body.split("R1:")[-1].split("\n")[0]


def test_op_part_stamps_value_on_component_without_part_meta(pcb, store):
    """A generic component carries no ``meta.part`` at all — the value
    stamp must CREATE it (``jsonb_set``'s last-key-only create would
    silently store nothing while the reply claimed a value)."""
    _seed_part(store, "C9001", params={"Resistance": "2.7MΩ"})
    pcb.put(id="v6", args=_generic_design())

    resp = pcb.put(id="v6", args={"op": "part", "refdes": "R1", "part": "C9001"})
    row = _component_row(store, "v6", "R1")
    assert row["meta"]["part"]["value"] == "2.7MΩ"
    # the generated label has no old value to swap — kept, warned
    assert row["label"] == "R 100k 0402 1%"
    assert "does not name" in resp.body


def test_op_part_value_swap_is_token_bounded(pcb, store):
    """old '1' must not bite inside '100k'/'1%', and old '4.7' must not
    bite inside '4.7k' — no whole token equal to the old value means a
    hand-written label: kept with the mismatch warning."""
    _seed_part(store, "C9001", params={"Resistance": "2.7MΩ"})
    pcb.put(id="v7", args=_valued_design(label="R 100k 0402 1%", value="1"))
    pcb.put(id="v8", args=_valued_design(label="R 4.7k 0402 1%", value="4.7"))

    resp = pcb.put(id="v7", args={"op": "part", "refdes": "R1", "part": "C9001"})
    row = _component_row(store, "v7", "R1")
    assert row["label"] == "R 100k 0402 1%"
    assert row["meta"]["part"]["value"] == "2.7MΩ"
    assert "hand-written" in resp.body

    resp = pcb.put(id="v8", args={"op": "part", "refdes": "R1", "part": "C9001"})
    row = _component_row(store, "v8", "R1")
    assert row["label"] == "R 4.7k 0402 1%"
    assert row["meta"]["part"]["value"] == "2.7MΩ"
    assert "hand-written" in resp.body


def test_op_part_valueless_catalog_row_warns_stale_label(pcb, store):
    """An IC-class row (no value attribute under _CATALOG_VALUE_KEYS)
    keeps the stored value/label and warns the label may now be stale —
    value=/label= is the way to set it."""
    _seed_part(store, "C9001")  # Height only — no recognised value key
    pcb.put(id="v9", args=_valued_design())

    resp = pcb.put(id="v9", args={"op": "part", "refdes": "R1", "part": "C9001"})
    row = _component_row(store, "v9", "R1")
    assert row["part_lcsc"] == "C9001"
    assert row["meta"]["part"]["value"] == "4.42M"
    assert row["label"] == "R 4.42M 0402 1%"
    assert "stale" in resp.body
    assert "label=" in resp.body and "value=" in resp.body


def test_op_part_refuses_empty_label_and_value(pcb, store):
    _seed_part(store, "C9001", params={"Resistance": "2.7MΩ"})
    pcb.put(id="v10", args=_valued_design())
    with pytest.raises(BadInput):
        pcb.put(
            id="v10",
            args={"op": "part", "refdes": "R1", "part": "C9001", "label": ""},
        )
    with pytest.raises(BadInput):
        pcb.put(
            id="v10",
            args={"op": "part", "refdes": "R1", "part": "C9001", "value": "  "},
        )
    with pytest.raises(BadInput):
        pcb.put(
            id="v10",
            args={
                "op": "part",
                "moves": [{"refdes": "R1", "part": "C9001", "value": ""}],
            },
        )
    # refused before the bind — nothing was stamped
    assert _component_row(store, "v10", "R1")["part_lcsc"] is None

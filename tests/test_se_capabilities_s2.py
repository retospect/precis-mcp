"""design-tree S2 capability promotion (gr516958) — the ``fdm/*``
process row and the new material rows the verifier-confirmed promote
records landed as, the researched-but-unpromoted null gap-fields, and
``capability()``/``resolve()`` over the new rows.

The row-building envelope (which side of the published bounds is
``physical`` vs ``house``) is documented in ``se_capabilities.json``'s
own ``_note`` and in the S2 SPEC; these tests pin the landed shape, not
the aggregation rule.
"""

from __future__ import annotations

import pytest

from precis.blocktree.types import OpError
from precis_se import capabilities as se_caps
from precis_se.ops import SeBlock, SeTree, apply_ops

#: The process/nozzle-class figures S2 promoted onto ``fdm/*`` — the
#: fields manifest ``table_ref: fdm/*.<field>`` rules resolve against.
_STAR_PROMOTED = {
    "min_wall": (0.45, 0.9),  # house = 2 perimeters (r2 resolution)
    "min_clearance": (0.3, 0.3),
    "layer_height": (0.32, 0.2),  # house = the 0.2 build default (r2)
    "line_width": (0.45, 0.45),
    "max_overhang": (75, 60),
}

#: Researched for ``fdm/*``, nothing met the promote bar — null
#: gap-fields with a source saying so, never a borrowed number.
_STAR_GAP_FIELDS = (
    "hole_diameter_compensation",
    "max_bridge",
    "max_build",
    "min_feature",
    "min_hole",
)

_NEW_MATERIAL_MODES = ("fdm/pa", "fdm/pc", "fdm/pa-cf", "fdm/pc-cf")


def _row(mode: str) -> dict:
    return se_caps._rows()[mode]


def test_star_row_exists_with_promoted_process_fields() -> None:
    row = _row("fdm/*")
    for name, (physical, house) in _STAR_PROMOTED.items():
        raw = row["fields"][name]
        assert raw["physical"] == pytest.approx(physical), name
        assert raw["house"] == pytest.approx(house), name
        assert raw["source"], name


def test_star_row_gap_fields_are_explicit_nulls_with_a_source() -> None:
    fields = _row("fdm/*")["fields"]
    for name in _STAR_GAP_FIELDS:
        raw = fields[name]
        assert raw["physical"] is None, name
        assert raw["house"] is None, name
        assert raw["source"], name


@pytest.mark.parametrize("mode", _NEW_MATERIAL_MODES)
def test_new_material_rows_carry_the_process_set(mode: str) -> None:
    # Per the file's convention a new material row copies the fdm/*
    # process figures in, then overrides with its own per-material
    # records — so every promoted process field is present with a value.
    fields = _row(mode)["fields"]
    for name in _STAR_PROMOTED:
        assert fields[name]["house"] is not None, f"{mode}.{name}"
        assert fields[name]["source"], f"{mode}.{name}"


@pytest.mark.parametrize(
    ("mode", "field"),
    [
        ("fdm/pa", "nozzle_temp_min"),
        ("fdm/pa", "bed_temp_min"),
        ("fdm/pa", "tensile_xy"),
        ("fdm/pa", "hdt_0.45mpa"),
        ("fdm/pc", "nozzle_temp_min"),
        ("fdm/pc", "tensile_xy"),
        ("fdm/pa-cf", "tensile_z"),
        ("fdm/pc-cf", "hdt_0.45mpa"),
    ],
)
def test_promoted_per_material_fields_have_figures(mode: str, field: str) -> None:
    raw = _row(mode)["fields"][field]
    assert raw["house"] is not None
    assert raw["physical"] is not None
    assert raw["source"]


@pytest.mark.parametrize(
    ("mode", "field"),
    [("fdm/pa", "enclosure"), ("fdm/pc", "shrinkage")],
)
def test_researched_unpromoted_material_field_is_a_null_gap(
    mode: str, field: str
) -> None:
    # Researched for this material, no desktop-vendor figure met the
    # promote bar — an explicit null, never filled from a sibling.
    raw = _row(mode)["fields"][field]
    assert raw["physical"] is None
    assert raw["house"] is None
    assert raw["confidence"] == "unknown"
    assert "no figure met the promote bar" in raw["source"]


def test_capability_resolves_the_star_row() -> None:
    cap = se_caps.capability("fdm/*", "min_wall")
    assert cap is not None
    assert cap.unit == "mm"
    assert cap.house == pytest.approx(0.9)
    assert cap.physical == pytest.approx(0.45)


def test_capability_star_gap_field_is_none() -> None:
    assert se_caps.capability("fdm/*", "min_feature") is None


def test_resolve_clamps_a_sub_floor_override_on_the_star_row() -> None:
    tree = SeTree()
    block = SeBlock(name="bracket", mode="fdm/*", process_overrides={"min_wall": 0.2})
    tree.blocks[block.name] = block
    resolved = se_caps.resolve(tree, block, "min_wall")
    assert resolved is not None
    assert resolved.tier == "override"
    assert resolved.clamped is True
    assert resolved.value == pytest.approx(0.45)


# ── temperature windows: *_min / *_max fields (r2 split) ─────────────


def test_temperature_fields_are_window_pairs() -> None:
    # The unsplit names are gone; every material row that carried them
    # now answers the *_min/*_max pair.
    for mode in ("fdm/pla", "fdm/petg", "fdm/pc"):
        fields = _row(mode)["fields"]
        for base in ("nozzle_temp", "bed_temp"):
            assert base not in fields
            assert f"{base}_min" in fields
            assert f"{base}_max" in fields


def test_window_pair_house_inside_physical() -> None:
    raw = _row("fdm/pla")["fields"]
    lo, hi = raw["nozzle_temp_min"], raw["nozzle_temp_max"]
    # physical = widest published window, house = the conservative end
    assert lo["physical"] == pytest.approx(190)
    assert lo["house"] == pytest.approx(200)
    assert hi["physical"] == pytest.approx(230)
    assert hi["house"] == pytest.approx(220)


def test_clamp_direction_suffix_form() -> None:
    assert se_caps.clamp_direction("nozzle_temp_min") == "floor"
    assert se_caps.clamp_direction("nozzle_temp_max") == "ceiling"
    assert se_caps.clamp_direction("min_wall") == "floor"
    assert se_caps.clamp_direction("max_overhang") == "ceiling"
    assert se_caps.clamp_direction("layer_height") is None


def test_resolve_clamps_suffix_floor_and_ceiling() -> None:
    tree = SeTree()
    block = SeBlock(
        name="bracket",
        mode="fdm/pla",
        process_overrides={"nozzle_temp_min": 150.0, "nozzle_temp_max": 250.0},
    )
    tree.blocks[block.name] = block
    lo = se_caps.resolve(tree, block, "nozzle_temp_min")
    assert lo is not None and lo.clamped is True
    assert lo.value == pytest.approx(190)
    hi = se_caps.resolve(tree, block, "nozzle_temp_max")
    assert hi is not None and hi.clamped is True
    assert hi.value == pytest.approx(230)


def test_set_process_override_rejects_beyond_suffix_bounds() -> None:
    tree = apply_ops(
        SeTree(),
        [
            {
                "op": "add_block",
                "name": "bracket",
                "envelope": "box:w0.01d0.01h0.01",
            },
            {"op": "set_mode", "block": "bracket", "mode": "fdm/pla"},
        ],
    )
    for field, value, match in (
        ("nozzle_temp_min", 150, "physical floor"),
        ("nozzle_temp_max", 250, "physical ceiling"),
    ):
        with pytest.raises(OpError, match=match):
            apply_ops(
                tree,
                [
                    {
                        "op": "set_process_override",
                        "block": "bracket",
                        "field": field,
                        "value": value,
                    }
                ],
            )


def test_set_process_override_rejects_below_the_star_floor() -> None:
    tree = apply_ops(
        SeTree(),
        [
            {
                "op": "add_block",
                "name": "bracket",
                "envelope": "box:w0.01d0.01h0.01",
            },
            {"op": "set_mode", "block": "bracket", "mode": "fdm/*"},
        ],
    )
    with pytest.raises(OpError, match="physical floor"):
        apply_ops(
            tree,
            [
                {
                    "op": "set_process_override",
                    "block": "bracket",
                    "field": "min_wall",
                    "value": 0.2,
                }
            ],
        )


# ── inverted vendor windows: ceiling wins (r3 ruling) ──────────────


def test_window_pair_house_floor_never_above_ceiling() -> None:
    # DESIGN ruling (gr516958 r3): vendors can disagree so far that the
    # strictest published minimum lands above the strictest published
    # maximum — then both house bounds take the ceiling. Every *_min/*_max
    # pair is a temperature window today.
    for mode, row in se_caps._rows().items():
        fields = row["fields"]
        bases = {n[:-4] for n in fields if n.endswith("_min")}
        bases |= {n[:-4] for n in fields if n.endswith("_max")}
        for base in bases:
            lo, hi = fields.get(f"{base}_min"), fields.get(f"{base}_max")
            if lo is None or hi is None or lo["house"] is None or hi["house"] is None:
                continue
            assert lo["house"] <= hi["house"], f"{mode}.{base}"


def test_inverted_window_house_collapses_to_the_ceiling() -> None:
    fields = _row("fdm/pla")["fields"]
    lo, hi = fields["drying_min"], fields["drying_max"]
    # Bambu/Polymaker's 55 degC floor sat above Prusa's 45 degC ceiling —
    # both house bounds take the ceiling; the source names the vendor
    # that set it; physical keeps the full vendor spread.
    assert lo["house"] == pytest.approx(45)
    assert hi["house"] == pytest.approx(45)
    assert lo["physical"] == pytest.approx(45)
    assert hi["physical"] == pytest.approx(55)
    for raw in (lo, hi):
        assert "vendors disagree; ceiling wins (Prusa)" in raw["source"]


def test_resolve_house_tier_on_a_collapsed_window() -> None:
    tree = SeTree()
    block = SeBlock(name="bracket", mode="fdm/pla")
    tree.blocks[block.name] = block
    lo = se_caps.resolve(tree, block, "drying_min")
    hi = se_caps.resolve(tree, block, "drying_max")
    assert lo is not None and lo.tier == "house"
    assert hi is not None and hi.tier == "house"
    assert lo.value == pytest.approx(45)
    assert hi.value == pytest.approx(45)

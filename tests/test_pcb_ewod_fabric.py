"""pcb-pre-place-route-blocks Slice 2 — ``ewod_pad_array`` emits its
escape fabric (neck track + plaza via) as real copper.

Pure-Python coverage (no DB) of :func:`precis.pcb.generators.expand`'s own
``copper``/``ledger['fabric']`` contract, matching the style
``tests/test_pcb_ewod_generator_geometry.py`` already uses for this
module; store/handler wiring (the DB round-trip through
``pcb_fixed_copper``, idempotent retire-on-change) is covered by
``tests/test_pcb_ewod_generator.py`` (real generator through the real
store) and ``tests/test_pcb_fixed_copper.py`` (the storage seam itself,
via a fake generator). The envelope-refusal test below is the one place
this file goes through the store, since the refusal itself is store-layer
behaviour (:meth:`precis.store._pcb_ops.PcbMixin.
_pcb_fixed_copper_envelope_mismatch`) this module only ever SUPPLIES data
for.

See ``precis.pcb.generators``'s module docstring ("pcb-pre-place-route-
blocks Slice 2") for what this replaces, and
``tests/test_pcb_ewod_generator_drc.py``'s own module docstring for the
KNOWN, documented clearance gap the constant-width track (vs. the former
tapered pad) introduces at default sizing — out of scope here, which is
pure shape/ledger/idempotency coverage, not DRC.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import generators as G


def _copper_by_ctype(
    exp: G.GeneratorExpansion,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tracks = [c for c in exp.copper if c["ctype"] == "track"]
    vias = [c for c in exp.copper if c["ctype"] == "via"]
    return tracks, vias


def _pads_by_pin(exp: G.GeneratorExpansion) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for p in exp.footprints[0]["pads"]:
        out.setdefault(p["pin"], []).append(p)
    return out


# ── 1. copper row shapes ────────────────────────────────────────────────


def test_driven_electrode_emits_one_track_and_one_via_row():
    exp = G.expand("ewod_pad_array", "ARR", {"grid": [3, 3]})
    tracks, vias = _copper_by_ctype(exp)
    assert len(tracks) == 8
    assert len(vias) == 8

    track = next(t for t in tracks if t["net"] == "ARR_R0C0")
    assert track["ctype"] == "track"
    assert track["layer"] == "F.Cu"
    seg = track["geom"]["segments"]
    assert len(seg) == 1
    assert seg[0]["shape"] == "line"
    assert len(seg[0]["start"]) == 2
    assert len(seg[0]["end"]) == 2
    assert track["geom"]["width_mm"] > 0
    assert track["envelope"]["layers"] == 4

    via = next(v for v in vias if v["net"] == "ARR_R0C0")
    assert via["ctype"] == "via"
    assert via["geom"]["span"] == ["F.Cu", "B.Cu"]
    assert via["geom"]["dia_mm"] > via["geom"]["drill_mm"] > 0
    assert via["envelope"] == track["envelope"]  # same call, same floor


def test_electrode_body_stays_the_pins_only_pad():
    """docs/backlog/pcb-pre-place-route-blocks.md Slice 2's core claim:
    the stub/via pad rows are GONE — every pin has exactly one pad (its
    electrode body), driven or not."""
    exp = G.expand("ewod_pad_array", "ARR", {"grid": [3, 3]})
    by_pin = _pads_by_pin(exp)
    assert len(by_pin) == 8
    for pin, pads in by_pin.items():
        assert len(pads) == 1, f"{pin}: {len(pads)} pads, expected exactly 1"
        assert pads[0]["shape"] == "polygon"
        assert not pads[0].get("drill")
    # the component's own pins list is unaffected -- one per electrode,
    # same identity the old 3-pads-per-pin shape already had.
    assert exp.components[0]["pins"] == [{"name": p} for p in sorted(by_pin)]


def test_unusable_boundary_electrode_gets_no_copper_and_no_pad_change():
    """A cell with no adjacent plaza (array-boundary effect) still gets
    its ONE body pad, but no track/via row at all."""
    # 1x1: no plaza fits, so the single electrode is unusable.
    exp = G.expand("ewod_pad_array", "ARR", {"grid": [1, 1]})
    assert exp.copper == []
    by_pin = _pads_by_pin(exp)
    assert len(by_pin) == 1
    assert len(next(iter(by_pin.values()))) == 1


# ── 2. subtractions honoured in copper ──────────────────────────────────


def test_merged_pad_covering_a_plaza_is_refused_before_any_copper_exists():
    """A merged pad may never cover a plaza (would short its 8 other
    nets) -- already enforced at ``pad_sizes`` validation, before any
    expansion (copper included) is built at all. P1_1 is the auto-placed
    plaza in a 3x3 grid."""
    with pytest.raises(ValueError, match="plaza"):
        G.expand(
            "ewod_pad_array",
            "ARR",
            {"grid": [3, 3], "pad_sizes": [{"cells": [[1, 1], [1, 2]]}]},
        )


def test_reserved_slot_suppresses_its_track_and_via():
    exp = G.expand("ewod_pad_array", "ARR", {"grid": [3, 3], "reserve": ["P1_1:N"]})
    nets_with_copper = {str(c["net"]) for c in exp.copper}
    assert "ARR_R0C1" not in nets_with_copper
    # every OTHER driven electrode is unaffected.
    assert len(exp.copper) == 14  # 7 usable electrodes * (1 track + 1 via)


# ── 3. per-tile ledger ───────────────────────────────────────────────────


def test_fabric_ledger_reports_per_tile_and_board_totals():
    exp = G.expand("ewod_pad_array", "ARR", {"grid": [3, 3]})
    fabric = exp.ledger["fabric"]
    assert set(fabric) == {"tiles", "totals", "reasons", "fan"}
    assert fabric["tiles"] == {"P1_1": {"emitted": 8, "refused": 0, "suppressed": 0}}
    assert fabric["totals"] == {"emitted": 8, "refused": 0, "suppressed": 0}
    assert fabric["reasons"] == []
    # B.Cu fan-out to the sink is explicitly out of this slice's scope --
    # the router's job, stated in the ledger so a reader never assumes it
    # silently happened (docs/backlog/pcb-pre-place-route-blocks.md
    # "Slice 2 — B.Cu fan to the sink: NOT emitted this slice").
    assert fabric["fan"] == "router"


def test_fabric_ledger_counts_a_reserved_slot_as_suppressed_with_a_reason():
    exp = G.expand("ewod_pad_array", "ARR", {"grid": [3, 3], "reserve": ["P1_1:N"]})
    fabric = exp.ledger["fabric"]
    assert fabric["tiles"]["P1_1"] == {"emitted": 7, "refused": 0, "suppressed": 1}
    assert fabric["totals"] == {"emitted": 7, "refused": 0, "suppressed": 1}
    assert len(fabric["reasons"]) == 1
    reason = fabric["reasons"][0]
    assert reason["pin"] == "R0C1"
    assert reason["tile"] == "P1_1"
    assert "reserved" in reason["reason"]


def test_fabric_ledger_counts_a_boundary_refusal_with_no_tile():
    """A cell with no adjacent plaza has nothing to attribute the refusal
    to -- ``tile: None``, board-total only (not silently dropped)."""
    exp = G.expand("ewod_pad_array", "ARR", {"grid": [1, 1]})
    fabric = exp.ledger["fabric"]
    assert fabric["totals"] == {"emitted": 0, "refused": 1, "suppressed": 0}
    assert fabric["tiles"] == {}
    assert len(fabric["reasons"]) == 1
    assert fabric["reasons"][0]["tile"] is None
    assert "no adjacent plaza" in fabric["reasons"][0]["reason"]


def test_fabric_ledger_tracks_rim_virtual_plaza_tiles_too():
    """A rim's own hollow-interior virtual plazas (3-consumer at a
    corner, 2 at a straight edge) get the SAME per-tile fabric accounting
    a real plaza does, keyed ``"rim:R{row}C{col}"``."""
    exp = G.expand("ewod_pad_array", "ARR", {"grid": [4, 4], "variant": "rim"})
    fabric = exp.ledger["fabric"]
    assert fabric["totals"] == {"emitted": 12, "refused": 0, "suppressed": 0}
    # 4 corners (3 consumers each) + 4 straight-edge midpoints... on a 4x4
    # rim there are exactly 4 virtual-plaza tiles, one per corner, shared
    # by that corner's 3 rim pads (see _rim_via_point's own docstring).
    assert sum(t["emitted"] for t in fabric["tiles"].values()) == 12
    assert all(k.startswith("rim:") for k in fabric["tiles"])


# ── 4. envelope refusal (store-layer behaviour, generator-supplied data) ──


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _array_args(**params: Any) -> dict[str, Any]:
    return {
        "generators": [
            {"name": "ARR1", "generator": "ewod_pad_array", "params": params}
        ]
    }


def test_envelope_refuses_reapply_after_the_board_stackup_shrinks(pcb):
    """docs/backlog/pcb-pre-place-route-blocks.md's "Rule envelope is a
    hard gate": the fabric records the layer count it was solved under
    (``ledger['fabric']`` doesn't carry this directly -- each copper
    row's own ``envelope`` does, :func:`precis.pcb.generators.
    _fabric_envelope`); a board whose stackup has since shrunk below that
    refuses re-apply with a named reason rather than keeping copper that
    may no longer be legal."""
    pcb.put(id="ewod-env-1", args=_array_args(grid=[3, 3]))
    ref = pcb.store.get_ref(kind="pcb", id="ewod-env-1")
    assert ref is not None
    board = pcb.store.pcb_load(ref.id)["board"]
    assert board is not None

    # Shrink the board's own stackup to 2 layers directly (no authoring
    # verb for this yet -- module docstring's own "put(stackup=...)"
    # forward-reference) so the NEXT apply's envelope disagrees.
    import json

    with pcb.store.pool.connection() as conn:
        conn.execute(
            "UPDATE pcb_boards SET stackup = %s WHERE board_id = %s",
            (
                json.dumps(
                    [
                        {"name": "F.Cu", "role": "signal"},
                        {"name": "B.Cu", "role": "signal"},
                    ]
                ),
                board["board_id"],
            ),
        )

    # A changed-params re-apply re-expands the fabric and tries to write
    # it against the now-2-layer board -- refuses.
    with pytest.raises(Exception, match="envelope mismatch"):
        pcb.put(id="ewod-env-1", args=_array_args(grid=[3, 3], gap=0.12))


# ── 5. B.Cu fan-out scope ────────────────────────────────────────────────


def test_via_span_is_f_cu_to_b_cu_but_no_bottom_side_fan_track_exists():
    """The via's own ``span`` reaches B.Cu (its landing IS there), but
    this generator emits no SECOND track continuing on from that landing
    to any sink footprint -- exactly one track (F.Cu neck) and one via
    per driven electrode, never two tracks."""
    exp = G.expand(
        "ewod_pad_array",
        "ARR",
        {
            "grid": [6, 6],
            "sink_grid": {
                "part": "C639448",
                "channel_pins": [f"OUT{i}" for i in range(16)],
            },
        },
    )
    tracks, vias = _copper_by_ctype(exp)
    by_net: dict[str, list[dict[str, Any]]] = {}
    for t in tracks:
        by_net.setdefault(str(t["net"]), []).append(t)
    assert all(len(v) == 1 for v in by_net.values())
    assert all(v["geom"]["span"] == ["F.Cu", "B.Cu"] for v in vias)
    assert exp.ledger["fabric"]["fan"] == "router"


# ── 6. 9x9 ────────────────────────────────────────────────────────────────


def test_9x9_full_has_nine_untruncated_plazas_and_72_driven_electrodes():
    exp = G.expand("ewod_pad_array", "ARR", {"grid": [9, 9]})
    assert exp.ledger["summary"]["plazas"] == 9
    assert exp.ledger["summary"]["pads_total"] == 72
    assert exp.ledger["summary"]["pads_unusable"] == 0

    plazas = {(r, c) for r in (1, 4, 7) for c in (1, 4, 7)}
    assert {(v["row"], v["col"]) for v in exp.ledger["plazas"].values()} == plazas

    tracks, vias = _copper_by_ctype(exp)
    assert len(tracks) == 72
    assert len(vias) == 72

    fabric = exp.ledger["fabric"]
    assert set(fabric["tiles"]) == {f"P{r}_{c}" for r, c in plazas}
    assert all(
        t == {"emitted": 8, "refused": 0, "suppressed": 0}
        for t in fabric["tiles"].values()
    )
    assert fabric["totals"] == {"emitted": 72, "refused": 0, "suppressed": 0}
    assert fabric["reasons"] == []


def test_8x8_dogfood_shape_reports_truncated_edge_tiles_honestly():
    """The 8x8 dogfood shape's last block on each axis is truncated (the
    module's own ``_default_plaza`` docstring: "8 rows -> blocks of 3,3,2")
    -- the corner/edge plazas serve FEWER than the full 8 directional
    slots, and the fabric ledger says so plainly (lower ``emitted``, not
    a truncated/padded 8) rather than silently reporting as if nothing
    were different from the 9x9 case above."""
    exp = G.expand("ewod_pad_array", "ARR", {"grid": [8, 8]})
    fabric = exp.ledger["fabric"]
    assert len(fabric["tiles"]) == 9
    # The three plazas sharing row or col 7 (the truncated edge) serve
    # fewer than 8 slots; the four fully-interior ones still serve all 8.
    truncated = {
        k: t["emitted"] for k, t in fabric["tiles"].items() if t["emitted"] < 8
    }
    assert truncated == {"P1_7": 5, "P4_7": 5, "P7_1": 5, "P7_4": 5, "P7_7": 3}
    full = {k: t for k, t in fabric["tiles"].items() if k not in truncated}
    assert all(
        t == {"emitted": 8, "refused": 0, "suppressed": 0} for t in full.values()
    )
    # No refusals/suppressions anywhere -- the shortfall is physical
    # absence (no neighbour cell exists there), not a denied request.
    assert fabric["totals"] == {"emitted": 55, "refused": 0, "suppressed": 0}
    assert exp.ledger["summary"]["pads_unusable"] == 0


def test_9x9_with_channels_per_sink_64_splits_into_two_balanced_sinks():
    """docs/backlog/pcb-ewod-multitile.md "Rulings 2026-09-18" item 2 --
    "9x9 sink packing → balanced by chain order": a 9x9 array has 72
    driven electrodes and the HV507 sink (64 channels) cannot serve all
    of them from ONE sink instance. The REPLACED ``per_tiles`` square-
    block rule could land this as a lopsided 64+8 or refuse outright
    (see the companion refusal tests below); ``channels_per_sink``
    instead balances by CHAIN ORDER: ``ceil(72/64) = 2`` sinks, split
    into as-equal-as-possible shares -- 36 + 36, never 64 + 8."""
    channel_pins = [f"OUT{i}" for i in range(64)]
    exp = G.expand(
        "ewod_pad_array",
        "ARR1",
        {
            "grid": [9, 9],
            "sink_grid": {
                "part": "C639448",
                "channels_per_sink": 64,
                "channel_pins": channel_pins,
                "serial_in_pin": "DIN",
                "serial_out_pin": "DOUT",
            },
        },
    )
    assert exp.ledger["summary"]["pads_total"] == 72

    sink_ledger = exp.ledger["sink_grid"]
    sink_refdes = {k for k in sink_ledger if not k.startswith("_")}
    assert sink_refdes == {"ARR1_SINK_0", "ARR1_SINK_1"}
    n_channels = {
        refdes: len(sink_ledger[refdes]["channels"]) for refdes in sink_refdes
    }
    assert n_channels == {"ARR1_SINK_0": 36, "ARR1_SINK_1": 36}
    assert all(n <= len(channel_pins) for n in n_channels.values())
    assert sum(n_channels.values()) == 72

    # Both centroids sit inside the field's own extent (the mask_open
    # feature's polygon is the field's bounding box), and in DIFFERENT
    # halves -- chain order groups roughly the first half of rows into
    # one share and the second half into the other.
    poly = exp.features[0]["geom"]["polygon"]
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    x_lo, x_hi = min(xs), max(xs)
    y_lo, y_hi = min(ys), max(ys)
    sink0, sink1 = sink_ledger["ARR1_SINK_0"], sink_ledger["ARR1_SINK_1"]
    for s in (sink0, sink1):
        assert x_lo <= s["x"] <= x_hi
        assert y_lo <= s["y"] <= y_hi
    assert (sink0["y"] < 0) != (sink1["y"] < 0)


def test_9x9_with_channels_per_sink_80_refuses_exceeding_the_part():
    """``channels_per_sink`` may never exceed the part's own
    ``channel_pins`` count (rule 1 of the 2026-09-18 ruling) -- a sink
    cannot serve more channels than the part has pins for, named at
    parse time rather than discovered lazily during assignment."""
    channel_pins = [f"OUT{i}" for i in range(64)]
    with pytest.raises(ValueError, match="exceeds channel_pins' own length"):
        G.expand(
            "ewod_pad_array",
            "ARR1",
            {
                "grid": [9, 9],
                "sink_grid": {
                    "part": "C639448",
                    "channels_per_sink": 80,
                    "channel_pins": channel_pins,
                    "serial_in_pin": "DIN",
                    "serial_out_pin": "DOUT",
                },
            },
        )


def test_9x9_sink_grid_per_tiles_is_refused_with_a_named_error():
    """``per_tiles`` (square cell blocks) was REPLACED by
    ``channels_per_sink`` (balanced by chain order) -- forward-only, no
    silent alias, since a spatial block count cannot map onto a channel
    count."""
    channel_pins = [f"OUT{i}" for i in range(64)]
    with pytest.raises(ValueError, match="per_tiles was replaced by channels_per_sink"):
        G.expand(
            "ewod_pad_array",
            "ARR1",
            {
                "grid": [9, 9],
                "sink_grid": {
                    "part": "C639448",
                    "per_tiles": 9,
                    "channel_pins": channel_pins,
                    "serial_in_pin": "DIN",
                    "serial_out_pin": "DOUT",
                },
            },
        )


# ── 7. determinism ───────────────────────────────────────────────────────


def test_expand_is_a_pure_function_of_generator_name_and_params():
    params = {"grid": [9, 9], "reserve": ["P1_1:N"]}
    exp1 = G.expand("ewod_pad_array", "ARR", dict(params))
    exp2 = G.expand("ewod_pad_array", "ARR", dict(params))
    assert exp1.copper == exp2.copper
    assert exp1.ledger == exp2.ledger
    assert exp1.canonical_params == exp2.canonical_params


def test_reapplying_the_same_params_through_the_store_is_a_copper_noop(pcb):
    pcb.put(id="ewod-det-1", args=_array_args(grid=[3, 3]))
    ref = pcb.store.get_ref(kind="pcb", id="ewod-det-1")
    assert ref is not None
    board = pcb.store.pcb_load(ref.id)["board"]
    assert board is not None
    before = sorted(
        pcb.store.pcb_fixed_copper_list(int(board["board_id"])),
        key=lambda r: (r["ctype"], r["net"] or ""),
    )

    resp = pcb.put(id="ewod-det-1", args=_array_args(grid=[3, 3]))
    assert "generator(s) applied" not in resp.body  # no-op branch

    after = sorted(
        pcb.store.pcb_fixed_copper_list(int(board["board_id"])),
        key=lambda r: (r["ctype"], r["net"] or ""),
    )
    assert before == after

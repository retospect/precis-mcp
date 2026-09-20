"""pcb-pre-place-route-blocks Slice 2 — ``ewod_pad_array`` emits its
escape fabric (neck track + plaza via, plus — Rulings 2026-09-19 item 11 —
a B.Cu breakout stub outward from the via) as real copper.

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

import math
from typing import Any

import pytest
from shapely.geometry import LineString  # type: ignore[import-untyped]
from shapely.geometry import Point as SPoint

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import generators as G


def _copper_by_ctype(
    exp: G.GeneratorExpansion,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(F.Cu neck tracks, B.Cu breakout tracks, vias) — split by layer
    since Rulings 2026-09-19 item 11 added a second, differently-layered
    track per driven electrode."""
    necks = [c for c in exp.copper if c["ctype"] == "track" and c["layer"] == "F.Cu"]
    breakouts = [
        c for c in exp.copper if c["ctype"] == "track" and c["layer"] == "B.Cu"
    ]
    vias = [c for c in exp.copper if c["ctype"] == "via"]
    return necks, breakouts, vias


def _pads_by_pin(exp: G.GeneratorExpansion) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for p in exp.footprints[0]["pads"]:
        out.setdefault(p["pin"], []).append(p)
    return out


# ── 1. copper row shapes ────────────────────────────────────────────────


def test_driven_electrode_emits_one_neck_one_breakout_and_one_via_row():
    exp = G.expand("ewod_pad_array", "ARR", {"grid": [3, 3]})
    necks, breakouts, vias = _copper_by_ctype(exp)
    assert len(necks) == 8
    assert len(breakouts) == 8
    assert len(vias) == 8

    track = next(t for t in necks if t["net"] == "ARR_R0C0")
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

    # Rulings 2026-09-19 item 11: the B.Cu breakout stub starts EXACTLY at
    # the via's own centre (so it unions onto the via's B.Cu terminal by
    # ordinary touching-copper connectivity) and runs outward.
    breakout = next(b for b in breakouts if b["net"] == "ARR_R0C0")
    assert breakout["ctype"] == "track"
    assert breakout["layer"] == "B.Cu"
    bseg = breakout["geom"]["segments"][0]
    assert bseg["start"] == [via["geom"]["x"], via["geom"]["y"]]
    assert bseg["start"] != bseg["end"]
    assert breakout["geom"]["width_mm"] > 0
    assert breakout["envelope"] == track["envelope"]


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
    # 7 usable electrodes * (1 F.Cu neck + 1 via + 1 B.Cu breakout,
    # Rulings 2026-09-19 item 11).
    assert len(exp.copper) == 21


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


def test_via_span_is_f_cu_to_b_cu_and_breakout_is_short_not_a_full_fan_to_sink():
    """The via's own ``span`` reaches B.Cu (its landing IS there).
    Rulings 2026-09-19 item 11 adds a SHORT, pre-solved B.Cu breakout
    stub past the via (so exactly TWO tracks per driven electrode now:
    the F.Cu neck and the B.Cu breakout) -- but this generator still
    emits no THIRD track continuing all the way to any sink footprint:
    the breakout's own length is bounded at the plaza's own ``slot_a``,
    nowhere near a real sink pad's position (this module has no DB access
    to compute that), and ``ledger['fabric']['fan']`` still names the
    router as the owner of that remaining run."""
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
    necks, breakouts, vias = _copper_by_ctype(exp)
    by_net: dict[str, list[dict[str, Any]]] = {}
    for t in necks + breakouts:
        by_net.setdefault(str(t["net"]), []).append(t)
    assert all(len(v) == 2 for v in by_net.values())
    assert all({t["layer"] for t in v} == {"F.Cu", "B.Cu"} for v in by_net.values())
    assert all(v["geom"]["span"] == ["F.Cu", "B.Cu"] for v in vias)

    slot_a = exp.canonical_params["slot_a"]
    for b in breakouts:
        seg = b["geom"]["segments"][0]
        length = math.hypot(
            seg["end"][0] - seg["start"][0], seg["end"][1] - seg["start"][1]
        )
        assert length == pytest.approx(slot_a, abs=1e-6)

    assert exp.ledger["fabric"]["fan"] == "router"


# ── 6. 9x9 ────────────────────────────────────────────────────────────────


def test_9x9_full_has_nine_untruncated_plazas_and_72_driven_electrodes():
    exp = G.expand("ewod_pad_array", "ARR", {"grid": [9, 9]})
    assert exp.ledger["summary"]["plazas"] == 9
    assert exp.ledger["summary"]["pads_total"] == 72
    assert exp.ledger["summary"]["pads_unusable"] == 0

    plazas = {(r, c) for r in (1, 4, 7) for c in (1, 4, 7)}
    assert {(v["row"], v["col"]) for v in exp.ledger["plazas"].values()} == plazas

    necks, breakouts, vias = _copper_by_ctype(exp)
    assert len(necks) == 72
    assert len(breakouts) == 72
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


# ── 6b. Rulings 2026-09-19 items 10/11 — HV-derived sizing, breakout ──────


def test_hv_separation_derives_from_ipc2221b_b4_at_declared_voltage():
    """Item 10: a declared ``drive_voltage_v`` derives ``hv_separation``
    from IPC-2221B Table 6-1's B4 (external, coated) column — 0.4mm for
    the whole 101-300V band (so both 250V and 100V land there), and the
    row used is recorded (``hv_row``) so the ledger/capability view can
    quote it."""
    sizing_250 = G.resolve_ewod_sizing({"grid": [3, 3], "drive_voltage_v": 250})
    assert sizing_250["hv_separation"] == pytest.approx(0.4)
    assert sizing_250["hv_row"] == "B4"

    sizing_100 = G.resolve_ewod_sizing({"grid": [3, 3], "drive_voltage_v": 100})
    assert sizing_100["hv_separation"] == pytest.approx(0.13)
    assert sizing_100["hv_row"] == "B4"

    # No declared voltage -- unchanged fallback to the fab spacing floor,
    # `hv_row` stays None (nothing was looked up in the IPC table at all).
    sizing_none = G.resolve_ewod_sizing({"grid": [3, 3]})
    assert sizing_none["hv_row"] is None


def test_drive_voltage_above_500v_refuses_with_a_named_error():
    """Item 10: per-volt extrapolation past IPC-2221B Table 6-1's 500V top
    band is explicitly out of scope -- refused, not silently invented."""
    with pytest.raises(ValueError, match="500"):
        G.resolve_ewod_sizing({"grid": [3, 3], "drive_voltage_v": 600})


def test_pitch_2mm_is_under_the_derived_floor_at_250v():
    """Item 10's own worked example: at 250V (B4 -> 0.4mm hv_separation)
    the derived plaza-capacity floor is ~2.233mm -- the array's PREVIOUS
    default pitch (2.0mm) no longer fits, and `resolve_ewod_sizing` must
    say so rather than silently under-space the plaza."""
    with pytest.raises(ValueError, match="derived plaza-capacity floor"):
        G.resolve_ewod_sizing({"grid": [3, 3], "drive_voltage_v": 250, "pitch": 2.0})

    # The ruling's own new default (2.25mm) clears that floor.
    sizing = G.resolve_ewod_sizing({"grid": [3, 3], "drive_voltage_v": 250})
    assert sizing["min_pitch"] == pytest.approx(2.233, abs=0.01)
    assert G.resolve_ewod_sizing(
        {"grid": [3, 3], "drive_voltage_v": 250, "pitch": 2.25}
    )


def test_8x8_breakout_stubs_clear_hv_separation_from_every_foreign_via_and_stub():
    """Item 11's own clearance requirement, checked at the design point
    the ruling was made against (8x8 @ pitch 2.25mm / 250V drive): no
    breakout stub comes within ``hv_separation`` of ANY other net's own
    via or breakout stub. Checked by real polygon distance (shapely),
    not the closed-form slot geometry the plaza layout search already
    proved pairwise-safe for VIA centres alone (:func:`precis.pcb.
    generators._plaza_capacity`) -- the breakout stubs are new geometry
    that search never reasoned about."""
    exp = G.expand(
        "ewod_pad_array",
        "ARR1",
        {"grid": [8, 8], "drive_voltage_v": 250, "pitch": 2.25},
    )
    hv = exp.canonical_params["hv_separation"]
    stub_width = exp.canonical_params["stub_width"]
    _, breakouts, vias = _copper_by_ctype(exp)

    stub_shapes = []
    for b in breakouts:
        seg = b["geom"]["segments"][0]
        line = LineString([tuple(seg["start"]), tuple(seg["end"])])
        stub_shapes.append((str(b["net"]), line.buffer(stub_width / 2.0)))
    via_shapes = [
        (
            str(v["net"]),
            SPoint(v["geom"]["x"], v["geom"]["y"]).buffer(v["geom"]["dia_mm"] / 2.0),
        )
        for v in vias
    ]

    worst = min(
        s1.distance(s2)
        for i, (n1, s1) in enumerate(stub_shapes)
        for n2, s2 in stub_shapes[i + 1 :]
        if n1 != n2
    )
    assert worst >= hv, f"stub-vs-stub clearance {worst}mm < hv_separation {hv}mm"

    worst_via = min(
        s1.distance(s2) for n1, s1 in stub_shapes for n2, s2 in via_shapes if n1 != n2
    )
    assert worst_via >= hv, (
        f"stub-vs-via clearance {worst_via}mm < hv_separation {hv}mm"
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

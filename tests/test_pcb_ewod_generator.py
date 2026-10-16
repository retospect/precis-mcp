"""pcb-ewod-multitile Slice 2 — the ``ewod_pad_array`` generator.

Exercises ``put(kind='pcb', args={'generators': [...]})`` end to end: the
array expands into ONE component whose pins are the electrode nets, the
expansion is idempotent (same params -> no-op, changed params -> retire
and reinsert), the emitted copper is real (polygon electrodes + drilled
via pads, all real -- not synthesized), the field-wide mask_open feature
lands, and export/SVG never choke on it. Geometry-level coverage (zigzag
validity, constant gap, zero cross-net overlap) lives in the pure-Python
unit tests against :mod:`precis.pcb.generators` directly
(``test_pcb_ewod_generator_geometry.py``); this file is the store/handler
wiring layer on top.
"""

from __future__ import annotations

import zipfile

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _array_args(**params):
    return {
        "generators": [
            {"name": "ARR1", "generator": "ewod_pad_array", "params": params}
        ]
    }


# ── authoring + persistence ─────────────────────────────────────────────
def test_put_generators_block_is_reported_in_the_response(pcb):
    resp = pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3]))
    assert "1 generator(s) applied" in resp.body


def test_generator_expands_to_one_component_with_electrode_pins(pcb):
    pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3]))
    ref = pcb.store.get_ref(kind="pcb", id="ewod-gen-1")
    assert ref is not None
    graph = pcb.store.pcb_graph(ref.id)
    assert len(graph["instances"]) == 1
    inst = graph["instances"][0]
    assert inst["refdes"] == "ARR1"
    assert inst["footprint"] == "__gen_ARR1"
    # 3x3 full -- 8 electrodes, one plaza (centre), one net per electrode.
    assert inst["n_pins"] == 8
    net_names = {n["name"] for n in graph["nets"]}
    assert net_names == {
        f"ARR1_{p}"
        for p in ("R0C0", "R0C1", "R0C2", "R1C0", "R1C2", "R2C0", "R2C1", "R2C2")
    }


def test_generator_row_and_ledger_are_readable_back(pcb):
    pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3]))
    ref = pcb.store.get_ref(kind="pcb", id="ewod-gen-1")
    assert ref is not None
    gens = pcb.store.pcb_generators_for(ref.id)
    assert set(gens) == {"ARR1"}
    row = gens["ARR1"]
    assert row["generator"] == "ewod_pad_array"
    assert row["refdes"] == "ARR1"
    assert row["ledger"]["summary"]["plazas"] == 1
    assert row["ledger"]["summary"]["pads_usable"] == 8


def test_local_footprint_carries_one_electrode_body_pad_per_pin(pcb):
    """pcb-pre-place-route-blocks Slice 2: the stub neck and the plaza
    via are no longer pad rows (see
    ``test_generator_copper_carries_the_escape_fabric_as_tracks_and_vias``
    below for where they went) — the local footprint's ``pads`` list
    carries ONLY the electrode body, one per pin, ever."""
    pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3]))
    ref = pcb.store.get_ref(kind="pcb", id="ewod-gen-1")
    assert ref is not None
    fp = pcb.store.pcb_local_footprints_for(ref.id)["__gen_ARR1"]
    pads = fp["pads"]
    electrodes = [p for p in pads if p["shape"] == "polygon" and len(p["poly"]) > 4]
    assert len(electrodes) == 8
    assert len(pads) == 8  # ONE pad per pin -- no stub, no via pad
    assert not any(p.get("drill") for p in pads)
    assert all(p["role"] == "electrode" for p in pads)
    # role=electrode defaults paste to "none" (Slice 1's own default).
    assert all(p["paste"] == "none" for p in pads)


def test_generator_copper_carries_the_escape_fabric_as_tracks_and_vias(pcb):
    """pcb-pre-place-route-blocks Slice 2: every driven electrode's neck
    stub and plaza via land in ``pcb_fixed_copper`` as real ``track``/
    ``via`` rows, scoped to this generator's own identity — not as
    footprint pads (see the sibling test above). Rulings 2026-09-19 item
    11 added a second track per electrode (a B.Cu breakout stub past the
    via); Reto removed that row 2026-09-29 (see ``precis.pcb.generators``'s
    module docstring, "B.Cu breakout stub" section) -- back to 8 tracks,
    one F.Cu neck per driven electrode."""
    pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3]))
    ref = pcb.store.get_ref(kind="pcb", id="ewod-gen-1")
    assert ref is not None
    board = pcb.store.pcb_load(ref.id)["board"]
    assert board is not None
    rows = pcb.store.pcb_fixed_copper_list(int(board["board_id"]))
    tracks = [r for r in rows if r["ctype"] == "track"]
    necks = [t for t in tracks if t["layer"] == "F.Cu"]
    vias = [r for r in rows if r["ctype"] == "via"]
    assert len(necks) == 8
    assert len(tracks) == 8
    assert len(vias) == 8
    assert all(r["fixed"] is True for r in rows)
    assert all(r["generator_name"] == "ARR1" for r in rows)
    expected_pins = ("R0C0", "R0C1", "R0C2", "R1C0", "R1C2", "R2C0", "R2C1", "R2C2")
    assert {r["net"] for r in vias} == {f"ARR1_{p}" for p in expected_pins}
    assert all(v["span"] == ["F.Cu", "B.Cu"] for v in vias)
    assert all(t["segments"] and t["width_mm"] > 0 for t in tracks)


def test_field_wide_mask_open_feature_is_emitted(pcb):
    pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3]))
    ref = pcb.store.get_ref(kind="pcb", id="ewod-gen-1")
    assert ref is not None
    regions = pcb._mask_open_regions(ref.id)
    assert len(regions) == 1
    assert regions[0]["side"] == "top"
    assert len(regions[0]["polygon"]) == 4


# ── idempotency ──────────────────────────────────────────────────────────
def test_reapplying_the_same_params_is_a_no_op(pcb):
    pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3]))
    ref = pcb.store.get_ref(kind="pcb", id="ewod-gen-1")
    assert ref is not None
    with pcb.store.pool.connection() as conn:
        before = conn.execute(
            "SELECT component_id FROM pcb_instances WHERE ref_id=%s AND refdes='ARR1'",
            (ref.id,),
        ).fetchone()

    resp = pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3]))
    assert "generator(s) applied" not in resp.body

    with pcb.store.pool.connection() as conn:
        after = conn.execute(
            "SELECT component_id FROM pcb_instances WHERE ref_id=%s AND refdes='ARR1' "
            "AND retired_at IS NULL",
            (ref.id,),
        ).fetchone()
        n_instances = conn.execute(
            "SELECT count(*) FROM pcb_instances WHERE ref_id=%s AND retired_at IS NULL",
            (ref.id,),
        ).fetchone()
    assert after is not None
    assert after[0] == before[0]  # SAME component -- never retired/reinserted
    assert n_instances[0] == 1


def test_a_stale_board_is_named_and_regenerate_re_expands_it(pcb):
    """A board stored at an older generator version says so on
    op='place' and on a put that does not name the generator; putting the
    generator entry again re-expands it and clears the warning."""
    pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3]))
    ref = pcb.store.get_ref(kind="pcb", id="ewod-gen-1")
    assert ref is not None
    with pcb.store.pool.connection() as conn:
        conn.execute(
            "UPDATE pcb_generators SET version = version - 1 WHERE ref_id=%s",
            (ref.id,),
        )
        conn.commit()

    assert (
        "generator output is stale"
        in pcb.put(id="ewod-gen-1", args={"op": "place", "iters": 10}).body
    )
    resp = pcb.put(id="ewod-gen-1", args={"meta": {"note": "x"}})
    assert "ARR1 (ewod_pad_array): stored version" in resp.body

    resp = pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3]))
    assert "1 generator(s) applied" in resp.body
    assert "generator output is stale" not in resp.body


def test_regenerate_re_expands_even_when_nothing_changed(pcb):
    """The escape for output that changed without a version
    bump — same generator, version and params, re-expanded on request."""
    pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3]))
    args = _array_args(grid=[3, 3])
    args["generators"][0]["regenerate"] = True
    assert "1 generator(s) applied" in pcb.put(id="ewod-gen-1", args=args).body


def test_changed_params_retires_and_reinserts_the_expansion(pcb):
    pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3]))
    ref = pcb.store.get_ref(kind="pcb", id="ewod-gen-1")
    assert ref is not None
    with pcb.store.pool.connection() as conn:
        before = conn.execute(
            "SELECT component_id FROM pcb_instances WHERE ref_id=%s AND refdes='ARR1'",
            (ref.id,),
        ).fetchone()

    resp = pcb.put(id="ewod-gen-1", args=_array_args(grid=[4, 4], variant="rim"))
    assert "1 generator(s) applied" in resp.body

    graph = pcb.store.pcb_graph(ref.id)
    assert len(graph["instances"]) == 1  # old one retired, not accumulated
    inst = graph["instances"][0]
    assert inst["refdes"] == "ARR1"
    assert inst["n_pins"] == 12  # 4x4 rim -- 16 - 2x2 hollow interior

    with pcb.store.pool.connection() as conn:
        after = conn.execute(
            "SELECT component_id FROM pcb_instances WHERE ref_id=%s AND refdes='ARR1' "
            "AND retired_at IS NULL",
            (ref.id,),
        ).fetchone()
        n_old_nets = conn.execute(
            "SELECT count(*) FROM pcb_nets WHERE ref_id=%s AND name LIKE 'ARR1\\_R2C2%%' "
            "AND retired_at IS NULL",
            (ref.id,),
        ).fetchone()
    assert after is not None
    assert after[0] != before[0]  # a NEW component, old one retired
    assert n_old_nets[0] == 0  # R2C2 doesn't exist in the 4x4 rim's pin set


# ── validation ───────────────────────────────────────────────────────────
def test_pitch_under_the_derived_floor_is_bad_input(pcb):
    with pytest.raises(BadInput, match="derived plaza-capacity floor"):
        pcb.put(id="ewod-bad", args=_array_args(grid=[3, 3], pitch=0.3))


def test_unknown_generator_type_is_bad_input(pcb):
    with pytest.raises(BadInput, match="unknown generator type"):
        pcb.put(
            id="ewod-bad",
            args={
                "generators": [{"name": "X", "generator": "not_a_thing", "params": {}}]
            },
        )


def test_pads_not_a_perfect_square_needs_explicit_grid(pcb):
    with pytest.raises(BadInput, match="perfect square"):
        pcb.put(id="ewod-bad", args=_array_args(pads=10))


# ── sink_grid (round 7; balanced-by-chain-order ruling 2026-09-18) ───────
def _sink_args(**overrides):
    sink_grid = {
        "part": "C639448",
        "channels_per_sink": 8,
        "channel_pins": [f"OUT{i}" for i in range(16)],
        "top_plate_pin": "CPLT",
        "power": {"VDD": "VCC_HV", "GND": "GND"},
    }
    sink_grid.update(overrides.pop("sink_grid", {}))
    return _array_args(grid=[6, 6], sink_grid=sink_grid, **overrides)


def test_sink_grid_needs_exactly_one_of_part_or_footprint(pcb):
    with pytest.raises(BadInput, match="EXACTLY one of"):
        pcb.put(
            id="ewod-bad",
            args=_array_args(
                grid=[6, 6],
                sink_grid={"channel_pins": ["A"]},
            ),
        )


def test_sink_grid_per_tiles_is_refused_with_a_named_error(pcb):
    with pytest.raises(BadInput, match="per_tiles was replaced by channels_per_sink"):
        pcb.put(
            id="ewod-bad",
            args=_sink_args(sink_grid={"per_tiles": 3}),
        )


def test_sink_grid_emits_one_bottom_side_instance_per_channels_per_sink_share(pcb):
    pcb.put(id="ewod-sink-1", args=_sink_args())
    ref = pcb.store.get_ref(kind="pcb", id="ewod-sink-1")
    assert ref is not None
    graph = pcb.store.pcb_graph(ref.id)
    by_refdes = {i["refdes"]: i for i in graph["instances"]}
    # 6x6 grid -> 32 driven electrodes (36 cells - 4 auto plazas);
    # channels_per_sink=8 -> ceil(32/8) = 4 equal shares of 8, chain-ordered.
    sink_refdes = {r for r in by_refdes if r.startswith("ARR1_SINK_")}
    assert sink_refdes == {
        "ARR1_SINK_0",
        "ARR1_SINK_1",
        "ARR1_SINK_2",
        "ARR1_SINK_3",
    }
    for r in sink_refdes:
        assert by_refdes[r]["layer"] == "bottom"


def test_sink_grid_channels_bind_to_the_electrode_escape_nets(pcb):
    pcb.put(id="ewod-sink-1", args=_sink_args())
    ref = pcb.store.get_ref(kind="pcb", id="ewod-sink-1")
    assert ref is not None
    gens = pcb.store.pcb_generators_for(ref.id)
    ledger = gens["ARR1"]["ledger"]["sink_grid"]
    # 32 driven electrodes / channels_per_sink=8 -> 4 equal shares of 8.
    sink0 = ledger["ARR1_SINK_0"]
    assert sink0["index"] == 0
    assert sink0["share"] == 8
    assert len(sink0["channels"]) == 8
    graph = pcb.store.pcb_graph(ref.id)
    nets_by_name = {n["name"]: n for n in graph["nets"]}
    for ch_pin, elec_pin in sink0["channels"].items():
        members = {
            (m["refdes"], m["pin"]) for m in nets_by_name[f"ARR1_{elec_pin}"]["members"]
        }
        assert ("ARR1_SINK_0", ch_pin) in members
        assert ("ARR1", elec_pin) in members


def test_sink_grid_supplies_pad_driver_and_electrodes_are_clients(pcb):
    """pcb-lazy-netlist-and-checks.md §1b: every sink SUPPLIES the
    ``pad_driver`` role with one one-pin block per datasheet channel pin
    (wired or not); every driven electrode pin is ``client_of`` it. No
    ``pin_swap_groups`` any more."""
    args = _sink_args()
    pcb.put(id="ewod-sink-1", args=args)
    ref = pcb.store.get_ref(kind="pcb", id="ewod-sink-1")
    assert ref is not None
    graph = pcb.store.pcb_graph(ref.id)
    by_refdes = {i["refdes"]: i for i in graph["instances"]}
    channel_pins = [f"OUT{i}" for i in range(16)]
    for refdes in ("ARR1_SINK_0", "ARR1_SINK_1", "ARR1_SINK_2", "ARR1_SINK_3"):
        inst = by_refdes[refdes]
        assert "pin_swap_groups" not in inst
        assert inst["supplies"] == [
            {"role": "pad_driver", "blocks": [[p] for p in channel_pins]}
        ]
    driven = 0
    for net in graph["nets"]:
        for m in net["members"]:
            if m["refdes"] == "ARR1" and net["name"].startswith("ARR1_"):
                assert m["meta"]["client_of"] == {"role": "pad_driver"}
                driven += 1
    assert driven > 0


def test_sink_grid_daisy_chains_din_dout_across_sinks(pcb):
    pcb.put(id="ewod-sink-1", args=_sink_args())
    ref = pcb.store.get_ref(kind="pcb", id="ewod-sink-1")
    assert ref is not None
    gens = pcb.store.pcb_generators_for(ref.id)
    ledger = gens["ARR1"]["ledger"]["sink_grid"]
    assert ledger["_serial_in_net"] == "ARR1_serial_in"
    assert ledger["_serial_out_net"] == "ARR1_serial_3"
    graph = pcb.store.pcb_graph(ref.id)
    members_by_net = {n["name"]: n["members"] for n in graph["nets"]}
    # Every intermediate daisy net has exactly 2 endpoints (one sink's DOUT,
    # the next sink's DIN); the very first/last are external (1 endpoint).
    assert len(members_by_net["ARR1_serial_in"]) == 1
    assert len(members_by_net[ledger["_serial_out_net"]]) == 1
    interior = [
        n
        for n in members_by_net
        if n.startswith("ARR1_serial_")
        and n not in (ledger["_serial_in_net"], ledger["_serial_out_net"])
    ]
    assert interior
    for n in interior:
        assert len(members_by_net[n]) == 2


def test_sink_grid_top_plate_rail_fans_out_to_every_sink(pcb):
    pcb.put(id="ewod-sink-1", args=_sink_args())
    ref = pcb.store.get_ref(kind="pcb", id="ewod-sink-1")
    assert ref is not None
    graph = pcb.store.pcb_graph(ref.id)
    members_by_net = {n["name"]: n["members"] for n in graph["nets"]}
    assert len(members_by_net["ARR1_top_plate"]) == 4  # one per sink


def test_sink_grid_channels_per_sink_exceeding_channel_pins_is_bad_input(pcb):
    with pytest.raises(BadInput, match="exceeds channel_pins' own length"):
        pcb.put(
            id="ewod-bad",
            args=_sink_args(
                sink_grid={"channel_pins": ["OUT0"], "channels_per_sink": 5}
            ),
        )


def test_sink_grid_changed_params_retires_old_sinks_too(pcb):
    pcb.put(id="ewod-sink-1", args=_sink_args())
    ref = pcb.store.get_ref(kind="pcb", id="ewod-sink-1")
    assert ref is not None

    # channels_per_sink=32 collapses the 4-sink split down to a single
    # sink -- 32 usable electrodes (36 cells - 4 plazas) now need one
    # sink's worth of channel_pins, so widen it past the base fixture's 16.
    pcb.put(
        id="ewod-sink-1",
        args=_sink_args(
            sink_grid={
                "channels_per_sink": 32,
                "channel_pins": [f"OUT{i}" for i in range(32)],
            }
        ),
    )
    graph = pcb.store.pcb_graph(ref.id)
    sink_refdes = {i["refdes"] for i in graph["instances"] if "_SINK_" in i["refdes"]}
    assert sink_refdes == {"ARR1_SINK_0"}
    with pcb.store.pool.connection() as conn:
        n_retired_sinks = conn.execute(
            "SELECT count(*) FROM pcb_instances WHERE ref_id=%s "
            "AND refdes LIKE 'ARR1\\_SINK\\_%%' AND retired_at IS NOT NULL",
            (ref.id,),
        ).fetchone()
    # All 4 old sinks are retired before the new one is inserted (retire
    # happens on the OLD refdes set, independent of what the new expansion
    # names) -- the surviving "ARR1_SINK_0" above is a FRESH row, not the
    # old one kept alive.
    assert n_retired_sinks[0] == 4


def test_sink_grid_not_supported_with_rim_variant(pcb):
    with pytest.raises(BadInput, match="not supported with variant='rim'"):
        pcb.put(
            id="ewod-bad",
            args=_sink_args(variant="rim"),
        )


# ── sink_grid control pins (ewod-controller-and-hv-supply.md Slice 2,
# ruling 12 — HV507's CLK/LE/BL/POL/DIR as named config) ──────────────────
_CONTROL_SINK_GRID = {
    "clk_pin": "CLK",
    "le_pin": "LE",
    "pol_pin": "POL",
    "dir_pin": "DIR",
    "bl_pin": "BL",
}


def test_sink_grid_control_pins_get_one_bussed_external_net_each(pcb):
    args = _sink_args(sink_grid=dict(_CONTROL_SINK_GRID))
    # A board-level connections entry wires the controller onto the
    # externally-facing `{name}_<ctl>` nets — the Slice-2 acceptance path.
    args["components"] = [
        {"refdes": "U1", "footprint": "mcu", "pins": [{"name": "SCK"}]}
    ]
    args["footprints"] = [
        {
            "name": "mcu",
            "pads": [{"pin": "SCK", "shape": "rect", "x": 0, "y": 0, "w": 0.4}],
        }
    ]
    args["connections"] = [{"net": "ARR1_clk", "refdes": "U1", "pin": "SCK"}]
    pcb.put(id="ewod-sink-ctl", args=args)
    ref = pcb.store.get_ref(kind="pcb", id="ewod-sink-ctl")
    assert ref is not None
    gens = pcb.store.pcb_generators_for(ref.id)
    control_nets = gens["ARR1"]["ledger"]["sink_grid"]["_control_nets"]
    assert control_nets == {
        ctl: {"pin": pin, "net": f"ARR1_{ctl}"}
        for ctl, pin in (
            ("clk", "CLK"),
            ("le", "LE"),
            ("bl", "BL"),
            ("pol", "POL"),
            ("dir", "DIR"),
        )
    }
    graph = pcb.store.pcb_graph(ref.id)
    members_by_net = {
        n["name"]: {(m["refdes"], m["pin"]) for m in n["members"]}
        for n in graph["nets"]
    }
    sinks = {f"ARR1_SINK_{i}" for i in range(4)}
    for ctl, pin in (
        ("clk", "CLK"),
        ("le", "LE"),
        ("bl", "BL"),
        ("pol", "POL"),
        ("dir", "DIR"),
    ):
        # Each control net is bussed to every sink's own pin of that name.
        assert {(s, pin) for s in sinks} <= members_by_net[f"ARR1_{ctl}"]
    # The board-level connections entry landed on the same net.
    assert ("U1", "SCK") in members_by_net["ARR1_clk"]


def test_sink_grid_a_control_pin_also_in_power_is_bad_input(pcb):
    with pytest.raises(BadInput, match="same pin twice"):
        pcb.put(
            id="ewod-bad",
            args=_sink_args(
                sink_grid={"clk_pin": "CLK", "power": {"CLK": "GND", "GND": "GND"}}
            ),
        )


def test_sink_grid_two_control_keys_on_one_pin_is_bad_input(pcb):
    with pytest.raises(BadInput, match="same pin twice"):
        pcb.put(
            id="ewod-bad",
            args=_sink_args(sink_grid={"clk_pin": "CTL", "le_pin": "CTL"}),
        )


def test_sink_grid_a_control_pin_colliding_with_a_channel_pin_is_bad_input(pcb):
    with pytest.raises(BadInput, match="same pin twice"):
        pcb.put(
            id="ewod-bad",
            args=_sink_args(sink_grid={"dir_pin": "OUT3"}),
        )


# ── tape_land (ewod-controller-and-hv-supply.md Slice 3 — ruling 4's
# copper-tape landing for the ITO top plate, "Defect B") ──────────────────
def test_tape_land_emits_one_open_paste_free_drill_free_pad(pcb):
    pcb.put(
        id="ewod-tape",
        args=_sink_args(
            tape_land={
                "edge": "N",
                "length_mm": 20.0,
                "width_mm": 6.0,
                "offset_mm": 1.0,
            }
        ),
    )
    ref = pcb.store.get_ref(kind="pcb", id="ewod-tape")
    assert ref is not None
    fp = pcb.store.pcb_local_footprints_for(ref.id)["__gen_ARR1"]
    lands = [p for p in fp["pads"] if p.get("role") == "tape_land"]
    assert len(lands) == 1
    pad = lands[0]
    assert pad["number"] == "top_plate"
    assert pad["shape"] == "rect"
    assert pad["mask"] == "open"
    assert pad["paste"] == "none"
    assert pad["layer"] == "F.Cu"
    assert not pad.get("drill")
    assert pad["w"] == pytest.approx(20.0)
    assert pad["h"] == pytest.approx(6.0)
    # N edge: the strip sits just past the +y boundary of the array's own
    # mask extent (rows*pitch/2 + gap + offset + width/2 => centre).
    assert pad["y"] > 0.0

    ledger = pcb.store.pcb_generators_for(ref.id)["ARR1"]["ledger"]["tape_land"]
    assert ledger["edge"] == "N"
    assert ledger["net"] == "ARR1_top_plate"
    assert ledger["length_mm"] == pytest.approx(20.0)

    # The strip is wired to the SAME top-plate net the sinks' CPLT pins
    # carry — one net, five members (4 sinks + the landing itself).
    graph = pcb.store.pcb_graph(ref.id)
    members = {
        (m["refdes"], m["pin"])
        for n in graph["nets"]
        if n["name"] == "ARR1_top_plate"
        for m in n["members"]
    }
    assert ("ARR1", "top_plate") in members
    assert len(members) == 5


def test_tape_land_defaults_to_a_full_edge_strip(pcb):
    pcb.put(id="ewod-tape", args=_array_args(grid=[6, 6], tape_land={}))
    ref = pcb.store.get_ref(kind="pcb", id="ewod-tape")
    assert ref is not None
    fp = pcb.store.pcb_local_footprints_for(ref.id)["__gen_ARR1"]
    (pad,) = [p for p in fp["pads"] if p.get("role") == "tape_land"]
    assert pad["h"] == pytest.approx(5.0)  # width_mm default
    assert pad["w"] == pytest.approx(6 * 2.25 + 2 * 0.10)  # full N edge extent
    # No sink_grid: the default top-plate net name applies.
    graph = pcb.store.pcb_graph(ref.id)
    members = {
        (m["refdes"], m["pin"])
        for n in graph["nets"]
        if n["name"] == "ARR1_top_plate"
        for m in n["members"]
    }
    assert members == {("ARR1", "top_plate")}


def test_tape_land_length_mm_changes_the_strip(pcb):
    pcb.put(id="ewod-tape", args=_array_args(grid=[6, 6], tape_land={}))
    pcb.put(
        id="ewod-tape",
        args=_array_args(grid=[6, 6], tape_land={"length_mm": 9.0}),
    )
    ref = pcb.store.get_ref(kind="pcb", id="ewod-tape")
    fp = pcb.store.pcb_local_footprints_for(ref.id)["__gen_ARR1"]
    (pad,) = [p for p in fp["pads"] if p.get("role") == "tape_land"]
    assert pad["w"] == pytest.approx(9.0)


def test_tape_land_bad_edge_is_bad_input(pcb):
    with pytest.raises(BadInput, match="tape_land.edge"):
        pcb.put(id="ewod-bad", args=_array_args(grid=[3, 3], tape_land={"edge": "X"}))


def test_tape_land_shows_in_the_capability_map(pcb):
    pcb.put(id="ewod-tape", args=_array_args(grid=[6, 6], tape_land={"edge": "E"}))
    resp = pcb.get(id="ewod-tape", view="capability")
    assert "TAPE LAND" in resp.body


def test_top_plate_net_lands_in_the_hv_class_when_voltage_is_declared(pcb):
    """Slice 4d: with drive_voltage_v declared, the top-plate net gets the
    ewod_<name>_hv class carrying working_voltage_v — the 0.4mm creepage
    floor is derived, not typed."""
    args = _sink_args(
        sink_grid=dict(_CONTROL_SINK_GRID),
        drive_voltage_v=250,
        tape_land={"edge": "N"},
    )
    pcb.put(id="ewod-hv", args=args)
    ref = pcb.store.get_ref(kind="pcb", id="ewod-hv")
    assert ref is not None
    classes = pcb.store.pcb_graph(ref.id)["net_classes"]
    assert classes["ewod_ARR1_hv"] == {"working_voltage_v": 250.0}
    graph = pcb.store.pcb_graph(ref.id)
    top_plate = [n for n in graph["nets"] if n["name"] == "ARR1_top_plate"]
    assert top_plate[0]["net_class"] == "ewod_ARR1_hv"


# ── top plate on a switched channel (ewod-controller-and-hv-supply.md
# ruling 18, Reto 2026-10-16) ─────────────────────────────────────────────
def _switched_plate_args(**sink_overrides):
    sink_grid = {
        **_CONTROL_SINK_GRID,
        "top_plate_pin": None,
        "top_plate_channel": "OUT15",
        **sink_overrides,
    }
    return _sink_args(sink_grid=sink_grid, drive_voltage_v=250)


def _members(graph, net):
    (row,) = [n for n in graph["nets"] if n["name"] == net]
    return {(m["refdes"], m["pin"]) for m in row["members"]}


def test_top_plate_rides_a_switched_channel_of_sink_0(pcb):
    pcb.put(id="ewod-plate-sw", args=_switched_plate_args())
    ref = pcb.store.get_ref(kind="pcb", id="ewod-plate-sw")
    assert ref is not None
    graph = pcb.store.pcb_graph(ref.id)
    plate = _members(graph, "ARR1_top_plate")
    assert ("ARR1_SINK_0", "OUT15") in plate
    # No fixed-rail tie: the plate net touches only that channel (and the
    # array's own tape land when one is emitted).
    assert {p for r, p in plate if r.startswith("ARR1_SINK_")} == {"OUT15"}
    # The plate channel is never an electrode's, on any sink.
    ledger = pcb.store.pcb_generators_for(ref.id)["ARR1"]["ledger"]["sink_grid"]
    for refdes, row in ledger.items():
        if refdes.startswith("ARR1_SINK_"):
            assert "OUT15" not in row["channels"]
    assert ledger["_top_plate_channel"] == {
        "sink": "ARR1_SINK_0",
        "pin": "OUT15",
        "polarity_net": "ARR1_pol",
    }
    # The plate net carries the drive voltage like the fixed-rail case.
    plate_row = [n for n in graph["nets"] if n["name"] == "ARR1_top_plate"]
    assert plate_row[0]["net_class"] == "ewod_ARR1_hv"


def test_switched_plate_pin_is_wired_on_sink_0_and_spare_elsewhere():
    """Sink 0 declares the plate pin wired; every other sink keeps it as an
    ``unused`` spare; no sink offers it as a pad_driver block, and the
    default channels_per_sink is the part less the plate channel."""
    from precis.pcb import generators

    params = _switched_plate_args()["generators"][0]["params"]
    del params["sink_grid"]["channels_per_sink"]
    exp = generators.expand("ewod_pad_array", "ARR1", params)
    sinks = [c for c in exp.components if c["refdes"].startswith("ARR1_SINK_")]
    assert sinks
    for comp in sinks:
        decl = {p["name"]: p for p in comp["pins"]}["OUT15"]
        assert bool(decl.get("unused")) == (comp["refdes"] != "ARR1_SINK_0")
        assert ["OUT15"] not in comp["supplies"]["blocks"]
        assert len(comp["supplies"]["blocks"]) == 15
    # 32 driven electrodes at 15 assignable channels -> 3 sinks, not 2.
    assert len(sinks) == 3


def test_switched_plate_channels_per_sink_counts_the_withheld_pin(pcb):
    with pytest.raises(BadInput, match="less top_plate_channel"):
        pcb.put(id="ewod-bad", args=_switched_plate_args(channels_per_sink=16))


def test_switched_plate_shares_the_polarity_net_with_every_pixel_sink(pcb):
    """'invert plate and pixel to avoid droplet polarization': the sink
    carrying the plate channel and every sink carrying pixel channels sit
    on ONE polarity net, so a single POL edge flips all of them."""
    pcb.put(id="ewod-plate-pol", args=_switched_plate_args())
    ref = pcb.store.get_ref(kind="pcb", id="ewod-plate-pol")
    assert ref is not None
    graph = pcb.store.pcb_graph(ref.id)
    sinks = {i["refdes"] for i in graph["instances"] if "_SINK_" in i["refdes"]}
    assert len(sinks) > 1
    assert _members(graph, "ARR1_pol") == {(s, "POL") for s in sinks}


def test_switched_plate_without_pol_pin_is_refused(pcb):
    args = _switched_plate_args()
    del args["generators"][0]["params"]["sink_grid"]["pol_pin"]
    with pytest.raises(BadInput, match="needs pol_pin"):
        pcb.put(id="ewod-bad", args=args)


def test_switched_plate_and_fixed_rail_together_are_refused(pcb):
    with pytest.raises(BadInput, match="pick one"):
        pcb.put(id="ewod-bad", args=_switched_plate_args(top_plate_pin="CPLT"))


def test_switched_plate_channel_must_be_a_channel_pin(pcb):
    with pytest.raises(BadInput, match="not one of channel_pins"):
        pcb.put(id="ewod-bad", args=_switched_plate_args(top_plate_channel="VPP"))


def test_hv_sink_put_succeeds_and_check_shows_the_intra_footprint_warn(pcb, store):
    # Needs a REAL cached footprint: the synthesized sink in _sink_args has
    # overlapping made-up pads, so every pair there is a different-pad error.
    from tests.test_pcb_island_terminal_polygon import (
        _RING_LCSC,
        _RING_PINS,
        _ring_design,
        _ring_footprint,
    )

    # x_half=9.0, not 8.5: at the seed pose the east ring's VPP land sits
    # 0.392mm from the col-7 escape via, and that cross-footprint pair is
    # held to the 0.4mm hv floor (an error — see test_pcb_drc.py).
    store.part_footprint_put(
        _RING_LCSC, _ring_footprint(_RING_PINS, per_side=20, x_half=9.0, y_half=11.5)
    )
    design = _ring_design()  # carries drive_voltage_v=250
    pcb.put(id="ewod-hv-chk", args=design)
    resp = pcb.get(id="ewod-hv-chk", view="check")
    assert "fixed by the part" in resp.body


def test_tape_land_explicit_offset_below_the_creepage_floor_is_refused(pcb):
    # default gap 0.10mm -> 1.5*gap = 0.15mm < the 250V 0.4mm floor.
    with pytest.raises(BadInput, match="creepage floor"):
        pcb.put(
            id="ewod-bad",
            args=_array_args(
                grid=[6, 6],
                drive_voltage_v=250,
                tape_land={"edge": "N", "offset_mm": 0},
            ),
        )


def test_tape_land_default_offset_clears_the_electrodes_by_the_floor(pcb):
    from precis.pcb.capabilities import conductor_spacing_mm

    pcb.put(
        id="ewod-tape-hv",
        args=_array_args(grid=[6, 6], drive_voltage_v=250, tape_land={"edge": "N"}),
    )
    ref = pcb.store.get_ref(kind="pcb", id="ewod-tape-hv")
    assert ref is not None
    land = pcb.store.pcb_generators_for(ref.id)["ARR1"]["ledger"]["tape_land"]
    pitch, gap, rows = 2.25, 0.10, 6  # generator defaults
    electrode_edge = rows * pitch / 2.0 - gap / 2.0
    inner_edge = land["y"] - land["h"] / 2.0
    floor = conductor_spacing_mm(250.0, layer="external", coated=True)
    assert inner_edge - electrode_edge >= floor - 1e-6


def test_class_rules_refuses_a_voltage_past_the_table(pcb):
    pcb.put(id="ewod-hv", args=_array_args(grid=[3, 3]))
    with pytest.raises(BadInput, match="top band"):
        pcb.put(
            id="ewod-hv",
            args={
                "op": "class_rules",
                "name": "hv",
                "rules": {"working_voltage_v": 600},
            },
        )


def test_fab_bundle_carries_the_notes_file_with_the_parylene_mask_note(pcb, tmp_path):
    # No sink_grid here: the synthetic sink part has no cached footprint in
    # this fixture and the fab bundle rightly refuses synthesized pads.
    pcb.put(id="ewod-tape", args=_array_args(grid=[6, 6], tape_land={"edge": "N"}))
    resp = pcb.get(id="ewod-tape", view="gerber", args={"dir": str(tmp_path)})
    assert "SynthesizedPadError" not in resp.body
    zpath = tmp_path / "ewod-tape-fab.zip"
    with zipfile.ZipFile(zpath) as zf:
        names = set(zf.namelist())
        notes = zf.read("ewod-tape-fab-notes.txt").decode("utf-8")
    assert "ewod-tape-fab-notes.txt" in names
    assert "parylene process order" in notes  # the pre-existing EWOD note
    assert "mask the copper-tape landing" in notes


def test_reserve_marks_a_plaza_slot_unusable(pcb):
    pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3], reserve=["P1_1:N"]))
    ref = pcb.store.get_ref(kind="pcb", id="ewod-gen-1")
    assert ref is not None
    ledger = pcb.store.pcb_generators_for(ref.id)["ARR1"]["ledger"]
    assert ledger["pads"]["R0C1"]["usable"] is False
    assert ledger["plazas"]["P1_1"]["slots"]["N"]["status"] == "reserved"
    # the reserved electrode gets no via/stub copper (suppressed, counted
    # in ledger['fabric'] -- test_pcb_ewod_fabric.py's own coverage), but
    # its electrode BODY still exists as copper (still wired -- just
    # unroutable for now).
    assert ledger["fabric"]["tiles"]["P1_1"]["suppressed"] == 1
    board = pcb.store.pcb_load(ref.id)["board"]
    assert board is not None
    fixed = pcb.store.pcb_fixed_copper_list(int(board["board_id"]))
    assert not any(r["net"] == "ARR1_R0C1" for r in fixed)
    net_names = {n["name"] for n in pcb.store.pcb_graph(ref.id)["nets"]}
    assert "ARR1_R0C1" in net_names  # still wired -- just unroutable for now


# ── export honesty ───────────────────────────────────────────────────────
def test_gerber_export_of_the_generated_array_never_raises_synthesized(pcb, tmp_path):
    pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3]))
    resp = pcb.get(id="ewod-gen-1", view="gerber", args={"dir": str(tmp_path)})
    assert "SynthesizedPadError" not in resp.body
    zpath = tmp_path / "ewod-gen-1-fab.zip"
    assert zpath.exists()
    with zipfile.ZipFile(zpath) as zf:
        names = set(zf.namelist())
        assert "ewod-gen-1-F_Cu.gbr" in names
        f_cu = zf.read("ewod-gen-1-F_Cu.gbr").decode("utf-8")
        assert "G36*" in f_cu and "G37*" in f_cu  # the polygon electrodes
        # role=electrode -> paste=none is asserted directly on the pad data
        # in test_local_footprint_carries_one_electrode_body_pad_per_pin;
        # solderpaste_gerber always writes both side files regardless of
        # content (round-1's own test avoided proving this negative on
        # gerber content directly, for the same reason). The plaza vias'
        # own drill hits (pcb-pre-place-route-blocks Slice 2, now real
        # ``pcb_fixed_copper`` rows) are covered board-wide by
        # ``tests/test_pcb_ewod_dogfood.py``'s gerber export test.


def test_svg_fab_level_render_does_not_crash_on_the_generated_array(pcb):
    pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3]))
    resp = pcb.get(id="ewod-gen-1", view="svg", args={"level": "fab"})
    assert "<svg" in resp.body


def test_drc_view_runs_the_full_pass_once_the_fabric_is_real_copper(pcb):
    """Round-4 contract (docs/backlog/pcb-ewod-multitile.md's decisions
    log) widened ``view='drc'`` to run a PADS-ONLY pass whenever real
    (non-synthesized) pad geometry exists but ``pcb_copper_list`` is
    still empty — the motivating case being a standalone
    ``ewod_pad_array`` board, since its escape geometry used to be
    footprint-pad copper the router never touches. **pcb-pre-place-
    route-blocks Slice 2 retires that motivating case**: the escape
    fabric is now real ``pcb_fixed_copper`` (track + via) rows, which
    ``pcb_copper_list`` unions in — so this board now has realized copper
    the moment ``generators:[...]`` is applied, before ``op='route'``
    ever runs, and gets the FULL geometric DRC pass, not the reduced
    pads-only one. Every escape net still reads ``'unrouted'`` (the
    router bookkeeping status is unrelated to whether the NET's copper
    exists — a fanout-1 net with a fixed-copper via landing has nothing
    left for a router to draw, see docs/backlog/pcb-pre-place-route-
    blocks.md's router-posture note) — a real, correctly-reported status,
    not a defect. The rule-level clearance/annular-ring/via-keepout
    coverage against REAL generator copper lives in
    ``test_pcb_ewod_generator_drc.py``, driving ``precis.pcb.drc``
    directly; this test stays the handler-path smoke + contract check."""
    pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3]))
    resp = pcb.get(id="ewod-gen-1", view="drc")
    assert "no realized copper yet" not in resp.body
    assert "(pads-only DRC — no routed copper yet)" not in resp.body
    assert "unrouted" in resp.body


def test_electrode_nets_get_a_dedicated_gap_net_class(pcb):
    pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3], gap=0.12))
    ref = pcb.store.get_ref(kind="pcb", id="ewod-gen-1")
    assert ref is not None
    design = pcb.store.pcb_load(ref.id)
    # `clearance_mm` is `gap` minus a small geometry-rounding safety
    # margin (precis.pcb.generators._GEOMETRY_ROUNDING_SLACK_MM) — never
    # the raw authored value (see that constant's own docstring).
    assert design["net_classes"]["ewod_ARR1"]["clearance_mm"] == pytest.approx(0.119)
    # Rulings 2026-09-19 item 7: every pin in a 3x3 full array has a plaza
    # via/stub (one centre plaza, all 8 electrodes usable), so EVERY
    # electrode net is actually tagged with the ESCAPE class (same
    # clearance floor, plus the B.Cu layer lock), not the plain
    # electrode-gap class this test used to check directly.
    assert design["net_classes"]["ewod_ARR1_escape"]["clearance_mm"] == pytest.approx(
        0.119
    )
    assert design["net_classes"]["ewod_ARR1_escape"]["layers"] == ["B.Cu"]
    nets_by_name = {n["name"]: n for n in pcb.store.pcb_graph(ref.id)["nets"]}
    assert nets_by_name["ARR1_R0C0"]["net_class"] == "ewod_ARR1_escape"


def test_a_pin_with_no_via_stays_on_the_plain_gap_class(pcb):
    """Rulings 2026-09-19 item 7: the B.Cu layer lock only makes sense for
    a pin that actually got a plaza via/stub — one that didn't (here,
    reserved out of its only slot, same fixture as
    ``test_reserve_marks_a_plaza_slot_unusable``) has no B.Cu track to
    lock and must stay on the plain electrode-gap class, never the escape
    one."""
    pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3], reserve=["P1_1:N"]))
    ref = pcb.store.get_ref(kind="pcb", id="ewod-gen-1")
    assert ref is not None
    nets_by_name = {n["name"]: n for n in pcb.store.pcb_graph(ref.id)["nets"]}
    assert nets_by_name["ARR1_R0C1"]["net_class"] == "ewod_ARR1"
    assert nets_by_name["ARR1_R0C0"]["net_class"] == "ewod_ARR1_escape"


# ── parylene-coater extent (gr414481) ───────────────────────────────────
def test_drc_warns_coating_extent_unknown_on_an_ewod_board(pcb, monkeypatch):
    monkeypatch.setattr("precis.handlers.pcb.coating_extent_mm", lambda: None)
    pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3]))
    body = pcb.get(id="ewod-gen-1", view="drc").body
    assert "coating_extent_unknown" in body
    assert "ARR1" in body


def test_drc_with_a_configured_coating_extent_drops_the_unknown_warning(
    pcb, monkeypatch
):
    monkeypatch.setattr("precis.handlers.pcb.coating_extent_mm", lambda: (500.0, 500.0))
    pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3]))
    body = pcb.get(id="ewod-gen-1", view="drc").body
    assert "coating_extent_unknown" not in body

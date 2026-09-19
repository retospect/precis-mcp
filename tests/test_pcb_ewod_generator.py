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
    footprint pads (see the sibling test above)."""
    pcb.put(id="ewod-gen-1", args=_array_args(grid=[3, 3]))
    ref = pcb.store.get_ref(kind="pcb", id="ewod-gen-1")
    assert ref is not None
    board = pcb.store.pcb_load(ref.id)["board"]
    assert board is not None
    rows = pcb.store.pcb_fixed_copper_list(int(board["board_id"]))
    tracks = [r for r in rows if r["ctype"] == "track"]
    vias = [r for r in rows if r["ctype"] == "via"]
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
    nets_by_name = {n["name"]: n for n in pcb.store.pcb_graph(ref.id)["nets"]}
    assert nets_by_name["ARR1_R0C0"]["net_class"] == "ewod_ARR1"

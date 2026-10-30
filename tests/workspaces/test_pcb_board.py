"""pcb-workspace-code slice 0 acceptance (docs/backlog/pcb-workspace-code.md,
Slices row 0): the ``precis_geom.pcb`` builder emits the ``pcb_apply``
batch shape; ``examples/boards/*/main.py`` rebuild the reference boards
and are byte-equal to the fixtures they came from (nano, nano_r2,
esp32c3); ``b.use`` refuses a reused prefix and an un-handed net;
``b.check()`` on nano lists exactly ``meta.synthesized_footprint_refdes``
as NOT-CHECKED; tier-1 goldens (canonical sha + counts) pin all four
boards ("byte-equal" means canonical-JSON-equal, ``pcb.canonical_json``:
sorted keys, compact separators — authored dict order does not matter);
trace keys are stable across two builds on structure-workspaces slice
2's ``precis_geom.trace``.

The kernel import-boundary belt (no first-party, stdlib only) covers
``precis_geom.pcb`` alone — the package-wide wheel boundary is the
import-linter contract (the carved ``precis`` subset the wheel
force-includes; ruling 2026-10-24, structure-workspaces §Rulings).

Regenerate the goldens after an intended change (then read the diff)::

    uv run python -c "import sys, json; sys.path.insert(0, 'tests/workspaces'); \\
      import test_pcb_board as t; t.GOLDEN.write_text(json.dumps({n: \\
      t._golden_row(t._example(n)) for n in t.EXAMPLES}, indent=1, \\
      sort_keys=True) + '\\n', encoding='utf-8')"
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

import pytest

import precis_geom.pcb as pcb
from precis_geom.pcb import Board, Footprint, PcbBuilderError, module, pad, rect
from tests.workspaces._script import load_module

ROOT = Path(__file__).resolve().parents[2]
BOARDS = ROOT / "examples" / "boards"
FIXTURES = ROOT / "tests" / "fixtures" / "pcb"
GOLDEN = Path(__file__).resolve().parent / "data" / "pcb_goldens.json"

#: example dir → the fixture whose dict it must reproduce byte for byte
CONVERTED = {
    "nano": "nano_usb_pd_testboard.py:build_design",
    "nano_usb_pd": "nano_usb_pd_testboard.py:build_design",
    "nano_usb_pd_r2": "nano_usb_pd_testboard_r2.py:build_design",
    "esp32c3": "esp32c3_reference.json",
}
EXAMPLES = (*CONVERTED, "sanity", "blinky_led", "usbc_attiny")


def _example(name: str) -> Board:
    return load_module(BOARDS / name / "main.py", f"example_{name}").build()


def _fixture(spec: str) -> dict[str, Any]:
    if spec.endswith(".json"):
        return json.loads((FIXTURES / spec).read_text(encoding="utf-8"))
    file, _, func = spec.partition(":")
    return getattr(load_module(FIXTURES / file, f"fixture_{file[:-3]}"), func)()


def _golden_row(b: Board) -> dict[str, Any]:
    batch = b.to_batch()
    check = b.check()
    return {
        "sha256": pcb.content_hash(batch),
        "parts": len(batch["components"]),
        "nets": len(batch["nets"]),
        "connections": len(batch["connections"]),
        "findings": [f_.code for f_ in check.findings],
        "not_checked": check.not_checked,
    }


# --- goldens -----------------------------------------------------------------


@pytest.mark.parametrize("name", list(CONVERTED))
def test_converted_example_is_byte_equal_to_its_fixture(name: str) -> None:
    got = _example(name).to_batch()
    want = _fixture(CONVERTED[name])
    assert got == want
    assert pcb.canonical_json(got) == pcb.canonical_json(want)


@pytest.mark.parametrize("name", EXAMPLES)
def test_tier1_golden(name: str) -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    row = _golden_row(_example(name))
    assert row == golden[name], (
        f"{name}: golden drift — if intended, regenerate "
        "tests/workspaces/data/pcb_goldens.json with the one-liner in this "
        f"module's docstring; got {json.dumps(row, indent=1)}"
    )


def test_golden_file_covers_every_example_dir() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    dirs = sorted(p.name for p in BOARDS.iterdir() if (p / "main.py").is_file())
    assert dirs == sorted(golden) == sorted(EXAMPLES)


def test_nano_check_not_checked_matches_synthesized_footprint_refdes() -> None:
    b = _example("nano_usb_pd")
    result = b.check()
    assert sorted(result.not_checked) == sorted(b.meta["synthesized_footprint_refdes"])
    assert result.findings == []
    assert "NOT-CHECKED" in result.summary()


def test_build_is_deterministic_across_two_builds() -> None:
    for name in EXAMPLES:
        assert _example(name).to_batch() == _example(name).to_batch()


def test_trace_keys_stable_across_two_builds() -> None:
    from precis_geom import trace

    keys = []
    for _ in range(2):
        with trace.run(work_root=BOARDS) as tr:
            _example("sanity")
            doc = tr.dump()
        keys.append([f["key"] for f in doc["frames"]])
        quals = {f["qual"] for f in doc["frames"]}
        assert {"pcb.Board.part", "pcb.Board.wire", "pcb.Board.footprint"} <= quals
    assert keys[0] == keys[1]
    assert len(keys[0]) > 20
    # schema 2: the re-anchor fields ride along on every frame.
    assert all("line_in_def" in f and "src" in f for f in doc["frames"])
    assert any(f["src"] for f in doc["frames"] if f["qual"].startswith("pcb."))


def test_manifest_and_write(tmp_path: Path) -> None:
    b = _example("esp32c3")
    man = b.manifest(requests=[{"op": "place", "key": "k", "params": {"seed": 1}}])
    assert man["schema"] == pcb.MANIFEST_SCHEMA and man["kind"] == "pcb"
    assert man["board"] == b.to_batch()
    path = pcb.write_manifest(b, tmp_path / "out")
    assert json.loads(path.read_text(encoding="utf-8"))["board"] == b.to_batch()


# --- builder units -----------------------------------------------------------


def test_board_emits_only_what_was_said() -> None:
    assert Board("bare").to_batch() == {"components": [], "nets": [], "connections": []}
    b = Board(
        "t1", profile="prototype", stackup="2l", outline=rect(30, 20, corner_mm=2)
    )
    batch = b.to_batch()
    assert batch["profile"] == "prototype"
    assert batch["meta"] == {"stackup_preset": "2l"}
    assert batch["features"] == [
        {
            "ftype": "outline",
            "geom": {
                "path": [[0, 0], [30, 0], [30, 20], [0, 20], [0, 0]],
                "corner_radius_mm": 2,
            },
        }
    ]
    with pytest.raises(PcbBuilderError):
        b.outline(rect(1, 1))


def test_stackup_count_sides_and_point_outline() -> None:
    b = Board(
        "t2",
        stackup=3,
        sides="top",
        outline=[(0, 0), (10, 0), (10, 10)],
    )
    batch = b.to_batch()
    assert batch["meta"] == {
        "stackup_layers": ["F.Cu", "In1.Cu", "B.Cu"],
        "placement_sides": "top",
    }
    assert batch["features"][0]["geom"]["path"] == [[0, 0], [10, 0], [10, 10]]


def test_stackup_forms_and_layer_indices() -> None:
    """gr514459: ``stackup=`` is a count or a preset, omitted means the
    semantic 4-layer default; copper layers are addressed by index."""
    assert Board("bare").layer_names == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert "stackup_layers" not in Board("bare").to_batch().get("meta", {})
    assert Board("t2l", stackup=2).layer_names == ["F.Cu", "B.Cu"]
    assert Board("t2p", stackup="2l").layer_names == ["F.Cu", "B.Cu"]

    legacy_names: Any = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    with pytest.raises(PcbBuilderError, match="stackup=4"):
        Board("bad", stackup=legacy_names)
    with pytest.raises(PcbBuilderError):
        Board("bad", stackup=1)
    with pytest.raises(PcbBuilderError):
        Board("bad", stackup=True)

    b = Board("t8")
    gnd = b.net("GND")
    assert b.plane(1, net=gnd, priority=1)["layer"] == "In1.Cu"
    assert b.plane(-1, net=gnd)["layer"] == "B.Cu"
    with pytest.raises(PcbBuilderError, match="index 1"):
        b.plane("In1.Cu", net=gnd)
    with pytest.raises(PcbBuilderError, match="out of range"):
        b.plane(4, net=gnd)
    assert b.keepout([[0, 0], [1, 0], [1, 1]], layers=[0])["layers"] == ["F.Cu"]


def test_part_pins_connections_shape() -> None:
    b = Board("t3")
    gnd = b.net("GND", cls="ground")
    vbus = b.net("VBUS", cls="power", current=0.5, voltage=5.0)
    j1 = b.part("J1", lcsc="C165948", roles={"connector"})
    vbus.tie(j1["VBUS"])
    gnd.tie(j1["GND"], note="shell")
    r1 = b.part("R1", value="5.1k", pins=["1", "2"])
    b.wire(j1["CC1"], r1["1"], name="CC1")
    gnd.tie(r1[2])
    batch = b.to_batch()
    assert batch["components"] == [
        {"refdes": "J1", "part": "C165948", "roles": ["connector"], "pins": []},
        {"refdes": "R1", "value": "5.1k", "pins": [{"name": "1"}, {"name": "2"}]},
    ]
    assert batch["connections"] == [
        {"net": "VBUS", "refdes": "J1", "pin": "VBUS"},
        {"net": "GND", "refdes": "J1", "pin": "GND", "note": "shell"},
        {"net": "CC1", "refdes": "J1", "pin": "CC1"},
        {"net": "CC1", "refdes": "R1", "pin": "1"},
        {"net": "GND", "refdes": "R1", "pin": "2"},
    ]
    assert batch["nets"] == [
        {"name": "GND", "class": "ground"},
        {"name": "VBUS", "class": "power", "current": 0.5, "working_voltage_v": 5.0},
        {"name": "CC1"},
    ]


def test_pin_markers_tags_facts_emit_store_shapes() -> None:
    b = Board("t4")
    p = b.part("U1", pins=[{"name": "2", "unused": True}, {"name": "7", "pad": "B3"}])
    p["NC1"].nc()
    p["RST"].must_terminate().tag("reset")
    p["SHUNT"].current_role("shunt").etype("power_in")
    pins = {pin["name"]: pin for pin in p.to_dict()["pins"]}
    assert pins["2"] == {"name": "2", "unused": True}
    assert pins["7"] == {"name": "7", "pad": "B3"}
    assert pins["NC1"] == {"name": "NC1", "nc": True}
    assert pins["RST"] == {"name": "RST", "must_terminate": True, "tags": ["reset"]}
    assert pins["SHUNT"]["etype"] == "power_in"  # top-level, the store's door
    assert pins["SHUNT"]["meta"] == {"current_role": "shunt"}
    assert list(pins) == ["2", "7", "NC1", "RST", "SHUNT"]


def test_at_leash_on_edge_group_offsets() -> None:
    b = Board("t5")
    p1 = b.part("J1").at(2.0, 10.0, rot=90, fixed="rot").on_edge("left")
    p2 = b.part("J2").leash(max_travel_mm=1.5)
    b.group("hdrs", [p1, p2], offsets=[(0.0, 0.0, 90), (15.24, 0.0, 90)])
    d1, d2 = p1.to_dict(), p2.to_dict()
    assert (d1["x"], d1["rot"], d1["fixed"], d1["layer"]) == (2.0, 90, "rot", "top")
    assert d1["edge_mate"] == {"face": "left", "require": "required"}
    assert d1["group"] == d2["group"] == "hdrs"
    assert d2["group_offset"] == {"x": 15.24, "y": 0.0, "rot": 90}
    assert d2["max_travel_mm"] == 1.5
    with pytest.raises(PcbBuilderError):
        b.group("bad", [p1, p2], offsets=[(0, 0, 0)])


def test_net_domain_is_electrical_only() -> None:
    b = Board("t6")
    assert b.net("E", domain="electrical").to_dict() == {
        "name": "E",
        "domain": "electrical",
    }
    with pytest.raises(PcbBuilderError):
        b.net("BRACKET", domain="mechanical")


def test_bad_kwarg_quotes_the_params_doc_line() -> None:
    """gr520905 push half: a PcbBuilderError for a bad kwarg appends that
    param's Args line from the method docstring."""
    b = Board("kw1")
    with pytest.raises(PcbBuilderError) as ei:
        b.net("BRACKET", domain="mechanical")
    msg = str(ei.value)
    assert "must be electrical" in msg  # the original message is kept
    assert "domain: Net domain" in msg  # and the param's doc line quotes
    # A second failure on another param quotes its own doc line.
    b.net("VBUS")
    with pytest.raises(PcbBuilderError, match="name: Net name"):
        b.net("VBUS")


def test_missing_kwarg_error_quotes_doc_too() -> None:
    """The semantic-missing raise (`pin()` with neither arg) quotes the
    param doc the same way."""
    p = Board("kw2").part("U1")
    with pytest.raises(PcbBuilderError, match="needs a pin number"):
        p.pin()


def test_param_doc_parse_reads_google_args() -> None:
    """`_param_docs` reads `name (type): doc` entries; wrapped lines
    join, a missing Args block yields {}."""

    def f(a: int, b: str = "x") -> None:
        """Summary.

        Args:
            a (int): First line. continuation line.
            b (str): Bee.
        """

    assert pcb._param_docs(f) == {
        "a": "First line. continuation line.",
        "b": "Bee.",
    }

    def g(x: int) -> None:
        """No sections."""

    assert pcb._param_docs(g) == {}


def test_part_kwarg_catalog_ref() -> None:
    """gr514215: ``part=`` takes a catalog-ref — lcsc, the footprint name
    (its ``package``) and ``height_mm`` come off the CatalogPart; an lcsc
    string is read through ``b.catalog`` so the manifest can record it."""
    cp = pcb.CatalogPart(
        lcsc="C25804",
        resolved=True,
        package="0402",
        height_mm=0.5,
        pins=["1", "2"],
        part_id="pn1",
        footprint_sha="abc123",
        read_at="2026-10-26T00:00:00+00:00",
    )
    b = Board("t7b")
    c1 = b.part("C1", part=cp)
    assert c1.lcsc == "C25804" and c1.footprint == "0402"
    assert c1.height_mm == 0.5
    row = b.to_batch()["components"][0]
    assert row["part"] == "C25804" and row["footprint"] == "0402"
    # the read's manifest entry binds to THIS board
    assert b.manifest()["parts"] == [
        {
            "lcsc": "C25804",
            "part_id": "pn1",
            "footprint_sha": "abc123",
            "read_at": "2026-10-26T00:00:00+00:00",
        }
    ]
    # explicit kwargs win over the ref
    c2 = b.part("C2", part=cp, footprint="0603", height_mm=1.0)
    assert c2.footprint == "0603" and c2.height_mm == 1.0
    with pytest.raises(PcbBuilderError, match="lcsc="):
        b.part("C3", part=cp, lcsc="C9")
    # an lcsc string goes through b.catalog — unresolved with no sandbox
    # client, but the refdes still carries the C-number (and is counted
    # as catalog-checked, not NOT-CHECKED)
    c4 = b.part("C4", part="C999")
    assert c4.lcsc == "C999" and c4.footprint is None
    assert "C4" not in b.check().not_checked
    # an unresolved CatalogPart leaves footprint unset but still sets lcsc
    c5 = b.part("C5", part=pcb.CatalogPart(lcsc="C7"))
    assert c5.lcsc == "C7" and c5.footprint is None
    assert b.manifest()["parts"] == [
        {
            "lcsc": "C25804",
            "part_id": "pn1",
            "footprint_sha": "abc123",
            "read_at": "2026-10-26T00:00:00+00:00",
        }
    ]


def test_refdes_net_and_footprint_reuse_refused() -> None:
    b = Board("t7")
    b.part("U1")
    with pytest.raises(PcbBuilderError):
        b.part("U1")
    b.net("GND")
    with pytest.raises(PcbBuilderError):
        b.net("GND")
    fp = Footprint("X", [pad("1", "circle", 0, 0, w=1)])
    b.footprint(fp)
    b.footprint(fp)
    with pytest.raises(PcbBuilderError):
        b.footprint(Footprint("X", []))


def test_mechanical_features_and_keepout() -> None:
    b = Board("t8")
    gnd = b.net("GND")
    b.hole(3, 3, drill=2.2, ring=4.0)
    b.fiducial(1, 1)
    b.testpoint(gnd, x=5, y=5)
    b.corner_nuts()
    b.keepout([[0, 0], [1, 0], [1, 1]], layers=[0], reason="RF")
    b.plane(1, net=gnd, priority=1)
    b.feature("mask_open", geom={"poly": [[0, 0]]})
    feats = b.to_batch()["features"]
    assert [f_["ftype"] for f_ in feats] == [
        "mounting_hole",
        "fiducial",
        "testpoint",
        "corner_nuts",
        "keepout",
        "plane_net",
        "mask_open",
    ]
    assert feats[0] == {
        "ftype": "mounting_hole",
        "x": 3,
        "y": 3,
        "geom": {"diameter": 2.2, "ring_dia_mm": 4.0, "plated": True},
    }


def test_measures_net_class_role_generator() -> None:
    b = Board("t9")
    u, c = b.part("U1"), b.part("C1")
    b.near(u, c, within_mm=2.0)
    b.apart(u, c, at_least_mm=10.0)
    b.align(u, c, axis="x")
    b.net_class("usb", width_mm=0.2, pairs=[["D+", "D-"]])
    role = b.role("vbus", voltage=5.0)
    u.supplies(role, pins=["VOUT"])
    u["VIN"].client_of(role)
    g = b.generate("pad_array", generator="ewod_pad_array", rows=4, cols=4)
    batch = b.to_batch()
    assert [(m["metric"], m["operands"]) for m in batch["measures"]] == [
        ("near", ["U1", "C1"]),
        ("apart", ["U1", "C1"]),
        ("align", ["U1", "C1"]),
    ]
    assert batch["net_classes"]["usb"]["pairs"] == [["D+", "D-"]]
    assert batch["roles"]["vbus"] == {"name": "vbus", "voltage": 5.0}
    assert batch["components"][0]["roles"] == ["vbus"]
    assert batch["generators"] == [g]


def test_footprint_registration_order_and_dedup() -> None:
    b = Board("t11")
    late = Footprint("LATE", [pad("1", "rect", 0, 0, w=1, h=1)])
    fp = Footprint("HDR-2", [pad("1", "circle", -1.27, 0, w=1.7, drill=0.9)])
    b.footprint(late)
    b.part("J1", footprint=fp)
    b.part("J2", footprint=fp)
    b.part("J3", footprint="MISSING")
    batch = b.to_batch()
    assert [f_["name"] for f_ in batch["footprints"]] == ["LATE", "HDR-2"]
    assert [c["footprint"] for c in batch["components"]] == [
        "HDR-2",
        "HDR-2",
        "MISSING",
    ]
    assert [(f_.code, f_.refdes) for f_ in b.check().findings] == [
        ("footprint.unknown", "J3")
    ]


def test_module_namespacing_and_refusals() -> None:
    b = Board("t13")
    gnd, vin, vout = b.net("GND"), b.net("VIN"), b.net("VOUT")

    @module
    def ldo(board: Board, *, vin: pcb.Net, vout: pcb.Net, gnd: pcb.Net) -> pcb.Part:
        u = board.part("U")
        cin = board.part("CIN")
        fb = board.net("FB")
        vin.tie(u["VIN"])
        vout.tie(u["VOUT"])
        gnd.tie(u["GND"], cin["2"])
        fb.tie(u["FB"])
        board.wire(u["X"], cin["1"])
        return u

    u2 = b.use(ldo, prefix="U2", vin=vin, vout=vout, gnd=gnd)
    assert u2.refdes == "U2.U"
    batch = b.to_batch()
    assert {"U2.U", "U2.CIN"} <= {c["refdes"] for c in batch["components"]}
    assert {"U2/FB", "U2/U2_U_X__U2_CIN_1"} <= {n["name"] for n in batch["nets"]}
    assert {"net": "U2/FB", "refdes": "U2.U", "pin": "FB"} in batch["connections"]

    with pytest.raises(PcbBuilderError, match="prefix 'U2' already used"):
        b.use(ldo, prefix="U2", vin=vin, vout=vout, gnd=gnd)

    stray = b.net("STRAY")

    @module
    def bad(board: Board, *, vin: pcb.Net) -> pcb.Part:
        u = board.part("X")
        stray.tie(u["A"])
        return u

    with pytest.raises(PcbBuilderError, match="not handed as a port"):
        b.use(bad, prefix="U3", vin=vin)


def test_check_findings() -> None:
    b = Board("t14")
    u1 = b.part("U1", lcsc="C1")
    b.part("R1", pins=["1", "2"])
    b.net("FLOAT")
    b.net("GND").tie(u1["GND"])
    result = b.check()
    assert result.not_checked == ["R1"]
    assert [(f_.code, f_.net) for f_ in result.findings] == [
        ("net.unconnected", "FLOAT"),
        ("pin.unwired", None),
        ("pin.unwired", None),
    ]
    # R1's declared pair is wired to nothing and carries no marker.
    assert [(f_.refdes, f_.pin) for f_ in result.findings[1:]] == [
        ("R1", "1"),
        ("R1", "2"),
    ]


def test_nets_of_pin_nets_and_pins_on() -> None:
    """gr513764: ``nets_of`` is the part's sorted net names;
    ``pin_nets`` is the pin → net map; ``pins_on`` is the net's pins."""
    b = Board("t15")
    gnd, vcc = b.net("GND"), b.net("VCC")
    u1, r1, c1 = b.part("U1"), b.part("R1"), b.part("C1")
    vcc.tie(u1["VDD"])
    gnd.tie(u1["VSS"], r1["2"], c1["1"])
    b.wire(u1["OUT"], r1["1"], name="SIG")
    assert b.nets_of("U1") == ["GND", "SIG", "VCC"]
    assert b.pin_nets("U1") == {"VDD": "VCC", "VSS": "GND", "OUT": "SIG"}
    assert list(b.pin_nets("U1")) == ["VDD", "VSS", "OUT"]  # pin order
    assert b.pin_nets("R1") == {"1": "SIG", "2": "GND"}
    assert b.nets_of("R1") == ["GND", "SIG"]
    assert b.pins_on("GND") == [("U1", "VSS"), ("R1", "2"), ("C1", "1")]
    assert gnd.pins() == b.pins_on("GND")
    assert b.pins_on(gnd) == b.pins_on("GND")
    with pytest.raises(PcbBuilderError, match="3 parts"):
        b.nets_of("Q9")
    with pytest.raises(PcbBuilderError, match="3 parts"):
        b.pin_nets("Q9")
    with pytest.raises(PcbBuilderError, match="3 nets"):
        b.pins_on("NOPE")


def test_nets_of_pins_on_nano_usb_pd() -> None:
    b = _example("nano_usb_pd")
    u1 = b.pin_nets("U1")
    assert len(u1) == 29 and len(set(u1.values())) == 28  # V5 lands on two pins
    assert len(b.nets_of("U1")) == 28
    assert b.pins_on("GND") == [
        (c["refdes"], c["pin"])
        for c in b.to_batch()["connections"]
        if c["net"] == "GND"
    ]


def test_check_not_checked_groups() -> None:
    b = Board("t16")
    for i in range(1, 9):
        b.part(f"R{i}")
    for i in range(1, 4):
        b.part(f"C{i}", value="100nF")
    b.part("C4", value="1uF")
    b.part("U1", lcsc="C2040")  # checked — never lands in the groups
    result = b.check()
    groups = result.not_checked_groups
    assert sum(len(v) for v in groups.values()) == len(result.not_checked)
    r_key = "no lcsc, no authored footprint (synthesized landpattern): R"
    assert groups[r_key] == [f"R{i}" for i in range(1, 9)]
    assert groups[r_key[:-1] + "C 100nF"] == ["C1", "C2", "C3"]
    assert groups[r_key[:-1] + "C 1uF"] == ["C4"]
    s = result.summary()
    assert "8 x R: R1, R2, R3, R4, R5, R6, +2 more" in s
    assert "3 x C 100nF: C1, C2, C3" in s
    assert "fix: pcb.catalog / b.footprint(...) / lcsc=" in s


def test_nano_check_groups_shorter_than_the_refdes_wall() -> None:
    b = _example("nano_usb_pd")
    result = b.check()
    groups = result.not_checked_groups
    assert sum(len(v) for v in groups.values()) == 42
    assert [k.rsplit(": ", 1)[1] for k in groups] == ["R", "C", "LED"]
    assert "+16 more" in result.summary()  # the 22-strong R group is capped
    wall = (
        f"{len(result.findings)} finding(s), {len(result.not_checked)} "
        "NOT-CHECKED (no catalog part, no footprint)\n  NOT-CHECKED: "
        + ", ".join(result.not_checked)
    )
    s = result.summary()
    assert "NOT-CHECKED" in s and len(s) < len(wall)


# --- kernel boundary (mirrors test_precis_flow.py) --------------------------

_GEOM_SRC = ROOT / "src" / "precis_geom"
_FORBIDDEN_PREFIXES = (
    "precis.",
    "precis_se",
    "precis_web",
    "asa_bot",
    "asa_slack",
    "hexfold",
)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            out.append(node.module)
    return out


def test_precis_geom_pcb_imports_nothing_first_party_and_only_stdlib() -> None:
    offenders: list[str] = []
    for name in _imports(_GEOM_SRC / "pcb.py"):
        root = name.split(".")[0]
        if root == "precis_geom":
            continue
        if name == "precis" or name.startswith(_FORBIDDEN_PREFIXES):
            offenders.append(f"pcb.py: {name} (first-party)")
        elif root not in sys.stdlib_module_names:
            offenders.append(f"pcb.py: {name} (third-party)")
    assert not offenders, (
        "precis_geom.pcb is the DB-free, stdlib-only board builder "
        "(structure-workspaces §Packaging). The package-wide wheel "
        "boundary is the import-linter contract — the carved precis "
        f"subset the wheel force-includes (ruling 2026-10-24): {offenders}"
    )


def test_pcb_builder_has_no_importlinter_root_leak() -> None:
    """`precis_geom.pcb` must import cleanly without `precis` loaded."""
    assert "precis_geom.pcb" in sys.modules
    assert not any(
        m == "precis" or m.startswith("precis.") for m in _imports(_GEOM_SRC / "pcb.py")
    )

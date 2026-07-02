"""Exporters + Freerouting round-trip (ADR 0042 §6, Slice 6).

Pure exporter coverage (BOM grouping, CPL coordinate/rotation conversion,
KiCad netlist, Specctra DSN structure, the mechanical 0041-bridge profile),
plus the handler export/route views over a live store. The router itself is
gated: with no Freerouting backend the round-trip degrades to a .dsn-only pass
(asserted), and a stub backend drives the routed path.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import export, route

# ── a small placed board (centroid coords in mm) ─────────────────────
_MODEL = {
    "instances": [
        {
            "refdes": "U1",
            "label": "ESP32-C3",
            "part_lcsc": "C2838500",
            "footprint": "QFN-32",
            "layer": "top",
            "x": 10.0,
            "y": 10.0,
            "rot": 90.0,
            "pins": ["VDD", "GND", "SCL"],
        },
        {
            "refdes": "C1",
            "label": "100nF 0402",
            "part_lcsc": "C1525",
            "footprint": "0402",
            "layer": "top",
            "x": 11.5,
            "y": 10.0,
            "rot": 0.0,
            "pins": ["1", "2"],
        },
        {
            "refdes": "C2",
            "label": "100nF 0402",
            "part_lcsc": "C1525",
            "footprint": "0402",
            "layer": "bottom",
            "x": 12.0,
            "y": 11.0,
            "rot": 0.0,
            "pins": ["1", "2"],
        },
        {
            "refdes": "R1",
            "label": "4.7k 0402",
            "part_lcsc": "C25900",
            "footprint": "0402",
            "layer": "top",
            "x": 13.0,
            "y": 10.0,
            "rot": 0.0,
            "pins": ["1", "2"],
        },
    ],
    "nets": [
        {
            "name": "VCC3V3",
            "net_class": "power",
            "width_mm": 0.4,
            "members": [{"refdes": "U1", "pin": "VDD"}, {"refdes": "C1", "pin": "1"}],
        },
        {
            "name": "I2C_SCL",
            "net_class": "i2c",
            "width_mm": None,
            "members": [{"refdes": "U1", "pin": "SCL"}, {"refdes": "R1", "pin": "1"}],
        },
    ],
}


# ── BOM ──────────────────────────────────────────────────────────────
def test_bom_groups_identical_parts():
    csv = export.bom_csv(_MODEL)
    lines = csv.strip().splitlines()
    assert lines[0] == "Comment,Designator,Footprint,LCSC Part #"
    # C1 and C2 are the same part → one BOM row, both designators.
    cap_rows = [ln for ln in lines if "C1525" in ln]
    assert len(cap_rows) == 1
    assert '"C1,C2"' in cap_rows[0] or "C1,C2" in cap_rows[0]
    assert "C2838500" in csv and "C25900" in csv


# ── CPL / pick-and-place ─────────────────────────────────────────────
def test_jlc_rotation_negates_cw_to_ccw():
    # internal CW-from-north → JLCPCB CCW: 90 → 270, 0 → 0, 270 → 90.
    assert export.jlc_rotation(0) == 0
    assert export.jlc_rotation(90) == 270
    assert export.jlc_rotation(270) == 90
    # bottom side: viewed from the bottom the mirror cancels the negation and
    # adds the flip — (rot + 180) % 360, the Fabrication-Toolkit mapping.
    assert export.jlc_rotation(0, bottom=True) == 180
    assert export.jlc_rotation(45, bottom=True) == 225
    assert export.jlc_rotation(270, bottom=True) == 90


def test_cpl_columns_layer_and_rotation():
    csv = export.cpl_csv(_MODEL)
    lines = csv.strip().splitlines()
    assert lines[0] == "Designator,Mid X,Mid Y,Layer,Rotation"
    u1 = next(ln for ln in lines if ln.startswith("U1,"))
    assert "10.0000,10.0000" in u1 and u1.endswith(",Top,270")
    c2 = next(ln for ln in lines if ln.startswith("C2,"))
    # bottom-side part: rot 0 → 180 (mirrored), NOT the top-side identity
    assert c2.endswith(",Bottom,180")


def test_unplaced_and_missing_lcsc():
    m = {
        "instances": [
            {"refdes": "U1", "x": 1.0, "y": 1.0, "part_lcsc": "C1"},
            {"refdes": "J1", "x": None, "y": None, "part_lcsc": None},
        ],
        "nets": [],
    }
    assert export.unplaced(m) == ["J1"]
    assert export.missing_lcsc(m) == ["J1"]


# ── KiCad netlist ────────────────────────────────────────────────────
def test_kicad_netlist_has_components_and_nets():
    net = export.kicad_netlist(_MODEL, name="sensor")
    assert '(comp (ref "U1")' in net
    assert '(net (code "1") (name "I2C_SCL")' in net or '(name "I2C_SCL")' in net
    assert '(node (ref "U1") (pin "SCL"))' in net


# ── Specctra DSN ─────────────────────────────────────────────────────
def test_dsn_structure_and_units():
    dsn = export.specctra_dsn(_MODEL, name="sensor")
    assert dsn.startswith("(pcb sensor")
    assert "(resolution um 10)" in dsn
    assert "(structure" in dsn and "(placement" in dsn
    assert "(library" in dsn and "(network" in dsn
    # (resolution um 10) = 10 units/µm, so coords are mm × 10000 (10mm → 100000)
    assert "(place U1 100000 100000 front 270)" in dsn
    assert "(place C2 120000 110000 back 0)" in dsn
    # widths/clearances scale with the same resolution (0.25mm → 2500 units)
    assert "(rule (width 2500) (clearance 2000))" in dsn
    # the network carries the named nets
    assert "(net VCC3V3 (pins" in dsn and "(net I2C_SCL (pins" in dsn


def test_dsn_placeholder_pins_do_not_overlap():
    # without cached footprints, a multi-pin part's pins spread (never all 0,0)
    dsn = export.specctra_dsn(_MODEL, name="s")
    # U1 image has 3 pins on a centred row at 1mm pitch → offsets -10000,0,10000
    assert "-10000 0" in dsn and "10000 0" in dsn


def test_dsn_uses_real_footprint_geometry_when_cached():
    fps = {
        "C1525": {
            "pads": [{"n": "1", "x": -0.5, "y": 0.0}, {"n": "2", "x": 0.5, "y": 0.0}],
            "pin_map": {"1": "1", "2": "2"},
        }
    }
    dsn = export.specctra_dsn(_MODEL, footprints=fps, name="s")
    # real 0.5mm pad offsets in 0.1µm units
    assert "-5000 0" in dsn and "5000 0" in dsn


def test_dsn_outline_overrides_bbox():
    dsn = export.specctra_dsn(
        _MODEL, outline=[[0, 0], [20, 0], [20, 15], [0, 15]], name="s"
    )
    # 'pcb' is the reserved outline layer id (the rect fallback's token too)
    assert "(boundary (path pcb 0 0 0 200000 0 200000 150000 0 150000))" in dsn


# ── mechanical profile (0041 bridge) ─────────────────────────────────
def test_mechanical_profile_outline_and_holes():
    features: list[dict[str, Any]] = [
        {"ftype": "outline", "geom": {"path": [[0, 0], [30, 0], [30, 20], [0, 20]]}},
        {"ftype": "mounting_hole", "x": 2.0, "y": 2.0, "geom": {"diameter": 3.2}},
    ]
    prof = export.mechanical_profile(_MODEL, features)
    assert prof["outline"] == [[0, 0], [30, 0], [30, 20], [0, 20]]
    assert prof["holes"] == [{"x": 2.0, "y": 2.0, "diameter": 3.2}]
    assert prof["thickness_mm"] == export.DEFAULT_THICKNESS_MM
    assert {b["refdes"] for b in prof["blocks"]} == {"U1", "C1", "C2", "R1"}


def test_mechanical_profile_falls_back_to_bbox():
    prof = export.mechanical_profile(_MODEL, [])
    assert len(prof["outline"]) == 4  # bbox rectangle from placed parts


def test_export_model_attaches_pins_and_filters_empty_nets():
    load = {
        "instances": [
            {
                "refdes": "U1",
                "label": "u",
                "part_lcsc": "C1",
                "footprint": "fp",
                "layer": "top",
                "x": 1.0,
                "y": 2.0,
                "rot": 0.0,
                "fixed": None,
                "roles": [],
                "note": None,
            },
        ],
        "nets": [{"name": "N1", "net_class": "sig", "width_mm": 0.3}],
    }
    graph = {
        "instances": [{"refdes": "U1"}],
        "nets": [
            {
                "name": "N1",
                "net_class": "sig",
                "members": [{"refdes": "U1", "pin": "A"}],
            },
            {"name": "EMPTY", "net_class": None, "members": []},
        ],
        "unconnected": [{"refdes": "U1", "pin": "B"}],
    }
    model = export.export_model(load, graph)
    assert model["instances"][0]["pins"] == ["A", "B"]  # connected ∪ unconnected
    assert [n["name"] for n in model["nets"]] == ["N1"]  # empty net dropped
    assert model["nets"][0]["width_mm"] == 0.3


# ── Freerouting wrapper (gated) ──────────────────────────────────────
def test_route_dsn_skips_without_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("PRECIS_FREEROUTING_JAR", raising=False)
    monkeypatch.delenv("PRECIS_FREEROUTING_BIN", raising=False)
    dsn = tmp_path / "x.dsn"
    dsn.write_text(export.specctra_dsn(_MODEL, name="x"))
    res = route.route_dsn(dsn)
    assert res.skipped and not res.ok and res.ses is None


def _stub_router(tmp_path: Path, *, unrouted: int) -> str:
    """A fake freerouting CLI: writes the .ses given by -do, prints a count."""
    script = tmp_path / "frstub.sh"
    script.write_text(
        "#!/bin/sh\n"
        'out=""\n'
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = "-do" ]; then out="$2"; shift; fi\n'
        "  shift\n"
        "done\n"
        'echo "(session)" > "$out"\n'
        f'echo "{unrouted} incomplete connections"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR)
    return str(script)


def test_route_dsn_ok_with_stub(tmp_path, monkeypatch):
    monkeypatch.setenv("PRECIS_FREEROUTING_BIN", _stub_router(tmp_path, unrouted=0))
    dsn = tmp_path / "x.dsn"
    dsn.write_text("(pcb x)")
    res = route.route_dsn(dsn)
    assert res.ok and res.unrouted == 0 and res.ses is not None
    assert res.ses.exists()


def test_route_dsn_incomplete_with_stub(tmp_path, monkeypatch):
    monkeypatch.setenv("PRECIS_FREEROUTING_BIN", _stub_router(tmp_path, unrouted=3))
    dsn = tmp_path / "x.dsn"
    dsn.write_text("(pcb x)")
    res = route.route_dsn(dsn)
    assert not res.ok and res.unrouted == 3


def test_round_trip_bounded_and_degrades(tmp_path, monkeypatch):
    monkeypatch.delenv("PRECIS_FREEROUTING_JAR", raising=False)
    monkeypatch.delenv("PRECIS_FREEROUTING_BIN", raising=False)
    calls = {"place": 0}

    def place_fn(iters, seed):
        calls["place"] += 1
        return {"crossings_after": 0}

    rt = route.place_route_round_trip(
        lambda: _MODEL,
        place_fn,
        lambda m: export.specctra_dsn(m, name="s"),
        tmp_path,
        max_passes=3,
        name="s",
    )
    # no router → one place, .dsn written, loop stops immediately (skipped)
    assert calls["place"] == 1
    assert rt.dsn.exists() and rt.route.skipped and rt.passes == 1


def test_round_trip_clamps_zero_passes(tmp_path, monkeypatch):
    # max_passes=0 (reachable via args={'max_passes':'0'}) must still run one
    # pass — the .dsn write lives inside the loop, so zero passes used to
    # report a .dsn path that was never written.
    monkeypatch.delenv("PRECIS_FREEROUTING_JAR", raising=False)
    monkeypatch.delenv("PRECIS_FREEROUTING_BIN", raising=False)
    rt = route.place_route_round_trip(
        lambda: _MODEL,
        lambda i, s: {"crossings_after": 0},
        lambda m: export.specctra_dsn(m, name="s"),
        tmp_path,
        max_passes=0,
        name="s",
    )
    assert rt.passes == 1 and rt.dsn.exists()


def test_round_trip_succeeds_with_stub(tmp_path, monkeypatch):
    monkeypatch.setenv("PRECIS_FREEROUTING_BIN", _stub_router(tmp_path, unrouted=0))
    rt = route.place_route_round_trip(
        lambda: _MODEL,
        lambda i, s: {"crossings_after": 0},
        lambda m: export.specctra_dsn(m, name="s"),
        tmp_path,
        max_passes=3,
        name="s",
    )
    assert rt.ok and rt.passes == 1 and rt.route.ses is not None


# ── handler views over a live store ──────────────────────────────────
@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


_BOARD = {
    "components": [
        {
            "refdes": "U1",
            "label": "ESP32-C3",
            "part": "C2838500",
            "footprint": "QFN-32",
            "x": 10.0,
            "y": 10.0,
            "rot": 90.0,
            "height_mm": 3.1,
            "pins": [{"name": "VDD"}, {"name": "GND"}, {"name": "SCL"}],
        },
        {
            "refdes": "C1",
            "label": "100nF 0402",
            "part": "C1525",
            "footprint": "0402",
            "x": 11.5,
            "y": 10.0,
            "pins": [{"name": "1"}, {"name": "2"}],
        },
    ],
    "nets": [{"name": "VCC3V3", "class": "power"}, {"name": "GND", "class": "gnd"}],
    "connections": [
        {"net": "VCC3V3", "refdes": "U1", "pin": "VDD"},
        {"net": "VCC3V3", "refdes": "C1", "pin": "1"},
        {"net": "GND", "refdes": "U1", "pin": "GND"},
        {"net": "GND", "refdes": "C1", "pin": "2"},
    ],
    "features": [
        {"ftype": "outline", "geom": {"path": [[0, 0], [20, 0], [20, 20], [0, 20]]}},
        {"ftype": "mounting_hole", "x": 2.0, "y": 2.0, "geom": {"diameter": 3.2}},
    ],
}


def test_handler_bom_and_cpl_views(pcb, tmp_path):
    pcb.put(id="sensor", args=_BOARD)
    bom = pcb.get(id="sensor", view="bom", args={"dir": str(tmp_path)})
    assert "exported sensor → BOM" in bom.body
    assert (tmp_path / "sensor.csv").exists()
    cpl = pcb.get(id="sensor", view="cpl", args={"dir": str(tmp_path)})
    assert "Top,270" in (tmp_path / "sensor.csv").read_text()  # U1 rot 90 → 270
    assert "exported sensor → CPL" in cpl.body


def test_handler_dsn_and_netlist_views(pcb, tmp_path):
    pcb.put(id="sensor", args=_BOARD)
    dsn = pcb.get(id="sensor", view="dsn", args={"dir": str(tmp_path)})
    body = (tmp_path / "sensor.dsn").read_text()
    # outline feature drove the boundary (reserved 'pcb' layer id, 0.1µm units)
    assert "(boundary (path pcb 0 0 0 200000 0 200000 200000 0 200000))" in body
    assert "exported sensor → DSN" in dsn.body
    pcb.get(id="sensor", view="netlist", args={"dir": str(tmp_path)})
    assert (tmp_path / "sensor.net").exists()


def test_handler_mechanical_view(pcb, tmp_path):
    pcb.put(id="sensor", args=_BOARD)
    pcb.get(id="sensor", view="mechanical", args={"dir": str(tmp_path)})
    import json

    prof = json.loads((tmp_path / "sensor.json").read_text())
    assert prof["holes"][0]["diameter"] == 3.2
    assert len(prof["outline"]) == 4
    # component height must survive store → export_model → profile (the 0041
    # lid keep-out); pcb_load dropping it made every block height None.
    u1 = next(b for b in prof["blocks"] if b["refdes"] == "U1")
    assert u1["height_mm"] == 3.1


def test_handler_route_view_degrades(pcb, tmp_path, monkeypatch):
    monkeypatch.delenv("PRECIS_FREEROUTING_JAR", raising=False)
    monkeypatch.delenv("PRECIS_FREEROUTING_BIN", raising=False)
    pcb.put(id="sensor", args=_BOARD)
    resp = pcb.get(id="sensor", view="route", args={"dir": str(tmp_path), "iters": 50})
    assert "No Freerouting backend" in resp.body
    assert (tmp_path / "sensor.dsn").exists()

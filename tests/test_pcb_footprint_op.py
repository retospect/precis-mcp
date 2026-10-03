"""gr341532 fix 3 — ``put(op='footprint')`` / ``get(view='footprints')``.

Prod's ``part_footprints`` cache had no rows for the EWOD dogfood board's
two catalog parts (HV507PG-G = C639448, and the I2C temp sensor — then a
placeholder C32254, now TMP112 = C28927) and there was no MCP-facing way
to fill it —
a fetch-and-cache helper existed but nothing in
:class:`precis.handlers.pcb.PcbHandler` ever called it (confirmed: no
production caller anywhere in the tree before this fix, only tests). This
exercises the two ways to close that gap — pull (with the fetcher
monkeypatched, no network) and author directly — plus the read-side
``view='footprints'`` and the ``synthesized_footprint`` DRC finding it
closes.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import drc as pcb_drc
from precis.pcb import footprint as pcb_footprint_mod
from precis.pcb import realize as pcb_realize
from precis.pcb import session as pcb_session
from tests.test_pcb_ewod_dogfood import (
    _HV507_LCSC,
    _HV507_PINS,
    _TEMP_SENSOR_LCSC,
    _TEMP_SENSOR_PINS,
    _design,
    _grid_footprint,
    _seed,
)

_LCSC = "C639448"


def _fake_footprint(*, source: str = "easyeda:packageDetail:test") -> dict:
    return {
        "pads": [
            {
                "number": "1",
                "shape": "RECT",
                "x": 0.0,
                "y": 0.0,
                "w": 0.5,
                "h": 0.5,
                "rot": 0.0,
                "layer": "F.Cu",
                "drill": None,
            },
            {
                "number": "2",
                "shape": "RECT",
                "x": 1.0,
                "y": 0.0,
                "w": 0.5,
                "h": 0.5,
                "rot": 0.0,
                "layer": "F.Cu",
                "drill": None,
            },
        ],
        "pin_map": {
            "1": {"name": "A", "tags": []},
            "2": {"name": "B", "tags": []},
        },
        "courtyard": {"bbox": [-0.25, -0.25, 1.25, 0.25]},
        "centroid": {"x": 0.5, "y": 0.0},
        "source": source,
        "raw": {},
    }


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _minimal_design(lcsc: str = _LCSC) -> dict:
    return {
        "components": [
            {
                "refdes": "U1",
                "label": "test part",
                "part": lcsc,
                "pins": [{"name": "A"}, {"name": "B"}],
            }
        ],
        "nets": [],
        "connections": [],
    }


# ── (a) pull, monkeypatched fetcher, no network ──────────────────────────


def test_op_footprint_pulls_and_caches_via_fetcher(pcb, monkeypatch):
    monkeypatch.setattr(
        pcb_footprint_mod, "_easyeda_fetch", lambda lcsc: _fake_footprint()
    )
    pcb.put(id="fp1", args=_minimal_design())

    resp = pcb.put(id="fp1", args={"op": "footprint", "part": _LCSC})

    assert _LCSC in resp.body
    assert "yes" in resp.body  # cached column
    assert "2" in resp.body  # n_pads / n_pins
    row = pcb.store.part_footprint_get(_LCSC)
    assert row is not None
    assert len(row["pads"]) == 2
    assert row["source"] == "easyeda:packageDetail:test"


def test_op_footprint_parts_batch_and_force(pcb, monkeypatch):
    calls: list[str] = []

    def fake(lcsc: str) -> dict:
        calls.append(lcsc)
        return _fake_footprint(source=f"easyeda:packageDetail:{lcsc}")

    monkeypatch.setattr(pcb_footprint_mod, "_easyeda_fetch", fake)
    pcb.put(id="fp1", args=_minimal_design())
    pcb.put(id="fp1", args={"op": "footprint", "part": "C222222"})

    resp = pcb.put(id="fp1", args={"op": "footprint", "parts": [_LCSC, "C222222"]})
    assert _LCSC in resp.body and "C222222" in resp.body
    # C222222 was already cached (not force) -> only ONE fetch happened for
    # it (the earlier op='footprint' call above), plus one for C639448 now.
    assert calls.count(_LCSC) == 1
    assert calls.count("C222222") == 1

    resp2 = pcb.put(
        id="fp1", args={"op": "footprint", "part": "C222222", "force": True}
    )
    assert "yes" in resp2.body
    assert calls.count("C222222") == 2  # force re-pulled despite the cache hit


# ── (b) a failing fetcher reports per-part, never raises ────────────────


def test_op_footprint_failed_pull_reports_error_not_raise(pcb, monkeypatch):
    def boom(lcsc: str) -> dict:
        raise RuntimeError("vendor unreachable")

    monkeypatch.setattr(pcb_footprint_mod, "_easyeda_fetch", boom)
    pcb.put(id="fp1", args=_minimal_design())

    resp = pcb.put(id="fp1", args={"op": "footprint", "part": _LCSC})

    assert "vendor unreachable" in resp.body
    assert pcb.store.part_footprint_get(_LCSC) is None


def test_op_footprint_batch_one_bad_one_good(pcb, monkeypatch):
    def flaky(lcsc: str) -> dict | None:
        if lcsc == "CBAD":
            raise RuntimeError("404-ish vendor error")
        return _fake_footprint()

    monkeypatch.setattr(pcb_footprint_mod, "_easyeda_fetch", flaky)
    pcb.put(id="fp1", args=_minimal_design())

    resp = pcb.put(id="fp1", args={"op": "footprint", "parts": [_LCSC, "CBAD"]})
    assert "1/2 cached" in resp.body
    assert pcb.store.part_footprint_get(_LCSC) is not None
    assert pcb.store.part_footprint_get("CBAD") is None
    assert "404-ish vendor error" in resp.body


# ── (c) author a footprint directly ──────────────────────────────────────


def test_op_footprint_authors_directly(pcb):
    pcb.put(id="fp1", args=_minimal_design(lcsc="C900001"))

    resp = pcb.put(
        id="fp1",
        args={
            "op": "footprint",
            "part": "C900001",
            "footprint": {
                "pads": [
                    {"pin": "1", "shape": "rect", "x": 0.0, "y": 0.0, "w": 0.5},
                    {"pin": "2", "shape": "rect", "x": 1.0, "y": 0.0, "w": 0.5},
                ]
            },
        },
    )
    assert "authored" in resp.body
    row = pcb.store.part_footprint_get("C900001")
    assert row is not None
    assert row["source"] == "authored"
    assert len(row["pads"]) == 2


def test_op_footprint_authoring_rejects_malformed_shape(pcb):
    pcb.put(id="fp1", args=_minimal_design(lcsc="C900002"))

    with pytest.raises(BadInput, match="needs at least one pad"):
        pcb.put(
            id="fp1",
            args={"op": "footprint", "part": "C900002", "footprint": {"pads": []}},
        )
    assert pcb.store.part_footprint_get("C900002") is None


def test_op_footprint_authoring_needs_exactly_one_part(pcb):
    pcb.put(id="fp1", args=_minimal_design())

    with pytest.raises(BadInput):
        pcb.put(
            id="fp1",
            args={
                "op": "footprint",
                "parts": [_LCSC, "C222222"],
                "footprint": {"pads": [{"pin": "1", "x": 0, "y": 0, "w": 0.5}]},
            },
        )


def test_op_footprint_needs_part_or_footprint(pcb):
    pcb.put(id="fp1", args=_minimal_design())
    with pytest.raises(BadInput):
        pcb.put(id="fp1", args={"op": "footprint"})


# ── (d) view='footprints' ─────────────────────────────────────────────────


def test_view_footprints_lists_cached_catalog_parts_on_dogfood(pcb):
    slug = _seed(pcb)  # caches both C639448 and C28927 via part_footprint_put
    resp = pcb.get(id=slug, view="footprints")

    assert _HV507_LCSC in resp.body
    assert _TEMP_SENSOR_LCSC in resp.body
    assert "2/2 catalog-part instance(s) cached" in resp.body


def test_view_footprints_shows_uncached_on_a_design_seeded_without_footprints(pcb):
    design = _design()
    pcb.put(id="ewod-no-footprints", args=design)  # no part_footprint_put calls

    resp = pcb.get(id="ewod-no-footprints", view="footprints")

    assert _HV507_LCSC in resp.body
    assert _TEMP_SENSOR_LCSC in resp.body
    assert "0/2 catalog-part instance(s) cached" in resp.body


# ── (e) op='footprint' closes the synthesized_footprint DRC finding ──────


@pytest.mark.slow
def test_op_footprint_fill_clears_synthesized_footprint_drc_finding(pcb, monkeypatch):
    design = _design()
    pcb.put(id="ewod-no-footprints-2", args=design)

    before = pcb.get(id="ewod-no-footprints-2", view="drc")
    assert "synthesized_footprint" in before.body

    def fake(lcsc: str) -> dict | None:
        if lcsc == _HV507_LCSC:
            return _grid_footprint(_HV507_PINS, cols=9)
        if lcsc == _TEMP_SENSOR_LCSC:
            return _grid_footprint(_TEMP_SENSOR_PINS, cols=3)
        return None

    monkeypatch.setattr(pcb_footprint_mod, "_easyeda_fetch", fake)
    resp = pcb.put(
        id="ewod-no-footprints-2",
        args={"op": "footprint", "parts": [_HV507_LCSC, _TEMP_SENSOR_LCSC]},
    )
    assert "2/2 cached" in resp.body

    after = pcb.get(id="ewod-no-footprints-2", view="drc")
    assert "synthesized_footprint" not in after.body


# ── (f) a declared pin name with no pad on a CACHED footprint ────────────


def _misnamed_design() -> dict:
    """The dogfood design with U_TEMP's supply declared ``VDD`` — the name
    the old SOIC-8 placeholder used — while the TMP112's footprint calls it
    ``V+``."""
    design = _design()
    for comp in design["components"]:
        if comp["refdes"] == "U_TEMP":
            comp["pins"] = [
                {"name": "VDD" if p["name"] == "V+" else p["name"]}
                for p in comp["pins"]
            ]
    for conn in design["connections"]:
        if conn.get("refdes") == "U_TEMP" and conn["pin"] == "V+":
            conn["pin"] = "VDD"
    return design


def test_put_names_a_pin_that_matches_no_pad_on_the_cached_footprint(pcb):
    pcb.store.part_footprint_put(
        _TEMP_SENSOR_LCSC, _grid_footprint(_TEMP_SENSOR_PINS, cols=3)
    )

    resp = pcb.put(id="ewod-misnamed", args=_misnamed_design())

    assert "U_TEMP: declared VDD;" in resp.body
    assert "V+" in resp.body  # the footprint's own name, beside the wrong one
    view = pcb.get(id="ewod-misnamed", view="footprints")
    assert "U_TEMP: declared VDD;" in view.body


def test_put_is_silent_when_every_pin_name_matches_the_cached_footprint(pcb):
    pcb.store.part_footprint_put(
        _TEMP_SENSOR_LCSC, _grid_footprint(_TEMP_SENSOR_PINS, cols=3)
    )

    resp = pcb.put(id="ewod-named", args=_design())

    assert "match no pad" not in resp.body


def test_drc_reports_a_misnamed_pin_apart_from_a_missing_footprint(pcb):
    # U_TEMP's footprint is cached (one pin misnamed); the sink's is not.
    pcb.store.part_footprint_put(
        _TEMP_SENSOR_LCSC, _grid_footprint(_TEMP_SENSOR_PINS, cols=3)
    )
    pcb.put(id="ewod-misnamed-drc", args=_misnamed_design())
    ref_id = pcb.store.get_ref(kind="pcb", id="ewod-misnamed-drc").id
    ir = pcb._build_ir(ref_id, pcb.store.pcb_graph(ref_id))
    footprints = pcb_session.footprints_by_refdes(
        ir, pcb.store.pcb_footprints_for(ref_id)
    )
    pads = pcb_realize.pads_for_ir(ir, ["F.Cu", "B.Cu"], footprints)

    flagged = {(p["refdes"], p["pin"]) for p in pads if p.get("pin_unmatched")}
    assert flagged == {("U_TEMP", "VDD")}

    by_rule: dict[str, set[str]] = {}
    for f in pcb_drc.check_synthesized_footprint({"pads": pads}):
        by_rule.setdefault(f.rule, set()).add(f.where)
    assert by_rule["pin_name_mismatch"] == {"part U_TEMP"}
    assert "part U_TEMP" not in by_rule.get("synthesized_footprint", set())
    assert by_rule["synthesized_footprint"]  # the uncached sink still reads as one

"""Slice 2 of docs/backlog/pcb-lazy-netlist-and-checks.md — every footprint
pad is a declared pin, with the §1j intent markers.

The defect class (gr339236, gr346744, Defect A of
``docs/backlog/ewod-controller-and-hv-supply.md``): a pad no pin declared
was invisible to the netlist — no name, no swap id, nothing to reason
about. The fix is in three parts, each with coverage here:

1. ``_pcb_apply`` completes a component's pins from the cached
   footprint's ``pin_map`` (and ``_expand_ewod_pad_array`` declares every
   channel pin itself, marking the unwired ones ``unused``).
2. The §1j markers — ``nc`` (part-level, never a complaint), ``unused``
   (design-level, no ``unconnected_pin`` complaint but full geometry and
   matcher-available), ``must_terminate`` (part-level, ``error`` when
   netless even if ``unused``).
3. The safety net: ``undeclared_pad`` (a footprint pad no pin claims, or
   a part whose footprint was never cached — reported as NOT-CHECKED,
   never a clean pass) and the phase-aware ``unconnected_pin`` (``warn``
   while the board is being worked, ``error`` at ``fab``).
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import drc
from precis.pcb.capabilities import capability_for

pytestmark = pytest.mark.db


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _outline() -> dict[str, Any]:
    return {
        "ftype": "outline",
        "geom": {"path": [[-15, -15], [15, -15], [15, 15], [-15, 15]]},
    }


#: An 8-pad local footprint: pin names exercise every marker path —
#: SIG/VDD/GND wired, SPARE completed-unmarked (the "you forgot?" case),
#: AUX + AUX2 author-marked ``unused`` in the component's own ``pins``
#: (design-level; deliberately ADJACENT, so one track can be pushed
#: through both), MT tagged ``must_terminate`` on the footprint itself
#: (part-level, propagates through completion), NC1 tagged ``nc`` on the
#: footprint (datasheet no-connect).
_MINI_FP = {
    "name": "MINI8",
    "pads": [
        {"number": "1", "shape": "rect", "x": -3.0, "y": 0, "w": 0.6, "h": 0.6},
        {"number": "2", "shape": "rect", "x": -2.0, "y": 0, "w": 0.6, "h": 0.6},
        {"number": "3", "shape": "rect", "x": -1.0, "y": 0, "w": 0.6, "h": 0.6},
        {"number": "4", "shape": "rect", "x": 0.0, "y": 0, "w": 0.6, "h": 0.6},
        {"number": "5", "shape": "rect", "x": 1.0, "y": 0, "w": 0.6, "h": 0.6},
        {"number": "6", "shape": "rect", "x": 2.0, "y": 0, "w": 0.6, "h": 0.6},
        {"number": "7", "shape": "rect", "x": 3.0, "y": 0, "w": 0.6, "h": 0.6},
        {"number": "8", "shape": "rect", "x": 4.0, "y": 0, "w": 0.6, "h": 0.6},
    ],
    "pin_map": {
        "1": "SIG",
        "2": "SPARE",
        "3": "AUX",
        "4": "AUX2",
        "5": "VDD",
        "6": {"name": "MT", "tags": ["must_terminate"]},
        "7": {"name": "NC1", "tags": ["nc"]},
        "8": "GND",
    },
}


def _mini_component(refdes: str, x: float) -> dict[str, Any]:
    return {
        "refdes": refdes,
        "label": "mini",
        "footprint": "MINI8",
        "x": x,
        "y": 0.0,
        "pins": [
            {"name": "SIG"},
            {"name": "VDD"},
            {"name": "GND"},
            {"name": "AUX", "unused": True},
            {"name": "AUX2", "unused": True},
        ],
    }


def _mini_design() -> dict[str, Any]:
    """Two MINI7 instances wired pad-to-pad on three nets — enough that no
    net is dangling, enough that completion and the markers have four
    distinct cases to resolve on each part."""
    return {
        "footprints": [_MINI_FP],
        # 16 mm of centre spacing keeps two 6 mm-wide packages clear of
        # each other's pads and courtyards (the judged put refuses a board
        # this close).
        "components": [_mini_component("U1", -8.0), _mini_component("U2", 8.0)],
        "nets": [{"name": n} for n in ("SIG", "VDD", "GND")],
        "connections": [
            {"net": "SIG", "refdes": "U1", "pin": "SIG"},
            {"net": "SIG", "refdes": "U2", "pin": "SIG"},
            {"net": "VDD", "refdes": "U1", "pin": "VDD"},
            {"net": "VDD", "refdes": "U2", "pin": "VDD"},
            {"net": "GND", "refdes": "U1", "pin": "GND"},
            {"net": "GND", "refdes": "U2", "pin": "GND"},
        ],
        "features": [_outline()],
    }


def _unconnected(store: Any, ref_id: int) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(u["refdes"]), str(u["pin"])): u
        for u in store.pcb_graph(ref_id)["unconnected"]
    }


# ── unit level: the two checks and the phase ladder ────────────────────


def test_derive_phase_truth_table() -> None:
    assert drc.derive_phase(placed=False, routed=False) == "netlist"
    assert drc.derive_phase(placed=True, routed=False) == "placed"
    assert drc.derive_phase(placed=True, routed=True) == "routed"
    # `fab` is never derived — it is an authored claim (Slice 8's op='phase').
    assert "fab" not in {
        drc.derive_phase(placed=p, routed=r)
        for p in (False, True)
        for r in (False, True)
    }


def test_unconnected_pin_warns_at_netlist_and_errors_at_fab() -> None:
    pins = [{"refdes": "U1", "pin": "SPARE", "tags": [], "meta": {}}]
    for phase in ("netlist", "placed", "routed"):
        # Aggregated per instance below fab — one warn names every spare.
        findings = drc.check_unconnected_pins(pins, phase=phase)
        assert [f.severity for f in findings] == ["warn"]
        assert findings[0].rule == "unconnected_pin"
        assert findings[0].where == "part U1"
        assert findings[0].objects[0]["pins"] == ("SPARE",)
        assert "SPARE" in findings[0].detail
    # At fab each pin is its own error row.
    fab = drc.check_unconnected_pins(pins, phase="fab")
    assert [(f.where, f.severity) for f in fab] == [("pin U1/SPARE", "error")]


def test_unconnected_pin_aggregates_one_instance_not_the_board() -> None:
    pins = [
        {"refdes": "U1", "pin": f"P{i}", "tags": [], "meta": {}} for i in range(30)
    ] + [{"refdes": "U2", "pin": "Q1", "tags": [], "meta": {}}]
    findings = drc.check_unconnected_pins(pins, phase="netlist")
    assert [(f.where, f.severity) for f in findings] == [
        ("part U1", "warn"),
        ("part U2", "warn"),
    ]
    assert len(findings[0].objects[0]["pins"]) == 30
    # A 30-name listing stays readable — first 12, then a count.
    assert "… +18" in findings[0].detail


def test_must_terminate_pin_errors_even_when_unused() -> None:
    pins = [
        {
            "refdes": "U1",
            "pin": "MT",
            "tags": [],
            "meta": {"must_terminate": True, "unused": True},
        }
    ]
    for phase in drc.PHASES:
        findings = drc.check_unconnected_pins(pins, phase=phase)
        assert [f.severity for f in findings] == ["error"], phase
        assert findings[0].rule == "unconnected_pin"


def test_nc_and_unused_pins_never_produce_an_unconnected_complaint() -> None:
    pins = [
        {"refdes": "U1", "pin": "NC1", "tags": ["nc"], "meta": {}},
        {"refdes": "U1", "pin": "AUX", "tags": [], "meta": {"unused": True}},
        # Same words as bare tags (a footprint pin_map tag lands here too).
        {"refdes": "U2", "pin": "M", "tags": ["must_terminate"], "meta": {}},
    ]
    for phase in drc.PHASES:
        findings = drc.check_unconnected_pins(pins, phase=phase)
        # Only the must_terminate-via-tag pin ever appears; nc and unused
        # never do, at any phase.
        assert [(f.where, f.severity) for f in findings] == [("pin U2/M", "error")], (
            phase
        )


def test_undeclared_pad_fires_on_an_unpinned_footprint_pad() -> None:
    model = {
        "layers": ["F.Cu"],
        "pads": [
            {
                "net": "",
                "refdes": "U1",
                "pin": "NC1",
                "pad_number": "6",
                "shape": "rect",
                "x": 0,
                "y": 0,
                "w": 0.5,
                "h": 0.5,
                "layer": "F.Cu",
                "undeclared": True,
            },
            {
                "net": "SIG",
                "refdes": "U1",
                "pin": "SIG",
                "shape": "rect",
                "x": 1,
                "y": 0,
                "w": 0.5,
                "h": 0.5,
                "layer": "F.Cu",
            },
        ],
    }
    findings = drc.check_undeclared_pads(model)
    assert [f.rule for f in findings] == ["undeclared_pad"]
    assert findings[0].severity == "warn"
    assert "U1/6" in findings[0].where


def test_undeclared_pad_reports_not_checked_never_a_clean_pass() -> None:
    findings = drc.check_undeclared_pads({"pads": []}, unchecked=["U2", "U9"])
    assert len(findings) == 1
    assert findings[0].rule == "undeclared_pad"
    assert findings[0].severity == "warn"
    assert "U2" in findings[0].detail and "U9" in findings[0].detail
    assert "CANNOT be checked" in findings[0].detail


# ── store level: declare-all completion ────────────────────────────────


def test_every_footprint_pad_is_a_declared_pin(pcb, store) -> None:
    pcb.put(id="d-mini", args=_mini_design())
    ref = store.get_ref(kind="pcb", id="d-mini")
    assert ref is not None
    for refdes in ("U1", "U2"):
        pins = store.pcb_instance_neighbors(ref.id, refdes)["pins"]
        assert {p["pin"] for p in pins} == {
            "SIG",
            "SPARE",
            "AUX",
            "AUX2",
            "VDD",
            "MT",
            "NC1",
            "GND",
        }, f"{refdes}: every footprint pad must be a declared pin"

    # Markers: the author's own `unused` decl lands as meta; the
    # footprint's pin_map tags propagate through completion.
    un = _unconnected(store, ref.id)
    assert un[("U1", "AUX")]["meta"].get("unused") is True
    assert "nc" in un[("U1", "NC1")]["tags"]
    assert "must_terminate" in un[("U1", "MT")]["tags"]
    # The wired pins are not on the unconnected list at all.
    assert ("U1", "SIG") not in un and ("U1", "VDD") not in un

    # Re-putting the same component neither duplicates pins nor clobbers
    # author-declared rows (the ON CONFLICT DO NOTHING dedupe).
    pcb.put(id="d-mini", args={"components": [_mini_component("U1", -8.0)]})
    pins = store.pcb_instance_neighbors(ref.id, "U1")["pins"]
    assert len(pins) == 8


def test_pads_for_ir_emits_every_footprint_pad(pcb, store) -> None:
    """The pad set the checks see now covers the whole package: 8 pads per
    MINI8 instance, including the ones whose pins stay netless."""
    pcb.put(id="d-mini-pads", args=_mini_design())
    ref = store.get_ref(kind="pcb", id="d-mini-pads")
    design = store.pcb_load(ref.id)
    layers = [str(l["name"]) for l in design["board"]["stackup"]]
    pads = pcb._drc_pads(ref.id, layers)
    per_refdes: dict[str, list[dict[str, Any]]] = {}
    for p in pads:
        per_refdes.setdefault(str(p["refdes"]), []).append(p)
    assert len(per_refdes["U1"]) == 8
    assert len(per_refdes["U2"]) == 8
    # Nothing carries the undeclared flag once completion ran.
    assert not any(p.get("undeclared") for p in pads)


def test_unconnected_pin_findings_on_the_real_graph(pcb, store) -> None:
    """Through `pcb_graph`'s marker-carrying `unconnected` rows into the
    check: MT errors (must_terminate, even at netlist phase), SPARE warns
    aggregated one-per-instance, AUX and NC1 never appear."""
    pcb.put(id="d-mini-erc", args=_mini_design())
    ref = store.get_ref(kind="pcb", id="d-mini-erc")
    findings = drc.check_unconnected_pins(
        store.pcb_graph(ref.id)["unconnected"], phase="netlist"
    )
    by_pin = {f.where: f.severity for f in findings}
    assert by_pin == {
        "part U1": "warn",
        "part U2": "warn",
        "pin U1/MT": "error",
        "pin U2/MT": "error",
    }
    assert next(f for f in findings if f.where == "part U1").objects[0]["pins"] == (
        "SPARE",
    )
    # And the same rows reach view='drc' — one check surface.
    body = pcb.get(id="d-mini-erc", view="drc").body
    assert "unconnected_pin" in body
    assert "must_terminate" in body


def test_unused_pads_keep_full_geometry_for_clearance(pcb, store) -> None:
    """`unused` is not `invisible`: push a track across two adjacent unused
    pads (U1's AUX and AUX2) and `check_clearance` reports the collisions —
    "not used" never means "ignore for geometry" (§1j)."""
    pcb.put(id="d-mini-geom", args=_mini_design())
    ref = store.get_ref(kind="pcb", id="d-mini-geom")
    design = store.pcb_load(ref.id)
    layers = [str(l["name"]) for l in design["board"]["stackup"]]
    pads = [
        p
        for p in pcb._drc_pads(ref.id, layers)
        if str(p["refdes"]) == "U1" and str(p["pin"]) in ("AUX", "AUX2")
    ]
    assert len(pads) == 2
    layer = str(pads[0]["layer"])
    # A track running straight through both unused pads' centres.
    xs = sorted(float(p["x"]) for p in pads)
    y = float(pads[0]["y"])
    model = {
        "layers": layers,
        "pads": pads,
        "copper": [
            {
                "ctype": "track",
                "net": "PROBE",
                "layer": layer,
                "width_mm": 0.15,
                "segments": [
                    {
                        "shape": "line",
                        "start": [xs[0] - 0.2, y],
                        "end": [xs[1] + 0.2, y],
                    }
                ],
            }
        ],
    }
    cap = capability_for(drc.process_for_stackup(design["board"]["stackup"]))
    findings = drc.check_clearance(model, cap)
    hit = {
        (o.get("refdes"), o.get("pin"))
        for f in findings
        if f.rule == "clearance" and f.severity == "error"
        for o in f.objects
        if o.get("refdes")
    }
    assert {("U1", "AUX"), ("U1", "AUX2")} <= hit


# ── the safety net: undeclared_pad ─────────────────────────────────────


def test_undeclared_pad_fires_until_reapply_completes(pcb, store) -> None:
    """A footprint cached AFTER the apply leaves a pad no pin claims:
    `undeclared_pad` names it until a re-put completes the declarations."""
    pcb.put(
        id="d-late",
        args={
            "components": [
                {
                    "refdes": "U1",
                    "label": "late",
                    "part": "C7654321",
                    "x": 0.0,
                    "y": 0.0,
                    "pins": [{"name": "P1"}, {"name": "P2"}],
                }
            ],
            "nets": [{"name": "N1"}],
            "connections": [
                {"net": "N1", "refdes": "U1", "pin": "P1"},
                {"net": "N1", "refdes": "U1", "pin": "P2"},
            ],
            "features": [_outline()],
        },
    )
    ref = store.get_ref(kind="pcb", id="d-late")
    assert ref is not None

    # Uncached: the rule cannot tell whether pads go undeclared — it says
    # so rather than passing silently.
    body = pcb.get(id="d-late", view="drc").body
    assert "undeclared_pad" in body
    assert "CANNOT be checked" in body

    # Cache the footprint now (the op='footprint' order): a third pad the
    # netlist never declared must be named by the rule.
    store.part_footprint_put(
        "C7654321",
        {
            "pads": [
                {"number": "1", "shape": "rect", "x": -1, "y": 0, "w": 0.5, "h": 0.5},
                {"number": "2", "shape": "rect", "x": 0, "y": 0, "w": 0.5, "h": 0.5},
                {"number": "3", "shape": "rect", "x": 1, "y": 0, "w": 0.5, "h": 0.5},
            ],
            "pin_map": {
                "1": {"name": "P1"},
                "2": {"name": "P2"},
                "3": {"name": "P3"},
            },
        },
    )
    body = pcb.get(id="d-late", view="drc").body
    assert "undeclared_pad" in body
    assert "U1/3" in body or "pad 3" in body

    # A re-put of the component completes P3; the finding goes away.
    pcb.put(
        id="d-late",
        args={
            "components": [
                {
                    "refdes": "U1",
                    "pins": [{"name": "P1"}, {"name": "P2"}],
                }
            ]
        },
    )
    assert "P3" in {
        p["pin"] for p in store.pcb_instance_neighbors(ref.id, "U1")["pins"]
    }
    # Check the RUN's findings, not the rendered body: the "gone since
    # last run" delta block legitimately reprints the resolved row.
    ran = pcb._drc_run(ref.id, store.pcb_load(ref.id))
    assert ran is not None
    assert "undeclared_pad" not in {f.rule for f in ran.findings}
    # …but the pad is now a netless unmarked pin, so the OTHER rule says so.
    assert "unconnected_pin" in {f.rule for f in ran.findings}


# ── the generator half: sink declares every channel pin ────────────────


def test_ring_sink_declares_all_80_pads(pcb, store) -> None:
    """The Slice-2 acceptance fixture: the ring-package sink (HV507-class,
    64 channels + 5 named control/power pins + 11 NC lands = 80 pads).
    With the footprint cached BEFORE the apply, completion covers even
    the pads the generator cannot name, so the sink has all 80 pins and
    `pads_for_ir` emits all 80 lands."""
    from tests.test_pcb_island_terminal_polygon import (
        _RING_LCSC,
        _RING_PINS,
        _ring_design,
        _ring_footprint,
    )

    # x_half=9.0, not 8.5: _ring_design declares drive_voltage_v=250, and at
    # the seed pose an 8.5mm ring puts the VPP land 0.392mm from the col-7
    # escape via — a cross-footprint pair under the 0.4mm hv floor.
    store.part_footprint_put(
        _RING_LCSC, _ring_footprint(_RING_PINS, per_side=20, x_half=9.0, y_half=11.5)
    )
    pcb.put(id="d-ring", args=_ring_design())
    ref = store.get_ref(kind="pcb", id="d-ring")
    assert ref is not None

    pins = store.pcb_instance_neighbors(ref.id, "ARR1_SINK_0")["pins"]
    assert len(pins) == 80

    design = store.pcb_load(ref.id)
    layers = [str(l["name"]) for l in design["board"]["stackup"]]
    sink_pads = [
        p for p in pcb._drc_pads(ref.id, layers) if str(p["refdes"]) == "ARR1_SINK_0"
    ]
    assert len(sink_pads) == 80

    # The spare channel(s) are marked `unused` by the generator; the
    # completed NC lands carry no marker but are declared pins now.
    un = _unconnected(store, ref.id)
    sink_unconnected = {pin for (r, pin) in un if r == "ARR1_SINK_0"}
    assert "HVOUT64" in sink_unconnected
    assert un[("ARR1_SINK_0", "HVOUT64")]["meta"].get("unused") is True
    nc_pins = {pin for (r, pin) in un if r == "ARR1_SINK_0" and pin.startswith("NC")}
    assert len(nc_pins) == 11
    assert all(
        un[("ARR1_SINK_0", p)]["meta"].get("completed_from_footprint") for p in nc_pins
    )


def test_join_failed_instance_is_not_completed_and_reports_mismatch(pcb, store) -> None:
    """An author pin name matching no footprint label fails the pin/pad
    join for the WHOLE instance (`ir.footprint_pad_set` skips it — the
    gr451276 all-or-nothing join). Completing pins on top would hand
    every footprint land a fresh netless pin while the one genuinely
    wrong pin stays a synthesized bound — the same defect, buried under
    new noise — so `_pcb_apply` skips that instance entirely and the
    pre-existing `pin_name_mismatch` finding names the bad name."""
    pcb.put(
        id="d-join-fail",
        args={
            "footprints": [_MINI_FP],
            "components": [
                {
                    "refdes": "U1",
                    "label": "mini",
                    "footprint": "MINI8",
                    "x": 0.0,
                    "y": 0.0,
                    "pins": [{"name": "SIG"}, {"name": "WRONGNAME"}],
                }
            ],
            "nets": [{"name": "SIG"}, {"name": "WRONG"}],
            "connections": [
                {"net": "SIG", "refdes": "U1", "pin": "SIG"},
                {"net": "WRONG", "refdes": "U1", "pin": "WRONGNAME"},
            ],
            "features": [_outline()],
        },
    )
    ref = store.get_ref(kind="pcb", id="d-join-fail")
    assert ref is not None

    # Completion skipped: exactly the two authored pins, nothing added.
    assert {p["pin"] for p in store.pcb_instance_neighbors(ref.id, "U1")["pins"]} == {
        "SIG",
        "WRONGNAME",
    }

    # The whole instance stays synthesized (the join cannot resolve it),
    # so the run is the flag-based bail pass — and it now reports the
    # mismatch by name instead of bailing on "no real geometry".
    ran = pcb._drc_run(ref.id, store.pcb_load(ref.id))
    assert ran is not None
    mismatch = [f for f in ran.findings if f.rule == "pin_name_mismatch"]
    assert len(mismatch) == 1
    assert "WRONGNAME" in mismatch[0].detail


def test_gerber_banner_reports_not_checked_for_uncached_footprint(pcb, store) -> None:
    """A board whose ONLY finding is 'this part cannot be checked' must
    not read clean on the fab path: '0 errors, 1 warn ✓' would present an
    uninspected footprint as verified copper. (A missing CATALOG
    footprint goes louder still — `synthesized_footprint` is an error —
    so the quiet case is a local footprint name nothing ever authored.)"""
    pcb.put(
        id="d-banner",
        args={
            "components": [
                {
                    "refdes": "U1",
                    "label": "ghost",
                    "footprint": "GHOST-FP",
                    "x": 0.0,
                    "y": 0.0,
                    "pins": [{"name": "P1"}],
                }
            ],
            "nets": [{"name": "N1"}],
            "connections": [{"net": "N1", "refdes": "U1", "pin": "P1"}],
            "features": [_outline()],
        },
    )
    ref = store.get_ref(kind="pcb", id="d-banner")
    assert ref is not None
    banner, _n = pcb._gerber_drc_banner(ref.id, "d-banner")
    assert "not fully checked" in banner
    assert "1 footprint(s) uncached" in banner
    assert "✓" not in banner


def test_retired_pin_is_not_resurrected_by_completion(pcb, store) -> None:
    """A pin row put to rest must stay retired: the ON CONFLICT partial
    index ignores retired rows, so completion would silently un-retire
    the name on every re-apply unless its dedupe set covers retired rows
    too (review of 8dbb84edb, finding 1)."""
    pcb.put(id="d-retire", args=_mini_design())
    ref = store.get_ref(kind="pcb", id="d-retire")
    assert ref is not None
    with store.pool.connection() as conn, conn.transaction():
        conn.execute(
            "UPDATE pcb_pins SET retired_at = now() WHERE name = 'SPARE' "
            "AND component_id IN ("
            "  SELECT i.component_id FROM pcb_instances i "
            "  WHERE i.ref_id = %s AND i.refdes = 'U1' "
            "  AND i.retired_at IS NULL)",
            (ref.id,),
        )
    pcb.put(id="d-retire", args={"components": [_mini_component("U1", -8.0)]})
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT (retired_at IS NOT NULL) FROM pcb_pins WHERE name = 'SPARE' "
            "AND component_id IN ("
            "  SELECT i.component_id FROM pcb_instances i "
            "  WHERE i.ref_id = %s AND i.refdes = 'U1' "
            "  AND i.retired_at IS NULL)",
            (ref.id,),
        ).fetchall()
    # Still exactly ONE row, still retired — completion did not re-add it.
    assert len(rows) == 1 and rows[0][0]

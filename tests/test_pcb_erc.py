"""Slice 5 of docs/backlog/pcb-lazy-netlist-and-checks.md — the §1i pin
semantics (``etype``/``max_voltage_v``/``rail``, validated in
``_pcb_apply``, carried through the IR) and the §1h ERC rules on the
shared check surface: ``pin_conflict``, ``no_driver``, ``over_voltage``,
``rail_crossing``, ``no_decoupling`` — each with a "cannot fire" path
that reports a not-checked note rather than silently passing.

Every accept bullet of the slice is a test below: a 20 V net on a pin
rated 12 V errors ``over_voltage``; a 3.3 V-rail output driving a 5 V
-rail input errors ``rail_crossing`` and a ``level_shifter`` between
them makes it clean; an IC ``power_in`` with no capacitor on its rail
within N mm warns ``no_decoupling``; a fixture with no etype declared
yields not-checked notes, never a clean pass.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import drc
from precis.pcb.ir import from_graph

pytestmark = pytest.mark.db


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _net(name: str, *members: dict[str, Any], **kw: Any) -> dict[str, Any]:
    """``_net("SIG", _m("U1","OUT",tags=["output"]), ..., voltage=3.3)``."""
    return {"name": name, "members": list(members), **kw}


def _m(refdes: str, pin: str, *, tags=(), meta=None) -> dict[str, Any]:
    return {"refdes": refdes, "pin": pin, "tags": list(tags), "meta": dict(meta or {})}


# ── unit level: the rules on graph-shaped dicts ───────────────────────


def test_pin_conflict_two_drivers_error() -> None:
    nets = [
        _net(
            "SIG",
            _m("U1", "OUT", tags=["output"]),
            _m("U2", "OUT", tags=["output"]),
        )
    ]
    findings = drc.check_pin_conflicts(nets)
    assert [(f.rule, f.severity) for f in findings] == [("pin_conflict", "error")]
    # open_drain/tristate drivers share by design — no conflict.
    nets2 = [
        _net(
            "BUS",
            _m("U1", "D", tags=["open_drain"]),
            _m("U2", "D", tags=["open_drain"]),
            _m("U3", "IN", tags=["input"]),
        )
    ]
    assert drc.check_pin_conflicts(nets2) == []


def test_no_driver_warns_input_only_net() -> None:
    nets = [_net("F", _m("U1", "IN", tags=["input"]), _m("U2", "IN", tags=["input"]))]
    findings = drc.check_no_driver(nets)
    assert [(f.rule, f.severity) for f in findings] == [("no_driver", "warn")]
    # Negative: an actual driver on the net leaves it clean.
    nets2 = [
        _net(
            "G",
            _m("U3", "OUT", tags=["output"]),
            _m("U1", "IN", tags=["input"]),
        )
    ]
    assert drc.check_no_driver(nets2) == []
    # A bidi/driver member satisfies it too.
    nets[0]["members"].append(_m("U3", "D", tags=["bidi"]))
    assert drc.check_no_driver(nets) == []


def test_no_driver_reaches_through_series_passives() -> None:
    """gr521600 — the nano EN wiring: VBUS feeds U_B15's EN input
    through R_EN15. The flat per-net test warned 'nothing that drives
    them' on a correctly-powered net; the driver judgement now runs
    over the union of nets joined by certified 2-pin series
    pass-throughs (R / L / ferrite / 0-ohm — etype ``passive`` or
    ``current_role`` ``series``)."""
    nets = [
        _net(
            "VBUS",
            _m("J1", "VBUS", tags=["power_out"]),
            _m("R_EN15", "1", tags=["passive"]),
        ),
        _net(
            "EN15",
            _m("R_EN15", "2", tags=["passive"]),
            _m("U_B15", "EN", tags=["input"]),
        ),
    ]
    inst = [{"refdes": "R_EN15", "n_pins": 2}]
    assert drc.check_no_driver(nets, inst) == []
    # The instances-less path counts member pins: same verdict.
    assert drc.check_no_driver(nets) == []
    # ``current_role='series'`` certifies the pin without the etype,
    # and a declared ``fuse`` class certifies a part whose refdes left
    # the R/L/FB convention (F* is no hop designator).
    nets2 = [
        _net(
            "VBUS",
            _m("J1", "VBUS", tags=["power_out"]),
            _m("F1", "1", meta={"current_role": "series"}),
        ),
        _net(
            "EN15",
            _m("F1", "2", meta={"current_role": "series"}),
            _m("U_B15", "EN", tags=["input"]),
        ),
    ]
    assert (
        drc.check_no_driver(nets2, [{"refdes": "F1", "n_pins": 2}], {"F1": ["fuse"]})
        == []
    )


def test_no_driver_hop_class_is_links_only() -> None:
    """gr521600 r2 — the hop class is DC links only. The old gate (any
    2-pin part with passive pins) let a pull-down LED to a driven GND
    pass an input off as driven; LED, diode, TVS, crystal, connector
    and transistor refdeses now never join nets. R/L/FB refdeses
    (incl. the ``R_``/``L_`` spellings) and a declared link-class role
    still hop."""
    for refdes in ("LED1", "D1", "TVS1", "Y1", "X1", "J1", "Q1"):
        nets = [
            _net(
                "GND",
                _m("J_PWR", "GND", tags=["power_out"]),
                _m(refdes, "2", tags=["passive"]),
            ),
            _net(
                "SIG",
                _m(refdes, "1", tags=["passive"]),
                _m("U1", "IN", tags=["input"]),
            ),
        ]
        findings = drc.check_no_driver(nets, [{"refdes": refdes, "n_pins": 2}])
        assert [(f.rule, f.where) for f in findings] == [("no_driver", "net SIG")], (
            refdes
        )
    # Certified links still join their nets: the L and FB
    # conventions, and a declared link-class role off-convention.
    for refdes, roles in (("L1", None), ("FB1", None), ("M1", ["jumper"])):
        nets = [
            _net(
                "VBUS",
                _m("J_PWR", "V", tags=["power_out"]),
                _m(refdes, "1", tags=["passive"]),
            ),
            _net(
                "SIG",
                _m(refdes, "2", tags=["passive"]),
                _m("U1", "IN", tags=["input"]),
            ),
        ]
        assert (
            drc.check_no_driver(
                nets,
                [{"refdes": refdes, "n_pins": 2}],
                {refdes: roles} if roles else None,
            )
            == []
        ), refdes


def _hop_nets(refdes: str) -> list[dict[str, Any]]:
    """A driver, one 2-pin part between it and an input — the
    minimal hop-or-warn fixture."""
    return [
        _net(
            "VBUS",
            _m("J_PWR", "V", tags=["power_out"]),
            _m(refdes, "1", tags=["passive"]),
        ),
        _net(
            "SIG",
            _m(refdes, "2", tags=["passive"]),
            _m("U1", "IN", tags=["input"]),
        ),
    ]


def test_no_driver_hop_class_word_is_anchored() -> None:
    """An unanchored match would certify ``LinkSwitch`` (a real PI
    IC) or ``diffuser`` as links — the class regex only counts whole
    words."""
    for word in ("LinkSwitch", "diffuser", "linker", "unfused"):
        findings = drc.check_no_driver(
            _hop_nets("M1"), [{"refdes": "M1", "n_pins": 2}], {"M1": [word]}
        )
        assert [f.rule for f in findings] == ["no_driver"], word
    # Whole-word declarations still certify — plurals included.
    for word in ("0-ohm link", "ferrite bead", "inductors (coils)"):
        findings = drc.check_no_driver(
            _hop_nets("M1"), [{"refdes": "M1", "n_pins": 2}], {"M1": [word]}
        )
        assert findings == [], word


def test_no_driver_ic_class_vetoes_refdes_hop() -> None:
    """A declared class saying non-passive vetoes even a
    convention-shaped refdes — an IC numbered ``R5`` never hops —
    unless a hop class is also declared."""
    inst = [{"refdes": "R5", "n_pins": 2}]
    findings = drc.check_no_driver(_hop_nets("R5"), inst, {"R5": ["ic"]})
    assert [(f.rule, f.where) for f in findings] == [("no_driver", "net SIG")]
    # The catalog-identity part_class vetoes the same way.
    inst_ic = [{"refdes": "R5", "n_pins": 2, "part": {"part_class": "ic"}}]
    findings = drc.check_no_driver(_hop_nets("R5"), inst_ic)
    assert [f.rule for f in findings] == ["no_driver"]
    # An explicit hop-class declaration wins over the veto.
    findings = drc.check_no_driver(_hop_nets("R5"), inst, {"R5": ["ic", "resistor"]})
    assert findings == []
    # A free tag naming no class leaves the convention alone.
    findings = drc.check_no_driver(_hop_nets("R5"), inst, {"R5": ["noisy"]})
    assert findings == []


def test_no_driver_capacitor_blocks_the_hop() -> None:
    """A capacitor between driver and consumer passes no DC — the
    net behind it still floats and still warns, on the C-refdes
    convention or a ``capacitor`` role."""
    nets = [
        _net(
            "VBUS",
            _m("J1", "VBUS", tags=["power_out"]),
            _m("C15", "1", tags=["passive"]),
        ),
        _net(
            "EN15",
            _m("C15", "2", tags=["passive"]),
            _m("U_B15", "EN", tags=["input"]),
        ),
    ]
    findings = drc.check_no_driver(nets, [{"refdes": "C15", "n_pins": 2}])
    assert [(f.rule, f.severity, f.where) for f in findings] == [
        ("no_driver", "warn", "net EN15")
    ]
    # A mis-designated capacitor declares the role instead — same block.
    nets[0]["members"][1]["refdes"] = "X1"
    nets[1]["members"][0]["refdes"] = "X1"
    findings = drc.check_no_driver(
        nets, [{"refdes": "X1", "n_pins": 2}], {"X1": ["capacitor"]}
    )
    assert [(f.rule, f.where) for f in findings] == [("no_driver", "net EN15")]


def test_no_driver_passive_must_be_certified() -> None:
    """The hop needs the declaration — an unannotated 2-pin part does
    not reach (it may be a diode, an isolator, anything), and a
    multi-pin part carrying ``series`` pins is not a series passive
    (a connector's 2 of 20 contacts join nothing)."""
    nets = [
        _net(
            "VBUS",
            _m("J1", "VBUS", tags=["power_out"]),
            _m("R1", "1"),
        ),
        _net(
            "EN15",
            _m("R1", "2"),
            _m("U_B15", "EN", tags=["input"]),
        ),
    ]
    assert [f.rule for f in drc.check_no_driver(nets)] == ["no_driver"]
    # Connector: series-carrying pins, but 4 pins declared — no hop.
    nets2 = [
        _net(
            "VBUS",
            _m("J1", "VBUS", tags=["power_out"]),
            _m("CN1", "1", meta={"current_role": "series"}),
        ),
        _net(
            "EN15",
            _m("CN1", "2", meta={"current_role": "series"}),
            _m("U_B15", "EN", tags=["input"]),
        ),
    ]
    assert [
        f.rule for f in drc.check_no_driver(nets2, [{"refdes": "CN1", "n_pins": 4}])
    ] == ["no_driver"]


def test_no_driver_resistor_chain_and_cycle_bound() -> None:
    """The closure is union-find over net names — a long R+R chain
    resolves, and a passive ring with no driver anywhere terminates
    and warns rather than walking forever."""
    chain = [
        _net("VBUS", _m("J1", "V", tags=["power_out"]), _m("R1", "1", tags=["passive"]))
    ]
    for i in range(1, 12):
        chain.append(
            _net(
                f"N{i}",
                _m(f"R{i}", "2", tags=["passive"]),
                _m(f"R{i + 1}", "1", tags=["passive"]),
            )
        )
    chain.append(
        _net(
            "EN",
            _m("R12", "2", tags=["passive"]),
            _m("U1", "EN", tags=["input"]),
        )
    )
    inst = [{"refdes": f"R{i}", "n_pins": 2} for i in range(1, 13)]
    assert drc.check_no_driver(chain, inst) == []
    # Ring of passives, a consumer, no driver: bounded walk, still warns.
    ring = [
        _net(
            "A",
            _m("R1", "1", tags=["passive"]),
            _m("R3", "2", tags=["passive"]),
            _m("U1", "EN", tags=["input"]),
        ),
        _net("B", _m("R1", "2", tags=["passive"]), _m("R2", "1", tags=["passive"])),
        _net("C", _m("R2", "2", tags=["passive"]), _m("R3", "1", tags=["passive"])),
    ]
    inst3 = [{"refdes": f"R{i}", "n_pins": 2} for i in range(1, 4)]
    findings = drc.check_no_driver(ring, inst3)
    assert [(f.rule, f.where) for f in findings] == [("no_driver", "net A")]


def test_over_voltage_from_net_and_class_voltage() -> None:
    nets = [
        _net(
            "HV",
            _m("U1", "OUT", tags=["output"]),
            _m("U2", "IN", tags=["input"], meta={"max_voltage_v": 12}),
            working_voltage_v=20.0,
        )
    ]
    findings = drc.check_over_voltage(nets)
    assert [(f.rule, f.severity, f.objects[0]["refdes"]) for f in findings] == [
        ("over_voltage", "error", "U2")
    ]
    # Same via the net class's rules working_voltage_v (Slice 4c key).
    nets[0]["working_voltage_v"] = None
    nets[0]["net_class"] = "hv"
    findings = drc.check_over_voltage(nets, {"hv": {"working_voltage_v": 20}})
    assert [f.rule for f in findings] == ["over_voltage"]
    # At and below the rating: clean. A negative rail compared by
    # magnitude — |−20| over a |12| bound fires just the same.
    assert drc.check_over_voltage(nets, {"hv": {"working_voltage_v": 12}}) == []
    assert drc.check_over_voltage(nets, {"hv": {"working_voltage_v": 5}}) == []
    findings = drc.check_over_voltage(nets, {"hv": {"working_voltage_v": -20}})
    assert [f.rule for f in findings] == ["over_voltage"]
    # No pin carrying a bound anywhere → the rule cannot check at all:
    # the not-checked row, not a silent pass.
    nets[0]["members"] = [nets[0]["members"][0]]
    findings = drc.check_over_voltage(nets, {"hv": {"working_voltage_v": 20}})
    assert [(f.rule, f.severity) for f in findings] == [("over_voltage", "warn")]
    assert "CANNOT be checked" in findings[0].detail


def test_erc_not_checked_names_the_missing_side_and_the_enable_path() -> None:
    """gr520795 — 'no net carries est_current_a or no pin carries
    max_current_a' told the reader a side was missing but never WHICH
    one or how to enable the check. taz-leaf-b: every net carried
    est_current_a, so the gap was pins — the row must say exactly that."""
    # Nets annotated, no pin bound anywhere → the PIN side is the gap.
    nets = [_net("VCC", _m("U1", "VDD"), est_current_a=0.5)]
    findings = drc.check_over_current(nets)
    assert [(f.rule, f.severity) for f in findings] == [("over_current", "warn")]
    assert "no pin carries max_current_a" in findings[0].detail
    assert "no net carries est_current_a" not in findings[0].detail
    assert "op='pins'" in findings[0].detail

    nets = [_net("VCC", _m("U1", "VDD"), working_voltage_v=3.3)]
    findings = drc.check_over_voltage(nets)
    assert [(f.rule, f.severity) for f in findings] == [("over_voltage", "warn")]
    assert "no pin carries max_voltage_v" in findings[0].detail
    assert "no net carries working_voltage_v" not in findings[0].detail
    assert "op='pins'" in findings[0].detail

    # Nothing declared at all → both sides named.
    findings = drc.check_over_current([_net("SIG", _m("U1", "A"), _m("U2", "B"))])
    assert "no net carries est_current_a" in findings[0].detail
    assert "no pin carries max_current_a" in findings[0].detail


def test_rail_crossing_and_level_shifter() -> None:
    nets = [
        _net(
            "SIG",
            _m("U1", "OUT", tags=["output"], meta={"rail": "3v3"}),
            _m("U2", "IN", tags=["input"], meta={"rail": "5v"}),
        )
    ]
    findings = drc.check_rail_crossing(nets)
    assert [(f.rule, f.severity) for f in findings] == [("rail_crossing", "error")]
    # A declared translator on the net makes the crossing clean.
    nets[0]["members"].append(_m("LS1", "A", tags=["bidi"]))
    findings = drc.check_rail_crossing(nets, roles_by_refdes={"LS1": ["level_shifter"]})
    assert findings == []


def test_no_decoupling_distance() -> None:
    positions = {
        ("U1", "VDD"): (0.0, 0.0),
        ("C1", "1"): (30.0, 0.0),  # 30 mm away — beyond the 5 mm default
    }
    nets = [
        _net(
            "VCC",
            _m("U1", "VDD", tags=["power_in"]),
            _m("C1", "1", tags=["passive"]),
            meta={"rail": "3v3"},
        )
    ]
    findings = drc.check_no_decoupling(nets, positions)
    assert [(f.rule, f.severity) for f in findings] == [("no_decoupling", "warn")]
    positions[("C1", "1")] = (2.0, 0.0)
    assert drc.check_no_decoupling(nets, positions) == []


def test_no_decoupling_capacitor_must_actually_be_a_capacitor() -> None:
    """A resistor or a connector on the rail within reach must NOT
    silence the warning — only `^C\\d+$` refdeses and `capacitor`-roled
    parts count."""
    positions = {
        ("U1", "VDD"): (0.0, 0.0),
        ("R1", "1"): (1.0, 0.0),  # passive resistor, 1 mm away
        ("CN1", "1"): (1.0, 0.0),  # connector refdes, starts with C
        ("LC1", "1"): (1.0, 0.0),  # "capacitor" role but non-C refdes
    }
    nets = [
        _net(
            "VCC",
            _m("U1", "VDD", tags=["power_in"]),
            _m("R1", "1", tags=["passive"]),
            _m("CN1", "1", tags=["passive"]),
            _m("LC1", "1", tags=["passive"]),
            meta={"rail": "3v3"},
        )
    ]
    # R1 (passive ≠ capacitor), CN1 (connector), LC1 without the role:
    # none count — the pin is unprotected.
    findings = drc.check_no_decoupling(nets, positions)
    assert [f.rule for f in findings] == ["no_decoupling"]
    # The `capacitor` ROLE on LC1 counts even off the C* convention.
    findings = drc.check_no_decoupling(
        nets, positions, roles_by_refdes={"LC1": ["capacitor"]}
    )
    assert findings == []


def test_every_rule_reports_not_checked_when_the_vocabulary_is_absent() -> None:
    """A fixture with no etype declared yields not-checked notes — the
    check can never read as a clean pass when it saw nothing."""
    nets = [_net("SIG", _m("U1", "A"), _m("U2", "B"))]
    for rule, findings in [
        ("pin_conflict", drc.check_pin_conflicts(nets)),
        ("no_driver", drc.check_no_driver(nets)),
        ("over_voltage", drc.check_over_voltage(nets)),
        ("rail_crossing", drc.check_rail_crossing(nets)),
        ("no_decoupling", drc.check_no_decoupling(nets, {("U1", "A"): (0, 0)})),
        ("over_current", drc.check_over_current(nets)),
        ("passive_rating", drc.check_passive_rating(nets, [])),
    ]:
        assert [(f.rule, f.severity) for f in findings] == [(rule, "warn")]
        assert "CANNOT be checked" in findings[0].detail


# ── integration: put-level authoring + view='check' ───────────────────

_ERC_DESIGN: dict[str, Any] = {
    "components": [
        {
            "refdes": "U1",
            "label": "driver",
            "x": 10.0,
            "y": 10.0,
            "pins": [
                {"name": "HVOUT", "etype": "output", "rail": "3v3"},
                {"name": "VDD", "etype": "power_in", "rail": "vcc"},
            ],
        },
        {
            "refdes": "U2",
            "label": "sink",
            "x": 30.0,
            "y": 10.0,
            "pins": [
                {
                    "name": "HVIN",
                    "etype": "input",
                    "max_voltage_v": 12,
                    "rail": "hv20",
                }
            ],
        },
        {
            "refdes": "C1",
            "label": "bypass",
            "x": 40.0,
            "y": 10.0,
            "pins": [{"name": "1", "etype": "passive"}],
        },
    ],
    "nets": [
        {"name": "HV", "voltage": 20},
        {"name": "VCC", "rail": "vcc"},
    ],
    "connections": [
        {"net": "HV", "refdes": "U1", "pin": "HVOUT"},
        {"net": "HV", "refdes": "U2", "pin": "HVIN"},
        {"net": "VCC", "refdes": "U1", "pin": "VDD"},
        {"net": "VCC", "refdes": "C1", "pin": "1"},
    ],
}


def test_etype_vocabulary_validated_at_apply(pcb: PcbHandler) -> None:
    bad = {
        "components": [
            {
                "refdes": "U1",
                "label": "x",
                "x": 0.0,
                "y": 0.0,
                "pins": [{"name": "A", "etype": "flux_capacitor"}],
            }
        ],
        "nets": [],
    }
    with pytest.raises(Exception, match="etype"):
        pcb.put(id="erc-bad-etype", args=bad)


def test_etype_decl_conflicts_with_existing_etype_tag(pcb: PcbHandler) -> None:
    """``etype='output'`` beside an ``input`` tag is a contradiction —
    refused at apply, not silently resolved."""
    bad = {
        "components": [
            {
                "refdes": "U1",
                "label": "x",
                "x": 0.0,
                "y": 0.0,
                "pins": [{"name": "A", "etype": "output", "tags": ["input"]}],
            }
        ],
        "nets": [],
    }
    with pytest.raises(Exception, match="etype"):
        pcb.put(id="erc-clash", args=bad)


def test_level_shifter_role_flows_through_put(pcb: PcbHandler) -> None:
    """End to end through ``design['instances'][i]['roles']``: a
    component declaring ``roles: ['level_shifter']`` on a crossing net is
    the declared translator — ``rail_crossing`` does not fire."""
    design: dict[str, Any] = {
        "components": [
            {
                "refdes": "U1",
                "label": "driver",
                "x": 0.0,
                "y": 0.0,
                "pins": [{"name": "OUT", "etype": "output", "rail": "3v3"}],
            },
            {
                "refdes": "LS1",
                "label": "translator",
                "x": 10.0,
                "y": 0.0,
                "roles": ["level_shifter"],
                "pins": [{"name": "A", "etype": "bidi"}],
            },
            {
                "refdes": "U2",
                "label": "sink",
                "x": 20.0,
                "y": 0.0,
                "pins": [{"name": "IN", "etype": "input", "rail": "5v"}],
            },
        ],
        "nets": [{"name": "SIG"}],
        "connections": [
            {"net": "SIG", "refdes": "U1", "pin": "OUT"},
            {"net": "SIG", "refdes": "LS1", "pin": "A"},
            {"net": "SIG", "refdes": "U2", "pin": "IN"},
        ],
    }
    pcb.put(id="erc-shifter", args=design)
    body = pcb.get(id="erc-shifter", view="check").body
    assert "rail_crossing" not in body
    # Without the role the same topology crosses rails.
    design["components"][1]["roles"] = []
    pcb.put(id="erc-shifter-bare", args=design)
    body = pcb.get(id="erc-shifter-bare", view="check").body
    assert "rail_crossing" in body


def test_check_surface_runs_erc_rules(pcb: PcbHandler) -> None:
    """End to end: the 20 V net on a 12 V pin errors ``over_voltage``,
    the rail mismatch errors ``rail_crossing``, and the distant
    capacitor warns ``no_decoupling`` — on ``view='check'``, and NOT on
    ``view='drc'`` (the geometric preset)."""
    pcb.put(id="erc-board", args=_ERC_DESIGN)
    body = pcb.get(id="erc-board", view="check").body
    assert "over_voltage" in body
    assert "rail_crossing" in body
    assert "no_decoupling" in body
    drc_body = pcb.get(id="erc-board", view="drc").body
    assert "over_voltage" not in drc_body
    assert "rail_crossing" not in drc_body


def test_untyped_fixture_reports_not_checked(pcb: PcbHandler) -> None:
    """The store-level fixture with no etype declarations: every ERC
    rule reports a not-checked row — the check is never a clean pass
    when it saw nothing."""
    from .test_pcb_declared_pins import _mini_design

    pcb.put(id="erc-untyped", args=_mini_design())
    body = pcb.get(id="erc-untyped", view="check").body
    assert "CANNOT be checked" in body


def test_ir_carries_pin_semantics(pcb: PcbHandler, store: Any) -> None:
    """``from_graph`` carries pin_etype/pin_max_voltage_v/pin_rail —
    the netlist consumer never re-joins pcb_pins."""
    pcb.put(id="erc-ir", args=_ERC_DESIGN)
    ref = store.get_ref(kind="pcb", id="erc-ir")
    ir = from_graph(store.pcb_graph(ref.id))
    labels = {str(l): i for i, l in enumerate(ir.pin_label)}
    inst = [str(r) for r in ir.instance_refdes]
    # find U2/HVIN
    target = [
        i
        for i, l in enumerate(ir.pin_label)
        if inst[int(ir.pin_instance[i])] == "U2" and str(l) == "HVIN"
    ]
    assert target
    pid = target[0]
    assert str(ir.pin_etype[pid]) == "input"
    assert float(ir.pin_max_voltage_v[pid]) == 12.0
    assert str(ir.pin_rail[pid]) == "hv20"


def test_net_rail_lands_in_meta(pcb: PcbHandler, store: Any) -> None:
    pcb.put(id="erc-rail", args=_ERC_DESIGN)
    ref = store.get_ref(kind="pcb", id="erc-rail")
    nets = {n["name"]: n for n in store.pcb_graph(ref.id)["nets"]}
    assert nets["VCC"]["meta"]["rail"] == "vcc"


# ── unsupplied_client ─────────────────────────────────────────────────


def test_unsupplied_client_flags_the_excess() -> None:
    from precis.pcb.matching import Client, Role, Supply

    sup = [Supply("U1", "drv", (("A",),))]
    clients = [Client("P1", "1", "drv"), Client("P2", "1", "drv")]
    out = drc.check_unsupplied_client(clients, sup, {"drv": Role("drv")})
    assert [(f.rule, f.severity) for f in out] == [("unsupplied_client", "error")]
    assert "drv" in out[0].detail and "2 clients vs 1 terminals" in out[0].detail
    assert out[0].where in ("pin P1/1", "pin P2/1")
    sup2 = [Supply("U1", "drv", (("A",), ("B",)))]
    assert drc.check_unsupplied_client(clients, sup2, {"drv": Role("drv")}) == []
    assert drc.check_unsupplied_client([], sup, {}) == []


def test_unsupplied_client_multi_pin_block() -> None:
    from precis.pcb.matching import Client, Supply

    # A size-1 client cannot split a size-2 block: a real unsupplied
    # error, not a not-checked warn. A matching tuple client is served.
    out = drc.check_unsupplied_client(
        [Client("P1", "1", "drv")], [Supply("U1", "drv", (("A", "B"),))], {}
    )
    assert [(f.rule, f.severity) for f in out] == [("unsupplied_client", "error")]
    assert out[0].where == "pin P1/1"
    assert (
        drc.check_unsupplied_client(
            [Client("P1", "1", "drv", members=("1", "2"))],
            [Supply("U1", "drv", (("A", "B"),))],
            {},
        )
        == []
    )

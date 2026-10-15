"""Slice 3 of docs/backlog/pcb-datasheet-review.md — a passive's rating
and dielectric parsed from the part catalog description (in-scope 4,
partially) plus the ``passive_rating`` ERC rule and its named margin
policy (in-scope 5).

Covered here, per the slice's accept list:

* ``catalog.parse_passive_description`` turns a catalog description
  (``"100nF ±10% 16V X7R …"``) into value/rating/dielectric — and stays
  ``None`` (never a guess) for parts that are not recognisably passives;
* ``check_passive_rating`` errors when a cap's rating is under
  factor × the voltage across it, or a parsed dielectric ranks below a
  pin's declared ``min_dielectric``, or a resistor dissipates over 50 %
  of rated power — each finding naming both numbers and the policy;
* unknown net voltage is the ask-the-user row, never a guess;
* ``op='pins'`` accepts/validates/canonicalises ``min_dielectric``;
* end to end, a catalog part on a board turns ``passive_rating`` from
  CANNOT-be-checked into a real verdict on ``view='check'``, and
  ``#REFDES`` shows the derived fact.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import catalog, drc

pytestmark = pytest.mark.db


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _net(name: str, *members: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {"name": name, "members": list(members), **kw}


def _m(refdes: str, pin: str, *, tags=(), meta=None) -> dict[str, Any]:
    return {"refdes": refdes, "pin": pin, "tags": list(tags), "meta": dict(meta or {})}


def _inst(refdes: str, description: str | None) -> dict[str, Any]:
    return {"refdes": refdes, "part_description": description}


# ── the catalog-description parse ────────────────────────────────────


def test_parse_mlcc_description() -> None:
    spec = catalog.parse_passive_description(
        "100nF ±10% 16V X7R 0402 Multilayer Ceramic Capacitors MLCC - SMD/SMT RoHS"
    )
    assert spec == {
        "kind": "capacitor",
        "value_f": pytest.approx(1e-7),
        "rated_v": 16.0,
        "dielectric": "x7r",
    }
    spec = catalog.parse_passive_description(
        "22pF ±5% 50V C0G 0603 Multilayer Ceramic Capacitors MLCC"
    )
    assert spec is not None
    assert spec["kind"] == "capacitor"
    assert spec["value_f"] == pytest.approx(22e-12)
    assert spec["rated_v"] == 50.0
    assert spec["dielectric"] == "c0g"


def test_parse_dielectric_is_not_a_voltage() -> None:
    """``Y5V``'s trailing ``5V`` must never parse as a 5 V rating."""
    spec = catalog.parse_passive_description(
        "100nF ±10% Y5V 0402 Multilayer Ceramic Capacitors MLCC"
    )
    assert spec is not None
    assert spec["rated_v"] is None
    assert spec["dielectric"] == "y5v"


def test_parse_capacitor_without_dielectric() -> None:
    """A tantalum/aluminium description carries no class word — the
    parse still rates it, dielectric None (never guessed)."""
    spec = catalog.parse_passive_description(
        "47uF ±20% 25V SMD,D5xL5.4mm Aluminum Electrolytic Capacitors RoHS"
    )
    assert spec == {
        "kind": "capacitor",
        "value_f": pytest.approx(47e-6),
        "rated_v": 25.0,
        "dielectric": None,
    }


def test_parse_resistor_description() -> None:
    spec = catalog.parse_passive_description(
        "10kΩ ±1% 100mW 0402 Chip Resistor - Surface Mount RoHS"
    )
    assert spec == {
        "kind": "resistor",
        "ohm": pytest.approx(10_000.0),
        "rated_w": pytest.approx(0.1),
    }
    # The fraction spelling, and MΩ staying mega (not milli).
    spec = catalog.parse_passive_description("1MΩ ±5% 1/10W Thick Film Resistors 0805")
    assert spec is not None
    assert spec["ohm"] == pytest.approx(1e6)
    assert spec["rated_w"] == pytest.approx(0.1)


def test_parse_non_passive_is_none() -> None:
    """A diode's ``16V`` or a bead's ``120Ω`` never misparse into a
    passive — kind detection needs the word."""
    assert catalog.parse_passive_description("SMAJ5.0A TVS Diode 400W") is None
    assert catalog.parse_passive_description("Ferrite Beads 120Ω 100MHz 2A") is None
    assert catalog.parse_passive_description(None) is None
    assert catalog.parse_passive_description("") is None


# ── the rule: capacitor voltage margin ───────────────────────────────


def test_passive_rating_cap_under_margin() -> None:
    """C4 rated 6.3 V across a 5 V drop: policy wants ≥ 2× = 10 V — the
    finding names both numbers and the policy."""
    nets = [
        _net("VCC", _m("C4", "1"), _m("U1", "VDD"), working_voltage_v=5.0),
        _net("GND", _m("C4", "2"), _m("U1", "GND"), working_voltage_v=0.0),
    ]
    findings = drc.check_passive_rating(
        nets, [_inst("C4", "100nF ±10% 6.3V Y5V 0402 Capacitors")]
    )
    assert [(f.rule, f.severity, f.where) for f in findings] == [
        ("passive_rating", "error", "part C4")
    ]
    assert "rated 6.3 V across 5 V" in findings[0].detail
    assert "10 V" in findings[0].detail
    assert "fixed-derating" in findings[0].detail


def test_passive_rating_cap_within_margin_is_clean() -> None:
    nets = [
        _net("VCC", _m("C4", "1"), working_voltage_v=5.0),
        _net("GND", _m("C4", "2"), working_voltage_v=0.0),
    ]
    assert (
        drc.check_passive_rating(
            nets, [_inst("C4", "100nF ±10% 16V X7R 0402 Capacitors")]
        )
        == []
    )


def test_passive_rating_c0g_gets_the_tighter_margin() -> None:
    """C0G/NP0 take 1.5× — a 6.3 V part across 5 V fails (needs 7.5 V)
    where the 2× classes would fail harder, and a 10 V part passes."""
    nets = [
        _net("VCC", _m("C4", "1"), working_voltage_v=5.0),
        _net("GND", _m("C4", "2"), working_voltage_v=0.0),
    ]
    findings = drc.check_passive_rating(
        nets, [_inst("C4", "22pF ±5% 6.3V C0G 0603 Capacitors")]
    )
    assert [f.severity for f in findings] == ["error"]
    assert "1.5× = 7.5 V" in findings[0].detail
    assert (
        drc.check_passive_rating(
            nets, [_inst("C4", "22pF ±5% 10V NP0 0603 Capacitors")]
        )
        == []
    )


# ── the rule: dielectric minimum ─────────────────────────────────────


def test_passive_rating_dielectric_below_minimum() -> None:
    """A pin's ``min_dielectric`` (op='pins' — the slice-5 part fact's
    design-layer stand-in) binds every parsed capacitor on its rail."""
    nets = [
        _net(
            "VCC",
            _m("C4", "1"),
            _m("U1", "VDD", meta={"rail": "vcc", "min_dielectric": "x7r"}),
            working_voltage_v=5.0,
            meta={"rail": "vcc"},
        ),
        _net("GND", _m("C4", "2"), working_voltage_v=0.0, meta={"rail": "gnd"}),
    ]
    findings = drc.check_passive_rating(
        nets, [_inst("C4", "100nF ±10% 16V Y5V 0402 Capacitors")]
    )
    assert [(f.severity,) for f in findings] == [("error",)]
    assert "y5v" in findings[0].detail and "x7r" in findings[0].detail
    # At the minimum, clean.
    findings = drc.check_passive_rating(
        nets, [_inst("C4", "100nF ±10% 16V X7R 0402 Capacitors")]
    )
    assert findings == []
    # Better than the minimum, clean.
    findings = drc.check_passive_rating(
        nets, [_inst("C4", "100nF ±10% 16V C0G 0402 Capacitors")]
    )
    assert findings == []


def test_passive_rating_dielectric_unparseable_cannot_confirm() -> None:
    """A minimum declared, a cap whose dielectric the description does
    not name: the check is a not-checked info row, not a pass."""
    nets = [
        _net(
            "VCC",
            _m("C4", "1"),
            _m("U1", "VDD", meta={"min_dielectric": "x7r"}),
            working_voltage_v=5.0,
        ),
        _net("GND", _m("C4", "2"), working_voltage_v=0.0),
    ]
    findings = drc.check_passive_rating(
        nets, [_inst("C4", "47uF ±20% 25V Aluminum Electrolytic Capacitors")]
    )
    assert [(f.severity, f.objects[0].get("not_checked")) for f in findings] == [
        ("warn", True)
    ]
    assert "dielectric" in findings[0].detail


# ── the rule: resistor power ─────────────────────────────────────────


def test_passive_rating_resistor_over_power() -> None:
    """10 Ω across 5 V dissipates 2.5 W — the policy allows half the
    0.1 W rating."""
    nets = [
        _net("VCC", _m("R1", "1"), working_voltage_v=5.0),
        _net("GND", _m("R1", "2"), working_voltage_v=0.0),
    ]
    findings = drc.check_passive_rating(
        nets, [_inst("R1", "10Ω ±1% 100mW 0402 Chip Resistor")]
    )
    assert [(f.rule, f.severity) for f in findings] == [("passive_rating", "error")]
    assert "2.5 W" in findings[0].detail and "0.1 W" in findings[0].detail
    # 10 kΩ dissipates 2.5 mW — clean.
    assert (
        drc.check_passive_rating(
            nets, [_inst("R1", "10kΩ ±1% 100mW 0402 Chip Resistor")]
        )
        == []
    )


def test_passive_rating_resistor_series_current_fallback() -> None:
    """Voltage unknown but a ``series`` pin's net declares current: I²R
    still answers the dissipation question."""
    nets = [
        _net(
            "LOAD",
            _m("R1", "1", meta={"current_role": "series"}),
            est_current_a=0.2,
        ),
        _net("OUT", _m("R1", "2")),
    ]
    # 0.2 A through 10 Ω = 0.4 W > 0.05 W allowed → error.
    findings = drc.check_passive_rating(
        nets, [_inst("R1", "10Ω ±1% 100mW 0402 Chip Resistor")]
    )
    assert [f.severity for f in findings] == ["error"]
    assert "0.4 W" in findings[0].detail
    # … and with nothing derivable at all, the ask-the-user row.
    findings = drc.check_passive_rating(
        [_net("A", _m("R1", "1")), _net("B", _m("R1", "2"))],
        [_inst("R1", "10kΩ ±1% 100mW 0402 Chip Resistor")],
    )
    assert [(f.severity, f.objects[0].get("not_checked")) for f in findings] == [
        ("warn", True)
    ]
    assert "voltage unknown for net A: ask the user" in findings[0].detail


# ── the rule: cannot-fire rows ────────────────────────────────────────


def test_passive_rating_not_checked_without_catalog_parts() -> None:
    nets = [_net("VCC", _m("U1", "VDD"), working_voltage_v=5.0)]
    findings = drc.check_passive_rating(nets, [_inst("U1", None)])
    assert [(f.rule, f.severity) for f in findings] == [("passive_rating", "warn")]
    assert "CANNOT be checked" in findings[0].detail


def test_passive_rating_unknown_net_voltage_asks_not_guesses() -> None:
    """Reto's never-guess ruling, again: a parsed rated cap on a net
    with no declared voltage is an ask-the-user row."""
    nets = [
        _net("VCC", _m("C4", "1")),
        _net("GND", _m("C4", "2"), working_voltage_v=0.0),
    ]
    findings = drc.check_passive_rating(
        nets, [_inst("C4", "100nF ±10% 16V X7R 0402 Capacitors")]
    )
    assert [(f.severity, f.objects[0].get("not_checked")) for f in findings] == [
        ("warn", True)
    ]
    assert "voltage unknown for net VCC: ask the user" in findings[0].detail


def test_passive_rating_cap_without_rating_is_info() -> None:
    """A parsed cap whose description carries no voltage cannot be
    margined — a per-part not-checked row, not a pass."""
    nets = [
        _net("VCC", _m("C4", "1"), working_voltage_v=5.0),
        _net("GND", _m("C4", "2"), working_voltage_v=0.0),
    ]
    findings = drc.check_passive_rating(
        nets, [_inst("C4", "100nF X7R 0402 Multilayer Ceramic Capacitors")]
    )
    assert [(f.severity, f.objects[0].get("not_checked")) for f in findings] == [
        ("warn", True)
    ]
    assert "no voltage rating" in findings[0].detail


# ── op='pins' min_dielectric ──────────────────────────────────────────


def test_op_pins_min_dielectric(pcb: PcbHandler, store: Any) -> None:
    pcb.put(
        id="pr-diel",
        args={
            "components": [
                {
                    "refdes": "U1",
                    "label": "ic",
                    "x": 0.0,
                    "y": 0.0,
                    "pins": [{"name": "VDD"}, {"name": "GND"}],
                }
            ],
            "nets": [{"name": "VCC"}, {"name": "GND"}],
            "connections": [
                {"net": "VCC", "refdes": "U1", "pin": "VDD"},
                {"net": "GND", "refdes": "U1", "pin": "GND"},
            ],
        },
    )
    # An unranked class is refused and names the vocabulary.
    with pytest.raises(Exception, match="min_dielectric"):
        pcb.put(
            id="pr-diel",
            args={
                "op": "pins",
                "refdes": "U1",
                "pins": [{"name": "VDD", "min_dielectric": "flux"}],
            },
        )
    pcb.put(
        id="pr-diel",
        args={
            "op": "pins",
            "refdes": "U1",
            "pins": [{"name": "VDD", "min_dielectric": "X7R"}],
        },
    )
    ref = store.get_ref(kind="pcb", id="pr-diel")
    member = next(
        m
        for n in store.pcb_graph(ref.id)["nets"]
        for m in n["members"]
        if (m["refdes"], m["pin"]) == ("U1", "VDD")
    )
    # Canonicalised to the rank table's keyspace.
    assert member["meta"]["min_dielectric"] == "x7r"


# ── end to end on view='check' + the #REFDES derived fact ─────────────


def _catalog_design() -> dict[str, Any]:
    return {
        "components": [
            {
                "refdes": "U1",
                "label": "ic",
                "x": 10.0,
                "y": 10.0,
                "pins": [{"name": "VDD"}, {"name": "GND"}],
            },
            {
                "refdes": "C4",
                "label": "bypass",
                "part_lcsc": "C1525",
                "x": 20.0,
                "y": 10.0,
                "pins": [{"name": "1"}, {"name": "2"}],
            },
            {
                "refdes": "R1",
                "label": "load",
                "part_lcsc": "C25804",
                "x": 30.0,
                "y": 10.0,
                "pins": [{"name": "1"}, {"name": "2"}],
            },
        ],
        "nets": [{"name": "VCC"}, {"name": "GND"}],
        "connections": [
            {"net": "VCC", "refdes": "U1", "pin": "VDD"},
            {"net": "VCC", "refdes": "C4", "pin": "1"},
            {"net": "VCC", "refdes": "R1", "pin": "1"},
            {"net": "GND", "refdes": "U1", "pin": "GND"},
            {"net": "GND", "refdes": "C4", "pin": "2"},
            {"net": "GND", "refdes": "R1", "pin": "2"},
        ],
    }


def _seed_catalog(store: Any) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO parts (lcsc, package, description) "
            "VALUES ('C1525', '0402', %s), ('C25804', '0402', %s)",
            (
                "100nF ±10% 6.3V Y5V 0402 Multilayer Ceramic Capacitors MLCC",
                "10Ω ±1% 100mW 0402 Chip Resistor - Surface Mount",
            ),
        )


def test_passive_rating_end_to_end(pcb: PcbHandler, store: Any) -> None:
    _seed_catalog(store)
    slug = "pr-e2e"
    pcb.put(id=slug, args=_catalog_design())
    # Nothing annotated: every parsed passive reports its gap, not a
    # guess — C4's rail voltage is unknown, R1's too.
    body = pcb.get(id=slug, view="check").body
    assert "passive_rating CANNOT check C4" in body
    assert "voltage unknown for net VCC: ask the user" in body
    # Now the rails carry voltage and U1's VDD asks for ≥ X7R.
    pcb.put(
        id=slug,
        args={
            "nets": [
                {"name": "VCC", "voltage": 5.0, "rail": "vcc"},
                {"name": "GND", "voltage": 0, "rail": "gnd"},
            ]
        },
    )
    pcb.put(
        id=slug,
        args={
            "op": "pins",
            "refdes": "U1",
            "source": "u1 datasheet p.4",
            "pins": [
                {
                    "name": "VDD",
                    "etype": "power_in",
                    "rail": "vcc",
                    "min_dielectric": "x7r",
                },
                {"name": "GND", "etype": "power_in", "rail": "gnd"},
            ],
        },
    )
    body = pcb.get(id=slug, view="check").body
    # C4 (Y5V, 6.3 V across 5 V): both the dielectric and the margin
    # verdicts fire, each naming its numbers.
    assert re.search(r"passive_rating\t", body) or "passive_rating" in body
    assert "C4 is y5v" in body
    assert "rated 6.3 V across 5 V" in body
    # R1: 10 Ω across 5 V dissipates 2.5 W > 50 % of 0.1 W.
    assert "dissipates ~2.5 W" in body
    # No not-checked rows remain for passive_rating.
    assert "passive_rating CANNOT" not in body
    # And #REFDES shows the derived fact.
    body = pcb.get(id=f"{slug}#C4").body
    assert "catalog: C1525" in body
    assert "capacitor" in body and "6.3 V" in body and "y5v" in body


def test_passive_rating_clears_after_swap(pcb: PcbHandler, store: Any) -> None:
    """The finding keys to the part — swap the catalog part for an
    adequately rated one and the verdict clears."""
    _seed_catalog(store)
    slug = "pr-swap"
    pcb.put(id=slug, args=_catalog_design())
    pcb.put(
        id=slug,
        args={
            "nets": [
                {"name": "VCC", "voltage": 5.0},
                {"name": "GND", "voltage": 0},
            ]
        },
    )
    body = pcb.get(id=slug, view="check").body
    assert "rated 6.3 V across 5 V" in body
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE parts SET description = %s WHERE lcsc = 'C1525'",
            ("100nF ±10% 16V X7R 0402 Multilayer Ceramic Capacitors MLCC",),
        )
    body = pcb.get(id=slug, view="check").body
    assert "rated 6.3 V across 5 V" not in body
    # R1's verdict (2.5 W into a 0.1 W part) still stands.
    assert "dissipates ~2.5 W" in body

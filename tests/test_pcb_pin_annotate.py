"""Slice 2 of docs/backlog/pcb-datasheet-review.md — ``op='pins'`` pin
annotation on existing parts + ``max_current_a`` + the ``over_current``
ERC rule.

An imported board (or any re-put) could never declare pin semantics:
``_pcb_apply`` is create-or-extend, not a pin editor. ``op='pins'`` is
that editor — bulk per-refdes annotation validated against the
component's real pin names, idempotent, origin-recorded (``source``).

Covered here, per the slice's accept list:

* annotating a name-only part turns each ERC rule's "CANNOT be checked"
  info row into a real verdict on ``view='check'``;
* a bad pin name refuses the call and lists the part's valid pins;
* ``over_current`` fires (net ``est_current_a`` > pin ``max_current_a``)
  and clears, with ``current_role='shunt'`` the one exemption;
* a bound pin on a net with no declared voltage/current reports
  ``voltage|current unknown for net X: ask the user`` — never guessed.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import drc

pytestmark = pytest.mark.db


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _net(name: str, *members: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {"name": name, "members": list(members), **kw}


def _m(refdes: str, pin: str, *, tags=(), meta=None) -> dict[str, Any]:
    return {"refdes": refdes, "pin": pin, "tags": list(tags), "meta": dict(meta or {})}


# ── unit level: over_current and the never-guess-ask rows ─────────────


def test_over_current_fires_above_pin_rating() -> None:
    nets = [
        _net(
            "PWR",
            _m("J1", "1", tags=["power_out"]),
            _m(
                "U1",
                "VIN",
                tags=["power_in"],
                meta={"max_current_a": 1.0, "current_role": "series"},
            ),
            est_current_a=3.0,
        )
    ]
    findings = drc.check_over_current(nets)
    assert [(f.rule, f.severity, f.objects[0]["refdes"]) for f in findings] == [
        ("over_current", "error", "U1")
    ]
    # At and below the rating: clean.
    assert (
        drc.check_over_current([_net("PWR", *nets[0]["members"], est_current_a=1.0)])
        == []
    )
    assert (
        drc.check_over_current([_net("PWR", *nets[0]["members"], est_current_a=0.5)])
        == []
    )


def test_over_current_shunt_is_the_only_exemption() -> None:
    """A ``shunt`` pin taps the net without carrying its through current;
    ``signal``, ``series`` and an undeclared role all still compare."""
    for role in ("signal", "series", None):
        meta: dict[str, Any] = {"max_current_a": 0.5}
        if role is not None:
            meta["current_role"] = role
        nets = [
            _net(
                "PWR",
                _m("U1", "P", tags=["passive"], meta=meta),
                est_current_a=3.0,
            )
        ]
        assert [f.rule for f in drc.check_over_current(nets)] == ["over_current"]
    nets = [
        _net(
            "PWR",
            _m(
                "C1",
                "1",
                tags=["passive"],
                meta={"max_current_a": 0.5, "current_role": "shunt"},
            ),
            est_current_a=3.0,
        )
    ]
    assert drc.check_over_current(nets) == []


def test_over_current_unknown_net_current_asks_not_guesses() -> None:
    """A bound pin on a net nobody gave an ``est_current_a`` is a per-net
    ask, not a guess and not a silent skip."""
    nets = [
        _net(
            "PWR",
            _m("U1", "VIN", meta={"max_current_a": 1.0}),
        )
    ]
    findings = drc.check_over_current(nets)
    assert [(f.rule, f.severity) for f in findings] == [("over_current", "warn")]
    assert "current unknown for net PWR: ask the user" in findings[0].detail
    assert findings[0].objects[0]["not_checked"] is True


def test_over_current_not_checked_when_vocabulary_absent() -> None:
    nets = [_net("SIG", _m("U1", "A"), _m("U2", "B"))]
    findings = drc.check_over_current(nets)
    assert [(f.rule, f.severity) for f in findings] == [("over_current", "warn")]
    assert "CANNOT be checked" in findings[0].detail


def test_over_voltage_unknown_net_voltage_asks_not_guesses() -> None:
    """Reto's ruling: a bound pin on a net with no declared voltage is a
    per-net ``voltage unknown … ask the user`` row — an imported board's
    rail voltages are never guessed."""
    nets = [
        _net(
            "HV",
            _m("U2", "IN", tags=["input"], meta={"max_voltage_v": 12}),
        )
    ]
    findings = drc.check_over_voltage(nets)
    assert [(f.rule, f.severity) for f in findings] == [("over_voltage", "warn")]
    assert "voltage unknown for net HV: ask the user" in findings[0].detail
    assert findings[0].objects[0]["not_checked"] is True


# ── integration: op='pins' on an annotation-free design ────────────────


def _imported_like_design() -> dict[str, Any]:
    """The shape an epro import leaves: pins carry names only (no
    etype/rail/bounds), so every ERC rule reports CANNOT be checked
    until someone annotates."""
    return {
        "components": [
            {
                "refdes": "J1",
                "label": "conn",
                "x": 0.0,
                "y": 0.0,
                "pins": [{"name": "1"}, {"name": "2"}],
            },
            {
                "refdes": "U1",
                "label": "driver",
                "x": 10.0,
                "y": 10.0,
                "pins": [
                    {"name": "HVOUT"},
                    {"name": "VDD"},
                    {"name": "GPIO"},
                    {"name": "GND"},
                ],
            },
            {
                "refdes": "U2",
                "label": "sink",
                "x": 30.0,
                "y": 10.0,
                "pins": [{"name": "HVIN"}, {"name": "SDA"}, {"name": "GND"}],
            },
            {
                "refdes": "C1",
                "label": "bypass",
                "x": 40.0,
                "y": 10.0,
                "pins": [{"name": "1"}, {"name": "2"}],
            },
        ],
        "nets": [
            {"name": "HV"},
            {"name": "VCC"},
            {"name": "GND"},
            {"name": "I2C_SDA"},
        ],
        "connections": [
            {"net": "HV", "refdes": "U1", "pin": "HVOUT"},
            {"net": "HV", "refdes": "U2", "pin": "HVIN"},
            {"net": "VCC", "refdes": "J1", "pin": "1"},
            {"net": "VCC", "refdes": "U1", "pin": "VDD"},
            {"net": "VCC", "refdes": "C1", "pin": "1"},
            {"net": "GND", "refdes": "J1", "pin": "2"},
            {"net": "GND", "refdes": "U1", "pin": "GND"},
            {"net": "GND", "refdes": "U2", "pin": "GND"},
            {"net": "GND", "refdes": "C1", "pin": "2"},
            {"net": "I2C_SDA", "refdes": "U1", "pin": "GPIO"},
            {"net": "I2C_SDA", "refdes": "U2", "pin": "SDA"},
        ],
    }


def test_op_pins_annotates_existing_part(pcb: PcbHandler, store: Any) -> None:
    pcb.put(id="ann-basic", args=_imported_like_design())
    out = pcb.put(
        id="ann-basic",
        args={
            "op": "pins",
            "refdes": "U1",
            "source": "esp32-s3 datasheet",
            "pins": [
                {"name": "VDD", "etype": "power_in", "rail": "vcc"},
                {"name": "GPIO", "etype": "bidi", "rail": "3v3", "max_voltage_v": 3.6},
                {"name": "HVOUT", "etype": "output", "rail": "3v3"},
            ],
        },
    )
    assert "3 pin(s) annotated" in out.body
    ref = store.get_ref(kind="pcb", id="ann-basic")
    members = {
        (m["refdes"], m["pin"]): m
        for n in store.pcb_graph(ref.id)["nets"]
        for m in n["members"]
    }
    vdd = members[("U1", "VDD")]
    assert "power_in" in vdd["tags"]
    assert vdd["meta"]["rail"] == "vcc"
    # args.source is the default origin for pins that don't name their own.
    assert vdd["meta"]["source"] == "esp32-s3 datasheet"
    gpio = members[("U1", "GPIO")]
    assert gpio["meta"]["max_voltage_v"] == 3.6


def test_op_pins_is_idempotent(pcb: PcbHandler) -> None:
    pcb.put(id="ann-idem", args=_imported_like_design())
    call = {
        "op": "pins",
        "refdes": "U1",
        "pins": [{"name": "VDD", "etype": "power_in", "rail": "vcc"}],
    }
    pcb.put(id="ann-idem", args=call)
    out = pcb.put(id="ann-idem", args=call)
    assert "0 pin(s) annotated" in out.body
    assert "already as declared" in out.body


def test_op_pins_rejects_bad_pin_name_and_lists_valid(pcb: PcbHandler) -> None:
    pcb.put(id="ann-bad", args=_imported_like_design())
    with pytest.raises(Exception, match="no pin 'VCC'") as exc:
        pcb.put(
            id="ann-bad",
            args={"op": "pins", "refdes": "U1", "pins": [{"name": "VCC"}]},
        )
    # The refusal names the component's real pins.
    for real in ("HVOUT", "VDD", "GPIO", "GND"):
        assert real in str(exc.value)


def test_op_pins_validates_fields(pcb: PcbHandler) -> None:
    pcb.put(id="ann-val", args=_imported_like_design())
    # pad binding stays out (the canonical-pad rule stands).
    with pytest.raises(Exception, match="pad"):
        pcb.put(
            id="ann-val",
            args={
                "op": "pins",
                "refdes": "U1",
                "pins": [{"name": "VDD", "pad": "4"}],
            },
        )
    with pytest.raises(Exception, match="etype"):
        pcb.put(
            id="ann-val",
            args={
                "op": "pins",
                "refdes": "U1",
                "pins": [{"name": "VDD", "etype": "flux_capacitor"}],
            },
        )
    with pytest.raises(Exception, match="current_role"):
        pcb.put(
            id="ann-val",
            args={
                "op": "pins",
                "refdes": "U1",
                "pins": [{"name": "VDD", "current_role": "sideways"}],
            },
        )
    with pytest.raises(Exception, match="number"):
        pcb.put(
            id="ann-val",
            args={
                "op": "pins",
                "refdes": "U1",
                "pins": [{"name": "VDD", "max_current_a": "lots"}],
            },
        )
    with pytest.raises(Exception, match="twice"):
        pcb.put(
            id="ann-val",
            args={
                "op": "pins",
                "refdes": "U1",
                "pins": [{"name": "VDD"}, {"name": "VDD"}],
            },
        )
    with pytest.raises(Exception, match="refdes"):
        pcb.put(
            id="ann-val",
            args={"op": "pins", "pins": [{"name": "VDD"}]},
        )
    with pytest.raises(Exception, match="no live instance"):
        pcb.put(
            id="ann-val",
            args={"op": "pins", "refdes": "U99", "pins": [{"name": "VDD"}]},
        )


def test_op_pins_clears_with_null(pcb: PcbHandler, store: Any) -> None:
    pcb.put(id="ann-clear", args=_imported_like_design())
    pcb.put(
        id="ann-clear",
        args={
            "op": "pins",
            "refdes": "U1",
            "pins": [{"name": "VDD", "etype": "power_in", "rail": "vcc"}],
        },
    )
    out = pcb.put(
        id="ann-clear",
        args={
            "op": "pins",
            "refdes": "U1",
            "pins": [{"name": "VDD", "etype": None, "rail": None}],
        },
    )
    assert "1 pin(s) annotated" in out.body
    ref = store.get_ref(kind="pcb", id="ann-clear")
    member = next(
        m
        for n in store.pcb_graph(ref.id)["nets"]
        for m in n["members"]
        if (m["refdes"], m["pin"]) == ("U1", "VDD")
    )
    assert "power_in" not in member["tags"]
    assert "rail" not in member["meta"]


def test_annotation_turns_erc_info_rows_into_verdicts(
    pcb: PcbHandler,
) -> None:
    """The slice's goal: an annotation-free board reports every ERC rule
    as CANNOT be checked; after ``op='pins'`` (+ the existing nets[]
    voltage/current patch path) the same rules return real verdicts —
    ``over_voltage``/``rail_crossing``/``over_current`` errors and a
    ``no_decoupling`` warn on this fixture."""
    slug = "ann-erc"
    pcb.put(id=slug, args=_imported_like_design())
    body = pcb.get(id=slug, view="check").body
    for rule in (
        "pin_conflict",
        "no_driver",
        "over_voltage",
        "rail_crossing",
        "no_decoupling",
        "over_current",
    ):
        assert f"{rule} CANNOT be checked" in body

    pcb.put(
        id=slug,
        args={
            "op": "pins",
            "refdes": "J1",
            "pins": [
                {"name": "1", "etype": "power_out", "rail": "vcc"},
                {"name": "2", "etype": "power_out", "rail": "gnd"},
            ],
        },
    )
    pcb.put(
        id=slug,
        args={
            "op": "pins",
            "refdes": "U1",
            "source": "esp32-s3 datasheet",
            "pins": [
                {"name": "HVOUT", "etype": "output", "rail": "3v3"},
                {"name": "VDD", "etype": "power_in", "rail": "vcc"},
                {"name": "GPIO", "etype": "bidi", "rail": "3v3", "max_voltage_v": 3.6},
                {"name": "GND", "etype": "power_in", "rail": "gnd"},
            ],
        },
    )
    pcb.put(
        id=slug,
        args={
            "op": "pins",
            "refdes": "U2",
            "pins": [
                {
                    "name": "HVIN",
                    "etype": "input",
                    "rail": "hv20",
                    "max_voltage_v": 12,
                    "max_current_a": 1.0,
                    "current_role": "signal",
                },
                {"name": "SDA", "etype": "bidi", "rail": "3v3", "max_voltage_v": 3.6},
                {"name": "GND", "etype": "power_in", "rail": "gnd"},
            ],
        },
    )
    pcb.put(
        id=slug,
        args={
            "op": "pins",
            "refdes": "C1",
            "pins": [
                {"name": "1", "etype": "passive"},
                {"name": "2", "etype": "passive"},
            ],
        },
    )
    # Net-side annotations ride the existing re-put patch path.
    pcb.put(
        id=slug,
        args={
            "nets": [
                {"name": "HV", "voltage": 20, "current": 3.0},
                {"name": "VCC", "voltage": 3.3, "rail": "vcc", "current": 0.5},
                {"name": "GND", "voltage": 0, "rail": "gnd"},
            ]
        },
    )

    body = pcb.get(id=slug, view="check").body
    # No ERC rule is blind any more (unrelated not-checked rows — e.g.
    # undeclared_pad on a footprint-less part — still say so).
    for rule in (
        "pin_conflict",
        "no_driver",
        "over_voltage",
        "rail_crossing",
        "no_decoupling",
        "over_current",
    ):
        assert f"{rule} CANNOT be checked" not in body
    # over_voltage: 20 V on U2's 12 V pin.
    assert "over_voltage" in body
    assert "works at 20 V" in body and "rated 12 V max" in body
    # rail_crossing: HV joins rails '3v3' and 'hv20'.
    assert "rail_crossing" in body
    # over_current: HV at 3 A through U2's 1 A pin.
    assert "over_current" in body
    assert "carries 3 A" in body and "rated 1 A max" in body
    # no_decoupling: U1/VDD's bypass cap is 30 mm away.
    assert "no_decoupling" in body
    # I2C_SDA pins declare bounds but the net declares no voltage:
    # the ask-the-user row, never a guess.
    assert "voltage unknown for net I2C_SDA: ask the user" in body
    # pin_conflict / no_driver reached verdicts (clean here) — no
    # not-checked rows survive for them.
    assert "pin_conflict CANNOT" not in body
    assert "no_driver CANNOT" not in body


def test_over_current_fires_and_clears(pcb: PcbHandler) -> None:
    slug = "ann-oc"
    pcb.put(id=slug, args=_imported_like_design())
    pcb.put(
        id=slug,
        args={
            "op": "pins",
            "refdes": "U2",
            "pins": [{"name": "HVIN", "etype": "input", "max_current_a": 1.0}],
        },
    )
    pcb.put(id=slug, args={"nets": [{"name": "HV", "current": 3.0}]})
    body = pcb.get(id=slug, view="check").body
    assert "over_current" in body
    assert "rated 1 A max" in body
    # Re-annotate the rating up — the finding clears (the `gone` delta
    # row still names it; no live table row does).
    pcb.put(
        id=slug,
        args={
            "op": "pins",
            "refdes": "U2",
            "pins": [{"name": "HVIN", "max_current_a": 5.0}],
        },
    )
    body = pcb.get(id=slug, view="check").body
    assert "resolved" in body or "gone" in body
    assert not re.search(r"\t(error|warn|todo|info)\tover_current\t", body)
    # And clearing the bound entirely does the same.
    pcb.put(
        id=slug,
        args={"nets": [{"name": "HV", "current": 9.0}]},
    )
    pcb.put(
        id=slug,
        args={
            "op": "pins",
            "refdes": "U2",
            "pins": [{"name": "HVIN", "max_current_a": None}],
        },
    )
    body = pcb.get(id=slug, view="check").body
    assert "over_current CANNOT be checked" in body

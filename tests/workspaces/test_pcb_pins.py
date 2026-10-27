"""Slice 1b wheel side (pcb-topo-place §1b): the ``precis_geom.pcb``
pin-declaration API — ``Part.pin`` fixed claims and ``any_of`` slots,
``pin_functions``/``pins_with``, ``swap_group``,
``net.tie(current=, source=)`` — plus the offline electrical checks
``over_current`` and ``led_without_limit`` (Decision 15). Solver-side
coverage lives in ``tests/test_pcb_topoplace_pins.py``."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

import precis_geom.pcb as pcb
from precis_geom.pcb import Board, FlexPin, PcbBuilderError, PinConflict
from tests.workspaces._script import load_script

#: Claim sites are ``file:line``; keep the pin() calls on known lines.
_CONFLICT_MAIN = """\
import precis_geom.pcb as pcb


def build() -> pcb.Board:
    b = pcb.Board("t")
    u1 = b.part("U1")
    u1.pin(5, name="BTN")
    u1.pin(5, name="LED")
    return b
"""


def _led_board(*, series: bool) -> Board:
    """GPIO → [N1] → R1 → [N2] → LED anode; cathode to GND. ``series``
    gives R1 a ``current_role("series")`` pin — the limiter the check
    asks for."""
    b = Board("t")
    u1, r1, d1 = b.part("U1"), b.part("R1"), b.part("D1")
    d1.pin_functions({"A": ["LED"]})
    n1, n2, gnd = b.net("N1"), b.net("N2"), b.net("GND")
    n1.tie(u1["PA1"], r1["1"], current=pcb.Max("20mA"), source="ds:9~c4")
    if series:
        r1["1"].current_role("series")
    n2.tie(r1["2"], d1["A"])
    gnd.tie(d1["K"])
    return b


def test_fixed_pin_conflict_names_both_sites(tmp_path: Path) -> None:
    """Decision 12: ``pin(5)`` claimed as BTN then as LED raises
    PinConflict naming both claim sites (name + file:line)."""
    mod = load_script(tmp_path, _CONFLICT_MAIN)
    with pytest.raises(PinConflict) as exc:
        mod.build()
    msg = str(exc.value)
    assert "'BTN'" in msg and "'LED'" in msg
    assert "main.py:7" in msg and "main.py:8" in msg


_SITE_MAIN = """\
import precis_geom.pcb as pcb


def build() -> pcb.Board:
    b = pcb.Board("t")
    u1 = b.part("U1")
    u1.pin(5, name="BTN")
    u1.pin(any_of=["PA1", "PA2"], name="SLOT")
    return b
"""


def test_pin_decl_sites_carry_no_absolute_path(tmp_path: Path) -> None:
    """gr520324: a claim ``site`` is relative to the calling script's
    directory — ``basename:line`` outside it — so ``to_batch()`` never
    leaks an absolute laptop path into ``pin_decls``."""
    mod = load_script(tmp_path, _SITE_MAIN)
    decls = mod.build().to_batch()["components"][0]["pin_decls"]
    sites = [c["site"] for c in decls["claims"]] + [s["site"] for s in decls["slots"]]
    assert sites == ["main.py:7", "main.py:8"]
    for site in sites:
        path, _, line = site.rpartition(":")
        assert not Path(path).is_absolute() and line.isdigit()


def test_pin_decl_site_relative_to_argv0_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr520324 follow-up: when ``sys.argv[0]`` names the entry script —
    the sandbox's ``python /work/main.py`` shape — its directory anchors
    the site, giving ``main.py:N`` via ``relative_to`` rather than the
    basename fallback the pytest argv0 exercises above."""
    mod = load_script(tmp_path, _SITE_MAIN)
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "main.py")])
    pcb._script_dir.cache_clear()
    try:
        assert pcb._script_dir() == (tmp_path / "main.py").resolve().parent
        decls = mod.build().to_batch()["components"][0]["pin_decls"]
    finally:
        pcb._script_dir.cache_clear()
    sites = [c["site"] for c in decls["claims"]] + [s["site"] for s in decls["slots"]]
    assert sites == ["main.py:7", "main.py:8"]


def test_fixed_pin_reclaim_same_name_is_noop() -> None:
    """The same name claimed again is idempotent — one claims entry."""
    b = Board("t")
    u1 = b.part("U1")
    u1.pin(5, name="BTN")
    assert u1.pin(5, name="BTN").name == "5"
    (claim,) = b.to_batch()["components"][0]["pin_decls"]["claims"]
    assert claim["pin"] == "5" and claim["name"] == "BTN"


def test_pin_declaration_errors() -> None:
    b = Board("t")
    u1 = b.part("U1")
    with pytest.raises(PcbBuilderError, match="needs a pin number"):
        u1.pin()
    with pytest.raises(PcbBuilderError, match="pin number OR any_of"):
        u1.pin(5, any_of=[1, 4])
    with pytest.raises(PcbBuilderError, match="≥1 pin"):
        u1.pin(any_of=[])
    with pytest.raises(PcbBuilderError, match="repeats a pin"):
        u1.pin(any_of=[1, 1])
    with pytest.raises(PcbBuilderError, match="≥2 members"):
        u1.swap_group([1])
    with pytest.raises(PcbBuilderError, match="equal-length"):
        u1.swap_group([(1, 8), (2,)], tuple=True)
    bad: Any = "20mA"
    with pytest.raises(PcbBuilderError, match=r"pcb\.Max/pcb\.Min"):
        b.net("N").tie(u1["1"], current=bad)


def test_flex_pin_tie_records_net_and_conflicts() -> None:
    """Tying a slot binds its net for the solver — no concrete
    connection is emitted; a second net raises PinConflict."""
    b = Board("t")
    u1 = b.part("U1")
    fp = u1.pin(any_of=["PA1", "PA2"], name="RED_LED")
    assert isinstance(fp, FlexPin)
    n1, n2 = b.net("N1"), b.net("N2")
    n1.tie(fp, current=pcb.Max("20mA"), source="ds:9~c4")
    assert fp.net == "N1"
    assert fp.requirement is not None and fp.requirement.amps == pytest.approx(0.02)
    assert fp.source == "ds:9~c4"
    assert b.to_batch()["connections"] == []
    with pytest.raises(PinConflict, match="tied to net 'N1'"):
        n2.tie(fp)


def test_tie_emits_current_and_source() -> None:
    """Decision 15: ``current=``/``source=`` land on the connection as
    checkable fields, not a comment."""
    b = Board("t")
    u1, r1 = b.part("U1"), b.part("R1")
    b.net("N").tie(u1["PA1"], r1["1"], current=pcb.Max("20mA"), source="ds:9~c4")
    b.net("M").tie(u1["PA2"], current=pcb.Min(0.1))
    assert b.to_batch()["connections"] == [
        {
            "net": "N",
            "refdes": "U1",
            "pin": "PA1",
            "current_max_a": 0.02,
            "current": "20mA",
            "source": "ds:9~c4",
        },
        {
            "net": "N",
            "refdes": "R1",
            "pin": "1",
            "current_max_a": 0.02,
            "current": "20mA",
            "source": "ds:9~c4",
        },
        {
            "net": "M",
            "refdes": "U1",
            "pin": "PA2",
            "current_min_a": 0.1,
            "current": "0.1",
        },
    ]


def test_pin_decls_batch_emission() -> None:
    """All four declaration kinds ride the component's ``pin_decls``
    batch key (the store carries it in component meta for the solver)."""
    b = Board("t")
    u1 = b.part("U1")
    u1.pin(5, name="BTN")
    u1.pin_functions(
        {
            "PA1": ["GPIO", "INT1"],
            "PA2": {"functions": ["GPIO"], "max_current_a": 0.02},
        },
        source="ds:9~c3",
    )
    assert u1.pins_with("GPIO") == ["PA1", "PA2"]
    assert u1.pins_with("INT1") == ["PA1"]
    fp = u1.pin(any_of=u1.pins_with("GPIO"), name="RED_LED")
    b.net("SIG").tie(fp, current=pcb.Max("20mA"), source="ds:9~c4")
    rn1 = b.part("RN1")
    rn1.swap_group([(1, 8), (2, 7), (3, 6), (4, 5)], tuple=True)
    comps = {c["refdes"]: c for c in b.to_batch()["components"]}
    decls = comps["U1"]["pin_decls"]
    (claim,) = decls["claims"]
    assert claim["pin"] == "5" and claim["name"] == "BTN"
    assert "test_pcb_pins.py:" in claim["site"]
    (slot,) = decls["slots"]
    assert slot["name"] == "RED_LED"
    assert slot["pins"] == ["PA1", "PA2"]
    assert slot["net"] == "SIG"
    assert slot["requirement_a"] == pytest.approx(0.02)
    assert slot["requirement_kind"] == "max"
    assert slot["requirement"] == "20mA"
    assert slot["source"] == "ds:9~c4"
    assert decls["pin_functions"] == {
        "PA1": {"functions": ["GPIO", "INT1"]},
        "PA2": {"functions": ["GPIO"], "max_current_a": 0.02},
    }
    assert decls["pin_functions_source"] == "ds:9~c3"
    # Declared pins reach the batch live — the solver needs real pad
    # positions even where no wire lands on them yet.
    assert [p["name"] for p in comps["U1"]["pins"]] == ["5", "PA1", "PA2"]
    assert comps["RN1"]["pin_decls"]["swap_groups"] == [
        {
            "members": [["1", "8"], ["2", "7"], ["3", "6"], ["4", "5"]],
            "tuple": True,
        }
    ]


def test_over_current_wired_pin() -> None:
    """A wired pin whose declared ``max_current_a`` sits under the net's
    ``current=Max`` requirement is ``over_current``."""
    b = Board("t")
    u1, r1 = b.part("U1"), b.part("R1")
    u1.pin_functions({"PA1": {"functions": ["GPIO"], "max_current_a": 0.005}})
    n = b.net("LED")
    n.tie(u1["PA1"], current=pcb.Max("20mA"), source="ds:9~c4")
    n.tie(r1["1"])
    # source.unchecked is the separate "no client staged" note for the
    # source= handle (gr521599), not part of the electrical verdict.
    (f,) = [x for x in b.check().findings if x.code != "source.unchecked"]
    assert f.code == "over_current"
    assert f.refdes == "U1" and f.net == "LED"


def test_slot_bound_net_is_not_unconnected() -> None:
    """gr516695: a net tied only to a ``FlexPin`` slot counts as
    connected — the solver wires the concrete pin at assign time. A
    declared net with no tie at all still floats."""
    b = Board("t")
    u1 = b.part("U1")
    b.net("SIG").tie(u1.pin(any_of=["PA1", "PA2"], name="BTN"))
    b.net("DANGLING")
    assert [(f.code, f.net) for f in b.check().findings] == [
        ("net.unconnected", "DANGLING")
    ]
    # an untied slot binds nothing — its net stays floating
    b2 = Board("t2")
    b2.part("U1").pin(any_of=["PA1", "PA2"], name="BTN")
    b2.net("SIG")
    assert [
        (f.code, f.net) for f in b2.check().findings if f.code == "net.unconnected"
    ] == [("net.unconnected", "SIG")]


def test_over_current_dedupes_per_net() -> None:
    """gr516696: N ties carrying ``current=`` on one net produce ONE
    over_current finding per under-rated pin, against the strictest
    bound — the finding is per pin per net, not per connection."""
    b = Board("t")
    u1, r1 = b.part("U1"), b.part("R1")
    u1.pin_functions({"PA1": {"functions": ["GPIO"], "max_current_a": 0.005}})
    n = b.net("LED")
    n.tie(u1["PA1"], r1["1"], current=pcb.Max("20mA"), source="ds:9~c4")
    n.tie(r1["2"], current=pcb.Max("30mA"), source="ds:9~c5")
    (f,) = [f for f in b.check().findings if f.code == "over_current"]
    assert f.refdes == "U1" and f.net == "LED"
    assert "≤ 0.02 A" in f.message


def test_over_current_slot_all_candidates_rated_low() -> None:
    """Every rated candidate under the bound leaves the slot no legal
    pin; one unrated candidate clears it (Decision 15 filters only where
    the library has a rating)."""
    b = Board("t")
    u1 = b.part("U1")
    u1.pin_functions(
        {
            "PA1": {"functions": ["GPIO"], "max_current_a": 0.005},
            "PA2": {"functions": ["GPIO"], "max_current_a": 0.004},
        }
    )
    b.net("N").tie(u1.pin(any_of=["PA1", "PA2"], name="PWR"), current=pcb.Max("20mA"))
    # (the slot tie emits no connection row but binds the net — it is no
    # longer net.unconnected, gr516695.)
    (f,) = [f for f in b.check().findings if f.code == "over_current"]
    assert f.refdes == "U1" and f.net == "N"

    b2 = Board("t2")
    u2 = b2.part("U1")
    u2.pin_functions({"PA1": {"functions": ["GPIO"], "max_current_a": 0.005}})
    b2.net("N").tie(u2.pin(any_of=["PA1", "PB0"], name="PWR"), current=pcb.Max("20mA"))
    assert not [f for f in b2.check().findings if f.code == "over_current"]


def test_led_without_limit_on_requirement_net() -> None:
    """An LED pin (a ``pin_functions`` entry whose function starts
    ``LED``) sitting directly on a current-requirement net is
    ``led_without_limit``."""
    b = Board("t")
    u1, d1 = b.part("U1"), b.part("D1")
    d1.pin_functions({"A": ["LED"]})
    b.net("LED_N").tie(u1["PA1"], d1["A"], current=pcb.Max("20mA"))
    b.net("GND").tie(d1["K"])
    (f,) = b.check().findings
    assert f.code == "led_without_limit"
    assert f.refdes == "D1" and f.net == "LED_N"


def test_led_without_limit_bridge_and_series_resistor() -> None:
    """The reach from a requirement net crosses non-limiting two-pin
    bridges — the LED fires — and stops at a ``current_role("series")``
    part, the fix the check asks for."""
    fires = [f.code for f in _led_board(series=False).check().findings]
    assert "led_without_limit" in fires
    cleared = [f.code for f in _led_board(series=True).check().findings]
    assert "led_without_limit" not in cleared

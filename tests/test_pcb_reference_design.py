"""The ESP32-C3 reference design fixture — the acceptance vehicle.

``docs/backlog/pcb-guided-place-route.md`` specifies it (user decision,
2026-08-27): "acceptance vehicle = a synthetic ESP32-C3 reference design
(~30 components: MCU + I2C sensor + regulator + decoupling + headers),
authored as slice-3 scope, reused by every later slice's tests."

It was never authored. The 2026-08-28 acceptance run had to build it from
scratch, and the result lived only in ``/tmp`` and a dev-DB row — so the
measurement it produced (81.8% routed on fanout>=2 nets, 1063 DRC errors)
was not reproducible by anyone else. Committing it as a fixture is what
makes that run citable.

These checks are structural and DB-free. They deliberately do NOT assert
routing or DRC outcomes: those are measurements of the engine, and pinning
them here would turn a moving target into a green test that means nothing.
What they pin is that the fixture is a *valid, self-consistent netlist* —
so a future failure is attributable to the engine and not to fixture rot.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "pcb" / "esp32c3_reference.json"


@pytest.fixture(scope="module")
def design() -> dict[str, Any]:
    with FIXTURE.open(encoding="utf-8") as fh:
        loaded: dict[str, Any] = json.load(fh)
    return loaded


def test_outline_is_deliberately_oversized_for_diagnosis(
    design: dict[str, Any],
) -> None:
    """The outline is 300x300mm ON PURPOSE. Do not "fix" it back to 40x30.

    User decision 2026-08-28: make the board big enough that board size is
    not a confound while the router is being repaired, and shrink it later.
    It separates two very different explanations for the DRC errors —
    "the router has no track-to-track collision model" (which persists at
    any board size) from "the board is too small" (which does not).

    Note what it does NOT do. ``OptimizeEngine.board_side`` is a SYNTHETIC
    ``max(20, 6*sqrt(n))`` square (~32.3mm at n=29); the authored outline
    never reaches the IR, which has no outline field at all. So enlarging
    this changes what ``check_board_edge_clearance`` measures against, but
    NOT where the placer puts parts — they stay packed into ~32mm. Until
    outline->IR is wired, this fixture cannot actually give the placer room.
    """
    outline = next(f for f in design["features"] if f["ftype"] == "outline")
    xs = [pt[0] for pt in outline["geom"]["path"]]
    ys = [pt[1] for pt in outline["geom"]["path"]]
    assert max(xs) - min(xs) >= 200, "outline shrank; see docstring before changing"
    assert max(ys) - min(ys) >= 200, "outline shrank; see docstring before changing"


def test_fixture_matches_the_specified_shape(design: dict[str, Any]) -> None:
    """~30 components / ~20 nets, per the spec's own description.

    Bounds rather than equality: the design may legitimately grow. What
    must not happen silently is it shrinking to a toy, which is what every
    other 'ESP32-C3' payload in the test suite already is (1-2 components).
    """
    assert 25 <= len(design["components"]) <= 40
    assert 15 <= len(design["nets"]) <= 30
    assert len(design["connections"]) >= 60
    assert design["features"], "the outline feature is required"


def test_every_connection_resolves(design: dict[str, Any]) -> None:
    """No connection may name a refdes, pin, or net that does not exist.

    A dangling refdes would be silently accepted by the handler and would
    then look like an engine defect at route time.
    """
    pins_by_refdes = {
        c["refdes"]: {p["name"] for p in c["pins"]} for c in design["components"]
    }
    net_names = {n["name"] for n in design["nets"]}

    for conn in design["connections"]:
        refdes, pin, net = conn["refdes"], conn["pin"], conn["net"]
        assert refdes in pins_by_refdes, f"connection names unknown refdes {refdes!r}"
        assert pin in pins_by_refdes[refdes], f"{refdes} has no pin {pin!r}"
        assert net in net_names, f"connection names unknown net {net!r}"


def test_power_nets_carry_current_annotations(design: dict[str, Any]) -> None:
    """VBUS/VCC3V3/GND must declare ``current``.

    Without it the IPC-2221 width machinery has nothing to read and the
    acceptance run silently exercises none of it — the run would look like
    a width test while testing only defaults.
    """
    annotated = {n["name"] for n in design["nets"] if n.get("current") is not None}
    for rail in ("VBUS", "VCC3V3", "GND"):
        assert rail in annotated, f"{rail} carries no current= annotation"


def test_has_the_high_fanout_nets_that_actually_stress_the_router(
    design: dict[str, Any],
) -> None:
    """GND and VCC3V3 must stay high-fanout.

    These are the two nets the 2026-08-28 run FAILED to route (26 and 23
    pins). If a future edit trims them, the fixture would start passing for
    the wrong reason — the hard case having quietly left the design.
    """
    fanout: dict[str, int] = {}
    for conn in design["connections"]:
        fanout[conn["net"]] = fanout.get(conn["net"], 0) + 1

    assert fanout.get("GND", 0) >= 20, f"GND fanout collapsed to {fanout.get('GND')}"
    assert fanout.get("VCC3V3", 0) >= 15, (
        f"VCC3V3 fanout collapsed to {fanout.get('VCC3V3')}"
    )

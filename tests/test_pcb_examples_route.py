"""Every example must route all multi-member nets with no DRC errors.

Measured baseline: 2026-11-05, main 99de0fedb, effort=20000, seed=1.
usbc_attiny re-measured 2026-11-06 on td569573 (the cutcap share);
esp32c3/usbc_attiny re-measured 2026-11-07 on td569571 (the rule
ladder); every board but sanity/blinky_led (unchanged) re-measured
2026-11-14 on td637902 (pad sides read off the routed pose), the nano
rows on a loaded host (load ~40-70), each against the same-load
pre-fix route. Seconds cover local.route (including placement); boards
were built first. Boards were measured concurrently in separate local
processes.

board           routable  routed  failed  drc_errors  seconds  cause
sanity                20      20       0           0    4.644  complete
blinky_led             1       1       0           0    0.617  complete; 1 dangling
usbc_attiny            8       6       2          14   30.400  3V3/GND legalise spacing; no walled-off pins
esp32c3               11       9       2           3  238.900  walled-off GND; VCC3V3 legalise spacing
nano                  64      61       3          87 2396.000  GND/V5/VBUS (pre-fix 45/64, DRC 81)
nano_usb_pd           64      61       3          87 2449.000  GND/V5/VBUS (pre-fix 45/64, DRC 81)
nano_usb_pd_r2        72      41      31          60 5371.000  signal + power nets (pre-fix 44/72, DRC 64)

For usbc_attiny, routable counts authored nets with at least two pins.
Through main 99de0fedb its placement overflowed the GND corridor and the
route stage raised LocalError/CorridorOverflow before returning any
routing or DRC counts. td569573's cutcap share cleared that pre-route
refusal; the board now routes 5/8 and fails like esp32c3 on walled-off
power nets (3V3/GND/VBUS). td637902 found usbc_attiny's GND "walled off"
was a placer-flipped R1 whose R1.2 the router still seeded on its
authored F.Cu face, under J1.5's pad: the route now reads every SMD
pad's side off the routed pose, VBUS routes and GND/3V3 fail on
legalise spacing instead. The numbers document the baseline; the
assertion always targets complete routing at DRC 0. Strict xfails turn a
completed board into a loud XPASS.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.pcb import local
from tests._script import load_script

ROOT = Path(__file__).resolve().parents[1]
EFFORT = 20000
BOARD_NAMES = sorted(
    path.name
    for path in (ROOT / "examples/boards").iterdir()
    if path.is_dir() and (path / "main.py").is_file()
)
BOARD_MARKS = {
    "usbc_attiny": [
        # Local 23s; at esp32c3's >6.7x macos-latest slowdown (gr647743)
        # that lands ~160s vs the 300s default — thin runner margin.
        pytest.mark.timeout(900),
        pytest.mark.xfail(
            strict=True,
            raises=AssertionError,
            reason=(
                "6/8 routed, 2 failed, DRC 14: 3V3/GND legalise spacing inside "
                "the move caps (td637902 posed the flipped R1.2 that walled GND; "
                "VBUS routes). td569573's cutcap share cleared the pre-route "
                "CorridorOverflow (GND demand 2 > cap 1)"
            ),
        ),
    ],
    "esp32c3": [
        pytest.mark.timeout(900),  # CI worst >300s macos-latest (gr647743) x3
        pytest.mark.xfail(
            strict=True,
            raises=AssertionError,
            reason=(
                "9/11 routed, 2 failed, DRC 3: GND walled pins, VCC3V3 legalise "
                "spacing inside the move caps"
            ),
        ),
    ],
    "nano": [
        pytest.mark.slow,
        pytest.mark.nightly,
        pytest.mark.timeout(3600),
        pytest.mark.xfail(
            strict=True,
            raises=AssertionError,
            reason=(
                "61/64 routed, 3 failed (GND/V5/VBUS), DRC 87 since td637902 "
                "read pad sides off the routed pose"
            ),
        ),
    ],
    "nano_usb_pd": [
        pytest.mark.slow,
        pytest.mark.nightly,
        pytest.mark.timeout(3600),
        pytest.mark.xfail(
            strict=True,
            raises=AssertionError,
            reason=(
                "61/64 routed, 3 failed (GND/V5/VBUS), DRC 87 since td637902 "
                "read pad sides off the routed pose"
            ),
        ),
    ],
    "nano_usb_pd_r2": [
        pytest.mark.slow,
        pytest.mark.nightly,
        pytest.mark.timeout(3600),
        pytest.mark.xfail(
            strict=True,
            raises=AssertionError,
            reason=(
                "41/72 routed, 31 failed, DRC 60: walled-off pins; "
                "short/connectivity guards withdraw copper"
            ),
        ),
    ],
}


def test_expected_failure_board_names_exist() -> None:
    stale_names = set(BOARD_MARKS) - set(BOARD_NAMES)
    assert not stale_names, (
        f"Expected-failure board names no longer exist: {sorted(stale_names)}; "
        f"discovered boards: {BOARD_NAMES}"
    )


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(name, id=name, marks=BOARD_MARKS.get(name, []))
        for name in BOARD_NAMES
    ],
)
@pytest.mark.nightly(
    reason="195 s at run 42354225792 (me580555, gate-s2): tests >=30 s run on the nightly FULL shape (the gate's soak also deselects -m 'not nightly'), off the 5-minute gate"
)
def test_example_routes_fully_at_drc_zero(name: str, tmp_path: Path) -> None:
    module = load_script(
        f"pcb_example_{name}", ROOT / "examples/boards" / name / "main.py"
    )
    board = module.build()

    # No board is expected to refuse before routing any more (td569573's
    # cutcap share cleared usbc_attiny's pre-route CorridorOverflow), so a
    # LocalError now propagates as a real failure rather than an xfail.
    rt = local.route(board, place=True, seed=1, effort=EFFORT, out=str(tmp_path))

    # The API buckets contain net names. Single-member (dangling) nets are
    # exempt from routing and must not inflate the required routed count.
    routable = len(rt.routed) + len(rt.failed) + len(rt.unrouted)
    assert len(rt.routed) == routable and rt.drc_errors == 0, (
        f"{name}: {len(rt.routed)}/{routable} routable nets, {rt.drc_errors} DRC errors; "
        f"failed={rt.failed}, unrouted={rt.unrouted}"
    )

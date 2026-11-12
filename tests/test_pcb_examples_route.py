"""Every example must route all multi-member nets with no DRC errors.

Measured baseline: 2026-11-05, main 99de0fedb, effort=20000, seed=1.
usbc_attiny re-measured 2026-11-06 on td569573 (the cutcap share);
esp32c3/usbc_attiny re-measured 2026-11-07 on td569571 (the rule
ladder). Seconds cover local.route (including placement); boards were
built first. Boards were measured concurrently in separate local
processes.

board           routable  routed  failed  drc_errors  seconds  cause
sanity                20      20       0           0    4.644  complete
blinky_led             1       1       0           0    0.617  complete; 1 dangling
usbc_attiny            8       6       2          18   23.398  walled-off power pins; short/connectivity guards
esp32c3               11       8       3           8   44.890  walled-off pins; short/connectivity guards
nano                  64      45      19          18 1947.002  walled-off pins; short/connectivity guards
nano_usb_pd           64      45      19          18 1936.141  walled-off pins; short/connectivity guards
nano_usb_pd_r2        72      32      40          38 2192.916  walled-off pins; short/connectivity guards

For usbc_attiny, routable counts authored nets with at least two pins.
Through main 99de0fedb its placement overflowed the GND corridor and the
route stage raised LocalError/CorridorOverflow before returning any
routing or DRC counts. td569573's cutcap share cleared that pre-route
refusal; the board now routes 5/8 and fails like esp32c3 on walled-off
power nets (3V3/GND/VBUS). The numbers document the baseline; the
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
                "6/8 routed, 2 failed, DRC 18: GND/VBUS power nets walled off "
                "(td569571's ladder recovered 3V3). td569573's cutcap "
                "share cleared the pre-route CorridorOverflow (GND demand 2 > cap 1)"
            ),
        ),
    ],
    "esp32c3": [
        pytest.mark.timeout(900),  # CI worst >300s macos-latest (gr647743) x3
        pytest.mark.xfail(
            strict=True,
            raises=AssertionError,
            reason=(
                "8/11 routed, 3 failed, DRC 8: VCC3V3/GND/SDA — walled pins and "
                "copper withdrawn by short/connectivity guards"
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
                "45/64 routed, 19 failed, DRC 18: 9 nets have walled-off pins; "
                "other copper withdrawn by short/connectivity guards"
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
                "45/64 routed, 19 failed, DRC 18: 9 nets have walled-off pins; "
                "other copper withdrawn by short/connectivity guards"
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
                "32/72 routed, 40 failed, DRC 38: 15 nets have walled-off pins; "
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

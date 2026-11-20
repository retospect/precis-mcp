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

Seconds re-measured 2026-11-16 on td686226 (sampling profiler on, load
~40-65, all boards concurrent; sanity/blinky_led/usbc_attiny/esp32c3
ran first, then nano, nano_usb_pd and r2 together). Results are
unchanged; the speedup is CopperIndex.blocked_points pruning its
(points x discs) distance matrix to the discs in reach, which was 16%
of r2's CPU samples (6930/43985) and is now 2.8% (1260/44468). r2 still
does not finish inside 3500 s. Its profile (44468 samples at 50 ms)
puts build_wires at 88% (_grow_net 68%), CopperIndex.blocked_points at
32% (mostly STRtree.query, 4693 samples at the leaf) and
crossings.resolve_crossings at 2.8% (1262): the td678692 hang stack in
resolve_crossings was a snapshot, not the cost; the cost is the wire
growth's clearance queries.

board           routable  routed  failed  drc_errors  seconds  cause
sanity                20      20       0           0   12.200  complete
blinky_led             1       1       0           0    0.600  complete; 1 dangling
usbc_attiny            8       6       2          14   29.500  3V3/GND legalise spacing; no walled-off pins
esp32c3               11       9       2           3  102.100  GND/VCC3V3 legalise spacing (no shove withdrawal)
nano                  64      61       3          87 1495.000  GND legalise+via; V5 withdrawn by shove (crossing; blocked by GND); VBUS withdrawn by shove (no-via-site)
nano_usb_pd           64      61       3          87 1487.000  GND legalise+via; V5 withdrawn by shove (crossing; blocked by GND); VBUS withdrawn by shove (no-via-site)
nano_usb_pd_r2        72      41      31          60  >3500.0  signal + power nets (pre-fix 44/72, DRC 64); unfinished at 3500 s

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

The three nano boards route for 25-90 minutes each, longer than any
sharded CI job runs: they carry ``_LONG_BOARD`` and run only where
``PRECIS_PCB_LONG_BOARDS=1`` — check.yml's nightly ``long-boards`` job.
Locally: ``PRECIS_TEST_ENV="PRECIS_PCB_LONG_BOARDS=1" scripts/test
tests/test_pcb_examples_route.py``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

from precis.pcb import local
from tests._script import load_script

ROOT = Path(__file__).resolve().parents[1]
EFFORT = 20000
BOARD_NAMES = sorted(
    path.name
    for path in (ROOT / "examples/boards").iterdir()
    if path.is_dir() and (path / "main.py").is_file()
)
#: Opt-in for the boards marked ``_LONG_BOARD``; check.yml's nightly
#: ``long-boards`` job sets it, every other lane skips them.
LONG_BOARDS_ENV = "PRECIS_PCB_LONG_BOARDS"
# A board whose route outlasts a CI shard. They were nightly-marked into
# test-linux shard 7 (40-minute job cap) and r2 alone runs 5371 s, so the
# shard was cancelled on both Pythons for 11 nightlies running
# (2026-11-08..18, e.g. run 44322386479) and nothing behind them in the
# shard got a verdict; on macOS nano still held one of three workers when
# that shard's 90-minute cap hit.
_LONG_BOARD = pytest.mark.skipif(
    os.environ.get(LONG_BOARDS_ENV) != "1",
    reason=(
        "long board: runs in check.yml's nightly long-boards job "
        f"({LONG_BOARDS_ENV}=1 opts in)"
    ),
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
        _LONG_BOARD,
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
        _LONG_BOARD,
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
        _LONG_BOARD,
        pytest.mark.slow,
        pytest.mark.nightly,
        # Measured 5371 s on a dev host. 3x, not the nano rows' ~1.5x: it
        # runs only in check.yml's long-boards job, on a hosted runner no
        # leg has finished it on yet — tighten once that job's junit has a
        # measured time.
        pytest.mark.timeout(16200),
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
LONG_BOARDS = sorted(
    name for name, marks in BOARD_MARKS.items() if _LONG_BOARD in marks
)


def test_expected_failure_board_names_exist() -> None:
    stale_names = set(BOARD_MARKS) - set(BOARD_NAMES)
    assert not stale_names, (
        f"Expected-failure board names no longer exist: {sorted(stale_names)}; "
        f"discovered boards: {BOARD_NAMES}"
    )


def test_long_boards_job_runs_exactly_the_long_boards() -> None:
    """check.yml's nightly ``long-boards`` job is the only lane that runs
    the ``_LONG_BOARD`` params: it must name exactly them, set the opt-in,
    run on the nightly shape only, and outlast the longest board's own
    timeout mark — a test timeout above its job's cap is a cancel with no
    verdict, which is how shard 7 lost 11 nightlies."""
    jobs = yaml.safe_load(
        (ROOT / ".github/workflows/check.yml").read_text(encoding="utf-8")
    )["jobs"]
    job = jobs["long-boards"]
    step = next(s for s in job["steps"] if LONG_BOARDS_ENV in (s.get("env") or {}))
    assert step["env"][LONG_BOARDS_ENV] == "1"
    named = re.findall(
        r"test_pcb_examples_route\.py::test_example_routes_fully_at_drc_zero\[(\w+)\]",
        step["run"],
    )
    assert sorted(named) == LONG_BOARDS == sorted(set(LONG_BOARDS) & set(BOARD_NAMES))
    # One xdist worker per board: the cap below is the longest single
    # board, not their sum.
    assert f"-n {len(LONG_BOARDS)} " in step["run"]
    assert job["if"] == jobs["coverage-db"]["if"]
    longest = max(
        mark.args[0]
        for name in LONG_BOARDS
        for mark in BOARD_MARKS[name]
        if mark.name == "timeout"
    )
    assert job["timeout-minutes"] * 60 >= longest + 600, (
        f"long-boards caps at {job['timeout-minutes']} min but a board may run "
        f"{longest} s: raise the job's timeout-minutes with the timeout mark"
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

"""gr503694: the field polish never writes a continuous or unnormalised
rotation, and the 90° preference holds on the real bench board.

The esp32c3 dogfood (29 parts, seeds 1-5) wrote every part with a
continuous, unnormalised θ (C1 324.05°, U1 494.87°, C11 −88.56°) on
three seeds and parked 15-18 of 29 off the 90° grid on the other two.
This runs the real place job per seed and asserts what the store must
never again see:

- every stored rotation is a multiple of 15° in [0, 360) — the fixture
  authors no rotation, so every stored value is engine-written;
- at most 10 % of parts sit off the 90° grid (D5's "strong preference
  for multiples of 90" — a tilt survives only on a measured energy gain
  over ``field.ROT_TILT_MIN_GAIN``).

Place-only (no route job): rotations are what the place job persists.
~12s per seed, so ``slow`` and parametrized like the fixture's own
acceptance test.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.store import Store
from tests.test_pcb_reference_end_to_end import _drain_one_job

pytestmark = pytest.mark.slow

FIXTURE = Path(__file__).parent / "fixtures" / "pcb" / "esp32c3_reference.json"
SEEDS: tuple[int, ...] = (1, 2, 3, 4, 5)

#: D5's strong-90° bound: at most this fraction of a board's parts may
#: sit off the 90° grid (gr503694 fix direction, Reto's dogfood review).
MAX_OFF90_FRAC = 0.10


@pytest.mark.parametrize("seed", SEEDS)
def test_esp32c3_placed_rotations_are_grid_values(store: Store, seed: int) -> None:
    with FIXTURE.open(encoding="utf-8") as fh:
        design: dict[str, Any] = copy.deepcopy(json.load(fh))
    pcb = PcbHandler(hub=Hub(store=store))
    slug = f"esp32c3-rotgrid-s{seed}"
    assert "created" in pcb.put(id=slug, args=design).body
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    assert "enqueued" in pcb.put(id=slug, args={"op": "place", "seed": seed}).body
    _drain_one_job(store, ref.id)

    graph = store.pcb_graph(ref.id)
    rots = {str(i["refdes"]): float(i["rot"]) for i in graph["instances"]}
    assert rots, "no placed instances"
    bad_range = {r: v for r, v in rots.items() if not 0.0 <= v < 360.0}
    bad_grid = {r: v for r, v in rots.items() if min(v % 15.0, 15.0 - v % 15.0) > 1e-6}
    off90 = {r: v for r, v in rots.items() if 1e-6 < v % 90.0 < 90.0 - 1e-6}
    assert not bad_range, f"seed {seed}: rotations outside [0, 360): {bad_range}"
    assert not bad_grid, f"seed {seed}: rotations off the 15° grid: {bad_grid}"
    assert len(off90) <= math.floor(MAX_OFF90_FRAC * len(rots) + 1e-9), (
        f"seed {seed}: {len(off90)}/{len(rots)} off the 90° grid: {off90}"
    )

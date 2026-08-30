"""ESP32-C3 acceptance vehicle — end-to-end place+route ratchet.

``docs/backlog/pcb-guided-place-route.md``'s acceptance vehicle
(``tests/fixtures/pcb/esp32c3_reference.json``, structurally pinned
DB-free by ``tests/test_pcb_reference_design.py``) driven for real here:
author the design, ``put(op='place')`` -> drain the enqueued ``pcb_place``
job, ``put(op='route')`` -> drain the enqueued ``pcb_route`` job, then read
back route status + DRC.

2026-08-28 first-run measurement (commit ``4ff7add4`` plus the — at the
time of that run — still-uncommitted ``core.put`` ``args=`` fix that made
``put(kind='pcb', args={...})`` reachable over MCP at all, see
``git diff -- src/precis/tools/core.py``; the handler-level path this test
uses was already reachable): **81.8% routed** (9 of the 11 nets with
fanout >= 2 realized), **1063 DRC errors** — ``clearance`` 1038,
``board_edge_clearance`` 20, ``courtyard_overlap`` 5 — from 54 vias / 61
tracks of realized copper. GND and VCC3V3 (the two highest-fanout nets)
FAILED to route, despite neither being plane-promoted.

**2026-08-28 late: the acceptance criterion is now MET and asserted.**
Zero DRC errors, all 11 fanout>=2 nets realized. (Dated record: the silk
rules did not exist yet. Copper still holds at zero; silk carries its own
ratchet, ``BASELINE_SILK_ERRORS``.) Three changes got it there, in this
order of contribution:

1. Per-pin pad geometry (``ir.pin_dx``/``pin_dy``). Every pin used to
   resolve to its instance's centroid, so a 14-pin part emitted 14 tracks
   on 14 nets all starting at one coordinate — ~600 clearance errors at
   an exact 0.000mm gap that no router could ever fix. 1063 -> 612.
2. Hard placement constraints (``OptimizeEngine._placement_is_legal`` /
   ``bounds_for``), replacing two graded cost terms that the search was
   simply paying: parts may not overlap and may not hang off the board,
   with both keep-outs derived from each part's OWN land pattern rather
   than a single 1.0mm constant. 612 -> 234, and courtyard_overlap and
   board_edge_clearance both to zero.
3. The maze router (``precis.pcb.maze``, ``RealizeConfig.router='maze'``).
   Copper is claimed on a shared occupancy grid before it is drawn, so
   inter-net overlap is structurally impossible. 234 -> 0.

**Zero DRC is trivially achievable by routing nothing, so this test
asserts BOTH numbers and neither alone is the result.** An early revision
of the maze router scored a perfect zero while leaving 58 of 61
connections unrouted, and read as a triumph until the routed count was
put beside it.

The DRC ceiling is a hard 0 rather than a ratchet because it is now a
property of the algorithm, not a tuning outcome: a nonzero here means
something can put copper on the board without claiming it first, which is
a defect in the guarantee and not a regression in quality. Measured 0
across seeds 1-5; what varies between seeds is the routed count, which is
the honest place for variance to live.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.store import Store
from precis.workers.executors.job_inproc import run_job_inproc_pass

# Heavy compute (a full simulated-annealing place + route pass over a
# 29-component / 20-net / 81-connection design) — see
# docs/conventions/testing.md § "Judging effectiveness".
pytestmark = pytest.mark.slow

FIXTURE = Path(__file__).parent / "fixtures" / "pcb" / "esp32c3_reference.json"

#: The acceptance criterion, asserted (module docstring). All 11 nets with
#: fanout >= 2 realized, zero COPPER-class DRC errors (silk is counted
#: separately against its own ratchet — see ``BASELINE_SILK_ERRORS``).
#:
#: A DRC count is only meaningful WITH its config — the same fixture at the
#: same commit measured 630 under this test's seeded config and 1063 under
#: an unseeded CLI run, a factor of 1.7 apart — so both figures below are
#: measured under ``_SEED`` and the handler-default iters, and re-measuring
#: is required if either changes.
BASELINE_ROUTED_FANOUT2 = 11  # of 11 nets with fanout >= 2, realized
#: Not a ratchet. Inter-net clearance is enforced by the occupancy grid in
#: :mod:`precis.pcb.maze` — copper is claimed before it is drawn — so a
#: nonzero value here is not "worse placement", it is a hole in the
#: guarantee: some path reached the board without claiming its corridor.
#: Do not raise this to accommodate a measurement. Find the leak.
#:
#: **Copper-class rules only** — see ``BASELINE_SILK_ERRORS``. The zero
#: above earns its strictness from the occupancy-grid argument, which is a
#: statement about copper and says nothing about silkscreen; folding silk
#: into this count would trade a hard guarantee for a soft one.
BASELINE_DRC_ERRORS = 0

#: Silk IS a ratchet, and unlike the copper count above it is knowingly
#: nonzero. ``silk_missing`` fires when a part's courtyard outline could not
#: be drawn without landing on a pad, and every one of these is a real
#: defect the fab would print as ink on copper — they are recorded, not
#: forgiven.
#:
#: 27 -> 9 -> 0 on 2026-08-30. Four mechanisms, each a different defect
#: that happened to share this one rule (the full ledger is in
#: ``tests/test_pcb_fab_render_all_layers.py``'s ``KNOWN_OPEN_DRC_ERRORS``
#: docstring): a courtyard derived from the hull of the part's OWN pads,
#: outlines that break around an obstacle instead of dropping whole, a
#: refdes ring sweep replacing six fixed candidate spots, and a dot beside
#: pin 1 when a fan-out via has taken the courtyard corner its tick marks.
#:
#: **This number may only go DOWN, and it is now at the floor.** Raising it
#: to accept a measurement is the failure mode; so is lowering it by making
#: the checker quieter, which is why the routed-nets assertion below must
#: be read alongside it.
BASELINE_SILK_ERRORS = 0

#: A fixed seed for run-to-run reproducibility of THIS test's own numbers
#: (the optimizer is simulated annealing). Still asserting direction, not
#: equality, below — a seed does not survive an engine change, only a
#: literal re-run of the same code.
_SEED = 1

_DRC_HEAD_RE = re.compile(r"— (\d+) error\(s\), (\d+) warn\(s\)")


def _fanout(connections: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for conn in connections:
        counts[conn["net"]] = counts.get(conn["net"], 0) + 1
    return counts


def _drain_one_job(store: Store) -> None:
    result = run_job_inproc_pass(store, limit=1)
    assert result["claimed"] == 1, f"expected exactly one queued job, got {result}"
    assert result["failed"] == 0, f"job failed to drain cleanly: {result}"


def test_esp32c3_reference_place_and_route_never_regresses_the_baseline(
    store: Store,
) -> None:
    with FIXTURE.open(encoding="utf-8") as fh:
        design: dict[str, Any] = json.load(fh)

    pcb = PcbHandler(hub=Hub(store=store))
    created = pcb.put(id="esp32c3-ref", args=design)
    assert "created" in created.body

    # --- place ------------------------------------------------------
    # No `iters=` override: the default schedule (2000 place / 3000 route
    # — pcb_place.py / pcb_route.py's `_DEFAULT_ITERS`) already completes
    # this whole test (author + place + drain + route + drain + DRC) in
    # ~12s measured locally — well inside the `slow` budget — so there is
    # no runtime/fidelity tradeoff to make here: cutting iters further
    # would only make the measurement less representative of what
    # put(op='place'/'route') actually does for an agent, for no speed
    # win worth taking.
    place_resp = pcb.put(id="esp32c3-ref", args={"op": "place", "seed": _SEED})
    assert "enqueued" in place_resp.body
    _drain_one_job(store)

    # --- route ------------------------------------------------------
    route_resp = pcb.put(id="esp32c3-ref", args={"op": "route", "seed": _SEED})
    assert "enqueued" in route_resp.body
    _drain_one_job(store)

    # --- read back: routed count, honestly denominated ---------------
    # view='route-status' / store.pcb_route_status report per-net status
    # for ALL 20 nets, including 9 single-pin ("dangling") nets that carry
    # only one connection each (test points, NC nets, mounting holes —
    # see tests/workers/test_pcb_route.py's dangling-net-exemption case).
    # Those "route" trivially (nothing to connect) and inflate a naive
    # numerator/denominator to 18/20 = 90%. The honest population is nets
    # with fanout >= 2 (11 of the 20, per this fixture) — computed here
    # from the fixture's own connections so a future edit to the fixture
    # can't silently drift the two out of sync.
    ref = store.get_ref(kind="pcb", id="esp32c3-ref")
    assert ref is not None
    fanout = _fanout(design["connections"])
    fanout2_nets = {name for name, n in fanout.items() if n >= 2}
    assert len(fanout2_nets) == 11

    status_rows = store.pcb_route_status(ref.id)
    routed_count = sum(
        1
        for row in status_rows
        if row["name"] in fanout2_nets and row["status"] == "realized"
    )

    # --- read back: DRC error count -----------------------------------
    drc = pcb.get(id="esp32c3-ref", view="drc")
    match = _DRC_HEAD_RE.search(drc.body)
    assert match is not None, f"DRC view head didn't match expected shape: {drc.body!r}"
    drc_error_count = int(match.group(1))

    # --- both numbers, together -----------------------------------------
    # Neither assertion means anything alone: routing nothing satisfies the
    # DRC one, and shorting everything satisfies the routed one.
    import collections

    _run_id, findings = store.pcb_drc_findings_latest(ref.id)
    breakdown = collections.Counter(
        f"{f['rule']}" for f in findings if f["severity"] == "error"
    )
    # Name the findings, don't just count them. A bare rule tally sends the
    # reader back to reproduce the board before they can even start; the
    # detail line is what the fix is actually made from.
    detail = " | ".join(
        str(f["detail"])[:160] for f in findings if f["severity"] == "error"
    )[:900]
    # Split by class before asserting: the two counts are held to different
    # standards (hard zero vs. a downward-only ratchet), so summing them
    # would let a silk drop mask a copper short.
    silk_errors = sum(n for rule, n in breakdown.items() if rule.startswith("silk"))
    copper_errors = drc_error_count - silk_errors
    assert copper_errors <= BASELINE_DRC_ERRORS, (
        f"{copper_errors} copper-class DRC errors (expected "
        f"{BASELINE_DRC_ERRORS}): {dict(breakdown)} -- inter-net clearance is "
        "enforced by the occupancy grid, so a clearance finding here means "
        f"copper reached the board without claiming its corridor first\n{detail}"
    )
    assert silk_errors <= BASELINE_SILK_ERRORS, (
        f"{silk_errors} silk DRC errors, above the {BASELINE_SILK_ERRORS} "
        f"baseline: {dict(breakdown)} -- this ratchet only goes down; see "
        f"docs/backlog/pcb-courtyard-polygon.md\n{detail}"
    )
    assert routed_count >= BASELINE_ROUTED_FANOUT2, (
        f"routed {routed_count}/11 fanout>=2 nets, below the "
        f"{BASELINE_ROUTED_FANOUT2}/11 baseline -- a regression (note that a "
        "router which declines to route also reports zero DRC errors)"
    )

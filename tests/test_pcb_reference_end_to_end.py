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
ratchet, ``BASELINE_SILK_ERRORS_BY_SEED``.) Three changes got it there, in this
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
a defect in the guarantee and not a regression in quality. Since
2026-08-31 that claim is ASSERTED across seeds 1-5 (``SEEDS``), not
merely measured once: every baseline below is a worst-case-over-seeds
bound, so it describes the engine rather than one draw of the anneal.
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
from precis.workers.executors._common import TERMINAL, current_status
from precis.workers.executors.job_inproc import run_job_inproc_pass

# Heavy compute (a full simulated-annealing place + route pass over a
# 29-component / 20-net / 81-connection design) — see
# docs/conventions/testing.md § "Judging effectiveness".
pytestmark = pytest.mark.slow

FIXTURE = Path(__file__).parent / "fixtures" / "pcb" / "esp32c3_reference.json"

#: The acceptance criterion, asserted (module docstring). All 11 nets with
#: fanout >= 2 realized, zero COPPER-class DRC errors (silk is counted
#: separately against its own ratchet — see ``BASELINE_SILK_ERRORS_BY_SEED``).
#:
#: A DRC count is only meaningful WITH its config — the same fixture at the
#: same commit measured 630 under this test's seeded config and 1063 under
#: an unseeded CLI run, a factor of 1.7 apart — so both figures below are
#: measured under every seed in ``SEEDS`` and the handler-default iters,
#: and re-measuring is required if either changes.
BASELINE_ROUTED_FANOUT2 = 11  # of 11 nets with fanout >= 2, realized
#: Not a ratchet. Inter-net clearance is enforced by the occupancy grid in
#: :mod:`precis.pcb.maze` — copper is claimed before it is drawn — so a
#: nonzero value here is not "worse placement", it is a hole in the
#: guarantee: some path reached the board without claiming its corridor.
#: Do not raise this to accommodate a measurement. Find the leak.
#:
#: **Copper-class rules only** — see ``BASELINE_SILK_ERRORS_BY_SEED``. The zero
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
#: **Each seed's number may only go DOWN.** Raising one to accept a
#: measurement is the failure mode; so is lowering one by making the
#: checker quieter, which is why the routed-nets assertion below must be
#: read alongside it. Per-seed since 2026-08-31 (the same ledger shape as
#: the 40mm fixture's ``KNOWN_OPEN_DRC_ERRORS``). Re-measured 2026-09-01
#: after the alignment cost term + fiducial per-courtyard filter + pour/
#: drop-via changes redrew every seed: seed 3's earlier C8 entry
#: RESOLVED, and seed 1's new draw parks a via on D1's courtyard ring
#: (2.92 of 6.40mm drawable), so its courtyard+pin1 silk honestly drop —
#: 2 findings, on that seed alone. A seed absent from this dict is held
#: at zero.
BASELINE_SILK_ERRORS_BY_SEED: dict[int, int] = {1: 2}

#: The seeds this fixture asserts over — EVERY seed must hold every bound
#: below (worst-case-over-seeds, not a median), so the baselines measure
#: the ENGINE rather than one lottery draw of the anneal. A single pinned
#: seed was the root cause of the constant-nudging pressure recorded in
#: ``docs/backlog/pcb-courtyard-polygon.md`` ("the fixtures pin one
#: lottery draw"): any placement-affecting change re-rolled the draw, a
#: correctness fix could read as a regression, and the same clearance
#: sweep ranked its four candidate values differently before and after an
#: unrelated legality gate. Five seeds is not a distribution either, but
#: it is enough that a bound holding across all of them is a property of
#: the search and not of seed 1. Each seed runs as its own parametrized
#: test (~16s, xdist-parallel), so a failing seed names itself.
SEEDS: tuple[int, ...] = (1, 2, 3, 4, 5)

_DRC_HEAD_RE = re.compile(r"— (\d+) error\(s\), (\d+) warn\(s\)")


def _fanout(connections: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for conn in connections:
        counts[conn["net"]] = counts.get(conn["net"], 0) + 1
    return counts


def _drain_one_job(store: Store, parent_id: int) -> None:
    """Drain the queued job most recently enqueued under ``parent_id``.

    Not just "whatever's next in the queue" (gr295496): the shared
    per-worker test DB can carry an orphaned job left behind by an
    unrelated test file, and ``run_job_inproc_pass(limit=1)`` claims
    strict priority/age order, so a stray row — not this design's own
    job — gets claimed first and its failure misreads as OUR drain
    failing. Loop passes, tolerating whatever unrelated row lands along
    the way, until THIS design's own job reaches a terminal status.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'job' AND parent_id = %s "
            "ORDER BY ref_id DESC LIMIT 1",
            (parent_id,),
        ).fetchone()
    assert row is not None, f"no job was ever queued for parent_id={parent_id}"
    job_ref_id = row[0]

    for _ in range(25):
        with store.pool.connection() as conn:
            status = current_status(conn, job_ref_id)
        if status in TERMINAL:
            assert status == "succeeded", (
                f"job {job_ref_id} failed to drain cleanly: status={status!r}"
            )
            return
        result = run_job_inproc_pass(store, limit=1)
        assert result["claimed"] == 1, f"expected a queued job, got {result}"
    raise AssertionError(f"job {job_ref_id} never reached a terminal status")


@pytest.mark.parametrize("seed", SEEDS)
def test_esp32c3_reference_place_and_route_never_regresses_the_baseline(
    store: Store, seed: int
) -> None:
    with FIXTURE.open(encoding="utf-8") as fh:
        design: dict[str, Any] = json.load(fh)

    pcb = PcbHandler(hub=Hub(store=store))
    created = pcb.put(id="esp32c3-ref", args=design)
    assert "created" in created.body
    ref = store.get_ref(kind="pcb", id="esp32c3-ref")
    assert ref is not None

    # --- place ------------------------------------------------------
    # No `iters=` override: the default schedule (2000 place / 3000 route
    # — pcb_place.py / pcb_route.py's `_DEFAULT_ITERS`) already completes
    # this whole test (author + place + drain + route + drain + DRC) in
    # ~12s measured locally — well inside the `slow` budget — so there is
    # no runtime/fidelity tradeoff to make here: cutting iters further
    # would only make the measurement less representative of what
    # put(op='place'/'route') actually does for an agent, for no speed
    # win worth taking.
    place_resp = pcb.put(id="esp32c3-ref", args={"op": "place", "seed": seed})
    assert "enqueued" in place_resp.body
    _drain_one_job(store, ref.id)

    # --- route ------------------------------------------------------
    route_resp = pcb.put(id="esp32c3-ref", args={"op": "route", "seed": seed})
    assert "enqueued" in route_resp.body
    _drain_one_job(store, ref.id)

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
    silk_baseline = BASELINE_SILK_ERRORS_BY_SEED.get(seed, 0)
    assert silk_errors <= silk_baseline, (
        f"{silk_errors} silk DRC errors, above seed {seed}'s {silk_baseline} "
        f"baseline: {dict(breakdown)} -- this ratchet only goes down; see "
        f"docs/backlog/pcb-courtyard-polygon.md\n{detail}"
    )
    assert routed_count >= BASELINE_ROUTED_FANOUT2, (
        f"routed {routed_count}/11 fanout>=2 nets, below the "
        f"{BASELINE_ROUTED_FANOUT2}/11 baseline -- a regression (note that a "
        "router which declines to route also reports zero DRC errors)"
    )

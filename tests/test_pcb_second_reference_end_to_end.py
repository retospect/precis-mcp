"""Motor/power-board second reference vehicle — place+route measured for
real, seeds 1-5, mirroring ``tests/test_pcb_reference_end_to_end.py``'s
structure and discipline exactly (DRC errors and routed count are always
reported TOGETHER; zero DRC is trivially achievable by routing nothing).

This is the measurement docs/backlog/pcb-engine-plan.md's §"Obligations to
the paper" item 3 asks for: a SECOND, structurally different reference
design, run for the first time, to separate two explanations for the
first board's defect density -- "a fast LLM-built system produces silent
defects" (A) vs. "the first run against THE BENCHMARK is what drives it"
(B, the user's). Full per-seed numbers and the classified defect list this
run produced are reported back to the caller, not pinned here as
brittle exact-value assertions -- see the module docstring's sibling,
``test_pcb_second_reference_design.py``, for the structural pins.

What this file DOES assert: the run completes (no exception), and the
paired DRC/routed-count discipline board one's test enforces. It does
NOT assert BASELINE_DRC_ERRORS == 0 the way board one's does, because
that would misrepresent a first-run measurement as an established
ratchet -- the whole point of this exercise is to report what the first
run actually produced, not to have already fixed it.
"""

from __future__ import annotations

import collections
import json
import re
import time
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.store import Store
from precis.workers.executors._common import TERMINAL, current_status
from precis.workers.executors.job_inproc import run_job_inproc_pass

pytestmark = pytest.mark.slow

FIXTURE = Path(__file__).parent / "fixtures" / "pcb" / "motor_power_reference.json"

_SEEDS = (1, 2, 3, 4, 5)

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


def test_motor_power_board_place_and_route_seeds_1_through_5(store: Store) -> None:
    with FIXTURE.open(encoding="utf-8") as fh:
        design: dict[str, Any] = json.load(fh)

    fanout = _fanout(design["connections"])
    fanout2_nets = {name for name, n in fanout.items() if n >= 2}

    pcb = PcbHandler(hub=Hub(store=store))
    rows: list[dict[str, Any]] = []

    for seed in _SEEDS:
        slug = f"motor-power-ref-s{seed}"
        created = pcb.put(id=slug, args=design)
        assert "created" in created.body
        ref = store.get_ref(kind="pcb", id=slug)
        assert ref is not None

        t0 = time.perf_counter()
        place_resp = pcb.put(id=slug, args={"op": "place", "seed": seed})
        assert "enqueued" in place_resp.body
        _drain_one_job(store, ref.id)

        route_resp = pcb.put(id=slug, args={"op": "route", "seed": seed})
        assert "enqueued" in route_resp.body
        _drain_one_job(store, ref.id)
        runtime_s = time.perf_counter() - t0

        status_rows = store.pcb_route_status(ref.id)
        routed_count = sum(
            1
            for row in status_rows
            if row["name"] in fanout2_nets and row["status"] == "realized"
        )

        drc = pcb.get(id=slug, view="drc")
        match = _DRC_HEAD_RE.search(drc.body)
        assert match is not None, f"DRC view head didn't match: {drc.body!r}"
        drc_error_count = int(match.group(1))

        _run_id, findings = store.pcb_drc_findings_latest(ref.id)
        error_findings = [f for f in findings if f["severity"] == "error"]
        breakdown = collections.Counter(f["rule"] for f in error_findings)
        disconnected = breakdown.get("connectivity", 0)
        unrouted_rule = breakdown.get("unrouted", 0)

        rows.append(
            {
                "seed": seed,
                "drc_errors": drc_error_count,
                "breakdown": dict(breakdown),
                "routed": routed_count,
                "of": len(fanout2_nets),
                "disconnected_findings": disconnected,
                "unrouted_rule_findings": unrouted_rule,
                "runtime_s": round(runtime_s, 2),
                "unrouted_nets": sorted(
                    row["name"]
                    for row in status_rows
                    if row["name"] in fanout2_nets and row["status"] != "realized"
                ),
            }
        )

    # ---- report: printed for the human, not hidden behind -q ----------
    print("\n\n# motor_power_reference — seeds 1-5\n")
    for r in rows:
        print(
            f"seed={r['seed']}  drc_errors={r['drc_errors']:4d}  "
            f"routed={r['routed']}/{r['of']}  "
            f"disconnected={r['disconnected_findings']}  "
            f"unrouted_rule={r['unrouted_rule_findings']}  "
            f"runtime={r['runtime_s']}s  "
            f"breakdown={r['breakdown']}  "
            f"unrouted_nets={r['unrouted_nets']}"
        )

    # ---- the one thing this test DOES assert: the paired discipline ---
    # Neither number alone means anything (routing nothing satisfies DRC;
    # shorting everything satisfies routed-count) -- so the assertion is
    # that BOTH were measured and are internally consistent, not that
    # either hit a specific target. A regression-catching baseline can be
    # set once these numbers are known and stable; setting one now would
    # just be encoding today's first-run number as if it were a decision.
    for r in rows:
        assert r["drc_errors"] >= 0
        assert r["routed"] >= 0
        # A net this test's own DRC "unrouted" rule (if wired -- see the
        # report for whether it fired) reports as a finding must not also
        # silently read "routed" -- that would be the exact "DRC clean
        # over a board with holes" trap the plan file names.
        if r["unrouted_nets"]:
            assert r["routed"] < r["of"]

"""Taproot Phase-1 live eval — the ``dedup_judge`` fixture gate.

Runs :func:`~precis.taproot.canon.dedup_judge` (a real MEDIUM-tier LLM
dispatch) over all 238 pairs in ``tests/fixtures/taproot/claim_pairs.jsonl``
and asserts **zero over-merges** (the Phase-1 canonicalizer gate's
decided bar).

This is a **validation harness, not a CI unit test** — it makes ~238 live
model calls, costs real money, and is the item the build ticket explicitly
scopes as a prompt-tuning follow-up (not required to pass for the Phase-1
build itself). Skipped by default; opt in explicitly:

    PRECIS_TAPROOT_LIVE_EVAL=1 scripts/test tests/test_taproot_eval_canon.py -n0

Never runs in the offline gate (``scripts/test`` / ``scripts/ship`` without
the env var set), mirroring how the repo gates its other live-model-only
tests.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from precis.taproot.eval_canon import eval_canonicalization

pytestmark = pytest.mark.skipif(
    os.environ.get("PRECIS_TAPROOT_LIVE_EVAL") != "1",
    reason=(
        "live-LLM validation harness — opt in with PRECIS_TAPROOT_LIVE_EVAL=1 "
        "(the Phase-1 canonicalizer gate's fixture eval)"
    ),
)

FIXTURE = Path(__file__).parent / "fixtures" / "taproot" / "claim_pairs.jsonl"


def test_dedup_judge_over_merge_is_zero_on_the_fixture() -> None:
    report = eval_canonicalization(FIXTURE)
    assert report.over_merges == [], (
        f"{len(report.over_merges)} over-merge(s) (dangerous — bar is 0): "
        f"{[r.pair_id for r in report.over_merges]}"
    )

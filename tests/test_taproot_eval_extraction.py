"""Taproot Phase-1 live eval — the ``extract_claim`` AIDA-Atomic gate.

Runs :func:`~precis.taproot.canon.extract_claim` (a real SMALL-tier LLM
dispatch) over every row in the packaged
``precis/data/taproot/extraction_passages.jsonl`` fixture and asserts the
two hard gates: **zero compound-without-atoms** and **zero residual-
conjunction atoms** (see ``eval_canon.ExtractionReport`` for the rationale
— atom-count agreement is a soft metric, not gated here).

This is a **validation harness, not a CI unit test** — it makes live model
calls and costs real money, mirroring
``tests/test_taproot_eval_canon.py``. Skipped by default; opt in
explicitly:

    PRECIS_TAPROOT_LIVE_EVAL=1 scripts/test tests/test_taproot_eval_extraction.py -n0

Never runs in the offline gate (``scripts/test`` / ``scripts/ship`` without
the env var set).
"""

from __future__ import annotations

import os

import pytest

from precis.taproot.eval_canon import EXTRACTION_PASSAGES_FIXTURE, eval_extraction

pytestmark = pytest.mark.skipif(
    os.environ.get("PRECIS_TAPROOT_LIVE_EVAL") != "1",
    reason=(
        "live-LLM validation harness — opt in with PRECIS_TAPROOT_LIVE_EVAL=1 "
        "(the Phase-1 extraction AIDA-Atomic gate's fixture eval)"
    ),
)

FIXTURE = EXTRACTION_PASSAGES_FIXTURE


def test_extract_claim_hard_gates_are_zero_on_the_fixture() -> None:
    report = eval_extraction(FIXTURE)
    assert report.compound_without_atoms_violations == [], (
        f"{len(report.compound_without_atoms_violations)} "
        "compound-without-atoms violation(s) (bar is 0): "
        f"{[r.passage_id for r in report.compound_without_atoms_violations]}"
    )
    assert report.conjunction_violations == [], (
        f"{len(report.conjunction_violations)} residual-conjunction "
        f"violation(s) (bar is 0): "
        f"{[r.passage_id for r in report.conjunction_violations]}"
    )

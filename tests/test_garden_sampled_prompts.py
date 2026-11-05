"""Legacy sampled-review prompts retain their origin/main bytes."""

from __future__ import annotations

import pytest

from precis.graph.garden.proposals import GardenProposal
from precis.graph.garden.reviewer import _prompt_for


@pytest.mark.parametrize("action", ["merge", "relink", "prune", "split", "tag"])
def test_non_memory_sampled_prompt_retains_legacy_wording(action: str) -> None:
    proposal = GardenProposal(
        review_id=1,
        target_kind="ref",
        target_id=2,
        action=action,
        key="k",
        detector="ordinary",
        title="t",
        payload={},
        evidence={},
        rationale="r",
        model=None,
        at=None,
        current=True,
    )
    # Copied from origin/main's prompt, including whitespace and JSON order.
    expected = """You are the sampled reviewer for a knowledge graph's gardener
proposals (refs = typed nodes, links = typed edges, tags = closed
namespaces).

You are given ONE pending maintenance proposal — a cheaper proposer
already accepted it and a human would carry it out. Give your verdict on
the proposal itself: does the evidence support the action? You are a
bar, not a cheerleader — false accepts hide a noisy detector.

Respond with a single JSON object:
{"verdict": "approve"|"reject",
 "why": "one sentence naming the evidence that decided you"}

Rules: never invent a why that cites nothing in the proposal's evidence;
when unsure, reject.

Proposal:
""" + (
        f'{{"action": "{action}", "detector": "ordinary", "evidence": {{}}, '
        '"payload": {}, "proposer_rationale": "r", "target": "ref:2", "title": "t"}\n'
    )
    assert _prompt_for(proposal) == expected

"""AC #9 (docs/proposals/finding-acquisition-mode.md): the planner's
cached CONTRACT layer teaches the acquisition-mode mint (``wants=`` +
``provenance=``) for its literature-hunt guidance, not the dead
``cited_in``-less shape the pre-fix template taught (gr183824, gr183865
-- every lit-hunt tick died on ``BadInput``).

Mechanism decided in the proposal's readiness review: plain pytest
string assertions on the template constant, no new lint tooling. A
plain constant import -- no DB, no fixtures.
"""

from __future__ import annotations

from precis.workers.planner_prompt import _PLANNER_CONTRACT


def test_lit_hunt_teaches_wants_and_provenance() -> None:
    assert "wants=" in _PLANNER_CONTRACT
    assert "provenance=" in _PLANNER_CONTRACT


def test_lit_hunt_drops_dead_verifier_confidence_kwarg() -> None:
    assert "verifier_confidence=" not in _PLANNER_CONTRACT


def test_lit_hunt_drops_cited_in_less_bare_mint_shape() -> None:
    assert "put(kind='finding', text=" not in _PLANNER_CONTRACT


def test_lit_hunt_does_not_misattribute_oa_resolution_to_finding_chase() -> None:
    """The OA fetch cascade is ``fetch_oa``, not ``finding_chase`` -- the
    chase worker walks the citation graph / grounds an already-fetched
    stub, it does not itself resolve Unpaywall/arXiv/S2/EPO. The old
    text (removed by this fix) claimed the opposite worker attribution."""
    assert "finding_chase" not in _PLANNER_CONTRACT

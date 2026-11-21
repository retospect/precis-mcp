"""The memory-body judge is told which checklist row applies (gr749479)."""

from __future__ import annotations

from dataclasses import replace

from precis.graph.garden.candidates import GardenCandidate
from precis.graph.garden.proposer import _prompt_for


def _compact(sections: list[str]) -> GardenCandidate:
    return GardenCandidate(
        detector="compact",
        action="compact",
        target_kind="ref",
        target_id=474395,
        evidence={"section_tags": sections},
        payload={},
    )


def test_compact_prompt_pins_the_tagged_section_row() -> None:
    prompt = _prompt_for(_compact(["section:workflow"]))
    assert '"section_tags": ["section:workflow"]' in prompt
    assert "section:workflow tag selects the workflow, rulings row" in prompt
    assert "If an ask is based on a missing checklist element" in prompt


def test_compact_prompt_without_section_uses_common_checks() -> None:
    assert "apply only the checks common to all types" in _prompt_for(_compact([]))


def test_notation_prompt_keeps_its_detector_specific_ask() -> None:
    candidate = replace(_compact([]), detector="notation", action="rewrite")
    assert "If an ask is based on a missing checklist element" not in _prompt_for(
        candidate
    )

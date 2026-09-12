"""The planner contract's temperature/unit notation teaching must agree with
the draft write-path lint (``handlers/_draft_lint.py::temperature_form_hint``)
— the same writing agent sees both, so a disagreement means it's taught one
style and then immediately told to rewrite it (units-cutover prompt-surface
audit, item 11)."""

from __future__ import annotations

from precis.handlers._draft_lint import temperature_form_hint
from precis.workers.planner_prompt import _PLANNER_CONTRACT


def test_planner_contract_uses_si_spaced_temperature_form():
    assert "63 °C" in _PLANNER_CONTRACT
    assert "63–65 °C" in _PLANNER_CONTRACT
    assert "±1 °C" in _PLANNER_CONTRACT
    # the unspaced form the contract used to teach must be gone
    assert "63°C" not in _PLANNER_CONTRACT


def test_planner_contracts_own_example_passes_the_draft_lint():
    """The exact strings the contract teaches must not trip the lint that
    fires on every draft write — a live proof the two surfaces agree."""
    assert temperature_form_hint("Anneal at 63 °C for an hour.") == ""
    assert temperature_form_hint("Held over 63–65 °C, ±1 °C.") == ""

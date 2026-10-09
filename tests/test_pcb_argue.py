"""precis.pcb.argue — the handle grammar and its resolution, DB-free.

docs/backlog/pcb-argue-with-design.md: one notation, no synonyms; a token
is a handle iff it resolves; dangling anchors are a read-time report.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.pcb import argue

VALID: dict[str, Any] = {
    "parts": ["U1", "R_BLEED", "ARR1"],
    "pins": {"U1": ["1", "2", "3"], "R_BLEED": ["1", "2"], "ARR1": ["R3C4"]},
    "nets": ["GND", "HV_RAIL", "SIG/2"],
    "features": ["outline"],
}


@pytest.mark.parametrize(
    ("handle", "kind"),
    [
        ("U1", "part"),
        ("U1.3", "pin"),
        ("ARR1.R3C4", "pin"),  # a generated array cell is a plain refdes.pin
        ("net:HV_RAIL", "net"),
        ("net:SIG/2", "net"),
        ("feature:outline", "feature"),
        ("U1.9", None),
        ("U9", None),
        ("net:NOPE", None),
        ("feature:plaza", None),  # plazas are not pcb_features rows
        ("the", None),
    ],
)
def test_classify_each_grammar_form(handle: str, kind: str | None) -> None:
    assert argue.classify(handle, VALID) == kind


def test_candidates_tokenize_prose_and_strip_sentence_punctuation() -> None:
    text = "R_BLEED is on the wrong side of U1.3, and net:HV_RAIL (not net:SIG/2) sags."
    toks = argue.candidates(text)
    assert "R_BLEED" in toks
    assert "U1.3" in toks  # the comma is not part of the handle
    assert "net:HV_RAIL" in toks
    assert "net:SIG/2" in toks  # a net name may carry a slash
    assert "is" in toks  # prose IS a candidate — resolution rejects it
    assert toks.count("U1.3") == 1


def test_handles_in_keeps_only_tokens_that_resolve() -> None:
    class Store:
        def pcb_handles(self, ref_id: int) -> dict[str, Any]:
            return VALID

    text = "R_BLEED is on the wrong side of U1.3; the DRC says net:HV_RAIL sags"
    assert argue.handles_in(text, Store(), 1) == ["R_BLEED", "U1.3", "net:HV_RAIL"]


def test_all_handles_roster_uses_the_same_notation() -> None:
    roster = argue.all_handles(VALID)
    assert "U1" in roster["parts"]
    assert "ARR1.R3C4" in roster["pins"]
    assert roster["nets"] == ["net:GND", "net:HV_RAIL", "net:SIG/2"]
    assert roster["features"] == ["feature:outline"]


def test_dangling_is_a_report_never_a_raise() -> None:
    assert argue.dangling(["U1", "U7", "net:HV_RAIL", "net:GONE"], VALID) == [
        "U7",
        "net:GONE",
    ]


def test_note_name_prefix_and_dedupe() -> None:
    assert (
        argue.note_name("question", "R_BLEED is wrong", set()) == "q-r-bleed-is-wrong"
    )
    assert argue.note_name("answer", "Move it.", {"a-move-it"}) == "a-move-it-2"
    assert argue.note_name("question", "…", set()) == "q-note"


def test_prompt_quotes_every_handle_with_its_context_and_the_vitals() -> None:
    resolved = [
        argue.Resolved("U1.3", "pin", {"refdes": "U1", "pin": {"net": "GND"}}),
        argue.Resolved("net:HV_RAIL", "net", {"members": [{"refdes": "R_BLEED"}]}),
    ]
    p = argue.prompt(
        title="ewod",
        text="U1.3 should be on net:HV_RAIL",
        resolved=resolved,
        vitals={"n_parts": 3},
    )
    assert "- U1.3 (pin):" in p and "'GND'" in p
    assert "- net:HV_RAIL (net):" in p and "R_BLEED" in p
    assert "- n_parts: 3" in p
    assert "U1.3 should be on net:HV_RAIL" in p
    assert "never guess a yes" in p


def test_prompt_names_a_whole_design_argument_as_such() -> None:
    p = argue.prompt(title="t", text="too dense overall", resolved=[], vitals={})
    assert "whole-design argument" in p


def test_ask_goes_through_the_router_and_raises_on_error(monkeypatch) -> None:
    import precis.utils.llm.router as router

    seen: dict[str, Any] = {}

    def fake_route(req):
        seen["req"] = req
        return router.LlmResult(
            text="  Move R_BLEED.  ",
            cost_usd=0.01,
            turns_used=1,
            model="m",
            tier=req.tier,
        )

    monkeypatch.setattr(router, "route", fake_route)
    out = argue.ask(title="t", text="x", resolved=[], vitals={}, ref_id=7)
    assert out == "Move R_BLEED."
    req = seen["req"]
    assert req.source == argue.LLM_SOURCE
    assert req.tier == router.Tier.MEDIUM
    assert req.ref_id == 7
    assert req.max_usd == argue.LLM_MAX_USD

    def failing(req):
        return router.LlmResult(
            text="",
            cost_usd=None,
            turns_used=None,
            model="m",
            tier=req.tier,
            error="quota",
        )

    monkeypatch.setattr(router, "route", failing)
    with pytest.raises(RuntimeError, match="quota"):
        argue.ask(title="t", text="x", resolved=[], vitals={})

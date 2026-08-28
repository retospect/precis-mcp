"""Pin the claude_p JSON extraction + the tick's payload preference.

Regression suite for the 2026-08-27 silent-no-op tick: the old regex-based
``_parse_last_json_block`` matched braces nested ≤2 deep only, so a quest
tick payload (``proposals[].structure.ops[].site.anchors`` — 5 deep) parsed
as its last shallow *fragment*; that fragment then shadowed the correct
text-fallback in ``tick._payload_from_result`` and the tick applied nothing.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from precis.quest import tick as tick_mod
from precis.utils.claude_p import _parse_last_json_block

# The exact failure shape: a fenced payload whose proposals nest 5 deep.
_NESTED_PAYLOAD: dict = {
    "logbook": [{"entry_type": "observation", "text": "one step of thinking"}],
    "ledger_ops": [{"op": "add", "text": "a direction", "status": "active"}],
    "dossier_text": "# Striving\n\nprose",
    "proposals": [
        {
            "name": "Pd111-vacancy-surf-1-H-subsurf-1",
            "structure": {
                "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
                "ops": [
                    {"op": "slab", "element": "Pd", "size": [3, 3, 4]},
                    {
                        "op": "add_atom_site",
                        "element": "H",
                        "site": {
                            "type": "hollow",
                            "anchors": ["aPd22", "aPd23", "aPd25"],
                        },
                    },
                ],
            },
        }
    ],
}


class TestParseLastJsonBlock:
    def test_deeply_nested_payload_parses_whole(self) -> None:
        text = "```json\n" + json.dumps(_NESTED_PAYLOAD, indent=2) + "\n```"
        parsed = _parse_last_json_block(text)
        assert parsed == _NESTED_PAYLOAD  # the WHOLE object, not a fragment

    def test_prose_prefix_tolerated(self) -> None:
        text = "Here is my analysis of the tick.\n" + json.dumps(_NESTED_PAYLOAD)
        assert _parse_last_json_block(text) == _NESTED_PAYLOAD

    def test_braces_inside_strings_do_not_desync(self) -> None:
        payload = {"dossier_text": "uses {curly} and } stray braces {", "logbook": []}
        text = "note first\n" + json.dumps(payload)
        assert _parse_last_json_block(text) == payload

    def test_last_object_wins(self) -> None:
        first = {"a": 1}
        second = {"b": {"c": {"d": {"e": 2}}}}
        text = json.dumps(first) + "\nthen\n" + json.dumps(second)
        assert _parse_last_json_block(text) == second

    def test_nested_objects_are_not_candidates(self) -> None:
        # Objects INSIDE a parsed block must not be re-offered as candidates:
        # the outer object is the last top-level one.
        text = json.dumps(_NESTED_PAYLOAD)
        parsed = _parse_last_json_block(text)
        assert parsed is not None
        assert "op" not in parsed  # not the add_atom_site fragment

    def test_stray_open_brace_before_json_is_skipped(self) -> None:
        # A lone unparseable "{" in prose ahead of the real object must be
        # stepped over, not abort the scan.
        text = "set {broken\n" + json.dumps({"logbook": []})
        assert _parse_last_json_block(text) == {"logbook": []}

    def test_no_json_returns_none(self) -> None:
        assert _parse_last_json_block("no json here at all") is None
        assert _parse_last_json_block("") is None

    def test_non_dict_json_returns_none(self) -> None:
        assert _parse_last_json_block("[1, 2, 3]") is None


class TestPayloadFromResult:
    def test_fragment_data_is_rejected_text_wins(self) -> None:
        # A transport mis-parse hands back an inner fragment as .data; the
        # guard must fall through to the full payload in .text.
        fragment = {"op": "add_atom_site", "element": "H", "site": {}}
        res = SimpleNamespace(data=fragment, text=json.dumps(_NESTED_PAYLOAD))
        assert tick_mod._payload_from_result(res) == _NESTED_PAYLOAD

    def test_real_payload_data_is_preferred(self) -> None:
        res = SimpleNamespace(data=_NESTED_PAYLOAD, text="ignored")
        assert tick_mod._payload_from_result(res) is _NESTED_PAYLOAD

    def test_single_key_payload_data_accepted(self) -> None:
        data = {"dossier_text": "# Striving"}
        res = SimpleNamespace(data=data, text="")
        assert tick_mod._payload_from_result(res) is data

    def test_no_data_no_parseable_text_is_none(self) -> None:
        res = SimpleNamespace(data=None, text="prose only")
        assert tick_mod._payload_from_result(res) is None


class TestTickLlmMaxUsd:
    def test_default(self, monkeypatch) -> None:
        monkeypatch.delenv(tick_mod._TICK_LLM_MAX_USD_ENV, raising=False)
        assert tick_mod._tick_llm_max_usd() == tick_mod._TICK_LLM_MAX_USD

    def test_senior_tiers_get_headroom(self, monkeypatch) -> None:
        # An escalated FRONTIER (opus-class) review died at the flat $0.50
        # (error_max_budget_usd, 2026-08-27) — senior pricing needs a senior
        # cap. Tier accepted as enum or string (StrEnum str()s either way).
        from precis.utils.llm.router import Tier

        monkeypatch.delenv(tick_mod._TICK_LLM_MAX_USD_ENV, raising=False)
        assert tick_mod._tick_llm_max_usd(Tier.FRONTIER) == 2.50
        assert tick_mod._tick_llm_max_usd("big") == 1.50
        assert tick_mod._tick_llm_max_usd(Tier.MEDIUM) == tick_mod._TICK_LLM_MAX_USD

    def test_env_override_applies_to_every_tier(self, monkeypatch) -> None:
        monkeypatch.setenv(tick_mod._TICK_LLM_MAX_USD_ENV, "1.25")
        assert tick_mod._tick_llm_max_usd() == 1.25
        assert tick_mod._tick_llm_max_usd("frontier") == 1.25

    def test_bad_env_falls_back(self, monkeypatch) -> None:
        monkeypatch.setenv(tick_mod._TICK_LLM_MAX_USD_ENV, "not-a-number")
        assert tick_mod._tick_llm_max_usd() == tick_mod._TICK_LLM_MAX_USD

"""Tests for the task→requirement judge — the agent surface (llm-catalog slice 5,
docs/proposals/llm-catalog.md).

Covers: `infer_requirement` (JSON parse + clamp/validate so a malformed judge
reply can't produce an illegal requirement), tolerant JSON extraction, and the
`choose_model` loop (judge → requirement → deterministic policy). The judge is
injected so no real LLM call is made. Real PG for the policy half.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def clean_catalog(store: Any) -> Any:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET deleted_at = now() "
            "WHERE kind = 'llm' AND deleted_at IS NULL"
        )
    return store


class TestInferRequirement:
    def test_maps_judge_json_to_requirement(self) -> None:
        from precis.utils.llm.requirement import infer_requirement
        from precis.utils.llm.router import Tier

        def judge(_t: str) -> dict[str, Any]:
            return {
                "axis": "code",
                "min_ordinal": 4,
                "needs_tools": True,
                "needs_structured": False,
                "max_input": 120000,
            }

        req = infer_requirement(
            "refactor auth", tier_floor=Tier.CLOUD_SUPER, judge=judge
        )
        assert req.axis == "code" and req.min_ordinal == 4
        assert req.needs_tools is True and req.max_input == 120000

    def test_clamps_and_sanitizes(self) -> None:
        from precis.utils.llm.requirement import infer_requirement
        from precis.utils.llm.router import Tier

        # unknown axis → None; out-of-range ordinal clamps; bad max_input → None
        def judge(_t: str) -> dict[str, Any]:
            return {"axis": "vibes", "min_ordinal": 99, "max_input": "lots"}

        req = infer_requirement("do a thing", tier_floor=Tier.CLOUD_MID, judge=judge)
        assert req.axis is None and req.min_ordinal == 5 and req.max_input is None

    def test_empty_judge_reply_is_a_bare_requirement(self) -> None:
        from precis.utils.llm.requirement import infer_requirement
        from precis.utils.llm.router import Tier

        req = infer_requirement("x", tier_floor=Tier.CLOUD_MID, judge=lambda _t: {})
        assert req.axis is None and req.min_ordinal == 1


class TestExtractJson:
    def test_pulls_json_from_prose(self) -> None:
        from precis.utils.llm.requirement import _extract_json

        raw = (
            'Sure! Here is the requirement:\n{"axis": "code", "min_ordinal": 3}\nDone.'
        )
        assert _extract_json(raw) == {"axis": "code", "min_ordinal": 3}

    def test_garbage_is_empty(self) -> None:
        from precis.utils.llm.requirement import _extract_json

        assert _extract_json("no json here") == {}


class TestChooseModel:
    def test_judge_then_policy_picks(self, clean_catalog: Any) -> None:
        from precis import llm_catalog
        from precis.utils.llm.requirement import choose_model
        from precis.utils.llm.router import Tier

        llm_catalog.upsert_card(
            clean_catalog,
            model_id="coder",
            text="A strong coder.",
            tier_floor="cloud-mid",
            offerings=[{"transport": "claude_agent", "price_in": 3.0}],
            capability={"code": 5},
        )

        def judge(_t: str) -> dict[str, Any]:
            return {"axis": "code", "min_ordinal": 4}

        req, sel = choose_model(
            clean_catalog, "write a parser", tier_floor=Tier.CLOUD_MID, judge=judge
        )
        assert req.axis == "code"
        assert sel.model == "coder" and sel.from_catalog is True

    def test_degrades_to_floor_when_no_card(self, clean_catalog: Any) -> None:
        from precis.utils.llm.requirement import choose_model
        from precis.utils.llm.router import Tier, resolve_model

        req, sel = choose_model(
            clean_catalog,
            "summarize this",
            tier_floor=Tier.CLOUD_SMALL,
            judge=lambda _t: {"axis": "summarize-extract", "min_ordinal": 3},
        )
        assert sel.from_catalog is False
        assert sel.model == resolve_model(Tier.CLOUD_SMALL)

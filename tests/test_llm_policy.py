"""Tests for the selection policy — deterministic requirement→model
(llm-catalog slice 4, docs/proposals/llm-catalog.md).

Covers: the degrade-to-Tier-floor invariant (empty catalog ⇒ resolve_model), the
hard filters (window / required flags / budget band), the cheapest-meeting-axis
rank, and the Pareto "next better" rung. Real PG; each test starts from a clean
catalog so the shared test DB's other cards don't leak into the candidate set.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def clean_catalog(store: Any) -> Any:
    """Soft-delete every existing llm card so select_offering's candidate set is
    exactly what the test creates."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET deleted_at = now() "
            "WHERE kind = 'llm' AND deleted_at IS NULL"
        )
    return store


def _card(store: Any, model_id: str, **kw: Any) -> int:
    from precis import llm_catalog

    rid, _ = llm_catalog.upsert_card(
        store, model_id=model_id, text=kw.pop("text", "x"), **kw
    )
    return rid


def _req(**kw: Any) -> Any:
    from precis.utils.llm.policy import Requirement
    from precis.utils.llm.router import Tier

    kw.setdefault("tier_floor", Tier.CLOUD_MID)
    if isinstance(kw["tier_floor"], str):
        kw["tier_floor"] = Tier(kw["tier_floor"])
    return Requirement(**kw)


class TestDegradeToFloor:
    def test_empty_catalog_uses_tier_floor(self, clean_catalog: Any) -> None:
        from precis.utils.llm.policy import select_offering
        from precis.utils.llm.router import Tier, resolve_model

        sel = select_offering(clean_catalog, _req(tier_floor=Tier.CLOUD_SUPER))
        assert sel.from_catalog is False
        assert sel.model == resolve_model(Tier.CLOUD_SUPER)
        assert sel.next_better is None

    def test_no_candidate_meets_min_degrades(self, clean_catalog: Any) -> None:
        from precis.utils.llm.policy import select_offering
        from precis.utils.llm.router import Tier, resolve_model

        _card(
            clean_catalog,
            "weak",
            tier_floor="cloud-mid",
            offerings=[{"transport": "claude_agent"}],
            capability={"code": 2},
        )
        sel = select_offering(clean_catalog, _req(axis="code", min_ordinal=5))
        assert sel.from_catalog is False
        assert sel.model == resolve_model(Tier.CLOUD_MID)


class TestRank:
    def test_cheapest_meeting_axis_wins_with_next_better(
        self, clean_catalog: Any
    ) -> None:
        from precis.utils.llm.policy import select_offering
        from precis.utils.llm.router import Tier

        _card(
            clean_catalog,
            "cheap3",
            tier_floor="cloud-small",
            offerings=[{"transport": "claude_agent", "price_in": 1.0}],
            capability={"code": 3},
        )
        _card(
            clean_catalog,
            "mid4",
            tier_floor="cloud-mid",
            offerings=[{"transport": "claude_agent", "price_in": 3.0}],
            capability={"code": 4},
        )
        sel = select_offering(
            clean_catalog, _req(tier_floor=Tier.CLOUD_SMALL, axis="code", min_ordinal=3)
        )
        assert sel.model == "cheap3" and sel.from_catalog is True
        assert sel.offering is not None
        # the Pareto step up (more capability, more cost)
        assert sel.next_better == "mid4"

    def test_top_capability_has_no_next_better(self, clean_catalog: Any) -> None:
        from precis.utils.llm.policy import select_offering
        from precis.utils.llm.router import Tier

        _card(
            clean_catalog,
            "only5",
            tier_floor="cloud-mid",
            offerings=[{"transport": "claude_agent", "price_in": 3.0}],
            capability={"code": 5},
        )
        sel = select_offering(
            clean_catalog, _req(tier_floor=Tier.CLOUD_MID, axis="code", min_ordinal=4)
        )
        assert sel.model == "only5" and sel.next_better is None


class TestHardFilters:
    def test_window_excludes_too_small(self, clean_catalog: Any) -> None:
        from precis.utils.llm.policy import select_offering
        from precis.utils.llm.router import Tier

        _card(
            clean_catalog,
            "narrow",
            tier_floor="local-small",
            offerings=[{"transport": "litellm", "max_input": 1000}],
        )
        _card(
            clean_catalog,
            "wide",
            tier_floor="local-small",
            offerings=[{"transport": "litellm", "max_input": 200_000}],
        )
        sel = select_offering(
            clean_catalog, _req(tier_floor=Tier.LOCAL_SMALL, max_input=50_000)
        )
        assert sel.model == "wide"

    def test_needs_tools_flag(self, clean_catalog: Any) -> None:
        from precis.utils.llm.policy import select_offering
        from precis.utils.llm.router import Tier

        rid = _card(
            clean_catalog,
            "notools",
            tier_floor="local-small",
            offerings=[{"transport": "litellm"}],
        )
        clean_catalog.update_ref(
            rid,
            meta_patch={"facts_openrouter": {"supported_parameters": ["max_tokens"]}},
        )
        rid2 = _card(
            clean_catalog,
            "hastools",
            tier_floor="local-small",
            offerings=[{"transport": "litellm"}],
        )
        clean_catalog.update_ref(
            rid2,
            meta_patch={
                "facts_openrouter": {"supported_parameters": ["tools", "max_tokens"]}
            },
        )
        sel = select_offering(
            clean_catalog, _req(tier_floor=Tier.LOCAL_SMALL, needs_tools=True)
        )
        assert sel.model == "hastools"

    def test_budget_band_excludes_gated_tier(
        self, clean_catalog: Any, monkeypatch: Any
    ) -> None:
        from precis.budget import breaker
        from precis.utils.llm.policy import select_offering
        from precis.utils.llm.router import Tier

        _card(
            clean_catalog,
            "expensive5",
            tier_floor="cloud-super",
            offerings=[{"transport": "claude_agent", "price_in": 15.0}],
            capability={"code": 5},
        )
        _card(
            clean_catalog,
            "mid4",
            tier_floor="cloud-mid",
            offerings=[{"transport": "claude_agent", "price_in": 3.0}],
            capability={"code": 4},
        )
        # Force the expensive band tripped: the cloud-super candidate is filtered
        # out, so the pick + the escalation both respect the budget.
        monkeypatch.setattr(
            breaker,
            "gate_tier",
            lambda tier, store=None: "tripped" if tier is Tier.CLOUD_SUPER else None,
        )
        sel = select_offering(
            clean_catalog, _req(tier_floor=Tier.CLOUD_MID, axis="code", min_ordinal=4)
        )
        assert sel.model == "mid4" and sel.next_better is None


class TestEndpointBooking:
    """gripe 162624 — select_offering pins the cheapest fitting endpoint."""

    def test_books_cheapest_fitting_endpoint(self, clean_catalog: Any) -> None:
        from precis.utils.llm.policy import select_offering

        _card(
            clean_catalog,
            "z-ai/glm-5.2",
            tier_floor="cloud-super",
            capability={"code": 5},
            endpoints=[
                {
                    "provider": "Baidu",
                    "quant": "fp8",
                    "max_input": 1_048_576,
                    "price_in": 0.97,
                    "tools": True,
                    "status": 0,
                },
                {
                    "provider": "DeepInfra",
                    "quant": "fp4",
                    "max_input": 1_048_576,
                    "price_in": 0.93,
                    "tools": True,
                    "status": 0,
                },
            ],
        )
        sel = select_offering(
            clean_catalog, _req(tier_floor="cloud-super", axis="code", min_ordinal=5)
        )
        assert sel.from_catalog
        assert sel.endpoint is not None
        assert sel.endpoint["provider"] == "DeepInfra"  # cheapest (0.93)
        assert "DeepInfra/fp4" in sel.reason

    def test_endpoint_window_filter_excludes_small_variant(
        self, clean_catalog: Any
    ) -> None:
        from precis.utils.llm.policy import select_offering

        _card(
            clean_catalog,
            "wide-model",
            tier_floor="cloud-super",
            endpoints=[
                {
                    "provider": "Small",
                    "quant": "fp8",
                    "max_input": 101_376,
                    "price_in": 0.1,
                    "status": 0,
                },
                {
                    "provider": "Wide",
                    "quant": "fp8",
                    "max_input": 1_048_576,
                    "price_in": 0.5,
                    "status": 0,
                },
            ],
        )
        # need a 500k window → the cheap 101k endpoint can't be booked
        sel = select_offering(
            clean_catalog, _req(tier_floor="cloud-super", max_input=500_000)
        )
        assert sel.endpoint is not None
        assert sel.endpoint["provider"] == "Wide"

    def test_no_endpoints_leaves_endpoint_none(self, clean_catalog: Any) -> None:
        from precis.utils.llm.policy import select_offering

        _card(
            clean_catalog,
            "slug-only",
            tier_floor="cloud-super",
            offerings=[{"transport": "openai_compat", "price_in": 1.0}],
        )
        sel = select_offering(clean_catalog, _req(tier_floor="cloud-super"))
        assert sel.from_catalog
        assert sel.endpoint is None  # bare slug, today's behaviour

    def test_endpoint_capability_overrides_card_ordinal(self) -> None:
        from precis.utils.llm.policy import _axis_ordinal

        meta = {"capability": {"code": 3}}
        ep = {"capability": {"code": {"score": 5}}}
        assert _axis_ordinal(meta, "code") == 3
        assert _axis_ordinal(meta, "code", ep) == 5  # variant-scoped wins

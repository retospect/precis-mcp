"""Tests for the selection policy — deterministic requirement→model
(llm-catalog slice 4, ``llm-catalog`` (git-only)).

Covers: the degrade-to-Tier-floor invariant (empty catalog ⇒ resolve_model), the
hard filters (window / required flags / budget band), the cheapest-meeting-axis
rank, and the Pareto "next better" rung. Real PG; each test starts from a clean
catalog so the shared test DB's other cards don't leak into the candidate set.
"""

from __future__ import annotations

import json
from typing import Any

import pytest


def _seed_heartbeat(store: Any, host: str, *, minutes_ago: float = 0.0) -> None:
    """Mark a serving host alive (or dark) via its host_heartbeat row — the
    freshness signal ``_live_served_hosts`` consults."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat (host, ts, meta) "
            "VALUES (%s, now() - (%s || ' minutes')::interval, %s::jsonb) "
            "ON CONFLICT (host) DO UPDATE SET ts = EXCLUDED.ts",
            (host, minutes_ago, json.dumps({})),
        )
        conn.commit()


@pytest.fixture
def clean_catalog(store: Any) -> Any:
    """Soft-delete every existing llm card so select_offering's candidate set is
    exactly what the test creates."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET retired_at = now() "
            "WHERE kind = 'llm' AND retired_at IS NULL"
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

    kw.setdefault("tier_floor", Tier.BIG)
    if isinstance(kw["tier_floor"], str):
        kw["tier_floor"] = Tier(kw["tier_floor"])
    return Requirement(**kw)


class TestDegradeToFloor:
    def test_empty_catalog_uses_tier_floor(self, clean_catalog: Any) -> None:
        from precis.utils.llm.policy import select_offering
        from precis.utils.llm.router import Tier, resolve_model

        sel = select_offering(clean_catalog, _req(tier_floor=Tier.FRONTIER))
        assert sel.from_catalog is False
        assert sel.model == resolve_model(Tier.FRONTIER)
        assert sel.next_better is None

    def test_no_candidate_meets_min_degrades(self, clean_catalog: Any) -> None:
        from precis.utils.llm.policy import select_offering
        from precis.utils.llm.router import Tier, resolve_model

        _card(
            clean_catalog,
            "weak",
            tier_floor="big",
            offerings=[{"transport": "claude_agent"}],
            capability={"code": 2},
        )
        sel = select_offering(clean_catalog, _req(axis="code", min_ordinal=5))
        assert sel.from_catalog is False
        assert sel.model == resolve_model(Tier.BIG)


class TestRank:
    def test_cheapest_meeting_axis_wins_with_next_better(
        self, clean_catalog: Any
    ) -> None:
        from precis.utils.llm.policy import select_offering
        from precis.utils.llm.router import Tier

        _card(
            clean_catalog,
            "cheap3",
            tier_floor="medium",
            offerings=[{"transport": "claude_agent", "price_in": 1.0}],
            capability={"code": 3},
        )
        _card(
            clean_catalog,
            "mid4",
            tier_floor="big",
            offerings=[{"transport": "claude_agent", "price_in": 3.0}],
            capability={"code": 4},
        )
        sel = select_offering(
            clean_catalog, _req(tier_floor=Tier.MEDIUM, axis="code", min_ordinal=3)
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
            tier_floor="big",
            offerings=[{"transport": "claude_agent", "price_in": 3.0}],
            capability={"code": 5},
        )
        sel = select_offering(
            clean_catalog, _req(tier_floor=Tier.BIG, axis="code", min_ordinal=4)
        )
        assert sel.model == "only5" and sel.next_better is None


class TestHardFilters:
    def test_window_excludes_too_small(self, clean_catalog: Any) -> None:
        from precis.utils.llm.policy import select_offering
        from precis.utils.llm.router import Tier

        _card(
            clean_catalog,
            "narrow",
            tier_floor="small",
            offerings=[{"transport": "local", "max_input": 1000}],
        )
        _card(
            clean_catalog,
            "wide",
            tier_floor="small",
            offerings=[{"transport": "local", "max_input": 200_000}],
        )
        sel = select_offering(
            clean_catalog, _req(tier_floor=Tier.SMALL, max_input=50_000)
        )
        assert sel.model == "wide"

    def test_needs_tools_flag(self, clean_catalog: Any) -> None:
        from precis.utils.llm.policy import select_offering
        from precis.utils.llm.router import Tier

        rid = _card(
            clean_catalog,
            "notools",
            tier_floor="small",
            offerings=[{"transport": "local"}],
        )
        clean_catalog.update_ref(
            rid,
            meta_patch={"facts_openrouter": {"supported_parameters": ["max_tokens"]}},
        )
        rid2 = _card(
            clean_catalog,
            "hastools",
            tier_floor="small",
            offerings=[{"transport": "local"}],
        )
        clean_catalog.update_ref(
            rid2,
            meta_patch={
                "facts_openrouter": {"supported_parameters": ["tools", "max_tokens"]}
            },
        )
        sel = select_offering(
            clean_catalog, _req(tier_floor=Tier.SMALL, needs_tools=True)
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
            tier_floor="frontier",
            offerings=[{"transport": "claude_agent", "price_in": 15.0}],
            capability={"code": 5},
        )
        _card(
            clean_catalog,
            "mid4",
            tier_floor="big",
            offerings=[{"transport": "claude_agent", "price_in": 3.0}],
            capability={"code": 4},
        )
        # Force the expensive band tripped: the frontier candidate is filtered
        # out, so the pick + the escalation both respect the budget.
        monkeypatch.setattr(
            breaker,
            "gate_tier",
            lambda tier, local=False, store=None: (
                "tripped" if tier is Tier.FRONTIER and not local else None
            ),
        )
        sel = select_offering(
            clean_catalog, _req(tier_floor=Tier.BIG, axis="code", min_ordinal=4)
        )
        assert sel.model == "mid4" and sel.next_better is None


class TestEndpointBooking:
    """gripe 162624 — select_offering pins the cheapest fitting endpoint."""

    def test_books_cheapest_fitting_endpoint(self, clean_catalog: Any) -> None:
        from precis.utils.llm.policy import select_offering

        _card(
            clean_catalog,
            "z-ai/glm-5.2",
            tier_floor="frontier",
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
            clean_catalog, _req(tier_floor="frontier", axis="code", min_ordinal=5)
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
            tier_floor="frontier",
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
            clean_catalog, _req(tier_floor="frontier", max_input=500_000)
        )
        assert sel.endpoint is not None
        assert sel.endpoint["provider"] == "Wide"

    def test_no_endpoints_leaves_endpoint_none(self, clean_catalog: Any) -> None:
        from precis.utils.llm.policy import select_offering

        _card(
            clean_catalog,
            "slug-only",
            tier_floor="frontier",
            offerings=[{"transport": "openai_compat", "price_in": 1.0}],
        )
        sel = select_offering(clean_catalog, _req(tier_floor="frontier"))
        assert sel.from_catalog
        assert sel.endpoint is None  # bare slug, today's behaviour

    def test_endpoint_capability_overrides_card_ordinal(self) -> None:
        from precis.utils.llm.policy import _axis_ordinal

        meta = {"capability": {"code": 3}}
        ep = {"capability": {"code": {"score": 5}}}
        assert _axis_ordinal(meta, "code") == 3
        assert _axis_ordinal(meta, "code", ep) == 5  # variant-scoped wins


class TestFreeLocal:
    """A served-local card is free: it prices at 0 (so it wins the cost sort and
    is a valid ``next_better`` rung) and survives a tripped $ cap (local-first
    invariant). The dead ``tier_floor.startswith('local')`` fast path — broken
    since migration 0090 relabelled tiers — used to price it ``inf``."""

    def test_model_price_zero_for_served_local(self) -> None:
        from precis.utils.llm.policy import _model_price

        # card-level served_by → free even with no price on the card
        assert _model_price({"served_by": [{"host": "spark"}]}) == 0.0
        # offering-nested served_by → also free (§6 nests it under the offering)
        assert (
            _model_price(
                {
                    "offerings": [
                        {"transport": "openai_tools", "served_by": [{"host": "spark"}]}
                    ]
                }
            )
            == 0.0
        )
        # an unpriced *cloud* card is still unknown → sorts last, never free
        assert _model_price({"offerings": [{"transport": "openai_compat"}]}) == float(
            "inf"
        )

    def test_served_local_beats_paid_cloud_on_cost(self, clean_catalog: Any) -> None:
        from precis.utils.llm.policy import select_offering
        from precis.utils.llm.router import Tier

        _card(
            clean_catalog,
            "cloud-cheap",
            tier_floor="big",
            offerings=[{"transport": "openai_compat", "price_in": 0.5}],
            capability={"code": 4},
        )
        _card(
            clean_catalog,
            "local-served",
            tier_floor="big",
            offerings=[{"transport": "openai_tools"}],
            served_by=[{"host": "spark", "max_parallel": 2}],
            capability={"code": 4},
        )
        sel = select_offering(
            clean_catalog, _req(tier_floor=Tier.BIG, axis="code", min_ordinal=4)
        )
        assert sel.model == "local-served"  # free (0) < paid cloud (0.5)

    def test_served_local_survives_tripped_cap(
        self, clean_catalog: Any, monkeypatch: Any
    ) -> None:
        from precis.budget import breaker
        from precis.utils.llm.policy import select_offering
        from precis.utils.llm.router import Tier

        # Everything paid is tripped; only a local-exempted call passes.
        monkeypatch.setattr(
            breaker,
            "gate_tier",
            lambda tier, local=False, store=None: None if local else "tripped",
        )
        _card(
            clean_catalog,
            "cloud-big",
            tier_floor="big",
            offerings=[{"transport": "openai_compat", "price_in": 0.5}],
            capability={"code": 4},
        )
        _card(
            clean_catalog,
            "local-big",
            tier_floor="big",
            offerings=[{"transport": "openai_tools"}],
            served_by=[{"host": "spark"}],
            capability={"code": 4},
        )
        sel = select_offering(
            clean_catalog, _req(tier_floor=Tier.BIG, axis="code", min_ordinal=4)
        )
        # The cloud card is filtered by the tripped cap; the served-local one
        # survives (would have degraded to the tier floor if it were gated too).
        assert sel.from_catalog is True and sel.model == "local-big"


class TestLocalOfferingSelection:
    """gr502698 — the picked *offering*: a live local serving host books the
    $0 ``transport='local'`` operating point; a dark host falls back to the
    cloud offering on the same card."""

    def _served_card(self, store: Any, model_id: str = "oss-served") -> int:
        return _card(
            store,
            model_id,
            tier_floor="medium",
            offerings=[{"transport": "openai_compat", "price_in": 0.5}],
            served_by=[{"host": "castor", "max_parallel": 4}],
            capability={"code": 3},
        )

    def test_live_host_picks_the_local_offering(self, clean_catalog: Any) -> None:
        from precis.utils.llm.policy import select_offering

        self._served_card(clean_catalog)
        _seed_heartbeat(clean_catalog, "castor", minutes_ago=0)
        sel = select_offering(clean_catalog, _req(tier_floor="medium"))
        assert sel.from_catalog and sel.model == "oss-served"
        assert sel.offering is not None
        assert sel.offering["transport"] == "local"
        assert sel.offering["served_by"][0]["host"] == "castor"

    def test_dark_host_falls_back_to_the_cloud_offering(
        self, clean_catalog: Any
    ) -> None:
        from precis.utils.llm.policy import select_offering

        self._served_card(clean_catalog)
        _seed_heartbeat(clean_catalog, "castor", minutes_ago=60)  # stale past HOST_DARK
        sel = select_offering(clean_catalog, _req(tier_floor="medium"))
        assert sel.offering is not None
        assert sel.offering["transport"] == "openai_compat"

    def test_no_heartbeat_row_reads_as_dark(self, clean_catalog: Any) -> None:
        from precis.utils.llm.policy import select_offering

        self._served_card(clean_catalog)
        sel = select_offering(clean_catalog, _req(tier_floor="medium"))
        assert sel.offering is not None
        assert sel.offering["transport"] == "openai_compat"

    def test_transport_local_requirement_picks_live_local(
        self, clean_catalog: Any
    ) -> None:
        from precis.utils.llm.policy import select_offering

        self._served_card(clean_catalog)
        _seed_heartbeat(clean_catalog, "castor", minutes_ago=0)
        sel = select_offering(
            clean_catalog, _req(tier_floor="medium", transport="local")
        )
        assert sel.offering is not None
        assert sel.offering["transport"] == "local"

    def test_transport_local_requirement_falls_back_when_dark(
        self, clean_catalog: Any
    ) -> None:
        """An explicit ``transport='local'`` requirement does not book a dead
        loopback — the cloud fallback wins (the strict pin is
        ``LlmRequest.placement='local'``, a different layer)."""
        from precis.utils.llm.policy import select_offering

        self._served_card(clean_catalog)
        _seed_heartbeat(clean_catalog, "castor", minutes_ago=60)
        sel = select_offering(
            clean_catalog, _req(tier_floor="medium", transport="local")
        )
        assert sel.offering is not None
        assert sel.offering["transport"] == "openai_compat"

    def test_transport_openai_compat_still_picks_cloud_when_local_is_live(
        self, clean_catalog: Any
    ) -> None:
        from precis.utils.llm.policy import select_offering

        self._served_card(clean_catalog)
        _seed_heartbeat(clean_catalog, "castor", minutes_ago=0)
        sel = select_offering(
            clean_catalog, _req(tier_floor="medium", transport="openai_compat")
        )
        assert sel.offering is not None
        assert sel.offering["transport"] == "openai_compat"

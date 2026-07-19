"""Tests for the `llm` catalog — model choice as a queryable resource
(llm-catalog slice 1, docs/proposals/llm-catalog.md).

Covers: the shared writer (`upsert_card` — create emits the embeddable card +
stamps facts; idempotent refresh), the handler surface (model-slug resolution on
`get`, guarded `put`, faceted render, lexical search), meta validation, and the
`llm_reconcile` pass (fact refresh from an injected feed + proxy-drift alerting +
auto-resolve). Runs against real PG (the `store` fixture) so it exercises
migration 0071's seeds.
"""

from __future__ import annotations

from typing import Any

import pytest


def _handler(store: Any) -> Any:
    from precis.dispatch import Hub
    from precis.handlers.llm import LlmHandler

    return LlmHandler(hub=Hub(store=store))


def _open_drift_alerts(store: Any) -> list[tuple[int, str]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'alert'
               AND r.deleted_at IS NULL
               AND r.meta->>'alert_source' = 'llm_reconcile:drift'
               AND t.namespace = 'OPEN'
               AND t.value = 'alert-state:open'
            """
        ).fetchall()
    return [(int(r[0]), r[1]) for r in rows]


class TestUpsertCard:
    def test_create_emits_card_and_stamps_meta(self, store: Any) -> None:
        from precis import llm_catalog

        ref_id, created = llm_catalog.upsert_card(
            store,
            model_id="claude-opus-4-8",
            text="Cloud reasoning tier; strong at careful SQL and refactors.",
            tier_floor="cloud-super",
            offerings=[{"effort": "medium", "transport": "claude_agent"}],
            capability={"code": 5, "long-context-recall": {"score": 4}},
            provenance={"source": "seed"},
        )
        assert created is True
        ref = store.get_ref(kind="llm", id=ref_id)
        assert ref is not None
        assert ref.meta["model_id"] == "claude-opus-4-8"
        assert ref.meta["tier_floor"] == "cloud-super"
        assert ref.meta["offerings"][0]["transport"] == "claude_agent"
        # embeddable card_combined (ord=-1) = the model vector
        with store.pool.connection() as conn:
            card = conn.execute(
                "select text from chunks where ref_id=%s and ord=-1", (ref_id,)
            ).fetchone()
        assert card is not None and "careful SQL" in card[0]

    def test_upsert_is_idempotent_on_model_id(self, store: Any) -> None:
        from precis import llm_catalog

        rid1, created1 = llm_catalog.upsert_card(
            store, model_id="qwen-heavy", text="Local big tier, v1."
        )
        rid2, created2 = llm_catalog.upsert_card(
            store,
            model_id="qwen-heavy",
            text="Local big tier, v2 (refreshed).",
            offerings=[{"transport": "openai_tools"}],
        )
        assert created1 is True and created2 is False
        assert rid1 == rid2
        ref = store.get_ref(kind="llm", id=rid1)
        assert ref.title == "Local big tier, v2 (refreshed)."
        assert ref.meta["offerings"][0]["transport"] == "openai_tools"


class TestMetaValidation:
    def test_rejects_unknown_offering_key(self, store: Any) -> None:
        from precis import llm_catalog
        from precis.errors import BadInput

        with pytest.raises(BadInput):
            llm_catalog.upsert_card(
                store, model_id="m1", text="x", offerings=[{"bogus": 1}]
            )

    def test_rejects_unknown_capability_axis(self, store: Any) -> None:
        from precis import llm_catalog
        from precis.errors import BadInput

        with pytest.raises(BadInput):
            llm_catalog.upsert_card(
                store, model_id="m2", text="x", capability={"vibes": 5}
            )

    def test_rejects_out_of_range_ordinal(self, store: Any) -> None:
        from precis import llm_catalog
        from precis.errors import BadInput

        with pytest.raises(BadInput):
            llm_catalog.upsert_card(
                store, model_id="m3", text="x", capability={"code": 9}
            )


class TestServedBy:
    """Local-serving declarations (the `served_by.endpoint` routing path)."""

    def test_card_level_served_by_with_model_roundtrips(self, store: Any) -> None:
        from precis import llm_catalog

        rid, _ = llm_catalog.upsert_card(
            store,
            model_id="qwen-local",
            text="Local big tier served direct by llama-swap.",
            served_by=[
                {
                    "host": "melchior",
                    "endpoint": "http://127.0.0.1:11445/v1",
                    "model": "qwen3-next-80b-a3b-q4_k_m",
                    "max_parallel": 2,
                }
            ],
        )
        entry = store.get_ref(kind="llm", id=rid).meta["served_by"][0]
        assert entry["host"] == "melchior"
        assert entry["endpoint"] == "http://127.0.0.1:11445/v1"
        assert entry["model"] == "qwen3-next-80b-a3b-q4_k_m"
        assert entry["max_parallel"] == 2

    def test_offering_nested_served_by_carries_model(self, store: Any) -> None:
        from precis import llm_catalog

        rid, _ = llm_catalog.upsert_card(
            store,
            model_id="spark-local",
            text="x",
            offerings=[
                {
                    "transport": "litellm",
                    "served_by": [
                        {
                            "host": "spark",
                            "endpoint": "http://127.0.0.1:11434/v1",
                            "model": "llama3.3:70b",
                        }
                    ],
                }
            ],
        )
        off = store.get_ref(kind="llm", id=rid).meta["offerings"][0]
        assert off["served_by"][0]["model"] == "llama3.3:70b"

    def test_rejects_unknown_served_by_key(self, store: Any) -> None:
        from precis import llm_catalog
        from precis.errors import BadInput

        with pytest.raises(BadInput):
            llm_catalog.upsert_card(
                store, model_id="sb1", text="x", served_by=[{"host": "m", "bogus": 1}]
            )

    def test_rejects_non_string_model(self, store: Any) -> None:
        from precis import llm_catalog
        from precis.errors import BadInput

        with pytest.raises(BadInput):
            llm_catalog.upsert_card(
                store, model_id="sb2", text="x", served_by=[{"host": "m", "model": 123}]
            )

    def test_rejects_missing_host(self, store: Any) -> None:
        from precis import llm_catalog
        from precis.errors import BadInput

        with pytest.raises(BadInput):
            llm_catalog.upsert_card(
                store,
                model_id="sb3",
                text="x",
                served_by=[{"endpoint": "http://x/v1", "model": "m"}],
            )


class TestHandler:
    def test_get_resolves_by_model_slug(self, store: Any) -> None:
        from precis import llm_catalog

        rid, _ = llm_catalog.upsert_card(
            store,
            model_id="claude-sonnet-4-6",
            text="Mid agentic tier — the workhorse rung.",
            tier_floor="cloud-mid",
        )
        h = _handler(store)
        resp = h.get(id="claude-sonnet-4-6")
        assert "claude-sonnet-4-6" in resp.body
        assert "workhorse" in resp.body
        # numeric id resolves to the same card
        assert f"lm{rid}" in h.get(id=rid).body

    def test_get_unknown_slug_raises_not_found(self, store: Any) -> None:
        from precis.errors import NotFound

        h = _handler(store)
        with pytest.raises(NotFound):
            h.get(id="no-such-model-xyz")

    def test_put_requires_model_id(self, store: Any) -> None:
        from precis.errors import BadInput

        h = _handler(store)
        with pytest.raises(BadInput):
            h.put(text="a card with no model id")

    def test_put_with_id_appends_review(self, store: Any) -> None:
        # Slice 3: put(id=…, text=…, entry=…) appends a WORM review entry.
        from precis import llm_catalog
        from precis.errors import BadInput

        rid, _ = llm_catalog.upsert_card(
            store, model_id="put-id-model", text="A model."
        )
        h = _handler(store)
        resp = h.put(id=rid, text="solid on migrations", entry="agent-review")
        assert "review" in resp.body
        # missing text is still rejected
        with pytest.raises(BadInput):
            h.put(id=rid)

    def test_put_creates_then_refreshes(self, store: Any) -> None:
        h = _handler(store)
        r1 = h.put(model_id="deepseek-ai/DeepSeek-V3", text="Hosted OSS, v1.")
        assert "created" in r1.body
        r2 = h.put(model_id="deepseek-ai/DeepSeek-V3", text="Hosted OSS, v2.")
        assert "refreshed" in r2.body
        # slug with slashes/case round-trips through meta lookup (not a tag)
        assert "DeepSeek-V3" in h.get(id="deepseek-ai/DeepSeek-V3").body

    def test_put_threads_served_by(self, store: Any) -> None:
        # The MCP put surface must forward served_by to the catalog (else a
        # served_by= lands in **_kw and is silently dropped — no local routing).
        h = _handler(store)
        h.put(
            model_id="local-served",
            text="Served locally by llama-swap.",
            served_by=[
                {
                    "host": "melchior",
                    "endpoint": "http://127.0.0.1:11445/v1",
                    "model": "qwen3-next-80b-a3b-q4_k_m",
                }
            ],
        )
        existing = store.find_ref_by_meta(
            kind="llm", key="model_id", value="local-served"
        )
        entry = store.get_ref(kind="llm", id=existing.id).meta["served_by"][0]
        assert entry["endpoint"] == "http://127.0.0.1:11445/v1"
        assert entry["model"] == "qwen3-next-80b-a3b-q4_k_m"

    def test_search_matches_capability_prose(self, store: Any) -> None:
        from precis import llm_catalog

        llm_catalog.upsert_card(
            store,
            model_id="claude-haiku-4-5",
            text="Fast cheap triage classifier for one-shot JSON judgement.",
            tier_floor="cloud-small",
        )
        h = _handler(store)
        resp = h.search(q="triage classifier")
        # lexical search over the capability prose finds the card.
        assert "llm card match" in resp.body
        assert "triage classifier" in resp.body


class TestVariantOfferings:
    """gripe 162624 — per-endpoint bookable variants + variant-scoped benchmarks."""

    def test_reconcile_populates_endpoints(self, store: Any) -> None:
        from precis import llm_catalog
        from precis.workers.llm_reconcile import _norm_model_key, run_llm_reconcile_pass

        rid, _ = llm_catalog.upsert_card(
            store, model_id="z-ai/glm-5.2", text="GLM 5.2 card."
        )
        key = _norm_model_key("z-ai/glm-5.2")
        eps = [
            {
                "provider": "Baidu",
                "quant": "fp8",
                "max_input": 1_048_576,
                "price_in": 0.97,
                "price_out": 3.0,
                "tools": True,
                "status": 0,
            },
            {
                "provider": "Ambient",
                "quant": "fp8",
                "max_input": 101_376,
                "price_in": 1.05,
                "price_out": 3.5,
                "tools": True,
                "status": 0,
            },
            {
                "provider": "DeepInfra",
                "quant": "fp4",
                "max_input": 1_048_576,
                "price_in": 0.93,
                "price_out": 3.0,
                "tools": True,
                "status": 0,
            },
        ]
        res = run_llm_reconcile_pass(
            store, models={}, endpoints_by_key={key: eps}, force=True
        )
        assert res.ok >= 1
        ref = store.get_ref(kind="llm", id=rid)
        got = ref.meta["endpoints"]
        assert len(got) == 3
        assert {e["provider"] for e in got} == {"Baidu", "Ambient", "DeepInfra"}
        assert ref.meta.get("endpoints_reconciled_at")

    def test_endpoint_price_and_window_are_variant_precise(self, store: Any) -> None:
        # The cheapest bookable price + widest window come from the endpoints, not
        # the single seed offering (which named one generic price).
        from precis import llm_catalog
        from precis.utils.llm.admit import window_for
        from precis.utils.llm.policy import _model_price

        rid, _ = llm_catalog.upsert_card(
            store,
            model_id="var-model",
            text="A model.",
            tier_floor="cloud-super",
            offerings=[
                {"transport": "openai_compat", "max_input": 200_000, "price_in": 5.0}
            ],
            endpoints=[
                {
                    "provider": "A",
                    "quant": "fp4",
                    "max_input": 1_048_576,
                    "price_in": 0.9,
                },
                {
                    "provider": "B",
                    "quant": "fp8",
                    "max_input": 101_376,
                    "price_in": 1.2,
                },
            ],
        )
        meta = store.get_ref(kind="llm", id=rid).meta
        # cheapest bookable = the fp4 endpoint (0.9), below the seed offering's 5.0
        assert _model_price(meta) == 0.9
        # widest window prefers the offering's max_input (most-specific) → 200k;
        # with no offering window it would fall to the endpoints' 1,048,576.
        assert window_for(meta) == 200_000
        assert window_for({"endpoints": meta["endpoints"]}) == 1_048_576

    def test_record_benchmark_scopes_ordinal_to_the_quant(self, store: Any) -> None:
        from precis import llm_catalog

        rid, _ = llm_catalog.upsert_card(
            store,
            model_id="bench-model",
            text="A model.",
            endpoints=[
                {"provider": "A", "quant": "fp8", "max_input": 1_048_576},
                {"provider": "B", "quant": "fp4", "max_input": 1_048_576},
            ],
        )
        n = llm_catalog.record_benchmark(
            store,
            "bench-model",
            axis="code",
            ordinal=5,
            quant="fp8",
            source_url="https://swebench.example/pro",
        )
        assert n >= 1
        meta = store.get_ref(kind="llm", id=rid).meta
        eps = {e["quant"]: e for e in meta["endpoints"]}
        # the fp8 endpoint carries the ordinal; the fp4 endpoint does NOT inherit it
        assert eps["fp8"]["capability"]["code"]["score"] == 5
        assert "capability" not in eps["fp4"] or "code" not in (
            eps["fp4"].get("capability") or {}
        )
        # a variant-scoped published-benchmark review lands with the quant in prov
        reviews = llm_catalog.list_reviews(store, rid)
        bench = [
            b
            for b in reviews
            if (b.meta or {}).get("entry_type") == "published-benchmark"
        ]
        assert bench and (bench[-1].meta or {}).get("variant") == "fp8"

    def test_frontier_seed_stamps_params(self, store: Any) -> None:
        from precis import llm_catalog

        llm_catalog.seed_frontier_cards(store)
        ref = store.find_ref_by_meta(kind="llm", key="model_id", value="z-ai/glm-5.2")
        assert (ref.meta.get("params") or {}).get("size") == "744B"

    def test_rejects_unknown_endpoint_key(self, store: Any) -> None:
        from precis import llm_catalog
        from precis.errors import BadInput

        with pytest.raises(BadInput):
            llm_catalog.upsert_card(
                store, model_id="ep1", text="x", endpoints=[{"bogus": 1}]
            )


class TestReconcile:
    def test_refresh_from_injected_feed(self, store: Any) -> None:
        from precis import llm_catalog
        from precis.workers.llm_reconcile import (
            _norm_model_key,
            run_llm_reconcile_pass,
        )

        rid, _ = llm_catalog.upsert_card(
            store, model_id="claude-opus-4-8", text="Opus card."
        )
        # OpenRouter names it anthropic/claude-opus-4.8 — normalisation must fold
        # both to the same key.
        assert _norm_model_key("anthropic/claude-opus-4.8") == _norm_model_key(
            "claude-opus-4-8"
        )
        feed = {
            _norm_model_key("claude-opus-4-8"): {
                "id": "anthropic/claude-opus-4.8",
                "context_length": 1_000_000,
                "top_provider": {"max_completion_tokens": 128_000},
                "pricing": {"prompt": "0.00001", "completion": "0.00005"},
                "supported_parameters": ["tools", "reasoning"],
            }
        }
        res = run_llm_reconcile_pass(store, models=feed, force=True)
        assert res.ok >= 1
        ref = store.get_ref(kind="llm", id=rid)
        facts = ref.meta["facts_openrouter"]
        assert facts["context_length"] == 1_000_000
        assert facts["price_in"] == 10.0  # 0.00001 * 1e6
        assert ref.meta.get("reconciled_at")

    def test_proxy_drift_raised_then_resolved(self, store: Any) -> None:
        from precis import llm_catalog
        from precis.workers.llm_reconcile import (
            _norm_model_key,
            run_llm_reconcile_pass,
        )

        # A card whose offering routes through the loopback proxy.
        llm_catalog.upsert_card(
            store,
            model_id="claude-opus-4-8",
            text="Opus, also served via the loopback litellm proxy.",
            offerings=[{"transport": "litellm", "endpoint": "http://127.0.0.1:4000"}],
        )
        # Proxy serves opus + 4-7, but NOT 4-8 → drift.
        proxy = {_norm_model_key("claude-opus"), _norm_model_key("claude-opus-4-7")}
        run_llm_reconcile_pass(store, models={}, proxy_models=proxy, force=True)
        alerts = _open_drift_alerts(store)
        assert any("claude-opus-4-8" in title for _id, title in alerts)

        # Proxy now serves 4-8 → the drift clears on the next pass.
        proxy_fixed = proxy | {_norm_model_key("claude-opus-4-8")}
        run_llm_reconcile_pass(store, models={}, proxy_models=proxy_fixed, force=True)
        assert not _open_drift_alerts(store)

    def test_no_drift_when_proxy_unknown(self, store: Any) -> None:
        from precis import llm_catalog
        from precis.workers.llm_reconcile import run_llm_reconcile_pass

        llm_catalog.upsert_card(
            store,
            model_id="claude-opus-4-8",
            text="Opus via proxy.",
            offerings=[{"transport": "litellm"}],
        )
        # proxy_models=None → we cannot assert absence, so no false alert.
        run_llm_reconcile_pass(store, models={}, proxy_models=None, force=True)
        assert not _open_drift_alerts(store)


class TestSeed:
    def test_seed_mints_a_card_per_tier(self, store: Any) -> None:
        from precis import llm_catalog
        from precis.utils.llm.router import Tier

        results = llm_catalog.seed_default_cards(store)
        model_ids = {model_id for model_id, _rid, _created in results}
        # One card per resolved model (distinct tier models).
        assert len(model_ids) >= 3
        cards = store.list_refs(kind="llm", limit=100)
        floors = {(c.meta or {}).get("tier_floor") for c in cards}
        assert {t.value for t in Tier} <= floors

    def test_frontier_seed_mints_oss_ladder(self, store: Any) -> None:
        from precis import llm_catalog
        from precis.llm_catalog import CAPABILITY_AXES

        results = llm_catalog.seed_frontier_cards(store)
        model_ids = {model_id for model_id, _rid, _created in results}
        # The curated ladder is present, spans the cloud tiers, and every card is
        # runnable (an offering) with a priced/windowed openai_compat operating point.
        assert "z-ai/glm-5.2" in model_ids
        assert len(model_ids) >= 10
        by_id = {
            (c.meta or {}).get("model_id"): c.meta or {}
            for c in store.list_refs(kind="llm", limit=100)
        }
        floors = {
            m.get("tier_floor")
            for m in by_id.values()
            if m.get("model_id") in model_ids
        }
        assert {"cloud-super", "cloud-mid", "cloud-small"} <= floors
        glm = by_id["z-ai/glm-5.2"]
        off = glm["offerings"][0]
        assert off["transport"] == "openai_compat"
        assert off["max_input"] == 1_048_576
        assert off["price_in"] > 0 and off["price_out"] > 0
        # Provisional capability ordinals are seeded, valid axes, 1..5.
        cap = glm["capability"]
        assert set(cap) <= set(CAPABILITY_AXES)
        assert all(1 <= v <= 5 for v in cap.values())

    def test_frontier_seed_is_idempotent(self, store: Any) -> None:
        from precis import llm_catalog

        first = llm_catalog.seed_frontier_cards(store)
        assert all(created for _mid, _rid, created in first)
        second = llm_catalog.seed_frontier_cards(store)
        assert not any(created for _mid, _rid, created in second)
        # Same ref ids on re-seed (upsert on model_id, no duplicates).
        assert {rid for _m, rid, _c in first} == {rid for _m, rid, _c in second}

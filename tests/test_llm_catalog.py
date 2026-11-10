"""Tests for the `llm` catalog — model choice as a queryable resource
(llm-catalog slice 1, ``llm-catalog`` (git-only)).

Covers: the shared writer (`upsert_card` — create emits the embeddable card +
stamps facts; idempotent refresh), the handler surface (model-slug resolution on
`get`, guarded `put`, faceted render, lexical search), meta validation, and the
`llm_reconcile` pass (fact refresh from an injected feed + proxy-drift alerting +
auto-resolve). Runs against real PG (the `store` fixture) so it exercises
migration 0071's seeds.
"""

from __future__ import annotations

import logging
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
               AND r.retired_at IS NULL
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
            tier_floor="frontier",
            offerings=[{"effort": "medium", "transport": "claude_agent"}],
            capability={"code": 5, "long-context-recall": {"score": 4}},
            provenance={"source": "seed"},
        )
        assert created is True
        ref = store.get_ref(kind="llm", id=ref_id)
        assert ref is not None
        assert ref.meta["model_id"] == "claude-opus-4-8"
        assert ref.meta["tier_floor"] == "frontier"
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
                    "transport": "local",
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


class TestLocalOffering:
    """gr502698 — a card-level ``served_by`` derives a ``transport='local'``
    offering in ``meta.offerings``, and no writer may silently drop it."""

    def test_upsert_derives_local_offering_from_served_by(self, store: Any) -> None:
        from precis import llm_catalog

        served = [{"host": "castor", "endpoint": "http://x/v1", "max_parallel": 4}]
        rid, _ = llm_catalog.upsert_card(
            store,
            model_id="oss-local",
            text="Served on the fleet AND on OpenRouter.",
            offerings=[{"transport": "openai_compat", "price_in": 0.5}],
            served_by=served,
        )
        offs = store.get_ref(kind="llm", id=rid).meta["offerings"]
        # local first: the derived mirror of the card-level served_by, then
        # the caller's cloud offering.
        assert offs[0] == {"transport": "local", "served_by": served}
        assert offs[1]["transport"] == "openai_compat"

    def test_offerings_rewrite_preserves_the_local_offering(self, store: Any) -> None:
        from precis import llm_catalog

        served = [{"host": "castor", "max_parallel": 2}]
        rid, _ = llm_catalog.upsert_card(
            store,
            model_id="oss-local2",
            text="x",
            offerings=[{"transport": "openai_compat", "price_in": 0.5}],
            served_by=served,
        )
        # The gripe's shape: a later wholesale offerings write (a seed's
        # cloud-only list) must not drop the local operating point.
        llm_catalog.upsert_card(
            store,
            model_id="oss-local2",
            text="x2",
            offerings=[{"transport": "openai_compat", "price_in": 0.6}],
        )
        offs = store.get_ref(kind="llm", id=rid).meta["offerings"]
        assert offs[0] == {"transport": "local", "served_by": served}
        assert offs[1]["price_in"] == 0.6
        # …and a re-run is stable (the derived offering is not duplicated).
        llm_catalog.upsert_card(
            store,
            model_id="oss-local2",
            text="x3",
            offerings=[{"transport": "openai_compat", "price_in": 0.7}],
        )
        offs = store.get_ref(kind="llm", id=rid).meta["offerings"]
        assert [o["transport"] for o in offs] == ["local", "openai_compat"]

    def test_served_by_change_resyncs_the_derived_offering(self, store: Any) -> None:
        from precis import llm_catalog

        rid, _ = llm_catalog.upsert_card(
            store,
            model_id="oss-moved",
            text="x",
            served_by=[{"host": "castor"}],
        )
        llm_catalog.upsert_card(
            store,
            model_id="oss-moved",
            text="x",
            served_by=[{"host": "spark", "max_parallel": 1}],
        )
        offs = store.get_ref(kind="llm", id=rid).meta["offerings"]
        assert offs == [
            {"transport": "local", "served_by": [{"host": "spark", "max_parallel": 1}]}
        ]

    def test_clearing_served_by_drops_the_derived_offering(self, store: Any) -> None:
        from precis import llm_catalog

        rid, _ = llm_catalog.upsert_card(
            store,
            model_id="oss-unserved",
            text="x",
            offerings=[{"transport": "openai_compat"}],
            served_by=[{"host": "castor"}],
        )
        llm_catalog.upsert_card(store, model_id="oss-unserved", text="x", served_by=[])
        meta = store.get_ref(kind="llm", id=rid).meta
        assert meta["served_by"] == []
        assert [o["transport"] for o in meta["offerings"]] == ["openai_compat"]

    def test_seed_frontier_keeps_the_local_offering(self, store: Any) -> None:
        """The exact gr502698 regression: ``seed --frontier`` rewrites
        ``meta.offerings`` wholesale — the local operating point must survive."""
        from precis import llm_catalog

        rid, _ = llm_catalog.upsert_card(
            store,
            model_id="openai/gpt-oss-120b",
            text="x",
            served_by=[
                {
                    "host": "castor",
                    "endpoint": "http://x:8000/v1",
                    "max_parallel": 32,
                    "source": "static",
                }
            ],
        )
        llm_catalog.seed_frontier_cards(store)
        meta = store.get_ref(kind="llm", id=rid).meta
        assert meta["offerings"][0]["transport"] == "local"
        assert meta["offerings"][0]["served_by"][0]["host"] == "castor"
        assert meta["offerings"][1]["transport"] == "openai_compat"

    def test_reconcile_resyncs_a_drifted_card(self, store: Any) -> None:
        from precis import llm_catalog
        from precis.workers.llm_reconcile import run_llm_reconcile_pass

        rid, _ = llm_catalog.upsert_card(store, model_id="drifted-local", text="x")
        # A direct meta write that bypassed the writer — the drifted shape.
        store.update_ref(
            rid,
            meta_patch={
                "served_by": [{"host": "castor"}],
                "offerings": [{"transport": "openai_compat"}],
            },
        )
        run_llm_reconcile_pass(store, force=True, _fetch=False)
        offs = store.get_ref(kind="llm", id=rid).meta["offerings"]
        assert [o["transport"] for o in offs] == ["local", "openai_compat"]

    def test_hand_authored_local_offering_is_preserved(self, store: Any) -> None:
        from precis import llm_catalog

        hand = {
            "transport": "local",
            "served_by": [{"host": "other", "max_parallel": 1}],
        }
        rid, _ = llm_catalog.upsert_card(
            store,
            model_id="two-local",
            text="x",
            offerings=[hand, {"transport": "openai_compat"}],
            served_by=[{"host": "castor"}],
        )
        offs = store.get_ref(kind="llm", id=rid).meta["offerings"]
        assert offs[0]["served_by"][0]["host"] == "castor"  # derived, first
        assert offs[1] == hand  # the hand-authored one survives verbatim


class TestListCommand:
    def test_list_shows_offerings_local_first_with_hosts(
        self, store: Any, capsys: Any
    ) -> None:
        from precis import llm_catalog
        from precis.cli.llm import _cmd_list

        llm_catalog.upsert_card(
            store,
            model_id="listed-local",
            text="x",
            offerings=[{"transport": "openai_compat"}],
            served_by=[{"host": "castor", "max_parallel": 4}],
        )
        _cmd_list(store)
        line = next(
            l for l in capsys.readouterr().out.splitlines() if "listed-local" in l
        )
        assert "local(castor), openai_compat" in line
        assert line.index("local(castor)") < line.index("openai_compat")


class TestHandler:
    def test_get_resolves_by_model_slug(self, store: Any) -> None:
        from precis import llm_catalog

        rid, _ = llm_catalog.upsert_card(
            store,
            model_id="claude-sonnet-4-6",
            text="Mid agentic tier — the workhorse rung.",
            tier_floor="big",
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

    def test_get_unknown_slug_next_is_a_runnable_search(self, store: Any) -> None:
        """gr581238 — the search hint carries the asked-for slug; a literal
        ``q='...'`` placeholder is prose, not a copy-pasteable call."""
        from precis.errors import NotFound

        h = _handler(store)
        with pytest.raises(NotFound) as excinfo:
            h.get(id="no-such-model-xyz")
        assert "search(kind='llm', q='no-such-model-xyz')" in str(excinfo.value)

    def test_get_provider_slug_points_at_accounts(self, store: Any) -> None:
        """gr581238 — ``id='openrouter'``/``'groq'`` are provider names, not
        model ids: the refusal says so and names the account read."""
        from precis.errors import NotFound

        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO llm_account (name, vendor, plan, adapter) "
                "VALUES ('acct-prov-test', 'vendorx-test', 'metered', 'api') "
                "ON CONFLICT (name) DO NOTHING"
            )
        try:
            h = _handler(store)
            with pytest.raises(NotFound, match="provider") as by_vendor:
                h.get(id="vendorx-test")
            assert "id='/accounts'" in str(by_vendor.value)
            with pytest.raises(NotFound, match="provider") as by_name:
                h.get(id="acct-prov-test")
            assert "id='/accounts'" in str(by_name.value)
        finally:
            with store.pool.connection() as conn:
                conn.execute("DELETE FROM llm_account WHERE name = 'acct-prov-test'")

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
            tier_floor="medium",
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
            tier_floor="frontier",
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
            text="Opus, also served via the loopback local transport.",
            offerings=[{"transport": "local", "endpoint": "http://127.0.0.1:4000"}],
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
            offerings=[{"transport": "local"}],
        )
        # proxy_models=None → we cannot assert absence, so no false alert.
        run_llm_reconcile_pass(store, models={}, proxy_models=None, force=True)
        assert not _open_drift_alerts(store)


def _gateway_model(mid: str, *, price: Any = None) -> dict[str, Any]:
    """One AI Gateway ``models[]`` entry; ``price`` overrides the whole field."""
    return {
        "id": mid,
        "name": mid.upper(),
        "members": [f"{mid}-v1"],
        "price": (
            {"input": "0", "output": "0", "cache_write": None, "cache_read": None}
            if price is None
            else price
        ),
    }


class _DevinGateway:
    """A recording stand-in for the AI Gateway: each request pops the next
    scripted ``(status, headers, json body)``; every request is kept."""

    def __init__(self) -> None:
        self.script: list[tuple[int, dict[str, str], Any]] = []
        self.requests: list[Any] = []

    def __call__(self, request: Any) -> Any:
        import httpx

        self.requests.append(request)
        status, headers, body = self.script.pop(0)
        return httpx.Response(status, headers=headers, json=body, request=request)


@pytest.fixture
def devin_gateway(monkeypatch: pytest.MonkeyPatch) -> Any:
    """``fetch_devin_entitlement`` against a scripted gateway: a token in the
    env, ``http_client`` over an ``httpx.MockTransport`` (it keeps its real
    header merge and ``follow_redirects=False``), no limiter and no sleeping."""
    import httpx

    from precis.utils.http import reset_circuit

    gw = _DevinGateway()
    monkeypatch.setenv("DEVIN_API_TOKEN", "tok-test")
    monkeypatch.setattr(
        "precis.utils.safe_fetch.pinning_transport",
        lambda **_kw: httpx.MockTransport(gw),
    )
    monkeypatch.setattr("precis.utils.rate_limit.require", lambda _p, **_kw: None)
    monkeypatch.setattr("precis.utils.rate_limit.park", lambda _p, _s: None)
    reset_circuit()
    yield gw
    reset_circuit()


class TestDevinEntitlement:
    """S9 (td568212): the pass's extension to the ``devin`` llm_account's
    entitlement. ``llm_account`` is NOT truncated between tests (it's a
    seeded ledger, not per-test data — see conftest's keep-list), so every
    test here restores the row's ``entitlement``/``promo_expiry`` to
    whatever it was before, or a sibling test / ``test_llm_accounts.py``
    would see this test's leftovers under random ordering.
    """

    @pytest.fixture(autouse=True)
    def _restore_devin_row(self, store: Any) -> Any:
        from psycopg.types.json import Jsonb

        with store.pool.connection() as conn:
            before = conn.execute(
                "SELECT entitlement, promo_expiry FROM llm_account WHERE name = 'devin'"
            ).fetchone()
        yield
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE llm_account SET entitlement = %s, promo_expiry = %s "
                "WHERE name = 'devin'",
                (Jsonb(before[0]), before[1]),
            )
            conn.commit()

    def test_first_fetch_seeds_models_with_no_stale_marker(self, store: Any) -> None:
        from precis.utils.llm.accounts import get_account
        from precis.workers.llm_reconcile import run_llm_reconcile_pass

        feed = [
            {
                "id": "swe-2-high",
                "name": "SWE-2 High",
                "members": ["swe-2-high-v1"],
                "price": {
                    "input": "0",
                    "output": "0",
                    "cache_write": "0",
                    "cache_read": "0",
                },
            }
        ]
        run_llm_reconcile_pass(store, models={}, devin_models=feed, force=True)
        a = get_account(store, "devin")
        assert a is not None
        entry = a.entitlement["models"]["swe-2-high"]
        assert entry["allowed"] is True
        assert "stale_since" not in entry
        # The fetch records where ``models`` came from under ``fetched_from``;
        # ``source`` is the row's curated provenance and is left alone.
        assert a.entitlement["fetched_from"] == "devin ai-gateway models API"

    def test_price_change_marks_the_model_stale(self, store: Any) -> None:
        from precis.utils.llm.accounts import get_account
        from precis.workers.llm_reconcile import run_llm_reconcile_pass

        def feed(price_in: str) -> list[dict[str, Any]]:
            return [
                {
                    "id": "m1",
                    "name": "M1",
                    "members": [],
                    "price": {
                        "input": price_in,
                        "output": "0.000002",
                        "cache_write": None,
                        "cache_read": None,
                    },
                }
            ]

        run_llm_reconcile_pass(
            store, models={}, devin_models=feed("0.000001"), force=True
        )
        res = run_llm_reconcile_pass(
            store, models={}, devin_models=feed("0.000003"), force=True
        )
        a = get_account(store, "devin")
        assert a is not None
        assert "stale_since" in a.entitlement["models"]["m1"]
        assert res.ok >= 1

    def test_unchanged_price_stays_unmarked(self, store: Any) -> None:
        from precis.utils.llm.accounts import get_account
        from precis.workers.llm_reconcile import run_llm_reconcile_pass

        feed = [
            {
                "id": "m1",
                "name": "M1",
                "members": [],
                "price": {
                    "input": "0.000001",
                    "output": "0.000002",
                    "cache_write": None,
                    "cache_read": None,
                },
            }
        ]
        run_llm_reconcile_pass(store, models={}, devin_models=feed, force=True)
        run_llm_reconcile_pass(store, models={}, devin_models=feed, force=True)
        a = get_account(store, "devin")
        assert a is not None
        assert "stale_since" not in a.entitlement["models"]["m1"]

    def test_model_dropped_from_feed_kept_as_not_allowed(self, store: Any) -> None:
        from precis.utils.llm.accounts import get_account
        from precis.workers.llm_reconcile import run_llm_reconcile_pass

        feed = [
            {
                "id": "m2",
                "name": "M2",
                "members": [],
                "price": {
                    "input": "0",
                    "output": "0",
                    "cache_write": None,
                    "cache_read": None,
                },
            }
        ]
        run_llm_reconcile_pass(store, models={}, devin_models=feed, force=True)
        run_llm_reconcile_pass(store, models={}, devin_models=[], force=True)
        a = get_account(store, "devin")
        assert a is not None
        assert a.entitlement["models"]["m2"]["allowed"] is False

    def test_promo_expiry_in_the_past_flags_promo_expired(self, store: Any) -> None:
        from precis.utils.llm.accounts import get_account
        from precis.workers.llm_reconcile import run_llm_reconcile_pass

        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE llm_account SET promo_expiry = '2020-01-01' WHERE name = 'devin'"
            )
            conn.commit()
        run_llm_reconcile_pass(store, models={}, devin_models=[], force=True)
        a = get_account(store, "devin")
        assert a is not None
        assert a.entitlement.get("promo_expired") is True

    def test_no_feed_leaves_entitlement_untouched(self, store: Any) -> None:
        from precis.utils.llm.accounts import get_account
        from precis.workers.llm_reconcile import run_llm_reconcile_pass

        before = get_account(store, "devin")
        assert before is not None
        run_llm_reconcile_pass(
            store, models={}, devin_models=None, _fetch=False, force=True
        )
        after = get_account(store, "devin")
        assert after is not None
        assert after.entitlement == before.entitlement

    def test_refresh_merges_into_the_stored_entitlement(self, store: Any) -> None:
        from psycopg.types.json import Jsonb

        from precis.utils.llm.accounts import get_account
        from precis.workers.llm_reconcile import run_llm_reconcile_pass

        owned = {
            "source": "curated provenance",
            "note": "curated prose",
            "owner_key": {"nested": [1, 2]},
        }
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE llm_account SET entitlement = %s WHERE name = 'devin'",
                (Jsonb(owned),),
            )
            conn.commit()
        run_llm_reconcile_pass(
            store, models={}, devin_models=[_gateway_model("m1")], force=True
        )
        a = get_account(store, "devin")
        assert a is not None
        for key, value in owned.items():
            assert a.entitlement[key] == value, key
        assert a.entitlement["models"]["m1"]["allowed"] is True
        assert a.entitlement["fetched_at"]
        assert a.entitlement["fetched_from"] == "devin ai-gateway models API"

    def test_migration_seeded_source_and_note_survive_the_first_refresh(
        self, store: Any
    ) -> None:
        from precis.utils.llm.accounts import get_account
        from precis.workers.llm_reconcile import run_llm_reconcile_pass

        before = get_account(store, "devin")
        assert before is not None
        # The premise: migration 0280 seeds both curated keys on the row.
        assert before.entitlement.get("source")
        assert before.entitlement.get("note")
        run_llm_reconcile_pass(
            store, models={}, devin_models=[_gateway_model("m1")], force=True
        )
        after = get_account(store, "devin")
        assert after is not None
        assert after.entitlement["source"] == before.entitlement["source"]
        assert after.entitlement["note"] == before.entitlement["note"]

    def test_promo_expired_flag_clears_once_the_expiry_is_extended(
        self, store: Any
    ) -> None:
        from precis.utils.llm.accounts import get_account
        from precis.workers.llm_reconcile import run_llm_reconcile_pass

        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE llm_account SET promo_expiry = '2020-01-01' WHERE name = 'devin'"
            )
            conn.commit()
        run_llm_reconcile_pass(store, models={}, devin_models=[], force=True)
        a = get_account(store, "devin")
        assert a is not None
        assert a.entitlement.get("promo_expired") is True
        # Merging into the old dict must not keep the stale flag alive.
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE llm_account SET promo_expiry = '2999-01-01' WHERE name = 'devin'"
            )
            conn.commit()
        run_llm_reconcile_pass(store, models={}, devin_models=[], force=True)
        a = get_account(store, "devin")
        assert a is not None
        assert "promo_expired" not in a.entitlement

    def test_unexpected_top_level_shape_leaves_entitlement_untouched(
        self,
        store: Any,
        devin_gateway: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from precis.utils.llm.accounts import get_account
        from precis.workers.llm_reconcile import run_llm_reconcile_pass

        run_llm_reconcile_pass(
            store, models={}, devin_models=[_gateway_model("m1")], force=True
        )
        before = get_account(store, "devin")
        assert before is not None
        assert before.entitlement["models"]["m1"]["allowed"] is True

        # The list now lives under another key. That used to read as ``[]`` —
        # "the account may call nothing" — and flip every model to not allowed.
        devin_gateway.script = [(200, {}, {"data": [_gateway_model("m1")]})]
        with caplog.at_level(logging.WARNING, logger="precis.workers.llm_reconcile"):
            run_llm_reconcile_pass(store, models={}, endpoints_by_key={}, force=True)
        assert len(devin_gateway.requests) == 1  # the fetch did run
        after = get_account(store, "devin")
        assert after is not None
        assert after.entitlement == before.entitlement
        shape_warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "no 'models' list" in r.getMessage()
        ]
        assert len(shape_warnings) == 1

    def test_non_object_price_skips_that_entry_and_the_pass_still_runs(
        self, store: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        from precis import llm_catalog
        from precis.utils.llm.accounts import get_account
        from precis.workers.llm_reconcile import (
            _norm_model_key,
            run_llm_reconcile_pass,
        )

        rid, _ = llm_catalog.upsert_card(
            store, model_id="claude-opus-4-8", text="Opus card."
        )
        feed = {
            _norm_model_key("claude-opus-4-8"): {
                "id": "anthropic/claude-opus-4.8",
                "context_length": 1_000_000,
            }
        }
        devin: list[Any] = [
            _gateway_model("str-price", price="free"),
            _gateway_model("list-price", price=["0", "1"]),
            _gateway_model("word-in-price", price={"input": "cheap"}),
            _gateway_model("nan-in-price", price={"input": float("nan")}),
            {"id": 7, "price": {}},
            "not an object",
            _gateway_model("good"),
            _gateway_model("bare-number", price=0),
        ]
        with caplog.at_level(logging.WARNING, logger="precis.workers.llm_reconcile"):
            res = run_llm_reconcile_pass(
                store,
                models=feed,
                endpoints_by_key={},
                devin_models=devin,
                force=True,
            )
        # The good entries landed; every bad one was skipped, by name, in the log.
        a = get_account(store, "devin")
        assert a is not None
        stored = a.entitlement["models"]
        assert {"good", "bare-number"} <= set(stored)
        for bad in ("str-price", "list-price", "word-in-price", "nan-in-price"):
            assert bad not in stored
            assert any(
                r.levelno == logging.WARNING and bad in r.getMessage()
                for r in caplog.records
            ), bad
        # ...and the legs after the devin one ran: card refresh + throttle stamp.
        ref = store.get_ref(kind="llm", id=rid)
        assert ref.meta["facts_openrouter"]["context_length"] == 1_000_000
        assert store.get_setting("llm_reconcile:last_run")
        assert res.ok >= 1

    def test_a_malformed_entry_keeps_its_stored_state(self, store: Any) -> None:
        from precis.utils.llm.accounts import get_account
        from precis.workers.llm_reconcile import run_llm_reconcile_pass

        feed = [_gateway_model("m1"), _gateway_model("m2")]
        run_llm_reconcile_pass(store, models={}, devin_models=feed, force=True)
        first = get_account(store, "devin")
        assert first is not None
        # m2 now arrives malformed: unknown, not "dropped from the account".
        feed = [_gateway_model("m1"), _gateway_model("m2", price="free")]
        run_llm_reconcile_pass(store, models={}, devin_models=feed, force=True)
        second = get_account(store, "devin")
        assert second is not None
        assert second.entitlement["models"]["m2"] == first.entitlement["models"]["m2"]
        assert second.entitlement["models"]["m2"]["allowed"] is True

    @pytest.mark.parametrize("failure", ["python", "sql"])
    def test_a_failure_inside_the_devin_leg_never_escapes_the_pass(
        self,
        store: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        failure: str,
    ) -> None:
        import psycopg

        from precis import llm_catalog
        from precis.utils.llm.accounts import get_account
        from precis.workers import llm_reconcile as lr

        rid, _ = llm_catalog.upsert_card(
            store, model_id="claude-opus-4-8", text="Opus card."
        )
        feed = {
            lr._norm_model_key("claude-opus-4-8"): {
                "id": "anthropic/claude-opus-4.8",
                "context_length": 123_000,
            }
        }

        def boom(*_a: Any, **_k: Any) -> Any:
            if failure == "python":
                raise RuntimeError("a shape the guards missed")
            # A value the jsonb UPDATE rejects (NaN is not valid JSON): the
            # statement itself fails, inside the pass-long transaction.
            return {"models": {}, "bad": float("nan")}, []

        monkeypatch.setattr(lr, "_devin_entitlement_from_models", boom)
        before = get_account(store, "devin")
        assert before is not None
        with caplog.at_level(logging.WARNING, logger=lr.log.name):
            res = lr.run_llm_reconcile_pass(
                store,
                models=feed,
                endpoints_by_key={},
                devin_models=[_gateway_model("m1")],
                force=True,
            )
        failed = [
            r
            for r in caplog.records
            if "devin entitlement refresh failed" in r.getMessage()
        ]
        assert len(failed) == 1
        # ...and the failure was the kind this case claims (not a stand-in).
        assert failed[0].exc_info is not None
        assert isinstance(
            failed[0].exc_info[1], psycopg.Error if failure == "sql" else RuntimeError
        )
        after = get_account(store, "devin")
        assert after is not None
        assert after.entitlement == before.entitlement  # untouched
        ref = store.get_ref(kind="llm", id=rid)
        assert ref.meta["facts_openrouter"]["context_length"] == 123_000
        assert store.get_setting(lr._STATE_KEY)
        assert res.ok >= 1


class TestDevinFetch:
    """``fetch_devin_entitlement`` against a scripted gateway (no DB)."""

    def test_returns_the_models_list_with_the_token(self, devin_gateway: Any) -> None:
        from precis.workers.llm_reconcile import fetch_devin_entitlement

        devin_gateway.script = [(200, {}, {"models": [_gateway_model("m1")]})]
        out = fetch_devin_entitlement()
        assert out is not None
        assert [m["id"] for m in out] == ["m1"]
        assert devin_gateway.requests[0].headers["authorization"] == "Bearer tok-test"

    def test_an_empty_models_list_is_a_real_answer_not_a_shape_error(
        self, devin_gateway: Any
    ) -> None:
        from precis.workers.llm_reconcile import fetch_devin_entitlement

        devin_gateway.script = [(200, {}, {"models": []})]
        assert fetch_devin_entitlement() == []

    def test_a_redirect_is_not_followed_and_never_carries_the_token_on(
        self, devin_gateway: Any
    ) -> None:
        from precis.workers.llm_reconcile import (
            DEVIN_AI_GATEWAY_MODELS_URL,
            fetch_devin_entitlement,
        )

        devin_gateway.script = [
            (302, {"Location": "https://redirect-target.example/models"}, {})
        ]
        assert fetch_devin_entitlement() is None
        # Exactly one request, to the fixed URL, with the token: a followed hop
        # would be a second request replaying the Authorization header.
        assert [str(r.url) for r in devin_gateway.requests] == [
            DEVIN_AI_GATEWAY_MODELS_URL
        ]
        assert devin_gateway.requests[0].headers["authorization"] == "Bearer tok-test"

    @pytest.mark.parametrize(
        "body",
        [
            {"data": [{"id": "m1"}]},
            {},
            {"models": None},
            {"models": "m1"},
            [],
            "models",
        ],
    )
    def test_an_unexpected_top_level_shape_is_none_with_one_warning(
        self, devin_gateway: Any, caplog: pytest.LogCaptureFixture, body: Any
    ) -> None:
        from precis.workers.llm_reconcile import fetch_devin_entitlement

        devin_gateway.script = [(200, {}, body)]
        with caplog.at_level(logging.WARNING, logger="precis.workers.llm_reconcile"):
            assert fetch_devin_entitlement() is None
        # Only this module's records: the rate limiter logs its own one-time
        # "runs unpaced" notice on the first call a process makes to a host.
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "precis.workers.llm_reconcile"
        ]
        assert len(warnings) == 1
        assert "no 'models' list" in warnings[0].getMessage()


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
        # the seed emits only the four capability tier_floor
        # values — the legacy location-coupled values (now retired from the
        # ``Tier`` enum itself) are gone from freshly-seeded cards. Checked as
        # raw strings since the legacy enum members no longer exist to import.
        canonical = {Tier.FRONTIER, Tier.BIG, Tier.MEDIUM, Tier.SMALL}
        assert {t.value for t in canonical} <= floors
        legacy = {"local-small", "local-big", "cloud-small", "cloud-mid", "cloud-super"}
        assert not (legacy & floors)

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
        assert {"frontier", "big", "medium"} <= floors
        # The fleet's BIG-tier local lane must declare the floor the op pins
        # demand (gr622036): a ``medium`` floor let the shadow decide reject
        # the gpt-oss-120b lane legacy runs on every claim.verify call.
        assert by_id["openai/gpt-oss-120b"]["tier_floor"] == "big"
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

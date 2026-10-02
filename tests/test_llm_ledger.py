"""Tests for the llm-catalog ledger — review log + tote + observed evals
(llm-catalog slice 3, ``llm-catalog`` (git-only)).

Covers: the append-only review log (WORM, typed, provenance) via `put(id=…,
entry=…)` and `view='reviews'`; the `llm_call_log` tote rollup + `view='tote'`;
observed-axis derivation from telemetry (the operational reasoning signal); the
`measured-eval` record surface; and the provenance trust ordering. Real PG.
"""

from __future__ import annotations

from typing import Any

import pytest


def _handler(store: Any) -> Any:
    from precis.dispatch import Hub
    from precis.handlers.llm import LlmHandler

    return LlmHandler(hub=Hub(store=store))


def _log_call(
    store: Any,
    model: str,
    *,
    errored: bool = False,
    cost: float = 0.001,
    duration: int = 1000,
    turns: int = 2,
    source: str = "test",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO llm_call_log "
            "(source, tier, transport, model, tools_needed, cost_usd, "
            " turns_used, duration_ms, errored, input_tokens, output_tokens) "
            "VALUES (%s, 'frontier', 'claude_agent', %s, false, %s, %s, %s, %s, "
            " %s, %s)",
            (
                source,
                model,
                cost,
                turns,
                duration,
                errored,
                input_tokens,
                output_tokens,
            ),
        )


class TestReviewLog:
    def test_put_appends_typed_review(self, store: Any) -> None:
        from precis import llm_catalog

        rid, _ = llm_catalog.upsert_card(store, model_id="rev-model-1", text="A model.")
        h = _handler(store)
        resp = h.put(
            id=rid,
            text="opus·medium was excellent at SQL-migration reasoning",
            entry="agent-review",
            by="agent",
        )
        assert "review" in resp.body
        # WORM chunk landed with the right kind + typed meta
        reviews = llm_catalog.list_reviews(store, rid)
        assert len(reviews) == 1
        assert (reviews[0].meta or {})["entry_type"] == "agent-review"

    def test_reviews_view_renders(self, store: Any) -> None:
        from precis import llm_catalog

        rid, _ = llm_catalog.upsert_card(store, model_id="rev-model-2", text="A model.")
        llm_catalog.append_review(
            store,
            rid,
            text="SWE-bench-verified 0.62",
            review_type="published-benchmark",
            by="human",
            provenance="vendor",
        )
        h = _handler(store)
        body = h.get(id="rev-model-2", view="reviews").body
        assert "published-benchmark" in body and "SWE-bench" in body

    def test_unknown_review_type_rejected(self, store: Any) -> None:
        from precis import llm_catalog
        from precis.errors import BadInput

        rid, _ = llm_catalog.upsert_card(store, model_id="rev-model-3", text="A model.")
        h = _handler(store)
        with pytest.raises(BadInput):
            h.put(id=rid, text="x", entry="vibes")

    def test_append_by_model_slug(self, store: Any) -> None:
        from precis import llm_catalog

        llm_catalog.upsert_card(store, model_id="rev/Slug-4", text="A model.")
        h = _handler(store)
        # slug with slash/case resolves for the append path too
        h.put(id="rev/Slug-4", text="fast", entry="observed-telemetry")
        body = h.get(id="rev/Slug-4", view="reviews").body
        assert "observed-telemetry" in body


class TestTote:
    def test_rollup(self, store: Any) -> None:
        from precis.llm_catalog import llm_tote

        m = "tote-model-A"
        for _ in range(9):
            _log_call(store, m, errored=False, cost=0.01, duration=1000)
        _log_call(store, m, errored=True, cost=0.01, duration=3000)
        tote = llm_tote(store, m)
        assert tote.calls == 10
        assert abs(tote.cost_usd - 0.10) < 1e-6
        assert tote.error_rate == pytest.approx(0.1)
        assert tote.avg_turns == pytest.approx(2.0)

    def test_tote_view(self, store: Any) -> None:
        from precis import llm_catalog

        m = "tote-model-B"
        llm_catalog.upsert_card(store, model_id=m, text="A model.")
        _log_call(store, m, cost=0.02)
        h = _handler(store)
        body = h.get(id=m, view="tote").body
        assert "tote" in body and "calls: 1" in body

    def test_tote_empty(self, store: Any) -> None:
        from precis.llm_catalog import llm_tote

        assert llm_tote(store, "never-called-model").calls == 0

    def test_rollup_sums_tokens(self, store: Any) -> None:
        # Migration 0122: token sums roll up the same way cost_usd does —
        # COALESCE(sum, 0) over the nullable columns, no false gap.
        from precis.llm_catalog import llm_tote

        m = "tote-model-tokens"
        _log_call(store, m, input_tokens=100, output_tokens=10)
        _log_call(store, m, input_tokens=50, output_tokens=5)
        _log_call(store, m)  # no token telemetry at all
        tote = llm_tote(store, m)
        assert tote.calls == 3
        assert tote.input_tokens == 150
        assert tote.output_tokens == 15


class TestObservedAxes:
    def test_derives_and_records(self, store: Any) -> None:
        from precis import llm_catalog

        m = "obs-model-A"
        llm_catalog.upsert_card(store, model_id=m, text="A model.")
        # 24 success, 1 error → 96% success → ordinal 4
        for _ in range(24):
            _log_call(store, m, errored=False)
        _log_call(store, m, errored=True)
        axes = llm_catalog.record_observed_axes(store, m)
        assert axes == {"reasoning-convergence": 4}
        ref = store.find_ref_by_meta(kind="llm", key="model_id", value=m)
        cap = ref.meta["capability"]["reasoning-convergence"]
        assert cap["score"] == 4 and cap["provenance"] == "observed-telemetry"
        # an observed-telemetry review was logged
        types = {
            (b.meta or {}).get("entry_type")
            for b in llm_catalog.list_reviews(store, ref.id)
        }
        assert "observed-telemetry" in types

    def test_below_floor_is_noop(self, store: Any) -> None:
        from precis import llm_catalog

        m = "obs-model-B"
        llm_catalog.upsert_card(store, model_id=m, text="A model.")
        for _ in range(5):
            _log_call(store, m)
        assert llm_catalog.derive_observed_axes(store, m) == {}


class TestRecordEval:
    def test_measured_eval_sets_axis_and_review(self, store: Any) -> None:
        from precis import llm_catalog

        m = "eval-model-A"
        llm_catalog.upsert_card(store, model_id=m, text="A model.")
        llm_catalog.record_eval(
            store,
            m,
            axis="code",
            ordinal=5,
            by="agent",
            note="perfect on fix_gripe gold",
        )
        ref = store.find_ref_by_meta(kind="llm", key="model_id", value=m)
        cap = ref.meta["capability"]["code"]
        assert cap["score"] == 5 and cap["provenance"] == "measured-eval"
        reviews = llm_catalog.list_reviews(store, ref.id)
        assert reviews[-1].meta["axis"] == "code" and reviews[-1].meta["ordinal"] == 5

    def test_rejects_bad_axis_and_range(self, store: Any) -> None:
        from precis import llm_catalog
        from precis.errors import BadInput

        m = "eval-model-B"
        llm_catalog.upsert_card(store, model_id=m, text="A model.")
        with pytest.raises(BadInput):
            llm_catalog.record_eval(store, m, axis="vibes", ordinal=3)
        with pytest.raises(BadInput):
            llm_catalog.record_eval(store, m, axis="code", ordinal=9)


class TestProvenanceTrust:
    def test_observed_beats_measured_beats_published(self) -> None:
        from precis.llm_catalog import PROVENANCE_TRUST

        assert (
            PROVENANCE_TRUST["observed-telemetry"]
            > PROVENANCE_TRUST["measured-eval"]
            > PROVENANCE_TRUST["published-benchmark"]
        )


class TestPlacementShare:
    """`llm_call_log` local-vs-cloud report: landed (`placement`) and the
    routed-vs-landed split (`placement_routed`, migration 0179)."""

    @staticmethod
    def _log(
        store: Any,
        source: str,
        tier: str,
        placement: str | None,
        routed: str | None,
        cost: float = 0.01,
    ) -> None:
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO llm_call_log (source, tier, transport, model, "
                "cost_usd, errored, placement, placement_routed) "
                "VALUES (%s, %s, 'openai_tools', 'm', %s, false, %s, %s)",
                (source, tier, cost, placement, routed),
            )

    def _seed(self, store: Any, source: str) -> None:
        for _ in range(3):  # routed local, kept local
            self._log(store, source, "small", "local", "local", cost=0.5)
        self._log(store, source, "small", "cloud", "local")  # fell to cloud
        self._log(store, source, "big", "cloud", "cloud", cost=0.25)
        self._log(store, source, "big", None, None, cost=0.25)  # pre-0179 row

    def test_by_tier_counts(self, store: Any) -> None:
        from uuid import uuid4

        from precis.llm_catalog import placement_share

        src = f"placement-{uuid4().hex[:8]}"
        self._seed(store, src)
        rows = {r.key: r for r in placement_share(store, by="tier", source=src)}
        small, big = rows["small"], rows["big"]
        assert (small.calls, small.landed_local, small.routed_known) == (4, 3, 4)
        assert (small.routed_local, small.routed_local_kept) == (4, 3)
        assert small.billed_usd == pytest.approx(0.01)  # local rows are priced
        assert (big.calls, big.landed_local, big.routed_known) == (2, 0, 1)
        # NULL placement counts as billed (the caps' fail-closed rule).
        assert big.billed_usd == pytest.approx(0.5)
        assert list(rows) == ["small", "big"]  # busiest first

    def test_by_day_and_bad_group(self, store: Any) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        from precis.llm_catalog import placement_share

        src = f"placement-{uuid4().hex[:8]}"
        self._seed(store, src)
        rows = placement_share(store, by="day", source=src)
        assert len(rows) == 1 and rows[0].calls == 6
        assert rows[0].key == datetime.now(UTC).date().isoformat()
        with pytest.raises(ValueError):
            placement_share(store, by="model")

    def test_view_renders(self, store: Any) -> None:
        from uuid import uuid4

        src = f"placement-{uuid4().hex[:8]}"
        self._seed(store, src)
        body = _handler(store)._render_placement(source=src)
        assert "landed local: 3/6 (50.0%)" in body
        assert "routed recorded on 5/6" in body
        assert "## by tier" in body and "## by day (UTC)" in body

    def test_view_empty_and_path_dispatch(self, store: Any) -> None:
        h = _handler(store)
        assert "no llm_call_log rows" in h._render_placement(source="never-logged")
        # The path view is reachable through get(); content depends on the
        # shared DB, so only the header is pinned.
        assert h.get(id="/placement").body.startswith("# llm placement")

"""Tests for window admission — the `admit()` guardrail (llm-catalog slice 2,
docs/proposals/llm-catalog.md).

Covers: the pure fit-check (boundaries, headroom, unknown-window degrade), the
catalog window lookup precedence, the router `dispatch` hook (ships dark on an
empty catalog; refuses an oversized pairing on a fixed-model path with the
numbers), the standalone context-assembly check, and the deduped oversize alert.
Runs against real PG (the `store` fixture).
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def bound_store(store: Any) -> Any:
    """Bind the process store the admit hook reads, with a clean catalog cache."""
    from precis.budget import meter
    from precis.utils.llm import admit

    meter.bind_store(store)
    admit.reset_cache()
    yield store
    meter.bind_store(None)
    admit.reset_cache()


class TestPureAdmit:
    def test_fits_within_window_and_headroom(self) -> None:
        from precis.utils.llm.admit import admit

        # 100 tokens ×1.2 = 120 ≤ 200 → fits, no reason.
        a = admit(100, 200)
        assert a.fits is True and a.reason is None

    def test_refuses_with_numbers(self) -> None:
        from precis.utils.llm.admit import admit

        a = admit(1000, 500)
        assert a.fits is False
        assert a.reason is not None
        # the reason carries est tokens, the ×headroom product, and the limit
        assert "1,000" in a.reason and "500" in a.reason

    def test_headroom_is_the_margin(self) -> None:
        from precis.utils.llm.admit import admit

        # Exactly at the window fails once headroom is applied…
        assert admit(1000, 1000, headroom=0.2).fits is False
        # …and passes with zero headroom.
        assert admit(1000, 1000, headroom=0.0).fits is True

    def test_unknown_window_admits(self) -> None:
        from precis.utils.llm.admit import admit

        # limit=None (window unknown) → the catalog can't refuse what it doesn't
        # know; the guardrail degrades to allow.
        assert admit(10_000_000, None).fits is True

    def test_estimate_tokens(self) -> None:
        from precis.utils.llm.admit import estimate_tokens

        assert estimate_tokens(400) == 100


class TestWindowFor:
    def test_precedence(self) -> None:
        from precis.utils.llm.admit import window_for

        meta = {
            "offerings": [
                {"transport": "litellm", "max_input": 8_000},
                {"transport": "claude_agent", "max_input": 200_000},
            ],
            "facts_openrouter": {"context_length": 1_000_000},
        }
        # transport-matched offering wins
        assert window_for(meta, "litellm") == 8_000
        assert window_for(meta, "claude_agent") == 200_000
        # unmatched transport → first offering with a max_input
        assert window_for(meta, "openai_tools") == 8_000
        # no offerings → the reconciled feed window
        assert window_for({"facts_openrouter": {"context_length": 128_000}}) == 128_000
        # nothing known
        assert window_for({}) is None


class TestCheckDispatch:
    def test_empty_catalog_is_noop(self, bound_store: Any) -> None:
        from precis.utils.llm.admit import check_dispatch
        from precis.utils.llm.router import LlmRequest, Transport

        req = LlmRequest(tier=_local_tier(), prompt="x" * 100_000, model="ghost")
        # No card for 'ghost' → None (byte-identical to today).
        assert check_dispatch(req, model="ghost", transport=Transport.LITELLM) is None

    def test_refuses_oversized_on_reconciled_window(self, bound_store: Any) -> None:
        from precis import llm_catalog
        from precis.utils.llm import admit
        from precis.utils.llm.admit import check_dispatch
        from precis.utils.llm.router import LlmRequest, Transport

        llm_catalog.upsert_card(
            store_of(bound_store),
            model_id="small-window-model",
            text="A tiny local model.",
            offerings=[{"transport": "litellm", "max_input": 100}],
        )
        admit.reset_cache()
        req = LlmRequest(
            tier=_local_tier(), prompt="x" * 1000, model="small-window-model"
        )
        reason = check_dispatch(
            req, model="small-window-model", transport=Transport.LITELLM
        )
        assert reason is not None and "max input" in reason

    def test_admits_small_request(self, bound_store: Any) -> None:
        from precis import llm_catalog
        from precis.utils.llm import admit
        from precis.utils.llm.admit import check_dispatch
        from precis.utils.llm.router import LlmRequest, Transport

        llm_catalog.upsert_card(
            store_of(bound_store),
            model_id="wide-model",
            text="A wide model.",
            offerings=[{"transport": "litellm", "max_input": 200_000}],
        )
        admit.reset_cache()
        req = LlmRequest(tier=_local_tier(), prompt="hello", model="wide-model")
        assert (
            check_dispatch(req, model="wide-model", transport=Transport.LITELLM) is None
        )


class TestDispatchIntegration:
    def test_dispatch_refuses_oversized_fixed_model(self, bound_store: Any) -> None:
        from precis import llm_catalog
        from precis.utils.llm import admit
        from precis.utils.llm.router import LlmRequest, dispatch

        llm_catalog.upsert_card(
            store_of(bound_store),
            model_id="tiny-test-model",
            text="Tiny.",
            offerings=[{"transport": "litellm", "max_input": 100}],
        )
        admit.reset_cache()
        # A fixed-model (pinned) call with a durably oversized prompt: dispatch
        # returns the refusal as a normalized error, WITHOUT running a provider.
        req = LlmRequest(
            tier=_local_tier(),
            model="tiny-test-model",
            prompt="x" * 4000,  # ~1000 tokens ≫ 100 window
            source="test:admit",
        )
        res = dispatch(req)
        assert res.error is not None and "max input" in res.error
        assert res.text == ""


class TestOversizeAlert:
    def test_dedup(self, bound_store: Any) -> None:
        from precis.utils.llm.admit import Admission, raise_oversize_alert

        adm = Admission(
            fits=False, est_tokens=1000, limit=100, headroom=0.2, reason="r"
        )
        s = store_of(bound_store)
        raise_oversize_alert(s, model="m", source="pass:x", adm=adm)
        raise_oversize_alert(s, model="m", source="pass:x", adm=adm)
        with s.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT r.meta->>'seen_count'
                  FROM refs r
                  JOIN ref_tags rt ON rt.ref_id = r.ref_id
                  JOIN tags t ON t.tag_id = rt.tag_id
                 WHERE r.kind = 'alert'
                   AND r.meta->>'alert_source' = 'admit:oversize'
                   AND t.namespace = 'OPEN' AND t.value = 'alert-state:open'
                """
            ).fetchall()
        # exactly one open alert, bumped to seen_count 2
        assert len(rows) == 1 and rows[0][0] == "2"


def _local_tier() -> Any:
    from precis.utils.llm.router import Tier

    return Tier.LOCAL_SMALL


def store_of(bound: Any) -> Any:
    return bound

"""Scenario tests for ``precis.workers.hub_tagline``.

Real DB ``store`` fixture (mirrors ``tests/test_taproot_reword.py``): a
hub is a real ``mint_hub``-minted ``TAPROOT:claim``/``STATUS:canonical``
finding. The LLM hook (``propose_fn``) is always a local stub/fake — never
networked — so the claim-and-lease query and the in-code validation belt
run for real while the paid call itself is a deterministic Python
function.
"""

from __future__ import annotations

from typing import Any

from precis.store import Store
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub
from precis.workers.hub_tagline import (
    _claim_candidates,
    _validate_tagline,
    run_hub_tagline_pass,
)

_SENTENCE = (
    "Nanoindentation measurements show graphene has a tensile strength of 130 GPa."
)
_SENTENCE_2 = "Transport measurements show silicon carbide has a bandgap of 3.2 eV."


def _mint(store: Any, sentence: str, **kwargs: Any) -> int:
    return mint_hub(store, CanonicalClaim(sentence=sentence, scope={}), **kwargs)


def _tagline(store: Store, hub: int) -> tuple[str | None, str | None]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->>'tagline', meta->>'tagline_by' FROM refs WHERE ref_id = %s",
            (hub,),
        ).fetchone()
    assert row is not None
    return row[0], row[1]


def _failures(store: Store, hub: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT COALESCE((meta->>'tagline_failures')::int, 0) "
            "FROM refs WHERE ref_id = %s",
            (hub,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _stub(tagline: str | None) -> Any:
    def fn(sentence: str, scope: dict[str, Any]) -> dict[str, Any] | None:
        return {"tagline": tagline}

    return fn


def _never(sentence: str, scope: dict[str, Any]) -> dict[str, Any] | None:
    raise AssertionError("the LLM must not be called for an excluded hub")


# ── _validate_tagline (pure) ────────────────────────────────────────────


class TestValidateTagline:
    def test_accepts_a_clean_short_tagline(self) -> None:
        assert _validate_tagline("Graphene is FET", _SENTENCE) == "Graphene is FET"

    def test_strips_surrounding_quotes_and_trailing_period(self) -> None:
        assert _validate_tagline('"Graphene is FET."', _SENTENCE) == "Graphene is FET"

    def test_strips_surrounding_backticks_and_trailing_colon(self) -> None:
        assert _validate_tagline("`Graphene is FET:`", _SENTENCE) == "Graphene is FET"

    def test_rejects_non_string(self) -> None:
        assert _validate_tagline(None, _SENTENCE) is None
        assert _validate_tagline(123, _SENTENCE) is None

    def test_rejects_empty_or_whitespace_only(self) -> None:
        assert _validate_tagline("", _SENTENCE) is None
        assert _validate_tagline("   ", _SENTENCE) is None
        assert _validate_tagline('"."', _SENTENCE) is None

    def test_rejects_multi_line(self) -> None:
        assert _validate_tagline("Graphene is FET\nExtra line", _SENTENCE) is None

    def test_rejects_too_many_words(self) -> None:
        long = "This is a way too long tagline with far too many words"
        assert len(long.split()) > 8
        assert _validate_tagline(long, _SENTENCE) is None

    def test_rejects_over_long_chars(self) -> None:
        padded = "A" * 65
        assert _validate_tagline(padded, _SENTENCE) is None

    def test_rejects_case_insensitive_equal_to_sentence(self) -> None:
        assert _validate_tagline(_SENTENCE.upper(), _SENTENCE) is None

    def test_rejects_verbatim_prefix_of_sentence(self) -> None:
        prefix = _SENTENCE[:20]
        assert _validate_tagline(prefix, _SENTENCE) is None

    def test_accepts_a_compression_that_is_not_a_prefix(self) -> None:
        # Shares words with the sentence but is not a literal prefix of it.
        assert _validate_tagline("Graphene tensile strength", _SENTENCE) == (
            "Graphene tensile strength"
        )


# ── cohort / claim-and-lease ─────────────────────────────────────────────


class TestClaimCandidates:
    def test_claims_a_missing_tagline_hub(self, store: Store) -> None:
        hub = _mint(store, _SENTENCE)
        claimed = _claim_candidates(store, limit=10)
        assert [c[0] for c in claimed] == [hub]
        assert claimed[0][1] == _SENTENCE

    def test_skips_hub_with_tagline_by_human(self, store: Store) -> None:
        hub = _mint(store, _SENTENCE)
        with store.pool.connection() as conn:
            conn.execute(
                'UPDATE refs SET meta = meta || \'{"tagline_by": "human"}\'::jsonb '
                "WHERE ref_id = %s",
                (hub,),
            )
            conn.commit()
        assert _claim_candidates(store, limit=10) == []

    def test_skips_hub_already_tagged(self, store: Store) -> None:
        untagged = _mint(store, _SENTENCE)
        tagged = _mint(store, _SENTENCE_2)
        with store.pool.connection() as conn:
            conn.execute(
                'UPDATE refs SET meta = meta || \'{"tagline": "Graphene is strong"}\''
                "::jsonb WHERE ref_id = %s",
                (tagged,),
            )
            conn.commit()
        claimed = _claim_candidates(store, limit=10)
        assert [c[0] for c in claimed] == [untagged]

    def test_skips_hub_past_failure_cap(self, store: Store) -> None:
        hub = _mint(store, _SENTENCE)
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || '{\"tagline_failures\": 3}'::jsonb "
                "WHERE ref_id = %s",
                (hub,),
            )
            conn.commit()
        assert _claim_candidates(store, limit=10) == []

    def test_second_claim_within_ttl_returns_nothing(self, store: Store) -> None:
        _mint(store, _SENTENCE)
        first = _claim_candidates(store, limit=10)
        assert len(first) == 1
        # A racing/second call within the lease TTL must not double-claim.
        second = _claim_candidates(store, limit=10)
        assert second == []

    def test_limit_caps_the_claim(self, store: Store) -> None:
        first = _mint(store, _SENTENCE)
        _mint(store, _SENTENCE_2)
        claimed = _claim_candidates(store, limit=1)
        assert [c[0] for c in claimed] == [first]


# ── run_hub_tagline_pass ──────────────────────────────────────────────────


class TestRunHubTaglinePass:
    def test_fills_missing_tagline(self, store: Store) -> None:
        hub = _mint(store, _SENTENCE)

        result = run_hub_tagline_pass(store, propose_fn=_stub("Graphene is strong"))

        assert result == {"claimed": 1, "ok": 1, "failed": 0}
        assert _tagline(store, hub) == ("Graphene is strong", "llm")

    def test_skips_tagline_by_human(self, store: Store) -> None:
        hub = _mint(store, _SENTENCE)
        with store.pool.connection() as conn:
            conn.execute(
                'UPDATE refs SET meta = meta || \'{"tagline_by": "human"}\'::jsonb '
                "WHERE ref_id = %s",
                (hub,),
            )
            conn.commit()

        result = run_hub_tagline_pass(store, propose_fn=_never)

        assert result == {"claimed": 0, "ok": 0, "failed": 0}
        assert _tagline(store, hub) == (None, "human")

    def test_skips_already_tagged_hub(self, store: Store) -> None:
        hub = _mint(store, _SENTENCE)
        with store.pool.connection() as conn:
            conn.execute(
                'UPDATE refs SET meta = meta || \'{"tagline": "Existing handle"}\''
                "::jsonb WHERE ref_id = %s",
                (hub,),
            )
            conn.commit()

        result = run_hub_tagline_pass(store, propose_fn=_never)

        assert result == {"claimed": 0, "ok": 0, "failed": 0}
        assert _tagline(store, hub) == ("Existing handle", None)

    def test_too_many_words_is_rejected_failures_bumped_no_write(
        self, store: Store
    ) -> None:
        hub = _mint(store, _SENTENCE)
        too_long = "This tagline has way too many words to ever be pithy at all"

        result = run_hub_tagline_pass(store, propose_fn=_stub(too_long))

        assert result == {"claimed": 1, "ok": 0, "failed": 1}
        assert _tagline(store, hub) == (None, None)
        assert _failures(store, hub) == 1

    def test_verbatim_prefix_is_rejected_failures_bumped_no_write(
        self, store: Store
    ) -> None:
        hub = _mint(store, _SENTENCE)
        prefix = _SENTENCE[:25]

        result = run_hub_tagline_pass(store, propose_fn=_stub(prefix))

        assert result == {"claimed": 1, "ok": 0, "failed": 1}
        assert _tagline(store, hub) == (None, None)
        assert _failures(store, hub) == 1

    def test_llm_failure_bumps_failures_no_write_no_crash(self, store: Store) -> None:
        hub = _mint(store, _SENTENCE)

        def down(sentence: str, scope: dict[str, Any]) -> dict[str, Any] | None:
            return None

        result = run_hub_tagline_pass(store, propose_fn=down)

        assert result == {"claimed": 1, "ok": 0, "failed": 1}
        assert _tagline(store, hub) == (None, None)
        assert _failures(store, hub) == 1

    def test_failure_cap_stops_retries(self, store: Store) -> None:
        hub = _mint(store, _SENTENCE)
        calls: list[int] = []

        def always_bad(sentence: str, scope: dict[str, Any]) -> dict[str, Any] | None:
            calls.append(1)
            return {"tagline": "way too many words to ever pass this belt check"}

        for _ in range(3):
            run_hub_tagline_pass(store, propose_fn=always_bad)

        assert len(calls) == 3
        assert _failures(store, hub) == 3

        # The cap is now hit -- a fourth pass must not call the LLM again.
        run_hub_tagline_pass(store, propose_fn=_never)
        assert len(calls) == 3

    def test_no_candidates_is_a_silent_no_op(self, store: Store) -> None:
        result = run_hub_tagline_pass(store, propose_fn=_never)
        assert result == {"claimed": 0, "ok": 0, "failed": 0}

    def test_limit_caps_the_pass(self, store: Store) -> None:
        first = _mint(store, _SENTENCE)
        _mint(store, _SENTENCE_2)

        result = run_hub_tagline_pass(
            store, limit=1, propose_fn=_stub("Graphene is strong")
        )

        assert result == {"claimed": 1, "ok": 1, "failed": 0}
        assert _tagline(store, first) == ("Graphene is strong", "llm")

    def test_default_propose_fn_is_the_llm_hook(self) -> None:
        # The pass's default seam is the module-level BIG-tier hook (the
        # monkeypatch target); a rename would silently orphan the dispatch
        # wiring in cli/worker.py.
        from precis.workers.hub_tagline import propose_tagline

        assert callable(propose_tagline)


# ── propose_tagline dispatch shape (monkeypatched dispatch, no network) ──


class TestProposeTagline:
    def test_dispatches_a_big_tier_request(self, monkeypatch: Any) -> None:
        from precis.utils.llm.router import LlmResult, Tier
        from precis.workers import hub_tagline

        captured: dict[str, Any] = {}

        def fake_dispatch(req: Any) -> LlmResult:
            captured["req"] = req
            return LlmResult(
                text='{"tagline": "Graphene is strong"}',
                cost_usd=0.0001,
                turns_used=None,
                model="fake-model",
                tier=req.tier,
                data={"tagline": "Graphene is strong"},
            )

        monkeypatch.setattr(hub_tagline, "route", fake_dispatch)

        result = hub_tagline.propose_tagline(_SENTENCE, {})

        assert result == {"tagline": "Graphene is strong"}
        assert captured["req"].tier == Tier.BIG
        assert captured["req"].source == "hub_tagline"
        assert _SENTENCE in captured["req"].prompt

    def test_dispatch_error_returns_none(self, monkeypatch: Any) -> None:
        from precis.utils.llm.router import LlmResult
        from precis.workers import hub_tagline

        def fake_dispatch(req: Any) -> LlmResult:
            return LlmResult(
                text="",
                cost_usd=None,
                turns_used=None,
                model="fake-model",
                tier=req.tier,
                error="down",
            )

        monkeypatch.setattr(hub_tagline, "route", fake_dispatch)

        assert hub_tagline.propose_tagline(_SENTENCE, {}) is None

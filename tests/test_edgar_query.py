"""Tests for the tag→FTS-param lift used by the ``edgar`` kind."""

from __future__ import annotations

import pytest

from precis.errors import BadInput
from precis.handlers._edgar_query import build_fts_params


class _FakeResolver:
    """Maps a couple of tickers to CIKs; unknown → None."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def resolve_ticker(self, ticker: str) -> str | None:
        return self._mapping.get(ticker.lower())


class TestFreeText:
    def test_q_passthrough(self) -> None:
        params = build_fts_params(q="climate risk disclosure", tags=None)
        assert params == {"q": "climate risk disclosure"}

    def test_q_stripped(self) -> None:
        params = build_fts_params(q="  going concern  ", tags=None)
        assert params["q"] == "going concern"


class TestFormLift:
    def test_single_form_uppercased(self) -> None:
        params = build_fts_params(q="risk", tags=["form:10-k"])
        assert params["forms"] == "10-K"

    def test_multiple_forms_comma_joined(self) -> None:
        params = build_fts_params(q=None, tags=["form:10-k", "form:8-k"])
        assert params["forms"] == "10-K,8-K"

    def test_duplicate_forms_deduped(self) -> None:
        params = build_fts_params(q=None, tags=["form:10-k", "form:10-k"])
        assert params["forms"] == "10-K"

    def test_complex_form_code(self) -> None:
        params = build_fts_params(q=None, tags=["form:defm14a"])
        assert params["forms"] == "DEFM14A"


class TestCikLift:
    def test_cik_zero_padded(self) -> None:
        params = build_fts_params(q=None, tags=["cik:320193"])
        assert params["ciks"] == "0000320193"

    def test_cik_already_padded(self) -> None:
        params = build_fts_params(q=None, tags=["cik:0000320193"])
        assert params["ciks"] == "0000320193"

    def test_multiple_ciks(self) -> None:
        params = build_fts_params(q=None, tags=["cik:320193", "cik:1045810"])
        assert params["ciks"] == "0000320193,0001045810"


class TestTickerLift:
    def test_ticker_resolves_to_cik(self) -> None:
        resolver = _FakeResolver({"aapl": "320193"})
        params = build_fts_params(q=None, tags=["ticker:aapl"], resolver=resolver)
        assert params["ciks"] == "0000320193"

    def test_unknown_ticker_skipped(self) -> None:
        resolver = _FakeResolver({})
        with pytest.raises(BadInput):
            build_fts_params(q=None, tags=["ticker:zzzz"], resolver=resolver)

    def test_ticker_without_resolver_skipped(self) -> None:
        with pytest.raises(BadInput):
            build_fts_params(q=None, tags=["ticker:aapl"], resolver=None)


class TestOpenPrefixesSkipped:
    def test_topic_narrows_local_only(self) -> None:
        # topic: has no FTS equivalent → skipped; q= still lifts.
        params = build_fts_params(q="revenue", tags=["topic:semiconductors"])
        assert params == {"q": "revenue"}

    def test_local_only_tag_with_no_q_raises(self) -> None:
        with pytest.raises(BadInput, match="q= or an FTS-liftable tag"):
            build_fts_params(q=None, tags=["topic:semiconductors"])


class TestEmpty:
    def test_no_q_no_tags_raises(self) -> None:
        with pytest.raises(BadInput):
            build_fts_params(q=None, tags=None)

    def test_blank_q_no_tags_raises(self) -> None:
        with pytest.raises(BadInput):
            build_fts_params(q="   ", tags=[])


class TestCombined:
    def test_q_form_and_cik(self) -> None:
        params = build_fts_params(q="cyber incident", tags=["form:8-k", "cik:320193"])
        assert params == {
            "q": "cyber incident",
            "forms": "8-K",
            "ciks": "0000320193",
        }

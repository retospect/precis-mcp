"""Tests for the EDGAR client shim (token bucket, fake, ticker map)."""

from __future__ import annotations

import json
import os
import time

import pytest

from precis.handlers._edgar_client import (
    EdgarNotFound,
    EdgarRateLimited,
    FakeEdgarClient,
    TokenBucket,
    parse_company_tickers,
)


class TestTokenBucket:
    def test_immediate_when_capacity_available(self) -> None:
        bucket = TokenBucket(rate=10.0, capacity=5.0)
        start = time.monotonic()
        for _ in range(5):
            bucket.acquire()
        assert time.monotonic() - start < 0.05

    def test_throttles_when_drained(self) -> None:
        # capacity 1, rate 20/s → the 2nd acquire waits ~1/20 = 50ms.
        bucket = TokenBucket(rate=20.0, capacity=1.0)
        bucket.acquire()
        start = time.monotonic()
        bucket.acquire()
        elapsed = time.monotonic() - start
        assert 0.02 < elapsed < 0.5


class TestParseCompanyTickers:
    def test_basic(self) -> None:
        raw = json.dumps(
            {
                "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
                "1": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA"},
            }
        ).encode()
        m = parse_company_tickers(raw)
        assert m["aapl"] == "320193"
        assert m["nvda"] == "1045810"

    def test_malformed_rows_skipped(self) -> None:
        raw = json.dumps(
            {
                "0": {"cik_str": 320193, "ticker": "AAPL"},
                "1": {"ticker": "NOCIK"},
                "2": "garbage",
            }
        ).encode()
        m = parse_company_tickers(raw)
        assert m == {"aapl": "320193"}

    def test_invalid_json(self) -> None:
        assert parse_company_tickers(b"not json") == {}


class TestFakeClient:
    def test_submissions_lookup(self) -> None:
        client = FakeEdgarClient(submissions={"320193": b'{"cik":"320193"}'})
        assert client.submissions("0000320193") == b'{"cik":"320193"}'
        assert ("submissions", "320193") in client.calls

    def test_document_lookup(self) -> None:
        client = FakeEdgarClient(
            documents={"320193/000032019323000106/aapl.htm": b"<html>"}
        )
        got = client.filing_document(
            cik="0000320193",
            accession_dashless="000032019323000106",
            primary_doc="aapl.htm",
        )
        assert got == b"<html>"

    def test_missing_raises_notfound(self) -> None:
        client = FakeEdgarClient()
        with pytest.raises(EdgarNotFound):
            client.submissions("999")

    def test_bound_exception(self) -> None:
        client = FakeEdgarClient(
            raises={("submissions", "320193"): EdgarRateLimited("blocked")}
        )
        with pytest.raises(EdgarRateLimited):
            client.submissions("320193")

    def test_resolve_ticker(self) -> None:
        client = FakeEdgarClient(tickers={"AAPL": "320193"})
        assert client.resolve_ticker("aapl") == "320193"
        assert client.resolve_ticker("AAPL") == "320193"
        assert client.resolve_ticker("zzz") is None

    def test_company_tickers_synthesised(self) -> None:
        client = FakeEdgarClient(tickers={"AAPL": "320193"})
        m = parse_company_tickers(client.company_tickers())
        assert m["aapl"] == "320193"

    def test_search_lookup_by_q(self) -> None:
        client = FakeEdgarClient(searches={"q=climate": b'{"hits":{}}'})
        resp = client.search({"q": "climate"})
        assert resp.json == b'{"hits":{}}'
        assert resp.bytes_out == len(b'{"hits":{}}')


@pytest.mark.skipif(
    os.environ.get("PRECIS_EDGAR_TEST_LIVE") != "1",
    reason="live SEC test gated on PRECIS_EDGAR_TEST_LIVE=1",
)
class TestLive:
    def test_submissions_apple(self) -> None:
        from precis.handlers._edgar_client import EdgarClient

        ua = (
            os.environ.get("PRECIS_EDGAR_USER_AGENT")
            or "precis-mcp test (test@example.com)"
        )
        client = EdgarClient(user_agent=ua)
        raw = client.submissions("320193")
        data = json.loads(raw)
        assert str(data.get("cik", "")).lstrip("0") == "320193"

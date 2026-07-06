"""Tests for ``EdgarHandler`` — get / views / list / search / diff.

Uses ``FakeEdgarClient`` so no network calls fly. PG-backed via the
``store``/``hub`` fixtures; skipped on the torch-free host, runs in the
dev container.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import Unsupported
from precis.handlers._edgar_client import FakeEdgarClient
from precis.handlers.edgar import EdgarHandler

CIK = "320193"

_SUBMISSIONS = json.dumps(
    {
        "cik": CIK,
        "name": "Apple Inc.",
        "tickers": ["AAPL"],
        "filings": {
            "recent": {
                "accessionNumber": [
                    "0000320193-24-000010",
                    "0000320193-23-000106",
                ],
                "form": ["10-K", "10-K"],
                "filingDate": ["2024-11-01", "2023-11-03"],
                "reportDate": ["2024-09-28", "2023-09-30"],
                "primaryDocument": ["aapl-2024.htm", "aapl-2023.htm"],
                "items": ["", ""],
            }
        },
    }
).encode()

_HTML_2023 = b"""
<html><body>
<p>Apple Inc. Form 10-K for fiscal 2023.</p>
<p>Item 1A. Risk Factors</p>
<p>Macroeconomic conditions could adversely affect the Company.</p>
<p>Supply chain concentration in Asia poses operational risk.</p>
<p>Item 7. Management's Discussion and Analysis</p>
<p>Net sales were flat in fiscal 2023.</p>
</body></html>
"""

_HTML_2024 = b"""
<html><body>
<p>Apple Inc. Form 10-K for fiscal 2024.</p>
<p>Item 1A. Risk Factors</p>
<p>Macroeconomic conditions could adversely affect the Company.</p>
<p>Supply chain concentration in Asia poses operational risk.</p>
<p>Generative AI competition is a new and material risk this year.</p>
<p>Item 7. Management's Discussion and Analysis</p>
<p>Net sales grew 2% in fiscal 2024 on services strength.</p>
</body></html>
"""

_FTS = json.dumps(
    {
        "hits": {
            "total": {"value": 1},
            "hits": [
                {
                    "_id": "0000320193-24-000010:aapl-2024.htm",
                    "_source": {
                        "display_names": ["Apple Inc. (AAPL) (CIK 0000320193)"],
                        "file_type": "10-K",
                        "file_date": "2024-11-01",
                    },
                }
            ],
        }
    }
).encode()


@pytest.fixture
def fake_client() -> FakeEdgarClient:
    return FakeEdgarClient(
        submissions={CIK: _SUBMISSIONS},
        documents={
            f"{CIK}/000032019324000010/aapl-2024.htm": _HTML_2024,
            f"{CIK}/000032019323000106/aapl-2023.htm": _HTML_2023,
        },
        tickers={"AAPL": CIK},
        searches={"q=risk": _FTS},
    )


@pytest.fixture
def raw_root(tmp_path: Path) -> Path:
    p = tmp_path / "edgar"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def handler(hub: Hub, fake_client: FakeEdgarClient, raw_root: Path) -> EdgarHandler:
    return EdgarHandler(hub=hub, client=fake_client, raw_root=raw_root)


class TestPutUnsupported:
    def test_put_raises(self, handler: EdgarHandler) -> None:
        with pytest.raises(Unsupported, match="read-only"):
            handler.put(id="0000320193-23-000106", text="note")


class TestGetIngestFlow:
    def test_first_call_ingests(
        self, handler: EdgarHandler, fake_client: FakeEdgarClient
    ) -> None:
        resp = handler.get(id="0000320193-23-000106")
        assert "Apple Inc." in resp.body
        assert "10-K" in resp.body
        # submissions + document fetched.
        endpoints = {c[0] for c in fake_client.calls}
        assert "submissions" in endpoints
        assert "document" in endpoints

    def test_second_call_no_fetch(
        self, handler: EdgarHandler, fake_client: FakeEdgarClient
    ) -> None:
        handler.get(id="0000320193-23-000106")
        n = len(fake_client.calls)
        handler.get(id="0000320193-23-000106")
        assert len(fake_client.calls) == n


class TestViews:
    def test_biblio(self, handler: EdgarHandler) -> None:
        handler.get(id="0000320193-23-000106")
        resp = handler.get(id="0000320193-23-000106", view="biblio")
        assert "Accession" in resp.body
        assert "0000320193-23-000106" in resp.body
        assert "Apple Inc." in resp.body

    def test_body(self, handler: EdgarHandler) -> None:
        handler.get(id="0000320193-23-000106")
        resp = handler.get(id="0000320193-23-000106", view="body")
        assert "Risk Factors" in resp.body or "Macroeconomic" in resp.body

    def test_toc(self, handler: EdgarHandler) -> None:
        handler.get(id="0000320193-23-000106")
        resp = handler.get(id="0000320193-23-000106", view="toc")
        assert "TOC" in resp.body


class TestListViews:
    def test_ticker_list(self, handler: EdgarHandler) -> None:
        resp = handler.get(id="ticker:aapl")
        assert "Apple Inc." in resp.body
        assert "0000320193-24-000010" in resp.body

    def test_cik_list(self, handler: EdgarHandler) -> None:
        resp = handler.get(id="cik:320193")
        assert "10-K" in resp.body

    def test_recent_empty(self, handler: EdgarHandler) -> None:
        resp = handler.get()
        assert "no filings ingested" in resp.body

    def test_recent_after_ingest(self, handler: EdgarHandler) -> None:
        handler.get(id="0000320193-23-000106")
        resp = handler.get()
        assert "0000320193-23-000106" in resp.body


class TestSearch:
    def test_local_hit_after_ingest(self, handler: EdgarHandler) -> None:
        handler.get(id="0000320193-23-000106")
        resp = handler.search(q="supply chain", source="local")
        assert "supply chain" in resp.body.lower() or "filing hit" in resp.body.lower()

    def test_remote_leg(self, handler: EdgarHandler) -> None:
        resp = handler.search(q="risk", source="remote")
        # Remote FTS hit surfaces the 2024 accession.
        assert "0000320193-24-000010" in resp.body


class TestDiff:
    def test_quarter_diff_detects_new_risk(self, handler: EdgarHandler) -> None:
        # Ingest prior then current.
        handler.get(id="0000320193-23-000106")
        handler.get(id="0000320193-24-000010")
        resp = handler.get(id="0000320193-24-000010", view="diff")
        assert "Risk Factors" in resp.body
        assert "Generative AI competition" in resp.body

    def test_diff_applies_tags(self, handler: EdgarHandler) -> None:
        handler.get(id="0000320193-23-000106")
        handler.get(id="0000320193-24-000010")
        handler.get(id="0000320193-24-000010", view="diff")
        ref = handler.store.get_ref(kind="edgar", id="0000320193-24-000010")
        assert ref is not None
        tags = {
            t.value for t in handler.store.tags_for(ref.id) if t.namespace == "open"
        }
        assert "changed:item-1a" in tags
        assert "new-risk-factor" in tags

    def test_diff_no_prior(self, handler: EdgarHandler) -> None:
        # Only the earliest filing ingested → nothing to compare against.
        handler.get(id="0000320193-23-000106")
        resp = handler.get(id="0000320193-23-000106", view="diff")
        assert "no prior filing" in resp.body.lower()

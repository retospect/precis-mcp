"""Tests for ``ingest_filing`` — fetch+parse+store pipeline.

Uses ``FakeEdgarClient`` (no network) and the standard ``store``
fixture from ``conftest.py`` (ephemeral postgres DB with all
migrations applied). Skipped on the torch-free host when no test DB
is reachable; runs in the dev container.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from precis.embedder import MockEmbedder
from precis.errors import NotFound
from precis.handlers._edgar_client import FakeEdgarClient
from precis.handlers._edgar_ingest import EDGAR_CHUNK_KIND, ingest_filing
from precis.store import Store

_ACCESSION = "0000320193-23-000106"
_DASHLESS = "000032019323000106"
_PRIMARY = "aapl-20230930.htm"

_SUBMISSIONS = json.dumps(
    {
        "cik": "320193",
        "name": "Apple Inc.",
        "tickers": ["AAPL"],
        "filings": {
            "recent": {
                "accessionNumber": [_ACCESSION],
                "form": ["10-K"],
                "filingDate": ["2023-11-03"],
                "reportDate": ["2023-09-30"],
                "primaryDocument": [_PRIMARY],
                "items": [""],
            }
        },
    }
).encode()

_FILING_HTML = b"""
<html><body>
<p>Apple Inc. Annual Report on Form 10-K</p>
<p>Item 1. Business</p>
<p>The Company designs, manufactures and markets smartphones and PCs.</p>
<p>Item 1A. Risk Factors</p>
<p>The Company's business is subject to global macroeconomic conditions.</p>
<p>Supply chain concentration in Asia could disrupt operations.</p>
<p>Item 7. Management's Discussion and Analysis</p>
<p>Total net sales decreased 3% during fiscal 2023.</p>
</body></html>
"""


@pytest.fixture
def fake_client() -> FakeEdgarClient:
    return FakeEdgarClient(
        submissions={"320193": _SUBMISSIONS},
        documents={f"320193/{_DASHLESS}/{_PRIMARY}": _FILING_HTML},
        tickers={"AAPL": "320193"},
    )


@pytest.fixture
def raw_root(tmp_path: Path) -> Path:
    p = tmp_path / "edgar"
    p.mkdir(parents=True, exist_ok=True)
    return p


class TestIngestFirstCall:
    def test_inserts_ref_blocks_meta(
        self, store: Store, fake_client: FakeEdgarClient, raw_root: Path
    ) -> None:
        result = ingest_filing(
            _ACCESSION,
            store=store,
            client=fake_client,
            embedder=MockEmbedder(dim=store.embedding_dim()),
            raw_root=raw_root,
        )
        assert result.inserted is True
        assert result.slug == _ACCESSION
        assert result.block_count > 0

        ref = store.get_ref(kind="edgar", id=_ACCESSION)
        assert ref is not None
        assert ref.title == "Apple Inc. — 10-K (2023-09-30)"
        assert ref.provider == "sec_edgar"
        assert ref.meta["cik"] == "320193"
        assert ref.meta["company"] == "Apple Inc."
        assert ref.meta["ticker"] == "AAPL"
        assert ref.meta["form"] == "10-K"
        assert ref.meta["period_of_report"] == "2023-09-30"
        assert "1a" in ref.meta["items"]

    def test_blocks_carry_section_labels(
        self, store: Store, fake_client: FakeEdgarClient, raw_root: Path
    ) -> None:
        result = ingest_filing(
            _ACCESSION,
            store=store,
            client=fake_client,
            embedder=None,
            raw_root=raw_root,
        )
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_kind, section_path, meta->>'item_code' "
                "FROM chunks WHERE ref_id = %s ORDER BY ord",
                (result.ref_id,),
            ).fetchall()
        kinds = {r[0] for r in rows}
        assert kinds == {EDGAR_CHUNK_KIND}
        # A risk-factors block is labelled item-1a.
        item_codes = {r[2] for r in rows}
        assert "1a" in item_codes
        # section_path populated for the risk-factors block.
        risk = [r for r in rows if r[2] == "1a"]
        assert any("Risk Factors" in (r[1] or []) for r in risk)

    def test_writes_raw_to_disk(
        self, store: Store, fake_client: FakeEdgarClient, raw_root: Path
    ) -> None:
        ingest_filing(
            _ACCESSION,
            store=store,
            client=fake_client,
            embedder=None,
            raw_root=raw_root,
        )
        d = raw_root / "320193" / _DASHLESS
        assert (d / "submission.json").exists()
        assert (d / "primary.htm").exists()
        assert (d / "ingest.log").exists()

    def test_auto_tags_applied(
        self, store: Store, fake_client: FakeEdgarClient, raw_root: Path
    ) -> None:
        result = ingest_filing(
            _ACCESSION,
            store=store,
            client=fake_client,
            embedder=None,
            raw_root=raw_root,
        )
        tags = {t.value for t in store.tags_for(result.ref_id) if t.namespace == "open"}
        assert "form:10-k" in tags
        assert "cik:320193" in tags
        assert "fiscal-year:2023" in tags
        # Company / ticker stay in meta, not tag rows.
        assert not any(t.startswith("company:") for t in tags)


class TestIdempotency:
    def test_second_call_skips_fetch(
        self, store: Store, fake_client: FakeEdgarClient, raw_root: Path
    ) -> None:
        first = ingest_filing(
            _ACCESSION,
            store=store,
            client=fake_client,
            embedder=None,
            raw_root=raw_root,
        )
        n_calls = len(fake_client.calls)
        second = ingest_filing(
            _ACCESSION,
            store=store,
            client=fake_client,
            embedder=None,
            raw_root=raw_root,
        )
        assert second.ref_id == first.ref_id
        assert second.inserted is False
        assert second.bytes_fetched == 0
        assert len(fake_client.calls) == n_calls


class TestErrors:
    def test_unknown_accession_raises(self, store: Store, raw_root: Path) -> None:
        client = FakeEdgarClient(submissions={"320193": _SUBMISSIONS})
        with pytest.raises(NotFound, match="not found"):
            ingest_filing(
                "0000320193-23-999999",
                store=store,
                client=client,
                embedder=None,
                raw_root=raw_root,
            )
        assert store.get_ref(kind="edgar", id="0000320193-23-999999") is None

    def test_unknown_cik_raises(self, store: Store, raw_root: Path) -> None:
        client = FakeEdgarClient()  # no submissions canned
        with pytest.raises(NotFound):
            ingest_filing(
                _ACCESSION,
                store=store,
                client=client,
                embedder=None,
                raw_root=raw_root,
            )

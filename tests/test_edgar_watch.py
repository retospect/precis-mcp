"""Saved EDGAR watches through the real store, findings and morning briefing."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from precis.handlers import _edgar_watch_db as watch_db
from precis.handlers._edgar_client import FakeEdgarClient
from precis.handlers._edgar_notable import is_notable
from precis.jobs.edgar_watch import run_one_pass
from precis.store import Store
from precis.workers.briefing import run_briefing
from precis.workers.chase import claim_tracing_findings

_PRIOR = "0000320193-25-000001"
_CURRENT = "0001193125-26-000002"
_ROUTINE = "0000320193-26-000003"
_QUERY = {"q": "supply risk"}


class _BriefingClient:
    def complete(self, messages: list[dict[str, str]]) -> SimpleNamespace:
        prompt = str(messages)
        assert "[edgar]" in prompt
        assert "Example Industries — 10-K" in prompt
        assert f"/320193/{_CURRENT.replace('-', '')}/current.htm" in prompt
        return SimpleNamespace(text="Example Industries described a new supply risk.")


def _client() -> FakeEdgarClient:
    def page(accessions: list[str]) -> bytes:
        return json.dumps(
            {
                "hits": {
                    "total": {"value": 3},
                    "hits": [
                        {
                            "_id": f"{accession}:exhibit.htm",
                            "_source": {
                                "ciks": ["0000320193"],
                                "file_date": "2025-11-01"
                                if accession == _PRIOR
                                else "2026-11-01",
                            },
                        }
                        for accession in accessions
                    ],
                }
            }
        ).encode()

    accessions = [_PRIOR, _CURRENT, _ROUTINE]
    submissions = {
        "cik": "320193",
        "name": "Example Industries",
        "filings": {
            "recent": {
                "accessionNumber": accessions,
                "form": ["10-K", "10-K", "8-K"],
                "filingDate": ["2025-11-01", "2026-11-01", "2026-11-01"],
                "reportDate": ["2025-09-30", "2026-09-30", "2026-11-01"],
                "primaryDocument": ["prior.htm", "current.htm", "routine.htm"],
                "items": ["", "", "7.01"],
            }
        },
    }
    documents = {}
    for accession, doc, paragraph in zip(
        accessions,
        ("prior.htm", "current.htm", "routine.htm"),
        (
            "Demand may fluctuate.",
            "A factory fire halted all shipments.",
            "Investor slides.",
        ),
        strict=True,
    ):
        key = f"320193/{accession.replace('-', '')}/{doc}"
        documents[key] = f"<p>Item 1A. Risk Factors</p><p>{paragraph}</p>".encode()

    return FakeEdgarClient(
        searches={
            "q=supply risk": page([_CURRENT, _ROUTINE]),
            "q=supply risk#2": page([_PRIOR]),
        },
        submissions={"320193": json.dumps(submissions).encode()},
        documents=documents,
    )


def test_watch_projects_news_and_generated_findings(
    store: Store, tmp_path: Path
) -> None:
    client = _client()
    for name in ("first", "overlap"):
        watch_db.create(store, name=name, params=_QUERY, interval_s=3600)

    dry = run_one_pass(store=store, client=client, raw_root=tmp_path, dry_run=True)
    assert not any(result.errors for result in dry.results)
    assert dry.rolling_bytes > 0
    assert not store.list_refs(kind="edgar")
    assert all(w.last_run_at is None for w in watch_db.list_all(store))

    live = run_one_pass(store=store, client=client, raw_root=tmp_path)
    assert [r.completed for r in live.results] == [[_PRIOR, _ROUTINE, _CURRENT]] * 2
    assert not watch_db.list_due(store)
    assert store.get_ref(kind="news", id=f"edgar-{_ROUTINE}") is None
    news = store.get_ref(kind="news", id=f"edgar-{_CURRENT}")
    assert news is not None
    open_tags = {value for _, value in store.ref_tags_bulk([news.id])[news.id]}
    assert {
        "category:news",
        "source:edgar",
        "form:10-k",
        "published:2026-11-01",
    } <= open_tags

    (finding,) = store.list_refs(kind="finding")
    trust = store.trust_state("ref", finding.id)
    assert (
        trust is not None
        and trust.origin == "generated"
        and trust.confidence == "hunch"
    )
    with store.tx() as conn:
        assert not claim_tracing_findings(conn, limit=10)
    prior = store.get_ref(kind="edgar", id=_PRIOR)
    current = store.get_ref(kind="edgar", id=_CURRENT)
    assert prior is not None and current is not None
    assert {
        edge.dst_ref_id
        for edge in store.links_for(
            finding.id, direction="out", relation="derived-from"
        )
    } == {prior.id, current.id}

    no_work = run_one_pass(store=store, client=client, raw_root=tmp_path)
    assert not no_work.results and no_work.rolling_bytes == live.rolling_bytes
    paused = run_one_pass(store=store, client=client, raw_root=tmp_path, limit_gb=1e-9)
    assert paused.paused and not paused.results

    brief = run_briefing(store, client=_BriefingClient(), weather_fn=lambda: "")
    assert brief["articles"] == 2
    assert (
        "new supply risk" in store.chunks.list_chunks_for_ref(brief["ref_id"])[0].text
    )


@pytest.mark.parametrize(
    ("form", "items", "expected"),
    [
        ("10-k/a", [], True),
        ("8-K", ["1.05"], True),
        ("8-K", ["7.01"], False),
        ("4", [], False),
    ],
)
def test_notable_forms_and_current_report_items(
    form: str, items: list[str], expected: bool
) -> None:
    assert is_notable(form, items) is expected

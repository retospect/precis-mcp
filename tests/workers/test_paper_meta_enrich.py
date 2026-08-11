"""The scheduled paper-metadata enrichment pass — claim + cadence + retry."""

from __future__ import annotations

from typing import Any

import pytest

from precis.ingest import paper_meta_enrich
from precis.store import Store
from precis.workers.paper_meta_enrich import _STATE_KEY, run_paper_meta_enrich_pass


def _paper(store: Store, *, slug: str, doi: str | None = None) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title="X")
    if doi:
        store.set_ref_identifier(ref.id, "doi", doi, source="manual")
    return ref.id


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    ref = store.fetch_refs_by_ids([ref_id])[ref_id]
    return ref.meta or {}


class TestBatchAndThrottle:
    def test_visits_and_stamps_doi_less_and_doi_papers(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        rid_no_doi = _paper(store, slug="w1")
        rid_doi = _paper(store, slug="w2", doi="10.1/w2")
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")

        monkeypatch.setattr(
            paper_meta_enrich, "_fetch_crossref_message", lambda doi, mailto: None
        )

        result = run_paper_meta_enrich_pass(store)

        assert result.claimed == 2
        assert result.ok == 2
        assert result.failed == 0
        assert paper_meta_enrich.RESOLVED_AT_KEY in _meta(store, rid_no_doi)
        assert paper_meta_enrich.RESOLVED_AT_KEY in _meta(store, rid_doi)

    def test_second_immediate_run_is_a_noop(self, store: Store) -> None:
        _paper(store, slug="w3")
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")

        r1 = run_paper_meta_enrich_pass(store)
        assert r1.claimed == 1

        # Force the throttle open again — the claim predicate now selects
        # zero rows regardless, because the visited ref got stamped.
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")
        r2 = run_paper_meta_enrich_pass(store)
        assert r2.claimed == 0
        assert r2.ok == 0
        assert r2.failed == 0

    def test_throttled_second_call_without_marker_reset_is_idle(
        self, store: Store
    ) -> None:
        _paper(store, slug="w4")
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")
        r1 = run_paper_meta_enrich_pass(store)
        assert r1.claimed == 1

        r2 = run_paper_meta_enrich_pass(store)  # not due yet
        assert r2.claimed == 0


class TestFailureIsolation:
    def test_one_ref_raising_does_not_sink_the_batch(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        rid_bad = _paper(store, slug="w5", doi="10.1/bad")
        rid_ok = _paper(store, slug="w6")
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")

        real_enrich = paper_meta_enrich.enrich_paper

        def _flaky(store_: Store, ref_id: int, **kw: Any) -> Any:
            if ref_id == rid_bad:
                raise RuntimeError("boom")
            return real_enrich(store_, ref_id, **kw)

        monkeypatch.setattr(paper_meta_enrich, "enrich_paper", _flaky)

        result = run_paper_meta_enrich_pass(store)
        assert result.claimed == 2
        assert result.failed == 1
        assert result.ok == 1
        assert paper_meta_enrich.RESOLVED_AT_KEY in _meta(store, rid_ok)
        assert paper_meta_enrich.RESOLVED_AT_KEY not in _meta(store, rid_bad)

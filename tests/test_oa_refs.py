"""Tests for the OpenAlex ``referenced_works`` backfill
(``precis.backfill.oa_refs``, gr513755 slice 2).

End-to-end against real PG (the ``store`` fixture); the OpenAlex fetch
functions and the shared rate limiter are monkeypatched — no network,
no real limiter row, anywhere in this file.
"""

from __future__ import annotations

from typing import Any

import pytest
from psycopg.types.json import Jsonb

from precis.backfill.oa_refs import (
    OA_LINK_SOURCE,
    backfill_oa_refs,
    materialize_oa_references,
    unparsed_candidates,
)
from precis.utils.rate_limit import RateLimited
from precis.workers.bib_parse import BIB_PARSE_VERSION
from tests.workers._helpers import seed_chunk, seed_ref


@pytest.fixture(autouse=True)
def _shared_limiter_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the real shared limiter."""
    monkeypatch.setattr("precis.utils.rate_limit.require", lambda p, **_: None)


def _work(*w_ids: str, oid: str = "W900") -> dict[str, Any]:
    """A trimmed OpenAlex work object — the fields ``normalize`` reads
    for this pass (id + referenced_works)."""
    return {
        "id": f"https://openalex.org/{oid}",
        "referenced_works": [f"https://openalex.org/{w}" for w in w_ids],
        "referenced_works_count": len(w_ids),
    }


def _add_id(store: Any, ref_id: int, id_kind: str, id_value: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES (%s, %s, %s, %s)",
            (id_kind, id_value, ref_id, "test"),
        )
        conn.commit()


def _seed_candidate(
    store: Any, *, openalex_id: str | None = None, doi: str | None = None
) -> int:
    """A live paper with a ``references`` chunk and the given identifier(s)."""
    ref_id = seed_ref(store)
    seed_chunk(
        store,
        ref_id=ref_id,
        chunk_kind="references",
        text="- [1] A. Author, Journal 2020, 1, 2.",
    )
    if openalex_id:
        _add_id(store, ref_id, "openalex", openalex_id)
    if doi:
        _add_id(store, ref_id, "doi", doi)
    return ref_id


def _seed_held(store: Any, *, openalex_id: str, doi: str | None = None) -> int:
    ref_id = seed_ref(store)
    _add_id(store, ref_id, "openalex", openalex_id)
    if doi:
        _add_id(store, ref_id, "doi", doi)
    return ref_id


def _entries(store: Any, ref_id: int) -> list[tuple[Any, ...]]:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT marker, raw_text, doi, held_ref_id, match_conf, "
            "       parse_version, source, openalex_id "
            "FROM paper_bib_entries WHERE ref_id = %s ORDER BY marker",
            (ref_id,),
        ).fetchall()


def _cites(store: Any, ref_id: int) -> list[tuple[Any, ...]]:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT dst_ref_id, meta->>'source', meta->>'bib_entry_id' "
            "FROM links WHERE src_ref_id = %s AND relation = 'cites' "
            "ORDER BY dst_ref_id",
            (ref_id,),
        ).fetchall()


def _meta(store: Any, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return row[0] or {}


def _set_meta(store: Any, ref_id: int, patch: dict[str, Any]) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s WHERE ref_id = %s",
            (Jsonb(patch), ref_id),
        )
        conn.commit()


class TestMaterialize:
    def test_writes_entries_and_links(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fixture work yields one row per referenced work — held
        targets resolved (+ DOI copied), unheld kept with the W-id — and
        one ``cites`` link per held target."""
        citing = _seed_candidate(store, openalex_id="w900")
        h1 = _seed_held(store, openalex_id="w111", doi="10.1234/foo")
        h2 = _seed_held(store, openalex_id="w222")
        calls: list[str] = []

        def _fetch(oid: str, **kw: Any) -> dict[str, Any]:
            calls.append(oid)
            return _work("W111", "W222", "W333")

        monkeypatch.setattr(
            "precis.ingest.openalex_meta.fetch_openalex_work_by_id", _fetch
        )

        r = materialize_oa_references(store, citing)

        assert calls == ["w900"]
        assert r.outcome == "written" and r.fetched
        assert r.entries == 3 and r.held == 2 and r.links == 2
        rows = _entries(store, citing)
        assert rows == [
            (-3, "openalex:W333", None, None, 1.0, 0, "openalex", "W333"),
            (-2, "openalex:W222", None, h2, 1.0, 0, "openalex", "W222"),
            (-1, "openalex:W111", "10.1234/foo", h1, 1.0, 0, "openalex", "W111"),
        ]
        links = _cites(store, citing)
        assert [(dst, src) for dst, src, _ in links] == [
            (h1, OA_LINK_SOURCE),
            (h2, OA_LINK_SOURCE),
        ]
        # The fetched work landed as meta.openalex + the w… identifier.
        meta = _meta(store, citing)
        assert meta["openalex"]["id"] == "W900"
        assert meta["openalex"]["referenced_works"] == ["W111", "W222", "W333"]

    def test_doi_fallback_fetch(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No OpenAlex identifier → the DOI door is tried."""
        citing = _seed_candidate(store, doi="10.9999/paper")
        monkeypatch.setattr(
            "precis.ingest.openalex_meta.fetch_openalex_work_by_id",
            lambda oid, **kw: pytest.fail("openalex-id fetch must not run"),
        )
        monkeypatch.setattr(
            "precis.ingest.openalex_meta.fetch_openalex_work",
            lambda doi, **kw: _work("W111", oid="W900"),
        )
        _seed_held(store, openalex_id="w111")

        r = materialize_oa_references(store, citing)

        assert r.outcome == "written" and r.entries == 1
        # And the work's own W-id was registered as this ref's identifier.
        assert _meta(store, citing)["openalex"]["id"] == "W900"

    def test_rerun_adds_nothing(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        citing = _seed_candidate(store, openalex_id="w900")
        _seed_held(store, openalex_id="w111")
        calls: list[str] = []

        def _fetch(oid: str, **kw: Any) -> dict[str, Any]:
            calls.append(oid)
            return _work("W111")

        monkeypatch.setattr(
            "precis.ingest.openalex_meta.fetch_openalex_work_by_id", _fetch
        )

        first = materialize_oa_references(store, citing)
        second = materialize_oa_references(store, citing)

        assert first.outcome == "written" and first.entries == 1
        assert second.outcome == "skipped"
        assert calls == ["w900"]  # no second fetch
        assert len(_entries(store, citing)) == 1

    def test_paper_with_parsed_entries_skipped(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Crossref/bib_parse-parsed paper is left entirely alone."""
        citing = _seed_candidate(store, openalex_id="w900")
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO paper_bib_entries "
                "(ref_id, marker, raw_text, parse_version) "
                "VALUES (%s, %s, %s, %s)",
                (citing, 1, "A. Author, Journal 2020, 1, 2", BIB_PARSE_VERSION),
            )
            conn.commit()
        monkeypatch.setattr(
            "precis.ingest.openalex_meta.fetch_openalex_work_by_id",
            lambda oid, **kw: pytest.fail("fetch must not run"),
        )
        monkeypatch.setattr(
            "precis.ingest.openalex_meta.fetch_openalex_work",
            lambda doi, **kw: pytest.fail("fetch must not run"),
        )

        r = materialize_oa_references(store, citing)

        assert r.outcome == "skipped"
        assert len(_entries(store, citing)) == 1

    def test_cached_meta_needs_no_fetch(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``meta.openalex.referenced_works`` already cached → zero calls."""
        citing = _seed_candidate(store)
        _set_meta(
            store,
            citing,
            {"openalex": {"id": "W900", "referenced_works": ["W111", "W333"]}},
        )
        _seed_held(store, openalex_id="w111")
        monkeypatch.setattr(
            "precis.ingest.openalex_meta.fetch_openalex_work_by_id",
            lambda oid, **kw: pytest.fail("fetch must not run"),
        )
        monkeypatch.setattr(
            "precis.ingest.openalex_meta.fetch_openalex_work",
            lambda doi, **kw: pytest.fail("fetch must not run"),
        )

        r = materialize_oa_references(store, citing)

        assert r.outcome == "written" and not r.fetched
        assert r.entries == 2 and r.held == 1

    def test_miss_stamps_and_converges(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuine 404 stamps the miss; the paper stops being a candidate."""
        citing = _seed_candidate(store, openalex_id="w900")
        monkeypatch.setattr(
            "precis.ingest.openalex_meta.fetch_openalex_work_by_id",
            lambda oid, **kw: None,
        )

        r = materialize_oa_references(store, citing)

        assert r.outcome == "miss"
        assert _meta(store, citing)["openalex"]["miss"] is True
        assert citing not in unparsed_candidates(store, limit=100)

    def test_empty_reference_list_converges(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A work with zero references leaves no rows but is done —
        ``referenced_works_count=0`` in meta excludes it next run."""
        citing = _seed_candidate(store, openalex_id="w900")
        monkeypatch.setattr(
            "precis.ingest.openalex_meta.fetch_openalex_work_by_id",
            lambda oid, **kw: _work(),
        )

        r = materialize_oa_references(store, citing)

        assert r.outcome == "empty"
        assert _entries(store, citing) == []
        assert citing not in unparsed_candidates(store, limit=100)


class TestCandidates:
    def test_shape_and_order(self, store: Any) -> None:
        """Newest first; papers without a references chunk, without any
        identifier, or already parsed are not candidates."""
        a = _seed_candidate(store, openalex_id="w1")
        b = _seed_candidate(store, doi="10.1/x")
        # No references chunk → excluded.
        c = seed_ref(store)
        _add_id(store, c, "openalex", "w2")
        # References chunk but no identifier → excluded.
        _seed_candidate(store)
        assert unparsed_candidates(store, limit=10) == [b, a]

    def test_cached_meta_paper_is_a_candidate(self, store: Any) -> None:
        """A paper whose only path is a cached referenced_works list
        (no doi/openalex identifier) still qualifies."""
        ref_id = _seed_candidate(store)
        _set_meta(store, ref_id, {"openalex": {"referenced_works": ["W1"]}})
        assert ref_id in unparsed_candidates(store, limit=10)


class TestRun:
    def test_stops_cleanly_on_rate_limit(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shared limiter refusing mid-run ends the run, not with a
        traceback — unworked candidates re-select next run."""
        _seed_candidate(store, openalex_id="w1")
        worked = _seed_candidate(store, openalex_id="w2")
        _seed_held(store, openalex_id="w111")
        calls = 0

        def _fetch(oid: str, **kw: Any) -> dict[str, Any] | None:
            nonlocal calls
            calls += 1
            # Newest first: w2 is fetched, then the limiter refuses w1.
            if calls == 1:
                return _work("W111")
            raise RateLimited("openalex")

        monkeypatch.setattr(
            "precis.ingest.openalex_meta.fetch_openalex_work_by_id", _fetch
        )

        r = backfill_oa_refs(store, limit=10)

        assert r.rate_limited is True
        assert r.candidates == 2
        assert r.written == 1
        # The unworked paper is still a candidate; the worked one is not.
        remaining = unparsed_candidates(store, limit=10)
        assert worked not in remaining and len(remaining) == 1

    def test_full_run_counts(self, store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_candidate(store, openalex_id="w1")
        _seed_candidate(store, openalex_id="w2")
        _seed_held(store, openalex_id="w111", doi="10.1/h")
        monkeypatch.setattr(
            "precis.ingest.openalex_meta.fetch_openalex_work_by_id",
            lambda oid, **kw: _work("W111", "W333"),
        )

        r = backfill_oa_refs(store, limit=10)

        assert r.candidates == 2 and r.written == 2
        assert r.entries == 4 and r.held == 2 and r.links == 2
        assert r.fetches == 2 and r.failed == 0 and not r.rate_limited
        assert unparsed_candidates(store, limit=10) == []

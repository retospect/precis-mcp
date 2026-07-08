"""Tests for ``precis.ingest.openalex_meta`` — free OpenAlex enrichment.

Network is not touched: the normalizer is pure, and ``enrich_ref`` is driven
with a monkeypatched ``fetch_openalex_work`` + a tiny fake store so the
byline-only-when-empty rule and the write calls are asserted without a DB.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.ingest import openalex_meta
from precis.ingest.openalex_meta import (
    ENRICH_VERSION,
    _reconstruct_abstract,
    _short_id,
    enrich_ref,
    normalize,
)

# A trimmed OpenAlex work object (the fields the normalizer reads).
_WORK: dict[str, Any] = {
    "id": "https://openalex.org/W4386410574",
    "abstract_inverted_index": {"Highly": [0], "sensitive": [1], "sensors": [2]},
    "primary_topic": {"display_name": "Advanced Nanomaterials in Catalysis"},
    "topics": [
        {"display_name": "Advanced Nanomaterials in Catalysis"},
        {"display_name": "Electrochemical Analysis"},
    ],
    "concepts": [{"display_name": "Nanomaterial"}],
    "keywords": [{"display_name": "single atom"}],
    "funders": [{"display_name": "NSFC"}],
    "mesh": [{"descriptor_name": "Biosensing Techniques"}],
    "sustainable_development_goals": [{"display_name": "Good Health"}],
    "fwci": 0.98,
    "cited_by_count": 11,
    "referenced_works_count": 2,
    "referenced_works": [
        "https://openalex.org/W1",
        "https://openalex.org/W2",
    ],
    "is_retracted": False,
    "open_access": {"oa_status": "gold"},
    "authorships": [
        {
            "author": {
                "display_name": "Jinglin Fu",
                "orcid": "https://orcid.org/0000-0002-0814-0089",
            },
            "institutions": [
                {
                    "display_name": "Tsinghua University",
                    "ror": "https://ror.org/03cve4549",
                    "country_code": "CN",
                }
            ],
        }
    ],
}


class TestHelpers:
    def test_short_id(self) -> None:
        assert _short_id("https://openalex.org/W4386410574") == "W4386410574"
        assert _short_id(None) == ""

    def test_reconstruct_abstract(self) -> None:
        assert _reconstruct_abstract({"a": [0], "b": [1], "c": [2]}) == "a b c"
        # gaps and non-int positions are tolerated, never raises
        assert _reconstruct_abstract({"x": [1]}) == "x"
        assert _reconstruct_abstract(None) == ""
        assert _reconstruct_abstract({}) == ""


class TestNormalize:
    def test_meta_block_shape(self) -> None:
        enr = normalize(_WORK)
        assert enr.openalex_id == "W4386410574"
        m = enr.meta
        assert m["v"] == ENRICH_VERSION
        assert m["id"] == "W4386410574"
        assert m["abstract"] == "Highly sensitive sensors"
        assert m["primary_topic"] == "Advanced Nanomaterials in Catalysis"
        assert m["topics"] == [
            "Advanced Nanomaterials in Catalysis",
            "Electrochemical Analysis",
        ]
        assert m["funders"] == ["NSFC"]
        assert m["mesh"] == ["Biosensing Techniques"]
        assert m["fwci"] == 0.98
        assert m["cited_by_count"] == 11
        assert m["referenced_works"] == ["W1", "W2"]
        assert m["referenced_works_count"] == 2
        assert m["is_retracted"] is False
        assert m["oa_status"] == "gold"

    def test_authorships_carry_orcid_ror(self) -> None:
        enr = normalize(_WORK)
        a = enr.meta["authorships"][0]
        assert a["name"] == "Jinglin Fu"
        assert a["orcid"].endswith("0002-0814-0089")
        assert a["ror"].endswith("03cve4549")
        assert a["country"] == "CN"
        # byline shape keeps affiliation + ror (drops orcid + country per schema)
        b = enr.byline_authors[0]
        assert b["affiliation"] == "Tsinghua University"
        assert b["ror"].endswith("03cve4549")

    def test_empty_work_is_safe(self) -> None:
        enr = normalize({})
        assert enr.openalex_id == ""
        assert enr.meta == {"v": ENRICH_VERSION}
        assert enr.byline_authors == []


class _FakeConn:
    def __init__(self, current_authors: Any) -> None:
        self._authors = current_authors

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def execute(self, sql: str, params: Any) -> _FakeConn:
        return self

    def fetchone(self) -> tuple[Any]:
        return (self._authors,)


class _FakePool:
    def __init__(self, current_authors: Any) -> None:
        self._authors = current_authors

    def connection(self) -> _FakeConn:
        return _FakeConn(self._authors)


class _FakeStore:
    def __init__(self, current_authors: Any) -> None:
        self.pool = _FakePool(current_authors)
        self.updates: list[dict[str, Any]] = []
        self.identifiers: list[tuple[int, str, str]] = []

    def update_paper_fields(self, ref_id: int, **kw: Any) -> None:
        self.updates.append({"ref_id": ref_id, **kw})

    def set_ref_identifier(
        self, ref_id: int, scheme: str, value: str, **kw: Any
    ) -> None:
        self.identifiers.append((ref_id, scheme, value))


class TestEnrichRef:
    def test_writes_meta_and_identifier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            openalex_meta, "fetch_openalex_work", lambda doi, **k: _WORK
        )
        store = _FakeStore(current_authors=None)
        enr = enrich_ref(store, 42, doi="10.3390/x")
        assert enr is not None
        assert store.identifiers == [(42, "openalex", "W4386410574")]
        upd = store.updates[0]
        assert upd["meta_patch"] == {"openalex": enr.meta}
        # ref had no authors → byline filled from OpenAlex
        assert upd["authors"] and upd["authors"][0]["name"] == "Jinglin Fu"

    def test_preserves_existing_byline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            openalex_meta, "fetch_openalex_work", lambda doi, **k: _WORK
        )
        store = _FakeStore(current_authors=[{"name": "Existing, Author"}])
        enrich_ref(store, 42, doi="10.3390/x")
        # ref already had authors → byline left untouched (None passed)
        assert store.updates[0]["authors"] is None

    def test_none_when_not_in_openalex(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(openalex_meta, "fetch_openalex_work", lambda doi, **k: None)
        store = _FakeStore(current_authors=None)
        assert enrich_ref(store, 42, doi="10.3390/x") is None
        assert store.updates == []

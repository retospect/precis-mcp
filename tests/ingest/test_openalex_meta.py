"""Tests for ``precis.ingest.openalex_meta`` — free OpenAlex enrichment.

Network is not touched: the normalizer is pure, and ``enrich_ref`` is driven
with a monkeypatched ``fetch_openalex_work`` + a tiny fake store so the
byline-only-when-empty rule and the write calls are asserted without a DB.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from precis.ingest import openalex_meta
from precis.ingest.openalex_meta import (
    ENRICH_VERSION,
    _authorships,
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
                "id": "https://openalex.org/A5023888391",
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

    def test_authorships_captures_openalex_author_id(self) -> None:
        """``author.id`` (an OpenAlex ``A…`` URL) lands as
        ``openalex_author_id`` in the short form — docs/backlog/
        precis.utils.authors module docstring."""
        work = {
            "authorships": [
                {
                    "author": {
                        "id": "https://openalex.org/A5023888391",
                        "display_name": "Jinglin Fu",
                    }
                },
                # No author.id at all — key is simply absent, never blank.
                {"author": {"display_name": "No Id Author"}},
            ]
        }
        rows = _authorships(work)
        assert rows[0]["openalex_author_id"] == "A5023888391"
        assert "openalex_author_id" not in rows[1]

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
        assert a["openalex_author_id"] == "A5023888391"
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
    def __init__(self, current_authors: Any, current_meta: Any) -> None:
        self._authors = current_authors
        self._meta = current_meta

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def execute(self, sql: str, params: Any) -> _FakeConn:
        return self

    def fetchone(self) -> tuple[Any, Any]:
        return (self._authors, self._meta)


class _FakePool:
    def __init__(self, current_authors: Any, current_meta: Any) -> None:
        self._authors = current_authors
        self._meta = current_meta

    def connection(self) -> _FakeConn:
        return _FakeConn(self._authors, self._meta)


class _FakeStore:
    def __init__(self, current_authors: Any, current_meta: Any = None) -> None:
        self.pool = _FakePool(current_authors, current_meta)
        self.updates: list[dict[str, Any]] = []
        self.identifiers: list[tuple[int, str, str]] = []

    def update_paper_fields(self, ref_id: int, **kw: Any) -> None:
        self.updates.append({"ref_id": ref_id, **kw})

    def insert_ref_identifiers(
        self, ref_id: int, identifiers: list[tuple[str, str, str]], **kw: Any
    ) -> int:
        for scheme, value, _source in identifiers:
            self.identifiers.append((ref_id, scheme, value))
        return len(identifiers)


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
        # ref had no top-level abstract → promoted alongside the openalex block
        assert upd["meta_patch"] == {
            "openalex": enr.meta,
            "abstract": enr.meta["abstract"],
        }
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

    def test_promotes_abstract_when_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            openalex_meta, "fetch_openalex_work", lambda doi, **k: _WORK
        )
        store = _FakeStore(current_authors=None, current_meta={})
        enrich_ref(store, 42, doi="10.3390/x")
        assert store.updates[0]["meta_patch"]["abstract"] == "Highly sensitive sensors"

    def test_does_not_overwrite_existing_abstract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            openalex_meta, "fetch_openalex_work", lambda doi, **k: _WORK
        )
        store = _FakeStore(
            current_authors=None, current_meta={"abstract": "Existing abstract."}
        )
        enrich_ref(store, 42, doi="10.3390/x")
        assert "abstract" not in store.updates[0]["meta_patch"]


class TestFetchByIdentifier:
    """gr514199 — ``enrich_ref`` addresses the work by any scheme
    :func:`fetch_openalex_work_by_identifier` resolves, and the W-id
    write is ``insert_ref_identifiers`` (first-write-wins — a claimed
    id no longer raises, so a bulk backfill survives it)."""

    def _selector_url(self, monkeypatch: pytest.MonkeyPatch, identifier: str) -> str:
        monkeypatch.delenv("PRECIS_OPENALEX_CONTENT_KEY", raising=False)
        monkeypatch.setattr(openalex_meta, "get_secret", lambda *a, **k: None)
        captured = _capture_request(monkeypatch)
        work = openalex_meta.fetch_openalex_work_by_identifier(identifier)
        assert work is not None
        return str(captured["url"])

    def test_doi_selector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        url = self._selector_url(monkeypatch, "10.3390/ChemoSensors11090486")
        assert url.endswith("/works/doi:10.3390/chemosensors11090486")

    def test_w_id_selector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        url = self._selector_url(monkeypatch, "W4386410574")
        assert url.endswith("/works/W4386410574")

    def test_openalex_url_selector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        url = self._selector_url(monkeypatch, "https://openalex.org/W4386410574")
        assert url.endswith("/works/W4386410574")

    def test_lowercase_storage_w_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``ref_identifiers`` stores ``w…`` lowercase — the form a
        backfill sweep reads; the detector's ``W`` is uppercase-only."""
        url = self._selector_url(monkeypatch, "w4386410574")
        assert url.endswith("/works/W4386410574")

    def test_pmid_selector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        url = self._selector_url(monkeypatch, "PMID:12345678")
        assert url.endswith("/works/pmid:12345678")

    def test_arxiv_resolves_through_its_datacite_doi(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OpenAlex has no ``arxiv:`` selector; the id rides its
        ``10.48550/arXiv.`` DOI (version stripped) instead."""
        url = self._selector_url(monkeypatch, "arxiv:1705.02630v2")
        assert url.endswith("/works/doi:10.48550/arXiv.1705.02630")

    def test_unselectable_identifier_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _capture_request(monkeypatch)
        with pytest.raises(ValueError, match="no OpenAlex selector"):
            # 40-hex is the S2 paperId shape — detected, but not a selector.
            openalex_meta.fetch_openalex_work_by_identifier("a" * 40)
        with pytest.raises(ValueError, match="unrecognised"):
            openalex_meta.fetch_openalex_work_by_identifier("not-an-id")

    def _recording_fetch(self, monkeypatch: pytest.MonkeyPatch, name: str) -> list[str]:
        """Stub ``openalex_meta.<name>`` to return ``_WORK``; returns the
        list it records each call's first argument on."""
        seen: list[str] = []

        def fake(arg: str, **k: Any) -> dict[str, Any]:
            seen.append(arg)
            return _WORK

        monkeypatch.setattr(openalex_meta, name, fake)
        return seen

    def test_enrich_ref_by_arxiv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = self._recording_fetch(monkeypatch, "fetch_openalex_work")
        store = _FakeStore(current_authors=None)
        enrich_ref(store, 42, identifier="1705.02630v1")
        assert seen == ["10.48550/arXiv.1705.02630"]
        assert store.identifiers == [(42, "openalex", "W4386410574")]

    def test_enrich_ref_by_pmid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = self._recording_fetch(monkeypatch, "_fetch_work")
        store = _FakeStore(current_authors=None)
        enrich_ref(store, 42, identifier="pubmed:12345678")
        assert seen == ["pmid:12345678"]

    def test_enrich_ref_by_w_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = self._recording_fetch(monkeypatch, "fetch_openalex_work_by_id")
        store = _FakeStore(current_authors=None)
        enrich_ref(store, 42, identifier="https://openalex.org/W4386410574")
        assert seen == ["w4386410574"]

    def test_refuses_both_and_neither_identifier(self) -> None:
        store = _FakeStore(current_authors=None)
        with pytest.raises(ValueError, match="not both"):
            enrich_ref(store, 42, doi="10.1/x", identifier="10.1/y")
        with pytest.raises(ValueError, match="needs doi= or identifier="):
            enrich_ref(store, 42)


# Assembled so the source holds no credential-shaped literal.
_FAKE_OA_KEY = "fake" + "-oa-content-key-00000001"


def _capture_request(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the transport; return a sink holding the request's url/params."""
    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"id": "https://openalex.org/W4386410574"}

    class _Client:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def get(self, url: str, params: Any = None) -> _Resp:
            captured["url"] = url
            captured["params"] = dict(params or {})
            return _Resp()

        def close(self) -> None:
            return None

    monkeypatch.setattr(openalex_meta, "http_client", lambda **kw: _Client())
    monkeypatch.setattr(openalex_meta, "call_with_backoff", lambda fn, **kw: fn())
    return captured


class TestApiKeyParam:
    """gr513771: a configured ``PRECIS_OPENALEX_CONTENT_KEY`` rides along as
    ``api_key`` so metadata calls draw the paid credit, not the free pool."""

    def test_sends_api_key_when_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_OPENALEX_CONTENT_KEY", _FAKE_OA_KEY)
        captured = _capture_request(monkeypatch)
        work = openalex_meta.fetch_openalex_work("10.3390/x", email="me@lab.test")
        assert work is not None
        assert captured["params"]["api_key"] == _FAKE_OA_KEY
        assert captured["params"]["mailto"] == "me@lab.test"

    def test_stays_keyless_when_unconfigured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PRECIS_OPENALEX_CONTENT_KEY", raising=False)
        # Pin off the vault/file fallthrough too — a dev host's real key
        # must never leak into a test request.
        monkeypatch.setattr(openalex_meta, "get_secret", lambda *a, **k: None)
        captured = _capture_request(monkeypatch)
        openalex_meta.fetch_openalex_work("10.3390/x")
        assert captured["params"] == {}


class TestApiKeyStaysOutOfLogs:
    """gr513771: a keyed request's ``api_key`` must never reach a log line
    (journald, worker_logs) — the record-creation scrub
    (:mod:`precis.utils.log_scrub`) leaves only the redaction marker."""

    def test_httpx_request_record_masks_api_key(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The exact record ``httpx._client`` emits per request — an
        ``httpx.URL`` arg — renders ``api_key=REDACTED``."""
        from precis.utils import log_scrub

        log_scrub.install_log_scrub()
        request = httpx.Request(
            "GET",
            "https://api.openalex.org/works/doi:10.3390/x",
            params={"mailto": "me@lab.test", "api_key": _FAKE_OA_KEY},
        )
        with caplog.at_level(logging.INFO, logger="httpx"):
            logging.getLogger("httpx").info(
                'HTTP Request: %s %s "%s %d %s"',
                "GET",
                request.url,
                "HTTP/1.1",
                200,
                "OK",
            )
        assert _FAKE_OA_KEY not in caplog.text
        assert "api_key=REDACTED" in caplog.text

    def test_debug_line_with_keyed_url_masks_api_key(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A record whose *text* carries ``?api_key=…`` (httpcore-style
        debug renders, exception messages folding the URL in) scrubs to
        ``<redacted:…>``."""
        from precis.utils import log_scrub

        log_scrub.install_log_scrub()
        url = f"https://api.openalex.org/works/doi:10.3390/x?api_key={_FAKE_OA_KEY}"
        with caplog.at_level(logging.DEBUG, logger="httpcore.http11"):
            logging.getLogger("httpcore.http11").debug(
                "send_request_headers.started request=%s", url
            )
        assert _FAKE_OA_KEY not in caplog.text
        assert "<redacted:url api_key param>" in caplog.text

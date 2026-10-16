"""On-demand / ingest-time Crossref calls go through the shared limiter.

Each site must call ``require("crossref")`` before the request, send no HTTP
when it raises ``RateLimited``, and never turn a refusal into a "not found".
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from precis.ingest import (
    crossref,
    crossref_citation,
    doi_registry,
    provenance,
    si_discovery,
)
from precis.utils.rate_limit import RateLimited


class _Limiter:
    def __init__(self, refuse: bool = False) -> None:
        self.calls: list[str] = []
        self.refuse = refuse

    def __call__(self, provider: str, **_: Any) -> None:
        self.calls.append(provider)
        if self.refuse:
            raise RateLimited(provider)


def _no_http(*_: Any, **__: Any) -> Any:
    raise AssertionError("HTTP sent despite a limiter refusal")


def test_lookup_crossref_acquires_then_fetches(monkeypatch) -> None:
    lim = _Limiter()
    monkeypatch.setattr(crossref, "require_rate_limit", lim)
    monkeypatch.setattr(crossref, "fetch_message", lambda doi, mailto="": None)
    assert crossref.lookup_crossref("10.1/x") is None
    assert lim.calls == ["crossref"]


def test_lookup_crossref_refusal_propagates_not_none(monkeypatch) -> None:
    lim = _Limiter(refuse=True)
    monkeypatch.setattr(crossref, "require_rate_limit", lim)
    monkeypatch.setattr(crossref, "fetch_message", _no_http)
    with pytest.raises(RateLimited):
        crossref.lookup_crossref("10.1/x")
    assert lim.calls == ["crossref"]


def test_bounded_lets_refusal_escape_not_a_miss() -> None:
    """``_bounded`` re-raises RateLimited (-> caller's except), never a verdict."""
    from precis.ingest import metadata_resolve as mr

    def _refused(doi: str, mailto: str = "") -> Any:
        raise RateLimited("crossref")

    with pytest.raises(RateLimited):
        mr._bounded(_refused, "10.1/x", "", timeout=5.0)


def test_provenance_fetch_message_refusal_sends_nothing(monkeypatch) -> None:
    import habanero

    lim = _Limiter(refuse=True)
    monkeypatch.setattr("precis.utils.rate_limit.require", lim)
    cr = MagicMock()
    cr.return_value.works.side_effect = _no_http
    monkeypatch.setattr(habanero, "Crossref", cr)
    with pytest.raises(RateLimited):
        provenance._fetch_crossref_message("10.1/x", None)
    assert lim.calls == ["crossref"]
    cr.return_value.works.assert_not_called()


def test_provenance_search_refusal_returns_empty(monkeypatch) -> None:
    import habanero

    lim = _Limiter(refuse=True)
    monkeypatch.setattr("precis.utils.rate_limit.require", lim)
    cr = MagicMock()
    monkeypatch.setattr(habanero, "Crossref", cr)
    assert provenance._search_crossref_works(title="t", author=None, mailto=None) == []
    assert lim.calls == ["crossref"]
    cr.return_value.works.assert_not_called()


def test_provenance_doi_validity_refusal_returns_none(monkeypatch) -> None:
    from precis.utils import safe_fetch

    lim = _Limiter(refuse=True)
    monkeypatch.setattr("precis.utils.rate_limit.require", lim)
    monkeypatch.setattr(safe_fetch, "safe_get", _no_http)
    assert provenance._fetch_doi_validity("10.1/x", mailto=None) is None
    assert lim.calls == ["crossref"]


def test_doi_registry_refusal_is_lookup_error_not_none(monkeypatch) -> None:
    lim = _Limiter(refuse=True)
    monkeypatch.setattr(doi_registry, "require_rate_limit", lim)
    monkeypatch.setattr(doi_registry, "_get_json", _no_http)
    with pytest.raises(doi_registry.RegistryLookupError):
        doi_registry._crossref_meta("10.1/x")
    assert lim.calls == ["crossref"]


def test_doi_registry_acquires_before_get(monkeypatch) -> None:
    lim = _Limiter()
    monkeypatch.setattr(doi_registry, "require_rate_limit", lim)
    monkeypatch.setattr(doi_registry, "_get_json", lambda *a, **k: None)
    assert doi_registry._crossref_meta("10.1/x") is None
    assert lim.calls == ["crossref"]


def test_citation_refusal_returns_empty(monkeypatch) -> None:
    from precis.utils import safe_fetch

    lim = _Limiter(refuse=True)
    monkeypatch.setattr(crossref_citation, "require_rate_limit", lim)
    monkeypatch.setattr(safe_fetch, "safe_get", _no_http)
    assert crossref_citation._fetch_items("Smith 2020 Nature", "") == []
    assert lim.calls == ["crossref"]


def test_si_discovery_refusal_skips_crossref_get(monkeypatch) -> None:
    lim = _Limiter(refuse=True)
    monkeypatch.setattr("precis.utils.rate_limit.require", lim)
    urls: list[str] = []

    def fetch(url: str) -> si_discovery.HttpResult:
        urls.append(url)
        return si_discovery.HttpResult(status=404)

    res = si_discovery.discover("10.1/x", fetch)
    assert lim.calls == ["crossref"]
    assert not any("api.crossref.org" in u for u in urls)
    assert any(
        m["source"] == si_discovery.SOURCE_CROSSREF
        and m["reason"] == "error:RateLimited"
        for m in res.misses
    )


@pytest.mark.parametrize(
    "fn_path",
    [
        "precis.ingest.doi_backfill.backfill_dois",
        "precis.ingest.metadata_resolve.resolve_triage",
    ],
)
def test_backfill_and_resolve_default_to_the_gated_lookup(fn_path: str) -> None:
    """doi_backfill and metadata_resolve reach Crossref only through their
    ``crossref_fn`` default, which must be the gated ``lookup_crossref``."""
    import importlib
    import inspect

    mod_name, fn_name = fn_path.rsplit(".", 1)
    fn = getattr(importlib.import_module(mod_name), fn_name)
    default = inspect.signature(fn).parameters["crossref_fn"].default
    assert default is crossref.lookup_crossref


def test_cited_doi_refusal_not_cached_clean(monkeypatch) -> None:
    def refused(doi: str, mailto: Any) -> Any:
        raise RateLimited("crossref")

    monkeypatch.setattr(provenance, "_fetch_crossref_message", refused)
    cache: dict = {}
    with pytest.raises(RateLimited):
        provenance._check_cited_doi("10.1/c", mailto=None, cache=cache)
    assert cache == {}


def test_check_doi_cite_walk_refusal_reports_error_not_clean(monkeypatch) -> None:
    parent = {
        "title": ["P"],
        "DOI": "10.1234/p",
        "reference": [{"DOI": "10.1234/a"}, {"DOI": "10.1234/b"}],
    }
    seen: list[str] = []

    def fetch(doi: str, mailto: Any) -> Any:
        seen.append(doi)
        if doi == "10.1234/p":
            return parent
        raise RateLimited("crossref")

    monkeypatch.setattr(provenance, "_fetch_crossref_message", fetch)
    cache: dict = {}
    r = provenance.check_doi("10.1234/p", transitive=True, _cite_cache=cache)
    assert r.status == "ok" and r.cited_findings == []
    assert r.error and "rate-limited" in r.error
    assert cache == {}  # nothing recorded as clean
    assert seen == ["10.1234/p", "10.1234/a"]  # walk stopped at first refusal


def test_lookup_cascade_falls_through_on_refusal(monkeypatch) -> None:
    from precis.ingest import lookup as lk

    monkeypatch.setattr(
        lk,
        "extract_pdf_meta",
        lambda p: {
            "doi": "10.1/x",
            "pdf_hash": "h",
            "page_count": 1,
            "first_pages_text": "",
            "info": {"title": "A Real Title About Things"},
        },
    )
    monkeypatch.setattr(lk, "get_secret", lambda k: "")
    monkeypatch.setattr(lk, "extract_doi_from_filename", lambda p: None)

    def refused(doi: str, mailto: str = "") -> Any:
        raise RateLimited("crossref")

    monkeypatch.setattr(lk, "lookup_crossref", refused)
    titles: list[str] = []

    def by_title(title: str, s2_key: str = "") -> dict[str, Any]:
        titles.append(title)
        return {"title": title, "source": "s2"}

    monkeypatch.setattr(lk, "lookup_title", by_title)
    res = lk.lookup("/nonexistent/x.pdf")
    assert titles == ["A Real Title About Things"]
    assert res["source"] == "s2"

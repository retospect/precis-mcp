"""SI discovery per source, from fixtures (no network)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import httpx

from precis.ingest import si_discovery as sd
from precis.ingest.si_discovery import HttpResult, SiCandidate, discover

DOI = "10.1021/acscatal.3c01963"


def test_default_fetch_landing_html_api_json(monkeypatch) -> None:
    from precis.utils import http, safe_fetch

    accepts: list[str] = []

    @contextmanager
    def client(**kwargs):
        accepts.append(kwargs["headers"]["Accept"])
        yield object()

    monkeypatch.setattr(http, "http_client", client)
    monkeypatch.setattr(sd._HostThrottle, "wait", lambda *_: None)
    monkeypatch.setattr(
        safe_fetch,
        "safe_get",
        lambda _, url: httpx.Response(200, request=httpx.Request("GET", url)),
    )
    fetch = sd.default_fetch()
    fetch(f"https://doi.org/{DOI}")
    for url in (
        f"https://doi.org/api/handles/{DOI}.s001",
        f"https://api.crossref.org/works/{DOI}",
        f"https://api.figshare.com/v2/articles?resource_doi={DOI}",
    ):
        fetch(url)
    assert accepts == [
        "text/html",
        "application/json",
        "application/json",
        "application/json",
    ]


def test_real_springer_moesm_includes_source_data_not_peer_review() -> None:
    fixture = Path(__file__).parents[1] / "fixtures/si/nature-s41467-023-40259-0.html"
    got = sd.parse_landing_links(
        fixture.read_text(encoding="utf-8"),
        "https://www.nature.com/articles/s41467-023-40259-0",
    )
    assert [c.filename for c in got] == [
        "41467_2023_40259_MOESM1_ESM.pdf",
        "41467_2023_40259_MOESM3_ESM.xlsx",
    ]
    assert got[0].is_pdf and not got[1].is_pdf


def _json(obj: object, status: int = 200) -> HttpResult:
    return HttpResult(status=status, text=json.dumps(obj))


FIGSHARE_LIST = [
    {"id": 23629437, "doi": "10.1021/acscatal.3c01963.s001", "title": "SI"},
    # a search neighbour that is NOT a component of this paper
    {"id": 1, "doi": "10.1021/other.999.s001", "title": "unrelated"},
]
FIGSHARE_FILES = [
    {
        "id": 41461572,
        "name": "cs3c01963_si_001.pdf",
        "size": 2080000,
        "mimetype": "application/pdf",
        "download_url": "https://ndownloader.figshare.com/files/41461572",
    },
    {
        "id": 41461573,
        "name": "cs3c01963_si_002.xlsx",
        "size": 5000,
        "mimetype": "application/vnd.ms-excel",
        "download_url": "https://ndownloader.figshare.com/files/41461573",
    },
]


def test_figshare_files_and_article_filter() -> None:
    arts = sd.parse_figshare_articles(json.dumps(FIGSHARE_LIST), DOI)
    assert arts == [(23629437, "10.1021/acscatal.3c01963.s001")]
    cands = sd.parse_figshare_files(json.dumps(FIGSHARE_FILES), arts[0][1])
    assert [c.filename for c in cands] == [
        "cs3c01963_si_001.pdf",
        "cs3c01963_si_002.xlsx",
    ]
    assert cands[0].is_pdf and not cands[1].is_pdf
    assert cands[0].source == "figshare"
    assert cands[0].component_doi == "10.1021/acscatal.3c01963.s001"


def test_crossref_relation_empty_and_populated() -> None:
    empty = json.dumps({"message": {"relation": {}}})
    assert sd.parse_crossref_components(empty, DOI) == []
    full = json.dumps(
        {
            "message": {
                "relation": {
                    "has-supplement": [{"id-type": "doi", "id": f"{DOI}.s001"}],
                    "cites": [{"id-type": "doi", "id": "10.1/unrelated"}],
                    "is-supplemented-by": [
                        {"id-type": "doi", "id": "10.9999/other-component"}
                    ],
                }
            }
        }
    )
    got = sd.parse_crossref_components(full, DOI)
    assert f"{DOI}.s001" in got
    assert "10.9999/other-component" in got  # type mentions supplement
    assert "10.1/unrelated" not in got


def test_handle_url_parse() -> None:
    payload = json.dumps(
        {
            "responseCode": 1,
            "values": [
                {"type": "URL", "data": {"value": "https://pubs.acs.org/x/si_001.pdf"}}
            ],
        }
    )
    assert sd.parse_handle_url(payload) == "https://pubs.acs.org/x/si_001.pdf"
    assert sd.parse_handle_url(json.dumps({"responseCode": 100})) is None
    assert sd.parse_handle_url("not json") is None


LANDING = {
    "acs": (
        "https://pubs.acs.org/doi/10.1021/acscatal.3c01963",
        '<a href="/doi/suppl/10.1021/acscatal.3c01963/suppl_file/cs3c01963_si_001.pdf">'
        "Supporting Information</a>",
        "cs3c01963_si_001.pdf",
    ),
    "elsevier": (
        "https://www.sciencedirect.com/science/article/pii/S0001",
        '<a href="https://ars.els-cdn.com/content/image/1-s2.0-S0001-mmc1.pdf">'
        "Download Supplementary data</a>",
        "1-s2.0-S0001-mmc1.pdf",
    ),
    "rsc": (
        "https://pubs.rsc.org/en/content/articlelanding/2023/cy/d3cy00001a",
        '<a href="https://www.rsc.org/suppdata/d3/cy/d3cy00001a/d3cy00001a1.pdf">ESI</a>',
        "d3cy00001a1.pdf",
    ),
    "wiley": (
        "https://onlinelibrary.wiley.com/doi/10.1002/anie.2023",
        '<a href="/action/downloadSupplement?doi=10.1002%2Fanie.2023&file=anie_si.pdf">'
        "Supporting Information</a>",
        "anie_si.pdf",
    ),
}


def test_landing_page_publisher_patterns() -> None:
    for name, (base, html, fname) in LANDING.items():
        got = sd.parse_landing_links(f"<html><body>{html}</body></html>", base)
        assert len(got) == 1, name
        assert got[0].filename == fname, name
        assert got[0].source == "landing_page"
        assert got[0].url.startswith("http")


def test_landing_page_non_si_pdf_link_does_not_match() -> None:
    base = "https://pubs.acs.org/doi/10.1021/acscatal.3c01963"
    html = (
        '<a href="/doi/pdf/10.1021/acscatal.3c01963">Download PDF</a>'
        '<a href="/doi/suppl/10.1021/acscatal.3c01963">Supporting Information</a>'
        '<a href="https://example.org/files/other-paper.pdf">Full text (PDF)</a>'
        '<a href="/toc/acscii/current">Current issue</a>'
    )
    # the tab link to an HTML page, the article PDF, a third-party PDF and a
    # nav link: none says supplement AND points at a file
    assert sd.parse_landing_links(html, base) == []


def _router(routes: dict[str, HttpResult]):
    calls: list[str] = []

    def fetch(url: str) -> HttpResult:
        calls.append(url)
        for prefix, res in routes.items():
            if url.startswith(prefix):
                return res
        return HttpResult(status=404)

    return fetch, calls


def test_discover_all_sources_dedupes_and_records_cloudflare_miss() -> None:
    si_url = f"https://pubs.acs.org/doi/suppl/{DOI}/suppl_file/cs3c01963_si_001.pdf"
    handle_ok = _json(
        {"values": [{"type": "URL", "data": {"value": si_url}}]},
    )
    fetch, calls = _router(
        {
            "https://api.figshare.com/v2/articles?resource_doi=": _json(FIGSHARE_LIST),
            "https://api.figshare.com/v2/articles/23629437/files": _json(
                FIGSHARE_FILES
            ),
            f"https://api.crossref.org/works/{DOI}": _json({"message": {}}),
            f"https://doi.org/api/handles/{DOI}.s001": handle_ok,
            # .s002 -> 404 ends the probe
            f"https://doi.org/{DOI}": HttpResult(
                status=403, headers={"cf-mitigated": "challenge"}
            ),
        }
    )
    res = discover(DOI, fetch)
    names = [c.filename for c in res.candidates]
    # Figshare pdf + xlsx; the component-DOI URL has the same filename as the
    # Figshare pdf, so it is not added twice
    assert names == ["cs3c01963_si_001.pdf", "cs3c01963_si_002.xlsx"]
    assert res.candidates[0].source == "figshare"
    assert {
        "url": f"https://doi.org/{DOI}",
        "source": "landing_page",
        "reason": "cloudflare_403",
    } in res.misses
    # the probe stopped at the first non-resolving component
    assert f"https://doi.org/api/handles/{DOI}.s002" in calls
    assert f"https://doi.org/api/handles/{DOI}.s003" not in calls
    # the handle API is used, never the gated landing URL of a component
    assert not any(c.endswith(".s001") and "api/handles" not in c for c in calls)


def test_discover_component_probe_only() -> None:
    si_url = "https://pubs.acs.org/doi/suppl/10.1021/x/suppl_file/x_si_001.pdf"
    fetch, _ = _router(
        {
            f"https://doi.org/api/handles/{DOI}.s001": _json(
                {"values": [{"type": "URL", "data": {"value": si_url}}]}
            ),
        }
    )
    res = discover(DOI, fetch)
    assert res.candidates == [
        SiCandidate(
            url=si_url,
            source="component_doi",
            filename="x_si_001.pdf",
            component_doi=f"{DOI}.s001",
        )
    ]


def test_discover_nothing_found_is_empty_not_error() -> None:
    fetch, _ = _router({})
    res = discover(DOI, fetch)
    assert res.candidates == []


def test_discover_source_exception_is_recorded_and_others_run() -> None:
    def fetch(url: str) -> HttpResult:
        if "figshare" in url:
            raise RuntimeError("boom")
        return HttpResult(status=404)

    res = discover(DOI, fetch)
    assert res.candidates == []
    assert any(
        m["source"] == "figshare" and m["reason"] == "error:RuntimeError"
        for m in res.misses
    )

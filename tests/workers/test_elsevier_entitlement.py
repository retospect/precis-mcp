"""Synthetic Article Retrieval previews must never stop the OA cascade."""

from pathlib import Path

import pytest

from precis.ingest.markup import MarkupParseError, parse_elsevier
from precis.workers import fetch_oa
from tests.workers.test_fetch_oa import _stub

FIXTURE = Path(__file__).parents[1] / "fixtures/elsevier/abstract-only.xml"


def test_serial_wrapper_is_not_fulltext_body():
    with pytest.raises(MarkupParseError):
        parse_elsevier(FIXTURE.read_bytes())


@pytest.mark.parametrize("leg", [fetch_oa._try_elsevier, fetch_oa._try_elsevier_markup])
def test_abstract_only_200_refuses_and_never_downloads_pdf(tmp_path, monkeypatch, leg):
    calls = []

    def xml(url, target, **kw):
        target.write_bytes(FIXTURE.read_bytes())
        return target.stat().st_size

    def pdf(*a, **kw):
        calls.append("pdf")
        return 100

    monkeypatch.setattr(fetch_oa, "_download_markup", xml)
    monkeypatch.setattr(fetch_oa, "_download_pdf", pdf)
    result = leg(
        _stub(doi="10.1016/j.synthetic.2026.1"), inbox_dir=tmp_path, api_key="synthetic"
    )
    assert result.event == "fetch_failed"
    assert result.payload["reason"] == "entitlement"
    assert calls == []
    assert not [p for p in tmp_path.rglob("*") if p.is_file()]


@pytest.mark.parametrize("leg", [fetch_oa._try_elsevier, fetch_oa._try_elsevier_markup])
def test_short_entitled_closed_body_is_fulltext(tmp_path, monkeypatch, leg):
    def xml(url, target, **kw):
        data = b"<full-text-retrieval-response><coredata><openaccess>0</openaccess></coredata><body><para>Short full body.</para></body></full-text-retrieval-response>"
        target.write_bytes(data)
        return len(data)

    def pdf(url, target, **kw):
        target.write_bytes(b"%PDF-synthetic")
        return target.stat().st_size

    monkeypatch.setattr(fetch_oa, "_download_markup", xml)
    monkeypatch.setattr(fetch_oa, "_download_pdf", pdf)
    result = leg(
        _stub(doi="10.1016/j.synthetic.2026.2"), inbox_dir=tmp_path, api_key="synthetic"
    )
    assert result.event == "fetch_ok"
    assert not list(tmp_path.rglob("*.entitlement.xml"))


def test_preview_falls_through_to_later_pdf_leg(store, tmp_path, monkeypatch):
    from tests.workers.test_fetch_oa import _seed_paper_stub

    ref_id = _seed_paper_stub(store, doi="10.1016/j.synthetic.2026.3")

    def xml(url, target, **kw):
        assert target.parent.name == ".staging"
        target.write_bytes(FIXTURE.read_bytes())
        return target.stat().st_size

    monkeypatch.setattr(fetch_oa, "_download_markup", xml)
    monkeypatch.setattr(fetch_oa, "_resolve_arxiv_id", lambda *a, **kw: "")
    for name in ("_try_publisher", "_try_unpaywall", "_try_crossref", "_try_openalex"):
        monkeypatch.setattr(fetch_oa, name, lambda *a, **kw: None)

    def later(stub, *, inbox_dir):
        target = inbox_dir / "later.pdf"
        target.write_bytes(b"%PDF-synthetic")
        return fetch_oa.FetchOutcome("fetch_ok", {"filename": target.name}, 0)

    monkeypatch.setattr(fetch_oa, "_try_europepmc", later)
    result = fetch_oa._run_cascade(
        store,
        _stub(ref_id=ref_id, doi="10.1016/j.synthetic.2026.3"),
        tmp_path,
        "synthetic@example.invalid",
        "synthetic",
        "",
        "",
    )
    assert result == tmp_path / "smith2024example.pdf"
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT source,event,payload FROM ref_events WHERE ref_id=%s ORDER BY ts",
            (ref_id,),
        ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [
        ("fetcher:elsevier", "fetch_failed"),
        ("fetcher:europepmc", "fetch_ok"),
    ]
    assert rows[0][2]["reason"] == "entitlement"


def test_empty_body_with_tail_bibliography_is_not_fulltext():
    data = b"<full-text-retrieval-response><serial-item><body/><tail><bibliography><para>Synthetic citation.</para></bibliography></tail></serial-item></full-text-retrieval-response>"
    with pytest.raises(MarkupParseError):
        parse_elsevier(data)

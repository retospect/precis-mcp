"""Citation string -> DOI candidates (gr437652). No network: fetch is injected."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from precis.dispatch import Hub
from precis.handlers.paper import PaperHandler
from precis.ingest import crossref
from precis.ingest import crossref_citation as cc
from precis.response import Response


def _item(
    doi: str, vol: str, page: str, year: int, jt: str = "Physical Review B"
) -> dict[str, Any]:
    return {
        "DOI": doi,
        "title": ["A title"],
        "container-title": [jt],
        "volume": vol,
        "page": page,
        "issued": {"date-parts": [[year]]},
        "author": [{"family": "Kresse"}],
        "score": 50.0,
    }


def test_parse_citation() -> None:
    p = cc.parse_citation("Phys. Rev. B 47, 558 (1993)")
    assert p["year"] == "1993"
    assert p["volume"] == "47"
    assert p["page"] == "558"
    assert p["journal"].startswith("Phys. Rev. B")


def test_unambiguous_hit_has_full_confidence() -> None:
    items = [
        _item("10.1103/PhysRevB.47.558", "47", "558-561", 1993),
        _item("10.1103/other", "47", "999", 1993),
    ]
    cands = cc.resolve_citation(
        "Phys. Rev. B 47, 558 (1993)", fetch=lambda _c, _m: items
    )
    assert cands[0]["doi"] == "10.1103/physrevb.47.558"
    assert cands[0]["confidence"] == 1.0
    assert cands[1]["confidence"] < 1.0
    assert cc.pick_unambiguous(cands) == cands[0]


def test_weak_or_tied_matches_are_not_picked() -> None:
    weak = cc.resolve_citation(
        "Phys. Rev. B 47, 558 (1993)",
        fetch=lambda _c, _m: [_item("10.1/x", "12", "1", 2001)],
    )
    assert weak[0]["confidence"] < cc.ACCEPT_CONFIDENCE
    assert cc.pick_unambiguous(weak) is None
    tied = cc.resolve_citation(
        "Phys. Rev. B 47, 558 (1993)",
        fetch=lambda _c, _m: [
            _item("10.1/a", "47", "558", 1993),
            _item("10.1/b", "47", "558", 1993),
        ],
    )
    assert cc.pick_unambiguous(tied) is None


def test_empty_result() -> None:
    assert cc.resolve_citation("garbage", fetch=lambda _c, _m: []) == []


def _handler(monkeypatch: pytest.MonkeyPatch) -> tuple[PaperHandler, dict[str, Any]]:
    h = PaperHandler(hub=cast(Hub, SimpleNamespace(store=object(), embedder=None)))
    cap: dict[str, Any] = {}

    def fake_acquire(**kw: object) -> Response:
        cap.update(kw)
        return Response(body="minted")

    monkeypatch.setattr(h, "acquire", fake_acquire)
    return h, cap


def test_put_citation_stubs_single_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [_item("10.1103/PhysRevB.47.558", "47", "558", 1993)]
    real = cc.resolve_citation
    monkeypatch.setattr(
        cc, "resolve_citation", lambda c, m="": real(c, m, fetch=lambda _c, _m: items)
    )
    h, cap = _handler(monkeypatch)
    h.put(citation="Phys. Rev. B 47, 558 (1993)")
    assert cap["identifier"] == "doi:10.1103/physrevb.47.558"


def test_put_citation_weak_returns_candidates_no_mint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [_item("10.1/x", "12", "1", 2001)]
    real = cc.resolve_citation
    monkeypatch.setattr(
        cc, "resolve_citation", lambda c, m="": real(c, m, fetch=lambda _c, _m: items)
    )
    h, cap = _handler(monkeypatch)
    resp = h.put(citation="Phys. Rev. B 47, 558 (1993)")
    assert cap == {}
    assert "doi:10.1/x" in resp.body
    assert "conf=" in resp.body


_WILEY_CITATION = "Adv. Energy Mater. 14, 2302550 (2024)"
_WILEY_DOI = "10.1002/aenm.202302550"


@pytest.mark.parametrize(
    "citation", [_WILEY_CITATION, "Advanced Energy Materials 14, e2302550 (2024)"]
)
def test_put_wiley_citation_checks_constructed_doi_first(
    monkeypatch: pytest.MonkeyPatch, citation: str
) -> None:
    item = _item(_WILEY_DOI, "14", "", 2023, "Advanced Energy Materials")
    locator = cc.parse_citation(citation)["page"]
    item.update(
        title=["Ion Mobility in Crystalline Battery Materials"],
        author=[{"family": "Sotoudeh"}],
        **{"article-number": locator, "published-print": {"date-parts": [[2024]]}},
    )

    def lookup(doi: str, _mailto: str) -> dict[str, Any]:
        assert doi == _WILEY_DOI
        return item

    def unexpected_fuzzy(_citation: str, _mailto: str) -> list[dict[str, Any]]:
        pytest.fail("A verified DOI should bypass the bibliographic query")

    monkeypatch.setattr(crossref, "fetch_message", lookup)
    monkeypatch.setattr(cc, "_fetch_items", unexpected_fuzzy)
    h, cap = _handler(monkeypatch)
    h.put(args={"citation": citation})
    assert cap["identifier"] == f"doi:{_WILEY_DOI}"


@pytest.mark.parametrize("wrong_volume", [False, True])
def test_wiley_doi_miss_falls_back_and_downranks_front_matter(
    wrong_volume: bool,
) -> None:
    article = _item(_WILEY_DOI, "14", "", 2024, "Advanced Energy Materials")
    article["article-number"] = "2302550"
    wrong = {**article, "volume": "13"}
    masthead = _item(
        "10.1002/aenm.masthead", "14", "", 2024, article["container-title"][0]
    )
    masthead.pop("author")
    masthead["title"] = ["Masthead"]
    issue_info = {
        **masthead,
        "DOI": "10.1002/aenm.issue",
        "title": ["Issue Publication Information"],
    }
    candidates = cc.resolve_citation(
        _WILEY_CITATION,
        fetch=lambda _c, _m: [masthead, issue_info, article],
        fetch_doi=lambda _d, _m: wrong if wrong_volume else None,
    )
    assert len(candidates) == 3
    assert candidates[0]["doi"] == _WILEY_DOI
    assert candidates[0]["page"] == "2302550"
    assert cc.pick_unambiguous(candidates) == candidates[0]
    assert all(candidate["confidence"] < 0.75 for candidate in candidates[1:])


def test_wiley_bibliographic_override_skips_default_doi_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_lookup(_doi: str, _mailto: str) -> dict[str, Any]:
        pytest.fail("A bibliographic override must keep the default transport offline")

    monkeypatch.setattr(crossref, "fetch_message", unexpected_lookup)
    assert cc.resolve_citation(_WILEY_CITATION, fetch=lambda _c, _m: []) == []

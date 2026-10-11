"""Citation string -> DOI candidates (gr437652). No network: fetch is injected."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from precis.dispatch import Hub
from precis.handlers.paper import PaperHandler
from precis.ingest import crossref_citation as cc
from precis.response import Response


def _item(doi: str, vol: str, page: str, year: int, jt: str = "Physical Review B"):
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


def _handler(monkeypatch) -> tuple[PaperHandler, dict[str, Any]]:
    h = PaperHandler(hub=cast(Hub, SimpleNamespace(store=object(), embedder=None)))
    cap: dict[str, Any] = {}

    def fake_acquire(**kw: object) -> Response:
        cap.update(kw)
        return Response(body="minted")

    monkeypatch.setattr(h, "acquire", fake_acquire)
    return h, cap


def test_put_citation_stubs_single_hit(monkeypatch) -> None:
    items = [_item("10.1103/PhysRevB.47.558", "47", "558", 1993)]
    real = cc.resolve_citation
    monkeypatch.setattr(
        cc, "resolve_citation", lambda c, m="": real(c, m, fetch=lambda _c, _m: items)
    )
    h, cap = _handler(monkeypatch)
    h.put(citation="Phys. Rev. B 47, 558 (1993)")
    assert cap["identifier"] == "doi:10.1103/physrevb.47.558"


def test_put_citation_weak_returns_candidates_no_mint(monkeypatch) -> None:
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

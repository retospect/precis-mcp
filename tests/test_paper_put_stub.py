"""put(kind='paper') stub-mint adapter — DB-free mapping tests.

``PaperHandler.put`` is a thin adapter that folds the ``doi=`` / ``arxiv=``
conveniences into the canonical ``identifier=`` form and delegates to
:meth:`PaperHandler.acquire`. These tests pin that mapping without a
database by stubbing ``acquire`` and asserting what it receives. The
end-to-end mint (S2 enrich → ``upsert_stub_paper`` → fetch_oa) is
covered by the DB-backed ``test_acquire.py`` / ``test_stubs.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.response import Response


def _handler() -> PaperHandler:
    # __init__ only needs a non-None store + an embedder attribute.
    hub = SimpleNamespace(store=object(), embedder=None)
    return PaperHandler(hub=hub)  # type: ignore[arg-type]


def _capturing_handler() -> tuple[PaperHandler, dict]:
    h = _handler()
    captured: dict = {}

    def fake_acquire(**kw: object) -> Response:
        captured.update(kw)
        return Response(body="ok")

    h.acquire = fake_acquire  # type: ignore[method-assign]
    return h, captured


def test_put_maps_doi_to_identifier() -> None:
    h, cap = _capturing_handler()
    h.put(doi="10.1038/nature10352", title="X", year=2012)
    assert cap["identifier"] == "doi:10.1038/nature10352"
    assert cap["title"] == "X"
    assert cap["year"] == 2012


def test_put_maps_arxiv_to_identifier() -> None:
    h, cap = _capturing_handler()
    h.put(arxiv="2401.00001")
    assert cap["identifier"] == "arxiv:2401.00001"


def test_put_identifier_passthrough() -> None:
    h, cap = _capturing_handler()
    h.put(identifier="s2:abc123")
    assert cap["identifier"] == "s2:abc123"


def test_put_identifier_wins_over_doi() -> None:
    # An explicit identifier= takes precedence over the doi= convenience.
    h, cap = _capturing_handler()
    h.put(identifier="arxiv:2401.00001", doi="10.1/x")
    assert cap["identifier"] == "arxiv:2401.00001"


def test_put_title_only_backlog_stub() -> None:
    h, cap = _capturing_handler()
    h.put(title="Some niche paper")
    assert cap["identifier"] is None
    assert cap["title"] == "Some niche paper"


def test_put_requires_an_identifier_or_title() -> None:
    h = _handler()
    with pytest.raises(BadInput):
        h.put()
    with pytest.raises(BadInput):
        h.put(doi="   ", title="   ")

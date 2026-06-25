"""The per-heading style picker's data path: the genre (doc_type) scopes
which section styles are offered (ADR 0037)."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis_web.routes.drafts import _doc_type, _section_styles_for


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _draft_with_doctype(hub: Hub, doc_type: str | None) -> object:
    ws = {"path": "p"}
    if doc_type:
        ws["doc_type"] = doc_type
    proj = hub.store.insert_ref(
        kind="todo", slug=None, title="Proj", meta={"workspace": ws}
    ).id
    DraftHandler(hub=hub).put(id="d", title="Doc", project=proj)
    return hub.store.get_ref(kind="draft", id="d")


def test_patent_doctype_offers_patent_styles(draft: DraftHandler, hub: Hub) -> None:
    ref = _draft_with_doctype(hub, "patent")
    assert _doc_type(hub.store, ref) == "patent"
    slugs = [s for s, _ in _section_styles_for(hub.store, ref)]
    assert "patent-claim" in slugs and "patent-image-part" in slugs
    assert all(s.startswith("patent-") for s in slugs)


def test_paper_doctype_offers_sci_styles(draft: DraftHandler, hub: Hub) -> None:
    ref = _draft_with_doctype(hub, "paper")
    slugs = [s for s, _ in _section_styles_for(hub.store, ref)]
    assert "sci-methods" in slugs and "sci-abstract" in slugs


def test_unknown_doctype_offers_nothing(draft: DraftHandler, hub: Hub) -> None:
    ref = _draft_with_doctype(hub, None)
    assert _section_styles_for(hub.store, ref) == []

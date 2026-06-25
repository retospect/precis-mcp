"""scaffold_sections lays down a genre's styled section skeleton (ADR 0037
step 4): pick a doc_type → its standard headings appear, each meta.style'd."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis_web.routes.drafts import _SCAFFOLDS


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _new_draft(hub: Hub, slug: str) -> int:
    proj = hub.store.insert_ref(kind="todo", slug=None, title="P").id
    DraftHandler(hub=hub).put(id=slug, title="Paperclip", project=proj)
    ref = hub.store.get_ref(kind="draft", id=slug)
    assert ref is not None
    return ref.id


def test_scaffold_patent_sections(draft: DraftHandler, hub: Hub) -> None:
    ref_id = _new_draft(hub, "pat")
    sections = _SCAFFOLDS["patent"]
    created = hub.store.scaffold_sections(ref_id, sections)
    assert len(created) == len(sections)

    ro = hub.store.reading_order(ref_id)
    # the auto-minted title heading, then the scaffolded sections in order
    assert ro[0].text == "Paperclip"
    assert [c.text for c in ro[1:]] == [t for t, _ in sections]
    # each scaffolded section is a heading carrying its style
    for c, (_title, style) in zip(ro[1:], sections):
        assert c.chunk_kind == "heading"
        assert hub.store.section_style_for(c.handle) == style


def test_scaffold_empty_is_noop(draft: DraftHandler, hub: Hub) -> None:
    ref_id = _new_draft(hub, "d")
    before = len(hub.store.reading_order(ref_id))
    assert hub.store.scaffold_sections(ref_id, []) == []
    assert len(hub.store.reading_order(ref_id)) == before


def test_paper_scaffold_uses_sci_styles(draft: DraftHandler, hub: Hub) -> None:
    ref_id = _new_draft(hub, "rp")
    hub.store.scaffold_sections(ref_id, _SCAFFOLDS["paper"])
    ro = hub.store.reading_order(ref_id)
    styles = [hub.store.section_style_for(c.handle) for c in ro[1:]]
    assert "sci-methods" in styles and "sci-abstract" in styles

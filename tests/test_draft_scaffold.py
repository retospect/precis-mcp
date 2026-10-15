"""scaffold_sections lays down a genre's styled section skeleton: pick a doc_type → its standard headings appear, each meta.style'd."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis_web.routes.drafts import _SCAFFOLDS


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _new_draft(hub: Hub, slug: str) -> int:
    proj = hub.live_store.insert_ref(kind="todo", slug=None, title="P").id
    DraftHandler(hub=hub).put(id=slug, title="Paperclip", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    return ref.id


def test_scaffold_patent_sections(draft: DraftHandler, hub: Hub) -> None:
    ref_id = _new_draft(hub, "pat")
    sections = _SCAFFOLDS["patent"]
    created = hub.live_store.drafts.scaffold_sections(ref_id, sections)
    assert len(created) == len(sections)

    ro = hub.live_store.drafts.reading_order(ref_id)
    # the auto-minted title heading, then the scaffolded sections in order
    assert ro[0].text == "Paperclip"
    assert [c.text for c in ro[1:]] == [s[0] for s in sections]
    # each scaffolded section is a heading carrying its style
    for c, s in zip(ro[1:], sections):
        assert c.chunk_kind == "heading"
        assert hub.live_store.drafts.section_style_for(c.handle) == s[1]


def test_scaffold_empty_is_noop(draft: DraftHandler, hub: Hub) -> None:
    ref_id = _new_draft(hub, "d")
    before = len(hub.live_store.drafts.reading_order(ref_id))
    assert hub.live_store.drafts.scaffold_sections(ref_id, []) == []
    assert len(hub.live_store.drafts.reading_order(ref_id)) == before


def test_paper_scaffold_uses_sci_styles(draft: DraftHandler, hub: Hub) -> None:
    ref_id = _new_draft(hub, "rp")
    hub.live_store.drafts.scaffold_sections(ref_id, _SCAFFOLDS["paper"])
    ro = hub.live_store.drafts.reading_order(ref_id)
    styles = [hub.live_store.drafts.section_style_for(c.handle) for c in ro[1:]]
    assert "sci-methods" in styles and "sci-abstract" in styles


def test_poster_scaffold_carries_column_hints(draft: DraftHandler, hub: Hub) -> None:
    """The poster scaffold lays down blocks with a ``meta.poster_column``
    hint (1|2|3) — the export packs them into the template's three
    stretch columns."""
    from precis.draft.scaffolds import DOC_TYPE_BRIEF

    assert "poster" in DOC_TYPE_BRIEF  # /drafts/new + edit(scaffold=) pick it up
    ref_id = _new_draft(hub, "post")
    created = hub.live_store.drafts.scaffold_sections(ref_id, _SCAFFOLDS["poster"])
    assert len(created) == len(_SCAFFOLDS["poster"])

    ro = hub.live_store.drafts.reading_order(ref_id)
    heads = [c for c in ro[1:] if c.chunk_kind == "heading"]
    assert [c.text for c in heads] == [t for t, *_ in _SCAFFOLDS["poster"]]
    cols = [(c.meta or {}).get("poster_column") for c in heads]
    assert cols == [1, 1, 2, 2, 3, 3]
    assert set(cols) <= {1, 2, 3}
    # the title heading itself carries no hint
    assert (ro[0].meta or {}).get("poster_column") is None


def test_poster_scaffold_via_edit_surface(draft: DraftHandler, hub: Hub) -> None:
    """edit(kind='draft', scaffold='poster') materialises the same skeleton
    /drafts/new would (MCP path, DraftHandler.edit)."""
    _new_draft(hub, "pe")
    r = draft.edit(id="pe", scaffold="poster")
    assert "poster" in r.body and "scaffolded" in r.body
    ref = hub.live_store.get_ref(kind="draft", id="pe")
    assert ref is not None
    ro = hub.live_store.drafts.reading_order(ref.id)
    heads = [c for c in ro if c.chunk_kind == "heading"]
    assert {c.text for c in heads} >= {"Introduction", "Results", "References"}

"""The planner injects the nearest enclosing section style (ADR 0037/0038).

`store.section_style_for` walks parent_chunk_id to the nearest styled
heading; `_render_section_style` resolves a change-request's `meta.anchor`,
finds that style, and injects the style skill's body.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.workers.planner_prompt import _render_section_style


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _proj(hub: Hub) -> int:
    return hub.store.insert_ref(kind="todo", slug=None, title="Proj").id


def _build(hub: Hub, draft: DraftHandler):
    """Draft 'pat' → title, a Claims heading, a paragraph under Claims."""
    draft.put(id="pat", title="Paperclip", project=_proj(hub))
    ref = hub.store.get_ref(kind="draft", id="pat")
    title = hub.store.reading_order(ref.id)[0]
    draft.put(
        id="pat",
        chunk_kind="heading",
        text="Claims",
        at={"after": "¶" + title.handle},
    )
    claims = hub.store.reading_order(ref.id)[1]
    draft.put(
        id="pat",
        chunk_kind="paragraph",
        text="A clip.",
        at={"into": "¶" + claims.handle, "last": True},
    )
    para = hub.store.reading_order(ref.id)[2]
    return claims, para


def test_section_style_for_walks_to_nearest_heading(
    draft: DraftHandler, hub: Hub
) -> None:
    claims, para = _build(hub, draft)
    draft.edit(id=claims.dc, style="patent-claim")
    assert hub.store.section_style_for(para.handle) == "patent-claim"
    assert hub.store.section_style_for(claims.handle) == "patent-claim"  # self


def test_section_style_for_none_when_unstyled(draft: DraftHandler, hub: Hub) -> None:
    _claims, para = _build(hub, draft)
    assert hub.store.section_style_for(para.handle) is None


def test_planner_injects_section_style_body(draft: DraftHandler, hub: Hub) -> None:
    claims, para = _build(hub, draft)
    # a real shipped skill, so the body loads end-to-end
    draft.edit(id=claims.dc, style="precis-draft-help")
    cr = hub.store.insert_ref(
        kind="todo", slug=None, title="cr", meta={"anchor": para.dc}
    ).id
    block = _render_section_style(hub.store, cr)
    assert "Section style — precis-draft-help" in block
    assert len(block) > 200  # the skill body was injected, not just a header


def test_planner_no_block_when_unstyled(draft: DraftHandler, hub: Hub) -> None:
    _claims, para = _build(hub, draft)
    cr = hub.store.insert_ref(
        kind="todo", slug=None, title="cr", meta={"anchor": para.dc}
    ).id
    assert _render_section_style(hub.store, cr) == ""


def test_patent_claim_style_skill_injects_real_body(
    draft: DraftHandler, hub: Hub
) -> None:
    """The shipped patent-claim style skill (step 3) loads as a real body —
    not the missing-skill pointer."""
    claims, para = _build(hub, draft)
    draft.edit(id=claims.dc, style="patent-claim")
    cr = hub.store.insert_ref(
        kind="todo", slug=None, title="cr", meta={"anchor": para.dc}
    ).id
    block = _render_section_style(hub.store, cr)
    assert "Section style — patent-claim" in block
    assert "antecedent basis" in block  # body content, not the pointer
    assert "get(kind='skill'" not in block  # i.e. it did NOT fall back

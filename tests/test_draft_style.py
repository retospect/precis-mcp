"""edit(style=) sets a heading's section style (ADR 0037).

A section style is a skill slug stored in ``meta.style`` on a *heading*
chunk. Setting it is metadata-only (no text change → no re-embed), and it
is rejected on a non-heading chunk.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.draft import DraftHandler


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _proj(hub: Hub) -> int:
    return hub.store.insert_ref(kind="todo", slug=None, title="Proj").id


def _second_chunk(hub: Hub, slug: str, *, kind: str, text: str):
    """Add a chunk after the auto-minted title heading, return it."""
    ref = hub.store.get_ref(kind="draft", id=slug)
    title = hub.store.reading_order(ref.id)[0]
    DraftHandler(hub=hub).put(
        id=slug, chunk_kind=kind, text=text, at={"after": "¶" + title.handle}
    )
    return hub.store.reading_order(ref.id)[1]


def test_set_and_clear_heading_style(draft: DraftHandler, hub: Hub) -> None:
    draft.put(id="pat", title="Paperclip", project=_proj(hub))
    claims = _second_chunk(hub, "pat", kind="heading", text="Claims")
    text_before = hub.store.get_draft_chunk(claims.handle).text

    draft.edit(id=claims.dc, style="patent-claim")
    assert hub.store.draft_chunk_meta(claims.handle).get("style") == "patent-claim"
    # metadata-only: the heading text is untouched (so content_sha / embedding
    # never re-derive).
    assert hub.store.get_draft_chunk(claims.handle).text == text_before

    draft.edit(id=claims.dc, style="")
    assert "style" not in hub.store.draft_chunk_meta(claims.handle)


def test_style_rejected_on_non_heading(draft: DraftHandler, hub: Hub) -> None:
    draft.put(id="pat", title="Paperclip", project=_proj(hub))
    para = _second_chunk(hub, "pat", kind="paragraph", text="A clip with a ball.")
    with pytest.raises(BadInput):
        draft.edit(id=para.dc, style="patent-claim")

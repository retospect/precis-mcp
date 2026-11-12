"""``put(kind='draft', chunk_kind=<unknown>)`` is a BadInput, not an FK Internal (gr648737)."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.draft import DraftHandler


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _mk(hub: Hub, draft: DraftHandler) -> None:
    proj = hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id="uk", title="T", project=proj)


def test_unknown_chunk_kind_is_bad_input(draft: DraftHandler, hub: Hub) -> None:
    _mk(hub, draft)
    with pytest.raises(BadInput, match="not a draft chunk kind"):
        draft.put(id="uk", chunk_kind="paper", text="A survey paragraph.")


def test_card_chunk_kind_is_bad_input(draft: DraftHandler, hub: Hub) -> None:
    _mk(hub, draft)
    with pytest.raises(BadInput, match="not a draft chunk kind"):
        draft.put(id="uk", chunk_kind="card_title", text="A title.")


def test_paragraph_still_lands(draft: DraftHandler, hub: Hub) -> None:
    _mk(hub, draft)
    out = draft.put(id="uk", chunk_kind="paragraph", text="Fine paragraph.")
    assert "added 1 chunk" in str(out)

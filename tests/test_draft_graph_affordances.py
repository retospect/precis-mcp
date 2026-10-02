"""Draft graph affordances (docs/backlog/draft-authoring-graph-affordances.md):
``view='history'`` over ``chunk_events``, the landed ``sha:<12>`` on a text
edit's ack, and ``view='proposals'`` over anchored todos carrying
``meta.proposed_text``."""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.draft import DraftHandler
from precis.store._draft_ops import DraftChunk, content_sha
from precis.store.store import Store
from precis.store.types import Tag


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _paragraph(store: Store) -> DraftChunk:
    proj = store.insert_ref(kind="todo", slug=None, title="Affordance project").id
    ref, title = store.drafts.create_draft(name="ga", title="T", project_ref_id=proj)
    return store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="first version",
        at={"after": title.handle},
    )[0]


def _ack_sha(body: str) -> str:
    m = re.search(r"\nsha:([0-9a-f]{12})$", body)
    assert m is not None, body
    return m.group(1)


# ---------------------------------------------------------------------------
# AC 1 — view='history'
# ---------------------------------------------------------------------------


def test_history_lists_text_events_newest_first(draft: DraftHandler, hub: Hub) -> None:
    p = _paragraph(hub.live_store)
    draft.edit(id=p.dc, text="second version")
    draft.edit(id=p.dc, text="third version")

    out = draft.get(id=p.dc, view="history").body
    assert out.startswith(f"# {p.dc} — history")
    # newest first: the latest edit's prior text precedes the earlier one's
    assert out.index("second version") < out.index("first version")
    assert content_sha("third version")[:12] in out
    assert content_sha("second version")[:12] in out
    assert re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", out)


def test_history_limit_caps_rows_and_validates(draft: DraftHandler, hub: Hub) -> None:
    p = _paragraph(hub.live_store)
    for i in range(3):
        draft.edit(id=p.dc, text=f"v{i}")
    out = draft.get(id=p.dc, view="history", args={"limit": 1}).body
    assert "showing the newest 1" in out
    assert content_sha("v2")[:12] in out and content_sha("v1")[:12] not in out
    with pytest.raises(BadInput):
        draft.get(id=p.dc, view="history", args={"limit": 501})


def test_history_unknown_chunk_is_not_found(draft: DraftHandler) -> None:
    with pytest.raises(NotFound):
        draft.get(id="dc999999999", view="history")


def test_history_on_whole_draft_is_refused(draft: DraftHandler, hub: Hub) -> None:
    _paragraph(hub.live_store)
    with pytest.raises(BadInput) as ei:
        draft.get(id="ga", view="history")
    assert "dc123" in (ei.value.next or "")


# ---------------------------------------------------------------------------
# AC 2 — the edit ack carries the landed sha
# ---------------------------------------------------------------------------


def test_edit_ack_sha_chains_and_stale_sha_is_refused(
    draft: DraftHandler, hub: Hub
) -> None:
    p = _paragraph(hub.live_store)
    sha1 = _ack_sha(draft.edit(id=p.dc, text="second version").body)
    assert sha1 == content_sha("second version")[:12]

    sha2 = _ack_sha(draft.edit(id=p.dc, text="third version", base_sha=sha1).body)
    assert sha2 == content_sha("third version")[:12]

    with pytest.raises(BadInput, match="changed since you read it"):
        draft.edit(id=p.dc, text="clobber", base_sha=sha1)


def test_find_replace_ack_carries_landed_sha(draft: DraftHandler, hub: Hub) -> None:
    p = _paragraph(hub.live_store)
    body = draft.edit(id=p.dc, find="first", text="1st").body
    assert _ack_sha(body) == content_sha("1st version")[:12]


# ---------------------------------------------------------------------------
# AC 3 — view='proposals'
# ---------------------------------------------------------------------------


def test_proposals_show_open_anchored_todos_as_diffs(
    draft: DraftHandler, hub: Hub
) -> None:
    store = hub.live_store
    p = _paragraph(store)
    assert "no open proposals" in draft.get(id=p.dc, view="proposals").body

    todo = store.insert_ref(
        kind="todo",
        slug=None,
        title="tighten",
        meta={"anchor": p.dc, "proposed_text": "tighter version"},
    )
    # an anchored change request without proposed_text is not a proposal
    store.insert_ref(kind="todo", slug=None, title="plain", meta={"anchor": p.dc})

    out = draft.get(id=p.dc, view="proposals").body
    assert f"td{todo.id} — tighten" in out
    assert "-first version" in out and "+tighter version" in out
    assert "plain" not in out

    store.add_tag(todo.id, Tag.closed("STATUS", "done"), set_by="agent")
    assert "no open proposals" in draft.get(id=p.dc, view="proposals").body

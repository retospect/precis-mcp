"""Draft edit find-replace: substitute within a chunk, never clobber (gr48203)."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.draft import DraftHandler
from precis.store.store import Store


def _draft_with_para(store: Store) -> tuple[DraftHandler, str]:
    proj = store.insert_ref(kind="todo", slug=None, title="proj").id
    ref, title = store.create_draft(name="frlst", title="Title", project_ref_id=proj)
    para = store.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="The temperature is 60°C in this section and everywhere.",
        at={"last": True},
    )[0]
    return DraftHandler(hub=Hub(store=store)), para.dc


def _draft_with_item(store: Store, text: str) -> tuple[DraftHandler, str]:
    """A list item chunk — the shape that surfaced gr45083 as a ValueError."""
    proj = store.insert_ref(kind="todo", slug=None, title="proj").id
    ref, title = store.create_draft(name="frit", title="Title", project_ref_id=proj)
    ul = store.add_chunks(
        ref_id=ref.id, chunk_kind="ulist", text="", at={"after": title.handle}
    )[0]
    item = store.add_chunks(
        ref_id=ref.id,
        chunk_kind="item",
        text=text,
        at={"into": ul.handle, "last": True},
    )[0]
    return DraftHandler(hub=Hub(store=store)), item.dc


def _text(store: Store, dc: str) -> str:
    c = store.get_draft_chunk(dc)
    assert c is not None
    return c.text


def test_find_replace_on_list_item(store: Store) -> None:
    # gr45083: edit(id='dc1529074', find='175 °C', text='175°C') on a
    # chunk_kind='item' raised ValueError on old code (find= swallowed →
    # the item path errored). Same find= root cause as gr48203; the fix
    # covers list items too — substitute in place, don't clobber.
    h, dc = _draft_with_item(store, "Insulation rated for 175 °C operation.")
    h.edit(id=dc, find="175 °C", text="175°C")
    assert _text(store, dc) == "Insulation rated for 175°C operation."


def test_find_replace_substitutes_within_chunk(store: Store) -> None:
    h, dc = _draft_with_para(store)
    resp = h.edit(id=dc, mode="find-replace", find="60°C", text="65°C")
    assert "edited" in resp.body
    # the surrounding text survived — only the span changed (gr48203)
    assert _text(store, dc) == "The temperature is 65°C in this section and everywhere."


def test_find_present_via_find_kwarg_alone(store: Store) -> None:
    h, dc = _draft_with_para(store)
    # find= alone (no mode=) also triggers substitution, not full replace
    h.edit(id=dc, find="temperature", text="temp")
    assert _text(store, dc) == "The temp is 60°C in this section and everywhere."


def test_find_absent_does_not_clobber(store: Store) -> None:
    h, dc = _draft_with_para(store)
    before = _text(store, dc)
    with pytest.raises(NotFound):
        h.edit(id=dc, mode="find-replace", find="NOT PRESENT", text="x")
    assert _text(store, dc) == before  # chunk untouched


def test_find_with_no_text_errors(store: Store) -> None:
    h, dc = _draft_with_para(store)
    with pytest.raises(BadInput):
        h.edit(id=dc, find="temperature")  # find= but no text=


def test_empty_find_errors(store: Store) -> None:
    h, dc = _draft_with_para(store)
    with pytest.raises(BadInput):
        h.edit(id=dc, find="", text="x")


def test_delete_span_with_empty_text(store: Store) -> None:
    h, dc = _draft_with_para(store)
    h.edit(id=dc, find=" in this section and everywhere", text="")
    assert _text(store, dc) == "The temperature is 60°C."


def test_plain_text_still_full_replaces(store: Store) -> None:
    h, dc = _draft_with_para(store)
    # no find → wholesale replace, even though the wire defaults
    # mode='find-replace' on every edit (that must NOT force find=).
    h.edit(id=dc, mode="find-replace", text="brand new content")
    assert _text(store, dc) == "brand new content"

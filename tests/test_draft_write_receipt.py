"""Post-commit advisory failures must not invite duplicate draft writes (gr685122)."""

from __future__ import annotations

import pytest
from psycopg import OperationalError

from precis.dispatch import Hub
from precis.draft import lint
from precis.handlers.draft import DraftHandler
from precis.store._draft_ops import content_sha
from precis.store.store import Store


@pytest.mark.parametrize("hint", ["write_abbrev_hints", "sentence_start_hint"])
@pytest.mark.parametrize("operation", ["put", "rewrite", "find", "replace-content"])
def test_committed_text_keeps_receipt_when_hints_lose_connection(
    store: Store, monkeypatch: pytest.MonkeyPatch, hint: str, operation: str
) -> None:
    project = store.insert_ref(kind="todo", slug=None, title="Project")
    ref, _ = store.drafts.create_draft(
        name="receipt", title="Receipt", project_ref_id=project.id
    )
    chunk = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="Original prose."
    )[0]
    handler = DraftHandler(hub=Hub(store=store))

    def disconnected(*args: object, **kwargs: object) -> str:
        raise OperationalError("the connection is closed")

    monkeypatch.setattr(lint, hint, disconnected)
    text = "Revised prose."
    if operation == "put":
        response = handler.put(id=ref.slug, chunk_kind="paragraph", text=text)
    elif operation == "find":
        response = handler.edit(id=chunk.dc, find="Original", text="Revised")
    elif operation == "replace-content":
        response = handler.edit(id=chunk.dc, mode="replace-content", text=text)
    else:
        response = handler.edit(id=chunk.dc, text=text)

    assert "Write committed; advisory hints unavailable" in response.body
    assert "Do not retry" in response.body
    chunks = store.drafts.reading_order(ref.id)
    landed = [c for c in chunks if c.text == text]
    assert len(landed) == 1
    assert landed[0].dc in response.body
    if operation != "put":
        assert f"sha:{content_sha(text)[:12]}" in response.body


def test_failed_mutation_still_raises(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = store.insert_ref(kind="todo", slug=None, title="Project")
    ref, _ = store.drafts.create_draft(
        name="receipt", title="Receipt", project_ref_id=project.id
    )
    handler = DraftHandler(hub=Hub(store=store))

    def disconnected(*args: object, **kwargs: object) -> None:
        raise OperationalError("the connection is closed")

    monkeypatch.setattr(store.drafts, "add_chunks", disconnected)
    with pytest.raises(OperationalError, match="connection is closed"):
        handler.put(id=ref.slug, chunk_kind="paragraph", text="New prose.")
    assert [c.text for c in store.drafts.reading_order(ref.id)] == ["Receipt"]


def test_edit_keeps_revision_when_receipt_ref_lookup_fails(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = store.insert_ref(kind="todo", slug=None, title="Project")
    ref, _ = store.drafts.create_draft(
        name="receipt", title="Receipt", project_ref_id=project.id
    )
    chunk = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="Original prose."
    )[0]
    handler = DraftHandler(hub=Hub(store=store))
    edit_text = store.drafts.edit_text

    def disconnected(*args: object, **kwargs: object) -> None:
        raise OperationalError("the connection is closed")

    def edit_then_disconnect(*args: object, **kwargs: object) -> object:
        # The real store mutation commits before the optional receipt read.
        result = edit_text(chunk.handle, "Revised prose.")
        monkeypatch.setattr(store, "get_ref", disconnected)
        return result

    monkeypatch.setattr(store.drafts, "edit_text", edit_then_disconnect)
    response = handler.edit(id=chunk.dc, text="Revised prose.")
    assert f"edited {chunk.dc}" in response.body
    assert "Write committed; advisory hints unavailable" in response.body
    assert f"sha:{content_sha('Revised prose.')[:12]}" in response.body
    landed = store.drafts.get_draft_chunk(chunk.dc)
    assert landed is not None and landed.text == "Revised prose."


def test_put_does_not_read_chunk_count_for_logging(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = store.insert_ref(kind="todo", slug=None, title="Project")
    ref, _ = store.drafts.create_draft(
        name="receipt", title="Receipt", project_ref_id=project.id
    )
    handler = DraftHandler(hub=Hub(store=store))

    def disconnected(*args: object, **kwargs: object) -> None:
        raise OperationalError("the connection is closed")

    # Link sync already treats this read as best-effort. Logging must too.
    monkeypatch.setattr(store.drafts, "chunk_ord_map", disconnected)
    response = handler.put(id=ref.slug, chunk_kind="code", text="print(1)")
    assert "added 1 chunk" in response.body
    chunks = store.drafts.reading_order(ref.id)
    assert len([c for c in chunks if c.text == "print(1)"]) == 1

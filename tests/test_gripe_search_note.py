"""Gripe search discloses loose OR matching in its scope warning."""

from precis.dispatch import Hub
from precis.handlers.gripe import GripeHandler
from precis.handlers.todo import TodoHandler
from precis.store import ChunkInsert


def test_note_names_or_fallback(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    for title, text in [
        ("matching", "copper nitrate reduction"),
        ("unrelated", "bananas oranges apples"),
    ]:
        result = handler.put(text=title, body=text, new=True)
        assert result.ref_id is not None
    loose = handler.search(q="copper nitrate missing", mode="lexical").body
    assert "q ran as ANY of its 3 words (no entry holds all of them)" in loose
    assert "loose superset" in loose
    assert (
        "next: for all-words matching quote the phrase or cut q to 1-2 words" in loose
    )
    assert "here q filters" not in loose
    strict = handler.search(q="copper nitrate reduction", mode="lexical").body
    assert "here q filters, it does not just order" in strict
    assert "q ran as ANY" not in strict

    # Cover the case where OR matches the whole scope, and unscoped callers.
    for tags in (None, ["STATUS:open"]):
        note = handler._tag_truncation_note(
            tags, matched_total=2, q="copper nitrate missing", or_fallback=True
        )
        assert "q ran as ANY of its 3 words" in note


def test_title_path_names_or_fallback(hub: Hub) -> None:
    """Todo chunk recall must disclose OR matching through ref fusion."""
    handler = TodoHandler(hub=hub)
    for title, text in [
        ("matching", "copper nitrate reduction"),
        ("unrelated", "bananas oranges apples"),
    ]:
        result = handler.put(text=title)
        assert result.ref_id is not None
        handler.store.chunks.insert_chunks(
            result.ref_id, [ChunkInsert(ord=0, text=text)]
        )
    loose = handler.search(
        q="copper nitrate missing", mode="lexical", tags=["STATUS:open"]
    ).body
    assert "q ran as ANY of its 3 words" in loose
    assert "here q filters" not in loose
    strict = handler.search(
        q="copper nitrate reduction", mode="lexical", tags=["STATUS:open"]
    ).body
    assert "q ran as ANY" not in strict
    assert "here q filters" in strict


def test_strict_title_match_keeps_scope_note_when_chunks_retry(hub: Hub) -> None:
    handler = TodoHandler(hub=hub)
    q = "copper nitrate reduction"
    title_match = handler.put(text=q)
    partial_match = handler.put(text="partial body match")
    handler.put(text="unrelated todo")
    assert partial_match.ref_id is not None
    handler.store.chunks.insert_chunks(
        partial_match.ref_id, [ChunkInsert(ord=0, text="copper")]
    )

    result = handler.search(q=q, mode="lexical", tags=["STATUS:open"])
    assert title_match.ref_id is not None
    assert f"td{title_match.ref_id}" in result.body
    assert f"td{partial_match.ref_id}" in result.body
    assert "here q filters" in result.body
    assert "q ran as ANY" not in result.body

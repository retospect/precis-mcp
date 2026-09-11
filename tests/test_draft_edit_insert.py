"""Draft edit mode='insert': the anchor (find=) must survive verbatim
(gr334153 — DATA LOSS). Before the fix, mode='insert' was silently
dropped by DraftHandler.edit's `**_kw` catch-all and fell through to
the plain find-replace path (`old_text.replace(find, text)`), which
overwrites the matched span with `text` alone instead of splicing
`text` alongside it — deleting the anchor (and, when the anchor ended
in bracket citations like `[pc123][pc456]`, deleting those citations
too)."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.draft import DraftHandler
from precis.store.store import Store


def _draft_with_para(store: Store, text: str) -> tuple[DraftHandler, str, int]:
    proj = store.insert_ref(kind="todo", slug=None, title="proj").id
    ref, title = store.drafts.create_draft(
        name="ins", title="Title", project_ref_id=proj
    )
    para = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text=text,
        at={"last": True},
    )[0]
    return DraftHandler(hub=Hub(store=store)), para.dc, para.chunk_id


def _text(store: Store, dc: str) -> str:
    c = store.drafts.get_draft_chunk(dc)
    assert c is not None
    return c.text


def _events(store: Store, chunk_id: int) -> list[tuple[str, str | None]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT event_kind, prev_text FROM chunk_events "
            "WHERE chunk_id=%s ORDER BY event_id",
            (chunk_id,),
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def test_insert_after_preserves_bracket_citation_anchor(store: Store) -> None:
    # The exact shape gr334153 reported: an anchor ending in two bracket
    # citations, inserting a new sentence after it.
    original = (
        "The classification remains unresolved [pc3346195][pc356616]. "
        "A separate finding covers the follow-up."
    )
    h, dc, chunk_id = _draft_with_para(store, original)
    anchor = "The classification remains unresolved [pc3346195][pc356616]."
    resp = h.edit(
        id=dc,
        mode="insert",
        find=anchor,
        text=" We resolve this ambiguity in section 3.",
        where="after",
    )
    new_text = _text(store, dc)
    # The anchor AND its citations survive verbatim.
    assert anchor in new_text
    assert "[pc3346195]" in new_text
    assert "[pc356616]" in new_text
    # The new sentence lands immediately after the anchor.
    assert new_text == (
        "The classification remains unresolved [pc3346195][pc356616]. "
        "We resolve this ambiguity in section 3. "
        "A separate finding covers the follow-up."
    )
    # No double space where the anchor used to sit.
    assert "  " not in new_text
    assert "inserted" in resp.body

    # chunk_events still logs the edit (the re-embed cascade trigger).
    events = _events(store, chunk_id)
    assert events[-1][0] == "edited"
    assert events[-1][1] == original


def test_insert_before_preserves_anchor(store: Store) -> None:
    original = "Baseline established [pc1000]. Results follow below."
    h, dc, _chunk_id = _draft_with_para(store, original)
    anchor = "Results follow below."
    h.edit(
        id=dc,
        mode="insert",
        find=anchor,
        text="First, a caveat applies. ",
        where="before",
    )
    new_text = _text(store, dc)
    assert anchor in new_text
    assert "[pc1000]" in new_text
    assert new_text == (
        "Baseline established [pc1000]. First, a caveat applies. Results follow below."
    )


def test_insert_anchor_with_regex_metacharacters_is_literal(store: Store) -> None:
    # The anchor is spliced back in with plain string concatenation, not
    # re.sub — backslashes/brackets/dots/parens in find= must never be
    # reinterpreted as a pattern or a backreference.
    original = "Unit cost was $5.00 (approx.) per part [a+b]* today."
    h, dc, _chunk_id = _draft_with_para(store, original)
    anchor = "$5.00 (approx.) per part [a+b]*"
    h.edit(
        id=dc,
        mode="insert",
        find=anchor,
        text=" (revised)",
        where="after",
    )
    new_text = _text(store, dc)
    assert anchor in new_text
    assert new_text == (
        "Unit cost was $5.00 (approx.) per part [a+b]* (revised) today."
    )


def test_insert_requires_where(store: Store) -> None:
    h, dc, _chunk_id = _draft_with_para(store, "Some text [pc1].")
    with pytest.raises(BadInput):
        h.edit(id=dc, mode="insert", find="[pc1]", text=" more")


def test_insert_rejects_bad_where(store: Store) -> None:
    h, dc, _chunk_id = _draft_with_para(store, "Some text [pc1].")
    with pytest.raises(BadInput):
        h.edit(id=dc, mode="insert", find="[pc1]", text=" more", where="beside")


def test_where_without_insert_mode_rejected(store: Store) -> None:
    h, dc, _chunk_id = _draft_with_para(store, "Some text [pc1].")
    with pytest.raises(BadInput):
        h.edit(id=dc, find="[pc1]", text="[pc2]", where="after")


def test_insert_find_absent_does_not_clobber(store: Store) -> None:
    h, dc, _chunk_id = _draft_with_para(store, "Some text [pc1].")
    before = _text(store, dc)
    with pytest.raises(NotFound):
        h.edit(id=dc, mode="insert", find="NOT PRESENT", text=" x", where="after")
    assert _text(store, dc) == before

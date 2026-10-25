"""DraftHandler — ``edit(mode='replace-content')`` (gr514338).

Replace a block's OWN content while its child subsections keep their
handles: for a heading, the span is the heading text plus its direct
body (the non-heading children up to the first child heading); for any
other chunk, just its text.
"""

from __future__ import annotations

import base64
import re
import struct
import zlib
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.draft import DraftHandler
from precis.store._draft_ops import own_content_sha


def _png_1x1_b64() -> str:
    """A real 1×1 PNG, built at runtime — a base64 literal trips the
    credential gate's high-entropy scan (the bytes are content, not a
    secret). Valid enough for the figure path's dimension probe."""

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00\x00\x00")
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode()


_PNG_B64 = _png_1x1_b64()


def _dc(body: str) -> str:
    m = re.search(r"dc\d+", body)
    assert m is not None, f"no dc handle in {body!r}"
    return m.group(0)


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _proj(hub: Hub) -> int:
    return hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id


def _order(hub: Hub, slug: str) -> list:
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    return hub.live_store.drafts.reading_order(ref.id)


def _chunk(hub: Hub, dc: str) -> Any:
    c = hub.live_store.drafts.get_draft_chunk(dc)
    assert c is not None
    return c


def _handle_of(hub: Hub, slug: str, text: str) -> str:
    """The dc handle of the live chunk carrying ``text``."""
    c = next(c for c in _order(hub, slug) if c.text == text)
    return c.dc


def _span_sha(hub: Hub, dc: str) -> str:
    chunk = _chunk(hub, dc)
    own = hub.live_store.drafts.block_own_chunks(chunk)
    return own_content_sha(c.text or "" for c in own)


def _build_sectioned(hub: Hub, draft: DraftHandler) -> dict[str, str]:
    """nt = T ▸ Intro (intro para, list, closing para ▸ Child A ▸ Child B)
    — the shape the replace-content span is defined against."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_dc = _order(hub, "nt")[0].dc
    draft.put(
        id="nt",
        chunk_kind="heading",
        text="Intro",
        at={"after": title_dc},
    )
    intro = _handle_of(hub, "nt", "Intro")
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="Intro lead paragraph.",
        at={"into": intro, "last": True},
    )
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="- first bullet\n- second bullet",
        at={"into": intro, "last": True},
    )
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="Intro closing paragraph.",
        at={"into": intro, "last": True},
    )
    draft.put(
        id="nt",
        chunk_kind="heading",
        text="Child A",
        at={"into": intro, "last": True},
    )
    child_a = _handle_of(hub, "nt", "Child A")
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="Body of child A.",
        at={"into": child_a, "last": True},
    )
    draft.put(
        id="nt",
        chunk_kind="heading",
        text="Child B",
        at={"into": intro, "last": True},
    )
    child_b = _handle_of(hub, "nt", "Child B")
    return {"intro": intro, "child_a": child_a, "child_b": child_b}


def test_replace_content_heading_keeps_subsections(
    draft: DraftHandler, hub: Hub
) -> None:
    h = _build_sectioned(hub, draft)
    own_before = [
        c.chunk_id
        for c in hub.live_store.drafts.block_own_chunks(_chunk(hub, h["intro"]))
    ]
    child_a_before = _chunk(hub, h["child_a"])
    child_b_before = _chunk(hub, h["child_b"])

    r = draft.edit(
        id=h["intro"],
        mode="replace-content",
        text="New intro prose.\n\n- new bullet one\n- new bullet two\n\nNew close.",
    )
    assert "replaced" in r.body and h["intro"] in r.body
    assert "kept" in r.body and "same ids" in r.body
    assert "cross-refs" in r.body or "dangle" in r.body

    order = _order(hub, "nt")
    by_id = {c.chunk_id: c for c in order}
    intro = by_id[_chunk(hub, h["intro"]).chunk_id]
    # The block's id and text survive; only its own body was swapped.
    assert intro.chunk_id == _chunk(hub, h["intro"]).chunk_id
    assert intro.text == "Intro"
    # The whole old own-content run is retired out of reading order.
    live_ids = {c.chunk_id for c in order}
    assert not (set(own_before[1:]) & live_ids)
    # Child sections keep their ids, texts and contents, in place.
    assert by_id[child_a_before.chunk_id].text == "Child A"
    assert by_id[child_b_before.chunk_id].text == "Child B"
    kid_texts = [c.text for c in order if c.parent_chunk_id == child_a_before.chunk_id]
    assert kid_texts == ["Body of child A."]
    # New own content lands ahead of the kept children, in order.
    texts = [c.text for c in order]
    i_new = texts.index("New intro prose.")
    i_a = texts.index("Child A")
    assert i_new < i_a
    new_body = [c.text for c in order[i_new + 1 : i_a] if c.depth > intro.depth]
    assert "new bullet one" in new_body
    assert "New close." in new_body
    # A markdown bullet block lands as a structured list, like put.
    ul = next(c for c in order[i_new:i_a] if c.chunk_kind == "ulist")
    items = [c for c in order if c.parent_chunk_id == ul.chunk_id]
    assert [c.text for c in items] == ["new bullet one", "new bullet two"]


def test_replace_content_heading_line_rewrites_heading(
    draft: DraftHandler, hub: Hub
) -> None:
    h = _build_sectioned(hub, draft)
    r = draft.edit(
        id=h["intro"],
        mode="replace-content",
        text="## Renamed Intro\n\nReplacement body.",
    )
    assert "replaced" in r.body
    assert _chunk(hub, h["intro"]).text == "Renamed Intro"
    texts = [c.text for c in _order(hub, "nt")]
    assert "Replacement body." in texts
    assert "Child A" in texts and "Child B" in texts


def test_replace_content_leaf_rewrites_text_keeps_id(
    draft: DraftHandler, hub: Hub
) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_dc = _order(hub, "nt")[0].dc
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="old leaf text",
        at={"after": title_dc},
    )
    leaf = _handle_of(hub, "nt", "old leaf text")
    before = _chunk(hub, leaf)
    r = draft.edit(id=leaf, mode="replace-content", text="new leaf text")
    assert "edited" in r.body
    after = _chunk(hub, leaf)
    assert after.chunk_id == before.chunk_id  # same handle
    assert after.text == "new leaf text"


def test_replace_content_leaf_keeps_children(draft: DraftHandler, hub: Hub) -> None:
    """A non-heading block's subtree is not part of the replaced span."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_dc = _order(hub, "nt")[0].dc
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="- item one\n- item two",
        at={"after": title_dc},
    )
    order = _order(hub, "nt")
    ul = next(c for c in order if c.chunk_kind == "ulist")
    item_ids = [c.chunk_id for c in order if c.parent_chunk_id == ul.chunk_id]
    draft.edit(id=ul.dc, mode="replace-content", text="list note")
    after = _order(hub, "nt")
    kept = [c.chunk_id for c in after if c.parent_chunk_id == ul.chunk_id]
    assert kept == item_ids  # items untouched, same ids, same parent


def test_replace_content_slug_heading_address(draft: DraftHandler, hub: Hub) -> None:
    h = _build_sectioned(hub, draft)
    r = draft.edit(
        id="nt~Intro",
        mode="replace-content",
        text="Replaced through the heading selector.",
    )
    assert "replaced" in r.body
    own = hub.live_store.drafts.block_own_chunks(_chunk(hub, h["intro"]))
    assert [c.text for c in own] == [
        "Intro",
        "Replaced through the heading selector.",
    ]


def test_replace_content_slug_heading_ambiguous(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_dc = _order(hub, "nt")[0].dc
    draft.put(id="nt", chunk_kind="heading", text="Dup", at={"after": title_dc})
    last = _order(hub, "nt")[-1].dc
    draft.put(id="nt", chunk_kind="heading", text="Dup", at={"after": last})
    with pytest.raises(BadInput) as ei:
        draft.edit(id="nt~Dup", mode="replace-content", text="x")
    assert ei.value.options  # refuses naming each candidate's dc handle


def test_replace_content_base_sha_mismatch(draft: DraftHandler, hub: Hub) -> None:
    h = _build_sectioned(hub, draft)
    stale = _span_sha(hub, h["intro"])
    # A write inside the span rolls the span sha.
    lead = _handle_of(hub, "nt", "Intro lead paragraph.")
    draft.edit(id=lead, text="quietly reworded")
    with pytest.raises(BadInput, match="changed since you read it") as ei:
        draft.edit(
            id=h["intro"],
            mode="replace-content",
            text="clobbering text",
            base_sha=stale,
        )
    assert ei.value.next and "get(kind='draft'" in ei.value.next
    # Nothing was written.
    own = hub.live_store.drafts.block_own_chunks(_chunk(hub, h["intro"]))
    assert "quietly reworded" in [c.text for c in own]
    assert "clobbering text" not in [c.text for c in _order(hub, "nt")]


def test_replace_content_base_sha_accepts_current(
    draft: DraftHandler, hub: Hub
) -> None:
    h = _build_sectioned(hub, draft)
    sha = _span_sha(hub, h["intro"])
    draft.edit(
        id=h["intro"],
        mode="replace-content",
        text="Optimistic rewrite.",
        base_sha=sha[:12],  # the read view's 12-char prefix form
    )
    own = hub.live_store.drafts.block_own_chunks(_chunk(hub, h["intro"]))
    assert [c.text for c in own] == ["Intro", "Optimistic rewrite."]


def test_chunk_read_surfaces_span_sha(draft: DraftHandler, hub: Hub) -> None:
    h = _build_sectioned(hub, draft)
    out = draft.get(id=h["intro"]).body
    m = re.search(r"span-sha:([0-9a-f]{12})", out)
    assert m is not None, out
    assert m.group(1) == _span_sha(hub, h["intro"])[:12]


def test_replace_content_dry_run_writes_nothing(draft: DraftHandler, hub: Hub) -> None:
    h = _build_sectioned(hub, draft)
    r = draft.edit(
        id=h["intro"],
        mode="replace-content",
        text="Preview only.",
        dry_run=True,
    )
    assert "[dry-run]" in r.body and "Preview only." in r.body
    own = hub.live_store.drafts.block_own_chunks(_chunk(hub, h["intro"]))
    assert "Intro lead paragraph." in [c.text for c in own]


def test_replace_content_ack_reports_retired_and_kept(
    draft: DraftHandler, hub: Hub
) -> None:
    h = _build_sectioned(hub, draft)
    r = draft.edit(id=h["intro"], mode="replace-content", text="New prose.")
    # Retire set: lead para + ulist (its 2 items go with it) + close para.
    assert "retired 5 body chunks" in r.body
    # Kept: Child A + its body para + Child B.
    assert "kept 3 child chunks" in r.body and "same ids" in r.body


def test_replace_content_heading_without_children(
    draft: DraftHandler, hub: Hub
) -> None:
    """A childless heading: nothing to retire, nothing to keep — the new
    body lands under it (the kids==[] insert path)."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_dc = _order(hub, "nt")[0].dc
    draft.put(id="nt", chunk_kind="heading", text="Solo", at={"after": title_dc})
    solo = _handle_of(hub, "nt", "Solo")
    r = draft.edit(id=solo, mode="replace-content", text="Fresh body.")
    assert "added 1 chunk" in r.body and "kept" not in r.body
    own = hub.live_store.drafts.block_own_chunks(_chunk(hub, solo))
    assert [c.text for c in own] == ["Solo", "Fresh body."]


def test_replace_content_chained_edit_uses_ack_span_sha(
    draft: DraftHandler, hub: Hub
) -> None:
    h = _build_sectioned(hub, draft)
    r1 = draft.edit(id=h["intro"], mode="replace-content", text="First rewrite.")
    m = re.search(r"span-sha:([0-9a-f]{12})", r1.body)
    assert m is not None, r1.body
    r2 = draft.edit(
        id=h["intro"],
        mode="replace-content",
        text="Second rewrite.",
        base_sha=m.group(1),
    )
    assert "replaced" in r2.body
    own = hub.live_store.drafts.block_own_chunks(_chunk(hub, h["intro"]))
    assert [c.text for c in own] == ["Intro", "Second rewrite."]


def test_replace_content_noop_heading_hashes_stored_text(
    draft: DraftHandler, hub: Hub
) -> None:
    """gr516613: a first-line ``# T`` equal to the stored heading's
    trimmed form skips the heading edit — the returned span-sha must
    fingerprint the STORED (whitespace-padded) text so a chained
    base_sha still matches the next read."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_dc = _order(hub, "nt")[0].dc
    draft.put(id="nt", chunk_kind="heading", text="Intro", at={"after": title_dc})
    head = _handle_of(hub, "nt", "Intro")
    draft.put(
        id="nt", chunk_kind="paragraph", text="Body.", at={"into": head, "last": True}
    )
    # Stray whitespace on the stored heading — what real drafts carry.
    hub.live_store.drafts.edit_text(_chunk(hub, head).handle, "  Intro  ")
    r1 = draft.edit(id=head, mode="replace-content", text="# Intro\nNew body.")
    m = re.search(r"span-sha:([0-9a-f]{12})", r1.body)
    assert m is not None, r1.body
    # The ack's token must equal what the next read hashes.
    assert _span_sha(hub, head).startswith(m.group(1))
    r2 = draft.edit(
        id=head,
        mode="replace-content",
        text="# Intro\nSecond body.",
        base_sha=m.group(1),
    )
    assert "replaced" in r2.body
    own = hub.live_store.drafts.block_own_chunks(_chunk(hub, head))
    assert [c.text for c in own] == ["  Intro  ", "Second body."]


def test_replace_content_refuses_non_prose_in_span(
    draft: DraftHandler, hub: Hub
) -> None:
    """A figure inside the replaced run can never be recreated from
    markdown — the op refuses and names it, nothing is retired."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_dc = _order(hub, "nt")[0].dc
    draft.put(id="nt", chunk_kind="heading", text="Results", at={"after": title_dc})
    head = _handle_of(hub, "nt", "Results")
    draft.put(
        id="nt", chunk_kind="paragraph", text="Lead.", at={"into": head, "last": True}
    )
    fr = draft.put(
        id="nt",
        chunk_kind="figure",
        text="Fig 1. Widget.",
        image=_PNG_B64,
        origin="original",
        at={"into": head, "last": True},
    )
    fig_dc = _dc(fr.body)
    with pytest.raises(BadInput, match="cannot recreate") as ei:
        draft.edit(id=head, mode="replace-content", text="New prose.")
    assert fig_dc in str(ei.value) and "[figure]" in str(ei.value)
    assert ei.value.next and "move" in ei.value.next
    # Nothing was retired — the old span is still live.
    own = hub.live_store.drafts.block_own_chunks(_chunk(hub, head))
    assert "Lead." in [c.text for c in own]
    assert "Fig 1. Widget." in [c.text for c in own]
    # dry_run refuses identically.
    with pytest.raises(BadInput, match="cannot recreate"):
        draft.edit(id=head, mode="replace-content", text="New prose.", dry_run=True)


def test_replace_content_refuses_registry_home(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    draft.put(
        id="nt",
        chunk_kind="term",
        terms=[{"text": "polyethylenimine", "meta": {"short": "PEI"}}],
    )
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    gloss = hub.live_store.drafts.ensure_registry_heading(ref.id, "glossary")
    home = hub.live_store.drafts.get_draft_chunk(gloss)
    assert home is not None
    with pytest.raises(BadInput, match="registry home"):
        draft.edit(id=home.dc, mode="replace-content", text="x")
    with pytest.raises(BadInput, match="registry home"):
        draft.edit(id=home.dc, mode="replace-content", text="x", dry_run=True)


def test_replace_content_dry_run_runs_base_sha_refusal(
    draft: DraftHandler, hub: Hub
) -> None:
    h = _build_sectioned(hub, draft)
    with pytest.raises(BadInput, match="changed since you read it"):
        draft.edit(
            id=h["intro"],
            mode="replace-content",
            text="x",
            base_sha="0" * 12,
            dry_run=True,
        )
    own = hub.live_store.drafts.block_own_chunks(_chunk(hub, h["intro"]))
    assert "Intro lead paragraph." in [c.text for c in own]


def test_replace_content_dry_run_diff_shows_kept_heading(
    draft: DraftHandler, hub: Hub
) -> None:
    h = _build_sectioned(hub, draft)
    r = draft.edit(
        id=h["intro"],
        mode="replace-content",
        text="Only new prose.",
        dry_run=True,
    )
    assert "[dry-run]" in r.body
    # The kept heading diffs as context, not a deletion; the old heading
    # text renders as the ATX line the reader sees.
    assert "# Intro" in r.body and "-# Intro" not in r.body
    assert "+Only new prose." in r.body


def test_replace_content_inserted_chunks_carry_source(
    draft: DraftHandler, hub: Hub
) -> None:
    """Every created chunk — list container and items included — stamps
    ``reason: replace-content`` on its created event (gr514338)."""
    h = _build_sectioned(hub, draft)
    draft.edit(id=h["intro"], mode="replace-content", text="Para.\n\n- a\n- b")
    intro = _chunk(hub, h["intro"])
    own = hub.live_store.drafts.block_own_chunks(intro)
    ids = [c.chunk_id for c in own[1:]]
    ul = next(c for c in own if c.chunk_kind == "ulist")
    ids += [c.chunk_id for c in _order(hub, "nt") if c.parent_chunk_id == ul.chunk_id]
    with hub.live_store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT source FROM chunk_events "
            "WHERE chunk_id = ANY(%s) AND event_kind = 'created'",
            (ids,),
        ).fetchall()
    assert len(rows) == len(ids) == 4  # para + ulist + 2 items
    assert all((r[0] or {}).get("reason") == "replace-content" for r in rows)


def test_replace_content_refuses_other_op_kwargs(draft: DraftHandler, hub: Hub) -> None:
    h = _build_sectioned(hub, draft)
    with pytest.raises(BadInput, match="find="):
        draft.edit(
            id=h["intro"],
            mode="replace-content",
            text="x",
            find="y",
        )


def test_replace_content_unknown_address(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    with pytest.raises(NotFound):
        draft.edit(id="dc999999999", mode="replace-content", text="x")
    with pytest.raises(BadInput):
        draft.edit(id="nt", mode="replace-content", text="x")  # bare slug

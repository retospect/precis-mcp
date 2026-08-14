"""DraftHandler — the verb surface over the draft store ops."""

from __future__ import annotations

import base64
import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Gone, NotFound, Unsupported
from precis.handlers.draft import DraftHandler
from precis.store.store import Store


def _dc(body: str) -> str:
    """Extract the universal handles ``dc<id>`` handle from a draft response."""
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


def _chunk_text(hub: Hub, handle: str) -> str | None:
    ch = hub.live_store.drafts.get_draft_chunk(handle)
    assert ch is not None
    return ch.text


def _chunk_meta(hub: Hub, handle: str) -> dict[str, Any]:
    ch = hub.live_store.drafts.get_draft_chunk(handle)
    assert ch is not None
    return ch.meta


def test_create_requires_project_then_outlines(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    with pytest.raises(BadInput, match="project="):
        draft.put(id="nt", title="Title")  # no project
    r = draft.put(id="nt", title="Title", project=proj)
    assert "created draft 'nt'" in r.body
    out = draft.get(id="nt").body
    assert "Title" in out and bool(re.search(r"dc\d+", out)) and "[heading]" in out


def test_recent_list_path_redirects_to_search(draft: DraftHandler, hub: Hub) -> None:
    # gr48523(2): a '/recent'-style list path on a slug-addressed kind used to
    # dead-end as "slug '/recent' not found". It now raises a BadInput that
    # names the real recovery path (search) instead of a bogus NotFound.
    with pytest.raises(BadInput, match="no '/recent' list view") as ei:
        draft.get(id="/recent")
    assert "search(kind='draft'" in (ei.value.next or "")


def test_dry_run_previews_text_edit_without_writing(
    draft: DraftHandler, hub: Hub
) -> None:
    """gr48518: edit(kind='draft', ..., dry_run=True) used to be swallowed in
    **_kw and the edit applied anyway — a data-loss footgun. It must now render
    a diff preview and write NOTHING (the user wants to see scary rewrites
    before committing)."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="The original paragraph text.",
        at={"after": "¶" + _order(hub, "nt")[0].handle},
    )
    para_h = _order(hub, "nt")[1].handle

    # whole-chunk rewrite, dry-run → diff preview, no write
    r = draft.edit(
        id=f"¶{para_h}", text="A completely rewritten paragraph.", dry_run=True
    )
    assert "[dry-run]" in r.body
    assert "original paragraph" in r.body and "rewritten paragraph" in r.body
    assert _chunk_text(hub, para_h) == "The original paragraph text."

    # find-replace, dry-run → preview, no write
    r2 = draft.edit(id=f"¶{para_h}", find="original", text="pristine", dry_run=True)
    assert "[dry-run]" in r2.body
    assert _chunk_text(hub, para_h) == "The original paragraph text."

    # dry_run='full' shows the whole post-edit text
    r3 = draft.edit(id=f"¶{para_h}", text="Brand new body.", dry_run="full")
    assert "Brand new body." in r3.body
    assert _chunk_text(hub, para_h) == "The original paragraph text."

    # Applying for real (no dry_run) still writes.
    draft.edit(id=f"¶{para_h}", text="Committed text.")
    assert _chunk_text(hub, para_h) == "Committed text."


def test_edit_review_verdict_retract_deletes_ledger_row(
    draft: DraftHandler, hub: Hub
) -> None:
    """The un-review door: `edit(review=<checker>,
    verdict='retract')` un-reviews instead of recording — the edit-door twin
    of `Store.retract_review` (tested at the store level in
    test_chunk_review.py), wired through the shared `edit` verb so the web
    reader's un-review endpoint can write through it too."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="A paragraph.",
        at={"after": "¶" + _order(hub, "nt")[0].handle},
    )
    para = _order(hub, "nt")[1]

    draft.edit(id=f"¶{para.handle}", review="human", verdict="approved")
    assert [
        r.checker for r in hub.live_store.drafts.review_status_for_chunk(para.chunk_id)
    ] == ["human"]

    r = draft.edit(id=f"¶{para.handle}", review="human", verdict="retract")
    assert "retracted human review" in r.body
    assert hub.live_store.drafts.review_status_for_chunk(para.chunk_id) == []

    # Retracting again (nothing left to retract) is a clean no-op, not an error.
    r2 = draft.edit(id=f"¶{para.handle}", review="human", verdict="retract")
    assert "no human review to retract" in r2.body


def test_put_claim_chunk_kind_inserts_without_fk_violation(
    draft: DraftHandler, hub: Hub
) -> None:
    """gripe 57812: put(kind='draft', chunk_kind='claim', ...) into a Claims
    heading (or a bare batched multi-claim write) used to fail with a raw
    ForeignKeyViolation — 'claim' was never registered in chunk_kinds, so the
    INSERT tripped chunks_chunk_kind_fkey (migration 0083 registers it,
    mirroring the 0031 table/aside/listing/term additions). Users had to fall
    back to chunk_kind='paragraph'; now a single claim and a batch both land."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].handle

    r1 = draft.put(
        id="nt",
        chunk_kind="claim",
        text="1. A widget comprising a frobnicator.",
        at={"after": "¶" + title_h},
    )
    assert "added 1 chunk" in r1.body
    dc1 = _dc(r1.body)
    chunk1 = hub.live_store.drafts.get_draft_chunk(dc1)
    assert chunk1 is not None
    assert chunk1.chunk_kind == "claim"

    # a second claim, batched right after the first
    r2 = draft.put(
        id="nt",
        chunk_kind="claim",
        text="2. The widget of claim 1, wherein the frobnicator is annular.",
        at={"after": dc1},
    )
    assert "added 1 chunk" in r2.body
    order = _order(hub, "nt")
    assert sum(1 for c in order if c.chunk_kind == "claim") == 2


def test_dry_run_rejected_on_structural_op(draft: DraftHandler, hub: Hub) -> None:
    """dry_run has no diff semantics for structural ops (e.g. move) — it must
    reject rather than silently write."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="Body.",
        at={"after": "¶" + _order(hub, "nt")[0].handle},
    )
    order = _order(hub, "nt")
    title_h, para_h = order[0].handle, order[1].handle
    with pytest.raises(BadInput, match="dry_run has no preview"):
        draft.edit(id=f"¶{para_h}", move={"before": "¶" + title_h}, dry_run=True)
    # Order unchanged — the move was refused.
    assert [c.handle for c in _order(hub, "nt")][0] == title_h


def test_add_read_edit_move_delete(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].handle

    # add a section heading after the title
    r = draft.put(
        id="nt",
        chunk_kind="heading",
        text="Introduction",
        at={"after": "¶" + title_h},
    )
    assert "added 1 chunk" in r.body
    intro_h = _order(hub, "nt")[1].handle

    # read it back verbatim (chunk addressing)
    assert "Introduction" in draft.get(id=f"¶{intro_h}").body

    # edit its text in place
    draft.edit(id=f"¶{intro_h}", text="Intro v2")
    intro_chunk = hub.live_store.drafts.get_draft_chunk(intro_h)
    assert intro_chunk is not None
    assert intro_chunk.text == "Intro v2"

    # move it before the title
    draft.edit(id=f"¶{intro_h}", move={"before": "¶" + title_h})
    assert [c.handle for c in _order(hub, "nt")][0] == intro_h

    # retire it (soft-delete)
    draft.delete(id=f"¶{intro_h}")
    assert intro_h not in [c.handle for c in _order(hub, "nt")]


def test_edit_flags_newly_introduced_dangling_ref(
    draft: DraftHandler, hub: Hub
) -> None:
    """An edit that introduces a `[handle]` resolving to nothing is flagged
    with a ⚠ scoped to *this edit* — the advisory half of the inline-editor
    validation gate (docs/backlog/draft-inline-editor.md). A dead ref already
    present in the chunk is NOT re-nagged: it isn't this edit's regression."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="A clean paragraph.",
        at={"after": "¶" + title_h},
    )
    para_h = _order(hub, "nt")[1].handle

    # introducing a dead ref → flagged, naming the offending token
    r = draft.edit(id=f"¶{para_h}", text="Now cites [dc999999].")
    assert "this edit introduced unresolved reference(s)" in r.body
    assert "[dc999999]" in r.body

    # re-editing OTHER text while the dead ref stays put → not re-nagged
    r2 = draft.edit(id=f"¶{para_h}", text="Reworded, still cites [dc999999].")
    assert "this edit introduced unresolved reference(s)" not in r2.body


def test_add_empty_block_inserts_paragraph_after_anchor(
    draft: DraftHandler, hub: Hub
) -> None:
    """The inline `+` affordance inserts an EMPTY paragraph after a block — the
    web `/drafts/{id}/block` endpoint calls `store.drafts.add_chunks` directly (the
    `put` verb rejects empty `text=`). It lands right after the anchor in
    reading order, ready to type into."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].handle
    draft.put(
        id="nt", chunk_kind="paragraph", text="First.", at={"after": "¶" + title_h}
    )
    para_h = _order(hub, "nt")[1].handle
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    chunks = hub.live_store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="", at={"after": "¶" + para_h}
    )
    assert len(chunks) == 1 and chunks[0].text == ""
    order = [c.handle for c in _order(hub, "nt")]
    assert order.index(chunks[0].handle) == order.index(para_h) + 1


def test_split_keeps_handle_and_inserts_tail_after(
    draft: DraftHandler, hub: Hub
) -> None:
    """The inline Enter-split (web `/block/{h}/split`): the current chunk keeps
    its handle + the `before` text, a new chunk with the `after` text lands
    right after it. Mirrors what the endpoint does via edit_text + add_chunks."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].handle
    draft.put(
        id="nt", chunk_kind="paragraph", text="Hello world", at={"after": "¶" + title_h}
    )
    para_h = _order(hub, "nt")[1].handle
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    hub.live_store.drafts.edit_text(para_h, "Hello ")
    new = hub.live_store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="world",
        at={"after": "¶" + para_h},
        split=False,
    )[0]
    para_chunk = hub.live_store.drafts.get_draft_chunk(para_h)
    assert para_chunk is not None
    assert para_chunk.text == "Hello "  # first keeps handle + before
    new_chunk = hub.live_store.drafts.get_draft_chunk(new.handle)
    assert new_chunk is not None
    assert new_chunk.text == "world"
    handles = [c.handle for c in _order(hub, "nt")]
    assert handles.index(new.handle) == handles.index(para_h) + 1


def test_merge_prev_joins_text_and_deletes_block(draft: DraftHandler, hub: Hub) -> None:
    """Backspace-merge (web `/block/{h}/merge-prev`): the block's text is
    appended onto the previous one and this block retired, caret at the join
    offset. Mirrors what the endpoint does via the atomic
    ``DraftStore.merge_prev_block`` (gr176088 part 2b)."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].handle
    draft.put(
        id="nt", chunk_kind="paragraph", text="Hello", at={"after": "¶" + title_h}
    )
    p1 = _order(hub, "nt")[1].handle
    hub.live_store.drafts.edit_text(
        p1, "Hello "
    )  # the editor saves verbatim (put would strip)
    draft.put(id="nt", chunk_kind="paragraph", text="world", at={"after": "¶" + p1})
    p2 = _order(hub, "nt")[2].handle

    prev = hub.live_store.drafts.get_draft_chunk(p1)
    assert prev is not None
    caret = len(prev.text or "")
    hub.live_store.drafts.merge_prev_block(p2, p1, "world")

    assert caret == 6
    merged = hub.live_store.drafts.get_draft_chunk(p1)
    assert merged is not None
    assert merged.text == "Hello world"
    assert p2 not in [c.handle for c in _order(hub, "nt")]


def test_newly_dangling_returns_only_new_breakage(
    draft: DraftHandler, hub: Hub
) -> None:
    """`_newly_dangling(new, old)` is the inline editor's hard-gate core (the
    web `/drafts/{id}/text` endpoint 422s on a non-empty result). It returns
    `(chunk_tokens, finding_slugs)` — only refs dead in *new* that weren't
    already dead in *old*."""
    # a newly-introduced dead chunk ref
    assert draft._newly_dangling("cites [dc999999]", "clean") == (["dc999999"], [])
    # dead in both old and new → pre-existing, not this edit's fault
    assert draft._newly_dangling("[dc999999] kept", "had [dc999999]") == ([], [])
    # nothing unresolved either side
    assert draft._newly_dangling("plain new text", "plain old text") == ([], [])


def test_outline_prefers_summary_then_keywords_then_text(
    draft: DraftHandler, hub: Hub
) -> None:
    """The default outline render glosses each block with its llm-v1
    summary, falling back to keywords, then the truncated first line."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].handle
    # three paragraphs: one summarised, one keyworded, one bare
    for body in ("Para with summary.", "Para with keywords.", "Bare paragraph text."):
        draft.put(
            id="nt", chunk_kind="paragraph", text=body, at={"after": "¶" + title_h}
        )
    order = _order(hub, "nt")  # T, then the 3 paras (newest-after-title first)
    by_text = {c.text: c for c in order}
    summ = by_text["Para with summary."]
    kw = by_text["Para with keywords."]
    with hub.live_store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_summaries (chunk_id, summarizer, text) "
            "VALUES (%s, 'llm-v1', %s)",
            (summ.chunk_id, "A crisp one-line gist."),
        )
        conn.execute(
            "UPDATE chunks SET keywords = %s WHERE chunk_id = %s",
            (["alpha", "beta", "gamma"], kw.chunk_id),
        )
        conn.commit()

    out = draft.get(id="nt").body
    assert "A crisp one-line gist." in out  # summary wins
    assert "alpha, beta, gamma" in out  # keywords fallback
    assert "Bare paragraph text." in out  # raw-text fallback


def test_explicit_outline_view_matches_default_render(
    draft: DraftHandler, hub: Hub
) -> None:
    """view='outline' is accepted as an alias for the default outline render
    (view omitted) — many independent planner jobs guessed the concept name
    the error message itself uses and hit a dead-end BadInput."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    default = draft.get(id="nt").body
    explicit = draft.get(id="nt", view="outline").body
    assert explicit == default
    # a genuinely-unknown view still raises, listing the real views
    with pytest.raises(BadInput, match="unknown draft view"):
        draft.get(id="nt", view="nope")


def test_chunk_view_fisheye_routes_to_render_eye_not_silent_degrade(
    draft: DraftHandler, hub: Hub
) -> None:
    """``view=`` is the sole door onto the turn-taking persona threads focus ladder on a draft
    chunk. It used to fall through to the lone-chunk render (silent degrade)
    when the label wasn't a recognised whole-chunk view; now a ladder label
    routes to ``render_eye`` and anything unknown raises."""
    proj = _proj(hub)
    draft.put(id="nt", title="Proposal", project=proj)
    title_dc = _order(hub, "nt")[0].dc
    intro = _add_heading(draft, hub, title_dc, "Introduction")
    _add_para(draft, intro, "First paragraph text.")
    mid = _dc(
        draft.put(
            id="nt",
            chunk_kind="paragraph",
            text="Second paragraph text.",
            at={"into": intro, "last": True},
        ).body
    )
    _add_para(draft, intro, "Third paragraph text.")

    # default (view omitted) is unchanged: the lone chunk, no neighbours.
    lone = draft.get(id=mid).body
    assert "Second paragraph" in lone
    assert "First paragraph" not in lone

    fisheye = draft.get(id=mid, view="fisheye").body
    assert "Second paragraph" in fisheye  # the focal chunk
    assert "Introduction" in fisheye  # ancestor context
    assert "First paragraph" in fisheye or "Third paragraph" in fisheye  # a neighbour

    # a genuinely unknown view still raises — no silent chunk-render fallback.
    with pytest.raises(BadInput, match="unknown draft chunk view"):
        draft.get(id=mid, view="bogus")


def test_numeric_paper_ref_hints_chunk_handle_form(
    draft: DraftHandler, hub: Hub
) -> None:
    """Writing a paper citation as a bare `paper:<id>` mention nudges
    toward the canonical inline chunk handle `[pc<id>]`; a bare handle
    citation does not trigger the hint."""
    proj = _proj(hub)
    paper = hub.live_store.insert_ref(kind="paper", slug="liu24", title="Liu 2024")
    draft.put(id="nt", title="T", project=proj)
    th = _order(hub, "nt")[0].handle

    r = draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"The rate rises sharply, as paper:{paper.id}~3 reports.",
        at={"after": "¶" + th},
    )
    assert f"paper:{paper.id}~3" in r.body  # the offending mention is named
    assert "[pc<id>]" in r.body  # suggests the chunk-handle citation form

    r2 = draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="A second mechanism is plausible [pc999].",
        at={"after": "¶" + th},
    )
    assert "paper: mention" not in r2.body  # a bare [pc<id>] handle is fine


def test_whole_paper_citation_hints_toward_chunk(draft: DraftHandler, hub: Hub) -> None:
    """A bare whole-paper handle `[pa<id>]` (no chunk) is tolerated but
    nudged toward `[pc<id>]`; a chunk-level citation trips nothing."""
    proj = _proj(hub)
    paper = hub.live_store.insert_ref(kind="paper", slug="liu24", title="Liu 2024")
    draft.put(id="nt", title="T", project=proj)
    th = _order(hub, "nt")[0].handle

    r = draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"This follows the approach of [pa{paper.id}].",
        at={"after": "¶" + th},
    )
    assert f"[pa{paper.id}]" in r.body
    assert "whole-paper citation" in r.body
    assert "[pc<id>]" in r.body

    r2 = draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="A second mechanism is plausible [pc999].",
        at={"after": "¶" + th},
    )
    assert "whole-paper citation" not in r2.body


def test_whole_paper_citation_hint_scoped_to_new_write(
    draft: DraftHandler, hub: Hub
) -> None:
    """Editing a chunk that already carried a `[pa<id>]` citation doesn't
    re-nag about it — only a *newly introduced* whole-paper cite fires."""
    proj = _proj(hub)
    paper = hub.live_store.insert_ref(kind="paper", slug="liu24", title="Liu 2024")
    draft.put(id="nt", title="T", project=proj)
    th = _order(hub, "nt")[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"This follows [pa{paper.id}].",
        at={"after": "¶" + th},
    )
    dc = "dc" + str(_order(hub, "nt")[-1].chunk_id)

    r = draft.edit(id=dc, text=f"This follows [pa{paper.id}], with a caveat.")
    assert "whole-paper citation" not in r.body


def test_literal_cite_in_draft_is_flagged(draft: DraftHandler, hub: Hub) -> None:
    r"""Typing a literal ``\cite{}``/``\citequote{}`` into a draft body is
    flagged — in a draft you cite by the ``[pc<id>]`` handle and the
    export engine writes the ``\cite``. A bare handle does not trip it."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    th = _order(hub, "nt")[0].handle

    r = draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=r"As reported \cite{smith2020}, the rate rises.",
        at={"after": "¶" + th},
    )
    assert "literal \\cite" in r.body  # the lint fires

    r2 = draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="The rate rises further [pc999].",
        at={"after": "¶" + th},
    )
    assert "literal \\cite" not in r2.body  # a bare [pc<id>] handle is clean


def test_edit_and_delete_require_chunk_handle(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    with pytest.raises(BadInput, match="targets a chunk"):
        draft.edit(id="nt", text="x")  # a slug, not a ¶handle
    with pytest.raises(BadInput, match="targets a chunk"):
        draft.delete(id="nt")


def test_reading_window(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].handle
    draft.put(
        id="nt", chunk_kind="paragraph", text="a\n\nb\n\nc", at={"after": "¶" + title_h}
    )
    order = _order(hub, "nt")  # T, a, b, c
    mid = order[2].dc  # "b"
    # Universal handles sibling span (supersedes the legacy -B+A window): 1 before,
    # 1 after → a, b, c.
    body = draft.get(id=f"{mid}-1..1").body
    assert "a" in body and "b" in body and "c" in body


def _handle_of(hub: Hub, text: str) -> str:
    return next(c.handle for c in _order(hub, "nt") if c.text == text)


def _dc_of(hub: Hub, text: str) -> str:
    """The ``dc<id>`` handle of the chunk whose text is ``text``."""
    return next(c.dc for c in _order(hub, "nt") if c.text == text)


def test_relative_navigation_sibling_ancestor_span(
    draft: DraftHandler, hub: Hub
) -> None:
    """Relative nav over the draft tree: ^ (ancestor), +N/-N
    (sibling step), -lo..hi (sibling span)."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].handle
    draft.put(id="nt", chunk_kind="heading", text="Intro", at={"after": "¶" + title_h})
    intro_h = _handle_of(hub, "Intro")
    draft.put(id="nt", chunk_kind="paragraph", text="p1", at={"into": "¶" + intro_h})
    draft.put(id="nt", chunk_kind="paragraph", text="p2", at={"into": "¶" + intro_h})
    draft.put(
        id="nt", chunk_kind="heading", text="Methods", at={"after": "¶" + intro_h}
    )

    p1, p2 = _dc_of(hub, "p1"), _dc_of(hub, "p2")
    intro, methods = _dc_of(hub, "Intro"), _dc_of(hub, "Methods")

    # sibling step
    assert "p2" in draft.get(id=f"{p1}+1").body
    assert "p1" in draft.get(id=f"{p2}-1").body
    # ancestor → enclosing heading
    assert "Intro" in draft.get(id=f"{p1}^").body
    # sibling step across headings
    assert "Methods" in draft.get(id=f"{intro}+1").body
    # span = reading window among siblings
    span = draft.get(id=f"{p1}-0..1").body
    assert "p1" in span and "p2" in span
    # out of range / no ancestor → clean not-found
    with pytest.raises(NotFound):
        draft.get(id=f"{p2}+1")  # p2 is the last child
    with pytest.raises(NotFound):
        draft.get(id=f"{_dc_of(hub, 'T')}^")  # root has no enclosing heading


def test_toc_view_headings_only_numbered_and_subtree(
    draft: DraftHandler, hub: Hub
) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    th = _order(hub, "nt")[0].handle
    draft.put(
        id="nt", chunk_kind="heading", text="Introduction", at={"after": "¶" + th}
    )
    intro = _handle_of(hub, "Introduction")
    draft.put(
        id="nt", chunk_kind="heading", text="Background", at={"into": "¶" + intro}
    )
    draft.put(id="nt", chunk_kind="heading", text="Methods", at={"after": "¶" + intro})
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="prose body here",
        at={"into": "¶" + intro, "last": True},
    )

    toc = draft.get(id="nt", view="toc").body
    # TOON table: headings only, addressed by ¶handle, depth in a `level`
    # column; the paragraph is excluded
    assert "level" in toc  # TOON schema column
    assert "Introduction" in toc and "Methods" in toc
    assert "prose body here" not in toc
    bg = next(c for c in _order(hub, "nt") if c.text == "Background")
    assert bg.dc in toc and "Background" in toc

    # TOC rooted at a heading (any hierarchy level)
    sub = draft.get(id="¶" + intro, view="toc").body
    assert "Background" in sub
    assert "Methods" not in sub and "prose body here" not in sub


def test_edit_base_sha_blocks_stale_overwrite(draft: DraftHandler, hub: Hub) -> None:
    """Optimistic concurrency: an edit carrying a base_sha that no longer
    matches the chunk's content_sha is rejected (the draft editable-document model — don't clobber
    a change that landed since the caller last read)."""
    from precis.errors import BadInput
    from precis.store._draft_ops import content_sha

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].handle
    draft.put(
        id="nt", chunk_kind="paragraph", text="original", at={"after": "¶" + title_h}
    )
    para_h = _order(hub, "nt")[1].dc

    stale = content_sha("original")
    # correct base_sha → succeeds, chunk now says v2
    draft.edit(id=para_h, text="v2", base_sha=stale)
    assert _chunk_text(hub, para_h) == "v2"

    # the same (now stale) base_sha → rejected, text unchanged
    with pytest.raises(BadInput, match="changed since you read it"):
        draft.edit(id=para_h, text="v3", base_sha=stale)
    assert _chunk_text(hub, para_h) == "v2"

    # no base_sha → force overwrite still works
    draft.edit(id=para_h, text="v4")
    assert _chunk_text(hub, para_h) == "v4"


def test_chunk_read_surfaces_sha(draft: DraftHandler, hub: Hub) -> None:
    from precis.store._draft_ops import content_sha

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].handle
    out = draft.get(id=f"¶{title_h}").body
    # Read shows a 12-char sha prefix, not the full 64-hex digest.
    assert f"sha:{content_sha('T')[:12]}" in out
    assert content_sha("T") not in out  # full digest is not shown


def test_retired_chunk_read_discloses_retired_state(
    draft: DraftHandler, hub: Hub
) -> None:
    """gr192827(8): a retired chunk stays readable by direct ``dc<id>`` handle
    (gripe 49153) but is invisible to search and reading order — undisclosed,
    that asymmetry reads as a search bug (text you can plainly read that
    ``mode='regex'`` won't match). The read must say the chunk is retired."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    para = draft.put(
        id="nt", chunk_kind="paragraph", text="centering payload", at={"last": True}
    )
    para_h = _dc(para.body)
    para_chunk = hub.live_store.drafts.get_draft_chunk(para_h)
    assert para_chunk is not None
    hub.live_store.drafts.retire_chunk(para_chunk.handle)

    out = draft.get(id=para_h).body
    assert "⚠ RETIRED" in out
    assert "excluded from reading order, search" in out
    # the text itself still reads (live-or-retired direct address)
    assert "centering payload" in out

    # regex search over the draft correctly skips it — the disclosure above
    # is what makes this a non-surprise
    r = draft.search(q="centering", mode="regex", scope="nt")
    assert "no draft chunk matches" in r.body

    # a live chunk carries no retired marker
    live = draft.get(id=f"¶{_order(hub, 'nt')[0].handle}").body
    assert "RETIRED" not in live


def test_edit_accepts_short_sha_prefix(draft: DraftHandler, hub: Hub) -> None:
    """The 12-char prefix shown on read is a valid base_sha; a full
    64-char digest still works too (prefix match)."""
    from precis.store._draft_ops import content_sha

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    para = draft.put(
        id="nt", chunk_kind="paragraph", text="original", at={"last": True}
    )
    para_h = _dc(para.body)

    short = content_sha("original")[:12]
    draft.edit(id=para_h, text="v2", base_sha=short)  # prefix → succeeds
    assert _chunk_text(hub, para_h) == "v2"

    full = content_sha("v2")  # full digest is also a valid prefix
    draft.edit(id=para_h, text="v3", base_sha=full)
    assert _chunk_text(hub, para_h) == "v3"


def test_edit_rejects_too_short_sha(draft: DraftHandler, hub: Hub) -> None:
    from precis.errors import BadInput

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    para = draft.put(
        id="nt", chunk_kind="paragraph", text="original", at={"last": True}
    )
    para_h = _dc(para.body)
    with pytest.raises(BadInput, match="too short"):
        draft.edit(id=para_h, text="v2", base_sha="abc")


def test_abbrev_loop_hint_define_and_silence(draft: DraftHandler, hub: Hub) -> None:
    """Writing an undefined acronym hints the LLM; defining a term
    (meta.short) and marking not_abbrev both clear it."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].handle

    r = draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="We graft KSJW onto the MOF at 1 bar.",
        at={"after": "¶" + title_h},
    )
    assert "undefined abbreviation" in r.body and "KSJW" in r.body and "MOF" in r.body
    para_h = next(c.dc for c in _order(hub, "nt") if c.text.startswith("We graft"))

    # define KSJW (filed under an auto-created Glossary heading)
    draft.put(
        id="nt",
        chunk_kind="term",
        text="Kil Solvent Joule Warbler",
        meta={"short": "KSJW"},
    )
    assert "Glossary" in [
        c.text for c in _order(hub, "nt") if c.chunk_kind == "heading"
    ]
    # silence MOF
    draft.edit(id="nt", not_abbrev=["MOF"])

    # re-edit the paragraph → both now resolved, no abbrev hint
    r2 = draft.edit(id=para_h, text="We graft KSJW onto the MOF again.")
    assert "undefined abbreviation" not in r2.body


def test_temperature_form_hint(draft: DraftHandler, hub: Hub) -> None:
    """A malformed temperature/unit notation lands but trips the
    ``temperature/unit formatting`` hint; the canonical ``63°C`` / ``±1°C``
    is silent."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].handle

    bad = [
        "Anneal at 63 °C for an hour.",  # spaced degree
        "Anneal at 63oC for an hour.",  # 'o' as degree
        "Anneal at 63℃ for an hour.",  # single-char degree-C
        r"Anneal at $63^\circ$C.",  # LaTeX
        "Anneal at 63 degrees Celsius.",  # spelt out
        "Hold to +/- 1 of the setpoint.",  # +/- tolerance
    ]
    for text in bad:
        r = draft.put(
            id="nt", chunk_kind="paragraph", text=text, at={"after": "¶" + title_h}
        )
        assert "temperature/unit formatting" in r.body, text

    # the canonical forms trip nothing
    for ok in ("Anneal at 63°C.", "Hold to ±1°C over 63–65°C."):
        r = draft.put(
            id="nt", chunk_kind="paragraph", text=ok, at={"after": "¶" + title_h}
        )
        assert "temperature/unit formatting" not in r.body, ok


def test_defined_abbrevs_collects_terms_and_inline(
    draft: DraftHandler, hub: Hub
) -> None:
    """defined_abbrevs returns {short: long} from term chunks AND inline
    `Long Form (ABBR)` first-uses; an explicit term wins on a clash."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    title_h = _order(hub, "nt")[0].handle

    # inline definition in prose → picked up by Schwartz-Hearst
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="We use polyethyleneimine (PEI) as the amine.",
        at={"after": "¶" + title_h},
    )
    # an explicit term chunk for a different abbrev
    draft.put(
        id="nt",
        chunk_kind="term",
        text="metal-organic framework",
        meta={"short": "MOF"},
    )

    abb = hub.live_store.drafts.defined_abbrevs(ref.id)
    assert abb["PEI"] == "polyethyleneimine"
    assert abb["MOF"] == "metal-organic framework"


def test_requests_by_handle_runs_against_real_pg(draft: DraftHandler, hub: Hub) -> None:
    """The reader's in-flight panel query (`_requests_by_handle`) must run
    against real Postgres — its `LIKE 'ask-user:%%'` / `'child-failed:%%'`
    literals need doubled `%` or psycopg rejects the placeholder. The
    fake-store web tests can't catch this (no real SQL parse)."""
    from precis.store.types import Tag
    from precis_web.routes.drafts import _requests_by_handle

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    para_h = _order(hub, "nt")[0].handle
    # an anchored change-request todo, tagged asking-the-user
    todo = hub.live_store.insert_ref(kind="todo", slug=None, title="tighten")
    hub.live_store.stamp_ref_meta(todo.id, {"anchor": f"¶{para_h}"})
    hub.live_store.add_tag(todo.id, Tag.open("ask-user:which-para"))

    out = _requests_by_handle(hub.live_store, [para_h])  # must not raise
    reqs = out.get(para_h, [])
    assert any(r["asking"] == "which-para" for r in reqs)


def test_resolve_ask_question_resolves_see_chunk_overflow(hub: Hub) -> None:
    """A >80-char ask-user question overflows into a ``tag_overflow`` chunk
    and the tag becomes ``ask-user:see-chunk-N``. resolve_ask_question must
    read the chunk back so the UI shows the real question, not the opaque
    "see chunk 0" slug. Short inline questions and the bare marker pass
    through unchanged."""
    from precis.store.types import BlockInsert

    store = hub.live_store
    todo = store.insert_ref(kind="todo", slug=None, title="fix bolding")
    q = (
        "Which did you mean? (A) fold the ~100 label-headings back inline "
        "(B) point me at a specific chunk (C) a renderer/export setting."
    )
    store.blocks.insert_blocks(
        todo.id,
        [
            BlockInsert(
                pos=0,
                text=f"ask-user: {q}",
                meta={"chunk_kind": "tag_overflow", "tag_namespace": "ask-user"},
            )
        ],
    )
    assert store.drafts.resolve_ask_question(todo.id, "see-chunk-0") == q
    assert store.drafts.resolve_ask_question(todo.id, "which para?") == "which para?"
    assert store.drafts.resolve_ask_question(todo.id, "") == ""
    assert store.drafts.resolve_ask_question(todo.id, "see-chunk-9") == ""


def test_requests_by_handle_surfaces_question_and_fail_reason(
    draft: DraftHandler, hub: Hub
) -> None:
    """The reader's per-block panel must show the real ask-user question
    (resolving a see-chunk redirect) and *why* a child job failed (its
    job_summary), so the operator never sees a bare "see chunk 0" / "failed".
    """
    from precis.store.types import BlockInsert, Tag
    from precis_web.routes.drafts import _requests_by_handle

    store = hub.live_store
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    para_h = _order(hub, "nt")[0].handle

    # (1) a request waiting on the user, with an overflowed question.
    asking = store.insert_ref(kind="todo", slug=None, title="fix the bolding")
    store.stamp_ref_meta(asking.id, {"anchor": f"¶{para_h}"})
    q = (
        "Which did you mean? (A) fold the label-headings back inline "
        "(B) point me at a specific chunk (C) a renderer/export setting."
    )
    store.blocks.insert_blocks(
        asking.id,
        [
            BlockInsert(
                pos=0,
                text=f"ask-user: {q}",
                meta={"chunk_kind": "tag_overflow", "tag_namespace": "ask-user"},
            )
        ],
    )
    store.add_tag(asking.id, Tag.open("ask-user:see-chunk-0"))

    # (2) a request blocked by a failed child job carrying the reason.
    failing = store.insert_ref(kind="todo", slug=None, title="add citations")
    store.stamp_ref_meta(failing.id, {"anchor": f"¶{para_h}"})
    job = store.insert_ref(
        kind="job", slug=None, title="plan_tick", parent_id=failing.id, meta={}
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", "failed"), set_by="system", replace_prefix=True
    )
    store.blocks.insert_blocks(
        job.id,
        [
            BlockInsert(
                pos=0,
                text="API Error: violates our Usage Policy. Try rephrasing.",
                meta={"chunk_kind": "job_summary"},
            )
        ],
    )
    store.add_tag(failing.id, Tag.open(f"child-failed:{job.id}"))

    reqs = _requests_by_handle(store, [para_h]).get(para_h, [])
    ask_req = next(r for r in reqs if r["ref_id"] == asking.id)
    assert ask_req["asking"] == q  # full question, not "see chunk 0"
    assert ask_req["ask_tag"] == "ask-user:see-chunk-0"
    assert ask_req["request"] == "fix the bolding"
    fail_req = next(r for r in reqs if r["ref_id"] == failing.id)
    assert fail_req["failed"] is True
    assert "Usage Policy" in fail_req["fail_reason"]


def test_requests_by_handle_fail_reason_falls_back_to_job_event(
    draft: DraftHandler, hub: Hub
) -> None:
    """When the failed child job has no ``job_summary`` chunk (the common
    case — ``record_failure`` writes only a ``job_event``; most executors
    write ``job_summary`` only on their SUCCESS tail), ``fail_reason``
    still surfaces something instead of blank: the first line of the
    latest ``job_event`` chunk."""
    from precis.store.types import BlockInsert, Tag
    from precis_web.routes.drafts import _requests_by_handle

    store = hub.live_store
    proj = _proj(hub)
    draft.put(id="nt2", title="T", project=proj)
    para_h = _order(hub, "nt2")[0].handle

    failing = store.insert_ref(kind="todo", slug=None, title="add citations")
    store.stamp_ref_meta(failing.id, {"anchor": f"¶{para_h}"})
    job = store.insert_ref(
        kind="job", slug=None, title="plan_tick", parent_id=failing.id, meta={}
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", "failed"), set_by="system", replace_prefix=True
    )
    store.blocks.insert_blocks(
        job.id,
        [
            BlockInsert(
                pos=0,
                text=(
                    "runner: killed at wall-clock deadline (handle {...})\n"
                    "--- tail ---\nraw subprocess output, not for the UI"
                ),
                meta={"chunk_kind": "job_event"},
            )
        ],
    )
    store.add_tag(failing.id, Tag.open(f"child-failed:{job.id}"))

    reqs = _requests_by_handle(store, [para_h]).get(para_h, [])
    fail_req = next(r for r in reqs if r["ref_id"] == failing.id)
    assert fail_req["failed"] is True
    assert fail_req["fail_reason"] == (
        "runner: killed at wall-clock deadline (handle {...})"
    )
    assert "raw subprocess output" not in fail_req["fail_reason"]


def test_chunk_connections_and_edit_stats(draft: DraftHandler, hub: Hub) -> None:
    """chunk_connections returns refs linked to a chunk (the dream/
    provenance surface); chunk_edit_stats counts edits."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].handle
    draft.put(
        id="nt", chunk_kind="paragraph", text="A claim.", at={"after": f"¶{title_h}"}
    )
    para = next(c for c in _order(hub, "nt") if c.text == "A claim.")
    dref = hub.live_store.get_ref(kind="draft", id="nt")
    assert dref is not None
    mem = hub.live_store.insert_ref(kind="memory", slug=None, title="A dreamt idea")
    with hub.live_store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO links (src_ref_id, src_chunk_id, dst_ref_id, relation, set_by) "
            "VALUES (%s, %s, %s, 'derived-from', 'agent')",
            (dref.id, para.chunk_id, mem.id),
        )

    conns = hub.live_store.drafts.chunk_connections(dref.id, [para.handle])
    assert conns[para.handle][0]["kind"] == "memory"
    assert conns[para.handle][0]["title"] == "A dreamt idea"
    assert conns[para.handle][0]["relation"] == "derived-from"
    assert conns[para.handle][0]["direction"] == "out"

    # edit the chunk → an 'edited' event is logged
    draft.edit(id=f"¶{para.handle}", text="A revised claim.")
    stats = hub.live_store.drafts.chunk_edit_stats(dref.id, [para.handle])
    assert stats[para.handle]["edits"] >= 1


def test_anchored_todos_groups_by_handle_and_keeps_done(
    draft: DraftHandler, hub: Hub
) -> None:
    """``Store.anchored_todos`` (gripe 178766) is the single query BOTH the
    classic reader's change-request cards (``_requests_by_handle``, now a
    thin wrapper) and ``precis_web.draft_links.chunk_links``'s ``flags``
    read — a standalone anchored todo (no project link, no job) is
    otherwise invisible outside the block it's pinned to. Matches the
    legacy ``¶<handle>`` anchor AND the newer bare ``dc<id>``/handle form,
    groups by the bare handle, and keeps done/won't-do (clickable, just
    de-emphasised) rather than dropping them."""
    from precis.store.types import Tag

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    para_h = _order(hub, "nt")[0].handle
    open_todo = hub.live_store.insert_ref(kind="todo", slug=None, title="tighten this")
    hub.live_store.stamp_ref_meta(open_todo.id, {"anchor": f"¶{para_h}"})
    done_todo = hub.live_store.insert_ref(kind="todo", slug=None, title="already fixed")
    hub.live_store.stamp_ref_meta(done_todo.id, {"anchor": para_h})  # bare form too
    hub.live_store.add_tag(done_todo.id, Tag.closed("STATUS", "done"))

    out = hub.live_store.drafts.anchored_todos([para_h])
    reqs = out.get(para_h, [])
    assert {r["ref_id"] for r in reqs} == {open_todo.id, done_todo.id}
    done = next(r for r in reqs if r["ref_id"] == done_todo.id)
    assert done["done"] is True and done["status"] == "done"
    # active-first ordering (_REQUEST_ORDER): open sorts ahead of done.
    assert reqs[0]["ref_id"] == open_todo.id
    # unrelated handle sees no anchored todos.
    assert hub.live_store.drafts.anchored_todos(["ZZZZZZ"]) == {}


# ── queued UX fixes: abbrev scoping, promote hint, link redirect ──


def test_edit_does_not_renag_preexisting_abbrev(draft: DraftHandler, hub: Hub) -> None:
    """Editing a chunk that already contained an undefined acronym must
    not re-nag about it — only abbreviations the edit introduces."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    p = draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="MOF systems are promising.",
        at={"last": True},
    )
    h = _dc(p.body)
    # First write nags about MOF (newly introduced).
    assert "MOF" in p.body
    # Editing the same chunk (MOF still present, not newly introduced) → no MOF nag.
    out = draft.edit(id=h, text="MOF systems are very promising.").body
    assert "undefined abbreviation" not in out


def test_edit_nags_only_new_abbrev(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    p = draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="MOF systems are promising.",
        at={"last": True},
    )
    h = _dc(p.body)
    # Introduce a NEW acronym (DAC) on edit → it should be nagged, MOF should not.
    out = draft.edit(id=h, text="MOF systems help DAC efforts.").body
    assert "DAC" in out and "undefined abbreviation" in out


def test_promote_hint_on_inline_definition(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    out = draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="We use multivariate templated modulation (MTVM) here.",
        at={"last": True},
    ).body
    # Inline def detected → promote hint, not an 'undefined' nag.
    assert "inline definition" in out
    assert "chunk_kind='term'" in out and "MTVM" in out


def test_no_promote_hint_when_already_a_term(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    draft.put(
        id="nt",
        chunk_kind="term",
        text="multivariate templated modulation",
        meta={"short": "MTVM"},
    )
    out = draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="We use multivariate templated modulation (MTVM) here.",
        at={"last": True},
    ).body
    assert "inline definition" not in out  # already promoted → no nag


def test_draft_link_verb_redirects_to_prose(hub: Hub) -> None:
    """A non-placement draft link still teaches the markdown-ref model.

    The link verb exists on drafts now (folder placement);
    any other relation raises with both the placement recipe and the
    embed-a-handle-in-prose teaching (formerly a runtime verb
    redirect on the unsupported-verb path).
    """
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import make_embedder
    from precis.runtime import PrecisRuntime

    store = hub.live_store
    rt = PrecisRuntime(
        config=PrecisConfig(),
        hub=boot(
            store=store, embedder=make_embedder("mock", dim=store.embedding_dim())
        ),
    )
    out = rt.dispatch("link", {"kind": "draft", "id": "¶ABC", "target": "¶DEF"})
    assert "only rel='parent'" in out
    assert "[dc<target>]" in out


# ── Fix A: the draft surfaces stuck / in-flight work on it ──────────


def test_outline_surfaces_blocked_work(draft: DraftHandler, hub: Hub) -> None:
    """A failed enrichment job parks its parent silently; the draft
    outline now walks draft→project→subtree and shows it as blocked."""
    from precis.handlers._job_bubble import bubble_job_failure
    from precis.store.types import Tag

    store = hub.live_store
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)

    child = store.insert_ref(
        kind="todo", slug=None, title="Enrich CNT section", parent_id=proj
    )
    store.add_tag(
        child.id, Tag.closed("STATUS", "open"), set_by="agent", replace_prefix=True
    )
    job = store.insert_ref(
        kind="job", slug=None, title="plan_tick", parent_id=child.id, meta={}
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", "failed"), set_by="system", replace_prefix=True
    )
    bubble_job_failure(store, job.id)

    out = draft.get(id="nt").body
    assert "Work in progress" in out
    assert f"todo:{child.id}" in out
    assert "blocked" in out
    # gr192827 item 3: the per-job history line collapses to a
    # per-status count ("1 failed"), not the raw ``job:<id> failed``.
    assert "1 failed" in out
    assert f"job:{job.id}" not in out


def test_outline_clean_draft_has_no_work_section(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    assert "Work in progress" not in draft.get(id="nt").body


def test_outline_wip_job_history_collapses_to_counts(
    draft: DraftHandler, hub: Hub
) -> None:
    """A todo with many retried child jobs used to print the whole
    history (``job:187049 succeeded, job:187242 succeeded, …`` x20) —
    gr192827 item 3 collapses it to per-status counts."""
    from precis.store.types import Tag

    store = hub.live_store
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)

    child = store.insert_ref(
        kind="todo", slug=None, title="Enrich section", parent_id=proj
    )
    store.add_tag(
        child.id, Tag.closed("STATUS", "open"), set_by="agent", replace_prefix=True
    )
    statuses = ["succeeded"] * 5 + ["failed"] + ["running"]
    for st in statuses:
        job = store.insert_ref(
            kind="job", slug=None, title="tick", parent_id=child.id, meta={}
        )
        store.add_tag(
            job.id, Tag.closed("STATUS", st), set_by="system", replace_prefix=True
        )

    out = draft.get(id="nt").body
    assert "Work in progress" in out
    assert "5 ok / 1 failed / 1 running" in out
    # No raw per-job ``job:<id> <status>`` entries leak into the summary.
    assert "succeeded" not in out


def test_outline_surfaces_hygiene_debt(draft: DraftHandler, hub: Hub) -> None:
    """Undefined abbreviations and whole-paper citations anywhere in the
    draft surface as a Hygiene section on every outline read — not just
    the write that introduced them — so legacy/bulk-authored content that
    never passed through an incremental edit still gets flagged."""
    proj = _proj(hub)
    paper = hub.live_store.insert_ref(kind="paper", slug="liu24", title="Liu 2024")
    draft.put(id="nt", title="T", project=proj)
    th = _order(hub, "nt")[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"The GFET switches slowly, per [pa{paper.id}].",
        at={"after": "¶" + th},
    )

    out = draft.get(id="nt").body
    assert "## Hygiene" in out
    assert "GFET" in out
    assert f"[pa{paper.id}]" in out


def test_outline_clean_draft_has_no_hygiene_section(
    draft: DraftHandler, hub: Hub
) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    assert "## Hygiene" not in draft.get(id="nt").body


def test_hygiene_view_returns_full_lists_unelided(
    draft: DraftHandler, hub: Hub
) -> None:
    """gr192827 item 9: get(kind='draft', view='hygiene') returns the
    COMPLETE undefined-abbreviation and whole-paper-cite lists, un-elided
    (no "+N more" truncation) — and nothing else, no outline body / WIP
    block. The default outline footer keeps its truncated-to-8 rendering
    unchanged."""
    proj = _proj(hub)
    paper = hub.live_store.insert_ref(kind="paper", slug="liu24", title="Liu 2024")
    draft.put(id="nt", title="T", project=proj)
    th = _order(hub, "nt")[0].handle
    abbrevs = [f"ABBR{i}" for i in range(12)]
    prose = " ".join(f"The {a} device works." for a in abbrevs)
    prose += f" See [pa{paper.id}] for background."
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=prose,
        at={"after": "¶" + th},
    )

    # The outline footer still elides — behavior preserved.
    outline = draft.get(id="nt").body
    assert "more" in outline

    out = draft.get(id="nt", view="hygiene").body

    # Every abbreviation present, none elided behind "+N more".
    for a in abbrevs:
        assert a in out, f"{a} missing from un-elided hygiene view"
    assert "more)" not in out
    assert f"[pa{paper.id}]" in out

    # Not the outline body or WIP block — hygiene-only.
    assert "hygiene report" in out
    assert not re.search(r"— \d+ chunks?\b", out)
    assert "[paragraph]" not in out
    assert "[heading]" not in out
    assert "## Work in progress" not in out


# ── Fix C: dangling [finding #slug] markers are flagged on read ─────


def test_dangling_finding_marker_flagged(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="See [finding #amdursky-azurin-review] and [finding #dahl-cytochrome].",
        at={"after": "¶" + title_h},
    )
    para_h = _order(hub, "nt")[1].dc
    out = draft.get(id=para_h).body
    assert "unresolved finding reference" in out
    assert "#amdursky-azurin-review" in out
    assert "#dahl-cytochrome" in out


def test_clean_chunk_has_no_finding_warning(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="Plain prose with no markers at all.",
        at={"after": "¶" + title_h},
    )
    para_h = _order(hub, "nt")[1].dc
    out = draft.get(id=para_h).body
    assert "unresolved finding" not in out


def test_numeric_chunk_ref_flagged(draft: DraftHandler, hub: Hub) -> None:
    # An LLM that writes a numeric id ([[45650]]) where a handle belongs
    # gets warned on read.
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].dc
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="As shown in [45650], the effect holds.",
        at={"after": title_h},
    )
    para_h = _order(hub, "nt")[1].dc
    out = draft.get(id=para_h).body
    assert "unresolved reference" in out
    assert "[45650]" in out


def test_valid_chunk_ref_not_flagged(draft: DraftHandler, hub: Hub) -> None:
    # A real, resolvable [[dc<id>]] reference must NOT trip the warning.
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].dc
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"See the title at [{title_h}] for context.",
        at={"after": title_h},
    )
    para_h = _order(hub, "nt")[1].dc
    out = draft.get(id=para_h).body
    assert "unresolved reference" not in out


# ── word count + word targets (proposal writing) ─────────────────────


def _add_heading(draft: DraftHandler, hub: Hub, after_dc: str, text: str) -> str:
    r = draft.put(id="nt", chunk_kind="heading", text=text, at={"after": after_dc})
    return _dc(r.body)


def _add_para(draft: DraftHandler, into_dc: str, text: str) -> None:
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=text,
        at={"into": into_dc, "last": True},
    )


def test_wordcount_view_counts_and_verdicts(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="Proposal", project=proj)
    title_dc = _order(hub, "nt")[0].dc
    intro = _add_heading(draft, hub, title_dc, "Introduction")
    _add_para(draft, intro, "one two three four five")  # 5 words

    # No target yet → verdict 'none', count shown.
    out = draft.get(id="nt", view="wordcount").body
    assert "Introduction" in out
    assert "total: 5 words" in out
    assert "none" in out

    # Set a target the section is under, then re-check.
    draft.edit(id=intro, word_target={"min": 50, "max": 100})
    out = draft.get(id="nt", view="wordcount").body
    assert "under" in out
    assert "off target" in out  # the ⚠ trailer fires

    # Widen the target so the section is within range → ok, no warning.
    draft.edit(id=intro, word_target={"min": 1, "max": 10})
    out = draft.get(id="nt", view="wordcount").body
    assert "ok" in out
    assert "off target" not in out


def test_wordcount_scoped_to_heading_subtree(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="Proposal", project=proj)
    title_dc = _order(hub, "nt")[0].dc
    a = _add_heading(draft, hub, title_dc, "Aims")
    _add_para(draft, a, "alpha beta")  # 2
    b = _add_heading(draft, hub, a, "Budget")
    _add_para(draft, b, "one two three four")  # 4

    whole = draft.get(id="nt", view="wordcount").body
    assert "total: 6 words" in whole

    scoped = draft.get(id=a, view="wordcount").body
    assert "total: 2 words" in scoped
    assert "Budget" not in scoped  # sibling excluded from the subtree


def test_word_target_validation(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_dc = _order(hub, "nt")[0].dc
    intro = _add_heading(draft, hub, title_dc, "Intro")

    with pytest.raises(BadInput, match="exceeds max"):
        draft.edit(id=intro, word_target={"min": 500, "max": 100})

    # A word target on a non-heading (paragraph) is rejected.
    _add_para(draft, intro, "some prose here")
    para_dc = _order(hub, "nt")[-1].dc
    with pytest.raises(BadInput, match="heading"):
        draft.edit(id=para_dc, word_target={"min": 1, "max": 10})


def test_word_target_clear(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_dc = _order(hub, "nt")[0].dc
    intro = _add_heading(draft, hub, title_dc, "Intro")
    draft.edit(id=intro, word_target={"min": 1, "max": 5})
    assert "word_target" in _chunk_meta(hub, intro)
    draft.edit(id=intro, word_target={})  # clear
    assert "word_target" not in _chunk_meta(hub, intro)


def test_edit_retired_or_unknown_chunk_is_typed(draft: DraftHandler, hub: Hub) -> None:
    """gripe #45083: editing a stale/retired handle used to surface the
    opaque ``[error:Internal] internal error in edit`` fallback (a raw
    ValueError from the store). It must now be a typed, actionable error."""
    proj = _proj(hub)
    draft.put(id="rt", title="T", project=proj)
    # A second live chunk so retiring the target doesn't hit "last live chunk".
    r = draft.put(id="rt", chunk_kind="paragraph", text="doomed body")
    para_h = _order(hub, "rt")[-1].handle
    # Retire it out from under the caller, then edit the now-stale handle.
    hub.live_store.drafts.retire_chunk(para_h)
    with pytest.raises(Gone, match="retired"):
        draft.edit(id="¶" + para_h, text="new text")
    # An unknown / garbage handle → typed NotFound (not the opaque fallback).
    with pytest.raises(NotFound):
        draft.edit(id="¶zzzznotarealhandle", text="x")


def test_edit_text_store_op_typed_errors(store: Store) -> None:
    """Store-level guard: ``edit_text`` on a retired chunk raises ``Gone``,
    on an unknown handle raises ``NotFound`` — never a bare ValueError."""
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    ref, _title = store.drafts.create_draft(name="es", title="T", project_ref_id=proj)
    para = store.drafts.add_chunks(ref_id=ref.id, chunk_kind="paragraph", text="doomed")
    handle = para[-1].handle
    store.drafts.retire_chunk(handle)
    with pytest.raises(Gone, match="retired"):
        store.drafts.edit_text(handle, "new text")
    with pytest.raises(NotFound):
        store.drafts.edit_text("zzzznotarealhandle", "x")


# ---------------------------------------------------------------------------
# OPEN-ITEMS follow-on to gripe #45083 / 138ed8cf: that fix typed only
# ``edit_text``'s path (the ``text=`` whole-chunk rewrite). Nothing pinned
# that the REST of the draft chunk-mutator verb set also returns a typed
# error — through the real dispatch path, which does the PrecisError →
# rendered-``[error:…]``-string mapping (``PrecisRuntime.dispatch_with_
# status``), not the bare handler/store call. A future mutator added
# without threading the typed-error convention would regress silently
# (opaque ``[error:Internal] internal error in edit`` / a bare traceback)
# and nothing here would catch it. Two contracts, both driven through
# ``dispatch_with_status``:
#
#   * a ``dc<id>`` that was NEVER allocated -> typed NotFound
#   * an already-retired handle -> typed Gone (except ``delete``, which is
#     a deliberate idempotent no-op on a retired target — see the
#     dedicated test below)
# ---------------------------------------------------------------------------

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA"
    "C0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

#: A chunk_id that a fresh per-test DB will never have allocated.
_UNKNOWN_DC = "dc999999999"


@pytest.mark.parametrize(
    "shape_id,verb,args",
    [
        ("edit-text", "edit", {"text": "new text"}),
        ("edit-find-replace", "edit", {"find": "x", "text": "y"}),
        ("edit-move", "edit", {"move": {"before": _UNKNOWN_DC}}),
        ("edit-style", "edit", {"style": "intro"}),
        ("edit-list-kind", "edit", {"list_kind": "olist"}),
        ("edit-word-target", "edit", {"word_target": {"min": 1, "max": 10}}),
        ("edit-figure-origin", "edit", {"origin": "original"}),
        ("edit-figure-permission", "edit", {"permission": {"status": "granted"}}),
        ("delete", "delete", {}),
    ],
)
def test_dispatch_typed_error_unknown_chunk_all_mutator_shapes(
    runtime_with_store: Any, shape_id: str, verb: str, args: dict[str, Any]
) -> None:
    """Every draft chunk-mutator verb shape, on a ``dc<id>`` that never
    existed, returns typed [error:NotFound] through the real dispatch
    path — never the opaque [error:Internal] ValueError fallback."""
    body, is_error = runtime_with_store.dispatch_with_status(
        verb, {"kind": "draft", "id": _UNKNOWN_DC, **args}
    )
    assert is_error, f"{shape_id}: expected an error, got {body!r}"
    assert "[error:NotFound]" in body, f"{shape_id}: {body!r}"
    assert "[error:Internal]" not in body, f"{shape_id}: {body!r}"
    assert "ValueError" not in body, f"{shape_id}: {body!r}"


@pytest.mark.parametrize(
    "shape_id,chunk_kind,seed_text,build_args",
    [
        ("edit-text", "paragraph", "doomed body", lambda title_dc: {"text": "fixed"}),
        (
            "edit-find-replace",
            "paragraph",
            "doomed body",
            lambda title_dc: {"find": "doomed", "text": "fixed"},
        ),
        (
            "edit-move",
            "paragraph",
            "doomed body",
            lambda title_dc: {"move": {"before": title_dc}},
        ),
        ("edit-style", "heading", "Sec", lambda title_dc: {"style": "intro"}),
        ("edit-list-kind", "ulist", "", lambda title_dc: {"list_kind": "olist"}),
        (
            "edit-word-target",
            "heading",
            "Sec",
            lambda title_dc: {"word_target": {"min": 1, "max": 10}},
        ),
    ],
)
def test_dispatch_typed_error_retired_chunk_all_mutator_shapes(
    runtime_with_store: Any,
    shape_id: str,
    chunk_kind: str,
    seed_text: str,
    build_args: Any,
) -> None:
    """Every draft chunk-mutator verb shape, on an already-retired handle,
    returns typed [error:Gone] through the real dispatch path — never the
    opaque [error:Internal] ValueError fallback (gripe #45083 / 138ed8cf
    fixed only the ``edit_text`` path; this pins the whole verb set)."""
    store = runtime_with_store.store
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    ref, title = store.drafts.create_draft(
        name=f"rt-{shape_id}", title="T", project_ref_id=proj
    )
    target = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind=chunk_kind,
        text=seed_text,
        at={"after": title.handle},
    )[0]
    store.drafts.retire_chunk(target.handle)
    args = {"kind": "draft", "id": target.dc, **build_args(title.dc)}
    body, is_error = runtime_with_store.dispatch_with_status("edit", args)
    assert is_error, f"{shape_id}: expected an error, got {body!r}"
    assert "[error:Gone]" in body, f"{shape_id}: {body!r}"
    assert "[error:Internal]" not in body, f"{shape_id}: {body!r}"
    assert "ValueError" not in body, f"{shape_id}: {body!r}"


@pytest.mark.parametrize(
    "arg_name,arg_value",
    [("origin", "original"), ("permission", {"status": "granted"})],
)
def test_dispatch_typed_error_retired_figure_provenance(
    runtime_with_store: Any, arg_name: str, arg_value: Any
) -> None:
    """The figure-provenance edit shape (``origin=``/``permission=``) on an
    already-retired figure chunk also returns typed [error:Gone]."""
    store = runtime_with_store.store
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    ref, title = store.drafts.create_draft(
        name=f"rtfig-{arg_name}", title="T", project_ref_id=proj
    )
    fig = store.drafts.add_figure(
        ref_id=ref.id,
        caption="c",
        origin="original",
        image=_PNG_1X1,
        mime="image/png",
        at={"after": title.handle},
    )
    store.drafts.retire_chunk(fig.handle)
    body, is_error = runtime_with_store.dispatch_with_status(
        "edit", {"kind": "draft", "id": fig.dc, arg_name: arg_value}
    )
    assert is_error, body
    assert "[error:Gone]" in body, body
    assert "[error:Internal]" not in body and "ValueError" not in body, body


def test_dispatch_delete_already_retired_chunk_is_idempotent_not_error(
    runtime_with_store: Any,
) -> None:
    """``delete`` on an already-retired handle is a deliberate idempotent
    no-op (the store's ``retire_chunk`` returns silently when
    ``retired_at`` is already set) — a success body, not a typed error.
    Pinned separately from the Gone/NotFound contract above so a future
    change that makes it raise (or, worse, blow up with a bare
    ValueError) is caught either way."""
    store = runtime_with_store.store
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    ref, title = store.drafts.create_draft(
        name="rt-delete-idempotent", title="T", project_ref_id=proj
    )
    target = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="doomed body",
        at={"after": title.handle},
    )[0]
    store.drafts.retire_chunk(target.handle)
    body, is_error = runtime_with_store.dispatch_with_status(
        "delete", {"kind": "draft", "id": target.dc}
    )
    assert not is_error, body
    assert "retired" in body
    assert "[error:Internal]" not in body and "ValueError" not in body


def test_authors_edit_sets_byline_with_affiliation(
    draft: DraftHandler, hub: Hub
) -> None:
    proj = _proj(hub)
    draft.put(id="byl", title="A Study", project=proj)
    r = draft.edit(
        id="byl",
        authors=[
            {"name": "Doe, Jane", "affiliation": "MIT", "ror": "https://ror.org/x"},
            {"family": "Roe", "given": "John"},
        ],
    )
    assert "set 2 authors" in r.body and "1 with affiliation" in r.body
    ref = hub.live_store.get_ref(kind="draft", id="byl")
    assert ref is not None
    # persisted to the first-class authors column, affiliation/ror preserved,
    # names canonicalised to the sortable {"name"} shape.
    assert ref.authors == [
        {"name": "Doe, Jane", "affiliation": "MIT", "ror": "https://ror.org/x"},
        {"name": "Roe, John"},
    ]


# ---------------------------------------------------------------------------
# scaffold — paper-writing pipeline rung 4 (docs/backlog/paper-writing-pipeline.md §"Document classes"): edit(kind='draft',
# scaffold=…) lays down a genre's standard section skeleton via the shared
# precis.draft.scaffolds table (see also tests/test_draft_scaffold.py for
# the store-level scaffold_sections behavior).
# ---------------------------------------------------------------------------


def test_scaffold_edit_book(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="bk", title="A Book", project=proj)
    r = draft.edit(id="bk", scaffold="book")
    assert "scaffolded 8 sections on bk (book)" in r.body
    ro = _order(hub, "bk")
    assert [c.text for c in ro[1:]] == [
        "Preface",
        "Introduction",
        "Background",
        "Chapter 1",
        "Chapter 2",
        "Chapter 3",
        "Conclusion",
        "Bibliography",
    ]


def test_scaffold_edit_summary(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="sm", title="A Summary", project=proj)
    r = draft.edit(id="sm", scaffold="summary")
    assert "scaffolded 4 sections on sm (summary)" in r.body
    ro = _order(hub, "sm")
    assert [c.text for c in ro[1:]] == [
        "Summary",
        "Key Points",
        "Details",
        "References",
    ]


def test_scaffold_edit_unknown_class(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="bad", title="T", project=proj)
    with pytest.raises(BadInput, match="unknown scaffold class") as ei:
        draft.edit(id="bad", scaffold="screenplay")
    assert "book" in (ei.value.next or "") and "paper" in (ei.value.next or "")


def test_scaffold_edit_by_chunk_handle(draft: DraftHandler, hub: Hub) -> None:
    # scaffold is a draft-level op — a ¶handle inside the draft resolves to
    # its owning draft, same as authors=/not_abbrev= (``_resolve_draft_any``).
    proj = _proj(hub)
    draft.put(id="via-chunk", title="T", project=proj)
    title_handle = _order(hub, "via-chunk")[0].handle
    r = draft.edit(id="¶" + title_handle, scaffold="report")
    assert "scaffolded 5 sections on via-chunk (report)" in r.body


# ---------------------------------------------------------------------------
# get(project=…) — reverse lookup (paper-writing pipeline rung 4;
# backlog_draft_by_project): the draft(s) bound to a project todo via the
# draft-of link.
# ---------------------------------------------------------------------------


def test_get_by_project_returns_bound_draft_outline(
    draft: DraftHandler, hub: Hub
) -> None:
    proj = _proj(hub)
    draft.put(id="pd", title="Project Draft", project=proj)
    by_id = draft.get(id="pd").body
    by_project = draft.get(project=proj).body
    assert by_project == by_id


def test_get_by_project_no_draft_raises_not_found(
    draft: DraftHandler, hub: Hub
) -> None:
    proj = _proj(hub)
    with pytest.raises(NotFound, match="no draft bound"):
        draft.get(project=proj)


def test_get_id_and_project_together_is_bad_input(
    draft: DraftHandler, hub: Hub
) -> None:
    proj = _proj(hub)
    draft.put(id="pd2", title="T", project=proj)
    with pytest.raises(BadInput, match="id= or project="):
        draft.get(id="pd2", project=proj)


# ---------------------------------------------------------------------------
# Wire-level: edit(kind='draft', scaffold=…) / get(kind='draft', project=…)
# through precis.tools.core — mirrors test_mermaid.py's
# test_mcp_edit_tool_persists_vocab_and_notes / test_chunk_review.py's
# test_mcp_edit_tool_records_review pattern. Regression guard: a param
# missing from the tools/core.py wire signature is silently dropped before
# reaching the handler even though the handler itself accepts it.
# ---------------------------------------------------------------------------


def test_mcp_edit_tool_scaffolds_draft(
    monkeypatch, hub: Hub, runtime_with_store
) -> None:
    import precis.tools.core as core

    monkeypatch.setattr(core, "_runtime", runtime_with_store)

    proj = _proj(hub)
    draft = DraftHandler(hub=hub)
    draft.put(id="wire-scaffold", title="T", project=proj)

    out = core.edit(kind="draft", id="wire-scaffold", scaffold="summary")
    assert isinstance(out, str) and "scaffolded 4 sections" in out
    ro = _order(hub, "wire-scaffold")
    assert [c.text for c in ro[1:]] == [
        "Summary",
        "Key Points",
        "Details",
        "References",
    ]


def test_mcp_get_tool_looks_up_draft_by_project(
    monkeypatch, hub: Hub, runtime_with_store
) -> None:
    import precis.tools.core as core

    monkeypatch.setattr(core, "_runtime", runtime_with_store)

    proj = _proj(hub)
    draft = DraftHandler(hub=hub)
    draft.put(id="wire-project", title="Wired", project=proj)

    out = core.get(kind="draft", project=proj)
    assert isinstance(out, str) and "Wired" in out and "wire-project" in out


# ---------------------------------------------------------------------------
# Machine-owned draft guard — a quest dossier (or its paper projection) is
# structured/rewritten by its owning process's own code
# (precis.quest.dossier), never by an agent through this handler's ordinary
# put/edit/delete surface. gr-precedent: a generic draft-hygiene todo once
# executed against a live dossier through exactly this surface, retiring its
# narrative AND its pinned ledger chunk and silently losing the whole
# attempt-tree ledger (quest 202469 / dossier 202546, Aug 2026).
# ---------------------------------------------------------------------------


def _make_dossier(hub: Hub) -> tuple[int, str, str]:
    """Mint an owner ref + its dossier draft via the real quest-side
    machine writer (`precis.quest.dossier.ensure_dossier`) — the same path
    a live quest tick takes. Returns (owner_ref_id, dossier_slug, narrative_dc)."""
    from precis.quest.dossier import ensure_dossier

    owner = hub.live_store.insert_ref(kind="todo", slug=None, title="Owner process")
    dossier_ref_id = ensure_dossier(hub.live_store, owner.id)
    ref = hub.live_store.get_ref(kind="draft", id=dossier_ref_id)
    assert ref is not None and ref.slug is not None
    body = hub.live_store.drafts.reading_order(dossier_ref_id)
    narrative = next(c for c in body if c.chunk_kind != "heading")
    return owner.id, ref.slug, narrative.dc


def test_put_refuses_on_dossier_owned_draft(draft: DraftHandler, hub: Hub) -> None:
    owner_id, slug, _narrative_dc = _make_dossier(hub)
    with pytest.raises(Unsupported, match="dossier") as ei:
        draft.put(
            id=slug, chunk_kind="paragraph", text="agent-added text", at={"last": True}
        )
    assert str(owner_id) in str(ei.value)


def test_edit_refuses_on_dossier_owned_draft(draft: DraftHandler, hub: Hub) -> None:
    _owner_id, _slug, narrative_dc = _make_dossier(hub)
    with pytest.raises(Unsupported, match="dossier"):
        draft.edit(id=narrative_dc, text="agent rewrite")


def test_delete_refuses_on_dossier_owned_draft(draft: DraftHandler, hub: Hub) -> None:
    _owner_id, _slug, narrative_dc = _make_dossier(hub)
    with pytest.raises(Unsupported, match="dossier"):
        draft.delete(id=narrative_dc)


def test_edit_title_and_scaffold_also_refuse_on_dossier_owned_draft(
    draft: DraftHandler, hub: Hub
) -> None:
    """Draft-level edit ops (title=/scaffold=/…) resolve through
    ``_resolve_draft_any``, the same guarded path — not just the per-chunk
    text-edit branch."""
    _owner_id, slug, _narrative_dc = _make_dossier(hub)
    with pytest.raises(Unsupported, match="dossier"):
        draft.edit(id=slug, title="Retitled")
    with pytest.raises(Unsupported, match="dossier"):
        draft.edit(id=slug, scaffold="summary")


def test_paper_of_owned_draft_also_refused(draft: DraftHandler, hub: Hub) -> None:
    """The sibling ``paper-of`` relation (the reader-facing paper
    projection) is guarded identically to ``dossier-of``."""
    owner = hub.live_store.insert_ref(kind="todo", slug=None, title="Paper owner")
    proj = hub.live_store.insert_ref(kind="todo", slug=None, title="Paper project")
    ref, _heading = hub.live_store.drafts.create_draft(
        name="paper-proj",
        title="A Paper",
        project_ref_id=proj.id,
        relation="draft-of",
    )
    hub.live_store.add_link(src_ref_id=ref.id, dst_ref_id=owner.id, relation="paper-of")
    with pytest.raises(Unsupported, match="paper") as ei:
        draft.edit(id="paper-proj", title="Retitled")
    assert str(owner.id) in str(ei.value)


def test_ordinary_draft_unaffected_by_machine_owner_guard(
    draft: DraftHandler, hub: Hub
) -> None:
    """(b) the guard is scoped to dossier-of/paper-of-owned drafts only — a
    plain project draft's put/edit/delete are untouched."""
    proj = _proj(hub)
    draft.put(id="plain", title="Plain Draft", project=proj)
    r = draft.put(
        id="plain", chunk_kind="paragraph", text="hello world", at={"last": True}
    )
    handle = _dc(r.body)
    draft.edit(id=handle, text="hello again")  # no raise
    draft.delete(id=handle)  # no raise


def test_machine_write_path_bypasses_handler_and_still_works(
    draft: DraftHandler, hub: Hub
) -> None:
    """(c) the CRITICAL regression check: the quest tick's own writers
    (`store.edit_text` / `store.add_chunks`, exactly what
    `precis.quest.dossier.rewrite_dossier`/`add_attempt` call) go straight
    to the store, never through this handler — so they must keep working on
    a dossier ref even though the agent-facing put/edit/delete verbs now
    refuse it. A guard that also blocked the quest tick would break every
    live quest."""
    from precis.quest.dossier import add_attempt, read_ledger, read_narrative

    owner_id, slug, narrative_dc = _make_dossier(hub)

    # First, confirm the handler DOES refuse this ref (sanity for the
    # regression check below to mean anything).
    with pytest.raises(Unsupported):
        draft.edit(id=narrative_dc, text="agent rewrite")

    # The machine path: rewrite_dossier (store.edit_text) + add_attempt
    # (store.edit_text on the pinned ledger chunk) — both bypass
    # DraftHandler entirely and must succeed.
    from precis.quest.dossier import rewrite_dossier

    rewrite_dossier(hub.live_store, owner_id, "The current understanding.")
    assert read_narrative(hub.live_store, owner_id) == "The current understanding."

    assert add_attempt(hub.live_store, owner_id, "try approach X") is True
    assert "try approach X" in read_ledger(hub.live_store, owner_id)

    # And the low-level store ops `ensure_dossier`/`rewrite_dossier` call
    # under the hood — same as the module docstring — also still work
    # directly.
    dossier_ref_id = hub.live_store.get_ref(kind="draft", id=slug)
    assert dossier_ref_id is not None
    hub.live_store.drafts.add_chunks(
        ref_id=dossier_ref_id.id, chunk_kind="paragraph", text="scratch", split=False
    )

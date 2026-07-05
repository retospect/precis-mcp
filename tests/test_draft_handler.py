"""DraftHandler — the verb surface over the draft store ops (ADR 0033)."""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Gone, NotFound
from precis.handlers.draft import DraftHandler
from precis.store.store import Store


def _dc(body: str) -> str:
    """Extract the ADR 0036 ``dc<id>`` handle from a draft response."""
    m = re.search(r"dc\d+", body)
    assert m is not None, f"no dc handle in {body!r}"
    return m.group(0)


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _proj(hub: Hub) -> int:
    return hub.store.insert_ref(kind="todo", slug=None, title="Proj").id


def _order(hub: Hub, slug: str) -> list:
    ref = hub.store.get_ref(kind="draft", id=slug)
    return hub.store.reading_order(ref.id)


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
    assert hub.store.get_draft_chunk(para_h).text == "The original paragraph text."

    # find-replace, dry-run → preview, no write
    r2 = draft.edit(id=f"¶{para_h}", find="original", text="pristine", dry_run=True)
    assert "[dry-run]" in r2.body
    assert hub.store.get_draft_chunk(para_h).text == "The original paragraph text."

    # dry_run='full' shows the whole post-edit text
    r3 = draft.edit(id=f"¶{para_h}", text="Brand new body.", dry_run="full")
    assert "Brand new body." in r3.body
    assert hub.store.get_draft_chunk(para_h).text == "The original paragraph text."

    # Applying for real (no dry_run) still writes.
    draft.edit(id=f"¶{para_h}", text="Committed text.")
    assert hub.store.get_draft_chunk(para_h).text == "Committed text."


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
    assert hub.store.get_draft_chunk(intro_h).text == "Intro v2"

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
    validation gate (docs/design/draft-inline-editor.md). A dead ref already
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
    web `/drafts/{id}/block` endpoint calls `store.add_chunks` directly (the
    `put` verb rejects empty `text=`). It lands right after the anchor in
    reading order, ready to type into."""
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].handle
    draft.put(
        id="nt", chunk_kind="paragraph", text="First.", at={"after": "¶" + title_h}
    )
    para_h = _order(hub, "nt")[1].handle
    ref = hub.store.get_ref(kind="draft", id="nt")
    chunks = hub.store.add_chunks(
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
    ref = hub.store.get_ref(kind="draft", id="nt")
    hub.store.edit_text(para_h, "Hello ")
    new = hub.store.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="world",
        at={"after": "¶" + para_h},
        split=False,
    )[0]
    assert (
        hub.store.get_draft_chunk(para_h).text == "Hello "
    )  # first keeps handle + before
    assert hub.store.get_draft_chunk(new.handle).text == "world"
    handles = [c.handle for c in _order(hub, "nt")]
    assert handles.index(new.handle) == handles.index(para_h) + 1


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
    with hub.store.pool.connection() as conn:
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


def test_numeric_paper_ref_hints_chunk_handle_form(
    draft: DraftHandler, hub: Hub
) -> None:
    """Writing a paper citation as a bare `paper:<id>` mention nudges
    toward the canonical inline chunk handle `[pc<id>]`; a bare handle
    citation does not trigger the hint."""
    proj = _proj(hub)
    paper = hub.store.insert_ref(kind="paper", slug="liu24", title="Liu 2024")
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
    # ADR 0036 sibling span (supersedes the legacy -B+A window): 1 before,
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
    """ADR 0036 relative nav over the draft tree: ^ (ancestor), +N/-N
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
    matches the chunk's content_sha is rejected (ADR 0033 — don't clobber
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
    assert hub.store.get_draft_chunk(para_h).text == "v2"

    # the same (now stale) base_sha → rejected, text unchanged
    with pytest.raises(BadInput, match="changed since you read it"):
        draft.edit(id=para_h, text="v3", base_sha=stale)
    assert hub.store.get_draft_chunk(para_h).text == "v2"

    # no base_sha → force overwrite still works
    draft.edit(id=para_h, text="v4")
    assert hub.store.get_draft_chunk(para_h).text == "v4"


def test_chunk_read_surfaces_sha(draft: DraftHandler, hub: Hub) -> None:
    from precis.store._draft_ops import content_sha

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_h = _order(hub, "nt")[0].handle
    out = draft.get(id=f"¶{title_h}").body
    # Read shows a 12-char sha prefix, not the full 64-hex digest.
    assert f"sha:{content_sha('T')[:12]}" in out
    assert content_sha("T") not in out  # full digest is not shown


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
    assert hub.store.get_draft_chunk(para_h).text == "v2"

    full = content_sha("v2")  # full digest is also a valid prefix
    draft.edit(id=para_h, text="v3", base_sha=full)
    assert hub.store.get_draft_chunk(para_h).text == "v3"


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
    (meta.short) and marking not_abbrev both clear it (ADR 0033)."""
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
    ref = hub.store.get_ref(kind="draft", id="nt")
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

    abb = hub.store.defined_abbrevs(ref.id)
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
    todo = hub.store.insert_ref(kind="todo", slug=None, title="tighten")
    hub.store.stamp_ref_meta(todo.id, {"anchor": f"¶{para_h}"})
    hub.store.add_tag(todo.id, Tag.open("ask-user:which-para"))

    out = _requests_by_handle(hub.store, [para_h])  # must not raise
    reqs = out.get(para_h, [])
    assert any(r["asking"] == "which-para" for r in reqs)


def test_resolve_ask_question_resolves_see_chunk_overflow(hub: Hub) -> None:
    """A >80-char ask-user question overflows into a ``tag_overflow`` chunk
    and the tag becomes ``ask-user:see-chunk-N``. resolve_ask_question must
    read the chunk back so the UI shows the real question, not the opaque
    "see chunk 0" slug. Short inline questions and the bare marker pass
    through unchanged."""
    from precis.store.types import BlockInsert

    store = hub.store
    todo = store.insert_ref(kind="todo", slug=None, title="fix bolding")
    q = (
        "Which did you mean? (A) fold the ~100 label-headings back inline "
        "(B) point me at a specific chunk (C) a renderer/export setting."
    )
    store.insert_blocks(
        todo.id,
        [
            BlockInsert(
                pos=0,
                text=f"ask-user: {q}",
                meta={"chunk_kind": "tag_overflow", "tag_namespace": "ask-user"},
            )
        ],
    )
    assert store.resolve_ask_question(todo.id, "see-chunk-0") == q
    assert store.resolve_ask_question(todo.id, "which para?") == "which para?"
    assert store.resolve_ask_question(todo.id, "") == ""
    assert store.resolve_ask_question(todo.id, "see-chunk-9") == ""


def test_requests_by_handle_surfaces_question_and_fail_reason(
    draft: DraftHandler, hub: Hub
) -> None:
    """The reader's per-block panel must show the real ask-user question
    (resolving a see-chunk redirect) and *why* a child job failed (its
    job_summary), so the operator never sees a bare "see chunk 0" / "failed".
    """
    from precis.store.types import BlockInsert, Tag
    from precis_web.routes.drafts import _requests_by_handle

    store = hub.store
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
    store.insert_blocks(
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
    store.insert_blocks(
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
    dref = hub.store.get_ref(kind="draft", id="nt")
    mem = hub.store.insert_ref(kind="memory", slug=None, title="A dreamt idea")
    with hub.store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO links (src_ref_id, src_chunk_id, dst_ref_id, relation, set_by) "
            "VALUES (%s, %s, %s, 'derived-from', 'agent')",
            (dref.id, para.chunk_id, mem.id),
        )

    conns = hub.store.chunk_connections(dref.id, [para.handle])
    assert conns[para.handle][0]["kind"] == "memory"
    assert conns[para.handle][0]["title"] == "A dreamt idea"
    assert conns[para.handle][0]["relation"] == "derived-from"
    assert conns[para.handle][0]["direction"] == "out"

    # edit the chunk → an 'edited' event is logged
    draft.edit(id=f"¶{para.handle}", text="A revised claim.")
    stats = hub.store.chunk_edit_stats(dref.id, [para.handle])
    assert stats[para.handle]["edits"] >= 1


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

    The link verb exists on drafts now (folder placement, ADR 0045);
    any other relation raises with both the placement recipe and the
    embed-a-handle-in-prose teaching (formerly a runtime verb
    redirect on the unsupported-verb path).
    """
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import make_embedder
    from precis.runtime import PrecisRuntime

    store = hub.store
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

    store = hub.store
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
    assert f"job:{job.id} failed" in out


def test_outline_clean_draft_has_no_work_section(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    assert "Work in progress" not in draft.get(id="nt").body


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
    assert "word_target" in (hub.store.get_draft_chunk(intro).meta or {})
    draft.edit(id=intro, word_target={})  # clear
    assert "word_target" not in (hub.store.get_draft_chunk(intro).meta or {})


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
    hub.store.retire_chunk(para_h)
    with pytest.raises(Gone, match="retired"):
        draft.edit(id="¶" + para_h, text="new text")
    # An unknown / garbage handle → typed NotFound (not the opaque fallback).
    with pytest.raises(NotFound):
        draft.edit(id="¶zzzznotarealhandle", text="x")


def test_edit_text_store_op_typed_errors(store: Store) -> None:
    """Store-level guard: ``edit_text`` on a retired chunk raises ``Gone``,
    on an unknown handle raises ``NotFound`` — never a bare ValueError."""
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    ref, _title = store.create_draft(name="es", title="T", project_ref_id=proj)
    para = store.add_chunks(ref_id=ref.id, chunk_kind="paragraph", text="doomed")
    handle = para[-1].handle
    store.retire_chunk(handle)
    with pytest.raises(Gone, match="retired"):
        store.edit_text(handle, "new text")
    with pytest.raises(NotFound):
        store.edit_text("zzzznotarealhandle", "x")


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
    ref = hub.store.get_ref(kind="draft", id="byl")
    # persisted to the first-class authors column, affiliation/ror preserved,
    # names canonicalised to the sortable {"name"} shape.
    assert ref.authors == [
        {"name": "Doe, Jane", "affiliation": "MIT", "ror": "https://ror.org/x"},
        {"name": "Roe, John"},
    ]

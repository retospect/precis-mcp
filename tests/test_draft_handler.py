"""DraftHandler — the verb surface over the draft store ops (ADR 0033)."""

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
    assert "Title" in out and "¶" in out and "[heading]" in out


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


def test_numeric_paper_ref_hints_cite_key_form(draft: DraftHandler, hub: Hub) -> None:
    """Writing a paper citation as `paper:<numeric>` (or `paper:slug`)
    nudges toward the canonical `[§<cite_key>~n]`; a correct `[§…]` does
    not trigger the hint."""
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
    assert f"paper:{paper.id}~3" in r.body
    assert "[§liu24~3]" in r.body  # suggests the cite_key sigil form

    r2 = draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="A second mechanism is plausible [§liu24~5].",
        at={"after": "¶" + th},
    )
    assert "cite papers as" not in r2.body  # the canonical form is fine


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
    mid = order[2].handle  # "b"
    body = draft.get(id=f"¶{mid}-1+1").body  # 1 before, 1 after → a, b, c
    assert "a" in body and "b" in body and "c" in body


def _handle_of(hub: Hub, text: str) -> str:
    return next(c.handle for c in _order(hub, "nt") if c.text == text)


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
    bg = _handle_of(hub, "Background")
    assert f"¶{bg}" in toc and "Background" in toc

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
    para_h = _order(hub, "nt")[1].handle

    stale = content_sha("original")
    # correct base_sha → succeeds, chunk now says v2
    draft.edit(id=f"¶{para_h}", text="v2", base_sha=stale)
    assert hub.store.get_draft_chunk(para_h).text == "v2"

    # the same (now stale) base_sha → rejected, text unchanged
    with pytest.raises(BadInput, match="changed since you read it"):
        draft.edit(id=f"¶{para_h}", text="v3", base_sha=stale)
    assert hub.store.get_draft_chunk(para_h).text == "v2"

    # no base_sha → force overwrite still works
    draft.edit(id=f"¶{para_h}", text="v4")
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
    para = draft.put(id="nt", chunk_kind="paragraph", text="original", at={"last": True})
    para_h = para.body.split("¶")[1].split()[0]

    short = content_sha("original")[:12]
    draft.edit(id=f"¶{para_h}", text="v2", base_sha=short)  # prefix → succeeds
    assert hub.store.get_draft_chunk(para_h).text == "v2"

    full = content_sha("v2")  # full digest is also a valid prefix
    draft.edit(id=f"¶{para_h}", text="v3", base_sha=full)
    assert hub.store.get_draft_chunk(para_h).text == "v3"


def test_edit_rejects_too_short_sha(draft: DraftHandler, hub: Hub) -> None:
    from precis.errors import BadInput

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    para = draft.put(id="nt", chunk_kind="paragraph", text="original", at={"last": True})
    para_h = para.body.split("¶")[1].split()[0]
    with pytest.raises(BadInput, match="too short"):
        draft.edit(id=f"¶{para_h}", text="v2", base_sha="abc")


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
    para_h = next(c.handle for c in _order(hub, "nt") if c.text.startswith("We graft"))

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
    r2 = draft.edit(id=f"¶{para_h}", text="We graft KSJW onto the MOF again.")
    assert "undefined abbreviation" not in r2.body


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
    assert any(r["asking"] == "which para" for r in reqs)


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

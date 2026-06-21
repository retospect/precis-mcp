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
    assert f"sha:{content_sha('T')}" in out


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

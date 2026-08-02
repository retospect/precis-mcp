"""Draft fork/deep-copy primitive: chunks + hierarchy + links copied into
a NEW draft bound to a project, source untouched (fork_draft /
put(kind='draft', copy_of='<src-slug>', project=<todo>))."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.draft import DraftHandler
from precis.store._draft_ops import _remap_intra_draft_xrefs, content_sha
from precis.store.store import Store


def _project(store: Store, title: str = "Project") -> int:
    return store.insert_ref(kind="todo", slug=None, title=title).id


def _make_source_draft(store: Store) -> tuple[int, int]:
    """A small draft with a heading → two paragraph children (one
    retired), a figure with a blob, a chunk tag, and both an internal
    ``plots`` self-link and an inbound ``related-to`` link from another
    ref. Returns (src_ref_id, project_id)."""
    proj = _project(store, "Source project")
    ref, title = store.create_draft(
        name="src", title="Source Draft", project_ref_id=proj
    )
    chunks = store.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="first paragraph\n\nsecond paragraph",
        at={"after": title.dc},
    )
    first, second = chunks
    # retire the second paragraph — the copy must preserve retired_at
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET retired_at = now() WHERE chunk_id = %s",
            (second.chunk_id,),
        )
    fig = store.add_figure(
        ref_id=ref.id,
        caption="Fig 1. widget",
        origin="original",
        image=b"\x89PNG\r\n\x1a\nfakebytes",
        mime="image/png",
        at={"after": first.dc},
    )
    # a chunk tag on the figure
    with store.pool.connection() as conn:
        tag_row = conn.execute(
            "INSERT INTO tags (namespace, value) VALUES ('OPEN', 'test-fork') "
            "ON CONFLICT (namespace, value) DO UPDATE SET namespace = EXCLUDED.namespace "
            "RETURNING tag_id"
        ).fetchone()
        tag_id = int(tag_row[0])
        conn.execute(
            "INSERT INTO chunk_tags (chunk_id, tag_id, set_by) VALUES (%s, %s, 'agent')",
            (fig.chunk_id, tag_id),
        )
    # an intra-draft self-link (plots): figure "plots" the first paragraph
    store.link_figure_plots(fig.chunk_id, [first.chunk_id])
    # an inbound related-to link from another ref
    other = store.insert_ref(kind="memory", slug=None, title="external note").id
    store.add_link(src_ref_id=other, dst_ref_id=ref.id, relation="related-to")
    # a chunk-scoped human review on the first paragraph — must NOT copy
    store.record_review(first.chunk_id, "human", verdict="approved")
    return ref.id, proj


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


# ---------------------------------------------------------------------------
# _remap_intra_draft_xrefs — the pure text-rewrite primitive
# ---------------------------------------------------------------------------


def test_remap_xrefs_rewrites_bare_display_and_legacy_forms() -> None:
    id_map = {41: 987, 42: 988}
    handle_to_new_id = {"5BL5xQ": 989}
    text = "See [dc41] and the [second one](dc42) below; legacy [¶5BL5xQ] anchor too."
    out = _remap_intra_draft_xrefs(text, id_map, handle_to_new_id)
    assert out == (
        "See [dc987] and the [second one](dc988) below; legacy [dc989] anchor too."
    )


def test_remap_xrefs_leaves_cross_draft_and_other_kinds_alone() -> None:
    """A ``dc<id>`` naming a chunk NOT in the source draft (a cross-draft
    ref — its chunk_id is absent from id_map) is untouched, as are other
    kinds' handles (``pc``/``me``/``pa``) and an unmapped legacy anchor."""
    id_map = {41: 987}
    text = "intra [dc41], cross [dc50000], cite [pc12], note [me7], legacy [¶ZZZ]"
    out = _remap_intra_draft_xrefs(text, id_map, {})
    assert (
        out == "intra [dc987], cross [dc50000], cite [pc12], note [me7], legacy [¶ZZZ]"
    )


def test_remap_xrefs_noop_when_no_refs() -> None:
    assert _remap_intra_draft_xrefs("plain prose, no handles", {41: 987}, {}) == (
        "plain prose, no handles"
    )


# ---------------------------------------------------------------------------
# store.fork_draft
# ---------------------------------------------------------------------------


def test_fork_preserves_chunk_count_and_hierarchy(store: Store) -> None:
    src_ref_id, _proj = _make_source_draft(store)
    dst_proj = _project(store, "Dest project")
    new_ref = store.fork_draft(src_ref_id, dst_proj, new_slug="dst")

    src_all = store.reading_order(src_ref_id)  # live only
    # reading_order is live-only; fetch ALL (incl. retired) for both sides
    with store.pool.connection() as conn:
        src_rows = conn.execute(
            "SELECT chunk_id, chunk_kind, pos, parent_chunk_id, retired_at "
            "FROM chunks WHERE ref_id = %s ORDER BY chunk_id",
            (src_ref_id,),
        ).fetchall()
        dst_rows = conn.execute(
            "SELECT chunk_id, chunk_kind, pos, parent_chunk_id, retired_at "
            "FROM chunks WHERE ref_id = %s ORDER BY chunk_id",
            (new_ref.id,),
        ).fetchall()
    assert len(dst_rows) == len(src_rows)
    assert [r[1] for r in dst_rows] == [r[1] for r in src_rows]  # same chunk_kind order
    # a retired source chunk copies as retired
    n_retired_src = sum(1 for r in src_rows if r[4] is not None)
    n_retired_dst = sum(1 for r in dst_rows if r[4] is not None)
    assert n_retired_src == 1
    assert n_retired_dst == 1

    # hierarchy preserved: same number of live children under the (mapped) root
    assert len(src_all) == len(store.reading_order(new_ref.id))
    for src_c, dst_c in zip(src_all, store.reading_order(new_ref.id), strict=True):
        assert src_c.depth == dst_c.depth
        assert src_c.chunk_kind == dst_c.chunk_kind
        assert src_c.text == dst_c.text
        assert src_c.pos == dst_c.pos


def test_fork_mints_fresh_handles(store: Store) -> None:
    src_ref_id, _proj = _make_source_draft(store)
    dst_proj = _project(store, "Dest project 2")
    new_ref = store.fork_draft(src_ref_id, dst_proj, new_slug="dst2")

    with store.pool.connection() as conn:
        src_handles = {
            r[0]
            for r in conn.execute(
                "SELECT handle FROM chunks WHERE ref_id = %s", (src_ref_id,)
            ).fetchall()
        }
        dst_handles = {
            r[0]
            for r in conn.execute(
                "SELECT handle FROM chunks WHERE ref_id = %s", (new_ref.id,)
            ).fetchall()
        }
    assert src_handles.isdisjoint(dst_handles)
    assert len(dst_handles) == len(src_handles)


def test_fork_copies_blob_and_tag_side_tables(store: Store) -> None:
    src_ref_id, _proj = _make_source_draft(store)
    dst_proj = _project(store, "Dest project 3")
    new_ref = store.fork_draft(src_ref_id, dst_proj, new_slug="dst3")

    with store.pool.connection() as conn:
        src_fig = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND chunk_kind = 'figure'",
            (src_ref_id,),
        ).fetchone()[0]
        dst_fig = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND chunk_kind = 'figure'",
            (new_ref.id,),
        ).fetchone()[0]
        src_blob = conn.execute(
            "SELECT bytes, mime FROM chunk_blobs WHERE chunk_id = %s", (src_fig,)
        ).fetchone()
        dst_blob = conn.execute(
            "SELECT bytes, mime FROM chunk_blobs WHERE chunk_id = %s", (dst_fig,)
        ).fetchone()
        assert dst_blob is not None
        assert bytes(dst_blob[0]) == bytes(src_blob[0])
        assert dst_blob[1] == src_blob[1]

        src_tags = conn.execute(
            "SELECT tag_id FROM chunk_tags WHERE chunk_id = %s", (src_fig,)
        ).fetchall()
        dst_tags = conn.execute(
            "SELECT tag_id FROM chunk_tags WHERE chunk_id = %s", (dst_fig,)
        ).fetchall()
        assert {t[0] for t in dst_tags} == {t[0] for t in src_tags}
        assert len(dst_tags) > 0


def test_fork_links_remapped_not_shared_with_source(store: Store) -> None:
    src_ref_id, _proj = _make_source_draft(store)
    dst_proj = _project(store, "Dest project 4")
    new_ref = store.fork_draft(src_ref_id, dst_proj, new_slug="dst4")

    with store.pool.connection() as conn:
        _src_row = conn.execute(
            "SELECT "
            " (SELECT chunk_id FROM chunks WHERE ref_id=%s AND chunk_kind='figure'), "
            " (SELECT chunk_id FROM chunks WHERE ref_id=%s AND chunk_kind='paragraph' "
            "  ORDER BY chunk_id LIMIT 1)",
            (src_ref_id, src_ref_id),
        ).fetchone()
        assert _src_row is not None
        src_fig, src_first = _src_row
        _dst_row = conn.execute(
            "SELECT "
            " (SELECT chunk_id FROM chunks WHERE ref_id=%s AND chunk_kind='figure'), "
            " (SELECT chunk_id FROM chunks WHERE ref_id=%s AND chunk_kind='paragraph' "
            "  ORDER BY chunk_id LIMIT 1)",
            (new_ref.id, new_ref.id),
        ).fetchone()
        assert _dst_row is not None
        dst_fig, dst_first = _dst_row

    # the copied intra-draft `plots` edge points at the COPY's own chunks
    plots = store.links_for(new_ref.id, direction="out", relation="plots")
    assert len(plots) == 1
    assert plots[0].src_ref_id == new_ref.id
    assert plots[0].dst_ref_id == new_ref.id
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT src_chunk_id, dst_chunk_id FROM links WHERE link_id = %s",
            (plots[0].id,),
        ).fetchone()
    assert row[0] == dst_fig
    assert row[1] == dst_first
    # never src's chunks
    assert row[0] != src_fig
    assert row[1] != src_first

    # the inbound related-to edge now points at the copy too, in addition
    # to the untouched original edge into the source
    src_in = store.links_for(src_ref_id, direction="in", relation="related-to")
    dst_in = store.links_for(new_ref.id, direction="in", relation="related-to")
    assert len(src_in) == 1  # source's inbound edge is untouched, not duplicated
    assert len(dst_in) == 1  # the copy inherited its own inbound edge


def test_fork_rewrites_intra_draft_prose_xrefs(store: Store) -> None:
    """An in-prose ``[dc<id>]`` / ``[cap](dc<id>)`` cross-ref points at the
    COPY's own chunks, not the source's (these live in the text, not the
    links table, so verbatim copy would dangle them back into the source);
    a cross-draft ref is left alone, and content_sha stays consistent."""
    proj = _project(store, "Xref source project")
    ref, title = store.create_draft(
        name="xref-src", title="Xref Source", project_ref_id=proj
    )
    # target paragraph the cross-refs point at (need its id first)
    [target] = store.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="the target paragraph",
        at={"after": title.dc},
    )
    # a chunk in ANOTHER draft — a cross-draft ref that must NOT be rewritten
    other_proj = _project(store, "Other project")
    other_ref, other_title = store.create_draft(
        name="xref-other", title="Other", project_ref_id=other_proj
    )
    [other_chunk] = store.add_chunks(
        ref_id=other_ref.id,
        chunk_kind="paragraph",
        text="external",
        at={"after": other_title.dc},
    )
    # the referencing paragraph: intra-draft bare + display-link, plus a
    # cross-draft bare ref to leave alone
    ref_text = (
        f"As shown in [dc{target.chunk_id}] and [the target]"
        f"(dc{target.chunk_id}); compare [dc{other_chunk.chunk_id}]."
    )
    [referrer] = store.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text=ref_text,
        at={"after": target.dc},
    )

    dst_proj = _project(store, "Xref dest project")
    new_ref = store.fork_draft(ref.id, dst_proj, new_slug="xref-dst")

    # align source ↔ copy by reading-order index to find the copied chunks
    src_order = store.reading_order(ref.id)
    dst_order = store.reading_order(new_ref.id)
    assert len(src_order) == len(dst_order)
    ti = next(i for i, s in enumerate(src_order) if s.chunk_id == target.chunk_id)
    ri = next(i for i, s in enumerate(src_order) if s.chunk_id == referrer.chunk_id)
    dst_target = dst_order[ti]
    dst_referrer = dst_order[ri]

    expected = (
        f"As shown in [dc{dst_target.chunk_id}] and [the target]"
        f"(dc{dst_target.chunk_id}); compare [dc{other_chunk.chunk_id}]."
    )
    assert dst_referrer.text == expected
    # the intra-draft ref was actually remapped (not left as the source id)
    assert f"dc{target.chunk_id}" not in dst_referrer.text
    # the cross-draft ref is untouched
    assert f"[dc{other_chunk.chunk_id}]" in dst_referrer.text

    # content_sha (and the created chunk_events row) track the rewritten text
    with store.pool.connection() as conn:
        sha_row = conn.execute(
            "SELECT c.content_sha, "
            " (SELECT content_sha FROM chunk_events "
            "  WHERE chunk_id = c.chunk_id AND event_kind = 'created') "
            "FROM chunks c WHERE c.chunk_id = %s",
            (dst_referrer.chunk_id,),
        ).fetchone()
    assert sha_row is not None
    assert sha_row[0] == content_sha(expected)
    assert sha_row[1] == content_sha(expected)


def test_fork_original_draft_untouched(store: Store) -> None:
    src_ref_id, _proj = _make_source_draft(store)
    with store.pool.connection() as conn:
        before_chunks = conn.execute(
            "SELECT chunk_id, handle FROM chunks WHERE ref_id = %s ORDER BY chunk_id",
            (src_ref_id,),
        ).fetchall()
        before_links = conn.execute(
            "SELECT link_id FROM links WHERE src_ref_id = %s OR dst_ref_id = %s",
            (src_ref_id, src_ref_id),
        ).fetchall()

    dst_proj = _project(store, "Dest project 5")
    store.fork_draft(src_ref_id, dst_proj, new_slug="dst5")

    with store.pool.connection() as conn:
        after_chunks = conn.execute(
            "SELECT chunk_id, handle FROM chunks WHERE ref_id = %s ORDER BY chunk_id",
            (src_ref_id,),
        ).fetchall()
        after_links = conn.execute(
            "SELECT link_id FROM links WHERE src_ref_id = %s OR dst_ref_id = %s",
            (src_ref_id, src_ref_id),
        ).fetchall()
    assert after_chunks == before_chunks
    # every pre-existing link survives untouched; the only addition is the
    # new copy's own `copy-of` provenance edge pointing back at the source
    # (which necessarily touches src_ref_id as its dst).
    before_ids = {r[0] for r in before_links}
    after_ids = {r[0] for r in after_links}
    assert before_ids <= after_ids
    assert len(after_ids) == len(before_ids) + 1


def test_fork_copy_review_ledger_is_empty(store: Store) -> None:
    src_ref_id, _proj = _make_source_draft(store)
    dst_proj = _project(store, "Dest project 6")
    new_ref = store.fork_draft(src_ref_id, dst_proj, new_slug="dst6")

    with store.pool.connection() as conn:
        src_reviewed = conn.execute(
            "SELECT count(*) FROM chunk_review cr "
            "JOIN chunks c ON c.chunk_id = cr.chunk_id WHERE c.ref_id = %s",
            (src_ref_id,),
        ).fetchone()[0]
        dst_reviewed = conn.execute(
            "SELECT count(*) FROM chunk_review cr "
            "JOIN chunks c ON c.chunk_id = cr.chunk_id WHERE c.ref_id = %s",
            (new_ref.id,),
        ).fetchone()[0]
    assert src_reviewed == 1  # the source's human review survives
    assert dst_reviewed == 0  # the copy starts fully unreviewed


def test_fork_binds_project_and_stamps_copy_of(store: Store) -> None:
    src_ref_id, _proj = _make_source_draft(store)
    dst_proj = _project(store, "Dest project 7")
    new_ref = store.fork_draft(src_ref_id, dst_proj, new_slug="dst7")

    draft_of = store.links_for(dst_proj, direction="in", relation="draft-of")
    assert any(link.src_ref_id == new_ref.id for link in draft_of)

    copy_of = store.links_for(new_ref.id, direction="out", relation="copy-of")
    assert len(copy_of) == 1
    assert copy_of[0].dst_ref_id == src_ref_id
    # has-copy mirrors at read time on the SOURCE side (the source is the
    # dst of the literal copy-of edge, so it queries the inverse with
    # direction='out' — same convention as cites/cited-by, gripe 160213)
    has_copy = store.links_for(src_ref_id, direction="out", relation="has-copy")
    assert any(link.src_ref_id == new_ref.id for link in has_copy)


def test_fork_refuses_when_project_already_has_a_draft(store: Store) -> None:
    src_ref_id, _proj = _make_source_draft(store)
    dst_proj = _project(store, "Dest project 8")
    store.create_draft(name="already-there", title="X", project_ref_id=dst_proj)
    with pytest.raises(ValueError, match="already has a draft"):
        store.fork_draft(src_ref_id, dst_proj, new_slug="dst8")


# ---------------------------------------------------------------------------
# handler: put(kind='draft', copy_of=..., project=...)
# ---------------------------------------------------------------------------


def test_handler_fork_creates_bound_copy(draft: DraftHandler, store: Store) -> None:
    src_ref_id, _proj = _make_source_draft(store)
    src_ref = store.get_ref(kind="draft", id=src_ref_id)
    dst_proj = _project(store, "Handler dest")

    resp = draft.put(copy_of=src_ref.slug, project=dst_proj)
    assert "forked" in resp.body

    new_ref = None
    for link in store.links_for(dst_proj, direction="in", relation="draft-of"):
        new_ref = store.get_ref(kind="draft", id=link.src_ref_id)
    assert new_ref is not None
    assert new_ref.slug == f"{src_ref.slug}-copy"
    assert len(store.reading_order(new_ref.id)) == len(store.reading_order(src_ref_id))


def test_handler_fork_project_todo_string_still_works(
    draft: DraftHandler, store: Store
) -> None:
    """The existing ``project=`` forms (int, and ``'todo:N'``) still resolve
    to the EXISTING project, exactly as before — no new-project mint."""
    src_ref_id, _proj = _make_source_draft(store)
    src_ref = store.get_ref(kind="draft", id=src_ref_id)
    dst_proj = _project(store, "Handler dest todo-string")

    resp = draft.put(copy_of=src_ref.slug, project=f"todo:{dst_proj}")
    assert "forked" in resp.body
    draft_of = store.links_for(dst_proj, direction="in", relation="draft-of")
    assert len(draft_of) == 1
    # no NEW project todo was minted — dst_proj's own title is unchanged
    assert store.get_ref(kind="todo", id=dst_proj).title == "Handler dest todo-string"


def test_handler_fork_project_title_mints_new_project(
    draft: DraftHandler, store: Store
) -> None:
    """A non-numeric ``project=`` is a NEW project's title: mint a fresh
    ``meta.rotation_root=true`` project todo with that title and bind the
    fork to it — never fuzzy-matched against an existing project."""
    src_ref_id, _proj = _make_source_draft(store)
    src_ref = store.get_ref(kind="draft", id=src_ref_id)

    resp = draft.put(copy_of=src_ref.slug, project="Nanotrans review pass")
    assert "forked" in resp.body

    new_project = None
    for row_ref in store.list_refs(kind="todo", limit=1000):
        if row_ref.title == "Nanotrans review pass":
            new_project = row_ref
    assert new_project is not None
    assert new_project.meta.get("rotation_root") is True

    draft_of = store.links_for(new_project.id, direction="in", relation="draft-of")
    assert len(draft_of) == 1
    new_ref = store.get_ref(kind="draft", id=draft_of[0].src_ref_id)
    assert new_ref is not None
    assert len(store.reading_order(new_ref.id)) == len(store.reading_order(src_ref_id))


def test_handler_fork_dedupes_slug(draft: DraftHandler, store: Store) -> None:
    src_ref_id, _proj = _make_source_draft(store)
    src_ref = store.get_ref(kind="draft", id=src_ref_id)
    proj1 = _project(store, "Handler dest 2a")
    proj2 = _project(store, "Handler dest 2b")

    draft.put(copy_of=src_ref.slug, project=proj1)
    resp = draft.put(copy_of=src_ref.slug, project=proj2)
    assert f"{src_ref.slug}-copy-2" in resp.body


def test_handler_fork_refuses_project_clobber(
    draft: DraftHandler, store: Store
) -> None:
    src_ref_id, _proj = _make_source_draft(store)
    src_ref = store.get_ref(kind="draft", id=src_ref_id)
    dst_proj = _project(store, "Handler dest 3")
    draft.put(id="already-there", title="X", project=dst_proj)

    with pytest.raises(BadInput, match="already has a draft"):
        draft.put(copy_of=src_ref.slug, project=dst_proj)


def test_handler_fork_requires_project(draft: DraftHandler, store: Store) -> None:
    src_ref_id, _proj = _make_source_draft(store)
    src_ref = store.get_ref(kind="draft", id=src_ref_id)
    with pytest.raises(BadInput, match="requires project"):
        draft.put(copy_of=src_ref.slug)


def test_handler_fork_unknown_source(draft: DraftHandler, store: Store) -> None:
    dst_proj = _project(store, "Handler dest 4")
    with pytest.raises(NotFound):
        draft.put(copy_of="does-not-exist", project=dst_proj)


# ---------------------------------------------------------------------------
# machine-authored provenance + the per-document auto-author toggle
# (paper-writing pipeline rungs 3d/3e)
# ---------------------------------------------------------------------------


def test_authored_provenance_new_chunk_and_edit_stamp(store: Store) -> None:
    """A new chunk stamped via ``meta.authored_by`` (put path) AND the
    latest grounded EXTEND of an existing chunk (``edit_text(source=…)``)
    both surface in ``authored_provenance``; an un-stamped chunk doesn't."""
    src_ref_id, _proj = _make_source_draft(store)
    order = store.reading_order(src_ref_id)
    plain = next(c for c in order if c.chunk_kind == "paragraph")

    # A NEW authored chunk (put(kind='draft', meta={'authored_by': …})).
    [new_chunk] = store.add_chunks(
        ref_id=src_ref_id,
        chunk_kind="paragraph",
        text="A grounded addition.",
        at={"after": plain.handle},
        meta={"authored_by": "review:structure"},
    )

    # A grounded EXTEND of an existing chunk (edit_text(source=…)).
    store.edit_text(
        plain.handle,
        "The extended, grounded paragraph.",
        source={"authored_by": "review:cites"},
    )

    prov = store.authored_provenance(src_ref_id)
    assert prov[new_chunk.chunk_id] == "review:structure"
    assert prov[plain.chunk_id] == "review:cites"
    # A chunk with neither stamp doesn't appear.
    unstamped = [c for c in order if c.chunk_id not in (plain.chunk_id,)]
    assert all(c.chunk_id not in prov for c in unstamped)


def test_authored_provenance_edit_stamp_uses_latest_stamped_edit(store: Store) -> None:
    """When a chunk is edited more than once, the *latest* stamped edit's
    ``authored_by`` wins (the store's ``ORDER BY ts DESC LIMIT 1`` over
    rows carrying a stamp — an interleaved unstamped edit, e.g. a human
    touch-up, doesn't itself clear a prior stamp, since the subquery only
    ranks among stamped rows)."""
    src_ref_id, _proj = _make_source_draft(store)
    order = store.reading_order(src_ref_id)
    plain = next(c for c in order if c.chunk_kind == "paragraph")

    store.edit_text(
        plain.handle, "First grounded edit.", source={"authored_by": "review:cites"}
    )
    assert store.authored_provenance(src_ref_id)[plain.chunk_id] == "review:cites"

    store.edit_text(
        plain.handle,
        "Second grounded edit, different lens.",
        source={"authored_by": "review:structure"},
    )
    assert store.authored_provenance(src_ref_id)[plain.chunk_id] == "review:structure"


def test_draft_authoring_enabled_reflects_stamp_ref_meta(store: Store) -> None:
    """``draft_authoring_enabled`` defaults False and reflects the last
    ``stamp_ref_meta(..., {'authoring_enabled': …})`` write."""
    src_ref_id, _proj = _make_source_draft(store)
    assert store.draft_authoring_enabled(src_ref_id) is False

    store.stamp_ref_meta(src_ref_id, {"authoring_enabled": True})
    assert store.draft_authoring_enabled(src_ref_id) is True

    store.stamp_ref_meta(src_ref_id, {"authoring_enabled": False})
    assert store.draft_authoring_enabled(src_ref_id) is False


def test_handler_edit_authoring_toggles_ref_meta(
    draft: DraftHandler, store: Store
) -> None:
    """``edit(kind='draft', authoring='on'|'off')`` (3e) is a draft-level
    op: id is the slug, and it writes ``refs.meta.authoring_enabled``."""
    src_ref_id, _proj = _make_source_draft(store)
    src_ref = store.get_ref(kind="draft", id=src_ref_id)

    resp = draft.edit(id=src_ref.slug, authoring="on")
    assert "ON" in resp.body
    assert store.draft_authoring_enabled(src_ref_id) is True

    resp = draft.edit(id=src_ref.slug, authoring="off")
    assert "OFF" in resp.body
    assert store.draft_authoring_enabled(src_ref_id) is False

    with pytest.raises(BadInput, match="not understood"):
        draft.edit(id=src_ref.slug, authoring="maybe")

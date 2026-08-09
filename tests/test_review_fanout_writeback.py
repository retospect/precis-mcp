"""Rung 3a/3b of the paper-writing pipeline
(docs/backlog/paper-writing-pipeline.md §"Review — the memoized approval
ledger"):

- 3a — :func:`precis.quest.review_fanout.mint_review_fanout`, the
  whole-draft "review everything" fanout, plus the shared
  :func:`precis.quest.weave_review.mint_review_todo` primitive it and
  :func:`precis.quest.weave_review.mint_weave_reviews` both reuse.
- 3b — the executor's pass-only ``chunk_review`` writeback
  (``workers/executors/claude_inproc.py::_run_plan_tick``): a review-mode
  tick that completes clean (zero findings, unchanged anchor content_sha)
  records ``record_review(chunk_id, lens, 'approved')``; anything else
  records nothing, and the writeback never raises.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.quest.review_fanout import ALL_LENSES, DOC_LENSES, mint_review_fanout
from precis.quest.weave_review import mint_review_todo
from precis.store.store import Store
from precis.store.types import Tag
from precis.utils import handle_registry
from precis.workers.executors import claude_inproc as ci
from precis.workers.job_types.plan_tick import PlanTickOutcome

# A finished review tick: clean exit whose stdout carries a real
# ``=== TICK CONCLUSION ===`` block declaring ``verdict: done`` (the exact
# delimiter ``parse_tick_conclusion`` requires — a bare ``verdict:`` line
# outside the block parses to None). The writeback fires ONLY for this shape.
_CLEAN = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"result":"=== TICK CONCLUSION ===\\nverdict: done\\n=== END ==="}'
)
# A resumable exhaustion (--max-turns) — is_error / nonzero exit; the
# executor marks the job succeeded-but-resumable, but the reviewer did NOT
# finish the section, so no approval may be recorded.
_MAX_TURNS = (
    '{"type":"result","subtype":"error_max_turns","is_error":true,'
    '"terminal_reason":"max_turns","num_turns":30,"result":"partial review"}'
)
# A clean exit whose reviewer explicitly yielded mid-pass (verdict
# continue) — also not a finished review.
_CONTINUE = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"result":"=== TICK CONCLUSION ===\\nverdict: continue\\n=== END ==="}'
)


def _project(store: Store) -> int:
    return store.insert_ref(kind="todo", slug=None, title="Review project").id


def _draft_with_chunks(store: Store, *, name: str, n_paragraphs: int = 1) -> int:
    """A fresh draft, bound draft-of a project todo, with ``n_paragraphs``
    live paragraph chunks under its title heading. Returns the draft
    ref_id."""
    proj = _project(store)
    ref, title = store.create_draft(name=name, title="T", project_ref_id=proj)
    for i in range(n_paragraphs):
        store.add_chunks(
            ref_id=ref.id,
            chunk_kind="paragraph",
            text=f"paragraph {i}",
            at={"after": title.handle},
        )
    return ref.id


# Lens x chunk-kind mapping mirrored from review_fanout.py (smartdraft-
# review-status-ui item 2): flow/cites mint on prose chunks only,
# structure/adversarial on heading chunks only. Used to compute the
# expected minted count for a fixture's exact chunk-kind mix, since a
# blunt "chunks x len(lenses)" no longer holds once kind narrows the set.
_HEADING_LENSES = {"structure", "adversarial"}
_PROSE_LENSES = {"flow", "cites"}


def _expected_pairs(chunks: list[dict[str, Any]], lenses: tuple[str, ...]) -> int:
    total = 0
    for c in chunks:
        allowed = _HEADING_LENSES if c["chunk_kind"] == "heading" else _PROSE_LENSES
        total += len(allowed & set(lenses))
    return total


# ---------------------------------------------------------------------------
# 3a — mint_review_fanout
# ---------------------------------------------------------------------------


class TestMintReviewFanout:
    def test_mints_chunk_times_lens_todos(self, store: Store) -> None:
        ref_id = _draft_with_chunks(store, name="fan1", n_paragraphs=2)
        chunks = store.reviewable_chunks(ref_id)
        assert len(chunks) == 3  # title + 2 paragraphs

        result = mint_review_fanout(store, ref_id)

        expected = _expected_pairs(chunks, ALL_LENSES)
        assert len(result["minted"]) == expected
        assert result["skipped"] == 0
        assert result["chunks_seen"] == len(chunks)

        seen_pairs: set[tuple[str, str]] = set()
        for todo_id in result["minted"]:
            ref = store.get_ref(kind="todo", id=todo_id)
            assert ref is not None
            assert ref.parent_id == result["parent_id"]
            lens = ref.meta.get("review")
            anchor = ref.meta.get("anchor")
            assert lens in ALL_LENSES
            assert anchor is not None
            seen_pairs.add((lens, anchor))

            tags = {(t.namespace, t.prefix, t.value) for t in store.tags_for(todo_id)}
            assert ("closed", "STATUS", "open") in tags
            expected_tier = "sonnet" if lens in ("flow", "cites") else "opus"
            assert ref.meta.get("llm_tier") == expected_tier
            # User-triggered fanout mints in the 0014 band-2 (cron) tier so
            # its plan_ticks aren't starved behind the recurring stream by
            # the prio-ASC claude_inproc claim (_FANOUT_PRIO).
            assert ref.prio == 2

        assert len(seen_pairs) == expected

    def test_parent_is_draft_owning_project_todo(self, store: Store) -> None:
        ref_id = _draft_with_chunks(store, name="fan-parent")
        links = store.links_for(ref_id, direction="out", relation="draft-of")
        assert len(links) == 1
        project_id = links[0].dst_ref_id

        result = mint_review_fanout(store, ref_id)

        assert result["parent_id"] == project_id
        for todo_id in result["minted"]:
            ref = store.get_ref(kind="todo", id=todo_id)
            assert ref is not None
            assert ref.parent_id == project_id

    def test_idempotent_on_repeat_call(self, store: Store) -> None:
        ref_id = _draft_with_chunks(store, name="fan-idem")
        first = mint_review_fanout(store, ref_id)
        assert len(first["minted"]) > 0

        second = mint_review_fanout(store, ref_id)
        assert second["minted"] == []
        assert second["skipped"] == len(first["minted"])

    def test_respects_lenses(self, store: Store) -> None:
        ref_id = _draft_with_chunks(store, name="fan-lenses")
        chunks = store.reviewable_chunks(ref_id)

        result = mint_review_fanout(store, ref_id, lenses=("flow",))

        assert len(result["minted"]) == _expected_pairs(chunks, ("flow",))
        for todo_id in result["minted"]:
            ref = store.get_ref(kind="todo", id=todo_id)
            assert ref is not None
            assert ref.meta.get("review") == "flow"

    def test_skips_non_reviewable_chunks(self, store: Store) -> None:
        ref_id = _draft_with_chunks(store, name="fan-retire", n_paragraphs=2)
        chunks_before = store.reviewable_chunks(ref_id)
        assert len(chunks_before) == 3  # title + 2 paragraphs

        # Retire one paragraph chunk — it drops out of reviewable_chunks
        # (retired_at IS NOT NULL) and must not get a review-todo minted.
        retired_handle = chunks_before[-1]["handle"]
        retired_chunk_id = chunks_before[-1]["chunk_id"]
        store.retire_chunk(retired_handle, kind="draft")

        chunks_after = store.reviewable_chunks(ref_id)
        assert len(chunks_after) == 2
        assert retired_chunk_id not in {c["chunk_id"] for c in chunks_after}

        result = mint_review_fanout(store, ref_id)

        retired_dc = handle_registry.format_handle(
            "draft", retired_chunk_id, chunk=True
        )
        anchors = set()
        for todo_id in result["minted"]:
            ref = store.get_ref(kind="todo", id=todo_id)
            assert ref is not None
            anchors.add(ref.meta.get("anchor"))
        assert retired_dc not in anchors
        assert len(result["minted"]) == _expected_pairs(chunks_after, ALL_LENSES)

    def test_author_flag_stamps_meta_only_on_eligible_lenses(
        self, store: Store
    ) -> None:
        ref_id = _draft_with_chunks(store, name="fan-author")

        result = mint_review_fanout(store, ref_id, author=True)

        author_true_lenses: set[str] = set()
        author_false_lenses: set[str] = set()
        for todo_id in result["minted"]:
            ref = store.get_ref(kind="todo", id=todo_id)
            assert ref is not None
            lens = str(ref.meta.get("review"))
            if ref.meta.get("author") is True:
                author_true_lenses.add(lens)
            else:
                assert "author" not in ref.meta
                author_false_lenses.add(lens)

        assert author_true_lenses == {"cites", "structure"}
        assert author_false_lenses == {"flow", "adversarial"}
        assert result["author_minted"] > 0

    def test_no_author_flag_stamps_no_author_meta_anywhere(self, store: Store) -> None:
        ref_id = _draft_with_chunks(store, name="fan-no-author")

        result = mint_review_fanout(store, ref_id, author=False)

        for todo_id in result["minted"]:
            ref = store.get_ref(kind="todo", id=todo_id)
            assert ref is not None
            assert "author" not in ref.meta
        assert result["author_minted"] == 0

    def test_toggle_on_stamps_author_without_explicit_flag(self, store: Store) -> None:
        """The per-document auto-author toggle (rung 3e) alone — with no
        explicit ``author=`` override — is enough to turn authoring on for
        the eligible lenses."""
        ref_id = _draft_with_chunks(store, name="fan-toggle-on")
        store.stamp_ref_meta(ref_id, {"authoring_enabled": True})

        result = mint_review_fanout(store, ref_id, author=False)

        assert result["author_minted"] > 0
        for todo_id in result["minted"]:
            ref = store.get_ref(kind="todo", id=todo_id)
            assert ref is not None
            lens = str(ref.meta.get("review"))
            if lens in ("cites", "structure"):
                assert ref.meta.get("author") is True
            else:
                assert "author" not in ref.meta

    def test_toggle_off_stamps_no_author(self, store: Store) -> None:
        ref_id = _draft_with_chunks(store, name="fan-toggle-off")
        # Explicitly off — default state, but stamp it to make the intent
        # visible in the test.
        store.stamp_ref_meta(ref_id, {"authoring_enabled": False})

        result = mint_review_fanout(store, ref_id, author=False)

        assert result["author_minted"] == 0
        for todo_id in result["minted"]:
            ref = store.get_ref(kind="todo", id=todo_id)
            assert ref is not None
            assert "author" not in ref.meta

    def test_explicit_author_overrides_toggle_off(self, store: Store) -> None:
        """The CLI/caller ``--author`` override still forces authoring on
        regardless of the toggle's state."""
        ref_id = _draft_with_chunks(store, name="fan-explicit-override")
        store.stamp_ref_meta(ref_id, {"authoring_enabled": False})

        result = mint_review_fanout(store, ref_id, author=True)

        assert result["author_minted"] > 0

    def test_no_draft_of_link_raises(self, store: Store) -> None:
        from precis.errors import BadInput

        # A draft-family ref with no draft-of bind (bypassing create_draft).
        orphan = store.insert_ref(kind="draft", slug="orphan-draft", title="O")
        with pytest.raises(BadInput, match="draft-of"):
            mint_review_fanout(store, orphan.id)


# ---------------------------------------------------------------------------
# review-status incremental re-check — only_dirty
# ---------------------------------------------------------------------------


class TestOnlyDirty:
    def test_fully_approved_draft_mints_zero(self, store: Store) -> None:
        ref_id = _draft_with_chunks(store, name="dirty-full", n_paragraphs=1)
        chunks = store.reviewable_chunks(ref_id)
        for c in chunks:
            allowed = _HEADING_LENSES if c["chunk_kind"] == "heading" else _PROSE_LENSES
            for lens in allowed:
                store.record_review(c["chunk_id"], lens, verdict="approved")

        result = mint_review_fanout(store, ref_id, only_dirty=True)
        assert result["minted"] == []

    def test_editing_one_paragraph_remints_exactly_its_lenses(
        self, store: Store
    ) -> None:
        ref_id = _draft_with_chunks(store, name="dirty-edit", n_paragraphs=2)
        chunks = store.reviewable_chunks(ref_id)
        for c in chunks:
            allowed = _HEADING_LENSES if c["chunk_kind"] == "heading" else _PROSE_LENSES
            for lens in allowed:
                store.record_review(c["chunk_id"], lens, verdict="approved")
        assert mint_review_fanout(store, ref_id, only_dirty=True)["minted"] == []

        target = next(c for c in chunks if c["chunk_kind"] == "paragraph")
        store.edit_text(target["handle"], "edited text bumps the sha")

        result = mint_review_fanout(store, ref_id, only_dirty=True)
        anchor = handle_registry.format_handle("draft", target["chunk_id"], chunk=True)
        minted_pairs = set()
        for todo_id in result["minted"]:
            ref = store.get_ref(kind="todo", id=todo_id)
            assert ref is not None
            minted_pairs.add((ref.meta.get("review"), ref.meta.get("anchor")))
        assert minted_pairs == {(lens, anchor) for lens in _PROSE_LENSES}
        assert len(result["minted"]) == len(_PROSE_LENSES)


# ---------------------------------------------------------------------------
# review-status incremental re-check — scope (subtree / single chunk)
# ---------------------------------------------------------------------------


class TestScope:
    def test_subtree_scope_mints_only_under_heading(self, store: Store) -> None:
        proj = _project(store)
        ref, title = store.create_draft(name="scope1", title="T", project_ref_id=proj)
        section_a = store.add_chunks(
            ref_id=ref.id,
            chunk_kind="heading",
            text="Section A",
            at={"after": title.handle},
        )[0]
        store.add_chunks(
            ref_id=ref.id,
            chunk_kind="paragraph",
            text="para A",
            at={"into": section_a.handle},
        )
        section_b = store.add_chunks(
            ref_id=ref.id,
            chunk_kind="heading",
            text="Section B",
            at={"after": section_a.handle},
        )[0]
        store.add_chunks(
            ref_id=ref.id,
            chunk_kind="paragraph",
            text="para B",
            at={"into": section_b.handle},
        )

        result = mint_review_fanout(store, ref.id, scope=section_a.chunk_id)

        assert result["chunks_seen"] == 2  # section_a heading + its paragraph
        assert len(result["minted"]) == 4  # 2 lenses x 2 chunks
        anchors = set()
        for todo_id in result["minted"]:
            r = store.get_ref(kind="todo", id=todo_id)
            assert r is not None
            anchors.add(r.meta.get("anchor"))
        title_anchor = handle_registry.format_handle(
            "draft", title.chunk_id, chunk=True
        )
        section_b_anchor = handle_registry.format_handle(
            "draft", section_b.chunk_id, chunk=True
        )
        assert title_anchor not in anchors
        assert section_b_anchor not in anchors

    def test_single_prose_chunk_scope_mints_only_that_chunk(self, store: Store) -> None:
        ref_id = _draft_with_chunks(store, name="scope2", n_paragraphs=2)
        chunks = store.reviewable_chunks(ref_id)
        paras = [c for c in chunks if c["chunk_kind"] == "paragraph"]

        result = mint_review_fanout(store, ref_id, scope=paras[0]["chunk_id"])

        assert result["chunks_seen"] == 1
        assert len(result["minted"]) == len(_PROSE_LENSES)
        anchor = handle_registry.format_handle(
            "draft", paras[0]["chunk_id"], chunk=True
        )
        for todo_id in result["minted"]:
            r = store.get_ref(kind="todo", id=todo_id)
            assert r is not None
            assert r.meta.get("anchor") == anchor


# ---------------------------------------------------------------------------
# review-status incremental re-check — skip-unsettled (open change request)
# ---------------------------------------------------------------------------


class TestSkipUnsettled:
    def test_open_change_request_skipped_then_remints_once_resolved(
        self, store: Store
    ) -> None:
        ref_id = _draft_with_chunks(store, name="unsettled", n_paragraphs=1)
        p = next(
            c for c in store.reviewable_chunks(ref_id) if c["chunk_kind"] == "paragraph"
        )
        anchor = handle_registry.format_handle("draft", p["chunk_id"], chunk=True)
        req = store.insert_ref(
            kind="todo", slug=None, title="fix this", meta={"anchor": anchor}
        )
        store.add_tag(
            req.id, Tag.closed("STATUS", "open"), set_by="agent", replace_prefix=True
        )

        # scope=p keeps the assertion pinned to the unsettled chunk itself —
        # the title heading (unaffected by this change-request) still mints
        # its own structure/adversarial pair independently.
        result = mint_review_fanout(store, ref_id, scope=p["chunk_id"])
        assert result["minted"] == []
        assert result["unsettled_skipped"] == len(_PROSE_LENSES)

        # resolve the change request and touch the chunk — mints again
        store.add_tag(
            req.id, Tag.closed("STATUS", "done"), set_by="agent", replace_prefix=True
        )
        store.edit_text(p["handle"], "revised after fix")

        result2 = mint_review_fanout(store, ref_id, scope=p["chunk_id"])
        assert len(result2["minted"]) == len(_PROSE_LENSES)
        assert result2["unsettled_skipped"] == 0


# ---------------------------------------------------------------------------
# review-fanout lens x chunk-kind mapping
# ---------------------------------------------------------------------------


class TestLensKindMapping:
    def test_flow_cites_prose_only_structure_adversarial_heading_only(
        self, store: Store
    ) -> None:
        proj = _project(store)
        ref, title = store.create_draft(name="kindmix", title="T", project_ref_id=proj)
        para = store.add_chunks(
            ref_id=ref.id, chunk_kind="paragraph", text="p", at={"after": title.handle}
        )[0]
        eq = store.add_chunks(
            ref_id=ref.id,
            chunk_kind="equation",
            text="E=mc^2",
            at={"after": para.handle},
        )[0]

        result = mint_review_fanout(store, ref.id)

        by_anchor: dict[str, set[str]] = {}
        for todo_id in result["minted"]:
            r = store.get_ref(kind="todo", id=todo_id)
            assert r is not None
            anchor, lens = r.meta.get("anchor"), r.meta.get("review")
            assert anchor is not None and lens is not None
            by_anchor.setdefault(anchor, set()).add(lens)

        title_anchor = handle_registry.format_handle(
            "draft", title.chunk_id, chunk=True
        )
        para_anchor = handle_registry.format_handle("draft", para.chunk_id, chunk=True)
        eq_anchor = handle_registry.format_handle("draft", eq.chunk_id, chunk=True)

        assert by_anchor.get(title_anchor) == set(_HEADING_LENSES)
        assert by_anchor.get(para_anchor) == set(_PROSE_LENSES)
        assert eq_anchor not in by_anchor


# ---------------------------------------------------------------------------
# the toc document-altitude lens
# ---------------------------------------------------------------------------


def _seed_toc_approval(store: Store, chunk_id: int, digest: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_review (chunk_id, checker, approved_sha, verdict) "
            "VALUES (%s, 'toc', %s, 'approved') "
            "ON CONFLICT (chunk_id, checker) DO UPDATE "
            "SET approved_sha = EXCLUDED.approved_sha, verdict = EXCLUDED.verdict",
            (chunk_id, digest),
        )
        conn.commit()


class TestDocLenses:
    def test_toc_minted_only_for_whole_draft_scope(self, store: Store) -> None:
        ref_id = _draft_with_chunks(store, name="doclens")

        result = mint_review_fanout(store, ref_id, lenses=(), doc_lenses=DOC_LENSES)

        assert len(result["minted"]) == 1
        ref = store.get_ref(kind="todo", id=result["minted"][0])
        assert ref is not None
        assert ref.meta.get("review") == "toc"
        assert ref.meta.get("llm_tier") == "opus"
        assert "author" not in ref.meta
        assert ref.prio == 2  # doc lenses share _FANOUT_PRIO (band 2)

    def test_toc_not_minted_when_scope_given(self, store: Store) -> None:
        ref_id = _draft_with_chunks(store, name="doclens-scope")
        p = next(
            c for c in store.reviewable_chunks(ref_id) if c["chunk_kind"] == "paragraph"
        )

        result = mint_review_fanout(
            store, ref_id, lenses=(), doc_lenses=DOC_LENSES, scope=p["chunk_id"]
        )

        assert result["minted"] == []

    def test_toc_never_author_eligible(self, store: Store) -> None:
        ref_id = _draft_with_chunks(store, name="doclens-author")

        result = mint_review_fanout(
            store, ref_id, lenses=(), doc_lenses=DOC_LENSES, author=True
        )

        assert len(result["minted"]) == 1
        ref = store.get_ref(kind="todo", id=result["minted"][0])
        assert ref is not None
        assert "author" not in ref.meta

    def test_toc_only_dirty_skips_when_digest_unchanged_remints_on_rename(
        self, store: Store
    ) -> None:
        ref_id = _draft_with_chunks(store, name="doclens-dirty")
        order = store.reading_order(ref_id)
        root = order[0]
        digest = store.toc_digest(ref_id)
        _seed_toc_approval(store, root.chunk_id, digest)

        result = mint_review_fanout(
            store, ref_id, lenses=(), doc_lenses=DOC_LENSES, only_dirty=True
        )
        assert result["minted"] == []

        store.edit_text(root.handle, "T (renamed)")

        result2 = mint_review_fanout(
            store, ref_id, lenses=(), doc_lenses=DOC_LENSES, only_dirty=True
        )
        assert len(result2["minted"]) == 1

    def test_toc_anchor_agrees_with_status_root_when_first_chunk_has_no_sha(
        self, store: Store
    ) -> None:
        """``review_root_chunk_id`` is the SINGLE selection rule shared by
        the fanout's toc-lens mint (``_mint_doc_lenses``) and
        ``review_status_for_draft``'s own toc-row patch — both must land
        on the same chunk even when the draft's first reading-order chunk
        (the title heading) carries a NULL ``content_sha`` (not yet
        reviewable). Before the fix, ``_mint_doc_lenses`` anchored on
        ``reading_order()[0]`` (no content_sha filter) while the status
        query skipped that same chunk — the toc indicator would then read
        permanently unapproved no matter how many times the anchored
        chunk was actually reviewed."""
        ref_id = _draft_with_chunks(store, name="doclens-nullsha", n_paragraphs=1)
        order = store.reading_order(ref_id)
        title, para = order[0], order[1]
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET content_sha = NULL WHERE chunk_id = %s",
                (title.chunk_id,),
            )
            conn.commit()

        assert store.review_root_chunk_id(ref_id) == para.chunk_id

        result = mint_review_fanout(store, ref_id, lenses=(), doc_lenses=DOC_LENSES)
        assert len(result["minted"]) == 1
        ref = store.get_ref(kind="todo", id=result["minted"][0])
        assert ref is not None
        expected_anchor = handle_registry.format_handle(
            "draft", para.chunk_id, chunk=True
        )
        assert ref.meta.get("anchor") == expected_anchor

        toc_rows = [
            s for s in store.review_status_for_draft(ref_id) if s["checker"] == "toc"
        ]
        assert len(toc_rows) == 1
        assert toc_rows[0]["chunk_id"] == para.chunk_id


# ---------------------------------------------------------------------------
# mint_review_todo — the shared primitive (idempotency + author plumbing)
# ---------------------------------------------------------------------------


class TestMintReviewTodo:
    def test_second_call_same_pair_is_a_noop(self, store: Store) -> None:
        parent = store.insert_ref(kind="todo", slug=None, title="P").id
        first = mint_review_todo(
            store, parent_id=parent, lens="flow", anchor="dc1", text="brief"
        )
        assert first is not None
        second = mint_review_todo(
            store, parent_id=parent, lens="flow", anchor="dc1", text="brief"
        )
        assert second is None

    def test_author_true_stamps_meta(self, store: Store) -> None:
        parent = store.insert_ref(kind="todo", slug=None, title="P").id
        todo_id = mint_review_todo(
            store,
            parent_id=parent,
            lens="cites",
            anchor="dc2",
            text="brief",
            author=True,
        )
        assert todo_id is not None
        ref = store.get_ref(kind="todo", id=todo_id)
        assert ref is not None
        assert ref.meta.get("author") is True


# ---------------------------------------------------------------------------
# 3b — executor writeback (claude_inproc._run_plan_tick)
# ---------------------------------------------------------------------------


class _FakeSpec:
    """Stands in for the plan_tick JobTypeSpec (mirrors
    test_plan_tick_resume.py's fake) — returns a canned outcome, with an
    optional side effect run first (e.g. simulating a reviewer that edits
    the anchor chunk instead of just filing findings)."""

    name = "plan_tick"

    def __init__(self, outcome: PlanTickOutcome, *, side_effect: Any = None) -> None:
        self._outcome = outcome
        self._side_effect = side_effect

    def run(self, **_kw: object) -> PlanTickOutcome:
        if self._side_effect is not None:
            self._side_effect()
        return self._outcome


def _mk_review_parent(store: Store, *, lens: str, anchor: str) -> int:
    parent = store.insert_ref(
        kind="todo",
        slug=None,
        title="review tick",
        meta={"review": lens, "anchor": anchor},
    )
    store.add_tag(
        parent.id, Tag.closed("STATUS", "open"), set_by="agent", replace_prefix=True
    )
    store.add_tag(parent.id, Tag.closed("LLM", "sonnet"), set_by="agent")
    return parent.id


def _mk_plain_parent(store: Store) -> int:
    parent = store.insert_ref(kind="todo", slug=None, title="a normal todo")
    store.add_tag(
        parent.id, Tag.closed("STATUS", "open"), set_by="agent", replace_prefix=True
    )
    store.add_tag(parent.id, Tag.closed("LLM", "sonnet"), set_by="agent")
    return parent.id


def _mk_job(store: Store, parent_id: int) -> int:
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="plan_tick",
        parent_id=parent_id,
        meta={
            "executor": "claude_inproc",
            "job_type": "plan_tick",
            "params": {"model": "sonnet"},
        },
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", "running"), set_by="system", replace_prefix=True
    )
    return job.id


def _run(
    store: Store,
    job_id: int,
    *,
    exit_code: int = 0,
    stream: str = _CLEAN,
    side_effect: Any = None,
) -> None:
    spec = _FakeSpec(
        PlanTickOutcome(exit_code=exit_code, stdout=stream, stderr="", duration_s=1.0),
        side_effect=side_effect,
    )
    ci._run_plan_tick(store, job_id, spec)


class TestReviewWriteback:
    def test_clean_pass_unchanged_sha_records_approval(self, store: Store) -> None:
        ref_id = _draft_with_chunks(store, name="wb-clean")
        p = store.reviewable_chunks(ref_id)[0]
        anchor = handle_registry.format_handle("draft", p["chunk_id"], chunk=True)
        parent_id = _mk_review_parent(store, lens="flow", anchor=anchor)
        job_id = _mk_job(store, parent_id)

        _run(store, job_id)

        job_tags = {str(t) for t in store.tags_for(job_id)}
        assert "STATUS:succeeded" in job_tags
        statuses = store.review_status_for_chunk(p["chunk_id"])
        assert len(statuses) == 1
        assert statuses[0]["checker"] == "flow"
        assert statuses[0]["verdict"] == "approved"
        assert statuses[0]["dirty"] is False

    def test_resumed_tick_records_nothing(self, store: Store) -> None:
        """A resumable exhaustion (max-turns / timeout) marks the job
        succeeded-but-resumable — but the reviewer never finished the
        section, so no approval may be recorded (a false ✓ would mask an
        unreviewed chunk)."""
        ref_id = _draft_with_chunks(store, name="wb-resume")
        p = store.reviewable_chunks(ref_id)[0]
        anchor = handle_registry.format_handle("draft", p["chunk_id"], chunk=True)
        parent_id = _mk_review_parent(store, lens="flow", anchor=anchor)
        job_id = _mk_job(store, parent_id)

        _run(store, job_id, exit_code=1, stream=_MAX_TURNS)

        job_tags = {str(t) for t in store.tags_for(job_id)}
        assert "STATUS:succeeded" in job_tags  # resumable → succeeded, not failed
        assert store.review_status_for_chunk(p["chunk_id"]) == []

    def test_non_done_verdict_records_nothing(self, store: Store) -> None:
        """A clean exit whose reviewer yielded mid-pass (verdict != done)
        is not a finished review — no approval."""
        ref_id = _draft_with_chunks(store, name="wb-continue")
        p = store.reviewable_chunks(ref_id)[0]
        anchor = handle_registry.format_handle("draft", p["chunk_id"], chunk=True)
        parent_id = _mk_review_parent(store, lens="flow", anchor=anchor)
        job_id = _mk_job(store, parent_id)

        _run(store, job_id, stream=_CONTINUE)

        job_tags = {str(t) for t in store.tags_for(job_id)}
        assert "STATUS:succeeded" in job_tags
        assert store.review_status_for_chunk(p["chunk_id"]) == []

    def test_open_anchored_change_request_records_nothing(self, store: Store) -> None:
        """The ``precis-draft-reviewer`` persona files findings as an
        anchored change-request ``kind='todo'`` (meta.anchor=dc<id>), NOT a
        ``kind='finding'`` child — so an OPEN anchored request on the chunk
        must also block the auto-approval."""
        ref_id = _draft_with_chunks(store, name="wb-anchored-open")
        p = store.reviewable_chunks(ref_id)[0]
        anchor = handle_registry.format_handle("draft", p["chunk_id"], chunk=True)
        parent_id = _mk_review_parent(store, lens="cites", anchor=anchor)
        job_id = _mk_job(store, parent_id)

        # An open anchored change-request (the shape a real reviewer files).
        req = store.insert_ref(
            kind="todo", slug=None, title="missing citation", meta={"anchor": anchor}
        )
        store.add_tag(
            req.id, Tag.closed("STATUS", "open"), set_by="agent", replace_prefix=True
        )

        _run(store, job_id)

        assert store.review_status_for_chunk(p["chunk_id"]) == []

    def test_resolved_anchored_change_request_still_approves(
        self, store: Store
    ) -> None:
        """A DONE anchored change-request (already resolved) does NOT block a
        fresh clean pass — the guard is open-requests-only, so a chunk that
        was fixed can still earn a later ✓."""
        ref_id = _draft_with_chunks(store, name="wb-anchored-done")
        p = store.reviewable_chunks(ref_id)[0]
        anchor = handle_registry.format_handle("draft", p["chunk_id"], chunk=True)
        parent_id = _mk_review_parent(store, lens="flow", anchor=anchor)
        job_id = _mk_job(store, parent_id)

        req = store.insert_ref(
            kind="todo", slug=None, title="was fixed", meta={"anchor": anchor}
        )
        store.add_tag(
            req.id, Tag.closed("STATUS", "done"), set_by="agent", replace_prefix=True
        )

        _run(store, job_id)

        statuses = store.review_status_for_chunk(p["chunk_id"])
        assert len(statuses) == 1
        assert statuses[0]["verdict"] == "approved"

    def test_findings_filed_records_nothing(self, store: Store) -> None:
        ref_id = _draft_with_chunks(store, name="wb-findings")
        p = store.reviewable_chunks(ref_id)[0]
        anchor = handle_registry.format_handle("draft", p["chunk_id"], chunk=True)
        parent_id = _mk_review_parent(store, lens="cites", anchor=anchor)
        job_id = _mk_job(store, parent_id)

        # A finding filed as a child of the review-todo during the tick.
        store.insert_ref(
            kind="finding", slug=None, title="hallucinated cite", parent_id=parent_id
        )

        _run(store, job_id)

        job_tags = {str(t) for t in store.tags_for(job_id)}
        assert "STATUS:succeeded" in job_tags
        assert store.review_status_for_chunk(p["chunk_id"]) == []

    def test_changed_anchor_sha_records_nothing(self, store: Store) -> None:
        ref_id = _draft_with_chunks(store, name="wb-changed-sha")
        p = store.reviewable_chunks(ref_id)[0]
        anchor = handle_registry.format_handle("draft", p["chunk_id"], chunk=True)
        parent_id = _mk_review_parent(store, lens="flow", anchor=anchor)
        job_id = _mk_job(store, parent_id)

        # The reviewer edited the anchor chunk itself during the tick
        # (e.g. a future authoring reviewer) — must not self-approve.
        chunk = store.get_draft_chunk(anchor)
        assert chunk is not None

        def _edit_during_tick() -> None:
            store.edit_text(chunk.handle, "the reviewer rewrote this")

        _run(store, job_id, side_effect=_edit_during_tick)

        job_tags = {str(t) for t in store.tags_for(job_id)}
        assert "STATUS:succeeded" in job_tags
        assert store.review_status_for_chunk(p["chunk_id"]) == []

    def test_toc_clean_pass_records_digest_approval(self, store: Store) -> None:
        """item 10 — a clean ``toc`` tick pins ``approved_sha`` to the
        draft's TOC digest (:meth:`Store.toc_digest`), not the anchor
        chunk's ``content_sha``."""
        ref_id = _draft_with_chunks(store, name="wb-toc-clean", n_paragraphs=1)
        root = store.reading_order(ref_id)[0]
        anchor = handle_registry.format_handle("draft", root.chunk_id, chunk=True)
        parent_id = _mk_review_parent(store, lens="toc", anchor=anchor)
        job_id = _mk_job(store, parent_id)

        _run(store, job_id)

        job_tags = {str(t) for t in store.tags_for(job_id)}
        assert "STATUS:succeeded" in job_tags
        statuses = [
            s
            for s in store.review_status_for_chunk(root.chunk_id)
            if s["checker"] == "toc"
        ]
        assert len(statuses) == 1
        assert statuses[0]["verdict"] == "approved"
        assert statuses[0]["approved_sha"] == store.toc_digest(ref_id)

    def test_toc_heading_renamed_mid_tick_records_nothing(self, store: Store) -> None:
        """A heading rename during the tick moves the TOC digest — the
        toc lens must refuse to self-approve, same mechanism as a chunk
        lens's sha check."""
        ref_id = _draft_with_chunks(store, name="wb-toc-renamed", n_paragraphs=1)
        root = store.reading_order(ref_id)[0]
        anchor = handle_registry.format_handle("draft", root.chunk_id, chunk=True)
        parent_id = _mk_review_parent(store, lens="toc", anchor=anchor)
        job_id = _mk_job(store, parent_id)

        def _rename_during_tick() -> None:
            store.edit_text(root.handle, "T (renamed)")

        _run(store, job_id, side_effect=_rename_during_tick)

        job_tags = {str(t) for t in store.tags_for(job_id)}
        assert "STATUS:succeeded" in job_tags
        statuses = [
            s
            for s in store.review_status_for_chunk(root.chunk_id)
            if s["checker"] == "toc"
        ]
        assert statuses == []

    def test_toc_paragraph_body_edit_mid_tick_still_approves(
        self, store: Store
    ) -> None:
        """Deliberate item-10 semantic difference: an edit to a paragraph's
        body doesn't move the TOC digest (it hashes only HEADING chunks),
        so the toc approval records even though the draft changed under
        the reviewer — unlike a chunk lens, which would refuse."""
        ref_id = _draft_with_chunks(store, name="wb-toc-para-edit", n_paragraphs=1)
        root = store.reading_order(ref_id)[0]
        para = next(
            c for c in store.reviewable_chunks(ref_id) if c["chunk_kind"] == "paragraph"
        )
        anchor = handle_registry.format_handle("draft", root.chunk_id, chunk=True)
        parent_id = _mk_review_parent(store, lens="toc", anchor=anchor)
        job_id = _mk_job(store, parent_id)

        def _edit_paragraph_during_tick() -> None:
            store.edit_text(para["handle"], "the reviewer tightened this prose")

        _run(store, job_id, side_effect=_edit_paragraph_during_tick)

        job_tags = {str(t) for t in store.tags_for(job_id)}
        assert "STATUS:succeeded" in job_tags
        statuses = [
            s
            for s in store.review_status_for_chunk(root.chunk_id)
            if s["checker"] == "toc"
        ]
        assert len(statuses) == 1
        assert statuses[0]["verdict"] == "approved"
        assert statuses[0]["approved_sha"] == store.toc_digest(ref_id)

    def test_non_review_tick_records_nothing_and_never_raises(
        self, store: Store
    ) -> None:
        parent_id = _mk_plain_parent(store)
        job_id = _mk_job(store, parent_id)

        _run(store, job_id)  # must not raise

        job_tags = {str(t) for t in store.tags_for(job_id)}
        assert "STATUS:succeeded" in job_tags
        with store.pool.connection() as conn:
            n_row = conn.execute("SELECT count(*) FROM chunk_review").fetchone()
        assert n_row is not None
        assert n_row[0] == 0

    def test_writeback_swallows_exceptions(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref_id = _draft_with_chunks(store, name="wb-swallow")
        p = store.reviewable_chunks(ref_id)[0]
        with store.pool.connection() as conn:
            current_sha_row = conn.execute(
                "SELECT content_sha FROM chunks WHERE chunk_id = %s",
                (p["chunk_id"],),
            ).fetchone()
        assert current_sha_row is not None
        current_sha = current_sha_row[0]

        def _boom(*_a: object, **_k: object) -> str:
            raise RuntimeError("boom")

        monkeypatch.setattr(store, "record_review", _boom)

        with store.pool.connection() as conn:
            # Must not raise even though store.record_review blows up. sha
            # matches current (0 findings, unchanged) so the swallow path
            # is genuinely reached inside the try/except, not short-
            # circuited by the sha-changed guard.
            ci._maybe_record_review_pass(
                store,
                conn,
                review_todo_id=999999,
                lens="flow",
                chunk_id=p["chunk_id"],
                sha_before=current_sha,
            )
            conn.commit()

        assert store.review_status_for_chunk(p["chunk_id"]) == []

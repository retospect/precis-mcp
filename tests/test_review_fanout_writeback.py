"""Rung 3a/3b of the paper-writing pipeline
(docs/design/paper-writing-pipeline.md §"Review — the memoized approval
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

from precis.quest.review_fanout import ALL_LENSES, mint_review_fanout
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


# ---------------------------------------------------------------------------
# 3a — mint_review_fanout
# ---------------------------------------------------------------------------


class TestMintReviewFanout:
    def test_mints_chunk_times_lens_todos(self, store: Store) -> None:
        ref_id = _draft_with_chunks(store, name="fan1", n_paragraphs=2)
        chunks = store.reviewable_chunks(ref_id)
        assert len(chunks) == 3  # title + 2 paragraphs

        result = mint_review_fanout(store, ref_id)

        assert len(result["minted"]) == len(chunks) * len(ALL_LENSES)
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
            assert ("closed", "LLM", expected_tier) in tags

        assert len(seen_pairs) == len(chunks) * len(ALL_LENSES)

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

        assert len(result["minted"]) == len(chunks)
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
        assert len(result["minted"]) == len(chunks_after) * len(ALL_LENSES)

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

    def test_non_review_tick_records_nothing_and_never_raises(
        self, store: Store
    ) -> None:
        parent_id = _mk_plain_parent(store)
        job_id = _mk_job(store, parent_id)

        _run(store, job_id)  # must not raise

        job_tags = {str(t) for t in store.tags_for(job_id)}
        assert "STATUS:succeeded" in job_tags
        with store.pool.connection() as conn:
            n = conn.execute("SELECT count(*) FROM chunk_review").fetchone()[0]
        assert n == 0

    def test_writeback_swallows_exceptions(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref_id = _draft_with_chunks(store, name="wb-swallow")
        p = store.reviewable_chunks(ref_id)[0]
        with store.pool.connection() as conn:
            current_sha = conn.execute(
                "SELECT content_sha FROM chunks WHERE chunk_id = %s",
                (p["chunk_id"],),
            ).fetchone()[0]

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

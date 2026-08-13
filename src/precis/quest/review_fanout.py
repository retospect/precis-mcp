"""Rung 3a of the paper-writing pipeline — the whole-draft review fanout
(docs/backlog/paper-writing-pipeline.md §"Review — the memoized approval
ledger"; the review-status UI work added incremental re-check + scope +
the document-altitude lens).

:mod:`precis.quest.weave_review`'s ``mint_weave_reviews`` mints review-
todos for *one just-woven section* across the two per-weave lenses
(``flow``/``cites``). :func:`mint_review_fanout` here is the whole-draft
analogue: mints one review-todo per ``(reviewable chunk × lens)`` across
the four per-chunk lenses in the design doc's persona table, for every
reviewable chunk of a draft (or a narrower ``scope``) — a manual "review
everything" pass (weekly/deep tiers included), not tied to a weave event.

**Shared minting.** Both callers go through
:func:`precis.quest.weave_review.mint_review_todo` — the single
``(parent, lens, anchor)`` ref/meta shape (idempotency check + insert +
``STATUS:open`` tag / ``meta.llm_tier``). This module only decides
*which* chunks, *which* lenses, *which* tier, and *which* parent.

**Lenses → tier.** ``flow``/``cites`` are the per-weave/local lenses
(``llm_tier='sonnet'``, matching ``weave_review``'s per-weave tier);
``structure`` (``precis-review-section-structure``) and ``adversarial``
(``precis-review-paper-help``) are the weekly/deep-tier lenses in the
design doc's persona table — routed to ``llm_tier='opus'``
(``Tier.FRONTIER`` in ``utils/llm/router.py``; the same value
``workers/deep_review.py`` and the dispatcher's auto-run-signal
predicate resolve for the opus rung). ``toc`` (the document-altitude
lens) is also ``opus``.

**Lens × chunk-kind granularity**:
``flow``/``cites`` mint on **prose chunks only**
(``store.PROSE_CHUNK_KINDS`` — paragraph/aside/callout/claim, never
equations, tables, headings, or terms); ``structure``/``adversarial``
mint on **heading chunks only** (the anchored reviewer already renders
the whole section via fisheye — per-paragraph minting would re-review the
same section N times for nothing). An equation/table/term chunk gets
nothing from either lens set. ``toc`` is document-level, not per-chunk —
see below.

**Which chunks.** ``store.drafts.reviewable_chunks(ref_id)`` — the draft's live,
draft-family chunks with a non-NULL ``content_sha`` (the same population
``chunks_requiring_review``/``review_status_for_draft`` scope to), or, for
a narrower ``scope``, ``store.drafts.review_subtree_chunk_ids`` (a heading's
subtree) or the single named chunk.

**Incremental re-check (``only_dirty``).** Off by default — a blunt "mint
for everything" pass, not filtered by whether a checker already passed a
chunk at its current sha (a re-run is still cheap: ``mint_review_todo``'s
own idempotency check skips a pair that already has a *live review-todo*).
``only_dirty=True`` additionally skips a ``(chunk, lens)`` pair that
already has an *approved* ``chunk_review`` row at the chunk's current sha
(``store.drafts.approved_pairs_at_current_sha``) — the cheap re-check loop after
an edit: only the touched chunks' lenses re-mint.

**Skip unsettled.** A chunk carrying an open anchored change-request is
excluded from minting entirely (mirrors the writeback's own guard —
``quest.review_guard.has_open_change_request_via_store``, the same check
``workers/executors/claude_inproc.py::_maybe_record_review_pass`` uses
before recording an approval): the writeback would refuse to approve that
chunk anyway, so a check there is a wasted LLM run on text about to
change under the open request. Counted in the summary as
``unsettled_skipped``. Checks run on settled text; "apply fixes → then
re-check" falls out by construction.

**Scope.** ``scope=None`` (default) walks the whole draft. ``scope=<chunk
id>`` narrows to either a heading's subtree (``review_subtree_chunk_ids``)
or a single prose chunk, letting the same primitive back a per-paragraph
"run checks on this" trigger, a per-section "run on this subtree"
trigger, and the whole-draft "run outstanding checks" button.

**Document-altitude lens (``toc``).** Minted ONLY for whole-draft scope
(``scope is None``) — a request via ``doc_lenses`` (``DOC_LENSES =
('toc',)`` by default; empty unless the caller opts in, so a narrow scope
call or an old caller that only passes ``lenses=`` never mints it). One
review-todo per document, anchored on the draft's first chunk in document
order (there is no single dedicated root — see
``store.drafts.toc_digest``/``review_status_for_draft``'s docstrings). Its
``only_dirty`` check compares the ledger's stored digest against
``store.drafts.toc_digest(ref_id)`` (recomputed), never a chunk sha — see that
method. Never author-eligible.

**Parent.** Parented on the draft's owning **project todo**, resolved via
the ``draft-of`` link (``draft --(draft-of)--> project``, 1:1 — see
``store.drafts.create_draft``'s bind + ``handlers/draft.py``'s
``_render_by_project``). This differs from ``mint_weave_reviews``, which
parents straight on the quest ref — that trigger fires *from* a quest
tick and already has ``quest_id`` in hand; this one fires from a bare
draft (there is no guaranteed owning quest — a draft can be authored
directly, ``docs/conventions/tex-vs-draft-authoring.md``), so the project
todo is the one parent every draft is guaranteed to have. Trade-off: a
review-todo minted here has no ``rotation_root`` ancestor beyond the
project todo itself (same accepted orphan-sweep caveat
``mint_weave_reviews`` already documents for its quest-parented todos).

**Author flag.** The effective authoring decision is ``author=True`` OR'd
with the draft's per-document auto-author toggle
(``store.drafts.draft_authoring_enabled(draft_ref_id)``, rung 3e — set via
``edit(kind='draft', authoring='on')`` / the web reader's toolbar toggle).
Either forces authoring on; the explicit ``author`` param still lets a
caller (e.g. the CLI ``--author`` flag) override the toggle regardless of
its state. The effective flag passes ``author=True`` through to
``mint_review_todo`` for the ``cites``/``structure`` lenses only
(``flow``/``adversarial``/``toc`` never author — they stay pure
find-and-file). This only stamps ``meta.author=True`` on the minted todo;
no authoring *behavior* exists yet (a separate piece teaches the reviewer
engine to edit instead of file findings when this flag is set). Default
``False`` — today's find-and-file-findings behavior is unchanged unless
the toggle is on.
"""

from __future__ import annotations

from typing import Any

from precis.errors import BadInput
from precis.quest import review_guard
from precis.quest.weave_review import _LENS_BRIEFS as _WEAVE_LENS_BRIEFS
from precis.quest.weave_review import mint_review_todo
from precis.store._draft_ops import PROSE_CHUNK_KINDS
from precis.utils import handle_registry

#: Briefs for the lenses the per-weave trigger doesn't cover: the two
#: deep/weekly per-chunk lenses plus the document-altitude ``toc`` lens.
#: Mirrors ``weave_review._LENS_BRIEFS``'s shape — each names the
#: lens-specific skill on top of the generic ``precis-draft-reviewer``
#: persona ``_load_review_persona`` auto-loads for any ``has_review`` tick.
_FANOUT_ONLY_BRIEFS: dict[str, str] = {
    "structure": (
        "Section-structure review of the draft section anchored at {h}. "
        "Load `get(kind='skill', id='precis-review-section-structure')` and "
        "apply it: does the section have a clear intro -> body -> "
        "conclusion arc, or does it read as an unordered list of "
        "paragraph-level facts? File concrete anchored change requests for "
        "what to restructure."
    ),
    "adversarial": (
        "Adversarial review of the draft section anchored at {h}. Load "
        "`get(kind='skill', id='precis-review-paper-help')` and apply its "
        "adversarial-reader lens: which claims are unsupported, which "
        "counterarguments are missing, where would a skeptical reviewer "
        "push back? File concrete anchored change requests."
    ),
    "toc": (
        "Document-altitude (table-of-contents) review of the whole draft "
        "anchored at {h}. Deterministic shape stats already exist for this "
        "draft — a scaffold-completeness diff against the genre's expected "
        "sections and per-section word-count balance (the `wordcount` "
        "view) — so don't re-derive or re-count them; read them and judge "
        "what they can't: does the outline order make narrative sense, "
        "does the document have a real arc (not just a bag of sections), "
        "and is any section-length imbalance an actual problem or just how "
        "this topic naturally splits? File concrete anchored change "
        "requests for what to reorder, split, merge, or rebalance."
    ),
}

#: Lens -> LLM tier (meta.llm_tier). flow/cites are the per-weave/local
#: lenses (matching weave_review's tier); structure/adversarial/toc are
#: the weekly/deep-tier lenses (design doc persona table) -> the opus rung.
_LENS_TIER: dict[str, str] = {
    "flow": "sonnet",
    "cites": "sonnet",
    "structure": "opus",
    "adversarial": "opus",
    "toc": "opus",
}

#: The four per-chunk lenses in the design doc's persona table — the
#: fanout's default ``lenses=`` (unlike weave_review's per-weave-only
#: ``("flow", "cites")``).
ALL_LENSES: tuple[str, ...] = ("flow", "cites", "structure", "adversarial")

#: The document-altitude lens(es) — minted once per document, never
#: per-chunk, and only for whole-draft scope. Opt-in via ``doc_lenses=``
#: (empty by default) so an old caller that only passes ``lenses=`` never
#: mints one, and a narrow ``scope=`` call never does either.
DOC_LENSES: tuple[str, ...] = ("toc",)

#: Per-chunk lenses restricted to PROSE chunks (item 2).
_PROSE_LENSES = frozenset({"flow", "cites"})

#: Per-chunk lenses restricted to HEADING chunks (item 2).
_HEADING_LENSES = frozenset({"structure", "adversarial"})

#: Lenses for which ``author=True`` is meaningful (see module docstring).
#: ``flow``/``adversarial``/``toc`` never author regardless of the flag.
_AUTHOR_ELIGIBLE_LENSES = frozenset({"cites", "structure"})

#: ``refs.prio`` for fanout-minted review todos (0014 scale: lower = more
#: urgent). The fanout is a *user-triggered* pass (the "run outstanding
#: checks" button / CLI), so it mints in band 2 — the cron band — where
#: the ``claude_inproc`` claim's ``prio ASC, ref_id ASC`` order drains it
#: FIFO alongside recurring spawns. At the NULL default (band 5) a big
#: fanout starves indefinitely behind the continuously re-minted
#: recurring stream (news_poll/briefing/... mint at 2); band 1 would
#: instead preempt-starve those cadences for the whole batch. The
#: dispatcher propagates this prio verbatim onto the ``plan_tick`` jobs.
_FANOUT_PRIO = 2


def _brief_for(lens: str, anchor: str) -> str:
    brief = _WEAVE_LENS_BRIEFS.get(lens) or _FANOUT_ONLY_BRIEFS.get(lens)
    if brief is not None:
        return brief.format(h=anchor)
    return (
        f"{lens} review of the draft section anchored at {anchor}. File "
        "concrete anchored change requests for what to fix."
    )


def _lenses_for_kind(chunk_kind: str, lenses: tuple[str, ...]) -> list[str]:
    """The subset of ``lenses`` (the per-chunk four) applicable to a chunk
    of this ``chunk_kind`` — item 2's lens × chunk-kind mapping. A kind
    that is neither prose nor heading (equation/table/term/…) gets
    nothing from either lens set."""
    if chunk_kind == "heading":
        allowed = _HEADING_LENSES
    elif chunk_kind in PROSE_CHUNK_KINDS:
        allowed = _PROSE_LENSES
    else:
        return []
    return [lens for lens in lenses if lens in allowed]


def _draft_project_parent(store: Any, draft_ref_id: int) -> int:
    """Resolve the draft's owning project todo via its ``draft-of`` link.

    Raises :class:`BadInput` when the draft has no such bind — the fanout
    has nowhere trusted to parent under (mirrors ``handlers/draft.py``'s
    ``_render_by_project`` "no draft bound to project" shape, inverted)."""
    links = store.links_for(draft_ref_id, direction="out", relation="draft-of")
    if not links:
        raise BadInput(
            f"draft {draft_ref_id} has no draft-of project link — cannot "
            "resolve an owning project todo to parent review-todos on",
            next="a draft created via put(kind='draft', ...) always binds "
            "draft-of at creation; this draft is missing that link",
        )
    return int(links[0].dst_ref_id)


def _scoped_chunks(
    store: Any, draft_ref_id: int, scope: int | None
) -> list[dict[str, Any]]:
    """The chunk dicts (``chunk_id``/``handle``/``chunk_kind``) the fanout
    walks: every reviewable chunk when ``scope`` is ``None``, else a
    heading's subtree or the single named chunk (item 1's ``scope``).

    Raises :class:`BadInput` when ``scope`` doesn't name a live reviewable
    chunk of this draft."""
    all_chunks = {
        c["chunk_id"]: c for c in store.drafts.reviewable_chunks(draft_ref_id)
    }
    if scope is None:
        return list(all_chunks.values())
    target = all_chunks.get(scope)
    if target is None:
        raise BadInput(
            f"scope chunk {scope} is not a live reviewable chunk of draft "
            f"{draft_ref_id}",
            next="pass the chunk_id of a live dc<id> chunk in this draft",
        )
    if target["chunk_kind"] != "heading":
        return [target]
    subtree_ids = store.drafts.review_subtree_chunk_ids(draft_ref_id, scope)
    return [all_chunks[cid] for cid in subtree_ids if cid in all_chunks]


def mint_review_fanout(
    store: Any,
    draft_ref_id: int,
    *,
    lenses: tuple[str, ...] = ALL_LENSES,
    doc_lenses: tuple[str, ...] = (),
    author: bool = False,
    only_dirty: bool = False,
    scope: int | None = None,
) -> dict[str, Any]:
    """Mint one review-todo per ``(chunk × applicable lens)`` for
    ``draft_ref_id`` — the whole draft, or a narrower ``scope`` (a heading
    chunk's subtree, or one prose chunk) — parented on the draft's owning
    project todo.

    ``lenses`` is the per-chunk lens set (default ``ALL_LENSES``, all
    four); each lens only mints on the chunk kinds it applies to (item 2 —
    ``_lenses_for_kind``). ``doc_lenses`` (default ``()``, opt-in) mints
    document-level lenses (``DOC_LENSES = ('toc',)``) ONLY when
    ``scope is None``. ``only_dirty=True`` additionally skips a pair
    already approved at the chunk's (or, for a doc lens, the draft's TOC
    digest's) current state. A chunk carrying an open anchored
    change-request is always skipped (counted as ``unsettled_skipped``),
    regardless of ``only_dirty``.

    Returns a summary dict:

    - ``parent_id``: the resolved project todo id.
    - ``minted``: list of newly-minted review-todo ids (this call only).
    - ``skipped``: count of ``(chunk, lens)`` pairs that already had a
      live review-todo (idempotent no-op).
    - ``unsettled_skipped``: count of ``(chunk, lens)`` pairs skipped
      because the chunk carries an open anchored change-request.
    - ``author_minted``: count of minted todos that carry
      ``meta.author=True`` (only nonzero when ``author=True`` and the
      lens is author-eligible — see module docstring).
    - ``chunks_seen``: count of chunks walked (within ``scope``).

    Idempotent: a repeat call over an unchanged draft mints nothing (every
    ``(chunk, lens)`` pair already has a live review-todo from the first
    call), so ``minted == []`` and ``skipped`` covers every pair.
    """
    parent_id = _draft_project_parent(store, draft_ref_id)
    # Per-document auto-author toggle (rung 3e): an explicit ``author=True``
    # forces authoring on regardless of the toggle; otherwise defer to the
    # draft's own ``meta.authoring_enabled`` (web toolbar / edit(authoring=)).
    effective_author = author or bool(
        store.drafts.draft_authoring_enabled(draft_ref_id)
    )
    chunks = _scoped_chunks(store, draft_ref_id, scope)

    approved_at_sha = (
        store.drafts.approved_pairs_at_current_sha(draft_ref_id)
        if only_dirty
        else set()
    )

    minted: list[int] = []
    skipped = 0
    unsettled_skipped = 0
    author_minted = 0
    for chunk in chunks:
        chunk_id = chunk["chunk_id"]
        chunk_lenses = _lenses_for_kind(chunk["chunk_kind"], lenses)
        if not chunk_lenses:
            continue
        if review_guard.has_open_change_request_via_store(store, chunk_id):
            unsettled_skipped += len(chunk_lenses)
            continue
        anchor = handle_registry.format_handle("draft", chunk_id, chunk=True)
        for lens in chunk_lenses:
            if only_dirty and (chunk_id, lens) in approved_at_sha:
                continue
            use_author = effective_author and lens in _AUTHOR_ELIGIBLE_LENSES
            todo_id = mint_review_todo(
                store,
                parent_id=parent_id,
                lens=lens,
                anchor=anchor,
                text=_brief_for(lens, anchor),
                llm_tag=_LENS_TIER.get(lens, "sonnet"),
                author=use_author,
                prio=_FANOUT_PRIO,
            )
            if todo_id is None:
                skipped += 1
            else:
                minted.append(todo_id)
                if use_author:
                    author_minted += 1

    if scope is None and doc_lenses:
        minted_doc, skipped_doc, unsettled_doc = _mint_doc_lenses(
            store,
            draft_ref_id,
            parent_id=parent_id,
            doc_lenses=doc_lenses,
            only_dirty=only_dirty,
        )
        minted.extend(minted_doc)
        skipped += skipped_doc
        unsettled_skipped += unsettled_doc

    return {
        "parent_id": parent_id,
        "minted": minted,
        "skipped": skipped,
        "unsettled_skipped": unsettled_skipped,
        "author_minted": author_minted,
        "chunks_seen": len(chunks),
    }


def _mint_doc_lenses(
    store: Any,
    draft_ref_id: int,
    *,
    parent_id: int,
    doc_lenses: tuple[str, ...],
    only_dirty: bool,
) -> tuple[list[int], int, int]:
    """Mint the document-level lenses (today: ``toc``) — one review-todo
    per document, anchored on the draft's first REVIEWABLE chunk in
    document order (item 10; see ``store.drafts.toc_digest``'s docstring for why
    there is no single dedicated root to anchor on instead). Anchor is
    ``store.drafts.review_root_chunk_id`` — NOT ``reading_order()[0]`` — so this
    mints on the SAME chunk ``review_status_for_draft`` reports the
    ``toc`` ledger row against; ``reading_order()[0]`` carries no
    content_sha filter, so if the draft's first chunk isn't yet
    reviewable this would anchor on a chunk the status query never
    surfaces, and the toc indicator would read permanently unapproved.
    Returns ``(minted_ids, skipped, unsettled_skipped)``."""
    root_chunk_id = store.drafts.review_root_chunk_id(draft_ref_id)
    if root_chunk_id is None:
        return [], 0, 0
    anchor = handle_registry.format_handle("draft", root_chunk_id, chunk=True)

    if review_guard.has_open_change_request_via_store(store, root_chunk_id):
        return [], 0, len(doc_lenses)

    minted: list[int] = []
    skipped = 0
    for lens in doc_lenses:
        if only_dirty and lens == "toc" and not _toc_is_dirty(store, draft_ref_id):
            continue
        todo_id = mint_review_todo(
            store,
            parent_id=parent_id,
            lens=lens,
            anchor=anchor,
            text=_brief_for(lens, anchor),
            llm_tag=_LENS_TIER.get(lens, "opus"),
            author=False,  # doc lenses are never author-eligible
            prio=_FANOUT_PRIO,
        )
        if todo_id is None:
            skipped += 1
        else:
            minted.append(todo_id)
    return minted, skipped, 0


def _toc_is_dirty(store: Any, draft_ref_id: int) -> bool:
    """Whether the ``toc`` lens's approval is stale — the stored digest
    (the root chunk's ``chunk_review.approved_sha``) no longer matches the
    recomputed :meth:`~precis.store._draft_ops.DraftMixin.toc_digest`.
    ``True`` (dirty) when never approved."""
    for row in store.drafts.review_status_for_draft(draft_ref_id):
        if row["checker"] == "toc":
            return bool(row["dirty"])
    return True


__all__ = ["ALL_LENSES", "DOC_LENSES", "mint_review_fanout"]

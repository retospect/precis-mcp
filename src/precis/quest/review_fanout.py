"""Rung 3a of the paper-writing pipeline — the whole-draft review fanout
(docs/design/paper-writing-pipeline.md §"Review — the memoized approval
ledger").

:mod:`precis.quest.weave_review`'s ``mint_weave_reviews`` mints review-
todos for *one just-woven section* across the two per-weave lenses
(``flow``/``cites``). :func:`mint_review_fanout` here is the whole-draft
analogue: a one-shot that mints one review-todo per ``(reviewable chunk ×
lens)`` across all four lenses in the design doc's persona table, for
every reviewable chunk of a draft — a manual "review everything" pass
(weekly/deep tiers included), not tied to a weave event.

**Shared minting.** Both callers go through
:func:`precis.quest.weave_review.mint_review_todo` — the single
``(parent, lens, anchor)`` ref/tag shape (idempotency check + insert +
``STATUS:open``/``LLM:<tier>`` tags). This module only decides *which*
chunks, *which* lenses, *which* tier, and *which* parent.

**Lenses → tier.** ``flow``/``cites`` are the per-weave/local lenses
(``LLM:sonnet``, matching ``weave_review``'s per-weave tier); ``structure``
(``precis-review-section-structure``) and ``adversarial``
(``precis-review-paper-help``) are the weekly/deep-tier lenses in the
design doc's persona table — routed to ``LLM:opus`` (``Tier.FRONTIER``
in ``utils/llm/router.py``; the same closed tag value ``workers/
deep_review.py`` and the dispatcher's auto-run-signal predicate resolve
for the opus rung).

**Which chunks.** ``store.reviewable_chunks(ref_id)`` — the draft's live,
draft-family chunks with a non-NULL ``content_sha`` (the same population
``chunks_requiring_review``/``review_status_for_draft`` scope to). This
fanout is a blunt "mint for everything" pass, not filtered by whether a
checker already passed a chunk at its current sha — a re-run is still
cheap: ``mint_review_todo``'s per-``(parent, lens, anchor)`` idempotency
check skips a chunk×lens pair that already has a live review-todo, so a
repeat call over an unchanged draft mints nothing.

**Parent.** Parented on the draft's owning **project todo**, resolved via
the ``draft-of`` link (``draft --(draft-of)--> project``, 1:1 — see
``store.create_draft``'s bind + ``handlers/draft.py``'s
``_render_by_project``). This differs from ``mint_weave_reviews``, which
parents straight on the quest ref — that trigger fires *from* a quest
tick and already has ``quest_id`` in hand; this one fires from a bare
draft (there is no guaranteed owning quest — a draft can be authored
directly, ``docs/conventions/tex-vs-draft-authoring.md``), so the project
todo is the one parent every draft is guaranteed to have. Trade-off: a
review-todo minted here has no ``level:strategic`` ancestor beyond the
project todo itself (same accepted orphan-sweep caveat
``mint_weave_reviews`` already documents for its quest-parented todos).

**Author flag.** The effective authoring decision is ``author=True`` OR'd
with the draft's per-document auto-author toggle
(``store.draft_authoring_enabled(draft_ref_id)``, rung 3e — set via
``edit(kind='draft', authoring='on')`` / the web reader's toolbar toggle).
Either forces authoring on; the explicit ``author`` param still lets a
caller (e.g. the CLI ``--author`` flag) override the toggle regardless of
its state. The effective flag passes ``author=True`` through to
``mint_review_todo`` for the ``cites``/``structure`` lenses only
(``flow``/``adversarial`` never author — they stay pure find-and-file).
This only stamps ``meta.author=True`` on the minted todo; no authoring
*behavior* exists yet (a separate piece teaches the reviewer engine to
edit instead of file findings when this flag is set). Default ``False``
— today's find-and-file-findings behavior is unchanged unless the toggle
is on.
"""

from __future__ import annotations

from typing import Any

from precis.errors import BadInput
from precis.quest.weave_review import _LENS_BRIEFS as _WEAVE_LENS_BRIEFS
from precis.quest.weave_review import mint_review_todo
from precis.utils import handle_registry

#: Briefs for the two deep/weekly lenses the per-weave trigger doesn't
#: cover. Mirrors ``weave_review._LENS_BRIEFS``'s shape — each names the
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
}

#: Lens -> LLM tier tag. flow/cites are the per-weave/local lenses
#: (matching weave_review's tier); structure/adversarial are the
#: weekly/deep-tier lenses (design doc persona table) -> the opus rung.
_LENS_TIER: dict[str, str] = {
    "flow": "sonnet",
    "cites": "sonnet",
    "structure": "opus",
    "adversarial": "opus",
}

#: All four lenses in the design doc's persona table — the fanout's
#: default (unlike weave_review's per-weave-only ``("flow", "cites")``).
ALL_LENSES: tuple[str, ...] = ("flow", "cites", "structure", "adversarial")

#: Lenses for which ``author=True`` is meaningful (see module docstring).
#: ``flow``/``adversarial`` never author regardless of the flag.
_AUTHOR_ELIGIBLE_LENSES = frozenset({"cites", "structure"})


def _brief_for(lens: str, anchor: str) -> str:
    brief = _WEAVE_LENS_BRIEFS.get(lens) or _FANOUT_ONLY_BRIEFS.get(lens)
    if brief is not None:
        return brief.format(h=anchor)
    return (
        f"{lens} review of the draft section anchored at {anchor}. File "
        "concrete anchored change requests for what to fix."
    )


def _draft_project_parent(store: Any, draft_ref_id: int) -> int:
    """Resolve the draft's owning project todo via its ``draft-of`` link.

    Raises :class:`BadInput` when the draft has no such bind — the fanout
    has nowhere trusted to parent under (mirrors
    ``handlers/draft.py``'s ``_render_by_project`` "no draft bound to
    project" shape, inverted)."""
    links = store.links_for(draft_ref_id, direction="out", relation="draft-of")
    if not links:
        raise BadInput(
            f"draft {draft_ref_id} has no draft-of project link — cannot "
            "resolve an owning project todo to parent review-todos on",
            next="a draft created via put(kind='draft', ...) always binds "
            "draft-of at creation; this draft is missing that link",
        )
    return int(links[0].dst_ref_id)


def mint_review_fanout(
    store: Any,
    draft_ref_id: int,
    *,
    lenses: tuple[str, ...] = ALL_LENSES,
    author: bool = False,
) -> dict[str, Any]:
    """Mint one review-todo per ``(reviewable chunk × lens)`` for the
    whole draft ``draft_ref_id`` — the one-shot "review everything"
    fanout (rung 3a), parented on the draft's owning project todo.

    Returns a summary dict:

    - ``parent_id``: the resolved project todo id.
    - ``minted``: list of newly-minted review-todo ids (this call only).
    - ``skipped``: count of ``(chunk, lens)`` pairs that already had a
      live review-todo (idempotent no-op).
    - ``author_minted``: count of minted todos that carry
      ``meta.author=True`` (only nonzero when ``author=True`` and the
      lens is author-eligible — see module docstring).
    - ``chunks_seen``: count of reviewable chunks walked.

    Idempotent: a repeat call over an unchanged draft mints nothing (every
    ``(chunk, lens)`` pair already has a live review-todo from the first
    call), so ``minted == []`` and ``skipped`` covers every pair.
    """
    parent_id = _draft_project_parent(store, draft_ref_id)
    # Per-document auto-author toggle (rung 3e): an explicit ``author=True``
    # forces authoring on regardless of the toggle; otherwise defer to the
    # draft's own ``meta.authoring_enabled`` (web toolbar / edit(authoring=)).
    effective_author = author or bool(store.draft_authoring_enabled(draft_ref_id))
    chunks = store.reviewable_chunks(draft_ref_id)

    minted: list[int] = []
    skipped = 0
    author_minted = 0
    for chunk in chunks:
        anchor = handle_registry.format_handle("draft", chunk["chunk_id"], chunk=True)
        for lens in lenses:
            use_author = effective_author and lens in _AUTHOR_ELIGIBLE_LENSES
            todo_id = mint_review_todo(
                store,
                parent_id=parent_id,
                lens=lens,
                anchor=anchor,
                text=_brief_for(lens, anchor),
                llm_tag=_LENS_TIER.get(lens, "sonnet"),
                author=use_author,
            )
            if todo_id is None:
                skipped += 1
            else:
                minted.append(todo_id)
                if use_author:
                    author_minted += 1

    return {
        "parent_id": parent_id,
        "minted": minted,
        "skipped": skipped,
        "author_minted": author_minted,
        "chunks_seen": len(chunks),
    }


__all__ = ["ALL_LENSES", "mint_review_fanout"]

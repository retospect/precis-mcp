"""Rung 6f of the paper-writing pipeline — the per-weave review-todo
trigger (docs/backlog/paper-writing-pipeline.md §"Review — the memoized
approval ledger").

The reviewer ENGINE already exists: a todo carrying ``meta.review=<persona>``
+ ``meta.anchor='dc<id>'`` flips a ``plan_tick`` into reviewer mode
(:mod:`precis.workers.planner_prompt`'s ``has_review``/``has_anchor``-gated
modules, resolved by :mod:`precis.utils.prompt.predicates`), which renders
the anchored section at ``fisheye+1hop`` and lets the model file anchored
change-request todos. What was missing is the *trigger* — nothing minted
one of these after a weave. :func:`mint_weave_reviews` is that trigger,
called by :func:`precis.quest.weave_tick.weave_tick` after each
successful, non-``dry_run`` ``weave_section``.

**Shape mirrors the one existing minting site** — the web draft reader's
per-heading "review ▾" menu (``precis_web.routes.drafts.review_block``):
``meta={"anchor": <dc-handle>, "review": <persona>}`` on a ``kind='todo'``
ref, ``text=`` a persona-specific brief. That site runs as an interactive web
request and goes through ``TodoHandler.put`` (workspace inheritance,
``current_todo_from_env``, owner guards, the auto ``meta.llm_tier='opus'`` default for
a parented child); this module is background quest-tick code, so it mints
via ``store.insert_ref`` + ``store.add_tag`` directly instead — the same
trusted-code-path convention ``workers/dispatch.py`` (job children of a
todo) and ``workers/backlog_groom.py`` (its own todo children) already use.

**Personas.** ``docs/backlog/paper-writing-pipeline.md``'s persona table
marks ``flow`` (``precis-review-paragraph-flow``) and ``cites``
(``precis-review-citation-faithfulness``) as the two *per-weave* checkers;
``structure``/``adversarial`` are the weekly/deep tiers, out of scope for
this trigger. The planner's ``_load_review_persona`` only auto-loads the
generic ``precis-draft-reviewer`` skill (not a persona-specific one), so
each brief below names the matching skill explicitly — mirroring how
``_REVIEW_BRIEFS`` gives the generic persona a specific instruction rather
than swapping personas.

**Parent.** Parented directly on ``quest_id``, mirroring
:func:`precis.quest.loop.ensure_quest_loop`'s coordinator job (also
parented straight on the quest ref despite the kind mismatch). The
``kind='todo'``-only parent check lives in ``TodoHandler.put``
(``handlers._todo_guards.check_parent_exists``), which this module — like
``dispatch.py``'s job-minting — bypasses by calling the store layer
directly. That bypass also dropped ``check_parent_exists``'s *liveness*
half, and nothing replaced it: see :class:`OrphanedParentError`, which
:func:`mint_review_todo` now raises rather than minting into a deleted
tree. ``has_review``/``has_anchor`` only read the review-todo's own
``refs.meta``, so this is sufficient for the reviewer engine to pick it
up; the known tradeoff is the todo has no ``rotation_root`` ancestor, so
a todo-tree hygiene sweep may flag it as an orphan — an accepted
side-effect, same as any code-minted ref parented straight on a quest.

**Idempotency.** No ``idem_key`` primitive exists below ``JobHandler`` (the
todo/store layer has none), so this does its own existence check — a live
``kind='todo'`` child of ``quest_id`` already carrying this exact
``(review, anchor)`` pair — before inserting, so a re-weave of an
unchanged body doesn't stack duplicates. Returns only the ids minted by
*this* call; a persona that already has a live review-todo is skipped, not
re-returned.

**Shared minting primitive.** :func:`mint_review_todo` is the single
per-``(parent, persona, anchor)`` minting op (idempotency check + insert +
tags), factored out so :mod:`precis.quest.review_fanout` (rung 3a, the
whole-draft review-all fanout) reuses it instead of re-deriving the same
ref/tag shape. This module's own :func:`mint_weave_reviews` is now a thin
loop over it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.quest import review_guard
from precis.store.types import Tag
from precis.utils.ref_tree import is_orphaned

if TYPE_CHECKING:
    from precis.store.store import Store


class OrphanedParentError(RuntimeError):
    """Raised when asked to mint a review-todo into a dead tree.

    Bypassing ``TodoHandler.put`` (see the module docstring) also bypasses
    its ``check_parent_exists``, which has rejected a soft-deleted parent
    since 2026-06-13. Nothing replaced that check on the store-layer path,
    so a fanout over a draft whose project todo had been deleted two days
    earlier minted 258 live review-todos into the grave — each one then a
    dispatch candidate, which is how a deleted project kept billing
    planner ticks. The check is by *ancestry*, not just the parent row:
    ``deleted_at`` is not transitive (see :mod:`precis.utils.ref_tree`).
    """


#: Per-weave persona briefs. The generic reviewer persona
#: (``_load_review_persona`` in ``workers/planner_prompt.py``) is loaded
#: automatically for any ``has_review`` tick; these briefs name the
#: persona-specific skill on top of it, mirroring
#: ``precis_web.routes.drafts._REVIEW_BRIEFS``'s pattern.
_PERSONA_BRIEFS: dict[str, str] = {
    "flow": (
        "Paragraph-flow review of the draft section anchored at {h} (just "
        "woven). Load `get(kind='skill', id='precis-review-paragraph-flow')` "
        "and apply it: does every paragraph have a topic sentence, a single "
        "developed claim, and a transition into the next? File concrete "
        "anchored change requests for what to fix."
    ),
    "cites": (
        "Citation-faithfulness review of the draft section anchored at {h} "
        "(just woven). Load `get(kind='skill', "
        "id='precis-review-citation-faithfulness')` and apply it, checking "
        "THREE things: (1) sufficiency — every non-obvious claim carries a "
        "cite, not just the ones that already have one; (2) correctness — "
        "each cite actually and strongly supports the claim it's attached "
        "to; (3) living-cite preference — where the taproot hub hint shows "
        "a cited paper already grounds a claim hub, file a change-request "
        "to switch the bare `[pa…]`/`[pc…]` cite to the hub's `[fi<hub>]` "
        "form (or `[fi<hub>>pc…]` to pin this passage while still riding "
        "the living resolution). Cite-token resolution/existence is "
        "already pre-checked deterministically before you ever see this "
        "text — judge only whether a cite *supports* its claim, never "
        "whether the token resolves. File concrete anchored change "
        "requests for any hallucinated, unsupported, missing, or "
        "hub-eligible citation."
    ),
}

#: Planner model tier for a per-weave review tick — BIG ("sonnet"),
#: matching the design doc's routing table row "Review (per-weave/weekly)
#: | anchored section fisheye+1hop | mid, per persona".
_REVIEW_LLM_TAG = "sonnet"

#: The two per-weave personas (design doc table) — ``structure``/
#: ``adversarial`` are weekly/deep-tier, minted elsewhere (not built here).
_DEFAULT_PERSONAS: tuple[str, ...] = ("flow", "cites")


def _existing_review_todo(
    store: Store, parent_id: int, persona: str, anchor: str
) -> int | None:
    """A live ``kind='todo'`` child of ``parent_id`` already carrying this
    exact ``(review, anchor)`` pair, or ``None``. Manual idempotency check
    — see the module docstring (no ``idem_key`` primitive below
    ``JobHandler``)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs "
            "WHERE parent_id = %s AND kind = 'todo' AND deleted_at IS NULL "
            "AND meta->>'review' = %s AND meta->>'anchor' = %s "
            "LIMIT 1",
            (parent_id, persona, anchor),
        ).fetchone()
    return int(row[0]) if row else None


def mint_review_todo(
    store: Store,
    *,
    parent_id: int,
    persona: str,
    anchor: str,
    text: str,
    llm_tag: str = _REVIEW_LLM_TAG,
    author: bool = False,
    prio: int | None = None,
) -> int | None:
    """Mint one review-todo for ``(persona, anchor)`` parented on
    ``parent_id``, or ``None`` if a live one already exists (idempotent
    skip — see the module docstring).

    Raises :class:`OrphanedParentError` if ``parent_id`` is soft-deleted
    or sits under a soft-deleted ancestor — the guarantee
    ``TodoHandler.put``'s ``check_parent_exists`` gives the interactive
    path, restated here because this path deliberately bypasses it.

    Each carries ``meta.review=<persona>`` + ``meta.anchor=anchor`` — the
    shape :mod:`precis.utils.prompt.predicates`'s ``has_review``/
    ``has_anchor`` read to flip a ``plan_tick`` into reviewer mode over
    this section — plus ``meta.llm_tier=<llm_tag>`` (so the dispatcher
    actually picks it up; see ``workers/dispatch.py``'s auto-run-signal
    predicate) and ``STATUS:open``.

    ``prio`` flows onto the minted ref verbatim (``refs.prio``, 0014
    scale: lower = more urgent, ``NULL`` = default band 5). The
    dispatcher propagates it onto the ``plan_tick`` jobs it mints, and
    the ``claude_inproc`` claim orders ``prio ASC`` — so a user-triggered
    fanout minting at 2 shares the cron band (FIFO within it) instead of
    starving at 5 behind the continuously re-minted recurring stream.

    ``author=True`` additionally stamps ``meta.author=True`` on the
    minted todo. This is plumbing only (no authoring behavior lives here
    yet — a later piece teaches the reviewer engine to *edit* instead of
    just filing findings when this flag is set); callers should only pass
    ``author=True`` for personas where authoring is meaningful (the caller
    decides which).

    The single minting primitive shared by :func:`mint_weave_reviews`
    (per-weave, parented on the quest) and
    :mod:`precis.quest.review_fanout`'s ``mint_review_fanout`` (whole-
    draft, parented on the draft's owning project todo) — same ref/tag
    shape, different parent + persona set.
    """
    if is_orphaned(store, parent_id):
        raise OrphanedParentError(
            f"refusing to mint a {persona!r} review-todo under ref {parent_id}: "
            "it is soft-deleted or lives under a soft-deleted ancestor"
        )
    if _existing_review_todo(store, parent_id, persona, anchor) is not None:
        return None
    meta: dict[str, Any] = {"anchor": anchor, "review": persona, "llm_tier": llm_tag}
    if author:
        meta["author"] = True
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=text,
            meta=meta,
            parent_id=parent_id,
            prio=prio,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
    return int(ref.id)


def mint_weave_reviews(
    store: Store,
    quest_id: int,
    body_handle: str,
    *,
    personas: tuple[str, ...] = _DEFAULT_PERSONAS,
) -> list[int]:
    """Mint one review-todo per persona for the section at ``body_handle``,
    parented on ``quest_id``.

    Thin loop over :func:`mint_review_todo` — see that function for the
    exact ref/tag shape. Returns the ids minted by *this* call; a persona
    already carrying a live review-todo for this exact ``body_handle`` is
    skipped (idempotent-friendly re-weave), so a repeat call over an
    unchanged body returns ``[]``.

    **Machine-owned drafts are skipped, not scanned.** This trigger fires
    for any ``meta.quest_body='weave'`` quest (``weave_tick.mark_weave_quest``'s
    "paper-writing/topic-dossier quest" — a dossier CAN be in weave mode),
    and — unlike :func:`precis.quest.review_fanout.mint_review_fanout` —
    parents straight on ``quest_id`` with no ``draft-of`` resolution to
    fail closed on, so nothing here would otherwise stop it minting
    findings against a quest's own dossier. If ``body_handle``'s chunk
    belongs to a draft that is the source of an outbound ``dossier-of``/
    ``paper-of`` link (:func:`precis.quest.review_guard.is_machine_owned_draft`)
    this returns ``[]`` without minting — see that guard's docstring for
    why (quest 202469 / dossier 202546, Aug 2026).
    """
    chunk = store.drafts.get_draft_chunk(body_handle)
    if chunk is not None and review_guard.is_machine_owned_draft(store, chunk.ref_id):
        return []
    minted: list[int] = []
    for persona in personas:
        brief = _PERSONA_BRIEFS.get(persona)
        text = (
            brief.format(h=body_handle)
            if brief is not None
            else (
                f"{persona} review of the draft section anchored at "
                f"{body_handle} (just woven). File concrete anchored "
                "change requests."
            )
        )
        todo_id = mint_review_todo(
            store,
            parent_id=quest_id,
            persona=persona,
            anchor=body_handle,
            text=text,
            llm_tag=_REVIEW_LLM_TAG,
        )
        if todo_id is not None:
            minted.append(todo_id)
    return minted


__all__ = ["mint_review_todo", "mint_weave_reviews"]

"""Rung 6f of the paper-writing pipeline — the per-weave review-todo
trigger (docs/design/paper-writing-pipeline.md §"Review — the memoized
approval ledger").

The reviewer ENGINE already exists: a todo carrying ``meta.review=<lens>``
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
``meta={"anchor": <dc-handle>, "review": <lens>}`` on a ``kind='todo'``
ref, ``text=`` a lens-specific brief. That site runs as an interactive web
request and goes through ``TodoHandler.put`` (workspace inheritance,
``current_todo_from_env``, owner guards, the auto ``LLM:opus`` default for
a parented child); this module is background quest-tick code, so it mints
via ``store.insert_ref`` + ``store.add_tag`` directly instead — the same
trusted-code-path convention ``workers/dispatch.py`` (job children of a
todo) and ``workers/backlog_groom.py`` (its own todo children) already use.

**Lenses.** ``docs/design/paper-writing-pipeline.md``'s persona table
marks ``flow`` (``precis-review-paragraph-flow``) and ``cites``
(``precis-review-citation-faithfulness``) as the two *per-weave* checkers;
``structure``/``adversarial`` are the weekly/deep tiers, out of scope for
this trigger. The planner's ``_load_review_persona`` only auto-loads the
generic ``precis-draft-reviewer`` skill (not a lens-specific one), so each
brief below names the matching skill explicitly — mirroring how
``_REVIEW_BRIEFS`` gives the generic persona a specific instruction rather
than swapping personas.

**Parent.** Parented directly on ``quest_id``, mirroring
:func:`precis.quest.loop.ensure_quest_loop`'s coordinator job (also
parented straight on the quest ref despite the kind mismatch). The
``kind='todo'``-only parent check lives in ``TodoHandler.put``
(``handlers._todo_guards.check_parent_exists``), which this module — like
``dispatch.py``'s job-minting — bypasses by calling the store layer
directly. ``has_review``/``has_anchor`` only read the review-todo's own
``refs.meta``, so this is sufficient for the reviewer engine to pick it
up; the known tradeoff is the todo has no ``level:strategic`` ancestor, so
a todo-tree hygiene sweep may flag it as an orphan — an accepted
side-effect, same as any code-minted ref parented straight on a quest.

**Idempotency.** No ``idem_key`` primitive exists below ``JobHandler`` (the
todo/store layer has none), so this does its own existence check — a live
``kind='todo'`` child of ``quest_id`` already carrying this exact
``(review, anchor)`` pair — before inserting, so a re-weave of an
unchanged body doesn't stack duplicates. Returns only the ids minted by
*this* call; a lens that already has a live review-todo is skipped, not
re-returned.
"""

from __future__ import annotations

from typing import Any

from precis.store.types import Tag

#: Per-weave lens briefs. The generic reviewer persona
#: (``_load_review_persona`` in ``workers/planner_prompt.py``) is loaded
#: automatically for any ``has_review`` tick; these briefs name the
#: lens-specific skill on top of it, mirroring
#: ``precis_web.routes.drafts._REVIEW_BRIEFS``'s pattern.
_LENS_BRIEFS: dict[str, str] = {
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
        "id='precis-review-citation-faithfulness')` and apply it: does each "
        "cited passage actually and strongly support the claim it's cited "
        "for? File concrete anchored change requests for any hallucinated "
        "or unsupported citation."
    ),
}

#: Planner model tier for a per-weave review tick — CLOUD_MID ("sonnet"),
#: matching the design doc's routing table row "Review (per-weave/weekly)
#: | anchored section fisheye+1hop | mid, per persona".
_REVIEW_LLM_TAG = "sonnet"

#: The two per-weave lenses (design doc table) — ``structure``/
#: ``adversarial`` are weekly/deep-tier, minted elsewhere (not built here).
_DEFAULT_LENSES: tuple[str, ...] = ("flow", "cites")


def _existing_review_todo(
    store: Any, quest_id: int, lens: str, anchor: str
) -> int | None:
    """A live ``kind='todo'`` child of ``quest_id`` already carrying this
    exact ``(review, anchor)`` pair, or ``None``. Manual idempotency check
    — see the module docstring (no ``idem_key`` primitive below
    ``JobHandler``)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs "
            "WHERE parent_id = %s AND kind = 'todo' AND deleted_at IS NULL "
            "AND meta->>'review' = %s AND meta->>'anchor' = %s "
            "LIMIT 1",
            (quest_id, lens, anchor),
        ).fetchone()
    return int(row[0]) if row else None


def mint_weave_reviews(
    store: Any,
    quest_id: int,
    body_handle: str,
    *,
    lenses: tuple[str, ...] = _DEFAULT_LENSES,
) -> list[int]:
    """Mint one review-todo per lens for the section at ``body_handle``,
    parented on ``quest_id``.

    Each carries ``meta.review=<lens>`` + ``meta.anchor=body_handle`` — the
    shape :mod:`precis.utils.prompt.predicates`'s ``has_review``/
    ``has_anchor`` read to flip a ``plan_tick`` into reviewer mode over
    this section — plus an ``LLM:sonnet`` tag (so the dispatcher actually
    picks it up; see ``workers/dispatch.py``'s auto-run-signal predicate)
    and ``STATUS:open``.

    Returns the ids minted by *this* call — a lens already carrying a live
    review-todo for this exact ``body_handle`` is skipped (idempotent-
    friendly re-weave), so a repeat call over an unchanged body returns
    ``[]``.
    """
    minted: list[int] = []
    for lens in lenses:
        if _existing_review_todo(store, quest_id, lens, body_handle) is not None:
            continue
        brief = _LENS_BRIEFS.get(lens)
        text = (
            brief.format(h=body_handle)
            if brief is not None
            else (
                f"{lens} review of the draft section anchored at "
                f"{body_handle} (just woven). File concrete anchored "
                "change requests."
            )
        )
        with store.tx() as conn:
            ref = store.insert_ref(
                kind="todo",
                slug=None,
                title=text,
                meta={"anchor": body_handle, "review": lens},
                parent_id=quest_id,
                conn=conn,
            )
            store.add_tag(
                ref.id,
                Tag.closed("STATUS", "open"),
                set_by="system",
                replace_prefix=True,
                conn=conn,
            )
            store.add_tag(
                ref.id,
                Tag.closed("LLM", _REVIEW_LLM_TAG),
                set_by="system",
                conn=conn,
            )
        minted.append(int(ref.id))
    return minted


__all__ = ["mint_weave_reviews"]

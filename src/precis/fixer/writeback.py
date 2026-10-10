"""Write back a fixer tick's outcome onto the gripe it built.

The gripe-intake lane's read side (``intake.gripe_items``) has no write
counterpart until this module: a promoted gripe got built, gated, maybe
shipped — and the gripe record itself never learned any of that. This is
the sink half: one ``gripe_comment`` chunk summarising the outcome, and
(build outcomes only) a ``STATUS:`` flip to ``in_review`` so a human
knows there is something to look at. Diagnosis failures leave the gripe
``STATUS:open`` — the local ``fix/grN`` branch surviving the tick is
what stops a re-pick, not a status change.

Mirrors :func:`precis.fixer.intake.gripe_items`'s lazy-import + small-pool
shape: importing :mod:`precis.store` and
:mod:`precis.workers.executors._common` costs nothing on the
proposals-only path (``gripe_db_url`` unset), since both imports live
inside the function.
"""

from __future__ import annotations

import logging

log = logging.getLogger("precis.fixer")

_GRIPE_COMMENT_KIND = "gripe_comment"


#: A skill gripe the fixer failed to land this many times goes to a human.
ESCALATE_AFTER = 2
_ESCALATE_TAG = "waiting-for:reto"
_FAIL_MARK = "did not land"


def should_escalate(prior_comments: list[str], comment: str) -> bool:
    """True when ``comment`` (a non-landing outcome) is at least the
    :data:`ESCALATE_AFTER`-th fixer failure on this gripe's timeline."""
    fails = sum(
        1 for t in prior_comments if t.startswith("FIXER (auto):") and _FAIL_MARK in t
    )
    return _FAIL_MARK in comment and fails + 1 >= ESCALATE_AFTER


def gripe_writeback(
    db_url: str,
    ref_id: int,
    comment: str,
    status: str | None = None,
    *,
    escalate_skill: bool = False,
) -> bool:
    """Append ``comment`` to the gripe's timeline, then flip ``STATUS`` if set.

    Fail-soft by design: a write-back failure (DB unreachable, schema
    surprise, whatever) must never crash or fail the tick that already
    built, gated, and possibly shipped the fix — it's logged and
    swallowed. ``status`` uses :meth:`Store.add_tag` directly, never
    ``executors._common.set_status`` — that helper also refunds a job's
    resource reservation on a terminal status, logic that has no meaning
    for a gripe (which was never a claimed job).

    ``escalate_skill`` (the gripe links a skill): on the 2nd non-landing
    outcome, also tag ``waiting-for:reto`` with a one-line note of what was
    tried. Other gripes are untouched.
    """
    try:
        from precis.store import Tag
        from precis.store.store import Store
        from precis.workers.executors._common import append_chunk

        store = Store.connect(db_url, min_size=1, max_size=2)
        try:
            prior = (
                [b.text for b in store.chunks.list_chunks_for_ref(ref_id)]
                if escalate_skill
                else []
            )
            append_chunk(store, ref_id, _GRIPE_COMMENT_KIND, comment)
            if escalate_skill and status is None and should_escalate(prior, comment):
                tried = comment.splitlines()[0][:200]
                append_chunk(
                    store,
                    ref_id,
                    _GRIPE_COMMENT_KIND,
                    "FIXER (auto): escalated to reto after "
                    f"{ESCALATE_AFTER} failed attempts on a skill gripe — tried: {tried}",
                )
                store.add_tag(
                    ref_id,
                    Tag.parse_strict(_ESCALATE_TAG),
                    set_by="agent",
                )
            if status is not None:
                store.add_tag(
                    ref_id,
                    Tag.parse_strict(f"STATUS:{status}"),
                    set_by="agent",
                    replace_prefix=True,
                )
        finally:
            store.close()
        return True
    except Exception as exc:
        log.warning("fixer: gripe write-back failed for gripe:%d (%s)", ref_id, exc)
        return False

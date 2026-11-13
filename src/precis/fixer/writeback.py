"""Write back a fixer tick's outcome onto the gripe it built.

The gripe-intake lane's read side (``intake.gripe_items``) has no write
counterpart until this module: a promoted gripe got built, gated, maybe
shipped — and the gripe record itself never learned any of that. This is
the sink half: one ``gripe_comment`` chunk summarising the outcome, and
(build outcomes only) a ``STATUS:`` flip to ``in_review`` so a human
knows there is something to look at — skipped when the gripe already
sits at ``verify`` or a terminal status (pipeline v2: only the probe
verdict or a human retag moves those). Diagnosis failures leave the gripe
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
from typing import Any

log = logging.getLogger("precis.fixer")

_GRIPE_COMMENT_KIND = "gripe_comment"

#: gr595551: the fixer is the pipeline's ``code`` stage — its
#: write-back comments carry ``chunks.meta['stage']`` so the ledger
#: marks which stage appended them and the summary fold can run.
_STAGE_META = {"stage": "code"}


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


def _with_evidence(
    store: Any, ref_id: int, comment: str, touched: tuple[list[str], list[str]]
) -> str:
    """Append the code stage's ledger evidence as anchor handles.

    ``touched`` is ``(files, symbols)`` from
    :func:`precis.fixer.tick._change_evidence` — raw paths and
    ``path::qual`` pairs mint to ``an<N>`` handles first
    (:func:`precis.utils.gripe_summary.mint_anchors`, linked to this
    gripe as provisional aliases), and the ``touched files:`` /
    ``changed symbols:`` lines carry handles only — the ledger's
    anchor-only contract (gr595551); ``/match`` resolves them back to
    locations. A mint failure drops the evidence lines, never the
    outcome comment.
    """
    from precis.utils import gripe_summary

    files, symbols = touched
    handles = gripe_summary.mint_anchors(store, [*files, *symbols], gripe=ref_id)
    file_handles = [handles[f] for f in files if f in handles]
    sym_handles = [handles[s] for s in symbols if s in handles]
    if file_handles:
        comment += "\ntouched files: " + ", ".join(file_handles)
    if sym_handles:
        comment += "\nchanged symbols: " + ", ".join(sym_handles)
    return comment


def gripe_writeback(
    db_url: str,
    ref_id: int,
    comment: str,
    status: str | None = None,
    *,
    escalate_skill: bool = False,
    touched: tuple[list[str], list[str]] | None = None,
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

    ``touched`` is ``(files, symbols)`` of the build's diff (the code
    stage's evidence, gr595551): each location mints to an ``an<N>``
    anchor before the comment lands, so the ledger holds handles only.
    The outcome prose itself anchorizes first
    (:func:`precis.utils.gripe_summary.anchorize`): a ``path`` /
    ``path::qual`` the report carried (a failed gate's tail names
    files) mints to a handle, or redacts on a mint fault — a raw
    location never lands.

    gr595551: every appended comment carries ``meta.stage='code'`` —
    the fixer is the pipeline's code stage — and the append is
    followed by a fail-soft summary rebuild so the gripe's mutable
    current-state block tracks the ledger the stage just wrote.
    """
    try:
        from precis.store import Tag
        from precis.store.store import Store
        from precis.utils import gripe_summary
        from precis.workers.executors._common import append_chunk

        store = Store.connect(db_url, min_size=1, max_size=2)
        try:
            # The outcome prose can name code locations too — a failed
            # gate's tail carries paths — so it anchorizes the same way
            # the evidence lines do: mint ``an<N>`` handles, redact on a
            # mint fault, never land raw (gr595551, review of td655156).
            comment = gripe_summary.anchorize(store, comment, gripe=ref_id)
            if touched is not None:
                comment = _with_evidence(store, ref_id, comment, touched)
            prior = (
                [b.text for b in store.chunks.list_chunks_for_ref(ref_id)]
                if escalate_skill
                else []
            )
            append_chunk(store, ref_id, _GRIPE_COMMENT_KIND, comment, meta=_STAGE_META)
            if escalate_skill and status is None and should_escalate(prior, comment):
                tried = comment.splitlines()[0][:200]
                append_chunk(
                    store,
                    ref_id,
                    _GRIPE_COMMENT_KIND,
                    "FIXER (auto): escalated to reto after "
                    f"{ESCALATE_AFTER} failed attempts on a skill gripe — tried: {tried}",
                    meta=_STAGE_META,
                )
                store.add_tag(
                    ref_id,
                    Tag.parse_strict(_ESCALATE_TAG),
                    set_by="agent",
                )
            if status is not None:
                from precis.workers.job_types.fix_gripe import (
                    _NO_DOWNGRADE_GRIPE_STATUSES,
                )

                current = next(
                    (
                        str(t).split(":", 1)[1]
                        for t in store.tags_for(ref_id)
                        if str(t).startswith("STATUS:")
                    ),
                    None,
                )
                # A ``verify`` gripe's fix already shipped; only its probe
                # verdict (or a human retag) moves it — a late write-back
                # must not bump it back to ``in_review``. Same for the
                # terminal pair.
                if current not in _NO_DOWNGRADE_GRIPE_STATUSES:
                    store.add_tag(
                        ref_id,
                        Tag.parse_strict(f"STATUS:{status}"),
                        set_by="agent",
                        replace_prefix=True,
                    )
                    # gr520237: the write-back path bypasses the handler's
                    # tag verb, so run the close cascade here too — a
                    # terminal STATUS must reach referrers either way.
                    if status in ("done", "wontfix"):
                        from precis.handlers.gripe import gripe_close_cascade

                        with store.tx() as conn:
                            gripe_close_cascade(store, ref_id, status, conn=conn)
            # gr595551: the stage that appended entries rewrites the
            # gripe's mutable summary. Fail-soft on its own — the
            # ledger write already landed; a rebuild fault leaves the
            # summary marked stale, never fails the write-back.
            gripe_summary.fold(store, ref_id)
        finally:
            store.close()
        return True
    except Exception as exc:
        log.warning("fixer: gripe write-back failed for gripe:%d (%s)", ref_id, exc)
        return False

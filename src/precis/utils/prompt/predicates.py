"""Named ``applies_when`` predicates for conditional modules (ADR 0038 §8).

A module names a predicate; the assembler includes it only when the
predicate returns true, gating its *capability* and its *data* together
("never show a capability you're suppressing, nor data with no
capability"). We use a small fixed set of named predicates rather than
free expressions — totality-tested like the handle registry (open
question 2, "lean: named predicates").

Each predicate is ``(ctx) -> bool`` and may stash a computed value in
``ctx.extras`` so the gated builder reuses it instead of re-querying.
"""

from __future__ import annotations

from collections.abc import Callable

from precis.utils.prompt.model import AssemblyContext


def _anchor_handle(ctx: AssemblyContext) -> str | None:
    """The change-request anchor handle for this tick, memoised in extras.

    Reads ``refs.meta->>'anchor'`` (the ``dc<id>`` a web change-request
    or per-heading menu filed against), normalising the legacy ``¶``
    prefix away. Cached under ``extras['anchor']`` so both the predicate
    and the doc_context builder share one query."""
    if "anchor" in ctx.extras:
        return ctx.extras["anchor"]  # type: ignore[no-any-return]
    handle: str | None = None
    if ctx.store is not None:
        with ctx.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta->>'anchor' FROM refs WHERE ref_id = %s", (ctx.ref_id,)
            ).fetchone()
        handle = ((row[0] if row else None) or "").lstrip("¶").strip() or None
    ctx.extras["anchor"] = handle
    return handle


def has_anchor(ctx: AssemblyContext) -> bool:
    """True when this tick is anchored to a specific draft chunk.

    Gates the ``doc_context`` window/references table — it is meaningless
    without an anchor to centre on."""
    return _anchor_handle(ctx) is not None


def has_styled_anchor(ctx: AssemblyContext) -> bool:
    """True when the anchored chunk sits inside a styled section (ADR 0037).

    Gates loading the section-style skill body."""
    handle = _anchor_handle(ctx)
    if handle is None or ctx.store is None:
        return False
    return bool(ctx.store.section_style_for(handle))


def _review_kind(ctx: AssemblyContext) -> str | None:
    """The ``meta.review`` lens of this tick's todo, memoised in extras.

    A review-todo (filed by the draft reader's "review ▾" menu) carries
    ``meta.review = structural | deep_review | citation | all``. Cached
    under ``extras['review']`` so both the predicate and the reviewer
    persona / section modules share one query."""
    if "review" in ctx.extras:
        return ctx.extras["review"]  # type: ignore[no-any-return]
    review: str | None = None
    if ctx.store is not None:
        with ctx.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta->>'review' FROM refs WHERE ref_id = %s", (ctx.ref_id,)
            ).fetchone()
        review = (row[0] if row else None) or None
    ctx.extras["review"] = review
    return review


def has_review(ctx: AssemblyContext) -> bool:
    """True when this tick is a draft-section review (``meta.review`` set).

    Gates the reviewer-persona + section-under-review blocks (ADR 0038
    step 3 / Shot 3): when set, the planner tick reviews the anchored
    section and files anchored change requests instead of editing."""
    return _review_kind(ctx) is not None


#: The named-predicate registry. ``applies_when`` strings resolve here;
#: an unknown name is a programming error (caught by the totality test),
#: not a silent always-true.
PREDICATES: dict[str, Callable[[AssemblyContext], bool]] = {
    "has_anchor": has_anchor,
    "has_styled_anchor": has_styled_anchor,
    "has_review": has_review,
}


def evaluate(name: str, ctx: AssemblyContext) -> bool:
    """Resolve and run a named predicate; raise on an unknown name."""
    try:
        pred = PREDICATES[name]
    except KeyError:
        raise KeyError(
            f"unknown applies_when predicate {name!r}; "
            f"known: {', '.join(sorted(PREDICATES))}"
        ) from None
    return pred(ctx)


__all__ = [
    "PREDICATES",
    "evaluate",
    "has_anchor",
    "has_review",
    "has_styled_anchor",
]

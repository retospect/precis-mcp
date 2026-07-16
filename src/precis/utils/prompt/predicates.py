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


def is_patent(ctx: AssemblyContext) -> bool:
    """True when this tick writes a patent (``meta.workspace.doc_type ==
    'patent'``, cascaded onto the todo).

    Gates the patent-authoring block: the prior-art sweep→ingest loop, the
    freedom-to-operate claims view, and the scoping-decision ledger
    convention (docs/design/patent-authoring-loop.md). Specialises a generic
    ``plan_tick`` in the variable layer — no separate job_type (like
    reviewer / backfill mode). Memoised under ``extras['is_patent']``."""
    if "is_patent" in ctx.extras:
        return ctx.extras["is_patent"]  # type: ignore[no-any-return]
    result = False
    if ctx.store is not None:
        from precis.utils.workspace import Workspace

        with ctx.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta FROM refs WHERE ref_id = %s", (ctx.ref_id,)
            ).fetchone()
        ws = Workspace.from_meta(row[0]) if row else None
        result = ws is not None and ws.doc_type == "patent"
    ctx.extras["is_patent"] = result
    return result


def _backfill_targets(ctx: AssemblyContext) -> list[str]:
    """The draft chunk handles this source-backfill tick works on, memoised.

    A source-backfill tick's todo carries ``meta.backfill`` — either a shape
    ``{"targets": ["dc12", "dc34"]}`` naming the sections to backfill, or a bare
    truthy marker, in which case we fall back to the tick's ``meta.anchor`` (the
    single anchored section). Returns ``[]`` when the tick is not a backfill run
    (or names no resolvable target). Cached under ``extras['backfill_targets']``
    so the predicate and the builder share one query."""
    if "backfill_targets" in ctx.extras:
        return ctx.extras["backfill_targets"]  # type: ignore[no-any-return]
    targets: list[str] = []
    if ctx.store is not None:
        with ctx.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta->'backfill', meta->>'anchor' FROM refs WHERE ref_id = %s",
                (ctx.ref_id,),
            ).fetchone()
        spec = row[0] if row else None
        anchor = ((row[1] if row else None) or "").lstrip("¶").strip() or None
        if spec is not None:
            if isinstance(spec, dict):
                raw = spec.get("targets") or []
                targets = [str(t).strip() for t in raw if str(t).strip()]
            if not targets and anchor:
                targets = [anchor]
    ctx.extras["backfill_targets"] = targets
    return targets


def has_backfill(ctx: AssemblyContext) -> bool:
    """True when this tick is a source-backfill run (``meta.backfill`` set with a
    resolvable target).

    Gates the ``backfill`` block — the recall workspace (uncited-but-relevant
    corpus sources for the target sections) plus the weave/dismiss/request
    instructions. Like reviewer-mode, it specialises a generic ``plan_tick`` in
    the variable layer; no separate job_type."""
    return bool(_backfill_targets(ctx))


#: The named-predicate registry. ``applies_when`` strings resolve here;
#: an unknown name is a programming error (caught by the totality test),
#: not a silent always-true.
PREDICATES: dict[str, Callable[[AssemblyContext], bool]] = {
    "has_anchor": has_anchor,
    "has_styled_anchor": has_styled_anchor,
    "has_review": has_review,
    "has_backfill": has_backfill,
    "is_patent": is_patent,
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
    "has_backfill",
    "has_review",
    "has_styled_anchor",
    "is_patent",
]

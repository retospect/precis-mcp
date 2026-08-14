"""``get(kind='draft', view='review')`` / ``view='review-diff'`` — the
memoized approval ledger (paper-writing pipeline rung 3, docs/backlog/paper-writing-pipeline.md §"Review — the memoized approval ledger").

Two renderers, both pure reads over :class:`~precis.store._draft_ops.
DraftStore`'s ledger methods (``chunk_review``, migration 0086):

* :func:`render_review_view` — whole-draft. Per live chunk, each checker's
  status (``current ✓`` / ``dirty ✗`` / never-reviewed), with a trailer
  flagging chunks dirty-for-``human`` — the set a later rung's export gate
  will block on.
* :func:`render_review_diff_view` — one chunk. The ``human`` checker's
  approved→current diff (:meth:`Store.review_diff_since`), which walks the
  ``chunk_events`` version chain.

Modelled on ``handlers/_integration_view.py``'s render-through-``Store``-
API style and ``draft.py``'s ``_render_wordcount``'s ``toon.dump`` table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.errors import NotFound
from precis.format import toon
from precis.response import Response
from precis.store._draft_ops import DraftReviewRow

if TYPE_CHECKING:
    from precis.store import Ref, Store

_HUMAN = "human"


def _preview(text: str, n: int = 60) -> str:
    first = (text or "").strip().splitlines()[0] if text else ""
    return first[:n] + "…" if len(first) > n else first


def render_review_view(store: Store, ref: Ref) -> Response:
    """Render ``view='review'`` for a whole draft."""
    chunks = store.drafts.reading_order(ref.id)
    header = f"# {ref.slug or ref.id} — review ledger"
    if not chunks:
        return Response(body=f"{header}\n\n(no chunks yet)")

    per_chunk: dict[int, dict[str, DraftReviewRow]] = {}
    checkers_seen: set[str] = {_HUMAN}
    for row in store.drafts.review_status_for_draft(ref.id):
        if row.checker is None:  # never reviewed by anyone — no entry
            continue
        by_checker = per_chunk.setdefault(row.chunk_id, {})
        by_checker[row.checker] = row
        checkers_seen.add(row.checker)
    checkers = [_HUMAN, *sorted(checkers_seen - {_HUMAN})]

    def _mark(status: DraftReviewRow | None) -> str:
        if status is None:
            return "–"  # never reviewed
        return "✗" if status.dirty else "✓"

    rows: list[dict[str, str]] = []
    dirty_for_human: list[str] = []
    for c in chunks:
        by_checker = per_chunk.get(c.chunk_id, {})
        human = by_checker.get(_HUMAN)
        if human is None or human.dirty:
            dirty_for_human.append(c.dc)
        line: dict[str, str] = {
            "handle": c.dc,
            "kind": c.chunk_kind,
            "text": _preview(c.text),
        }
        for checker in checkers:
            line[checker] = _mark(by_checker.get(checker))
        rows.append(line)

    table = toon.dump(rows, schema=["handle", "kind", "text", *checkers])
    trailer = (
        f"\n\n{len(chunks)} chunk(s), {len(checkers)} checker(s): {', '.join(checkers)}"
    )
    if dirty_for_human:
        names = ", ".join(dirty_for_human[:10])
        more = (
            ""
            if len(dirty_for_human) <= 10
            else f" (+{len(dirty_for_human) - 10} more)"
        )
        trailer += f"\n⚠ {len(dirty_for_human)} chunk(s) dirty-for-human: {names}{more}"
    else:
        trailer += "\n✓ nothing dirty-for-human"
    trailer += (
        "\n\nNext: edit(kind='draft', id='dc<id>', review='human') to approve "
        "a chunk at its current sha; get(kind='draft', id='dc<id>', "
        "view='review-diff') to see what changed since human last approved."
    )
    return Response(body=f"{header}\n\n{table}{trailer}")


def render_review_diff_view(store: Store, addr: str) -> Response:
    """Render ``view='review-diff'`` for one chunk — the ``human``
    checker's approved→current diff."""
    chunk = store.drafts.get_draft_chunk(addr)
    if chunk is None:
        raise NotFound(f"draft chunk {addr!r} not found")
    statuses = store.drafts.review_status_for_chunk(chunk.chunk_id)
    human = next((s for s in statuses if s.checker == _HUMAN), None)
    header = f"# {chunk.dc} — review-diff (human)"
    if human is None:
        return Response(
            body=f"{header}\n\nnever approved by human — nothing to diff against.\n\n"
            f"Next: edit(kind='draft', id='{chunk.dc}', review='human') to approve "
            "the current text."
        )
    assert human.approved_sha is not None  # human always has an approved_sha
    diff = store.drafts.review_diff_since(chunk.chunk_id, human.approved_sha)
    if not diff:
        return Response(
            body=f"{header}\n\nno change since human approved "
            f"@ {human.approved_sha[:12]}…"
        )
    return Response(
        body=f"{header} — approved @ {human.approved_sha[:12]}… vs current\n\n{diff}"
    )


__all__ = ["render_review_diff_view", "render_review_view"]

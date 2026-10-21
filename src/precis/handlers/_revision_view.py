"""``view='history'`` and ``view='diff'`` for ref kinds — the
local-mesh-upkeep §2b audit surfaces on the ``revisions`` log.

``view='history'`` renders the ref's revision chain (one row per
transaction that changed a covered part): when, what (edited / retired /
restored / deleted / merged-into), who, which model, the reason, and the
sha pair. ``view='diff'`` with ``args={'since': <sha>}`` renders the
recorded state at that sha against now via
:meth:`precis.store._review_ops.ReviewsMixin.revision_diff` — the
"diff the version a review saw against the current one" reader, where
``since`` is the review's ``content_sha``. Kinds without
``kinds.covered_meta`` keep no history (migration 0185): the views
answer so rather than pretending the table is empty.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.response import Response

if TYPE_CHECKING:
    from precis.store import Ref, Store


def _keeps_history(store: Store, kind: str) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT covered_meta IS NOT NULL FROM kinds WHERE slug = %s",
            (kind,),
        ).fetchone()
    return bool(row[0]) if row else False


def render_revision_history(store: Store, ref: Ref) -> Response:
    """The ref's revision chain, oldest first — one line per recorded
    transaction: ``at  event  actor[/model]  prev_sha→new_sha  reason``."""
    revs = store.revisions_for("ref", ref.id)
    if not revs:
        why = (
            ""
            if _keeps_history(store, ref.kind)
            else f" — kind {ref.kind!r} keeps no history (kinds.covered_meta is NULL)"
        )
        return Response(
            body=f"history: no revisions recorded for {ref.kind} {ref.id}{why}",
            result_count=0,
        )
    lines = [f"history: {len(revs)} revision(s) for {ref.kind} {ref.id}"]
    for r in revs:
        who = r.actor + (f"/{r.model}" if r.model else "")
        shas = f"{(r.prev_sha or '-')[:8]}→{(r.new_sha or '-')[:8]}"
        lines.append(
            f"  {r.at:%Y-%m-%d %H:%MZ}  {r.event:<12}  {who:<24}  {shas}  {r.reason}"
        )
    return Response(body="\n".join(lines), result_count=len(revs))


def render_revision_diff(store: Store, ref: Ref, since: Any) -> Response:
    """``view='diff'``: the recorded state at ``since`` vs now."""
    if since is None or str(since).strip() == "":
        raise BadInput(
            "view='diff' needs the sha to diff from",
            next=f"get(kind={ref.kind!r}, id={ref.id}, view='history') lists "
            "the recorded shas; then view='diff', args={'since': <sha>}",
        )
    diff = store.revision_diff("ref", ref.id, str(since).strip())
    if diff == "":
        return Response(
            body=f"diff: no change since {str(since).strip()} on {ref.kind} {ref.id}",
            result_count=0,
        )
    return Response(body=diff, result_count=1)


__all__ = ["render_revision_diff", "render_revision_history"]

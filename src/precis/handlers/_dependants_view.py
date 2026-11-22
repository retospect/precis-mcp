"""``view='dependants'`` rendering + the mutation-review preview block
(gr626866).

Three surfaces share the one walk (:meth:`precis.store._dependants.
DependantsMixin.dependants`):

- ``render_dependants_view`` — the ``get(kind=K, id=N,
  view='dependants', args={'depth'|'limit'|'cursor'})`` read on every
  numeric-ref kind and on paper-family kinds.
- ``dependants_block`` — the ``## Dependants`` section a
  ``delete(dry_run=True)`` / ``edit(dry_run=True)`` preview appends:
  the first page plus the view call that continues the walk.
- ``dependants_summary`` — the one-line ``dependants: X shown of Y``
  suffix a real delete/edit ack appends, ``(INCOMPLETE)`` when the walk
  was truncated, so a mutation ack never reads a capped remainder as
  "no dependants".
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.response import Response
from precis.store._dependants import (
    DEPENDANTS_MAX_LIMIT,
    DEPENDANTS_NODE_BUDGET,
    DependantRow,
    DependantsPage,
)
from precis.utils import handle_registry, record_frame

if TYPE_CHECKING:
    from precis.store.store import Store

_CURSOR_RE = re.compile(r"dep:\d+")

#: Rows a mutation preview lists inline — the full walk stays one
#: ``view='dependants'`` call away.
_PREVIEW_ROWS = 10


def _handle(kind: str, ref_id: int) -> str:
    return handle_registry.try_format(kind, ref_id) or f"{kind}:{ref_id}"


def _parse_walk_args(
    args: dict[str, Any] | None,
    *,
    kind: str,
    id_arg: str,
) -> tuple[int, int, str | None]:
    """``args={'depth'|'limit'|'cursor'}`` → typed walk args.

    A malformed cursor is a ``BadInput`` pointing at the first-page call
    rather than a silent first-page read — a guessed cursor must never
    page to the wrong place (gr626866's "unknown cursor" contract).
    """
    a = args or {}
    call = (
        f"get(kind={kind!r}, id={id_arg!r}, view='dependants', "
        "args={'depth': 2, 'limit': 25})"
    )
    depth = a.get("depth", 2)
    limit = a.get("limit", 25)
    cursor = a.get("cursor")
    try:
        depth = int(depth)
    except (TypeError, ValueError):
        raise BadInput(
            f"args['depth'] must be an int, got {a.get('depth')!r}", next=call
        ) from None
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise BadInput(
            f"args['limit'] must be an int, got {a.get('limit')!r}", next=call
        ) from None
    if limit < 1 or limit > DEPENDANTS_MAX_LIMIT:
        raise BadInput(
            f"args['limit'] must be 1..{DEPENDANTS_MAX_LIMIT}, got {limit!r}",
            next=call,
        )
    if depth < 1:
        raise BadInput(f"args['depth'] must be >= 1, got {depth!r}", next=call)
    if cursor is not None and not _CURSOR_RE.fullmatch(str(cursor)):
        raise BadInput(
            f"unknown dependants cursor {cursor!r}",
            next=call,
        )
    return depth, limit, (str(cursor) if cursor is not None else None)


def _row_line(row: DependantRow, statuses: dict[int, str] | None = None) -> str:
    status = (statuses or {}).get(row.ref_id)
    token = f" [{status}]" if status else ""
    return (
        f"- d{row.depth} {_handle(row.kind, row.ref_id)}{token} ({row.relation}) "
        f"via {_handle(row.via_kind, row.via_ref_id)}"
    )


def _footer(
    page: DependantsPage,
    *,
    kind: str,
    id_arg: str,
    depth: int,
    limit: int,
) -> str:
    """``shown X of Y · INCOMPLETE (N uninspected) · next: <call>`` or
    ``shown X of Y · complete`` — the incomplete marker and the exact
    continuation call travel together (gr626866)."""
    line = f"shown {page.shown} of {page.total_seen}"
    if page.complete:
        return f"{line} · complete"
    line += " · INCOMPLETE"
    if page.uninspected:
        line += f" ({page.uninspected} uninspected)"
    if page.next_cursor is not None:
        line += (
            f" · next: get(kind={kind!r}, id={id_arg!r}, view='dependants', "
            f"args={{'depth': {depth}, 'limit': {limit}, "
            f"'cursor': {page.next_cursor!r}}})"
        )
    return line


def render_dependants_view(
    store: Store,
    *,
    kind: str,
    sense: str,
    ref_id: int,
    id_arg: str,
    args: dict[str, Any] | None = None,
) -> Response:
    """``view='dependants'`` — one paged page of the rely-side walk.

    ``id_arg`` is the id spelling the ``next:`` call re-issues (the
    numeric id for numeric kinds, ``pa<N>``/slug for papers)."""
    depth, limit, cursor = _parse_walk_args(args, kind=kind, id_arg=id_arg)
    page = store.dependants(ref_id, depth=depth, limit=limit, cursor=cursor)
    statuses = record_frame.status_cells(store, {r.ref_id for r in page.rows})
    lines = [f"# {sense} {id_arg} — dependants"]
    if page.rows:
        lines += ["", *(_row_line(r, statuses) for r in page.rows)]
    else:
        lines += [
            "",
            "(no dependants seen)" if page.complete else "(none on this page)",
        ]
    lines += ["", _footer(page, kind=kind, id_arg=id_arg, depth=depth, limit=limit)]
    return Response(body="\n".join(lines), result_count=page.shown)


def dependants_block(
    store: Store,
    *,
    kind: str,
    ref_id: int,
    id_arg: str,
    page: DependantsPage | None = None,
) -> str:
    """The ``## Dependants`` preview section for dry-run previews.

    First page only; the footer always carries the view call — even a
    complete page — so the section teaches where the read lives.
    ``page`` reuses a walk the caller already ran for the same ref.
    """
    if page is None:
        page = store.dependants(
            ref_id,
            depth=2,
            limit=_PREVIEW_ROWS,
            node_budget=DEPENDANTS_NODE_BUDGET,
        )
    lines = [
        "## Dependants — "
        + _footer(page, kind=kind, id_arg=id_arg, depth=2, limit=_PREVIEW_ROWS)
    ]
    statuses = record_frame.status_cells(store, {r.ref_id for r in page.rows})
    lines += [_row_line(r, statuses) for r in page.rows] or ["(no dependants seen)"]
    if not page.complete:
        lines.append(f"continue: get(kind={kind!r}, id={id_arg!r}, view='dependants')")
    else:
        lines.append(f"inspect: get(kind={kind!r}, id={id_arg!r}, view='dependants')")
    return "\n".join(lines)


def dependants_summary(
    store: Store,
    *,
    ref_id: int,
    page: DependantsPage | None = None,
) -> str:
    """The one-line ack suffix: ``dependants: X shown of Y`` plus
    ``(INCOMPLETE: N uninspected)`` when the walk was cut — the mutation
    ack never claims "no dependants" off a capped read."""
    if page is None:
        page = store.dependants(ref_id, depth=2, limit=_PREVIEW_ROWS)
    line = f"dependants: {page.shown} shown of {page.total_seen}"
    if page.complete:
        return line
    line += " (INCOMPLETE)"
    if page.uninspected:
        line += f" — {page.uninspected} uninspected"
    return line


def dependants_portion(body: str) -> list[str]:
    """The dependants lines of a single-mutation response body.

    Returns the ``## Dependants`` block (dry-run preview) or the
    ``dependants: …`` summary line (real mutation) so batch callers can
    append them to a per-id line that otherwise keeps only the body's
    first line. The block ends at the first blank line — an advisory a
    wrapper appends after it is not dependants content.
    """
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## Dependants"):
            out = [line]
            for tail in lines[i + 1 :]:
                if not tail.strip():
                    break
                out.append(tail)
            return out
        if line.startswith("dependants:"):
            return [line]
    return []

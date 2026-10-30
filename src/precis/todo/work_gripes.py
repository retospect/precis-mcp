"""Open gripes in the work view (dark-factory step 1, td540842).

``search(kind='todo', view='work')`` hangs every open gripe under the
todo root that carries its ``thread:<slug>`` tag, one row each::

    gr541348 in-build [claude] — title

``<stage>`` is the loop queue derived by :class:`precis.handlers._gripe_fix.
FixState` (``unbriefed → queued → in-build → in-review → landing /
fix-round → awaiting-deploy``); a gripe outside the fix loop has no brief
yet and reads ``unbriefed``. ``[lane]`` is the builder lane: a ``lane:
<name>`` line in the newest brief chunk, else the vendor half of the
loop's ``session`` (``claude:window``); absent when neither names one.

Gripes whose thread matches no rendered root go in one "unplaced" bucket
at the end of the view — the count plus the five oldest — so
``placed + unplaced == open gripes``. "Open" is every non-terminal STATUS,
the same definition as ``get(kind='gripe', id='/open')``. A gripe that
carries two thread tags hangs under both roots.

Two entry points, called from :mod:`precis.todo.work_view`:
:func:`append_root_gripes` (one root's rows) and :func:`append_unplaced`
(the closing bucket).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from precis.handlers import _gripe_fix as gf
from precis.todo import views
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store import Ref, Store

THREAD_PREFIX = "thread:"
#: Gripe rows shown under one root before the ``… +N`` line.
MAX_GRIPES_PER_ROOT = 15
#: Oldest unplaced gripes listed in the closing bucket.
UNPLACED_SHOWN = 5
#: Indent of a gripe row: one level below the root's children.
_INDENT = "    "
_LANE_RE = re.compile(r"^\W*lane\s*[:=]\s*([\w.-]+)", re.IGNORECASE | re.MULTILINE)
_STAGE_RANK = {q: i for i, q in enumerate(gf.QUEUES)}


@dataclass(frozen=True)
class GripeRow:
    ref_id: int
    title: str
    stage: str
    lane: str | None
    created_at: datetime
    threads: tuple[str, ...]


def thread_slugs(tags: list[str]) -> tuple[str, ...]:
    """The ``<slug>`` of every ``thread:<slug>`` tag, sorted, deduped."""
    return tuple(
        sorted(
            {
                t[len(THREAD_PREFIX) :]
                for t in tags
                if t.startswith(THREAD_PREFIX) and len(t) > len(THREAD_PREFIX)
            }
        )
    )


def _terminal_tags() -> list[str]:
    # Lazy: the numeric-ref base pulls the handler stack in.
    from precis.handlers._numeric_ref import _BROWSE_TERMINALS

    return sorted(_BROWSE_TERMINALS["gripe"])


def _open_gripes(store: Store, *, tags: list[str] | None = None) -> list[Ref]:
    return store.list_refs(
        kind="gripe",
        tags=tags,
        exclude_tags=_terminal_tags(),
        order_by="created_asc",
        limit=5000,
    )


def _lane(state: gf.FixState) -> str | None:
    brief = state.newest_of(gf.BRIEF_KIND)
    if brief is not None:
        m = _LANE_RE.search(brief.text)
        if m:
            return m.group(1)
    if state.session and ":" in state.session:
        return state.session.split(":", 1)[0] or None
    return None


def _rows(store: Store, refs: list[Ref]) -> list[GripeRow]:
    """Rows for ``refs`` in the order given (two queries for the states,
    one for the thread tags)."""
    tag_rows = store.ref_tags_bulk([r.id for r in refs])
    out: list[GripeRow] = []
    for ref, state in zip(refs, gf.bulk_states(store, refs), strict=True):
        open_tags = [v for ns, v in tag_rows.get(ref.id, []) if ns == "OPEN"]
        out.append(
            GripeRow(
                ref_id=ref.id,
                title=ref.title or "",
                stage=state.queue or "unbriefed",
                lane=_lane(state),
                created_at=ref.created_at,
                threads=thread_slugs(open_tags),
            )
        )
    return out


def gripes_by_thread(store: Store) -> dict[str, list[GripeRow]]:
    """``{slug: rows}`` over every open gripe carrying a ``thread:`` tag;
    a gripe with several threads appears under each. Rows are ordered by
    loop stage, then id."""
    refs = _open_gripes(store)
    out: dict[str, list[GripeRow]] = {}
    for row in _rows(store, refs):
        for slug in row.threads:
            out.setdefault(slug, []).append(row)
    for rows in out.values():
        rows.sort(key=lambda r: (_STAGE_RANK.get(r.stage, 99), r.ref_id))
    return out


def _handle(ref_id: int) -> str:
    return handle_registry.try_format("gripe", ref_id) or f"gripe:{ref_id}"


def _first_line(title: str, cap: int = 72) -> str:
    line = title.split("\n", 1)[0].strip()
    return line if len(line) <= cap else line[: cap - 1].rstrip() + "…"


def render_gripe_rows(rows: list[GripeRow], *, indent: str = _INDENT) -> list[str]:
    """One ``gr<id> <stage> [lane] — title`` line per row."""
    out = []
    for r in rows:
        cells = [_handle(r.ref_id), r.stage]
        if r.lane:
            cells.append(f"[{r.lane}]")
        out.append(f"{indent}{' '.join(cells)} — {_first_line(r.title)}")
    return out


def append_root_gripes(store: Store, root_tags: list[str], out: list[str]) -> None:
    """Append the open gripes tagged with any of the root's ``thread:``
    slugs, under the root's rows."""
    slugs = thread_slugs(root_tags)
    if not slugs:
        return
    seen: set[int] = set()
    refs: list[Ref] = []
    for slug in slugs:
        for ref in _open_gripes(store, tags=[f"{THREAD_PREFIX}{slug}"]):
            if ref.id not in seen:
                seen.add(ref.id)
                refs.append(ref)
    if not refs:
        return
    rows = _rows(store, refs)
    rows.sort(key=lambda r: (_STAGE_RANK.get(r.stage, 99), r.ref_id))
    out.extend(render_gripe_rows(rows[:MAX_GRIPES_PER_ROOT]))
    if len(rows) > MAX_GRIPES_PER_ROOT:
        out.append(
            f"{_INDENT}… +{len(rows) - MAX_GRIPES_PER_ROOT} more gripes — "
            f"search(kind='gripe', tags=['{THREAD_PREFIX}{slugs[0]}'])"
        )


def _rendered_root_ids(store: Store, served: set[int]) -> set[int]:
    """The served roots the view renders as roots: a served todo that sits
    inside another served root's tree renders there instead."""
    refs = store.fetch_refs_by_ids(set(served))
    rendered: set[int] = set()
    for rid, ref in refs.items():
        seen = {rid}
        parent = ref.parent_id
        nested = False
        while parent is not None and parent not in seen:
            if parent in served:
                nested = True
                break
            seen.add(parent)
            up = store.fetch_refs_by_ids({parent}).get(parent)
            parent = up.parent_id if up is not None else None
        if not nested:
            rendered.add(rid)
    return rendered


def append_unplaced(store: Store, served: set[int], out: list[str]) -> None:
    """Append the "unplaced" bucket: open gripes whose thread matches no
    rendered root, the count plus the :data:`UNPLACED_SHOWN` oldest."""
    roots = store.fetch_refs_by_ids(_rendered_root_ids(store, served))
    root_tags = views._all_open_tags(store, list(roots))
    placed_threads: set[str] = set()
    for tags in root_tags.values():
        placed_threads.update(thread_slugs(tags))
    refs = _open_gripes(store)
    tag_rows = store.ref_tags_bulk([r.id for r in refs])
    unplaced = [
        r
        for r in refs
        if not placed_threads.intersection(
            thread_slugs([v for ns, v in tag_rows.get(r.id, []) if ns == "OPEN"])
        )
    ]
    if not unplaced:
        return
    out += ["", f"unplaced gripes ({len(unplaced)}) — open, thread matches no root"]
    out.extend(render_gripe_rows(_rows(store, unplaced[:UNPLACED_SHOWN]), indent="  "))
    if len(unplaced) > UNPLACED_SHOWN:
        out.append(
            f"  … +{len(unplaced) - UNPLACED_SHOWN} newer — get(kind='gripe', id='/open')"
        )


__all__ = [
    "MAX_GRIPES_PER_ROOT",
    "UNPLACED_SHOWN",
    "GripeRow",
    "append_root_gripes",
    "append_unplaced",
    "gripes_by_thread",
    "render_gripe_rows",
    "thread_slugs",
]

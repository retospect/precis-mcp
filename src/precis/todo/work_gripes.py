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

Rows are data first (td541980): :func:`root_gripes` (one root's rows) and
:func:`unplaced_gripes` (the closing bucket) return a :class:`GripeBlock`
of frozen :class:`GripeRow`, carried by :mod:`precis.todo.work_view`'s
model; :func:`render_root_gripes` / :func:`render_unplaced` format a block
as the MCP text above and the ``/work`` page formats the same rows as
HTML (``data-handle``, ``/eye`` link), so the two cannot drift.
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
    """One open gripe. ``stage`` is the derived loop queue and ``lane`` the
    builder lane (module docstring); ``created_at`` is the web age column;
    ``threads`` every ``thread:`` slug it carries (a row hung under one
    root may carry others)."""

    ref_id: int
    title: str
    stage: str
    lane: str | None
    created_at: datetime
    threads: tuple[str, ...]
    #: ``gr<id>`` — the click / ``get`` handle.
    handle: str = ""


@dataclass(frozen=True)
class GripeBlock:
    """The rows one view section shows, plus what the cap left out.
    ``hidden`` rows are not in ``rows``; ``more`` is the call that lists
    them."""

    rows: tuple[GripeRow, ...] = ()
    hidden: int = 0
    more: str = ""


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
                handle=_handle(ref.id),
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
        cells = [r.handle, r.stage]
        if r.lane:
            cells.append(f"[{r.lane}]")
        out.append(f"{indent}{' '.join(cells)} — {_first_line(r.title)}")
    return out


def root_gripes(store: Store, root_tags: list[str]) -> GripeBlock:
    """The open gripes tagged with any of the root's ``thread:`` slugs
    (``root_tags`` are the root's open tags), by loop stage then id, capped
    at :data:`MAX_GRIPES_PER_ROOT`."""
    slugs = thread_slugs(root_tags)
    if not slugs:
        return GripeBlock()
    seen: set[int] = set()
    refs: list[Ref] = []
    for slug in slugs:
        for ref in _open_gripes(store, tags=[f"{THREAD_PREFIX}{slug}"]):
            if ref.id not in seen:
                seen.add(ref.id)
                refs.append(ref)
    if not refs:
        return GripeBlock()
    rows = _rows(store, refs)
    rows.sort(key=lambda r: (_STAGE_RANK.get(r.stage, 99), r.ref_id))
    return GripeBlock(
        rows=tuple(rows[:MAX_GRIPES_PER_ROOT]),
        hidden=max(0, len(rows) - MAX_GRIPES_PER_ROOT),
        more=f"search(kind='gripe', tags=['{THREAD_PREFIX}{slugs[0]}'])",
    )


def render_root_gripes(block: GripeBlock) -> list[str]:
    """A root's gripe rows as MCP text lines (empty for an empty block)."""
    out = render_gripe_rows(list(block.rows))
    if block.hidden:
        out.append(f"{_INDENT}… +{block.hidden} more gripes — {block.more}")
    return out


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


def unplaced_gripes(store: Store, served: set[int]) -> GripeBlock:
    """The "unplaced" bucket: open gripes whose thread matches no rendered
    root — the :data:`UNPLACED_SHOWN` oldest as rows, the rest as
    ``hidden`` (so ``len(rows) + hidden`` is the bucket's count)."""
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
        return GripeBlock()
    return GripeBlock(
        rows=tuple(_rows(store, unplaced[:UNPLACED_SHOWN])),
        hidden=max(0, len(unplaced) - UNPLACED_SHOWN),
        more="get(kind='gripe', id='/open')",
    )


def render_unplaced(block: GripeBlock) -> list[str]:
    """The unplaced bucket as MCP text lines (empty for an empty block)."""
    if not block.rows:
        return []
    count = len(block.rows) + block.hidden
    out = ["", f"unplaced gripes ({count}) — open, thread matches no root"]
    out.extend(render_gripe_rows(list(block.rows), indent="  "))
    if block.hidden:
        out.append(f"  … +{block.hidden} newer — {block.more}")
    return out


__all__ = [
    "MAX_GRIPES_PER_ROOT",
    "UNPLACED_SHOWN",
    "GripeBlock",
    "GripeRow",
    "gripes_by_thread",
    "render_gripe_rows",
    "render_root_gripes",
    "render_unplaced",
    "root_gripes",
    "thread_slugs",
    "unplaced_gripes",
]

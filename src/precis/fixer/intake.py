"""Fixer intake: what's ready to build, and which one is next.

Three risky small bits flagged at design time live here:

* **Ready convention.** A work item is a transient file under
  ``docs/backlog/*.md`` with a YAML-ish front-matter block; it is
  *pickable* only when ``status: ready`` (a human ran ``/ready`` in
  tandem, both keys turned). ``README.md``/``TEMPLATE.md`` and any
  other ``status:`` value (``idea``, ``draft``, …) are ignored.
* **Idempotent pick.** The loop re-fires every interval, so it must
  skip an item it has already branched — otherwise it re-clones and
  re-builds the same thing forever. Skip is a ``branch_exists``
  predicate (local branch / worktree / remote head), injected so the
  pure pick logic stays unit-testable.
* **Gripe intake, behind a dial.** ``PRECIS_FIXER_GRIPE_DB`` (a
  Postgres URL) gates a second source: *promoted* open gripes — tag
  ``auto-fix`` + a ``DIAGNOSIS``-prefixed timeline comment (minted by
  :mod:`precis.workers.job_types.diagnose_gripe`, see
  ``docs/backlog/dark-factory-arming.md``) — normalized into the same
  :class:`WorkItem`
  shape as a proposal and merged into one priority-ordered queue via
  :func:`all_items`. Unset (the plist default) means the lane is
  fully dark and **no store/DB import is attempted** —
  :func:`gripe_items` imports :mod:`precis.store` lazily, only when
  called with a URL, so the proposals-only path never pays for it and
  never needs a DB reachable. This slice is read-only: SELECT the
  promoted gripe + its timeline, never write back (status flip /
  timeline append on build is a follow-on).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger("precis.fixer")

#: Front-matter fence: a leading ``---`` line, body, closing ``---``.
_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

#: Filenames under docs/backlog/ that are never work items.
_NON_PROPOSAL_STEMS = frozenset({"template", "readme"})


@dataclass(frozen=True)
class WorkItem:
    """One pickable unit of repo-dev work.

    ``branch`` is the deterministic branch name the fixer uses; the
    idempotent-pick check keys on it. ``spec_text`` is the brief fed
    to the builder (the proposal body, or a gripe's timeline).
    """

    kind: str  # "proposal" | "gripe"
    slug: str  # proposal slug (file stem) or gripe id as str
    title: str
    branch: str
    spec_text: str
    source_path: Path | None = None
    model: str | None = None  # front-matter "model:" tier (sonnet/opus/haiku)
    blocked_by: str | None = None  # front-matter "blocked-by:" predecessor slug
    prio: str = "normal"  # front-matter "prio:" bucket — high | normal | low


def parse_front_matter(text: str) -> dict[str, str]:
    """Parse a leading ``---`` front-matter block into a flat dict.

    Deliberately minimal — flat ``key: value`` lines only, values
    lower-cased-key'd but value-preserved, ``#`` comments and blank
    lines skipped. Consumers read ``status`` / ``title`` / ``model`` /
    ``blocked-by``; anything richer belongs in a real YAML load, which
    the pick path does not warrant. Returns ``{}`` when there is no
    front-matter block.
    """
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for raw in m.group(1).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key:
            out[key] = value.strip().strip("'\"")
    return out


def _slugify(stem: str) -> str:
    """Normalise a file stem into a branch-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return slug or "proposal"


def _title_from_body(text: str, fallback: str) -> str:
    """First ``# heading`` after the front-matter, else the fallback."""
    body = _FRONT_MATTER_RE.sub("", text, count=1)
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip() or fallback
    return fallback


#: Priority bucket → sort rank (high first); unset/unknown falls to normal.
_PRIO_ORDER = {"high": 0, "normal": 1, "low": 2}


def ready_items(backlog_dir: Path) -> list[WorkItem]:
    """All ``status: ready`` proposals, highest ``prio:`` first.

    Ordered by priority bucket (``high`` → ``normal`` → ``low``), then by
    filename within a bucket — the fixer picks the most important ready item
    first, not the alphabetically first. A missing directory yields ``[]``
    (the MVP may run before any proposal exists). ``TEMPLATE.md`` /
    ``README.md`` are skipped.
    """
    if not backlog_dir.is_dir():
        return []
    items: list[WorkItem] = []
    for path in sorted(backlog_dir.glob("*.md")):
        if path.stem.lower() in _NON_PROPOSAL_STEMS:
            continue
        text = path.read_text(encoding="utf-8")
        fm = parse_front_matter(text)
        if fm.get("status", "").lower() != "ready":
            continue
        slug = _slugify(path.stem)
        title = fm.get("title") or _title_from_body(text, slug)
        prio = (fm.get("prio") or "normal").lower()
        items.append(
            WorkItem(
                kind="proposal",
                slug=slug,
                title=title,
                branch=f"fix/{slug}",
                spec_text=text,
                source_path=path,
                model=fm.get("model") or None,
                blocked_by=fm.get("blocked-by") or None,
                prio=prio if prio in _PRIO_ORDER else "normal",
            )
        )
    # Stable sort keeps filename order within a priority bucket.
    items.sort(key=lambda it: _PRIO_ORDER[it.prio])
    return items


def pick_next(
    items: Iterable[WorkItem],
    branch_exists: Callable[[str], bool],
) -> WorkItem | None:
    """First item whose branch does not already exist (idempotent).

    The re-firing loop must not re-pick something it already branched;
    ``branch_exists`` encapsulates the git check (local/worktree/remote)
    so this stays pure and testable. An item with ``blocked_by`` set is
    also skipped while its predecessor's branch (``fix/<blocked_by>``)
    still exists — the check is against ``branch_exists`` alone, not
    against the predecessor still being present in ``items`` (it may
    have already shipped and dropped out of ``ready_items``).
    """
    for item in items:
        if branch_exists(item.branch):
            continue
        if item.blocked_by and branch_exists(f"fix/{item.blocked_by}"):
            continue
        return item
    return None


# ── gripe intake (behind PRECIS_FIXER_GRIPE_DB) ─────────────────────

#: Chunk-kind slugs a gripe timeline carries. Mirrors the literals in
#: ``handlers/gripe.py`` — duplicated rather than imported so a module
#: this trivial doesn't drag the handler layer (and its store-package
#: import weight) into every fixer tick, dial on or off.
_GRIPE_BODY_KIND = "gripe_body"
_GRIPE_COMMENT_KIND = "gripe_comment"

#: A diagnosis comment's text prefix (the sibling diagnose-executor
#: slice's convention — it appends ``"DIAGNOSIS (auto …"``). Matched by
#: prefix only, not the parenthetical, so a human-authored comment that
#: simply starts the word also counts as a diagnosis.
_DIAGNOSIS_PREFIX = "DIAGNOSIS"


class _TimelineEntry(NamedTuple):
    """One chunk of a gripe's timeline — just enough to render + filter.

    Deliberately not :class:`precis.store.types.Block` — keeping this
    local means the pure rendering/filtering helpers below take no
    dependency on the store package and stay unit-testable with plain
    tuples, no DB or fakes required.
    """

    chunk_kind: str
    pos: int
    text: str


def _gripe_prio_bucket(prio: int | None) -> str:
    """Map a gripe's ``refs.prio`` (1..10, NULL=default) to a bucket.

    1-3 → high, 4-6 → normal, everything else (7-10, and unset) → low.
    Mirrors :data:`_PRIO_ORDER`'s bucket names so a gripe sorts on the
    same axis as a proposal's front-matter ``prio:``.
    """
    if prio is not None and 1 <= prio <= 3:
        return "high"
    if prio is not None and 4 <= prio <= 6:
        return "normal"
    return "low"


def _is_diagnosed(entries: Iterable[_TimelineEntry]) -> bool:
    """True if the timeline carries at least one diagnosis comment.

    The second half of the promotion criterion (the first half — open +
    ``auto-fix`` tag — is a DB-side filter in :func:`gripe_items`, cheaper
    to push into the query than to re-check here).
    """
    return any(
        e.chunk_kind == _GRIPE_COMMENT_KIND and e.text.startswith(_DIAGNOSIS_PREFIX)
        for e in entries
    )


def _render_gripe_spec(title: str, entries: Iterable[_TimelineEntry]) -> str:
    """Title + timeline (body, then comments in pos order) as one spec.

    Fed to the builder as ``WorkItem.spec_text`` — the diagnosis comment
    is just another entry in pos order, so it flows in verbatim without
    special-casing.
    """
    lines = [f"# {title}", ""]
    for e in entries:
        if e.chunk_kind == _GRIPE_BODY_KIND:
            lines.append(e.text)
        elif e.chunk_kind == _GRIPE_COMMENT_KIND:
            lines.append("")
            lines.append(f"## comment {e.pos}")
            lines.append(e.text)
        else:
            lines.append("")
            lines.append(e.text)
    return "\n".join(lines)


def _work_item_from_gripe(
    ref_id: int, title: str, prio: int | None, entries: list[_TimelineEntry]
) -> WorkItem | None:
    """Normalize one promoted gripe into a :class:`WorkItem`, or ``None``.

    ``None`` when the timeline has no diagnosis entry yet — the tag alone
    (``auto-fix``, human- or diagnose-executor-applied) isn't sufficient;
    a pinned diagnosis is what makes the item buildable unattended.
    """
    if not _is_diagnosed(entries):
        return None
    return WorkItem(
        kind="gripe",
        slug=f"gr{ref_id}",
        title=title,
        branch=f"fix/gr{ref_id}",
        spec_text=_render_gripe_spec(title, entries),
        model=None,
        prio=_gripe_prio_bucket(prio),
    )


def gripe_items(db_url: str) -> list[WorkItem]:
    """Promoted open gripes (open + ``auto-fix`` + diagnosed), as ``WorkItem``s.

    Read-only: two SELECTs per candidate (the tagged-refs list, then each
    ref's timeline) against ``db_url`` via a throwaway small pool — this
    runs once per fixer tick, not a long-lived server, so a 1-2
    connection pool is plenty and kinder to pgbouncer than the store's
    server-sized default. Any failure (unreachable DB, query error,
    schema surprise) is caught and logged; the caller degrades to
    proposals-only rather than crashing the tick.
    """
    try:
        from precis.store.store import Store
    except Exception:
        log.exception("gripe intake: could not import the store layer")
        return []

    try:
        store = Store.connect(db_url, min_size=1, max_size=2)
    except Exception as exc:
        log.warning("gripe intake: DB unreachable (%s) — proposals-only", exc)
        return []

    try:
        refs = store.list_refs(
            kind="gripe", tags=["STATUS:open", "auto-fix"], limit=200
        )
        items: list[WorkItem] = []
        for ref in refs:
            blocks = store.list_blocks_for_ref(ref.id)
            entries = [_TimelineEntry(b.chunk_kind, b.pos, b.text) for b in blocks]
            item = _work_item_from_gripe(ref.id, ref.title, ref.prio, entries)
            if item is not None:
                items.append(item)
        return items
    except Exception as exc:
        log.warning("gripe intake: query failed (%s) — proposals-only", exc)
        return []
    finally:
        store.close()


def all_items(backlog_dir: Path, gripe_db_url: str | None) -> list[WorkItem]:
    """The one normalized, priority-ordered queue: proposals, then gripes.

    ``gripe_db_url`` unset (the plist default) means :func:`gripe_items`
    is never called — no import, no connection attempt, and the result
    is exactly :func:`ready_items`'s list, so landing this lane changes
    nothing until the dial is turned on. When set, gripes are appended
    after proposals and the combined list is re-sorted by
    :data:`_PRIO_ORDER`; Python's stable sort means same-bucket order is
    preserved, so a high-prio gripe outranks a normal proposal, but at
    equal prio a proposal always precedes a gripe (list order before the
    sort) and each source keeps its own internal ordering (filename /
    ref id).
    """
    proposals = ready_items(backlog_dir)
    gripes = gripe_items(gripe_db_url) if gripe_db_url else []
    merged = proposals + gripes
    merged.sort(key=lambda it: _PRIO_ORDER[it.prio])
    return merged

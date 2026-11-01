"""``precis memory import | index`` — harness memory as ``SPACE:repo-dev`` nodes.

The write and load halves of ``docs/backlog/memory-native-authoring.md``:

- ``precis memory import <dir>`` seeds the graph from a harness memory
  directory: ``<dir>/MEMORY.md`` plus the topic files its bullets name.
  Each ``## Section`` header becomes a *section node*, each
  ``- [Title](file.md) — hook`` bullet a *topic node*; ``[X](other.md)`` /
  ``[[other]]`` references in a topic body become ``related-to`` links.
  Idempotent on ``meta.slug`` (topic) / ``meta.section`` (section); an
  existing node is never overwritten — graph-side edits win. ``--sync``
  is the one-shot exception for the cutover: it re-converges the graph on
  the current files (updates, creates, retires), leaving nodes that carry
  neither key (native writes) alone. ``--dry-run`` plans a run and writes
  nothing; a sync that would retire more than ``max(5, 10%)`` of the
  imported nodes is refused unless ``--allow-retire N`` covers it; a
  ``MEMORY.md`` carrying :data:`GRAPH_MARKER` (post-cutover pointer file)
  is always refused.
- ``precis memory index [--budget-tok N] [--days D] [--export-dir DIR]``
  renders, for ``scripts/hooks/session-start-memory.sh``, only the *live
  threads* (``section:threads`` nodes edited within ``D`` days, default 14,
  newest first, one ``- <handle> <title> — <Left: line>`` each, plus a count of
  the older ones); recall of everything else is ``search(kind='memory')``.
  ``--full`` keeps the old whole-graph listing, one ``- <Title> (<handle>) —
  <hook>`` bullet per node. ``--export-dir`` also writes each topic node's body
  to ``DIR/<handle>.md`` (same query, swapped in whole) so
  ``scripts/memory-lint`` can lint node bodies.
- ``precis memory backfill-space [--dry-run] [--limit N]`` infers ``SPACE:``
  on live memories that carry none (gr548275): present tags, then the
  writer signal on the refs row (``meta.file_mirror`` / ``slug`` /
  ``section`` / ``source`` / ``hook``, digest meta keys, ``set_by``),
  ``ref_events`` sources as fallback, then link-endpoint kinds. The
  residue keeps no SPACE tag but carries ``space:unknown`` so it stays
  countable. ``--dry-run`` reports per-space and per-rule counts with
  sample handles and writes nothing.
- ``precis memory migrate-rulings [--apply]`` (gr548835 slice 1): one-off
  move of the backfilled ``ruling`` nodes' trailing ``Meta:`` body line into
  real meta; dry run by default, operator-run only (:func:`migrate_rulings`).

The logic lives in :func:`import_memory_dir` and :func:`render_memory_index`
(both take a :class:`~precis.store.Store`) so tests call them directly; the
argparse layer is a thin shell. The legacy import only reads its source.
Explicit ``mirror import`` / ``mirror export`` in :mod:`precis.cli.memory_mirror`
preserve filenames and YAML for coexistence; export requires a fresh directory,
while conflict baselines prevent implicit file-over-graph updates. Neither
operation is a cutover.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from precis.cli import memory_recall_set
from precis.cli._common import resolve_dsn
from precis.store import Store
from precis.store.types import Tag
from precis.utils.text import slugify

log = logging.getLogger(__name__)

#: Every imported and index-rendered node carries this space tag.
SPACE_TAG = "SPACE:repo-dev"
#: Tag on a section node (the ``## Section`` header of the index).
SECTION_INDEX_TAG = "section:index"
#: Prefix of the open tag naming which section a topic node sits under.
SECTION_TAG_PREFIX = "section:"
#: Hooks are cut to this many characters when the index is over budget.
HOOK_CUT_CHARS = 60
#: Title line the rendered index leads with (matches ``MEMORY.md``).
INDEX_TITLE = "# Memory index"
#: Tag naming the live-thread section the session-start render shows.
THREADS_TAG = "section:threads"
#: A thread node untouched this many days is a retire candidate, not "live".
LIVE_THREAD_DAYS = 14
#: Cut of a live-thread line's lead text.
THREAD_LEAD_CHARS = 160
#: Rough bytes-per-token used for the budget check (memory-lint's ratio).
_BYTES_PER_TOKEN = 4

#: First-line marker of the post-cutover pointer ``MEMORY.md``: the graph is the
#: truth, the file only points at it, so importing from it is always wrong.
GRAPH_MARKER = "<!-- memory-index: graph -->"
#: A sync may retire up to this many nodes (or :data:`RETIRE_CAP_FRACTION` of
#: the existing imported nodes, whichever is more) without ``--allow-retire``.
RETIRE_CAP_FLOOR = 5
RETIRE_CAP_FRACTION = 0.10
#: Slugs named in a refusal message / dry-run summary.
_SLUG_PREVIEW = 10

_BULLET_RE = re.compile(
    r"^- \[(?P<title>[^\]]+)\]\((?P<file>[^)\s]+)\)(?: — (?P<hook>.*))?$"
)
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s#]+\.md)(?:#[^)]*)?\)")
_WIKI_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")


class ImportRefused(ValueError):
    """The import was refused before any write (marker file, retire cap)."""


@dataclass
class ImportReport:
    """What one :func:`import_memory_dir` run did."""

    sections_created: int = 0
    sections_existing: int = 0
    topics_created: int = 0
    topics_existing: int = 0
    #: Topic slugs whose file was missing (body fell back to the bullet text).
    missing_files: list[str] = field(default_factory=list)
    #: ``related-to`` edges ensured (``add_link`` is idempotent, so this
    #: counts attempts on resolvable targets, not new rows).
    links_ensured: int = 0
    #: ``(source slug, target slug)`` references that matched no node.
    unresolved_links: list[tuple[str, str]] = field(default_factory=list)
    #: ``--sync`` only: existing nodes changed to match the files (counted once
    #: per node however many of title/body/hook/order/section moved).
    updated: int = 0
    #: ``--sync`` only: nodes whose bullet / header is gone, soft-deleted.
    retired: int = 0
    #: ``--dry-run``: the plan was computed, nothing was written.
    dry_run: bool = False
    #: ``--sync`` only: slugs (``section:<slug>`` for sections) of the nodes
    #: that were / would be retired.
    would_retire: list[str] = field(default_factory=list)

    def summary(self) -> str:
        prefix = "DRY RUN (nothing written): " if self.dry_run else ""
        text = self._counts()
        if self.dry_run and self.would_retire:
            shown = ", ".join(self.would_retire[:_SLUG_PREVIEW])
            more = len(self.would_retire) - _SLUG_PREVIEW
            text += f"; would retire: {shown}" + (
                f" (+{more} more)" if more > 0 else ""
            )
        return prefix + text

    def _counts(self) -> str:
        return (
            f"sections: {self.sections_created} created, "
            f"{self.sections_existing} existing; "
            f"topics: {self.topics_created} created, "
            f"{self.topics_existing} existing; "
            f"updated: {self.updated}; retired: {self.retired}; "
            f"links ensured: {self.links_ensured}; "
            f"missing files: {len(self.missing_files)}; "
            f"unresolved links: {len(self.unresolved_links)}"
        )


@dataclass
class _Bullet:
    title: str
    slug: str
    hook: str
    text: str  # the bullet minus its leading "- "
    order: int  # 1-based position inside the section


@dataclass
class _Section:
    title: str
    slug: str
    order: int  # 1-based position among the sections
    bullets: list[_Bullet] = field(default_factory=list)


def parse_index(text: str) -> list[_Section]:
    """Parse ``MEMORY.md`` into sections of bullets (headers present, in order).

    Bullets before the first ``##`` header and lines matching neither shape
    are ignored.
    """
    sections: list[_Section] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            title = line[3:].strip()
            sections.append(_Section(title, slugify(title), len(sections) + 1))
            continue
        m = _BULLET_RE.match(line)
        if m is None or not sections:
            continue
        sec = sections[-1]
        slug = Path(m["file"]).stem
        sec.bullets.append(
            _Bullet(
                title=m["title"].strip(),
                slug=slug,
                hook=(m["hook"] or "").strip(),
                text=line[2:],
                order=len(sec.bullets) + 1,
            )
        )
    return sections


def strip_frontmatter(text: str) -> str:
    """The file body without a leading ``---`` … ``---`` YAML block."""
    if not text.startswith("---"):
        return text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1 :])
    return text


def _link_targets(body: str) -> list[str]:
    """Slugs (file stems) referenced by ``[X](other.md)`` and ``[[other]]``."""
    out: list[str] = []
    for raw in (*_MD_LINK_RE.findall(body), *_WIKI_LINK_RE.findall(body)):
        slug = Path(raw.strip()).stem
        if slug and slug not in out:
            out.append(slug)
    return out


def _created_id(resp: Any) -> int:
    """Ref id of a memory ``put``: ``Response.ref_id``, else the ack's handle."""
    from precis.utils import handle_registry

    if resp.ref_id is not None:
        return int(resp.ref_id)
    head = resp.body.split("\n", 1)[0]
    for tok in head.replace(",", " ").replace(".", " ").split():
        parsed = handle_registry.parse(tok)
        if parsed is not None and not parsed[1]:
            return parsed[2]
    raise RuntimeError(f"cannot read the new memory id from ack: {head!r}")


def _live_repo_dev_nodes(store: Store) -> list[Any]:
    return store.list_refs(
        kind="memory", tags=[SPACE_TAG], order_by="id_asc", limit=1_000_000
    )


def _retire_key(meta: dict[str, Any]) -> str | None:
    """Label of an imported node (slug, or ``section:<slug>``); ``None`` = native."""
    if meta.get("slug"):
        return str(meta["slug"])
    if meta.get("section"):
        return f"section:{meta['section']}"
    return None


def import_memory_dir(
    store: Store,
    path: Path | str,
    *,
    sync: bool = False,
    dry_run: bool = False,
    allow_retire: int | None = None,
) -> ImportReport:
    """Seed ``SPACE:repo-dev`` memory nodes from a harness memory directory.

    Reads ``<path>/MEMORY.md`` and the topic files its bullets name. Safe to
    re-run: a node whose ``meta.slug`` / ``meta.section`` already exists
    among live ``SPACE:repo-dev`` memories is left untouched, links are
    re-added idempotently. Raises ``FileNotFoundError`` when ``MEMORY.md``
    is absent.

    ``sync=True`` re-converges the graph on the files instead (cutover, run
    once): an existing node's title, body (via the handler's edit-replace, so
    the chunk / embedding / mentions re-derive), ``meta.hook``,
    ``meta.order`` and ``section:`` tag (plus its ``part-of`` link) are
    updated where they differ; nodes whose bullet / header is gone are
    retired through the handler's soft delete; missing ones are created as
    usual. Nodes carrying neither ``meta.slug`` nor ``meta.section`` (native
    writes) are never touched.

    ``dry_run=True`` runs the same planning path with every store write
    skipped and returns the counts a real run would (``report.dry_run``,
    ``report.would_retire``). Raises :class:`ImportRefused`, before any
    write, when ``MEMORY.md`` carries :data:`GRAPH_MARKER`, or when a sync
    would retire more than ``max(5, 10%)`` of the existing imported nodes
    and ``allow_retire`` is below the planned count (a dry run only reports).
    """
    from precis.dispatch import Hub
    from precis.handlers.memory import MemoryHandler

    root = Path(path)
    index_text = (root / "MEMORY.md").read_text(encoding="utf-8")
    if GRAPH_MARKER in index_text:
        raise ImportRefused(
            f"{root / 'MEMORY.md'} carries the marker {GRAPH_MARKER}: the graph "
            "is the truth and this file is only a pointer to it, so importing "
            "(or syncing) from it is always wrong; nothing was written."
        )
    sections = parse_index(index_text)
    handler = MemoryHandler(hub=Hub(store=store))
    report = ImportReport(dry_run=dry_run)
    write = not dry_run

    live = _live_repo_dev_nodes(store)
    by_id = {r.id: r for r in live}

    if sync:
        index_slugs = {b.slug for s in sections for b in s.bullets}
        index_sections = {s.slug for s in sections}
        imported = 0
        for ref in live:
            meta = ref.meta or {}
            label = _retire_key(meta)
            if label is None:
                continue  # a native write — never touched
            imported += 1
            if meta.get("slug"):
                gone = str(meta["slug"]) not in index_slugs
            else:
                gone = str(meta["section"]) not in index_sections
            if gone:
                report.would_retire.append(label)
        cap = max(RETIRE_CAP_FLOOR, int(imported * RETIRE_CAP_FRACTION))
        n_retire = len(report.would_retire)
        if (
            write
            and n_retire > cap
            and (allow_retire is None or allow_retire < n_retire)
        ):
            first = ", ".join(report.would_retire[:_SLUG_PREVIEW])
            more = n_retire - _SLUG_PREVIEW
            raise ImportRefused(
                f"sync would retire {n_retire} of {imported} imported nodes, over "
                f"the cap of {cap} (max({RETIRE_CAP_FLOOR}, "
                f"{RETIRE_CAP_FRACTION:.0%} of existing)); first slugs: {first}"
                + (f" (+{more} more)" if more > 0 else "")
                + ". Nothing was written. Run with --dry-run first; if the list "
                f"is right, re-run with --allow-retire {n_retire}."
            )
    tag_values: dict[int, set[str]] = {}
    if sync:
        tag_values = {
            rid: {v for _ns, v in pairs}
            for rid, pairs in store.ref_tags_bulk(list(by_id)).items()
        }
    section_ids: dict[str, int] = {}
    topic_ids: dict[str, int] = {}
    # ``put`` and the meta patch are two writes; a run killed between them
    # leaves a node with no ``meta.slug``/``meta.section``. Such orphans are
    # matched by title on the next run and patched instead of duplicated.
    orphans: dict[str, int] = {}
    for ref in live:
        meta = ref.meta or {}
        if meta.get("section") and not meta.get("slug"):
            section_ids.setdefault(str(meta["section"]), ref.id)
        elif meta.get("slug"):
            topic_ids.setdefault(str(meta["slug"]), ref.id)
        elif "order" not in meta:
            orphans.setdefault(ref.title or "", ref.id)

    fake_ids = iter(range(-1, -1_000_000_000, -1))

    def _create(title: str, body: str, tags: list[str], meta: dict[str, Any]) -> int:
        orphan = orphans.pop(title, None)
        if orphan is not None:
            if write:
                store.update_ref(orphan, meta_patch=meta)
                store.chunks.sync_header_card(orphan)
            return orphan
        if not write:
            return next(fake_ids)  # a dry run's stand-in id; never stored
        resp = handler.put(text=body, title=title, tags=tags)
        ref_id = _created_id(resp)
        store.update_ref(ref_id, meta_patch=meta)
        store.chunks.sync_header_card(ref_id)  # put() ran before meta.hook landed
        return ref_id

    # Section node ids as they stood before this run — the only ``part-of``
    # targets a sync may re-point away from.
    old_section_ids = set(section_ids.values())

    def _sync_section(sec_id: int, sec: _Section) -> None:
        ref = by_id.get(sec_id)
        if ref is None:
            return
        changed = False
        if ref.title != sec.title:
            if write:
                store.chunks.set_ref_title(sec_id, sec.title, source="agent")
                store.chunks.sync_header_card(sec_id)
            changed = True
        if (ref.meta or {}).get("order") != sec.order:
            if write:
                store.update_ref(sec_id, meta_patch={"order": sec.order})
            changed = True
        report.updated += changed

    def _sync_topic(
        topic_id: int, sec: _Section, sec_id: int, b: _Bullet, body: str
    ) -> None:
        ref = by_id.get(topic_id)
        if ref is None:
            return
        changed = False
        meta = ref.meta or {}
        if ref.title != b.title:
            if write:
                store.chunks.set_ref_title(topic_id, b.title, source="agent")
                store.chunks.sync_header_card(topic_id)
            changed = True
        if handler._body_text(ref) != body:
            if write:
                handler.edit(id=topic_id, text=body)
            changed = True
        patch: dict[str, Any] = {}
        if meta.get("hook") != b.hook:
            patch["hook"] = b.hook
        if meta.get("order") != b.order:
            patch["order"] = b.order
        if patch:
            if write:
                store.update_ref(topic_id, meta_patch=patch)
                store.chunks.sync_header_card(topic_id)
            changed = True
        want = f"{SECTION_TAG_PREFIX}{sec.slug}"
        have = sorted(
            v
            for v in tag_values.get(topic_id, set())
            if v.startswith(SECTION_TAG_PREFIX) and v != SECTION_INDEX_TAG
        )
        if have != [want]:
            if write:
                handler.tag(
                    id=topic_id,
                    add=[want] if want not in have else None,
                    remove=[v for v in have if v != want] or None,
                )
            changed = True
        for link in store.links_for(topic_id, direction="out", relation="part-of"):
            if link.dst_ref_id != sec_id and link.dst_ref_id in old_section_ids:
                if write:
                    store.remove_link(
                        src_ref_id=topic_id,
                        dst_ref_id=link.dst_ref_id,
                        relation="part-of",
                    )
                changed = True
        report.updated += changed

    topic_bodies: dict[str, str] = {}
    for sec in sections:
        sec_id = section_ids.get(sec.slug)
        if sec_id is None:
            sec_id = _create(
                sec.title,
                f"Memory index section: {sec.title}",
                [SPACE_TAG, SECTION_INDEX_TAG],
                {"section": sec.slug, "order": sec.order},
            )
            section_ids[sec.slug] = sec_id
            report.sections_created += 1
        else:
            report.sections_existing += 1
            if sync:
                _sync_section(sec_id, sec)

        for b in sec.bullets:
            file_body: str | None = None
            topic_file = root / f"{b.slug}.md"
            if topic_file.is_file():
                file_body = strip_frontmatter(
                    topic_file.read_text(encoding="utf-8")
                ).strip()
            if not file_body:
                report.missing_files.append(b.slug)
                body = b.text
            else:
                body = file_body
                topic_bodies[b.slug] = file_body
            topic_id = topic_ids.get(b.slug)
            if topic_id is None:
                topic_id = _create(
                    b.title,
                    body,
                    [SPACE_TAG, f"{SECTION_TAG_PREFIX}{sec.slug}"],
                    {"slug": b.slug, "hook": b.hook, "order": b.order},
                )
                topic_ids[b.slug] = topic_id
                report.topics_created += 1
            else:
                report.topics_existing += 1
                if sync:
                    _sync_topic(topic_id, sec, sec_id, b, body)
            if write:
                store.add_link(
                    src_ref_id=topic_id, dst_ref_id=sec_id, relation="part-of"
                )

    if sync:
        retiring = set(report.would_retire)
        for ref in live:
            meta = ref.meta or {}
            label = _retire_key(meta)
            if label is None or label not in retiring:
                continue
            if write:
                handler.delete(id=ref.id)
            report.retired += 1
            if meta.get("slug"):
                topic_ids.pop(str(meta["slug"]), None)

    # Cross-links last: a topic may cite one that comes later in the index.
    for src_slug, body in topic_bodies.items():
        for dst_slug in _link_targets(body):
            if dst_slug == src_slug:
                continue
            dst_id = topic_ids.get(dst_slug)
            if dst_id is None:
                report.unresolved_links.append((src_slug, dst_slug))
                continue
            if write:
                store.add_link(
                    src_ref_id=topic_ids[src_slug],
                    dst_ref_id=dst_id,
                    relation="related-to",
                )
            report.links_ensured += 1
    return report


def _order_key(ref: Any) -> tuple[int, int, int]:
    """Ordered nodes by ``meta.order``; unordered after them, oldest first."""
    order = (ref.meta or {}).get("order")
    if isinstance(order, int) and not isinstance(order, bool):
        return (0, order, ref.id)
    return (1, 0, ref.id)


def bullet_line(
    ref: Any,
    *,
    cut_hooks: bool,
    filename: bool = False,
    fallback_hook: str = "",
    size: str = "",
) -> str:
    """One index bullet: ``- <Title> (<handle>[, <filename>]) — <hook>``.

    The session-start index uses the default (no filename); the recall
    renders (``--q`` and ``search(view='index')``) pass ``filename=True`` so a
    hit matches the topic file the harness may also have recalled, and
    ``fallback_hook`` (the body's first line) for a node with no ``meta.hook``.
    ``size`` (a ``size_cell`` label) appends `` · <n>kB`` when the body
    is big enough to matter — its caller already holds the text.
    """
    from precis.utils import handle_registry

    meta = ref.meta or {}
    handle = handle_registry.try_format("memory", ref.id) or str(ref.id)
    if filename:
        fname = (meta.get("file_mirror") or {}).get("filename")
        if fname:
            handle = f"{handle}, {fname}"
    hook = str(meta.get("hook") or fallback_hook)
    if cut_hooks and len(hook) > HOOK_CUT_CHARS:
        hook = hook[: HOOK_CUT_CHARS - 1].rstrip() + "…"
    line = f"- {ref.title} ({handle})"
    if hook:
        line = f"{line} — {hook}"
    return f"{line} · {size}" if size else line


def _bullet_line(
    ref: Any, *, cut_hooks: bool, prefix: str = "", indent: int = 0
) -> str:
    line = bullet_line(ref, cut_hooks=cut_hooks)
    if prefix:
        line = f"- {prefix} {line[2:]}"
    return "  " * indent + line


#: One rendered index line: the node, its ``[type]`` prefix, nesting depth.
_Entry = tuple[Any, str, int]


def _render(sections: list[tuple[str | None, list[_Entry]]], *, cut_hooks: bool) -> str:
    out = [INDEX_TITLE]
    for title, entries in sections:
        out.append("")
        if title is not None:
            out.append(f"## {title}")
            out.append("")
        out.extend(
            _bullet_line(n, cut_hooks=cut_hooks, prefix=pre, indent=ind)
            for n, pre, ind in entries
        )
    return "\n".join(out) + "\n"


def render_memory_index(
    store: Store,
    budget_tok: int | None = None,
    *,
    q: str | None = None,
    k: int = 5,
    embedder: Any = None,
    full: bool = False,
    days: int = LIVE_THREAD_DAYS,
    now: datetime | None = None,
) -> str:
    """Render the session-start memory view: the live threads (default).

    Without ``full``/``q`` the render is :func:`_render_live_threads`: only
    ``section:threads`` nodes edited within ``days`` days. ``full=True`` is the
    whole-graph index below.

    The full index is one bullet per ``SPACE:repo-dev`` node.

    Every node, imported or native, renders as ``- <Title> (<handle>) —
    <hook>`` (``- <Title> (<handle>)`` with no hook); the handle (``me…``) is
    what ``get``/``edit`` take. Sections by ``meta.order``, then each section's topic nodes by
    ``meta.order`` (unordered native writes after, oldest first). Topic nodes
    are grouped by their ``section:<slug>`` tag; ones naming no known section
    render under a trailing ``## Unfiled``. When ``budget_tok`` is given and the
    full render exceeds it (~4 bytes/token), hooks are cut to
    :data:`HOOK_CUT_CHARS` characters and one trailing line names the
    overage — a tripwire, not a hard limit.

    With ``q`` the render is instead the ``k`` best hits for ``q`` among
    ``SPACE:repo-dev`` memories, one bullet each with the mirror filename
    beside the handle — exactly :meth:`MemoryHandler.search`'s
    ``view='index'``, run through the handler so the hybrid search uses the
    ``embedder`` the caller wires (``None`` = lexical only).
    """
    if q is not None:
        from precis.dispatch import Hub
        from precis.handlers.memory import MemoryHandler

        handler = MemoryHandler(hub=Hub(store=store, embedder=embedder))
        resp = handler.search(q=q, tags=[SPACE_TAG], page_size=k, view="index")
        return resp.body + "\n"
    loaded = _load_nodes(store)
    if full:
        return _render_loaded(loaded, budget_tok, store)
    return _render_live_threads(store, loaded, budget_tok, days=days, now=now)


def score_recall(
    store: Store, entries: list[dict[str, Any]], *, k: int = 5, embedder: Any = None
) -> list[int | None]:
    """Rank of each entry's ``want`` handle in the ``--q`` render of its
    ``query`` (1-based; ``None`` when it is not in the top ``k``).

    The question-set scorer for ``memory_recall_set.json``: the
    same :func:`render_memory_index` call a session's recall makes.
    """
    ranks: list[int | None] = []
    for entry in entries:
        lines = render_memory_index(
            store, q=entry["query"], k=k, embedder=embedder
        ).splitlines()
        handle = f"({entry['want']}"
        ranks.append(
            next(
                (
                    i
                    for i, ln in enumerate(lines, 1)
                    if handle + ")" in ln or handle + "," in ln
                ),
                None,
            )
        )
    return ranks


def _mirrored_handles(store: Store, handles: list[str]) -> set[str]:
    """The ``me…`` handles among ``handles`` whose node carries a
    ``file_mirror`` stamp (imported from a topic file, not written native)."""
    ids = [int(h[2:]) for h in handles if h[2:].isdigit()]
    with store.tx() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE ref_id = ANY(%s) AND meta ? 'file_mirror'",
            (ids,),
        ).fetchall()
    return {f"me{rid}" for (rid,) in rows}


def recall_report_rows(
    store: Store, entries: list[dict[str, Any]], *, k: int = 5, embedder: Any = None
) -> list[tuple[dict[str, Any], int | None, str]]:
    """``(entry, rank, origin)`` per question — the data the ``--set`` report
    prints and the weekly ``memory_recall`` cadence records. ``origin`` is
    ``"mirror"`` when the answer node carries a ``file_mirror`` stamp,
    ``"native"`` otherwise."""
    ranks = score_recall(store, entries, k=k, embedder=embedder)
    mirrored = _mirrored_handles(store, [e["want"] for e in entries])
    return [
        (entry, rank, "mirror" if entry["want"] in mirrored else "native")
        for entry, rank in zip(entries, ranks, strict=True)
    ]


def render_recall_report(
    store: Store, entries: list[dict[str, Any]], *, k: int = 5, embedder: Any = None
) -> str:
    """One ``HIT <rank>``/``MISS -`` line per entry (with the answer node's
    origin, native or mirror), then a ``RECALL`` total split by origin."""
    rows = recall_report_rows(store, entries, k=k, embedder=embedder)
    out: list[str] = []
    tally = {"native": [0, 0], "mirror": [0, 0]}
    for entry, rank, origin in rows:
        tally[origin][1] += 1
        tally[origin][0] += rank is not None
        verdict = f"HIT  {rank}" if rank is not None else "MISS -"
        out.append(f"{verdict} {entry['want']} {origin:6} {entry['query']}")
    hits = sum(r is not None for _e, r, _o in rows)
    out.append(
        f"RECALL {hits}/{len(rows)} at k={k} "
        f"(native {tally['native'][0]}/{tally['native'][1]}, "
        f"mirror {tally['mirror'][0]}/{tally['mirror'][1]})"
    )
    return "\n".join(out) + "\n"


#: Packaged P4 question set (``precis/data/memory_recall_set.json``) —
#: in ``precis.data`` so a deployed worker (an installed package, no
#: ``tests/`` tree) reads the same set ``precis memory index --set``
#: scores by hand and the ``memory_recall`` cadence scores weekly.
RECALL_SET_RESOURCE = "memory_recall_set.json"


def load_recall_set(path: str | Path | None = None) -> list[dict[str, Any]]:
    """A recall question set as a list of ``{query, want, ...}`` dicts —
    the packaged P4 set when ``path`` is ``None`` (bare ``--set``, and the
    ``memory_recall`` cadence), else the JSON file at ``path``."""
    import json
    from importlib import resources

    if path is None:
        raw = (
            resources.files("precis.data")
            .joinpath(RECALL_SET_RESOURCE)
            .read_text(encoding="utf-8")
        )
    else:
        raw = Path(path).read_text(encoding="utf-8")
    return json.loads(raw)


def _thread_lead(handler: Any, ref: Any) -> str:
    """A thread node's one-line state: the ``Left:`` first line, else
    ``meta.hook``, else the first non-empty body line; cut to
    :data:`THREAD_LEAD_CHARS`."""
    first = next(
        (ln.strip() for ln in handler._body_text(ref).splitlines() if ln.strip()), ""
    )
    lead = (
        first
        if first.startswith("Left:")
        else str((ref.meta or {}).get("hook") or first)
    )
    if len(lead) > THREAD_LEAD_CHARS:
        lead = lead[: THREAD_LEAD_CHARS - 1].rstrip() + "…"
    return lead


def _live_thread_lines(
    store: Store,
    loaded: tuple[list[Any], dict[int, Any]],
    *,
    days: int,
    now: datetime | None,
) -> tuple[list[str], int]:
    """(bullet lines for live threads, total top-level thread count).

    Only top-level threads: ones with no ``part-of`` parent or a hub parent.
    ``refs.updated_at`` is the edit clock (body, title and meta writes all
    bump it). Newest first.
    """
    from precis.dispatch import Hub
    from precis.handlers.memory import MemoryHandler
    from precis.utils import handle_registry
    from precis.utils.memory_hubs import parents_of

    refs, tags = loaded
    parent_of = parents_of(store, [r.id for r in refs])

    def top_level(rid: int) -> bool:
        # A thread under another thread (not a hub) is a child, listed there.
        return not (parent_of.get(rid) or []) or any(
            SECTION_INDEX_TAG in {v for _ns, v in tags.get(p, [])}
            for p in parent_of[rid]
        )

    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    threads = [
        r
        for r in refs
        if THREADS_TAG in {v for _ns, v in tags.get(r.id, [])}
        and SECTION_INDEX_TAG not in {v for _ns, v in tags.get(r.id, [])}
        and top_level(r.id)
    ]
    threads.sort(key=lambda r: (r.updated_at, r.id), reverse=True)
    handler = MemoryHandler(hub=Hub(store=store))
    lines = []
    for r in threads:
        if r.updated_at < cutoff:
            continue
        handle = handle_registry.try_format("memory", r.id) or str(r.id)
        lead = _thread_lead(handler, r)
        lines.append(f"- {handle} {r.title}" + (f" — {lead}" if lead else ""))
    return lines, len(threads)


def render_hook_summary(
    store: Store,
    loaded: tuple[list[Any], dict[int, Any]],
    *,
    days: int = LIVE_THREAD_DAYS,
    now: datetime | None = None,
) -> str:
    """``precis memory index --for-hook``: one counts line, not the list.

    gr499042: SessionStart output is injected once and then carried on every
    later turn, so the live-threads block moved behind the on-demand
    ``precis memory index`` this line names.
    """
    lines, total = _live_thread_lines(store, loaded, days=days, now=now)
    n_live = len(lines)
    older = total - n_live
    if n_live:
        counts = f"{n_live} live thread{'s' if n_live != 1 else ''}"
        if older:
            counts += f" (+{older} older)"
    else:
        counts = "no live threads" + (f" ({older} older)" if older else "")
    return f"🧠 memory: {counts} — 'precis memory index' for the list\n"


def _render_live_threads(
    store: Store,
    loaded: tuple[list[Any], dict[int, Any]],
    budget_tok: int | None,
    *,
    days: int,
    now: datetime | None,
) -> str:
    """``## Live threads`` block: ``section:threads`` nodes edited in ``days`` days.

    ``budget_tok`` caps the size (~4 bytes/token): the
    oldest listed lines drop first and join the "older" count.
    """
    lines, total = _live_thread_lines(store, loaded, days=days, now=now)

    def render(n_shown: int) -> str:
        out = [f"## Live threads (edited ≤{days} days)", *lines[:n_shown]]
        older = total - n_shown
        if older:
            out.append(
                f"(+{older} older thread nodes: search(kind='memory', "
                f"tags=['{SPACE_TAG}','{THREADS_TAG}']))"
            )
        return "\n".join(out) + "\n"

    shown = len(lines)
    text = render(shown)
    while (
        budget_tok is not None
        and shown > 0
        and len(text.encode("utf-8")) // _BYTES_PER_TOKEN > budget_tok
    ):
        shown -= 1
        text = render(shown)
    return text


def _load_nodes(store: Store) -> tuple[list[Any], dict[int, Any]]:
    """Live ``SPACE:repo-dev`` memory refs and their tags (the renderer's query)."""
    refs = _live_repo_dev_nodes(store)
    return refs, store.ref_tags_bulk([r.id for r in refs])


#: ``section:<type>`` tag -> the bracketed type prefix of an index line.
_TYPE_PREFIX = {
    "threads": "[thread]",
    "runbooks": "[runbook]",
    "gotchas": "[gotcha]",
    "workflow": "[workflow]",
    "reference": "[reference]",
}


def _type_prefix(values: set[str]) -> str:
    for v in sorted(values):
        if v.startswith(SECTION_TAG_PREFIX):
            pre = _TYPE_PREFIX.get(v[len(SECTION_TAG_PREFIX) :])
            if pre:
                return pre
    return ""


def _hub_key(ref: Any) -> tuple[int, int, str, int]:
    """Hubs by ``meta.order``, then title."""
    k = _order_key(ref)
    return (k[0], k[1], (ref.title or "").lower(), ref.id)


def _render_loaded(
    loaded: tuple[list[Any], dict[int, Any]],
    budget_tok: int | None,
    store: Store | None = None,
) -> str:
    """The session-start index. A hub (``section:index`` node) heads a block of
    the nodes whose nearest hub ancestor along ``part-of`` it is — band
    order (ACTIVE members first, then recency inside each group;
    :func:`precis.utils.memory_hubs.band_order`), each line prefixed with
    its ``section:<type>``; a detail node nests one level under its
    ``part-of`` parent. The root hub (no parent, has part-of members)
    renders no header. Older graphs pair a hub with the topics tagged
    ``section:<its slug>`` instead; nodes reaching no hub fall under
    ``Unfiled``. ``store`` supplies the edges and recency; without it only the
    slug pairing applies."""
    from precis.utils.memory_hubs import (
        band_order,
        hubs_of,
        parents_of,
        recency_stamps,
    )

    refs, tags = loaded
    values_of = {r.id: {v for _ns, v in tags.get(r.id, [])} for r in refs}
    sections = [r for r in refs if SECTION_INDEX_TAG in values_of[r.id]]
    hub_ids = {h.id for h in sections}
    topic_refs = [r for r in refs if r.id not in hub_ids]

    hub_of: dict[int, int] = {}
    parent_of: dict[int, list[int]] = {}
    stamps: dict[int, Any] = {}
    hub_has_parent: set[int] = set()
    hub_parents: set[int] = set()
    if store is not None and hub_ids:
        ancestry = hubs_of(store, [r.id for r in topic_refs])
        hub_of = {n: hs[0] for n, hs in ancestry.items() if hs[0] in hub_ids}
        parent_of = parents_of(store, [r.id for r in topic_refs])
        hub_parent_map = parents_of(store, sorted(hub_ids))
        hub_has_parent = set(hub_parent_map)
        hub_parents = {p for ps in hub_parent_map.values() for p in ps}
        stamps = recency_stamps(store, [r.id for r in topic_refs])

    slug_of = {
        h.id: str((h.meta or {}).get("section") or slugify(h.title)) for h in sections
    }
    by_slug = {slug: hid for hid, slug in slug_of.items()}
    legacy: dict[int, list[Any]] = {}  # hub id -> slug-paired topics
    stray: list[Any] = []
    graph: dict[int, list[Any]] = {}  # hub id -> part-of members
    for r in topic_refs:
        if r.id in hub_of:
            graph.setdefault(hub_of[r.id], []).append(r)
            continue
        slug = next(
            (
                v[len(SECTION_TAG_PREFIX) :]
                for v in sorted(values_of[r.id])
                if v.startswith(SECTION_TAG_PREFIX)
            ),
            "",
        )
        if slug in by_slug:
            legacy.setdefault(by_slug[slug], []).append(r)
        else:
            stray.append(r)

    def graph_entries(members: list[Any], hub_slug: str) -> list[_Entry]:
        ids = {m.id for m in members}

        def prefix(m: Any) -> str:
            # a header already names the type when it is the hub's own slug
            pre = _type_prefix(values_of[m.id])
            return "" if pre == f"[{hub_slug.rstrip('s')}]" else pre

        ordered = any(_order_key(m)[0] == 0 for m in members)
        if store is not None and stamps and not ordered:
            # meta.order pins a hand-ordered section; else band order —
            # ACTIVE first, recency within a group
            rank = {
                rid: i
                for i, rid in enumerate(
                    band_order(
                        store,
                        [m.id for m in members],
                        tags=values_of,
                        stamps=stamps,
                    )
                )
            }
            members = sorted(members, key=lambda m: rank.get(m.id, len(rank)))
        else:
            members = sorted(members, key=_order_key)
        kids: dict[int, list[Any]] = {}
        top: list[Any] = []
        for m in members:
            par = next((p for p in parent_of.get(m.id, []) if p in ids), None)
            (kids.setdefault(par, []) if par is not None else top).append(m)
        out: list[_Entry] = []
        for m in top:
            out.append((m, prefix(m), 0))
            stack = list(reversed(kids.get(m.id, [])))
            while stack:
                c = stack.pop()
                out.append((c, prefix(c), 1))
                stack.extend(reversed(kids.get(c.id, [])))
        return out

    grouped: list[tuple[str | None, list[_Entry]]] = []
    for sec in sorted(sections, key=_hub_key):
        entries = graph_entries(graph.get(sec.id, []), slug_of[sec.id]) + [
            (n, "", 0) for n in sorted(legacy.get(sec.id, []), key=_order_key)
        ]
        # the root: no parent, and other hubs hang off it
        root = sec.id not in hub_has_parent and sec.id in hub_parents
        if root:
            grouped.insert(0, (None, entries))
        else:
            grouped.append((sec.title, entries))
    if stray:
        grouped.append(("Unfiled", [(n, "", 0) for n in sorted(stray, key=_order_key)]))

    full = _render(grouped, cut_hooks=False)
    if budget_tok is None:
        return full
    size_tok = len(full.encode("utf-8")) // _BYTES_PER_TOKEN
    if size_tok <= budget_tok:
        return full
    cut = _render(grouped, cut_hooks=True)
    cut_tok = len(cut.encode("utf-8")) // _BYTES_PER_TOKEN
    return (
        cut + f"(memory index over budget: ~{size_tok} tok full, ~{cut_tok} tok "
        f"with hooks cut to {HOOK_CUT_CHARS} chars, budget {budget_tok} tok)\n"
    )


#: Per-node manifest written beside the exported node files, one row per
#: node (a section node's section is ``index``): handle, section slug, updated
#: (UTC date), body chars, chars the fisheye eye shows, live ring neighbours,
#: neighbours the eye's per-group cap hides (a ``section:index`` hub's
#: ``contains`` members are listed uncapped, so never hidden), dead-end links,
#: ``hub`` (handle of the node's ``part-of`` ``section:index`` hub; empty if
#: none, ``-`` on a hub node), ``qualified`` (live ``qualified-by`` edges),
#: ``accessed`` (UTC date of the last human view / agent recall; empty if never),
#: ``parent`` (handle of the direct ``part-of`` parent; empty if none),
#: ``review`` (comma-separated ``td<id>:<created date>`` of the open
#: ``memory-review`` todos linked to the node; empty if none), ``gotchas_none``
#: (1 if tagged ``gotchas:none``: reviewed, no gotcha applies),
#: ``supersedes`` (comma-separated handles of the node's ``supersedes``
#: edge targets; empty if none). ``hub`` is the nearest ``section:index``
#: *ancestor* along the part-of chain, so a detail node under a summary
#: node names the summary's hub.
#: Column order is the contract with scripts/memory-lint.
SECTIONS_MANIFEST = "_sections.tsv"


def render_prune_links(store: Store, *, dry_run: bool) -> str:
    """The ``precis memory prune-links`` report — one line per dangling edge
    (src or dst ref retired/missing, ``supersedes`` tombstones kept), then
    the count. Without ``dry_run`` the edges are deleted in one transaction."""
    dangling = store.dangling_links(kinds=("memory",))
    lines = [
        f"  link {e['link_id']}: {e['src_ref_id']} -[{e['relation']}]-> "
        f"{e['dst_ref_id']} (src {e['src_state']}, dst {e['dst_state']})"
        for e in dangling
    ]
    if dry_run:
        lines.append(
            f"memory prune-links (dry run): {len(dangling)} "
            "dangling edge(s), nothing written"
        )
        return "\n".join(lines) + "\n"
    with store.tx() as conn:
        n = store.delete_links([e["link_id"] for e in dangling], conn=conn)
    lines.append(f"memory prune-links: removed {n} dangling edge(s)")
    return "\n".join(lines) + "\n"


def _handle_of(ids: list[int]) -> str:
    """Handle of the first id (``part-of`` is one-per-node by convention),
    ``''`` for none."""
    from precis.utils import handle_registry

    if not ids:
        return ""
    return handle_registry.try_format("memory", ids[0]) or str(ids[0])


def _hub_column(section: str, hubs: list[int]) -> str:
    """The manifest ``hub`` cell: ``-`` for a hub node, else the handle of the
    nearest ``section:index`` ancestor along ``part-of`` (``''`` if the chain
    never reaches one)."""
    return "-" if section == "index" else _handle_of(hubs)


def export_memory_nodes(
    store: Store,
    dest: Path | str,
    *,
    loaded: tuple[list[Any], dict[int, Any]] | None = None,
) -> int:
    """Write each topic node's body to ``<dest>/<handle>.md``; return the count.

    A topic node is a live ``SPACE:repo-dev`` memory that is not a
    ``section:index`` node. A file is ``# <title>``, a blank line, then the
    body exactly as :meth:`MemoryHandler._body_text` returns it. The set is
    written into a sibling temp dir and swapped in with renames, so a reader
    sees the old set or the new one, never a half-written one (the one gap is
    the instant between the two renames, when ``dest`` is briefly absent).
    ``<dest>/_sections.tsv`` (:data:`SECTIONS_MANIFEST`) gives each handle —
    section nodes too, as section ``index``, though they get no file — its
    section slug (the renderer's rule: first ``section:*`` tag), its last
    update, and what a ``fisheye+1hop`` read of it shows — body chars against
    the eye's cap, ring neighbours live / hidden by the group cap / dead — so
    ``scripts/memory-lint`` lints the graph, not just the bodies.
    ``loaded`` reuses a :func:`_load_nodes` result.
    """
    from precis.dispatch import Hub
    from precis.handlers.memory import MemoryHandler
    from precis.utils import handle_registry
    from precis.utils.eye_render import (
        _NEIGHBOR_GROUP_CAP,
        _VERBATIM_CAP,
        first_hop_shape,
    )

    refs, tags = loaded if loaded is not None else _load_nodes(store)
    handler = MemoryHandler(hub=Hub(store=store))
    from precis.utils.memory_hubs import (
        accessed_dates,
        hubs_of,
        parents_of,
        qualified_counts,
        review_todos,
        supersedes_targets,
    )

    ids = [r.id for r in refs]
    shape = first_hop_shape(store, ids)
    hub_of = hubs_of(store, ids)
    parent_of = parents_of(store, ids)
    qualified = qualified_counts(store, ids)
    accessed = accessed_dates(store, ids)
    reviews = review_todos(store, ids)
    # Column 15: the node's ``supersedes`` targets as handles — the graph
    # truth memory-lint's prose-vs-edge check compares bodies against.
    supersede_of = supersedes_targets(store, ids)
    dst_kinds = {
        r.id: r.kind
        for r in store.fetch_refs_by_ids(
            sorted({d for ds in supersede_of.values() for d in ds})
        ).values()
    }

    def _sup_handles(ref_id: int) -> str:
        return ",".join(
            handle_registry.try_format(dst_kinds.get(d, "memory"), d) or str(d)
            for d in supersede_of.get(ref_id, [])
        )

    final = Path(dest)
    tmp = final.with_name(f"{final.name}.tmp.{os.getpid()}")
    old = final.with_name(f"{final.name}.old.{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        count = 0
        manifest: list[str] = []
        for ref in refs:
            values = {v for _ns, v in tags.get(ref.id, [])}
            handle = handle_registry.try_format("memory", ref.id) or str(ref.id)
            body = handler._body_text(ref)
            if SECTION_INDEX_TAG in values:
                # a section node: a manifest row (section ``index``) for its
                # shape — it is where fisheye navigation starts — but no file
                section = SECTION_INDEX_TAG[len(SECTION_TAG_PREFIX) :]
            else:
                text = f"# {ref.title}\n\n{body}"
                (tmp / f"{handle}.md").write_text(text, encoding="utf-8")
                count += 1
                section = next(
                    (
                        v[len(SECTION_TAG_PREFIX) :]
                        for v in sorted(values)
                        if v.startswith(SECTION_TAG_PREFIX)
                    ),
                    "",
                )
            live, largest, dead = shape.get(ref.id, (0, 0, 0))
            chars = len(body.strip())
            row = (
                handle,
                section,
                ref.updated_at.astimezone(UTC).strftime("%Y-%m-%d"),
                chars,
                min(chars, _VERBATIM_CAP),
                live,
                max(0, largest - _NEIGHBOR_GROUP_CAP),
                dead,
                _hub_column(section, hub_of.get(ref.id, [])),
                qualified.get(ref.id, 0),
                accessed.get(ref.id, ""),
                _handle_of(parent_of.get(ref.id, [])),
                reviews.get(ref.id, ""),
                1 if "gotchas:none" in values else 0,
                _sup_handles(ref.id),
            )
            manifest.append("\t".join(map(str, row)) + "\n")
        (tmp / SECTIONS_MANIFEST).write_text("".join(manifest), encoding="utf-8")
        if final.exists():
            os.replace(final, old)
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        if old.exists() and not final.exists():
            os.replace(old, final)  # put the previous set back
        raise
    shutil.rmtree(old, ignore_errors=True)
    return count


# ---------------------------------------------------------------------------
# backfill-space (gr548275)
# ---------------------------------------------------------------------------

#: Open tag stamped on a memory no rule could space — the residue stays
#: countable (``search(kind='memory', tags=['space:unknown'])``) without a
#: bogus ``SPACE:`` value. Lowercase prefix: an *open* tag, not the axis.
UNKNOWN_SPACE_TAG = "space:unknown"

#: Rule (a) — a tag already on the node names its writer's space. The
#: gripe's list is ``tier:synthetic-insight``, ``DREAM:*``,
#: ``kind:lemma``/``kind:inference`` → research; ``dream-thread`` /
#: ``thread:*`` are the dream writer's open tags, and ``section:*`` /
#: ``mirror:*`` / ``gotchas:none`` / ``repo-dev`` exist only inside the
#: repo-dev memory shape.
_RESEARCH_TAGS = frozenset(
    {"tier:synthetic-insight", "kind:lemma", "kind:inference", "dream-thread"}
)
_RESEARCH_TAG_PREFIXES = ("thread:", "DREAM:")
_REPO_DEV_TAGS = frozenset({"gotchas:none", "repo-dev"})
_REPO_DEV_TAG_PREFIXES = ("section:", "mirror:")

#: Rule (b) — the writer signal on the refs row: ``meta.source`` and
#: ``ref_events.source`` values → space (the gripe's writer map; prod
#: carries no memory create events, so events are only a fallback).
_WRITER_SPACE = {
    "asa": "research",
    "discord": "research",
    "dreaming": "research",
    "quest-tick": "research",
    "fixer": "repo-dev",
    "gripe-fix": "repo-dev",
    "harness": "repo-dev",
    "memory-import": "repo-dev",
    "memory-mirror": "repo-dev",
}
#: ``refs.meta`` keys only research-side system writers stamp — the
#: ``write_system`` digests (citer-watch / upstream-watch / the two
#: reviewers' ``meta_prefix``) and the dream consolidation summary.
_RESEARCH_META_PREFIXES = (
    "dream_consolidated",
    "citer_watch_",
    "upstream_watch_",
    "deep_review_",
    "structural_",
)
#: ``refs.set_by`` values only research-side writers produce (handler
#: creates leave the column NULL; ingest/worker paths stamp it).
_RESEARCH_SET_BY = frozenset({"dream", "system"})

#: Rule (c) — a link endpoint's kind says which corpus authored the note.
_RESEARCH_LINK_KINDS = frozenset({"quest", "paper", "finding", "conv"})

#: Sample handles printed per inferred space on a dry run.
_SAMPLE_HANDLES = 10


@dataclass
class BackfillSpaceReport:
    """What one :func:`backfill_space` run did / would do."""

    dry_run: bool = False
    #: Live memories without a SPACE tag that were classified.
    scanned: int = 0
    #: space → count of memories the rules inferred for it.
    counts: dict[str, int] = field(default_factory=dict)
    #: ``"<space>/<rule>"`` → count — which evidence fired.
    rules: dict[str, int] = field(default_factory=dict)
    #: ``(ref_id, space, rule)`` per classified memory — the full plan;
    #: :data:`samples` is the rendered slice.
    decisions: list[tuple[int, str, str]] = field(default_factory=list)
    #: space → up to :data:`_SAMPLE_HANDLES` handles.
    samples: dict[str, list[str]] = field(default_factory=dict)
    #: Real run only: space → SPACE tags written.
    tagged: dict[str, int] = field(default_factory=dict)
    #: Real run only: ``space:unknown`` newly stamped / already present.
    unknown_stamped: int = 0
    unknown_kept: int = 0

    def summary(self) -> str:
        head = "memory backfill-space"
        if self.dry_run:
            head += " (dry run — nothing written)"
        lines = [f"{head}: scanned {self.scanned} unspaced live memories"]
        for space in ("research", "repo-dev", "unknown"):
            rules = ", ".join(
                f"{key.split('/', 1)[1]}={n}"
                for key, n in sorted(self.rules.items())
                if key.startswith(f"{space}/")
            )
            lines.append(
                f"  {space}: {self.counts.get(space, 0)}"
                + (f" ({rules})" if rules else "")
            )
            samp = self.samples.get(space)
            if samp:
                lines.append(f"    samples: {' '.join(samp)}")
        if not self.dry_run:
            tagged = (
                ", ".join(
                    f"SPACE:{space}={self.tagged[space]}"
                    for space in sorted(self.tagged)
                )
                or "none"
            )
            lines.append(f"  tagged: {tagged}")
            lines.append(
                f"  {UNKNOWN_SPACE_TAG}: {self.unknown_stamped} stamped"
                + (
                    f", {self.unknown_kept} already stamped"
                    if self.unknown_kept
                    else ""
                )
            )
        return "\n".join(lines) + "\n"


def infer_space(
    ref: Any,
    tag_values: set[str],
    event_sources: set[str],
    linked: list[tuple[str, bool]],
) -> tuple[str, str]:
    """``(space, rule)`` for one unspaced memory — the gr548275 rule order.

    ``tag_values`` renders the ref's live tags as the agent sees them
    (``"SPACE:research"`` for a closed tag, ``"section:threads"`` for an
    open one). ``event_sources`` is the ref's distinct
    ``ref_events.source`` set; ``linked`` is ``(kind, is_repo_dev)`` per
    live link endpoint, where ``is_repo_dev`` means the endpoint carries
    ``SPACE:repo-dev`` (or, for a todo, an open ``repo-dev`` tag).

    (a) tags → (b) writer signal on the refs row (``meta`` identity keys,
    ``meta.source``, digest meta prefixes, ``meta.hook``, ``set_by``, then
    ``ref_events`` as fallback) → (c) link-endpoint kinds (research kinds
    first, per the gripe's written order) → (d) ``unknown``.
    """
    # (a) tags already present
    if tag_values & _RESEARCH_TAGS or any(
        v.startswith(_RESEARCH_TAG_PREFIXES) for v in tag_values
    ):
        return "research", "a:tags"
    if tag_values & _REPO_DEV_TAGS or any(
        v.startswith(_REPO_DEV_TAG_PREFIXES) for v in tag_values
    ):
        return "repo-dev", "a:tags"
    # (b) writer signal on the refs row, then ref_events as fallback
    meta = ref.meta or {}
    if "file_mirror" in meta:
        return "repo-dev", "b:file-mirror"
    if "slug" in meta or "section" in meta:
        return "repo-dev", "b:import-keys"
    source = meta.get("source")
    if isinstance(source, str) and source in _WRITER_SPACE:
        return _WRITER_SPACE[source], "b:meta-source"
    if any(k.startswith(_RESEARCH_META_PREFIXES) for k in meta):
        return "research", "b:system-digest"
    if "hook" in meta:
        return "repo-dev", "b:hook"
    if ref.set_by in _RESEARCH_SET_BY:
        return "research", "b:set-by"
    ev_spaces = {_WRITER_SPACE[s] for s in event_sources if s in _WRITER_SPACE}
    if "repo-dev" in ev_spaces:
        return "repo-dev", "b:events"
    if "research" in ev_spaces:
        return "research", "b:events"
    # (c) link-endpoint kinds — research first (written order)
    if any(kind in _RESEARCH_LINK_KINDS for kind, _ in linked):
        return "research", "c:links"
    if any(
        kind == "gripe" or (kind in ("todo", "memory") and repo_dev)
        for kind, repo_dev in linked
    ):
        return "repo-dev", "c:links"
    # (d) residue
    return "unknown", "d:none"


def _unspaced_memory_ids(store: Store, limit: int | None) -> list[int]:
    """Live ``kind='memory'`` ids carrying no live ``SPACE:*`` tag."""
    sql = (
        "SELECT r.ref_id FROM refs r "
        "WHERE r.kind = 'memory' AND r.retired_at IS NULL "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
        "  WHERE rt.ref_id = r.ref_id AND t.namespace = 'SPACE' "
        "  AND (rt.expires_at IS NULL OR rt.expires_at > now())"
        ") ORDER BY r.ref_id"
    )
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with store.pool.connection() as conn:
        return [int(r[0]) for r in conn.execute(sql, params).fetchall()]


def _event_sources(store: Store, ids: list[int]) -> dict[int, set[str]]:
    """Distinct ``ref_events.source`` values per candidate ref."""
    if not ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ref_id, source FROM ref_events WHERE ref_id = ANY(%s)",
            (ids,),
        ).fetchall()
    out: dict[int, set[str]] = {}
    for ref_id, source in rows:
        out.setdefault(int(ref_id), set()).add(str(source))
    return out


def _linked_endpoints(
    store: Store, ids: list[int]
) -> dict[int, list[tuple[str, bool]]]:
    """``{memory_id: [(endpoint kind, endpoint is repo-dev-marked), …]}``.

    Both edge directions count — a gripe linking *to* a memory is as much
    repo-dev evidence as the memory linking the gripe. An endpoint is
    repo-dev-marked when it carries ``SPACE:repo-dev`` (or, for a todo,
    the open ``repo-dev`` tag). Retired endpoints count too: a retired
    gripe still says who wrote the note.
    """
    if not ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_ref_id, dst_ref_id FROM links "
            "WHERE src_ref_id = ANY(%s) OR dst_ref_id = ANY(%s)",
            (ids, ids),
        ).fetchall()
    idset = set(ids)
    neighbours: dict[int, set[int]] = {}
    for src, dst in rows:
        s, d = int(src), int(dst)
        if s == d:
            continue
        if s in idset:
            neighbours.setdefault(s, set()).add(d)
        if d in idset:
            neighbours.setdefault(d, set()).add(s)
    other_ids = sorted({o for ends in neighbours.values() for o in ends})
    others = store.fetch_refs_by_ids(other_ids)  # tombstones still signal
    space_rd = store.ref_tag_values(other_ids, "SPACE", ["repo-dev"])
    open_rd = store.ref_tag_values(other_ids, "OPEN", ["repo-dev"])
    out: dict[int, list[tuple[str, bool]]] = {}
    for rid, ends in neighbours.items():
        entries = []
        for o in ends:
            other = others.get(o)
            if other is None:
                continue
            repo_dev = o in space_rd or (other.kind == "todo" and o in open_rd)
            entries.append((other.kind, repo_dev))
        out[rid] = entries
    return out


def backfill_space(
    store: Store, *, dry_run: bool = False, limit: int | None = None
) -> BackfillSpaceReport:
    """Infer ``SPACE:`` on every live memory that carries none (gr548275).

    ``limit`` caps the candidate set (oldest first). ``dry_run`` plans
    only — same classification, no writes. A real run stamps
    ``SPACE:research`` / ``SPACE:repo-dev`` (``set_by='system'``,
    ``replace_prefix`` for safety) and leaves the ``unknown`` residue
    unspaced but carrying :data:`UNKNOWN_SPACE_TAG` so it stays countable;
    a stale ``space:unknown`` is removed when a node later classifies.
    Idempotent: a re-run finds no unspaced node left to tag and re-reports
    the stamped unknowns as ``unknown_kept``.
    """
    from precis.utils import handle_registry

    ids = _unspaced_memory_ids(store, limit)
    refs = store.fetch_refs_by_ids(ids, include_deleted=False)
    tag_rows = store.ref_tags_bulk(ids)
    tag_values = {
        rid: {v if ns in ("OPEN", "FLAG") else f"{ns}:{v}" for ns, v in pairs}
        for rid, pairs in tag_rows.items()
    }
    sources = _event_sources(store, ids)
    linked = _linked_endpoints(store, ids)

    report = BackfillSpaceReport(dry_run=dry_run, scanned=len(ids))
    plan: list[tuple[Any, str, str]] = []
    for rid in ids:
        ref = refs.get(rid)
        if ref is None:
            continue  # retired between the two reads — skip
        space, rule = infer_space(
            ref,
            tag_values.get(rid, set()),
            sources.get(rid, set()),
            linked.get(rid, []),
        )
        plan.append((ref, space, rule))
        report.decisions.append((rid, space, rule))
        report.counts[space] = report.counts.get(space, 0) + 1
        key = f"{space}/{rule}"
        report.rules[key] = report.rules.get(key, 0) + 1
        bucket = report.samples.setdefault(space, [])
        if len(bucket) < _SAMPLE_HANDLES:
            handle = handle_registry.try_format("memory", rid) or f"me{rid}"
            bucket.append(handle)

    if dry_run:
        return report

    with store.tx() as conn:
        for ref, space, _rule in plan:
            has_unknown = UNKNOWN_SPACE_TAG in tag_values.get(ref.id, set())
            if space == "unknown":
                if has_unknown:
                    report.unknown_kept += 1
                    continue
                store.add_tag(
                    ref.id,
                    Tag.open(UNKNOWN_SPACE_TAG),
                    set_by="system",
                    conn=conn,
                )
                report.unknown_stamped += 1
                continue
            store.add_tag(
                ref.id,
                Tag.closed("SPACE", space),
                set_by="system",
                replace_prefix=True,
                conn=conn,
            )
            if has_unknown:
                store.remove_tag(ref.id, Tag.open(UNKNOWN_SPACE_TAG), conn=conn)
            report.tagged[space] = report.tagged.get(space, 0) + 1
    return report


def migrate_rulings(store: Store, *, apply: bool = False) -> str:
    """One-off: move the backfilled ruling nodes' trailing ``Meta:`` body line
    into real ``meta`` (``ruled_by``/``ruled_on``/``status``/``review_by``)
    and drop their duplicate ``status:*`` tag (gr548835 slice 1).

    Operator-run only; never wired to a hook or cadence. ``apply=False``
    (the default) plans and writes nothing. The write is each node's own
    ``edit(meta=...)``, so validation and the tag drop are the handler's;
    the body, including its ``Meta:`` line, is left untouched. Nodes whose
    ``meta.status`` is already set are skipped, so a re-run plans nothing.
    """
    from precis.dispatch import Hub
    from precis.errors import BadInput
    from precis.handlers.memory import MemoryHandler
    from precis.utils import handle_registry
    from precis.utils.memory_rulings import plan_meta_migration, validate_ruling_meta

    handler = MemoryHandler(hub=Hub(store=store, embedder=None))
    items = plan_meta_migration(store, handler._body_text)
    lines = [
        "migrate-rulings: "
        + ("applying" if apply else "dry run — pass --apply to write")
    ]
    wrote = failed = skipped = 0
    for item in items:
        handle = handle_registry.try_format("memory", item.ref_id) or f"me{item.ref_id}"
        head = f"  {handle} {item.title!r}"
        if not item.meta:
            skipped += 1
            lines.append(f"{head} — no 'Meta:' line found; stamp by hand")
            continue
        try:
            patch = validate_ruling_meta(item.meta, existing=item.existing)
            if apply:
                handler.edit(id=item.ref_id, meta=item.meta)
        except BadInput as exc:
            failed += 1
            lines.append(f"{head} — refused: {exc.cause}")
            continue
        wrote += 1
        shown = ", ".join(f"{k}={v}" for k, v in {**item.meta, **patch}.items())
        lines.append(f"{head} — {'wrote' if apply else 'would write'} {shown}")
    verb = "wrote" if apply else "would write"
    lines.append(
        f"migrate-rulings: {verb} {wrote}, refused {failed}, no Meta line {skipped}"
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# argparse shell
# ---------------------------------------------------------------------------


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``memory`` subparser (``import`` and ``index``) on ``sub``."""
    mp = sub.add_parser(
        "memory",
        help="Import / render harness memory as SPACE:repo-dev graph nodes.",
        description=(
            "Harness memory in the graph: `import` seeds nodes from a memory "
            "directory (MEMORY.md + topic files), `index` renders them as the "
            "session-start index."
        ),
    )
    msub = mp.add_subparsers(dest="memory_cmd", required=True)
    memory_recall_set.configure_parser(msub)

    imp = msub.add_parser(
        "import",
        help="Seed memory nodes from <dir>/MEMORY.md (idempotent; --sync converges).",
    )
    imp.add_argument("dir", help="Harness memory directory (holds MEMORY.md).")
    imp.add_argument(
        "--sync",
        action="store_true",
        help=(
            "Re-converge the graph on the files (cutover, run once): update "
            "changed nodes, create missing ones, retire nodes whose bullet is "
            "gone. Native nodes (no meta.slug/section) are never touched."
        ),
    )
    imp.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Plan the run (created/updated/retired/links counts, slugs that "
            "would be retired) and write nothing."
        ),
    )
    imp.add_argument(
        "--allow-retire",
        type=int,
        default=None,
        metavar="N",
        help=(
            "--sync only: permit retiring up to N nodes. Without it a sync "
            "that would retire more than max(5, 10%% of imported nodes) is "
            "refused; N must be >= the planned retire count (see --dry-run)."
        ),
    )
    imp.add_argument("--database-url", default=None, help="Postgres DSN override.")

    idx = msub.add_parser(
        "index",
        help="Print the memory index rendered from the graph.",
    )
    idx.add_argument(
        "--budget-tok",
        type=int,
        default=None,
        help=(
            "Token budget for the rendered index (~4 bytes/token). Over it, "
            "the live-thread list drops its oldest lines (with --full: hooks "
            f"are cut to {HOOK_CUT_CHARS} chars and a trailing line names "
            "the overage)."
        ),
    )
    idx.add_argument(
        "--days",
        type=int,
        default=LIVE_THREAD_DAYS,
        help=(
            "Live-thread window: section:threads nodes edited within this many "
            f"days are listed (default {LIVE_THREAD_DAYS}); older ones are counted."
        ),
    )
    idx.add_argument(
        "--full",
        action="store_true",
        help="Print the old whole-graph index (every node, grouped by section).",
    )
    idx.add_argument(
        "--for-hook",
        action="store_true",
        help=(
            "SessionStart form (gr499042): one counts line "
            "(N live threads +M older) naming this command, not the list. "
            "Wins over --full/--budget-tok."
        ),
    )
    idx.add_argument(
        "--export-dir",
        default=None,
        metavar="DIR",
        help=(
            "Also write each topic node's body to DIR/<handle>.md ('# title', "
            "blank line, body), swapped in whole. A failed export prints one "
            "stderr line and leaves the index output and exit code alone."
        ),
    )
    idx.add_argument(
        "--q",
        default=None,
        metavar="TEXT",
        help=(
            "Recall instead of listing: print the --k best hybrid-search hits "
            "for TEXT among SPACE:repo-dev memories as index bullets (with the "
            "mirror filename beside the handle)."
        ),
    )
    idx.add_argument(
        "--set",
        dest="recall_set",
        nargs="?",
        const="",
        default=None,
        metavar="JSON",
        help=(
            "Score a recall question set instead of listing: a JSON list of "
            "{query, want} (want = the answer me-handle). Prints HIT/MISS per "
            "entry and a RECALL total at --k, split native/mirror. Bare "
            "--set scores the packaged P4 set (precis.data "
            "memory_recall_set.json)."
        ),
    )
    idx.add_argument(
        "--curve",
        action="store_true",
        help=(
            "With --set, print recall and median index + fisheye+1hop "
            "estimated tokens at k=3/5/8/12 (chars/4)."
        ),
    )
    idx.add_argument(
        "--k", type=int, default=5, help="Hits to print with --q/--set (default 5)."
    )
    idx.add_argument("--database-url", default=None, help="Postgres DSN override.")
    swp = msub.add_parser(
        "sweep",
        help=(
            "List (or with --apply, run) the section:threads retirement "
            "sweep — the same rules the daily memory_thread_sweep cadence "
            "applies. Dry-run by default."
        ),
    )
    swp.add_argument(
        "--apply",
        action="store_true",
        help="Actually retire the listed nodes (default: plan only).",
    )
    swp.add_argument("--database-url", default=None, help="Postgres DSN override.")
    prn = msub.add_parser(
        "prune-links",
        help=(
            "List (and unless --dry-run, delete) link edges touching a "
            "memory whose src or dst ref is retired or missing — the dead "
            "ends a memory delete used to leave (gr503031). 'supersedes' "
            "tombstone edges are kept."
        ),
    )
    prn.add_argument(
        "--dry-run",
        action="store_true",
        help="List the dangling edges and write nothing.",
    )
    prn.add_argument("--database-url", default=None, help="Postgres DSN override.")
    bfs = msub.add_parser(
        "backfill-space",
        help=(
            "Infer SPACE: on live memories that carry none (gr548275): "
            "present tags, then the writer signal on the refs row "
            "(meta keys, ref_events fallback), then link endpoints. The "
            "residue gets a 'space:unknown' open tag so it stays countable."
        ),
    )
    bfs.add_argument(
        "--dry-run",
        action="store_true",
        help="Report counts per inferred space and per rule, with sample handles; write nothing.",
    )
    bfs.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Classify at most N unspaced memories (oldest first).",
    )
    bfs.add_argument("--database-url", default=None, help="Postgres DSN override.")
    mrl = msub.add_parser(
        "migrate-rulings",
        help=(
            "One-off (gr548835): move the ruling nodes' trailing 'Meta:' body "
            "line into real meta (ruled_by/ruled_on/status/review_by) and drop "
            "the duplicate status:* tag. Dry run by default; operator-run only."
        ),
    )
    mrl.add_argument(
        "--apply",
        action="store_true",
        help="Write the planned meta (default: plan only).",
    )
    mrl.add_argument("--database-url", default=None, help="Postgres DSN override.")
    mirror = msub.add_parser(
        "mirror", help="Explicit faithful file/graph snapshot exchange."
    )
    modes = mirror.add_subparsers(dest="mirror_cmd", required=True)
    for mode in ("import", "export"):
        parser = modes.add_parser(
            mode,
            help=(
                "Import a flat YAML/Markdown snapshot; refuse graph conflicts."
                if mode == "import"
                else "Export original filenames to a NEW directory."
            ),
        )
        parser.add_argument("dir", help="Source directory / fresh export destination.")
        parser.add_argument(
            "--namespace", required=True, help="Stable identity for this file set."
        )
        if mode == "import":
            parser.add_argument(
                "--legacy",
                choices=("refuse", "retire", "keep", "refresh"),
                default="refuse",
                help=(
                    "Live repo-dev nodes from the one-shot importer (no "
                    "file_mirror, title or slug = a topic's name: or file stem; unmatched ones are only listed): refuse (default), "
                    "retire (soft-delete in the import), keep (import beside), or "
                    "refresh (adopt the node in place, keeping its id and links)."
                ),
            )
        if mode == "import":
            parser.add_argument(
                "--missing",
                choices=("keep", "retire"),
                default="keep",
                help="Nodes of files deleted since last import: keep live (default) or retire.",
            )
        if mode == "export":
            parser.add_argument(
                "--no-stamp",
                action="store_true",
                help=(
                    "Backup mode: render native nodes to files but leave the "
                    "graph untouched (no file_mirror stamp, no retitling)."
                ),
            )
        parser.add_argument(
            "--database-url", default=None, help="Postgres DSN override."
        )
    return mp


def run(args: argparse.Namespace) -> None:
    """Implements ``precis memory import`` / ``precis memory index``."""
    from precis.config import load_config

    if args.memory_cmd == "recall-set":
        memory_recall_set.run(args)
        return
    if getattr(args, "curve", False) and args.recall_set is None:
        raise SystemExit("precis memory index: --curve requires --set")
    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        if args.memory_cmd == "mirror":
            import json
            from dataclasses import asdict

            from precis.cli.memory_mirror import export_mirror, import_mirror

            try:
                if args.mirror_cmd == "import":
                    report_mirror = import_mirror(
                        store,
                        Path(args.dir),
                        namespace=args.namespace,
                        legacy=args.legacy,
                        missing=args.missing,
                    )
                    print(json.dumps(asdict(report_mirror), sort_keys=True))
                else:
                    exported = export_mirror(
                        store,
                        Path(args.dir),
                        namespace=args.namespace,
                        stamp=not args.no_stamp,
                    )
                    print(f"exported {exported.created} files")
                    if exported.exported_native:
                        print(
                            f"exported native: {' '.join(exported.exported_native)}",
                            file=sys.stderr,
                        )
                    if exported.unexported:
                        print(
                            f"unexported: {' '.join(exported.unexported)}",
                            file=sys.stderr,
                        )
            except (ImportRefused, OSError) as exc:
                raise SystemExit(f"precis memory mirror: refused: {exc}") from exc
        elif args.memory_cmd == "import":
            try:
                report = import_memory_dir(
                    store,
                    Path(args.dir),
                    sync=args.sync,
                    dry_run=args.dry_run,
                    allow_retire=args.allow_retire,
                )
            except ImportRefused as exc:
                raise SystemExit(f"precis memory import: refused: {exc}") from exc
            print(report.summary())
        elif args.memory_cmd == "sweep":
            from precis.utils import handle_registry
            from precis.workers.memory_thread_sweep import (
                plan_thread_sweep,
                run_memory_thread_sweep_pass,
            )

            plan = plan_thread_sweep(store)
            if not args.apply:
                if not plan:
                    print("memory sweep: nothing to retire")
                else:
                    print("memory sweep (dry run — pass --apply to retire):")
                    for item in plan:
                        handle = (
                            handle_registry.try_format("memory", item.ref_id)
                            or f"me{item.ref_id}"
                        )
                        carry = "; Left: line becomes a todo" if item.left else ""
                        print(
                            f"  {handle} {item.title!r} — rule {item.rule}: "
                            f"{item.reason}{carry}"
                        )
            else:
                result = run_memory_thread_sweep_pass(store)
                print(
                    f"memory sweep: claimed={result.claimed} "
                    f"retired={result.ok} failed={result.failed}"
                )
        elif args.memory_cmd == "prune-links":
            print(render_prune_links(store, dry_run=args.dry_run), end="")
        elif args.memory_cmd == "migrate-rulings":
            print(migrate_rulings(store, apply=args.apply), end="")
        elif args.memory_cmd == "backfill-space":
            bfs_report = backfill_space(store, dry_run=args.dry_run, limit=args.limit)
            print(bfs_report.summary(), end="")
        elif args.recall_set is not None:
            from precis.cli._common import cli_embedder

            # Recall is measured with vectors; lexical-only would mislead.
            embedder = cli_embedder(
                cfg, store, needed=True, prog="precis memory index --recall-set"
            )
            entries = load_recall_set(args.recall_set or None)
            if args.curve:
                from precis.cli.memory_recall import measure_recall, render_recall_curve

                print(
                    render_recall_curve(
                        measure_recall(store, entries, embedder=embedder)
                    ),
                    end="",
                    flush=True,
                )
                return
            print(
                render_recall_report(store, entries, k=args.k, embedder=embedder),
                end="",
                flush=True,
            )
        elif args.q is not None:
            from precis.cli._common import cli_embedder

            embedder = cli_embedder(cfg, store, needed=False, prog="precis memory")
            if embedder is None:
                print(
                    "precis memory: PRECIS_EMBEDDER_URL unset; lexical search only",
                    file=sys.stderr,
                )
            print(
                render_memory_index(store, q=args.q, k=args.k, embedder=embedder),
                end="",
                flush=True,
            )
        else:
            loaded = _load_nodes(store)
            if args.for_hook:
                rendered = render_hook_summary(store, loaded, days=args.days, now=None)
            elif args.full:
                rendered = _render_loaded(loaded, args.budget_tok, store)
            else:
                rendered = _render_live_threads(
                    store, loaded, args.budget_tok, days=args.days, now=None
                )
            print(rendered, end="", flush=True)
            if args.export_dir:
                try:
                    export_memory_nodes(store, args.export_dir, loaded=loaded)
                except Exception as exc:  # the index is already out; stay exit 0
                    print(
                        f"precis memory index: node export to {args.export_dir} "
                        f"failed: {type(exc).__name__}: {exc}"[:300],
                        file=sys.stderr,
                    )
    finally:
        store.close()


__all__ = [
    "GRAPH_MARKER",
    "ImportRefused",
    "ImportReport",
    "add_parser",
    "export_memory_nodes",
    "import_memory_dir",
    "render_hook_summary",
    "render_memory_index",
    "run",
]

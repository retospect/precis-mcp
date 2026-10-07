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
- ``precis memory index [--budget-tok N] [--export-dir DIR]`` renders the
  index back out, one ``- <Title> (<handle>) — <hook>`` bullet per node (the
  handle is what ``get``/``edit`` take; the graph node is the truth, not a
  file), for ``scripts/hooks/session-start-memory.sh``. ``--export-dir``
  also writes each topic node's body to ``DIR/<handle>.md`` (same query,
  swapped in whole) so ``scripts/memory-lint`` can lint node bodies.

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
from pathlib import Path
from typing import Any

from precis.cli._common import resolve_dsn
from precis.store import Store
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
            return orphan
        if not write:
            return next(fake_ids)  # a dry run's stand-in id; never stored
        resp = handler.put(text=body, title=title, tags=tags)
        ref_id = _created_id(resp)
        store.update_ref(ref_id, meta_patch=meta)
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
) -> str:
    """One index bullet: ``- <Title> (<handle>[, <filename>]) — <hook>``.

    The session-start index uses the default (no filename); the recall
    renders (``--q`` and ``search(view='index')``) pass ``filename=True`` so a
    hit matches the topic file the harness may also have recalled, and
    ``fallback_hook`` (the body's first line) for a node with no ``meta.hook``.
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
    return f"{line} — {hook}" if hook else line


def _bullet_line(ref: Any, *, cut_hooks: bool) -> str:
    return bullet_line(ref, cut_hooks=cut_hooks)


def _render(sections: list[tuple[str, list[Any]]], *, cut_hooks: bool) -> str:
    out = [INDEX_TITLE]
    for title, nodes in sections:
        out.append("")
        out.append(f"## {title}")
        out.append("")
        out.extend(_bullet_line(n, cut_hooks=cut_hooks) for n in nodes)
    return "\n".join(out) + "\n"


def render_memory_index(
    store: Store,
    budget_tok: int | None = None,
    *,
    q: str | None = None,
    k: int = 5,
    embedder: Any = None,
) -> str:
    """Render the ``SPACE:repo-dev`` memory index, one bullet per node.

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
    return _render_loaded(_load_nodes(store), budget_tok)


def _load_nodes(store: Store) -> tuple[list[Any], dict[int, Any]]:
    """Live ``SPACE:repo-dev`` memory refs and their tags (the renderer's query)."""
    refs = _live_repo_dev_nodes(store)
    return refs, store.ref_tags_bulk([r.id for r in refs])


def _render_loaded(
    loaded: tuple[list[Any], dict[int, Any]], budget_tok: int | None
) -> str:
    refs, tags = loaded
    sections: list[Any] = []
    topics: dict[str, list[Any]] = {}
    for ref in refs:
        values = {v for _ns, v in tags.get(ref.id, [])}
        if SECTION_INDEX_TAG in values:
            sections.append(ref)
            continue
        for v in sorted(values):
            if v.startswith(SECTION_TAG_PREFIX):
                topics.setdefault(v[len(SECTION_TAG_PREFIX) :], []).append(ref)
                break
        else:
            topics.setdefault("", []).append(ref)

    grouped: list[tuple[str, list[Any]]] = []
    known: set[str] = set()
    for sec in sorted(sections, key=_order_key):
        slug = str((sec.meta or {}).get("section") or slugify(sec.title))
        known.add(slug)
        grouped.append((sec.title, sorted(topics.get(slug, []), key=_order_key)))
    stray = [n for slug, nodes in topics.items() if slug not in known for n in nodes]
    if stray:
        grouped.append(("Unfiled", sorted(stray, key=_order_key)))

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


#: Handle → section-slug manifest written beside the exported node files.
SECTIONS_MANIFEST = "_sections.tsv"


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
    ``<dest>/_sections.tsv`` maps each handle to its section slug (the
    renderer's rule: first ``section:*`` tag), so ``scripts/memory-lint`` can
    scope its landed-thread scan to ``threads``.
    ``loaded`` reuses a :func:`_load_nodes` result.
    """
    from precis.dispatch import Hub
    from precis.handlers.memory import MemoryHandler
    from precis.utils import handle_registry

    refs, tags = loaded if loaded is not None else _load_nodes(store)
    handler = MemoryHandler(hub=Hub(store=store))
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
            if SECTION_INDEX_TAG in values:
                continue
            handle = handle_registry.try_format("memory", ref.id) or str(ref.id)
            text = f"# {ref.title}\n\n{handler._body_text(ref)}"
            (tmp / f"{handle}.md").write_text(text, encoding="utf-8")
            section = next(
                (
                    v[len(SECTION_TAG_PREFIX) :]
                    for v in sorted(values)
                    if v.startswith(SECTION_TAG_PREFIX)
                ),
                "",
            )
            manifest.append(f"{handle}\t{section}\n")
            count += 1
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
            f"hooks are cut to {HOOK_CUT_CHARS} chars and a trailing line "
            "names the overage."
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
        "--k", type=int, default=5, help="Hits to print with --q (default 5)."
    )
    idx.add_argument("--database-url", default=None, help="Postgres DSN override.")
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
        parser.add_argument(
            "--database-url", default=None, help="Postgres DSN override."
        )
    return mp


def run(args: argparse.Namespace) -> None:
    """Implements ``precis memory import`` / ``precis memory index``."""
    from precis.config import load_config

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
                        store, Path(args.dir), namespace=args.namespace
                    )
                    print(json.dumps(asdict(report_mirror), sort_keys=True))
                else:
                    print(
                        f"exported {export_mirror(store, Path(args.dir), namespace=args.namespace)} files"
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
        elif args.q is not None:
            from precis.embedder import make_embedder

            embedder = make_embedder(cfg.embedder, dim=store.embedding_dim())
            print(
                render_memory_index(store, q=args.q, k=args.k, embedder=embedder),
                end="",
                flush=True,
            )
        else:
            loaded = _load_nodes(store)
            print(_render_loaded(loaded, args.budget_tok), end="", flush=True)
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
    "render_memory_index",
    "run",
]

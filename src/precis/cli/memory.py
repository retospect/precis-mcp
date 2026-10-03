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
  neither key (native writes) alone.
- ``precis memory index [--budget-tok N]`` renders the index back out, one
  ``- <Title> (<handle>) — <hook>`` bullet per node (the handle is what
  ``get``/``edit`` take; the graph node is the truth, not a file), for
  ``scripts/hooks/session-start-memory.sh``.

The logic lives in :func:`import_memory_dir` and :func:`render_memory_index`
(both take a :class:`~precis.store.Store`) so tests call them directly; the
argparse layer is a thin shell. Nothing here writes to the harness memory
directory — import only reads it.
"""

from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from precis.cli._common import resolve_dsn
from precis.store import Store

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

_BULLET_RE = re.compile(
    r"^- \[(?P<title>[^\]]+)\]\((?P<file>[^)\s]+)\)(?: — (?P<hook>.*))?$"
)
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s#]+\.md)(?:#[^)]*)?\)")
_WIKI_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")


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

    def summary(self) -> str:
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


def _slugify(text: str) -> str:
    """Lowercase, alnum runs joined by ``-`` — a space-free tag/meta value."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


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
            sections.append(_Section(title, _slugify(title), len(sections) + 1))
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


def import_memory_dir(
    store: Store, path: Path | str, *, sync: bool = False
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
    """
    from precis.dispatch import Hub
    from precis.handlers.memory import MemoryHandler

    root = Path(path)
    index_text = (root / "MEMORY.md").read_text(encoding="utf-8")
    sections = parse_index(index_text)
    handler = MemoryHandler(hub=Hub(store=store))
    report = ImportReport()

    live = _live_repo_dev_nodes(store)
    by_id = {r.id: r for r in live}
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

    def _create(title: str, body: str, tags: list[str], meta: dict[str, Any]) -> int:
        orphan = orphans.pop(title, None)
        if orphan is not None:
            store.update_ref(orphan, meta_patch=meta)
            return orphan
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
            store.chunks.set_ref_title(sec_id, sec.title, source="agent")
            changed = True
        if (ref.meta or {}).get("order") != sec.order:
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
            store.chunks.set_ref_title(topic_id, b.title, source="agent")
            changed = True
        if handler._body_text(ref) != body:
            handler.edit(id=topic_id, text=body)
            changed = True
        patch: dict[str, Any] = {}
        if meta.get("hook") != b.hook:
            patch["hook"] = b.hook
        if meta.get("order") != b.order:
            patch["order"] = b.order
        if patch:
            store.update_ref(topic_id, meta_patch=patch)
            changed = True
        want = f"{SECTION_TAG_PREFIX}{sec.slug}"
        have = sorted(
            v
            for v in tag_values.get(topic_id, set())
            if v.startswith(SECTION_TAG_PREFIX) and v != SECTION_INDEX_TAG
        )
        if have != [want]:
            handler.tag(
                id=topic_id,
                add=[want] if want not in have else None,
                remove=[v for v in have if v != want] or None,
            )
            changed = True
        for link in store.links_for(topic_id, direction="out", relation="part-of"):
            if link.dst_ref_id != sec_id and link.dst_ref_id in old_section_ids:
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
            store.add_link(src_ref_id=topic_id, dst_ref_id=sec_id, relation="part-of")

    if sync:
        index_slugs = {b.slug for s in sections for b in s.bullets}
        index_sections = {s.slug for s in sections}
        for ref in live:
            meta = ref.meta or {}
            slug, section = meta.get("slug"), meta.get("section")
            if slug:
                gone = str(slug) not in index_slugs
            elif section:
                gone = str(section) not in index_sections
            else:
                continue  # a native write — never touched
            if gone:
                handler.delete(id=ref.id)
                report.retired += 1
                if slug:
                    topic_ids.pop(str(slug), None)

    # Cross-links last: a topic may cite one that comes later in the index.
    for src_slug, body in topic_bodies.items():
        for dst_slug in _link_targets(body):
            if dst_slug == src_slug:
                continue
            dst_id = topic_ids.get(dst_slug)
            if dst_id is None:
                report.unresolved_links.append((src_slug, dst_slug))
                continue
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


def _bullet_line(ref: Any, *, cut_hooks: bool) -> str:
    from precis.utils import handle_registry

    handle = handle_registry.try_format("memory", ref.id) or str(ref.id)
    hook = str((ref.meta or {}).get("hook") or "")
    if cut_hooks and len(hook) > HOOK_CUT_CHARS:
        hook = hook[: HOOK_CUT_CHARS - 1].rstrip() + "…"
    line = f"- {ref.title} ({handle})"
    return f"{line} — {hook}" if hook else line


def _render(sections: list[tuple[str, list[Any]]], *, cut_hooks: bool) -> str:
    out = [INDEX_TITLE]
    for title, nodes in sections:
        out.append("")
        out.append(f"## {title}")
        out.append("")
        out.extend(_bullet_line(n, cut_hooks=cut_hooks) for n in nodes)
    return "\n".join(out) + "\n"


def render_memory_index(store: Store, budget_tok: int | None = None) -> str:
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
    """
    refs = _live_repo_dev_nodes(store)
    tags = store.ref_tags_bulk([r.id for r in refs])

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
        slug = str((sec.meta or {}).get("section") or _slugify(sec.title))
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
    idx.add_argument("--database-url", default=None, help="Postgres DSN override.")
    return mp


def run(args: argparse.Namespace) -> None:
    """Implements ``precis memory import`` / ``precis memory index``."""
    from precis.config import load_config

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    store = Store.connect(dsn)
    try:
        if args.memory_cmd == "import":
            report = import_memory_dir(store, Path(args.dir), sync=args.sync)
            print(report.summary())
        else:
            print(render_memory_index(store, args.budget_tok), end="")
    finally:
        store.close()


__all__ = [
    "ImportReport",
    "add_parser",
    "import_memory_dir",
    "render_memory_index",
    "run",
]

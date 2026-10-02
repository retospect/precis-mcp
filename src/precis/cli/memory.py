"""``precis memory import | index`` — harness memory as ``SPACE:repo-dev`` nodes.

The write and load halves of ``docs/backlog/memory-native-authoring.md``:

- ``precis memory import <dir>`` seeds the graph from a harness memory
  directory: ``<dir>/MEMORY.md`` plus the topic files its bullets name.
  Each ``## Section`` header becomes a *section node*, each
  ``- [Title](file.md) — hook`` bullet a *topic node*; ``[X](other.md)`` /
  ``[[other]]`` references in a topic body become ``related-to`` links.
  Idempotent on ``meta.slug`` (topic) / ``meta.section`` (section); an
  existing node is never overwritten — graph-side edits win.
- ``precis memory index [--budget-tok N]`` renders the index back out as
  the text ``MEMORY.md`` used to supply, for
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

    def summary(self) -> str:
        return (
            f"sections: {self.sections_created} created, "
            f"{self.sections_existing} existing; "
            f"topics: {self.topics_created} created, "
            f"{self.topics_existing} existing; "
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


def import_memory_dir(store: Store, path: Path | str) -> ImportReport:
    """Seed ``SPACE:repo-dev`` memory nodes from a harness memory directory.

    Reads ``<path>/MEMORY.md`` and the topic files its bullets name. Safe to
    re-run: a node whose ``meta.slug`` / ``meta.section`` already exists
    among live ``SPACE:repo-dev`` memories is left untouched, links are
    re-added idempotently. Raises ``FileNotFoundError`` when ``MEMORY.md``
    is absent.
    """
    from precis.dispatch import Hub
    from precis.handlers.memory import MemoryHandler

    root = Path(path)
    index_text = (root / "MEMORY.md").read_text(encoding="utf-8")
    sections = parse_index(index_text)
    handler = MemoryHandler(hub=Hub(store=store))
    report = ImportReport()

    section_ids: dict[str, int] = {}
    topic_ids: dict[str, int] = {}
    # ``put`` and the meta patch are two writes; a run killed between them
    # leaves a node with no ``meta.slug``/``meta.section``. Such orphans are
    # matched by title on the next run and patched instead of duplicated.
    orphans: dict[str, int] = {}
    for ref in _live_repo_dev_nodes(store):
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
            store.add_link(src_ref_id=topic_id, dst_ref_id=sec_id, relation="part-of")

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

    meta = ref.meta or {}
    slug = meta.get("slug")
    if not slug:
        handle = handle_registry.try_format("memory", ref.id) or str(ref.id)
        return f"- {ref.title} ({handle})"
    hook = str(meta.get("hook") or "")
    if cut_hooks and len(hook) > HOOK_CUT_CHARS:
        hook = hook[: HOOK_CUT_CHARS - 1].rstrip() + "…"
    line = f"- [{ref.title}]({slug}.md)"
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
    """Render the ``SPACE:repo-dev`` memory index as ``MEMORY.md``-shaped text.

    Sections by ``meta.order``, then each section's topic nodes by
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
        help="Seed memory nodes from <dir>/MEMORY.md (idempotent).",
    )
    imp.add_argument("dir", help="Harness memory directory (holds MEMORY.md).")
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
            report = import_memory_dir(store, Path(args.dir))
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

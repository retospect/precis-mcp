"""``precis mirror-links`` — links between the files of mirrored roots.

``PRECIS_ROOTS`` mounts skills and the harness memory as read-only
``markdown`` refs (``alias/<slug>``); this pass gives them graph edges so a
walk or a fisheye reaches them (``docs/backlog/file-mirror.md`` in-scope 3):

- every mirror: ``related-to`` from a relative ``[Title](slug.md)`` link or
  a ``[[slug]]`` to a sibling file; an index bullet in ``MEMORY.md`` is
  also ``part-of`` the ``##`` section chunk it sits under.
- alias ``skills``: ``part-of`` the skill's parent (:func:`skill_parents`),
  ``related-to`` its ``[[precis-…]]`` cross-refs, the ``precis-<kind>-help``
  of each ``kinds:`` entry and the ``precis-<verb>-help`` of each verb in
  ``applies-to``.

Every minted link carries ``meta.source='mirror'``; a re-run adds what the
files say and removes only mirror links they no longer say. Links an agent
made are never touched. The files themselves are never written.
"""

from __future__ import annotations

import argparse
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from precis.cli.memory_mirror import _MARKDOWN, _WIKI
from precis.store import Ref, Store
from precis.utils.md_parse import file_slug_from_path
from precis.utils.skill_common import SkillFrontmatter, parse_frontmatter

if TYPE_CHECKING:
    from precis.handlers.markdown import MarkdownHandler

log = logging.getLogger(__name__)

#: ``links.meta.source`` stamp on every link this pass owns.
SOURCE = "mirror"
_KIND = "markdown"
_RELATIONS = ("related-to", "part-of")
_ROOT_SKILL = "precis-overview"
_INDEX_FILE = "MEMORY.md"
_VERBS = ("get", "search", "put", "edit", "delete", "tag", "link")
_SECTION = re.compile(r"^##\s+(.+?)\s*$")

#: ``(src slug, relation, dst slug, dst section heading or None)``.
Edge = tuple[str, str, str, str | None]


@dataclass
class MirrorLinkReport:
    files: int = 0
    added: int = 0
    removed: int = 0

    def render(self) -> str:
        return (
            f"mirror-links: {self.files} files, "
            f"{self.added} links added, {self.removed} removed"
        )


def _kind_help(slug: str, fm: SkillFrontmatter) -> bool:
    """``precis-<kind>-help`` for one of the skill's own ``kinds:``."""
    return any(slug == f"precis-{k}-help" for k in fm.kinds or ())


def skill_parents(fms: Mapping[str, SkillFrontmatter]) -> dict[str, str]:
    """``{skill: parent}`` for the skill tree; the root has no entry.

    ``parent:`` frontmatter wins. Otherwise a ``precis-<kind>-help`` skill
    is ``part-of`` ``precis-overview``; any other skill is ``part-of`` the
    ``precis-<kind>-help`` of its first ``kinds:`` entry that exists, else
    ``precis-overview``. A ``parent:`` naming no skill falls back to the
    rule; a ``parent:`` cycle is cut by re-rooting one member at
    ``precis-overview``.
    """
    out: dict[str, str] = {}
    for slug, fm in fms.items():
        if slug == _ROOT_SKILL:
            continue
        parent = (fm.parent or "").strip()
        if parent not in fms or parent == slug:
            parent = ""
        if not parent and not _kind_help(slug, fm):
            parent = next(
                (
                    f"precis-{k}-help"
                    for k in fm.kinds or ()
                    if f"precis-{k}-help" in fms and f"precis-{k}-help" != slug
                ),
                "",
            )
        parent = parent or _ROOT_SKILL
        if parent in fms and parent != slug:
            out[slug] = parent
    for slug in list(out):
        seen = {slug}
        node = out.get(slug)
        while node is not None and node not in seen:
            seen.add(node)
            node = out.get(node)
        if node is not None and _ROOT_SKILL in fms:
            out[slug] = _ROOT_SKILL
    return out


def _link_targets(body: str) -> list[str]:
    """Relative ``.md`` link / ``[[slug]]`` targets in document order."""
    out: list[str] = []
    for raw in (*_MARKDOWN.findall(body), *_WIKI.findall(body)):
        name = raw.strip()
        if "://" in name or not name:
            continue
        out.append(name if name.endswith(".md") else f"{name}.md")
    return out


def _sibling(alias: str, src: Path, root: Path, name: str) -> str | None:
    target = (src.parent / name).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    try:
        return f"{alias}/{file_slug_from_path(str(target.relative_to(root)))}"
    except ValueError:
        return None


def _memory_edges(alias: str, root: Path, files: Mapping[str, Path]) -> set[Edge]:
    edges: set[Edge] = set()
    for slug, path in files.items():
        is_index = path.name == _INDEX_FILE and path.parent == root
        section: str | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _SECTION.match(line)
            if m:
                section = m.group(1)
            for name in _link_targets(line):
                dst = _sibling(alias, path, root, name)
                if dst is None or dst == slug:
                    continue
                edges.add((slug, "related-to", dst, None))
                if is_index and section is not None:
                    edges.add((dst, "part-of", slug, section))
    return edges


def _skill_edges(alias: str, files: Mapping[str, Path]) -> set[Edge]:
    # Skill ids are file stems (``personas/x.md`` is skill ``x``), the
    # same keys the skill handler's file walk uses.
    by_stem: dict[str, str] = {}
    for slug, path in sorted(files.items()):
        if path.stem in by_stem:
            log.warning(
                "mirror-links: skill %r in %s and %s - %s wins",
                path.stem,
                by_stem[path.stem],
                slug,
                slug,
            )
        by_stem[path.stem] = slug
    texts = {
        stem: files[slug].read_text(encoding="utf-8") for stem, slug in by_stem.items()
    }
    fms = {stem: parse_frontmatter(text) for stem, text in texts.items()}

    def ref(skill: str) -> str:
        return by_stem[skill]

    parents = skill_parents(fms)
    edges: set[Edge] = {
        (ref(child), "part-of", ref(parent), None) for child, parent in parents.items()
    }
    for skill, fm in fms.items():
        parent = parents.get(skill)
        wanted = [w for w in _WIKI.findall(texts[skill]) if w.startswith("precis-")]
        wanted += [f"precis-{k}-help" for k in fm.kinds or ()]
        applies = (fm.applies_to or "").lower()
        wanted += [
            f"precis-{v}-help" for v in _VERBS if re.search(rf"\b{v}\(", applies)
        ]
        for other in dict.fromkeys(w.strip() for w in wanted):
            if other in fms and other not in (skill, parent):
                edges.add((ref(skill), "related-to", ref(other), None))
    return edges


def skill_tree_lines(parents: Mapping[str, str]) -> list[str]:
    """The skill index as ``child -> parent`` lines, sorted — the shape
    both the file walk and the mirrored graph render to (test 4b)."""
    return sorted(f"{child} -> {parent}" for child, parent in parents.items())


def graph_skill_parents(store: Store, *, alias: str = "skills") -> dict[str, str]:
    """``{skill: parent}`` read back from ``markdown:<alias>/*`` refs and
    their ``part-of`` links; skills are keyed by file stem."""
    refs = _mirrored_refs(store, alias)
    stem = {r.id: Path(str((r.meta or {}).get("path", r.slug))).stem for r in refs}
    out: dict[str, str] = {}
    for r in refs:
        for link in store.links_for(r.id, direction="out", relation="part-of"):
            if link.src_ref_id == r.id and link.dst_ref_id in stem:
                out[stem[r.id]] = stem[link.dst_ref_id]
    return out


def _mirrored_refs(store: Store, alias: str) -> list[Ref]:
    """Live ``markdown:<alias>/*`` refs, paged in id order."""
    refs: list[Ref] = []
    while page := store.list_refs(
        kind=_KIND,
        tags=[f"mirror:{alias}"],
        order_by="id_asc",
        limit=100,
        offset=len(refs),
    ):
        refs.extend(page)
    return [r for r in refs if (r.slug or "").startswith(f"{alias}/")]


def _section_pos(store: Store, ref_id: int, heading: str) -> int | None:
    for chunk in store.chunks.list_chunks_for_ref(ref_id):
        first = chunk.text.strip().splitlines()[0] if chunk.text.strip() else ""
        m = _SECTION.match(first)
        if m and m.group(1) == heading:
            return chunk.ord
    return None


def sync_mirror_links(
    store: Store, handler: MarkdownHandler, *, dry_run: bool = False
) -> MirrorLinkReport:
    """Converge every mirrored root's ``meta.source='mirror'`` links on
    what its files say. ``dry_run`` ingests nothing and writes nothing;
    a not-yet-ingested file counts all its edges as adds."""
    report = MirrorLinkReport()
    for alias, mirror in handler.mirrors.items():
        files = handler.mirror_files(alias)
        report.files += len(files)
        edges = (
            _skill_edges(alias, files)
            if alias == "skills"
            else _memory_edges(alias, mirror.path, files)
        )
        if not dry_run:
            for slug in files:
                handler.ensure_ingested(slug)
            # A file gone from the root: ensure_ingested retires its ref.
            for gone in _mirrored_refs(store, alias):
                if gone.slug not in files:
                    handler.ensure_ingested(str(gone.slug))
        by_src: dict[str, list[Edge]] = {slug: [] for slug in files}
        for edge in edges:
            by_src.setdefault(edge[0], []).append(edge)
        for slug, src_edges in sorted(by_src.items()):
            _converge(store, slug, src_edges, report=report, dry_run=dry_run)
    return report


def _converge(
    store: Store,
    slug: str,
    edges: list[Edge],
    *,
    report: MirrorLinkReport,
    dry_run: bool,
) -> None:
    src = store.get_ref(kind=_KIND, id=slug)
    if src is None:
        if dry_run:
            report.added += len(edges)
        return
    want: set[tuple[str, int, int | None]] = set()
    for _src, relation, dst_slug, heading in edges:
        dst = store.get_ref(kind=_KIND, id=dst_slug)
        if dst is None:
            report.added += int(dry_run)
            continue
        pos = None if heading is None else _section_pos(store, dst.id, heading)
        if heading is not None and pos is None:
            continue
        want.add((relation, dst.id, pos))
    present: set[tuple[str, int, int | None]] = set()
    owned: set[tuple[str, int, int | None]] = set()
    for link in store.links_for(src.id, direction="out"):
        if link.src_ref_id != src.id or link.src_ord is not None:
            continue
        if link.relation not in _RELATIONS:
            continue
        key = (link.relation, link.dst_ref_id, link.dst_ord)
        present.add(key)
        if (link.meta or {}).get("source") == SOURCE:
            owned.add(key)
    for relation, dst_id, pos in sorted(want - present, key=str):
        report.added += 1
        if not dry_run:
            store.add_link(
                src_ref_id=src.id,
                dst_ref_id=dst_id,
                relation=relation,
                dst_pos=pos,
                set_by="system",
                meta={"source": SOURCE},
            )
    for relation, dst_id, pos in sorted(owned - want, key=str):
        report.removed += 1
        if not dry_run:
            store.remove_link(
                src_ref_id=src.id,
                dst_ref_id=dst_id,
                relation=relation,
                dst_pos=pos,
            )


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = sub.add_parser(
        "mirror-links",
        help="Mint related-to/part-of links between files of PRECIS_ROOTS mirrors.",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="Count; ingest and write nothing."
    )
    return p


def run(args: argparse.Namespace) -> None:
    """Implements ``precis mirror-links``."""
    from precis.config import load_config
    from precis.runtime.factory import build_runtime

    runtime = build_runtime(load_config())
    handler = runtime.hub.handlers.get(_KIND)
    store = runtime.hub.store
    if store is None or handler is None or not getattr(handler, "mirrors", None):
        raise SystemExit("precis mirror-links: no PRECIS_ROOTS mirrors configured")
    print(sync_mirror_links(store, handler, dry_run=args.dry_run).render())


__all__ = [
    "SOURCE",
    "MirrorLinkReport",
    "add_parser",
    "graph_skill_parents",
    "run",
    "skill_parents",
    "skill_tree_lines",
    "sync_mirror_links",
]

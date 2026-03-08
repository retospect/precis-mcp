"""Tool implementations: activate, toc, get, put, move.

Precis generation uses RAKE keyword extraction — no LLM, no sidecar.
Every call parses fresh from disk and generates precis inline.
"""

from __future__ import annotations

import logging
from pathlib import Path

from precis.config import PrecisConfig
from precis.grep import parse_grep
from precis.nodes import Node
from precis.parser import get_parser
from precis.rake import telegram_precis

log = logging.getLogger(__name__)


class Session:
    """Session state: active file path only."""

    def __init__(self):
        self.active_file: str | None = None
        self.config = PrecisConfig.load()

    def require_active(self) -> str:
        if not self.active_file:
            raise PrecisError(
                'no active file\nCall activate("path/to/file.docx") first.'
            )
        return self.active_file


class PrecisError(Exception):
    """Error that formats as !! ERROR for the LLM."""

    def format(self) -> str:
        return f"!! ERROR {self}"


# ─── Shared helpers ──────────────────────────────────────────────────


def _load_nodes(file_path: str) -> list[Node]:
    """Parse document fresh from disk and generate RAKE precis."""
    path = Path(file_path)
    parser = get_parser(file_path)
    nodes = parser.parse(path)
    _apply_precis(nodes)
    return nodes


def _apply_precis(nodes: list[Node]) -> None:
    """Generate RAKE precis for content nodes that don't already have one."""
    for node in nodes:
        if node.precis:
            continue
        if node.node_type in ("p", "t", "f", "e"):
            node.precis = telegram_precis(node.text)


def _build_index(nodes: list[Node]) -> dict[str, Node]:
    """Build slug→node and path→node and label→node index."""
    index: dict[str, Node] = {}
    for n in nodes:
        index[n.slug] = n
        index[str(n.path)] = n
        if n.label:
            index[n.label] = n
    return index


def _resolve_id(id_str: str, index: dict[str, Node]) -> Node:
    """Resolve a single id (slug, path, or label) to a Node."""
    node = index.get(id_str)
    if node is None:
        raise PrecisError(
            f"slug {id_str} not found\n"
            "The document may have changed since you last read it.\n"
            "Run toc() to refresh node slugs."
        )
    return node


def _resolve_ids(id_str: str, index: dict[str, Node]) -> list[Node]:
    """Resolve comma-separated ids to a list of Nodes."""
    parts = [p.strip() for p in id_str.split(",") if p.strip()]
    return [_resolve_id(p, index) for p in parts]


def _heading_level_from_text(text: str) -> int:
    """Detect # prefix for heading level."""
    level = 0
    for ch in text:
        if ch == "#":
            level += 1
        else:
            break
    return min(level, 4)


# ─── Tool implementations ───────────────────────────────────────────


async def activate(session: Session, file: str, progress_cb=None) -> str:
    """Open or switch active document."""
    path = Path(file)

    # Create if missing
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if file.endswith(".docx"):
            from docx import Document

            doc = Document()
            doc.save(str(path))
        elif file.endswith(".tex"):
            path.write_text(
                "\\documentclass{article}\n\\begin{document}\n\n\\end{document}\n",
                encoding="utf-8",
            )
        else:
            raise PrecisError(f"Unsupported format: {file}\nUse .docx or .tex")

        session.active_file = file
        return f"📄 {path.name}  (created, 0 nodes)"

    session.active_file = file
    nodes = _load_nodes(file)

    # Format toc output
    header = f"📄 {path.name}  ({len(nodes)} nodes)"
    if file.endswith(".tex"):
        parser = get_parser(file)
        files = parser.source_files(path)
        file_names = [f.name for f in files]
        header += f"  [{len(files)} files: {', '.join(file_names)}]"

    lines = [header, ""]
    for node in nodes:
        lines.append(node.toc_line())

    return "\n".join(lines)


async def toc(session: Session, scope: str = "", grep: str = "") -> str:
    """Navigate and search the active document."""
    file_path = session.require_active()
    path = Path(file_path)
    nodes = _load_nodes(file_path)

    # Filter by scope
    if scope:
        nodes = [n for n in nodes if str(n.path).startswith(scope)]

    # Grep mode
    if grep:
        pattern = parse_grep(grep)
        hits = []
        for n in nodes:
            if pattern.matches(n.text) or pattern.matches(n.precis):
                hits.append(n)

        header = f"📄 {path.name}  grep: {grep}  ({len(hits)} hits)"
        lines = [header, ""]
        for h in hits:
            lines.append(h.grep_line())
        return "\n".join(lines)

    # Normal toc
    header = f"📄 {path.name}"
    lines = [header, ""]
    for node in nodes:
        lines.append(node.toc_line())
    return "\n".join(lines)


async def get(session: Session, id: str) -> str:
    """Read full content by id."""
    file_path = session.require_active()
    nodes = _load_nodes(file_path)
    index = _build_index(nodes)

    parts = [p.strip() for p in id.split(",") if p.strip()]
    output_lines: list[str] = []

    for part in parts:
        node = _resolve_id(part, index)

        if node.path.is_heading():
            # Return heading + all children
            section_nodes = []
            for n in nodes:
                if n.slug == node.slug or n.path.is_child_of(node.path):
                    if n not in section_nodes:
                        section_nodes.append(n)
            for n in section_nodes:
                output_lines.append(n.meta_line())
                if n.node_type != "h":
                    output_lines.append(n.text)
        else:
            output_lines.append(node.meta_line())
            output_lines.append(node.text)

    return "\n".join(output_lines)


async def put(
    session: Session,
    id: str = "",
    text: str = "",
    mode: str = "replace",
    tracked: bool = True,
) -> str:
    """Mutate the document."""
    file_path = session.require_active()
    path = Path(file_path)

    # Validate single paragraph
    if text and "\n\n" in text:
        raise PrecisError(
            "text contains multiple paragraphs\n"
            "put() accepts one paragraph at a time. Split your text and\n"
            "call put() once per paragraph."
        )

    valid_modes = {"replace", "after", "before", "delete", "append"}
    if mode not in valid_modes:
        raise PrecisError(
            f"invalid mode: {mode}\nValid modes: {', '.join(valid_modes)}"
        )

    if mode != "append" and not id:
        raise PrecisError(f"id required for mode={mode}")

    parser = get_parser(file_path)
    nodes = _load_nodes(file_path)
    index = _build_index(nodes)

    heading_level = _heading_level_from_text(text) if text else 0
    clean_text = text.lstrip("#").strip() if heading_level else text

    if mode == "append":
        parser.append_node(path, clean_text, heading_level)
        new_nodes = _load_nodes(file_path)
        new_node = new_nodes[-1] if new_nodes else None
        if new_node:
            precis_display = new_node.precis or clean_text
            return f"+ {new_node.slug}  {new_node.path}  {precis_display}"
        return "+ appended"

    node = _resolve_id(id, index)

    if mode == "delete":
        parser.delete_node(path, node)
        return f"- {node.slug}  {node.path}  deleted"

    if mode == "replace":
        if not text:
            raise PrecisError("text required for replace mode")

        if tracked and file_path.endswith(".docx"):
            from precis.parser.docx import DocxParser

            if isinstance(parser, DocxParser):
                parser.write_tracked(path, node, clean_text, session.config.author)
            else:
                parser.write_node(path, node, clean_text)
        else:
            parser.write_node(path, node, clean_text)

        new_nodes = _load_nodes(file_path)
        new_node = None
        for nn in new_nodes:
            if str(nn.path) == str(node.path):
                new_node = nn
                break

        if new_node:
            tracked_label = "tracked" if tracked and file_path.endswith(".docx") else ""
            precis_display = new_node.precis or clean_text
            return f"{node.slug} → {new_node.slug}  {node.path}  {tracked_label}  replace\n{precis_display}"

        return f"{node.slug} → ???  {node.path}  replace"

    if mode in ("after", "before"):
        if not text:
            raise PrecisError(f"text required for {mode} mode")

        if mode == "after":
            parser.insert_after(path, node, clean_text, heading_level)
        else:
            parser.insert_before(path, node, clean_text, heading_level)

        new_nodes = _load_nodes(file_path)
        new_node = None
        old_slugs = {n.slug for n in nodes}
        for nn in new_nodes:
            if nn.slug not in old_slugs:
                new_node = nn
                break

        if new_node:
            tracked_label = "tracked" if tracked and file_path.endswith(".docx") else ""
            precis_display = new_node.precis or clean_text
            return f"+ {new_node.slug}  {new_node.path}  {mode} {node.slug}  {tracked_label}\n{precis_display}"

        return f"+ ???  {mode} {node.slug}"

    raise PrecisError(f"unhandled mode: {mode}")


async def move(session: Session, id: str, after: str) -> str:
    """Reorder nodes within the document."""
    file_path = session.require_active()
    path = Path(file_path)
    parser = get_parser(file_path)
    nodes = _load_nodes(file_path)
    index = _build_index(nodes)

    move_nodes_list = _resolve_ids(id, index)
    after_node = _resolve_id(after, index)

    parser.move_nodes(path, move_nodes_list, after_node)

    new_nodes = _load_nodes(file_path)
    new_index = _build_index(new_nodes)

    lines = []
    for mn in move_nodes_list:
        new_node = new_index.get(mn.slug)
        if new_node:
            lines.append(f"moved {mn.slug} {mn.path} → {new_node.path}")
        else:
            lines.append(f"moved {mn.slug} {mn.path} → ???")

    return "\n".join(lines)

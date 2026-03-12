"""Tool implementations: activate, toc, get, put, move.

Precis generation uses RAKE keyword extraction — no LLM, no sidecar.
Every call parses fresh from disk and generates precis inline.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from precis.citations import BIB_DEF_RE, CITE_RE
from precis.config import PrecisConfig
from precis.grep import parse_grep
from precis.nodes import Node
from precis.parser import get_parser
from precis.rake import telegram_precis

log = logging.getLogger(__name__)

# ─── Raw file access ─────────────────────────────────────────────────

ALLOWED_EXT = {
    ".tex",
    ".bib",
    ".sty",
    ".cls",
    ".bst",
    ".txt",
    ".md",
    ".csv",
    ".tikz",
    ".pgf",
}

# Match: path/to/file.ext or path/to/file.ext:start or path/to/file.ext:start..end
_RAW_FILE_RE = re.compile(r"^([\w./ \-]+\.[a-zA-Z]+)(?::(\d+|\$)(?:\.\.(\d+)?)?)?$")


def _parse_raw_file_id(id_str: str) -> tuple[str, int | None, int | None, bool] | None:
    """Parse a raw file reference. Returns (rel_path, start, end, is_append) or None."""
    m = _RAW_FILE_RE.match(id_str)
    if not m:
        return None
    rel_path = m.group(1)
    ext = Path(rel_path).suffix.lower()
    if ext not in ALLOWED_EXT:
        return None
    raw_start = m.group(2)
    raw_end = m.group(3)
    is_append = raw_start == "$"
    start = int(raw_start) if raw_start and raw_start != "$" else None
    end = int(raw_end) if raw_end else None
    return (rel_path, start, end, is_append)


def _project_root(session_file: str) -> Path:
    """Get project root directory from active file path."""
    return Path(session_file).parent


def _resolve_raw_path(session_file: str, rel_path: str) -> Path:
    """Resolve a relative path within the project, with sandbox check."""
    root = _project_root(session_file)
    resolved = (root / rel_path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise PrecisError(
            f"path '{rel_path}' escapes project directory\n"
            f"All file paths must be within {root}"
        )
    return resolved


def _raw_read(
    session_file: str, rel_path: str, start: int | None, end: int | None
) -> str:
    """Read raw lines from a file, with optional range."""
    full_path = _resolve_raw_path(session_file, rel_path)
    if not full_path.exists():
        raise PrecisError(f"file not found: {rel_path}")
    lines = full_path.read_text(encoding="utf-8").splitlines()
    total = len(lines)
    if start is not None:
        s = max(1, start) - 1  # 1-indexed → 0-indexed
        e = min(total, end) if end is not None else total
        selected = lines[s:e]
        header = f">> {rel_path}:{start}..{end or total}  ({e - s} of {total} lines)"
    else:
        selected = lines
        header = f">> {rel_path}  ({total} lines)"
    numbered = [f"{i + (start or 1):4d}: {line}" for i, line in enumerate(selected)]
    return header + "\n" + "\n".join(numbered)


def _raw_write(
    session_file: str,
    rel_path: str,
    text: str,
    start: int | None,
    end: int | None,
    is_append: bool,
) -> str:
    """Write raw lines to a file (replace range or append)."""
    full_path = _resolve_raw_path(session_file, rel_path)
    new_lines = text.splitlines(keepends=True)
    # Ensure final newline
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"

    if is_append:
        if not full_path.exists():
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text("", encoding="utf-8")
        with open(full_path, "a", encoding="utf-8") as f:
            f.writelines(new_lines)
        total = len(full_path.read_text(encoding="utf-8").splitlines())
        return f"+ {rel_path}  appended {len(new_lines)} lines (now {total} total)"

    if not full_path.exists():
        raise PrecisError(f"file not found: {rel_path}")

    existing = full_path.read_text(encoding="utf-8").splitlines(keepends=True)
    if start is not None:
        s = max(1, start) - 1
        e = min(len(existing), end) if end is not None else len(existing)
        existing[s:e] = new_lines
    else:
        existing = new_lines  # replace whole file

    full_path.write_text("".join(existing), encoding="utf-8")
    total = len(full_path.read_text(encoding="utf-8").splitlines())
    if start is not None:
        return f"{rel_path}:{start}..{end or ''}  replaced → {len(new_lines)} lines (now {total} total)"
    return f"{rel_path}  replaced whole file → {total} lines"


def _citation_hints(file_path: str) -> str:
    """Scan document for undefined [@key] citations and return hint text."""
    nodes = _load_nodes(file_path)
    inline_keys: set[str] = set()
    defined_keys: set[str] = set()
    for node in nodes:
        text = node.text or ""
        # Inline citations from text (survives DOCX round-trip via hyperlink→[@key])
        for m in CITE_RE.finditer(text):
            inline_keys.add(m.group(1))
        # Bib definitions: b-type nodes have label=key from ref_ bookmark
        if node.node_type == "b" and node.label:
            defined_keys.add(node.label)
        # Also check raw text for [@key]: pattern (LaTeX, new docs)
        for m in BIB_DEF_RE.finditer(text):
            defined_keys.add(m.group(1))
    undefined = sorted(inline_keys - defined_keys)
    if not undefined:
        return ""
    cite_list = " ".join(f"[@{k}]" for k in undefined)
    lines = [
        f"\n⚠ {len(undefined)} undefined citation(s): {cite_list}",
        "You MUST define each one before finishing. Steps:",
        "  1. Look up each slug: paper(id='slug:<key>/meta') to get author, title, journal, year",
        "  2. Append a References heading (if not already present): put(text='## References', mode='append')",
        "  3. Append each entry: put(text='[@key]: Author, A. B. (Year). Title. *Journal*, vol, pages.', mode='append')",
    ]
    return "\n".join(lines)


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
        # Detect common LLM mistake: passing heading text instead of slug
        if id_str.startswith("#") or len(id_str) > 10 or " " in id_str:
            slugs = [k for k in index if not k.startswith("S") and "." not in k]
            slug_list = ", ".join(slugs[:8])
            raise PrecisError(
                f"'{id_str}' is not a valid SLUG.\n"
                "The id parameter must be a short SLUG from toc(), not heading text.\n"
                f"Available slugs: {slug_list}\n"
                "Run toc() to see all slugs. For append mode, use text= not id=."
            )
        raise PrecisError(
            f"slug '{id_str}' not found\n"
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
    stripped = text.lstrip()
    level = 0
    for ch in stripped:
        if ch == "#":
            level += 1
        else:
            break
    return min(level, 4)


_SECTION_NUM_RE = re.compile(r"^[\d]+(?:\.[\d]+)*\.?\s+")


def _strip_heading_numbering(text: str) -> str:
    """Strip leading section numbers from heading text.

    Two mechanisms:
      1. Explicit ``|`` separator: ``## 3.3 | Foo`` → ``Foo``
      2. Regex fallback: ``## 3.3 Foo`` → ``Foo``

    LLMs often include numbering despite instructions not to.
    Word auto-numbers headings, so these must be removed.
    """
    if "|" in text:
        return text.split("|", 1)[1].strip()
    return _SECTION_NUM_RE.sub("", text)


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
    lines = [header]

    # LaTeX: show file listing with line counts
    if file.endswith(".tex"):
        parser = get_parser(file)
        source_files = parser.source_files(path)
        lines.append(f"  📁 {path.parent.name}/  ({len(source_files)} files)")
        for sf in source_files:
            try:
                lc = len(sf.read_text(encoding="utf-8").splitlines())
            except Exception:
                lc = 0
            tag = "  [root]" if sf.name == path.name else ""
            rel = (
                sf.relative_to(path.parent)
                if sf.is_relative_to(path.parent)
                else sf.name
            )
            lines.append(f"    {rel}{tag}  {lc} lines")

        # Label hint
        labels = [n.label for n in nodes if n.label]
        if labels:
            example = labels[0]
            lines.append(
                f"Hint: \\label{{}} values work as IDs in get/put (e.g. '{example}')"
            )
        # Raw file hint
        lines.append(
            "Hint: get(id='file.tex:1..50') for raw source, "
            "put(id='file.tex:10..20', text='...') to edit"
        )

    lines.append("")
    for node in nodes:
        lines.append(node.toc_line())

    return "\n".join(lines)


_LARGE_DOC_THRESHOLD = 100


async def toc(session: Session, scope: str = "", grep: str = "", depth: int = 0) -> str:
    """Navigate and search the active document."""
    file_path = session.require_active()
    path = Path(file_path)
    nodes = _load_nodes(file_path)
    total_nodes = len(nodes)

    # Filter by scope (ignore if LLM passed the filename itself)
    if scope and scope.lower() != path.name.lower():
        nodes = [n for n in nodes if str(n.path).startswith(scope)]
    else:
        scope = ""  # clear so header doesn't show it

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

    # Auto-adaptive: large docs default to headings-only
    auto_truncated = False
    effective_depth = depth
    if depth == 0 and len(nodes) > _LARGE_DOC_THRESHOLD and not scope:
        effective_depth = 4  # all headings, no content
        auto_truncated = True

    # Apply depth filter
    if effective_depth > 0:
        nodes = [
            n
            for n in nodes
            if n.node_type == "h" and n.heading_level() <= effective_depth
        ]

    # Normal toc
    header = f"📄 {path.name}"
    if scope:
        header += f"  scope: {scope}"
    if effective_depth > 0:
        header += f"  depth: {effective_depth}"
    header += f"  ({len(nodes)} nodes"
    if len(nodes) != total_nodes:
        header += f" / {total_nodes} total"
    header += ")"

    if not nodes and total_nodes == 0:
        return (
            f"{header}\n\n"
            "The document is empty.\n"
            "Start writing with put(text='# | Introduction', mode='append') to add a heading,\n"
            "or put(text='First paragraph.', mode='append') to add body text."
        )

    legend = "  PATH  SLUG  [source]  #|heading or |precis   — use SLUG as id in put()"
    lines = [header, legend, ""]
    for node in nodes:
        lines.append(node.toc_line())

    if auto_truncated:
        lines.append("")
        lines.append(f"⚠ Large document ({total_nodes} nodes) — showing headings only.")
        lines.append(
            "Drill in: toc(scope='S3.2') for full section, "
            "toc(depth=2) for outline, toc(depth=0, scope='S3') for all detail in §3."
        )

    return "\n".join(lines)


async def get(session: Session, id: str) -> str:
    """Read full content by id."""
    file_path = session.require_active()

    # Redirect common LLM mistakes: 'file.docx#toc', '#toc', 'toc'
    id_lower = id.strip().lower()
    if id_lower.endswith("#toc") or id_lower == "toc":
        return await toc(session)

    # Raw file access: file.tex:start..end
    raw = _parse_raw_file_id(id)
    if raw is not None:
        rel_path, start, end, _is_append = raw
        return _raw_read(file_path, rel_path, start, end)

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
                for c in n.comments:
                    output_lines.append(f"  💬 [{c['author']}] {c['text']}")
        else:
            output_lines.append(node.meta_line())
            output_lines.append(node.text)
            for c in node.comments:
                output_lines.append(f"  💬 [{c['author']}] {c['text']}")

    return "\n".join(output_lines)


async def _put_multi(
    session: Session,
    id: str,
    paragraphs: list[str],
    mode: str,
    tracked: bool,
) -> str:
    """Apply multiple paragraphs sequentially, chaining by slug."""
    file_path = session.require_active()
    path = Path(file_path)
    results = []
    cursor_slug = id  # tracks where to insert next

    for i, para in enumerate(paragraphs):
        heading_level = _heading_level_from_text(para)
        clean = para.lstrip("#").strip() if heading_level else para
        if heading_level:
            clean = _strip_heading_numbering(clean)

        parser = get_parser(file_path)
        nodes = _load_nodes(file_path)

        if mode == "append" or (mode in ("after", "before") and i > 0):
            # First paragraph uses original mode; rest always append after previous
            if i == 0 and mode == "before":
                index = _build_index(nodes)
                node = _resolve_id(cursor_slug, index)
                parser.insert_before(path, node, clean, heading_level)
            elif i == 0 and mode == "after":
                index = _build_index(nodes)
                node = _resolve_id(cursor_slug, index)
                parser.insert_after(path, node, clean, heading_level)
            elif i > 0 and cursor_slug and mode != "append":
                index = _build_index(nodes)
                node = _resolve_id(cursor_slug, index)
                parser.insert_after(path, node, clean, heading_level)
            else:
                parser.append_node(path, clean, heading_level)
        elif mode == "replace" and i == 0:
            index = _build_index(nodes)
            node = _resolve_id(cursor_slug, index)
            if tracked and file_path.endswith(".docx"):
                from precis.parser.docx import DocxParser

                if isinstance(parser, DocxParser):
                    parser.write_tracked(path, node, clean, session.config.author)
                else:
                    parser.write_node(path, node, clean)
            else:
                parser.write_node(path, node, clean)
        elif mode == "replace" and i > 0:
            # After replacing first, insert remaining after it
            index = _build_index(nodes)
            node = _resolve_id(cursor_slug, index)
            parser.insert_after(path, node, clean, heading_level)
        else:
            parser.append_node(path, clean, heading_level)

        # Find the newly created node to chain from
        new_nodes = _load_nodes(file_path)
        old_slugs = {n.slug for n in nodes}
        new_node = None
        if mode == "replace" and i == 0:
            # For replace, find the node at the same path
            for nn in new_nodes:
                if str(nn.path) == str(node.path):
                    new_node = nn
                    break
        else:
            for nn in new_nodes:
                if nn.slug not in old_slugs:
                    new_node = nn
                    break

        if new_node:
            cursor_slug = new_node.slug
            preview = (new_node.precis or clean)[:60]
            results.append(f"+ {new_node.slug}  {new_node.path}  {preview}")
        else:
            results.append(f"+ ???  {clean[:40]}")

    summary = f"Auto-split: {len(paragraphs)} paragraphs written\n"
    summary += "\n".join(results)
    if cursor_slug:
        summary += (
            f"\nHint: put(id='{cursor_slug}', text='...', mode='after') to write more"
        )
    summary += _citation_hints(file_path)
    return summary


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

    # Strip leading/trailing whitespace (LLMs often prefix with \n)
    text = text.strip()

    # Raw file write: file.tex:start..end or file.tex:$
    if id:
        raw = _parse_raw_file_id(id)
        if raw is not None:
            if not text:
                raise PrecisError("text required for raw file write")
            rel_path, start, end, is_append = raw
            return _raw_write(file_path, rel_path, text, start, end, is_append)

    valid_modes = {"replace", "after", "before", "delete", "append", "comment"}
    if mode not in valid_modes:
        raise PrecisError(
            f"invalid mode: {mode}\nValid modes: {', '.join(valid_modes)}"
        )

    # Comment mode — DOCX only, no multi-paragraph
    if mode == "comment":
        if not text:
            raise PrecisError("text required for comment mode")
        if not id:
            raise PrecisError("id required for comment mode")
        if not file_path.endswith(".docx"):
            raise PrecisError("comments only supported for .docx files")

        parser = get_parser(file_path)
        nodes = _load_nodes(file_path)
        index = _build_index(nodes)
        node = _resolve_id(id, index)

        from precis.parser.docx import DocxParser

        if not isinstance(parser, DocxParser):
            raise PrecisError("comments only supported for .docx files")

        comment_id = parser.write_comment(path, node, text, session.config.author)
        return f"💬 {node.slug}  {node.path}  comment #{comment_id}\n" f"{text}"

    # Auto-split: DOCX only. LaTeX handles newlines natively.
    is_docx = file_path.endswith(".docx")
    if is_docx and text and "\n" in text:
        paragraphs = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if len(paragraphs) > 1:
            return await _put_multi(session, id, paragraphs, mode, tracked)

    if mode == "append" and not text:
        hint = ""
        if id:
            hint = (
                f"\nIt looks like you put the content in id= instead of text=.\n"
                f"Use: put(text='{id}', mode='append')"
            )
        raise PrecisError(f"text required for mode=append{hint}")

    if mode != "append" and not id:
        raise PrecisError(f"id required for mode={mode}")

    parser = get_parser(file_path)
    nodes = _load_nodes(file_path)
    index = _build_index(nodes)

    heading_level = _heading_level_from_text(text) if text else 0
    clean_text = text.lstrip("#").strip() if heading_level else text
    if heading_level:
        clean_text = _strip_heading_numbering(clean_text)

    if mode == "append":
        parser.append_node(path, clean_text, heading_level)
        new_nodes = _load_nodes(file_path)
        new_node = new_nodes[-1] if new_nodes else None
        if new_node:
            precis_display = new_node.precis or clean_text
            return (
                f"+ {new_node.slug}  {new_node.path}  {precis_display}\n"
                f"Hint: put(id='{new_node.slug}', text='...', mode='after') to write more"
                + _citation_hints(file_path)
            )
        return "+ appended"

    node = _resolve_id(id, index)

    if mode == "delete":
        parser.delete_node(path, node)
        return (
            f"- {node.slug}  {node.path}  deleted\nHint: toc() to see updated structure"
        )

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
            return (
                f"{node.slug} → {new_node.slug}  {node.path}  {tracked_label}  replace\n"
                f"{precis_display}\n"
                f"Hint: use slug '{new_node.slug}' to reference this node"
                + _citation_hints(file_path)
            )

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
            return (
                f"+ {new_node.slug}  {new_node.path}  {mode} {node.slug}  {tracked_label}\n"
                f"{precis_display}\n"
                f"Hint: put(text='...', mode='after', id='{new_node.slug}') to write more after this node"
                + _citation_hints(file_path)
            )

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

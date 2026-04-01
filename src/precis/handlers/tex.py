"""LaTeX handler — read/write for .tex projects.

Integrates LatexParser from precis v1 with the v2 handler protocol.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from precis.citations import BIB_DEF_RE
from precis.formatting import LIST_ITEM_RE, list_prefix, parse_list_prefix
from precis.handlers._file_base import FileHandlerBase
from precis.protocol import Node, PathCounter, PrecisError, make_slug, resolve_slug

# Sectioning commands → heading level
SECTION_COMMANDS = {
    r"\section": 1,
    r"\subsection": 2,
    r"\subsubsection": 3,
    r"\paragraph": 4,
}

SECTION_RE = re.compile(
    r"^\\(section|subsection|subsubsection|paragraph)\*?\{(.+?)\}",
    re.MULTILINE,
)
ENV_RE = re.compile(r"\\begin\{(\w+)\}")
ENV_END_RE = re.compile(r"\\end\{(\w+)\}")
LABEL_RE = re.compile(r"\\label\{([^}]+)\}")
CAPTION_RE = re.compile(r"\\caption\{([^}]+)\}")
INPUT_RE = re.compile(r"\\(?:input|include)\{([^}]+)\}")
COMMENT_RE = re.compile(r"(?<!\\)%.*$", re.MULTILINE)
BIB_CMD_RE = re.compile(r"\\bibliography\{([^}]+)\}")
ADDBIBRESOURCE_RE = re.compile(r"\\addbibresource\{([^}]+)\}")

TABLE_ENVS = {"table", "tabular", "longtable"}
FIGURE_ENVS = {"figure", "figure*"}
EQUATION_ENVS = {"equation", "equation*", "align", "align*", "gather", "gather*"}
LIST_ENVS = {"itemize", "enumerate"}

ITEM_RE = re.compile(r"\\item\s*(.*)")

# .bib entry start: @type{key,
_BIB_ENTRY_START_RE = re.compile(r"@(\w+)\{([^,\s]+)\s*,")

# Allowed extensions for raw file access
_RAW_ALLOWED_EXT = {
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
_RAW_FILE_RE = re.compile(r"^([\w./ \-]+\.[a-zA-Z]+)(?::(\d+|\$)(?:\.\.(\d+)?)?)?$")


class TexHandler(FileHandlerBase):
    """Handler for .tex files."""

    extensions = {".tex"}

    # ── Parser implementation ───────────────────────────────────────

    def parse(self, path: Path) -> list[Node]:
        """Parse a LaTeX project starting from root .tex file."""
        files = self._resolve_files(path)
        all_nodes: list[Node] = []
        counter = PathCounter()
        slug_counts: dict[str, int] = {}

        for tex_file in files:
            content = tex_file.read_text(encoding="utf-8")
            rel_name = tex_file.name
            nodes = self._parse_file(content, rel_name, counter, slug_counts)
            all_nodes.extend(nodes)

        # Parse .bib file entries as 'b' nodes
        bib_path = self._find_bib_file(path)
        if bib_path and bib_path.exists():
            bib_nodes = _parse_bib_file(bib_path, counter, slug_counts)
            all_nodes.extend(bib_nodes)

        return all_nodes

    def source_files(self, path: Path) -> list[Path]:
        files = self._resolve_files(path)
        bib_path = self._find_bib_file(path)
        if bib_path and bib_path.exists():
            files.append(bib_path)
        return files

    def write_node(self, path: Path, node: Node, new_text: str) -> None:
        tex_file = path.parent / node.source_file
        content = tex_file.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)

        start = node.source_line_start - 1
        end = node.source_line_end
        new_lines = [new_text + "\n"] if not new_text.endswith("\n") else [new_text]
        lines[start:end] = new_lines

        _atomic_write(tex_file, "".join(lines))

    def insert_after(
        self, path: Path, anchor: Node, new_text: str, heading_level: int = 0
    ) -> None:
        tex_file = path.parent / anchor.source_file
        content = tex_file.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)

        if heading_level:
            cmd = _level_to_command(heading_level)
            new_text = f"{cmd}{{{new_text}}}"

        insert_at = anchor.source_line_end
        lines.insert(insert_at, "\n" + new_text + "\n")
        _atomic_write(tex_file, "".join(lines))

    def insert_before(
        self, path: Path, anchor: Node, new_text: str, heading_level: int = 0
    ) -> None:
        tex_file = path.parent / anchor.source_file
        content = tex_file.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)

        if heading_level:
            cmd = _level_to_command(heading_level)
            new_text = f"{cmd}{{{new_text}}}"

        insert_at = anchor.source_line_start - 1
        lines.insert(insert_at, new_text + "\n\n")
        _atomic_write(tex_file, "".join(lines))

    def delete_node(self, path: Path, node: Node) -> None:
        tex_file = path.parent / node.source_file
        content = tex_file.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)

        start = node.source_line_start - 1
        end = node.source_line_end
        del lines[start:end]

        _atomic_write(tex_file, "".join(lines))

    def append_node(self, path: Path, new_text: str, heading_level: int = 0) -> None:
        # Check for bibliography definition: [@key]: reference text
        bib_m = BIB_DEF_RE.match(new_text)
        if bib_m:
            key = bib_m.group(1)
            ref_text = bib_m.group(2)
            bib_path = self._find_bib_file(path)
            if bib_path is None:
                raise PrecisError(
                    "No .bib file found. Add \\bibliography{name} or "
                    "\\addbibresource{name.bib} to your .tex source first."
                )
            _append_bib_entry(bib_path, key, ref_text)
            return

        files = self._resolve_files(path)
        target = files[-1]
        content = target.read_text(encoding="utf-8")

        if heading_level:
            cmd = _level_to_command(heading_level)
            new_text = f"{cmd}{{{new_text}}}"
        elif LIST_ITEM_RE.match(new_text.strip()):
            new_text = _markdown_list_to_latex(new_text)

        end_doc = content.find(r"\end{document}")
        if end_doc >= 0:
            content = content[:end_doc] + "\n" + new_text + "\n\n" + content[end_doc:]
        else:
            content = content.rstrip() + "\n\n" + new_text + "\n"

        _atomic_write(target, content)

    def move_nodes(self, path: Path, nodes: list[Node], after: Node) -> None:
        texts = []
        for n in nodes:
            tex_file = path.parent / n.source_file
            content = tex_file.read_text(encoding="utf-8")
            lines = content.splitlines(keepends=True)
            start = n.source_line_start - 1
            end = n.source_line_end
            texts.append("".join(lines[start:end]))

        for n in reversed(nodes):
            self.delete_node(path, n)

        fresh_nodes = self.parse(path)
        fresh_after = None
        for fn in fresh_nodes:
            if fn.slug == after.slug:
                fresh_after = fn
                break
        if fresh_after is None:
            raise PrecisError(f"Anchor not found after deletion: {after.slug}")

        tex_file = path.parent / fresh_after.source_file
        content = tex_file.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)
        insert_at = fresh_after.source_line_end
        for text in reversed(texts):
            lines.insert(insert_at, "\n" + text)
        _atomic_write(tex_file, "".join(lines))

    # ── Raw file access (TeX-specific) ──────────────────────────────

    def read(
        self,
        path: str,
        selector: str | None,
        view: str | None,
        subview: str | None,
        query: str,
        summarize: bool,
        depth: int,
        page: int,
    ) -> str:
        # Check for raw file access pattern in selector
        if selector:
            raw = _parse_raw_file_id(selector)
            if raw is not None:
                file_path = self._resolve_path(path)
                rel_path, start, end, _is_append = raw
                return _raw_read(file_path, rel_path, start, end)

        # Delegate to base implementation
        return super().read(
            path, selector, view, subview, query, summarize, depth, page
        )

    def put(
        self,
        path: str,
        selector: str | None,
        text: str,
        mode: str,
        **kwargs,
    ) -> str:
        # Check for raw file write in selector
        if selector:
            raw = _parse_raw_file_id(selector)
            if raw is not None:
                file_path = self._resolve_path(path)
                if not text:
                    raise PrecisError("text required for raw file write")
                rel_path, start, end, is_append = raw
                return _raw_write(file_path, rel_path, text, start, end, is_append)

        return super().put(path, selector, text, mode, **kwargs)

    # ── Internal helpers ────────────────────────────────────────────

    def _find_bib_file(self, root: Path) -> Path | None:
        files = self._resolve_files(root)
        for tex_file in files:
            content = tex_file.read_text(encoding="utf-8")
            m = BIB_CMD_RE.search(content)
            if m:
                name = m.group(1).split(",")[0].strip()
                if not name.endswith(".bib"):
                    name += ".bib"
                return root.parent / name
            m = ADDBIBRESOURCE_RE.search(content)
            if m:
                name = m.group(1).strip()
                if not name.endswith(".bib"):
                    name += ".bib"
                return root.parent / name
        return None

    def _resolve_files(self, root: Path) -> list[Path]:
        result: list[Path] = []
        self._collect_files(root, result, set())
        return result

    def _collect_files(self, path: Path, result: list[Path], seen: set[str]) -> None:
        resolved = path.resolve()
        if str(resolved) in seen:
            return
        seen.add(str(resolved))

        if not path.exists():
            return

        result.append(path)
        content = path.read_text(encoding="utf-8")

        for m in INPUT_RE.finditer(content):
            child_name = m.group(1)
            if not child_name.endswith(".tex"):
                child_name += ".tex"
            child_path = path.parent / child_name
            self._collect_files(child_path, result, seen)

    def _parse_file(
        self,
        content: str,
        filename: str,
        counter: PathCounter,
        slug_counts: dict[str, int],
    ) -> list[Node]:
        nodes: list[Node] = []
        lines = content.split("\n")
        i = 0

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            if stripped.startswith("%") or not stripped:
                i += 1
                continue

            sec_match = SECTION_RE.match(stripped)
            if sec_match:
                cmd_name = sec_match.group(1)
                title = sec_match.group(2)
                level = SECTION_COMMANDS.get(f"\\{cmd_name}", 0)
                if level:
                    label = ""
                    end_line = i
                    label_m = LABEL_RE.search(stripped)
                    if label_m:
                        label = label_m.group(1)
                    elif i + 1 < len(lines) and LABEL_RE.match(lines[i + 1].strip()):
                        label_m = LABEL_RE.match(lines[i + 1].strip())
                        if label_m:
                            label = label_m.group(1)
                            end_line = i + 1

                    node_path = counter.next_heading(level)
                    base_slug = make_slug(title)
                    slug = resolve_slug(base_slug, slug_counts)
                    nodes.append(
                        Node(
                            slug=slug,
                            path=node_path,
                            node_type="h",
                            text=title,
                            precis=title,
                            style=f"\\{cmd_name}",
                            source_file=filename,
                            source_line_start=i + 1,
                            source_line_end=end_line + 1,
                            label=label,
                        )
                    )
                    i = end_line + 1
                    continue

            env_match = ENV_RE.match(stripped)
            if env_match:
                env_name = env_match.group(1)

                if env_name in TABLE_ENVS:
                    start_line = i + 1
                    env_content, end_line = self._collect_environment(
                        lines, i, env_name
                    )
                    label = ""
                    label_m = LABEL_RE.search(env_content)
                    if label_m:
                        label = label_m.group(1)

                    synopsis = _table_synopsis_latex(env_content)
                    node_path = counter.next_child("t")
                    base_slug = make_slug(env_content)
                    slug = resolve_slug(base_slug, slug_counts)
                    nodes.append(
                        Node(
                            slug=slug,
                            path=node_path,
                            node_type="t",
                            text=env_content,
                            precis=synopsis,
                            style=f"\\begin{{{env_name}}}",
                            source_file=filename,
                            source_line_start=start_line,
                            source_line_end=end_line,
                            label=label,
                        )
                    )
                    i = end_line
                    continue

                elif env_name in FIGURE_ENVS:
                    start_line = i + 1
                    env_content, end_line = self._collect_environment(
                        lines, i, env_name
                    )
                    label = ""
                    label_m = LABEL_RE.search(env_content)
                    if label_m:
                        label = label_m.group(1)
                    caption = ""
                    cap_m = CAPTION_RE.search(env_content)
                    if cap_m:
                        caption = cap_m.group(1)

                    node_path = counter.next_child("f")
                    base_slug = make_slug(env_content)
                    slug = resolve_slug(base_slug, slug_counts)
                    nodes.append(
                        Node(
                            slug=slug,
                            path=node_path,
                            node_type="f",
                            text=env_content,
                            precis=caption or "[figure]",
                            style=f"\\begin{{{env_name}}}",
                            source_file=filename,
                            source_line_start=start_line,
                            source_line_end=end_line,
                            label=label,
                        )
                    )
                    i = end_line
                    continue

                elif env_name in EQUATION_ENVS:
                    start_line = i + 1
                    env_content, end_line = self._collect_environment(
                        lines, i, env_name
                    )
                    label = ""
                    label_m = LABEL_RE.search(env_content)
                    if label_m:
                        label = label_m.group(1)

                    node_path = counter.next_child("e")
                    base_slug = make_slug(env_content)
                    slug = resolve_slug(base_slug, slug_counts)
                    nodes.append(
                        Node(
                            slug=slug,
                            path=node_path,
                            node_type="e",
                            text=env_content,
                            precis=env_content.strip(),
                            style=f"\\begin{{{env_name}}}",
                            source_file=filename,
                            source_line_start=start_line,
                            source_line_end=end_line,
                            label=label,
                        )
                    )
                    i = end_line
                    continue

                elif env_name in LIST_ENVS:
                    start_line = i + 1
                    env_content, end_line = self._collect_environment(
                        lines, i, env_name
                    )
                    md_text = _list_env_to_markdown(env_content, env_name)

                    node_path = counter.next_child("p")
                    base_slug = make_slug(md_text)
                    slug = resolve_slug(base_slug, slug_counts)
                    nodes.append(
                        Node(
                            slug=slug,
                            path=node_path,
                            node_type="p",
                            text=md_text,
                            style=f"\\begin{{{env_name}}}",
                            source_file=filename,
                            source_line_start=start_line,
                            source_line_end=end_line,
                        )
                    )
                    i = end_line
                    continue

                elif env_name == "document":
                    i += 1
                    continue

            if stripped.startswith(r"\end{document}"):
                i += 1
                continue

            if INPUT_RE.match(stripped):
                i += 1
                continue

            if stripped.startswith("\\") and not _is_content_command(stripped):
                i += 1
                continue

            # Collect paragraph text
            para_lines = []
            start_line = i + 1
            while i < len(lines):
                ln = lines[i].strip()
                if not ln:
                    break
                if SECTION_RE.match(ln):
                    break
                if ENV_RE.match(ln):
                    break
                if ln.startswith(r"\end{document}"):
                    break
                if INPUT_RE.match(ln):
                    break
                clean = COMMENT_RE.sub("", lines[i]).rstrip()
                if clean:
                    para_lines.append(clean)
                i += 1

            if para_lines:
                text = "\n".join(para_lines)
                label = ""
                label_m = LABEL_RE.search(text)
                if label_m:
                    label = label_m.group(1)

                end_line = start_line + len(para_lines) - 1
                node_path = counter.next_child("p")
                base_slug = make_slug(text)
                slug = resolve_slug(base_slug, slug_counts)
                nodes.append(
                    Node(
                        slug=slug,
                        path=node_path,
                        node_type="p",
                        text=text,
                        source_file=filename,
                        source_line_start=start_line,
                        source_line_end=end_line,
                        label=label,
                    )
                )
            else:
                i += 1

        return nodes

    def _collect_environment(
        self, lines: list[str], start: int, env_name: str
    ) -> tuple[str, int]:
        collected = []
        depth = 0
        i = start

        while i < len(lines):
            line = lines[i]
            collected.append(line)

            for m in ENV_RE.finditer(line):
                if m.group(1) == env_name:
                    depth += 1
            for m in ENV_END_RE.finditer(line):
                if m.group(1) == env_name:
                    depth -= 1
                    if depth == 0:
                        return "\n".join(collected), i + 1

            i += 1

        return "\n".join(collected), i


# ─── Module-level helpers ───────────────────────────────────────────


def _level_to_command(level: int) -> str:
    # # → \section* (title), ## → \section, ### → \subsection, #### → \subsubsection
    return {
        1: r"\section*",
        2: r"\section",
        3: r"\subsection",
        4: r"\subsubsection",
    }.get(level, r"\section")


def _is_content_command(line: str) -> bool:
    content_prefixes = (
        r"\textbf",
        r"\textit",
        r"\emph",
        r"\cite",
        r"\ref",
        r"\eqref",
        r"\footnote",
        r"\url",
        r"\href",
    )
    for prefix in content_prefixes:
        if line.startswith(prefix):
            return True
    return False


def _table_synopsis_latex(content: str) -> str:
    caption_m = CAPTION_RE.search(content)
    if caption_m:
        return caption_m.group(1)
    return "[table]"


def _list_env_to_markdown(content: str, env_name: str) -> str:
    """Convert LaTeX itemize/enumerate content to markdown list items."""
    lt = "number" if env_name == "enumerate" else "bullet"
    items: list[str] = []
    current: list[str] = []

    for line in content.split("\n"):
        stripped = line.strip()
        m = ITEM_RE.match(stripped)
        if m:
            if current:
                items.append(" ".join(current))
                current = []
            rest = m.group(1).strip()
            if rest:
                current.append(rest)
        elif (
            stripped
            and not stripped.startswith(r"\begin")
            and not stripped.startswith(r"\end")
        ):
            if current is not None:
                current.append(stripped)

    if current:
        items.append(" ".join(current))

    return "\n".join(list_prefix(lt, 0) + item for item in items if item)


def _markdown_list_to_latex(text: str) -> str:
    """Convert a markdown list block to LaTeX itemize/enumerate."""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return text

    # Detect list type from first line
    first = parse_list_prefix(lines[0])
    if not first:
        return text
    env = "enumerate" if first[0] == "number" else "itemize"

    items: list[str] = []
    for line in lines:
        parsed = parse_list_prefix(line)
        if parsed:
            items.append(f"  \\item {parsed[2]}")
        else:
            items.append(f"  {line}")

    return f"\\begin{{{env}}}\n" + "\n".join(items) + f"\n\\end{{{env}}}"


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tex.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ─── .bib file parsing/writing ──────────────────────────────────────


def _parse_bib_entries(content: str) -> list[tuple[str, str, str, int, int]]:
    entries = []
    lines = content.split("\n")
    i = 0

    while i < len(lines):
        m = _BIB_ENTRY_START_RE.match(lines[i].strip())
        if m:
            entry_type = m.group(1).lower()
            key = m.group(2)
            start = i
            depth = 0
            collected = []

            for j in range(i, len(lines)):
                line = lines[j]
                collected.append(line)
                depth += line.count("{") - line.count("}")
                if depth <= 0:
                    entries.append(
                        (entry_type, key, "\n".join(collected), start + 1, j + 1)
                    )
                    i = j + 1
                    break
            else:
                entries.append(
                    (
                        entry_type,
                        key,
                        "\n".join(collected),
                        start + 1,
                        i + len(collected),
                    )
                )
                i += len(collected)
        else:
            i += 1

    return entries


def _parse_bib_file(
    bib_path: Path, counter: PathCounter, slug_counts: dict[str, int]
) -> list[Node]:
    content = bib_path.read_text(encoding="utf-8")
    entries = _parse_bib_entries(content)
    nodes = []

    for entry_type, key, text, start_line, end_line in entries:
        if entry_type in ("comment", "preamble", "string"):
            continue

        node_path = counter.next_child("b")
        base_slug = make_slug(text)
        slug = resolve_slug(base_slug, slug_counts)
        nodes.append(
            Node(
                slug=slug,
                path=node_path,
                node_type="b",
                text=text,
                precis=_bib_entry_precis(key, text),
                style=f"@{entry_type}",
                source_file=bib_path.name,
                source_line_start=start_line,
                source_line_end=end_line,
                label=key,
            )
        )

    return nodes


def _bib_entry_precis(key: str, text: str) -> str:
    title_m = re.search(r"title\s*=\s*\{([^}]+)\}", text, re.IGNORECASE)
    if title_m:
        title = title_m.group(1)[:50]
        return f"{key}: {title}"
    short = text[:60].rstrip()
    if len(text) > 60:
        short += "…"
    return f"{key}: {short}"


def _bib_keys(bib_path: Path) -> set[str]:
    if not bib_path.exists():
        return set()
    content = bib_path.read_text(encoding="utf-8")
    return {key for _, key, _, _, _ in _parse_bib_entries(content)}


def _append_bib_entry(bib_path: Path, key: str, ref_text: str) -> None:
    existing = _bib_keys(bib_path)
    if key in existing:
        return

    entry = f"@misc{{{key},\n  note = {{{ref_text}}}\n}}\n"

    if bib_path.exists():
        content = bib_path.read_text(encoding="utf-8")
        content = content.rstrip() + "\n\n" + entry
    else:
        content = entry

    fd, tmp = tempfile.mkstemp(dir=bib_path.parent, suffix=".bib.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, bib_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ─── Raw file access (TeX-specific) ────────────────────────────────


def _parse_raw_file_id(id_str: str) -> tuple[str, int | None, int | None, bool] | None:
    m = _RAW_FILE_RE.match(id_str)
    if not m:
        return None
    rel_path = m.group(1)
    ext = Path(rel_path).suffix.lower()
    if ext not in _RAW_ALLOWED_EXT:
        return None
    raw_start = m.group(2)
    raw_end = m.group(3)
    is_append = raw_start == "$"
    start = int(raw_start) if raw_start and raw_start != "$" else None
    end = int(raw_end) if raw_end else None
    return (rel_path, start, end, is_append)


def _raw_read(file_path: str, rel_path: str, start: int | None, end: int | None) -> str:
    root = Path(file_path).parent
    full_path = (root / rel_path).resolve()
    if not full_path.is_relative_to(root.resolve()):
        raise PrecisError(f"path '{rel_path}' escapes project directory")
    if not full_path.exists():
        raise PrecisError(f"file not found: {rel_path}")
    lines = full_path.read_text(encoding="utf-8").splitlines()
    total = len(lines)
    if start is not None:
        s = max(1, start) - 1
        e = min(total, end) if end is not None else total
        selected = lines[s:e]
        header = f">> {rel_path}:{start}..{end or total}  ({e - s} of {total} lines)"
    else:
        selected = lines
        header = f">> {rel_path}  ({total} lines)"
    numbered = [f"{i + (start or 1):4d}: {line}" for i, line in enumerate(selected)]
    return header + "\n" + "\n".join(numbered)


def _raw_write(
    file_path: str,
    rel_path: str,
    text: str,
    start: int | None,
    end: int | None,
    is_append: bool,
) -> str:
    root = Path(file_path).parent
    full_path = (root / rel_path).resolve()
    if not full_path.is_relative_to(root.resolve()):
        raise PrecisError(f"path '{rel_path}' escapes project directory")
    new_lines = text.splitlines(keepends=True)
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
        existing = new_lines

    full_path.write_text("".join(existing), encoding="utf-8")
    total = len(full_path.read_text(encoding="utf-8").splitlines())
    if start is not None:
        return f"{rel_path}:{start}..{end or ''}  replaced → {len(new_lines)} lines (now {total} total)"
    return f"{rel_path}  replaced whole file → {total} lines"

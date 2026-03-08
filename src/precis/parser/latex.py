"""LaTeX parser — regex-based, no full TeX engine."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from precis.citations import BIB_DEF_RE
from precis.nodes import Node, PathCounter, make_slug, resolve_slug

# Sectioning commands → heading level
SECTION_COMMANDS = {
    r"\section": 1,
    r"\subsection": 2,
    r"\subsubsection": 3,
    r"\paragraph": 4,
}

# Regex for sectioning: \section{Title} or \section*{Title}
SECTION_RE = re.compile(
    r"^\\(section|subsection|subsubsection|paragraph)\*?\{(.+?)\}",
    re.MULTILINE,
)

# Environment detection
ENV_RE = re.compile(r"\\begin\{(\w+)\}")
ENV_END_RE = re.compile(r"\\end\{(\w+)\}")

# Label detection
LABEL_RE = re.compile(r"\\label\{([^}]+)\}")

# Caption detection
CAPTION_RE = re.compile(r"\\caption\{([^}]+)\}")

# Input/include detection
INPUT_RE = re.compile(r"\\(?:input|include)\{([^}]+)\}")

# Display math
DISPLAY_MATH_RE = re.compile(r"\\\[(.+?)\\\]", re.DOTALL)
DOLLAR_MATH_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)

# Comment stripping (but preserve line numbers)
COMMENT_RE = re.compile(r"(?<!\\)%.*$", re.MULTILINE)

# Bibliography file detection
BIB_CMD_RE = re.compile(r"\\bibliography\{([^}]+)\}")
ADDBIBRESOURCE_RE = re.compile(r"\\addbibresource\{([^}]+)\}")

# Environment types we care about
TABLE_ENVS = {"table", "tabular", "longtable"}
FIGURE_ENVS = {"figure", "figure*"}
EQUATION_ENVS = {"equation", "equation*", "align", "align*", "gather", "gather*"}


class LatexParser:
    """Parse and manipulate LaTeX documents."""

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
        """Replace a node's text in its source .tex file."""
        tex_file = path.parent / node.source_file
        content = tex_file.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)

        # Replace lines from source_line_start to source_line_end (1-indexed)
        start = node.source_line_start - 1
        end = node.source_line_end
        new_lines = [new_text + "\n"] if not new_text.endswith("\n") else [new_text]
        lines[start:end] = new_lines

        self._atomic_write(tex_file, "".join(lines))

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
        self._atomic_write(tex_file, "".join(lines))

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
        self._atomic_write(tex_file, "".join(lines))

    def delete_node(self, path: Path, node: Node) -> None:
        tex_file = path.parent / node.source_file
        content = tex_file.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)

        start = node.source_line_start - 1
        end = node.source_line_end
        del lines[start:end]

        self._atomic_write(tex_file, "".join(lines))

    def append_node(self, path: Path, new_text: str, heading_level: int = 0) -> None:
        """Append to the last \\input'd file, or .bib for [@key]: definitions."""
        # Check for bibliography definition: [@key]: reference text
        bib_m = BIB_DEF_RE.match(new_text)
        if bib_m:
            key = bib_m.group(1)
            ref_text = bib_m.group(2)
            bib_path = self._find_bib_file(path)
            if bib_path is None:
                raise ValueError(
                    "No .bib file found. Add \\bibliography{name} or "
                    "\\addbibresource{name.bib} to your .tex source first."
                )
            _append_bib_entry(bib_path, key, ref_text)
            return

        files = self._resolve_files(path)
        target = files[-1]  # last input'd file
        content = target.read_text(encoding="utf-8")

        if heading_level:
            cmd = _level_to_command(heading_level)
            new_text = f"{cmd}{{{new_text}}}"

        # Find \end{document} or append at end
        end_doc = content.find(r"\end{document}")
        if end_doc >= 0:
            content = content[:end_doc] + "\n" + new_text + "\n\n" + content[end_doc:]
        else:
            content = content.rstrip() + "\n\n" + new_text + "\n"

        self._atomic_write(target, content)

    def move_nodes(self, path: Path, nodes: list[Node], after: Node) -> None:
        # Group by source file for efficiency
        # For simplicity in V1: delete then insert
        texts = []
        for n in nodes:
            tex_file = path.parent / n.source_file
            content = tex_file.read_text(encoding="utf-8")
            lines = content.splitlines(keepends=True)
            start = n.source_line_start - 1
            end = n.source_line_end
            texts.append("".join(lines[start:end]))

        # Delete in reverse order to preserve line numbers
        for n in reversed(nodes):
            self.delete_node(path, n)

        # Re-parse to get fresh line numbers for the anchor
        fresh_nodes = self.parse(path)
        fresh_after = None
        for fn in fresh_nodes:
            if fn.slug == after.slug:
                fresh_after = fn
                break
        if fresh_after is None:
            raise ValueError(f"Anchor not found after deletion: {after.slug}")

        # Insert after anchor in order
        tex_file = path.parent / fresh_after.source_file
        content = tex_file.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)
        insert_at = fresh_after.source_line_end
        for text in reversed(texts):
            lines.insert(insert_at, "\n" + text)
        self._atomic_write(tex_file, "".join(lines))

    def _find_bib_file(self, root: Path) -> Path | None:
        """Find .bib file referenced by \\bibliography{} or \\addbibresource{}."""
        files = self._resolve_files(root)
        for tex_file in files:
            content = tex_file.read_text(encoding="utf-8")
            # \bibliography{name} (natbib / traditional)
            m = BIB_CMD_RE.search(content)
            if m:
                # \bibliography can list multiple comma-separated names; take first
                name = m.group(1).split(",")[0].strip()
                if not name.endswith(".bib"):
                    name += ".bib"
                return root.parent / name
            # \addbibresource{name.bib} (biblatex)
            m = ADDBIBRESOURCE_RE.search(content)
            if m:
                name = m.group(1).strip()
                if not name.endswith(".bib"):
                    name += ".bib"
                return root.parent / name
        return None

    def _resolve_files(self, root: Path) -> list[Path]:
        """Resolve all \\input/\\include from root, in order."""
        result = []
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
        """Parse a single .tex file into nodes."""
        nodes: list[Node] = []
        lines = content.split("\n")
        i = 0

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # Skip comments and blank lines (handled by paragraph collection)
            if stripped.startswith("%") or not stripped:
                i += 1
                continue

            # Check for sectioning command
            sec_match = SECTION_RE.match(stripped)
            if sec_match:
                cmd_name = sec_match.group(1)
                title = sec_match.group(2)
                level = SECTION_COMMANDS.get(f"\\{cmd_name}", 0)
                if level:
                    label = ""
                    end_line = i
                    # Check current line for label
                    label_m = LABEL_RE.search(stripped)
                    if label_m:
                        label = label_m.group(1)
                    # Check next line for label (common LaTeX convention)
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

            # Check for environment start
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

                elif env_name == "document":
                    # Skip \begin{document} itself
                    i += 1
                    continue

            # Check for \end{document}
            if stripped.startswith(r"\end{document}"):
                i += 1
                continue

            # Skip \input/\include lines
            if INPUT_RE.match(stripped):
                i += 1
                continue

            # Skip other commands that aren't content
            if stripped.startswith("\\") and not _is_content_command(stripped):
                i += 1
                continue

            # Collect paragraph text (until blank line or sectioning command)
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
                # Strip comments but keep line
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
        """Collect all lines of an environment, return (content, end_line_1indexed)."""
        collected = []
        depth = 0
        i = start

        while i < len(lines):
            line = lines[i]
            collected.append(line)

            # Count nested begins/ends of same environment
            for m in ENV_RE.finditer(line):
                if m.group(1) == env_name:
                    depth += 1
            for m in ENV_END_RE.finditer(line):
                if m.group(1) == env_name:
                    depth -= 1
                    if depth == 0:
                        return "\n".join(collected), i + 1

            i += 1

        # Unclosed environment — return what we have
        return "\n".join(collected), i

    def _atomic_write(self, path: Path, content: str) -> None:
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


def _level_to_command(level: int) -> str:
    """Convert heading level to LaTeX command."""
    return {
        1: r"\section",
        2: r"\subsection",
        3: r"\subsubsection",
        4: r"\paragraph",
    }.get(level, r"\section")


def _is_content_command(line: str) -> bool:
    """Check if a line starting with \\ is actual content vs preamble."""
    # These are content commands that should be treated as paragraph text
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
    """Generate table precis from LaTeX table content."""
    caption_m = CAPTION_RE.search(content)
    if caption_m:
        return caption_m.group(1)
    return "[table]"


# ---------------------------------------------------------------------------
# .bib file parsing and writing
# ---------------------------------------------------------------------------

# Matches the start of a bib entry: @type{key,
_BIB_ENTRY_START_RE = re.compile(r"@(\w+)\{([^,\s]+)\s*,")


def _parse_bib_entries(content: str) -> list[tuple[str, str, str, int, int]]:
    """Parse .bib content into (entry_type, key, full_text, start_line, end_line).

    Uses a brace-depth counter to find entry boundaries.
    """
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

            # Count braces to find the closing }
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
                # Unclosed entry — take what we have
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
    """Parse a .bib file into 'b' nodes."""
    content = bib_path.read_text(encoding="utf-8")
    entries = _parse_bib_entries(content)
    nodes = []

    for entry_type, key, text, start_line, end_line in entries:
        if entry_type in ("comment", "preamble", "string"):
            continue  # skip non-reference entries

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
    """Generate short precis for a bib entry."""
    # Try to extract title field
    title_m = re.search(r"title\s*=\s*\{([^}]+)\}", text, re.IGNORECASE)
    if title_m:
        title = title_m.group(1)[:50]
        return f"{key}: {title}"
    # Fallback: first 60 chars
    short = text[:60].rstrip()
    if len(text) > 60:
        short += "…"
    return f"{key}: {short}"


def _bib_keys(bib_path: Path) -> set[str]:
    """Return the set of existing keys in a .bib file."""
    if not bib_path.exists():
        return set()
    content = bib_path.read_text(encoding="utf-8")
    return {key for _, key, _, _, _ in _parse_bib_entries(content)}


def _append_bib_entry(bib_path: Path, key: str, ref_text: str) -> None:
    """Append a @misc entry to the .bib file if key is not already present.

    Creates the .bib file if it does not exist.
    """
    existing = _bib_keys(bib_path)
    if key in existing:
        return  # already present — leave bib alone

    # Build a @misc entry from the reference text
    # Try to extract author and year heuristically
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

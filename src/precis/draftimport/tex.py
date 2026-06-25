"""LaTeX -> draft dry-run parser (read-only).

Pure parsing only; no DB, no pandoc. Produces an inspectable *map* of a
LaTeX project so we can eyeball structure + citation coverage before
anything is written:

* :func:`flatten_inputs`  — recursively inline ``\\input`` / ``\\include``,
  stripping comments, so the whole book is one source string.
* :func:`extract_cites`   — brace-aware scan of the citation family
  (``\\cite``, ``\\citeE``, ``\\citeq``, ``\\citeqp``, ``\\citeqm``, ...),
  recovering keys + (for the quote variants) the verbatim quote + page.
* :func:`parse_bib`       — light BibTeX reader -> ``{key: BibEntry}``
  (doi / arxiv / title / year), the join material for DOI resolution.
* :func:`build_tree`      — explicit-depth level stack over the heading
  commands -> a section tree with per-node block counts.

Run as a module to emit a markdown report::

    uv run python -m precis.draftimport.tex \\
        /path/to/nano-computer.tex --bib references.bib --report out.md
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# 1. flatten \input / \include
# --------------------------------------------------------------------------

_COMMENT_RE = re.compile(r"(?<!\\)%.*")
_INPUT_RE = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")


def strip_comments(text: str) -> str:
    """Drop LaTeX line comments (an unescaped ``%`` to end-of-line)."""
    return "\n".join(_COMMENT_RE.sub("", line) for line in text.splitlines())


def flatten_inputs(
    root: Path,
    *,
    base: Path | None = None,
    _seen: set[Path] | None = None,
    missing: list[str] | None = None,
    loaded: list[str] | None = None,
) -> str:
    """Return ``root`` with every ``\\input``/``\\include`` inlined (comments
    stripped first, so commented-out inputs are ignored).

    LaTeX resolves ``\\input`` paths relative to the *master* document's
    directory (the compilation cwd), not relative to the file doing the
    ``\\input`` — so we thread ``base`` (the master's dir) through the
    recursion and resolve against it first, only falling back to the
    including file's own directory. A missing ``.tex`` is recorded in
    ``missing`` and left out rather than aborting."""
    _seen = _seen if _seen is not None else set()
    missing = missing if missing is not None else []
    loaded = loaded if loaded is not None else []
    root = root.resolve()
    base = base or root.parent
    if root in _seen:
        return ""  # cycle guard
    _seen.add(root)
    loaded.append(str(root))
    raw = strip_comments(root.read_text(encoding="utf-8", errors="replace"))

    def _resolve(target: str) -> Path | None:
        for stem in (base / target, root.parent / target):
            cand = stem if stem.suffix == ".tex" else stem.with_suffix(".tex")
            if cand.exists():
                return cand.resolve()
        return None

    def _sub(m: re.Match[str]) -> str:
        target = m.group(1).strip()
        cand = _resolve(target)
        if cand is None:
            missing.append(target)
            return ""  # font/aux includes etc. — drop silently from content
        return flatten_inputs(
            cand, base=base, _seen=_seen, missing=missing, loaded=loaded
        )

    return _INPUT_RE.sub(_sub, raw)


def document_body(text: str) -> str:
    """Slice ``\\begin{document}`` .. ``\\end{document}`` (preamble dropped).
    Falls through to the whole text when the markers are absent."""
    b = text.find(r"\begin{document}")
    e = text.rfind(r"\end{document}")
    if b == -1:
        return text
    start = b + len(r"\begin{document}")
    return text[start:e] if e != -1 else text[start:]


# --------------------------------------------------------------------------
# 2. citation family
# --------------------------------------------------------------------------

# arg layout per macro (citations.sty): which 0-based brace group holds the
# verbatim quote / the page; default macros are 1-arg (key only).
_QUOTE_ARG = {"citeq": 1, "citeqp": 2, "citeqm": 1}
_PAGE_ARG = {"citeqp": 1}
_NARGS = {"citeq": 3, "citeqp": 4, "citeqm": 3}

# The \mciteboxp* family is where the verbatim quotes actually live:
# every variant is {key}{page}{quote} (citations.sty). We treat any
# macro whose name starts "mcitebox" uniformly.
_CITE_HEAD = re.compile(r"\\((?:cite|mcitebox)[A-Za-z]*)")


@dataclass
class Cite:
    macro: str
    keys: list[str]
    quote: str | None = None
    page: str | None = None


def _balanced_groups(s: str, i: int, n: int) -> tuple[list[str], int]:
    """Consume up to ``n`` balanced ``{...}`` groups starting at ``s[i]``
    (skipping an optional ``[...]`` and whitespace before each). Returns the
    inner strings and the index just past the last consumed group."""
    out: list[str] = []
    for _ in range(n):
        while i < len(s) and s[i].isspace():
            i += 1
        if i < len(s) and s[i] == "[":  # optional arg, e.g. \cite[p.~3]{..}
            depth = 1
            i += 1
            while i < len(s) and depth:
                depth += (s[i] == "[") - (s[i] == "]")
                i += 1
            while i < len(s) and s[i].isspace():
                i += 1
        if i >= len(s) or s[i] != "{":
            break
        depth, start = 0, i
        while i < len(s):
            depth += (s[i] == "{") - (s[i] == "}")
            i += 1
            if depth == 0:
                out.append(s[start + 1 : i - 1])
                break
    return out, i


def extract_cites(text: str) -> list[Cite]:
    """Scan ``text`` for the whole ``\\cite*`` family, brace-aware so nested
    braces inside a quote don't truncate it."""
    cites: list[Cite] = []
    for m in _CITE_HEAD.finditer(text):
        macro = m.group(1)
        if macro.startswith("mcitebox"):
            nargs, qi, pi = 3, 2, 1  # {key}{page}{quote}
        else:
            nargs, qi, pi = (
                _NARGS.get(macro, 1),
                _QUOTE_ARG.get(macro),
                _PAGE_ARG.get(macro),
            )
        groups, _ = _balanced_groups(text, m.end(), nargs)
        if not groups:
            continue
        keys = [k.strip() for k in groups[0].split(",") if k.strip()]
        if not keys:
            continue
        cites.append(
            Cite(
                macro=macro,
                keys=keys,
                quote=(
                    groups[qi].strip() if qi is not None and qi < len(groups) else None
                ),
                page=(
                    groups[pi].strip() if pi is not None and pi < len(groups) else None
                ),
            )
        )
    return cites


# --------------------------------------------------------------------------
# 3. bibliography
# --------------------------------------------------------------------------


@dataclass
class BibEntry:
    key: str
    doi: str | None = None
    arxiv: str | None = None
    title: str | None = None
    year: str | None = None
    entrytype: str | None = None
    author: str | None = None
    venue: str | None = None  # journal / booktitle / publisher
    note: str | None = None

    def human(self) -> str:
        """A one-line human reference: 'Author (Year). Title. Venue. Note'."""
        bits = []
        au = re.sub(r"\s+and\s+", "; ", (self.author or "").replace("\n", " ")).strip()
        if au:
            bits.append(au)
        if self.year:
            bits.append(f"({self.year})")
        t = (self.title or "").replace("{", "").replace("}", "").strip()
        if t:
            bits.append(t.rstrip(".") + ".")
        if self.venue:
            bits.append(self.venue.replace("{", "").replace("}", "").rstrip(".") + ".")
        if self.note:
            bits.append(self.note.strip())
        return " ".join(bits)


_BIB_ENTRY_RE = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", re.IGNORECASE)


def _field(body: str, name: str) -> str | None:
    m = re.search(rf"\b{name}\s*=\s*[{{\"]", body, re.IGNORECASE)
    if not m:
        return None
    i = m.end() - 1
    close = "}" if body[i] == "{" else '"'
    if close == "}":
        depth, start = 0, i
        while i < len(body):
            depth += (body[i] == "{") - (body[i] == "}")
            i += 1
            if depth == 0:
                return body[start + 1 : i - 1].strip()
        return None
    j = body.find('"', i + 1)
    return body[i + 1 : j].strip() if j != -1 else None


def parse_bib(text: str) -> dict[str, BibEntry]:
    """Parse a .bib into ``{key: BibEntry}`` (doi/arxiv/title/year only)."""
    entries: dict[str, BibEntry] = {}
    marks = list(_BIB_ENTRY_RE.finditer(text))
    for idx, m in enumerate(marks):
        key = m.group(2).strip()
        end = marks[idx + 1].start() if idx + 1 < len(marks) else len(text)
        body = text[m.end() : end]
        arxiv = _field(body, "eprint")
        doi = _field(body, "doi")
        if not arxiv and doi and "arxiv" in doi.lower():
            arxiv = doi
        entries[key] = BibEntry(
            key=key,
            doi=doi,
            arxiv=arxiv,
            title=_field(body, "title"),
            year=_field(body, "year"),
            entrytype=m.group(1).lower(),
            author=_field(body, "author"),
            venue=(
                _field(body, "journal")
                or _field(body, "booktitle")
                or _field(body, "publisher")
                or _field(body, "institution")
                or _field(body, "howpublished")
            ),
            note=_field(body, "note"),
        )
    return entries


_BIBCMD_RE = re.compile(
    r"\\(?:bibliography|addbibresource|addglobalbib)\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}"
)


def bib_paths_in(text: str, base: Path) -> list[Path]:
    """Resolve the .bib files a document actually declares.

    Reads every ``\\bibliography{a,b}`` / ``\\addbibresource{c.bib}`` command —
    the authoritative source LaTeX itself uses — and resolves each named file
    relative to the master's directory ``base`` (``\\bibliography`` paths are
    master-relative, like ``\\input``). ``.bib`` is appended when the name is
    bare. Comma lists are split. Only existing files are returned, de-duped in
    declaration order. This is far more reliable than directory-globbing for a
    stray ``*.bib`` (which mis-grabs a shared/sibling bib that lacks the keys)."""
    out: list[Path] = []
    seen: set[Path] = set()
    for m in _BIBCMD_RE.finditer(strip_comments(text)):
        for name in m.group(1).split(","):
            name = name.strip()
            if not name:
                continue
            stem = base / name
            cand = stem if stem.suffix == ".bib" else stem.with_suffix(".bib")
            cand = cand.resolve()
            if cand.exists() and cand not in seen:
                seen.add(cand)
                out.append(cand)
    return out


# --------------------------------------------------------------------------
# 4. section tree + block counts
# --------------------------------------------------------------------------

_DEPTH = {
    "part": 1,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
    "paragraph": 5,
}
_HEAD_RE = re.compile(
    r"\\(part|chapter|section|subsection|subsubsection|paragraph)\*?\s*"
    r"(?:\[[^\]]*\])?\s*\{"
)

_LIST_ENVS = ("itemize", "enumerate", "description")
_EQ_ENVS = (
    "equation",
    "equation*",
    "align",
    "align*",
    "gather",
    "gather*",
    "displaymath",
)
_FIG_ENVS = ("figure", "figure*")
_TBL_ENVS = ("table", "table*", "tabular")


@dataclass
class Counts:
    paragraphs: int = 0
    lists: int = 0
    items: int = 0
    equations: int = 0
    figures: int = 0
    tables: int = 0
    cites: int = 0
    quote_cites: int = 0

    def add(self, o: Counts) -> None:
        for f in self.__dataclass_fields__:
            setattr(self, f, getattr(self, f) + getattr(o, f))


@dataclass
class Node:
    cmd: str
    title: str
    depth: int
    counts: Counts = field(default_factory=Counts)
    children: list[Node] = field(default_factory=list)

    def total(self) -> Counts:
        t = Counts()
        t.add(self.counts)
        for c in self.children:
            t.add(c.total())
        return t


def _count_segment(seg: str) -> Counts:
    c = Counts()
    for env in _LIST_ENVS:
        n = seg.count(rf"\begin{{{env}}}")
        c.lists += n
    c.items += len(re.findall(r"\\item\b", seg))
    for env in _EQ_ENVS:
        c.equations += seg.count(rf"\begin{{{env}}}")
    c.equations += len(re.findall(r"\\\[", seg)) + seg.count("$$") // 2
    for env in _FIG_ENVS:
        c.figures += seg.count(rf"\begin{{{env}}}")
    c.figures += seg.count(r"\includegraphics")
    for env in _TBL_ENVS:
        c.tables += seg.count(rf"\begin{{{env}}}")
    cites = extract_cites(seg)
    c.cites += sum(len(x.keys) for x in cites)
    c.quote_cites += sum(1 for x in cites if x.quote)
    # crude paragraph count: blank-line blocks with letters, minus env noise
    stripped = re.sub(r"\\begin\{.*?\}.*?\\end\{.*?\}", "", seg, flags=re.DOTALL)
    blocks = [b for b in re.split(r"\n\s*\n", stripped) if re.search(r"[A-Za-z]{3}", b)]
    c.paragraphs = len(blocks)
    return c


def build_tree(body: str) -> tuple[Node, list[Cite]]:
    """Build the section tree (level stack) with per-node block counts.
    Returns the synthetic root and the flat list of every citation found."""
    root = Node(cmd="root", title="(document root)", depth=0)
    heads = list(_HEAD_RE.finditer(body))
    stack: list[Node] = [root]
    all_cites: list[Cite] = []

    # preamble body before the first heading
    pre_end = heads[0].start() if heads else len(body)
    pre = body[:pre_end]
    root.counts.add(_count_segment(pre))
    all_cites.extend(extract_cites(pre))

    for idx, m in enumerate(heads):
        cmd = m.group(1)
        # brace-match the title
        titles, after = _balanced_groups(body, m.end() - 1, 1)
        title = re.sub(r"\s+", " ", titles[0]).strip() if titles else ""
        depth = _DEPTH[cmd]
        node = Node(cmd=cmd, title=title, depth=depth)
        seg_end = heads[idx + 1].start() if idx + 1 < len(heads) else len(body)
        seg = body[after:seg_end]
        node.counts.add(_count_segment(seg))
        all_cites.extend(extract_cites(seg))
        # place via level stack
        while len(stack) > 1 and stack[-1].depth >= depth:
            stack.pop()
        stack[-1].children.append(node)
        stack.append(node)
    return root, all_cites


# --------------------------------------------------------------------------
# 5. block planner — body segment -> ordered chunk tree
# --------------------------------------------------------------------------
#
# This is the writing-pass core: a section body becomes ordered chunks.
# - text runs           -> `paragraph` chunks (blank-line split); inline
#                          `$...$` math stays *inside* the prose verbatim.
# - itemize/enumerate    -> `ulist`/`olist` container + `item` children
#                          (nested lists recurse as children of an item).
# - equation/align/\[..\] -> `equation` chunk, raw LaTeX, needs-math-review.
# - table                -> `table` chunk (best-effort), needs-table-review.
# - figure               -> dropped.

_LIST_KIND = {"itemize": "ulist", "enumerate": "olist", "description": "ulist"}
_MATH_ENVS = {
    "equation",
    "equation*",
    "align",
    "align*",
    "gather",
    "gather*",
    "displaymath",
    "multline",
    "multline*",
}
# A table is any of these *outermost* envs (a bare \begin{tabular} is common,
# and "tabular".startswith("table") is False — that was the leak).
_TABLE_BLOCK_ENVS = {
    "table",
    "table*",
    "tabular",
    "tabular*",
    "tabularx",
    "longtable",
    "array",
    "supertabular",
    "tabulary",
}
_FIGURE_BLOCK_ENVS = {
    "figure",
    "figure*",
    "wrapfigure",
    "subfigure",
    "SCfigure",
    "tikzpicture",
    "pgfpicture",
    "tikzcd",
    "landscape",
    "circuitikz",
}
_ANY_BEGIN = re.compile(r"\\begin\{(\w+\*?)\}")
_ANY_BE = re.compile(r"\\(begin|end)\{(\w+\*?)\}")
_DISPLAY_MATH = re.compile(r"\\\[(.+?)\\\]", re.DOTALL)


@dataclass
class Chunk:
    kind: str
    text: str = ""
    children: list[Chunk] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


def _split_top_envs(s: str) -> list[tuple]:
    """Split into ordered ('text', str) and ('env', name, inner) segments,
    matching ``\\begin``/``\\end`` with depth so nested envs stay whole."""
    out: list[tuple] = []
    i = 0
    while i < len(s):
        m = _ANY_BEGIN.search(s, i)
        if not m:
            out.append(("text", s[i:]))
            break
        if m.start() > i:
            out.append(("text", s[i : m.start()]))
        depth = 0
        end_start = after = None
        for t in _ANY_BE.finditer(s, m.start()):
            depth += 1 if t.group(1) == "begin" else -1
            if depth == 0:
                end_start, after = t.start(), t.end()
                break
        if end_start is None:
            out.append(("env", m.group(1), s[m.end() :]))
            break
        out.append(("env", m.group(1), s[m.end() : end_start]))
        i = after
    return out


def _split_paras(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for blk in re.split(r"\n\s*\n", text):
        # pull out any display math \[..\] as its own block
        last = 0
        for dm in _DISPLAY_MATH.finditer(blk):
            pre = blk[last : dm.start()]
            if pre.strip():
                out.append(("p", re.sub(r"\s+", " ", pre).strip()))
            out.append(("eq", dm.group(1).strip()))
            last = dm.end()
        tail = blk[last:]
        if re.search(r"[A-Za-z]{3}", tail):
            out.append(("p", re.sub(r"\s+", " ", tail).strip()))
    return out


def _split_items(inner: str) -> list[str]:
    """Split a list body on top-level ``\\item`` (skip nested-env items)."""
    tok = re.compile(r"\\(begin|end)\{\w+\*?\}|\\item\b")
    depth, starts = 0, []
    for m in tok.finditer(inner):
        g = m.group(0)
        if g.startswith("\\begin"):
            depth += 1
        elif g.startswith("\\end"):
            depth -= 1
        elif depth == 0:
            starts.append(m.start())
    parts = []
    for idx, s in enumerate(starts):
        e = starts[idx + 1] if idx + 1 < len(starts) else len(inner)
        parts.append(re.sub(r"^\\item\b\s*(\[[^\]]*\])?\s*", "", inner[s:e]))
    return parts


def _plan_list(kind: str, inner: str) -> Chunk:
    cont = Chunk(kind)
    for raw in _split_items(inner):
        item = Chunk("item")
        texts: list[str] = []
        for seg in _split_top_envs(raw):
            if seg[0] == "text":
                texts.append(seg[1])
            elif seg[1] in _LIST_KIND:
                item.children.append(_plan_list(_LIST_KIND[seg[1]], seg[2]))
            elif seg[1] in _MATH_ENVS:
                item.children.append(
                    Chunk(
                        "equation", seg[2].strip(), meta={"flag": "needs-math-review"}
                    )
                )
            else:
                texts.append(seg[2])
        item.text = re.sub(r"\s+", " ", " ".join(texts)).strip()
        cont.children.append(item)
    return cont


def plan_blocks(body: str) -> list[Chunk]:
    """Ordered chunk plan for a section body (no headings)."""
    chunks: list[Chunk] = []
    for seg in _split_top_envs(body):
        if seg[0] == "text":
            for kind, txt in _split_paras(seg[1]):
                chunks.append(
                    Chunk(
                        "equation" if kind == "eq" else "paragraph",
                        txt,
                        meta={"flag": "needs-math-review"} if kind == "eq" else {},
                    )
                )
        elif seg[1] in _LIST_KIND:
            chunks.append(_plan_list(_LIST_KIND[seg[1]], seg[2]))
        elif seg[1] in _MATH_ENVS:
            chunks.append(
                Chunk("equation", seg[2].strip(), meta={"flag": "needs-math-review"})
            )
        elif seg[1] in _TABLE_BLOCK_ENVS:
            # keep the FULL tabular (raw LaTeX) — flagged for later review; the
            # dry-run report truncates for display, the stored chunk must not.
            chunks.append(
                Chunk("table", seg[2].strip(), meta={"flag": "needs-table-review"})
            )
        elif seg[1] in _FIGURE_BLOCK_ENVS:
            continue  # dropped
        else:
            # unknown wrapper env (center, minipage, trackonepara, …) — recurse
            # so a tabular/list nested inside it is detected, not leaked. Strip a
            # leading optional arg (`\begin{env}[label]` — env config, not prose).
            inner = re.sub(r"^\s*\[[^\]]*\]", "", seg[2])
            chunks.extend(plan_blocks(inner))
    return chunks


def render_plan(chunks: list[Chunk], lines: list[str], indent: int = 0) -> None:
    for c in chunks:
        pad = "  " * indent
        preview = (c.text or "").replace("\n", " ")
        if len(preview) > 88:
            preview = preview[:88] + "…"
        flag = f"  ⚑{c.meta['flag']}" if c.meta.get("flag") else ""
        tag = {
            "ulist": "• ulist",
            "olist": "1. olist",
            "item": "└ item",
            "equation": "∑ equation",
            "paragraph": "¶",
            "table": "▦ table",
        }.get(c.kind, c.kind)
        lines.append(f"{pad}{tag}: {preview}{flag}".rstrip())
        if c.children:
            render_plan(c.children, lines, indent + 1)


def walk_document(body: str) -> Chunk:
    """Integrate headings (``build_tree``'s level stack) with bodies
    (``plan_blocks``) into one ordered chunk tree, ready for the writer.

    Returns a synthetic ``root`` ``Chunk``; every node carries the *raw*
    LaTeX in ``.text`` (the writer cleans it with the cite keymap and
    extracts ``\\label``s before creating the chunk). Headings nest by the
    explicit-depth stack; the body between two headings attaches under the
    current heading (sub-headings handled by the stack)."""
    root = Chunk("root")
    heads = list(_HEAD_RE.finditer(body))
    stack: list[tuple[int, Chunk]] = [(0, root)]

    def _attach_body(seg: str, parent: Chunk) -> None:
        parent.children.extend(plan_blocks(seg))

    pre_end = heads[0].start() if heads else len(body)
    _attach_body(body[:pre_end], root)
    for idx, m in enumerate(heads):
        cmd = m.group(1)
        titles, after = _balanced_groups(body, m.end() - 1, 1)
        title = re.sub(r"\s+", " ", titles[0]).strip() if titles else ""
        depth = _DEPTH[cmd]
        node = Chunk("heading", text=title, meta={"cmd": cmd, "depth": depth})
        seg_end = heads[idx + 1].start() if idx + 1 < len(heads) else len(body)
        while len(stack) > 1 and stack[-1][0] >= depth:
            stack.pop()
        stack[-1][1].children.append(node)
        _attach_body(body[after:seg_end], node)
        stack.append((depth, node))
    return root


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def _title_of(text: str) -> str | None:
    m = re.search(r"\\title\s*\{", text)
    if not m:
        return None
    g, _ = _balanced_groups(text, m.end() - 1, 1)
    return re.sub(r"\s+", " ", g[0]).strip() if g else None


def _render_tree(node: Node, lines: list[str], max_depth: int) -> None:
    for ch in node.children:
        t = ch.total()
        if ch.depth <= max_depth:
            indent = "  " * (ch.depth - 1)
            bits = []
            if t.paragraphs:
                bits.append(f"{t.paragraphs}¶")
            if t.lists:
                bits.append(f"{t.lists}list/{t.items}item")
            if t.equations:
                bits.append(f"{t.equations}eq")
            if t.figures:
                bits.append(f"{t.figures}fig")
            if t.tables:
                bits.append(f"{t.tables}tbl")
            if t.cites:
                bits.append(f"{t.cites}cite")
            meta = ("  · " + ", ".join(bits)) if bits else ""
            lines.append(f"{indent}{ch.cmd}: {ch.title}{meta}")
        _render_tree(ch, lines, max_depth)


def build_report(root_path: Path, bib_path: Path | None, max_depth: int) -> str:
    missing: list[str] = []
    loaded: list[str] = []
    full = flatten_inputs(root_path, missing=missing, loaded=loaded)
    title = _title_of(full) or root_path.stem
    body = document_body(full)
    tree, cites = build_tree(body)
    tot = tree.total()

    used_keys: dict[str, int] = {}
    for c in cites:
        for k in c.keys:
            used_keys[k] = used_keys.get(k, 0) + 1
    quote_cites = [c for c in cites if c.quote]

    by_macro: dict[str, int] = {}
    for c in cites:
        by_macro[c.macro] = by_macro.get(c.macro, 0) + 1

    bib = (
        parse_bib(bib_path.read_text(encoding="utf-8", errors="replace"))
        if bib_path
        else {}
    )
    in_bib = {k for k in used_keys if k in bib}
    not_in_bib = sorted(k for k in used_keys if k not in bib)
    with_doi = {
        k for k in in_bib if bib[k].doi and "arxiv" not in (bib[k].doi or "").lower()
    }
    with_arxiv = {k for k in in_bib if bib[k].arxiv}
    neither = sorted(in_bib - with_doi - with_arxiv)

    L: list[str] = []
    L.append(f"# import dry-run — {title}")
    L.append("")
    L.append(f"root: `{root_path}`")
    L.append(
        f"flattened {len(loaded)} files; {len(missing)} \\input targets missing/skipped"
    )
    L.append("")
    L.append("## totals")
    L.append(
        f"- {tot.paragraphs} paragraphs · {tot.lists} lists ({tot.items} items) · "
        f"{tot.equations} equations · {tot.figures} figures · {tot.tables} tables"
    )
    L.append(
        f"- {tot.cites} citation refs, {len(used_keys)} unique keys, "
        f"{len(quote_cites)} quote-bearing cites"
    )
    L.append("")
    L.append("## citations by macro")
    for mac, n in sorted(by_macro.items(), key=lambda x: -x[1]):
        L.append(f"- `\\{mac}` × {n}")
    L.append("")
    L.append("## bibliography coverage")
    L.append(f"- unique used keys: **{len(used_keys)}**")
    L.append(
        f"- present in .bib: **{len(in_bib)}**  (missing from .bib: {len(not_in_bib)})"
    )
    L.append(
        f"- resolvable join material — DOI: **{len(with_doi)}**, "
        f"arXiv: **{len(with_arxiv)}**, neither: **{len(neither)}**"
    )
    if not_in_bib:
        L.append("")
        L.append(f"### used keys NOT in .bib ({len(not_in_bib)})")
        L.append("`" + "`, `".join(not_in_bib) + "`")
    if neither:
        L.append("")
        L.append(f"### in .bib but no DOI/arXiv to resolve by ({len(neither)})")
        L.append("`" + "`, `".join(neither) + "`")
    L.append("")
    L.append("## quote-bearing cites (anchor by quote -> ~N)")
    L.append(f"- {len(quote_cites)} total; sample:")
    for c in quote_cites[:8]:
        q = (c.quote or "")[:90].replace("\n", " ")
        L.append(f"  - `\\{c.macro}{{{','.join(c.keys)}}}` → “{q}…”")
    L.append("")
    L.append(f"## section tree (to depth {max_depth})")
    L.append("```")
    tl: list[str] = []
    _render_tree(tree, tl, max_depth)
    L.extend(tl)
    L.append("```")
    if missing:
        L.append("")
        L.append(f"## missing \\input targets ({len(missing)})")
        L.append("`" + "`, `".join(sorted(set(missing))) + "`")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="LaTeX -> draft import dry-run (read-only)."
    )
    ap.add_argument("root", type=Path, help="Root .tex file.")
    ap.add_argument(
        "--bib", type=Path, default=None, help="references.bib for DOI coverage."
    )
    ap.add_argument(
        "--report", type=Path, default=None, help="Write markdown report here."
    )
    ap.add_argument("--depth", type=int, default=3, help="Max section depth to print.")
    ap.add_argument(
        "--dump-keys",
        type=Path,
        default=None,
        help="Write 'key<TAB>doi<TAB>arxiv' for every used key (for the DB pass).",
    )
    ap.add_argument(
        "--plan",
        type=Path,
        default=None,
        help="Print the chunk plan (lists/math/paragraphs) for one .tex file and exit.",
    )
    args = ap.parse_args(argv)

    if args.plan:
        from precis.draftimport.demacro import demacro, harvest_acronyms, harvest_macros

        full = flatten_inputs(args.root) if args.root != Path("/dev/null") else ""
        macros = harvest_macros(full[: full.find(r"\begin{document}")]) if full else {}
        gloss = args.root.parent / "tex" / "glossary-entries.tex"
        acro = harvest_acronyms(gloss.read_text()) if gloss.exists() else {}
        body = strip_comments(args.plan.read_text(encoding="utf-8", errors="replace"))
        chunks = plan_blocks(body)

        def _clean(cs: list[Chunk]) -> None:
            for c in cs:
                if c.text and c.kind not in ("equation", "table"):
                    c.text = demacro(c.text, macros=macros, acronyms=acro)
                _clean(c.children)

        _clean(chunks)
        out: list[str] = []
        render_plan(chunks, out)
        print(
            f"# chunk plan (cleaned) — {args.plan.name}  ({len(chunks)} top-level chunks)\n"
        )
        print("\n".join(out))
        return 0

    report = build_report(args.root, args.bib, args.depth)
    if args.report:
        args.report.write_text(report, encoding="utf-8")
        print(f"wrote {args.report}")
    else:
        print(report)

    if args.dump_keys:
        full = flatten_inputs(args.root)
        _, cites = build_tree(document_body(full))
        bib = (
            parse_bib(args.bib.read_text(encoding="utf-8", errors="replace"))
            if args.bib
            else {}
        )
        used = sorted({k for c in cites for k in c.keys})
        with args.dump_keys.open("w", encoding="utf-8") as fh:
            for k in used:
                e = bib.get(k)
                fh.write(
                    f"{k}\t{(e.doi if e else '') or ''}\t{(e.arxiv if e else '') or ''}\n"
                )
        print(f"wrote {args.dump_keys} ({len(used)} keys)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

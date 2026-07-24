"""Data/table chunks — canonical ``meta.table`` JSON and its derived
markdown projection (ADR 0035 §1, build step 1).

A ``chunk_kind='table'`` draft chunk is the single source of truth for a
small dataset: the canonical data lives in ``meta.table = {header, rows}``
(cells stay JSON scalars so numbers remain numbers, ready for the
``numerics`` index), and ``chunks.text`` is a *derived* GFM markdown
render of that data — regenerated on every write, never hand-edited (the
same one-source/no-drift discipline as summaries and ``ord<0`` cards). The
derived text keeps the table embeddable and lexically searchable.

This module is pure: validate/normalise a caller-supplied table, and
render it to a single markdown block (no internal blank line, so the
``add_chunks`` blank-line splitter keeps it as one chunk). **No code is
executed here** — a table chunk is inert payload; the graph/figure render
recipe (§2) and its sandbox (§3) are a later build step.
"""

from __future__ import annotations

import re
from typing import Any

from precis.errors import BadInput

#: JSON scalar types a cell may hold (preserved verbatim in ``meta.table``).
Scalar = str | int | float | bool | None


def normalize_table(obj: Any) -> dict[str, Any]:
    """Validate a caller-supplied table and return the canonical
    ``{header: [...], rows: [[...], ...]}`` shape stored in ``meta.table``.

    Header cells are coerced to ``str``; row cells keep their JSON scalar
    type (so ``1.523`` stays a number, not ``"1.523"``). Every row must be
    the same width as the header. Raises :class:`BadInput` with a
    copy-ready ``next=`` on any malformed input.
    """
    nxt = (
        "table={'header': ['element', 'gap_eV'], 'rows': [['Si', 1.12], ['Ge', 0.67]]}"
    )
    if not isinstance(obj, dict):
        raise BadInput(f"table must be an object, got {type(obj).__name__}", next=nxt)
    header_raw = obj.get("header")
    rows_raw = obj.get("rows")
    if not isinstance(header_raw, list) or not header_raw:
        raise BadInput(
            "table.header must be a non-empty list of column names", next=nxt
        )
    if not isinstance(rows_raw, list):
        raise BadInput("table.rows must be a list of rows", next=nxt)
    header = [str(h) for h in header_raw]
    width = len(header)
    rows: list[list[Scalar]] = []
    for i, row in enumerate(rows_raw):
        if not isinstance(row, list):
            raise BadInput(f"table.rows[{i}] must be a list of cells", next=nxt)
        if len(row) != width:
            raise BadInput(
                f"table.rows[{i}] has {len(row)} cells, header has {width}",
                next="every row must align to header — pad short rows with null",
            )
        for cell in row:
            if not isinstance(cell, (str, int, float, bool, type(None))):
                raise BadInput(
                    f"table.rows[{i}] cell {cell!r} is not a JSON scalar "
                    "(string/number/bool/null)",
                    next=nxt,
                )
        rows.append(list(row))
    return {"header": header, "rows": rows}


def _cell_md(value: Scalar) -> str:
    """Render one cell for a GFM table: stringify, escape pipes, and keep
    it on a single line (newlines → ``<br>``) so the row stays one line."""
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    text = str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def table_to_markdown(table: dict[str, Any], *, caption: str | None = None) -> str:
    """Render a normalised ``{header, rows}`` table to a single GFM block.

    The result has **no internal blank line** so the ``add_chunks``
    blank-line splitter keeps the whole table in one chunk. An optional
    ``caption`` (the table's legend) is rendered as a leading ``**…**``
    line so it stays in the embeddable ``text`` projection without
    breaking the block.
    """
    header = table["header"]
    rows = table["rows"]
    lines: list[str] = []
    if caption and caption.strip():
        lines.append(f"**{caption.strip()}**")
    lines.append("| " + " | ".join(_cell_md(h) for h in header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in rows:
        lines.append("| " + " | ".join(_cell_md(c) for c in row) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Render-side recovery — the inverse of the build above. Every display surface
# (web reader, .docx, LaTeX) wants the *structured* table, not the derived
# pipe text, so they share one recovery path here (DRY, mirroring how the
# build side is single-sourced).
# ---------------------------------------------------------------------------


def cell_text(value: Scalar) -> str:
    """Plain (unescaped) display string for one cell — the surface applies
    its own escaping (HTML / OOXML / LaTeX). Mirrors :func:`_cell_md` minus
    the markdown pipe/newline escaping, so booleans and ``None`` read the
    same everywhere."""
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _uncell_md(text: str) -> str:
    """Reverse :func:`_cell_md`: ``<br>`` → newline and unescape ``\\|`` /
    ``\\\\`` (used only on the markdown-parse fallback path)."""
    return text.replace("<br>", "\n").replace(r"\|", "|").replace("\\\\", "\\").strip()


def parse_markdown_table(text: str) -> dict[str, Any] | None:
    """Recover ``{header, rows, caption}`` from a GFM table block — the
    fallback for a ``chunk_kind='table'`` chunk that predates the canonical
    ``meta.table`` (e.g. a Marker-ingested table). Returns ``None`` if the
    text is not a well-formed table (header + ``---`` separator + body)."""
    caption: str | None = None
    pipe_lines: list[str] = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("|"):
            pipe_lines.append(s)
        elif not pipe_lines and s.startswith("**") and s.endswith("**"):
            caption = s[2:-2].strip() or None
    if len(pipe_lines) < 2:
        return None

    def split_row(s: str) -> list[str]:
        s = s.strip().strip("|")
        return [_uncell_md(c) for c in re.split(r"(?<!\\)\|", s)]

    sep = pipe_lines[1]
    is_sep = bool(sep) and set(sep) <= set("|-: ")
    if not is_sep:
        return None
    header = split_row(pipe_lines[0])
    rows = [split_row(r) for r in pipe_lines[2:]]
    return {"header": header, "rows": rows, "caption": caption}


def _find_balanced(text: str, start: int) -> int | None:
    """``text[start]`` is a ``{``; return the index just past its matching
    ``}`` (brace-depth aware, backslash-escaped chars skipped so ``\\{``/
    ``\\}`` don't perturb the count), or ``None`` if unbalanced."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


_LATEX_COMMENT_RE = re.compile(r"(?<!\\)%.*")


def _strip_latex_comments(text: str) -> str:
    """Drop an unescaped ``%`` to end-of-line, line by line (``\\%`` kept)."""
    return "\n".join(_LATEX_COMMENT_RE.sub("", line) for line in text.splitlines())


def _extract_caption(text: str) -> tuple[str | None, str]:
    """Pull the first ``\\caption{...}`` (balanced braces) out of ``text``,
    returning ``(inner, text-with-caption-removed)``."""
    m = re.search(r"\\caption\*?\s*\{", text)
    if not m:
        return None, text
    end = _find_balanced(text, m.end() - 1)
    if end is None:
        return None, text
    inner = text[m.end() : end - 1]
    return inner, text[: m.start()] + text[end:]


def _strip_one_arg_command(text: str, name: str) -> str:
    """Remove every ``\\name{...}`` (balanced braces), name and all."""
    pat = re.compile(r"\\" + name + r"\s*\{")
    while True:
        m = pat.search(text)
        if not m:
            return text
        end = _find_balanced(text, m.end() - 1)
        if end is None:
            return text
        text = text[: m.start()] + text[end:]


def _strip_resizebox(text: str) -> str:
    """Unwrap ``\\resizebox{w}{h}{ ... }`` to just its inner content."""
    pat = re.compile(r"\\resizebox\s*\{")
    while True:
        m = pat.search(text)
        if not m:
            return text
        end1 = _find_balanced(text, m.end() - 1)
        if end1 is None:
            return text
        m2 = re.match(r"\s*\{", text[end1:])
        if not m2:
            return text
        start2 = end1 + m2.start()
        end2 = _find_balanced(text, start2)
        if end2 is None:
            return text
        m3 = re.match(r"\s*\{", text[end2:])
        if not m3:
            return text
        start3 = end2 + m3.start()
        end3 = _find_balanced(text, start3)
        if end3 is None:
            return text
        inner = text[start3 + 1 : end3 - 1]
        text = text[: m.start()] + inner + text[end3:]


_INNER_TABULAR_ENV_RE = re.compile(
    r"\\begin\{(tabular\*?|tabularx|tabulary|longtable|supertabular|array)\}"
)


def _locate_tabular_body(text: str) -> str:
    """Take from just after an inner ``\\begin{tabular…}`` up to its
    matching ``\\end{…}`` (depth-aware). If no such env is found, ``text``
    is assumed to already be the bare tabular body (the importer's
    captured ``\\begin{tabular}`` inner) and is returned unchanged."""
    m = _INNER_TABULAR_ENV_RE.search(text)
    if not m:
        return text
    env = m.group(1)
    begin_re = re.compile(r"\\begin\{" + re.escape(env) + r"\}")
    end_re = re.compile(r"\\end\{" + re.escape(env) + r"\}")
    depth = 1
    pos = m.end()
    while depth > 0:
        nb = begin_re.search(text, pos)
        ne = end_re.search(text, pos)
        if ne is None:
            return text[m.end() :]
        if nb is not None and nb.start() < ne.start():
            depth += 1
            pos = nb.end()
        else:
            depth -= 1
            if depth == 0:
                return text[m.end() : ne.start()]
            pos = ne.end()
    return text[m.end() :]


def _strip_colspec(text: str) -> str:
    """Strip a leading optional ``[pos]`` then one-or-more balanced
    ``{...}`` groups (covers a plain colspec, or a ``tabularx``/``tabular*``
    width arg followed by the colspec) — what's left is rows only."""
    s = text.lstrip()
    if s.startswith("["):
        end = s.find("]")
        if end != -1:
            s = s[end + 1 :].lstrip()
    while s.startswith("{"):
        end = _find_balanced(s, 0)
        if end is None:
            break
        s = s[end:].lstrip()
    return s


_RULE_MACRO_RE = re.compile(
    r"\\hline"
    r"|\\toprule"
    r"|\\midrule"
    r"|\\bottomrule"
    r"|\\cline\{[^}]*\}"
    r"|\\cmidrule(?:\([^)]*\))?\{[^}]*\}"
    r"|\\addlinespace(?:\[[^\]]*\])?"
    r"|\\rowcolor\{[^}]*\}"
    r"|\\noalign\{[^}]*\}"
)


def _strip_rule_macros(text: str) -> str:
    return _RULE_MACRO_RE.sub("", text)


_ROW_SPLIT_RE = re.compile(r"\\\\(?:\[[^\]]*\])?|\\tabularnewline(?:\[[^\]]*\])?")
_UNESCAPED_AMP_RE = re.compile(r"(?<!\\)&")


def _split_row_cells(row: str) -> list[str]:
    """Split one row on ``&`` that is neither escaped (``\\&``) nor inside
    a ``{...}`` group (brace-depth tracked)."""
    cells: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    n = len(row)
    while i < n:
        c = row[i]
        if c == "\\" and i + 1 < n:
            buf.append(row[i : i + 2])
            i += 2
            continue
        if c == "{":
            depth += 1
            buf.append(c)
        elif c == "}":
            depth -= 1
            buf.append(c)
        elif c == "&" and depth == 0:
            cells.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    cells.append("".join(buf))
    return cells


def _unwrap_last_arg_macro(text: str, name: str, num_args: int) -> str:
    """Best-effort ``\\name{a1}...{aN}`` -> content of the *last* arg
    (keeps content, doesn't try to honor the macro's real semantics — used
    for ``\\multicolumn``/``\\multirow``)."""
    pat = re.compile(r"\\" + name + r"\s*\{")
    while True:
        m = pat.search(text)
        if not m:
            return text
        pos = m.end() - 1
        last_span: tuple[int, int] | None = None
        ok = True
        for k in range(num_args):
            end = _find_balanced(text, pos)
            if end is None:
                ok = False
                break
            last_span = (pos, end)
            if k < num_args - 1:
                nxt = re.match(r"\s*\{", text[end:])
                if not nxt:
                    ok = False
                    break
                pos = end + nxt.start()
            else:
                pos = end
        if not ok or last_span is None:
            return text
        start, end = last_span
        inner = text[start + 1 : end - 1]
        text = text[: m.start()] + inner + text[pos:]


_ONE_ARG_FONT_MACROS = "textbf|textit|textrm|texttt|textsc|emph|mathrm|text"
_ONE_ARG_FONT_RE = re.compile(r"\\(?:" + _ONE_ARG_FONT_MACROS + r")\s*\{")


def _unwrap_one_arg_font_macros(s: str) -> str:
    """Unwrap ``\\textbf{X}``-style one-arg font/format macros to ``X``,
    recursively (so nested macros inside ``X`` are unwrapped too)."""
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        m = _ONE_ARG_FONT_RE.match(s, i)
        if m:
            end = _find_balanced(s, m.end() - 1)
            if end is not None:
                inner = s[m.end() : end - 1]
                out.append(_unwrap_one_arg_font_macros(inner))
                i = end
                continue
        out.append(s[i])
        i += 1
    return "".join(out)


_MATH_SPAN_RE = re.compile(r"\$[^$]*\$")
_STRAY_SPACING_RE = re.compile(r"~|\\,|\\;|\\!|\\centering\b|\\bf\b|\\it\b")


def _clean_latex_cell(raw: str) -> str:
    """Clean one table cell per the frozen contract's step 6: unwrap
    font/multicolumn/multirow macros, drop stray spacing tokens, unescape
    ``\\&``/``\\%``/etc., collapse whitespace — ``$...$`` math kept
    verbatim throughout."""
    math_spans: list[str] = []

    def _protect(m: re.Match[str]) -> str:
        math_spans.append(m.group(0))
        return f"\x00MATH{len(math_spans) - 1}\x00"

    s = _MATH_SPAN_RE.sub(_protect, raw)
    s = _unwrap_last_arg_macro(s, "multicolumn", 3)
    s = _unwrap_last_arg_macro(s, "multirow", 3)
    s = _unwrap_one_arg_font_macros(s)
    s = _STRAY_SPACING_RE.sub(" ", s)
    s = (
        s.replace(r"\&", "&")
        .replace(r"\%", "%")
        .replace(r"\_", "_")
        .replace(r"\#", "#")
        .replace(r"\$", "$")
        .replace(r"\{", "{")
        .replace(r"\}", "}")
    )
    s = re.sub(r"\s+", " ", s).strip()
    for i, span in enumerate(math_spans):
        s = s.replace(f"\x00MATH{i}\x00", span)
    return s


def parse_latex_table(text: str) -> dict[str, Any] | None:
    """Recover ``{header, rows, caption}`` from raw LaTeX ``tabular``-family
    text — the fallback for a LaTeX-imported ``chunk_kind='table'`` chunk
    that has no canonical ``meta.table`` (gr51405 + gr52564). Accepts a
    bare tabular body (importer's captured ``\\begin{tabular}`` inner,
    starting with the ``{colspec}``) or a full float wrapper (``table``/
    ``longtable`` with a ``\\caption{}`` and a nested ``tabular``).

    Structureless input (prose, an empty string, a GFM table chunk the
    markdown parser already handled) returns ``None`` — this runs on
    arbitrary table-chunk text at render time and must never raise."""
    try:
        if not text or not text.strip():
            return None
        body = _strip_latex_comments(text)
        caption, body = _extract_caption(body)
        body = _strip_one_arg_command(body, "label")
        body = re.sub(r"\\centering\b|\\small\b|\\footnotesize\b", "", body)
        body = _strip_resizebox(body)
        body = re.sub(r"\\begin\{center\}|\\end\{center\}", "", body)
        body = _locate_tabular_body(body)
        body = _strip_colspec(body)
        body = _strip_rule_macros(body)
        if not _ROW_SPLIT_RE.search(body) or not _UNESCAPED_AMP_RE.search(body):
            return None
        row_texts = [r.strip() for r in _ROW_SPLIT_RE.split(body)]
        row_texts = [r for r in row_texts if r]
        if not row_texts:
            return None
        parsed_rows = [
            [_clean_latex_cell(c) for c in _split_row_cells(r)] for r in row_texts
        ]
        header = parsed_rows[0]
        if not header:
            return None
        width = len(header)
        rows: list[list[str]] = []
        for row in parsed_rows[1:]:
            if len(row) > width:
                return None
            if len(row) < width:
                row = row + [""] * (width - len(row))
            rows.append(row)
        cap_clean = _clean_latex_cell(caption) if caption else None
        return {"header": header, "rows": rows, "caption": cap_clean or None}
    except Exception:
        return None


def table_payload(
    meta: dict[str, Any] | None, text: str | None
) -> dict[str, Any] | None:
    """Recover a renderable table from a ``chunk_kind='table'`` chunk.

    Prefers the canonical ``meta.table`` (+ ``meta.caption`` legend); falls
    back to parsing the derived GFM ``text`` (:func:`parse_markdown_table`)
    for chunks that predate the canonical store, then to raw-LaTeX recovery
    (:func:`parse_latex_table`) for LaTeX-imported chunks that never got a
    canonical table. Returns ``{header: [str], rows: [[str]], caption:
    str|None}`` with every cell already stringified via :func:`cell_text`,
    or ``None`` when no table can be recovered (the surface then renders the
    raw text as prose)."""
    meta = meta or {}
    tbl = meta.get("table")
    if isinstance(tbl, dict) and isinstance(tbl.get("header"), list) and tbl["header"]:
        header = [cell_text(h) for h in tbl["header"]]
        rows = [[cell_text(c) for c in row] for row in (tbl.get("rows") or [])]
        cap = meta.get("caption")
        return {"header": header, "rows": rows, "caption": cap or None}
    return parse_markdown_table(text or "") or parse_latex_table(text or "")

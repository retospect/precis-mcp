"""Data/table chunks — canonical ``meta.table`` JSON and its derived
markdown projection (build step 1).

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

import json
import math
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

    ``obj`` may also be a top-level JSON **string** (decoded once via
    ``json.loads`` before the dict validation below) — the reliable string
    channel `caption=` already has, given to `table=` too so an agent isn't
    forced through the nested-dict MCP wire path where a client-side
    backslash-doubling bug corrupts cell strings like ``$\\sim$`` (gr178512).
    The server never touches backslashes itself either way.
    """
    nxt = (
        "table={'header': ['element', 'gap_eV'], 'rows': [['Si', 1.12], ['Ge', 0.67]]}"
    )
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except json.JSONDecodeError as e:
            raise BadInput(f"table= string is not valid JSON: {e}", next=nxt) from e
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


def infer_scalar(s: str) -> Scalar:
    """Excel-style on-entry type inference for a data-cell edit's raw
    string value: try ``int``, then ``float``, then ``bool`` (``'true'``/
    ``'false'``, case-insensitive), else keep the string verbatim
    (including ``''`` — an empty edit stays an empty string, never becomes
    ``None``). Used by :func:`set_cell` so a numeric edit lands as a JSON
    number in ``meta.table``, keeping the numerics index working.

    A ``float()`` parse that comes back non-finite (``'nan'``/``'inf'``/
    ``'-inf'``/``'infinity'``, case-insensitive — all valid Python float
    literals) is treated as NOT a number: it's kept as the original
    string. Two reasons: (1) it's a legitimate "not measured" cell
    placeholder (``'NaN'``), not a numeric value; (2) ``NaN``/``Infinity``
    are not valid RFC-8259 JSON tokens — ``json.dumps`` still emits them,
    but Postgres ``jsonb`` rejects them outright, so a non-finite float
    would crash the write rather than merely mis-type the cell."""
    if s == "":
        return s
    try:
        return int(s)
    except ValueError:
        pass
    try:
        f = float(s)
    except ValueError:
        f = None
    if f is not None:
        if math.isfinite(f):
            return f
        # non-finite (nan/inf/-inf) — fall through, keep as string below
    low = s.strip().lower()
    if low == "true":
        return True
    if low == "false":
        return False
    return s


def col_letters_to_index(letters: str) -> int:
    """A1 column letters → 0-based column index, bijective base-26
    (``'A'``→0, ``'Z'``→25, ``'AA'``→26 — no digit ``0``, so this is not
    plain base-26)."""
    idx = 0
    for ch in letters.upper():
        if not ("A" <= ch <= "Z"):
            raise BadInput(
                f"{letters!r} is not a valid A1 column (letters only)",
                next="cell='B2'  # column letters + row number",
            )
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def index_to_col_letters(index: int) -> str:
    """0-based column index → A1 column letters (inverse of
    :func:`col_letters_to_index`)."""
    n = index + 1
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


_A1_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def parse_cell_address(
    cell: str | dict[str, Any], *, n_rows: int, n_cols: int
) -> tuple[int, int]:
    """Resolve a caller-supplied cell address to ``(row1, col0)`` — a
    1-based row where **row 1 is the header row** (data rows are
    2..1+``n_rows``), and a 0-based column index.

    Accepts an A1 string (``'B2'``) or ``{'row': int, 'col': int}`` with
    1-based ints for both. Raises :class:`BadInput` (with the actual table
    dimensions named in ``next=``) on a malformed address or one out of
    range."""
    nxt = "cell='B2'  # A1 notation (row 1 = header) — or cell={'row': 2, 'col': 2}"
    if isinstance(cell, dict):
        if "row" not in cell or "col" not in cell:
            raise BadInput(
                f"cell={cell!r} must have both 'row' and 'col' (1-based ints)",
                next=nxt,
            )
        try:
            row1 = int(cell["row"])
            col1 = int(cell["col"])
        except (TypeError, ValueError) as exc:
            raise BadInput(
                f"cell row/col must be ints, got {cell!r}", next=nxt
            ) from exc
        col0 = col1 - 1
    elif isinstance(cell, str):
        m = _A1_RE.match(cell.strip())
        if not m:
            raise BadInput(
                f"cell={cell!r} is not valid A1 notation (column letters + "
                "row number, e.g. 'B2')",
                next=nxt,
            )
        col0 = col_letters_to_index(m.group(1))
        row1 = int(m.group(2))
    else:
        raise BadInput(
            f"cell must be an A1 string or {{'row':int,'col':int}}, got "
            f"{type(cell).__name__}",
            next=nxt,
        )
    max_row1 = 1 + n_rows
    if row1 < 1 or row1 > max_row1:
        raise BadInput(
            f"cell row {row1} out of range — table has {n_rows} data row(s) "
            f"(+1 header row), valid rows are 1..{max_row1}",
            next=nxt,
        )
    if col0 < 0 or col0 >= n_cols:
        raise BadInput(
            f"cell column out of range — table has {n_cols} column(s) "
            f"(A..{index_to_col_letters(n_cols - 1)})",
            next=nxt,
        )
    return row1, col0


def set_cell(
    table: dict[str, Any], cell: str | dict[str, Any], value_str: str
) -> dict[str, Any]:
    """Return a NEW normalised table with one field set — the coordinate
    edit path (the shipped draft-table-editing proposal item 1, git
    history). Row 1 (the
    header) coerces the value to ``str`` (a header is always a name); a
    data-row cell is type-inferred via :func:`infer_scalar` so a numeric
    edit stays a JSON number."""
    header = list(table["header"])
    rows = [list(r) for r in table["rows"]]
    row1, col0 = parse_cell_address(cell, n_rows=len(rows), n_cols=len(header))
    if row1 == 1:
        header[col0] = str(value_str)
    else:
        rows[row1 - 2][col0] = infer_scalar(value_str)
    return normalize_table({"header": header, "rows": rows})


def find_replace_cells(
    table: dict[str, Any], find: str, replace: str, *, regex: bool
) -> tuple[dict[str, Any], int]:
    """Find-replace over every STRING cell (header + body) of ``table`` —
    the cell-level counterpart of the draft's whole-chunk find/``sub``
    (the shipped draft-table-editing proposal item 1, git history).
    Non-string cells
    (numbers, bools, ``None``) are never touched, and a string cell that
    survives the replace stays a string (no re-inference — an edited
    string cell doesn't silently become a number). ``regex=False`` is a
    literal ``str.replace``; ``regex=True`` is ``re.sub`` (backreferences
    in ``replace`` resolve). Returns the new normalised table plus the
    total replacement count across every cell."""
    if not find:
        raise BadInput(
            "find/sub pattern must be a non-empty string",
            next="edit(kind='draft', id='dc<chunk_id>', find='old', text='new')",
        )
    pattern: re.Pattern[str] | None = None
    if regex:
        try:
            pattern = re.compile(find)
        except re.error as exc:
            raise BadInput(
                f"invalid regex {find!r}: {exc}",
                next="check the pattern — it is Python regex (\\w, \\d, groups, …)",
            ) from exc

    count = 0

    def _apply(s: str) -> str:
        nonlocal count
        if pattern is not None:
            new_s, n = pattern.subn(replace, s)
        else:
            n = s.count(find)
            new_s = s.replace(find, replace)
        count += n
        return new_s

    header = [_apply(h) if isinstance(h, str) else h for h in table["header"]]
    rows = [
        [_apply(c) if isinstance(c, str) else c for c in row] for row in table["rows"]
    ]
    return normalize_table({"header": header, "rows": rows}), count


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
        bracket_end = s.find("]")
        if bracket_end != -1:
            s = s[bracket_end + 1 :].lstrip()
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


# ---------------------------------------------------------------------------
# LaTeX in-place patching — the non-destructive sibling of the recovery above.
#
# A LaTeX-imported table chunk (no canonical ``meta.table``) is recovered for
# READING by re-parsing the raw text every time (:func:`parse_latex_table`),
# throwing the cleaned grid away afterwards. Editing used to persist that
# cleaned grid as GFM markdown instead — losing ``\label{}`` (dangling
# ``\ref``s elsewhere), flattening ``\multicolumn`` spans onto the wrong
# column, and dropping booktabs rules / ``\,`` thin spaces (gripe 271129).
#
# The functions below patch the addressed cell (or every cell matching a
# find/sub pattern) directly in the RAW LaTeX text instead. The parsed grid
# still supplies the ADDRESSING (row/col -> which raw cell), it just stops
# being the SERIALISATION — everything not touched by the edit, including
# ``\label``/rules/spacing and every other row, is copied through verbatim.
# ---------------------------------------------------------------------------


_CAPTION_CMD_RE = re.compile(r"\\caption\*?\s*\{")
_LABEL_CMD_RE = re.compile(r"\\label\s*\{")


def _blank_balanced_command(text: str, pattern: re.Pattern[str]) -> str:
    """Same-length sibling of :func:`_strip_one_arg_command`/
    :func:`_extract_caption`: every match of ``pattern`` (expected to end in
    an opening ``{``) through its balanced closing ``}`` is overwritten with
    spaces, so `text`'s length — and every other character's absolute
    position — is unchanged. Used only to keep the row/cell-boundary finder
    below honest when a ``\\caption{}``/``\\label{}`` ends up embedded
    inside a wrapper-less "captured tabular inner" (an importer shape
    :func:`parse_latex_table` also accepts) — real positions in the caller's
    original text stay valid indices into this blanked copy."""
    out = list(text)
    pos = 0
    while True:
        m = pattern.search(text, pos)
        if not m:
            break
        end = _find_balanced(text, m.end() - 1)
        if end is None:
            break
        for k in range(m.start(), end):
            out[k] = " "
        pos = end
    return "".join(out)


def _latex_position_shadow(text: str) -> str:
    """A same-length copy of `text` with any ``\\caption{}``/``\\label{}``
    blanked out — structural (row/cell span) lookups below run against this
    shadow so a caption/label anywhere in the text can never leak into a
    located cell's span; every surviving character sits at the SAME index
    as in `text`, so spans found here slice `text` correctly."""
    return _blank_balanced_command(
        _blank_balanced_command(text, _CAPTION_CMD_RE), _LABEL_CMD_RE
    )


def _tabular_env_span(text: str) -> tuple[int, int]:
    """Position-preserving sibling of :func:`_locate_tabular_body`: the
    ``(start, end)`` span of the tabular body's interior within `text`
    itself (``text[start:end]``, no slicing/offset bookkeeping needed by
    the caller). Mirrors that function's own fallbacks exactly — no inner
    ``\\begin{tabular…}`` → the whole string is the (bare-captured) body; an
    unmatched ``\\begin`` → everything after it."""
    m = _INNER_TABULAR_ENV_RE.search(text)
    if not m:
        return 0, len(text)
    env = m.group(1)
    begin_re = re.compile(r"\\begin\{" + re.escape(env) + r"\}")
    end_re = re.compile(r"\\end\{" + re.escape(env) + r"\}")
    depth = 1
    pos = m.end()
    while depth > 0:
        nb = begin_re.search(text, pos)
        ne = end_re.search(text, pos)
        if ne is None:
            return m.end(), len(text)
        if nb is not None and nb.start() < ne.start():
            depth += 1
            pos = nb.end()
        else:
            depth -= 1
            if depth == 0:
                return m.end(), ne.start()
            pos = ne.end()
    return m.end(), len(text)  # pragma: no cover — loop always returns above


def _skip_colspec(text: str, start: int) -> int:
    """Position-preserving sibling of :func:`_strip_colspec`: the index in
    `text` just past the colspec starting at `start` (an optional
    ``[pos]`` then one-or-more balanced ``{...}`` groups)."""
    i = start
    n = len(text)
    while i < n and text[i].isspace():
        i += 1
    if i < n and text[i] == "[":
        j = text.find("]", i)
        if j != -1:
            i = j + 1
            while i < n and text[i].isspace():
                i += 1
    while i < n and text[i] == "{":
        end = _find_balanced(text, i)
        if end is None:
            break
        i = end
        while i < n and text[i].isspace():
            i += 1
    return i


def _trim_row_leading_noise(text: str, start: int, end: int) -> int:
    """Advance `start` past interleaved whitespace + rule-macro tokens
    (``\\toprule``/``\\midrule``/…) at the front of a row's span. The
    ``\\\\``-based row split folds a rule macro sitting between two row
    separators into the FOLLOWING row's segment (it has no ``&``/``\\`` of
    its own to delimit it) — without this trim it would leak into that
    row's first cell."""
    i = start
    while i < end:
        j = i
        while j < end and text[j].isspace():
            j += 1
        m = _RULE_MACRO_RE.match(text, j, end)
        if m:
            i = m.end()
            continue
        return j
    return end


def _row_spans_abs(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """``(start, end)`` absolute spans of each non-blank row within
    ``text[start:end]`` — the position-preserving sibling of the row split
    + blank-row filter inside :func:`parse_latex_table` (split on the same
    ``_ROW_SPLIT_RE`` separators; a row that's blank once a leading rule
    macro is trimmed off contributes no row, mirroring that function's
    post-strip ``if r`` filter)."""
    bounds: list[tuple[int, int]] = []
    pos = start
    for m in _ROW_SPLIT_RE.finditer(text, start, end):
        bounds.append((pos, m.start()))
        pos = m.end()
    bounds.append((pos, end))
    spans: list[tuple[int, int]] = []
    for s, e in bounds:
        s2 = _trim_row_leading_noise(text, s, e)
        if text[s2:e].strip():
            spans.append((s2, e))
    return spans


def _split_row_cells_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Position-preserving sibling of :func:`_split_row_cells`: ``(start,
    end)`` absolute spans of each ``&``-delimited raw cell within
    ``text[start:end]`` (brace-depth aware, same as the string-returning
    original)."""
    spans: list[tuple[int, int]] = []
    depth = 0
    i = start
    cell_start = start
    while i < end:
        c = text[i]
        if c == "\\" and i + 1 < end:
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "&" and depth == 0:
            spans.append((cell_start, i))
            cell_start = i + 1
        i += 1
    spans.append((cell_start, end))
    return spans


def locate_latex_cell(text: str, row1: int, col0: int) -> tuple[int, int] | None:
    """Locate the raw-text ``(start, end)`` span of one cell in raw LaTeX
    ``tabular``-family `text`, addressed the same way as
    :func:`parse_cell_address` (``row1=1`` is the header row; data rows are
    ``2..``). Returns ``None`` — refuse, don't guess — when the table's
    row/cell shape can't be confidently re-derived: an out-of-range row, or
    a row whose raw ``&``-count is narrower than the requested column (a
    ``\\multicolumn`` row, whose parsed-grid width is wider than its raw
    cell count — the exact mismatch gripe 271129 exists to never paper
    over)."""
    try:
        shadow = _latex_position_shadow(text)
        env_start, env_end = _tabular_env_span(shadow)
        rows_start = _skip_colspec(shadow, env_start)
        row_spans = _row_spans_abs(shadow, rows_start, env_end)
        row_idx = row1 - 1
        if row_idx < 0 or row_idx >= len(row_spans):
            return None
        r_start, r_end = row_spans[row_idx]
        cell_spans = _split_row_cells_spans(shadow, r_start, r_end)
        if col0 < 0 or col0 >= len(cell_spans):
            return None
        return cell_spans[col0]
    except Exception:
        return None


def patch_latex_cell(text: str, row1: int, col0: int, value: str) -> str | None:
    """Return `text` with the addressed cell's raw content replaced by
    `value` — the LaTeX sibling of :func:`set_cell`. The cell's own
    surrounding whitespace is kept (so column alignment/spacing style is
    otherwise undisturbed) but its content is fully overwritten, same
    "set this field" semantics as the JSON-grid path (any macro/math
    wrapping *on that one cell* is not preserved — only the OTHER cells,
    the label, spans and rules are the guarantee here). Returns ``None``
    (refuse, don't guess) when :func:`locate_latex_cell` can't confidently
    locate the addressed cell."""
    span = locate_latex_cell(text, row1, col0)
    if span is None:
        return None
    start, end = span
    raw = text[start:end]
    lstripped = raw.lstrip()
    lead = raw[: len(raw) - len(lstripped)]
    trail = lstripped[len(lstripped.rstrip()) :]
    return text[:start] + lead + value + trail + text[end:]


def patch_latex_table_text(
    text: str, pattern: re.Pattern[str], replacement: str
) -> tuple[str, int]:
    """Apply a find/sub substitution directly to the row/cell area of raw
    LaTeX `text` — the LaTeX sibling of :func:`find_replace_cells`. The
    caption/label/colspec (everything before the first data cell) are
    never in scope, so they survive untouched even if `pattern` would
    otherwise match inside them. Returns the patched text plus the
    replacement count."""
    shadow = _latex_position_shadow(text)
    env_start, env_end = _tabular_env_span(shadow)
    rows_start = _skip_colspec(shadow, env_start)
    body = text[rows_start:env_end]
    new_body, n = pattern.subn(replacement, body)
    return text[:rows_start] + new_body + text[env_end:], n


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

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
import secrets
from collections.abc import Iterable
from typing import Any

from precis.errors import BadInput
from precis.utils.fractional import even_keys, key_between
from precis.utils.handle_registry import TABLE_ID_PATTERN, TABLE_SEL_PATTERN

#: JSON scalar types a cell may hold (preserved verbatim in ``meta.table``).
Scalar = str | int | float | bool | None

# ---------------------------------------------------------------------------
# Stable row/column ids (spreadsheet-tables slice 1a)
#
# Every row and column of a canonical table carries a random 4-char id,
# lowercase Crockford base-32 (``0-9a-z`` minus ``i l o u``), minted at
# creation and checked unique within the table — rows and columns share one
# pool, so an id resolves alone (``dc<id>~<id>`` is never row-vs-col
# ambiguous). The first character is a letter other than ``p``: the selector
# grammar already owns ``~N``, ``~N..M`` and ``~pN``, so ``4821``/``p123``
# shaped ids would collide. A deleted id is recorded in ``retired_ids`` and
# never re-minted. Order is separate from identity: each row/column carries
# a fractional ``pos`` key (:mod:`precis.utils.fractional`); insert-between
# mints a key, sort rewrites row keys only.
# ---------------------------------------------------------------------------

#: Crockford base-32 alphabet — ``0-9a-z`` minus ``i l o u``.
_ID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
#: First character: a letter, never ``p`` (and the dropped Crockford
#: letters). ``handle_registry.TABLE_ID_PATTERN`` is the same rule as a regex.
_ID_FIRST = "".join(c for c in _ID_ALPHABET if c.isalpha() and c != "p")

_ID_FULL_RE = re.compile(rf"^(?:{TABLE_ID_PATTERN})$")
_SEL_FULL_RE = re.compile(rf"^(?:{TABLE_SEL_PATTERN})$")

#: ``meta.table.columns`` entries may carry these keys (only ``name`` is
#: required; ``formula``/``from``/``role``/``key`` land semantics in later
#: slices — the schema accepts and preserves them now).
_COLUMN_KEYS = frozenset(
    {"id", "pos", "name", "type", "unit", "formula", "from", "role", "key", "align"}
)
#: ``columns[].type`` — ``type`` is inferred from the data when absent.
_COLUMN_TYPES = frozenset({"number", "text", "bool", "ref"})
#: ``columns[].align`` (the structured-enrichment vocabulary).
_COLUMN_ALIGNS = frozenset({"l", "c", "r"})


def mint_table_id() -> str:
    """A fresh 4-char table id: first char a letter other than ``p``, the
    rest Crockford base-32."""
    return secrets.choice(_ID_FIRST) + "".join(
        secrets.choice(_ID_ALPHABET) for _ in range(3)
    )


def is_table_id(s: Any) -> bool:
    """True iff ``s`` matches the stable row/column id shape."""
    return isinstance(s, str) and _ID_FULL_RE.match(s) is not None


def fresh_table_id(used: Iterable[str]) -> str:
    """Mint an id absent from ``used`` (live ids plus ``retired_ids``, so a
    deleted id never reappears). Redraws on collision."""
    pool = set(used)
    for _ in range(100):
        cand = mint_table_id()
        if cand not in pool:
            return cand
    raise BadInput("could not mint a unique table id after 100 draws")


def _normalize_column(col: Any, i: int) -> dict[str, Any]:
    """Validate one caller-supplied ``columns`` entry (an ``{id?, pos?,
    name, type?, unit?, formula?, from?, role?, key?, align?}`` dict);
    returns a shallow copy with only known keys."""
    nxt = "columns=[{'name': 'gap_eV', 'type': 'number', 'unit': 'eV'}]"
    if not isinstance(col, dict):
        raise BadInput(f"table.columns[{i}] must be an object", next=nxt)
    unknown = set(col) - _COLUMN_KEYS
    if unknown:
        raise BadInput(
            f"table.columns[{i}] has unknown key(s) {sorted(unknown)}",
            next=f"allowed: {sorted(_COLUMN_KEYS)}",
        )
    name = col.get("name")
    if not isinstance(name, str) or not name:
        raise BadInput(f"table.columns[{i}].name must be a non-empty string", next=nxt)
    typ = col.get("type")
    if typ is not None and typ not in _COLUMN_TYPES:
        raise BadInput(
            f"table.columns[{i}].type {typ!r} — must be one of {sorted(_COLUMN_TYPES)}",
            next=nxt,
        )
    align = col.get("align")
    if align is not None and align not in _COLUMN_ALIGNS:
        raise BadInput(
            f"table.columns[{i}].align {align!r} — must be one of "
            f"{sorted(_COLUMN_ALIGNS)}",
            next=nxt,
        )
    for k in ("id", "pos", "unit", "formula", "from", "role"):
        v = col.get(k)
        if v is not None and not isinstance(v, str):
            raise BadInput(f"table.columns[{i}].{k} must be a string", next=nxt)
    if col.get("key") is not None and not isinstance(col["key"], bool):
        raise BadInput(f"table.columns[{i}].key must be a bool", next=nxt)
    if col.get("id") is not None and not is_table_id(col["id"]):
        raise BadInput(
            f"table.columns[{i}].id {col['id']!r} is not a valid table id "
            f"(shape {TABLE_ID_PATTERN})",
            next=nxt,
        )
    return {k: v for k, v in col.items() if v is not None or k == "key"}


def _normalize_rowmeta(rm: Any, i: int) -> dict[str, Any]:
    """Validate one caller-supplied ``rowmeta`` entry (``{id?, pos?}``);
    returns a dict with the present keys only."""
    nxt = "rowmeta=[{'id': 'r2k7', 'pos': 'a'}] — one entry per row"
    if not isinstance(rm, dict):
        raise BadInput(f"table.rowmeta[{i}] must be an object", next=nxt)
    unknown = set(rm) - {"id", "pos"}
    if unknown:
        raise BadInput(
            f"table.rowmeta[{i}] has unknown key(s) {sorted(unknown)}", next=nxt
        )
    if rm.get("id") is not None and not is_table_id(rm["id"]):
        raise BadInput(
            f"table.rowmeta[{i}].id {rm['id']!r} is not a valid table id",
            next=nxt,
        )
    if rm.get("pos") is not None and not isinstance(rm["pos"], str):
        raise BadInput(f"table.rowmeta[{i}].pos must be a string", next=nxt)
    return {k: rm[k] for k in ("id", "pos") if k in rm}


def _fill_pos(items: list[dict[str, Any]]) -> None:
    """Assign a fractional ``pos`` to every entry missing one, in list
    order — a full fresh run uses :func:`even_keys`; a hole between two
    keyed neighbours gets :func:`key_between`."""
    if not any("pos" in it for it in items):
        for it, key in zip(items, even_keys(len(items)), strict=True):
            it["pos"] = key
        return
    for i, it in enumerate(items):
        if "pos" in it:
            continue
        prev = next(
            (items[j]["pos"] for j in range(i - 1, -1, -1) if "pos" in items[j]),
            None,
        )
        nxt = next(
            (items[j]["pos"] for j in range(i + 1, len(items)) if "pos" in items[j]),
            None,
        )
        it["pos"] = key_between(prev, nxt)


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

    The canonical shape is ``{header, rows, columns, rowmeta}``:

    - ``columns`` — ``[{id, pos, name, type?, unit?, formula?, from?,
      role?, key?, align?}]``, the column schema; ``name`` is unique per
      table so a column may be named by id or name.
    - ``rowmeta`` — ``[{id, pos}]``, parallel to ``rows`` (row identity +
      order live here, cells in ``rows``).
    - ``header`` — the derived column-name list kept for back-compat
      readers; recomputed from ``columns`` on every write.
    - ``retired_ids`` — ids of deleted rows/columns (absent until a
      delete); kept out of the mint pool so a deleted id never reappears.

    Caller input needs only ``header`` + ``rows``: missing ``columns``/
    ``rowmeta`` are synthesised (names from ``header``) and missing ids /
    ``pos`` keys minted. Supplied ids/pos are preserved (an edit rewrites
    cells, not identities) as long as they stay unique within the table.
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
    cols_raw = obj.get("columns")
    rowmeta_raw = obj.get("rowmeta")
    retired_raw = obj.get("retired_ids") or []
    if not isinstance(retired_raw, list):
        raise BadInput(
            f"table.retired_ids must be a list of ids, got "
            f"{type(retired_raw).__name__}",
            next=nxt,
        )
    if not isinstance(rows_raw, list):
        raise BadInput("table.rows must be a list of rows", next=nxt)
    if header_raw is None and cols_raw is not None:
        header_raw = [c.get("name") for c in cols_raw if isinstance(c, dict)]
    if not isinstance(header_raw, list) or not header_raw:
        raise BadInput(
            "table.header must be a non-empty list of column names", next=nxt
        )
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

    retired = [str(i) for i in retired_raw]
    used = set(retired)
    # Columns: supplied spec wins for the optional keys; names always come
    # from it when present and must match the header positionally (the
    # caller's header is then redundant but harmless — canonical output
    # re-derives it from names either way).
    if cols_raw is None:
        columns = [{"name": h} for h in header]
    else:
        if not isinstance(cols_raw, list) or len(cols_raw) != width:
            raise BadInput(
                f"table.columns must be a list of {width} column specs",
                next=nxt,
            )
        columns = [_normalize_column(c, i) for i, c in enumerate(cols_raw)]
        names = [c["name"] for c in columns]
        if len(set(names)) != len(names):
            raise BadInput(
                "table.columns names must be unique — a column is addressed "
                "by id or name",
                next=nxt,
            )
        if header_raw is not None and names != header:
            raise BadInput(
                f"table.columns names {names} disagree with header {header}",
                next="pass either header= or columns= — columns' names are "
                "the schema, header is derived from them",
            )
        header = names
    if rowmeta_raw is None:
        rowmeta: list[dict[str, Any]] = [{} for _ in rows]
    else:
        if not isinstance(rowmeta_raw, list) or len(rowmeta_raw) != len(rows):
            raise BadInput(
                f"table.rowmeta must be a list of {len(rows)} entries",
                next=nxt,
            )
        rowmeta = [_normalize_rowmeta(rm, i) for i, rm in enumerate(rowmeta_raw)]
    for entry in [*columns, *rowmeta]:
        entry_id = entry.get("id")
        if entry_id is not None:
            if entry_id in used:
                raise BadInput(
                    f"table id {entry_id!r} is already taken in this table "
                    "(rows and columns share one id pool)",
                    next="drop the duplicate id and it re-mints",
                )
            used.add(entry_id)
    for entry in [*columns, *rowmeta]:
        if "id" not in entry:
            entry["id"] = fresh_table_id(used)
            used.add(entry["id"])
    _fill_pos(columns)
    _fill_pos(rowmeta)
    out: dict[str, Any] = {
        "header": header,
        "rows": rows,
        "columns": columns,
        "rowmeta": rowmeta,
    }
    if retired:
        out["retired_ids"] = retired
    return out


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


def cell_next_hint() -> str:
    """The ``next=`` hint pointing at ``cell='<row-id>.<col-id>'``
    addressing — one string shared by every refusal so it always matches
    the ``ids:`` line ``get`` renders at read time from ``meta.table``."""
    return (
        "cell='<row-id>.<col-id>' — e.g. cell='r2k7.k5f9' (stable ids, "
        "not A1); get(kind='draft', id='dc<id>') lists them on the "
        "'ids:' line under the grid"
    )


def resolve_cell(table: dict[str, Any], cell: Any) -> tuple[int, int]:
    """Resolve a ``cell='<row-id>.<col-id>'`` address to ``(row_index,
    col_index)`` into ``rows``/``columns``.

    Row ids come from ``rowmeta``; the column side resolves by id, then by
    exact ``name`` (columns may be named either way). A1 strings
    (``'B3'``) and ``{'row','col'}`` dicts are gone — the refusal's
    ``next=`` shows the id form. Raises :class:`BadInput` naming the
    offending half when an id is unknown."""
    nxt = cell_next_hint()
    if not isinstance(cell, str):
        raise BadInput(
            f"cell={cell!r} must be a '<row-id>.<col-id>' string",
            next=nxt,
        )
    row_id, sep, col_ref = cell.strip().partition(".")
    if not sep or not row_id or not col_ref:
        raise BadInput(
            f"cell={cell!r} is not a cell address — the form is '<row-id>.<col-id>'",
            next=nxt,
        )
    rowmeta = table.get("rowmeta") or []
    row_i = next((i for i, rm in enumerate(rowmeta) if rm.get("id") == row_id), None)
    if row_i is None:
        raise BadInput(
            f"cell={cell!r}: no row id {row_id!r} in this table",
            options=[rm.get("id") for rm in rowmeta[:20] if rm.get("id")],
            next=nxt,
        )
    columns = table.get("columns") or []
    col_i = next((i for i, c in enumerate(columns) if c.get("id") == col_ref), None)
    if col_i is None:
        col_i = next(
            (i for i, c in enumerate(columns) if c.get("name") == col_ref), None
        )
    if col_i is None:
        raise BadInput(
            f"cell={cell!r}: no column id/name {col_ref!r} in this table",
            options=[str(c.get("id") or c.get("name")) for c in columns[:20]],
            next=nxt,
        )
    return row_i, col_i


def set_cell(table: dict[str, Any], cell: str, value_str: str) -> dict[str, Any]:
    """Return a NEW normalised table with one data cell set, addressed by
    stable ids (``cell='r2k7.k5f9'`` — the ``<row-id>.<col-id>`` form that
    replaced A1). The value is type-inferred via :func:`infer_scalar` so a
    numeric edit stays a JSON number. Column names are set through the
    column schema (``rename_column=``), never through a cell address."""
    rows = [list(r) for r in table["rows"]]
    row_i, col_i = resolve_cell(table, cell)
    rows[row_i][col_i] = infer_scalar(value_str)
    return normalize_table({**table, "rows": rows})


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

    rows = [
        [_apply(c) if isinstance(c, str) else c for c in row] for row in table["rows"]
    ]
    # With a column schema the header is derived — replace in
    # ``columns[].name`` and let normalize_table re-derive it (a rename
    # colliding with another name refuses there, as it should). Without
    # one (a pre-schema table dict) the header is the name list.
    out: dict[str, Any] = {**table, "rows": rows}
    if table.get("columns"):
        out["columns"] = [
            {**c, "name": _apply(c["name"])} if isinstance(c.get("name"), str) else c
            for c in table["columns"]
        ]
        out["header"] = [c["name"] for c in out["columns"]]
    else:
        out["header"] = [
            _apply(h) if isinstance(h, str) else h for h in table["header"]
        ]
    return normalize_table(out), count


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


def table_ids_line(table: dict[str, Any]) -> str | None:
    """The ``ids:`` trailer the agent-facing read paths append under the
    grid — the stable row/column ids ``cell='<row-id>.<col-id>'`` and
    ``dc<N>~<id>`` address by (spreadsheet-tables 1a). Ids print in grid
    order, so the agent pairs a row's line with its id positionally and a
    column's header with its id the same way. ``None`` for a bare
    ``{header, rows}`` table — a pre-schema grid with no ids minted yet
    renders as plain markdown, unchanged.

    The line is rendered **from ``meta.table`` at read time**
    (``DraftHandler._render_chunk`` / ``_render_table_select``), never
    stored in ``chunks.text`` — keeping it out of the stored text means
    the backfill's meta-only write needs no text re-derive, and
    embeddings/``content_sha`` never carry it."""
    row_ids = [
        rm["id"]
        for rm in table.get("rowmeta") or []
        if isinstance(rm, dict) and rm.get("id")
    ]
    col_ids = [
        c["id"]
        for c in table.get("columns") or []
        if isinstance(c, dict) and c.get("id")
    ]
    if not row_ids and not col_ids:
        return None
    parts = []
    if row_ids:
        parts.append("rows " + " ".join(row_ids))
    if col_ids:
        parts.append("cols " + " ".join(col_ids))
    return "ids: " + "; ".join(parts)


def table_to_markdown(table: dict[str, Any], *, caption: str | None = None) -> str:
    """Render a normalised ``{header, rows}`` table to a single GFM block.

    The result has **no internal blank line** so the ``add_chunks``
    blank-line splitter keeps the whole table in one chunk. An optional
    ``caption`` (the table's legend) is rendered as a leading ``**…**``
    line so it stays in the embeddable ``text`` projection without
    breaking the block. The stored text is always plain GFM — the
    ``ids:`` line naming the table's stable row/column ids is NOT part
    of it; ``get`` renders it from ``meta.table`` at read time
    (:func:`table_ids_line`), so ids never reach the embedding or
    ``content_sha``.
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


def _extract_label(text: str) -> tuple[str | None, str]:
    """Pull the first ``\\label{...}`` (balanced braces) out of ``text``,
    returning ``(inner, text-with-label-removed)`` — mirrors
    :func:`_extract_caption` (gripe 271129: the anchor used to be silently
    discarded (stripped, not captured) instead)."""
    m = re.search(r"\\label\s*\{", text)
    if not m:
        return None, text
    end = _find_balanced(text, m.end() - 1)
    if end is None:
        return None, text
    inner = text[m.end() : end - 1]
    return inner, text[: m.start()] + text[end:]


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
    """Recover ``{header, rows, caption, label}`` from raw LaTeX
    ``tabular``-family text — the fallback for a LaTeX-imported
    ``chunk_kind='table'`` chunk that has no canonical ``meta.table``
    (gr51405 + gr52564). Accepts a bare tabular body (importer's captured
    ``\\begin{tabular}`` inner, starting with the ``{colspec}``) or a full
    float wrapper (``table``/``longtable`` with a ``\\caption{}`` and a
    nested ``tabular``).

    ``label`` is the ``\\label{...}`` argument (``None`` when absent) —
    captured, not discarded, so a caller that persists the recovered grid
    has somewhere to carry the cross-reference anchor forward (gripe
    271129: it used to be stripped with no trace, silently orphaning any
    ``\\ref`` elsewhere).

    Structureless input (prose, an empty string, a GFM table chunk the
    markdown parser already handled) returns ``None`` — this runs on
    arbitrary table-chunk text at render time and must never raise."""
    try:
        if not text or not text.strip():
            return None
        body = _strip_latex_comments(text)
        caption, body = _extract_caption(body)
        label, body = _extract_label(body)
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
        return {
            "header": header,
            "rows": rows,
            "caption": cap_clean or None,
            "label": label.strip() if label else None,
        }
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
    """Same-length sibling of :func:`_extract_caption`/:func:`_extract_label`:
    every match of ``pattern`` (expected to end in
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


def _caption_inner_span(text: str) -> tuple[int, int] | None:
    """Position span of the first ``\\caption{...}``'s inner content in
    `text` (position-preserving sibling of :func:`_extract_caption`; a
    starred ``\\caption*`` matches too). ``None`` when there is no caption
    or its braces don't balance."""
    m = _CAPTION_CMD_RE.search(text)
    if not m:
        return None
    end = _find_balanced(text, m.end() - 1)
    if end is None:
        return None
    return m.end(), end - 1


def _post_tabular_span(text: str) -> tuple[int, int]:
    """Position span of `text` AFTER the closing ``\\end{tabular…}`` of the
    outer table environment — footnote/note prose trailing the grid
    (gr344147). Empty (``len(text), len(text)``) when the tabular body
    runs to the end of `text` (the bare-captured-inner or unmatched-
    ``\\begin`` fallbacks of :func:`_tabular_env_span`, neither of which
    leaves anything after the body to widen into)."""
    shadow = _latex_position_shadow(text)
    _, env_end = _tabular_env_span(shadow)
    if env_end >= len(text):
        return len(text), len(text)
    m = re.match(r"\\end\{[^}]*\}", text[env_end:])
    if not m:
        return len(text), len(text)
    start = env_end + m.end()
    return start, len(text)


def patch_latex_table_text(
    text: str, pattern: re.Pattern[str], replacement: str
) -> tuple[str, int]:
    """Apply a find/sub substitution directly to raw LaTeX `text` — the
    LaTeX sibling of :func:`find_replace_cells`. Tries the row/cell area
    (everything between the colspec and the closing ``\\end{tabular…}``)
    first, exactly as before; the label/colspec stay out of scope there,
    so they survive untouched even if `pattern` would otherwise match
    inside them.

    A body match wins outright (every occurrence in the body is
    replaced, same as before). Only when the body has ZERO matches does
    the search widen to the caption (``\\caption{...}``'s inner content)
    and to any note/footnote text after ``\\end{tabular…}`` (gr344147: a
    cite macro in a caption or a trailing note is real content the
    renderer linkifies the same as a cell, so refusing it as "no cell
    matches" forced hand edits outside the tool). A pattern that matches
    in BOTH the caption and the note is ambiguous — refuse rather than
    guess which one the caller meant. A match confined to one of the two
    replaces every occurrence there, same all-matches semantics as the
    body path.

    Returns the patched text plus the replacement count."""
    shadow = _latex_position_shadow(text)
    env_start, env_end = _tabular_env_span(shadow)
    rows_start = _skip_colspec(shadow, env_start)
    body = text[rows_start:env_end]
    new_body, n = pattern.subn(replacement, body)
    if n:
        return text[:rows_start] + new_body + text[env_end:], n
    cap_span = _caption_inner_span(text)
    note_start, note_end = _post_tabular_span(text)
    cap_hit = cap_span is not None and pattern.search(text[cap_span[0] : cap_span[1]])
    note_hit = note_start < note_end and pattern.search(text[note_start:note_end])
    if cap_hit and note_hit:
        raise BadInput(
            f"/{pattern.pattern}/ matches both the caption and a note after "
            "the table — ambiguous, nothing replaced.",
            next="narrow find=/sub= so it matches only the caption or only "
            "the note, or edit that one span directly",
        )
    if cap_span is not None and cap_hit:
        start, end = cap_span
        new_inner, n2 = pattern.subn(replacement, text[start:end])
        return text[:start] + new_inner + text[end:], n2
    if note_hit:
        new_note, n2 = pattern.subn(replacement, text[note_start:note_end])
        return text[:note_start] + new_note + text[note_end:], n2
    return text, 0


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

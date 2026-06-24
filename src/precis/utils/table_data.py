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

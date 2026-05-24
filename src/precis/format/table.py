"""ASCII box-drawing table renderer for TTY-default CLI output.

Deliberately tiny — operator-readable rows in a monospace terminal,
no colour, no external dependency. The visual contract is:

::

    ┌──────────┬────────┐
    │ handler  │ status │
    ├──────────┼────────┤
    │ embed    │ ok     │
    │ summary  │ ok     │
    └──────────┴────────┘

A 0-row table renders only the header inside the box (no separator
row, no data). A 1+-row table puts a separator between header and
data, but rows themselves are not separated — cramming a `├──┤`
between every row triples the line count without helping readability.

The renderer accepts the same `data` / `schema` arguments as
`precis.format.toon.dump`; scalar coercion is identical so an
operator switching between formats sees the same cell contents.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Box-drawing glyphs (`U+2500` family). ASCII-printable in any
# monospace terminal. Listed here so a future change is one spot,
# not search-and-replace across the file.
_H = "─"
_V = "│"
_TL = "┌"
_TR = "┐"
_BL = "└"
_BR = "┘"
_LCROSS = "├"
_RCROSS = "┤"
_TCROSS = "┬"
_BCROSS = "┴"
_CROSS = "┼"


def render(
    data: list[Mapping[str, Any]] | Mapping[str, Any],
    *,
    schema: list[str] | None = None,
    **_kwargs: Any,  # absorb sep= from the registry shim
) -> str:
    """Render *data* as an ASCII box-drawing table.

    The ``**_kwargs`` swallow is intentional — :func:`precis.format.serialize`
    passes every kwarg through to the chosen renderer; ``sep=`` is
    meaningful for TOON but a no-op here.
    """
    if isinstance(data, Mapping):
        rows: list[Mapping[str, Any]] = [data]
    elif isinstance(data, list):
        rows = data
    else:
        raise TypeError(
            f"table.render: expected list of mappings or a single mapping; "
            f"got {type(data).__name__}"
        )

    columns = _resolve_columns(rows, schema)
    if not rows and not columns:
        return ""

    # Stringify every cell up-front so column widths can be measured.
    str_rows: list[list[str]] = [
        [_stringify(row.get(c)) for c in columns] for row in rows
    ]
    widths = [len(c) for c in columns]
    for srow in str_rows:
        for i, cell in enumerate(srow):
            if len(cell) > widths[i]:
                widths[i] = len(cell)

    def _rule(left: str, mid: str, right: str) -> str:
        return left + mid.join(_H * (w + 2) for w in widths) + right

    def _row(cells: list[str]) -> str:
        return (
            _V
            + _V.join(f" {cell.ljust(widths[i])} " for i, cell in enumerate(cells))
            + _V
        )

    lines: list[str] = []
    lines.append(_rule(_TL, _TCROSS, _TR))
    lines.append(_row(columns))
    if str_rows:
        lines.append(_rule(_LCROSS, _CROSS, _RCROSS))
        for srow in str_rows:
            lines.append(_row(srow))
    lines.append(_rule(_BL, _BCROSS, _BR))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_columns(
    rows: list[Mapping[str, Any]],
    schema: list[str] | None,
) -> list[str]:
    """Column order: pinned schema wins, else first-seen union."""
    if schema is not None:
        return list(schema)
    seen: dict[str, None] = {}
    for row in rows:
        for k in row:
            if k not in seen:
                seen[k] = None
    return list(seen)


def _stringify(value: Any) -> str:
    """Render a cell value to its display string.

    Kept aligned with `precis.format.toon._encode_cell` (minus the
    quoting step) so an operator who switches between ``--format
    toon`` and ``--format table`` sees the same cell contents.
    """
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return value
    return str(value)


__all__ = ["render"]

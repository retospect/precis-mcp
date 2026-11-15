"""Table lens — one shared server-side table protocol for tabular MCP
views (gr592637).

A tabular view is a *lens* on rows that stay in the DB: the caller
narrows with ``where``, orders with ``sort``, selects ``columns``, caps
with ``limit`` and pages with ``cursor`` — the response stays small
without losing fidelity, because every trimmed row is reachable with
another call (the footer names the exact one). Programmatic lenses only;
an LLM summary in this path would lose what a re-read could get back.

The protocol (one shape for every tabular view):

- ``where={col: value|range|set}`` — equality (numeric-aware, so
  ``'yes'`` matches ``'yes'`` and ``0.5`` matches ``'≈0.5'``), a
  ``{'min': x, 'max': y}`` range (either end optional), or a
  list/tuple/set membership test.
- ``sort='-energy'`` / ``'name'`` or a list of those (or
  ``[(col, asc), ...]``) — applied left to right; missing values
  (``None``/``''``/``'-'``) sink to the tail in every direction.
- ``columns=[...]`` — the projection, in the given order.
- ``limit=N`` — the page size.
- ``cursor=<token>`` — an opaque continuation from a previous page's
  footer. The cursor pins ``where``/``sort``/``columns``/``limit``;
  passing a different value for any of them alongside the cursor is
  refused as stale (:class:`BadInput`) rather than silently re-cutting
  a different table.

The response always carries ``total`` (rows before ``where``),
``matched`` (rows after ``where``), per-column aggregates over the
hidden rows (count/min/max/mean for numeric columns, a value histogram
for low-cardinality enums), and — while rows remain — a cursor whose
footer line names the exact call that continues the read. A byte guard
caps the rendered page near :data:`DEFAULT_BYTE_BUDGET`: a full page
defers its tail to the cursor, and a single row wider than the budget by
itself is omitted whole — a row the caller asked for is never cut
mid-row — with the footer naming how to reach it (narrow ``columns=``,
raise the budget).

Consumers: ``view='results'``/``'leaderboard'``/``'frontier'`` on the
quest handler, ``view='compare'`` on the pathway handler (this module
owns the mechanism; gr592638/gr592563 consume it).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from precis.errors import BadInput

#: Default byte budget for one lens page — sized so the common answer
#: fits in ~4 kB (the transport cap pain in gr592276/gr592637); views
#: pass a smaller or larger one where their row width demands it.
DEFAULT_BYTE_BUDGET = 4096

#: Cursor payload version — a future incompatible encoding refuses old
#: tokens instead of misreading them.
_CURSOR_VERSION = 1

#: Aggregate rendering caps: a column whose distinct-value count passes
#: _HISTOGRAM_CARDINALITY collapses to a count+distinct line; the block
#: itself caps at _MAX_AGGREGATE_LINES lines so the footer stays a
#: footer.
_HISTOGRAM_CARDINALITY = 8
_MAX_AGGREGATE_LINES = 8
_AGG_VALUE_CLIP = 24

_MISSING = (None, "", "-")


def _num(value: Any) -> float | None:
    """Numeric reading of a cell, or ``None``.

    Strips the provisional ``≈`` marker so ``'≈0.5'`` compares and
    sorts as ``0.5``; bools and unparseable strings are not numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        text = value.strip().removeprefix("≈").strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _is_missing(value: Any) -> bool:
    return value is None or value in _MISSING


def _scalar_eq(row_value: Any, where_value: Any) -> bool:
    """One ``where`` scalar against one cell: exact match, else numeric
    match (``0.5`` hits ``'≈0.5'``), else string equality."""
    if row_value == where_value:
        return True
    rn, wn = _num(row_value), _num(where_value)
    if rn is not None and wn is not None:
        return rn == wn
    return str(row_value) == str(where_value)


def _matches(row: Mapping[str, Any], where: Mapping[str, Any]) -> bool:
    for col, cond in where.items():
        value = row.get(col)
        if isinstance(cond, Mapping):
            known = set(cond) - {"min", "max"}
            if known:
                raise BadInput(
                    f"where={{{col!r}: …}}: range conditions take 'min'/'max'; "
                    f"unknown keys: {', '.join(sorted(map(str, known)))}",
                    next=f"where={{{col!r}: {{'min': 0, 'max': 1}}}} or a plain value",
                )
            num = _num(value)
            for end in ("min", "max"):
                if end in cond and _num(cond[end]) is None:
                    raise BadInput(
                        f"where={{{col!r}: {{'{end}': …}}}}: {end} must be a number",
                        next=f"where={{{col!r}: {{'{end}': 0}}}}",
                    )
            lo = _num(cond["min"]) if "min" in cond else None
            hi = _num(cond["max"]) if "max" in cond else None
            if lo is not None and (num is None or num < lo):
                return False
            if hi is not None and (num is None or num > hi):
                return False
        elif isinstance(cond, list | tuple | set | frozenset):
            if not any(_scalar_eq(value, option) for option in cond):
                return False
        elif not _scalar_eq(value, cond):
            return False
    return True


def _normalize_sort(sort: Any) -> list[tuple[str, bool]]:
    """``sort`` → ``[(column, ascending)]``.

    Accepts one key (``'-energy'`` descending, ``'name'`` ascending), a
    list of those, or ``[(col, asc_bool), ...]`` pairs. Anything else is
    a :class:`BadInput` naming the shape."""
    if sort is None:
        return []
    items: list[Any] = [sort] if isinstance(sort, str) else list(sort)
    out: list[tuple[str, bool]] = []
    for item in items:
        if isinstance(item, str):
            name, asc = item.lstrip("+-"), not item.startswith("-")
        elif isinstance(item, tuple | list) and len(item) == 2:
            name, asc = str(item[0]), bool(item[1])
        else:
            raise BadInput(
                f"sort item {item!r}: a key ('-energy') or a (column, asc) pair",
                next="sort='-energy' or sort=[('energy', False), ('name', True)]",
            )
        if not name:
            raise BadInput(
                f"sort item {item!r}: the column name is empty",
                next="sort='-energy'",
            )
        out.append((name, asc))
    return out


def _sort_key(spec: list[tuple[str, bool]]):
    """Key function for one normalized sort spec.

    Missing cells sink to the tail in every direction (a ``None``
    energy never outranks a measured one because the read was
    descending); descending strings wrap in :class:`_Reversed`."""

    def _key(row: Mapping[str, Any]) -> tuple:
        parts: list[tuple[int, Any]] = []
        for col, asc in spec:
            value = row.get(col)
            if _is_missing(value):
                parts.append((1, 0.0))
                continue
            num = _num(value)
            if num is not None:
                parts.append((0, num if asc else -num))
            else:
                text = str(value)
                parts.append((0, text if asc else _Reversed(text)))
        return tuple(parts)

    return _key


class _Reversed:
    """A string that orders opposite — the descending-string key."""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Reversed) and self.text == other.text

    def __lt__(self, other: _Reversed) -> bool:
        return self.text > other.text

    def __hash__(self) -> int:
        return hash(("reversed", self.text))


def _normalize_columns(columns: Any) -> list[str] | None:
    if columns is None:
        return None
    if not isinstance(columns, list | tuple) or not all(
        isinstance(c, str) and c for c in columns
    ):
        raise BadInput(
            f"columns={columns!r}: a list of column names",
            next="columns=['handle', 'energy', 'trusted']",
        )
    return list(dict.fromkeys(columns))


def _normalize_limit(limit: Any) -> int | None:
    if limit is None:
        return None
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise BadInput(
            f"limit={limit!r}: a positive int (rows per page)",
            next="args={'limit': 50}",
        )
    return limit


def _encode_cursor(state: Mapping[str, Any]) -> str:
    raw = json.dumps(state, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(token: Any) -> dict[str, Any]:
    if not isinstance(token, str) or not token:
        raise BadInput(
            f"cursor={token!r}: an opaque token from a previous page's footer",
            next="re-read without args={'cursor': …} to start a new page",
        )
    try:
        state = json.loads(base64.urlsafe_b64decode(token.encode("ascii")))
    except Exception:
        state = None
    if (
        not isinstance(state, dict)
        or state.get("v") != _CURSOR_VERSION
        or not isinstance(state.get("o"), int)
    ):
        raise BadInput(
            "cursor is not a readable table-lens token (stale, foreign or truncated)",
            next="re-read without args={'cursor': …} to start a new page",
        )
    return state


@dataclass
class LensPage:
    """One page of a lens read."""

    rows: list[dict[str, Any]]
    #: The projection the page was cut from (caller's ``columns`` or the
    #: rows' first-seen union).
    columns: list[str]
    #: Row count before ``where``.
    total: int
    #: Row count after ``where``, before paging.
    matched: int
    #: Rows in this page (after the byte guard).
    shown: int
    #: Page-tail rows the byte guard deferred to the next page (still
    #: reachable through the cursor).
    deferred: int
    #: Rows wider than ``byte_budget`` by themselves — omitted whole,
    #: named in the footer, reachable by narrowing ``columns=`` or
    #: raising the budget.
    skipped_wide: int
    #: Continuation token, ``None`` when no matched row remains after
    #: this page.
    next_cursor: str | None
    #: Rendered per-column aggregates over the hidden rows.
    aggregate_lines: list[str]
    where: dict[str, Any] = field(default_factory=dict)
    sort: list[tuple[str, bool]] = field(default_factory=list)
    #: Every matched row after ``where`` and ``sort`` (unprojected) —
    #: views that group the page (the frontier's bands) read their full
    #: group counts here instead of re-implementing the match.
    matched_rows: list[dict[str, Any]] = field(default_factory=list)

    def footer(self, call: str) -> str:
        """The trailing block: counts, hidden-row aggregates, and — while
        rows remain — the exact call that continues the read.

        ``call`` is the MCP call prefix without the closing paren, e.g.
        ``get(kind='quest', id='qu164903', view='results'`` — the
        footer's ``next:`` line is that call plus the cursor arg, never
        an unrelated search hint (gr592637)."""
        bits = [f"{self.shown} of {self.matched} rows"]
        if self.matched != self.total:
            bits.append(f"{self.total} total")
        if self.deferred:
            bits.append(f"{self.deferred} continue on the next page")
        if self.skipped_wide:
            bits.append(
                f"{self.skipped_wide} row(s) wider than the byte budget — "
                "narrow columns= or raise the budget"
            )
        lines = ["lens: " + " · ".join(bits)]
        if self.aggregate_lines:
            lines.append("hidden-row aggregates: " + "; ".join(self.aggregate_lines))
        if self.next_cursor is not None:
            lines.append(f"next: {call}, args={{'cursor': {self.next_cursor!r}}})")
        return "\n".join(lines)


def _aggregate_lines(
    hidden: Sequence[Mapping[str, Any]],
    columns: Sequence[str],
) -> list[str]:
    """Per-column aggregates over the rows a page hides.

    Numeric columns report count/min/max/mean; low-cardinality columns
    a value histogram (``trusted {no: 15, yes: 20}``); the rest collapse
    to count + distinct. Capped so the footer stays a footer."""
    lines: list[str] = []
    for col in columns:
        values = [r.get(col) for r in hidden]
        present = [v for v in values if not _is_missing(v)]
        nums = [_num(v) for v in present]
        numeric = bool(present) and all(n is not None for n in nums)
        if numeric:
            cast = [float(n) for n in nums if n is not None]
            mean = sum(cast) / len(cast)
            lines.append(
                f"{col} n={len(cast)} min={min(cast):g} max={max(cast):g} mean={mean:g}"
            )
        else:
            hist: dict[str, int] = {}
            for v in present:
                key = str(v)
                if len(key) > _AGG_VALUE_CLIP:
                    key = key[: _AGG_VALUE_CLIP - 1] + "…"
                hist[key] = hist.get(key, 0) + 1
            if len(hist) <= _HISTOGRAM_CARDINALITY:
                body = ", ".join(
                    f"{k}: {n}"
                    for k, n in sorted(hist.items(), key=lambda kv: (-kv[1], kv[0]))
                )
                lines.append(f"{col} {{{body}}}")
            else:
                lines.append(f"{col} n={len(present)} distinct={len(hist)}")
    if len(lines) > _MAX_AGGREGATE_LINES:
        rest = len(lines) - _MAX_AGGREGATE_LINES
        lines = lines[:_MAX_AGGREGATE_LINES]
        lines.append(f"(+{rest} more column aggregate(s) — narrow columns= to choose)")
    return lines


def apply_lens(
    rows: Sequence[Mapping[str, Any]],
    *,
    where: Mapping[str, Any] | None = None,
    sort: Any = None,
    columns: Sequence[str] | None = None,
    limit: int | None = None,
    cursor: Any = None,
    byte_budget: int = DEFAULT_BYTE_BUDGET,
    internal: Sequence[str] = (),
    row_bytes: Callable[[Mapping[str, Any], Sequence[str]], int] | None = None,
) -> LensPage:
    """Cut ``rows`` through the shared table-lens protocol.

    ``where``/``sort``/``columns``/``limit`` are the caller's knobs (see
    the module docstring); ``cursor`` is a continuation token from an
    earlier page and pins all four — passing a different value for any
    of them alongside it raises :class:`BadInput` (a stale cursor must
    never silently re-cut a different table). ``byte_budget`` caps the
    page's rendered width: a full page defers its tail — whole rows — to
    the cursor, and a single row wider than the budget by itself is
    omitted whole (never cut mid-row) and named in the footer with its
    escape hatch. ``internal`` names columns kept for ordering but never
    aggregated or returned (``ref_id``); ``row_bytes`` measures one
    rendered row — the plain column join by default, a view's real
    renderer when it has one.
    """
    where = dict(where or {})
    sort_spec: list[tuple[str, bool]] = _normalize_sort(sort)
    column_list = _normalize_columns(columns)
    limit = _normalize_limit(limit)

    if cursor is not None:
        state = _decode_cursor(cursor)
        pinned_where = state.get("w", {})
        pinned_sort = [tuple(pair) for pair in state.get("s", [])]
        pinned_columns = state.get("c")
        pinned_limit = state.get("l")
        for name, pinned, given in (
            ("where", pinned_where, where),
            ("sort", pinned_sort, sort_spec),
            ("columns", pinned_columns, column_list),
            ("limit", pinned_limit, limit),
        ):
            if given and given != pinned:
                raise BadInput(
                    f"args={{{name!r}: …}} disagrees with the cursor's pinned "
                    f"{name}={pinned!r} — the page was cut from that read",
                    next=f"drop args={{{name!r}: …}} and follow the cursor, or "
                    f"drop args={{'cursor': …}} and start a new page",
                )
        where, sort_spec, column_list, limit = (
            pinned_where,
            [(str(c), bool(a)) for c, a in pinned_sort],
            pinned_columns,
            pinned_limit,
        )
        offset = state["o"]
        if offset < 0:
            raise BadInput(
                f"cursor offset {offset}: a tampered cursor",
                next="re-read without args={'cursor': …} to start a new page",
            )
    else:
        offset = 0

    all_rows = [dict(r) for r in rows]
    total = len(all_rows)
    matched_rows = [r for r in all_rows if _matches(r, where)]
    matched = len(matched_rows)
    if sort_spec:
        matched_rows.sort(key=_sort_key(sort_spec))

    if column_list is None:
        seen: dict[str, None] = {}
        for r in matched_rows or all_rows:
            for key in r:
                if key not in seen:
                    seen[key] = None
        column_list = [c for c in seen if c not in internal]

    def _row_bytes(row: Mapping[str, Any], cols: Sequence[str]) -> int:
        if row_bytes is not None:
            return row_bytes(row, cols)
        return len("  ".join(str(row.get(c, "")) for c in cols))

    header_bytes = len("  ".join(column_list))
    page: list[dict[str, Any]] = []
    deferred = 0
    skipped_wide = 0
    consumed = 0
    size = header_bytes
    for row in matched_rows[offset : offset + limit if limit is not None else None]:
        width = _row_bytes(row, column_list)
        consumed += 1
        if width + header_bytes > byte_budget:
            # Wider than the budget by itself: rendering it whole would
            # bust the cap every page (the cursor would never advance).
            # Omit it whole — never cut mid-row — and name the escape
            # hatch in the footer.
            skipped_wide += 1
            continue
        if page and size + width > byte_budget:
            # The page is full: defer the tail to the next page, whole
            # rows only.
            deferred += 1
            continue
        page.append(row)
        size += width
    shown = len(page)
    page = [{c: row.get(c, "") for c in column_list} for row in page]
    # Deferred rows are not consumed — the next page starts at the first
    # of them. Skipped-wide rows ARE consumed: omitted for good (named
    # in the footer with the escape hatch), so the cursor never loops
    # on a row that can never fit.
    next_offset = offset + consumed - deferred
    next_cursor = None
    if next_offset < matched:
        next_cursor = _encode_cursor(
            {
                "v": _CURSOR_VERSION,
                "w": where,
                "s": [[c, a] for c, a in sort_spec],
                "c": column_list,
                "l": limit,
                "o": next_offset,
            }
        )
    hidden = matched_rows[:offset] + matched_rows[next_offset:]
    aggregate_cols = [c for c in column_list if c not in internal]
    return LensPage(
        rows=page,
        columns=column_list,
        total=total,
        matched=matched,
        shown=shown,
        deferred=deferred,
        skipped_wide=skipped_wide,
        next_cursor=next_cursor,
        aggregate_lines=_aggregate_lines(hidden, aggregate_cols),
        where=dict(where),
        sort=sort_spec,
        matched_rows=matched_rows,
    )


def lens_kwargs(args: Mapping[str, Any] | None) -> dict[str, Any]:
    """The lens knobs out of a handler's raw ``args=`` dict — the view
    pops them before its own arg parsing so each key is read once.

    Only keys actually present land in the result; validation of each
    value happens in :func:`apply_lens` (one BadInput vocabulary for
    every view)."""
    out: dict[str, Any] = {}
    if not isinstance(args, dict):
        return out
    for key in ("where", "sort", "columns", "limit", "cursor"):
        if key in args:
            out[key] = args.pop(key)
    return out


__all__ = [
    "DEFAULT_BYTE_BUDGET",
    "LensPage",
    "apply_lens",
    "lens_kwargs",
]

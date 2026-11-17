"""Server-side table protocol: a *lens* over rows that stay in the DB (gr592637).

A tabular view renders every row and lets the transport cut it at ~24 kB —
positionally, so the rows that matter (recent, trusted, best) are the ones
dropped. A lens trims *programmatically* instead: ``where`` filters,
``sort`` orders, ``columns`` selects, ``limit`` pages and ``cursor``
continues. Nothing is summarised by a model, so nothing is lost the caller
cannot get back with another call.

``args={'where': {col: value | [set] | {'min': a, 'max': b}},
'sort': '-col' | ['col', '-col2'] | [['col', 'desc']], 'columns': [...],
'limit': N, 'cursor': token}``.

:func:`apply_lens` is pure over a list of row dicts. It returns the shown
rows plus a footer carrying the matched/total counts, per-column aggregates
of the rows left out (count/min/max/mean for numerics, a value histogram
for enums) and the exact next call. The byte guard drops whole rows only.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from precis.errors import BadInput
from precis.response import Response

#: Rows per page when the caller sets no ``limit`` — sized so a typical
#: ~150 B row table answers in about 4 kB.
DEFAULT_LIMIT = 20

#: Byte ceiling on the full response when the caller asks for more rows;
#: rows are dropped whole, never cut mid-row, and the cursor resumes there.
DEFAULT_MAX_BYTES = 16000

#: A column with more distinct values than this is free text, not an enum:
#: it gets no histogram.
_ENUM_MAX = 8

LENS_ARG_KEYS = frozenset({"where", "sort", "columns", "limit", "cursor"})


@dataclass(frozen=True)
class LensSpec:
    """Parsed lens arguments."""

    where: dict[str, Any] = field(default_factory=dict)
    sort: tuple[tuple[str, str], ...] = ()
    columns: tuple[str, ...] | None = None
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class LensResult:
    """One lens page: the shown rows, rendered body and footer."""

    rows: list[dict[str, Any]]
    body: str
    footer: str
    total: int
    matched: int
    offset: int
    next_cursor: str | None


def num(v: Any) -> float | None:
    """A cell as a float — ``≈``-marked values count, ``—``/text do not."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float, str)):
        try:
            value = float(v.strip().lstrip("≈").strip() if isinstance(v, str) else v)
        except (ValueError, OverflowError):
            return None
        return value if math.isfinite(value) else None
    return None


def literal(obj: Any) -> str:
    """Render an executable argument literal, including quoted cell values."""
    return repr(obj)


def _fingerprint(rows: Sequence[Mapping[str, Any]], spec: LensSpec, scope: str) -> str:
    raw = json.dumps([scope, spec.where, spec.sort, rows], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _encode_cursor(offset: int, fp: str) -> str:
    return f"{offset}.{fp}"


def _decode_cursor(token: str) -> tuple[int, str]:
    try:
        offset, fp = token.split(".", 1)
        return int(offset), fp
    except ValueError as exc:
        raise BadInput(
            "invalid table cursor",
            next="repeat the call without args={'cursor': ...}",
        ) from exc


def parse_lens_args(
    args: Mapping[str, Any] | None,
    *,
    columns: Sequence[str],
    default_limit: int = DEFAULT_LIMIT,
    where_alias: Mapping[str, Any] | None = None,
    parse_sort: bool = True,
) -> LensSpec:
    """Validate ``args`` against the view's ``columns`` into a :class:`LensSpec`.

    ``where_alias`` is merged into ``where`` (the ``trusted: true`` alias);
    a ``where`` that contradicts it is refused. ``parse_sort=False`` leaves
    ``sort`` to the caller (a legacy measure-key sort).
    """
    a = args or {}
    cols = list(columns)
    avail = f"columns: {', '.join(cols)}"

    def _col(name: Any, what: str) -> str:
        if not isinstance(name, str) or name not in cols:
            raise BadInput(
                f"args={{{what!r}: ...}}: unknown column {name!r}; {avail}",
                options=cols,
                next="repeat the call with a column named in options",
            )
        return name

    where_raw = a.get("where")
    if where_raw is None:
        where: dict[str, Any] = {}
    elif isinstance(where_raw, Mapping):
        where = {_col(k, "where"): v for k, v in where_raw.items()}
    else:
        raise BadInput(
            f"args={{'where': {where_raw!r}}}: where must be {{column: value}}",
            next="args={'where': {'trusted': 'yes'}}",
        )
    for key, condition in where.items():
        if isinstance(condition, Mapping) and (
            not condition
            or set(condition) - {"min", "max"}
            or any(num(value) is None for value in condition.values())
            or (
                (lo := num(condition.get("min"))) is not None
                and (hi := num(condition.get("max"))) is not None
                and lo > hi
            )
        ):
            raise BadInput(
                f"invalid numeric range for {key!r}",
                next=f"use args={{'where': {{{key!r}: {{'min': 0, 'max': 1}}}}}}",
            )
    for k, v in (where_alias or {}).items():
        if k in where and where[k] != v:
            raise BadInput(
                f"args where[{k!r}]={where[k]!r} contradicts the {k!r} shortcut",
                next=f"args={{'where': {{{k!r}: {v!r}}}}}",
            )
        where[k] = v

    sort: list[tuple[str, str]] = []
    sort_raw = a.get("sort") if parse_sort else None
    if sort_raw is not None:
        if not isinstance(sort_raw, (str, list, tuple)):
            raise BadInput("invalid table sort", next="use args={'sort': ['-column']}")
        items = [sort_raw] if isinstance(sort_raw, str) else list(sort_raw)
        for it in items:
            if isinstance(it, str):
                desc = it.startswith("-")
                sort.append(
                    (_col(it.lstrip("+-").strip(), "sort"), "desc" if desc else "asc")
                )
            elif isinstance(it, (list, tuple)) and len(it) == 2:
                d = str(it[1]).lower()
                if d not in ("asc", "desc"):
                    raise BadInput(
                        f"args={{'sort': ...}}: direction {it[1]!r} must be asc or desc",
                        next="args={'sort': [['energy', 'desc']]}",
                    )
                sort.append((_col(it[0], "sort"), d))
            else:
                raise BadInput(
                    f"args={{'sort': {it!r}}}: sort entries are '-col' or [col, asc|desc]",
                    next="args={'sort': ['-energy']}",
                )

    cols_raw = a.get("columns")
    sel: tuple[str, ...] | None = None
    if cols_raw is not None:
        if not isinstance(cols_raw, (list, tuple)) or not cols_raw:
            raise BadInput(
                f"args={{'columns': {cols_raw!r}}}: columns must be a list of names",
                next=f"args={{'columns': {literal(cols[:3])}}}",
            )
        sel = tuple(_col(c, "columns") for c in cols_raw)

    limit = default_limit
    if a.get("limit") is not None:
        raw = a["limit"]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise BadInput(
                f"args={{'limit': {raw!r}}}: limit must be an integer >= 1",
                next=f"args={{'limit': {default_limit}}}",
            )
        limit = raw
    cur = a.get("cursor")
    if cur is not None and (not isinstance(cur, str) or not cur or len(cur) > 48):
        raise BadInput(
            f"args={{'cursor': {cur!r}}}: cursor is the token a previous page gave",
            next="repeat the call without args={'cursor': ...}",
        )
    return LensSpec(where, tuple(sort), sel, limit, cur)


def _matches(row: Mapping[str, Any], where: Mapping[str, Any]) -> bool:
    for col, cond in where.items():
        v = row.get(col)
        if isinstance(cond, Mapping):
            n = num(v)
            lo, hi = num(cond.get("min")), num(cond.get("max"))
            if n is None or (lo is not None and n < lo) or (hi is not None and n > hi):
                return False
        elif isinstance(cond, (list, tuple, set, frozenset)):
            if str(v) not in {str(c) for c in cond}:
                return False
        elif isinstance(cond, (int, float)) and not isinstance(cond, bool):
            if num(v) != float(cond):
                return False
        elif str(v) != str(cond):
            return False
    return True


def _numkey(col: str) -> Callable[[dict[str, Any]], float]:
    return lambda r: num(r.get(col)) or 0.0


def _strkey(col: str) -> Callable[[dict[str, Any]], str]:
    return lambda r: str(r.get(col))


def _sorted(
    rows: list[dict[str, Any]], sort: Sequence[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Stable multi-key sort; a missing value sorts last in either direction."""
    out = list(rows)
    for col, direction in reversed(sort):
        present = [r for r in out if r.get(col) not in (None, "", "—", "-", "n/a")]
        absent = [r for r in out if r.get(col) in (None, "", "—", "-", "n/a")]
        rev = direction == "desc"
        if all(num(r.get(col)) is not None for r in present):
            present.sort(key=_numkey(col), reverse=rev)
        else:
            present.sort(key=_strkey(col), reverse=rev)
        out = present + absent
    return out


def _aggregate(rows: Sequence[Mapping[str, Any]], col: str) -> str | None:
    """``col`` over ``rows``: count/min/max/mean (numeric), histogram (enum)."""
    vals = [r.get(col) for r in rows]
    nums = [n for n in (num(v) for v in vals) if n is not None]
    if nums and len(nums) == sum(v not in (None, "", "—", "-", "n/a") for v in vals):
        return (
            f"{col}: n={len(nums)} min={min(nums):g} max={max(nums):g} "
            f"mean={sum(n / len(nums) for n in nums):g}"
        )
    hist = Counter(str(v) if v not in (None, "") else "—" for v in vals)
    # Identifier and free-text columns only need a populated-cell count.
    if (
        col not in {"name", "handle", "cand", "candidate", "pathway", "blocker"}
        and len(hist) <= _ENUM_MAX
        and all(len(key) <= 80 for key in hist)
    ):
        parts = ", ".join(f"{k}={n}" for k, n in hist.most_common())
        return f"{col}: {parts}"
    return f"{col}: n={len(vals) - hist.get('—', 0)}"


def apply_lens(
    rows: Sequence[Mapping[str, Any]],
    spec: LensSpec,
    *,
    columns: Sequence[str],
    render: Callable[[list[dict[str, Any]], list[str]], str],
    call: Callable[[dict[str, Any]], str],
    default_columns: Sequence[str] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    row_unit: str = "rows",
) -> LensResult:
    """Run ``spec`` over ``rows``; render one page and its footer.

    ``columns`` is every column the rows carry (the aggregate domain);
    ``default_columns`` the ones shown when ``spec.columns`` is unset.
    ``render(rows, shown_columns)`` produces the table; ``call(args)``
    formats the literal call for a given ``args`` dict, so the footer names
    the exact next move. A cursor minted for a different ``where``/``sort``
    is refused (:class:`BadInput`).
    """
    total = len(rows)
    shown_cols = list(spec.columns or default_columns or columns)
    matched_rows: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in rows:
        (matched_rows if _matches(row, spec.where) else excluded).append(dict(row))
    if spec.sort:
        matched_rows = _sorted(matched_rows, spec.sort)
    matched = len(matched_rows)
    base = _base_args(spec)
    fp = _fingerprint(matched_rows, spec, call({}))
    offset = 0
    if spec.cursor is not None:
        offset, cfp = _decode_cursor(spec.cursor)
        if cfp != fp or not 0 < offset < matched:
            raise BadInput(
                "table cursor is stale or belongs to a different view, filter or sort",
                next=call(base),
            )

    size = min(spec.limit, matched - offset)
    while True:
        end = offset + size
        page = matched_rows[offset:end]
        body = render(page, shown_cols)
        next_cursor = _encode_cursor(end, fp) if end < matched else None
        span = f"{offset + 1}–{end}" if page else "0"
        counts = f"{span} of {matched} {row_unit}"
        if matched != total:
            counts += f" matching where ({total} total)"
        lines = [f"({counts})"]
        left_out = excluded + matched_rows[:offset] + matched_rows[end:]
        if left_out:
            aggs = [a for c in shown_cols if (a := _aggregate(left_out, c))]
            lines.append(f"left out ({len(left_out)} {row_unit}) — " + "; ".join(aggs))
        if next_cursor:
            lines.append(f"next: {call({**base, 'cursor': next_cursor})}")
        if next_cursor or spec.where or spec.cursor:
            wider = {k: v for k, v in base.items() if k != "where"}
            if next_cursor and not spec.where:
                wider["limit"] = min(total, max(spec.limit * 4, spec.limit + 1))
            lines.append(f"widen: {call(wider)}")
        footer = "\n".join(lines)
        if len(f"{body}\n{footer}".encode()) <= max_bytes:
            return LensResult(page, body, footer, total, matched, offset, next_cursor)
        if size <= 1:
            raise BadInput(
                "table response cannot fit the byte budget with a complete row and footer",
                next=call(
                    {
                        **base,
                        "columns": shown_cols[:1],
                        "limit": 1,
                        **({"cursor": spec.cursor} if spec.cursor else {}),
                    }
                ),
            )
        size = max(1, size // 2)


def lens_response(
    rows: Sequence[Mapping[str, Any]],
    spec: LensSpec,
    *,
    columns: Sequence[str],
    render: Callable[[list[dict[str, Any]], list[str]], str],
    call: Callable[[dict[str, Any]], str],
    header: str = "",
    row_unit: str = "rows",
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Response:
    """Fit the full page again if runtime reserves space for hints."""

    def fit(cap: int) -> str:
        result = apply_lens(
            rows,
            spec,
            columns=columns,
            render=render,
            call=call,
            row_unit=row_unit,
            max_bytes=cap - len(header.encode()),
        )
        return f"{header}{result.body}\n{result.footer}"

    return Response(body=fit(max_bytes), budget_fit=fit)


def _base_args(spec: LensSpec) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if spec.where:
        out["where"] = spec.where
    if spec.sort:
        out["sort"] = [[c, d] for c, d in spec.sort]
    if spec.columns:
        out["columns"] = list(spec.columns)
    out["limit"] = spec.limit
    return out


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_BYTES",
    "LENS_ARG_KEYS",
    "LensResult",
    "LensSpec",
    "apply_lens",
    "literal",
    "num",
    "parse_lens_args",
]

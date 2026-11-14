"""Server-side table lens — the shared ``where/sort/columns/limit/cursor``
protocol for every tabular MCP view (gr592637).

Reto's question (2026-11-08): can leaderboard-style prints that carry many
kB of rows be programmatically sorted/trimmed/evaluated before injection
into context **without losing fidelity**? The answer this module encodes:
**yes for programmatic lenses, no for LLM summarisation**. A view is a lens
on rows that stay in the DB — trimming by filter/sort/column-selection/
top-N loses nothing the caller cannot get back with another call; a
summary in the path would. Until this module, the quest frontier/leader-
board/results and pathway compare views rendered EVERY row then cut at
the ~24 kB transport budget positionally — dropping exactly the rows that
mattered (recent, trusted, best).

The protocol every tabular view shares:

- ``where`` — ``{col: value | [values] | {min|max|gt|gte|lt|lte|ne}}``;
  scalar equality, set membership, or a numeric range. Cells that carry
  display marks (``≈``, ``—``) are normalised before comparing so the
  rendered value and the filtered value can't drift.
- ``sort`` — ``'col'``/``'-col'``, a list of those, or ``[[col,'asc'|
  'desc'], …]`` (multi-key, stable). Missing values sink to the tail in
  BOTH directions — an unmeasured row is never "best" or "worst".
- ``columns`` — the projection to render; unknown names are refused with
  the carried columns listed.
- ``limit`` — rows per page (default :data:`DEFAULT_LIMIT`, sized so the
  common answer lands near ~4 kB; capped at :data:`MAX_LIMIT`).
- ``cursor`` — opaque continuation token minted by the previous page. It
  binds the full spec (where/sort/columns/limit): a cursor presented
  alongside a changed spec raises :class:`BadInput`, because silently
  resuming a different question is worse than refusing.

The response contract: every page carries ``shown/matched/total`` counts,
per-column aggregates over the rows the page did NOT show (``count, min/
max/mean`` for numeric columns, a value histogram for enums — e.g. the
``trusted yes/no`` tally), and a ``next:`` line naming the EXACT call that
continues (same view, same args, plus the minted cursor) — never an
unrelated search hint. Rows the caller asked for are never truncated
mid-row: the optional ``byte_cap`` guard drops WHOLE trailing rows until
the render fits.

Callers shape ``args`` for the continuation themselves (they know their
own spelling) and hand it to :func:`render_lens_page` with a ``call``
closure — the lens owns the mechanics, the view owns the sentence.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from precis.errors import BadInput

#: The five args= keys the lens protocol owns (gr592637).
LENS_KEYS: frozenset[str] = frozenset({"where", "sort", "columns", "limit", "cursor"})

#: Default page size — the common answer lands near ~4 kB at typical
#: ~150-200 B/row. ``args={'limit': N}`` re-sizes; the footer's ``next:``
#: always names the continuing call so nothing is unreachable.
DEFAULT_LIMIT = 20

#: Hard ceiling — a ``limit`` past this is refused, not clamped, so a
#: caller asking for "everything" learns the paging door instead of
#: silently getting a different number back.
MAX_LIMIT = 200

#: Top values kept per enum-column histogram in the hidden-row aggregates.
_HIST_TOP = 5

#: Cursor token prefix — version namespace so a future payload shape
#: change is detectable instead of misdecoded.
_CURSOR_V = "L1."

#: Cells that render a placeholder for "no value" — treated as missing
#: for sort, where and aggregates alike.
_MISSING_CELLS = frozenset({"", "-", "—", "–", "n/a", "N/A"})

#: Operators accepted inside a ``where`` range dict.
_RANGE_OPS = frozenset({"min", "gte", "max", "lte", "gt", "lt", "ne"})


class _Rev(str):
    """A str whose ordering is reversed — lets one ascending tuple sort
    express descending string order while keeping the missing-tail slot
    fixed in both directions."""

    def __lt__(self, other: Any) -> bool:
        return str.__gt__(self, other)


def _num(v: Any) -> float | None:
    """Best-effort numeric read of a cell — strips the display marks the
    views stamp (``≈`` untrusted, ``$``/``,`` money) so the filtered/sorted
    value equals the rendered one. ``None`` (or ``bool``/non-finite)
    → ``None``."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(float(v)) else None
    s = str(v).strip()
    if s in _MISSING_CELLS:
        return None
    s = s.lstrip("≈").strip().replace(",", "").lstrip("$")
    try:
        f = float(s)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def _missing(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() in _MISSING_CELLS)


def _eq(cell: Any, cond: Any) -> bool:
    """Equality that stays numeric-aware: ``where={'n_dopant': 2}`` matches
    an int cell AND a ``'2'`` string cell; enum filters stay string-exact."""
    cn, vn = _num(cell), _num(cond)
    if cn is not None and vn is not None:
        return cn == vn
    if _missing(cell):
        return False
    return str(cell).strip() == str(cond).strip()


def _match(cell: Any, cond: Any) -> bool:
    """One ``where`` condition against one cell: scalar → equality,
    list/tuple/set → membership, dict → numeric range/operators."""
    if isinstance(cond, Mapping):
        num = _num(cell)
        if num is None:
            return False
        for op, bound in cond.items():
            b = _num(bound)
            if b is None:
                raise BadInput(
                    f"where range bound {bound!r} is not a number",
                    next="where={'energy': {'max': -1.0}}",
                )
            if op in ("min", "gte") and num < b:
                return False
            if op in ("max", "lte") and num > b:
                return False
            if op == "gt" and num <= b:
                return False
            if op == "lt" and num >= b:
                return False
            if op == "ne" and num == b:
                return False
        return True
    if isinstance(cond, (list, tuple, set, frozenset)) and not isinstance(cond, str):
        return any(_eq(cell, v) for v in cond)
    return _eq(cell, cond)


def match_where(row: Mapping[str, Any], where: Mapping[str, Any]) -> bool:
    """``True`` when ``row`` satisfies every ``where`` clause (AND)."""
    return all(_match(row.get(col), cond) for col, cond in where.items())


def _sort_key(v: Any, asc: bool) -> tuple[int, float, Any]:
    """``(rank, value, text)`` — missing always ranks last in both
    directions; numerics compare as floats, text lexicographically
    (reversed under ``_Rev`` when ``desc``)."""
    if _missing(v):
        return (2, 0.0, "")
    n = _num(v)
    if n is not None:
        return (0, n if asc else -n, "")
    s = str(v).strip()
    return (1, 0.0, s if asc else _Rev(s))


@dataclass(frozen=True)
class LensSpec:
    """The parsed lens — where/sort/columns/limit + the decoded page offset.

    ``sort`` is normalised to ``((column, ascending), …)`` regardless of
    which spelling the caller used. ``columns=None`` means "the view's
    full schema" (projection happens at render).
    """

    where: dict[str, Any]
    sort: tuple[tuple[str, bool], ...]
    columns: tuple[str, ...] | None
    limit: int
    offset: int = 0


@dataclass
class LensPage:
    """One page of a lensed table.

    ``rows`` — the page slice (post where/sort/cursor, post byte-guard
    trims). ``matched`` — rows surviving ``where``. ``total`` — every row
    the view built before the lens. ``hidden`` — matched rows NOT on this
    page (the aggregate surface). ``columns`` — the effective rendered
    column order. ``next_args``/cursor minting lives in
    :func:`render_lens_page`, which needs the caller's call-spelling.
    """

    rows: list[dict[str, Any]]
    matched: int
    total: int
    hidden: list[dict[str, Any]]
    columns: list[str]
    spec: LensSpec


def _canonical_spec(spec_bits: dict[str, Any]) -> str:
    """Stable JSON fingerprint of the lens spec — the cursor binds it, so
    every dict/key order the caller chooses collapses to one hash."""
    return json.dumps(spec_bits, sort_keys=True, default=str, separators=(",", ":"))


def _spec_hash(spec: LensSpec) -> str:
    bits = {
        "where": spec.where,
        "sort": [[c, "asc" if a else "desc"] for c, a in spec.sort],
        "columns": list(spec.columns) if spec.columns is not None else None,
        "limit": spec.limit,
    }
    return hashlib.sha256(_canonical_spec(bits).encode("utf-8")).hexdigest()[:16]


def mint_cursor(spec: LensSpec, offset: int) -> str:
    """The opaque continuation token — binds spec-hash + next offset."""
    payload = json.dumps({"h": _spec_hash(spec), "o": offset}, separators=(",", ":"))
    return _CURSOR_V + base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _read_cursor(token: Any, spec: LensSpec) -> int:
    """Decode ``cursor`` → offset; refuse stale/forged tokens by name."""
    if not isinstance(token, str) or not token.startswith(_CURSOR_V):
        raise BadInput(
            f"args={{'cursor': {token!r}}}: not a lens cursor "
            f"(expected the '{_CURSOR_V}…' token a previous page minted)",
            next="retry without 'cursor' for page one",
        )
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(token[len(_CURSOR_V) :]).decode("utf-8")
        )
        want_hash, offset = str(payload["h"]), int(payload["o"])
    except Exception:
        raise BadInput(
            f"args={{'cursor': {token!r}}}: cursor payload undecodable",
            next="retry without 'cursor' for page one",
        ) from None
    if want_hash != _spec_hash(spec):
        raise BadInput(
            "cursor belongs to a different lens spec — where/sort/columns/"
            "limit changed since this cursor was minted; silently resuming "
            "a different question is refused",
            next="retry without 'cursor' (fresh page one of the new spec), "
            "or restore the args the cursor was minted with",
        )
    if offset < 0:
        raise BadInput(
            f"args={{'cursor': {token!r}}}: negative offset",
            next="retry without 'cursor' for page one",
        )
    return offset


def _parse_where(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise BadInput(
            f"args={{'where': {raw!r}}}: where must be a dict "
            "{column: value|[values]|{min,max}}",
            next="args={'where': {'trusted': 'yes'}}",
        )
    out: dict[str, Any] = {}
    for col, cond in raw.items():
        if not isinstance(col, str) or not col.strip():
            raise BadInput("args={'where': …}: column names must be non-empty strings")
        if isinstance(cond, Mapping):
            unknown = sorted(set(cond) - _RANGE_OPS)
            if unknown:
                raise BadInput(
                    f"where[{col!r}]: unknown range ops {unknown!r}",
                    options=sorted(_RANGE_OPS),
                    next="where={'energy': {'min': -3.0, 'max': -1.0}}",
                )
            if not cond:
                raise BadInput(f"where[{col!r}]: empty range dict")
        out[col.strip()] = dict(cond) if isinstance(cond, Mapping) else cond
    return out


def parse_sort(raw: Any) -> tuple[tuple[str, bool], ...]:
    """``sort`` spellings → ``((col, asc), …)``: ``'col'``/``'-col'``,
    a list of the same, or ``[[col,'asc'|'desc'], …]``.

    Public — views that resolve a domain sort key (the quest measure-key
    → declared-sense path) call this only for the generic spellings they
    do not intercept."""
    if raw is None:
        return ()
    items = (
        [raw]
        if isinstance(raw, str)
        else list(raw)
        if isinstance(raw, (list, tuple))
        else None
    )
    if items is None or not items:
        raise BadInput(
            f"args={{'sort': {raw!r}}}: sort is 'col'/'-col' or a list "
            "of those / [[col,'asc'|'desc'], …]",
            next="args={'sort': '-energy'}",
        )
    out: list[tuple[str, bool]] = []
    for it in items:
        if isinstance(it, str):
            c = it.strip()
            if not c:
                raise BadInput("args={'sort': …}: empty sort column")
            out.append((c[1:], False) if c.startswith("-") else (c, True))
        elif isinstance(it, (list, tuple)) and len(it) == 2:
            c, d = str(it[0]).strip(), str(it[1]).strip().lower()
            if not c or d not in ("asc", "desc"):
                raise BadInput(
                    f"args={{'sort': {it!r}}}: pairs are [col, 'asc'|'desc']",
                    next="args={'sort': [['energy', 'desc']]}",
                )
            out.append((c, d == "asc"))
        else:
            raise BadInput(
                f"args={{'sort': {it!r}}}: entry is 'col'/'-col' or [col, dir]",
                next="args={'sort': '-energy'}",
            )
    return tuple(out)


def _parse_columns(raw: Any) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or not raw:
        raise BadInput(
            f"args={{'columns': {raw!r}}}: columns is a non-empty list of column names",
            next="args={'columns': ['handle', 'energy', 'trusted']}",
        )
    cols = [str(c).strip() for c in raw]
    if any(not c for c in cols):
        raise BadInput("args={'columns': …}: column names must be non-empty")
    return tuple(cols)


def _parse_limit(raw: Any, default: int, maximum: int) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise BadInput(
            f"args={{'limit': {raw!r}}}: limit must be a positive int",
            next=f"args={{'limit': {default}}}",
        )
    if raw > maximum:
        raise BadInput(
            f"args={{'limit': {raw}}}: limit exceeds the {maximum}-row cap — "
            "page with the cursor instead of widening past it",
            next=f"args={{'limit': {maximum}}}",
        )
    return raw


def parse_lens_args(
    args: Mapping[str, Any] | None,
    *,
    default_limit: int = DEFAULT_LIMIT,
    max_limit: int = MAX_LIMIT,
    where_extra: Mapping[str, Any] | None = None,
    sort_resolved: Sequence[tuple[str, bool]] | None = None,
) -> LensSpec:
    """``args`` → :class:`LensSpec`, with caller-injected extras.

    ``where_extra`` merges into the caller's ``where`` (the quest views'
    ``args={'trusted': true}`` alias lands here as ``{'trusted': 'yes'}`` —
    the alias and the literal where are the SAME spec, so a cursor minted
    under either resumes under the other). ``sort_resolved`` overrides the
    raw ``args['sort']`` when a view resolves the key itself (the quest
    measure-key → declared-sense path); pass the raw ``args['sort']``
    through for views without a resolution layer.
    """
    args = args or {}
    where = _parse_where(args.get("where"))
    if where_extra:
        merged = dict(where)
        for col, cond in where_extra.items():
            if col in merged and merged[col] != cond:
                raise BadInput(
                    f"where[{col!r}] conflicts with its alias arg "
                    f"({cond!r} vs {merged[col]!r}) — drop one",
                    next="state the filter once: either the alias arg or "
                    "inside where={}",
                )
            merged[col] = cond
        where = merged
    sort = (
        tuple(sort_resolved)
        if sort_resolved is not None
        else parse_sort(args.get("sort"))
    )
    spec = LensSpec(
        where=where,
        sort=sort,
        columns=_parse_columns(args.get("columns")),
        limit=_parse_limit(args.get("limit"), default_limit, max_limit),
    )
    if "cursor" in args:
        spec = LensSpec(
            where=spec.where,
            sort=spec.sort,
            columns=spec.columns,
            limit=spec.limit,
            offset=_read_cursor(args["cursor"], spec),
        )
    return spec


def _project(row: Mapping[str, Any], columns: Sequence[str]) -> dict[str, Any]:
    return {c: row.get(c) for c in columns}


def _row_columns(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    seen: dict[str, None] = {}
    for r in rows:
        for k in r:
            if k not in seen:
                seen[k] = None
    return list(seen)


def apply_lens(
    rows: Sequence[Mapping[str, Any]],
    spec: LensSpec,
    *,
    schema: Sequence[str] | None = None,
) -> LensPage:
    """``rows`` → :class:`LensPage`: where → sort → offset/limit slice.

    ``schema`` is the view's declared column order; the lens validates
    ``columns``/``where``/``sort`` names against it (plus whatever keys the
    rows themselves carry) so a typo is refused by name, not silently
    empty.
    """
    carried = sorted(set(schema or ()) | set(_row_columns(rows)))
    unknown_cols = sorted(
        {
            c
            for c in (
                *(spec.columns or ()),
                *spec.where,
                *(c for c, _ in spec.sort),
            )
            if c not in carried
        }
    )
    if unknown_cols:
        raise BadInput(
            f"lens names columns the rows do not carry: {unknown_cols!r}",
            options=carried,
            next="args={'columns': [<only listed names>]}",
        )
    filtered = [dict(r) for r in rows if match_where(r, spec.where)]
    for col, asc in reversed(spec.sort):

        def _key(r: Mapping[str, Any], c: str = col, a: bool = asc) -> Any:
            return _sort_key(r.get(c), a)

        filtered.sort(key=_key)
    matched, offset = len(filtered), min(spec.offset, len(filtered))
    page = filtered[offset : offset + spec.limit]
    columns = (
        list(spec.columns)
        if spec.columns is not None
        else list(schema or _row_columns(rows))
    )
    return LensPage(
        rows=[_project(r, columns) for r in page],
        matched=matched,
        total=len(rows),
        hidden=[_project(r, columns) for r in filtered[offset + len(page) :]],
        columns=columns,
        spec=spec,
    )


def hidden_aggregates(hidden: Sequence[Mapping[str, Any]]) -> str:
    """Per-column aggregates over the rows the page did not show.

    Numeric column → ``col n=K min=… max=… mean=…``; enum-ish column →
    ``col v1×n · v2×n`` (top :data:`_HIST_TOP`, ``+k more`` tail). Pure
    string columns with unbounded cardinality still histogram — the tail
    count keeps them honest."""
    if not hidden:
        return ""
    bits: list[str] = []
    cols = _row_columns(hidden)
    for col in cols:
        vals = [r.get(col) for r in hidden if not _missing(r.get(col))]
        if not vals:
            continue
        nums = [_num(v) for v in vals]
        if all(n is not None for n in nums):
            ns = [n for n in nums if n is not None]
            bits.append(
                f"{col} n={len(ns)} min={min(ns):g} max={max(ns):g} "
                f"mean={sum(ns) / len(ns):g}"
            )
        else:
            hist = Counter(str(v).strip() for v in vals)
            top = hist.most_common(_HIST_TOP)
            s = " · ".join(f"{v}×{n}" for v, n in top)
            rest = len(hist) - len(top)
            bits.append(f"{col} {s}" + (f" · +{rest} more" if rest else ""))
    return "; ".join(bits)


def render_lens_page(
    page: LensPage,
    *,
    args: Mapping[str, Any],
    call: Callable[[Mapping[str, Any]], str],
    render: Callable[[list[dict[str, Any]]], str] | None = None,
    byte_cap: int = 0,
) -> str:
    """Table body + lens footer.

    ``render`` produces the visible table from the page rows (default:
    :func:`precis.format.render_agent_table` over ``page.columns``).
    ``byte_cap`` (chars) drops WHOLE trailing rows until the render fits —
    a row is never cut mid-cell. ``args`` is the caller's normalised
    continuation args (its own preserved knobs + the lens keys); the minted
    ``cursor`` is merged in for the ``next:`` line, which is emitted only
    when rows remain after this page.
    """
    from precis.format import render_agent_table

    shown = list(page.rows)
    if render is None:
        cols = page.columns

        def render(rs: list[dict[str, Any]]) -> str:
            return render_agent_table(rs, schema=cols)

    text = render(shown)
    while byte_cap > 0 and len(text) > byte_cap and shown:
        shown.pop()  # whole rows only — never a mid-row cut
        text = render(shown)
    n_shown = len(shown)
    # Rows the byte guard dropped join the hidden set — aggregates and the
    # continue-offset must cover them, else the guard loses rows silently.
    hidden = [*page.rows[n_shown:], *page.hidden]
    exhausted = page.spec.offset + n_shown >= page.matched

    lines = [text] if text else ["(no rows on this page)"]
    lines.append(f"{n_shown} shown · {page.matched} matched · {page.total} total")
    if hidden:
        agg = hidden_aggregates(hidden)
        if agg:
            lines.append(f"hidden: {agg}")
    if exhausted and page.matched > 0:
        lines.append("(cursor exhausted — every matched row shown)")
    elif hidden and n_shown == 0:
        lines.append(
            "(every matched row exceeds the byte budget — widen it or "
            "narrow where; a cursor at this offset would self-loop)"
        )
    elif hidden:
        next_args = dict(args)
        next_args["cursor"] = mint_cursor(page.spec, page.spec.offset + n_shown)
        lines.append(f"next: {call(next_args)}")
    return "\n".join(lines)


def lens_echo_args(
    args: Mapping[str, Any] | None,
    spec: LensSpec,
    *,
    preserve: Sequence[str] = (),
) -> dict[str, Any]:
    """The normalised continuation args for a view's ``next:`` line.

    Lens keys are emitted in canonical form (``where`` as the merged dict,
    ``sort`` as ``'col'``/``'-col'`` strings, ``limit`` as int); keys in
    ``preserve`` (view-local knobs like ``format``/``U``/``gate``) pass
    through unchanged. ``cursor`` is added by :func:`render_lens_page`.
    """
    out: dict[str, Any] = {}
    for k in preserve:
        if args and k in args:
            out[k] = args[k]
    if spec.where:
        out["where"] = spec.where
    if spec.sort:
        out["sort"] = [c if a else f"-{c}" for c, a in spec.sort]
        if len(out["sort"]) == 1:
            out["sort"] = out["sort"][0]
    if spec.columns is not None:
        out["columns"] = list(spec.columns)
    out["limit"] = spec.limit
    return out


__all__ = [
    "DEFAULT_LIMIT",
    "LENS_KEYS",
    "MAX_LIMIT",
    "LensPage",
    "LensSpec",
    "apply_lens",
    "hidden_aggregates",
    "lens_echo_args",
    "match_where",
    "mint_cursor",
    "parse_lens_args",
    "parse_sort",
    "render_lens_page",
]

"""TOON read/patch surface for id-addressed draft tables
(spreadsheet-tables slice 1b — spec §9 "TOON read, patch and replace").

The TOON view is the agent-facing grid: a braced header line then one
tab-separated line per row. Row ids ride a leading ``#`` column; a header
cell is ``<id>:<name>`` plus the optional suffixes `` [unit]``,
`` <from>`` and `` =formula`` (in that order):

    {#	k5f9:vendor	m3qa:part	x7d2:qty	t6ne:line_cost =unit_price*qty}
    r2k7	jlcpcb	C19077769	100	1.23

Inside a header cell the characters ``% [ ] < > = : { } #`` and
tab/newline/carriage-return are percent-escaped (``%XX``) in the name /
unit / from / formula parts, so any stored name — including one carrying
`` [mm]``-shaped text — round-trips through the grammar losslessly. The
``id:`` prefix itself is emitted verbatim. Data cells use the stock TOON
quoting (:mod:`precis.format.toon`): wrapped in ``"…"`` only on
tab/newline/CR.

The same grammar is the write channel:

- ``patch=`` — a subset of columns (by id or bare name) and rows (by row
  id, or by the declared ``key`` column). Empty cell = unchanged,
  ``null`` = clear; ``mode='update'`` (default) refuses the whole patch
  on any unmatched row, ``mode='upsert'`` appends unmatched rows (needs
  a ``key`` column — the ``#`` cell supplies its value). Columns holding
  ``formula``/``from`` (calculated/pulled) are refused.
- ``toon=`` — full replace; calculated/pulled columns may be omitted
  (they persist). ``#`` cells carry the row ids to keep; an empty ``#``
  mints.

Every read and every write reply carries ``hash: <h>`` — a short content
hash over ``{columns, rowmeta, rows}`` (not ``recent``/``retired_ids``/
``header``, which are bookkeeping or derived). ``base=<hash>`` is the
optimistic-concurrency token: refuse when the table changed since the
caller read — *unless the touched cells don't overlap*. Overlap is
tracked by ``meta.table.recent``, a bounded journal (newest ~32 entries)
of per-write diffs — each entry ``{p, h, cells, rows, cols}`` records the
pre-hash, the post-hash, the changed cell addresses (``rid.cid``) and
the added/dropped/spec-changed row and column ids (``+id``/``-id``/
``~id``). ``p`` lets a ``base`` that predates the journal still resolve:
the first entry's ``p`` is the pre-journal hash.

This module is pure: every op maps a normalised ``meta.table`` dict to a
new one (validated through :func:`normalize_table`); persistence lives in
``DraftHandler._edit_table``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections import Counter
from typing import Any

from precis.errors import BadInput
from precis.format import toon as _toon
from precis.utils.fractional import even_keys, key_between
from precis.utils.table_data import (
    TABLE_CTRL_RE,
    cell_text,
    fresh_table_id,
    infer_scalar,
    is_table_id,
    normalize_table,
)

#: hex chars of the table content hash shown on reads/writes.
_HASH_LEN = 12
#: ``meta.table.recent`` — bounded concurrency journal (see module doc).
_RECENT_CAP = 32
#: per-entry bound on the journal's ``cells`` list — past it, the entry
#: degrades to a ``'*all'`` coarse marker plus the touched column ids.
_JOURNAL_CELL_CAP = 200
#: reply TOON bodies cap at this many rows — past it, the reply filters
#: to the touched part (spec §9: "filtered to the touched part for large
#: tables").
REPLY_ROW_CAP = 200

_PULL_SCHEMES = ("ref:", "table:", "vendor:")


def table_content_hash(table: dict[str, Any]) -> str:
    """Short deterministic hash over a table's content — ``columns``,
    ``rowmeta`` and ``rows`` as stored. ``header`` (derived), the
    ``retired_ids`` pool and the ``recent`` journal are excluded, so a
    journal append never self-invalidates."""
    canon = {
        "c": table.get("columns") or [],
        "rm": table.get("rowmeta") or [],
        "r": table.get("rows") or [],
    }
    raw = json.dumps(canon, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_HASH_LEN]


# ---------------------------------------------------------------------------
# Header grammar — <id>:<name> [unit] <from> =formula
# ---------------------------------------------------------------------------

_ESCAPES = {
    "%": "%25",
    "[": "%5B",
    "]": "%5D",
    "<": "%3C",
    ">": "%3E",
    "=": "%3D",
    ":": "%3A",
    "{": "%7B",
    "}": "%7D",
    "#": "%23",
    "\t": "%09",
    "\n": "%0A",
    "\r": "%0D",
}
_ESC_RE = re.compile("|".join(re.escape(k) for k in _ESCAPES))
_UNESC_RE = re.compile(r"%([0-9A-Fa-f]{2})")
#: control characters that can never land in a Postgres text field —
#: tab/newline/CR are legit cell content (quoted round-trip), the rest
#: of C0 plus DEL refuse (a ``%00``-decoded NUL crashes the write as a
#: raw ``DataError`` otherwise). Shared with the JSON write paths' gate
#: in ``normalize_table`` (:data:`precis.utils.table_data.TABLE_CTRL_RE`).
_CTRL_RE = TABLE_CTRL_RE


def _esc(s: Any) -> str:
    return _ESC_RE.sub(lambda m: _ESCAPES[m.group(0)], str(s))


def _unesc(s: str) -> str:
    out = _UNESC_RE.sub(lambda m: chr(int(m.group(1), 16)), s)
    if _CTRL_RE.search(out):
        raise BadInput(
            "a header cell decodes to a control character — only tab, "
            "newline and CR survive round-trip",
            next="strip the control character (or use %09/%0A/%0D)",
        )
    return out


_ID_PREFIX_RE = re.compile(r"^([a-hjkmnq-tv-z][0-9a-hjkmnp-tv-z]{3,7}):(.*)$", re.S)
_FROM_TAIL_RE = re.compile(r" <([^<>]*)>$")
_UNIT_TAIL_RE = re.compile(r" \[([^\[\]]*)\]$")


def header_cell(col: dict[str, Any]) -> str:
    """Render one column spec as a TOON header cell:
    ``<id>:<name> [unit] <from> =formula`` (suffixes only when set)."""
    name = _esc(col.get("name", ""))
    out = f"{col['id']}:{name}" if col.get("id") else name
    if col.get("unit"):
        out += f" [{_esc(col['unit'])}]"
    if col.get("from"):
        out += f" <{_esc(col['from'])}>"
    if col.get("formula"):
        out += f" ={_esc(col['formula'])}"
    return out


def parse_header_cell(cell: str) -> dict[str, Any]:
    """Parse one TOON header cell to ``{id, name, unit, from, formula}``
    (absent parts are ``None``; a bare name is allowed in patches).
    Suffixes come off the right in fixed order — emit escapes the marker
    characters inside names/units, so any literal `` [`` / `` <`` /
    `` =`` at a cell's tail is structural."""
    s = cell
    col_id = None
    m = _ID_PREFIX_RE.match(s)
    if m:
        col_id, s = m.group(1), m.group(2)
    formula = None
    i = s.rfind(" =")
    if i != -1:
        formula = _unesc(s[i + 2 :]) or None
        s = s[:i]
    from_ = None
    m = _FROM_TAIL_RE.search(s)
    if m:
        cand = _unesc(m.group(1))
        if cand.startswith(_PULL_SCHEMES):
            from_ = cand
            s = s[: m.start()]
    unit = None
    m = _UNIT_TAIL_RE.search(s)
    if m:
        unit = _unesc(m.group(1))
        s = s[: m.start()]
    return {
        "id": col_id,
        "name": _unesc(s),
        "unit": unit,
        "from": from_,
        "formula": formula,
    }


# ---------------------------------------------------------------------------
# Doc parse / emit
# ---------------------------------------------------------------------------


def _toon_records(text: str) -> tuple[list[str], list[list[str]]]:
    """Split a table-TOON document into ``(header_cells, data_rows)``,
    header stripped of its ``{ }`` markers and ``#`` first cell.
    ``data_rows[i][0]`` is the row's ``#`` key."""
    if not isinstance(text, str) or not text.strip():
        raise BadInput(
            "empty TOON — the doc is a {#\tcol…} header line plus one "
            "tab-separated line per row",
            next="get(kind='draft', id='dc<id>', view='toon') shows the shape",
        )
    records = _toon._tokenise(text, sep="\t")
    if not records:
        raise BadInput(
            "no TOON rows found",
            next="get(kind='draft', id='dc<id>', view='toon') shows the shape",
        )
    header = list(records[0])
    if not header or not header[0].startswith("{") or not header[-1].endswith("}"):
        raise BadInput(
            "the first TOON line must be the {…} header",
            next="get(kind='draft', id='dc<id>', view='toon') shows the shape",
        )
    header[0] = header[0][1:]
    header[-1] = header[-1][:-1]
    if not header or header[0] != "#":
        raise BadInput(
            "a table TOON doc's first column must be '#' — it carries the "
            "row ids (patch keys)",
            next="get(kind='draft', id='dc<id>', view='toon') shows the shape",
        )
    for rec in records:
        for cell in rec:
            if _CTRL_RE.search(cell):
                raise BadInput(
                    "a TOON cell contains a control character — only tab, "
                    "newline and CR are allowed",
                    next="strip the control character from the cell",
                )
    return header[1:], records[1:]


def _resolve_col_list(columns: list[dict[str, Any]], refs: Any) -> list[int]:
    """``cols=[…]`` filter → column indices, in the order given. Entries
    are a column id, a column name, or an ``id..id`` range in current
    order (read-time selection only — same rule as the ``dc<id>~a..b``
    selector)."""
    if refs is None:
        return list(range(len(columns)))
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, list):
        raise BadInput(
            f"cols= must be a list of column ids/names, got {refs!r}",
            next="cols=['qty', 'k5f9'] or cols=['k5f9..m3qa']",
        )
    out: list[int] = []

    def one(ref: str) -> int:
        i = next((i for i, c in enumerate(columns) if c.get("id") == ref), None)
        if i is None:
            i = next((i for i, c in enumerate(columns) if c.get("name") == ref), None)
        if i is None:
            raise BadInput(
                f"no column id/name {ref!r} in this table",
                options=[str(c.get("id") or c.get("name")) for c in columns[:20]],
                next="get(kind='draft', id='dc<id>', view='toon') lists them",
            )
        return i

    for ref in refs:
        if not isinstance(ref, str):
            raise BadInput(
                f"cols= entries must be column ids or names, got {ref!r}",
                next="cols=['qty', 'k5f9']",
            )
        if ".." in ref:
            a, _, b = ref.partition("..")
            ia, ib = one(a), one(b)
            if ia > ib:
                ia, ib = ib, ia
            out.extend(i for i in range(ia, ib + 1) if i not in out)
            continue
        i = one(ref)
        if i not in out:
            out.append(i)
    return out


def _resolve_row_list(rowmeta: list[dict[str, Any]], refs: Any) -> list[int]:
    """``rows=[…]`` filter → row indices (ids only + ``a..b`` ranges)."""
    if refs is None:
        return list(range(len(rowmeta)))
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, list):
        raise BadInput(
            f"rows= must be a list of row ids, got {refs!r}",
            next="rows=['r2k7', 'h8n3'] or rows=['r2k7..h8n3']",
        )
    out: list[int] = []

    def one(ref: str) -> int:
        i = next((i for i, r in enumerate(rowmeta) if r.get("id") == ref), None)
        if i is None:
            raise BadInput(
                f"no row id {ref!r} in this table",
                options=[str(r.get("id")) for r in rowmeta[:20] if r.get("id")],
                next="get(kind='draft', id='dc<id>', view='toon') lists them",
            )
        return i

    for ref in refs:
        if not isinstance(ref, str):
            raise BadInput(
                f"rows= entries must be row ids, got {ref!r}",
                next="rows=['r2k7', 'h8n3']",
            )
        if ".." in ref:
            a, _, b = ref.partition("..")
            ia, ib = one(a), one(b)
            if ia > ib:
                ia, ib = ib, ia
            out.extend(i for i in range(ia, ib + 1) if i not in out)
            continue
        i = one(ref)
        if i not in out:
            out.append(i)
    return out


_WHERE_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.UnaryOp,
    ast.Compare,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.And,
    ast.Or,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
)
_WHERE_NEXT = (
    "where='stock < 1000' — column names, literals, comparisons "
    "(== != < <= > >= in not in), and/or/not"
)
#: ``where=`` complexity caps — deep nesting overflows the parser stack
#: (MemoryError) and a 100k-term conjunction scans seconds per row even
#: on a whitelist AST.
_WHERE_MAX_CHARS = 1000
_WHERE_MAX_NODES = 400


def compile_where(expr: str, columns: list[dict[str, Any]]):
    """Compile a ``where=`` row filter to a predicate over a row's cell
    list. AST-gated: only names, literals, comparisons, boolean ops and
    membership — no calls, attributes or subscripts. Names resolve to
    column names first, then ids (only identifier-shaped names are
    reachable — a column named ``'gap (eV)'`` can't be a Python name).
    A non-comparable row (``str < int``, a ``None`` operand) simply fails
    the predicate — it's filtered out, not an error."""
    if len(expr) > _WHERE_MAX_CHARS:
        raise BadInput(
            f"where= is {len(expr)} chars — the cap is {_WHERE_MAX_CHARS}",
            next=_WHERE_NEXT,
        )
    try:
        tree = ast.parse(expr, mode="eval")
        code = compile(tree, "<where>", "eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError) as e:
        raise BadInput(
            f"where= is not a valid expression: {e}", next=_WHERE_NEXT
        ) from e
    lookup: dict[str, int] = {}
    for i, c in enumerate(columns):
        if isinstance(c.get("id"), str):
            lookup[c["id"]] = i
    for i, c in enumerate(columns):  # names shadow ids
        if isinstance(c.get("name"), str):
            lookup[c["name"]] = i
    usable = sorted(n for n in lookup if n.isidentifier())
    for n_nodes, node in enumerate(ast.walk(tree), start=1):
        if n_nodes > _WHERE_MAX_NODES:
            raise BadInput(
                f"where= has more than {_WHERE_MAX_NODES} AST nodes — "
                "the expression is too complex",
                next=_WHERE_NEXT,
            )
        if not isinstance(node, _WHERE_NODES):
            raise BadInput(
                f"where= can't use {type(node).__name__} — only names, "
                "literals, comparisons and and/or/not",
                next=_WHERE_NEXT,
            )
        if (
            isinstance(node, ast.Name)
            and node.id not in lookup
            and node.id not in ("null", "true", "false")
        ):
            raise BadInput(
                f"where= names {node.id!r} — no such column",
                options=usable[:20],
                next=_WHERE_NEXT,
            )
        if isinstance(node, ast.Constant) and not isinstance(
            node.value, str | int | float | bool | type(None)
        ):
            raise BadInput(
                f"where= literal {node.value!r} is not a scalar",
                next=_WHERE_NEXT,
            )
    literals = {"null": None, "true": True, "false": False}

    def pred(row: list[Any]) -> bool:
        env = dict(literals)
        for name, i in lookup.items():
            if i < len(row):
                env[name] = row[i]
        try:
            return bool(eval(code, {"__builtins__": {}}, env))
        except Exception:
            return False

    return pred


def table_toon(
    table: dict[str, Any],
    *,
    cols: Any = None,
    rows: Any = None,
    where: Any = None,
) -> str:
    """Render a normalised ``meta.table`` as the id-addressed TOON grid —
    ``{#}`` header + ``<rowid>`` rows. Filters: ``cols=[id|name|a..b]``,
    ``rows=[id|a..b]``, ``where='<expr>'``."""
    columns = table.get("columns") or []
    rowmeta = table.get("rowmeta") or []
    data = table.get("rows") or []
    col_idx = _resolve_col_list(columns, cols)
    row_idx = _resolve_row_list(rowmeta, rows)
    if where is not None:
        pred = compile_where(str(where), columns)
        row_idx = [i for i in row_idx if i < len(data) and pred(data[i])]
    head = ["#"] + [header_cell(columns[i]) for i in col_idx]
    lines = ["{" + "\t".join(head) + "}"]
    for ri in row_idx:
        rid = str(rowmeta[ri].get("id") or "")
        lines.append(
            "\t".join(
                [rid, *(_toon._encode_cell(data[ri][ci], "\t") for ci in col_idx)]
            )
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Concurrency — base= + the recent journal
# ---------------------------------------------------------------------------


def table_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, list[str]]:
    """The per-write change set used by the ``recent`` journal:
    ``cells`` (``rid.cid`` whose value changed), ``rows``/``cols``
    (``+id`` added, ``-id`` dropped, ``~id`` spec/pos changed)."""
    oc = {c.get("id"): (i, c) for i, c in enumerate(old.get("columns") or [])}
    nc = {c.get("id"): (i, c) for i, c in enumerate(new.get("columns") or [])}
    orm = {r.get("id"): i for i, r in enumerate(old.get("rowmeta") or [])}
    nrm = {r.get("id"): i for i, r in enumerate(new.get("rowmeta") or [])}
    oc = {k: v for k, v in oc.items() if k}
    nc = {k: v for k, v in nc.items() if k}
    orm = {k: v for k, v in orm.items() if k}
    nrm = {k: v for k, v in nrm.items() if k}
    orows = old.get("rows") or []
    nrows = new.get("rows") or []
    ocpos = {cid: i for cid, (i, _c) in oc.items()}
    ncpos = {cid: i for cid, (i, _c) in nc.items()}
    cells: list[str] = []
    for rid, oi in orm.items():
        ni = nrm.get(rid)
        if ni is None or oi >= len(orows) or ni >= len(nrows):
            continue
        for cid, oci in ocpos.items():
            nci = ncpos.get(cid)
            if nci is None or oci >= len(orows[oi]) or nci >= len(nrows[ni]):
                continue
            if orows[oi][oci] != nrows[ni][nci]:
                cells.append(f"{rid}.{cid}")
    rows = [f"+{r}" for r in nrm if r not in orm] + [
        f"-{r}" for r in orm if r not in nrm
    ]
    rows += [
        f"~{r}"
        for r in nrm
        if r in orm
        and (old.get("rowmeta") or [])[orm[r]].get("pos")
        != (new.get("rowmeta") or [])[nrm[r]].get("pos")
    ]
    cols = [f"+{c}" for c in nc if c not in oc] + [f"-{c}" for c in oc if c not in nc]
    cols += [f"~{c}" for c in nc if c in oc and nc[c][1] != oc[c][1]]
    return {"cells": cells, "rows": rows, "cols": cols}


def journal_append(
    cur_table: dict[str, Any] | None, diff: dict[str, list[str]], new_hash: str
) -> list[dict[str, Any]]:
    """The ``recent`` list after one write: prior entries plus this
    write's ``{p, h, cells, rows, cols}``, capped at ``_RECENT_CAP``.
    The per-entry ``cells`` list is bounded too: past
    ``_JOURNAL_CELL_CAP`` it degrades to ``['*all', <cid>…]`` — the
    touched column ids — which :func:`check_table_base` treats as
    overlapping every cell in those columns."""
    recent = list((cur_table or {}).get("recent") or [])
    cells = list(diff.get("cells") or [])
    if len(cells) > _JOURNAL_CELL_CAP:
        cells = ["*all", *sorted({c.rpartition(".")[2] for c in cells})]
    entry = {
        "p": table_content_hash(cur_table or {}),
        "h": new_hash,
        **{**diff, "cells": cells},
    }
    return [*recent, entry][-_RECENT_CAP:]


def check_table_base(
    cur: dict[str, Any],
    base: str,
    touched: dict[str, list[str]],
    *,
    dc: str,
    full: bool = False,
) -> None:
    """``base=<hash>`` optimistic guard. ``touched`` is this edit's
    :func:`table_diff`-shaped change set. Refuses (BadInput) when the
    table changed since ``base`` AND the post-base journal overlaps the
    touched set; a ``base`` older than the journal (or with no journal
    at all) refuses conservatively.

    ``full=True`` (a ``toon=``/``table=`` whole-grid replace) refuses on
    ANY post-base write — a replace can't merge edits it never saw.

    Otherwise an edit collides when the journal:

    - changed a cell this edit changes (``rid.cid`` ∩ ``rid.cid``);
    - dropped an id this edit touches (any ``+-~`` mark on it);
    - holds a cell on an id this edit drops (``-X`` meets ``X.*`` or
      ``*.X`` — the drop would clobber a write the caller never saw);
    - added or spec-changed an id this edit drops (``-X`` meets ``+X`` /
      ``~X``);
    - spec-changed an id this edit spec-changes (``~X`` meets ``~X``).

    A coarse ``'*all'`` journal entry (see :func:`journal_append`)
    counts as changing every cell in its listed columns — every cell
    address and every row-drop in those columns collides, a column drop
    collides only when that column is listed (or none are, meaning all).
    """
    curh = table_content_hash(cur)
    nxt = f"get(kind='draft', id={dc!r}, view='toon') for the current table + hash"
    if base == curh:
        return
    recent = [e for e in (cur.get("recent") or []) if isinstance(e, dict)]
    since: list[dict[str, Any]] | None = None
    for i, e in enumerate(recent):
        # Oldest state that equals `base` wins: pre-hash of an entry
        # covers bases that predate the journal's first write.
        if e.get("p") == base:
            since = recent[i:]
            break
        if e.get("h") == base:
            since = recent[i + 1 :]
            break
    if since is None:
        raise BadInput(
            f"table changed since base {base!r} (now {curh}) — its change "
            "journal no longer reaches back that far; re-read before "
            "editing so you don't clobber a newer write",
            next=nxt,
        )
    if full:
        if since:
            raise BadInput(
                f"table changed since base {base!r} (now {curh}) — a "
                "whole-grid replace can't merge edits it never saw; "
                "re-read and redo it",
                next=nxt,
            )
        return
    changed_cells: set[str] = set()
    coarse_cols: set[str] = set()
    coarse = False
    marks: dict[str, set[str]] = {}
    for e in since:
        e_cells = [c for c in (e.get("cells") or []) if isinstance(c, str)]
        if "*all" in e_cells:
            coarse = True
            coarse_cols.update(c for c in e_cells if c != "*all")
        else:
            changed_cells.update(e_cells)
        for ref in [*(e.get("rows") or []), *(e.get("cols") or [])]:
            if isinstance(ref, str) and ref[:1] in ("+", "-", "~"):
                marks.setdefault(ref[1:], set()).add(ref[0])
    dropped = {x for x, m in marks.items() if "-" in m}
    cur_cols = {c.get("id") for c in cur.get("columns") or []}

    def cell_hit(cell: str) -> bool:
        if cell in changed_cells:
            return True
        return coarse and (not coarse_cols or cell.rpartition(".")[2] in coarse_cols)

    def drop_hit(x: str) -> bool:
        """A touched ``-X`` meets a journal cell on X, a ``+X``/``~X``,
        or a coarse entry covering a cell on X."""
        if marks.get(x, set()) & {"+", "~"}:
            return True
        if any(c.startswith(x + ".") or c.endswith("." + x) for c in changed_cells):
            return True
        if not coarse:
            return False
        if x in cur_cols:
            return not coarse_cols or x in coarse_cols
        return True  # a row id — a coarse entry touched some cell on it

    bad_cells = sorted(c for c in touched.get("cells") or [] if cell_hit(c))
    bad_ids: set[str] = set()
    for ref in [*(touched.get("rows") or []), *(touched.get("cols") or [])]:
        if not isinstance(ref, str) or ref[:1] not in ("+", "-", "~"):
            continue
        x = ref[1:]
        if (
            x in dropped
            or (ref[0] == "-" and drop_hit(x))
            or (ref[0] == "~" and "~" in marks.get(x, set()))
        ):
            bad_ids.add(x)
    if bad_cells or bad_ids:
        bits = []
        if bad_cells:
            bits.append(f"cells {bad_cells[:12]}")
        if bad_ids:
            bits.append(f"ids {sorted(bad_ids)[:12]}")
        raise BadInput(
            f"table changed since base {base!r} and it overlaps this edit "
            f"({'; '.join(bits)}) — re-read and redo it",
            next=nxt,
        )


# ---------------------------------------------------------------------------
# Patch (subset edit) and replace
# ---------------------------------------------------------------------------


def _resolve_patch_col(
    columns: list[dict[str, Any]], spec: dict[str, Any]
) -> dict[str, Any]:
    """One patch header cell → the stored column. ``id:name`` resolves by
    id (the name is then checked); a bare token resolves by id, then
    name. Calculated/pulled columns are refused — patches write data,
    not derivations."""
    nxt = (
        "valid columns are listed under 'options'; drop or fix the header "
        "cell — a schema change goes through add_column=/rename_column="
    )
    if spec.get("id") is not None:
        col = next((c for c in columns if c.get("id") == spec["id"]), None)
        if col is None:
            raise BadInput(
                f"patch column {spec['id']!r}: no such column id",
                options=[str(c.get("id")) for c in columns[:20]],
                next=nxt,
            )
        if spec.get("name") and spec["name"].strip() != col.get("name"):
            raise BadInput(
                f"column {spec['id']} is named {col.get('name')!r}, not "
                f"{spec['name']!r} — rename_column= first, or fix the "
                "patch header",
                next=nxt,
            )
    else:
        token = (spec.get("name") or "").strip()
        col = next((c for c in columns if c.get("id") == token), None)
        if col is None:
            col = next((c for c in columns if c.get("name") == token), None)
        if col is None:
            raise BadInput(
                f"patch column {token!r} matches no column id or name",
                options=[str(c.get("name") or c.get("id")) for c in columns[:20]],
                next=nxt,
            )
    for k in ("unit", "from", "formula"):
        if spec.get(k) is not None and (spec[k] or None) != (col.get(k) or None):
            raise BadInput(
                f"patch declares {k}={spec[k]!r} on column "
                f"{col.get('name')!r} — stored is {col.get(k)!r}",
                next=nxt,
            )
    if col.get("formula") or col.get("from"):
        raise BadInput(
            f"column {col.get('name')!r} is calculated/pulled — its cells "
            "can't be written in a patch",
            next="drop it from the patch header; it re-derives itself",
        )
    return col


def _cell_from_toon(raw: str, col: dict[str, Any]) -> Any:
    """TOON cell text → a JSON scalar. ``null`` or an empty cell clears
    (``None`` — the emit side renders ``None`` as ``''``, so this is the
    lossless inverse); a ``type='text'`` column keeps the string
    verbatim (no Excel inference), else :func:`infer_scalar`."""
    if raw == "" or raw == "null":
        return None
    if col.get("type") == "text":
        return raw
    return infer_scalar(raw)


def apply_patch(
    table: dict[str, Any],
    text: str,
    *,
    mode: str = "update",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply a ``patch=`` TOON doc to a normalised table; returns
    ``(new_table, report)``. Rows match by row id, else by the declared
    ``key`` column's value; empty cell = unchanged, ``null`` = clear.
    ``mode='update'`` refuses the whole patch on any unmatched row;
    ``mode='upsert'`` appends them (needs a key column)."""
    if mode not in ("update", "upsert"):
        raise BadInput(
            f"patch mode={mode!r} — use 'update' or 'upsert'",
            options=["update", "upsert"],
            next="mode='update' refuses unmatched rows; 'upsert' appends them",
        )
    columns = table.get("columns") or []
    rowmeta = table.get("rowmeta") or []
    data = table.get("rows") or []
    if not columns:
        raise BadInput(
            "this table has no column schema/ids to patch against",
            next="edit(kind='draft', id='dc<id>', table={…}) stores a grid first",
        )
    head_cells, raw_rows = _toon_records(text)
    specs = [parse_header_cell(c) for c in head_cells]
    cols = [_resolve_patch_col(columns, s) for s in specs]
    col_idx = [columns.index(c) for c in cols]
    if len(set(col_idx)) != len(col_idx):
        raise BadInput(
            "patch lists the same column twice",
            next="one header cell per column — merge the cells into one column",
        )
    key_idx = [i for i, c in enumerate(columns) if c.get("key")]
    rid_to_i = {rm.get("id"): i for i, rm in enumerate(rowmeta) if rm.get("id")}
    # key-column value → row indices, built once (a scan per patch row is
    # O(patch × table) — 5k×5k was seconds).
    key_to_i: dict[str, list[int]] = {}
    if key_idx:
        for i, row in enumerate(data):
            for k in key_idx:
                if k < len(row):
                    key_to_i.setdefault(cell_text(row[k]), []).append(i)
    matches: list[tuple[list[str], int | None, str]] = []
    unmatched: list[str] = []
    for prow in raw_rows:
        if len(prow) - 1 > len(specs):
            raise BadInput(
                f"patch row {prow[0]!r} has {len(prow) - 1} cells, the "
                f"header has {len(specs)} — extra cells past the header "
                "are refused (same rule as toon=)",
                next="drop the extra cells, or add the column first",
            )
        key = prow[0] if prow else ""
        ri = rid_to_i.get(key)
        if ri is None and key_idx:
            hits = key_to_i.get(key) or []
            if len(hits) > 1:
                raise BadInput(
                    f"patch row key {key!r} matches {len(hits)} rows — the "
                    "key column must be unique",
                    next="fix the duplicate keys, or patch by row id",
                )
            if hits:
                ri = hits[0]
        matches.append((prow, ri, key))
        if ri is None:
            unmatched.append(key)
    if unmatched and mode == "update":
        raise BadInput(
            f"patch row(s) {unmatched[:8]} match no row id"
            + (" or key-column value" if key_idx else "")
            + " — the whole patch was refused",
            options=[str(r.get("id")) for r in rowmeta[:20] if r.get("id")],
            next="fix the row keys, or mode='upsert' to append new rows "
            "(needs a key column)",
        )
    if unmatched and mode == "upsert" and not key_idx:
        raise BadInput(
            "mode='upsert' appends unmatched rows — that needs a declared "
            "key column (columns[].key=true) so re-patches find them again",
            next="add_column= a key column first, or patch by row id with "
            "mode='update'",
        )
    if mode == "upsert":
        dupes = sorted(k for k, n in Counter(unmatched).items() if n > 1)
        if dupes:
            raise BadInput(
                f"upsert patch lists new key(s) {dupes[:8]} twice — each "
                "would append a second row for the same key",
                next="merge the duplicate rows into one, or give them "
                "distinct '#' keys",
            )
    new_rows = [list(r) for r in data]
    changed_cells: list[str] = []
    changed_row_ids: list[str] = []
    for prow, ri, _key in matches:
        if ri is None:
            continue
        rid = str(rowmeta[ri].get("id"))
        touched = False
        for j, ci in enumerate(col_idx):
            raw = prow[j + 1] if j + 1 < len(prow) else ""
            if raw == "":
                continue  # empty = unchanged
            val = _cell_from_toon(raw, columns[ci])
            if new_rows[ri][ci] != val:
                new_rows[ri][ci] = val
                changed_cells.append(f"{rid}.{columns[ci].get('id')}")
                touched = True
        if touched:
            changed_row_ids.append(rid)
    appended_ids: list[str] = []
    new_rowmeta = [dict(rm) for rm in rowmeta]
    if mode == "upsert":
        used = {
            str(e["id"])
            for e in [*columns, *new_rowmeta]
            if isinstance(e, dict) and e.get("id")
        } | {str(i) for i in table.get("retired_ids") or []}
        kcol = key_idx[0] if key_idx else None
        for prow, ri, key in matches:
            if ri is not None:
                continue
            vals: list[Any] = [None] * len(columns)
            for j, ci in enumerate(col_idx):
                raw = prow[j + 1] if j + 1 < len(prow) else ""
                if raw == "":
                    continue
                vals[ci] = _cell_from_toon(raw, columns[ci])
            if kcol is not None:
                kval = _cell_from_toon(key, columns[kcol])
                if vals[kcol] is None:
                    vals[kcol] = kval
                elif cell_text(vals[kcol]) != key:
                    raise BadInput(
                        f"upsert row: '#' key {key!r} disagrees with the "
                        f"key-column cell {cell_text(vals[kcol])!r}",
                        next="make the '#' cell and the key column agree",
                    )
            rid = fresh_table_id(used)
            used.add(rid)
            new_rowmeta.append({"id": rid})
            new_rows.append(vals)
            appended_ids.append(rid)
    norm = normalize_table({**table, "rows": new_rows, "rowmeta": new_rowmeta})
    report = {
        "changed_cells": len(changed_cells),
        "changed_row_ids": changed_row_ids,
        "appended_row_ids": appended_ids,
        "unmatched": unmatched,
    }
    return norm, report


def table_from_toon(cur: dict[str, Any], text: str) -> dict[str, Any]:
    """``toon=`` full replace: the doc's columns and ``#``-keyed rows
    define the new grid. Column ids and row ids supplied in the doc are
    kept (round-trip); bare names match the stored column by name (keep
    its id); anything else mints. Stored ``formula``/``from`` columns the
    doc omits are preserved (appended) — they're calculated, not data.
    Dropped ids land in ``retired_ids``."""
    old_cols = cur.get("columns") or []
    old_rowmeta = cur.get("rowmeta") or []
    old_rows = cur.get("rows") or []
    head_cells, raw_rows = _toon_records(text)
    specs = [parse_header_cell(c) for c in head_cells]

    by_id = {c.get("id"): c for c in old_cols if c.get("id")}
    by_name = {c.get("name"): c for c in old_cols if c.get("name")}
    new_cols: list[dict[str, Any]] = []
    listed: set[str] = set()
    for spec in specs:
        stored = None
        if spec.get("id") is not None:
            stored = by_id.get(spec["id"])
        if stored is None and spec.get("name"):
            stored = by_name.get(spec["name"].strip())
        if spec.get("formula") is not None and spec["formula"] != (stored or {}).get(
            "formula"
        ):
            # A header formula byte-identical to the stored one is a
            # no-op echo (read-then-replace on a formula table) — kept.
            # A changed or new formula can't go through this channel:
            # the formula evaluator isn't part of this slice.
            raise BadInput(
                "toon= headers can't write a column formula — the "
                "formula evaluator isn't part of this slice",
                next="set formulas via columns= until slice 2 lands",
            )
        col: dict[str, Any] = dict(stored or {})
        if spec.get("id") is not None:
            col["id"] = spec["id"]
        name = (spec.get("name") or "").strip() or col.get("name")
        if not name:
            raise BadInput(
                f"toon= column {spec.get('id') or '?'!r} needs a name",
                next="header cells are <id>:<name> — "
                "get(kind='draft', id='dc<id>', view='toon') shows the shape",
            )
        col["name"] = name
        for k in ("unit", "from", "formula"):
            if spec.get(k) is not None:
                col[k] = spec[k]
        new_cols.append(col)
        if col.get("id"):
            listed.add(col["id"])
    preserved = [
        c
        for c in old_cols
        if (c.get("formula") or c.get("from")) and c.get("id") not in listed
    ]
    new_cols += [dict(c) for c in preserved]

    # Rows: '#' cell is the id to keep (or mint when empty). Cells align
    # to the doc's column order; preserved columns keep their stored cell
    # for rows that survive, None for new ones.
    doc_col_src: list[int | None] = []
    for col in new_cols[: len(specs)]:
        sid = col.get("id")
        src = next((i for i, c in enumerate(old_cols) if c.get("id") == sid), None)
        if src is None:
            src = next(
                (i for i, c in enumerate(old_cols) if c.get("name") == col.get("name")),
                None,
            )
        doc_col_src.append(src)
    pres_src = [
        next((i for i, c in enumerate(old_cols) if c.get("id") == p.get("id")), None)
        for p in preserved
    ]
    old_row_i = {rm.get("id"): i for i, rm in enumerate(old_rowmeta) if rm.get("id")}
    retired = {str(i) for i in cur.get("retired_ids") or []}
    used = retired | set(listed) | {c["id"] for c in preserved if c.get("id")}
    new_rows: list[list[Any]] = []
    new_rowmeta: list[dict[str, Any]] = []
    seen_rows: set[str] = set()
    for prow in raw_rows:
        rid = (prow[0] if prow else "").strip()
        if rid:
            if not is_table_id(rid):
                raise BadInput(
                    f"toon= '#' cell {rid!r} is not a row id — ids mint "
                    "server-side; leave the cell empty for a new row",
                    next="leave '#' empty to mint, or keep the id from view='toon'",
                )
            if rid in retired:
                raise BadInput(
                    f"toon= row id {rid!r} is retired — a deleted id never reappears",
                    next="leave '#' empty to mint a fresh id",
                )
            if rid in seen_rows:
                raise BadInput(f"toon= lists row id {rid!r} twice")
        else:
            rid = fresh_table_id(used)
        seen_rows.add(rid)
        used.add(rid)
        if len(prow) - 1 != len(specs):
            raise BadInput(
                f"toon= row {rid} has {len(prow) - 1} cells, header has "
                f"{len(specs)} — a full replace needs every cell",
                next="pad missing cells with 'null' (clears) — or patch= "
                "for a partial edit",
            )
        oi = old_row_i.get(rid)
        vals: list[Any] = []
        for j, col in enumerate(new_cols[: len(specs)]):
            raw = prow[j + 1]
            src = doc_col_src[j]
            # Lossless round-trip: when the doc cell renders exactly the
            # stored value, keep the stored scalar verbatim — a string
            # like '0603' or 'null' isn't re-inferred, a float keeps its
            # type, and a no-op toon= diffs to nothing.
            if (
                oi is not None
                and src is not None
                and src < len(old_rows[oi])
                and cell_text(old_rows[oi][src]) == raw
            ):
                vals.append(old_rows[oi][src])
            else:
                vals.append(_cell_from_toon(raw, col))
        for col, src in zip(preserved, pres_src):
            vals.append(
                old_rows[oi][src]
                if oi is not None and src is not None and src < len(old_rows[oi])
                else None
            )
        new_rows.append(vals)
        new_rowmeta.append({"id": rid})
    # Dropped row/col ids retire; pos re-mints fresh in doc order.
    dropped = [
        str(e.get("id"))
        for e in [*old_cols, *old_rowmeta]
        if isinstance(e, dict)
        and e.get("id")
        and e["id"] not in listed
        and e["id"] not in seen_rows
        and e["id"] not in {c.get("id") for c in preserved}
    ]
    for col in new_cols:
        col.pop("pos", None)
    out = {
        "header": [c["name"] for c in new_cols],
        "rows": new_rows,
        "columns": new_cols,
        "rowmeta": new_rowmeta,
        "retired_ids": sorted(retired | set(dropped)),
    }
    return normalize_table(out)


# ---------------------------------------------------------------------------
# Structure ops — add/move/delete row and column by id
# ---------------------------------------------------------------------------


def _col_index(columns: list[dict[str, Any]], ref: Any, *, what: str = "column") -> int:
    if not isinstance(ref, str):
        raise BadInput(
            f"{what} ref must be a column id or name, got {ref!r}",
            next="get(kind='draft', id='dc<id>', view='toon') lists them",
        )
    i = next((i for i, c in enumerate(columns) if c.get("id") == ref), None)
    if i is None:
        i = next((i for i, c in enumerate(columns) if c.get("name") == ref), None)
    if i is None:
        raise BadInput(
            f"no {what} id/name {ref!r} in this table",
            options=[str(c.get("id") or c.get("name")) for c in columns[:20]],
            next="get(kind='draft', id='dc<id>', view='toon') lists them",
        )
    return i


def _row_index(rowmeta: list[dict[str, Any]], ref: Any) -> int:
    if not isinstance(ref, str):
        raise BadInput(
            f"row ref must be a row id, got {ref!r}",
            next="get(kind='draft', id='dc<id>', view='toon') lists them",
        )
    i = next((i for i, r in enumerate(rowmeta) if r.get("id") == ref), None)
    if i is None:
        raise BadInput(
            f"no row id {ref!r} in this table",
            options=[str(r.get("id")) for r in rowmeta[:20] if r.get("id")],
            next="get(kind='draft', id='dc<id>', view='toon') lists them",
        )
    return i


def _id_pool(table: dict[str, Any]) -> set[str]:
    return {
        str(e["id"])
        for e in [*(table.get("columns") or []), *(table.get("rowmeta") or [])]
        if isinstance(e, dict) and e.get("id")
    } | {str(i) for i in table.get("retired_ids") or []}


def _insert_at(
    items: list[dict[str, Any]], at: Any, *, what: str
) -> tuple[int, str | None, str | None]:
    """``at='end'|'start'|{'after':id}|{'before':id}`` → ``(index,
    prev_pos, next_pos)`` for a :func:`key_between` mint."""
    if at is None or at == "end":
        return len(items), (items[-1].get("pos") if items else None), None
    if at == "start":
        return 0, None, (items[0].get("pos") if items else None)
    if isinstance(at, dict):
        for key, idx in (("after", 1), ("before", 0)):
            if key in at:
                i = next(
                    (i for i, e in enumerate(items) if e.get("id") == at[key]),
                    None,
                )
                if i is None:
                    raise BadInput(
                        f"at={at!r}: no {what} id {at[key]!r} in this table",
                        options=[str(e.get("id")) for e in items[:20] if e.get("id")],
                        next="get(kind='draft', id='dc<id>', view='toon') lists ids",
                    )
                j = i + idx
                return (
                    j,
                    items[j - 1].get("pos") if j else None,
                    items[j].get("pos") if j < len(items) else None,
                )
    raise BadInput(
        f"at={at!r} — use 'end', 'start', {{'after': '<id>'}} or {{'before': '<id>'}}",
        next="at='end' (default) or at={'after': 'r2k7'}",
    )


def _strip_pos(*groups: list[dict[str, Any]]) -> None:
    """Drop ``pos`` from every entry so ``normalize_table`` re-mints a
    clean :func:`even_keys` run in list order — used when an op rewrote
    order wholesale (insert/delete of the id'd kind)."""
    for g in groups:
        for e in g:
            e.pop("pos", None)


def op_add_column(table: dict[str, Any], spec: Any, at: Any = None) -> dict[str, Any]:
    """``add_column={name, type?, unit?, formula?, from?, role?, key?,
    align?}`` — append (or ``at=``-position) a column; its cells start
    ``None``."""
    if not isinstance(spec, dict):
        raise BadInput(
            f"add_column= must be a column spec dict, got {spec!r}",
            next="add_column={'name': 'qty', 'type': 'number'}",
        )
    columns = [dict(c) for c in table.get("columns") or []]
    rows = [list(r) for r in table.get("rows") or []]
    rowmeta = [dict(r) for r in table.get("rowmeta") or []]
    col = dict(spec)
    col["id"] = fresh_table_id(_id_pool(table))
    j, prev, nxt = _insert_at(columns, at, what="column")
    col["pos"] = key_between(prev, nxt)
    columns.insert(j, col)
    for row in rows:
        row.insert(j, None)
    _strip_pos(columns)  # keep pos a monotone function of list order
    return normalize_table(
        {
            **table,
            "header": [c.get("name") for c in columns],
            "columns": columns,
            "rows": rows,
            "rowmeta": rowmeta,
        }
    )


def op_drop_column(table: dict[str, Any], ref: Any) -> dict[str, Any]:
    """``drop_column=<id|name>`` — remove the column and its cells; the id
    retires (never re-mints)."""
    columns = [dict(c) for c in table.get("columns") or []]
    i = _col_index(columns, ref)
    if len(columns) == 1:
        raise BadInput(
            "can't drop the last column — a table needs a header",
            next="toon= replaces the whole grid instead",
        )
    col = columns.pop(i)
    rows = [[c for j, c in enumerate(row) if j != i] for row in table.get("rows") or []]
    _strip_pos(columns)
    retired = sorted({str(x) for x in table.get("retired_ids") or []} | {col["id"]})
    return normalize_table(
        {
            **table,
            "header": [c.get("name") for c in columns],
            "columns": columns,
            "rows": rows,
            "retired_ids": retired,
        }
    )


def op_rename_column(table: dict[str, Any], spec: Any) -> dict[str, Any]:
    """``rename_column={'from': <id|name>, 'to': '<name>'}`` (also
    ``{<ref>: '<name>'}`` or a 2-list) — the schema-level rename; ids and
    cells untouched."""
    ref: Any = None
    name: Any = None
    if isinstance(spec, dict):
        if "from" in spec or "to" in spec:
            ref, name = spec.get("from"), spec.get("to")
        elif len(spec) == 1:
            ref, name = next(iter(spec.items()))
    elif isinstance(spec, list | tuple) and len(spec) == 2:
        ref, name = spec
    if ref is None or not isinstance(name, str) or not name.strip():
        raise BadInput(
            f"rename_column={spec!r} — use {'from': '<id|name>', 'to': '<name>'}",
            next="rename_column={'from': 'k5f9', 'to': 'qty'}",
        )
    columns = [dict(c) for c in table.get("columns") or []]
    i = _col_index(columns, ref)
    columns[i]["name"] = name.strip()
    return normalize_table(
        {
            **table,
            "header": [c.get("name") for c in columns],
            "columns": columns,
        }
    )


def _move(
    items: list[Any], i: int, at: Any, *, what: str
) -> tuple[list[Any], int, str | None]:
    """Reinsert ``items[i]`` per ``at`` and re-mint its ``pos`` with
    :func:`key_between`; returns the new list (entries are dicts or
    plain values — pos only applies to dicts)."""
    rest = [*items[:i], *items[i + 1 :]]
    j, prev, nxt = _insert_at(rest, at, what=what)
    moved = items[i]
    out = [*rest[:j], moved, *rest[j:]]
    return out, j, key_between(prev, nxt)


def op_move_column(table: dict[str, Any], ref: Any, at: Any) -> dict[str, Any]:
    """``move_column=<id|name>`` + ``at=`` — reposition one column;
    identity and cells follow it."""
    columns = [dict(c) for c in table.get("columns") or []]
    i = _col_index(columns, ref)
    if isinstance(at, dict):
        tgt = at.get("after") or at.get("before")
        if tgt == columns[i].get("id"):
            raise BadInput(
                "can't move a column relative to itself",
                next="at={'after': '<other column id>'}",
            )
    new_cols, j, pos = _move(columns, i, at or "end", what="column")
    new_cols[j]["pos"] = pos
    rows = [
        [*row[:i], *row[i + 1 :]][:j] + [row[i]] + [*row[:i], *row[i + 1 :]][j:]
        for row in table.get("rows") or []
    ]
    return normalize_table(
        {
            **table,
            "header": [c.get("name") for c in new_cols],
            "columns": new_cols,
            "rows": rows,
        }
    )


def op_move_row(table: dict[str, Any], ref: Any, at: Any) -> dict[str, Any]:
    """``move_row=<id>`` + ``at=`` — reposition one row."""
    rowmeta = [dict(r) for r in table.get("rowmeta") or []]
    i = _row_index(rowmeta, ref)
    if isinstance(at, dict):
        tgt = at.get("after") or at.get("before")
        if tgt == rowmeta[i].get("id"):
            raise BadInput(
                "can't move a row relative to itself",
                next="at={'after': '<other row id>'}",
            )
    new_rm, j, pos = _move(rowmeta, i, at or "end", what="row")
    new_rm[j]["pos"] = pos
    rows = [*(table.get("rows") or [])]
    row = rows.pop(i)
    rows.insert(j, row)
    return normalize_table({**table, "rowmeta": new_rm, "rows": rows})


def op_insert_rows(
    table: dict[str, Any], new_rows: Any, at: Any = None
) -> dict[str, Any]:
    """``rows=[[…]]`` + ``at=`` — insert rows (cells in column order);
    ids mint."""
    columns = table.get("columns") or []
    width = len(columns)
    if not isinstance(new_rows, list) or not all(isinstance(r, list) for r in new_rows):
        raise BadInput(
            "rows= must be a list of cell lists (column order)",
            next="rows=[['jlcpcb', 'C1', 100]]",
        )
    for i, row in enumerate(new_rows):
        if len(row) != width:
            raise BadInput(
                f"rows[{i}] has {len(row)} cells, table has {width} columns",
                next="pad missing cells with null",
            )
        for cell in row:
            if not isinstance(cell, str | int | float | bool | type(None)):
                raise BadInput(
                    f"rows[{i}] cell {cell!r} is not a JSON scalar",
                    next="cells are string/number/bool/null",
                )
    rowmeta = [dict(r) for r in table.get("rowmeta") or []]
    rows = [list(r) for r in table.get("rows") or []]
    j, prev, nxt = _insert_at(rowmeta, at, what="row")
    used = _id_pool(table)
    for k, row in enumerate(new_rows):
        rm = {"id": fresh_table_id(used), "pos": key_between(prev, nxt)}
        used.add(rm["id"])
        prev = rm["pos"]
        rowmeta.insert(j + k, rm)
        rows.insert(j + k, list(row))
    return normalize_table({**table, "rowmeta": rowmeta, "rows": rows})


def op_drop_rows(table: dict[str, Any], refs: Any) -> dict[str, Any]:
    """``drop_rows=[<id>, 'a..b', …]`` — delete rows by id or range; ids
    retire."""
    rowmeta = [dict(r) for r in table.get("rowmeta") or []]
    idx = _resolve_row_list(rowmeta, refs)
    gone = {str(rowmeta[i].get("id")) for i in idx}
    new_rm = [r for i, r in enumerate(rowmeta) if i not in idx]
    rows = [r for i, r in enumerate(table.get("rows") or []) if i not in idx]
    retired = sorted({str(x) for x in table.get("retired_ids") or []} | gone)
    _strip_pos(new_rm)
    return normalize_table(
        {**table, "rowmeta": new_rm, "rows": rows, "retired_ids": retired}
    )


def op_sort_rows(table: dict[str, Any], spec: Any) -> dict[str, Any]:
    """``sort='<col> [asc|desc]'`` — reorder rows by one column (id or
    name, default asc); empties sort last either way. Rewrites the row
    ``pos`` keys only — cells and ids are untouched."""
    if not isinstance(spec, str) or not spec.strip():
        raise BadInput(
            f"sort= must be '<column>' or '<column> desc', got {spec!r}",
            next="sort='qty desc'",
        )
    parts = spec.strip().split()
    direction = parts[1].lower() if len(parts) > 1 else "asc"
    if len(parts) > 2 or direction not in ("asc", "desc"):
        raise BadInput(
            f"sort={spec!r} — use '<column>' or '<column> asc|desc'",
            next="sort='qty desc'",
        )
    columns = table.get("columns") or []
    ci = _col_index(columns, parts[0])
    desc = direction == "desc"
    rows = table.get("rows") or []
    rowmeta = [dict(r) for r in table.get("rowmeta") or []]

    def rank(i: int) -> tuple[int, Any]:
        v = rows[i][ci] if ci < len(rows[i]) else None
        if isinstance(v, bool):
            return (1, "true" if v else "false")
        if isinstance(v, int | float):
            return (0, v)
        return (1, str(v).casefold())

    solid = [i for i in range(len(rows)) if rows[i][ci] is not None]
    empty = [i for i in range(len(rows)) if rows[i][ci] is None]
    solid.sort(key=rank, reverse=desc)
    order = solid + empty
    out_rows = [rows[i] for i in order]
    out_rm = [rowmeta[i] for i in order]
    for rm, pos in zip(out_rm, even_keys(len(out_rm)), strict=True):
        rm["pos"] = pos
    return normalize_table({**table, "rowmeta": out_rm, "rows": out_rows})

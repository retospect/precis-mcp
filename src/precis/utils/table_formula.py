"""Spreadsheet-table formulas and footer aggregates (spreadsheet-tables
slice 2).

A ``columns[].formula`` is a per-row expression over the table's own
columns (``formula='unit_price*qty'``), evaluated on every write in
dependency order with the result stored back into ``rows`` — there is no
read-time recompute. ``meta.table.footer = {col_id: func}`` declares
aggregates over a column (``sum mean median std min max count``); the
computed values land in ``meta.table.footer_values`` on the same write.

SECURITY: formula text can arrive from an agent relaying ingested
content, so the compile whitelist — not the sandbox — is the boundary.
A formula is parsed to an AST and every node must be one of the allowed
arithmetic/comparison/boolean/call forms: attribute access, subscripts,
f-strings, comprehensions, lambdas, literals for compound types and any
statement-ish node are refused at declaration (``BadInput``), never
evaluated. Evaluation itself runs on a :class:`simpleeval.SimpleEval`
subclass with the ``ast.Attribute``/``ast.Subscript`` handlers removed
(belt and braces), a fixed function whitelist, tightened power/string
caps, and a per-write cell + wall-clock budget. ``**`` and ``*`` on
strings/sequences are capped so ``9**9**9`` / ``'x'*10**8`` payloads die
as error cells, not as a hung write.

Formulas are *stored* in column-id form (``k5f9*q0wc``) — ids are valid
Python names by construction (letter first) — while callers write them
with column names; :func:`compile_formula` is the name→id rewrite so a
later rename touches nothing. A dependency cycle between formula columns is a stored fault, not a
refusal (spec §4): every column on the loop stores ``#CYCLE`` and is
dropped from the evaluation order, the edit lands, and the reply's
warning names the loop. A runtime fault (``#DIV/0``, ``#REF``,
``#VALUE``, ``#NUM``, ``#CYCLE``, ``#ERROR``) is stored as the cell
value and named in the reply — data errors never refuse an edit, and an
error-marker operand propagates its marker to dependents (the first
error operand wins, as in Excel/Sheets — ``#REF`` stays ``#REF``;
``#VALUE`` is only a fresh type error).
"""

from __future__ import annotations

import ast
import math
import time
from typing import Any

import numpy as np
from simpleeval import (
    DEFAULT_OPERATORS,
    IterableTooLong,
    NameNotDefined,
    NumberTooHigh,
    SimpleEval,
)

from precis.errors import BadInput
from precis.utils.table_data import FOOTER_FUNCS  # noqa: F401  (re-export)


#: Function whitelist for per-row formulas (the whole reachable surface —
#: anything else raises ``FunctionNotDefined`` at eval, refused earlier at
#: compile). ``if_`` evaluates both branches eagerly; the lazy form is the
#: ``x if cond else y`` IfExp, also allowed.
def _vsum(*args: Any) -> Any:
    return sum(args)


def _vavg(*args: Any) -> Any:
    vals = [a for a in args]
    if not vals:
        raise ValueError("avg() needs at least one argument")
    return sum(vals) / len(vals)


def _safe_round(x: Any, ndigits: Any = 0) -> Any:
    """``round`` with the unbounded-argument door shut: ``round(int, -k)``
    divides by ``10**k`` — ``k`` is caller input, and the wall-clock
    deadline only runs BETWEEN evals (one cell at ``k=-10**7`` took
    ~10 s; ``-10**8`` runs for minutes). ``-ndigits`` past every bit of
    ``x`` provably rounds to 0 (``|x| < 2**(-ndigits) < 10**(-ndigits)/2``),
    so the giant divisor never materialises; what's left divides by at
    most a ``bit_length(x)``-digit power — bounded by the stored-int cap.
    Positive ``ndigits`` short-circuits in CPython, floats both ways.
    """
    if isinstance(x, int) and isinstance(ndigits, int) and -ndigits > x.bit_length():
        return 0
    return round(x, ndigits)


#: Function whitelist for per-row formulas (the whole reachable surface —
#: anything else raises ``FunctionNotDefined`` at eval, refused earlier at
#: compile). ``if_`` evaluates both branches eagerly; the lazy form is the
#: ``x if cond else y`` IfExp, also allowed. Audit for the
#: ``round(int, -k)`` pattern (an argument that scales work): ``min``/
#: ``max``/``sum``/``avg`` are bounded by the 400-node AST cap on arg
#: count and the operand bit caps; ``abs``/``sqrt``/``log`` are O(digits);
#: ``exp`` overflows fast; ``if_`` is O(1). Only ``round`` needed a wrap.
_FORMULA_FUNCTIONS: dict[str, Any] = {
    "abs": abs,
    "round": _safe_round,
    "min": min,
    "max": max,
    "sum": _vsum,
    "avg": _vavg,
    "sqrt": math.sqrt,
    "log": math.log,
    "exp": math.exp,
    "if_": lambda cond, a, b: a if cond else b,
}

#: Allowed AST node types in a formula — anything else is refused at
#: compile time. Deliberately excludes Attribute, Subscript, JoinedStr/
#: FormattedValue (f-strings), comprehensions, Lambda, literal compound
#: types (List/Tuple/Dict/Set), Starred, NamedExpr, Await, Slice/Index —
#: the reachable object-graph and DoS doors.
_ALLOWED_NODES: tuple[type, ...] = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.keyword,
    # operators (BinOp/UnaryOp op payloads are nodes too)
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Not,
    ast.Eq,
    ast.NotEq,
    ast.Gt,
    ast.Lt,
    ast.GtE,
    ast.LtE,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
    ast.And,
    ast.Or,
)

_FORMULA_MAX_LEN = 1000
_AST_MAX_NODES = 400

#: Tighter than simpleeval's module caps (MAX_POWER 4e6, MAX_STRING_LENGTH
#: 1e5): table math never legitimately needs a 4000-digit integer or a
#: 4 KB repeated string per cell.
_POW_CAP = 1000
_STR_CAP = 4000

#: Stored ints above ~1e308 carry no spreadsheet meaning and break the
#: write itself (``json.dumps`` refuses ints over ~4300 digits, jsonb
#: can't take them either) — they store as ``#NUM``.
_INT_CAP = 10**308

#: Operand bit-length budget for ``*``, ``+`` and ``**``: chained formula
#: columns square an int per step, so without a bound a few columns turn
#: one write into minutes of bignum math. 4096 bits (~1234 digits) is far
#: past the storage cap, so the bound only bites growth that would store
#: ``#NUM`` anyway.
_INT_BIT_CAP = 4096

#: Per-write evaluation budget — a formula column on a large table
#: multiplies evals, so cap rows×formula-cols AND wall-clock. A breach
#: refuses the write (it is a schema/size problem, not a data error).
_EVAL_CELL_CAP = 200_000
_EVAL_WALL_S = 5.0


def _over_bit_cap(a: Any, b: Any, bits: int) -> bool:
    """True when both operands are ints whose result needs > ``bits``
    bits — the bignum-growth bound shared by ``*``/``+``/``**``."""
    return (
        isinstance(a, int)
        and isinstance(b, int)
        and a.bit_length() + b.bit_length() > bits
    )


def _safe_pow(a: Any, b: Any) -> Any:
    if (
        isinstance(a, int)
        and isinstance(b, int)
        and b > 0
        and b * max(a.bit_length(), 1) > _INT_BIT_CAP
    ):
        raise NumberTooHigh(f"power over the {_INT_BIT_CAP}-bit cap")
    if abs(a) > _POW_CAP or abs(b) > _POW_CAP:
        raise NumberTooHigh(f"exponent/base over the {_POW_CAP} cap")
    return a**b


def _safe_mult(a: Any, b: Any) -> Any:
    # No numeric-string coercion (spec §4): ``'2' * 3`` and ``'x' * n``
    # are #VALUE, which also removes the repeat-DoS path entirely.
    # Bytes too — compile refuses them, this is the eval-side brace.
    if isinstance(a, str | bytes) or isinstance(b, str | bytes):
        raise TypeError("* takes numbers, not strings")
    if _over_bit_cap(a, b, _INT_BIT_CAP):
        raise NumberTooHigh(f"product over the {_INT_BIT_CAP}-bit cap")
    return a * b


def _safe_add(a: Any, b: Any) -> Any:
    if isinstance(a, str | bytes) or isinstance(b, str | bytes):
        raise TypeError("+ takes numbers, not strings")
    if _over_bit_cap(a, b, _INT_BIT_CAP):
        raise NumberTooHigh(f"sum over the {_INT_BIT_CAP}-bit cap")
    return a + b


def _safe_mod(a: Any, b: Any) -> Any:
    # ``'%0100000000d' % x`` is a 100 MB string per cell — a str (or
    # bytes) left operand is sprintf, not spreadsheet math: #VALUE,
    # never evaluated.
    if isinstance(a, str | bytes):
        raise TypeError("% takes numbers, not format strings")
    return a % b


#: Operators a formula may use — arithmetic + comparison + boolean, with
#: the capped pow/mult/add above; bitwise shifts/bitops are dropped
#: entirely (not spreadsheet math, only a shift-DoS surface).
def _operators() -> dict[type, Any]:
    keep = (
        ast.Sub,
        ast.Div,
        ast.FloorDiv,
        ast.Eq,
        ast.NotEq,
        ast.Gt,
        ast.Lt,
        ast.GtE,
        ast.LtE,
        ast.Not,
        ast.USub,
        ast.UAdd,
        ast.Is,
        ast.IsNot,
        ast.In,
        ast.NotIn,
    )
    ops = {k: v for k, v in DEFAULT_OPERATORS.items() if k in keep}
    ops[ast.Add] = _safe_add
    ops[ast.Mult] = _safe_mult
    ops[ast.Pow] = _safe_pow
    ops[ast.Mod] = _safe_mod
    return ops


class _TableEval(SimpleEval):
    """``SimpleEval`` locked to the formula grammar: the attribute and
    subscript handlers are *removed* (simpleeval has no switch for them —
    spec §4), so a node that survived the compile whitelist still dies on
    ``FeatureNotAvailable`` instead of walking an object graph."""

    def __init__(self) -> None:
        super().__init__(
            operators=_operators(),
            functions=dict(_FORMULA_FUNCTIONS),
            names={},
        )
        for node in (
            ast.Attribute,
            ast.Subscript,
            ast.Index,
            ast.Slice,
            ast.JoinedStr,
            ast.FormattedValue,
            ast.Assign,
            ast.AugAssign,
            ast.Import,
        ):
            self.nodes.pop(node, None)


def _col_resolver(columns: list[dict[str, Any]]) -> dict[str, str]:
    """name-or-id → column id. Ids win on the (pathological) collision of a
    column *named* like another column's id."""
    out: dict[str, str] = {}
    for c in columns:
        if c.get("name"):
            out[str(c["name"])] = c["id"]
    for c in columns:
        if c.get("id"):
            out[c["id"]] = c["id"]
    return out


def compile_formula(
    formula: str,
    columns: list[dict[str, Any]],
    *,
    strict: bool = False,
    retired: set[str] | None = None,
) -> str:
    """Validate a caller-supplied formula and return its stored form with
    every column reference rewritten to the column's stable id.

    Refuses (``BadInput``) on: over-long/unparseable text, any AST node
    outside the whitelist (attribute access, subscripts, comprehensions,
    f-strings, lambdas, compound literals, …), a call to a
    non-whitelisted function, and — with ``strict`` — a name that
    resolves to no column of this table. ``strict`` is for a NEW or
    CHANGED formula: a typo must refuse with the real column names/ids,
    not land silently. Without it the name stays unresolved in the
    stored form — the ``#REF`` path for a stored formula whose column
    was dropped by a later edit. ``retired`` is the table's retired-id
    pool: with ``strict`` it sharpens the refusal for a stored formula
    restated after its column was dropped (still holding the retired
    id) — restated is declared, but the message names the real cause.
    """
    nxt = "formula='unit_price*qty' — column names/ids, +-*/** %, comparisons, and/or/not, if_(c,a,b)"
    if not isinstance(formula, str) or not formula.strip():
        raise BadInput("a column formula must be a non-empty string", next=nxt)
    if len(formula) > _FORMULA_MAX_LEN:
        raise BadInput(
            f"formula is {len(formula)} chars — cap is {_FORMULA_MAX_LEN}",
            next=nxt,
        )
    try:
        tree = ast.parse(formula, mode="eval")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise BadInput(f"formula {formula!r} does not parse: {exc}", next=nxt) from exc

    resolver = _col_resolver(columns)
    func_nodes: set[int] = set()
    for n_nodes, node in enumerate(ast.walk(tree), 1):
        if n_nodes > _AST_MAX_NODES:
            raise BadInput(
                f"formula is too large ({_AST_MAX_NODES} AST node cap)",
                next="simplify the expression or split it over two formula columns",
            )
        if not isinstance(node, _ALLOWED_NODES):
            raise BadInput(
                f"formula {formula!r}: {type(node).__name__} is not allowed "
                "(no attribute access, subscripts, f-strings, comprehensions, "
                "lambdas or compound literals)",
                next=nxt,
            )
        if isinstance(node, ast.Constant) and not isinstance(
            node.value, str | int | float | bool | type(None)
        ):
            # Scalar literals only: a bytes constant (``b'x'*10**9``,
            # ``b'%0999999999d' % 1``) slips past the str guards below
            # and materialises gigabytes per cell; a complex (``1j``)
            # or ``...`` has no spreadsheet meaning anyway.
            raise BadInput(
                f"formula {formula!r}: literal {node.value!r} is not a "
                "scalar — only numbers, text, true/false and null",
                next=nxt,
            )
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or (
                node.func.id not in _FORMULA_FUNCTIONS
            ):
                raise BadInput(
                    f"formula {formula!r}: only whitelisted functions may be "
                    f"called ({sorted(_FORMULA_FUNCTIONS)})",
                    next=nxt,
                )
            func_nodes.add(id(node.func))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and id(node) not in func_nodes:
            col_id = resolver.get(node.id)
            if col_id is None:
                if strict:
                    if retired and node.id in retired:
                        raise BadInput(
                            f"formula {formula!r} names {node.id!r} — a "
                            "dropped column (a retired id)",
                            next="fix the name in the formula to a live "
                            "column, or remove the column's formula",
                        )
                    raise BadInput(
                        f"formula {formula!r}: unknown name {node.id!r} — "
                        "not a column of this table",
                        options=[
                            f"{c.get('name') or '?'} ({c.get('id') or '?'})"
                            for c in columns[:20]
                        ],
                        next=nxt,
                    )
                # Unresolved names stay as written — see the docstring:
                # they are the #REF path, not a declaration fault.
            else:
                node.id = col_id
    return ast.unparse(tree)


def formula_name_form(compiled: str, columns: list[dict[str, Any]]) -> str:
    """The display inverse of :func:`compile_formula`: a stored (id-form)
    formula rendered the way the caller wrote it — every live column id
    becomes its current name when that name is a valid identifier.
    Anything else keeps the stored text: a dropped column's id (the cell
    is ``#REF`` anyway), a non-identifier name (the caller must have used
    the id), or an unparseable store. Display-only — the stored id form
    is what evaluation and the content hash see.
    """
    id_to_name = {
        c["id"]: str(c["name"])
        for c in columns
        if c.get("id") and c.get("name") and str(c["name"]).isidentifier()
    }
    try:
        tree = ast.parse(compiled, mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        return compiled
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in id_to_name:
            node.id = id_to_name[node.id]
    return ast.unparse(tree)


def _formula_deps(
    compiled: dict[int, str], columns: list[dict[str, Any]]
) -> dict[int, set[int]]:
    """formula-col index → indices of the formula columns it reads."""
    col_ids = {c.get("id") for c in columns}
    deps: dict[int, set[int]] = {}
    for i, text in compiled.items():
        tree = ast.parse(text, mode="eval")
        refs = {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in col_ids
        }
        deps[i] = {
            j for j, c in enumerate(columns) if j in compiled and c.get("id") in refs
        }
    return deps


def _topo_order(
    compiled: dict[int, str], columns: list[dict[str, Any]]
) -> tuple[list[int], list[list[int]]]:
    """``(order, cycles)`` — formula-column indices in dependency order,
    and the dependency cycles found. Each cycle is the full member list
    of one strongly connected component, so a loop names every column on
    it: ``A=B+C, B=A, C=B`` is one component ``{A, B, C}``, not just the
    ``A -> B -> A`` walk a DFS happens to take. Tarjan, iterative: a
    several-thousand-column chain must not hit the recursion limit.
    Cyclic columns are dropped from ``order`` — never evaluated;
    :func:`evaluate_table` stamps their cells ``#CYCLE`` so dependents
    propagate the marker. A column that depends on a cyclic column is
    not itself cyclic: it stays in ``order`` and picks the marker up at
    eval. Tarjan emits components sinks-first, which IS the evaluation
    order (dependencies first) for the acyclic rest."""
    deps = _formula_deps(compiled, columns)
    index: dict[int, int] = {}
    low: dict[int, int] = {}
    on_stack: set[int] = set()
    tstack: list[int] = []
    sccs: list[list[int]] = []
    counter = 0
    for root in compiled:
        if root in index:
            continue
        index[root] = low[root] = counter
        counter += 1
        tstack.append(root)
        on_stack.add(root)
        work: list[tuple[int, Any]] = [(root, iter(sorted(deps[root])))]
        while work:
            node, it = work[-1]
            descended = False
            for j in it:
                if j not in index:
                    index[j] = low[j] = counter
                    counter += 1
                    tstack.append(j)
                    on_stack.add(j)
                    work.append((j, iter(sorted(deps[j]))))
                    descended = True
                    break
                if j in on_stack:
                    low[node] = min(low[node], index[j])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                scc: list[int] = []
                while True:
                    w = tstack.pop()
                    on_stack.discard(w)
                    scc.append(w)
                    if w == node:
                        break
                sccs.append(sorted(scc))
    cyclic = {i for scc in sccs if len(scc) > 1 or scc[0] in deps[scc[0]] for i in scc}
    cycles = [scc for scc in sccs if any(i in cyclic for i in scc)]
    order = [i for scc in sccs for i in scc if i not in cyclic]
    return order, cycles


_ERR_DIV = "#DIV/0"
_ERR_REF = "#REF"
_ERR_VALUE = "#VALUE"
_ERR_NUM = "#NUM"
_ERR_CYCLE = "#CYCLE"
_ERR_GENERIC = "#ERROR"

#: The stored runtime-fault vocabulary. A cell holding one of these IS an
#: error — formulas propagate it untouched (first operand wins), and the
#: reply names the cell. Membership is the test: a text result that merely
#: *starts* with ``#`` is data, not an error.
_ERR_MARKERS = frozenset(
    {_ERR_DIV, _ERR_REF, _ERR_VALUE, _ERR_NUM, _ERR_CYCLE, _ERR_GENERIC}
)


def _err_marker(exc: BaseException) -> str:
    if isinstance(exc, ZeroDivisionError):
        return _ERR_DIV
    if isinstance(exc, NameNotDefined):
        return _ERR_REF
    if isinstance(exc, (NumberTooHigh, IterableTooLong, OverflowError)):
        return _ERR_NUM
    if isinstance(exc, (TypeError, ValueError, KeyError, IndexError)):
        return _ERR_VALUE
    return _ERR_GENERIC


def _store_scalar(v: Any) -> Any:
    """Coerce an eval result to a JSON scalar; non-finite floats and
    over-cap ints/strings become error markers (jsonb rejects ``inf``/
    ``nan``, ``json.dumps`` refuses ints over ~4300 digits, and a
    multi-MB cell is a chunk-text bomb)."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, str):
        return v if len(v) <= _STR_CAP else _ERR_VALUE
    if isinstance(v, int):
        return v if abs(v) <= _INT_CAP else _ERR_NUM
    if isinstance(v, float):
        return v if math.isfinite(v) else _ERR_NUM
    return _ERR_VALUE


def _row_ids(table: dict[str, Any]) -> list[str]:
    return [str(rm.get("id") or i) for i, rm in enumerate(table.get("rowmeta") or [])]


def _numeric_column(
    rows: list[list[Any]], i: int
) -> tuple[list[float], int, int, bool]:
    """(finite numeric cells, non-empty count, skipped count, all-int)
    for column ``i`` — ``count`` footers want non-empty cells
    (``None``/``''`` only); the numeric aggregates run over finite
    numbers, so ``skipped`` covers empty, boolean and non-numeric cells
    alike. An int cell over float range (``10**400``) overflows
    ``float()`` — skipped, not raised. ``all_int`` is True when every
    numeric cell arrived as a Python int, so ``sum`` can stay an int
    (a float cell like ``1.5`` flips the column's sum to ``3.0``)."""
    vals: list[float] = []
    nonempty = 0
    skipped = 0
    all_int = True
    for row in rows:
        v = row[i] if i < len(row) else None
        if v is None or v == "":
            skipped += 1
            continue
        nonempty += 1
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            skipped += 1
            continue
        try:
            f = float(v)
        except OverflowError:
            skipped += 1
            continue
        if math.isfinite(f):
            vals.append(f)
            all_int = all_int and isinstance(v, int)
        else:
            skipped += 1
    return vals, nonempty, skipped, all_int


def _aggregate(
    func: str, vals: list[float], n_nonempty: int, all_int: bool = False
) -> Any:
    if func == "count":
        return n_nonempty
    if not vals:
        return None
    arr = np.asarray(vals, dtype=float)
    # std is the SAMPLE std (ddof=1) — spreadsheet STDEV, not numpy's
    # population default; undefined under two values.
    if func == "std" and len(arr) < 2:
        return None
    if func == "sum":
        s = arr.sum()
        if not math.isfinite(s):
            return None
        # an all-integer column sums to an int, so it displays as one —
        # [1.5, 1.5] shows 3.0, not 3
        return int(s) if all_int and float(s).is_integer() else float(s)
    if func == "mean":
        v = arr.mean()
    elif func == "median":
        v = np.median(arr)
    elif func == "std":
        v = arr.std(ddof=1)
    elif func == "min":
        v = arr.min()
    elif func == "max":
        v = arr.max()
    else:
        raise BadInput(f"unknown footer function {func!r}")
    f = float(v)
    return f if math.isfinite(f) else None


def _name_refs(text: str) -> list[str]:
    """A compiled formula's column-name ids in left-to-right evaluation
    order — the error-propagation scan, where the first error operand
    wins (``#DIV/0 + #REF`` is ``#DIV/0``, like Excel/Sheets)."""
    tree = ast.parse(text, mode="eval")
    out: list[str] = []
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Name):
            out.append(node.id)
        stack.extend(reversed(list(ast.iter_child_nodes(node))))
    return out


def _unresolved_names(compiled: str, columns: list[dict[str, Any]]) -> list[str]:
    """Name tokens in a compiled formula resolving to no column of this
    table — the dropped-column references eval stores as ``#REF``
    (deduped, first-appearance order)."""
    col_ids = {c.get("id") for c in columns}
    out: list[str] = []
    for n in _name_refs(compiled):
        if n not in col_ids and n not in _FORMULA_FUNCTIONS and n not in out:
            out.append(n)
    return out


def evaluate_table(
    table: dict[str, Any], *, declared: set[str] | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Apply a normalised table's formulas and footer, in place, and return
    ``(table, notes)`` — ``notes`` names error cells and footer skips for
    the reply.

    Formulas are compiled to id-form (``columns[].formula`` rewritten),
    then evaluated per row in dependency order; results land in ``rows``.
    ``declared`` is the set of column ids whose formula the caller
    supplied THIS write (a put, ``table=`` or ``add_column=``) — those
    compile strict, so a name that resolves to no column refuses with
    ``options=`` instead of silently landing. Every other formula is a
    stored one being re-evaluated: an unresolved name there is a column
    dropped by a later edit — cells store ``#REF`` and a note names it.
    A formula on a dependency cycle is never evaluated — its cells store
    ``#CYCLE`` and a warning note names every member. An error-marker
    operand propagates its marker unchanged to dependents (first operand
    wins). Footer specs (``footer = {col_id: func}``) are computed into
    ``footer_values = {col_id: {'value', 'skipped'}}``. A table with
    neither returns unchanged with no notes.
    """
    columns = table.get("columns") or []
    rows = table.get("rows") or []
    notes: list[str] = []
    compiled: dict[int, str] = {}
    for i, c in enumerate(columns):
        f = c.get("formula")
        if not f:
            continue
        c["formula"] = compile_formula(
            str(f),
            columns,
            strict=declared is not None and c.get("id") in declared,
            retired={str(i) for i in table.get("retired_ids") or []},
        )
        compiled[i] = c["formula"]
        unresolved = _unresolved_names(c["formula"], columns)
        if unresolved:
            label = c.get("name") or c.get("id") or i
            notes.append(
                f"formula {label} names unknown "
                f"{', '.join(repr(n) for n in unresolved)} — cells store "
                "#REF; next: fix formula= or restore the column"
            )

    if compiled:
        order, cycles = _topo_order(compiled, columns)
        cyclic = {i for scc in cycles for i in scc}
        for scc in cycles:
            scc_names = ", ".join(
                str(columns[k].get("name") or columns[k].get("id") or k) for k in scc
            )
            notes.append(
                f"warning: formula cycle {scc_names} — cells store "
                "#CYCLE; next: drop one side's formula= to break the loop"
            )
        if len(rows) * len(order) > _EVAL_CELL_CAP:
            raise BadInput(
                f"formula evaluation needs {len(rows)}x{len(order)} evals — "
                f"cap is {_EVAL_CELL_CAP}",
                next="reduce the row count or the number of formula columns",
            )
        ev = _TableEval()
        deadline = time.monotonic() + _EVAL_WALL_S
        col_ids = [c.get("id") for c in columns]
        row_ids = _row_ids(table)
        refs = {j: _name_refs(text) for j, text in compiled.items()}
        for r_i, row in enumerate(rows):
            env = {
                cid: (row[j] if j < len(row) else None)
                for j, cid in enumerate(col_ids)
                if cid
            }
            ev.names = env
            for j in cyclic:
                row[j] = _ERR_CYCLE
                ev.names[columns[j]["id"]] = _ERR_CYCLE
            for j in order:
                if time.monotonic() > deadline:
                    raise BadInput(
                        f"formula evaluation exceeded its {_EVAL_WALL_S}s "
                        "wall-clock budget",
                        next="simplify the formulas or split the table",
                    )
                # An error-marker operand propagates its marker unchanged —
                # the first one in evaluation order wins (#REF stays #REF,
                # never degrades to #VALUE).
                dep_err = next(
                    (v for cid in refs[j] if (v := ev.names.get(cid)) in _ERR_MARKERS),
                    None,
                )
                if dep_err is not None:
                    row[j] = dep_err
                else:
                    try:
                        row[j] = _store_scalar(ev.eval(compiled[j]))
                    except Exception as exc:
                        row[j] = _err_marker(exc)
                # A later formula in topo order sees this column's computed
                # value — including an error marker, which its dependents
                # then propagate.
                ev.names[columns[j]["id"]] = row[j]
                if row[j] in _ERR_MARKERS:
                    notes.append(
                        f"{row_ids[r_i] if r_i < len(row_ids) else r_i}"
                        f".{columns[j].get('name') or columns[j].get('id')}"
                        f"={row[j]}"
                    )

    footer = table.get("footer") or {}
    if footer:
        values: dict[str, Any] = {}
        for col_id, func in footer.items():
            ci = next((i for i, c in enumerate(columns) if c.get("id") == col_id), None)
            if ci is None:
                continue
            vals, nonempty, skipped, all_int = _numeric_column(rows, ci)
            values[col_id] = {
                "value": _aggregate(func, vals, nonempty, all_int),
                "skipped": skipped,
            }
            # count's empties ARE the data — only the numeric aggregates
            # earn a "skipped" note.
            if skipped and func != "count":
                name = columns[ci].get("name") or col_id
                notes.append(f"footer {name}: skipped {skipped} non-numeric/empty")
        table["footer_values"] = values
    return table, notes


def table_footer_line(table: dict[str, Any]) -> str | None:
    """The ``footer:`` trailer rendered at read time (same read-time-only
    discipline as the ``ids:`` line — never stored in ``chunks.text``)."""
    spec = table.get("footer") or {}
    values = table.get("footer_values") or {}
    if not spec:
        return None
    names = {c.get("id"): c.get("name") for c in table.get("columns") or []}
    parts = []
    for col_id, func in spec.items():
        name = names.get(col_id) or col_id
        v = values.get(col_id)
        val = v.get("value") if isinstance(v, dict) else v
        parts.append(f"{func}({name})={'' if val is None else val}")
    return "footer: " + "; ".join(parts)


def table_stats(table: dict[str, Any]) -> list[str]:
    """``view='stats'`` body lines — per-column n (numeric) / nonempty /
    empty / mean / sample std / range, computed on read (never stored).
    ``n`` counts the cells the aggregates actually used — a column of
    numbers-as-strings (a recovered LaTeX table) shows ``n=0`` next to
    its ``nonempty``, not a mean over nothing."""
    columns = table.get("columns") or []
    rows = table.get("rows") or []
    lines: list[str] = []
    for i, c in enumerate(columns):
        label = c.get("name") or c.get("id") or str(i)
        cid = c.get("id")
        vals, nonempty, skipped, _all_int = _numeric_column(rows, i)
        empty = len(rows) - nonempty
        head = f"{label} ({cid}): n={len(vals)} nonempty={nonempty} empty={empty}"
        if vals:
            arr = np.asarray(vals, dtype=float)
            # sample std needs 2+ values — a lone cell shows None, not nan
            std = f"{arr.std(ddof=1):g}" if len(arr) > 1 else "None"
            lines.append(
                f"{head} mean={arr.mean():g} std={std} "
                f"range=[{arr.min():g}, {arr.max():g}]"
            )
        else:
            lines.append(f"{head} (no numeric cells)")
    if not lines:
        lines.append("(no columns)")
    return lines

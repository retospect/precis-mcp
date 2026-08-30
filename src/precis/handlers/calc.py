"""Local sympy-backed calculator. Stateless. No DB.

Pass an expression as `id=` (or `q=`); the result is the value. Full
SymPy CAS — calculus, solve, algebra, linear algebra, number theory.
Trig is **degrees by default for numeric arguments** (``sin(30)`` →
``1/2``); a *symbolic* argument (``sin(x)`` inside ``integrate``/``diff``)
stays in sympy-native radians so calculus comes out clean. ``view='rad'``
forces radians everywhere.

A query with an explicit ``to`` / ``in`` / ``->`` clause (``3 ft to m``,
``1 ton to kg``, ``100 degC to degF``) is a **unit conversion** — routed
to ``pint`` (curated, disambiguating unit registry) before sympy ever
sees it, so an agent gets local, offline, unambiguous conversions at no
API cost. Capability catalogue + examples live in the ``precis-calc-help``
skill, not here (handler stays token-light).
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.protocol import Handler, KindSpec
from precis.response import Response


class CalcHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="calc",
        title="Calculator",
        description=(
            "Local symbolic and numeric computation via sympy: arithmetic, "
            "roots, trigonometry (sin/cos/tan/atan2, pi), calculus, linear "
            "algebra. Pass an expression as `id` (or `q`); the result is the "
            "value. Numeric angles are degrees by default (sin(30)=1/2); "
            "symbolic args (sin(x) in a calculus op) stay in radians so "
            "integrate/diff come out clean. Pass view='rad' to force radians. "
            "A `to`/`in`/`->` clause is a local unit conversion via pint "
            "(3 ft to m; 1 ton to kg; 100 degC to degF) — offline, exact, "
            "disambiguating (metric_ton vs long_ton, US vs imperial_gallon)."
        ),
        supports_get=True,
        is_numeric=False,
        id_required=True,
        placement="system",
    )

    def __init__(self, *, hub: Hub) -> None:
        # ``sympy`` is an optional [calc] / [all] extra. Import here
        # so a bare ``pip install precis-mcp`` surface a clean
        # missing-dep at boot (dispatch._try catches ImportError and
        # drops the calc kind), rather than failing at module import
        # and taking the whole precis.handlers package down with it.
        import sympy

        # Calc is stateless — no store, no embedder, no hint usage
        # at __init__ time. ``hub`` is taken for signature uniformity
        # across every handler, and planted on ``self.hub`` by
        # :meth:`Handler._register_with` right after construction in
        # case future features want to emit hints from here.
        _ = hub
        self._sympy = sympy

    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        q: str | None = None,
        **_kw: Any,
    ) -> Response:
        sympy = self._sympy
        expr_str = self._coerce_expr(id, q)
        # A `to` / `in` / `->` clause means a unit conversion — hand it to
        # pint before sympy ever sees it (sympy would parse "3 ft to m" as
        # gibberish free symbols). Returns None when the query isn't a
        # conversion, so ordinary math falls straight through unchanged.
        unit_resp = _try_unit_conversion(expr_str)
        if unit_resp is not None:
            return unit_resp
        # Degrees is the **default** — this is an engineering-leaning
        # calculator (bolt circles, draft angles, cad poses are all in
        # degrees). ``view='rad'`` opts back into sympy's native radians
        # for symbolic calculus etc. In degrees mode we shadow the trig
        # builtins inside ``sympify`` so ``sin(30)`` reads its argument
        # as degrees (``sin(rad(30))`` → ``1/2``) and ``atan2(1,1)``
        # returns degrees (``deg(...)`` → 45). The shared ``used`` flag
        # flips the first time any wrapped trig fn is actually applied,
        # so we only stamp the "interpreted in degrees" note when trig
        # really ran.
        degrees = not _wants_radians(view)
        used = {"trig": False}
        local_dict = _degrees_locals(sympy, used) if degrees else None
        try:
            expr = sympy.sympify(expr_str, locals=local_dict)
        except (sympy.SympifyError, SyntaxError, TypeError) as e:
            # Hint uses ``q=`` to match the canonical example in
            # precis-overview / precis-help. The handler accepts
            # ``id=`` too, but teaching ``id=`` here trains agents to
            # mix kwargs across tool-kinds and trip over the q= vs
            # id= split elsewhere. (MCP critic MINOR — calc recovery
            # hint uses id= while canonical example uses q=.)
            if _is_unit_ish(expr_str):
                raise BadInput(
                    f"could not parse expression: {expr_str!r}. That looks "
                    "like a unit conversion — calc converts units when you "
                    "give it a 'to' clause.",
                    next="get(kind='calc', q='3 ft to m')",
                ) from e
            raise BadInput(
                f"could not parse expression: {expr_str!r}",
                next="get(kind='calc', q='2+3*4')",
            ) from e

        # Sympy silently promotes unknown function names
        # (``randint(1,6)``, ``random()``, ``foo(x)``) to
        # :class:`AppliedUndef` — applied undefined functions. They
        # then round-trip through ``.doit()`` / ``simplify()``
        # unchanged, and the existing "simplifies to itself" guard
        # doesn't fire because the expression carries no free
        # symbols (``Function('randint')`` isn't a symbol).  The
        # result used to be ``randint(1, 6) = randint(1, 6)`` — a
        # silent echo that small-model callers read as success.
        # Refuse the call instead, and name the offending functions
        # so the caller can pick a real sympy op. (MCP critic
        # MINOR-C — calc silently echoes unknown functions.)
        from sympy.core.function import AppliedUndef

        if hasattr(expr, "atoms"):
            undef = expr.atoms(AppliedUndef)
            if undef:
                names = sorted({f.func.__name__ for f in undef})
                names_str = ", ".join(repr(n) for n in names)
                raise BadInput(
                    f"unknown function(s) in expression: {names_str}. "
                    "calc is sympy-backed; common builtins are "
                    "integrate, diff, solve, simplify, factor, expand, "
                    "limit, Sum, Product. Python builtins like "
                    "randint() or random() are not wired - see "
                    "get(kind='skill', id='precis-oracle-help') for "
                    "randomness workflows.",
                    next="get(kind='calc', q='solve(Eq(x+1, 3), x)')",
                )

        # Some sympy functions — notably ``solve`` and ``factor_list``
        # — run eagerly inside ``sympify`` and return plain Python
        # containers (list / tuple / dict) rather than sympy objects.
        # The rest of the pipeline (``.is_number``, ``.doit()``,
        # ``.free_symbols``, ``simplify``) assumes a sympy Basic, so
        # without this short-circuit ``solve(Eq(x+1, 3), x)``
        # AttributeErrored with the cryptic ``'list' object has no
        # attribute 'is_number'`` that the next clause then masked as
        # "unsupported expression". sympy's own container kinds
        # (``FiniteSet``, ``ImmutableMatrix``, ``Tuple``) are Basic
        # subclasses and keep the fast path. (MCP critic round 2 —
        # calc solve unwired.)
        if isinstance(expr, (list, tuple, dict, set, frozenset)):
            return Response(
                body=f"{expr_str} = {_humanise(expr)}" + _degrees_note(degrees, used)
            )

        try:
            result = expr if expr.is_number else expr.doit()
        except (AttributeError, TypeError, ValueError, sympy.SympifyError) as e:
            # Sanitize the upstream error message — sympy's
            # ``AttributeError`` on ``__import__('os').system(...)``
            # bubbles up as ``'int' object has no attribute
            # 'is_number'``, which a 7B caller misreads as advice
            # about its own input (the MCP critic's MINOR finding).
            # Keep the full traceback in error.data via ``from e``
            # for debugging, but the agent-facing message is short
            # and structural. (Critic MINOR #9.)
            #
            # ``cause`` carries the scope disambiguation ("calc does
            # math, not I/O"); ``next`` is a single copy-pasteable
            # call that works — consistent with the envelope
            # contract in precis/errors.py (``next`` = "one
            # copy-pasteable next action"). Earlier revisions stuffed
            # a prose list of operator names into ``next``, which
            # broke the copy-paste affordance. (c4 cleanup.)
            raise BadInput(
                f"could not evaluate {expr_str!r} - unsupported expression. "
                "calc handles arithmetic, calculus, simplify, solve, and "
                "similar symbolic math; for Python builtins or I/O use a "
                "different tool.",
                next="get(kind='calc', q='integrate(sin(x), x)')",
            ) from e

        # The MCP critic flagged ``calc`` cheerfully echoing
        # ``malformed**broken = malformed**broken`` — sympy parses
        # arbitrary identifiers as free symbols, so an English
        # snippet like ``one plus two`` (or a typo'd op name) round-
        # trips through .doit() unchanged with no evaluation
        # actually happening. When the result is identical to the
        # input *and* contains free symbols rather than numeric
        # primitives, that's almost certainly the user mis-typing
        # rather than a deliberate symbolic expression.
        # (Critic MINOR m4.)
        try:
            simplified = sympy.simplify(result) if not result.is_number else result
        except Exception:
            simplified = result
        # ``getattr(..., set())`` would be the natural form here but
        # mypy's overload selection latches onto sympy's typed
        # ``free_symbols`` attribute and flags the default. Use the
        # ``hasattr`` + access pattern instead — same semantics.
        free_symbols = (
            simplified.free_symbols if hasattr(simplified, "free_symbols") else set()
        )
        if (
            str(simplified).replace(" ", "") == expr_str.replace(" ", "")
            and free_symbols
        ):
            # See the comment above the unsupported-expression raise
            # for the cause/next split rationale. Here ``next`` picks
            # ``solve(Eq(...))`` because it's the concrete shape the
            # cause text recommends (giving sympy "more structure").
            # (c4 cleanup.)
            if _is_unit_ish(expr_str):
                # A bare unit word (``hogshead``) or ``5 miles`` sympifies
                # to a lone/undecorated symbol — point at conversion, not
                # solve(Eq(...)).
                raise BadInput(
                    f"{expr_str!r} names a unit but isn't a conversion. "
                    "calc converts units when you give it a 'to' clause.",
                    next="get(kind='calc', q='3 ft to m')",
                )
            raise BadInput(
                f"expression simplifies to itself: {expr_str!r}. "
                "calc evaluates expressions with operators; for bare "
                "symbolic identities give sympy more structure - wrap "
                "in solve(Eq(lhs, rhs), var) or similar.",
                next="get(kind='calc', q='solve(Eq(x+1, 3), x)')",
            )

        # A result that still carries free symbols named after units
        # (``3 feet`` → ``3*feet``) is a silent-garbage echo: the agent
        # meant a quantity, not a symbolic product. Append the conversion
        # nudge rather than let the echo read as success.
        unit_note = ""
        if free_symbols and _is_unit_ish(expr_str):
            unit_note = (
                "\n(names a unit — for a conversion use 'to', e.g. q='3 ft to m')"
            )
        return Response(
            body=f"{expr_str} = {_humanise(result)}"
            + _degrees_note(degrees, used)
            + unit_note
        )

    @staticmethod
    def _coerce_expr(id: str | int | None, q: str | None) -> str:
        if isinstance(id, str) and id:
            return id
        if isinstance(id, int):
            return str(id)
        if isinstance(q, str) and q:
            return q
        raise BadInput(
            "calc requires an expression as `q` (or `id`)",
            next="get(kind='calc', q='2+3*4')",
        )


# Sympy's special constants render with cryptic names (``zoo``, ``oo``,
# ``nan``) that 7B callers misread as typos. Translate the trio into
# plain English in the response so the meaning is unambiguous. (MCP
# critic MINOR — calc 1/0 returns ``zoo`` with no explanation.)
_SYMPY_HUMAN_NAMES: dict[str, str] = {
    "zoo": "complex infinity (e.g. division by zero)",
    "oo": "+infinity",
    "-oo": "-infinity",
    "nan": "undefined (NaN)",
}


def _humanise(result: Any) -> str:
    """Render a sympy result, replacing opaque constants with English."""
    rendered = str(result)
    return _SYMPY_HUMAN_NAMES.get(rendered, rendered)


# ── unit conversion (pint) ─────────────────────────────────────────
#
# calc's second job: local, offline, unambiguous unit conversion. A
# query carrying an explicit ``to`` / ``in`` / ``->`` clause is routed
# here *before* sympy — sympy would read ``3 ft to m`` as the product of
# free symbols and cheerfully echo it. pint is chosen over
# sympy.physics.units for the curated, disambiguating registry: it keeps
# ``ton`` (US short) distinct from ``metric_ton``/``long_ton``, US
# ``gallon`` from ``imperial_gallon``, mass ``oz`` from ``fluid_ounce``,
# and *raises* on an unknown unit rather than silently inventing a
# symbol — which is exactly the "unambiguous" property we want.


# ``->`` is unambiguous; for the word separators we take the RIGHTMOST
# ``to``/``in`` (greedy ``.+``) so ``3 ft + 2 in to cm`` splits at
# `` to ``, leaving the inch in the source expression intact. ``to`` is
# tried before ``in`` because ``in`` collides with the inch unit.
_TO_RE = re.compile(r"^(?P<src>.+)\s+to\s+(?P<dst>\S.*)$", re.IGNORECASE)
_IN_RE = re.compile(r"^(?P<src>.+)\s+in\s+(?P<dst>\S.*)$", re.IGNORECASE)

# Split ``"100 degC"`` → magnitude ``100`` + unit ``degC`` for the offset-
# unit (temperature) path, where the magnitude and unit must be handed to
# pint's Quantity() separately (see _parse_quantity).
_MAGNITUDE_UNIT_RE = re.compile(
    r"^\s*(?P<mag>[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s+(?P<unit>.+?)\s*$"
)

# Unit tokens for the "you named a unit but didn't ask to convert it"
# near-miss nudge (``3 feet``, ``hogshead``). Only ≥2-char, low-collision
# abbreviations + full names — bare ``m``/``g``/``l``/``in`` are omitted
# because they clash with ordinary symbolic variables and the English word
# "in". A number glued to one of these (``\d\s*<unit>``) is the strong
# signal; a few unambiguous nouns (``hogshead``, ``gallon``) also fire on
# their own. See _is_unit_ish — this only ever runs on already-degenerate
# input (parse-fail / bare-symbol echo), never on valid math.
_UNIT_TOKENS = (
    "cm|mm|km|nm|um|ft|yd|inch|inches|foot|feet|meter|meters|metre|metres|"
    "kilometer|kilometers|mile|miles|yard|yards|micron|microns|furlong|furlongs|"
    "kg|mg|lb|lbs|oz|gram|grams|kilogram|kilograms|pound|pounds|ounce|ounces|"
    "ton|tons|tonne|tonnes|stone|"
    "ml|cl|dl|gal|liter|liters|litre|litres|gallon|gallons|pint|pints|"
    "quart|quarts|cup|cups|hogshead|hogsheads|barrel|barrels|acre|acres|"
    "celsius|fahrenheit|kelvin|degC|degF|degK|"
    "joule|joules|calorie|calories|cal|kwh|watt|watts|psi|bar|atm|mph|kph|knot|knots"
)
_NUM_UNIT_RE = re.compile(rf"\d\s*(?:{_UNIT_TOKENS})\b", re.IGNORECASE)
_STANDALONE_UNIT_RE = re.compile(
    r"\b(?:hogshead|gallon|fahrenheit|celsius|kelvin|ounce|furlong|acre|"
    r"tonne|litre|liter|kilometer|kilogram|fluid_ounce)s?\b",
    re.IGNORECASE,
)

# A single UnitRegistry is expensive to build (parses the full
# definitions file) and safe to share — it's an immutable lookup table
# once constructed. Cache it for the life of the process.
_UREG: Any = None


def _split_conversion(expr_str: str) -> tuple[str, str] | None:
    """Split ``"3 ft to m"`` → ``("3 ft", "m")`` when the query is a
    conversion, else ``None`` so ordinary math falls through to sympy."""
    if "->" in expr_str:
        src, _, dst = expr_str.partition("->")
        src, dst = src.strip(), dst.strip()
        return (src, dst) if src and dst else None
    for pattern in (_TO_RE, _IN_RE):
        m = pattern.match(expr_str)
        if m:
            src, dst = m.group("src").strip(), m.group("dst").strip()
            if src and dst:
                return src, dst
    return None


def _is_unit_ish(expr_str: str) -> bool:
    """True when the expression names a physical unit but *isn't* a
    conversion — the ``3 feet`` / ``hogshead`` near-miss where an agent
    reached for calc with a unit in hand but didn't use a ``to`` clause.

    Returns False for a real conversion (already routed to pint) so we
    never double-nudge. Callers gate this behind an already-degenerate
    result (parse failure or a bare-symbol echo), so it never fires on
    valid math — ``3 kg`` there is far more likely a unit than a stray
    ``kg`` symbol worth echoing.
    """
    if _split_conversion(expr_str) is not None:
        return False
    return bool(_NUM_UNIT_RE.search(expr_str) or _STANDALONE_UNIT_RE.search(expr_str))


def _unit_registry(pint: Any) -> Any:
    global _UREG
    if _UREG is None:
        _UREG = pint.UnitRegistry()
    return _UREG


def _parse_quantity(pint: Any, ureg: Any, text: str) -> Any:
    """Parse the source side into a pint Quantity.

    ``parse_expression`` handles unit arithmetic (``3 ft + 2 in``) but
    rejects offset units (temperature) with ``OffsetUnitCalculusError``:
    ``100 degC`` parses as ``100 * degC`` and multiplying a scalar by an
    offset unit is ambiguous. The string form ``Quantity("100 degC")``
    hits the same wall — the *only* form that works is magnitude and unit
    passed **separately** (``Quantity(100.0, "degC")``), which reads
    ``100 degC`` as a point on the scale, not a multiplication. So on the
    offset error we split the leading numeric magnitude from the unit and
    rebuild that way.

    A bare unit with no magnitude (``ft`` in ``ft to m``) can come back
    as a :class:`pint.Unit` rather than a Quantity on some pint versions;
    normalise it to ``1 <unit>`` so the caller always gets a Quantity
    with ``.magnitude`` / ``.to``.
    """
    try:
        parsed = ureg.parse_expression(text)
    except pint.OffsetUnitCalculusError:
        m = _MAGNITUDE_UNIT_RE.match(text)
        if m is None:
            raise
        parsed = ureg.Quantity(float(m.group("mag")), m.group("unit"))
    if isinstance(parsed, pint.Unit):
        return ureg.Quantity(1, parsed)
    return parsed


def _format_quantity(qty: Any) -> str:
    """Compact, low-token rendering: magnitude at 6 significant figures +
    the abbreviated unit symbol — ``0.9144 m``, ``96.52 cm``,
    ``907.185 kg``."""
    return f"{qty.magnitude:.6g} {qty.units:~}"


def _try_unit_conversion(expr_str: str) -> Response | None:
    """Return a converted-units :class:`Response`, or ``None`` when the
    query isn't a conversion (so ``get`` proceeds to sympy)."""
    split = _split_conversion(expr_str)
    if split is None:
        return None
    src, dst = split
    try:
        import pint
    except ImportError as e:
        # Detected a conversion but the optional dep is absent — say so
        # concretely rather than letting sympy mangle it downstream.
        raise BadInput(
            "unit conversion needs the optional 'pint' dependency; "
            "install precis-mcp[calc].",
            next="get(kind='calc', q='2+3*4')",
        ) from e

    ureg = _unit_registry(pint)
    try:
        converted = _parse_quantity(pint, ureg, src).to(dst)
    except pint.UndefinedUnitError as e:
        raise BadInput(
            f"unknown unit in {expr_str!r}: {e}. Use a full, explicit unit "
            "name to disambiguate — metric_ton / long_ton, imperial_gallon, "
            "fluid_ounce.",
            next="get(kind='calc', q='1 ton to kg')",
        ) from e
    except pint.DimensionalityError as e:
        raise BadInput(
            f"incompatible units in {expr_str!r}: {e}",
            next="get(kind='calc', q='3 ft to m')",
        ) from e
    except (pint.PintError, ValueError, TypeError, AssertionError) as e:
        # Everything else pint can throw (bad offset arithmetic, parse
        # gaps) — keep the agent-facing message short + structural.
        raise BadInput(
            f"could not convert {expr_str!r}: {e}",
            next="get(kind='calc', q='3 ft to m')",
        ) from e

    return Response(body=f"{src} = {_format_quantity(converted)}")


def _wants_radians(view: str | None) -> bool:
    """``view='rad'`` / ``'radian'`` / ``'radians'`` opts out of the
    degrees default and back into sympy's native radians."""
    return isinstance(view, str) and view.strip().lower() in (
        "rad",
        "radian",
        "radians",
    )


def _degrees_note(degrees: bool, used: dict[str, bool]) -> str:
    """One-line footer stamped when trig actually ran in degrees mode,
    so the reader knows the convention and how to switch."""
    if degrees and used["trig"]:
        return "\n(trig evaluated in degrees — pass view='rad' for radians)"
    return ""


def _degrees_locals(sympy: Any, used: dict[str, bool]) -> dict[str, Any]:
    """Trig builtins that read/return **degrees** instead of radians —
    but only for **numeric** arguments.

    Forward functions interpret their argument as degrees (wrap in
    ``rad``); inverse functions return degrees (wrap in ``deg``). Sympy
    keeps these exact — ``sin(30)`` → ``1/2``, ``tan(45)`` → ``1`` — and
    ``N(...)`` still works for a decimal.

    The degrees convention applies **only when the argument carries no
    free symbols** (``sin(30)``, ``atan2(1, 1)``). A *symbolic* argument
    — ``sin(x)`` inside ``integrate(sin(x)**2, x)`` — falls through to
    sympy's native radians untouched, because substituting ``x → rad(x)``
    into an indefinite integral over ``x`` corrupts the calculus (it
    yields a garbled ``pi*x/180`` antiderivative instead of the correct
    one). This is the "degrees for engineering numerics, radians for
    symbolic calculus" split (gr48509) — the old code applied ``rad`` to
    every argument, so ``integrate(sin(x)**2, x)`` in the default mode
    returned nonsense until you remembered ``view='rad'``.

    ``used['trig']`` flips only when the degrees conversion actually
    fires (a numeric argument), so the "interpreted in degrees" note is
    stamped only when it's true; ``pi``, ``sqrt``, calculus etc. fall
    through to sympy untouched.
    """
    rad, deg = sympy.rad, sympy.deg

    def _is_symbolic(a: Any) -> bool:
        arg = sympy.sympify(a)
        return bool(getattr(arg, "free_symbols", set()))

    def fwd(fn: Any) -> Any:  # arg-in-degrees (numeric args only)
        def f(a: Any) -> Any:
            if _is_symbolic(a):
                return fn(a)  # symbolic → sympy-native radians
            used["trig"] = True
            return fn(rad(a))

        return f

    def inv(fn: Any) -> Any:  # result-in-degrees (numeric args only)
        def f(a: Any) -> Any:
            if _is_symbolic(a):
                return fn(a)
            used["trig"] = True
            return deg(fn(a))

        return f

    out: dict[str, Any] = {}
    for name in ("sin", "cos", "tan", "sec", "csc", "cot"):
        out[name] = fwd(getattr(sympy, name))
    for name in ("asin", "acos", "atan", "acot", "asec", "acsc"):
        out[name] = inv(getattr(sympy, name))

    def _atan2(y: Any, x: Any) -> Any:
        if _is_symbolic(y) or _is_symbolic(x):
            return sympy.atan2(y, x)
        used["trig"] = True
        return deg(sympy.atan2(y, x))

    out["atan2"] = _atan2
    return out

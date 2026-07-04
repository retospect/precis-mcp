"""Local sympy-backed calculator. Stateless. No DB.

Pass an expression as `id=` (or `q=`); the result is the value. Full
SymPy CAS — calculus, solve, algebra, linear algebra, number theory.
Trig is **degrees by default for numeric arguments** (``sin(30)`` →
``1/2``); a *symbolic* argument (``sin(x)`` inside ``integrate``/``diff``)
stays in sympy-native radians so calculus comes out clean. ``view='rad'``
forces radians everywhere. Capability catalogue + examples live in the
``precis-calc-help`` skill, not here (handler stays token-light).
"""

from __future__ import annotations

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
            "integrate/diff come out clean. Pass view='rad' to force radians."
        ),
        supports_get=True,
        is_numeric=False,
        id_required=True,
        role="system",
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

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        q: str | None = None,
        **_kw: Any,
    ) -> Response:
        sympy = self._sympy
        expr_str = self._coerce_expr(id, q)
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
            raise BadInput(
                f"expression simplifies to itself: {expr_str!r}. "
                "calc evaluates expressions with operators; for bare "
                "symbolic identities give sympy more structure - wrap "
                "in solve(Eq(lhs, rhs), var) or similar.",
                next="get(kind='calc', q='solve(Eq(x+1, 3), x)')",
            )

        return Response(
            body=f"{expr_str} = {_humanise(result)}" + _degrees_note(degrees, used)
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

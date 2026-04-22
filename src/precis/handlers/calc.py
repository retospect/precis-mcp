"""CalcHandler — local calculator backed by SymPy (Phase 5).

Stateless, read-only, free.  Evaluates math expressions with support for
arithmetic, roots, trigonometry, exact rationals, symbolic manipulation,
calculus (``integrate`` / ``diff`` / ``limit``), linear algebra
(``Matrix``), equation solving (``solve``), and unit conversion (via
``sympy.physics.units``).  Also accepts Python-style base literals
(``0xff``, ``0b1010``, ``0o17``) and base-conversion builtins (``hex``,
``bin``, ``oct``, ``int``).

Gating:

- ``sympy`` must be importable (part of the ``[calc]`` extra).  Registry
  registration is skipped at startup via the usual ``ImportError`` catch
  in ``_register_builtins``.
- No env vars — the kind is always available when installed.
- Declares ``cost_hint="free"`` so the response footer and the per-kind
  description both advertise this as a zero-cost local tool, in contrast
  to the paid ``math:`` (Wolfram) and ``web:`` / ``think:`` / ``research:``
  (Perplexity) kinds.

Slash management:

The URI parser treats ``calc`` as an opaque-path scheme (see
``precis.uri._OPAQUE_PATH_SCHEMES``) because math expressions routinely
contain ``/`` as division — ``calc:1/2`` must mean ``Rational(1, 2)``,
not ``path=1, view=2``.  This handler then parses its own *trailing*
``/view`` suffix (one of ``/help``, ``/pretty``, ``/latex``, ``/numeric``)
so agents can still request alternate output formats without escaping
division.

Dispatch:

- ``read(path=<expr>)`` evaluates the expression and returns a
  formatted markdown block (input, exact, numeric, optional bases,
  optional pretty).
- ``read(path='/help')`` returns the onboarding skill inline.
- ``read(path=<expr>/pretty)`` returns the Unicode pretty-print.
- ``read(path=<expr>/latex)`` returns LaTeX.
- ``read(path=<expr>/numeric)`` returns only the decimal approximation.

Safety:

SymPy's ``parse_expr`` evaluates expressions via ``eval`` under the hood,
which is not a sandbox by itself.  We apply three layers of defense:

1. AST walk of the raw input rejects dunder attribute/name access
   (``.__class__``, ``__import__``), ``lambda``, comprehensions, and
   ``yield`` / ``await`` (see :func:`_sanitize`).
2. ``global_dict`` passed to ``parse_expr`` sets ``__builtins__={}`` so
   even if an attack slips past the AST check, ``__import__`` /
   ``open`` / ``eval`` are not resolvable.
3. ``local_dict`` is a fixed whitelist of SymPy callables + a small set
   of base-conversion builtins (``hex``, ``bin``, ``oct``, ``int``).

This is defence-in-depth, not a formally-verified sandbox.  The
calculator is intended for trusted local use by the agent, not as a
service exposed to hostile input.
"""

from __future__ import annotations

import ast
import logging
import re
from functools import lru_cache
from typing import Any, ClassVar

from precis.protocol import ErrorCode, Handler, PrecisError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_KNOWN_VIEWS = frozenset({"help", "pretty", "latex", "numeric"})

# Hex / binary / octal integer literals (Python style).  Used to detect
# when the user cares about multi-base output.
_BASE_LITERAL_RE = re.compile(r"\b0[xX][0-9a-fA-F]+\b|\b0[bB][01]+\b|\b0[oO][0-7]+\b")
_BASE_CALL_RE = re.compile(r"\b(hex|bin|oct|int)\s*\(")

# Single underscore is sometimes used as a throwaway; we block anything
# starting with ``_`` but allow the bare ``_`` if it ever shows up as a
# symbol (it won't resolve to anything useful given our locked namespace).
_ALLOWED_UNDERSCORE_NAMES = frozenset({"_"})

_FOOTER = "---\n_Computed locally by SymPy {version} — no network, no cost._"


# ---------------------------------------------------------------------------
# Safety: AST sanitisation
# ---------------------------------------------------------------------------


_BLOCKED_NODES: tuple[type[ast.AST], ...] = (
    ast.Lambda,
    ast.GeneratorExp,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.Yield,
    ast.YieldFrom,
    ast.Await,
    ast.FormattedValue,
    ast.JoinedStr,
    ast.NamedExpr,  # walrus :=
    ast.Starred,
)


def _sanitize(expr_str: str) -> None:
    """Pre-parse AST check — reject dangerous constructs.

    Raises :class:`PrecisError` (``PARAM_INVALID``) when the input
    contains a dunder attribute access, a blocked name, or a blocked
    Python construct.  Pure parse failures are left to ``parse_expr``
    so the user sees SymPy's (better) syntax error messages.
    """
    try:
        tree = ast.parse(expr_str, mode="eval")
    except SyntaxError:
        return  # defer to parse_expr's error reporting

    for node in ast.walk(tree):
        if isinstance(node, _BLOCKED_NODES):
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                f"calc: {type(node).__name__} is not allowed in expressions",
            )
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                f"calc: dunder/private attribute access is blocked "
                f"(.{node.attr})",
            )
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            if node.id not in _ALLOWED_UNDERSCORE_NAMES:
                raise PrecisError(
                    ErrorCode.PARAM_INVALID,
                    f"calc: names starting with underscore are blocked "
                    f"({node.id})",
                )


# ---------------------------------------------------------------------------
# Namespace construction (lazy — SymPy import is optional)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _build_namespace() -> tuple[dict[str, Any], dict[str, Any], tuple, str]:
    """Build (local_dict, global_dict, transformations, sympy_version).

    Memoised so the namespace is built exactly once per process.  Raises
    ``ImportError`` when ``sympy`` is not installed; callers are
    responsible for catching and returning a ``KIND_UNAVAILABLE`` error.
    """
    import sympy
    from sympy.parsing.sympy_parser import (
        convert_xor,
        implicit_multiplication_application,
        parse_expr,  # noqa: F401 — imported here so it's verified
        rationalize,
        standard_transformations,
    )
    from sympy.physics import units as U

    transformations = standard_transformations + (
        convert_xor,
        implicit_multiplication_application,
        rationalize,
    )

    # ``global_dict`` must contain the SymPy primitives emitted by the
    # transformations (Integer, Symbol, Function, Rational, Float) — if
    # these are missing, evaluation fails with NameError.  Setting
    # ``__builtins__`` to an empty dict means names like ``__import__``,
    # ``open``, ``eval`` aren't resolvable even if they slip past the
    # AST check.
    global_dict: dict[str, Any] = {
        "Integer": sympy.Integer,
        "Float": sympy.Float,
        "Symbol": sympy.Symbol,
        "Function": sympy.Function,
        "Rational": sympy.Rational,
        "__builtins__": {},
    }

    # ``local_dict`` — the user-visible namespace.  Everything accessible
    # inside a ``calc:`` expression must be registered here.
    local_dict: dict[str, Any] = {
        # constants
        "pi": sympy.pi,
        "E": sympy.E,
        "I": sympy.I,
        "oo": sympy.oo,
        "zoo": sympy.zoo,
        "inf": sympy.oo,
        "infinity": sympy.oo,
        "nan": sympy.nan,
        "GoldenRatio": sympy.GoldenRatio,
        "EulerGamma": sympy.EulerGamma,
        "Catalan": sympy.Catalan,
        # elementary functions
        "sin": sympy.sin,
        "cos": sympy.cos,
        "tan": sympy.tan,
        "cot": sympy.cot,
        "sec": sympy.sec,
        "csc": sympy.csc,
        "asin": sympy.asin,
        "acos": sympy.acos,
        "atan": sympy.atan,
        "atan2": sympy.atan2,
        "sinh": sympy.sinh,
        "cosh": sympy.cosh,
        "tanh": sympy.tanh,
        "asinh": sympy.asinh,
        "acosh": sympy.acosh,
        "atanh": sympy.atanh,
        "exp": sympy.exp,
        "log": sympy.log,
        "ln": sympy.log,
        "sqrt": sympy.sqrt,
        "cbrt": sympy.cbrt,
        "root": sympy.root,
        "Abs": sympy.Abs,
        "abs": sympy.Abs,
        "sign": sympy.sign,
        "factorial": sympy.factorial,
        "gamma": sympy.gamma,
        "beta": sympy.beta,
        "gcd": sympy.gcd,
        "lcm": sympy.lcm,
        "floor": sympy.floor,
        "ceiling": sympy.ceiling,
        "ceil": sympy.ceiling,
        "Mod": sympy.Mod,
        "mod": sympy.Mod,
        "re": sympy.re,
        "im": sympy.im,
        "conjugate": sympy.conjugate,
        "arg": sympy.arg,
        "binomial": sympy.binomial,
        # numeric wrappers
        "Rational": sympy.Rational,
        "Integer": sympy.Integer,
        "Float": sympy.Float,
        "N": sympy.N,
        "nsimplify": sympy.nsimplify,
        # calculus
        "integrate": sympy.integrate,
        "Integral": sympy.Integral,
        "diff": sympy.diff,
        "Derivative": sympy.Derivative,
        "limit": sympy.limit,
        "Limit": sympy.Limit,
        "series": sympy.series,
        "Sum": sympy.Sum,
        "summation": sympy.summation,
        "Product": sympy.Product,
        "product": sympy.product,
        # solvers
        "solve": sympy.solve,
        "roots": sympy.roots,
        "nroots": sympy.nroots,
        "solveset": sympy.solveset,
        "linsolve": sympy.linsolve,
        "nonlinsolve": sympy.nonlinsolve,
        # simplification
        "simplify": sympy.simplify,
        "expand": sympy.expand,
        "factor": sympy.factor,
        "collect": sympy.collect,
        "cancel": sympy.cancel,
        "apart": sympy.apart,
        "together": sympy.together,
        "trigsimp": sympy.trigsimp,
        "powsimp": sympy.powsimp,
        "radsimp": sympy.radsimp,
        "ratsimp": sympy.ratsimp,
        "sqrtdenest": sympy.sqrtdenest,
        # linear algebra
        "Matrix": sympy.Matrix,
        "eye": sympy.eye,
        "zeros": sympy.zeros,
        "ones": sympy.ones,
        "diag": sympy.diag,
        "Transpose": sympy.Transpose,
        # base conversion builtins
        "hex": hex,
        "bin": bin,
        "oct": oct,
        "int": int,
        "float": float,
        # pre-allocated symbols so ``2x + 3`` works without declaration
        "x": sympy.Symbol("x"),
        "y": sympy.Symbol("y"),
        "z": sympy.Symbol("z"),
        "t": sympy.Symbol("t"),
        "n": sympy.Symbol("n"),
        "k": sympy.Symbol("k"),
        "a": sympy.Symbol("a"),
        "b": sympy.Symbol("b"),
        "r": sympy.Symbol("r"),
        "theta": sympy.Symbol("theta"),
        "phi": sympy.Symbol("phi"),
        "alpha": sympy.Symbol("alpha"),
        "beta_sym": sympy.Symbol("beta_sym"),
        "omega": sympy.Symbol("omega"),
        # Symbol constructor — allow users to declare their own
        "Symbol": sympy.Symbol,
        "symbols": sympy.symbols,
    }

    # Units — only multi-letter aliases & full names to avoid collision
    # with single-letter symbols (e.g. ``m`` / ``c`` / ``I``).
    _UNIT_NAMES = ["meter", "kilometer", "centimeter", "millimeter", "micrometer", "inch", "foot", "feet", "yard", "mile", "kilogram", "gram", "milligram", "microgram", "pound", "tonne", "amu", "dalton", "second", "millisecond", "microsecond", "minute", "hour", "day", "year", "joule", "watt", "newton", "pascal", "bar", "atmosphere", "kelvin", "mole", "ampere", "candela", "hertz", "volt", "ohm", "coulomb", "farad", "henry", "tesla", "weber", "liter", "milliliter", "electronvolt", "electronvolts", "speed_of_light", "gravitational_constant", "planck", "hbar", "avogadro", "elementary_charge", "degree", "radian", "psi"]
    for name in _UNIT_NAMES:
        unit = getattr(U, name, None)
        if unit is not None:
            local_dict[name] = unit
    local_dict["convert_to"] = U.convert_to

    return local_dict, global_dict, transformations, sympy.__version__


# ---------------------------------------------------------------------------
# Path parsing — split trailing /view from the opaque-path expression
# ---------------------------------------------------------------------------


def _parse_path(path: str) -> tuple[str, str | None]:
    """Split a trailing ``/view`` suffix from the expression.

    Only triggers when the trailing token is a *known* view name
    (``help``, ``pretty``, ``latex``, ``numeric``).  Any other ``/`` is
    treated as division and left in the expression.

    Examples::

        >>> _parse_path("1/2")
        ('1/2', None)
        >>> _parse_path("1/2/pretty")
        ('1/2', 'pretty')
        >>> _parse_path("/help")
        ('', 'help')
        >>> _parse_path("integrate(sin(x)*cos(x), x)/latex")
        ('integrate(sin(x)*cos(x), x)', 'latex')
    """
    if "/" not in path:
        return path, None
    last_slash = path.rfind("/")
    candidate = path[last_slash + 1 :]
    if candidate in _KNOWN_VIEWS:
        return path[:last_slash], candidate
    return path, None


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------


def _attribution(version: str) -> str:
    """Local-computation attribution footer.

    Not legally required (no third-party data) but useful for debugging:
    the agent knows which engine produced the answer and which version,
    which matters when SymPy behaviour changes across releases.
    """
    return _FOOTER.format(version=version)


def _is_integer_result(result: Any) -> bool:
    """Return True if ``result`` is a concrete integer value.

    Accepts both plain Python ``int`` (produced by the whitelisted
    ``int()`` builtin — e.g. ``int('ff', 16)`` → ``255``) and SymPy
    ``Integer`` instances.  Plain Python ``bool`` is a subclass of
    ``int`` but we still include it since ``True``/``False`` round-trip
    as ``1``/``0`` in the base formatters.
    """
    if isinstance(result, int):
        return True
    try:
        return bool(result.is_Integer)
    except AttributeError:
        return False


def _format_bases(result: Any) -> list[str]:
    """Return hex/bin/oct lines for an integer result."""
    try:
        n = int(result)
    except (TypeError, ValueError):
        return []
    return [
        f"Hex:     {hex(n)}",
        f"Bin:     {bin(n)}",
        f"Oct:     {oct(n)}",
    ]


def _format_numeric(result: Any, exact_str: str) -> str | None:
    """Return a numeric approximation string, or None if uninformative.

    Skips the approximation for plain integers (``14`` doesn't benefit
    from ``14.0000000000000``) and for plain rationals (``1/2`` is
    already the clearer form).  Otherwise returns ``result.evalf()`` as
    a string, but only if it's distinct from the exact form.
    """
    try:
        if result.is_Integer:
            return None
        if result.is_Rational:
            return None
    except AttributeError:
        pass
    try:
        approx = result.evalf()
    except Exception:
        return None
    approx_str = str(approx)
    if approx_str == exact_str:
        return None
    return approx_str


def _format_result(
    result: Any,
    expression: str,
    view: str | None,
    show_bases: bool,
    sympy_version: str,
) -> str:
    """Format ``result`` for agent consumption.

    Views override the default layout:

    - ``numeric`` → just the ``.evalf()`` string.
    - ``latex``   → ``sympy.latex(result)``.
    - ``pretty``  → ``sympy.pretty(result, use_unicode=True)``.
    - *default*   → multi-line block with Input / Exact / Numeric /
      Hex/Bin/Oct (when integer + requested) / Pretty (for matrices).
    """
    import sympy

    if view == "numeric":
        try:
            body = str(result.evalf())
        except Exception:
            body = str(result)
        return f"{body}\n\n{_attribution(sympy_version)}"

    if view == "latex":
        try:
            body = sympy.latex(result)
        except Exception:
            body = str(result)
        return f"{body}\n\n{_attribution(sympy_version)}"

    if view == "pretty":
        try:
            body = sympy.pretty(result, use_unicode=True)
        except Exception:
            body = str(result)
        return f"{body}\n\n{_attribution(sympy_version)}"

    # Default: structured block.
    exact_str = str(result)
    lines = [f"Input:   {expression}", f"Exact:   {exact_str}"]

    numeric = _format_numeric(result, exact_str)
    if numeric is not None:
        lines.append(f"Numeric: {numeric}")

    if show_bases and _is_integer_result(result):
        lines.extend(_format_bases(result))

    # Pretty-print for matrices and non-trivial expressions.  For simple
    # integers / rationals / single symbols the Exact line is enough.
    if isinstance(result, sympy.MatrixBase):
        try:
            pretty = sympy.pretty(result, use_unicode=True)
            lines.append("")
            lines.append("Pretty:")
            lines.append(pretty)
        except Exception:
            pass

    body = "\n".join(lines)
    return f"{body}\n\n{_attribution(sympy_version)}"


# ---------------------------------------------------------------------------
# Help view — inlines the onboarding skill
# ---------------------------------------------------------------------------


def _help_text(sympy_version: str) -> str:
    """Return a concise help block.

    Points the agent at the two skills (``calc-basics``,
    ``calc-advanced``) where the full how-to lives.  The skill handler
    is what renders them on demand; here we just surface the pointers
    so a bare ``calc:/help`` call is useful even if the skill kind is
    disabled.
    """
    return (
        "# calc — local calculator (SymPy)\n"
        "\n"
        "Free, offline, deterministic.  Evaluates math expressions in\n"
        "Python-like notation with implicit multiplication and ``^`` for\n"
        "powers.\n"
        "\n"
        "**Basics** — arithmetic, roots, trig, number bases:\n"
        "  get(id='calc:2+3*4')              # 14\n"
        "  get(id='calc:sqrt(2)')            # exact + numeric\n"
        "  get(id='calc:0xff + 0b1010')      # 265 with hex/bin/oct shown\n"
        "  get(id='calc:sin(pi/6)')          # 1/2\n"
        "  get(id='calc:1/3 + 1/6')          # 1/2 (exact rational)\n"
        "\n"
        "  See: get(id='skill:calc-basics')\n"
        "\n"
        "**Advanced** — calculus, matrices, symbolic, units:\n"
        "  get(id='calc:integrate(sin(x)*cos(x), x)')\n"
        "  get(id='calc:diff(x^3 + 2x, x)')\n"
        "  get(id='calc:solve(x^2 - 4, x)')\n"
        "  get(id='calc:Matrix([[1,2],[3,4]]).det()')\n"
        "  get(id='calc:convert_to(5*foot, meter)')\n"
        "\n"
        "  See: get(id='skill:calc-advanced')\n"
        "\n"
        "**Views**:\n"
        "  /pretty   Unicode pretty-print\n"
        "  /latex    LaTeX form\n"
        "  /numeric  decimal approximation only\n"
        "  /help     this message\n"
        "\n"
        "For natural-language queries (e.g. 'population of Ireland',\n"
        "'what is the boiling point of water') use the paid ``math:``\n"
        "(Wolfram Alpha) kind instead.\n"
        "\n"
        f"{_attribution(sympy_version)}"
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class CalcHandler(Handler):
    """Handler for the ``calc:`` scheme — local SymPy calculator.

    Agent usage::

        get(id='calc:2+3*4')                       # arithmetic
        get(id='calc:1/2')                         # exact rational
        get(id='calc:sqrt(2)')                     # roots, exact + numeric
        get(id='calc:integrate(sin(x)*cos(x), x)') # calculus
        get(id='calc:Matrix([[1,2],[3,4]]).det()') # linear algebra
        get(id='calc:convert_to(5*foot, meter)')   # unit conversion
        get(id='calc:0xff')                        # hex literal → 255
        get(id='calc:2+2/pretty')                  # Unicode pretty-print
        get(id='calc:/help')                       # onboarding
    """

    scheme = "calc"
    writable = False
    # Match the ClassVar shape on Handler.views exactly.
    views: ClassVar[set[str] | dict[str, str]] = set(_KNOWN_VIEWS)
    onboarding_skill: ClassVar[str | None] = "calc-basics"

    # ---- Core read --------------------------------------------------

    def read(
        self,
        path: str,
        selector: str | None,
        view: str | None,
        subview: str | None,
        query: str,
        summarize: bool,
        depth: int,
        page: int,
    ) -> str:
        # The URI parser treats ``calc`` as opaque, so ``view`` passed
        # in here is always None.  Extract any trailing ``/view`` from
        # the raw path ourselves.
        expression, parsed_view = _parse_path(path or query or "")
        resolved_view = view or parsed_view

        # Bare ``calc:`` or ``calc:/help`` — return onboarding.
        if resolved_view == "help" or not expression.strip():
            try:
                _local, _global, _trans, version = _build_namespace()
            except ImportError as exc:
                raise PrecisError(
                    ErrorCode.KIND_UNAVAILABLE,
                    "sympy package not installed. "
                    "Install with: pip install precis-mcp[calc]",
                ) from exc
            return _help_text(version)

        # Pre-filter dangerous constructs.
        _sanitize(expression)

        # Build namespace (cached) and evaluate.
        try:
            local_dict, global_dict, transformations, version = _build_namespace()
        except ImportError as exc:
            raise PrecisError(
                ErrorCode.KIND_UNAVAILABLE,
                "sympy package not installed. "
                "Install with: pip install precis-mcp[calc]",
            ) from exc

        from sympy.parsing.sympy_parser import parse_expr

        try:
            result = parse_expr(
                expression,
                local_dict=local_dict,
                global_dict=global_dict,
                transformations=transformations,
            )
        except (SyntaxError, TypeError, ValueError, NameError) as exc:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                f"calc: could not parse {expression!r}: {exc}",
            ) from exc
        except Exception as exc:
            raise PrecisError(
                ErrorCode.UNEXPECTED,
                f"calc: evaluation failed for {expression!r}: {exc}",
            ) from exc

        # Show base representations when the input suggested the user
        # cares about them.
        show_bases = bool(
            _BASE_LITERAL_RE.search(expression) or _BASE_CALL_RE.search(expression)
        )

        return _format_result(result, expression, resolved_view, show_bases, version)

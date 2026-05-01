"""Local sympy-backed calculator. Stateless. No DB.

Pass an expression as `id=` (or `q=`); the result is the value. Sympy
handles arithmetic, exact fractions, roots, calculus, linear algebra,
symbolic manipulation.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.errors import BadInput
from precis.protocol import Handler, KindSpec
from precis.response import Response


class CalcHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="calc",
        title="Calculator",
        description=(
            "Local symbolic and numeric computation via sympy. "
            "Pass an expression as `id`; the result is the value."
        ),
        supports_get=True,
        is_numeric=False,
        id_required=True,
    )

    def __init__(self) -> None:
        # ``sympy`` is an optional [calc] / [all] extra. Import here
        # so a bare ``pip install precis-mcp`` surface a clean
        # missing-dep at boot (dispatch._try catches ImportError and
        # drops the calc kind), rather than failing at module import
        # and taking the whole precis.handlers package down with it.
        import sympy

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
        try:
            expr = sympy.sympify(expr_str)
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
            raise BadInput(
                f"could not evaluate {expr_str!r} — unsupported expression",
                next=(
                    "calc handles arithmetic, calculus, simplify, and similar "
                    "math; check operator names like integrate, diff, simplify, "
                    "solve. For Python builtins / I/O, use a different tool."
                ),
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
            raise BadInput(
                f"expression simplifies to itself: {expr_str!r}",
                next=(
                    "calc evaluates math expressions (2+3*4, "
                    "integrate(sin(x), x), …). For symbolic identities "
                    "with no operators sympy can act on, give it more "
                    "structure (e.g. solve(Eq(x+1, 3), x))"
                ),
            )

        return Response(body=f"{expr_str} = {_humanise(result)}")

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

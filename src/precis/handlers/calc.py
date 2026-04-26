"""Local sympy-backed calculator. Stateless. No DB.

Pass an expression as `id=` (or `q=`); the result is the value. Sympy
handles arithmetic, exact fractions, roots, calculus, linear algebra,
symbolic manipulation.
"""

from __future__ import annotations

from typing import Any, ClassVar

import sympy

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

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        q: str | None = None,
        **_kw: Any,
    ) -> Response:
        expr_str = self._coerce_expr(id, q)
        try:
            expr = sympy.sympify(expr_str)
        except (sympy.SympifyError, SyntaxError, TypeError) as e:
            raise BadInput(
                f"could not parse expression: {expr_str!r}",
                next="get(kind='calc', id='2+3*4')",
            ) from e

        try:
            result = expr if expr.is_number else expr.doit()
        except Exception as e:
            raise BadInput(
                f"could not evaluate {expr_str!r}: {e}",
                next="check operator names: integrate, diff, simplify, ...",
            ) from e

        return Response(body=f"{expr_str} = {result}")

    @staticmethod
    def _coerce_expr(id: str | int | None, q: str | None) -> str:
        if isinstance(id, str) and id:
            return id
        if isinstance(id, int):
            return str(id)
        if isinstance(q, str) and q:
            return q
        raise BadInput(
            "calc requires an expression as `id` or `q`",
            next="get(kind='calc', id='2+3*4')",
        )

"""CalcHandler — sympy-backed calculator."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Unsupported
from precis.handlers.calc import CalcHandler


@pytest.fixture
def handler() -> CalcHandler:
    return CalcHandler(hub=Hub())


def test_basic_arithmetic(handler: CalcHandler) -> None:
    r = handler.get(id="2+3*4")
    assert "14" in r.body


def test_uses_q_when_id_absent(handler: CalcHandler) -> None:
    r = handler.get(q="1+1")
    assert "2" in r.body


def test_integer_id_is_coerced(handler: CalcHandler) -> None:
    r = handler.get(id=255)
    # 255 alone evaluates to 255
    assert "255" in r.body


def test_integration(handler: CalcHandler) -> None:
    r = handler.get(id="integrate(sin(x), x)")
    assert "cos" in r.body  # -cos(x)


def test_matrix_determinant(handler: CalcHandler) -> None:
    r = handler.get(id="Matrix([[1,2],[3,4]]).det()")
    assert "-2" in r.body


def test_unparseable(handler: CalcHandler) -> None:
    with pytest.raises(BadInput) as exc:
        handler.get(id="this is not math")
    assert exc.value.next is not None


def test_missing_expr_raises(handler: CalcHandler) -> None:
    with pytest.raises(BadInput):
        handler.get()


def test_other_verbs_unsupported(handler: CalcHandler) -> None:
    with pytest.raises(Unsupported):
        handler.search(q="x")
    with pytest.raises(Unsupported):
        handler.put(text="x")
    with pytest.raises(Unsupported):
        handler.edit(id=1)
    with pytest.raises(Unsupported):
        handler.delete(id=1)
    with pytest.raises(Unsupported):
        handler.tag(id=1, add=["x"])
    with pytest.raises(Unsupported):
        handler.link(id=1, target="x:y")


def test_kindspec_declares_only_get() -> None:
    spec = CalcHandler.spec
    assert spec.kind == "calc"
    assert spec.supports_get is True
    assert spec.supports_search is False
    assert spec.supports_put is False
    assert spec.supports_edit is False
    assert spec.supports_delete is False
    assert spec.supports_tag is False
    assert spec.supports_link is False


# ── solve / factor_list (sympy containers) ─────────────────────────


class TestSympyContainerReturns:
    """Regression for MCP critic round 2: ``solve(Eq(x+1, 3), x)`` was
    advertised as the recovery hint for "expression simplifies to
    itself" errors, but actually calling it surfaced the cryptic
    ``'list' object has no attribute 'is_number'`` → wrapped as
    ``could not evaluate ... — unsupported expression``. Root cause:
    sympy's ``solve`` runs eagerly inside ``sympify`` and returns a
    plain Python list, which the downstream pipeline (``.is_number``,
    ``.doit()``, ``simplify``) can't process.

    The calc handler now short-circuits on Python container returns
    (list / tuple / dict / set / frozenset) and renders them directly,
    so every sympy function the recovery hints advertise actually
    works.
    """

    def test_solve_linear(self, handler: CalcHandler) -> None:
        r = handler.get(id="solve(Eq(x+1, 3), x)")
        # Result is a list with the single solution.
        assert "[2]" in r.body
        # The rendered body keeps the original expression on the LHS.
        assert "solve(Eq(x+1, 3), x)" in r.body

    def test_solve_quadratic_two_roots(self, handler: CalcHandler) -> None:
        r = handler.get(id="solve(Eq(x**2 - 1, 0), x)")
        # sympy returns [-1, 1]; exact order may vary across versions
        # but both roots must appear.
        assert "-1" in r.body
        assert "1" in r.body

    def test_factor_list_returns_tuple(self, handler: CalcHandler) -> None:
        r = handler.get(id="factor_list(x**4 - 1)")
        # Result is a (content, factors) tuple.
        assert "x - 1" in r.body or "(x - 1" in r.body
        assert "x + 1" in r.body or "(x + 1" in r.body

    def test_solve_no_solutions_renders_empty_list(
        self, handler: CalcHandler
    ) -> None:
        """``solve(Eq(x+1, x+2), x)`` has no solutions — sympy returns
        ``[]``. The handler must render that cleanly rather than
        re-entering the "simplifies to itself" branch."""
        r = handler.get(id="solve(Eq(x+1, x+2), x)")
        assert "[]" in r.body

    def test_finiteset_still_uses_fast_path(
        self, handler: CalcHandler
    ) -> None:
        """``solveset`` returns a sympy ``FiniteSet`` — a ``Basic``
        subclass — which must NOT hit the container short-circuit.
        Regression in case someone tightens the isinstance check."""
        r = handler.get(id="solveset(Eq(x**2 - 1, 0), x)")
        # FiniteSet renders with curly braces.
        assert "{-1, 1}" in r.body or "{1, -1}" in r.body

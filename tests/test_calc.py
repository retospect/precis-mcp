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


# ── trigonometry: radians default + degrees switch ─────────────────


class TestTrig:
    """calc is sympy-backed, so trig is exact. **Degrees is the default**
    (engineering-leaning: ``sin(30)`` → ``1/2``); ``view='rad'`` opts
    into sympy's native radians for symbolic calculus."""

    def test_degrees_is_default(self, handler: CalcHandler) -> None:
        # sin(30°) = 1/2 with no view= needed.
        r = handler.get(q="sin(30)")
        assert "1/2" in r.body

    def test_degrees_tan_45(self, handler: CalcHandler) -> None:
        r = handler.get(q="tan(45)")
        assert "= 1" in r.body

    def test_degrees_inverse_returns_degrees(self, handler: CalcHandler) -> None:
        # atan2(1, 1) = 45° in the default degrees mode.
        r = handler.get(q="N(atan2(1, 1))")
        assert "45" in r.body

    def test_explicit_view_deg_is_degrees(self, handler: CalcHandler) -> None:
        # view='deg' is an explicit synonym for the default.
        r = handler.get(q="cos(60)", view="deg")
        assert "1/2" in r.body

    def test_radian_switch(self, handler: CalcHandler) -> None:
        # view='rad' → sympy native: sin(pi/6) = 1/2.
        r = handler.get(q="sin(pi/6)", view="rad")
        assert "1/2" in r.body

    def test_radian_switch_bare_number_stays_symbolic(
        self, handler: CalcHandler
    ) -> None:
        # In radians, sin(30) is 30 radians — does NOT collapse to 1/2.
        r = handler.get(q="sin(30)", view="rad")
        assert "1/2" not in r.body

    def test_degrees_note_present_when_trig_used(self, handler: CalcHandler) -> None:
        r = handler.get(q="sin(30)")
        assert "degrees" in r.body
        assert "view='rad'" in r.body

    def test_no_degrees_note_without_trig(self, handler: CalcHandler) -> None:
        r = handler.get(q="2+3*4")
        assert "degrees" not in r.body

    def test_no_degrees_note_in_radian_mode(self, handler: CalcHandler) -> None:
        r = handler.get(q="sin(pi/6)", view="rad")
        assert "degrees" not in r.body

    def test_radian_calculus_is_clean(self, handler: CalcHandler) -> None:
        # The canonical calculus example only stays clean in radians.
        r = handler.get(q="integrate(sin(x), x)", view="rad")
        assert "cos" in r.body and "180" not in r.body

    def test_sqrt_and_power(self, handler: CalcHandler) -> None:
        assert "2" in handler.get(q="sqrt(4)").body
        assert "1024" in handler.get(q="2**10").body


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

    def test_solve_no_solutions_renders_empty_list(self, handler: CalcHandler) -> None:
        """``solve(Eq(x+1, x+2), x)`` has no solutions — sympy returns
        ``[]``. The handler must render that cleanly rather than
        re-entering the "simplifies to itself" branch."""
        r = handler.get(id="solve(Eq(x+1, x+2), x)")
        assert "[]" in r.body

    def test_finiteset_still_uses_fast_path(self, handler: CalcHandler) -> None:
        """``solveset`` returns a sympy ``FiniteSet`` — a ``Basic``
        subclass — which must NOT hit the container short-circuit.
        Regression in case someone tightens the isinstance check."""
        r = handler.get(id="solveset(Eq(x**2 - 1, 0), x)")
        # FiniteSet renders with curly braces.
        assert "{-1, 1}" in r.body or "{1, -1}" in r.body


# ── error-envelope contract ────────────────────────────────────────
#
# The critic's rule for ``PrecisError.next`` is "one copy-pasteable
# next action". Earlier revisions of calc violated that in two
# places (the unsupported-expression and simplifies-to-itself
# branches) by stuffing prose operator lists into ``next``.
#
# These tests pin the contract for every calc BadInput:
#   1. ``cause`` is a non-empty string (preserves the old
#      information that used to live in the prose next=).
#   2. ``next`` parses as a literal ``get(kind='calc', q='...')``
#      call — i.e. an LLM can paste it verbatim back into the tool
#      and it runs. No English prose, no parentheticals.
# Regresses silently otherwise; the contract is architectural,
# not testable any other way.


class TestErrorEnvelopeShape:
    """Every calc BadInput has cause + copy-pasteable next."""

    _GET_CALC_PREFIX = "get(kind='calc', q='"

    def _assert_envelope(self, exc: BadInput) -> None:
        # Cause is present and non-empty.
        assert isinstance(exc.cause, str) and exc.cause.strip()
        # next is present and shaped like a concrete get() call.
        assert exc.next is not None
        assert exc.next.startswith(self._GET_CALC_PREFIX), (
            f"next= is not a copy-pasteable get() call: {exc.next!r}"
        )
        # The call body closes with '). That's not a perfect parser,
        # but it catches prose that slipped past the prefix check.
        assert exc.next.endswith("')"), (
            f"next= is not a copy-pasteable get() call: {exc.next!r}"
        )

    def test_parse_error_envelope(self, handler: CalcHandler) -> None:
        """``this is not math`` → SympifyError → parse-error branch."""
        with pytest.raises(BadInput) as exc_info:
            handler.get(id="this is not math")
        self._assert_envelope(exc_info.value)

    def test_unsupported_expression_envelope(self, handler: CalcHandler) -> None:
        """Expression that parses but fails ``.doit()``. ``1/0`` now
        evaluates to ``zoo``, so we need a sympify-parseable shape
        that blows up at evaluation. ``Integral(nonsense, (x,0,1))``
        where ``nonsense`` is something sympy can't integrate."""
        # diff() on a non-expression raises AttributeError inside .doit()
        # in some sympy versions, but the cleanest trigger is a Symbol
        # treated as a callable — ``Symbol('f')(x).diff(x)`` which
        # bubbles up TypeError.
        with pytest.raises(BadInput) as exc_info:
            # ``Derivative(f(x), x).doit()`` where ``f`` is just a
            # symbol is not a supported evaluation — surfaces as
            # TypeError inside .doit().
            handler.get(id="Derivative(log).doit()")
        self._assert_envelope(exc_info.value)

    def test_simplifies_to_itself_envelope(self, handler: CalcHandler) -> None:
        """Bare symbolic identifier — ``one plus two`` trips the
        "simplifies to itself + has free symbols" branch."""
        with pytest.raises(BadInput) as exc_info:
            handler.get(id="one plus two")
        self._assert_envelope(exc_info.value)

    def test_missing_expr_envelope(self, handler: CalcHandler) -> None:
        """No id / no q → coerce-error branch."""
        with pytest.raises(BadInput) as exc_info:
            handler.get()
        self._assert_envelope(exc_info.value)

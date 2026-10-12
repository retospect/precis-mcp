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


# ── trigonometry: explicit angle units (gr336352) ──────────────────


class TestTrig:
    """Numeric trig arguments carry an explicit unit (``sin(30 deg)``,
    ``sin(1.5 rad)``, ``sin(pi/6)``); bare numerics are rejected with the
    two-reading hint; symbolic args stay sympy-native; inverse functions
    answer in both forms."""

    def test_deg_literal(self, handler: CalcHandler) -> None:
        assert "= 1/2" in handler.get(q="sin(30 deg)").body

    @pytest.mark.parametrize("spelling", ["30°", "30 °", "30\\circ", "30 degrees"])
    def test_deg_spellings(self, handler: CalcHandler, spelling: str) -> None:
        assert "= 1/2" in handler.get(q=f"sin({spelling})").body

    def test_rad_literal(self, handler: CalcHandler) -> None:
        r = handler.get(q="N(sin(1.5 rad))")
        assert "0.997" in r.body

    def test_tan_45_deg(self, handler: CalcHandler) -> None:
        assert "= 1" in handler.get(q="tan(45 deg)").body

    def test_pi_argument_is_unambiguous(self, handler: CalcHandler) -> None:
        assert "= 1/2" in handler.get(q="sin(pi/6)").body

    def test_bare_numeric_rejected_with_two_readings(
        self, handler: CalcHandler
    ) -> None:
        with pytest.raises(BadInput) as ei:
            handler.get(q="sin(30)")
        msg = str(ei.value) + str(getattr(ei.value, "next", ""))
        assert "30 deg" in msg and "30 rad" in msg

    def test_inverse_answers_in_both_forms(self, handler: CalcHandler) -> None:
        r = handler.get(q="asin(1/2)")
        assert "pi/6" in r.body
        assert "30°" in r.body and "0.5236 rad" in r.body

    def test_atan2_both_forms(self, handler: CalcHandler) -> None:
        r = handler.get(q="atan2(1, 1)")
        assert "45°" in r.body and "0.7854 rad" in r.body

    def test_view_rad_is_obsolete_and_ignored(self, handler: CalcHandler) -> None:
        a = handler.get(q="sin(30 deg)", view="rad")
        assert "= 1/2" in a.body

    def test_no_angle_note_without_inverse_trig(self, handler: CalcHandler) -> None:
        assert "angle:" not in handler.get(q="2+3*4").body

    def test_symbolic_calculus_clean(self, handler: CalcHandler) -> None:
        r = handler.get(q="integrate(sin(x)**2, x)")
        assert "180" not in r.body and "pi" not in r.body
        assert "x/2" in r.body

    def test_symbolic_diff_clean(self, handler: CalcHandler) -> None:
        r = handler.get(q="diff(sin(x), x)")
        assert "cos(x)" in r.body and "180" not in r.body

    def test_mixed_numeric_unit_and_symbolic(self, handler: CalcHandler) -> None:
        r = handler.get(q="sin(30 deg) + diff(cos(x), x)")
        assert "1/2" in r.body and "sin(x)" in r.body and "180" not in r.body

    def test_collection_results_strip_unit_markers(self, handler: CalcHandler) -> None:
        r = handler.get(q="solve(x - 30 deg, x)")
        assert "angleunit" not in r.body and "pi/6" in r.body

    def test_angle_note_only_when_result_is_the_inverse_value(
        self, handler: CalcHandler
    ) -> None:
        assert "angle:" not in handler.get(q="asin(1/2)*180/pi").body
        assert "angle:" not in handler.get(q="cos(asin(1/2))").body

    def test_sin_zero_and_oo(self, handler: CalcHandler) -> None:
        assert "= 0" in handler.get(q="sin(0)").body
        with pytest.raises(BadInput) as ei:
            handler.get(q="sin(oo)")
        assert "non-finite" not in str(ei.value)

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
        # next is present and shaped like a concrete get() call — a
        # single string, not the multi-hint list form.
        assert isinstance(exc.next, str), f"expected a single next=, got {exc.next!r}"
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


# ── unit near-miss nudge (sympy path — no pint needed) ─────────────
#
# Discoverability catch: an agent that names a unit but doesn't use a
# `to` clause (`3 feet`, `hogshead`) would otherwise get a silent garbage
# echo (`3 feet = 3*feet`) or a solve(Eq()) hint. These paths now point at
# the conversion syntax instead. Only fires on already-degenerate input —
# valid math must never be nudged.


class TestUnitNearMissNudge:
    """`3 feet` / `hogshead` → point at the `to` conversion syntax instead
    of a generic parse error or a silent `3*feet` echo."""

    def test_number_space_unit_raises_with_nudge(self, handler: CalcHandler) -> None:
        # "5 miles" — a number juxtaposed with a unit is a Python syntax
        # error, so it hits the parse-fail branch; the recovery points at
        # conversion rather than the generic 2+3*4 example.
        with pytest.raises(BadInput) as exc:
            handler.get(q="5 miles")
        assert exc.value.next is not None
        assert "ft to m" in exc.value.next

    def test_feet_raises_with_nudge(self, handler: CalcHandler) -> None:
        with pytest.raises(BadInput) as exc:
            handler.get(q="3 feet")
        assert exc.value.next is not None and "ft to m" in exc.value.next

    def test_abbrev_unit_raises_with_nudge(self, handler: CalcHandler) -> None:
        with pytest.raises(BadInput) as exc:
            handler.get(q="2 kg")
        assert exc.value.next is not None and "ft to m" in exc.value.next

    def test_bare_unit_word_raises_with_conversion_next(
        self, handler: CalcHandler
    ) -> None:
        # "hogshead" alone → simplifies-to-itself, but it's a unit → the
        # recovery points at conversion, not solve(Eq(...)).
        with pytest.raises(BadInput) as exc:
            handler.get(q="hogshead")
        assert exc.value.next is not None
        assert "ft to m" in exc.value.next

    def test_free_symbol_echo_gets_note(self, handler: CalcHandler) -> None:
        # A unit-named free symbol surviving into the result ("gallon*2 + 3"
        # → "2*gallon + 3") is a silent-garbage echo — append the nudge.
        r = handler.get(q="gallon*2 + 3")
        assert "names a unit" in r.body
        assert "3 ft to m" in r.body

    def test_valid_math_is_never_nudged(self, handler: CalcHandler) -> None:
        # Plain arithmetic must not acquire a spurious unit note.
        r = handler.get(q="2+3*4")
        assert "14" in r.body
        assert "unit" not in r.body.lower()

    def test_trig_is_never_nudged(self, handler: CalcHandler) -> None:
        # 'in' inside 'sin' / degree trig must not trip the nudge.
        r = handler.get(q="sin(30 deg)")
        assert "1/2" in r.body
        assert "names a unit" not in r.body

    def test_symbolic_calculus_not_nudged(self, handler: CalcHandler) -> None:
        r = handler.get(q="integrate(sin(x), x)", view="rad")
        assert "cos" in r.body
        assert "names a unit" not in r.body


# ── unit conversion (pint) ─────────────────────────────────────────
#
# calc routes a query with a `to`/`in`/`->` clause to pint before sympy.
# The selling point over sympy.physics.units is the curated, DISAMBIGUATING
# registry — so the disambiguation cases (ton/gallon/oz) are the load-
# bearing tests, not the happy-path length conversion. pint is an optional
# [calc] dep; skip the whole module when it's absent so the suite stays
# green on a torch-free / minimal install.

pint = pytest.importorskip("pint")


class TestUnitConversion:
    """`3 ft to m` and friends — local, offline, exact."""

    def test_length_to(self, handler: CalcHandler) -> None:
        r = handler.get(q="3 ft to m")
        assert "0.9144" in r.body
        # The source side is echoed on the LHS, like the sympy path.
        assert "3 ft =" in r.body

    def test_arrow_form(self, handler: CalcHandler) -> None:
        r = handler.get(q="100 km -> mi")
        assert "62.13" in r.body  # 62.1371…

    def test_in_separator(self, handler: CalcHandler) -> None:
        r = handler.get(q="5 km in miles")
        assert "3.106" in r.body  # 3.10686…

    def test_unit_only_factor(self, handler: CalcHandler) -> None:
        # No magnitude — a bare unit-to-unit conversion factor.
        r = handler.get(q="ft to m")
        assert "0.3048" in r.body

    def test_unit_arithmetic_keeps_inch(self, handler: CalcHandler) -> None:
        # Rightmost-` to ` split must leave the inch in the source intact:
        # 3 ft + 2 in = 38 in = 96.52 cm.
        r = handler.get(q="3 ft + 2 in to cm")
        assert "96.52" in r.body


class TestUnitDisambiguation:
    """The reason pint beats sympy.physics.units: it keeps colliding unit
    names distinct and resolves the bare name to a documented default."""

    def test_ton_is_us_short_ton(self, handler: CalcHandler) -> None:
        # Bare `ton` = US short ton = 907.185 kg (pint's documented default).
        assert "907" in handler.get(q="1 ton to kg").body

    def test_metric_ton(self, handler: CalcHandler) -> None:
        assert "1000" in handler.get(q="1 metric_ton to kg").body
        assert "1000" in handler.get(q="1 tonne to kg").body

    def test_long_ton(self, handler: CalcHandler) -> None:
        assert "1016" in handler.get(q="1 long_ton to kg").body  # 1016.05

    def test_us_vs_imperial_gallon(self, handler: CalcHandler) -> None:
        assert "3.785" in handler.get(q="1 gallon to L").body  # US
        assert "4.546" in handler.get(q="1 imperial_gallon to L").body

    def test_mass_vs_fluid_ounce(self, handler: CalcHandler) -> None:
        assert "28.3" in handler.get(q="1 oz to g").body  # mass, 28.3495
        assert "29.57" in handler.get(q="1 fluid_ounce to mL").body  # volume


class TestTemperatureConversion:
    """Offset units go through the Quantity() parser, not parse_expression."""

    def test_celsius_to_fahrenheit(self, handler: CalcHandler) -> None:
        assert "212" in handler.get(q="100 degC to degF").body

    def test_fahrenheit_to_celsius(self, handler: CalcHandler) -> None:
        assert "37.7" in handler.get(q="100 degF to degC").body  # 37.777…


class TestConversionErrors:
    """Ambiguity and nonsense fail loud with a copy-pasteable next=."""

    def test_incompatible_dimensions(self, handler: CalcHandler) -> None:
        with pytest.raises(BadInput) as exc:
            handler.get(q="3 ft to kg")  # length → mass
        assert exc.value.next is not None

    def test_unknown_unit(self, handler: CalcHandler) -> None:
        with pytest.raises(BadInput) as exc:
            handler.get(q="3 blorp to m")
        assert exc.value.next is not None


class TestConversionDetectorDoesNotStealMath:
    """The detector must only fire on a real `to`/`in`/`->` clause — plain
    sympy expressions (which never contain a space-delimited to/in) fall
    straight through unchanged."""

    def test_plain_arithmetic_still_sympy(self, handler: CalcHandler) -> None:
        assert "14" in handler.get(q="2+3*4").body

    def test_trig_still_sympy(self, handler: CalcHandler) -> None:
        # 'in' inside 'sin' is not space-delimited — no false route.
        assert "1/2" in handler.get(q="sin(30 deg)").body

    def test_integrate_still_sympy(self, handler: CalcHandler) -> None:
        assert "cos" in handler.get(q="integrate(sin(x), x)", view="rad").body

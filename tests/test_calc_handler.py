"""Phase 5 — local calculator handler (SymPy).

Covers:

- Path parsing: ``calc:1/2`` vs ``calc:1/2/pretty`` vs ``calc:/help``
- Arithmetic, exact fractions, roots, trig
- Calculus, linear algebra, equation solving, unit conversion
- Number-base conversion (hex / bin / oct literals & builtins)
- Views: default, ``/pretty``, ``/latex``, ``/numeric``, ``/help``
- Safety: dunder access, lambda, comprehensions, ``__import__`` all rejected
- Registry: kind is registered when sympy is available
- Scheme opacity: URI parser keeps ``/`` inside calc expressions
"""

from __future__ import annotations

import pytest

sympy = pytest.importorskip("sympy")

from precis.handlers.calc import (
    CalcHandler,
    _parse_path,
    _sanitize,
)
from precis.protocol import ErrorCode, PrecisError
from precis.registry import KINDS, SCHEMES
from precis.uri import _OPAQUE_PATH_SCHEMES, parse

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _read(h: CalcHandler, path: str) -> str:
    return h.read(
        path=path,
        selector=None,
        view=None,
        subview=None,
        query="",
        summarize=False,
        depth=0,
        page=1,
    )


# ---------------------------------------------------------------------------
# Path parsing
# ---------------------------------------------------------------------------


class TestParsePath:
    def test_bare_expression_no_view(self):
        assert _parse_path("2+3") == ("2+3", None)

    def test_division_stays_in_expression(self):
        assert _parse_path("1/2") == ("1/2", None)

    def test_trailing_pretty(self):
        assert _parse_path("2+2/pretty") == ("2+2", "pretty")

    def test_trailing_latex(self):
        assert _parse_path("pi/4/latex") == ("pi/4", "latex")

    def test_trailing_numeric(self):
        assert _parse_path("sqrt(2)/numeric") == ("sqrt(2)", "numeric")

    def test_bare_help_view(self):
        assert _parse_path("/help") == ("", "help")

    def test_empty_path(self):
        assert _parse_path("") == ("", None)

    def test_unknown_trailing_segment_stays_in_expression(self):
        # ``foo`` isn't a known view, so the whole string is the expr.
        assert _parse_path("1/2/foo") == ("1/2/foo", None)

    def test_function_call_with_slashes_then_view(self):
        expr = "integrate(sin(x)*cos(x), x)/latex"
        assert _parse_path(expr) == ("integrate(sin(x)*cos(x), x)", "latex")


# ---------------------------------------------------------------------------
# URI-level — calc is opaque, slashes survive
# ---------------------------------------------------------------------------


class TestCalcIsOpaque:
    def test_calc_in_opaque_schemes(self):
        assert "calc" in _OPAQUE_PATH_SCHEMES

    def test_parser_keeps_slash_in_path(self):
        # With calc opaque, ``calc:1/2`` must round-trip as path='1/2'.
        parsed = parse("calc:1/2")
        assert parsed.scheme == "calc"
        assert parsed.path == "1/2"
        assert parsed.view is None

    def test_parser_keeps_trailing_view_in_path(self):
        # Handler is responsible for stripping /pretty; parser passes
        # it through verbatim.
        parsed = parse("calc:1/2/pretty")
        assert parsed.path == "1/2/pretty"
        assert parsed.view is None

    def test_parser_keeps_function_call_with_commas(self):
        parsed = parse("calc:integrate(sin(x)*cos(x), x)")
        assert parsed.path == "integrate(sin(x)*cos(x), x)"


# ---------------------------------------------------------------------------
# Safety — AST sanitiser
# ---------------------------------------------------------------------------


class TestSanitize:
    def test_plain_arithmetic_passes(self):
        _sanitize("2+3*4")

    def test_sqrt_passes(self):
        _sanitize("sqrt(2)")

    def test_matrix_passes(self):
        _sanitize("Matrix([[1,2],[3,4]]).det()")

    def test_attribute_call_passes(self):
        _sanitize("Matrix([[1]]).rank()")

    def test_dunder_attribute_blocked(self):
        with pytest.raises(PrecisError) as exc:
            _sanitize("(1).__class__")
        assert exc.value.code == ErrorCode.PARAM_INVALID

    def test_dunder_name_blocked(self):
        with pytest.raises(PrecisError):
            _sanitize("__import__('os')")

    def test_private_attribute_blocked(self):
        # Any leading-underscore attr is refused — SymPy surface uses
        # public names.
        with pytest.raises(PrecisError):
            _sanitize("x._hidden")

    def test_lambda_blocked(self):
        with pytest.raises(PrecisError) as exc:
            _sanitize("lambda x: x")
        assert "Lambda" in exc.value.cause

    def test_list_comprehension_blocked(self):
        with pytest.raises(PrecisError):
            _sanitize("[i for i in range(5)]")

    def test_generator_expression_blocked(self):
        with pytest.raises(PrecisError):
            _sanitize("(i for i in range(5))")

    def test_walrus_blocked(self):
        with pytest.raises(PrecisError):
            _sanitize("(y := 2+3)")

    def test_fstring_blocked(self):
        with pytest.raises(PrecisError):
            _sanitize('f"{x}"')

    def test_syntax_error_deferred_to_parse_expr(self):
        # Malformed input shouldn't blow up in the sanitiser — we let
        # parse_expr produce the better error message.
        _sanitize("2 ++ 3 ++")  # garbage, but _sanitize shouldn't raise


# ---------------------------------------------------------------------------
# Handler — end-to-end basic computation
# ---------------------------------------------------------------------------


class TestCalcBasics:
    def setup_method(self):
        self.h = CalcHandler()

    def test_arithmetic(self):
        out = _read(self.h, "2+3*4")
        assert "Exact:   14" in out
        # Plain integers shouldn't show a redundant Numeric line
        assert "Numeric:" not in out

    def test_power_caret(self):
        out = _read(self.h, "2^10")
        assert "Exact:   1024" in out

    def test_power_double_star(self):
        out = _read(self.h, "2**10")
        assert "Exact:   1024" in out

    def test_implicit_multiplication(self):
        out = _read(self.h, "2x + 3")
        assert "2*x + 3" in out

    def test_exact_rational(self):
        out = _read(self.h, "1/2")
        assert "Exact:   1/2" in out
        # Rationals don't need the redundant decimal approximation
        assert "Numeric:" not in out

    def test_rationalised_decimals(self):
        out = _read(self.h, "0.1 + 0.2")
        assert "Exact:   3/10" in out

    def test_sqrt_shows_exact_and_numeric(self):
        out = _read(self.h, "sqrt(2)")
        assert "Exact:   sqrt(2)" in out
        assert "Numeric: 1.41421" in out

    def test_sqrt_simplifies(self):
        out = _read(self.h, "sqrt(50)")
        assert "5*sqrt(2)" in out

    def test_trig(self):
        out = _read(self.h, "sin(pi/2)")
        assert "Exact:   1" in out

    def test_pi_over_four(self):
        out = _read(self.h, "pi/4")
        assert "Exact:   pi/4" in out
        assert "Numeric: 0.7853" in out

    def test_complex_arithmetic(self):
        out = _read(self.h, "(3+4I)*(5-2I)")
        assert "23" in out and "14" in out


class TestCalcBaseConversion:
    def setup_method(self):
        self.h = CalcHandler()

    def test_hex_literal_shows_all_bases(self):
        out = _read(self.h, "0xff")
        assert "Exact:   255" in out
        assert "Hex:     0xff" in out
        assert "Bin:     0b11111111" in out
        assert "Oct:     0o377" in out

    def test_bin_literal(self):
        out = _read(self.h, "0b1010")
        assert "Exact:   10" in out
        assert "Hex:     0xa" in out

    def test_oct_literal(self):
        out = _read(self.h, "0o17")
        assert "Exact:   15" in out
        assert "Hex:     0xf" in out

    def test_hex_builtin(self):
        out = _read(self.h, "hex(255)")
        # hex() returns a string — the literal 'ff' appears in the result
        assert "0xff" in out

    def test_int_from_hex_string(self):
        out = _read(self.h, "int('ff', 16)")
        # int() returns a Python int — bases view should fire
        assert "Exact:   255" in out
        assert "Hex:     0xff" in out

    def test_plain_integer_no_base_lines(self):
        # No hex/bin/oct literal in input → don't bother showing bases.
        out = _read(self.h, "2+3")
        assert "Hex:" not in out
        assert "Bin:" not in out


class TestCalcCalculus:
    def setup_method(self):
        self.h = CalcHandler()

    def test_indefinite_integral(self):
        out = _read(self.h, "integrate(sin(x)*cos(x), x)")
        assert "sin(x)**2/2" in out

    def test_gaussian_integral(self):
        out = _read(self.h, "integrate(exp(-x^2), (x, -oo, oo))")
        assert "sqrt(pi)" in out

    def test_derivative(self):
        out = _read(self.h, "diff(x^3 + 2x, x)")
        assert "3*x**2 + 2" in out

    def test_limit(self):
        out = _read(self.h, "limit(sin(x)/x, x, 0)")
        assert "Exact:   1" in out

    def test_solve_quadratic(self):
        out = _read(self.h, "solve(x^2 - 4, x)")
        assert "-2" in out and "2" in out

    def test_expand(self):
        out = _read(self.h, "expand((x+1)^3)")
        assert "x**3 + 3*x**2 + 3*x + 1" in out

    def test_factor(self):
        out = _read(self.h, "factor(x^2 - 4)")
        assert "(x - 2)*(x + 2)" in out or "(x + 2)*(x - 2)" in out


class TestCalcLinearAlgebra:
    def setup_method(self):
        self.h = CalcHandler()

    def test_matrix_det(self):
        out = _read(self.h, "Matrix([[1,2],[3,4]]).det()")
        assert "Exact:   -2" in out

    def test_matrix_inv(self):
        out = _read(self.h, "Matrix([[1,2],[3,4]]).inv()")
        # Inverse of [[1,2],[3,4]] is [[-2, 1], [3/2, -1/2]]
        assert "-2" in out and "3/2" in out
        # Matrix results trigger the pretty block
        assert "Pretty:" in out

    def test_identity(self):
        out = _read(self.h, "eye(3)")
        assert "Matrix" in out or "1" in out  # body contains identity
        assert "Pretty:" in out

    def test_eigenvalues(self):
        out = _read(self.h, "Matrix([[0,1],[-2,-3]]).eigenvals()")
        assert "-1" in out and "-2" in out


class TestCalcUnits:
    def setup_method(self):
        self.h = CalcHandler()

    def test_feet_to_meters(self):
        out = _read(self.h, "convert_to(5*foot, meter)")
        # Exact: 381*meter/250 (SymPy's rational form for 1.524)
        assert "meter" in out
        assert "381" in out or "1.524" in out

    def test_mph_to_mps(self):
        out = _read(self.h, "convert_to(100*mile/hour, meter/second)")
        assert "meter" in out and "second" in out
        assert "44.704" in out or "5588" in out

    def test_degrees_to_radians(self):
        out = _read(self.h, "convert_to(360*degree, radian)")
        assert "2*pi*radian" in out or "radian" in out


class TestCalcViews:
    def setup_method(self):
        self.h = CalcHandler()

    def test_default_view_has_input_line(self):
        out = _read(self.h, "2+2")
        assert out.startswith("Input:")

    def test_latex_view(self):
        out = _read(self.h, "pi/4/latex")
        assert "\\frac" in out or "\\pi" in out
        # LaTeX view doesn't include the "Input:" header
        assert "Input:" not in out.split("---")[0]

    def test_numeric_view(self):
        out = _read(self.h, "sqrt(2)/numeric")
        assert "1.41421" in out
        assert "Input:" not in out.split("---")[0]

    def test_pretty_view(self):
        out = _read(self.h, "sqrt(2)/pretty")
        # Unicode pretty-print for sqrt(2) uses the radical symbol
        assert "√" in out or "sqrt" in out

    def test_help_view_bare(self):
        out = _read(self.h, "/help")
        assert "calc" in out.lower()
        assert "skill:calc-basics" in out
        assert "skill:calc-advanced" in out

    def test_help_view_empty_path(self):
        out = _read(self.h, "")
        assert "calc" in out.lower()
        assert "skill:calc-basics" in out


class TestCalcSafety:
    """End-to-end: dangerous inputs raise PrecisError, not silent compute."""

    def setup_method(self):
        self.h = CalcHandler()

    def test_import_blocked(self):
        with pytest.raises(PrecisError) as exc:
            _read(self.h, "__import__('os').system('echo pwn')")
        assert exc.value.code == ErrorCode.PARAM_INVALID

    def test_dunder_attribute_blocked(self):
        with pytest.raises(PrecisError) as exc:
            _read(self.h, "(1).__class__.__bases__[0]")
        assert exc.value.code == ErrorCode.PARAM_INVALID

    def test_subclasses_walk_blocked(self):
        with pytest.raises(PrecisError):
            _read(self.h, "Matrix([[1]]).__class__.__subclasses__()")

    def test_lambda_blocked(self):
        with pytest.raises(PrecisError) as exc:
            _read(self.h, "lambda x: x")
        assert exc.value.code == ErrorCode.PARAM_INVALID

    def test_comprehension_blocked(self):
        with pytest.raises(PrecisError):
            _read(self.h, "[i for i in range(5)]")

    def test_malformed_expression_gives_param_invalid(self):
        with pytest.raises(PrecisError) as exc:
            _read(self.h, "2 ++ 3 ++")
        assert exc.value.code == ErrorCode.PARAM_INVALID


class TestCalcAttribution:
    """Every response must include the local-compute footer.

    The footer is not legally required (no third-party data), but it
    tags the engine + version so the agent knows which tool produced
    the answer and can debug version-specific behaviour.
    """

    def setup_method(self):
        self.h = CalcHandler()

    def test_default_view_has_footer(self):
        out = _read(self.h, "2+2")
        assert "Computed locally by SymPy" in out

    def test_latex_view_has_footer(self):
        out = _read(self.h, "pi/latex")
        assert "Computed locally by SymPy" in out

    def test_numeric_view_has_footer(self):
        out = _read(self.h, "sqrt(2)/numeric")
        assert "Computed locally by SymPy" in out

    def test_pretty_view_has_footer(self):
        out = _read(self.h, "sqrt(2)/pretty")
        assert "Computed locally by SymPy" in out

    def test_help_view_has_footer(self):
        out = _read(self.h, "/help")
        assert "Computed locally by SymPy" in out

    def test_footer_includes_version(self):
        out = _read(self.h, "2+2")
        # Version string format: 1.14.0 etc.
        import re

        assert re.search(r"SymPy \d+\.\d+", out)


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestCalcRegistration:
    @classmethod
    def setup_class(cls):
        import precis.registry as reg

        reg._discover()

    def test_calc_kind_registered(self):
        assert "calc" in KINDS
        assert "calc" in SCHEMES

    def test_calc_description_marks_free(self):
        spec = KINDS["calc"].spec
        assert "FREE" in spec.description.upper()

    def test_calc_cost_hint_is_free(self):
        assert KINDS["calc"].spec.cost_hint == "free"

    def test_calc_has_no_env_requirements(self):
        assert KINDS["calc"].spec.requires == []

    def test_calc_onboarding_skill_points_at_basics(self):
        handler_cls = KINDS["calc"].handler_cls
        assert handler_cls.onboarding_skill == "calc-basics"

    def test_calc_has_examples(self):
        spec = KINDS["calc"].spec
        assert spec.examples
        assert any("integrate" in e for e in spec.examples)
        assert any("Matrix" in e for e in spec.examples)

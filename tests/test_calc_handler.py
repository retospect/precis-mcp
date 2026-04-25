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
    _check_unknown_names,
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
# Phase 6a — unknown-name check
# ---------------------------------------------------------------------------


class TestCheckUnknownNames:
    """Regression for the silent-nonsense bug where typos in calc
    expressions were exploded by ``implicit_multiplication_application``
    into a product of free symbols.

    Examples that used to silently succeed (with wrong answers)::

        calc:potato + 3      → a*o**2*p*t**2 + 3   (should error)
        calc:diff(sni(x))    → diff(s*n*i*x) ...   (should suggest sin)
        calc:my_avg          → my_avg              (free symbol — looks
                                                     correct but isn't
                                                     computed)

    The new ``_check_unknown_names`` rejects any pure-letter Name of
    length >= 2 that isn't in the calc namespace.
    """

    @property
    def _allowed(self) -> set[str]:
        # Real namespace — exercises the full allow-list rather than a
        # hand-rolled stub.  Avoids drift when sympy adds primitives.
        from precis.handlers.calc import _build_namespace

        local_dict, global_dict, _, _ = _build_namespace()
        return set(local_dict) | set(global_dict)

    def test_known_function_passes(self):
        _check_unknown_names("integrate(sin(x)*cos(x), x)", self._allowed)

    def test_known_constant_passes(self):
        _check_unknown_names("pi + E*I", self._allowed)

    def test_single_letter_symbols_pass(self):
        # Single letters auto-promote to free Symbols even without
        # being in the namespace — the existing ``2x + 3`` behaviour
        # must keep working.
        _check_unknown_names("2q + 3w", self._allowed)
        _check_unknown_names("u*v", self._allowed)

    def test_letters_with_digits_pass(self):
        # Implicit multiplication handles ``x1`` as ``x*1``; we leave
        # these alone so the user doesn't have to declare ``x1``.
        _check_unknown_names("x1 + y2", self._allowed)

    def test_underscored_name_passes(self):
        # Underscored names are intentional symbol identifiers; the
        # check skips them rather than rejecting valid declarations.
        _check_unknown_names("my_var + 1", self._allowed)

    def test_unknown_pure_letter_typo_rejected(self):
        # The bug from the critic: ``potato + 3`` would otherwise
        # return ``a*o**2*p*t**2 + 3``.
        with pytest.raises(PrecisError) as exc:
            _check_unknown_names("potato + 3", self._allowed)
        assert exc.value.code == ErrorCode.PARAM_INVALID
        assert "potato" in exc.value.cause
        # The error names the offending symbol and explains why.
        assert "implicit-multiplication" in exc.value.cause
        # Recovery hint shows how to declare the symbol if intentional.
        assert "Symbol(" in exc.value.next

    def test_typo_with_suggestion(self):
        # ``sni`` is a typo of ``sin``; difflib should surface it.
        with pytest.raises(PrecisError) as exc:
            _check_unknown_names("diff(sni(x))", self._allowed)
        assert "sni" in exc.value.cause
        assert "Did you mean:" in exc.value.cause
        assert "sin" in exc.value.cause

    def test_one_unknown_drives_message(self):
        # When an expression has multiple unknowns we report exactly
        # one — the agent fixes typos one at a time.  ast.walk visits
        # in BFS order, so the *order* isn't left-to-right and we don't
        # pin a specific suspect; we just assert one is named and the
        # others don't leak into the message.
        #
        # Names chosen so they're NOT near-matches of each other —
        # otherwise difflib would surface a sibling unknown in the
        # suggestion list and confuse the assertion.
        with pytest.raises(PrecisError) as exc:
            _check_unknown_names(
                "xylophone + qwertyplant + kazoo", self._allowed
            )
        names_in_cause = [
            n for n in ("xylophone", "qwertyplant", "kazoo")
            if n in exc.value.cause
        ]
        assert len(names_in_cause) == 1, (
            f"expected exactly one unknown surfaced, got {names_in_cause!r}"
        )

    def test_syntax_error_deferred(self):
        # Malformed input doesn't blow up the unknown-name check;
        # parse_expr produces the better error.
        _check_unknown_names("2 ++ 3", self._allowed)  # no raise


class TestCalcUnknownNameIntegration:
    """End-to-end through the handler — guards against any future
    refactor that drops the ``_check_unknown_names`` call from the
    dispatch flow.
    """

    def setup_method(self):
        self.h = CalcHandler()

    def test_typo_no_longer_silently_explodes(self):
        # The previous bug returned ``a*o**2*p*t**2 + 3`` with no error
        # at all.  The handler now raises ``PARAM_INVALID`` instead.
        # ``handler.read`` propagates PrecisError directly; the
        # ``ERROR […]`` envelope rendering happens further up the
        # dispatch stack in ``invoke_handler``.
        with pytest.raises(PrecisError) as exc:
            _read(self.h, "potato + 3")
        assert exc.value.code == ErrorCode.PARAM_INVALID
        assert "potato" in exc.value.cause
        # And critically, the bogus expansion does NOT appear anywhere.
        assert "a*o" not in exc.value.cause
        assert "p*t" not in exc.value.cause

    def test_typo_with_close_match_suggests(self):
        with pytest.raises(PrecisError) as exc:
            _read(self.h, "diff(sni(x))")
        assert exc.value.code == ErrorCode.PARAM_INVALID
        assert "Did you mean" in exc.value.cause
        assert "sin" in exc.value.cause

    def test_implicit_multiplication_still_works(self):
        # The fix must not break the documented ``2x + 3y`` syntax.
        out = _read(self.h, "2x + 3y")
        assert "ERROR" not in out
        assert "2*x + 3*y" in out

    def test_known_compound_expression_still_works(self):
        out = _read(self.h, "integrate(sin(x)*cos(x), x)")
        assert "ERROR" not in out
        assert "sin(x)" in out


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


# ---------------------------------------------------------------------------
# Server-level — comma-split must skip calc; search() must not pass top_k
# ---------------------------------------------------------------------------


class TestCalcCommaNotSplit:
    """Regression for the server-side comma-split bug.

    ``get(id='integrate(sin(x), x)', type='calc')`` was being torn apart
    at the comma into two ids: ``['integrate(sin(x)', 'x)']``.  The
    first failed parsing as a calc expression; the second was sent to
    the dispatcher as a separate call.  Symptoms: garbled multi-id
    output instead of a clean integration result.

    Fix: ``_supports_comma_batch`` returns False for compute / external
    kinds (``calc``, ``math``, ``websearch``, …).
    """

    def test_calc_with_comma_argument_routes_as_single_call(self):
        from precis import server

        out = server.get(id="integrate(sin(x), x)", type="calc")
        # Real result of the integration: -cos(x).  We don't pin the
        # exact rendering (depends on view/pretty mode) — just assert
        # the expression evaluated and we got a sensible cos-bearing
        # output, NOT a multi-id error or split-fragment failure.
        assert "cos(x)" in out, f"unexpected output: {out!r}"
        # Multi-id batch would dispatch twice and produce two ``Exact:``
        # blocks joined by the multi-id separator.  The single-id path
        # produces exactly one.  ``\n---\n`` alone is too weak — calc's
        # own footer uses it — so count the per-result marker instead.
        assert out.count("Exact:") <= 1, (
            f"calc was split into multi-id batch: {out!r}"
        )
        # And no error fragments from the failed-parse first half.
        assert "ERROR [param_invalid]" not in out
        assert "ERROR [id_malformed]" not in out

    def test_math_with_comma_argument_routes_as_single_call(self, monkeypatch):
        # math (Wolfram) handler may not be configured locally; mock
        # tools.read so we can verify the URI handed to it.
        from precis import server

        captured: dict[str, object] = {}

        def fake_read(uri, **kwargs):
            captured["uri"] = uri
            captured.update(kwargs)
            return "OK"

        monkeypatch.setattr(server.tools, "read", fake_read)
        out = server.get(id="population of Ireland, in millions", type="math")
        assert "ERROR [" not in out
        # The whole opaque expression (with comma) reaches the handler.
        assert captured["uri"] == "math:population of Ireland, in millions"

    def test_paper_batch_still_works(self, monkeypatch):
        """Sanity: comma-split must still fire for ref-backed kinds.

        We're guarding against an over-broad fix where 'no commas
        anywhere' would break the documented batch syntax for papers.
        """
        from precis import server

        calls: list[str] = []

        def fake_read(uri, **kwargs):
            calls.append(uri)
            return f"[stub for {uri}]"

        monkeypatch.setattr(server.tools, "read", fake_read)
        out = server.get(id="ni2024atomic,wang2020state", type="paper")
        # Two separate dispatches happened.
        assert len(calls) == 2, f"expected 2 dispatches, got {len(calls)}"
        assert "paper:ni2024atomic" in calls
        assert "paper:wang2020state" in calls
        # Output stitches them together with the batch separator.
        assert "\n---\n" in out


class TestSearchCalcDoesNotCrashOnTopK:
    """Regression for the TypeError when ``search(type='calc', query=…)``
    forwarded ``top_k`` to ``CalcHandler.read`` whose signature did not
    declare it.

    Fix: the server's ``search`` router detects compute/external kinds
    (in ``_SEARCH_INCOMPATIBLE_KINDS``) and dispatches without ``top_k``.
    """

    def test_search_calc_evaluates_query_as_expression(self):
        from precis import server

        out = server.search(query="2+3*4", type="calc", top_k=5)
        # Whatever the rendering, the answer 14 must appear.
        assert "14" in out, f"unexpected calc output: {out!r}"
        # Must not surface the historical TypeError envelope.
        assert "ERROR [unexpected]" not in out
        assert "top_k" not in out

    def test_search_calc_with_complex_expression_round_trips(self):
        from precis import server

        out = server.search(
            query="integrate(sin(x)*cos(x), x)", type="calc", top_k=5
        )
        # The integral of sin(x)*cos(x) is sin(x)**2/2 (or equivalent).
        # Either form is acceptable; both contain 'sin' and a '/2' or '**2'.
        assert "sin" in out
        assert "ERROR [unexpected]" not in out

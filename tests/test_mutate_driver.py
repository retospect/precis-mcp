"""Unit tests for ``scripts/lib/mutate_driver.py``.

No DB fixtures — must stay in the fast, no-Postgres set (`scripts/test
--fast`). Loaded via ``importlib.util.spec_from_file_location`` (the module
lives under ``scripts/``, outside the ``precis`` package, same pattern as
``tests/test_guard_secret_read.py`` for the hook scripts).
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from typing import Any

_DRIVER = Path(__file__).resolve().parents[1] / "scripts" / "lib" / "mutate_driver.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("mutate_driver", _DRIVER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec: the driver defines a frozen
    # dataclass, and Python 3.12's dataclass machinery resolves the class's
    # module via `sys.modules[cls.__module__]` while processing it — skip
    # this and it AttributeErrors on None at import time (the module isn't
    # found there yet).
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


md = _load()


# ── parse_patch ──────────────────────────────────────────────────────────


def test_hunk_header_plus_c_with_no_comma_d_is_one_line() -> None:
    patch = textwrap.dedent(
        """\
        diff --git a/src/precis/x.py b/src/precis/x.py
        --- a/src/precis/x.py
        +++ b/src/precis/x.py
        @@ -10 +10 @@
        -old
        +new
        """
    )
    assert md.parse_patch(patch) == {"src/precis/x.py": {10}}


def test_hunk_header_with_explicit_count() -> None:
    patch = textwrap.dedent(
        """\
        diff --git a/src/precis/x.py b/src/precis/x.py
        --- a/src/precis/x.py
        +++ b/src/precis/x.py
        @@ -5,0 +6,3 @@
        +a
        +b
        +c
        """
    )
    assert md.parse_patch(patch) == {"src/precis/x.py": {6, 7, 8}}


def test_hunk_with_d_zero_is_a_pure_deletion_and_contributes_no_lines() -> None:
    patch = textwrap.dedent(
        """\
        diff --git a/src/precis/x.py b/src/precis/x.py
        --- a/src/precis/x.py
        +++ b/src/precis/x.py
        @@ -10,3 +10,0 @@
        -a
        -b
        -c
        """
    )
    assert md.parse_patch(patch) == {"src/precis/x.py": set()}


def test_non_src_path_is_filtered_out() -> None:
    patch = textwrap.dedent(
        """\
        diff --git a/tests/test_x.py b/tests/test_x.py
        --- a/tests/test_x.py
        +++ b/tests/test_x.py
        @@ -1 +1 @@
        -old
        +new
        """
    )
    assert md.parse_patch(patch) == {}


def test_non_py_path_under_src_is_filtered_out() -> None:
    patch = textwrap.dedent(
        """\
        diff --git a/src/precis/data/x.sql b/src/precis/data/x.sql
        --- a/src/precis/data/x.sql
        +++ b/src/precis/data/x.sql
        @@ -1 +1 @@
        -old
        +new
        """
    )
    assert md.parse_patch(patch) == {}


def test_dev_null_target_is_dropped() -> None:
    patch = textwrap.dedent(
        """\
        diff --git a/src/precis/gone.py b/src/precis/gone.py
        --- a/src/precis/gone.py
        +++ /dev/null
        @@ -1,3 +0,0 @@
        -a
        -b
        -c
        """
    )
    assert md.parse_patch(patch) == {}


def test_multiple_hunks_same_file_union_lines() -> None:
    patch = textwrap.dedent(
        """\
        diff --git a/src/precis/x.py b/src/precis/x.py
        --- a/src/precis/x.py
        +++ b/src/precis/x.py
        @@ -1 +1 @@
        -a
        +a2
        @@ -20,2 +20,2 @@
        -b
        -c
        +b2
        +c2
        """
    )
    assert md.parse_patch(patch) == {"src/precis/x.py": {1, 20, 21}}


# ── context stripping / covering-test lookup ────────────────────────────


def test_strip_context_drops_pipe_run_suffix() -> None:
    assert md.strip_context("tests/test_x.py::test_y|run") == "tests/test_x.py::test_y"


def test_strip_context_of_bare_id_is_unchanged() -> None:
    assert md.strip_context("tests/test_x.py::test_y") == "tests/test_x.py::test_y"


def test_covering_tests_from_contexts_dedupes_and_drops_empty() -> None:
    contexts = {
        5: [
            "tests/test_x.py::test_y|run",
            "",
            "tests/test_x.py::test_y|run",
            "tests/test_x.py::test_z|run",
        ],
        6: [""],
    }
    assert md.covering_tests_from_contexts(contexts, 5) == [
        "tests/test_x.py::test_y",
        "tests/test_x.py::test_z",
    ]
    assert md.covering_tests_from_contexts(contexts, 6) == []
    assert md.covering_tests_from_contexts(contexts, 999) == []


class _FakeCoverageData:
    """Minimal stand-in for ``coverage.CoverageData`` — avoids standing up a
    real sqlite file just to exercise the lookup/resolution logic.
    """

    def __init__(self, files: dict[str, dict[int, list[str]]]) -> None:
        self._files = files

    def measured_files(self) -> set[str]:
        return set(self._files)

    def contexts_by_lineno(self, filename: str) -> dict[int, list[str]]:
        return self._files.get(filename, {})


def test_resolve_measured_file_exact_match() -> None:
    data = _FakeCoverageData({"src/precis/x.py": {}})
    assert md.resolve_measured_file(data, "src/precis/x.py") == "src/precis/x.py"


def test_resolve_measured_file_suffix_fallback() -> None:
    data = _FakeCoverageData({"/app/src/precis/x.py": {}})
    assert md.resolve_measured_file(data, "src/precis/x.py") == "/app/src/precis/x.py"


def test_resolve_measured_file_no_match_is_none() -> None:
    data = _FakeCoverageData({"src/precis/other.py": {}})
    assert md.resolve_measured_file(data, "src/precis/x.py") is None


def test_covering_tests_for_file() -> None:
    data = _FakeCoverageData(
        {
            "src/precis/x.py": {
                1: ["tests/test_x.py::test_a|run"],
                2: [""],
            }
        }
    )
    result = md.covering_tests_for_file(data, "src/precis/x.py")
    assert result == {1: ["tests/test_x.py::test_a"]}


def test_select_covering_tests_name_matched_test_beats_the_cap() -> None:
    """A test whose OWN file names the mutated module has to survive the
    cap even when it sorts after enough unrelated tests to fill it — the
    exact gr302974 miss: 5 other tests filled ``--max-tests`` and the
    name-matched killer never ran."""
    tests = [
        "tests/test_a.py::test_a",
        "tests/test_b.py::test_b",
        "tests/test_c.py::test_c",
        "tests/test_d.py::test_d",
        "tests/test_e.py::test_e",
        "tests/workers/test_pcb_route.py::test_routed_net_is_never_failed",
    ]
    selected = md.select_covering_tests(tests, "src/precis/pcb/pcb_route.py", 5)
    assert "tests/workers/test_pcb_route.py::test_routed_net_is_never_failed" in selected
    assert len(selected) == 5


def test_select_covering_tests_no_match_keeps_original_order() -> None:
    tests = ["tests/test_a.py::test_a", "tests/test_b.py::test_b"]
    assert md.select_covering_tests(tests, "src/precis/pcb/pcb_route.py", 5) == tests


def test_select_covering_tests_under_cap_is_unchanged() -> None:
    tests = ["tests/workers/test_pcb_route.py::test_x", "tests/test_a.py::test_a"]
    assert md.select_covering_tests(tests, "src/precis/pcb/pcb_route.py", 5) == tests


def test_has_any_test_context_true_when_a_real_context_exists() -> None:
    data = _FakeCoverageData({"src/precis/x.py": {1: ["tests/test_x.py::test_a|run"]}})
    assert md.has_any_test_context(data) is True


def test_has_any_test_context_false_when_only_empty_contexts() -> None:
    data = _FakeCoverageData({"src/precis/x.py": {1: [""], 2: [""]}})
    assert md.has_any_test_context(data) is False


# ── mutation operators ──────────────────────────────────────────────────


def _mutants(source: str, lines: set[int]) -> list[Any]:
    return md.generate_mutants(source, lines, path="<t>")


def _descriptions(source: str, lines: set[int]) -> list[str]:
    return [m.description for m in _mutants(source, lines)]


def test_compare_eq_flips_to_not_eq() -> None:
    src = "x = a == b\n"
    (m,) = _mutants(src, {1})
    assert md.apply_mutant(src, m) == "x = a != b\n"


def test_compare_lt_flips_to_gte() -> None:
    src = "x = a < b\n"
    (m,) = _mutants(src, {1})
    assert md.apply_mutant(src, m) == "x = a >= b\n"


def test_compare_is_not_flips_to_is() -> None:
    src = "x = a is not b\n"
    (m,) = _mutants(src, {1})
    assert md.apply_mutant(src, m) == "x = a is b\n"


def test_compare_not_in_flips_to_in() -> None:
    src = "x = a not in b\n"
    (m,) = _mutants(src, {1})
    assert md.apply_mutant(src, m) == "x = a in b\n"


def test_boolop_and_flips_to_or() -> None:
    src = "x = a and b\n"
    mutants = _mutants(src, {1})
    assert any("and -> or" in m.description for m in mutants)
    m = next(m for m in mutants if "and -> or" in m.description)
    assert md.apply_mutant(src, m).strip() == "x = a or b"


def test_boolop_or_flips_to_and() -> None:
    src = "x = a or b\n"
    mutants = _mutants(src, {1})
    m = next(m for m in mutants if "or -> and" in m.description)
    assert md.apply_mutant(src, m).strip() == "x = a and b"


def test_arith_plus_flips_to_minus() -> None:
    src = "x = a + b\n"
    (m,) = _mutants(src, {1})
    assert md.apply_mutant(src, m) == "x = a - b\n"


def test_arith_minus_flips_to_plus() -> None:
    src = "x = a - b\n"
    (m,) = _mutants(src, {1})
    assert md.apply_mutant(src, m) == "x = a + b\n"


def test_string_concat_plus_is_not_mutated() -> None:
    src = 'x = "a" + b\n'
    assert _mutants(src, {1}) == []


def test_list_concat_plus_is_not_mutated() -> None:
    # The `+` itself is skipped (list-literal operand) — but the `1` inside
    # the list is its own, unrelated int-constant mutant, which IS expected
    # (int constants are mutated unconditionally per generate_mutants' spec).
    src = "x = [1] + b\n"
    assert all("arith" not in m.description for m in _mutants(src, {1}))


def test_unary_not_removes_not() -> None:
    src = "x = not flag\n"
    (m,) = _mutants(src, {1})
    assert md.apply_mutant(src, m) == "x = flag\n"


def test_bool_constant_true_flips_to_false() -> None:
    src = "x = True\n"
    (m,) = _mutants(src, {1})
    assert md.apply_mutant(src, m) == "x = False\n"


def test_bool_constant_false_flips_to_true() -> None:
    src = "x = False\n"
    (m,) = _mutants(src, {1})
    assert md.apply_mutant(src, m) == "x = True\n"


def test_int_constant_increments() -> None:
    src = "x = 5\n"
    (m,) = _mutants(src, {1})
    assert md.apply_mutant(src, m) == "x = 6\n"


def test_bool_constant_is_not_int_mutated() -> None:
    # A bare `True`/`False` line must produce exactly the bool mutant, not
    # also an int-increment mutant (bool is an int subclass in the AST).
    src = "x = True\n"
    descriptions = _descriptions(src, {1})
    assert descriptions == ["const True -> False"]


def test_str_constant_is_never_mutated() -> None:
    src = 'x = "hello"\n'
    assert _mutants(src, {1}) == []


def test_float_constant_is_never_mutated() -> None:
    src = "x = 1.5\n"
    assert _mutants(src, {1}) == []


def test_break_flips_to_continue() -> None:
    src = "for i in x:\n    break\n"
    (m,) = _mutants(src, {2})
    assert md.apply_mutant(src, m) == "for i in x:\n    continue\n"


def test_continue_flips_to_break() -> None:
    src = "for i in x:\n    continue\n"
    (m,) = _mutants(src, {2})
    assert md.apply_mutant(src, m) == "for i in x:\n    break\n"


def test_only_changed_lines_produce_mutants() -> None:
    src = "x = a == b\ny = c == d\n"
    mutants = _mutants(src, {1})
    assert {m.lineno for m in mutants} == {1}


def test_multiline_node_is_skipped() -> None:
    src = "x = (\n    a\n    == b\n)\n"
    # The Compare node spans lines 2-3 — _single_line_span rejects it, so no
    # mutant is produced even though line 3 is "changed".
    assert _mutants(src, {2, 3}) == []


def test_all_generated_mutants_compile() -> None:
    src = textwrap.dedent(
        """\
        def f(a, b, flag):
            if a == b and not flag:
                return a + b
            for i in range(3):
                if i > 2:
                    break
                else:
                    continue
            return True
        """
    )
    lines = set(range(1, src.count("\n") + 1))
    mutants = md.generate_mutants(src, lines, path="<t>")
    assert mutants  # sanity: this fixture should produce several
    for m in mutants:
        mutated = md.apply_mutant(src, m)
        compile(mutated, "<t>", "exec")  # raises SyntaxError if broken


# ── selection ────────────────────────────────────────────────────────────


def _mk(path: str, lineno: int, desc: str) -> Any:
    return md.Mutant(path, lineno, 0, 1, "a", "b", desc)


def test_select_mutants_round_robins_across_files() -> None:
    by_file = {
        "src/precis/a.py": [
            _mk("src/precis/a.py", 1, "d1"),
            _mk("src/precis/a.py", 2, "d2"),
        ],
        "src/precis/b.py": [_mk("src/precis/b.py", 1, "d1")],
    }
    selected = md.select_mutants(by_file, max_mutants=100)
    paths = [m.path for m in selected]
    assert paths == ["src/precis/a.py", "src/precis/b.py", "src/precis/a.py"]


def test_select_mutants_caps_at_max() -> None:
    by_file = {
        "src/precis/a.py": [_mk("src/precis/a.py", i, f"d{i}") for i in range(5)],
        "src/precis/b.py": [_mk("src/precis/b.py", i, f"d{i}") for i in range(5)],
    }
    selected = md.select_mutants(by_file, max_mutants=3)
    assert len(selected) == 3


def test_select_mutants_is_deterministic() -> None:
    by_file = {
        "src/precis/z.py": [
            _mk("src/precis/z.py", 3, "c"),
            _mk("src/precis/z.py", 1, "a"),
        ],
        "src/precis/a.py": [_mk("src/precis/a.py", 1, "a")],
    }
    first = md.select_mutants(by_file, max_mutants=10)
    second = md.select_mutants(by_file, max_mutants=10)
    assert [(m.path, m.lineno, m.description) for m in first] == [
        (m.path, m.lineno, m.description) for m in second
    ]
    # a.py sorts before z.py, and z.py's own mutants sort by lineno.
    assert [(m.path, m.lineno) for m in first] == [
        ("src/precis/a.py", 1),
        ("src/precis/z.py", 1),
        ("src/precis/z.py", 3),
    ]


# ── classification ───────────────────────────────────────────────────────


def test_classify_rc_zero_is_survived() -> None:
    assert md.classify(0) == "SURVIVED"


def test_classify_rc_one_is_killed() -> None:
    assert md.classify(1) == "KILLED"


def test_classify_timeout_is_killed() -> None:
    assert md.classify(-1, timed_out=True) == "KILLED"


def test_classify_rc_five_is_skipped() -> None:
    assert md.classify(5) == "SKIPPED"


def test_classify_rc_two_is_skipped() -> None:
    assert md.classify(2) == "SKIPPED"

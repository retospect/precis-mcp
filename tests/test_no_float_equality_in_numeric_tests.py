"""Ban ``== 0.0`` (and friends) on floats in the numeric test suites.

Slice 1 lost two rounds to this exact shape. A degeneracy test asserted
``area == 0.0`` and passed vacuously: the degenerate triangles have areas
around 9e-23 -- twenty orders of magnitude below the 1e-3 median, and
never bit-exactly zero. Independently, the sampling tie-break it was
guarding had the same flaw, because symmetric grid points cancel
algebraically to ~1e-16 rather than to 0.0.

Floating-point results land near a value, not on it. An exact-equality
assertion against a float literal therefore tests almost nothing while
looking like the strictest check in the file, which is the worst
combination a test can have. Use ``pytest.approx``, an explicit
tolerance, or a scale-relative bound.

Scoped to the numeric suites rather than the whole tree: exact equality
is perfectly reasonable for a parsed literal, a serialised round-trip, or
an integer-valued float, and this rule would only generate noise there.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

#: Suites where every float is the output of a computation.
WATCHED = ("test_precis_surface_",)

_ALLOW = "float-eq-ok:"  # trailing comment opts a line out, with a reason


def _watched_files() -> list[pathlib.Path]:
    here = pathlib.Path(__file__).parent
    return sorted(
        p
        for p in here.glob("test_*.py")
        if p.name.startswith(WATCHED) and p.name != pathlib.Path(__file__).name
    )


def test_watched_files_exist() -> None:
    """Guards the guard: a rename that empties the glob would make this
    file pass forever while checking nothing."""
    assert _watched_files(), f"no test files match {WATCHED!r} -- rule is dead"


@pytest.mark.parametrize("path", _watched_files(), ids=lambda p: p.name)
def test_no_exact_float_comparison(path: pathlib.Path) -> None:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source, filename=str(path))

    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for op, comparator in zip(node.ops, node.comparators):
            if not isinstance(op, (ast.Eq, ast.NotEq)):
                continue
            operands = [node.left, comparator]
            if not any(
                isinstance(o, ast.Constant) and isinstance(o.value, float)
                for o in operands
            ):
                continue
            line = lines[node.lineno - 1]
            if _ALLOW in line:
                continue
            bad.append(f"{path.name}:{node.lineno}: {line.strip()}")

    assert not bad, (
        "exact float equality in a numeric test -- these pass vacuously when "
        "the computed value lands near the literal instead of on it. Use "
        "pytest.approx, an explicit tolerance, or a scale-relative bound "
        f"(or append a `# {_ALLOW} <reason>` comment if it is genuinely "
        "exact):\n  " + "\n  ".join(bad)
    )

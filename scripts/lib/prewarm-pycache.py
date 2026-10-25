"""scripts/lib/prewarm-pycache.py — pre-compile noisy deps' .pyc files.

pysbd ships invalid escape sequences that fire a SyntaxWarning at
compile time. The dev image sets PYTHONDONTWRITEBYTECODE=1, so no .pyc
is ever written: every process that imports it recompiles and re-prints
the warnings — once in the pytest controller and once per xdist worker —
above the result on every ``scripts/test`` run (gr514615). The
``-p no:warnings`` pytest flag can't help: the noise happens at
import-compile time, before pytest's warnings machinery exists.

py_compile (what compileall drives) writes the .pyc regardless of
``sys.dont_write_bytecode``, so compiling the package once here — inside
the same ``uv run`` env pytest then execs — makes the controller's and
every worker's later import read the cached bytecode: zero warnings, and
no interpreter-wide warnings filter that would also hide a real invalid
escape in first-party code.

Usage: ``prewarm-pycache.py <top-level module or package> […]``.
Non-fatal by design — a removed dep or an unwritable venv just means the
old noise returns; the test run must not pay for a cosmetic step.
"""

from __future__ import annotations

import compileall
import importlib.util
import sys
import warnings
from pathlib import Path


def _source_root(name: str) -> Path | None:
    """Locate a top-level module or package without importing it (an
    import would trigger the very compile warnings this script silences)."""
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, AttributeError, ValueError):
        return None
    if spec is None:
        return None
    if spec.submodule_search_locations:
        return Path(spec.submodule_search_locations[0])
    if spec.origin is not None:
        return Path(spec.origin)
    return None


def main() -> int:
    warnings.simplefilter("ignore", SyntaxWarning)
    for name in sys.argv[1:]:
        root = _source_root(name)
        if root is None:
            continue
        ok = (
            compileall.compile_dir(str(root), quiet=1, maxlevels=20)
            if root.is_dir()
            else compileall.compile_file(str(root), quiet=1)
        )
        if not ok:
            print(
                f"prewarm-pycache: compile failed for {name} ({root})",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())

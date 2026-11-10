"""gr568182: the jo482106 anchor-key pin's engine label must name the
autocatpath version uv.lock actually pins.

``runner.coverage_anchor_key`` folds ``autocatpath==<version>`` into the
key, so the pinned hash is reproducible only when the label matches the
locked engine. The pin test reads the label through the runner's
version seam (gr596075), which makes it immune to a host whose installed
autocatpath drifted from uv.lock — the failure gr568182 measured by hand
(installed 0.43.0 computing 3ec4b5c6… against a pin derived for the
lock's 0.45.1, f3cf3ae8…). What that seam cannot catch is the mirror
drift: an engine bump updates uv.lock but nobody re-derives the label
and the key, leaving a pin no synced host can reproduce — red only on an
engine host running the coverage suite. This guard is engine-free: it
compares the ``JO482106_ENGINE`` literal in
``tests/test_coverage_job.py`` against the version uv.lock resolves, so
the label/lock mismatch fails on every lane, including the no-catalyst
CI one.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PIN_FILE = _REPO / "tests" / "test_coverage_job.py"


def _pin_label() -> str:
    """The ``JO482106_ENGINE`` literal, read by AST so the guard tracks the
    pin even if the label stops being a plain module-level assign."""
    tree = ast.parse(_PIN_FILE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "JO482106_ENGINE"
            for target in node.targets
        ):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
    raise AssertionError(
        "JO482106_ENGINE literal not found in tests/test_coverage_job.py"
    )


def _locked_engine_version() -> str:
    data = tomllib.loads((_REPO / "uv.lock").read_text(encoding="utf-8"))
    for pkg in data.get("package", []):
        if pkg.get("name") == "autocatpath":
            return str(pkg.get("version", ""))
    raise AssertionError("autocatpath not resolved in uv.lock")


def test_jo482106_engine_label_matches_the_lock() -> None:
    """An autocatpath bump must re-derive the pin AND its label, from the
    same lock — never one without the other (gr568182, gr596075)."""
    assert _pin_label() == _locked_engine_version()

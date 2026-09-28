"""precis_chain imports nothing from precis — the kernel boundary.

Same approach, and the same reason, as
``tests/test_hexfold_import_boundary.py``: an AST walk is the cheap check that
cannot be fooled by a lazy import inside a function (which an import-linter
contract over the static graph also catches, but which a runtime import test
would miss entirely).

Why it matters here: ``src/precis_chain/`` is the pure-numpy polymer-chain
kernel (``docs/backlog/precis-chain-kernel.md``). Its whole value is that a
consumer — the ``se`` nucleic-acid binding today, a protein importer next, a
standalone pip later — can use it with numpy and nothing else. One
``from precis.structure...`` import anywhere under the package silently drags
in psycopg, the store and the handler registry, and the boundary is gone
without any test failing.

The ban covers ``precis*`` deliberately widely: not just ``precis`` itself but
``precis_se``, ``precis_chem`` and every other in-tree plugin package, none of
which are import-linter root packages, so this test is the only thing watching
those edges.
"""

from __future__ import annotations

import ast
from pathlib import Path

_CHAIN_SRC = Path(__file__).resolve().parent.parent / "src" / "precis_chain"

#: Prefixes precis_chain must never import — everything first-party in this
#: monorepo. ``precis_surface`` and ``hexfold`` are covered by the ``precis``
#: prefix and by their own boundary conventions respectively; the kernel has no
#: business importing either.
_FORBIDDEN_PREFIXES = ("precis.", "precis_", "asa_bot", "asa_slack", "hexfold")

#: The package's own name — ``from precis_chain.path import Path`` is how every
#: module here reaches its siblings, and it starts with a forbidden prefix.
_SELF = "precis_chain"


def _is_forbidden(module: str | None) -> bool:
    if module is None:
        return False
    if module == _SELF or module.startswith(f"{_SELF}."):
        return False
    return module.startswith(_FORBIDDEN_PREFIXES) or module == "precis"


def test_precis_chain_imports_nothing_first_party() -> None:
    offenders: list[str] = []
    for path in sorted(_CHAIN_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_forbidden(alias.name):
                        offenders.append(f"{path}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # a relative import can only reach siblings
                    continue
                if _is_forbidden(node.module):
                    offenders.append(f"{path}: from {node.module} import ...")
    assert not offenders, (
        "src/precis_chain must import nothing first-party (numpy-only kernel "
        f"boundary, docs/backlog/precis-chain-kernel.md): {offenders}"
    )


def test_precis_chain_third_party_imports_are_numpy_only() -> None:
    """The dependency claim in the package docstring, mechanised: the only
    non-stdlib import anywhere under ``src/precis_chain/`` is numpy. A new
    dependency here (scipy for a spline, trimesh for a sweep) is explicitly
    out of scope and would break the standalone-pip promise."""
    allowed = {"numpy"}
    stdlib_seen: set[str] = set()
    extras: set[str] = set()
    import sys

    for path in sorted(_CHAIN_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            for root in roots:
                if not root or root == _SELF:
                    continue
                if root in sys.stdlib_module_names:
                    stdlib_seen.add(root)
                elif root not in allowed:
                    extras.add(root)
    assert not extras, f"precis_chain gained a non-numpy dependency: {sorted(extras)}"
    assert stdlib_seen, "expected at least one stdlib import (sanity check)"

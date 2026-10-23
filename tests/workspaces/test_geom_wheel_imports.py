"""The ``precis-geom`` wheel imports nothing outside its allow-list.

``packaging/geom/pyproject.toml`` force-includes a *partial* ``precis``
package — only what the shipped modules use. Anything beyond the
allow-list below (``precis.store``, ``precis.utils.http``,
``precis.handlers`` …) would drag the DB stack into a ``--network none``
container. Walks the shipped sources with ``ast`` — no wheel build, no
imports — like ``test_hexfold_import_boundary`` but relative-import-aware.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _ROOT / "src"
_GEOM_TOML = _ROOT / "packaging" / "geom" / "pyproject.toml"

#: Directories shipped in the wheel → their import roots.
_SHIPPED_DIRS = [
    _SRC / "precis_geom",
    _SRC / "precis_ws",
    _SRC / "hexfold",
    _SRC / "precis_chain",
    _SRC / "precis_flow",
    _SRC / "precis" / "cad",
    _SRC / "precis" / "structsolve",
    _SRC / "precis" / "structure",
]

_SHIPPED_FILES = [
    _SRC / "precis" / "__init__.py",
    _SRC / "precis" / "errors.py",
    _SRC / "precis" / "component_series.py",
    _SRC / "precis" / "utils" / "__init__.py",
    _SRC / "precis" / "utils" / "units.py",
]

#: Excluded inside a shipped dir (fetch paths, dead under --network none).
_EXCLUDED = {_SRC / "precis" / "structure" / "importers"}

#: What wheel code may import (the wheel itself + its declared deps).
_ALLOW = (
    "precis.cad",
    "precis.structsolve",
    "precis.structure",
    "precis.errors",
    "precis.utils.units",
    "precis.component_series",
    "precis_chain",
    "precis_flow",
    "precis_geom",
    "precis_ws",
    "hexfold",
)

#: Wheel packages that are precis.* — a `precis.` import not under _ALLOW
#: is a leak (precis.store, precis.utils.*, precis.handlers, …).
_FORBIDDEN_PREFIX = "precis."


def _iter_files() -> list[Path]:
    files: list[Path] = list(_SHIPPED_FILES)
    for d in _SHIPPED_DIRS:
        for f in sorted(d.rglob("*.py")):
            if any(ex == f or ex in f.parents for ex in _EXCLUDED):
                continue
            files.append(f)
    return files


def _resolve(path: Path, node: ast.ImportFrom) -> str:
    """The absolute module a ``from … import`` names (relative-aware)."""
    if node.level == 0:
        return node.module or ""
    base = list(path.relative_to(_SRC).parent.parts)
    for _ in range(node.level - 1):
        base.pop()
    return ".".join(base + ([node.module] if node.module else []))


def _is_allowed(mod: str) -> bool:
    return any(mod == a or mod.startswith(a + ".") for a in _ALLOW)


def test_pyproject_lists_the_shipped_set() -> None:
    """The force-include map must name exactly the shipped dirs/files —
    a drift between it and this test silently widens the wheel."""
    text = _GEOM_TOML.read_text(encoding="utf-8")
    for d in _SHIPPED_DIRS:
        assert f'"../../{d.relative_to(_ROOT)}"' in text, d
    for f in _SHIPPED_FILES:
        assert f'"../../{f.relative_to(_ROOT)}"' in text, f
    for ex in _EXCLUDED:
        assert str(ex.relative_to(_SRC)) in text, ex


def test_wheel_import_graph() -> None:
    offenders: list[str] = []
    for path in _iter_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            mods: list[str] = []
            names: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [_resolve(path, node)]
                names = [a.name for a in node.names]
            for m in mods:
                top = m.split(".")[0]
                if top not in (
                    "precis",
                    "precis_geom",
                    "precis_ws",
                    "hexfold",
                    "precis_chain",
                    "precis_flow",
                ):
                    continue
                if _is_allowed(m):
                    continue
                # ``from precis import component_series`` names the leaf
                # module — check the imported names before flagging.
                if names and any(_is_allowed(f"{m}.{n}") for n in names):
                    continue
                offenders.append(f"{path.relative_to(_ROOT)}: {m}")
    assert not offenders, (
        "precis-geom wheel code must stay inside its allow-list "
        f"(no precis.store / utils.http / handlers): {offenders}"
    )


def test_wheel_is_dependency_light() -> None:
    """Module-level third-party imports stay inside the declared deps —
    everything heavier (torch/OCP/tblite/httpx) must stay lazy."""
    deps = {"numpy", "numba", "ase", "rdkit", "pint", "manifold3d", "scipy"}
    import sys

    stdlib = sys.stdlib_module_names
    first_party = {
        "precis",
        "precis_geom",
        "precis_ws",
        "hexfold",
        "precis_chain",
        "precis_flow",
    }
    bad: list[str] = []
    for path in _iter_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:  # module top level only
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                mods = [node.module or ""]
            elif isinstance(node, ast.ImportFrom):
                mods = [_resolve(path, node)]
            for m in mods:
                top = m.split(".")[0]
                if top not in stdlib and top not in deps and top not in first_party:
                    bad.append(f"{path.relative_to(_ROOT)}: {m}")
    assert not bad, f"undeclared top-level dep in wheel code: {bad}"

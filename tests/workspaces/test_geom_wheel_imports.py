"""The ``precis-geom`` wheel imports nothing outside its allow-list.

``packaging/geom/pyproject.toml`` force-includes a *partial* ``precis``
package — only what the shipped modules use. Anything beyond the
allow-list below (``precis.store``, ``precis.utils.http``,
``precis.handlers`` …) would drag the DB stack into a ``--network none``
container. ``precis_se`` is partial the same way: the scene generators
plus ``atomic/surface_target``, with a stub ``precis_se/__init__.py``
(``packaging/geom/precis_se/``) standing in for the real one, which
imports ``SeHandler`` and the DB stack. Walks the shipped sources with
``ast`` — no wheel build, no imports — like
``test_hexfold_import_boundary`` but relative-import-aware.
"""

from __future__ import annotations

import ast
import importlib
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _ROOT / "src"
_GEOM_TOML = _ROOT / "packaging" / "geom" / "pyproject.toml"

#: ``[tool.hatch.build.targets.wheel]`` straight from the pyproject —
#: the one place that decides what the wheel contains, so the checks
#: derived from it can't drift from the build.
_WHEEL = tomllib.loads(_GEOM_TOML.read_text(encoding="utf-8"))["tool"]["hatch"][
    "build"
]["targets"]["wheel"]

#: Top-level packages the wheel installs — the first segment of every
#: force-include target. Derived, not hand-kept: a hand-kept tuple here
#: missed ``precis_se`` and ``precis_surface``, so a first-party import
#: the scan wasn't even looking at passed silently (gr514168).
_WHEEL_TOPS = {dest.split("/")[0] for dest in _WHEEL["force-include"].values()}

#: The wheel's ``precis_se/__init__.py``: the stub under packaging/,
#: not ``src/precis_se/__init__.py`` (that one imports ``SeHandler``
#: and the DB stack).
_SE_STUB = _ROOT / "packaging" / "geom" / "precis_se" / "__init__.py"

#: Repo source path → wheel destination, from the force-include map —
#: keys resolve against ``packaging/geom`` so shipped sources outside
#: ``src/`` (the stub) map to where they actually install.
_SRC_TO_WHEEL = {
    (_GEOM_TOML.parent / k).resolve(): dest
    for k, dest in _WHEEL["force-include"].items()
}

#: Directories shipped in the wheel → their import roots.
_SHIPPED_DIRS = [
    _SRC / "precis_geom",
    _SRC / "precis_ws",
    _SRC / "hexfold",
    _SRC / "precis_chain",
    _SRC / "precis_flow",
    _SRC / "precis_surface",
    _SRC / "precis" / "cad",
    _SRC / "precis" / "structsolve",
    _SRC / "precis" / "structure",
    _SRC / "precis_se" / "atomic" / "generators",
    _SRC / "precis" / "pcb",
    _SRC / "precis" / "format",
]

_SHIPPED_FILES = [
    _SRC / "precis" / "__init__.py",
    _SRC / "precis" / "errors.py",
    _SRC / "precis" / "component_series.py",
    _SRC / "precis" / "finding_id.py",
    _SRC / "precis" / "fit_classes.py",
    _SRC / "precis" / "utils" / "__init__.py",
    _SRC / "precis" / "utils" / "units.py",
    _SRC / "precis_se" / "atomic" / "__init__.py",
    _SRC / "precis_se" / "atomic" / "surface_target.py",
    #: The wheel's ``precis_se/__init__.py`` is the stub under packaging/,
    #: not ``src/precis_se/__init__.py`` (that one imports ``SeHandler``).
    _SE_STUB,
    _SRC / "precis" / "utils" / "text.py",
    _SRC / "precis" / "utils" / "coerce.py",
]

#: Excluded inside a shipped dir (fetch paths, dead under --network none;
#: and, for ``precis/pcb``, the store/network/server-side modules the
#: ``[pcb-kernel]`` slice of the wheel never carries — see
#: ``packaging/geom/pyproject.toml``'s exclude list, which must name the
#: same set).
_EXCLUDED = {_SRC / "precis" / "structure" / "importers"} | {
    _SRC / "precis" / "pcb" / name
    for name in (
        "_http.py",
        "argue.py",
        "bench.py",
        "catalog.py",
        "check_run.py",
        "datasheet_facts.py",
        "datasheets.py",
        "easyeda.py",
        "footprint.py",
        "jlc_api.py",
        "jlc_library.py",
        "jlc_order.py",
        "review.py",
        "sanity_board.py",
        "sanity_power.py",
        "sanity_upload.py",
        "session_store.py",
        "snapshot.py",
    )
}

#: What wheel code may import (the wheel itself + its declared deps).
#: ``precis_se.atomic`` is NOT allowed wholesale — the shipped slice is
#: generators + surface_target; bind/apply/generate (the store-aware
#: modules) must never be reachable from wheel code.
_ALLOW = (
    "precis.cad",
    "precis.structsolve",
    "precis.structure",
    "precis.pcb",
    "precis.format",
    "precis.errors",
    "precis.finding_id",
    "precis.utils.units",
    "precis.utils.text",
    "precis.utils.coerce",
    "precis.component_series",
    "precis.fit_classes",
    "precis_chain",
    "precis_flow",
    "precis_geom",
    "precis_ws",
    "hexfold",
    "precis_surface",
    "precis_se.atomic.generators",
    "precis_se.atomic.surface_target",
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


def _wheel_path(path: Path) -> Path | None:
    """The path ``path`` installs at inside the wheel, per the
    force-include map — or ``None`` when nothing ships it."""
    for src, dest in _SRC_TO_WHEEL.items():
        if path == src or src in path.parents:
            return Path(dest) / path.relative_to(src)
    return None


def _resolve(path: Path, node: ast.ImportFrom) -> str:
    """The absolute module a ``from … import`` names (relative-aware).

    Relative imports resolve against the file's location *inside the
    wheel*, not its repo path — a shipped source outside ``src/`` (the
    ``packaging/geom`` ``precis_se`` stub, installed as
    ``precis_se/__init__.py``) would otherwise crash on
    ``relative_to(_SRC)`` (gr514169)."""
    if node.level == 0:
        return node.module or ""
    wheel = _wheel_path(path)
    if wheel is None:
        raise AssertionError(
            f"{path}: shipped source with no force-include entry — "
            "test_pyproject_lists_the_shipped_set should have caught this"
        )
    base = list(wheel.parent.parts)
    for _ in range(node.level - 1):
        base.pop()
    return ".".join(base + ([node.module] if node.module else []))


def _is_allowed(mod: str) -> bool:
    return any(mod == a or mod.startswith(a + ".") for a in _ALLOW)


def _module_source(mod: str) -> Path | None:
    """The repo file a wheel-visible module name resolves to — the
    packaging stub for ``precis_se`` itself (the real
    ``src/precis_se/__init__.py`` is not shipped), else the ``src/``
    file; ``None`` for symbols and nonexistent modules."""
    if mod == "precis_se":
        return _SE_STUB
    parts = mod.split(".")
    for cand in (
        _SRC.joinpath(*parts).with_suffix(".py"),
        _SRC.joinpath(*parts, "__init__.py"),
    ):
        if cand.is_file():
            return cand
    return None


def _is_shipped(path: Path) -> bool:
    """Whether ``path`` is one of the files the wheel ships."""
    if path in _SHIPPED_FILES:
        return True
    if any(ex == path or ex in path.parents for ex in _EXCLUDED):
        return False
    return any(d == path or d in path.parents for d in _SHIPPED_DIRS)


def test_pyproject_lists_the_shipped_set() -> None:
    """The force-include map must name exactly the shipped dirs/files —
    a drift between it and this test silently widens the wheel.

    Keys are relative to ``packaging/geom``: a source inside it (the
    ``precis_se`` stub) takes a plain project-relative key, everything
    else ``../../…``. Asserting the keys as map keys, not substrings —
    a uniform ``../../`` prefix leaves the project dir and re-enters it
    (gr514167), and the stub's wheel target would hide that."""
    includes = _WHEEL["force-include"]
    for p in [*_SHIPPED_DIRS, *_SHIPPED_FILES]:
        key = os.path.relpath(p, _GEOM_TOML.parent)
        assert key in includes, p
    for ex in _EXCLUDED:
        assert str(ex.relative_to(_SRC)) in _WHEEL["exclude"], ex


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
                if top not in _WHEEL_TOPS:
                    continue
                # ``from precis import component_series`` names the leaf
                # module — check the imported names before flagging.
                leaf_allowed = names and any(_is_allowed(f"{m}.{n}") for n in names)
                if not _is_allowed(m) and not leaf_allowed:
                    offenders.append(f"{path.relative_to(_ROOT)}: {m}")
                    continue
                # The allow-list is a prefix gate; the wheel must also
                # *contain* the module. A lazy in-function import of an
                # allowed-but-unshipped file (gr514166:
                # precis_se.atomic.surface_target) still raises
                # ModuleNotFoundError at call time — the same hole a
                # static scan misses when it only reads module-level
                # imports.
                src = _module_source(m)
                if src is None or not _is_shipped(src):
                    state = "not shipped" if src else "does not resolve"
                    offenders.append(f"{path.relative_to(_ROOT)}: {m} {state}")
                    continue
                for n in names:
                    leaf = _module_source(f"{m}.{n}")
                    if leaf is not None and not _is_shipped(leaf):
                        offenders.append(
                            f"{path.relative_to(_ROOT)}: {m}.{n} not shipped"
                        )
    assert not offenders, (
        "precis-geom wheel code must stay inside its allow-list "
        f"(no precis.store / utils.http / handlers): {offenders}"
    )


def test_wheel_is_dependency_light() -> None:
    """Module-level third-party imports stay inside the declared deps —
    everything heavier (torch/OCP/tblite/httpx) must stay lazy."""
    # ``shapely`` is the pcb-kernel extra's one declared dep — declared,
    # albeit optional, so module-level use is honest (a base install
    # without the extra hits it only on a kernel path).
    deps = {"numpy", "numba", "ase", "rdkit", "pint", "manifold3d", "scipy", "shapely"}
    import sys

    stdlib = sys.stdlib_module_names
    first_party = _WHEEL_TOPS
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


def test_resolve_relative_import_against_wheel_location() -> None:
    """gr514169: a relative import in a shipped source outside ``src/``
    resolves against where it installs in the wheel — the ``precis_se``
    stub installs as ``precis_se/__init__.py``, so ``from . import
    atomic`` there names ``precis_se``, not a ``relative_to`` crash."""
    node = ast.parse("from . import atomic").body[0]
    assert isinstance(node, ast.ImportFrom)
    assert _resolve(_SE_STUB, node) == "precis_se"

    node = ast.parse("from .surface_target import decode_target").body[0]
    assert isinstance(node, ast.ImportFrom)
    assert (
        _resolve(_SRC / "precis_se" / "atomic" / "__init__.py", node)
        == "precis_se.atomic.surface_target"
    )


def test_imports_must_resolve_into_the_shipped_set() -> None:
    """gr514166: the allow-list is a prefix gate, not a contents check —
    an allowed-but-unshipped import (the ``surface_target`` hole:
    imported lazily but unconditionally, so a wheel without the file
    still raises ``ModuleNotFoundError`` at call time) must flag."""
    # The stub, not the real src/precis_se/__init__.py, is what
    # ``import precis_se`` resolves inside the wheel.
    assert _module_source("precis_se") == _SE_STUB
    # Allow-listed AND shipped.
    shipped = _module_source("precis_se.atomic.surface_target")
    assert shipped is not None and _is_shipped(shipped)
    # Allow-listed by the ``precis.structure`` prefix but excluded from
    # the wheel — the gap the coverage check closes.
    excluded = _module_source("precis.structure.importers")
    assert excluded is not None and not _is_shipped(excluded)
    # Exists in src, absent from the wheel.
    unshipped = _module_source("precis_se.handler")
    assert unshipped is not None and not _is_shipped(unshipped)
    # A nonexistent module doesn't resolve at all.
    assert _module_source("precis.cad.does_not_exist") is None


# ---------------------------------------------------------------------------
# gr514458: the ``pcb.local`` lazy alias must lint clean — agents lint
# their own scripts, and ``from precis_geom.pcb import local`` must not
# read as a gotcha.
# ---------------------------------------------------------------------------

_PCB_SRC = _SRC / "precis_geom" / "pcb.py"


def test_pcb_local_alias_declared_for_checkers() -> None:
    """A lazy alias whose target can't be imported (the stdlib-only belt
    in ``test_pcb_board`` walks ``TYPE_CHECKING`` blocks too) is
    *declared*, not imported, so ``from precis_geom.pcb import local``
    type-checks in an agent's own lint. ``local`` stays out of
    ``__all__``: a star-import on a wheel without ``[pcb-kernel]`` would
    call ``_kernel()`` and raise the install hint."""
    tree = ast.parse(_PCB_SRC.read_text(encoding="utf-8"), filename=str(_PCB_SRC))
    declared = {
        node.target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert "local" in declared, (
        "precis_geom.pcb must declare ``local`` under TYPE_CHECKING so "
        "the lazy alias type-checks"
    )
    star_names = {
        e.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
        for e in (node.value.elts if isinstance(node.value, ast.List) else [])
        if isinstance(e, ast.Constant)
    }
    assert "local" not in star_names


def test_pcb_local_alias_resolves_or_names_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``from precis_geom.pcb import local`` works with ``[pcb-kernel]``
    and raises the install hint without it."""
    import precis_geom.pcb as pcb

    local = pcb.local
    assert local.__name__ == "precis.pcb.local"

    def fake_module(name: str) -> object:
        raise ImportError("No module named 'precis.pcb.local'")

    monkeypatch.setattr(importlib, "import_module", fake_module)
    with pytest.raises(ImportError, match="pcb-kernel"):
        _ = pcb.local


# Whether ruff is reachable from the test process (same probe as
# test_python_handler_writes).
_RUFF_AVAILABLE = (
    shutil.which("ruff") is not None or (Path(sys.executable).parent / "ruff").is_file()
)


@pytest.mark.skipif(not _RUFF_AVAILABLE, reason="ruff binary not on PATH")
def test_precis_geom_ruff_clean() -> None:
    """The shipped builder package passes ``ruff check`` with no noqa —
    the alias fix must not lean on suppression."""
    ruff = shutil.which("ruff") or str(Path(sys.executable).parent / "ruff")
    result = subprocess.run(
        [ruff, "check", "src/precis_geom"],
        cwd=_ROOT,
        capture_output=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr

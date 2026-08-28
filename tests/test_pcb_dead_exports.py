"""Dead-export structural safeguard for src/precis/pcb/ — every public,
top-level function must have a production caller (a call site outside
tests/) or be named on the known-unwired allow list below, with its own
reason. This would have caught realize.to_gerber_model and
gerber.export_fab the day they were written (docs/backlog/
pcb-fab-output-unwired.md): both correct, tested, and unreachable in
production, indistinguishable from working code by any other test. By
the time this test was built, a concurrent agent had already wired
export_fab/zip_fab/padplace.board_pads into handlers/pcb.py — so none of
those three are on the allow list; this test would now catch it if that
wiring ever regressed.

Caller detection reuses scripts/coderef's ast machinery (same exactness
docs/conventions/code-anchors.md asks for), with two patches this test
needed and a bare ``coderef callers`` scan doesn't have:

- ``coderef._parse_imports`` only walks a file's TOP-LEVEL statements —
  a function-local/lazy import (common in handlers/pcb.py's methods) is
  invisible to it.
- it resolves ``from pkg import submodule as alias`` as "imported the
  name ``submodule`` FROM ``pkg``" rather than "imported the MODULE
  ``pkg.submodule``" — so ``handlers/pcb.py``'s own dominant import style
  (``from precis.pcb import session as pcb_session``, then
  ``pcb_session.build_ir(...)``) false-flags nearly every real production
  entry point in this package as dead.

:func:`_all_imports` below fixes both (whole-tree walk, submodule-import
resolution); everything else is coderef's own resolve/leaf-match logic,
unmodified.
"""

from __future__ import annotations

import ast
import functools
import importlib.machinery
import importlib.util
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PCB_DIR = _REPO_ROOT / "src" / "precis" / "pcb"

# scripts/coderef has no .py suffix (it's an executable, not a package
# module) — load a byte-identical .py-suffixed COPY, same convention as
# tests/test_coderef_structural.py (pytest-testmon fingerprints executed
# files by extension and INTERNALERRORs on one that has none).
_coderef_copy = Path(tempfile.mkdtemp(prefix="coderef_dead_export_")) / "coderef.py"
shutil.copyfile(_REPO_ROOT / "scripts" / "coderef", _coderef_copy)
_loader = importlib.machinery.SourceFileLoader("coderef", str(_coderef_copy))
_spec = importlib.util.spec_from_loader("coderef", _loader)
assert _spec and _spec.loader
coderef = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(coderef)


#: Known-unwired public functions: correct, tested, structurally
#: unreachable from any production call path today. Each value is WHY —
#: printed on failure by pytest's own assertion, never a blanket skip.
#: :func:`test_known_unwired_list_has_no_stale_entries` is the other half
#: of the contract: an entry that gains a real caller must be REMOVED,
#: not left masking a future regression check forever.
_KNOWN_UNWIRED: dict[str, str] = {
    "src/precis/pcb/_http.py::reset_circuit": (
        "own docstring: 'for tests, and for an operator retrying by hand' "
        "-- an ops tool, never code-called"
    ),
    "src/precis/pcb/capabilities.py::headroom": (
        "exported via __all__ for a DRC digest to quote margin headroom; "
        "no digest formatter calls it yet"
    ),
    "src/precis/pcb/catalog.py::refresh_parts_from_sqlite": (
        "own docstring: superseded by bulk_refresh_parts_from_sqlite for "
        "any real dump; kept for this module's own small fixtures/tests"
    ),
    "src/precis/pcb/drc.py::clearance_violations_naive": (
        "own docstring: 'the O(n^2) reference oracle (backlog, verbatim)' "
        "-- a test oracle by design, not a production checker (see this "
        "module's own docstring note on reference-oracle bugs)"
    ),
    "src/precis/pcb/escape.py::escape_graph_to_dict": (
        "serializes an EscapeGraph for the part_footprints.escape jsonb "
        "cache; the caching call site (escape-routing precompute) isn't "
        "wired yet"
    ),
    "src/precis/pcb/escape.py::escape_graph_from_dict": (
        "inverse of escape_graph_to_dict -- same unwired cache call site"
    ),
    "src/precis/pcb/footprint.py::ensure_footprint": (
        "fetch-and-cache-on-miss for a footprint; no ingest/handler path calls it yet"
    ),
    "src/precis/pcb/ir.py::validate_embedding": (
        "read-only L2/L3 consistency check; no DRC or session pass invokes it yet"
    ),
    "src/precis/pcb/ir.py::unconnected_items": (
        "dangling-pin/net report; no handler surfaces it yet"
    ),
    "src/precis/pcb/ir.py::per_layer_planar": (
        "Euler-bound planarity pre-check; superseded operationally by "
        "same_layer_crossing_count's real geometric count (see cost.py's "
        "crossings term), never wired as a separate gate"
    ),
    "src/precis/pcb/ir.py::compute_gap_capacity": (
        "bulk/full gap recompute; optimize.py's engine maintains its own "
        "incremental version via nearest_other_instance directly instead"
    ),
    "src/precis/pcb/ir.py::compute_region_density": (
        "fills seg_region_density (a RUDY-style proxy); no placement pass "
        "reads that field yet"
    ),
    "src/precis/pcb/ir.py::plane_connectivity": (
        "stitch-via redundancy check; no DRC pass calls it yet (tiling.py's "
        "own docstring names this the topological proxy a later module "
        "combines with its geometric floating-tile check)"
    ),
    "src/precis/pcb/pinswap.py::group_from_pads": (
        "needs caller-supplied datasheet-derived pin-equivalence data "
        "that 'doesn't exist yet' in production -- optimize.py's own "
        "documented gap (OptimizeConfig.pin_swap_groups defaults empty)"
    ),
    "src/precis/pcb/pinswap.py::total_group_crossings": (
        "own docstring: 'ground truth' crossing count for MEASURING a "
        "swap's payoff; only tests want that today"
    ),
    "src/precis/pcb/realize.py::rip_net": (
        "net-removal half of an interactive route-edit path; no such "
        "handler verb is wired yet"
    ),
    "src/precis/pcb/realize.py::pin_topology": (
        "own docstring: a thin delegate for the same not-yet-wired "
        "rip-up/re-realize loop as rip_net"
    ),
    "src/precis/pcb/realize.py::re_realize_segments": (
        "incremental re-realize for edited segments; same unwired "
        "route-edit gap as rip_net"
    ),
    "src/precis/pcb/realize.py::to_gerber_model": (
        "this defect class's own flagship example (docs/backlog/"
        "pcb-fab-output-unwired.md) -- handlers/pcb.py builds its fab "
        "model dict by hand instead of calling this"
    ),
    "src/precis/pcb/tiling.py::expansion_rate_from_objective": (
        "Slice 5's copper-tiling engine (module docstring) -- not yet "
        "invoked from realize.py's copper-generation path (docs/backlog/"
        "pcb-guided-place-route.md)"
    ),
    "src/precis/pcb/tiling.py::grow_tiles": "same Slice 5 tiling engine, not yet wired",
    "src/precis/pcb/tiling.py::find_acute_angles": (
        "same Slice 5 tiling engine, not yet wired"
    ),
    "src/precis/pcb/tiling.py::cull_slivers": "same Slice 5 tiling engine, not yet wired",
    "src/precis/pcb/tiling.py::find_floating_pieces": (
        "same Slice 5 tiling engine, not yet wired"
    ),
    "src/precis/pcb/tiling.py::drop_floating_pieces": (
        "same Slice 5 tiling engine, not yet wired"
    ),
    "src/precis/pcb/tiling.py::neck_down_at_pads": (
        "same Slice 5 tiling engine, not yet wired"
    ),
}


def _public_top_level_functions(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and not node.name.startswith("_")
    ]


def _all_imports(tree: ast.Module) -> dict[str, str]:
    """Like ``coderef._parse_imports``, but (a) walks the WHOLE tree so a
    function-local/lazy import counts too, and (b) resolves ``from pkg
    import submodule [as alias]`` to the submodule's own dotted path
    (``pkg.submodule``) rather than treating ``submodule`` as a plain name
    imported FROM ``pkg`` — see this file's module docstring."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                out[local] = alias.name
        elif isinstance(node, ast.ImportFrom) and not node.level:
            module = node.module or ""
            for alias in node.names:
                local = alias.asname or alias.name
                out[local] = f"{module}.{alias.name}" if module else alias.name
    return out


def _tracked_py_files() -> list[Path]:
    """Every tracked+untracked ``*.py`` file, ``git``-scoped like
    ``coderef._grep_candidates``. The gate container has no ``.git``
    (source arrives via ``git archive`` — see pyproject.toml's pytest
    addopts comment), so this falls back to a plain ``src/`` walk in that
    case, same fallback shape (and same ``src/``-only scope) as
    ``coderef._grep_candidates`` itself — every production caller this
    package cares about lives under ``src/``."""
    try:
        out = subprocess.run(
            [
                "git",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                "*.py",
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode == 0:
            return [_REPO_ROOT / line for line in out.stdout.splitlines() if line]
    except OSError:
        pass
    base = _REPO_ROOT / "src"
    return sorted(p for p in base.rglob("*.py") if ".git" not in p.parts)


#: One AST pass per file, memoized process-wide: (import map, {referenced
#: leaf name -> [line numbers]}). 141 parametrized cases over ~1700 repo
#: files means a per-anchor ``git grep`` + re-parse (coderef's own
#: per-call shape) is O(anchors x files); this test instead pays the
#: parse cost O(files) ONCE (first anchor a given pytest-xdist worker
#: handles) and every anchor after that is a dict lookup — the difference
#: between a ~2 minute run and a ~10 second one.
@functools.cache
def _file_index(path_str: str) -> tuple[dict[str, str], dict[str, list[int]]] | None:
    path = Path(path_str)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None
    leaves: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            leaves.setdefault(node.id, []).append(getattr(node, "lineno", 0))
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            leaves.setdefault(node.attr, []).append(getattr(node, "lineno", 0))
    return _all_imports(tree), leaves


def _production_callers(anchor: str) -> set[str]:
    """Every non-``tests/`` file with a real (AST, not textual) reference
    to ``anchor``'s leaf name, confirmed by an import that resolves to its
    module — coderef's own callers logic (``_is_leaf_ref``-equivalent
    confirmation via :func:`_file_index`), plus the two import-resolution
    patches in :func:`_all_imports`."""
    relpath, qual = coderef.parse_anchor(anchor)
    def_line, note = coderef.resolve(_REPO_ROOT, relpath, qual)
    assert def_line is not None, f"{anchor}: {note}"
    idx = coderef._index_path(str(_REPO_ROOT / relpath))
    matched_qual, _ = coderef._lookup_qual(idx, qual)
    leaf = coderef._leaf_name(matched_qual or qual)
    def_path = (_REPO_ROOT / relpath).resolve()
    target_module = coderef._path_to_module(_REPO_ROOT, _REPO_ROOT / relpath)

    callers: set[str] = set()
    for path in _tracked_py_files():
        rel = path.relative_to(_REPO_ROOT)
        if rel.parts[0] == "tests":
            continue
        indexed = _file_index(str(path))
        if indexed is None:
            continue
        imports, leaves = indexed
        lines = leaves.get(leaf)
        if not lines:
            continue
        same_file = path.resolve() == def_path
        if not same_file:
            if not any(
                dotted in (target_module, f"{target_module}.{leaf}")
                for dotted in imports.values()
            ):
                continue
            callers.add(str(rel))
        elif any(ln != def_line for ln in lines):
            callers.add(str(rel))
    return callers


def _all_public_function_anchors() -> list[str]:
    anchors = []
    for f in sorted(_PCB_DIR.glob("*.py")):
        relpath = f.relative_to(_REPO_ROOT).as_posix()
        for name in _public_top_level_functions(f):
            anchors.append(f"{relpath}::{name}")
    return anchors


@pytest.mark.parametrize("anchor", _all_public_function_anchors())
def test_public_function_has_a_production_caller_or_is_known_unwired(
    anchor: str,
) -> None:
    if anchor in _KNOWN_UNWIRED:
        pytest.skip(f"known-unwired: {_KNOWN_UNWIRED[anchor]}")
    callers = _production_callers(anchor)
    assert callers, (
        f"{anchor} has no production caller (tests/ only, or nothing at "
        "all) and is not on the known-unwired allow list -- either wire "
        "it in or add it there with its own reason (see this file's "
        "module docstring)"
    )


def test_known_unwired_list_has_no_stale_entries() -> None:
    """The inverse check: an allow-list entry that GAINED a production
    caller since it was added must be removed, not left shadowing this
    test's actual job forever — this is what happened to
    ``gerber.export_fab``/``zip_fab``/``padplace.board_pads`` mid-build
    (see this file's module docstring), so they're correctly ABSENT from
    :data:`_KNOWN_UNWIRED`, not stale entries in it."""
    stale = {
        anchor: sorted(callers)
        for anchor in _KNOWN_UNWIRED
        if (callers := _production_callers(anchor))
    }
    assert not stale, (
        f"known-unwired entries that now HAVE a production caller -- "
        f"remove them from _KNOWN_UNWIRED: {stale}"
    )

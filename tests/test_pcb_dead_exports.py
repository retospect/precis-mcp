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

**Widened 2026-08-29**: a top-level function's own caller check would not
have caught a single one of the six real defects this subsystem shipped
the same week, because none of them were top-level functions —

1. ``PcbGrid._pads``/``stamp_pad`` (a method whose sole caller used
   ``stamp_disk`` instead, so the collection stayed permanently empty —
   see :func:`precis.pcb.realize._stamp_pads`'s own docstring for the
   postmortem; fixed 2026-08-29, in flight when this test was written).
2. ``PcbIR.inst_extended_part`` (a dataclass field read by ``cost.py``
   but never populated by the graph SQL; also fixed 2026-08-29).
3. ``capabilities.FIELDS``' ``soldermask_dam_mm``/``silk_width_mm`` (two
   entries in a declared schema table with zero consumers).
   ``soldermask_dam_mm`` is STILL true, see the allow list below;
   ``silk_width_mm`` was fixed 2026-08-29 —
   :func:`precis.pcb.drc.check_silk_printability` now reads it as the
   fab's minimum printable silk stroke width, so it is off the allow
   list.
4. ``maze.GridSpec.n_cells`` (a public property with zero references in
   the whole tree — STILL true, see the allow list below).
5. ``check_npth_clearance`` reading a ``model["drills"]`` dict key
   nothing populates (``padplace.py`` hardcodes every drill
   ``"plated": True`` — a producer/consumer gap on an untyped dict key,
   structurally out of AST reach; not gated here, reported separately).
6. ``PcbIR.inst_rot`` (write-only: persisted but never selected back by
   the graph SQL — also fixed 2026-08-29).

Four checks now run, sharing the SAME allow-list/staleness discipline as
the original function check:

- **Public methods/properties on a public top-level class** in any
  ``src/precis/pcb/*.py`` file (catches (4)) — :func:`_all_class_member_anchors`,
  ``test_public_method_or_property_has_a_production_caller_or_is_known_unwired``.
- **``PcbIR`` dataclass fields** (catches (2)/(6) structurally — a field
  declared but never both written AND read from outside ``ir.py``) —
  :func:`_pcbir_field_names`, ``test_pcbir_field_has_a_producer_and_a_consumer_or_is_known_unwired``.
- **``capabilities.FIELDS`` entries** (catches (3)) — not a resolvable
  Python symbol (a tuple element has no ``def_line``), so this one is a
  literal string-constant scan, not a coderef anchor —
  :func:`_field_table_consumers`, ``test_capabilities_field_has_a_consumer_or_is_known_unwired``.
- **``CostConfig`` dataclass fields** — the other declared-tunables
  struct named in the review (no FIELDS-style table of its own found;
  its fields ARE all consumed today per a manual audit — this is a
  regression net for a future one that isn't) —
  ``test_costconfig_field_has_a_consumer_or_is_known_unwired``.

(5) is a plain ``dict[str, Any]`` key flowing between ``padplace.py`` and
``drc.py`` with no declared schema anywhere (no dataclass, no FIELDS
tuple) — there is no structural anchor an AST pass can hang a
producer/consumer check on without a schema to check it against, so it
is out of this gate's reach; see the triage report for detail instead.

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
    "src/precis/pcb/geom.py::max_inward_deviation": (
        "the FORWARD measurement (given a radius, how far does the arc "
        "bulge past the mitered path). Production calls its inverse, "
        "max_radius_for_deviation, instead: _track_from_run used to call "
        "this one and scale by budget/worst, which was a no-op at "
        "setback-clamped corners (fixed 2026-08-29). Kept because "
        "tests/test_pcb_geom.py cross-checks the two against an "
        "INDEPENDENT sagitta formula -- deleting it would leave the cap "
        "with only its own math to agree with"
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
    # ---- class methods/properties (widened 2026-08-29) --------------
    "src/precis/pcb/maze.py::GridSpec.n_cells": (
        "real defect flagged by this widened gate 2026-08-29 (module "
        "docstring item (4)): zero references anywhere in the tree "
        "including tests -- not fixed here (out of this agent's remit, "
        "reported to the caller as a real gap, not a design choice)"
    ),
    # ---- capabilities.FIELDS entries (widened 2026-08-29) -------------
    "src/precis/pcb/capabilities.py::FIELDS.soldermask_dam_mm": (
        "real defect flagged by this widened gate 2026-08-29 (module "
        "docstring item (3)): declared in the schema and given a "
        "house_default rule (this module's own docstring), but no DRC/"
        "cost/export consumer reads it anywhere -- not fixed here, "
        "reported to the caller as a real gap"
    ),
    # ---- PcbIR fields: L2 explicit-embedding tier ----------------------
    # set_rotation/rotation_index/rotation_darts are the sole L2 tier
    # (module docstring: "L2: explicit combinatorial embedding"); ir.py's
    # own seg_side field (the OTHER L2 field, right next to these) is
    # documented in-line as "reserved obstacle-side annotation
    # (vocabulary settles in sketch.py, slice 7)" -- the whole tier is a
    # documented future slice, not a forgotten wire-up, so this is
    # legitimately-unwired, not a defect report.
    "src/precis/pcb/ir.py::PcbIR.set_rotation": (
        "the only mutator for rotation_darts/rotation_index (L2, "
        "'explicit combinatorial embedding') -- ir.py's own seg_side "
        "field, right next to these, documents the WHOLE L2 tier as "
        "'vocabulary settles in sketch.py, slice 7' -- a documented "
        "future slice, not a forgotten wire-up"
    ),
    "src/precis/pcb/ir.py::PcbIR.rotation_index": (
        "L2 tier -- same 'slice 7' future-work note as set_rotation, see "
        "that entry's reason"
    ),
    "src/precis/pcb/ir.py::PcbIR.rotation_darts": (
        "L2 tier -- same 'slice 7' future-work note as set_rotation, see "
        "that entry's reason"
    ),
    # ---- PcbIR fields: via_* real gap ------------------------------------
    # add_via/via_net/via_layer_span are the ONLY way to populate a via in
    # the IR's own L1 model ("vias as transitions") -- but realize.py's
    # actual via placement (_route_pass, _drop_via_site, etc.) builds its
    # own separate RealizedVia dataclass (realize.py:538) and never once
    # calls ir.add_via or reads ir.via_net/via_layer_span. Unlike the L2
    # tier above, L1 IS load-bearing in production today (seg_layer,
    # set_layer are real, wired production data) -- this is a real,
    # reported gap, not a documented future slice; classified (a), flagged
    # to the caller, NOT fixed here (out of remit, and realize.py is
    # another agent's file).
    "src/precis/pcb/ir.py::PcbIR.add_via": (
        "real gap flagged by this widened gate 2026-08-29: the IR's own "
        "L1 via model has zero producers in realize.py's actual via "
        "placement -- realize.py maintains a wholly separate "
        "RealizedVia dataclass (realize.py:538) instead and never calls "
        "this. Unlike the L2 tier, L1 is production load-bearing today "
        "(seg_layer/set_layer are real) -- reported to the caller as a "
        "real gap, not fixed here (realize.py is out of this agent's "
        "remit)"
    ),
    "src/precis/pcb/ir.py::PcbIR.via_net": (
        "same real gap as add_via -- see that entry's reason; nothing "
        "outside ir.py itself ever reads or writes this"
    ),
    "src/precis/pcb/ir.py::PcbIR.via_layer_span": (
        "same real gap as add_via -- see that entry's reason; nothing "
        "outside ir.py itself ever reads or writes this"
    ),
    # ---- PcbIR fields: L4/L5 invalidation cascade ------------------------
    # dirty_l1..l5 are set by every mutator and cleared by clean() (called
    # from optimize.py), but nothing anywhere READS a dirty_lN mask to
    # decide what needs recomputing -- optimize.py calls ir.clean(...)
    # unconditionally off its OWN bookkeeping (`seeded`), never consulting
    # the mask first. This is the field-level shape of an ALREADY
    # allow-listed gap: ir.py::compute_gap_capacity and
    # ir.py::compute_region_density (both above, pre-existing entries)
    # are exactly the L4 recompute functions a mask-driven engine would
    # call, and both are already documented as unwired for the same
    # reason ("no placement pass reads that field yet"). Classified (b):
    # legitimately unwired, consistent with pre-existing precedent in
    # this same file, not a new finding.
    "src/precis/pcb/ir.py::PcbIR.dirty_l1": (
        "field-level shape of the already-allow-listed "
        "compute_gap_capacity/compute_region_density gap above: no "
        "engine reads ANY dirty_lN mask to decide what needs "
        "recomputing -- optimize.py calls ir.clean() off its own "
        "bookkeeping, never consulting the mask first (see this file's "
        "module docstring)"
    ),
    "src/precis/pcb/ir.py::PcbIR.dirty_l2": (
        "same invalidation-cascade gap as dirty_l1 -- see that entry's reason"
    ),
    "src/precis/pcb/ir.py::PcbIR.dirty_l3": (
        "same invalidation-cascade gap as dirty_l1 -- see that entry's reason"
    ),
    "src/precis/pcb/ir.py::PcbIR.dirty_l4": (
        "same invalidation-cascade gap as dirty_l1 -- see that entry's reason"
    ),
    "src/precis/pcb/ir.py::PcbIR.dirty_l5": (
        "same invalidation-cascade gap as dirty_l1 -- see that entry's reason"
    ),
    # ---- PcbIR fields: realize.py's own remit, in flight ----------------
    "src/precis/pcb/ir.py::PcbIR.seg_copper_length_mm": (
        "in flight 2026-08-29: this field's own inline comment promises "
        "'nan until realize.py runs', but realize.py never writes it "
        "(grepped zero hits) -- realize.py is another agent's file, out "
        "of this agent's remit; flagged separately in the triage report"
    ),
    "src/precis/pcb/ir.py::PcbIR.seg_region_density": (
        "same already-allow-listed L4 gap as compute_region_density "
        "above (that function is this field's ONLY writer, and is "
        "itself already documented 'no placement pass reads that field "
        "yet') -- not a new finding"
    ),
    "src/precis/pcb/ir.py::PcbIR.pin_offsets_synthesized": (
        "real gap flagged by this widened gate 2026-08-29: this field's "
        "own module docstring calls it 'same discipline as' its sibling "
        "pin_pad_synthesized and says the synthesized/real distinction "
        "'must never be lost on the way to fabrication' -- but "
        "pin_pad_synthesized IS read by realize.py (realize.py:3108) "
        "while pin_offsets_synthesized never is. Reported to the caller "
        "as a real gap, not fixed here (realize.py is out of remit)"
    ),
    # ---- class methods/properties: legitimately unwired ------------------
    "src/precis/pcb/maze.py::OccupancyGrid.owner": (
        "a raw ``._owner`` occupancy-array accessor used only by "
        "tests/test_pcb_maze.py's own assertions (grepped) -- a "
        "test-inspection property, same status as OccupancyGrid.pads' "
        "sibling ``._pads`` before the 2026-08-29 stamp_pad fix, except "
        "this one was never meant to feed a production reader"
    ),
    "src/precis/pcb/jlc_api.py::JlcApiClient.component_info": (
        "the single-part live-stock lookup ('in stock now' check at "
        "part-selection time, own docstring) -- its sibling "
        "iter_components IS wired (workers/parts_refresh.py), but no "
        "part-selection call site exists yet for the single-part live "
        "check"
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
    patches in :func:`_all_imports`.

    **Third acceptable import-confirmation, added for the class-member
    widening** (methods/properties, and ``PcbIR``/``CostConfig`` fields —
    see this file's module docstring): a caller may import the OWNING
    CLASS directly (``from precis.pcb.ir import PcbIR``) rather than the
    module (``from precis.pcb import ir``) or the leaf itself — the
    dominant style for a class whose instances (not the module) carry the
    behavior. Coderef's own two acceptable dotted forms
    (``target_module``, ``target_module.leaf``) were built for bare
    top-level functions/module imports and never anticipated this; for a
    top-level (non-member) anchor ``qual == leaf``, so this third form
    collapses to the existing ``target_module.leaf`` one — a strict
    widening, never a narrowing, of what already passes."""
    relpath, qual = coderef.parse_anchor(anchor)
    def_line, note = coderef.resolve(_REPO_ROOT, relpath, qual)
    assert def_line is not None, f"{anchor}: {note}"
    idx = coderef._index_path(str(_REPO_ROOT / relpath))
    matched_qual, _ = coderef._lookup_qual(idx, qual)
    full_qual = matched_qual or qual
    leaf = coderef._leaf_name(full_qual)
    owner_qual = full_qual.rsplit(".", 1)[0] if "." in full_qual else None
    def_path = (_REPO_ROOT / relpath).resolve()
    target_module = coderef._path_to_module(_REPO_ROOT, _REPO_ROOT / relpath)
    acceptable = {target_module, f"{target_module}.{leaf}"}
    if owner_qual:
        acceptable.add(f"{target_module}.{owner_qual}")

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
            if not any(dotted in acceptable for dotted in imports.values()):
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


# ─── widened 2026-08-29: class methods/properties ───────────────────────


def _public_top_level_classes(path: Path) -> list[ast.ClassDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
    ]


def _public_methods_and_properties(class_node: ast.ClassDef) -> list[str]:
    return [
        member.name
        for member in class_node.body
        if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef)
        and not member.name.startswith("_")
    ]


def _all_class_member_anchors() -> list[str]:
    """``relpath::Class.member`` for every public method/property on every
    public top-level class in ``src/precis/pcb/*.py`` — this is what
    would have caught :class:`~precis.pcb.maze.GridSpec`'s ``n_cells``
    (module docstring item (4)): a public property with zero references
    anywhere, the exact shape the original function-only check could
    never see. ``_production_callers`` resolves ``Class.member`` anchors
    unmodified (``symbol_index`` already nests qualnames by class); the
    only gap was the THIRD import-confirmation form patched into that
    function itself (a caller importing the CLASS, not the module)."""
    anchors = []
    for f in sorted(_PCB_DIR.glob("*.py")):
        relpath = f.relative_to(_REPO_ROOT).as_posix()
        for class_node in _public_top_level_classes(f):
            for name in _public_methods_and_properties(class_node):
                anchors.append(f"{relpath}::{class_node.name}.{name}")
    return anchors


@pytest.mark.parametrize("anchor", _all_class_member_anchors())
def test_public_method_or_property_has_a_production_caller_or_is_known_unwired(
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


# ─── widened 2026-08-29: PcbIR dataclass fields ──────────────────────────

_IR_PATH = _PCB_DIR / "ir.py"


def _dataclass_field_names(path: Path, class_name: str) -> list[str]:
    """Public field names declared directly on ``class_name``'s body — an
    ``AnnAssign`` at class scope (a dataclass field). A method/property is
    a ``FunctionDef``, not an ``AnnAssign``, so this and
    :func:`_public_methods_and_properties` are disjoint by construction —
    no double-coverage of ``PcbIR.n_instances``-style computed
    properties, which the class-member check above already owns."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return [
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and not stmt.target.id.startswith("_")
            ]
    raise AssertionError(f"{class_name} not found in {path}")


def _pcbir_field_names() -> list[str]:
    return _dataclass_field_names(_IR_PATH, "PcbIR")


_PCBIR_FIELD_NAMES = frozenset(_pcbir_field_names())
_IR_RELPATH = _IR_PATH.relative_to(_REPO_ROOT).as_posix()


@functools.cache
def _file_write_index(path_str: str) -> frozenset[str]:
    """Every name this file WRITES to as a dataclass-field PRODUCER: a
    constructor keyword argument (``PcbIR(field=...)``) or a direct
    attribute-store assignment (``self.field = ...``). A keyword-argument
    name is invisible to :func:`_file_index`'s Name/Attribute Load scan
    (``keyword.arg`` is a bare string, never a ``Name``/``Attribute``
    node) — this is that function's write-side analogue, needed because a
    ``slots=True`` dataclass field's only real producer site is almost
    always a constructor kwarg, not an attribute-store assignment."""
    try:
        tree = ast.parse(Path(path_str).read_text(encoding="utf-8", errors="ignore"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return frozenset()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg:
            out.add(node.arg)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            out.add(node.attr)
    return frozenset(out)


def _field_write_sites(field: str) -> set[str]:
    out: set[str] = set()
    for path in _tracked_py_files():
        rel = path.relative_to(_REPO_ROOT)
        if rel.parts[0] == "tests":
            continue
        if field in _file_write_index(str(path)):
            out.add(str(rel))
    return out


def _pcbir_field_consumers(field: str) -> set[str]:
    """Reads OUTSIDE ``ir.py`` itself — the module docstring's item
    (2)/(6) contract ("written somewhere and read somewhere outside its
    own defining module"): a field that's only ever touched by ir.py's
    own constructor/mutators (real, but not evidence anything downstream
    actually consumes it) doesn't satisfy this."""
    return _production_callers(f"{_IR_RELPATH}::PcbIR.{field}") - {_IR_RELPATH}


@pytest.mark.parametrize("field", _pcbir_field_names())
def test_pcbir_field_has_a_producer_and_a_consumer_or_is_known_unwired(
    field: str,
) -> None:
    anchor = f"{_IR_RELPATH}::PcbIR.{field}"
    if anchor in _KNOWN_UNWIRED:
        pytest.skip(f"known-unwired: {_KNOWN_UNWIRED[anchor]}")
    writers = _field_write_sites(field)
    assert writers, (
        f"PcbIR.{field} has no producer anywhere (tests/ excluded) -- "
        "nothing ever passes it as a constructor kwarg or assigns it -- "
        "either wire a producer in or add it to the known-unwired allow "
        "list with its own reason"
    )
    readers = _pcbir_field_consumers(field)
    assert readers, (
        f"PcbIR.{field} is written but never READ outside ir.py itself "
        "(tests/ excluded) -- either wire a consumer in or add it to the "
        "known-unwired allow list with its own reason (see this file's "
        "module docstring, items (2)/(6))"
    )


# ─── widened 2026-08-29: capabilities.FIELDS entries ─────────────────────

_CAPABILITIES_PATH = _PCB_DIR / "capabilities.py"
_CAPABILITIES_RELPATH = _CAPABILITIES_PATH.relative_to(_REPO_ROOT).as_posix()


def _tuple_string_constants(path: Path, name: str) -> list[str]:
    """String literals in a module-level ``name = (...)`` tuple —
    ``capabilities.FIELDS``'s shape: a declared schema whose elements are
    not resolvable Python symbols (a tuple element has no ``def_line``,
    unlike a ``def``/``class``/module-level assignment target), so these
    can't go through :func:`_production_callers`/coderef at all — a
    literal string-constant scan is the only structural handle."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            assert isinstance(node.value, ast.Tuple), (
                f"{name} in {path} is not a tuple literal -- "
                "_tuple_string_constants assumes capabilities.FIELDS's shape"
            )
            return [
                elt.value
                for elt in node.value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]
    raise AssertionError(f"{name} not found in {path}")


def _capability_fields() -> list[str]:
    return _tuple_string_constants(_CAPABILITIES_PATH, "FIELDS")


@functools.cache
def _string_constants_in_file(path_str: str) -> frozenset[str]:
    try:
        tree = ast.parse(Path(path_str).read_text(encoding="utf-8", errors="ignore"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return frozenset()
    return frozenset(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _field_table_consumers(field: str, *, exclude_relpath: str) -> set[str]:
    out: set[str] = set()
    for path in _tracked_py_files():
        rel = path.relative_to(_REPO_ROOT)
        if rel.parts[0] == "tests" or rel.as_posix() == exclude_relpath:
            continue
        if field in _string_constants_in_file(str(path)):
            out.add(str(rel))
    return out


@pytest.mark.parametrize("field", _capability_fields())
def test_capabilities_field_has_a_consumer_or_is_known_unwired(field: str) -> None:
    anchor = f"{_CAPABILITIES_RELPATH}::FIELDS.{field}"
    if anchor in _KNOWN_UNWIRED:
        pytest.skip(f"known-unwired: {_KNOWN_UNWIRED[anchor]}")
    consumers = _field_table_consumers(field, exclude_relpath=_CAPABILITIES_RELPATH)
    assert consumers, (
        f"capabilities.FIELDS entry {field!r} has no consumer anywhere "
        "(tests/ and capabilities.py itself excluded) -- either wire a "
        "reader in (e.g. a DRC/cost/export check keyed on it) or add it "
        "to the known-unwired allow list with its own reason (see this "
        "file's module docstring, item (3))"
    )


# ─── widened 2026-08-29: CostConfig dataclass fields ─────────────────────

_COST_PATH = _PCB_DIR / "cost.py"
_COST_RELPATH = _COST_PATH.relative_to(_REPO_ROOT).as_posix()


def _costconfig_field_names() -> list[str]:
    return _dataclass_field_names(_COST_PATH, "CostConfig")


@pytest.mark.parametrize("field", _costconfig_field_names())
def test_costconfig_field_has_a_consumer_or_is_known_unwired(field: str) -> None:
    """Unlike ``PcbIR``, a ``CostConfig`` tunable is legitimately consumed
    WITHIN ``cost.py`` itself (that is the whole module's job) — no
    outside-module filter here, just "read somewhere at all"
    (:func:`_production_callers`'s ordinary same-file-counts rule)."""
    anchor = f"{_COST_RELPATH}::CostConfig.{field}"
    if anchor in _KNOWN_UNWIRED:
        pytest.skip(f"known-unwired: {_KNOWN_UNWIRED[anchor]}")
    consumers = _production_callers(anchor)
    assert consumers, (
        f"CostConfig.{field} has no consumer anywhere (tests/ excluded) "
        "-- either wire a reader in or add it to the known-unwired allow "
        "list with its own reason"
    )


def test_known_unwired_list_has_no_stale_entries() -> None:
    """The inverse check: an allow-list entry that GAINED a production
    caller/consumer since it was added must be removed, not left
    shadowing this test's actual job forever — this is what happened to
    ``gerber.export_fab``/``zip_fab``/``padplace.board_pads`` mid-build
    (see this file's module docstring), so they're correctly ABSENT from
    :data:`_KNOWN_UNWIRED`, not stale entries in it. Dispatches by anchor
    shape (function/method/CostConfig-field anchors go through
    :func:`_production_callers`; ``FIELDS.<name>`` entries through
    :func:`_field_table_consumers`; ``PcbIR.<field>`` entries additionally
    drop ir.py itself, same as :func:`_pcbir_field_consumers`) so ALL
    four checks' allow-list entries share this one staleness test, per
    this file's own reuse-the-machinery discipline."""

    def consumers_for(anchor: str) -> set[str]:
        relpath, qual = coderef.parse_anchor(anchor)
        if qual and qual.startswith("FIELDS."):
            return _field_table_consumers(
                qual.split(".", 1)[1], exclude_relpath=relpath
            )
        if (
            relpath == _IR_RELPATH
            and qual
            and qual.startswith("PcbIR.")
            and qual.split(".", 1)[1] in _PCBIR_FIELD_NAMES
        ):
            return _pcbir_field_consumers(qual.split(".", 1)[1])
        return _production_callers(anchor)

    stale = {
        anchor: sorted(callers)
        for anchor in _KNOWN_UNWIRED
        if (callers := consumers_for(anchor))
    }
    assert not stale, (
        f"known-unwired entries that now HAVE a production caller -- "
        f"remove them from _KNOWN_UNWIRED: {stale}"
    )


#: A reason that would make the allow list a rubber stamp — "the test
#: should fail if the reason is empty or a placeholder" is the discipline
#: this codifies, not just a convention left to reviewer vigilance.
_PLACEHOLDER_REASONS = {
    "todo",
    "tbd",
    "unknown",
    "n/a",
    "na",
    "unused",
    "not used",
    "unwired",
    "dead code",
    "no reason",
    "wip",
    "fixme",
    "xxx",
}


def test_known_unwired_reasons_are_non_trivial() -> None:
    """Every :data:`_KNOWN_UNWIRED` reason must be a real, checkable
    claim — a blank string or a placeholder converts "absence of
    evidence" into a green tick (this file's module docstring's own
    warning about what a bad allow list does), which is worse than no
    test at all."""
    trivial = {
        anchor: reason
        for anchor, reason in _KNOWN_UNWIRED.items()
        if len(reason.strip()) < 20
        or reason.strip().strip("-'\" .").lower() in _PLACEHOLDER_REASONS
    }
    assert not trivial, (
        f"placeholder/empty _KNOWN_UNWIRED reasons -- write a real, "
        f"checkable one: {trivial}"
    )

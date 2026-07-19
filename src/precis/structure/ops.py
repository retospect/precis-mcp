"""The write surface — typed ops the LLM emits (ADR 0043 §5).

The LLM edits the *graph* (intent); the framework applies and re-derives. v1 op
catalog floor: set_cell · add_atom · set_element · vacancy · displace · add_bond ·
remove_bond · constrain. Bulk template ``slab`` (fcc(111), §5b) seeds a whole
metal surface from a compact spec — mirrors catpath's ``build_slab`` (same ASE
call → identical atom order + geometry) so the slab can be *injected* into a
catpath barrier run and its NEB endpoints line up. The validator gate wiring
(§5c) is the next increment. ``apply_ops`` mutates the Scene in place
and returns it; an unknown op or a bad reference raises ``OpError`` (the Edit
contract surfaces this as a structured error, §5c).
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .cell import Cell
from .measures import _MEASURE_ARITY, _MEASURE_KINDS
from .scene import FIX_ALL, FIX_X, FIX_Y, FIX_Z, Atom, Bond, Measure, Scene

_FIX_KINDS = {
    "none": 0,
    "fixed-x": FIX_X,
    "fixed-y": FIX_Y,
    "fixed-z": FIX_Z,
    "fixed-all": FIX_ALL,
}


class OpError(ValueError):
    """A rejected op (bad reference, unknown op, malformed payload)."""


def apply_ops(scene: Scene, ops: list[dict[str, Any]]) -> Scene:
    """Apply a list of typed ops to ``scene`` in order, mutating it."""
    for op in ops:
        if "op" not in op:
            raise OpError(f"op missing 'op' key: {op!r}")
        name = op["op"]
        handler = _OPS.get(name)
        if handler is None:
            raise OpError(f"unknown op: {name!r}")
        handler(scene, op)
    return scene


def _require_atom(scene: Scene, label: str) -> Atom:
    atom = scene.atoms.get(label)
    if atom is None:
        raise OpError(f"no such atom: {label!r}")
    return atom


def _op_set_cell(scene: Scene, op: dict[str, Any]) -> None:
    if "lattice" in op:
        lattice = np.asarray(op["lattice"], dtype=float).reshape(3, 3)
        pbc = tuple(op.get("pbc", scene.cell.pbc))
        scene.cell = Cell(lattice, pbc)  # type: ignore[arg-type]
    else:
        cell = Cell.from_lengths_angles(
            op["a"],
            op["b"],
            op["c"],
            op.get("alpha", 90.0),
            op.get("beta", 90.0),
            op.get("gamma", 90.0),
            tuple(op.get("pbc", scene.cell.pbc)),  # type: ignore[arg-type]
        )
        scene.cell = cell


def _op_add_atom(scene: Scene, op: dict[str, Any]) -> None:
    element = op["element"]
    if "frac" in op:
        frac = scene.cell.wrap(np.asarray(op["frac"], dtype=float))
    elif "cart" in op:
        frac = scene.cell.wrap(
            scene.cell.cart_to_frac(np.asarray(op["cart"], dtype=float))
        )
    else:
        raise OpError("add_atom needs 'frac' or 'cart'")
    label = op.get("label") or scene.next_label(element)
    if label in scene.atoms:
        raise OpError(f"duplicate atom label: {label!r}")
    scene.atoms[label] = Atom(
        label=label,
        element=element,
        frac=frac,
        magmom=op.get("magmom"),
        oxidation=op.get("oxidation"),
        hybridization=op.get("hybridization"),
    )


def _op_set_element(scene: Scene, op: dict[str, Any]) -> None:
    _require_atom(scene, op["atom"]).element = op["element"]


def _op_vacancy(scene: Scene, op: dict[str, Any]) -> None:
    label = op["atom"]
    _require_atom(scene, label)
    del scene.atoms[label]
    scene.bonds = [b for b in scene.bonds if label not in (b.i, b.j)]


def _op_displace(scene: Scene, op: dict[str, Any]) -> None:
    atom = _require_atom(scene, op["atom"])
    vec = np.asarray(op["vector"], dtype=float)
    if op.get("cartesian", True):
        atom.frac = scene.cell.wrap(atom.frac + scene.cell.cart_to_frac(vec))
    else:
        atom.frac = scene.cell.wrap(atom.frac + vec)


def _op_add_bond(scene: Scene, op: dict[str, Any]) -> None:
    i, j = op["i"], op["j"]
    _require_atom(scene, i)
    _require_atom(scene, j)
    scene.bonds.append(
        Bond(
            i=i,
            j=j,
            order=float(op.get("order", 1.0)),
            kind=op.get("kind", "pairwise"),
            provenance="declared",
            image=tuple(op.get("image", (0, 0, 0))),  # type: ignore[arg-type]
        )
    )


def _op_remove_bond(scene: Scene, op: dict[str, Any]) -> None:
    i, j = op["i"], op["j"]
    before = len(scene.bonds)
    scene.bonds = [b for b in scene.bonds if {b.i, b.j} != {i, j}]
    if len(scene.bonds) == before:
        raise OpError(f"no bond between {i!r} and {j!r}")


def _op_constrain(scene: Scene, op: dict[str, Any]) -> None:
    kind = op.get("kind", "fixed-all")
    if kind not in _FIX_KINDS:
        raise OpError(f"unknown constraint kind: {kind!r}")
    mask = _FIX_KINDS[kind]
    for label in op.get("atoms", []):
        _require_atom(scene, label).fixed = mask


def _op_eye(scene: Scene, op: dict[str, Any]) -> None:
    """Drop / replace a named eye — a §6.8 embodiment over a support set."""
    name = op.get("name")
    if not name:
        raise OpError("eye needs a 'name' (e.g. 'active_site')")
    atoms = op.get("atoms") or op.get("support") or []
    if not atoms:
        raise OpError("eye needs 'atoms' (its support set)")
    for label in atoms:
        _require_atom(scene, label)
    reach = op.get("reach")
    m = Measure(
        kind="eye",
        name=str(name),
        operands=[str(a) for a in atoms],
        reach=float(reach) if reach is not None else None,
        for_=op.get("for"),
    )
    # an eye name is unique within the design — replace any prior one
    scene.measures = [
        x for x in scene.measures if not (x.kind == "eye" and x.name == m.name)
    ]
    scene.measures.append(m)


def _op_measure(scene: Scene, op: dict[str, Any]) -> None:
    """Pin a measure (distance / angle / coordination / bond_length) with an
    optional graded goal. Replaces an existing measure over the same operands."""
    kind = op.get("kind")
    if kind not in _MEASURE_KINDS:
        raise OpError(
            f"measure kind must be one of {sorted(_MEASURE_KINDS)}, got {kind!r}"
        )
    atoms = [str(a) for a in (op.get("atoms") or [])]
    if len(atoms) != _MEASURE_ARITY[kind]:
        raise OpError(
            f"measure {kind!r} needs {_MEASURE_ARITY[kind]} atom(s), got {len(atoms)}"
        )
    for label in atoms:
        _require_atom(scene, label)
    direction = op.get("direction")
    if direction is not None and direction not in ("min", "max", "target"):
        raise OpError(f"measure direction must be min|max|target, got {direction!r}")
    m = Measure(
        kind=str(kind),
        operands=atoms,
        direction=direction,
        goal=op.get("goal"),
        strength=str(op.get("strength", "gauge")),
        for_=op.get("for"),
    )
    scene.measures = [
        x for x in scene.measures if not (x.kind == m.kind and x.operands == m.operands)
    ]
    scene.measures.append(m)


def _op_unmark(scene: Scene, op: dict[str, Any]) -> None:
    """Retire an eye by name."""
    name = op.get("name")
    if not name:
        raise OpError("unmark needs an eye 'name'")
    before = len(scene.measures)
    scene.measures = [
        x for x in scene.measures if not (x.kind == "eye" and x.name == str(name))
    ]
    if len(scene.measures) == before:
        raise OpError(f"no eye named {name!r}")


def _op_remove_measure(scene: Scene, op: dict[str, Any]) -> None:
    """Retire a measure by (kind, operands)."""
    kind = op.get("kind")
    atoms = [str(a) for a in (op.get("atoms") or [])]
    before = len(scene.measures)
    scene.measures = [
        x for x in scene.measures if not (x.kind == kind and x.operands == atoms)
    ]
    if len(scene.measures) == before:
        raise OpError(f"no {kind!r} measure over {atoms!r}")


def _op_slab(scene: Scene, op: dict[str, Any]) -> None:
    """Bulk template (§5b): build an fcc(111) metal slab and (re)seed the scene.

    Mirrors catpath's ``build_slab`` exactly — same ``ase.build.fcc111`` call →
    identical atom order + geometry — so the resulting slab can be *injected*
    into a catpath barrier run and its NEB endpoints line up. Params:
    ``element`` (required), ``size`` ``[nx, ny, nz]`` (required), ``vacuum`` Å
    (default 10.0), ``fix_layers`` (bottom layers frozen, default 0), ``a``
    (lattice constant Å; default = ASE reference). A slab is a fresh base, so
    this **clears** any existing atoms/bonds/measures and sets the cell.
    """
    try:
        from ase.build import fcc111
    except ImportError as exc:  # pragma: no cover - ASE is the [dft] extra
        raise OpError("slab op needs ASE (the [dft] extra)") from exc
    element = op.get("element")
    size = op.get("size")
    if not element or not isinstance(size, (list, tuple)) or len(size) != 3:
        raise OpError("slab needs 'element' and 'size' as [nx, ny, nz]")
    nx, ny, nz = int(size[0]), int(size[1]), int(size[2])
    vacuum = float(op.get("vacuum", 10.0))
    fix_layers = int(op.get("fix_layers", 0))
    a = op.get("a")
    slab = fcc111(
        str(element),
        size=(nx, ny, nz),
        vacuum=vacuum,
        a=float(a) if a is not None else None,
    )
    slab.pbc = (True, True, True)
    # Freeze the bottom `fix_layers` layers (mirror catpath: sort by z ascending).
    frozen: set[int] = set()
    if fix_layers:
        order = np.argsort(slab.positions[:, 2])
        frozen = set(order[: fix_layers * nx * ny].tolist())
    # (Re)seed the scene from the ASE slab, preserving ASE's atom order.
    scene.cell = Cell(np.asarray(slab.cell), (True, True, True))  # type: ignore[arg-type]
    scene.atoms.clear()
    scene.bonds = []
    scene.measures = []
    scaled = slab.get_scaled_positions(wrap=False)
    for i, sym in enumerate(slab.get_chemical_symbols()):
        label = scene.next_label(sym)
        scene.atoms[label] = Atom(
            label=label,
            element=sym,
            frac=np.asarray(scaled[i], dtype=float),
            fixed=FIX_ALL if i in frozen else 0,
        )


_OPS = {
    "set_cell": _op_set_cell,
    "slab": _op_slab,
    "add_atom": _op_add_atom,
    "set_element": _op_set_element,
    "vacancy": _op_vacancy,
    "displace": _op_displace,
    "add_bond": _op_add_bond,
    "remove_bond": _op_remove_bond,
    "constrain": _op_constrain,
    "eye": _op_eye,
    "measure": _op_measure,
    "unmark": _op_unmark,
    "remove_measure": _op_remove_measure,
}

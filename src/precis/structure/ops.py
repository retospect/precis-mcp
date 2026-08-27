"""The write surface — typed ops the LLM emits.

The LLM edits the *graph* (intent); the framework applies and re-derives. v1 op
catalog floor: set_cell · add_atom · set_element · vacancy · displace · add_bond ·
remove_bond · constrain. Bulk template ``slab`` (fcc(111), §5b) seeds a whole
metal surface from a compact spec — mirrors autocatpath's ``build_slab`` (same ASE
call → identical atom order + geometry) so the slab can be *injected* into a
autocatpath barrier run and its NEB endpoints line up. The validator gate wiring
(§5c) is the next increment. ``apply_ops`` mutates the Scene in place
and returns it; an unknown op or a bad reference raises ``OpError`` (the Edit
contract surfaces this as a structured error, §5c).

``add_atom_site`` (blind-3D fix, ``docs/backlog/estimate-kind-ms-chemistry-workup.md``
§"Blind 3D design") is the preferred adsorbate-placement op: the model NAMES a
site (top/bridge/hollow over existing atom labels) instead of guessing
fractional coordinates. :func:`_resolve_site` turns the name into exact
coordinates and delegates to :func:`_op_add_atom` — one label-minting/
validation path for both raw and site-symbolic placement.
"""

from __future__ import annotations

import re
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
        raise OpError(_no_atom_msg(scene, label))
    return atom


def _no_atom_msg(scene: Scene, label: str) -> str:
    """A friendlier not-found message than a bare miss.

    The common trap (only the strongest models hit it, by doing more): atom
    labels are **stable** — ``set_element`` transmutes an atom's element but
    KEEPS its mint-time ``a<El><n>`` label, so an ``aPd28`` doped to Cu is still
    named ``aPd28``; there is no ``aCu28``. A model that assumes a relabel then
    references the phantom name. So when a lookup misses on a label with a
    numeric suffix, surface the atom(s) at that same position (the one the
    caller likely means) and name the label-retention rule; otherwise fall back
    to a short roster of what exists.
    """
    base = f"no such atom: {label!r}"
    if not scene.atoms:
        return f"{base} — the scene has no atoms yet"
    m = re.search(r"(\d+)$", label)
    if m:
        n = m.group(1)
        twins = sorted(
            lb
            for lb in scene.atoms
            if (mm := re.search(r"(\d+)$", lb)) and mm.group(1) == n
        )
        if twins:
            return (
                f"{base}. Labels are STABLE: set_element keeps an atom's original "
                f"mint label when it changes element, so a transmuted atom is not "
                f"renamed. Did you mean {twins[0]!r}? (same position {n})"
            )
    roster = ", ".join(sorted(scene.atoms)[:8])
    more = "" if len(scene.atoms) <= 8 else f", … ({len(scene.atoms)} atoms total)"
    return f"{base}. Available atoms: {roster}{more}"


def _op_set_cell(scene: Scene, op: dict[str, Any]) -> None:
    if "lattice" in op:
        lattice = np.asarray(op["lattice"], dtype=float).reshape(3, 3)
        pbc = tuple(op.get("pbc", scene.cell.pbc))
        scene.cell = Cell(lattice, pbc)
    else:
        cell = Cell.from_lengths_angles(
            op["a"],
            op["b"],
            op["c"],
            op.get("alpha", 90.0),
            op.get("beta", 90.0),
            op.get("gamma", 90.0),
            tuple(op.get("pbc", scene.cell.pbc)),
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


#: Anchor count required per site type (v1 closed set — no interstitials yet;
#: an octahedral/tetrahedral void needs a different resolver, backlogged in
#: the estimate-kind design doc).
_SITE_ANCHOR_COUNT = {"top": 1, "bridge": 2, "hollow": 3}


def _op_add_atom_site(scene: Scene, op: dict[str, Any]) -> None:
    """Place an atom by NAMING a site instead of guessing coordinates.

    ``{"op": "add_atom_site", "element": "H", "site": {"type": "hollow",
    "anchors": ["aPd1", "aPd2", "aPd3"]}, "height": <optional Å>}``. Resolves
    the site to Cartesian coordinates (:func:`_resolve_site`) and delegates
    to :func:`_op_add_atom` for the actual mint — same label/validation path
    as raw ``add_atom``.
    """
    element = op.get("element")
    if not element:
        raise OpError("add_atom_site needs an 'element'")
    site = op.get("site")
    if not isinstance(site, dict):
        raise OpError(
            "add_atom_site needs a 'site' object: "
            "{'type': 'top'|'bridge'|'hollow', 'anchors': [labels]}"
        )
    frac = _resolve_site(scene, site, str(element), op.get("height"))
    delegate = dict(op)
    delegate.pop("site", None)
    delegate.pop("height", None)
    delegate.pop("cart", None)
    delegate["frac"] = [float(x) for x in frac]
    _op_add_atom(scene, delegate)


def _resolve_site(
    scene: Scene, site: dict[str, Any], element: str, height: Any
) -> np.ndarray:
    """Resolve a symbolic site (type + anchor labels) to fractional coords.

    xy = centroid of the anchors' Cartesian xy, PBC/minimum-image aware (each
    anchor is first moved to the periodic image nearest the first anchor —
    the same :meth:`Cell.mic` the tick's PBC scar block warns about, so a
    hollow site spanning a cell wall doesn't average a wrapped-away copy).
    z = the anchors' highest z + ``height`` (default: the covalent-radius-sum
    rule, see :func:`_default_site_height`).
    """
    site_type = site.get("type")
    if site_type not in _SITE_ANCHOR_COUNT:
        raise OpError(
            f"add_atom_site 'site.type' must be one of "
            f"{sorted(_SITE_ANCHOR_COUNT)}, got {site_type!r} — interstitials "
            "aren't supported yet"
        )
    anchors = site.get("anchors")
    if not isinstance(anchors, list) or not anchors:
        raise OpError("add_atom_site 'site.anchors' must be a list of atom labels")
    want = _SITE_ANCHOR_COUNT[site_type]
    if len(anchors) != want:
        raise OpError(
            f"a {site_type!r} site needs exactly {want} anchor(s), got "
            f"{len(anchors)}: {anchors!r}"
        )
    if len(set(anchors)) != len(anchors):
        raise OpError(f"add_atom_site anchors must be distinct labels, got {anchors!r}")
    atoms = [_require_atom(scene, str(a)) for a in anchors]
    ref_frac = atoms[0].frac
    carts = []
    anchor_elements = []
    for atom in atoms:
        _dist, img = scene.cell.mic(ref_frac, atom.frac)
        image_frac = atom.frac + np.asarray(img, dtype=float)
        carts.append(scene.cell.frac_to_cart(image_frac))
        anchor_elements.append(atom.element)
    carts_arr = np.asarray(carts, dtype=float)
    xy = carts_arr[:, :2].mean(axis=0)
    z_top = float(carts_arr[:, 2].max())
    if height is None:
        h = _default_site_height(anchor_elements, element)
    else:
        try:
            h = float(height)
        except (TypeError, ValueError) as exc:
            raise OpError(
                f"add_atom_site 'height' must be a number, got {height!r}"
            ) from exc
    target_cart = np.array([xy[0], xy[1], z_top + h])
    return scene.cell.wrap(scene.cell.cart_to_frac(target_cart))


def _default_site_height(anchor_elements: list[str], element: str) -> float:
    """Default adsorbate height (Å) above the anchor plane: covalent-radius sum.

    No campaign-validated per-site-type constant exists — :mod:`.invariants`
    only carries a site *classification* cutoff (``_SURFACE_CUTOFF`` 2.8 Å,
    for reading a built structure back), not a placement height, and the
    on-surface z=0.66 figure the design doc flags is trial-and-error folklore
    carried in prose, not a code constant. So the fallback is deterministic
    and element-aware instead: the mean covalent radius of the anchor
    element(s) plus the placed element's own covalent radius — the same
    ``ase.data.covalent_radii`` table :mod:`.preflight`'s settle field
    already leans on for bonding distance.
    """
    try:
        from ase.data import atomic_numbers, covalent_radii
    except ImportError as exc:  # pragma: no cover - ASE is the [dft] extra
        raise OpError(
            "add_atom_site needs ASE (the [dft] extra) to size a default "
            "height — pass 'height' explicitly to skip this"
        ) from exc
    anchor_r = [covalent_radii[atomic_numbers[el]] for el in anchor_elements]
    placed_r = covalent_radii[atomic_numbers[element]]
    return float(np.mean(anchor_r) + placed_r)


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
            image=tuple(op.get("image", (0, 0, 0))),
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

    Mirrors autocatpath's ``build_slab`` exactly — same ``ase.build.fcc111`` call →
    identical atom order + geometry — so the resulting slab can be *injected*
    into a autocatpath barrier run and its NEB endpoints line up. Params:
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
    # Coerce every numeric param defensively: an LLM emits messy JSON — an
    # explicit ``null`` for an optional key (treat as absent → default), or a
    # non-number where an int/float is due. Any non-coercible value raises a
    # clean OpError (retryable) instead of a raw TypeError/ValueError crashing
    # apply_ops. ``null`` vacuum/fix_layers mean "use the default / none frozen".
    fl = op.get("fix_layers")
    if isinstance(fl, (list, tuple)):
        # A common model misreading (deepseek): fix_layers as a list of layer
        # *indices*. It's an integer COUNT of bottom layers — say so, retryably.
        raise OpError(
            f"slab 'fix_layers' is an integer COUNT of bottom layers to freeze "
            f"(e.g. 2 = the bottom two layers), not a list of indices — got {fl!r}"
        )
    try:
        nx, ny, nz = int(size[0]), int(size[1]), int(size[2])
        vac = op.get("vacuum")
        vacuum = float(vac) if vac is not None else 10.0
        fix_layers = int(fl) if fl is not None else 0
        a_raw = op.get("a")
        a = float(a_raw) if a_raw is not None else None
    except (TypeError, ValueError) as exc:
        raise OpError(
            f"slab numeric params must be numbers — got size={size!r}, "
            f"vacuum={op.get('vacuum')!r}, fix_layers={op.get('fix_layers')!r}, "
            f"a={op.get('a')!r}"
        ) from exc
    slab = fcc111(
        str(element),
        size=(nx, ny, nz),
        vacuum=vacuum,
        a=a,
    )
    slab.pbc = (True, True, True)
    # Freeze the bottom `fix_layers` layers (mirror autocatpath: sort by z ascending).
    frozen: set[int] = set()
    if fix_layers:
        order = np.argsort(slab.positions[:, 2])
        frozen = set(order[: fix_layers * nx * ny].tolist())
    # (Re)seed the scene from the ASE slab, preserving ASE's atom order.
    scene.cell = Cell(np.asarray(slab.cell), (True, True, True))
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
    "add_atom_site": _op_add_atom_site,
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

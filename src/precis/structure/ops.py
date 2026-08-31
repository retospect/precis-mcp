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

**Fragment-building ops** (``docs/backlog/nm-kind.md`` slice 2 — molecule-mode
fragment library): ``ring`` mints a regular n-gon of one element (the aromatic
6-ring template most callers reach for); ``attach`` rigidly bonds two
fragments together, moving the entire fragment containing ``from`` so it
bonds to ``to``; ``from_smiles`` mints a whole organic fragment from a SMILES
string via rdkit's ETKDG 3D embedder (the ``[chem]`` extra, lazy-imported —
never at module level). All three are pure (no store access). ``import_fragment``
(copy a whole other design's atoms/bonds in as a positioned fragment) needs the
store to hydrate a source design, so it is **not** in this module — it's a
handler-level expansion into ``add_atom``/``add_bond`` ops
(``handlers/structure.py``).
"""

from __future__ import annotations

import re
from typing import Any

import numpy as np

from . import elements
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


def _as_vec3(value: Any, default: np.ndarray, what: str) -> np.ndarray:
    """Defensive 3-vector coercion (mirrors ``_op_slab``'s numeric coercion
    discipline): ``None``/absent → ``default``; anything else must reshape to
    a length-3 float array, else a retryable ``OpError``."""
    if value is None:
        return np.array(default, dtype=float)
    try:
        return np.asarray(value, dtype=float).reshape(3)
    except (TypeError, ValueError) as exc:
        raise OpError(f"{what} must be a 3-vector [x, y, z], got {value!r}") from exc


def _plane_basis_uv(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Two orthonormal in-plane axes for a unit ``normal`` — mirrors
    ``probe._plane_basis``'s construction (seed off the axis least aligned
    with ``normal``, Gram-Schmidt, cross for the second axis), returning
    just the ``(u, v)`` pair this module needs."""
    seed = (
        np.array([1.0, 0.0, 0.0]) if abs(normal[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    )
    u = seed - (seed @ normal) * normal
    u = u / np.linalg.norm(u)
    v = np.cross(normal, u)
    return u, v


def _op_ring(scene: Scene, op: dict[str, Any]) -> None:
    """Mint a regular n-gon ring of one element (§ molecule-mode fragment
    library, nm-kind.md slice 2 — the aromatic 6-ring template).

    ``{"op": "ring", "element": "C", "n": 6, "aromatic": true, "center":
    [x,y,z], "normal": [nx,ny,nz], "bond_length": <Å, optional>}``.

    Atoms land in the plane through ``center`` (Cartesian, default the
    origin) perpendicular to ``normal`` (default ``+z``), evenly spaced on a
    circle of circumradius ``r = L / (2 sin(pi/n))`` where ``L`` is
    ``bond_length`` (default ``2 * covalent_radius(element) * 0.915`` — the
    0.915 factor shrinks the raw single-bond radius sum toward the shorter
    delocalized/aromatic bond length; for carbon this gives ~1.39 Å, the
    textbook benzene C-C bond). Consecutive atoms (ring closes last→first)
    get a declared bond: ``aromatic=true`` → order 1.5, kind ``aromatic``,
    and each atom's ``hybridization`` is set to ``sp2``; ``aromatic=false``
    → order 1, kind ``pairwise``, no hybridization declared. Labels mint via
    :meth:`Scene.next_label` in polygon order.
    """
    element = op.get("element")
    if not element:
        raise OpError("ring needs an 'element'")
    element = str(element)
    n_raw = op.get("n")
    if n_raw is None:
        raise OpError("ring needs an 'n' (ring size, 3-12)")
    try:
        n = int(n_raw)
    except (TypeError, ValueError) as exc:
        raise OpError(f"ring 'n' must be an integer, got {n_raw!r}") from exc
    if not (3 <= n <= 12):
        raise OpError(f"ring 'n' must be in [3, 12] (a ring needs 3-12 atoms), got {n}")
    aromatic = bool(op.get("aromatic", False))
    center = _as_vec3(op.get("center"), np.zeros(3), "ring 'center'")
    normal_raw = op.get("normal")
    normal = _as_vec3(normal_raw, np.array([0.0, 0.0, 1.0]), "ring 'normal'")
    nnorm = float(np.linalg.norm(normal))
    if nnorm < 1e-9:
        raise OpError(f"ring 'normal' must be a nonzero vector, got {normal_raw!r}")
    normal = normal / nnorm
    bl_raw = op.get("bond_length")
    if bl_raw is None:
        bond_length = 2.0 * elements.covalent_radius(element) * 0.915
    else:
        try:
            bond_length = float(bl_raw)
        except (TypeError, ValueError) as exc:
            raise OpError(
                f"ring 'bond_length' must be a number, got {bl_raw!r}"
            ) from exc
        if bond_length <= 0:
            raise OpError(f"ring 'bond_length' must be positive, got {bond_length!r}")
    radius = bond_length / (2.0 * np.sin(np.pi / n))
    u, v = _plane_basis_uv(normal)
    labels: list[str] = []
    for k in range(n):
        theta = 2.0 * np.pi * k / n
        cart = center + radius * (np.cos(theta) * u + np.sin(theta) * v)
        label = scene.next_label(element)
        frac = scene.cell.wrap(scene.cell.cart_to_frac(cart))
        scene.atoms[label] = Atom(
            label=label,
            element=element,
            frac=frac,
            hybridization="sp2" if aromatic else None,
        )
        labels.append(label)
    order = 1.5 if aromatic else 1.0
    kind = "aromatic" if aromatic else "pairwise"
    for k in range(n):
        i, j = labels[k], labels[(k + 1) % n]
        # wrap() may split a ring across a cell wall — carry each bond's MIC
        # image so image-trusting probes (bonds_through_plane) stay exact.
        _, img = scene.cell.mic(scene.atoms[i].frac, scene.atoms[j].frac)
        scene.bonds.append(
            Bond(i=i, j=j, order=order, kind=kind, provenance="declared", image=img)
        )


def _combined_adjacency(scene: Scene) -> dict[str, set[str]]:
    """Bond-graph adjacency over DECLARED + geometrically-INFERRED bonds —
    the "physically attached atoms must move together" definition of a
    fragment for :func:`_op_attach` (broader than the pure declared-only
    graph :mod:`.probe`/:mod:`.vsepr` use, since a fragment straddling a
    close but never-``add_bond``-ed contact still has to move as one rigid
    body)."""
    adj: dict[str, set[str]] = {label: set() for label in scene.atoms}
    for b in scene.bonds:
        if b.i in adj and b.j in adj:
            adj[b.i].add(b.j)
            adj[b.j].add(b.i)
    from . import probe  # local import: probe.py doesn't import ops.py, safe

    for b in probe.detect_bonds(scene):
        if b.i in adj and b.j in adj:
            adj[b.i].add(b.j)
            adj[b.j].add(b.i)
    return adj


def _fragment_labels(adj: dict[str, set[str]], start: str) -> set[str]:
    """The connected component containing ``start`` over ``adj`` (BFS/DFS,
    order doesn't matter — a plain set)."""
    seen = {start}
    stack = [start]
    while stack:
        cur = stack.pop()
        for nxt in adj.get(cur, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _neighbor_unit_sum(
    scene: Scene, adj: dict[str, set[str]], label: str
) -> np.ndarray | None:
    """Sum of unit vectors from ``label`` to its bond-graph neighbours
    (:func:`_combined_adjacency`), each MIC-unwrapped relative to ``label``
    first (the ``_resolve_site`` discipline — a neighbour across a cell wall
    isn't averaged from its wrapped-away image). ``None`` if ``label`` has
    no neighbours."""
    neighbors = adj.get(label, set())
    if not neighbors:
        return None
    atom = scene.atoms[label]
    total = np.zeros(3)
    for other_label in neighbors:
        other = scene.atoms[other_label]
        _, img = scene.cell.mic(atom.frac, other.frac)
        vec = scene.cell.frac_to_cart(
            other.frac + np.array(img, dtype=float) - atom.frac
        )
        norm = float(np.linalg.norm(vec))
        if norm > 1e-9:
            total += vec / norm
    return total


def _attach_direction(op: dict[str, Any], key: str, atom_label: str) -> np.ndarray:
    """The explicit ``direction``/``from_direction`` fallback arg (unit
    vector), for when a bonding-direction sum is unavailable or degenerate.
    Raises a retryable ``OpError`` naming the atom and the arg when absent."""
    raw = op.get(key)
    if raw is None:
        raise OpError(
            f"attach can't find a bonding direction at {atom_label!r} — it has "
            f"no bonded neighbours (or their directions cancel out, e.g. "
            f"perfectly linear/symmetric surroundings); pass an explicit "
            f"{key!r}: [x, y, z]"
        )
    try:
        vec = np.asarray(raw, dtype=float).reshape(3)
    except (TypeError, ValueError) as exc:
        raise OpError(f"attach {key!r} must be a 3-vector, got {raw!r}") from exc
    norm = float(np.linalg.norm(vec))
    if norm < 1e-9:
        raise OpError(f"attach {key!r} must be a nonzero vector, got {raw!r}")
    return vec / norm


def _rotation_aligning(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Rotation matrix ``R`` such that ``R @ normalize(a) == normalize(b)``
    (Rodrigues' formula). Antiparallel inputs (``sin(theta) ~ 0``, ``cos ~
    -1``) get a stable 180°-about-a-perpendicular-axis rotation instead of
    dividing by zero; parallel inputs (``cos ~ 1``) get the identity."""
    a_hat = a / np.linalg.norm(a)
    b_hat = b / np.linalg.norm(b)
    cross = np.cross(a_hat, b_hat)
    s = float(np.linalg.norm(cross))
    c = float(a_hat @ b_hat)
    if s < 1e-9:
        if c > 0:
            return np.eye(3)
        seed = (
            np.array([1.0, 0.0, 0.0])
            if abs(a_hat[0]) < 0.9
            else np.array([0.0, 1.0, 0.0])
        )
        perp = seed - (seed @ a_hat) * a_hat
        perp = perp / np.linalg.norm(perp)
        return 2.0 * np.outer(perp, perp) - np.eye(3)
    vx = np.array(
        [
            [0.0, -cross[2], cross[1]],
            [cross[2], 0.0, -cross[0]],
            [-cross[1], cross[0], 0.0],
        ]
    )
    return np.eye(3) + vx + vx @ vx * ((1.0 - c) / (s * s))


def _op_attach(scene: Scene, op: dict[str, Any]) -> None:
    """Rigidly attach one fragment to another by bonding ``from`` to ``to``
    (§ molecule-mode fragment library, nm-kind.md slice 2).

    ``{"op": "attach", "from": "aC7", "to": "aC1", "order": 1, "distance":
    <Å, optional>, "direction": [x,y,z] (optional, 'to' fallback),
    "from_direction": [x,y,z] (optional, 'from' fallback)}``.

    Moves the ENTIRE fragment containing ``from`` (the connected component
    over declared + inferred bonds, :func:`_combined_adjacency` — physically
    attached atoms must move together) so ``from`` bonds to ``to``, then
    declares the new bond. ``from``/``to`` already in the same fragment is
    rejected — that's a ring closure, not an attach.

    Geometry: at ``to``, ``d_to = -normalize(sum of unit vectors from 'to'
    to each of its bond-graph neighbours)`` — the open coordination
    direction away from ``to``'s existing bonds. Same construction for
    ``d_from`` at ``from`` within its own fragment. A degenerate/absent sum
    falls back to the ``direction``/``from_direction`` arg, else raises.
    The fragment is rotated (Rodrigues, about ``from``'s position) so
    ``d_from`` maps to ``-d_to``, then translated so
    ``from's position = to's position + distance * d_to`` (``distance``
    defaults to the sum of covalent radii). Under pbc, the fragment is
    unwrapped to the MIC image nearest ``from`` (the ``_resolve_site``
    per-atom-relative-to-one-reference discipline, not a bond-graph walk)
    before the rotation — exact for a compact fragment, and exact by
    construction in molecule mode (pbc all-False).

    No torsional choice is made — the dihedral about the new bond is
    arbitrary; ``relax``/``vsepr.advisories`` (``pi_twist``) handle it
    downstream.
    """
    from_label = op.get("from")
    to_label = op.get("to")
    if not from_label or not to_label:
        raise OpError("attach needs 'from' and 'to' atom labels")
    from_label = str(from_label)
    to_label = str(to_label)
    from_atom = _require_atom(scene, from_label)
    to_atom = _require_atom(scene, to_label)

    adj = _combined_adjacency(scene)
    from_frag = _fragment_labels(adj, from_label)
    if to_label in from_frag:
        raise OpError(
            "attach joins two fragments — for a ring closure within one "
            "fragment, declare the bond with add_bond instead"
        )

    order_raw = op.get("order", 1.0)
    try:
        order = float(order_raw)
    except (TypeError, ValueError) as exc:
        raise OpError(f"attach 'order' must be a number, got {order_raw!r}") from exc

    dist_raw = op.get("distance")
    if dist_raw is None:
        distance = elements.covalent_radius(
            from_atom.element
        ) + elements.covalent_radius(to_atom.element)
    else:
        try:
            distance = float(dist_raw)
        except (TypeError, ValueError) as exc:
            raise OpError(
                f"attach 'distance' must be a number, got {dist_raw!r}"
            ) from exc

    to_sum = _neighbor_unit_sum(scene, adj, to_label)
    if to_sum is None or float(np.linalg.norm(to_sum)) < 1e-6:
        d_to = _attach_direction(op, "direction", to_label)
    else:
        d_to = -to_sum / float(np.linalg.norm(to_sum))

    from_sum = _neighbor_unit_sum(scene, adj, from_label)
    if from_sum is None or float(np.linalg.norm(from_sum)) < 1e-6:
        d_from = _attach_direction(op, "from_direction", from_label)
    else:
        d_from = -from_sum / float(np.linalg.norm(from_sum))

    # unwrap the fragment relative to `from` (per-atom MIC against one fixed
    # reference — the `_resolve_site` discipline; exact in molecule mode).
    from_frac = from_atom.frac
    unwrapped: dict[str, np.ndarray] = {}
    for label in from_frag:
        atom = scene.atoms[label]
        _, img = scene.cell.mic(from_frac, atom.frac)
        unwrapped[label] = scene.cell.frac_to_cart(
            atom.frac + np.array(img, dtype=float)
        )

    pivot = unwrapped[from_label]
    rot = _rotation_aligning(d_from, -d_to)
    to_cart = scene.cell.frac_to_cart(to_atom.frac)
    target_from_cart = to_cart + distance * d_to

    for label in from_frag:
        rel = unwrapped[label] - pivot
        new_cart = target_from_cart + rot @ rel
        scene.atoms[label].frac = scene.cell.wrap(scene.cell.cart_to_frac(new_cart))

    # The wrapped target position may sit in a different periodic image than
    # `to` (a wall-adjacent `to` with d_to pointing across it) — declare the
    # bond with its MIC image, not a blind (0,0,0), for the image-trusting
    # probes (bonds_through_plane / bonds_in_sphere).
    _, img = scene.cell.mic(scene.atoms[from_label].frac, to_atom.frac)
    scene.bonds.append(
        Bond(
            i=from_label,
            j=to_label,
            order=order,
            kind=str(op.get("kind", "pairwise")),
            provenance="declared",
            image=img,
        )
    )


def _op_from_smiles(scene: Scene, op: dict[str, Any]) -> None:
    """Mint a whole organic fragment from a SMILES string (§ molecule-mode
    fragment library, nm-kind.md slice 2 — the rdkit-embedded complement to
    the hand-built ``ring``/``attach`` primitives).

    ``{"op": "from_smiles", "smiles": "c1ccccc1O", "offset": [x,y,z]
    (Cartesian Å, optional, default origin), "seed": <int, optional,
    default 0>}``.

    Needs rdkit (the ``[chem]`` extra) — imported lazily, here, only when
    this op actually runs; a missing rdkit raises a clean, retryable
    ``OpError`` naming the extra, never a bare ``ImportError``. The SMILES is
    parsed (``Chem.MolFromSmiles``), hydrogenated (``Chem.AddHs``), and
    embedded into 3D with ``AllChem.EmbedMolecule`` under ETKDGv3 params
    seeded from ``seed`` — same ``(smiles, seed)`` in, bit-identical geometry
    out (rdkit's embedder is deterministic per seed). A follow-up MMFF
    force-field cleanup (``AllChem.MMFFOptimizeMolecule``) is attempted but
    **best-effort**: any failure (missing MMFF params for an exotic atom,
    non-convergence) is swallowed — the raw ETKDG geometry is acceptable on
    its own.

    One scene atom per rdkit atom (element = ``GetSymbol()``, position = the
    embedded conformer + ``offset``); an aromatic atom gets
    ``hybridization="sp2"``. One declared bond per rdkit bond: an
    ``AROMATIC`` rdkit bond → order 1.5, kind ``aromatic``; anything else →
    order = ``GetBondTypeAsDouble()`` (1/2/3), kind ``pairwise``. Each bond's
    periodic image is set via ``scene.cell.mic`` (the ``ring``/``attach``
    discipline — ``wrap()`` may split the fragment across a cell wall).

    v1 scope: geometry only. Formal charges, stereochemistry, and anything
    else beyond what ETKDG's distance-geometry embedding encodes are not
    carried into the scene.
    """
    smiles = op.get("smiles")
    if not smiles:
        raise OpError("from_smiles needs a 'smiles' string")
    smiles = str(smiles)
    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem
    except ImportError as exc:  # pragma: no cover - rdkit is the [chem] extra
        raise OpError(
            "from_smiles needs rdkit (the [chem] extra) to parse SMILES and "
            "embed 3D geometry"
        ) from exc

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise OpError(f"{smiles!r} is not parseable as SMILES")
    mol = Chem.AddHs(mol)

    seed_raw = op.get("seed", 0)
    try:
        seed = int(seed_raw) if seed_raw is not None else 0
    except (TypeError, ValueError) as exc:
        raise OpError(
            f"from_smiles 'seed' must be an integer, got {seed_raw!r}"
        ) from exc

    # The installed `rdkit-stubs` package's AllChem.pyi doesn't re-export
    # these three (they live in rdDistGeom/rdForceFieldHelpers at runtime,
    # via AllChem's real `from .rdDistGeom import *` — the stub just misses
    # the wildcard) — real, present attributes at runtime; a stub gap only.
    params = AllChem.ETKDGv3()  # type: ignore[attr-defined]
    params.randomSeed = seed
    embed_fail_msg = (
        "3D embedding failed — the SMILES may be valid but geometrically "
        "pathological; try a different seed"
    )
    try:
        embed_status = AllChem.EmbedMolecule(mol, params)  # type: ignore[attr-defined]
    except Exception as exc:
        raise OpError(embed_fail_msg) from exc
    if embed_status != 0:
        raise OpError(embed_fail_msg)

    try:
        AllChem.MMFFOptimizeMolecule(mol)  # type: ignore[attr-defined]
    except Exception:
        pass  # best-effort cleanup only — raw ETKDG geometry is acceptable

    offset = _as_vec3(op.get("offset"), np.zeros(3), "from_smiles 'offset'")

    conf = mol.GetConformer()
    labels: list[str] = [""] * mol.GetNumAtoms()
    for atom in mol.GetAtoms():
        idx = atom.GetIdx()
        pos = conf.GetAtomPosition(idx)
        cart = np.array([pos.x, pos.y, pos.z]) + offset
        element = atom.GetSymbol()
        label = scene.next_label(element)
        frac = scene.cell.wrap(scene.cell.cart_to_frac(cart))
        scene.atoms[label] = Atom(
            label=label,
            element=element,
            frac=frac,
            hybridization="sp2" if atom.GetIsAromatic() else None,
        )
        labels[idx] = label

    for bond in mol.GetBonds():
        i_label = labels[bond.GetBeginAtomIdx()]
        j_label = labels[bond.GetEndAtomIdx()]
        if bond.GetBondType() == Chem.BondType.AROMATIC:
            order, kind = 1.5, "aromatic"
        else:
            order, kind = float(bond.GetBondTypeAsDouble()), "pairwise"
        # wrap() may split the fragment across a cell wall — carry each
        # bond's MIC image (the ring/attach discipline).
        _, img = scene.cell.mic(scene.atoms[i_label].frac, scene.atoms[j_label].frac)
        scene.bonds.append(
            Bond(
                i=i_label,
                j=j_label,
                order=order,
                kind=kind,
                provenance="declared",
                image=img,
            )
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
    "ring": _op_ring,
    "attach": _op_attach,
    "from_smiles": _op_from_smiles,
}

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
validation path for both raw and site-symbolic placement. With no
``anchors`` (gr547306) the op picks them itself: :func:`_auto_site` lists
the slab top layer's top/bridge/fcc/hcp sites and takes the free one
nearest ``near`` (else the cell centre), "free" meaning no placed atom
within bond-inference range of a non-anchor atom — the validator's own
bond rule, so an auto-placed group never trips an overlap or an extra
bond. It never binds to a ``near`` atom; naming that atom as an anchor
does. ``set_element`` takes the same by-site form for substitutions.
``add_adsorbate`` is its multi-atom twin: a catalogued group (H · O · N ·
OH · H2O · NH · NH2 · NH3 · C · CH · CH2 · CH3 · CH4) placed from the same
named site with its own gas-phase internal geometry and bonds, so a
coverage/solvation series never hand-places an H.

**Reaction-endpoint ops** (gr512126, NEB endpoint authoring): ``transfer``
moves ONE atom onto an acceptor atom along the acceptor's open
coordination direction — the elementary bond-swap step of a surface
reaction (CH4 + O* → CH3* + OH* is one transfer of an H from C to O);
``move_group`` rigidly moves a whole declared-bond fragment (an adsorbate)
so its named atom lands on a new site. Both keep atom labels stable, so
the edited design stays pairable with its pre-edit parent.

**Fragment-building ops** (built for the molecule-mode fragment library the
retired ``nm`` kind — now ``se`` atomic mode, docs/backlog/nm-se-merge.md —
consumes): ``ring`` mints a regular n-gon of one element (the aromatic
6-ring template most callers reach for); ``attach`` rigidly bonds two
fragments together, moving the entire fragment containing ``from`` so it
bonds to ``to``; ``from_smiles`` mints a whole organic fragment from a SMILES
string via rdkit's ETKDG 3D embedder (the ``[chem]`` extra, lazy-imported —
never at module level). All three are pure (no store access). ``import_fragment``
(copy a whole other design's atoms/bonds in as a positioned fragment) needs the
store to hydrate a source design, so it is **not** in this module — it's a
handler-level expansion into ``add_atom``/``add_bond`` ops
(``handlers/structure.py``).

Unit enclave (package docstring): Å-native; every length-valued op argument
(bond lengths, ring radii, displacement vectors, ...) is Å, never SI.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

import numpy as np

from precis.errors import BadInput
from precis.utils.units import parse_angle_deg

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
    """A rejected op (bad reference, unknown op, malformed payload).

    ``options`` carries the closed-vocabulary / ambiguous-candidate list a
    refusal wants to surface (e.g. ``transfer``'s equidistant tie) — the
    handler boundary forwards it onto the ``BadInput``'s ``options=``.
    ``next`` is an op-specific hint the handler forwards as the
    ``BadInput``'s ``next=`` in place of its generic skill pointer.
    ``example`` is a valid op that LEADS the message (``valid form: <op> —
    <cause>``), so it survives a capped echo of the error such as a quest's
    rejected-proposal note (``quest.compute._REJECT_REASON_CAP``) however
    long the cause's ``got`` repr runs (gr547306)."""

    def __init__(
        self,
        cause: str,
        *,
        options: Sequence[str] | None = None,
        next: str | None = None,
        example: str | None = None,
    ) -> None:
        self.options = list(options) if options is not None else None
        self.next = next
        super().__init__(f"valid form: {example} — {cause}" if example else cause)


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
    charge_raw = op.get("charge", 0)
    try:
        charge = int(charge_raw) if charge_raw is not None else 0
    except (TypeError, ValueError) as exc:
        raise OpError(
            f"add_atom 'charge' must be an integer, got {charge_raw!r}"
        ) from exc
    site = op.get("site")
    if site is not None and (
        not isinstance(site, str) or site not in _SUBSURFACE_SITE_INTENT
    ):
        raise OpError(
            f"add_atom 'site' declares a deliberate below-surface "
            f"placement — one of {sorted(_SUBSURFACE_SITE_INTENT)}, got "
            f"{site!r}; surface sites (top|bridge|hollow|fcc|hcp) belong "
            "to add_atom_site",
            example=_SITE_EXAMPLE,
            next=_SITE_HINT,
        )
    scene.atoms[label] = Atom(
        label=label,
        element=element,
        frac=frac,
        magmom=op.get("magmom"),
        oxidation=op.get("oxidation"),
        charge=charge,
        hybridization=op.get("hybridization"),
        site=site,
    )


#: The intent markers raw ``add_atom`` accepts on its ``site`` key
#: (gr549760): a declared below-surface placement — ``"subsurface"`` for
#: an atom meant one layer down, ``"interstitial"`` for a bulk-style
#: hole. The op does no site resolution; it only stamps :attr:`Atom.site`
#: so preflight treats a deliberate below-top-layer atom as intended
#: instead of buried (a hard overlap still fails). The surface site
#: vocabulary lives on ``add_atom_site``.
_SUBSURFACE_SITE_INTENT = frozenset({"subsurface", "interstitial"})


#: Anchor count required per site type (v1 closed set — no interstitials yet;
#: an octahedral/tetrahedral void needs a different resolver, backlogged in
#: the estimate-kind design doc). ``fcc`` and ``hcp`` are the two hollows of
#: a (111) surface: an hcp hollow sits over an atom of the layer below, an
#: fcc hollow over a gap in it (:func:`_hollow_kind`).
_SITE_ANCHOR_COUNT = {"top": 1, "bridge": 2, "hollow": 3, "fcc": 3, "hcp": 3}

#: Metal atoms within this many Å in z are one slab layer — the
#: :mod:`.invariants` layer tolerance (fcc(111) layers sit ~2 Å apart).
_LAYER_TOL = 0.5

#: In-plane Å within which an atom of the layer below sits under a hollow,
#: making it the hcp hollow. Under an fcc hollow the nearest such atom is
#: one hollow-to-atom distance (d_nn/√3, ~1.5 Å on Cu) away.
_HCP_XY_TOL = 0.5

#: The working op a site refusal leads with, and the ``next=`` hint after
#: it (gr547306: the quest agent kept hand-placing dopant adatoms). ``Ni``
#: is a syntax example, not a suggested dopant.
_SITE_EXAMPLE = '{"op":"add_atom_site","element":"Ni","site":{"type":"fcc"}}'
_SITE_HINT = (
    "omit 'anchors' and precis picks a free top/bridge/fcc/hcp site on the "
    "top layer; 'near': <label or element> picks the free one beside that "
    "atom (give 'anchors' or 'near', not both). "
    "Recipe: get(kind='skill', id='precis-structure-slab-help')"
)

#: The keys a site object reads; any other is refused, so a typo
#: (``anchor``) never silently falls through to an auto-picked site.
_SITE_KEYS = ("type", "anchors", "near")

#: Misspellings of ``anchors`` the unknown-key refusal names it for.
_ANCHOR_TYPOS = frozenset({"anchor", "labels", "label", "atoms", "atom"})

#: ``set_element`` site types: the lattice layer a by-name substitution
#: lands in (``top`` = the surface layer, ``subsurface`` = the one below).
_SUBST_LAYER = {"top": -1, "subsurface": -2}

_SUBST_EXAMPLE = '{"op":"set_element","element":"Ni","site":{"type":"top"}}'
_SUBST_HINT = (
    "precis picks a host atom of that layer ('near': <label or element> "
    "picks the one beside that atom); or give 'atom' instead of 'site': "
    '{"op":"set_element","atom":"aCu28","element":"Ni"}'
)


def _check_site_keys(site: dict[str, Any], allowed: tuple[str, ...]) -> None:
    """Refuse a site object carrying a key outside ``allowed``, naming it
    and the valid ones (``did you mean 'anchors'?`` for its misspellings)."""
    unknown = sorted(str(k) for k in site if k not in allowed)
    if not unknown:
        return
    guess = (
        " — did you mean 'anchors'?"
        if "anchors" in allowed and _ANCHOR_TYPOS & set(unknown)
        else ""
    )
    form = (
        (_SITE_EXAMPLE, _SITE_HINT)
        if allowed == _SITE_KEYS
        else (_SUBST_EXAMPLE, _SUBST_HINT)
    )
    raise OpError(
        f"unknown site key(s) {unknown}; a site reads only {list(allowed)}{guess}",
        example=form[0],
        next=form[1],
    )


def _op_add_atom_site(scene: Scene, op: dict[str, Any]) -> None:
    """Place an atom by NAMING a site instead of guessing coordinates.

    ``{"op": "add_atom_site", "element": "H", "site": {"type": "hollow",
    "anchors": ["aPd1", "aPd2", "aPd3"]}, "height": <optional Å>}``. Resolves
    the site to Cartesian coordinates (:func:`_resolve_site`) and delegates
    to :func:`_op_add_atom` for the actual mint — same label/validation path
    as raw ``add_atom``. Without ``anchors`` precis picks the site itself
    (:func:`_auto_site`).
    """
    element = op.get("element")
    if not element:
        raise OpError(
            "add_atom_site needs an 'element'", example=_SITE_EXAMPLE, next=_SITE_HINT
        )
    site = op.get("site")
    if not isinstance(site, dict):
        raise OpError(
            "add_atom_site needs a 'site' object: "
            "{'type': 'top'|'bridge'|'hollow'|'fcc'|'hcp', 'anchors'?: [labels]}",
            example=_SITE_EXAMPLE,
            next=_SITE_HINT,
        )
    frac = _resolve_site(scene, site, str(element), op.get("height"))
    delegate = dict(op)
    delegate.pop("site", None)
    delegate.pop("height", None)
    delegate.pop("cart", None)
    delegate["frac"] = [float(x) for x in frac]
    _op_add_atom(scene, delegate)


def _resolve_site(
    scene: Scene,
    site: dict[str, Any],
    element: str,
    height: Any,
    *,
    group: list[tuple[str, np.ndarray]] | None = None,
    ignore: frozenset[str] | set[str] = frozenset(),
) -> np.ndarray:
    """Resolve a symbolic site (type + anchor labels) to fractional coords.

    xy = centroid of the anchors' Cartesian xy, PBC/minimum-image aware (each
    anchor is first moved to the periodic image nearest the first anchor —
    the same :meth:`Cell.mic` the tick's PBC scar block warns about, so a
    hollow site spanning a cell wall doesn't average a wrapped-away copy).
    z = the anchors' highest z + ``height`` (default: the covalent-radius-sum
    rule, see :func:`_default_site_height`).

    No ``anchors`` key → :func:`_auto_site` picks them; ``group`` (the
    placed atoms as ``(element, Cartesian offset from the binding atom)``)
    and ``ignore`` (labels that are moving anyway) feed its clearance
    check. An unknown key, or ``anchors`` with ``near``, refuses: either
    would otherwise place the atom somewhere the caller didn't ask for.
    """
    _check_site_keys(site, _SITE_KEYS)
    if "anchors" in site and "near" in site:
        raise OpError(
            "a site takes 'anchors' (you name the site) or 'near' (precis "
            "picks the free one beside that atom), not both",
            example=_SITE_EXAMPLE,
            next=_SITE_HINT,
        )
    site_type = site.get("type")
    if site_type not in _SITE_ANCHOR_COUNT:
        raise OpError(
            f"add_atom_site 'site.type' must be one of "
            f"{sorted(_SITE_ANCHOR_COUNT)}, got {site_type!r} — interstitials "
            "aren't supported yet",
            example=_SITE_EXAMPLE,
            next=_SITE_HINT,
        )
    anchors = site.get("anchors")
    if "anchors" not in site:
        return _auto_site(
            scene, site, element, height, group or [(element, np.zeros(3))], ignore
        )
    if not isinstance(anchors, list) or not anchors:
        raise OpError(
            "add_atom_site 'site.anchors' must be a list of atom labels, or "
            "absent for precis to pick the site",
            example=_SITE_EXAMPLE,
            next=_SITE_HINT,
        )
    want = _SITE_ANCHOR_COUNT[site_type]
    if len(anchors) != want:
        raise OpError(
            f"a {site_type!r} site needs exactly {want} anchor(s), got "
            f"{len(anchors)}: {anchors!r}",
            example=_SITE_EXAMPLE,
            next=_SITE_HINT,
        )
    if len(set(anchors)) != len(anchors):
        raise OpError(
            f"add_atom_site anchors must be distinct labels, got {anchors!r}",
            example=_SITE_EXAMPLE,
            next=_SITE_HINT,
        )
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
    if site_type in ("fcc", "hcp"):
        kind = _hollow_kind(scene, xy, z_top)
        if kind != site_type:
            raise OpError(
                f"anchors {anchors!r} form an {kind} hollow, not {site_type} — "
                f"say type {kind!r}, or omit 'anchors' and precis picks a free "
                f"{site_type} site",
                example=_SITE_EXAMPLE,
                next=_SITE_HINT,
            )
    h = _site_height(anchor_elements, element, height)
    target_cart = np.array([xy[0], xy[1], z_top + h])
    return scene.cell.wrap(scene.cell.cart_to_frac(target_cart))


def _site_height(anchor_elements: list[str], element: str, height: Any) -> float:
    """``height`` as Å, or the covalent-radius default when it is absent."""
    if height is None:
        return _default_site_height(anchor_elements, element)
    try:
        return float(height)
    except (TypeError, ValueError) as exc:
        raise OpError(
            f"add_atom_site 'height' must be a number, got {height!r}"
        ) from exc


def _metal_layers(scene: Scene) -> list[tuple[float, list[str]]]:
    """The slab's layers bottom→top as ``(mean z Å, labels)``.

    Metal atoms (``elements.max_valence`` is ``None``, the rule
    :func:`_largest_metal_component` uses) clustered by Cartesian z. A layer
    counts only when it holds more than half the fullest layer's atoms, so
    a dopant adatom above the surface is not mistaken for the surface. A
    substituted dopant stays in its layer."""
    metal = sorted(
        (float(scene.cell.frac_to_cart(a.frac)[2]), label)
        for label, a in scene.atoms.items()
        if elements.max_valence(a.element) is None
    )
    clusters: list[list[tuple[float, str]]] = []
    for z, label in metal:
        if clusters and z - clusters[-1][-1][0] <= _LAYER_TOL:
            clusters[-1].append((z, label))
        else:
            clusters.append([(z, label)])
    if not clusters:
        return []
    full = max(len(c) for c in clusters)
    return [
        (float(np.mean([z for z, _ in c])), [label for _, label in c])
        for c in clusters
        if len(c) > full / 2
    ]


#: The atom-free span (Å) along some cell axis that marks a *surface* cell —
#: the ``slab`` op's default vacuum is 10 Å a side while a bulk cell's largest
#: gap is an interlayer spacing (~2 Å), so this sits between them.
_SLAB_VACUUM_A = 4.0


def _is_slab_scene(scene: Scene) -> bool:
    """True when ``scene`` is a surface slab — the ``slab`` op's product.

    The ``slab`` op (ASE ``fcc111``) always orients the cell the same way:
    lattice vectors 0 and 1 span the surface, vector 2 is the surface
    normal carrying the vacuum gap, and :func:`_metal_layers` stacks the
    dense metal layers along it — so the layer stack and the gap are read
    along the SAME axis here. A scene is a slab when it holds ≥2 dense
    metal layers, is periodic on the two in-plane axes, and shows the
    atom-free span (≥ :data:`_SLAB_VACUUM_A`) along axis 2 ONLY: a slab
    tiles in-plane so its in-plane gaps never exceed an interatomic
    spacing, while a metal cluster in a periodic vacuum box has the gap
    on every side and is not a slab. A bulk cell fails the axis-2 gap
    test (no vacuum — an interstitial ``add_atom`` there is legitimate),
    a molecule the layer test."""
    pbc = scene.cell.pbc
    if not (pbc[0] and pbc[1]):
        return False
    if len(_metal_layers(scene)) < 2:
        return False
    lat = scene.cell.lattice
    scaled = np.asarray([a.frac for a in scene.atoms.values()], dtype=float) % 1.0

    def max_gap(ax: int) -> float:
        h = float(np.linalg.norm(lat[ax]))
        if h < 1e-9:
            return 0.0
        coords = np.sort(scaled[:, ax])
        gaps = np.diff(np.concatenate([coords, coords[:1] + 1.0]))
        return float(gaps.max()) * h

    if max_gap(2) < _SLAB_VACUUM_A:
        return False
    return max(max_gap(0), max_gap(1)) < _SLAB_VACUUM_A


def _in_plane_shifts(scene: Scene) -> list[np.ndarray]:
    """Cartesian translations to the 3×3 in-plane periodic images."""
    lat = scene.cell.lattice
    pbc = scene.cell.pbc
    return [
        i * lat[0] + j * lat[1]
        for i in (-1, 0, 1)
        if pbc[0] or i == 0
        for j in (-1, 0, 1)
        if pbc[1] or j == 0
    ]


def _hollow_kind(
    scene: Scene,
    xy: np.ndarray,
    z: float,
    layers: list[tuple[float, list[str]]] | None = None,
) -> str:
    """``'hcp'`` when an atom of the slab layer below ``z`` sits under the
    in-plane point ``xy``, else ``'fcc'``. ``layers`` is a precomputed
    :func:`_metal_layers`."""
    layers = _metal_layers(scene) if layers is None else layers
    below = [labels for zl, labels in layers if zl < z - _LAYER_TOL]
    if not below:
        raise OpError(
            "telling an fcc from an hcp hollow needs the slab layer under it — "
            "build at least two layers, or use type 'hollow'",
            example=_SITE_EXAMPLE,
            next=_SITE_HINT,
        )
    shifts = _in_plane_shifts(scene)
    for label in below[-1]:
        p = scene.cell.frac_to_cart(scene.atoms[label].frac)
        for s in shifts:
            if float(np.linalg.norm((p + s)[:2] - xy)) < _HCP_XY_TOL:
                return "hcp"
    return "fcc"


def _surface_sites(
    scene: Scene, layers: list[tuple[float, list[str]]], site_type: str
) -> list[tuple[tuple[str, ...], np.ndarray]]:
    """Every distinct ``site_type`` site of the top slab layer (the last of
    ``layers``) as ``(anchor labels, Cartesian point at the highest
    anchor's z)``.

    Neighbours are the in-plane periodic images within 1.2× the layer's
    nearest-neighbour distance, so a small cell still yields all its
    bridges and hollows. Sites are de-duplicated by wrapped in-plane
    position; iteration follows sorted labels, so the result is
    deterministic."""
    top = sorted(layers[-1][1])
    base = {la: scene.cell.frac_to_cart(scene.atoms[la].frac) for la in top}
    images = [(la, p + s) for la, p in base.items() for s in _in_plane_shifts(scene)]
    seps = [float(np.linalg.norm(q - p)) for p in base.values() for _lb, q in images]
    cutoff = 1.2 * min((d for d in seps if d > 1e-6), default=0.0)
    seen: set[tuple[float, float]] = set()
    out: list[tuple[tuple[str, ...], np.ndarray]] = []

    def keep(anchors: tuple[str, ...], pts: list[np.ndarray]) -> None:
        point = np.mean(pts, axis=0)
        point[2] = max(float(p[2]) for p in pts)
        frac = scene.cell.cart_to_frac(point)
        key = (round(float(frac[0]), 3) % 1.0, round(float(frac[1]), 3) % 1.0)
        if key in seen:
            return
        seen.add(key)
        if site_type in ("fcc", "hcp") and (
            _hollow_kind(scene, point[:2], float(point[2]), layers) != site_type
        ):
            return
        out.append((anchors, point))

    for la, p in base.items():
        if site_type == "top":
            keep((la,), [p])
            continue
        nbrs = [
            (lb, q) for lb, q in images if 1e-6 < float(np.linalg.norm(q - p)) <= cutoff
        ]
        if site_type == "bridge":
            for lb, q in nbrs:
                keep((la, lb), [p, q])
            continue
        for n, (lb, q) in enumerate(nbrs):
            for lc, r in nbrs[n + 1 :]:
                if float(np.linalg.norm(r - q)) <= cutoff:
                    keep((la, lb, lc), [p, q, r])
    return out


def _mic_dists(scene: Scene, frac: np.ndarray, others: np.ndarray) -> np.ndarray:
    """Minimum-image distances (Å) from one fractional point to each row of
    ``others``: reduce per periodic axis, then take the nearest of the 27
    neighbouring images (exact for the slab cells the site ops run on)."""
    pbc = np.asarray(scene.cell.pbc, dtype=bool)
    d = np.asarray(others, dtype=float) - np.asarray(frac, dtype=float)
    d[:, pbc] -= np.round(d[:, pbc])
    shifts = np.array(
        [
            (i, j, k)
            for i in (-1, 0, 1)
            for j in (-1, 0, 1)
            for k in (-1, 0, 1)
            if (pbc[0] or i == 0) and (pbc[1] or j == 0) and (pbc[2] or k == 0)
        ],
        dtype=float,
    )
    cart = (d[:, None, :] + shifts[None, :, :]) @ scene.cell.lattice
    return np.linalg.norm(cart, axis=-1).min(axis=1)


def _near_fracs(scene: Scene, near: Any) -> np.ndarray | None:
    """A site's ``near`` — an atom label, or an element meaning every atom
    of it — as fractional positions; ``None`` when absent."""
    if near is None:
        return None
    key = str(near)
    if key in scene.atoms:
        return np.array([scene.atoms[key].frac])
    same = [a.frac for a in scene.atoms.values() if a.element == key]
    if same:
        return np.array(same)
    raise OpError(
        f"site 'near' {key!r} is neither an atom label nor an element in the "
        f"scene — {_no_atom_msg(scene, key)}",
        example=_SITE_EXAMPLE,
        next=_SITE_HINT,
    )


def _ref_dist(scene: Scene, frac: np.ndarray, near: np.ndarray | None) -> float:
    """How far ``frac`` sits from ``near``, or from the cell's in-plane
    centre when no ``near`` was given — the order sites are tried in."""
    ref = near if near is not None else np.array([[0.5, 0.5, float(frac[2])]])
    return float(_mic_dists(scene, frac, ref).min())


def _site_is_free(
    scene: Scene,
    point: np.ndarray,
    group: list[tuple[str, np.ndarray]],
    exclude: set[str],
) -> bool:
    """True when no placed atom lands within bond-inference range
    (:func:`elements.bond_cutoff`) of an existing atom other than the
    site's own anchors — the placement bonds to nothing the validator would
    then flag as an overlap or an extra bond."""
    labels = [la for la in scene.atoms if la not in exclude]
    if not labels:
        return True
    others = np.array([scene.atoms[la].frac for la in labels])
    els = [scene.atoms[la].element for la in labels]
    for el, offset in group:
        d = _mic_dists(scene, scene.cell.cart_to_frac(point + offset), others)
        cut = np.array([elements.bond_cutoff(el, e) for e in els])
        if bool(np.any(d < cut)):
            return False
    return True


def _auto_site(
    scene: Scene,
    site: dict[str, Any],
    element: str,
    height: Any,
    group: list[tuple[str, np.ndarray]],
    ignore: frozenset[str] | set[str],
) -> np.ndarray:
    """Pick a ``site.type`` site on the slab's top layer when the caller
    named no anchors (gr547306: hand-picked anchors and hand-typed z kept
    failing as clashes and floating atoms).

    Candidates are tried nearest ``site.near`` first (an atom label, or an
    element: nearest any atom of it), else nearest the cell's in-plane
    centre; the first one whose placed atoms clear every non-anchor atom by
    its bond-inference distance wins. Height follows the anchored rule."""
    site_type = str(site["type"])
    layers = _metal_layers(scene)
    if not layers:
        raise OpError(
            f"a {site_type!r} site without 'anchors' needs a metal slab to pick "
            "from — build it with the slab op first, or name the anchors",
            example=_SITE_EXAMPLE,
            next=_SITE_HINT,
        )
    near = _near_fracs(scene, site.get("near"))
    tried: list[tuple[float, tuple[str, ...], np.ndarray]] = []
    for anchors, point in _surface_sites(scene, layers, site_type):
        h = _site_height([scene.atoms[a].element for a in anchors], element, height)
        target = point + np.array([0.0, 0.0, h])
        dist = _ref_dist(scene, scene.cell.cart_to_frac(target), near)
        tried.append((round(dist, 6), anchors, target))
    tried.sort(key=lambda t: (t[0], t[1]))
    for _dist, anchors, target in tried:
        if _site_is_free(scene, target, group, set(anchors) | set(ignore)):
            return scene.cell.wrap(scene.cell.cart_to_frac(target))
    raise OpError(
        f"no free {site_type} site on the top layer — each one puts the "
        f"{element} within bond range of an existing atom; name the 'anchors' "
        "yourself or choose another site type",
        example=_SITE_EXAMPLE,
        next=_SITE_HINT,
    )


#: Adsorbate species catalogue for :func:`_op_add_adsorbate` — the whole
#: group placed from ONE named site, so a co-adsorbate campaign can vary
#: coverage without hand-placing each H (docs/backlog/
#: pathway-conditions-effects-report.md Phase 1 item 3).
#:
#: Each entry is ``(binding_element, [(element, (dx, dy, dz)), ...],
#: [(i, j), ...])``: the binding atom sits AT the resolved site (index 0,
#: offset (0,0,0)); every other atom is placed by a Cartesian offset in Å
#: from it, and the index pairs are the intra-group bonds minted alongside.
#:
#: The offsets are **gas-phase equilibrium geometries** (NIST CCCBDB: O–H
#: 0.958 Å / H–O–H 104.5°, N–H 1.012 Å / H–N–H 106.7°, NH₂ 1.024 Å /
#: 103.4°), oriented with the binding atom down and the hydrogens up and
#: away from the surface. That is a **pre-relaxation starting guess**, not
#: an adsorbed equilibrium geometry — adsorption tilts OH and flattens
#: NH₃, which only the relax/NEB that follows can find. Placing them
#: upright is the standard, deliberately unbiased starting point; it is
#: never reported as a result.
ADSORBATES: dict[
    str,
    tuple[
        str,
        list[tuple[str, tuple[float, float, float]]],
        list[tuple[int, int]],
    ],
] = {
    "H": ("H", [("H", (0.0, 0.0, 0.0))], []),
    "O": ("O", [("O", (0.0, 0.0, 0.0))], []),
    "N": ("N", [("N", (0.0, 0.0, 0.0))], []),
    "OH": ("O", [("O", (0.0, 0.0, 0.0)), ("H", (0.0, 0.0, 0.958))], [(0, 1)]),
    "H2O": (
        "O",
        [
            ("O", (0.0, 0.0, 0.0)),
            ("H", (0.757, 0.0, 0.586)),
            ("H", (-0.757, 0.0, 0.586)),
        ],
        [(0, 1), (0, 2)],
    ),
    "NH": ("N", [("N", (0.0, 0.0, 0.0)), ("H", (0.0, 0.0, 1.012))], [(0, 1)]),
    "NH2": (
        "N",
        [
            ("N", (0.0, 0.0, 0.0)),
            ("H", (0.803, 0.0, 0.635)),
            ("H", (-0.803, 0.0, 0.635)),
        ],
        [(0, 1), (0, 2)],
    ),
    "NH3": (
        "N",
        [
            ("N", (0.0, 0.0, 0.0)),
            ("H", (0.938, 0.0, 0.381)),
            ("H", (-0.469, 0.812, 0.381)),
            ("H", (-0.469, -0.812, 0.381)),
        ],
        [(0, 1), (0, 2), (0, 3)],
    ),
    # Hydrocarbon adsorbates (gr512126, NEB endpoint authoring): all bind
    # through C. CH4 is a physisorbate — one H pointing down at the surface,
    # the other three up in the tetrahedral cap (C–H 1.09 Å); CH3/CH2/CH
    # are the progressive dehydrogenation fragments with the H's up and away
    # from the C–surface bond. Starting guesses like the rows above.
    "C": ("C", [("C", (0.0, 0.0, 0.0))], []),
    "CH": ("C", [("C", (0.0, 0.0, 0.0)), ("H", (0.0, 0.0, 1.12))], [(0, 1)]),
    "CH2": (
        "C",
        [
            ("C", (0.0, 0.0, 0.0)),
            ("H", (0.862, 0.0, 0.699)),
            ("H", (-0.862, 0.0, 0.699)),
        ],
        [(0, 1), (0, 2)],
    ),
    "CH3": (
        "C",
        [
            ("C", (0.0, 0.0, 0.0)),
            ("H", (1.028, 0.0, 0.363)),
            ("H", (-0.514, 0.890, 0.363)),
            ("H", (-0.514, -0.890, 0.363)),
        ],
        [(0, 1), (0, 2), (0, 3)],
    ),
    "CH4": (
        "C",
        [
            ("C", (0.0, 0.0, 0.0)),
            ("H", (0.0, 0.0, -1.090)),
            ("H", (1.028, 0.0, 0.363)),
            ("H", (-0.514, 0.890, 0.363)),
            ("H", (-0.514, -0.890, 0.363)),
        ],
        [(0, 1), (0, 2), (0, 3), (0, 4)],
    ),
}


def _op_add_adsorbate(scene: Scene, op: dict[str, Any]) -> None:
    """Place a whole adsorbate group on a NAMED site in one op.

    ``{"op": "add_adsorbate", "species": "OH", "site": {"type": "hollow",
    "anchors": ["aPd1", "aPd2", "aPd3"]}, "height": <optional Å>,
    "rotate": <optional angle with unit, e.g. "30deg"/"0.5 rad", about z>}``.

    The group's **binding atom** lands exactly where :func:`_op_add_atom_site`
    would put a lone atom of that element (same :func:`_resolve_site`, same
    default covalent-radius height), and the rest of the group follows at
    the catalogued offsets (:data:`ADSORBATES`), optionally spun about the
    surface normal by ``rotate`` so two co-adsorbates need not face the same
    way. Intra-group bonds are minted; the group is NOT bonded to the
    surface (a surface bond is an inferred contact, not a declared edge —
    same convention ``add_atom_site`` follows).

    This exists because an OH or H₂O spectator was otherwise a raw-``frac``
    puzzle: place O by site, then compute H's coordinates by hand. Coverage
    series (θ_H, θ_O, θ_OH, explicit-water solvation probes) are the whole
    point of the op.
    """
    species = op.get("species")
    if not isinstance(species, str) or species not in ADSORBATES:
        raise OpError(
            f"add_adsorbate 'species' must be one of {sorted(ADSORBATES)}, "
            f"got {species!r} — build anything else atom-by-atom with "
            "add_atom_site + add_bond"
        )
    site = op.get("site")
    if not isinstance(site, dict):
        raise OpError(
            "add_adsorbate needs a 'site' object: "
            "{'type': 'top'|'bridge'|'hollow'|'fcc'|'hcp', 'anchors'?: [labels]}",
            example=_SITE_EXAMPLE,
            next=_SITE_HINT,
        )
    binder, atoms, bonds = ADSORBATES[species]

    rotate = op.get("rotate", 0.0)
    try:
        theta = math.radians(parse_angle_deg(rotate, arg_name="add_adsorbate rotate"))
    except BadInput as exc:
        raise OpError(f"{exc}; {exc.next}" if exc.next else str(exc)) from exc
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    offsets = [
        (element, np.array([dx * cos_t - dy * sin_t, dx * sin_t + dy * cos_t, dz]))
        for element, (dx, dy, dz) in atoms
    ]
    origin_frac = _resolve_site(scene, site, binder, op.get("height"), group=offsets)

    labels: list[str] = []
    for element, offset in offsets:
        # The offset is converted as a VECTOR and added in fractional space:
        # round-tripping the origin through cart_to_frac would leave the
        # binding atom an epsilon off the site (and, at frac 0, wrapped to
        # the far wall), so it would no longer land exactly where
        # ``add_atom_site`` puts a lone atom. ``cart_to_frac`` is linear, so
        # a difference vector converts cleanly.
        frac = origin_frac + scene.cell.cart_to_frac(offset)
        label = scene.next_label(element)
        _op_add_atom(
            scene,
            {"op": "add_atom", "element": element, "frac": [*frac], "label": label},
        )
        labels.append(label)
    for i, j in bonds:
        scene.bonds.append(
            Bond(i=labels[i], j=labels[j], order=1.0, provenance="declared")
        )


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
    """Transmute one atom: by label (``atom``), or by lattice site
    (``site: {"type": "top"|"subsurface", "near"?: <label or element>}``),
    where precis picks the host atom (:func:`_lattice_atom`)."""
    element = op.get("element")
    if not element:
        raise OpError(
            "set_element needs an 'element'", example=_SUBST_EXAMPLE, next=_SUBST_HINT
        )
    if "atom" in op and "site" in op:
        raise OpError(
            "set_element takes 'atom' (you name the atom) or 'site' (precis "
            "picks it), not both",
            example=_SUBST_EXAMPLE,
            next=_SUBST_HINT,
        )
    if op.get("atom") is not None:
        _require_atom(scene, str(op["atom"])).element = element
        return
    site = op.get("site")
    if isinstance(site, dict):
        _check_site_keys(site, ("type", "near"))
    if not isinstance(site, dict) or site.get("type") not in _SUBST_LAYER:
        raise OpError(
            "set_element needs 'atom' (a label) or 'site': {'type': "
            "'top'|'subsurface', 'near'?: <label or element>}",
            example=_SUBST_EXAMPLE,
            next=_SUBST_HINT,
        )
    scene.atoms[_lattice_atom(scene, site)].element = element


def _lattice_atom(scene: Scene, site: dict[str, Any]) -> str:
    """The host atom a by-site ``set_element`` substitutes (gr547306): in
    the named slab layer, the atom of that layer's majority element nearest
    ``site.near`` (else nearest the cell's in-plane centre), so a second
    substitution never re-picks a dopant. Ties break by label."""
    layers = _metal_layers(scene)
    depth = _SUBST_LAYER[str(site["type"])]
    if len(layers) < -depth:
        raise OpError(
            f"a {site['type']!r} substitution needs a slab with at least "
            f"{-depth} layer(s) — build it with the slab op first",
            example=_SUBST_EXAMPLE,
            next=_SUBST_HINT,
        )
    layer = layers[depth][1]
    host = Counter(scene.atoms[la].element for la in layer).most_common(1)[0][0]
    near = _near_fracs(scene, site.get("near"))
    return min(
        (la for la in layer if scene.atoms[la].element == host),
        key=lambda la: (round(_ref_dist(scene, scene.atoms[la].frac, near), 6), la),
    )


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


def _op_ray(scene: Scene, op: dict[str, Any]) -> None:
    """Drop / replace a named observation ray (structure-workspaces 2r) — a
    definition, never an event list: ``origin`` (Å, default the cell
    origin), ``direction``, ``length`` (Å) required; ``fields``, ``step``
    (Å), ``reach`` (Å) optional. Observation only — geometry is untouched."""
    from .probe import RAY_FIELDS

    name = op.get("name")
    if not name or any(c in str(name) for c in ".~:"):
        raise OpError("ray needs a 'name' without '.', '~' or ':' (e.g. 'axis')")
    try:
        origin = [float(x) for x in (op.get("origin") or [0.0, 0.0, 0.0])]
        direction = np.asarray([float(x) for x in op.get("direction") or []])
        length = float(op["length"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OpError(
            "ray needs 'direction' (3-vector) and 'length' (Å, > 0); 'origin' Å"
        ) from exc
    if len(origin) != 3 or direction.shape != (3,) or not np.linalg.norm(direction):
        raise OpError("ray 'origin' and 'direction' are 3-vectors; direction nonzero")
    if not length > 0:
        raise OpError(f"ray 'length' must be > 0 Å, got {op.get('length')!r}")
    fields = op.get("fields")
    if fields is not None:
        fields = [str(f) for f in fields]
        bad = sorted(set(fields) - set(RAY_FIELDS))
        if bad:
            raise OpError(f"unknown ray field(s) {bad} — fields are {list(RAY_FIELDS)}")
    spec: dict[str, Any] = {
        "origin": origin,
        "direction": [float(x) for x in direction / np.linalg.norm(direction)],
        "length": length,
        "fields": fields,
    }
    for key in ("step", "reach"):
        if op.get(key) is not None:
            if not float(op[key]) > 0:
                raise OpError(f"ray {key!r} must be > 0 Å")
            spec[key] = float(op[key])
    m = Measure(kind="ray", name=str(name), spec=spec, for_=op.get("for"))
    scene.measures = [
        x for x in scene.measures if not (x.kind == "ray" and x.name == m.name)
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
    """Retire an eye — or an observation ray — by name."""
    name = op.get("name")
    if not name:
        raise OpError("unmark needs an eye or ray 'name'")
    before = len(scene.measures)
    scene.measures = [
        x
        for x in scene.measures
        if not (x.kind in ("eye", "ray") and x.name == str(name))
    ]
    if len(scene.measures) == before:
        raise OpError(f"no eye or ray named {name!r}")


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


#: The keys the ``slab`` op reads.
_SLAB_KEYS = ("element", "size", "vacuum", "fix_layers", "a")

#: The working op every ``slab`` refusal leads with, and its ``next=``
#: hint (gr547306; :class:`OpError`'s ``example``).
_SLAB_EXAMPLE = (
    '{"op":"slab","element":"Cu","size":[3,3,4],"vacuum":10.0,"fix_layers":2}'
)
_SLAB_HINT = "recipe: get(kind='skill', id='precis-structure-slab-help')"


def _check_slab_fields(op: dict[str, Any]) -> tuple[str, Sequence[Any]]:
    """``(element, size)`` of a ``slab`` op, or a refusal naming each absent
    or malformed field, what it got, and any key the op does not read
    (gr547306: quest proposals kept dying on 'missing element/size' without
    learning which, or that a key they sent was ignored)."""
    element, size = op.get("element"), op.get("size")
    bad: list[str] = []
    if not isinstance(element, str) or not element.strip():
        got = "absent" if "element" not in op else f"got {element!r}"
        bad.append(f"'element' (host metal symbol; {got})")
    if not isinstance(size, (list, tuple)) or len(size) != 3:
        got = "absent" if "size" not in op else f"got {size!r}"
        bad.append(f"'size' ([nx, ny, nz]; {got})")
    if not bad and isinstance(size, (list, tuple)):
        return str(element), size
    extra = sorted(k for k in op if k != "op" and k not in _SLAB_KEYS)
    tail = f"; ignored key(s) {extra}" if extra else ""
    raise OpError(
        f"slab needs {' and '.join(bad)}{tail}", example=_SLAB_EXAMPLE, next=_SLAB_HINT
    )


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
    element, size = _check_slab_fields(op)
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
            f"(e.g. 2 = the bottom two layers), not a list of indices — got {fl!r}",
            example=_SLAB_EXAMPLE,
            next=_SLAB_HINT,
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
            f"a={op.get('a')!r}",
            example=_SLAB_EXAMPLE,
            next=_SLAB_HINT,
        ) from exc
    try:
        slab = fcc111(
            str(element),
            size=(nx, ny, nz),
            vacuum=vacuum,
            a=a,
        )
    except (KeyError, ValueError) as exc:
        raise OpError(
            f"slab can't build fcc(111) {element!r} ({exc}) — 'element' is a "
            "metal symbol; one that isn't fcc in ASE's reference table needs "
            "'a' (its lattice constant, Å)",
            example=_SLAB_EXAMPLE,
            next=_SLAB_HINT,
        ) from exc
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
    library — the aromatic 6-ring template).

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


def _attach_direction(
    op: dict[str, Any], key: str, atom_label: str, op_name: str = "attach"
) -> np.ndarray:
    """The explicit ``direction``/``from_direction`` fallback arg (unit
    vector), for when a bonding-direction sum is unavailable or degenerate.
    Raises a retryable ``OpError`` naming the atom and the arg when absent."""
    raw = op.get(key)
    if raw is None:
        raise OpError(
            f"{op_name} can't find a bonding direction at {atom_label!r} — it has "
            f"no bonded neighbours (or their directions cancel out, e.g. "
            f"perfectly linear/symmetric surroundings); pass an explicit "
            f"{key!r}: [x, y, z]"
        )
    try:
        vec = np.asarray(raw, dtype=float).reshape(3)
    except (TypeError, ValueError) as exc:
        raise OpError(f"{op_name} {key!r} must be a 3-vector, got {raw!r}") from exc
    norm = float(np.linalg.norm(vec))
    if norm < 1e-9:
        raise OpError(f"{op_name} {key!r} must be a nonzero vector, got {raw!r}")
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
    (§ molecule-mode fragment library).

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
    fragment library — the rdkit-embedded complement to the hand-built
    ``ring``/``attach`` primitives).

    ``{"op": "from_smiles", "smiles": "c1ccccc1O", "offset": [x,y,z]
    (Cartesian Å, optional, default origin), "seed": <int, optional,
    default 0>}``.

    Needs rdkit (the ``[chem]`` extra) — imported lazily, here, only when
    this op actually runs; a missing rdkit raises a clean, retryable
    ``OpError`` naming the extra, never a bare ``ImportError``. The SMILES is
    parsed (``Chem.MolFromSmiles``), hydrogenated (``Chem.AddHs``), and
    embedded into 3D by :func:`precis_geom.mol.embed_molecule` (ETKDGv3
    seeded from ``seed`` — same ``(smiles, seed)`` in, bit-identical geometry
    out — then a **best-effort** MMFF cleanup; any MMFF failure is swallowed,
    the raw ETKDG geometry is acceptable on its own).

    One scene atom per rdkit atom (element = ``GetSymbol()``, position = the
    embedded conformer + ``offset``, ``charge`` = ``GetFormalCharge()`` —
    gr285775, so a quaternary-ammonium/carboxylate SMILES round-trips its
    declared charge into the validator's charge-aware valence budget); an
    aromatic atom gets ``hybridization="sp2"``. One declared bond per rdkit
    bond: an ``AROMATIC`` rdkit bond → order 1.5, kind ``aromatic``; anything
    else → order = ``GetBondTypeAsDouble()`` (1/2/3), kind ``pairwise``. Each
    bond's periodic image is set via ``scene.cell.mic`` (the ``ring``/
    ``attach`` discipline — ``wrap()`` may split the fragment across a cell
    wall).

    v1 scope: geometry + formal charge. Stereochemistry and anything else
    beyond what ETKDG's distance-geometry embedding encodes is not carried
    into the scene.
    """
    smiles = op.get("smiles")
    if not smiles:
        raise OpError("from_smiles needs a 'smiles' string")
    smiles = str(smiles)
    try:
        from rdkit import Chem
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

    # The embed (seeded ETKDGv3 + best-effort MMFF) lives in the molecular
    # workspace package — one path shared with ``mol.frag``.
    from precis_geom.mol import embed_molecule

    try:
        embed_molecule(mol, seed)
    except BadInput as exc:
        raise OpError(exc.cause) from exc

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
            charge=int(atom.GetFormalCharge()),
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


def _declared_adjacency(scene: Scene) -> dict[str, set[str]]:
    """Bond-graph adjacency over DECLARED bonds only — the intent graph a
    ``transfer``/``move_group`` reads. Unlike :func:`_combined_adjacency`
    (which adds inferred contacts so a physically-touching fragment moves
    as one body), an adsorbate resting on a slab is deliberately NOT
    declared-bonded to it, so a physisorbed group stays its own component
    here instead of merging into the whole slab."""
    adj: dict[str, set[str]] = {label: set() for label in scene.atoms}
    for b in scene.bonds:
        if b.i in adj and b.j in adj:
            adj[b.i].add(b.j)
            adj[b.j].add(b.i)
    return adj


def _largest_metal_component(scene: Scene, adj: dict[str, set[str]]) -> set[str]:
    """The slab, for slab-vs-adsorbate splits (gr512126's rule): the
    largest connected component over the combined adjacency restricted to
    metal atoms (``elements.max_valence`` is ``None``). A dopant swapped in
    via ``set_element`` chains in through inferred metal–metal contacts, so
    a Pd slab with one surface Cu counts the Cu as slab — the dominant-
    element heuristic ``preflight._slab_adsorbate_indices`` still carries
    gets this wrong."""
    metals = {
        label
        for label, atom in scene.atoms.items()
        if elements.max_valence(atom.element) is None
    }
    best: set[str] = set()
    seen: set[str] = set()
    for start in metals:
        if start in seen:
            continue
        comp = {start}
        seen.add(start)
        stack = [start]
        while stack:
            cur = stack.pop()
            for nxt in adj.get(cur, ()):
                if nxt in metals and nxt not in comp:
                    comp.add(nxt)
                    seen.add(nxt)
                    stack.append(nxt)
        if len(comp) > len(best):
            best = comp
    return best


#: ``transfer``'s ``from``-mode pick: candidates whose MIC distance to the
#: acceptor sits within this of the minimum count as equidistant, and the
#: op refuses rather than silently picking one (rule 9: one reading).
_TRANSFER_TIE_A = 0.05


def _op_transfer(scene: Scene, op: dict[str, Any]) -> None:
    """Move ONE atom onto an acceptor atom — the elementary bond-swap step
    of a reaction endpoint (gr512126: CH4 + O* → CH3* + OH* is one
    transfer of an H from C onto the bridging O).

    ``{"op": "transfer", "atom": "aH3", "to": "aO1", "distance?": <Å>,
    "order?": <bond order>, "direction?": [x,y,z]}`` — moves ``atom`` onto
    ``to`` along ``to``'s open coordination direction at the
    covalent-radius sum (or ``distance``), drops ``atom``'s declared
    bonds, and mints the ``atom``–``to`` bond.

    ``{"op": "transfer", "from": "aC1", "to": "aO1", ...}`` — picks the
    atom declared-bonded to ``from`` that sits nearest ``to`` and writes
    its label back into the op (``op['atom']``) so the stored revision
    names the one reading taken. Two candidates equidistant within
    :data:`_TRANSFER_TIE_A` refuse with both labels as ``options=`` — the
    caller picks by naming ``atom`` itself.
    """
    to_label = op.get("to")
    if not to_label:
        raise OpError("transfer needs 'to' (the acceptor atom label)")
    to_label = str(to_label)
    to_atom = _require_atom(scene, to_label)
    atom_label = op.get("atom")
    from_label = op.get("from")
    if bool(atom_label) == bool(from_label):
        raise OpError(
            "transfer needs exactly one of 'atom' (the atom to move) or "
            "'from' (pick the atom bonded to it that is nearest 'to')"
        )
    if from_label:
        from_label = str(from_label)
        _require_atom(scene, from_label)
        declared = _declared_adjacency(scene)
        candidates = declared.get(from_label, set()) - {to_label, from_label}
        if not candidates:
            raise OpError(
                f"no atom is declared-bonded to {from_label!r} — name the "
                "atom to move directly with 'atom' instead of 'from'"
            )
        dists = {
            cand: scene.cell.mic(scene.atoms[cand].frac, to_atom.frac)[0]
            for cand in candidates
        }
        nearest = min(dists.values())
        tied = sorted(c for c, d in dists.items() if d - nearest <= _TRANSFER_TIE_A)
        if len(tied) > 1:
            raise OpError(
                f"transfer(from={from_label!r}) is ambiguous: {len(tied)} "
                f"candidates sit equidistant (within {_TRANSFER_TIE_A} Å) "
                f"from {to_label!r} — name one with 'atom'",
                options=tied,
            )
        moved_label = tied[0]
        # Write the pick back into the op dict so the revision record (and
        # any echo of the authored ops) names the atom moved.
        op["atom"] = moved_label
    else:
        moved_label = str(atom_label)
        _require_atom(scene, moved_label)
        if moved_label == to_label:
            raise OpError(f"transfer can't move {moved_label!r} onto itself")
    moved = scene.atoms[moved_label]
    if moved.fixed:
        raise OpError(
            f"transfer can't move {moved_label!r} — it is constrained "
            "('fixed'); release it with constrain kind='none' first"
        )
    dist_raw = op.get("distance")
    if dist_raw is None:
        distance = elements.covalent_radius(moved.element) + elements.covalent_radius(
            to_atom.element
        )
    else:
        try:
            distance = float(dist_raw)
        except (TypeError, ValueError) as exc:
            raise OpError(
                f"transfer 'distance' must be a number, got {dist_raw!r}"
            ) from exc
        if distance <= 0:
            raise OpError(f"transfer 'distance' must be positive Å, got {dist_raw!r}")

    adj = _combined_adjacency(scene)
    to_sum = _neighbor_unit_sum(scene, adj, to_label)
    if to_sum is None or float(np.linalg.norm(to_sum)) < 1e-6:
        d_to = _attach_direction(op, "direction", to_label, op_name="transfer")
    else:
        d_to = -to_sum / float(np.linalg.norm(to_sum))

    to_cart = scene.cell.frac_to_cart(to_atom.frac)
    new_cart = to_cart + distance * d_to
    moved.frac = scene.cell.wrap(scene.cell.cart_to_frac(new_cart))
    scene.bonds = [b for b in scene.bonds if moved_label not in (b.i, b.j)]
    _, img = scene.cell.mic(moved.frac, to_atom.frac)
    order_raw = op.get("order", 1.0)
    try:
        order = float(order_raw)
    except (TypeError, ValueError) as exc:
        raise OpError(f"transfer 'order' must be a number, got {order_raw!r}") from exc
    scene.bonds.append(
        Bond(
            i=moved_label,
            j=to_label,
            order=order,
            kind=str(op.get("kind", "pairwise")),
            provenance="declared",
            image=img,
        )
    )


def _op_move_group(scene: Scene, op: dict[str, Any]) -> None:
    """Rigidly move the whole fragment containing ``atom`` so ``atom``
    lands on a named site — repositioning a whole adsorbate (CH3* to
    another top site) instead of rebuilding it atom by atom (gr512126).

    ``{"op": "move_group", "atom": "aC1", "site": {"type": "top",
    "anchors": ["aCu32"]}, "height?": <Å>}``

    The fragment is the connected component over DECLARED bonds only
    (:func:`_declared_adjacency`): a physisorbed group is never
    declared-bonded to the slab, so the slab stays behind. Refuses when
    the fragment contains a fixed atom or a slab atom — the slab being the
    largest connected metal-atom component over inferred contacts
    (:func:`_largest_metal_component`), so an adsorbate declared-bonded to
    a surface atom also refuses (drop that bond first).
    """
    atom_label = op.get("atom")
    if not atom_label:
        raise OpError("move_group needs 'atom' (a label in the fragment to move)")
    atom_label = str(atom_label)
    atom = _require_atom(scene, atom_label)
    site = op.get("site")
    if not isinstance(site, dict):
        raise OpError(
            "move_group needs a 'site' object: "
            "{'type': 'top'|'bridge'|'hollow', 'anchors': [labels]}"
        )
    frag = _fragment_labels(_declared_adjacency(scene), atom_label)
    fixed = sorted(la for la in frag if scene.atoms[la].fixed)
    if fixed:
        raise OpError(
            f"move_group refuses: the fragment containing {atom_label!r} "
            f"includes fixed atom(s) {fixed} — release them with "
            "constrain kind='none' first, or move atoms individually with "
            "displace"
        )
    slab = _largest_metal_component(scene, _combined_adjacency(scene))
    on_slab = sorted(frag & slab)
    if on_slab:
        raise OpError(
            f"move_group refuses: the fragment containing {atom_label!r} "
            f"includes slab atom(s) {on_slab} — the slab does not move; "
            "drop the declared bond tying the group to the surface, or "
            "move adsorbate atoms individually with displace"
        )
    target_frac = _resolve_site(
        scene, site, str(atom.element), op.get("height"), ignore=frag
    )
    delta = np.asarray(target_frac, dtype=float) - atom.frac
    for label in frag:
        scene.atoms[label].frac = scene.cell.wrap(scene.atoms[label].frac + delta)


_OPS = {
    "set_cell": _op_set_cell,
    "slab": _op_slab,
    "add_atom": _op_add_atom,
    "add_atom_site": _op_add_atom_site,
    "add_adsorbate": _op_add_adsorbate,
    "set_element": _op_set_element,
    "vacancy": _op_vacancy,
    "displace": _op_displace,
    "add_bond": _op_add_bond,
    "remove_bond": _op_remove_bond,
    "constrain": _op_constrain,
    "eye": _op_eye,
    "ray": _op_ray,
    "measure": _op_measure,
    "unmark": _op_unmark,
    "remove_measure": _op_remove_measure,
    "ring": _op_ring,
    "attach": _op_attach,
    "from_smiles": _op_from_smiles,
    "transfer": _op_transfer,
    "move_group": _op_move_group,
}

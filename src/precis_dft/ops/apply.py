"""Apply a list of typed ops to an ASE ``Atoms`` object.

The op vocabulary is declared in :mod:`precis_dft.ops.catalog`. This
module is the engine that walks the op list and mutates the
underlying Atoms via ASE primitives.

Apply semantics:

- Mutation ops chain (``set_species`` then ``add_adsorbate`` then
  ``vacancy`` works as a sequence on the same Atoms object). The
  caller gets one Atoms back.
- Combinatorial ops (the ``substitute(... enumerate='all')`` shape)
  terminate the sequence — their expansion *is* the result. We
  return a list of Atoms instead.
- Validation happens here at apply time (an unknown site index, a
  non-recognised element). Catalog-level schema validation lives in
  :mod:`precis_dft.ops.validate` and is the precondition; this
  module assumes shapes are well-formed.

Adsorbate species library: a small table mapping the catalysis
notation (``*OH``, ``*O``, ``*OOH``, ``*H``, ``*CO``) to ASE-
buildable :class:`ase.Atoms` objects.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
from ase import Atom, Atoms
from ase.build import add_adsorbate
from ase.constraints import FixAtoms

from .catalog import OP_CATALOG


def apply_ops(atoms: Atoms, ops: Sequence[dict[str, Any]]) -> Atoms | list[Atoms]:
    """Apply ops in order. Returns one Atoms or a list of siblings.

    Combinatorial ops (``substitute`` with ``enumerate='all'``)
    terminate the chain — their expansion is the result and any
    later ops in the list are ignored (with a warning in the
    raised exception's message).
    """
    current = atoms.copy()
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            raise ValueError(f"op {i} is not a mapping: {op!r}")
        kind = _kind(op)
        meta = OP_CATALOG.get(kind)
        if meta is None:
            raise ValueError(f"unknown op kind {kind!r} (known: {sorted(OP_CATALOG)})")
        raw_params = op.get(kind) if kind in op else op
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
        if meta.get("combinatorial") and _combinatorial_active(params):
            siblings = _apply_combinatorial(current, kind, params)
            if i + 1 < len(ops):
                raise ValueError(
                    f"combinatorial op {kind} at index {i} terminates the chain; "
                    f"{len(ops) - i - 1} subsequent ops were ignored"
                )
            return siblings
        current = _apply_mutation(current, kind, params)
    return current


def _kind(op: dict[str, Any]) -> str:
    """Resolve op kind from either ``{set_species: {...}}`` or
    ``{kind: 'set_species', ...}`` shapes."""
    if "kind" in op and isinstance(op["kind"], str):
        return op["kind"]
    keys = [k for k in op if k in OP_CATALOG]
    if len(keys) == 1:
        return keys[0]
    if not keys:
        raise ValueError(
            f"op has no recognised kind key (got {list(op)}, expected one of {sorted(OP_CATALOG)})"
        )
    raise ValueError(f"op has multiple recognised kinds: {keys}")


def _combinatorial_active(params: dict[str, Any]) -> bool:
    """``substitute(enumerate='all')`` is combinatorial; ``enumerate='first'``
    is a single mutation."""
    return params.get("enumerate", "first") == "all"


# ── Mutation ops ──────────────────────────────────────────────────


def _apply_mutation(atoms: Atoms, kind: str, params: dict[str, Any]) -> Atoms:
    dispatch = {
        "set_species": _set_species,
        "substitute": _substitute_first,
        "add_adsorbate": _add_adsorbate,
        "vacancy": _vacancy,
        "strain": _strain,
        "supercell": _supercell,
        "constrain": _constrain,
        "displace": _displace,
        "set_magmom": _set_magmom,
        "intercalate": _intercalate,
    }
    fn = dispatch.get(kind)
    if fn is None:
        raise ValueError(f"no mutation handler for op {kind!r}")
    return fn(atoms, params)


def _set_species(atoms: Atoms, params: dict[str, Any]) -> Atoms:
    site = int(params["site"])
    element = str(params["element"])
    _check_site(atoms, site)
    symbols = atoms.get_chemical_symbols()
    symbols[site] = element
    atoms.set_chemical_symbols(symbols)
    return atoms


def _substitute_first(atoms: Atoms, params: dict[str, Any]) -> Atoms:
    """``substitute`` in the non-combinatorial case picks the first
    symmetry-distinct pattern at the requested fraction."""
    siblings = _enumerate_substitutions(atoms, params)
    if not siblings:
        return atoms
    return siblings[0]


def _add_adsorbate(atoms: Atoms, params: dict[str, Any]) -> Atoms:
    """Place an adsorbate at a named special site.

    ``params.site`` is either an integer atom index (top-site over
    that atom) or a string name like ``bridge_12_13``. The string
    naming convention is documented in
    :mod:`precis_dft.annotator.special_sites`.
    """
    species = str(params["species"])
    height = float(params.get("height_angstrom", 1.8))
    site = params["site"]
    target = _resolve_site(atoms, site)

    adsorbate = _build_adsorbate(species)
    orientation = params.get("orientation")
    if orientation is not None:
        adsorbate = _orient_adsorbate(adsorbate, orientation)
    # Find which axis is the slab normal (the longest cell vector).
    from precis_dft.annotator.header import detect_dimensionality

    if detect_dimensionality(atoms) != "slab":
        raise ValueError("add_adsorbate requires a slab structure")
    cell_lengths = atoms.cell.lengths()
    axis = int(np.argmax(cell_lengths))
    if axis != 2:
        raise NotImplementedError(
            "add_adsorbate currently requires the slab normal to be along z; "
            "rotate the structure first"
        )
    add_adsorbate(atoms, adsorbate, height=height, position=tuple(target[:2]))
    return atoms


def _vacancy(atoms: Atoms, params: dict[str, Any]) -> Atoms:
    site = int(params["site"])
    _check_site(atoms, site)
    del atoms[site]
    return atoms


def _strain(atoms: Atoms, params: dict[str, Any]) -> Atoms:
    """Apply a uniform strain to the cell.

    ``component='biaxial'`` strains the two in-plane axes;
    ``uniaxial-x/y/z`` strains a single axis. ``magnitude`` is the
    fractional strain (0.02 = 2 %).
    """
    component = str(params["component"])
    magnitude = float(params["magnitude"])
    cell = atoms.cell.array.copy()
    if component == "biaxial":
        cell[0] *= 1 + magnitude
        cell[1] *= 1 + magnitude
    elif component in ("uniaxial-x", "uniaxial-y", "uniaxial-z"):
        axis = {"uniaxial-x": 0, "uniaxial-y": 1, "uniaxial-z": 2}[component]
        cell[axis] *= 1 + magnitude
    else:
        raise ValueError(f"unknown strain component {component!r}")
    atoms.set_cell(cell, scale_atoms=True)
    return atoms


def _supercell(atoms: Atoms, params: dict[str, Any]) -> Atoms:
    repeats = params["repeats"]
    if len(repeats) != 3:
        raise ValueError(f"supercell repeats must be length 3; got {repeats}")
    return atoms.repeat(tuple(int(r) for r in repeats))


def _constrain(atoms: Atoms, params: dict[str, Any]) -> Atoms:
    """Add a FixAtoms constraint over a layer range or explicit indices."""
    kind = str(params.get("kind", "frozen"))
    if kind != "frozen":
        raise ValueError(f"constrain kind {kind!r} not yet supported")

    indices: list[int] = []
    if "sites" in params:
        indices = [int(i) for i in params["sites"]]
    elif "layer" in params:
        from precis_dft.annotator.header import detect_slab_layers

        cell_lengths = atoms.cell.lengths()
        axis = int(np.argmax(cell_lengths))
        layers = detect_slab_layers(atoms, axis)
        layer_spec = params["layer"]
        if isinstance(layer_spec, int):
            layer_spec = [layer_spec]
        for layer_index in layer_spec:
            if 0 <= layer_index < len(layers):
                indices.extend(layers[layer_index])
    else:
        raise ValueError("constrain requires either 'sites' or 'layer'")

    if not indices:
        return atoms
    existing = list(atoms.constraints)
    existing.append(FixAtoms(indices=sorted(set(indices))))
    atoms.set_constraint(existing)
    return atoms


def _displace(atoms: Atoms, params: dict[str, Any]) -> Atoms:
    site = int(params["site"])
    vector = params["vector"]
    if len(vector) != 3:
        raise ValueError(f"displace vector must be length 3; got {vector}")
    _check_site(atoms, site)
    positions = atoms.get_positions()
    positions[site] = positions[site] + np.asarray(vector, dtype=float)
    atoms.set_positions(positions)
    return atoms


def _set_magmom(atoms: Atoms, params: dict[str, Any]) -> Atoms:
    site = int(params["site"])
    magmom = float(params["magmom"])
    _check_site(atoms, site)
    magmoms = atoms.get_initial_magnetic_moments()
    magmoms[site] = magmom
    atoms.set_initial_magnetic_moments(magmoms)
    return atoms


def _intercalate(atoms: Atoms, params: dict[str, Any]) -> Atoms:
    """Insert atoms at named interstitial sites.

    Two shapes supported:
    - ``interstitial='octahedral_3'`` — place one species at the
      named site.
    - ``species='H', count=4, mode='octahedral', select='most_symmetric'``
      — let the system pick N interstitials of the given mode.
    """
    from precis_dft.annotator.special_sites import find_interstitials

    species = str(params["species"])
    interstitials = find_interstitials(atoms)
    chosen: list[dict[str, Any]] = []

    if "interstitial" in params:
        name = str(params["interstitial"])
        chosen = [s for s in interstitials if s["name"] == name]
        if not chosen:
            raise ValueError(
                f"interstitial {name!r} not found; available: {[s['name'] for s in interstitials]}"
            )
    else:
        mode = params.get("mode", "octahedral")
        count = int(params.get("count", 1))
        filtered = [s for s in interstitials if s["kind"] == mode]
        # 'most_symmetric' is approximated as the first N sorted by
        # name (deterministic ordering by Voronoi-finder index).
        chosen = filtered[:count]

    for site in chosen:
        atoms.append(Atom(species, position=site["position"]))
    return atoms


# ── Helpers ──────────────────────────────────────────────────────


def _check_site(atoms: Atoms, site: int) -> None:
    if not (0 <= site < len(atoms)):
        raise ValueError(
            f"site index {site} out of range for structure with {len(atoms)} atoms"
        )


def _resolve_site(atoms: Atoms, site: Any) -> np.ndarray:
    """Resolve a site spec to an (x, y, z) position.

    - Integer ``site=N`` → position of atom N.
    - String name like ``bridge_12_13`` → look up in special_sites
      view and use the recorded position.
    """
    if isinstance(site, int):
        _check_site(atoms, site)
        return atoms.get_positions()[site]
    if isinstance(site, str):
        from precis_dft.annotator.special_sites import find_surface_sites

        sites = find_surface_sites(atoms)
        for s in sites:
            if s["name"] == site:
                return np.asarray(s["position"], dtype=float)
        raise ValueError(
            f"named site {site!r} not found in special_sites; available: "
            f"{[s['name'] for s in sites]}"
        )
    raise ValueError(f"site spec must be int or str, got {type(site).__name__}")


# Tiny in-tree adsorbate library. ASE's ``molecule()`` covers most
# but the catalysis notation (``*OH``, ``*OOH``) doesn't map there.
_ADSORBATES: dict[str, list[tuple[str, list[float]]]] = {
    "*H": [("H", [0, 0, 0])],
    "*O": [("O", [0, 0, 0])],
    "*OH": [("O", [0, 0, 0]), ("H", [0.0, 0.0, 0.97])],
    "*OOH": [
        ("O", [0, 0, 0]),
        ("O", [1.30, 0, 0.50]),
        ("H", [2.10, 0, 0.95]),
    ],
    "*CO": [("C", [0, 0, 0]), ("O", [0, 0, 1.16])],
    "OH": [("O", [0, 0, 0]), ("H", [0.0, 0.0, 0.97])],
    "O": [("O", [0, 0, 0])],
    "H": [("H", [0, 0, 0])],
    "CO": [("C", [0, 0, 0]), ("O", [0, 0, 1.16])],
}


def _build_adsorbate(species: str) -> Atoms:
    """Return an ASE Atoms object for the named adsorbate."""
    if species not in _ADSORBATES:
        # Fall back to ASE's molecule database for common gas-phase
        # species.
        from ase.build import molecule

        try:
            return molecule(species)
        except (KeyError, ValueError) as exc:
            raise ValueError(
                f"unknown adsorbate species {species!r}; known: {sorted(_ADSORBATES)}"
            ) from exc
    pieces = _ADSORBATES[species]
    return Atoms(
        symbols=[p[0] for p in pieces],
        positions=[p[1] for p in pieces],
    )


def _orient_adsorbate(adsorbate: Atoms, orientation: str) -> Atoms:
    """Apply an orientation hint (``'O-down'``, ``'C-down'``) by
    rotating the adsorbate so the named species is at z=0."""
    target = orientation.split("-")[0]
    symbols = adsorbate.get_chemical_symbols()
    if target not in symbols:
        return adsorbate
    target_idx = symbols.index(target)
    # Translate so the target sits at the bottom.
    positions = adsorbate.get_positions()
    z = positions[:, 2]
    if z[target_idx] > z.min():
        # Flip across the y axis so the named species is at the bottom.
        positions[:, 2] = -positions[:, 2]
        adsorbate.set_positions(positions)
    # Translate so the target's z = 0.
    positions = adsorbate.get_positions()
    positions[:, 2] -= positions[target_idx, 2]
    adsorbate.set_positions(positions)
    return adsorbate


# ── Combinatorial expansion ──────────────────────────────────────


def _apply_combinatorial(
    atoms: Atoms, kind: str, params: dict[str, Any]
) -> list[Atoms]:
    if kind != "substitute":
        raise ValueError(f"combinatorial expansion not supported for op {kind!r}")
    return _enumerate_substitutions(atoms, params)


def _enumerate_substitutions(atoms: Atoms, params: dict[str, Any]) -> list[Atoms]:
    """Symmetry-distinct substitution patterns at the requested fraction.

    Uses the equivalence_classes view to pick which sites are
    candidates; for each unique pattern of ``ceil(fraction * |class|)``
    Cu atoms among the equivalence class members, returns a sibling
    Atoms.

    Bounded by ``max_siblings`` (default 32, hard cap 256) so a
    runaway combinatorial doesn't fan out unbounded.
    """
    from itertools import combinations

    from precis_dft.annotator.symmetry import analyse

    equivalence_class = str(params["equivalence_class"])
    fraction = float(params["fraction"])
    new_element = str(params["element"])
    max_siblings = int(params.get("max_siblings", 32))
    max_siblings = min(max_siblings, 256)

    sym = analyse(atoms)
    classes = sym["equivalence_classes"]
    if equivalence_class not in classes:
        # Allow the user to name by element only (``surface_top``-like
        # symbols aren't in the spglib output today). Fall back to
        # the matching element if there's a unique match.
        candidates = [
            c
            for c in classes
            if c.startswith(equivalence_class + "_eq_") or c == equivalence_class
        ]
        if len(candidates) == 1:
            equivalence_class = candidates[0]
        else:
            raise ValueError(
                f"equivalence_class {params['equivalence_class']!r} not found; "
                f"available: {sorted(classes)}"
            )

    member_indices = classes[equivalence_class]
    n_total = len(member_indices)
    n_swap = max(1, round(fraction * n_total))
    if n_swap >= n_total:
        # Replace all members.
        copy = atoms.copy()
        symbols = copy.get_chemical_symbols()
        for i in member_indices:
            symbols[i] = new_element
        copy.set_chemical_symbols(symbols)
        return [copy]

    siblings: list[Atoms] = []
    for combo in combinations(member_indices, n_swap):
        copy = atoms.copy()
        symbols = copy.get_chemical_symbols()
        for i in combo:
            symbols[i] = new_element
        copy.set_chemical_symbols(symbols)
        siblings.append(copy)
        if len(siblings) >= max_siblings:
            break

    return siblings


__all__ = ["apply_ops"]

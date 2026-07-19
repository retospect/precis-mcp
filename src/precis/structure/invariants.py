"""Representation-invariant structure fingerprint + comparison.

The round-trip eval (``docs/design/structure-roundtrip-eval.md``) scores a model
by sending a structure through language (describe → build) and asking whether the
rebuilt :class:`~precis.structure.scene.Scene` is the SAME structure. "Same" must
ignore atom relabeling, ordering, translation, and periodic image — an atom
reachable "down 2, over 3" several equivalent ways is one structure, not many,
and a prose round trip rebuilds at *canonical* positions, not the source's exact
floats. So we never compare coordinates; we compare a canonical **fingerprint**
of invariants, all derived from geometry via the existing :mod:`.probe` tools:

* composition (element multiset)
* per-layer composition (bottom→top) — captures a dopant's layer
* number of frozen atoms
* adsorbate site classes (top / bridge / hollow, by surface coordination)
* coordination-number histogram — a permutation/translation-invariant graph shape
* min interatomic distance — a validity floor

This is the CHEAP tier: two equivalent representations always fingerprint
identically, so it has no false-*negatives* from relabeling. Exact structural
identity (pymatgen ``StructureMatcher`` / InChI) is the eventual upgrade for
false-*positive* tightness; :class:`Fingerprint` is the seam — swap the
comparator, keep the loop.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import probe
from .scene import FIX_ALL, Scene

#: Atoms within this many Å in z are one layer (fcc(111) interlayer spacing is
#: ~2.3 Å, and an adsorbate sits well above — so a 0.5 Å tolerance cleanly
#: separates layers while absorbing small rebuild jitter).
_Z_TOL = 0.5

#: Below this Å, two atoms overlap — an unphysical rebuild (validity floor).
_MIN_DIST_FLOOR = 0.75

#: Cutoff (Å) for counting an adsorbate's bonds to the surface (site typing).
_SURFACE_CUTOFF = 2.8

#: Per-field weights for :func:`compare` (sum to 1.0).
_WEIGHTS = {
    "composition": 0.30,
    "layers": 0.25,
    "adsorbate_sites": 0.20,
    "n_fixed": 0.15,
    "coordination": 0.10,
}

_SITE_BY_COORD = {0: "detached", 1: "top", 2: "bridge"}  # ≥3 → "hollow"


@dataclass(frozen=True)
class Fingerprint:
    """A canonical, representation-invariant summary of a :class:`Scene`.

    All fields are order- and label-independent (sorted / counted), so two scenes
    that differ only by relabeling, atom order, translation, or periodic image
    produce equal fingerprints.
    """

    composition: tuple[tuple[str, int], ...]  # sorted (element, count)
    layers: tuple[tuple[tuple[str, int], ...], ...]  # bottom→top, per-layer comp
    n_fixed: int
    adsorbate_sites: tuple[str, ...]  # sorted site classes of above-surface atoms
    coordination: tuple[tuple[int, int], ...]  # sorted (coord_number, count)
    min_dist: float


def _cart_z(scene: Scene, label: str) -> float:
    return float(scene.cell.frac_to_cart(scene.atoms[label].frac)[2])


def _layers(scene: Scene) -> list[list[str]]:
    """Atom labels grouped into z-layers, bottom→top.

    A 1-D clustering by z: a gap larger than :data:`_Z_TOL` starts a new layer.
    """
    ranked = sorted(scene.atoms, key=lambda la: _cart_z(scene, la))
    layers: list[list[str]] = []
    cur: list[str] = []
    prev: float | None = None
    for la in ranked:
        z = _cart_z(scene, la)
        if prev is not None and z - prev > _Z_TOL:
            layers.append(cur)
            cur = []
        cur.append(la)
        prev = z
    if cur:
        layers.append(cur)
    return layers


def _comp(scene: Scene, labels: list[str]) -> tuple[tuple[str, int], ...]:
    counts: dict[str, int] = {}
    for la in labels:
        el = scene.atoms[la].element
        counts[el] = counts.get(el, 0) + 1
    return tuple(sorted(counts.items()))


def _split_slab_adsorbate(
    scene: Scene, layers: list[list[str]]
) -> tuple[list[str], list[str]]:
    """Partition atoms into (top-surface-layer, adsorbate) labels.

    Slab layers are the dense clusters (≈ nx·ny atoms); an adsorbate forms a
    sparse cluster *above* the top slab layer. Rule: a top cluster with fewer
    than half the median dense-layer size is an adsorbate; the last dense layer
    below it is the surface. Returns ``([], [])`` when there is no clear slab.
    """
    if not layers:
        return [], []
    sizes = [len(la) for la in layers]
    dense = float(np.median(sizes))
    is_ads = [n < max(1.0, dense / 2) for n in sizes]
    # adsorbate = atoms in sparse layers at the very top (from the top down)
    adsorbate: list[str] = []
    top_slab = len(layers) - 1
    for idx in range(len(layers) - 1, -1, -1):
        if is_ads[idx]:
            adsorbate.extend(layers[idx])
            top_slab = idx - 1
        else:
            break
    surface = layers[top_slab] if 0 <= top_slab < len(layers) else []
    return surface, adsorbate


def _adsorbate_sites(
    scene: Scene, surface: list[str], adsorbate: list[str]
) -> tuple[str, ...]:
    """Classify each adsorbate atom by how many surface atoms it caps.

    Counts surface neighbors within :data:`_SURFACE_CUTOFF`: 1 = top, 2 =
    bridge, ≥3 = hollow, 0 = detached. Returns the sorted multiset of classes.
    """
    surf = set(surface)
    out: list[str] = []
    for la in adsorbate:
        n = sum(
            1
            for other, _img, _d in scene.neighbors(la, _SURFACE_CUTOFF)
            if other in surf
        )
        out.append(_SITE_BY_COORD.get(n, "hollow"))
    return tuple(sorted(out))


def _coordination_hist(scene: Scene) -> tuple[tuple[int, int], ...]:
    hist: dict[int, int] = {}
    for la in scene.atoms:
        cn = probe.coordination(scene, la)
        hist[cn] = hist.get(cn, 0) + 1
    return tuple(sorted(hist.items()))


def _min_dist(scene: Scene) -> float:
    atoms = list(scene.atoms)
    if len(atoms) < 2:
        return 99.9
    best = 99.9
    for i in range(len(atoms)):
        for j in range(i + 1, len(atoms)):
            d, _img = scene.cell.mic(
                scene.atoms[atoms[i]].frac, scene.atoms[atoms[j]].frac
            )
            best = min(best, d)
    return best


def fingerprint(scene: Scene) -> Fingerprint:
    """The canonical, representation-invariant fingerprint of ``scene``."""
    layers = _layers(scene)
    surface, adsorbate = _split_slab_adsorbate(scene, layers)
    return Fingerprint(
        composition=_comp(scene, list(scene.atoms)),
        layers=tuple(_comp(scene, la) for la in layers),
        n_fixed=sum(1 for a in scene.atoms.values() if a.fixed == FIX_ALL),
        adsorbate_sites=_adsorbate_sites(scene, surface, adsorbate),
        coordination=_coordination_hist(scene),
        min_dist=round(_min_dist(scene), 3),
    )


def _layer_sim(
    a: tuple[tuple[tuple[str, int], ...], ...],
    b: tuple[tuple[tuple[str, int], ...], ...],
) -> float:
    """Fraction of layers whose composition matches (0 when layer counts differ)."""
    if len(a) != len(b):
        return 0.0
    if not a:
        return 1.0
    return sum(1 for x, y in zip(a, b, strict=True) if x == y) / len(a)


def _hist_sim(a: tuple[tuple[int, int], ...], b: tuple[tuple[int, int], ...]) -> float:
    """Histogram overlap in [0,1]: 1 − L1(a,b) / (|a|+|b|)."""
    da, db = dict(a), dict(b)
    total = sum(da.values()) + sum(db.values())
    if total == 0:
        return 1.0
    l1 = sum(abs(da.get(k, 0) - db.get(k, 0)) for k in set(da) | set(db))
    return 1.0 - l1 / total


def compare(source: Fingerprint, rebuilt: Fingerprint) -> dict:
    """Score ``rebuilt`` against ``source`` — a weighted invariant-match fraction.

    Returns ``{"score", "parts", "valid"}``. ``score`` ∈ [0,1] is the weighted
    per-field match (:data:`_WEIGHTS`); an overlapping rebuild (``min_dist`` below
    the validity floor) caps it at 0.5 regardless of the rest — a physically
    impossible structure can't be "mostly right". ``parts`` gives the per-field
    sub-scores so a low score says *which* invariant diverged.
    """
    parts = {
        "composition": 1.0 if source.composition == rebuilt.composition else 0.0,
        "layers": _layer_sim(source.layers, rebuilt.layers),
        "adsorbate_sites": (
            1.0 if source.adsorbate_sites == rebuilt.adsorbate_sites else 0.0
        ),
        "n_fixed": 1.0 if source.n_fixed == rebuilt.n_fixed else 0.0,
        "coordination": _hist_sim(source.coordination, rebuilt.coordination),
    }
    score = sum(_WEIGHTS[k] * v for k, v in parts.items())
    valid = rebuilt.min_dist >= _MIN_DIST_FLOOR
    if not valid:
        score = min(score, 0.5)
    return {
        "score": round(score, 4),
        "parts": {k: round(v, 3) for k, v in parts.items()},
        "valid": valid,
    }


__all__ = ["Fingerprint", "compare", "fingerprint"]

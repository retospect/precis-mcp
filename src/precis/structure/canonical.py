"""Periodic-symmetry canonical frame for slab scenes.

A crystal tiles: a lone defect in a periodic supercell has no absolute
position, and two decorations that differ only by a lattice translation, an
in-plane rotation, or an in-plane mirror are the *same* physical structure.
Left un-normalized, that symmetry manufactures phantom experiments — a
"corner" vs "central" substitution registering as two candidates (and two
sim dispatches) when they are translation images of one crystal (qu164903's
corner saga).

This module defines one canonical frame and two entry points:

* :func:`canonical_form` — the canonical payload + ``sha256[:12]`` hash for a
  scene, invariant under every cell-preserving in-plane isometry. The
  symmetry-aware sibling of :func:`precis.quest.compute._geom_hash` (which
  hashes *absolute* rounded coordinates and therefore sees translation twins
  as distinct).
* :func:`normalize_scene` — rewrite a scene's fractional coordinates into
  that canonical frame in place (atom labels, order, ``fixed`` masks and all
  other per-atom fields untouched — only ``frac`` moves), so newly stored
  structures all live in one frame and the symmetry can't re-enter prose.

The canonical frame is found by brute force over a small exact group: every
2×2 integer matrix ``M`` (entries in ``{-1, 0, 1}``, ``|det| = 1``) that
preserves the in-plane metric ``G = L₂ L₂ᵀ`` (``Mᵀ G M = G``) is an isometry
of the tiling — for the hexagonal fcc(111) cell that is the full 12-element
p6m point group, mirrors included. Each ``M`` is combined with an anchor
translation putting one atom of the scene's rarest element at the in-plane
origin (any lattice translation maps that element's atom set onto itself, so
anchoring over it covers all translations). The candidate with the
lexicographically smallest rounded payload wins; ties are exact re-descriptions
of one another, so the choice is deterministic. The z axis is never touched —
a slab is never flipped or shifted through its vacuum, whatever ``pbc[2]``
claims (the builder emits TTT).

Cost: ≤12 ops × (atoms of the rarest element) anchors × an O(N log N) sort of
N ≤ a-few-dozen rows — noise next to a single relax step.
"""

from __future__ import annotations

import hashlib
import itertools
import json

import numpy as np

from .scene import Scene

#: Decimal places for payload rounding — matches the legacy ``_geom_hash``
#: rounding so the two hashes have the same tolerance to spec formatting.
_ROUND = 3

#: Relative tolerance for the metric-preservation test ``Mᵀ G M = G``.
_METRIC_RTOL = 1e-6


def inplane_symmetry_ops(scene: Scene) -> list[np.ndarray]:
    """Cell-preserving in-plane integer ops for ``scene``'s lattice.

    Returns 2×2 integer matrices ``M`` acting on column fractional
    coordinates (``y = M x``) with ``|det M| = 1`` and ``Mᵀ G M = G`` for the
    in-plane Gram matrix ``G = L₂ L₂ᵀ``. Entries beyond ``{-1, 0, 1}`` never
    occur for the conventional cells our builders emit (hexagonal, square,
    rectangular), so the 81-matrix enumeration is exhaustive here. When the
    scene is not periodic in both in-plane axes there is no tiling to exploit
    and only the identity is returned.
    """
    if not (scene.cell.pbc[0] and scene.cell.pbc[1]):
        return [np.eye(2, dtype=int)]
    l2 = np.asarray(scene.cell.lattice, dtype=float)[:2, :]
    gram = l2 @ l2.T
    scale = max(float(np.abs(gram).max()), 1.0)
    ops: list[np.ndarray] = []
    for entries in itertools.product((-1, 0, 1), repeat=4):
        m = np.array(entries, dtype=int).reshape(2, 2)
        if abs(round(np.linalg.det(m))) != 1:
            continue
        if np.allclose(m.T @ gram @ m, gram, atol=_METRIC_RTOL * scale):
            ops.append(m)
    return ops


def _anchor_labels(scene: Scene) -> list[str]:
    """Labels of every atom of the rarest element (ties → alphabetical min).

    A lattice translation permutes atoms within each element, so anchoring
    over one element's full atom set covers all translations; the rarest
    element (the dopant/adsorbate, usually one atom) keeps the search tiny.
    """
    counts = scene.composition()
    element = min(counts, key=lambda e: (counts[e], e))
    return [a.label for a in scene.atoms.values() if a.element == element]


def _payload(
    scene: Scene, m: np.ndarray, shift: np.ndarray
) -> list[tuple[str, float, float, float]]:
    """The sorted, rounded row list for one ``(op, anchor)`` candidate frame.

    In-plane coordinates are transformed, wrapped, and wrap-after-round
    reduced (so 0.9996 and 0.0 collapse); z is rounded untouched.
    """
    rows = []
    for a in scene.atoms.values():
        y = m @ ((np.asarray(a.frac, dtype=float)[:2] - shift) % 1.0) % 1.0
        rows.append(
            (
                a.element,
                round(float(y[0]), _ROUND) % 1.0,
                round(float(y[1]), _ROUND) % 1.0,
                round(float(a.frac[2]), _ROUND),
            )
        )
    rows.sort()
    return rows


def _best_frame(scene: Scene) -> tuple[str, np.ndarray, np.ndarray]:
    """The winning ``(payload, m, shift)`` over all candidate frames.

    Anchor-position noise is a knife-edge (a shift of 4e-4 moves *every*
    coordinate's third decimal) — same tolerance class as the legacy
    ``_geom_hash``, and moot for the creation-time scenes this runs on,
    whose coordinates are template-exact.
    """
    identity = np.eye(2, dtype=int)
    zero = np.zeros(2)
    if not scene.atoms:
        return json.dumps([], separators=(",", ":")), identity, zero
    translatable = scene.cell.pbc[0] and scene.cell.pbc[1]
    ops = inplane_symmetry_ops(scene)
    shifts = (
        [
            np.asarray(scene.atoms[lbl].frac, dtype=float)[:2]
            for lbl in _anchor_labels(scene)
        ]
        if translatable
        else [zero]
    )
    best: tuple[str, np.ndarray, np.ndarray] | None = None
    for m in ops:
        for shift in shifts:
            payload = json.dumps(_payload(scene, m, shift), separators=(",", ":"))
            if best is None or payload < best[0]:
                best = (payload, m, shift)
    assert best is not None
    return best


def canonical_form(
    scene: Scene,
) -> tuple[np.ndarray, np.ndarray, str]:
    """The canonical frame and hash for ``scene``.

    Returns ``(m, shift, geom_hash_c)``: the winning 2×2 integer op, the
    in-plane fractional anchor shift (subtract, then apply ``m``, then wrap),
    and ``sha256[:12]`` over the winning payload. Two scenes related by any
    cell-preserving in-plane isometry + lattice translation produce the same
    hash. An empty scene hashes its (empty) payload under the identity.
    """
    payload, m, shift = _best_frame(scene)
    return m, shift, hashlib.sha256(payload.encode()).hexdigest()[:12]


def geom_hash_c(scene: Scene) -> str:
    """The symmetry-invariant geometry hash alone (see :func:`canonical_form`)."""
    return canonical_form(scene)[2]


def normalize_scene(scene: Scene) -> bool:
    """Rewrite ``scene``'s atom coordinates into the canonical frame, in place.

    Full-precision transform (no rounding): in-plane coordinates get the
    canonical op + anchor shift and wrap into ``[0, 1)``; z is untouched.
    Atom labels, insertion order, ``fixed`` masks and every other per-atom
    field are preserved, so downstream consumers that rely on build order
    (autocatpath injection) are unaffected.

    Returns ``True`` when any coordinate moved. Refuses (returns ``False``,
    scene untouched) when the scene carries bonds — bond ``image`` offsets
    are frame-dependent and would go stale — or when the cell is not periodic
    in both in-plane axes.
    """
    if scene.bonds or not (scene.cell.pbc[0] and scene.cell.pbc[1]):
        return False
    payload, m, shift = _best_frame(scene)
    # Idempotence: when the scene as stored already realises the winning
    # payload (ties included — several frames can share it), leave it alone
    # rather than hopping between tied frames on every call.
    as_is = json.dumps(
        _payload(scene, np.eye(2, dtype=int), np.zeros(2)), separators=(",", ":")
    )
    if as_is == payload:
        return False
    for a in scene.atoms.values():
        f = np.asarray(a.frac, dtype=float)
        new2 = (m @ ((f[:2] - shift) % 1.0)) % 1.0
        a.frac = np.array([new2[0], new2[1], f[2]])
    return True

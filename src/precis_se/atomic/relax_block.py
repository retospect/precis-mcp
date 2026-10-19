"""Relax an assembled hexfold block: cheap whole-block polish plus an
energy-rung pass on per-junction fragments, then ``tunnel_strain``
re-scored on the relaxed atoms (hexfold H3 —
docs/backlog/hexfold-nanoreactor-tube.md, the
docs/backlog/hexfold-relax-pipeline.md tiers T1/T2 without the module
cache).

This is the compute the ``hexfold_relax`` job type runs on the cluster —
it lives outside the generation op because a 7k-atom scene relaxes in
seconds under ``stick`` but is minutes-to-hours under a real potential.
Every relax goes through the one ladder,
:func:`precis.structure.relax.relax`: ``polish`` picks any rung it
accepts (``ml``/``emt``/``xtb``/``geo``/``clean`` — GFN-FF is not a
supported backend anywhere in the tree, so the T1 whole-block polish is
the MLIP rung by default) and ``fragment`` is ``"xtb"`` (GFN2, the
relaxer of record for hexfold blocks) or ``"none"``.

A junction fragment is a distance cut, not the pipeline spec's full
armchair-cut module: every atom within ``buffer_A`` of a junction atom
joins the fragment, severed bonds to atoms outside it are H-capped along
the bond direction, atoms farther than ``core_A`` from the junction are
frozen (``fixed``), and only the movable atoms' relaxed positions are
pasted back. The junction/seam bookkeeping travels in the stored
record (``meta["generated"]["junctions"]`` / ``["seam_atoms"]`` /
``["sigma_A"]``, minted by the tunnel / hexfold generators) as
**stored-block ordinals** — positions in the block's atom order, which
is the scene's atom order too (atoms are inserted in block order).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from hexfold.build import JunctionRecord
from precis.structure import Atom as StructAtom
from precis.structure import Bond as StructBond
from precis.structure import Scene as StructScene
from precis.structure.relax import RelaxResult, relax
from precis_se.atomic.generators.tunnel import tunnel_strain

log = logging.getLogger(__name__)

#: Rungs ``polish`` may name — anything the relax ladder accepts in-place.
POLISH_RUNGS = frozenset({"ml", "emt", "xtb", "geo", "clean"})
#: Rungs ``fragment`` may name; ``"none"`` skips the junction pass.
FRAGMENT_RUNGS = frozenset({"xtb", "none"})

DEFAULT_BUFFER_A = 12.0  # relax displacement dies out by 10-12 A (me485004)
DEFAULT_CORE_A = 4.0  # movable core inside the frozen buffer shell
_SIGMA_CH_A = 1.09  # the terminator's C-H cap length (hexfold Lattice)


def strain_bookkeeping(
    generated: dict[str, Any],
) -> tuple[list[JunctionRecord], list[list[int]], float] | None:
    """``(junctions, seam_atom_chains, sigma_A)`` from a stored
    ``meta["generated"]`` record — ``None`` when the record carries no
    junction bookkeeping (a block minted before H3, or not a
    composite)."""
    raw = generated.get("junctions")
    if not isinstance(raw, list) or not raw:
        return None
    sigma = generated.get("sigma_A", 1.42)
    zones = [
        JunctionRecord(
            name=str(j.get("name", f"junction{k}")),
            fused=tuple((int(p[0]), int(p[1])) for p in j.get("fused", ())),
            faces=tuple(tuple(int(a) for a in f) for f in j.get("faces", ())),
            atoms=tuple(int(a) for a in j.get("atoms", ())),
        )
        for k, j in enumerate(raw)
        if isinstance(j, dict)
    ]
    seams = [
        [int(a) for a in chain]
        for chain in (generated.get("seam_atoms") or [])
        if isinstance(chain, list)
    ]
    return zones, seams, float(sigma)


@dataclass
class Fragment:
    """One junction's relax fragment (block ordinals + frag-local data)."""

    name: str
    members: list[int]  # block ordinals in the fragment
    movable: set[int]  # block ordinals free to move (the rest are frozen)
    frag_index: dict[int, int]  # block ordinal -> fragment row
    bonds: list[tuple[int, int, float, str]]  # frag-local
    caps: list[tuple[int, np.ndarray]]  # (host frag row, cap position)


@dataclass
class FragmentResult:
    name: str
    atoms: int
    h_caps: int
    frozen: int
    result: RelaxResult


@dataclass
class BlockRelaxReport:
    """The relax receipt: the polish rung's envelope, each fragment's,
    and the ``tunnel_strain`` findings before and after."""

    polish: RelaxResult
    fragments: list[FragmentResult]
    before: list[dict[str, Any]] = field(default_factory=list)
    after: list[dict[str, Any]] = field(default_factory=list)


def _scene_arrays(
    scene: StructScene,
) -> tuple[list[str], np.ndarray, np.ndarray, list[tuple[int, int, float, str]]]:
    """Block-ordinal arrays of the scene: labels in stored order,
    Cartesian coords, elements, and ``(i, j, order, kind)`` bonds over
    those ordinals — the shape :func:`tunnel_strain` reads."""
    labels = list(scene.atoms)
    index = {la: i for i, la in enumerate(labels)}
    coords = np.array(
        [scene.cell.frac_to_cart(scene.atoms[la].frac) for la in labels],
        dtype=float,
    )
    elements = np.array([scene.atoms[la].element for la in labels])
    bonds = [
        (index[b.i], index[b.j], float(b.order), str(b.kind))
        for b in scene.bonds
        if b.i in index and b.j in index
    ]
    return labels, coords, elements, bonds


def cut_junction_fragment(
    coords: np.ndarray,
    elements: np.ndarray,
    bonds: list[tuple[int, int, float, str]],
    jun_atoms: tuple[int, ...],
    name: str,
    *,
    buffer_A: float = DEFAULT_BUFFER_A,
    core_A: float = DEFAULT_CORE_A,
) -> Fragment:
    """The H-capped, buffer-frozen fragment around one junction.

    Members are every atom within ``buffer_A`` of a junction atom (plus
    the junction atoms themselves); the movable core is every member
    within ``core_A``; bonds from a member to a non-member are capped
    with an H along the severed direction at ``_SIGMA_CH_A``."""
    from scipy.spatial import cKDTree

    tree = cKDTree(coords)
    jun = list(jun_atoms)
    member_set = set(jun)
    for row in tree.query_ball_point(coords[jun], r=float(buffer_A)):
        member_set.update(int(a) for a in row)
    movable = set(jun)
    for row in tree.query_ball_point(coords[jun], r=float(core_A)):
        movable.update(int(a) for a in row)
    movable &= member_set
    members = sorted(member_set)
    frag_index = {a: k for k, a in enumerate(members)}
    frag_bonds: list[tuple[int, int, float, str]] = []
    caps: list[tuple[int, np.ndarray]] = []
    for i, j, order, kind in bonds:
        i_in, j_in = i in member_set, j in member_set
        if i_in and j_in:
            frag_bonds.append((frag_index[i], frag_index[j], order, kind))
        elif i_in != j_in:
            host, out = (i, j) if i_in else (j, i)
            vec = coords[out] - coords[host]
            dist = float(np.linalg.norm(vec))
            if dist > 1e-9:
                caps.append((frag_index[host], coords[host] + _SIGMA_CH_A * vec / dist))
    return Fragment(
        name=name,
        members=members,
        movable=movable,
        frag_index=frag_index,
        bonds=frag_bonds,
        caps=caps,
    )


def _fragment_scene(
    scene: StructScene,
    coords: np.ndarray,
    frag: Fragment,
    labels: list[str],
) -> tuple[StructScene, list[str]]:
    """The fragment as its own Scene for :func:`relax`: member atoms on
    their block labels (paste-back identity), frozen atoms pinned via
    ``fixed``, and each cap minted an H label on a ``pairwise`` bond."""
    sub = StructScene(cell=scene.cell)
    frag_labels: list[str] = []
    for a in frag.members:
        la = labels[a]
        src = scene.atoms[la]
        sub.atoms[la] = StructAtom(
            label=la,
            element=src.element,
            frac=scene.cell.wrap(scene.cell.cart_to_frac(coords[a])),
            fixed=int(src.fixed) if a in frag.movable else 7,
            hybridization=src.hybridization,
        )
        frag_labels.append(la)
    for host_row, pos in frag.caps:
        h_label = sub.next_label("H")
        sub.atoms[h_label] = StructAtom(
            label=h_label,
            element="H",
            frac=sub.cell.wrap(sub.cell.cart_to_frac(pos)),
            fixed=0,
            hybridization="s",
        )
        sub.bonds.append(
            StructBond(
                i=sub.atoms[frag_labels[host_row]].label,
                j=h_label,
                order=1.0,
                kind="pairwise",
            )
        )
    for i, j, order, kind in frag.bonds:
        sub.bonds.append(
            StructBond(i=frag_labels[i], j=frag_labels[j], order=order, kind=kind)
        )
    return sub, frag_labels


def relax_block(
    scene: StructScene,
    generated: dict[str, Any],
    *,
    polish: str = "ml",
    model: str | None = None,
    fragment: str = "xtb",
    buffer_A: float = DEFAULT_BUFFER_A,
    core_A: float = DEFAULT_CORE_A,
    steps: int = 200,
) -> BlockRelaxReport:
    """Polish the whole block, relax each junction fragment, re-score
    the strain — mutating ``scene`` to the relaxed geometry.

    ``generated`` is the stored build record (``meta["generated"]``) with
    the H3 bookkeeping; a record without ``junctions`` raises
    ``ValueError``. Rung failures propagate as
    :class:`~precis.structure.relax.RelaxUnsupported` for the caller to
    class as infra, never silently different physics."""
    if polish not in POLISH_RUNGS:
        raise ValueError(
            f"polish must be one of {sorted(POLISH_RUNGS)}; got {polish!r}"
        )
    if fragment not in FRAGMENT_RUNGS:
        raise ValueError(
            f"fragment must be one of {sorted(FRAGMENT_RUNGS)}; got {fragment!r}"
        )
    bookkeeping = strain_bookkeeping(generated)
    if bookkeeping is None:
        raise ValueError(
            "generated record carries no 'junctions' bookkeeping — "
            "hexfold_relax only scores blocks minted with the H3 "
            "strain record (tunnel-reservoirs, fuse/opening builds)"
        )
    zones, seams, sigma = bookkeeping
    labels, coords, elements, bonds = _scene_arrays(scene)
    names = [z.name for z in zones]
    before = [
        f.to_dict()
        for f in tunnel_strain(
            coords, elements, bonds, zones, seams, sigma, names=names
        )
    ]

    polish_result = relax(scene, fidelity=polish, model=model or "mace_mp", steps=steps)

    frag_results: list[FragmentResult] = []
    if fragment != "none":
        for zone in zones:
            # coords re-read per junction: a prior fragment's paste-back
            # may sit inside this one's buffer when junctions are close.
            _, coords, _, _ = _scene_arrays(scene)
            frag = cut_junction_fragment(
                coords,
                elements,
                bonds,
                zone.atoms,
                zone.name,
                buffer_A=buffer_A,
                core_A=core_A,
            )
            sub, frag_labels = _fragment_scene(scene, coords, frag, labels)
            res = relax(sub, fidelity=fragment, steps=steps)
            moved = 0
            for row, a in enumerate(frag.members):
                if a not in frag.movable:
                    continue
                scene.atoms[labels[a]].frac = sub.atoms[frag_labels[row]].frac
                moved += 1
            frag_results.append(
                FragmentResult(
                    name=zone.name,
                    atoms=len(frag.members) + len(frag.caps),
                    h_caps=len(frag.caps),
                    frozen=len(frag.members) - len(frag.movable),
                    result=res,
                )
            )
            log.info(
                "hexfold_relax: fragment %s — %d atoms (%d caps, %d frozen), "
                "%d pasted back, converged=%s",
                zone.name,
                len(frag.members),
                len(frag.caps),
                len(frag.members) - len(frag.movable),
                moved,
                res.converged,
            )

    _, coords, _, _ = _scene_arrays(scene)
    after = [
        f.to_dict()
        for f in tunnel_strain(
            coords, elements, bonds, zones, seams, sigma, names=names
        )
    ]
    return BlockRelaxReport(
        polish=polish_result, fragments=frag_results, before=before, after=after
    )


__all__ = [
    "BlockRelaxReport",
    "Fragment",
    "FragmentResult",
    "cut_junction_fragment",
    "relax_block",
    "strain_bookkeeping",
]

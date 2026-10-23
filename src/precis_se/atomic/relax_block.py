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

Step checkpoints (gr508493). ``relax_block`` takes an optional
``on_checkpoint`` callable and emits one overwrite-slot payload after
every optimiser step — a multi-hour xtb fragment loses at most one step
to a kill, deploy or restart. The payload::

    {"v": 1, "stage": "polish"|"fragment"|"strain",
     "fragment": <in-flight fragment name>|None,
     "stage_steps": <optimiser steps done in the in-flight stage>,
     "curve": <in-flight stage's convergence trace so far>,
     "positions": {atom label: [fa, fb, fc]},
     "polish": <stage record>|None,        # once the polish completes
     "fragments": [<stage record>, ...],   # completed fragments only
     "before": <the strain findings on the ORIGINAL geometry>}

A stage record carries ``{rung, model, converged, n_steps, max_disp,
energy, max_force, curve}`` (fragments add ``name``/``atoms``/``h_caps``/
``frozen``). ``resume`` takes the same dict back: checkpointed positions
are applied to the scene, completed stages are reconstructed (never
re-run), and the in-flight stage runs its remaining ``steps`` budget —
a killed job picks up exactly where it stopped.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
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


def _positions(scene: StructScene) -> dict[str, list[float]]:
    """The block's fractional positions, rounded — the checkpoint's
    geometry. Label-keyed so a later apply tolerates atom-order drift."""
    return {
        la: [round(float(x), 6) for x in atom.frac] for la, atom in scene.atoms.items()
    }


def _apply_positions(scene: StructScene, positions: Any) -> int:
    """Overlay checkpointed ``{label: [fa, fb, fc]}`` onto the scene.
    Returns the count applied; unknown labels and malformed rows are
    skipped (a checkpoint names what it saw, never invents atoms)."""
    if not isinstance(positions, dict):
        return 0
    applied = 0
    for label, frac in positions.items():
        atom = scene.atoms.get(label)
        if atom is None or not isinstance(frac, (list, tuple)) or len(frac) != 3:
            continue
        atom.frac = scene.cell.wrap(np.asarray(frac, dtype=float))
        applied += 1
    return applied


def _stage_record(res: RelaxResult) -> dict[str, Any]:
    """A RelaxResult as the checkpoint/record's JSON-shaped stage record."""
    return {
        "rung": res.rung,
        "model": res.model,
        "converged": res.converged,
        "n_steps": res.n_steps,
        "max_disp": res.max_disp,
        "energy": res.energy,
        "max_force": res.max_force,
        "curve": list(res.curve),
    }


def _result_from_record(rec: dict[str, Any]) -> RelaxResult:
    """Rebuild a completed stage's RelaxResult from its checkpoint record
    — the envelope a resumed run reports, without re-running the stage.
    The qualitative per-atom ``forces`` are not checkpointed (the job's
    write-back never reads them)."""
    return RelaxResult(
        rung=str(rec.get("rung") or "?"),
        converged=bool(rec.get("converged")),
        n_steps=int(rec.get("n_steps") or 0),
        max_disp=float(rec.get("max_disp") or 0.0),
        curve=[float(x) for x in (rec.get("curve") or [])],
        energy=rec.get("energy"),
        max_force=rec.get("max_force"),
        model=rec.get("model"),
    )


def _add_prior(
    res: RelaxResult, prior_steps: int, prior_curve: list[float]
) -> RelaxResult:
    """Fold an interrupted attempt's steps into a resumed stage's result,
    so the report carries the stage's whole cost, not this attempt's."""
    res.n_steps += prior_steps
    res.curve = list(prior_curve) + list(res.curve)
    return res


def _fragment_record(
    zone_name: str, frag: Fragment, res: RelaxResult
) -> dict[str, Any]:
    """One completed fragment's checkpoint record — the stage record plus
    the fragment's shape (atoms/caps/frozen), keyed on the zone name."""
    return {
        "name": zone_name,
        "atoms": len(frag.members) + len(frag.caps),
        "h_caps": len(frag.caps),
        "frozen": len(frag.members) - len(frag.movable),
        **_stage_record(res),
    }


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
    on_checkpoint: Callable[[dict[str, Any]], None] | None = None,
    resume: dict[str, Any] | None = None,
) -> BlockRelaxReport:
    """Polish the whole block, relax each junction fragment, re-score
    the strain — mutating ``scene`` to the relaxed geometry.

    ``generated`` is the stored build record (``meta["generated"]``) with
    the H3 bookkeeping; a record without ``junctions`` raises
    ``ValueError``. Rung failures propagate as
    :class:`~precis.structure.relax.RelaxUnsupported` for the caller to
    class as infra, never silently different physics.

    ``on_checkpoint`` gets one overwrite-slot payload after every
    optimiser step (module docstring for the shape); ``resume`` is such
    a payload from a killed attempt — completed stages are skipped, the
    in-flight one runs its remaining ``steps`` budget."""
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

    resume = resume if isinstance(resume, dict) else {}
    applied = _apply_positions(scene, resume.get("positions"))
    if applied:
        _, coords, _, _ = _scene_arrays(scene)
        log.info("hexfold_relax: resumed %d checkpointed atom positions", applied)
    # The strain baseline is the ORIGINAL geometry's — a resumed run
    # inherits it from the checkpoint rather than re-scoring the
    # partially-relaxed atoms and understating the before/after delta.
    before = resume.get("before")
    if not isinstance(before, list) or not before:
        before = [
            f.to_dict()
            for f in tunnel_strain(
                coords, elements, bonds, zones, seams, sigma, names=names
            )
        ]

    polish_rec = (
        dict(resume["polish"]) if isinstance(resume.get("polish"), dict) else None
    )
    frag_recs: list[dict[str, Any]] = [
        dict(f)
        for f in (resume.get("fragments") or [])
        if isinstance(f, dict) and f.get("name")
    ]
    frag_done = {str(f["name"]): f for f in frag_recs}
    stage = str(resume.get("stage") or "")
    inflight_frag = (
        str(resume["fragment"])
        if stage == "fragment" and resume.get("fragment")
        else None
    )
    prior_steps = int(resume.get("stage_steps") or 0)
    prior_curve = [
        float(x) for x in (resume.get("curve") or []) if isinstance(x, (int, float))
    ]

    def _emit(
        stage_name: str,
        *,
        frag: str | None = None,
        stage_steps: int = 0,
        curve: list[float] | None = None,
    ) -> None:
        if on_checkpoint is None:
            return
        on_checkpoint(
            {
                "v": 1,
                "stage": stage_name,
                "fragment": frag,
                "stage_steps": int(stage_steps),
                "curve": list(curve or []),
                "positions": _positions(scene),
                "polish": polish_rec,
                "fragments": list(frag_recs),
                "before": before,
            }
        )

    _emit(
        stage or "polish",
        frag=inflight_frag,
        stage_steps=prior_steps,
        curve=prior_curve,
    )

    t0 = time.monotonic()
    polish_result: RelaxResult
    if polish_rec is not None:
        polish_result = _result_from_record(polish_rec)
        log.info(
            "hexfold_relax: polish[%s] skipped — checkpoint has it done "
            "(%d steps, converged=%s)",
            polish,
            polish_result.n_steps,
            polish_result.converged,
        )
    else:
        polish_prior = prior_steps if stage == "polish" else 0
        polish_curve = prior_curve if stage == "polish" else []

        def _polish_step(n: int, pos: np.ndarray, curve: list[float]) -> None:
            for idx, la in enumerate(labels):
                scene.atoms[la].frac = scene.cell.wrap(
                    scene.cell.cart_to_frac(pos[idx])
                )
            _emit(
                "polish",
                stage_steps=polish_prior + n,
                curve=polish_curve + list(curve),
            )

        log.info(
            "hexfold_relax: polish[%s] start — %d atoms, %d steps max%s",
            polish,
            len(labels),
            steps - polish_prior,
            f" (resuming at step {polish_prior})" if polish_prior else "",
        )
        polish_result = relax(
            scene,
            fidelity=polish,
            model=model or "mace_mp",
            steps=max(steps - polish_prior, 0),
            on_step=_polish_step if on_checkpoint is not None else None,
        )
        if polish_prior:
            polish_result = _add_prior(polish_result, polish_prior, polish_curve)
        polish_rec = _stage_record(polish_result)
        # Stage boundary: the polish is done and now lives in the
        # record, so stage_steps must not count it a second time
        # (the in-flight stage is whatever runs next, at 0 steps).
        _emit("fragment" if fragment != "none" else "strain", stage_steps=0)
        log.info(
            "hexfold_relax: polish[%s] done — %d steps, converged=%s, %.0fs",
            polish,
            polish_result.n_steps,
            polish_result.converged,
            time.monotonic() - t0,
        )

    frag_results: list[FragmentResult] = []
    if fragment != "none":
        for zone in zones:
            done = frag_done.get(zone.name)
            if done is not None:
                frag_results.append(
                    FragmentResult(
                        name=zone.name,
                        atoms=int(done.get("atoms") or 0),
                        h_caps=int(done.get("h_caps") or 0),
                        frozen=int(done.get("frozen") or 0),
                        result=_result_from_record(done),
                    )
                )
                log.info(
                    "hexfold_relax: fragment %s skipped — checkpoint has it "
                    "done (%d steps, converged=%s)",
                    zone.name,
                    int(done.get("n_steps") or 0),
                    done.get("converged"),
                )
                continue
            t_frag = time.monotonic()
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
            frag_prior = prior_steps if inflight_frag == zone.name else 0
            frag_curve = prior_curve if frag_prior else []

            def _frag_step(
                n: int,
                pos: np.ndarray,
                curve: list[float],
                _frag: Fragment = frag,
                _labels: list[str] = frag_labels,
                _zone: str = zone.name,
                _prior: int = frag_prior,
                _curve: list[float] = frag_curve,
            ) -> None:
                # pos rows follow the SUB scene's atom order — members
                # first, caps last; only movable members write back.
                for row, a in enumerate(_frag.members):
                    if a not in _frag.movable:
                        continue
                    scene.atoms[_labels[row]].frac = scene.cell.wrap(
                        scene.cell.cart_to_frac(pos[row])
                    )
                _emit(
                    "fragment",
                    frag=_zone,
                    stage_steps=_prior + n,
                    curve=_curve + list(curve),
                )

            res = relax(
                sub,
                fidelity=fragment,
                steps=max(steps - frag_prior, 0),
                on_step=_frag_step if on_checkpoint is not None else None,
            )
            if frag_prior:
                res = _add_prior(res, frag_prior, frag_curve)
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
            frag_recs.append(_fragment_record(zone.name, frag, res))
            # Done boundary: the fragment is recorded, so it is no
            # longer the in-flight stage (frag=None, 0 steps).
            _emit("fragment", frag=None, stage_steps=0)
            log.info(
                "hexfold_relax: fragment %s — %d atoms (%d caps, %d frozen), "
                "%d pasted back, %d steps, converged=%s, %.0fs",
                zone.name,
                len(frag.members),
                len(frag.caps),
                len(frag.members) - len(frag.movable),
                moved,
                res.n_steps,
                res.converged,
                time.monotonic() - t_frag,
            )

    _emit("strain")
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

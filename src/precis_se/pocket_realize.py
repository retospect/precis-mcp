"""The realize loop — pocket-from-molecule slice 4
(docs/backlog/pocket-from-molecule.md), the first implementation of the
intent-to-realize pick → join → check loop.

**Pick.** Each candidate is a wall atom cloud posed in the se world
frame — today a ``structure`` design's scene posed by the wall block's
own transform (:class:`WallCandidate`). The candidate *set* is an input;
nothing here generates walls. The scaffold chemistry the spec's
candidates answer (a carbon ring tethered inside the tube, hydroxyl
"sugar" and alkyl "oil" pendants on CH2 separators — open question 2's
answer) is what a wall structure expresses; the loop is agnostic to it.

**Join.** For each candidate, :func:`precis_se.atomic.clearance.
cluster_cut` takes the wall atoms within ``clearance +
CLUSTER_MARGIN_A`` of the pocket's molecule surface (the pocket shape
``mol:<block>[!<label>]@<clearance>`` supplies both the molecule and
the offset), :func:`precis_se.atomic.clearance.cap_dangling` caps the
severed bonds with H at the X–H covalent distance, the molecule is
placed at its stored pose — the pocket was inverted from that pose —
and :func:`precis.structure.relax._relax_xtb` relaxes the complex at
GFN2 with the far wall pinned (cluster atoms beyond ``clearance +
FREE_MARGIN_A`` of the surface, and the caps whose anchors are
pinned). Charge is the sum of formal charges and multiplicity the
lowest the electron count allows, matching
:func:`precis_se.properties.pocket_spec.painted_molecule`.

**Check.** The score is three numbers the spec lists:

- binding energy = E(relaxed complex) − E(wall atoms at their relaxed
  positions) − E(molecule at its relaxed position), all GFN2 single
  points;
- each region measure's computed value against its band — the
  slice-2a wall computers (:data:`precis_se.properties.COMPUTERS`) and
  the ``mcone:`` count (:func:`precis_se.properties.computers.
  cone_atom_count`) evaluated on the *relaxed* wall, with region
  membership resolved in the pocket's own frame (the molecule stays at
  its spec pose; a v1 frame mix, documented not hidden);
- for a held-shape pocket (``mol:<block>!<label>``) the Kabsch-aligned
  RMSD of the fragment from its frozen shape — also reported for case
  a as the molecule's drift.

A ``binding energy`` measure takes the relax's number, not a computer.
A measure whose measurand has no computer (the categorical
hydrophobicity index) is *uncomputable* — reported with its reason,
never counted as unmet. Ranking is lexicographic on
``(unmet hard measures, unmet measures of any strength, binding
energy, name)``: fewer unmet first, then stronger binding.

**Iterate.** Candidates evaluate in order — the greedy re-pick: a
failing check moves to the next candidate, never a silent placement.
``cap`` bounds the evaluations; the loop ends when the set is
exhausted or the cap is hit, and reports the ranking plus the failing
regions of every loser. This is a research loop, not a solver: a run
can end with no candidate inside every band (``solved`` is then
``False`` and the failing regions are the answer).

**Seam, not engine (open questions 5–7).** Slice 4 runs GFN2 only.
The loop accepts a ``dft`` mapping — autocatpath's ``dft`` config
block from a quest ``reaction_config`` — and carries it verbatim into
provenance so slice 5's refinement ladder (UMA then GPAW, the Q7
correction rule) takes it unchanged. No catpath code is imported.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from precis.cad.vec import as_vec3
from precis.cad.vec import pose as cad_pose
from precis.errors import BadInput, Unsupported
from precis_se.pockets import region_measures

#: Wall atoms within this of the pocket surface join the relax cluster,
#: on top of the pocket's own clearance (builder pick 2026-10-25): the
#: computers paint ``clearance + 3 Å``; the relax wants one more Å of
#: chemical environment around the contact.
CLUSTER_MARGIN_A = 4.0

#: Cluster atoms within this of the pocket surface relax free; the far
#: wall beyond it is pinned (builder pick 2026-10-25) — the scaffold
#: holds its shape while the contact moves.
FREE_MARGIN_A = 3.0

#: FIRE budget for one candidate's relax (``_relax_xtb``; its force
#: tolerance floors at 0.05 eV/Å).
RELAX_STEPS = 300

#: Bound on the joined complex's atom count: a bigger cluster is a
#: slice-5 problem (its DFT ruling caps the checked cluster at ~150).
MAX_COMPLEX_ATOMS = 160

#: The measurand slug the relax's own binding energy feeds — seeded by
#: core migration 0228. It has no computer by design: the number comes
#: from the join, not a region evaluation.
BINDING_ENERGY_SLUG = "binding-energy"


@dataclass
class WallCandidate:
    """One wall candidate: a posed atom cloud (world frame, Å) with
    provenance. ``source`` is whatever made it — today a ``structure``
    design slug; ``meta`` carries the ref meta (``version`` rides into
    the result's provenance)."""

    name: str
    source: str
    labels: list[str]
    elements: list[str]
    coords_A: NDArray[np.float64]
    formal: list[int]
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class MeasureScore:
    """One pocket region measure scored against its band. ``met`` is
    ``True``/``False`` inside/outside the band, ``None`` when
    uncomputable — ``reason`` then says why (no computer, GFN2 failure,
    an unresolvable region)."""

    name: str
    datum: str | None
    measurand: str | None
    strength: str
    value: float | None
    min_value: float | None
    max_value: float | None
    met: bool | None
    violation: float | None
    reason: str | None = None

    @property
    def band(self) -> str:
        lo = "−∞" if self.min_value is None else f"{self.min_value:g}"
        hi = "+∞" if self.max_value is None else f"{self.max_value:g}"
        return f"[{lo}, {hi}]"


@dataclass
class CandidateResult:
    """One candidate's join + check: the energies, the per-measure
    scores, the loop metadata and its provenance. ``error`` names a
    candidate the loop could not even join (no wall atoms in the cut,
    a complex over :data:`MAX_COMPLEX_ATOMS`, a backend failure) — it
    ranks last and is never silent."""

    name: str
    source: str
    iteration: int
    met: bool
    unmet_hard: int
    unmet_soft: int
    uncomputable: int
    scores: list[MeasureScore]
    binding_eV: float | None
    e_complex_eV: float | None
    e_wall_eV: float | None
    e_mol_eV: float | None
    mol_drift_A: float | None
    held_rmsd_A: float | None
    cluster_atoms: int
    cap_atoms: int
    pinned_atoms: int
    relax_converged: bool | None
    relax_steps: int | None
    provenance: dict[str, Any]
    error: str | None = None

    @property
    def failing(self) -> list[str]:
        """Names of the measures missed — the spec's "which regions
        failed" answer, in pocket order."""
        return [s.name for s in self.scores if s.met is False]


@dataclass
class PocketRealizeRequest:
    """The op's answer when GFN2 is absent locally (the prod MCP image's
    aarch64 case, open question 13 / gr519389): mint an
    ``se_pocket_realize`` job once the call's tree commits — the
    pending-jobs path ``SimpRequest`` already rides, because the job
    reloads the pocket from the saved design and a job minted mid-walk
    could dispatch against a tree that never lands. ``target_node`` is
    resolved at op time (the lane check is part of the up-front
    refusal); ``digest`` is the idem key's tail — same wall + pocket +
    candidate set collapses onto the in-flight job."""

    wall: str
    pocket: str
    candidates: list[str]
    cap: int | None
    dft: dict[str, Any] | None
    target_node: str
    digest: str


@dataclass
class RealizeResult:
    """The loop's output: the ranked candidates, the loop metadata and
    whether the best met every hard band. ``evaluated`` counts the
    picks that ran (≤ ``cap``); ``unevaluated`` the candidates the cap
    left untried."""

    pocket: str
    class_hash: str | None
    results: list[CandidateResult]
    cap: int
    evaluated: int
    unevaluated: int
    solved: bool


def _kabsch_rmsd(mobile: Any, target: Any) -> float:
    """RMS per-atom deviation (Å) of ``mobile`` after least-squares
    rigid superposition on ``target`` — the same Kabsch as
    :func:`precis_se.properties.pocket_spec._kabsch_onto`."""
    p = np.asarray(mobile, dtype=float)
    q = np.asarray(target, dtype=float)
    if not len(p):
        return 0.0
    pc, qc = p.mean(axis=0), q.mean(axis=0)
    h = (p - pc).T @ (q - qc)
    u, _s, vt = np.linalg.svd(h)
    d = np.sign(np.linalg.det(vt.T @ u.T)) or 1.0
    rot = vt.T @ np.diag([1.0, 1.0, d]) @ u.T
    aligned = (rot @ (p - pc).T).T + qc
    return float(np.sqrt(((aligned - q) ** 2).sum(axis=1).mean()))


def _pocket_molecule(
    tree: Any, pocket: Any
) -> tuple[str, str | None, float, list[str], list[str], Any, list[int]] | str:
    """``(block, without, clearance Å, labels, elements, coords Å,
    formal)`` behind the pocket's ``mol:<ref>@<clearance>`` shape, or
    an error string."""
    from precis_se import datums as se_datums
    from precis_se.datums import posed_fragment_atoms
    from precis_se.measures import MeasureError

    try:
        sel = se_datums.parse_selector(pocket.shape or "")
    except MeasureError:
        return f"pocket {pocket.name!r} has no mol: shape — nothing to realize"
    if sel.kind != "mol" or not sel.instance:
        return (
            f"pocket {pocket.name!r} shape {pocket.shape!r} is not a "
            "mol:<block>@<clearance> — nothing to realize"
        )
    got = posed_fragment_atoms(tree, sel.instance, sel.without)
    if isinstance(got, str):
        return got
    if got is None or not got[0]:
        return f"block {sel.instance!r} binds no loadable structure"
    labels, elements, coords_A, formal, _meta = got
    return (
        sel.instance,
        sel.without,
        float(sel.clearance or 0.0) * 1e10,
        labels,
        elements,
        np.asarray(coords_A, dtype=float),
        formal,
    )


def _scene_from_atoms(
    elements: list[str],
    coords_A: NDArray[np.float64],
    *,
    formal: list[int] | None = None,
    fixed: NDArray[np.bool_] | None = None,
    centre: bool = True,
) -> Any:
    """A :class:`precis.structure.scene.Scene` holding ``elements`` at
    ``coords_A`` (Å) in a non-periodic cell — the shape
    :func:`precis_se.datums.posed_structure_atoms` and ``_relax_xtb``
    both read. ``fixed`` marks pins (``FIX_ALL``). ``centre`` (the
    relax scene) shifts the cloud to the cell centre; ``False`` (the
    resolver scene) stores the coordinates verbatim — its
    ``frac_to_cart`` must return the frame the coordinates are in."""
    from precis.structure.cell import Cell
    from precis.structure.scene import FIX_ALL, Atom, Scene

    xyz = np.asarray(coords_A, dtype=float).reshape(-1, 3)
    side = float(max(np.ptp(xyz, axis=0).max(initial=1.0), 1.0)) + 20.0
    centre_pt = xyz.mean(axis=0) if len(xyz) else np.zeros(3)
    shift = np.full(3, side / 2.0) - centre_pt if centre else np.zeros(3)
    cell = Cell.from_lengths_angles(side, side, side, pbc=(False, False, False))
    scene = Scene(cell=cell)
    labels = []
    formal = formal or [0] * len(elements)
    for k, (el, p) in enumerate(zip(elements, xyz, strict=True)):
        label = scene.next_label(el)
        atom = Atom(
            label,
            el,
            cell.cart_to_frac(p + shift),
            fixed=FIX_ALL if fixed is not None and fixed[k] else 0,
            charge=int(formal[k]),
        )
        scene.atoms[label] = atom
        labels.append(label)
    return scene, labels, shift


def _default_relaxer(scene: Any, *, charge: int, multiplicity: int) -> Any:
    """GFN2 relax of the joined complex — the seam tests stub and slice
    5's ladder extends (its UMA/DFT rungs take the same scene)."""
    from precis.structure.relax import _relax_xtb

    return _relax_xtb(
        scene, steps=RELAX_STEPS, tol=0.0, charge=charge, multiplicity=multiplicity
    )


def _default_single_point(
    elements: list[str], coords: Any, *, charge: int, multiplicity: int
) -> float:
    """GFN2 single-point energy (eV) — the seam tests stub."""
    from precis_se.properties import surface as se_surface

    return float(
        se_surface.gfn2_single_point(
            elements, coords, charge=charge, multiplicity=multiplicity
        ).energy_eV
    )


def evaluate_candidate(
    tree: Any,
    wall: str,
    pocket: Any,
    candidate: WallCandidate,
    *,
    iteration: int,
    relaxer: Any = None,
    single_point: Any = None,
    dft: dict[str, Any] | None = None,
) -> CandidateResult:
    """Join + check one candidate wall against ``pocket`` (module
    docstring). Never raises for the candidate itself: a join or
    backend failure lands in ``error`` and ranks last."""
    from precis_se import datums as se_datums
    from precis_se.atomic import clearance as se_clearance
    from precis_se.ops import BoundScene
    from precis_se.properties import COMPUTERS
    from precis_se.properties import computers as se_computers
    from precis_se.properties import surface as se_surface

    relaxer = relaxer or _default_relaxer
    single_point = single_point or _default_single_point
    provenance: dict[str, Any] = {
        "candidate": candidate.name,
        "source": candidate.source,
        "class_hash": pocket.class_hash,
        "iteration": iteration,
        "backend": "gfn2-xtb",
        "cluster_margin_A": CLUSTER_MARGIN_A,
        "free_margin_A": FREE_MARGIN_A,
    }
    if candidate.meta.get("version") is not None:
        provenance["source_version"] = candidate.meta["version"]
    if dft is not None:
        # The Q5/Q7 seam: catpath's ``dft`` config rides verbatim so a
        # later rung consumes it unchanged — read, never interpreted.
        provenance["dft"] = dict(dft)

    def _result(error: str | None = None, **kw: Any) -> CandidateResult:
        scores = kw.pop("scores", [])
        unmet_hard = sum(1 for s in scores if s.met is False and s.strength == "hard")
        unmet_soft = sum(1 for s in scores if s.met is False and s.strength != "hard")
        uncomputable = sum(1 for s in scores if s.met is None)
        return CandidateResult(
            name=candidate.name,
            source=candidate.source,
            iteration=iteration,
            met=error is None and unmet_hard == 0,
            unmet_hard=unmet_hard,
            unmet_soft=unmet_soft,
            uncomputable=uncomputable,
            scores=scores,
            binding_eV=kw.get("binding_eV"),
            e_complex_eV=kw.get("e_complex_eV"),
            e_wall_eV=kw.get("e_wall_eV"),
            e_mol_eV=kw.get("e_mol_eV"),
            mol_drift_A=kw.get("mol_drift_A"),
            held_rmsd_A=kw.get("held_rmsd_A"),
            cluster_atoms=kw.get("cluster_atoms", 0),
            cap_atoms=kw.get("cap_atoms", 0),
            pinned_atoms=kw.get("pinned_atoms", 0),
            relax_converged=kw.get("relax_converged"),
            relax_steps=kw.get("relax_steps"),
            provenance=provenance,
            error=error,
        )

    mol = _pocket_molecule(tree, pocket)
    if isinstance(mol, str):
        return _result(mol)
    mol_block, without, clearance_A, m_labels, m_elements, m_coords, m_formal = mol
    points, _normals, _owner = se_surface.sample_surface(
        m_coords,
        se_surface.vdw_radii(list(m_elements), se_surface.SURFACE_SETTINGS.scale),
        se_surface.SURFACE_SETTINGS.density_per_A2,
    )
    wall_coords = np.asarray(candidate.coords_A, dtype=float)
    keep = se_clearance.cluster_cut(wall_coords, points, clearance_A + CLUSTER_MARGIN_A)
    if not len(keep):
        return _result(
            f"no wall atoms within {clearance_A + CLUSTER_MARGIN_A:g} Å of "
            "the pocket surface — the candidate does not cover the pocket"
        )
    n_wall = len(keep)
    # The bound check runs BEFORE the bond finding: a several-thousand
    # atom wall whose cut alone is already over the bound refuses here,
    # without paying for cap_dangling's kept × all distance matrix.
    if n_wall + len(m_elements) > MAX_COMPLEX_ATOMS:
        return _result(
            f"{n_wall} wall atoms in the cut plus the "
            f"{len(m_elements)}-atom molecule is already over the "
            f"{MAX_COMPLEX_ATOMS}-atom bound, before any H caps",
            cluster_atoms=n_wall,
        )
    cl_elements, cl_coords, anchors = se_clearance.cap_dangling(
        list(candidate.elements), wall_coords, keep
    )
    n_caps = len(anchors)
    # The free shell: wall atoms within clearance + FREE_MARGIN_A of the
    # surface move; beyond it the wall is pinned, and a cap is pinned
    # with its anchor.
    free_shell = se_clearance.cluster_cut(
        cl_coords[:n_wall], points, clearance_A + FREE_MARGIN_A
    )
    free = np.zeros(n_wall, dtype=bool)
    free[free_shell] = True
    pinned_flags = np.concatenate(
        [
            ~free,
            np.asarray([not free[int(a)] for a in anchors], dtype=bool),
            np.zeros(len(m_elements), dtype=bool),
        ]
    )
    cx_elements = list(cl_elements) + list(m_elements)
    cx_coords = np.concatenate([cl_coords, m_coords], axis=0)
    cx_formal = (
        [int(candidate.formal[int(i)]) for i in keep]
        + [0] * n_caps
        + [int(q) for q in m_formal]
    )
    if len(cx_elements) > MAX_COMPLEX_ATOMS:
        return _result(
            f"{len(cx_elements)} atoms in the joined complex is over the "
            f"{MAX_COMPLEX_ATOMS}-atom bound (cluster {n_wall} + caps "
            f"{n_caps} + molecule {len(m_elements)})",
            cluster_atoms=n_wall,
            cap_atoms=n_caps,
        )
    try:
        charge = sum(cx_formal)
        electrons = se_surface.check_charge_multiplicity(cx_elements, charge, None)
        multiplicity = 1 if electrons % 2 == 0 else 2
        scene, labels, shift = _scene_from_atoms(
            cx_elements, cx_coords, formal=cx_formal, fixed=pinned_flags
        )
        relax = relaxer(scene, charge=charge, multiplicity=multiplicity)
    except Exception as exc:
        return _result(f"join failed: {exc}", cluster_atoms=n_wall, cap_atoms=n_caps)
    relaxed_A = (
        np.asarray(
            [scene.cell.frac_to_cart(scene.atoms[la].frac) for la in labels],
            dtype=float,
        )
        - shift
    )
    w_relaxed = relaxed_A[: n_wall + n_caps]
    m_relaxed = relaxed_A[n_wall + n_caps :]
    try:
        e_complex = float(relax.energy)
        e_wall = single_point(
            cl_elements,
            w_relaxed,
            charge=sum(cx_formal[: n_wall + n_caps]),
            multiplicity=1
            if se_surface.check_charge_multiplicity(
                cl_elements, sum(cx_formal[: n_wall + n_caps]), None
            )
            % 2
            == 0
            else 2,
        )
        e_mol = single_point(
            list(m_elements),
            m_relaxed,
            charge=sum(int(q) for q in m_formal),
            multiplicity=1
            if se_surface.check_charge_multiplicity(
                list(m_elements), sum(int(q) for q in m_formal), None
            )
            % 2
            == 0
            else 2,
        )
    except Exception as exc:
        return _result(f"scoring failed: {exc}", cluster_atoms=n_wall)
    binding = e_complex - e_wall - e_mol
    mol_drift = _kabsch_rmsd(m_relaxed, m_coords)
    held_rmsd = mol_drift if without else None

    # Region scores on the relaxed wall: the tree's structure resolver
    # is swapped for the relaxed cloud — cluster atoms at their relaxed
    # positions, the rest of the candidate untouched — while region
    # membership stays in the pocket's frame (the molecule's spec pose).
    # The resolver scene holds the wall's LOCAL coords (the block's
    # pose is applied on top by posed_structure_atoms), so the relaxed
    # world coordinates go back through the inverse pose.
    relaxed_wall = wall_coords.copy()
    relaxed_wall[keep] = w_relaxed[:n_wall]
    wall_node = tree.blocks.get(wall)
    if wall_node is None:
        return _result(
            f"no block {wall!r} — the pocket's regions live on it",
            cluster_atoms=n_wall,
            cap_atoms=n_caps,
        )
    inv = cad_pose(as_vec3(wall_node.pose), as_vec3(wall_node.rot)).inverse()
    local_relaxed = np.asarray(
        [
            np.asarray(inv.apply(as_vec3([float(c) * 1e-10 for c in p])), dtype=float)
            * 1e10
            for p in relaxed_wall
        ],
        dtype=float,
    )
    w_scene, _w_labels, _w_shift = _scene_from_atoms(
        list(candidate.elements),
        local_relaxed,
        formal=list(candidate.formal),
        centre=False,
    )
    orig_resolver = getattr(tree, "structures", None)
    # The binding is read off the owner (the template when the block
    # instances one), the same lookup posed_structure_atoms applies.
    owner = tree.blocks.get(wall_node.template) if wall_node.template else wall_node
    bound_slug = getattr(owner or wall_node, "bound", None)
    t2 = copy.copy(tree)

    def _resolver(slug: str) -> Any:
        if slug == bound_slug:
            return BoundScene(scene=w_scene, meta=dict(candidate.meta))
        return orig_resolver(slug) if orig_resolver is not None else None

    t2.structures = _resolver
    measures: dict[str, Any] = {}
    for region in pocket.regions:
        for m in region_measures(tree, wall, region):
            measures[f"{m.block}.{m.name}"] = m
    scores: list[MeasureScore] = []
    for m in measures.values():
        value: float | None = None
        reason: str | None = None
        got = se_computers.cone_atom_count(t2, m)
        if got is not None:
            value, reason = got
        elif m.measurand == BINDING_ENERGY_SLUG:
            value = binding
        elif m.measurand is not None and m.measurand in COMPUTERS:
            computer = COMPUTERS[m.measurand]
            value = computer(t2, m)
            if value is None:
                reason = getattr(computer, "reason", None) or "no computed value"
        elif m.measurand is None and m.datum is not None:
            try:
                rdat = se_datums.resolve(
                    t2, wall_node, se_datums.parse_selector(m.datum)
                )
                if rdat.error is None:
                    value = None
                    reason = "geometric datum — not scored by this loop"
                else:
                    reason = rdat.error
            except Exception as exc:
                reason = str(exc)
        else:
            reason = f"no computer registered for {m.measurand or m.unit!r}"
        if value is None and reason is None:
            reason = "uncomputable"
        if value is None:
            met: bool | None = None
            violation = None
        else:
            under = m.min_value is not None and value < m.min_value
            over = m.max_value is not None and value > m.max_value
            met = not (under or over)
            violation = max(
                (m.min_value - value) if under else 0.0,
                (value - m.max_value) if over else 0.0,
            )
        scores.append(
            MeasureScore(
                name=m.name,
                datum=m.datum,
                measurand=m.measurand,
                strength=m.strength,
                value=value,
                min_value=m.min_value,
                max_value=m.max_value,
                met=met,
                violation=violation,
                reason=reason,
            )
        )
    return _result(
        scores=scores,
        binding_eV=round(binding, 6),
        e_complex_eV=round(e_complex, 6),
        e_wall_eV=round(e_wall, 6),
        e_mol_eV=round(e_mol, 6),
        mol_drift_A=round(mol_drift, 4),
        held_rmsd_A=round(held_rmsd, 4) if held_rmsd is not None else None,
        cluster_atoms=n_wall,
        cap_atoms=n_caps,
        pinned_atoms=int(pinned_flags.sum()),
        relax_converged=bool(getattr(relax, "converged", False)),
        relax_steps=int(getattr(relax, "n_steps", 0) or 0),
    )


def _require_gfn2() -> None:
    """Refuse up front when the GFN2 backend is absent: the default
    relaxer and single point both need tblite, and on the prod MCP
    image (no linux-aarch64 wheel — open question 13) a missing
    backend would otherwise surface per candidate as ``join failed``
    and the echo would read like a research result. Probing by running
    the calculator's own construction keeps one source of truth for
    what "installed" means (in-process or isolated child)."""
    from precis.structure.relax import RelaxUnsupported, _xtb_calculator

    try:
        calc = _xtb_calculator(charge=0, multiplicity=None)
    except RelaxUnsupported as exc:
        raise Unsupported(
            f"pocket_realize needs GFN2 (tblite): {exc}",
            next="run on a host with tblite (open question 13)",
        ) from exc
    close = getattr(calc, "close", None)
    if callable(close):
        close()


def realize_pocket(
    tree: Any,
    wall: str,
    pocket: Any,
    candidates: list[WallCandidate],
    *,
    cap: int | None = None,
    relaxer: Any = None,
    single_point: Any = None,
    dft: dict[str, Any] | None = None,
) -> RealizeResult:
    """The greedy loop: evaluate candidates in order until the set is
    exhausted or ``cap`` evaluations ran, then rank (module docstring).
    A default relaxer or single point means the loop needs GFN2 —
    :func:`_require_gfn2` refuses once, up front, when tblite is
    absent; a stubbed backend needs no probe."""
    if relaxer is None or single_point is None:
        _require_gfn2()
    cap = len(candidates) if cap is None else max(1, int(cap))
    results = [
        evaluate_candidate(
            tree,
            wall,
            pocket,
            c,
            iteration=k,
            relaxer=relaxer,
            single_point=single_point,
            dft=dft,
        )
        for k, c in enumerate(candidates[:cap])
    ]
    results.sort(
        key=lambda r: (
            r.error is not None,
            r.unmet_hard,
            r.unmet_hard + r.unmet_soft,
            r.binding_eV if r.binding_eV is not None else math.inf,
            r.name,
        )
    )
    best = results[0] if results else None
    return RealizeResult(
        pocket=pocket.name,
        class_hash=pocket.class_hash,
        results=results,
        cap=cap,
        evaluated=len(results),
        unevaluated=max(0, len(candidates) - len(results)),
        solved=bool(best is not None and best.met and best.error is None),
    )


def _candidate_from_structure(
    store: Any, slug: str, wall_node: Any
) -> WallCandidate | str:
    """Load a ``structure`` design as a candidate cloud, posed by the
    wall block's transform (the same placement
    :func:`precis_se.datums.posed_structure_atoms` applies)."""
    ref = store.get_ref(kind="structure", id=slug)
    if ref is None:
        return f"no structure {slug!r}"
    try:
        scene, _handles = store.structure_load(ref.id)
    except Exception as exc:
        return f"structure {slug!r} failed to load: {exc}"
    labels = list(scene.atoms)
    if not labels:
        return f"structure {slug!r} has no atoms"
    local_A = np.asarray(
        [scene.cell.frac_to_cart(scene.atoms[la].frac) for la in labels],
        dtype=float,
    )
    placement = cad_pose(as_vec3(wall_node.pose), as_vec3(wall_node.rot))
    coords_A = np.asarray(
        [
            np.asarray(
                placement.apply(as_vec3([float(c) * 1e-10 for c in p])), dtype=float
            )
            * 1e10
            for p in local_A
        ],
        dtype=float,
    )
    return WallCandidate(
        name=slug,
        source=f"structure:{slug}",
        labels=labels,
        elements=[scene.atoms[la].element for la in labels],
        coords_A=coords_A,
        formal=[int(scene.atoms[la].charge or 0) for la in labels],
        meta=dict(ref.meta or {}),
    )


def _report(result: RealizeResult) -> str:
    """The op's echo: the ranking with per-candidate energies,
    failing regions and provenance."""
    lines = [
        f"pocket_realize: pocket {result.pocket!r} "
        f"(class {str(result.class_hash or '—')[:12]}) — "
        f"{result.evaluated} candidate(s) evaluated"
        + (f", {result.unevaluated} left over the cap" if result.unevaluated else "")
    ]
    for rank, r in enumerate(result.results, 1):
        if r.error is not None:
            lines.append(f"{rank}. {r.name} — not joined: {r.error}")
            continue
        energy = (
            f"binding {r.binding_eV:+.3f} eV"
            if r.binding_eV is not None
            else "binding —"
        )
        relax = (
            f"relax {'converged' if r.relax_converged else 'NOT converged'}"
            f" in {r.relax_steps} steps"
            if r.relax_converged is not None
            else "no relax"
        )
        uncomputable = f", {r.uncomputable} uncomputable" if r.uncomputable else ""
        drift = (
            f", fragment RMSD {r.held_rmsd_A:g} Å"
            if r.held_rmsd_A is not None
            else f", molecule drift {r.mol_drift_A:g} Å"
            if r.mol_drift_A is not None
            else ""
        )
        lines.append(
            f"{rank}. {r.name} — unmet {r.unmet_hard} hard"
            f"{uncomputable}, {energy} ({relax}, cluster "
            f"{r.cluster_atoms}+{r.cap_atoms} atoms, {r.pinned_atoms} "
            f"pinned{drift})"
        )
        for s in r.scores:
            if s.met is False:
                got = f"{s.value:g}" if s.value is not None else "—"
                lines.append(
                    f"     unmet {s.name} ({s.strength}): {got} outside {s.band}"
                )
            elif s.met is None:
                lines.append(f"     uncomputable {s.name}: {s.reason}")
    if result.solved:
        lines.append(f"solved: {result.results[0].name} meets every hard band")
    elif result.results:
        lines.append("no candidate inside every band — failing regions named above")
    return "\n".join(lines)


def _request_realize_job(
    wall: str,
    pocket_name: str,
    slugs: list[str],
    cap: int | None,
    dft: dict[str, Any] | None,
) -> PocketRealizeRequest:
    """GFN2 is absent locally: build the pending-job request the handler
    mints post-commit as ``se_pocket_realize`` on a tblite host. The
    up-front refusal stays for the case where no host has the lane —
    ``target_node_for('xtb', …)`` empty means no compute node is
    configured (``PRECIS_MLIP_NODES`` / ``PRECIS_AUTOCATPATH_ROUTE_NODE``
    / ``PRECIS_DFT_NODE`` all unset), so the job could never be claimed."""
    import hashlib
    import json as _json

    from precis.workers.job_types.struct_relax import target_node_for

    key_src = _json.dumps(
        {
            "wall": wall,
            "pocket": pocket_name,
            "candidates": slugs,
            "cap": cap,
            "dft": dft,
        },
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(key_src.encode("utf-8")).hexdigest()[:16]
    target = target_node_for("xtb", key=digest)
    if not target:
        raise Unsupported(
            "pocket_realize needs GFN2 (tblite): no local backend, and no "
            "compute node is configured — PRECIS_MLIP_NODES, "
            "PRECIS_AUTOCATPATH_ROUTE_NODE and PRECIS_DFT_NODE are all "
            "unset, so an se_pocket_realize job would have nowhere to run",
            next="run on a host with tblite, or have deploy render a "
            "compute node for the xtb lane (precis_capabilities)",
        )
    return PocketRealizeRequest(
        wall=wall,
        pocket=pocket_name,
        candidates=list(slugs),
        cap=cap,
        dft=dft,
        target_node=target,
        digest=digest,
    )


def op_pocket_realize(
    store: Any, tree: Any, op: dict[str, Any]
) -> str | PocketRealizeRequest:
    """``pocket_realize`` (handler-level, like ``pocket_from_molecule``:
    it needs the store for candidate structures and GFN2 for the join).

    Args: ``wall`` (the block the pocket lives on), ``pocket`` (its
    name; optional when the wall has exactly one), ``candidates`` (a
    list of ``structure`` design slugs — each is posed by the wall
    block's transform), ``cap`` (max evaluations, default all),
    ``dft`` (optional mapping — the catpath ``dft`` config the result
    carries verbatim into provenance; open question 5's seam).

    Writes nothing: a research loop's answer is its ranking, echoed
    here (the spec's ranked candidate list with per-region mismatch,
    energies and provenance per pick). When the host has no GFN2
    backend (Q13 — the prod MCP image is linux-aarch64, which PyPI has
    no tblite wheel for), the op returns a
    :class:`PocketRealizeRequest` instead: the caller enqueues
    ``se_pocket_realize`` on the compute lane after its tree commits,
    and the job's ``job_summary`` chunk carries this same report. The
    up-front ``Unsupported`` refusal stays for the case where no host
    has the lane at all."""
    opname = "pocket_realize"
    allowed = {"op", "wall", "pocket", "candidates", "cap", "dft"}
    strays = sorted(set(op) - allowed)
    if strays:
        raise BadInput(
            f"{opname}: unknown key(s) {', '.join(strays)} — args are "
            "wall, pocket, candidates, cap, dft",
            next="drop the unknown keys",
        )
    wall = op.get("wall")
    if not isinstance(wall, str) or not wall.strip():
        raise BadInput(
            f"{opname} needs 'wall' (the block the pocket lives on)",
            next="pass 'wall': the name of the block the pocket lives on",
        )
    wall = wall.strip()
    wall_node = tree.blocks.get(wall)
    if wall_node is None:
        raise BadInput(
            f"{opname}: no block {wall!r}",
            options=sorted(tree.blocks),
        )
    pockets = wall_node.pockets
    pocket_name = op.get("pocket")
    if pocket_name is None:
        if len(pockets) != 1:
            raise BadInput(
                f"{opname}: {wall!r} has {len(pockets)} pockets — name "
                "'pocket' explicitly",
                options=sorted(pockets),
            )
        pocket_name = next(iter(pockets))
    if not isinstance(pocket_name, str) or pocket_name not in pockets:
        raise BadInput(
            f"{opname}: no pocket {pocket_name!r} on {wall!r}",
            options=sorted(pockets),
        )
    pocket = pockets[pocket_name]
    raw = op.get("candidates")
    if (
        not isinstance(raw, list | tuple)
        or not raw
        or not all(isinstance(c, str) and c.strip() for c in raw)
    ):
        raise BadInput(
            f"{opname} 'candidates' must be a non-empty list of structure design slugs",
            next="pass 'candidates': ['<structure slug>', …]",
        )
    cap = op.get("cap")
    if cap is not None and (
        isinstance(cap, bool) or not isinstance(cap, int) or cap < 1
    ):
        raise BadInput(
            f"{opname} 'cap' must be a positive integer, got {cap!r}",
            next="pass a positive integer 'cap', or omit it to evaluate all",
        )
    dft = op.get("dft")
    if dft is not None and not isinstance(dft, dict):
        raise BadInput(
            f"{opname} 'dft' must be a mapping (catpath's dft config "
            "block — carried verbatim into provenance), "
            f"got {dft!r}",
            next="pass autocatpath's 'dft' config mapping, or omit it",
        )
    candidates: list[WallCandidate] = []
    for slug in raw:
        got = _candidate_from_structure(store, slug.strip(), wall_node)
        if isinstance(got, str):
            raise BadInput(
                f"{opname}: candidate {slug!r}: {got}",
                next="name an existing structure design in 'candidates'",
            )
        candidates.append(got)
    try:
        _require_gfn2()
    except Unsupported:
        # No tblite here (the prod MCP image, Q13): hand the relaxes to
        # the compute lane. The probe runs AFTER the arg vetting above so
        # a malformed call still refuses by name rather than as a lane
        # decision; _request_realize_job keeps the up-front refusal when
        # no host has the lane either.
        return _request_realize_job(wall, pocket_name, list(raw), cap, dft)
    result = realize_pocket(tree, wall, pocket, candidates, cap=cap, dft=dft)
    return _report(result)


__all__ = [
    "BINDING_ENERGY_SLUG",
    "CLUSTER_MARGIN_A",
    "FREE_MARGIN_A",
    "MAX_COMPLEX_ATOMS",
    "RELAX_STEPS",
    "CandidateResult",
    "MeasureScore",
    "PocketRealizeRequest",
    "RealizeResult",
    "WallCandidate",
    "evaluate_candidate",
    "op_pocket_realize",
    "realize_pocket",
]

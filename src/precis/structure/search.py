"""Global structure search — a GOFEE/AGOX surrogate search over a confined box.

Slice 1 of `docs/backlog/global-structure-search-gofee-agox.md`: today a
quest's candidate `structure`s come one at a time from an LLM proposal, named
by a human-shaped guess (a dopant here, an adatom there). This module is the
alternative proposal source: point AGOX (Christiansen, Rønne & Hammer 2022,
JCP 157 054701) at a confined box on a periodic slab seed with a fixed
stoichiometry to place, and let its surrogate-guided search (GOFEE — Bisbo &
Hammer 2020 PRL 124 086102, 2022 PRB 105 245404 — or its cheaper
`basin_hopping`/`random` baselines) explore reconstructions nobody thought to
propose. The oracle is always the ``ml`` rung's own MACE calculator
(:func:`precis.structure.relax._ml_calculator`, injected here as
``calculator_factory`` so this module never imports ``relax.py`` and stays
free of the MLIP-instantiation policy) — there is no DFT oracle in this
slice (`gpaw` is container-only; a GOFEE oracle call is a single-point
evaluation, not a `gpaw-relax` job).

**Pure module** (no DB, no job context): the job_type
(`workers/job_types/struct_search.py`) is the seam that loads a seed
`structure`, builds a :class:`SearchSpec`, calls :func:`run_search`, and
writes the returned :class:`SearchCandidate`\\ s back as `structure` rows.

**AGOX 3.11's config API, not the hand-wired classic script.** AGOX 3.11
exposes each algorithm (`GOFEE`, `BasinHopping`, `RandomStructureSearch`) as
an ``Algorithm`` subclass driven by three Pydantic configs
(`ProblemConfig`/`RunConfig`/the algorithm's own `*Config`) —
``AlgoClass.create(problem=..., run=...)`` returns an instance whose
``_setup_modules()`` builds AGOX's observer graph (generator/collector,
surrogate model, acquisitor, relaxer, oracle evaluator, database) from that
config, the same graph the classic hand-composed script wires by hand. Using
the config API means the surrogate/acquisition wiring (GPR model, LCB
acquisition, sparsification, ...) is entirely AGOX's, matching the backlog's
"AGOX owns the surrogate" decision — this module never touches a kernel or a
fingerprint.

``ProblemConfig.fix_template=True`` (the default we keep) freezes the WHOLE
template — the entire seed slab, `fixed` mask or not — for the duration of
the search; only the placed ``add`` atoms move. Surface relaxation is the
per-candidate `struct_relax` step that runs on a returned candidate
afterward, not this search. The seed's own `Atom.fixed` bitmask still
round-trips through :func:`scene_from_ase`/:func:`atoms_from_scene` (general
Scene<->Atoms plumbing, used by the template build here), it's just not
independently meaningful while the whole template is frozen.

**Budget = oracle single-point evaluations = AGOX iterations.** Every
algorithm here defaults its evaluator to ``number_to_evaluate=1`` (AGOX
`LocalOptConfig`/`SinglePointEvaluatorConfig`'s own default) — one oracle
call per `AGOX.run(N_iterations=...)` iteration — so ``spec.budget`` maps
directly onto ``N_iterations``. Documented here because it is a mapping, not
an AGOX-visible parameter this module sets.

**Wall-clock, not a raise.** :func:`run_search` never raises on a timeout:
a tiny :class:`~agox.observer.Observer` (built lazily inside `run_search`,
since ``agox`` is optional) checks ``time.monotonic()`` against the deadline
once per iteration (``order=0``, so it runs before that iteration's
generate/evaluate/store) and flips AGOX's own convergence flag when the
deadline has passed — the run then stops after finishing its current
iteration and this function returns whatever the database already holds,
per the backlog's "return what it holds so far" contract.

**Cwd side effect.** AGOX's own `run_directory` context manager `chdir`s the
process into ``workdir`` for the duration of the run (so its ray/db files
land there) and restores the prior cwd afterward — a known AGOX quirk, not
this module's choice; a caller running two searches concurrently *in the
same process* would race it, which is why `struct_search` (the job_type
seam) runs at most one search per claimed job.

Unit enclave (package docstring): Å/eV-native, following ASE/AGOX; energies
in eV, coordinates in Å, never converted to SI here.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import geom_hash_c, normalize_scene
from .cell import Cell
from .scene import FIX_ALL, Atom, Scene

#: Algorithms this slice exposes (backlog item 1). ``gofee`` is the default —
#: the surrogate-guided search; ``basin_hopping``/``random`` are AGOX's own
#: cheaper baselines, useful for a quick sanity pass on a new box/composition
#: before spending a GOFEE budget on it.
_ALGOS = frozenset({"gofee", "basin_hopping", "random"})

#: AGOX's own registered ``KIND``/class per our ``algo`` name
#: (`agox.algorithms.gofee.GOFEE.KIND == "GOFEE"`,
#: `agox.algorithms.basin_hopping.BasinHopping.KIND == "basin_hopping"`,
#: `agox.algorithms.rss.RandomStructureSearch.KIND == "random_structure_search"`)
#: — resolved lazily (inside :func:`run_search`) so importing this module
#: never requires ``agox`` to be installed.
_ALGO_CLASS_NAMES: dict[str, str] = {
    "gofee": "GOFEE",
    "basin_hopping": "BasinHopping",
    "random": "RandomStructureSearch",
}

#: Hard cap on ``budget`` (oracle calls) — a runaway job param should not be
#: able to ask for an unbounded number of MACE single-points.
_BUDGET_MAX = 1000

#: Ray worker count for AGOX's parallel collector/relaxer (GOFEE's default
#: `ParallelCollectorConfig` is ray-backed even for a single search) —
#: deliberately small rather than "all cores": the GPU node runs other work.
#: ``PRECIS_SEARCH_CPUS``.
_SEARCH_CPUS_DEFAULT = 4


def _search_cpu_count() -> int:
    raw = os.environ.get("PRECIS_SEARCH_CPUS")
    if not raw:
        return _SEARCH_CPUS_DEFAULT
    try:
        return max(1, int(raw))
    except ValueError:
        return _SEARCH_CPUS_DEFAULT


def scene_from_ase(atoms: Any) -> Scene:
    """ASE ``Atoms`` -> :class:`Scene`. Order-preserving (atoms keep their ASE
    order), ``fixed`` carried from any ``FixAtoms`` constraint, bond-free.

    Duplicated from :func:`precis_pathway.ingest.scene_from_ase` rather than
    imported: `precis_pathway` is the optional `[catalyst]` package and this
    module (a core `precis-mcp` capability, gated only behind
    `[struct-search]`) must not pull it in. Keep the two in sync by hand if
    either changes — they encode the same ASE<->Scene contract.
    """
    lattice = np.asarray(atoms.cell, dtype=float)
    pbc_bits = [bool(x) for x in atoms.pbc]
    pbc: tuple[bool, bool, bool] = (pbc_bits[0], pbc_bits[1], pbc_bits[2])
    scaled = atoms.get_scaled_positions(wrap=False)

    fixed_idx: set[int] = set()
    for con in getattr(atoms, "constraints", None) or []:
        if type(con).__name__ == "FixAtoms":
            try:
                fixed_idx.update(int(i) for i in con.get_indices())
            except Exception:  # pragma: no cover - ASE version variance
                fixed_idx.update(int(i) for i in getattr(con, "index", []))

    scene_atoms: dict[str, Atom] = {}
    label_hi: dict[str, int] = {}
    for i, sym in enumerate(atoms.get_chemical_symbols()):
        label_hi[sym] = label_hi.get(sym, 0) + 1
        label = f"a{sym}{label_hi[sym]}"
        scene_atoms[label] = Atom(
            label=label,
            element=sym,
            frac=np.asarray(scaled[i], dtype=float),
            fixed=FIX_ALL if i in fixed_idx else 0,
        )
    return Scene(
        cell=Cell(lattice=lattice, pbc=pbc), atoms=scene_atoms, label_hi=label_hi
    )


def atoms_from_scene(scene: Scene) -> Any:
    """:class:`Scene` -> ASE ``Atoms``, the inverse of :func:`scene_from_ase`.

    Unlike :func:`precis.structure.export._to_ase` (which this delegates to
    for the cell/symbols/positions), a whole atom with any ``fixed`` axis set
    round-trips as a ``FixAtoms`` constraint — the shape AGOX (and
    :func:`precis.structure.relax._relax_emt`/``_relax_ml``'s own
    ``FixCartesian`` use, though whole-atom here rather than per-axis, since
    ``FixAtoms`` has no per-axis freedom and AGOX's template is either frozen
    entirely (``fix_template=True``, this slice's setting) or not at all)."""
    from ase.constraints import FixAtoms

    from . import export

    atoms = export._to_ase(scene)
    fixed_idx = [i for i, atom in enumerate(scene.atoms.values()) if atom.fixed]
    if fixed_idx:
        atoms.set_constraint(FixAtoms(indices=fixed_idx))
    return atoms


@dataclass(frozen=True)
class SearchSpec:
    """One AGOX search request: a seed slab, a confinement box on it, a fixed
    stoichiometry to place inside the box, and a budget of oracle calls.

    ``box`` is fractional bounds in the seed's own cell,
    ``((fx0, fx1), (fy0, fy1), (fz0, fz1))`` — converted to a Cartesian
    confinement cell/corner in :func:`run_search`. ``add`` is a fixed
    stoichiometry (``{"Pd": 2, "N": 1, "O": 1}``) — variable composition
    (``add`` as ranges) is slice 2, out of scope here (see the backlog's
    "Explicitly NOT in scope").

    The seed must be periodic in both in-plane axes (``cell.pbc[0]`` and
    ``[1]``) — a free (non-periodic) cluster is out of scope for this slice:
    :func:`precis.structure.canonical.inplane_symmetry_ops` (which
    :func:`~precis.structure.canonical.normalize_scene`/``geom_hash_c`` rely
    on for translation/rotation/mirror dedup) is identity-only off a
    periodic-in-plane cell, so a free-cluster twin would never dedup in
    :func:`top_k_distinct`.
    """

    seed: Scene
    box: tuple[tuple[float, float], tuple[float, float], tuple[float, float]]
    add: dict[str, int]
    model: str
    algo: str = "gofee"
    budget: int = 200
    timeout_s: float = 7200.0

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.add:
            raise ValueError("add: a search needs a non-empty stoichiometry to place")
        for el, n in self.add.items():
            if int(n) <= 0:
                raise ValueError(f"add: count for {el!r} must be positive, got {n!r}")
        if not (1 <= int(self.budget) <= _BUDGET_MAX):
            raise ValueError(
                f"budget: must be between 1 and {_BUDGET_MAX} (oracle calls), "
                f"got {self.budget!r}"
            )
        if len(self.box) != 3:
            raise ValueError(
                "box: needs exactly 3 axis bounds ([fx0,fx1],[fy0,fy1],[fz0,fz1])"
            )
        for axis, bounds in zip("xyz", self.box, strict=True):
            if len(bounds) != 2:
                raise ValueError(f"box: axis {axis!r} needs [lo, hi], got {bounds!r}")
            lo, hi = float(bounds[0]), float(bounds[1])
            if not (0.0 <= lo < hi <= 1.0):
                raise ValueError(
                    f"box: axis {axis!r} bounds must satisfy 0 <= lo < hi <= 1, "
                    f"got [{lo}, {hi}]"
                )
        if self.algo not in _ALGOS:
            raise ValueError(
                f"algo: must be one of {sorted(_ALGOS)}, got {self.algo!r}"
            )
        if not (self.seed.cell.pbc[0] and self.seed.cell.pbc[1]):
            raise ValueError(
                "seed: must be periodic in both in-plane axes (a/b) — free "
                "(non-periodic) clusters are out of scope for this search"
            )


@dataclass(frozen=True)
class SearchCandidate:
    """One structure AGOX's database evaluated with the oracle."""

    scene: Scene
    oracle_energy_eV: float
    surrogate_energy_eV: float | None
    iteration: int


@dataclass(frozen=True)
class SearchResult:
    """A finished (or wall-clock-truncated) search's population + summary."""

    candidates: list[SearchCandidate] = field(default_factory=list)
    budget_used: int = 0
    wall_s: float = 0.0
    #: AGOX's own sqlite database file, under ``workdir`` — a later item can
    #: warm-start a surrogate from it (slice 2, `blocked-by` this one).
    database_path: str | None = None
    best_energy_eV: float | None = None


def _agox_compat_shims() -> None:
    """Patch the two places AGOX 3.11.1 breaks on the fleet's pinned ase/numpy.

    Runs in the driver before ``import agox`` and, via ray's
    ``worker_process_setup_hook``, in every AGOX worker process (see
    :func:`_start_ray_with_shim`). Idempotent. The fleet's constraints file
    pins ase and numpy to uv.lock, so version bounds on the extra would not
    resolve — putting the old behaviour back is the fix that installs.

    1. ase 3.29 split ``ase/constraints.py`` into a package and dropped
       ``IndexedConstraint`` / ``slice2enlist`` from the public namespace
       (they live in ``ase.constraints.constraint``); AGOX does ``from
       ase.constraints import IndexedConstraint``. Re-export them.
    2. numpy 2.5 made ``float()`` of a shape-(1,) array a ``TypeError``;
       ``GPR._log_marginal_likelihood_gradient`` returns the log marginal
       likelihood as the ``(k,)`` einsum result and both hyperparameter
       optimisers ``float()`` it (the sibling ``_log_marginal_likelihood``
       already ``np.sum``s). Wrap it to return the scalar.
    """
    import ase.constraints as ase_constraints

    try:
        from ase.constraints import constraint as ase_constraint_mod
    except ImportError:  # pre-3.29 ase: flat module, names already public
        pass
    else:
        for name in ("IndexedConstraint", "slice2enlist"):
            if not hasattr(ase_constraints, name) and hasattr(ase_constraint_mod, name):
                setattr(ase_constraints, name, getattr(ase_constraint_mod, name))

    try:
        from agox.models.GPR.GPR import GPR
    except ImportError:  # no agox here — run_search raises SearchUnsupported
        return
    original = GPR._log_marginal_likelihood_gradient
    if getattr(original, "__name__", "") == "_scalar_lml_gradient":
        return  # already wrapped (driver and workers both call this)

    def _scalar_lml_gradient(self: Any, theta: Any) -> tuple[float, Any]:
        log_p, grad = original(self, theta)
        return float(np.sum(log_p)), grad

    GPR._log_marginal_likelihood_gradient = _scalar_lml_gradient


def _start_ray_with_shim(*, cpu_count: int, tmp_dir: Path) -> None:
    """Start AGOX's local ray cluster ourselves so every worker process runs
    :func:`_agox_compat_shims` before it imports agox.

    AGOX's ``ray_startup`` returns early when ray is already initialised, and
    its actors import ``agox.environments`` in fresh worker processes where
    an in-process shim is invisible — the first cluster run died with the
    same ``IndexedConstraint`` ImportError inside ``ray::Actor.add_module``
    after the driver had imported fine. ``worker_process_setup_hook`` is the
    job-level ray knob for exactly that; AGOX's per-actor ``runtime_env``
    only sets ``env_vars``, which merge rather than replace it.
    """
    import ray

    if ray.is_initialized():
        return
    tmp_dir.mkdir(parents=True, exist_ok=True)
    ray.init(
        address="local",
        num_cpus=cpu_count,
        ignore_reinit_error=True,
        include_dashboard=False,
        _temp_dir=str(tmp_dir.resolve()),
        runtime_env={"worker_process_setup_hook": _agox_compat_shims},
    )


class SearchUnsupported(RuntimeError):
    """AGOX (the ``[struct-search]`` extra) is not importable on this host."""


def run_search(
    spec: SearchSpec,
    *,
    calculator_factory: Callable[[], Any],
    workdir: Path,
) -> SearchResult:
    """Run ``spec`` with AGOX, using ``calculator_factory()`` as the oracle.

    ``calculator_factory`` is a zero-arg callable returning a fresh ASE
    calculator — the caller passes
    ``lambda: precis.structure.relax._ml_calculator(model)`` so this module
    never imports ``relax.py`` (keeping the MLIP-instantiation policy in one
    place) and a fresh calculator is minted per call (some ASE calculators
    are not safe to reuse across processes/threads).

    Raises :class:`SearchUnsupported` when ``agox`` is not importable.
    Never raises on a wall-clock overrun (see the module docstring) — it
    returns whatever AGOX's database holds when the deadline observer stops
    the run.
    """
    _agox_compat_shims()
    try:
        import agox  # noqa: F401
    except ImportError as exc:
        raise SearchUnsupported(
            "structure search needs AGOX — install precis-mcp[struct-search]"
        ) from exc

    import agox.algorithms as agox_algorithms
    from agox import AGOX, Observer
    from agox.algorithms.algorithm import run_directory
    from agox.algorithms.config import ProblemConfig, RunConfig

    algo_classes: dict[str, Any] = {
        algo: getattr(agox_algorithms, cls_name)
        for algo, cls_name in _ALGO_CLASS_NAMES.items()
    }

    class _DeadlineObserver(Observer):
        """Flips AGOX's convergence flag once ``deadline`` has passed.

        ``order=0`` — runs before that iteration's generate/relax/evaluate/
        store, so the LOOP exits after finishing the iteration already in
        flight, never mid-iteration (see the module docstring)."""

        name = "PrecisSearchDeadline"

        def __init__(self, deadline: float) -> None:
            super().__init__(order=0)
            self._deadline = deadline
            self.add_observer_method(
                self._check, sets={}, gets={}, order=0, handler_identifier="AGOX"
            )

        def _check(self, state: Any) -> None:
            if time.monotonic() > self._deadline:
                state.set_convergence_status(True)

    workdir.mkdir(parents=True, exist_ok=True)

    template = atoms_from_scene(spec.seed)
    lattice = np.asarray(spec.seed.cell.lattice, dtype=float)
    (fx0, fx1), (fy0, fy1), (fz0, fz1) = spec.box
    corner = spec.seed.cell.frac_to_cart(np.array([fx0, fy0, fz0]))
    span = np.array([fx1 - fx0, fy1 - fy0, fz1 - fz0])
    confinement_cell = span[:, np.newaxis] * lattice
    symbols = "".join(f"{el}{n}" for el, n in spec.add.items())

    problem_cfg = ProblemConfig(
        template=template,
        symbols=symbols,
        confinement_cell=confinement_cell,
        confinement_corner=corner,
        calculator=calculator_factory(),
        fix_template=True,
    )
    run_cfg = RunConfig(
        path=str(workdir),
        cpu_count=_search_cpu_count(),
        ray_tmp_dir=str(workdir / "ray"),
    )

    # Before create(): the parallel algorithms call ray_startup() inside it.
    _start_ray_with_shim(cpu_count=run_cfg.cpu_count, tmp_dir=workdir / "ray")
    search = algo_classes[spec.algo].create(problem=problem_cfg, run=run_cfg)

    deadline = time.monotonic() + float(spec.timeout_s)
    deadline_observer = _DeadlineObserver(deadline)

    started = time.monotonic()
    with run_directory(search.get_path()):
        modules = search._setup_modules()
        agox_instance = AGOX(*modules, deadline_observer, seed=search.get_seed())
        agox_instance.run(N_iterations=int(spec.budget))
    wall_s = time.monotonic() - started

    db = search.get_database()
    rows = db.get_all_structures_data()
    candidates = [
        SearchCandidate(
            scene=scene_from_ase(db.db_to_atoms(row)),
            oracle_energy_eV=float(row["energy"]),
            # AGOX's stored structures carry only the oracle energy; a
            # surrogate (model-predicted) energy isn't in the database row,
            # so this stays None rather than fabricated. Slice 2 (surrogate
            # persistence) may add it.
            surrogate_energy_eV=None,
            iteration=int(row.get("iteration") or 0),
        )
        for row in rows
    ]
    candidates.sort(key=lambda c: c.oracle_energy_eV)

    db_filename = str(db.filename)
    db_path = (
        db_filename if Path(db_filename).is_absolute() else str(workdir / db_filename)
    )

    return SearchResult(
        candidates=candidates,
        budget_used=len(candidates),
        wall_s=wall_s,
        database_path=db_path,
        best_energy_eV=candidates[0].oracle_energy_eV if candidates else None,
    )


def top_k_distinct(
    candidates: Sequence[SearchCandidate], k: int = 10
) -> list[SearchCandidate]:
    """The ``k`` lowest-``oracle_energy_eV`` candidates, symmetry-deduped.

    Each candidate's scene is normalised in place
    (:func:`~precis.structure.canonical.normalize_scene`) and then grouped by
    :func:`~precis.structure.canonical.geom_hash_c` — the SAME
    periodic-symmetry hash the quest uses to collapse translation/rotation/
    mirror twins (`qu164903`'s "corner" vs "central" saga) — keeping only the
    lowest-energy representative per hash. Pure: no AGOX import, so it runs
    fine wherever ``agox`` isn't installed (including under `scripts/test`)."""
    best_by_hash: dict[str, SearchCandidate] = {}
    for cand in candidates:
        normalize_scene(cand.scene)
        h = geom_hash_c(cand.scene)
        current = best_by_hash.get(h)
        if current is None or cand.oracle_energy_eV < current.oracle_energy_eV:
            best_by_hash[h] = cand
    ordered = sorted(best_by_hash.values(), key=lambda c: c.oracle_energy_eV)
    return ordered[:k]


__all__ = [
    "SearchCandidate",
    "SearchResult",
    "SearchSpec",
    "SearchUnsupported",
    "atoms_from_scene",
    "run_search",
    "scene_from_ase",
    "top_k_distinct",
]

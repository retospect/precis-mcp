"""``surface_coverage_scan`` job_type — compute one model's coverage anchors
on a slab and fold them into the inner CHE sweep (surface-Pourbaix slice 1,
``docs/backlog/surface-pourbaix-staircase-optimizer.md``).

One job = one ``(slab config, model)`` scan: catpath's ab-initio
thermodynamics coverage scan (``autocatpath.coverage.scan``) relaxes the
clean slab and each requested adsorbate at each requested coverage, prices
every termination against the engine's reservoirs, and returns
``gamma(theta)`` per adsorbate — the **anchors** the outer loop pays for
once per slab. The job then:

1. stamps the anchor footing onto its meta — ``anchor_key`` (slab +
   adsorbates + coverages + conditions + engine version; the model is NOT
   in the key so several models pool under one), ``model``,
   ``engine_version``, ``corrections`` (the correction set the engine ran
   with, ``None`` on an engine that records none) — so every anchor says
   what it was measured on (slice-1 acceptance: one footing);
2. pools this scan with every earlier succeeded scan on the same
   ``anchor_key`` AND the same correction footing
   (:func:`precis.quest.pourbaix_map.correction_footing` — the key the
   map partitions on; other footings are named in the sweep's
   ``not_pooled`` and never blended, gr489162; the sweep stamps its
   ``footing_key``) — one per model, newest wins — drops any model whose
   recorded correction set is a strict subset of the pool's
   (:func:`precis_pathway.surface_pourbaix.drop_uncorrected` — a band
   across mixed corrected/uncorrected anchors prices the correction, not
   the model form; gr486949), and runs
   :func:`precis_pathway.surface_pourbaix.sweep` over what is left — the
   resting envelope along U, every boundary with its propagated band and,
   with two or more models, its model-form band — into ``meta.che_sweep``
   and a ``job_summary`` chunk. Dropped models are named in the sweep's
   ``dropped_uncorrected`` and in the scan warnings.

Scope (Reto's 2026-10-07 rulings): (111) only — ``coverage.facets`` must
be empty and ``slab.miller`` unset or (1,1,1). ``slab_extxyz`` injects an
externally-prepared slab (the quest's exported candidate geometry — a
doped slab or a β-PdH twin): honoured since autocatpath 0.25.0, which
scans the injected geometry and records ``slab_source``/``lattice_source``
in ``coverage.json``. The anchor key folds a digest of the slab bytes so
two candidates sharing the ``config.slab`` label never pool anchors
measured on different slabs (:func:`runner.coverage_anchor_key`'s
``slab_key``).

``structure_ref`` is the same thing by address: a precis ``structure``
ref id the server resolves to the exported slab itself —
``export.to_extxyz(scene, constraints=True)``, the exact export the
quest-minted scans inline — so a per-candidate scan is one short put
instead of a ~100-atom paste (:func:`structure_slab_extxyz`). It refuses
structures the scan can't honestly anchor on: a cell that is not a (111)
surface cut, or a structure with no fully-frozen atoms (the frozen bottom
layers ride the export as a ``FixAtoms``; without them the scan would
relax what the candidate pins). A put names the slab one way — either
``slab_extxyz`` or ``structure_ref``; both together is refused, since
the server cannot tell which geometry the caller meant to scan. The
resolved row then carries both (``structure_ref`` as provenance).

Registered via the ``precis.job_types`` entry point
(``surface_coverage_scan = precis_pathway.coverage_job:SPEC``). Same
executor seam as ``autocatpath_seed``: ``ssh_node`` with a ``target_node``
pin to the box that has the engine + backend, the compute in a fresh,
killable child (:func:`precis_pathway.runner.run_coverage_scan_subprocess`)
under ``resources.wall_seconds``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from typing import Any

import numpy as np

from precis.quest.pourbaix_map import U_RHE_WINDOW
from precis.workers.job_types import JobTypeSpec

from .coverage_sites import representable_coverages, slab_top_sites

log = logging.getLogger(__name__)

NAME = "surface_coverage_scan"

#: Slice-1 default anchor set: the terminations the resting-state map needs
#: (transfer prompt §3: "bare, H, O, OH, oxide, plus every intermediate";
#: the oxide is a bulk phase, Part A's business; intermediates come from
#: the pathway run itself).
DEFAULT_ADSORBATES: tuple[str, ...] = ("H", "O", "OH")
DEFAULT_COVERAGES: tuple[float, ...] = (0.25, 0.5, 0.75, 1.0)
#: Co-adsorbed feed + H* phases (gr489475: the NORR selectivity switch is
#: NO*/H* co-coverage) — requested only when the reaction has a feed
#: reservoir and the engine takes ``coverage.mixtures``. Splits a cell
#: cannot represent are dropped by the engine with a warning (3:1 at
#: 0.5 ML needs n_top >= 8).
DEFAULT_MIXTURE_RATIOS: tuple[tuple[int, int], ...] = ((3, 1), (1, 1), (1, 3))
DEFAULT_MIXTURE_COVERAGES: tuple[float, ...] = (0.5, 0.75)
DEFAULT_U_WINDOW: tuple[float, float] = U_RHE_WINDOW
DEFAULT_GRID = 151
DEFAULT_PH = 7.0

_RANGE = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The catpath config (same shape the pathway lane runs — slab, mlip,
        # conditions, thermo). Its ``coverage`` block is filled with the
        # slice-1 defaults when absent.
        "config": {"type": "object"},
        # Which cfg.mlip.specs() entry this scan runs.
        "model_index": {"type": "integer", "minimum": 0},
        # Backend override; null -> the config's own mlip.backend.
        "force_backend": {"type": ["string", "null"]},
        # Content address of the anchor set (runner.coverage_anchor_key);
        # recomputed at dispatch when absent.
        "anchor_key": {"type": ["string", "null"]},
        # The node this job pins itself to (claim gate -> runs here).
        "target_node": {"type": ["string", "null"]},
        # Sweep framing: the operating point (V vs RHE) the summary names,
        # the U window, the grid, and the pH that labels the SHE view.
        "point_U_RHE": {"type": ["number", "null"]},
        "window_U_RHE": _RANGE,
        "grid": {"type": "integer", "minimum": 2, "maximum": 2001},
        "pH": {"type": "number"},
        # Per-anchor energy bar (eV) for the propagated band; default =
        # config.search.energy_thresh, else 0.05.
        "sigma_e_eV": {"type": "number", "minimum": 0},
        # An externally-prepared slab (extxyz; the precis `structure`
        # seam). null → the engine builds the clean config.slab itself —
        # or the structure_ref below resolves one server-side.
        "slab_extxyz": {"type": ["string", "null"]},
        # The precis structure ref the slab comes from: resolved +
        # exported (with its FixAtoms) at put time. Mutually exclusive
        # with slab_extxyz on a put.
        "structure_ref": {"type": ["integer", "null"]},
    },
    "required": ["config"],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"ssh_node"})
REQUIRES: frozenset[str] = frozenset()
DESCRIPTION = (
    "Compute one MLIP model's adsorbate coverage anchors on a slab "
    "(autocatpath coverage scan) and fold them into the inner CHE sweep "
    "(surface-Pourbaix slice 1)."
)


def with_defaults(config: dict[str, Any], *, mixtures: bool = False) -> dict[str, Any]:
    """``config`` with the slice-1 ``coverage`` defaults filled in (a copy;
    caller-set keys win).

    ``DEFAULT_COVERAGES`` are not representable on every slab — 0.25 ML is
    no integer count on a 9-site cell — so the defaulted list is snapped
    to the representable ``n / n_top`` fractions of *this* config's slab
    (the same normalisation :func:`runner.coverage_anchor_key` hashes, so
    the dispatched config and its key agree). Caller-set ``coverages``
    pass through verbatim: for an explicit ask the engine's own "not
    representable" warning is the record, and the key normalisation pools
    it with the snapped form anyway.

    ``mixtures`` says whether the engine takes ``coverage.mixtures``
    (:func:`runner.coverage_mixtures_supported`). When it does and the
    caller set none, a reaction with a feed reservoir (an NO substrate)
    gets the feed + H* grid (``DEFAULT_MIXTURE_*``). When it does not, the
    key is removed — caller-set or not — since an older engine refuses
    the whole config over it; :func:`_dispatch` names the drop."""
    cfg = dict(config)
    cov = dict(cfg.get("coverage") or {})
    cov.setdefault("adsorbates", list(DEFAULT_ADSORBATES))
    if "coverages" not in cov:
        cov["coverages"] = representable_coverages(
            list(DEFAULT_COVERAGES), slab_top_sites(cfg)
        )
    cov.setdefault("facets", [])
    if not mixtures:
        cov.pop("mixtures", None)
    elif "mixtures" not in cov:
        from .surface_pourbaix import feed_reservoir

        feed = feed_reservoir(cfg.get("substrate"))
        if feed is not None:
            cov["mixtures"] = [
                {
                    "species": [feed.species, "H"],
                    "ratios": [list(r) for r in DEFAULT_MIXTURE_RATIOS],
                    "coverages": list(DEFAULT_MIXTURE_COVERAGES),
                }
            ]
    cfg["coverage"] = cov
    return cfg


def check_params(params: dict[str, Any]) -> str | None:
    """The reason a params dict is unusable, or ``None``. Scope rules of the
    module docstring: (111) only, no extra facets. ``slab_extxyz`` is
    accepted (engine >= 0.25.0 scans the injected slab)."""
    config = params.get("config")
    if not isinstance(config, dict) or not config:
        return "params.config must be a non-empty catpath config object"
    cov = config.get("coverage") or {}
    if cov.get("facets"):
        return "coverage.facets must be empty: slice 1 scans the (111) facet only"
    miller = (config.get("slab") or {}).get("miller")
    if miller is not None and [int(i) for i in miller] != [1, 1, 1]:
        return f"slab.miller={miller!r} is out of scope: slice 1 scans (111) only"
    w = params.get("window_U_RHE")
    if w is not None and not float(w[0]) < float(w[1]):
        return "window_U_RHE must be [lo, hi] with lo < hi"
    return None


#: (111)-cell signature tolerances. The candidates' cells are computed
#: (exact to print precision), so the slack only needs to absorb
#: serialisation noise and a relaxed lattice's last decimal — not real
#: distortion.
_LEN_TOL = 0.05  # relative slack on in-plane vector lengths
_ANG_TOL = 0.05  # |cos| slack on the in-plane angle (~3°)
_TILT_TOL = 0.06  # |cos| slack on c vs the surface normal (~3.4° tilt)
_MIN_VACUUM_A = 4.0  # Å of empty cell along c that makes a cut a slab


def _slab_111_problem(scene: Any) -> str | None:
    """The reason ``scene`` is not a (111) slab this scan can anchor on, or
    ``None``. The structure store records no facet label, so the check is
    geometric — the fcc(111) signature: in-plane vectors equal at 60°/120°
    (the hexagonal cell) or in the orthogonal √3 ratio, the third vector
    along the surface normal, and a vacuum gap along it. A bulk or
    non-(111) cut fails by shape rather than by name."""
    if not scene.atoms:
        return "has no atoms"
    lat = np.asarray(scene.cell.lattice, dtype=float)
    if lat.shape != (3, 3):
        return f"has a malformed cell (shape {lat.shape}, expected 3x3)"
    a, b, c = lat[0], lat[1], lat[2]
    na, nb, nc = (float(np.linalg.norm(v)) for v in (a, b, c))
    if min(na, nb, nc) <= 0.0:
        return "has a degenerate cell (a zero-length lattice vector)"
    if (
        abs(float(np.dot(c, a))) > _TILT_TOL * nc * na
        or abs(float(np.dot(c, b))) > _TILT_TOL * nc * nb
    ):
        return (
            "is not a surface cut: the third cell vector is not the "
            "surface normal (slice 1 scans (111) slabs only)"
        )
    cos_ab = float(np.dot(a, b)) / (na * nb)
    hexagonal = (
        abs(na - nb) <= _LEN_TOL * max(na, nb)
        and min(abs(cos_ab - 0.5), abs(cos_ab + 0.5)) <= _ANG_TOL
    )
    ratio = max(na, nb) / min(na, nb)
    orthogonal = abs(cos_ab) <= _ANG_TOL and abs(ratio - math.sqrt(3.0)) <= (
        _LEN_TOL * math.sqrt(3.0)
    )
    if not (hexagonal or orthogonal):
        return (
            "is not a (111) surface cell: the in-plane vectors are neither "
            "the hexagonal 60°/120° pair nor the orthogonal √3 cell a "
            "(111) cut produces"
        )
    zs = sorted(float(at.frac[2]) % 1.0 for at in scene.atoms.values())
    gaps = [zs[i + 1] - zs[i] for i in range(len(zs) - 1)] + [zs[0] + 1.0 - zs[-1]]
    if max(gaps) * nc < _MIN_VACUUM_A:
        return (
            f"has no vacuum gap along the surface normal "
            f"(< {_MIN_VACUUM_A:g} Å of empty cell) — a bulk cell, not a slab"
        )
    return None


def structure_slab_extxyz(
    store: Any, structure_ref: Any
) -> tuple[str | None, str | None]:
    """``(slab_extxyz, None)`` or ``(None, reason)`` — resolve
    ``params.structure_ref`` to the candidate's exported slab the way the
    quest-minted scans do (``export.to_extxyz(scene, constraints=True)``,
    the frozen bottom layers riding as a ``FixAtoms``).

    Refusals name the problem: not a structure ref id, no such live
    structure, a cell that is not a (111) surface cut, or no fully-frozen
    atoms — a scan without the ``FixAtoms`` would relax the layers the
    candidate's geometry pins."""
    try:
        sid = int(structure_ref)
    except (TypeError, ValueError):
        return None, (
            f"params.structure_ref={structure_ref!r} is not a structure ref id"
        )
    ref = store.get_ref(kind="structure", id=sid)
    if ref is None:
        return None, f"params.structure_ref={sid}: no live structure with that id"
    try:
        scene, _handles = store.structure_load(sid)
    except Exception as exc:
        return None, f"params.structure_ref={sid}: structure_load failed ({exc})"
    problem = _slab_111_problem(scene)
    if problem is not None:
        return None, f"params.structure_ref={sid}: {problem}"
    from precis.structure import export
    from precis.structure.scene import FIX_ALL

    if not any(a.fixed == FIX_ALL for a in scene.atoms.values()):
        return None, (
            f"params.structure_ref={sid}: the structure pins no atoms — the "
            "scan needs the slab's frozen bottom layers to ride the export "
            "as a FixAtoms; freeze them on the structure (the slab op's "
            "fix_layers) or pass slab_extxyz directly"
        )
    return export.to_extxyz(scene, constraints=True), None


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    """Submit-time gate (``put(kind='job')``): the ``check_params`` scope
    refusals up front, then ``structure_ref`` resolved into
    ``params['slab_extxyz']`` — the minted job then carries the exact
    geometry it scans, the same shape ``precis.quest.surface_anchors``
    mints. ``slab_extxyz`` and ``structure_ref`` are one-of: both given
    is refused before the structure is loaded."""
    del gripe_id
    bad = check_params(params)
    if bad is not None:
        return bad
    if params.get("structure_ref") is None:
        return None
    if params.get("slab_extxyz"):
        return (
            "params.slab_extxyz and params.structure_ref are one-of: pass "
            "structure_ref alone (the server exports the slab with its "
            "FixAtoms) or slab_extxyz alone, not both"
        )
    slab, err = structure_slab_extxyz(store, params["structure_ref"])
    if err is not None:
        return err
    params["slab_extxyz"] = slab
    return None


def scan_idem_key(
    structure_ref: int | str, model_index: int, config: dict[str, Any]
) -> str:
    """The idem key for a hand-minted scan (the per-candidate recipe in
    ``precis-pathway-help``): distinct per (structure, model_index, config
    basis), so scanning a second candidate or model mints a fresh job
    while a re-run of the same put collapses onto the first. Do NOT rely
    on the ``link=`` fallback — ``put`` derives a missing idem key from
    the link string alone, so two scans carrying ``link='quest:N'`` would
    dedupe onto one job."""
    basis = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    return f"{NAME}:{structure_ref}:m{int(model_index)}:{basis}"


def find_sibling_scans(
    store: Any, anchor_key: str, *, exclude: int | None = None
) -> dict[str, dict[str, Any]]:
    """``{model: coverage payload}`` from the newest succeeded
    ``surface_coverage_scan`` job per model on ``anchor_key`` (``exclude``
    drops the calling job's own row). The lookup mirrors
    ``pourbaix_bulk.find_cached_entries``: newest ``ref_id`` first, a job
    that did not reach ``STATUS:succeeded`` is skipped."""
    return {
        model: row["coverage"]
        for model, row in sibling_scan_rows(store, anchor_key, exclude=exclude).items()
    }


def sibling_scan_rows(
    store: Any, anchor_key: str, *, exclude: int | None = None
) -> dict[str, dict[str, Any]]:
    """:func:`find_sibling_scans` with each scan's footing alongside:
    ``{model: {ref_id, coverage, engine_version, corrections}}`` — what the
    dispatch needs to pool only scans on its own footing (gr489162)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta->>'model', meta->'coverage', "
            "meta->>'engine_version', meta->'corrections' FROM refs "
            "WHERE kind = 'job' AND retired_at IS NULL "
            "AND meta->>'job_type' = %(jt)s "
            "AND meta->>'anchor_key' = %(key)s "
            "ORDER BY ref_id DESC LIMIT 50",
            {"jt": NAME, "key": anchor_key},
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for ref_id, model, coverage, engine_version, corrections in rows:
        if exclude is not None and int(ref_id) == exclude:
            continue
        if not model or not isinstance(coverage, dict) or model in out:
            continue
        if any(str(t) == "STATUS:succeeded" for t in store.tags_for(int(ref_id))):
            out[str(model)] = {
                "ref_id": int(ref_id),
                "coverage": coverage,
                "engine_version": engine_version,
                # a scan stamped before the meta field: the coverage.json
                # block the engine recorded is the same record
                "corrections": corrections
                if corrections is not None
                else coverage.get("corrections"),
            }
    return out


def _sigma_e(params: dict[str, Any], config: dict[str, Any]) -> float:
    from .surface_pourbaix import DEFAULT_SIGMA_E_EV

    if params.get("sigma_e_eV") is not None:
        return float(params["sigma_e_eV"])
    thresh = (config.get("search") or {}).get("energy_thresh")
    return (
        float(thresh)
        if isinstance(thresh, int | float) and thresh > 0
        else DEFAULT_SIGMA_E_EV
    )


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``ssh_node`` for a claimed job: one scan
    in a killable child, then the pooled sweep onto this job's own meta."""
    params = (ctx.meta or {}).get("params") or {}
    bad = check_params(params)
    if bad is not None:
        ctx.record_failure(f"{NAME}: {bad}", failure_class="input")
        return
    from . import runner, surface_pourbaix

    mixtures_ok = runner.coverage_mixtures_supported()
    config = with_defaults(dict(params["config"]), mixtures=mixtures_ok)
    asked_mixtures = (params["config"].get("coverage") or {}).get("mixtures")
    model_index = int(params.get("model_index", 0) or 0)
    slab_extxyz = params.get("slab_extxyz")
    structure_ref = params.get("structure_ref")
    if slab_extxyz is None and structure_ref is not None:
        # The put path resolves via validate_submit; this fallback covers
        # jobs minted off that path (a todo-dispatched row skips
        # validate_submit). A resolved row carries both; its slab wins.
        slab_extxyz, err = structure_slab_extxyz(ctx.store, structure_ref)
        if slab_extxyz is None:
            ctx.record_failure(f"{NAME}: {err}", failure_class="input")
            return
    resources = params.get("resources") or {}
    try:
        timeout_s = int(resources.get("wall_seconds") or 0)
    except (TypeError, ValueError):
        timeout_s = 0
    cpuset = resources.get("cpuset")

    from precis.quest import pourbaix_map

    # Spectator axes (gr489164): an explicit `reservoirs` param, else the
    # reaction's own feed (an NO substrate sets mu_N), else none. Parsed
    # before the scan so a malformed line costs no compute.
    try:
        if params.get("reservoirs") is not None:
            reservoirs = surface_pourbaix.reservoirs_from(params["reservoirs"])
        else:
            feed = surface_pourbaix.feed_reservoir(config.get("substrate"))
            reservoirs = [feed] if feed is not None else []
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(f"{NAME}: reservoirs: {exc}", failure_class="input")
        return

    # An injected slab is part of the anchor set's content address (see
    # runner.coverage_anchor_key's slab_key): a digest of the bytes, never
    # the bytes themselves, so the key stays a key.
    slab_key = (
        hashlib.sha256(slab_extxyz.encode("utf-8")).hexdigest() if slab_extxyz else None
    )
    anchor_key = str(
        params.get("anchor_key")
        or runner.coverage_anchor_key(config, slab_key=slab_key)
    )
    cov = config["coverage"]
    n_top = slab_top_sites(config)
    achieved = representable_coverages(cov["coverages"], n_top)
    requested = (params["config"].get("coverage") or {}).get("coverages")
    requested = list(DEFAULT_COVERAGES) if requested is None else requested
    achieved_text = (
        f"[{', '.join(f'{t:g}' for t in achieved)}]"
        if isinstance(achieved, list)
        else repr(achieved)
    )
    snap_note = (
        f" (requested {requested}, snapped to {n_top} top sites)"
        if achieved != requested
        else ""
    )
    if structure_ref is not None:
        slab_note = f" slab=st{structure_ref}"
    elif slab_extxyz:
        slab_note = " slab=injected"
    else:
        slab_note = ""
    if cov.get("mixtures"):
        mix_note = " mixtures=" + ",".join(
            "+".join(str(sp) for sp in m.get("species") or []) for m in cov["mixtures"]
        )
    elif asked_mixtures:
        mix_note = (
            f" mixtures dropped (engine {runner.__version__} lacks coverage.mixtures)"
        )
    else:
        mix_note = ""
    ctx.append_chunk(
        "job_event",
        f"{NAME}: {config.get('name', '?')} model#{model_index} "
        f"adsorbates={cov['adsorbates']} "
        f"coverages={achieved_text}{snap_note}{slab_note}{mix_note} "
        f"key={anchor_key[:12]}…",
    )
    try:
        kw: dict[str, Any] = {
            "force_backend": params.get("force_backend"),
            "slab_extxyz": slab_extxyz,
        }
        if timeout_s > 0:
            kw["timeout"] = timeout_s
        if cpuset:
            kw["cpuset"] = cpuset
        result = runner.run_coverage_scan_subprocess(config, model_index, **kw)
    except runner.ChildKilledError as exc:
        log.warning("%s: run failed (child killed)", NAME, exc_info=True)
        ctx.record_failure(f"{NAME}: run failed: {exc}", open_tag="infra:child-killed")
        return
    except Exception as exc:  # pragma: no cover - env/compute dependent
        log.warning("%s: run failed", NAME, exc_info=True)
        ctx.record_failure(f"{NAME}: run failed: {exc}")
        return

    model = str(result["model"])
    coverage = result["coverage"]
    own_id = getattr(ctx, "ref_id", None)
    siblings = sibling_scan_rows(
        ctx.store, anchor_key, exclude=int(own_id) if isinstance(own_id, int) else None
    )
    # Pool only scans on this run's own correction footing (gr489162):
    # anchor_key pins slab + anchor set + engine, but not the correction
    # set, and a sweep across footings blends them (ruling q2). The rest
    # are named; a sibling that applied a strict subset of this run's
    # levers is the gr486949 "uncorrected" case and keeps that name.
    own_corr = result.get("corrections", coverage.get("corrections"))
    footing = pourbaix_map.correction_footing(own_corr)
    own_levers = set(pourbaix_map.applied_levers(own_corr))
    payloads: dict[str, dict[str, Any]] = {}
    not_pooled: list[dict[str, Any]] = []
    for sib_model, row in siblings.items():
        if sib_model == model:
            continue  # this run wins over an older scan of the same model
        if pourbaix_map.correction_footing(row["corrections"]) == footing:
            payloads[sib_model] = row["coverage"]
        else:
            not_pooled.append({"model": sib_model, "job": row["ref_id"]})
    payloads[model] = coverage
    payloads, dropped_uncorrected = surface_pourbaix.drop_uncorrected(payloads)
    dropped_uncorrected = sorted(
        {
            *dropped_uncorrected,
            *(
                r["model"]
                for r in not_pooled
                if set(pourbaix_map.applied_levers(siblings[r["model"]]["corrections"]))
                < own_levers
            ),
        }
    )
    window = params.get("window_U_RHE") or list(DEFAULT_U_WINDOW)
    point = params.get("point_U_RHE")
    che = surface_pourbaix.sweep(
        payloads,
        u_window=(float(window[0]), float(window[1])),
        grid=int(params.get("grid") or DEFAULT_GRID),
        ph=float(params.get("pH", DEFAULT_PH)),
        point_u_rhe=None if point is None else float(point),
        sigma_e_ev=_sigma_e(params, config),
        reservoirs=reservoirs,
    )
    che["footing_key"] = footing
    if dropped_uncorrected:
        che["dropped_uncorrected"] = dropped_uncorrected
    if not_pooled:
        che["not_pooled"] = not_pooled
    ctx.set_meta(
        anchor_key=anchor_key,
        model=model,
        model_index=model_index,
        engine_version=result["engine_version"],
        corrections=result.get("corrections"),
        slab_source=coverage.get("slab_source"),
        lattice_source=coverage.get("lattice_source"),
        coverage=coverage,
        che_sweep=che,
    )
    warnings = list(coverage.get("warnings") or [])
    if dropped_uncorrected:
        warnings.append(
            "corrections: model(s) "
            + ", ".join(dropped_uncorrected)
            + " dropped from the pooled sweep — scanned without the "
            "correction set the kept model(s) applied; a band across mixed "
            "corrected/uncorrected anchors prices the correction, not the "
            "model form (the dropped scans' own anchors still stand)"
        )
    others = sorted({r["model"] for r in not_pooled} - set(dropped_uncorrected))
    if others:
        warnings.append(
            "footing: model(s) "
            + ", ".join(others)
            + " not pooled — scanned on a different correction footing than "
            "this run; a sweep across footings blends them (ruling q2)"
        )
    text = surface_pourbaix.render_text(che)
    if warnings:
        text += "\nscan warnings: " + "; ".join(str(w) for w in warnings[:6])
    ctx.append_chunk("job_summary", text)
    ctx.set_status("succeeded")


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError(f"{NAME} runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=_run,
    dispatch=_dispatch,
    validate_submit=validate_submit,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "NAME",
    "SPEC",
    "check_params",
    "find_sibling_scans",
    "load",
    "scan_idem_key",
    "structure_slab_extxyz",
    "validate_submit",
    "with_defaults",
]

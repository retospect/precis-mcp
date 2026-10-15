"""Quest-minted surface-coverage anchors (surface-Pourbaix wave B).

Where Part A's bulk verdict (:mod:`precis.quest.pourbaix`) answers "does
the bulk survive at (U, pH)", this module mints the *surface* anchors —
one ``surface_coverage_scan`` job per live candidate structure — and
harvests the pooled sweep onto the candidate's meta
(``docs/backlog/surface-pourbaix-staircase-optimizer.md``, wave B).

Opt-in: the quest declares ``meta.surface_anchors: true`` (default off,
so existing quests are unchanged) and carries the usual
``meta.reaction_config`` (the catpath config the scan runs; its
``coverage`` block defaults are filled by
:mod:`precis_pathway.coverage_job`). Operating inputs come from
``meta.operating_conditions`` when set — ``U_RHE`` → ``point_U_RHE``,
``pH`` → ``pH``, ``window.U_RHE`` → ``window_U_RHE`` — else the job's own
module defaults apply.

Dispatch mirrors the Part A pattern: content-addressed on the candidate
geometry (``geom_hash_c``), the reaction config, the engine token and the
operating inputs, so a re-tick never mints a duplicate and a failed scan
is never re-minted — the Part A convention: ``_find_job_by_idem_key``
sees jobs of ANY status, and a human re-arms by changing the basis.

**Mint rule.** A candidate earns a scan when it has completed at least
the ``neb`` rung of the fidelity ladder (``meta.tier`` or
``meta.barrier_fidelity`` ≥ ``neb`` — :func:`precis.quest.compute.promote_tiers`)
OR sits on the quest's current merged Pareto front
(:func:`precis.quest.compute._merged_frontier_ids` over
:func:`precis.quest.frontier.quest_frontier`). The scan is cheap
(~1 min on MACE), so front members are anchored ahead of promotion.

**Anchor-placement proposals.** When the quest also declares window
objectives, the same dispatch pass mints the top-ranked scans
:func:`precis.quest.window_objectives.propose_anchor_scans` returns —
one per band, bounded per tick — each with the bracketing coverage
counts folded into ``config.coverage.coverages`` (a *different* anchor
set, hence a different ``anchor_key``/idem key) and the proposal itself
as ``params.proposal`` provenance. A proposal that adds no representable
θ and no new adsorbate is skipped rather than re-keyed onto a duplicate
footing.

The candidate's own exported slab — ``export.to_extxyz(scene,
constraints=True)``, the same export ``dispatch_autocatpath`` hands the
seed fan-out — is injected as ``slab_extxyz``: on autocatpath ≥ 0.25.0
the scan relaxes anchors on the real candidate geometry rather than the
config's label slab, and the job's ``anchor_key`` folds a digest of those
bytes (:func:`precis_pathway.runner.coverage_anchor_key`'s ``slab_key``)
so distinct slabs sharing the label never pool.

Harvest stamps onto the candidate structure: ``anchors_job_id``,
``anchor_key``, ``anchors_footing`` (``{model, engine_version,
corrections}`` — one footing, reported never blended),
``resting_termination_at_point`` (the sweep's ``point`` block),
``needs_anchor_top`` (the widest propagated strip — where the next
anchor is worth most), and ``theta0_check`` — the θ→0 acceptance check:
each adsorbate's lowest-coverage anchor vs the candidate pathway's
single-adsorbate energies (succeeded seed partials: parked-fragment
``refs.bare`` prices, single-fragment node cells, and ``X+S``
differential proxies for species with no bare cell — H* in the ammonia
network), all priced on the scan's own ``coverage.mu`` + ``c_ads`` gauge
so the delta carries only the electronic/lateral difference. ``None``
with a reasoned ``theta0_note`` when the inputs are missing.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import math
from typing import Any

log = logging.getLogger(__name__)

#: The job_type this module orchestrates — by NAME, never by import:
#: ``precis_pathway`` is a plugin package and core (``src/precis``) may not
#: import it (``tests/test_plugin_import_boundary.py``); the same rule
#: ``precis.quest.compute`` follows for ``autocatpath_seed``.
SCAN_JOB_TYPE = "surface_coverage_scan"


def _params_reason(params: dict[str, Any]) -> str | None:
    """A local mirror of ``precis_pathway.coverage_job.check_params``'s
    scope rules — the four cheap refusal classes (config shape, facets,
    miller, window ordering), so a quest-side dispatch never mints a job
    that can only fail ``failure_class="input"``. Kept deliberately
    narrower than the job's own check (which runs again at dispatch)."""
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


def _request(
    meta: dict[str, Any] | None, candidate: Any
) -> tuple[dict[str, Any], str] | None:
    """The scan params + idem key for one candidate on this quest's meta.

    ``None`` fails closed: flag off, no ``reaction_config``, or no
    ``geom_hash_c``. The key folds the engine token — an engine bump
    re-keys every anchor set (the same standing fix the pathway content
    keys carry)."""
    meta = meta or {}
    if not meta.get("surface_anchors"):
        return None
    config = meta.get("reaction_config")
    geometry = (candidate.meta or {}).get("geom_hash_c")
    if not isinstance(config, dict) or not config:
        return None
    if not isinstance(geometry, str) or not geometry:
        return None
    conditions = meta.get("operating_conditions")
    conditions = conditions if isinstance(conditions, dict) else {}
    params: dict[str, Any] = {"config": config}
    u = conditions.get("U_RHE")
    if (
        isinstance(u, int | float)
        and not isinstance(u, bool)
        and math.isfinite(float(u))
    ):
        params["point_U_RHE"] = float(u)
    ph = conditions.get("pH")
    if (
        isinstance(ph, int | float)
        and not isinstance(ph, bool)
        and math.isfinite(float(ph))
    ):
        params["pH"] = float(ph)
    window = conditions.get("window")
    w = window.get("U_RHE") if isinstance(window, dict) else None
    if (
        isinstance(w, list | tuple)
        and len(w) == 2
        and all(isinstance(v, int | float) and not isinstance(v, bool) for v in w)
    ):
        params["window_U_RHE"] = [float(w[0]), float(w[1])]

    return params, _idem_key(candidate.id, geometry, params)


def _idem_key(candidate_id: int, geometry: str, params: dict[str, Any]) -> str:
    """The scan idem key for (geometry, engine token, params) — the key
    folds the engine token so an engine bump re-keys every anchor set;
    proposal scans differ only in params and so key distinctly."""
    from precis.quest.compute import _autocatpath_engine_token

    basis = {
        "geom_hash_c": geometry,
        "engine": _autocatpath_engine_token(),
        "params": params,
    }
    token = hashlib.sha256(
        json.dumps(basis, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return f"surface_coverage_scan:{candidate_id}:{token}"


def _reached_neb(meta: dict[str, Any] | None) -> bool:
    """The candidate's highest completed rung is at least ``neb`` —
    ``meta.tier`` (the ladder stamp) or the pre-ladder
    ``meta.barrier_fidelity`` (the same pair :func:`promote_tiers`' neb →
    verify pass reads)."""
    from precis.quest.compute import _TIER_FIDELITY, _TIER_NEB

    meta = meta or {}
    ranks = [
        _TIER_FIDELITY.get(str(meta.get(k)), -1) for k in ("tier", "barrier_fidelity")
    ]
    return max(ranks) >= _TIER_FIDELITY[_TIER_NEB]


def _frontier_ids(store: Any, quest_id: int) -> set[int]:
    """The quest's current merged Pareto front — confirmed frontier plus
    provisional-front members (the same set :func:`promote_tiers` ranks
    first)."""
    from precis.quest.compute import _merged_frontier_ids
    from precis.quest.frontier import quest_frontier

    return _merged_frontier_ids(quest_frontier(store, quest_id))


def _measured(meta: dict[str, Any] | None) -> bool:
    """True when the candidate carries anything the frontier could rank on —
    a harvested-run marker or a numeric meta field outside the frontier's
    non-measure set. An unmeasured candidate lands in ``unevaluated`` and
    is *never* a merged-front member, so the dispatch can skip the full
    ``quest_frontier`` call for it. (A relax that landed since the last
    harvest passes this on the *next* tick — one tick later, then stamped
    ``quest_harvested_upto`` — a scan is idempotent, so the delay is
    harmless.)"""
    from precis.quest.frontier import _META_NON_MEASURE, _UNTRUSTED_VALUE_SUFFIX

    meta = meta or {}
    if meta.get("quest_harvested_upto") or meta.get("quest_autocatpath_harvested_upto"):
        return True
    for k, v in meta.items():
        if (
            k == "params"
            or k in _META_NON_MEASURE
            or str(k).endswith(_UNTRUSTED_VALUE_SUFFIX)
        ):
            continue
        if isinstance(v, int | float) and not isinstance(v, bool):
            return True
    return False


def dispatch_surface_anchors(store: Any, quest_id: int, *, hub: Any) -> list[str]:
    """Mint one ``surface_coverage_scan`` job per eligible live candidate.

    Eligible = reached at least the ``neb`` rung OR on the current merged
    front (see the module docstring). Idempotent on (geometry hash,
    anchor basis): ``_find_job_by_idem_key`` sees every status, so a
    re-tick never duplicates and a failed scan is not re-minted — the
    Part A convention."""
    from precis.quest.compute import _find_job_by_idem_key
    from precis.quest.gaps import _live_servers

    quest = store.fetch_refs_by_ids({quest_id}).get(quest_id)
    if quest is None or not (quest.meta or {}).get("surface_anchors"):
        return []
    if not isinstance((quest.meta or {}).get("reaction_config"), dict):
        return ["surface anchors skipped: quest has no reaction_config"]
    job_handler = hub.handler_for("job")
    if job_handler is None:
        return [f"{SCAN_JOB_TYPE} dispatch skipped: job handler unavailable"]

    notes: list[str] = []
    frontier_ids: set[int] | None = None
    for candidate in _live_servers(store, quest_id):
        if candidate.kind != "structure":
            continue
        built = _request(quest.meta, candidate)
        if built is None:
            notes.append(
                f"surface anchors skipped: structure {candidate.id} "
                "has no geometry hash"
            )
            continue
        params, idem_key = built
        if _find_job_by_idem_key(store, idem_key) is not None:
            continue
        if not _reached_neb(candidate.meta):
            if not _measured(candidate.meta):
                # Unmeasured → ``unevaluated`` → never a front member; skip
                # the frontier eval so a sub-neb cohort doesn't cost one
                # ``quest_frontier`` per tick.
                continue
            if frontier_ids is None:
                frontier_ids = _frontier_ids(store, quest_id)
            if candidate.id not in frontier_ids:
                continue
        bad = _params_reason(params)
        if bad is not None:
            notes.append(f"surface anchors skipped for structure {candidate.id}: {bad}")
            continue
        note = _mint_scan_job(store, job_handler, quest_id, candidate, params, idem_key)
        if note:
            notes.append(note)
    # Anchor-placement proposals (window_objectives): where the map's
    # propagated uncertainty band is widest, mint one denser-grid scan per
    # top-ranked band. Only when window objectives are declared — that is
    # what makes propose_anchor_scans non-empty — and bounded to its
    # top-n, so a flagged quest pays at most a couple of extra scans per
    # tick, each idempotent by the same idem_key convention.
    from precis.quest.window_objectives import window_objectives_enabled

    if window_objectives_enabled(quest.meta or {}):
        notes.extend(
            _dispatch_anchor_proposals(store, quest_id, quest.meta or {}, job_handler)
        )
    return notes


def _mint_scan_job(
    store: Any,
    job_handler: Any,
    quest_id: int,
    candidate: Any,
    params: dict[str, Any],
    idem_key: str,
) -> str | None:
    """Export the slab and mint one ``surface_coverage_scan`` job;
    returns the dispatch/skip note, ``None`` only never."""
    try:
        from precis.structure import export

        scene, _handles = store.structure_load(candidate.id)
        # constraints=True → the frozen bottom layers ride along as a
        # FixAtoms (same export the seed fan-out consumes).
        slab_extxyz = export.to_extxyz(scene, constraints=True)
    except Exception as exc:
        log.warning(
            "surface anchors: slab export failed for %s",
            candidate.id,
            exc_info=True,
        )
        return f"surface anchors skipped for structure {candidate.id}: export ({exc})"
    try:
        response = job_handler.put(
            parent_id=candidate.id,
            job_type=SCAN_JOB_TYPE,
            executor="ssh_node",
            # parent_id is the provenance: the put refuses slab_extxyz
            # and structure_ref together.
            params={**params, "slab_extxyz": slab_extxyz},
            idem_key=idem_key,
        )
    except Exception as exc:
        log.warning(
            "surface_coverage_scan dispatch failed for structure %s",
            candidate.id,
            exc_info=True,
        )
        return (
            f"surface_coverage_scan dispatch failed for structure {candidate.id}: {exc}"
        )
    job_id = getattr(response, "ref_id", None)
    if isinstance(job_id, int):
        try:
            store.add_link(
                src_ref_id=job_id,
                dst_ref_id=quest_id,
                relation="part-of",
            )
        except Exception:
            log.warning(
                "surface anchors: part-of link failed for job %s",
                job_id,
                exc_info=True,
            )
    return response.body


#: Proposal scans minted per dispatch pass — ``propose_anchor_scans``
#: returns at most ``top_n`` proposals; keep the two in lock-step.
_PROPOSAL_TOP_N = 2


def _proposal_coverages(
    config: dict[str, Any], extension: dict[str, Any]
) -> list[float] | None:
    """The proposal's densified coverage grid: the strip's ``below``/
    ``above`` terminations sit at site counts ``n``, so the extension
    asks for the *skipped* counts bracketing them (n ± 1, clamped to
    [1, n_top]) on top of the config's requested θ. ``None`` when the
    site count can't be derived — the caller then leaves the grid alone
    rather than request a wrong grid."""
    n_list = extension.get("n")
    if not isinstance(n_list, list) or not n_list:
        return None
    try:
        mod = importlib.import_module("precis_pathway.coverage_sites")
        n_top = int(mod.slab_top_sites(config))
        representable = mod.representable_coverages
    except Exception:
        # Plugin-free fallback: (111) slab top-site count is nx*ny.
        size = (config.get("slab") or {}).get("size") or []
        try:
            n_top = int(size[0]) * int(size[1])
        except (TypeError, ValueError, IndexError):
            return None
        representable = None
    if n_top < 1:
        return None
    counts = {min(n_top, max(1, int(n) + d)) for n in n_list for d in (-1, 1)}
    requested = (config.get("coverage") or {}).get("coverages")
    if isinstance(requested, str) and requested == "all":
        return None  # already dense
    if isinstance(requested, list | tuple):
        base_counts = {min(n_top, max(1, round(float(t) * n_top))) for t in requested}
    else:
        try:
            mod = importlib.import_module("precis_pathway.coverage_job")
            base_counts = {
                min(n_top, max(1, round(float(t) * n_top)))
                for t in mod.DEFAULT_COVERAGES
            }
        except Exception:
            base_counts = {2, 4, 7, n_top}
    if counts <= base_counts:
        return None  # every bracketing count is already measured
    if representable is not None:
        return representable([k / n_top for k in sorted(base_counts | counts)], n_top)
    return [k / n_top for k in sorted(base_counts | counts)]


def _proposal_scan_params(
    params: dict[str, Any], proposal: dict[str, Any]
) -> dict[str, Any] | None:
    """Fold a placement proposal into scan params — or ``None`` when the
    proposal adds nothing the baseline scan wouldn't measure (no new
    representable θ and no new adsorbate): a same-anchors job would just
    re-key a duplicate footing.

    Otherwise: union the target adsorbates, extend the coverage grid with
    the counts bracketing the strip's two terminations, and centre the
    sweep's point marker on the band's U. The distinct ``coverages`` is
    what makes this a *different* anchor set — a different
    ``anchor_key``/idem key. The proposal itself rides as provenance."""
    out = dict(params)
    raw_p = proposal.get("params")
    p: dict[str, Any] = dict(raw_p) if isinstance(raw_p, dict) else {}
    config = dict(out.get("config") or {})
    cov = dict(config.get("coverage") or {})
    extended = _proposal_coverages(config, p.get("coverage_grid_extension") or {})
    existing_ads = {str(a) for a in cov.get("adsorbates") or []}
    asked_ads = {str(a) for a in p.get("adsorbates") or []}
    if extended is None and (not asked_ads or asked_ads <= existing_ads):
        return None
    if extended is not None:
        cov["coverages"] = extended
    ads = sorted(existing_ads | asked_ads)
    if ads:
        cov["adsorbates"] = ads
    config["coverage"] = cov
    out["config"] = config
    u = p.get("around_U_RHE")
    if (
        isinstance(u, int | float)
        and not isinstance(u, bool)
        and math.isfinite(float(u))
    ):
        out["point_U_RHE"] = float(u)
    out["proposal"] = {
        k: v
        for k, v in proposal.items()
        if k not in ("structure_ref", "anchor_key", "job_type")
    }
    return out


def _dispatch_anchor_proposals(
    store: Any, quest_id: int, quest_meta: dict[str, Any], job_handler: Any
) -> list[str]:
    """Mint the anchor-placement scans ``propose_anchor_scans`` ranks —
    the proposal params fold into the idem key, so a repeat proposal (and
    a failed one, per the Part A convention) is never re-minted."""
    from precis.quest.compute import _find_job_by_idem_key
    from precis.quest.window_objectives import propose_anchor_scans

    try:
        proposals = propose_anchor_scans(store, quest_id, top_n=_PROPOSAL_TOP_N)
    except Exception:
        log.warning("surface anchors: proposal ranking failed", exc_info=True)
        return []
    notes: list[str] = []
    for proposal in proposals:
        cid = proposal.get("structure_ref")
        if not isinstance(cid, int):
            continue
        ref = store.fetch_refs_by_ids({cid}).get(cid)
        if (
            ref is None
            or ref.kind != "structure"
            or getattr(ref, "retired_at", None) is not None
        ):
            continue
        built = _request(quest_meta, ref)
        if built is None:
            continue
        base_params, _base_key = built
        params = _proposal_scan_params(base_params, proposal)
        if params is None:
            continue
        geometry = str((ref.meta or {}).get("geom_hash_c"))
        idem_key = _idem_key(cid, geometry, params)
        if _find_job_by_idem_key(store, idem_key) is not None:
            continue
        bad = _params_reason(params)
        if bad is not None:
            notes.append(f"anchor proposal skipped for structure {cid}: {bad}")
            continue
        note = _mint_scan_job(store, job_handler, quest_id, ref, params, idem_key)
        if note:
            u = (proposal.get("params") or {}).get("around_U_RHE")
            notes.append(f"anchor proposal (U≈{u}): {note}")
    return notes


def _fragments_of(node: str) -> list[str]:
    """A pathway state-key's adsorbate fragments — the engine's split
    mirrored: '+' separates co-adsorbates, '@site' marks an adsorption
    site, '~' marks a desorption channel. A key carrying any '~' part is
    skipped wholesale — its cell bookkeeping (which fragments are still
    adsorbed) is ambiguous for this use."""
    frags: list[str] = []
    for part in str(node).split("+"):
        part = part.strip()
        if not part or part == "*" or "~" in part:
            return []
        frags.append(part.split("@", 1)[0])
    return frags


def _seed_partials(store: Any, pathway_id: int) -> list[dict[str, Any]]:
    """The pathway's succeeded seed-job partials — each carries ``refs``
    (``slab``/``gas``/``mu``/``bare`` prices) and ``states`` (raw per-node
    cell energies)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta->'partial' FROM refs "
            "WHERE kind = 'job' AND retired_at IS NULL "
            "AND meta->>'job_type' = 'autocatpath_seed' "
            "AND meta->>'pathway_ref' = %(pw)s AND meta ? 'partial' "
            "ORDER BY ref_id",
            {"pw": str(pathway_id)},
        ).fetchall()
    return [
        partial
        for ref_id, partial in rows
        if isinstance(partial, dict)
        and any(str(t) == "STATUS:succeeded" for t in store.tags_for(int(ref_id)))
    ]


def _theta0_reference(
    partials: list[dict[str, Any]],
    low_confidence: set[str],
    mu: dict[str, Any],
    c_ads: dict[str, Any],
) -> tuple[dict[str, float], dict[str, Any], int]:
    """θ→0 reference formation energies {species: eV} on the scan's gauge.

    Per species S and per seed partial, absolute-energy differences priced
    on the scan's reservoirs ``mu`` (``c_ads`` is added back so the thermal
    term cancels in the delta — the A5 recipe):

    * ``refs.bare[S] − slab`` — the parked-fragment price (bare fragment
      relaxed on the clean slab);
    * ``states[S…] − slab`` — a single-fragment node's own cell (lowest
      energy across site variants is canonical);
    * ``states[X+S] − cell(X)`` — the differential adsorption of S next to
      X, the only source for species with no bare cell (the ammonia
      network's H*), where ``cell(X)`` is ``bare[X]`` else the lowest
      single-fragment node energy.

    Estimates pool across partials (mean); per-species provenance and
    spread are returned for the stamp. Returns (reference, provenance,
    n_partials_used).
    """
    est: dict[str, list[float]] = {}
    src: dict[str, set[str]] = {}
    used = 0
    for partial in partials:
        refs = partial.get("refs")
        states = partial.get("states")
        refs = refs if isinstance(refs, dict) else {}
        states = states if isinstance(states, dict) else {}
        slab = refs.get("slab")
        if not isinstance(slab, int | float) or isinstance(slab, bool):
            slab = states.get("*")
        if not isinstance(slab, int | float) or isinstance(slab, bool):
            continue
        slab = float(slab)
        bare = {
            str(f): float(e)
            for f, e in (refs.get("bare") or {}).items()
            if isinstance(e, int | float) and not isinstance(e, bool)
        }
        singles: dict[str, float] = {}
        cells: dict[str, float] = {}
        for name, e in states.items():
            if (
                name in low_confidence
                or not isinstance(e, int | float)
                or isinstance(e, bool)
            ):
                continue
            frags = _fragments_of(name)
            if len(frags) == 1:
                f = frags[0]
                singles[f] = min(singles.get(f, math.inf), float(e))
        cells.update(singles)
        cells.update(bare)  # the parked price is canonical
        for f, e in sorted(cells.items()):
            muv = mu.get(f)
            if not isinstance(muv, int | float):
                continue
            est.setdefault(f, []).append(e - slab - float(muv))
            src.setdefault(f, set()).add("bare" if f in bare else f"node:{f}")
        for name, e in states.items():
            if (
                name in low_confidence
                or not isinstance(e, int | float)
                or isinstance(e, bool)
            ):
                continue
            frags = _fragments_of(name)
            if len(frags) != 2:
                continue
            for i, f in enumerate(frags):
                x = frags[1 - i]
                muv = mu.get(f)
                if x in cells and isinstance(muv, int | float):
                    est.setdefault(f, []).append(float(e) - cells[x] - float(muv))
                    src.setdefault(f, set()).add(f"proxy:{name} via {x}")
        used += 1
    reference: dict[str, float] = {}
    provenance: dict[str, Any] = {}
    for f, vs in est.items():
        ca = c_ads.get(f)
        reference[f] = sum(vs) / len(vs) + (
            float(ca) if isinstance(ca, int | float) else 0.0
        )
        provenance[f] = {
            "n_estimates": len(vs),
            "spread_eV": round(max(vs) - min(vs), 4),
            "sources": sorted(src.get(f) or ()),
        }
    return reference, provenance, used


def _theta0_check(
    store: Any, candidate: Any, job_meta: dict[str, Any]
) -> tuple[dict[str, Any] | None, str]:
    """The θ→0 acceptance check: the scan's lowest-coverage per-adsorbate
    formation energies against the candidate pathway's own
    single-adsorbate energies (succeeded seed partials), on the scan's
    reservoir gauge. ``(check, note)`` — ``check`` is ``None`` with a
    reasoned note whenever an input is missing; nothing is fabricated."""
    cov = job_meta.get("coverage")
    cov = cov if isinstance(cov, dict) else {}
    try:
        sp = importlib.import_module("precis_pathway.surface_pourbaix")
    except Exception:
        return None, "precis_pathway.surface_pourbaix unavailable"
    anchors = sp.anchors_from_coverage(cov) if cov else []
    if not anchors:
        return None, "scan job carries no coverage anchors"
    facets = cov.get("facets") or []
    facet = facets[0] if facets and isinstance(facets[0], dict) else {}
    ads_rec = facet.get("adsorbates") or {}
    mu = {str(k): v for k, v in (cov.get("mu") or {}).items()}
    if not mu:
        mu = {
            str(k): v.get("mu")
            for k, v in ads_rec.items()
            if isinstance(v, dict) and v.get("mu") is not None
        }
    c_ads = {str(k): v.get("c_ads") for k, v in ads_rec.items() if isinstance(v, dict)}
    from precis.quest.window_objectives import _candidate_pathway_ref_id

    pathway_id = _candidate_pathway_ref_id(store, int(candidate.id))
    if pathway_id is None:
        return None, "no pathway ref for this candidate"
    partials = _seed_partials(store, pathway_id)
    if not partials:
        return None, "pathway has no succeeded seed partials"
    pw = store.fetch_refs_by_ids({pathway_id}).get(pathway_id)
    pw_meta = dict(pw.meta or {}) if pw is not None else {}
    nodes = (pw_meta.get("results") or {}).get("nodes") or {}
    low_confidence = {
        str(n)
        for n, v in nodes.items()
        if isinstance(v, dict) and v.get("low_confidence")
    }
    reference, provenance, used = _theta0_reference(partials, low_confidence, mu, c_ads)
    if not reference:
        return None, "pathway partials carry no priced single-adsorbate energies"
    rows = sp.compare_lowest_theta(anchors, reference)
    if not rows:
        return None, "no shared adsorbates between scan anchors and pathway"
    return (
        {
            "rows": rows,
            "reference": reference,
            "per_species": provenance,
            "n_partials": used,
            "gauge": "scan coverage.mu + per-adsorbate c_ads",
        },
        f"{len(rows)} species vs pathway single-adsorbate energies "
        f"({used} seed partials, scan mu/c_ads gauge)",
    )


def harvest_surface_anchors(
    store: Any,
    quest_id: int,
    candidate: Any,
    *,
    quest_meta: dict[str, Any] | None = None,
) -> str | None:
    """Stamp a succeeded scan's anchors once; failures are not anchors.

    ``quest_meta`` lets the per-tick caller (``harvest_measures``) hand down
    the quest meta it already fetched rather than paying one fetch per
    candidate; standalone callers may omit it."""
    from precis.quest.compute import _find_job_by_idem_key
    from precis.quest.logbook import MEASURED_BY, append_entry

    if quest_meta is None:
        quest = store.fetch_refs_by_ids({quest_id}).get(quest_id)
        if quest is None:
            return None
        quest_meta = quest.meta or {}
    built = _request(quest_meta, candidate)
    if built is None:
        return None
    _params, idem_key = built
    found = _find_job_by_idem_key(store, idem_key)
    if found is None:
        return None
    job_id, status = found
    if status != "succeeded" or (candidate.meta or {}).get("anchors_job_id") == job_id:
        return None
    job = store.fetch_refs_by_ids({job_id}).get(job_id)
    meta = dict(job.meta or {}) if job is not None else {}
    che = meta.get("che_sweep")
    che = che if isinstance(che, dict) else {}
    point = che.get("point")
    needs = che.get("needs_anchor")
    values = {
        "anchors_job_id": job_id,
        "anchor_key": meta.get("anchor_key"),
        "anchors_footing": {
            "model": meta.get("model"),
            "engine_version": meta.get("engine_version"),
            "corrections": meta.get("corrections"),
        },
        "resting_termination_at_point": point if isinstance(point, dict) else None,
        "needs_anchor_top": (needs[0] if isinstance(needs, list) and needs else None),
    }
    try:
        theta0_check, theta0_note = _theta0_check(store, candidate, meta)
    except Exception:
        log.warning("theta0_check failed for structure %s", candidate.id, exc_info=True)
        theta0_check, theta0_note = None, "theta0_check errored (see logs)"
    values["theta0_check"] = theta0_check
    values["theta0_note"] = theta0_note
    store.stamp_ref_meta(candidate.id, values)
    from precis.utils import handle_registry

    handle = (
        handle_registry.try_format("structure", candidate.id)
        or f"structure:{candidate.id}"
    )
    if isinstance(point, dict):
        winner = point.get("winner")
        term = (
            "clean surface"
            if winner is None
            else f"{winner.get('adsorbate')}*({winner.get('n')})"
        )
        u = point.get("U_RHE")
        detail = f"resting termination {term}"
        if isinstance(u, int | float):
            detail += f" at {u:+.2f} V_RHE"
    else:
        detail = f"sweep: {len(che.get('boundaries') or [])} boundary(ies)"
    text = f"surface anchors for [{handle}]: {detail}"
    append_entry(store, quest_id, text=text, entry_type="result", by=MEASURED_BY)
    return text


__all__ = [
    "dispatch_surface_anchors",
    "harvest_surface_anchors",
]

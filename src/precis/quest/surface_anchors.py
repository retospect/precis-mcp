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
anchor is worth most), and ``theta0_check`` — currently ``None`` with a
``theta0_note``: the θ→0 comparison against the pathway's
single-adsorbate energies is deferred (needs the pathway's per-adsorbate
node energies; a follow-up).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from typing import Any

log = logging.getLogger(__name__)

#: Deferred θ→0 acceptance note — stamped next to ``theta0_check: None``
#: until the pathway's per-adsorbate node energies are wired in.
THETA0_DEFERRED_NOTE = (
    "deferred: needs per-adsorbate node energies from the pathway (follow-up)"
)

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

    from precis.quest.compute import _autocatpath_engine_token

    basis = {
        "geom_hash_c": geometry,
        "engine": _autocatpath_engine_token(),
        "params": params,
    }
    token = hashlib.sha256(
        json.dumps(basis, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return params, f"surface_coverage_scan:{candidate.id}:{token}"


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
            if frontier_ids is None:
                frontier_ids = _frontier_ids(store, quest_id)
            if candidate.id not in frontier_ids:
                continue
        bad = _params_reason(params)
        if bad is not None:
            notes.append(f"surface anchors skipped for structure {candidate.id}: {bad}")
            continue
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
            notes.append(
                f"surface anchors skipped for structure {candidate.id}: export ({exc})"
            )
            continue
        try:
            response = job_handler.put(
                parent_id=candidate.id,
                job_type=SCAN_JOB_TYPE,
                executor="ssh_node",
                params={**params, "slab_extxyz": slab_extxyz},
                idem_key=idem_key,
            )
        except Exception as exc:
            log.warning(
                "surface_coverage_scan dispatch failed for structure %s",
                candidate.id,
                exc_info=True,
            )
            notes.append(
                f"surface_coverage_scan dispatch failed for structure "
                f"{candidate.id}: {exc}"
            )
        else:
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
            notes.append(response.body)
    return notes


def harvest_surface_anchors(store: Any, quest_id: int, candidate: Any) -> str | None:
    """Stamp a succeeded scan's anchors once; failures are not anchors."""
    from precis.quest.compute import _find_job_by_idem_key
    from precis.quest.logbook import MEASURED_BY, append_entry

    quest = store.fetch_refs_by_ids({quest_id}).get(quest_id)
    if quest is None:
        return None
    built = _request(quest.meta, candidate)
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
        "theta0_check": None,
        "theta0_note": THETA0_DEFERRED_NOTE,
    }
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
    "THETA0_DEFERRED_NOTE",
    "dispatch_surface_anchors",
    "harvest_surface_anchors",
]

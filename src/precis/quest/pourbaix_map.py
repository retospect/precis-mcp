"""Surface-Pourbaix resting-state map (slice 2) — the validity-map read over
stored coverage anchors.

Design of record: ``docs/backlog/surface-pourbaix-staircase-optimizer.md``
(§Rulings 2026-10-12). Slice 1 (``precis_pathway.coverage_job`` +
``precis_pathway.surface_pourbaix``) pays for the anchors once per slab and
stamps the pooled inner CHE sweep onto each scan job's ``meta.che_sweep``.
This module is the read side: it turns the *stored* sweep(s) into a resting-
state map object — validity regions (one per winning termination over its
U interval), each boundary carrying its two bands separately, a labelled
uncertain bulk layer, and a trust/provenance block — for the quest and
structure ``view='pourbaix'`` surfaces and the ``/pourbaix/<handle>`` page.

**Lives in core, not in ``precis_pathway``, for the same reason
``precis.utils.reaction_graph`` does:** the view is served by the quest and
structure handlers, and core may not import a plugin
(``tests/test_plugin_import_boundary.py``). Everything here reads plain
dicts off stored meta — no engine call, no new table — so nothing needs the
plugin's physics anyway; ``precis_pathway.pourbaix_map`` re-exports this
module's names for pathway-side callers.

Rulings honoured:

* **One footing per map** (q2): scans are partitioned by
  ``(anchor_key, engine_version, corrections)`` — mixed footings become
  separate maps, never blended.
* **The bulk layer is a labelled uncertain overlay** (q3): the
  ``pourbaix_*`` verdict ``precis.quest.pourbaix`` harvests onto a
  candidate (plus the source job's ``meta.verdict`` grid cells) is drawn
  beside the surface envelope, never merged into it; its boundaries carry
  a few tenths of a volt of oxide-DFT error.
* **No invented sigmas**: a band whose inputs are missing is marked
  ``incomplete`` (a one-model sweep has no model-form band; an engine that
  recorded no correction set leaves the footing provenance-incomplete).

A scan is tied to a structure through the job-parent convention
``precis.quest.compute`` uses: the job is parented on the structure itself
or on a todo under it.
"""

from __future__ import annotations

import itertools
import json
import math
from typing import Any

from precis.utils import handle_registry

#: The job_type name the map reads (orchestrate the plugin by name, never
#: by import — the same convention ``precis.quest.compute`` uses).
SCAN_JOB_TYPE = "surface_coverage_scan"

#: Candidate-meta keys the bulk layer reads — stamped by
#: :func:`precis.quest.pourbaix.harvest_pourbaix_candidate`.
_BULK_META_KEYS = (
    "pourbaix_verdict",
    "pourbaix_worst_in_window",
    "pourbaix_dG_eV_atom",
    "pourbaix_domain",
    "pourbaix_basis",
    "pourbaix_job_id",
)

#: The ruling-q3 caveat the bulk layer always carries — said once here so
#: the text view and the web legend cannot drift.
BULK_LAYER_NOTE = (
    "uncertain overlay: MP-corrected bulk references; boundaries carry a "
    "few tenths of a volt of oxide-DFT error — never merged into the "
    "surface envelope"
)

#: pH span the map covers when nothing narrower is asked for. The surface
#: sweep is pH-flat on the RHE scale (pH only tilts the SHE view), so any
#: span is a display choice, not a measurement bound.
DEFAULT_PH_WINDOW = (0.0, 14.0)

#: The swept potential window (V vs RHE) — a coverage scan's default sweep
#: (``precis_pathway.coverage_job``) and the map's U axis. A potential
#: outside it is gas evolution (H2 bubbles below, O2 above), so an engine
#: ``U_opt`` out there is bounded to the nearest edge for ranking
#: (gr489166, Reto 2026-10-15: "bound").
U_RHE_WINDOW: tuple[float, float] = (-1.0, 0.5)


# ── store collectors ────────────────────────────────────────────────────


def scan_jobs(store: Any, structure_ref_id: int) -> list[dict[str, Any]]:
    """Succeeded ``surface_coverage_scan`` jobs tied to a structure, newest
    first: ``[{ref_id, model, anchor_key, engine_version, corrections,
    che_sweep}]`` (``che_sweep`` may be ``None`` on a job that predates the
    pooled-sweep stamp). The parent convention mirrors
    ``precis.quest.compute``'s candidate-job lookup: parented on the
    structure or on a todo under it."""
    return scan_jobs_many(store, [int(structure_ref_id)])[int(structure_ref_id)]


def scan_jobs_many(
    store: Any, structure_ref_ids: list[int]
) -> dict[int, list[dict[str, Any]]]:
    """:func:`scan_jobs` for many structures in two queries (jobs, then
    their tags) instead of two per structure plus one per job — the quest
    page reads every candidate (gr488208). Same rows per structure, same
    order, same 100-job cap; a structure without scans maps to ``[]``."""
    out: dict[int, list[dict[str, Any]]] = {int(i): [] for i in structure_ref_ids}
    if not out:
        return out
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT sid, ref_id, meta FROM ("
            "  SELECT COALESCE(t.parent_id, j.parent_id) AS sid, j.ref_id, j.meta,"
            "    row_number() OVER (PARTITION BY COALESCE(t.parent_id, j.parent_id)"
            "      ORDER BY j.ref_id DESC) AS rn"
            "  FROM refs j"
            "  LEFT JOIN refs t ON t.ref_id = j.parent_id AND t.kind = 'todo'"
            "    AND t.retired_at IS NULL AND t.parent_id = ANY(%(sids)s)"
            "  WHERE j.kind = 'job' AND j.retired_at IS NULL"
            "    AND j.meta->>'job_type' = %(jt)s"
            "    AND (j.parent_id = ANY(%(sids)s) OR t.ref_id IS NOT NULL)"
            ") q WHERE rn <= 100 ORDER BY sid, ref_id DESC",
            {"jt": SCAN_JOB_TYPE, "sids": list(out)},
        ).fetchall()
        job_ids = [int(r[1]) for r in rows]
        tag_rows = (
            conn.execute(
                "SELECT rt.ref_id, t.namespace, t.value "
                "FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
                "WHERE rt.ref_id = ANY(%s) AND t.namespace = 'STATUS' "
                "AND t.value = 'succeeded'",
                (job_ids,),
            ).fetchall()
            if job_ids
            else []
        )
    succeeded = {int(r[0]) for r in tag_rows}
    for sid, ref_id, meta in rows:
        if int(ref_id) not in succeeded:
            continue
        m = dict(meta or {})
        out[int(sid)].append(
            {
                "ref_id": int(ref_id),
                "model": m.get("model"),
                "anchor_key": m.get("anchor_key"),
                "engine_version": m.get("engine_version"),
                "corrections": m.get("corrections"),
                "che_sweep": m.get("che_sweep")
                if isinstance(m.get("che_sweep"), dict)
                else None,
            }
        )
    return out


#: Per-lever keys of a recorded correction block that describe one model's
#: own fit — its table entry (``sets``) or the models it had none for
#: (``unknown``/``unanchored``). They differ between models by
#: construction, so they are not part of the footing two models share;
#: whether the lever *applied* is (:func:`correction_footing`).
_PER_MODEL_CORRECTION_KEYS = frozenset({"sets", "unknown", "unanchored"})


def applied_levers(corrections: Any) -> list[str]:
    """The correction levers a scan's recorded block actually applied —
    a dict lever counts when it is enabled AND carries a set for the model
    (an enabled lever with no table entry applies nothing, gr486949); a
    legacy scalar lever (a set name or a shift) counts when present.
    ``[]`` for ``None`` (an engine that records no block)."""
    if not isinstance(corrections, dict):
        return []
    out = []
    for lever, v in corrections.items():
        if lever == "version":
            continue
        if isinstance(v, dict):
            if v.get("enabled") and v.get("sets"):
                out.append(str(lever))
        elif v is not None and v is not False:
            out.append(str(lever))
    return sorted(out)


def correction_footing(corrections: Any) -> str | None:
    """The correction footing of a recorded block, as a canonical string:
    the correction table version and, per lever, its settings and whether
    it applied — minus the per-model fit itself. Two models share a footing
    when the same corrections applied to both under the same table; a model
    the table has no entry for is on a different footing from one it has
    (ruling q2, gr489162). ``None`` for an engine that records no block.
    The coverage-scan dispatch pools on this exact key, so the stored sweep
    and the map partition agree."""
    if corrections is None:
        return None
    if not isinstance(corrections, dict):
        return json.dumps(corrections, sort_keys=True, default=str)
    out: dict[str, Any] = {}
    for lever, v in corrections.items():
        if isinstance(v, dict):
            kept = {k: x for k, x in v.items() if k not in _PER_MODEL_CORRECTION_KEYS}
            kept["applied"] = bool(v.get("enabled")) and bool(v.get("sets"))
            out[str(lever)] = kept
        else:
            out[str(lever)] = v
    return json.dumps(out, sort_keys=True, default=str)


def _footing_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """The footing a scan was measured on — one map per distinct footing,
    never blended (ruling q2). ``anchor_key`` already pins slab + anchor set
    + engine version; :func:`correction_footing` is the correction footing
    the scan recorded (``None`` on engines that apply none — a distinct
    footing from any recorded set)."""
    return (
        row.get("anchor_key"),
        row.get("engine_version"),
        correction_footing(row.get("corrections")),
    )


def _sweep_models(sweep: dict[str, Any]) -> set[str]:
    """Every model a stored sweep pooled — its ``models`` list plus the
    ``+``-joined per-anchor model labels."""
    out = {str(m) for m in sweep.get("models") or [] if m}
    for a in sweep.get("anchors") or []:
        out.update(m for m in str(a.get("model") or "").split("+") if m)
    return out


def bulk_verdict(store: Any, candidate_meta: dict[str, Any]) -> dict[str, Any] | None:
    """The ``pourbaix_bulk`` job's full ``meta.verdict`` (grid cells included)
    for the verdict stamped on the candidate, or ``None``."""
    job_id = (candidate_meta or {}).get("pourbaix_job_id")
    if not isinstance(job_id, int):
        return None
    return _verdict_of(store.fetch_refs_by_ids({job_id}).get(job_id))


def _verdict_of(job: Any) -> dict[str, Any] | None:
    """A ``pourbaix_bulk`` job ref's ``meta.verdict``, else ``None``."""
    verdict = (job.meta or {}).get("verdict") if job is not None else None
    return dict(verdict) if isinstance(verdict, dict) else None


def bulk_layer(
    candidate_meta: dict[str, Any], verdict: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The bulk-Pourbaix overlay for the map: the verdict stamped on the
    candidate (point + window), widened by the source job's grid cells when
    available. ``None`` when the candidate carries no verdict."""
    meta = candidate_meta or {}
    if not isinstance(meta.get("pourbaix_verdict"), str):
        return None
    raw_basis = meta.get("pourbaix_basis")
    basis: dict[str, Any] = raw_basis if isinstance(raw_basis, dict) else {}
    cells: list[dict[str, Any]] = []
    for c in (verdict or {}).get("grid") or []:
        if not isinstance(c, dict):
            continue
        cells.append(
            {
                "U_RHE": c.get("U_RHE"),
                "pH": c.get("pH"),
                "verdict": c.get("verdict"),
                "domain": c.get("domain"),
                "dG_pbx_eV_atom": c.get("dG_pbx_eV_atom"),
            }
        )
    return {
        "present": True,
        "point": {
            "U_RHE": basis.get("U_RHE"),
            "pH": basis.get("pH"),
            "verdict": meta.get("pourbaix_verdict"),
            "domain": meta.get("pourbaix_domain"),
            "dG_pbx_eV_atom": meta.get("pourbaix_dG_eV_atom"),
        },
        "window": basis.get("window"),
        "worst_in_window": meta.get("pourbaix_worst_in_window"),
        "cells": cells,
        "job_id": meta.get("pourbaix_job_id"),
        "matched_phase": basis.get("matched_phase"),
        "mp_version": basis.get("mp_version"),
        "ion_conc_M": basis.get("ion_conc_M"),
        "source": (verdict or {}).get("source"),
        "uncertain": True,
        "note": BULK_LAYER_NOTE,
    }


# ── the map object ──────────────────────────────────────────────────────


def _termination_label(winner: dict[str, Any] | None) -> str:
    if winner is None:
        return "clean"
    theta = winner.get("theta")
    suffix = f" at {float(theta):.2f} ML" if isinstance(theta, int | float) else ""
    return f"{winner.get('adsorbate')}*{suffix}"


def _regions(
    sweep: dict[str, Any], jobs: list[dict[str, Any]], uncorrected: bool
) -> list[dict[str, Any]]:
    """One validity region per contiguous run of the same resting
    termination along the envelope. The lower envelope of affine lines is
    convex, so each termination occupies one U interval; a repeated winner
    would still be reported as separate regions rather than merged."""
    anchor_models: dict[tuple[Any, Any], set[str]] = {}
    anchor_converged: dict[tuple[Any, Any], bool] = {}
    for a in sweep.get("anchors") or []:
        key = (a.get("adsorbate"), a.get("n"))
        anchor_models[key] = {m for m in str(a.get("model") or "").split("+") if m}
        anchor_converged[key] = bool(a.get("converged", True))
    all_models = {str(j.get("model")) for j in jobs if j.get("model")}
    jobs_by_model = {
        str(j.get("model")): int(j["ref_id"]) for j in jobs if j.get("model")
    }
    footing = "uncorrected footing" if uncorrected else "corrected footing"

    regions: list[dict[str, Any]] = []
    for row in sweep.get("envelope") or []:
        w = row.get("winner")
        term_key: tuple[Any, Any] | None = (
            None if w is None else (w.get("adsorbate"), w.get("n"))
        )
        u = float(row.get("U_RHE"))
        same = regions and regions[-1]["key"] == (list(term_key) if term_key else None)
        if same:
            regions[-1]["U_RHE_range"][1] = u
            continue
        if term_key is None:
            models = sorted(all_models)
            converged = True
            trust = f"clean-slab reference, {len(models)} model(s), {footing}"
        else:
            models = sorted(anchor_models.get(term_key) or all_models)
            converged = anchor_converged.get(term_key, True)
            trust = (
                f"{len(models)} model(s), "
                f"{'converged' if converged else 'unconverged'} anchors, "
                f"{footing}"
            )
        regions.append(
            {
                "key": list(term_key) if term_key else None,
                "termination": None if w is None else dict(w),
                "label": _termination_label(w),
                "U_RHE_range": [u, u],
                "models": models,
                "jobs": sorted(
                    {jobs_by_model[m] for m in models if m in jobs_by_model}
                ),
                "converged": converged,
                "uncorrected": uncorrected,
                "trust": trust,
            }
        )
    _snap_edges(regions, sweep.get("boundaries") or [])
    return regions


def _boundary_key(side: Any) -> list[Any] | None:
    if not isinstance(side, dict):
        return None
    return [side.get("adsorbate"), side.get("n")]


def _snap_edges(regions: list[dict[str, Any]], bnds: list[dict[str, Any]]) -> None:
    """Close the gap sampled rows leave between neighbouring regions: both
    sides of a transition share one edge, the stored closed-form crossing
    of the same two terminations (nearest the gap), else the gap's midpoint.
    Without this a U between two grid samples belongs to no region."""
    for lo_r, hi_r in itertools.pairwise(regions):
        a, b = lo_r["U_RHE_range"][1], hi_r["U_RHE_range"][0]
        mid = (a + b) / 2
        hits = [
            float(x["U_RHE"])
            for x in bnds
            if _boundary_key(x.get("below")) == lo_r["key"]
            and _boundary_key(x.get("above")) == hi_r["key"]
            and isinstance(x.get("U_RHE"), int | float)
            and math.isfinite(float(x["U_RHE"]))
        ]
        edge = min(hits, key=lambda u: abs(u - mid)) if hits else mid
        # Never past either side's own samples (a crossing inconsistent
        # with the sampled envelope would invert a one-sample region).
        edge = min(max(edge, min(a, b)), max(a, b))
        lo_r["U_RHE_range"][1] = hi_r["U_RHE_range"][0] = edge


def build_map(
    jobs: list[dict[str, Any]],
    *,
    bulk: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The resting-state map for one footing's scan jobs (``scan_jobs`` rows
    already partitioned by :func:`_footing_key`, newest first).

    The sweep is the *stored* ``meta.che_sweep`` of the newest job carrying
    one — the job already pooled every succeeded scan on the ``anchor_key``
    at dispatch. A group whose jobs all predate the sweep stamp yields a map
    with no regions and an ``incomplete`` note rather than an invented one."""
    newest = next((j for j in jobs if isinstance(j.get("che_sweep"), dict)), None)
    sweep = dict(newest["che_sweep"]) if newest else {}
    models = sorted({str(j.get("model")) for j in jobs if j.get("model")})
    corrections = newest_or_first(jobs, "corrections")
    uncorrected = not applied_levers(corrections)
    window = sweep.get("U_RHE_window") or [None, None]
    incomplete: list[str] = []
    if newest is None:
        incomplete.append("no pooled che_sweep recorded on this footing's scan jobs")
    # A sweep that pooled anchors from another footing (a scan recorded
    # under a different correction set or engine — gr489162) is not this
    # footing's map: never drawn, never blended (ruling q2). A sweep that
    # stamps its own footing key is checked against it; an older one by
    # the models it pooled.
    footing = correction_footing(corrections)
    foreign = sorted(_sweep_models(sweep) - set(models))
    if newest is not None and (foreign or sweep.get("footing_key", footing) != footing):
        incomplete.append(
            f"the stored sweep on {_job_handle(int(newest['ref_id']))} pooled "
            f"{', '.join(foreign) or 'anchors'} from another footing (a "
            "different correction set or engine) — not drawn, never blended; "
            "re-running this footing's scan stamps a sweep of its own"
        )
        sweep = {"U_RHE_window": window}
    n_models = int(sweep.get("n_models") or len(models))
    boundaries: list[dict[str, Any]] = []
    for b in sweep.get("boundaries") or []:
        prop = b.get("band_propagated")
        row = dict(b)
        row["model_form_complete"] = b.get("band_model_form") is not None
        # A missing bar is as incomplete as an infinite one (no slope gap).
        row["propagated_complete"] = isinstance(prop, int | float) and math.isfinite(
            float(prop)
        )
        boundaries.append(row)
    if boundaries and n_models < 2:
        incomplete.append(
            "model-form bands need ≥2 models — one model scanned; "
            "band_model_form is absent, not zero"
        )
    if corrections is None:
        incomplete.append(
            "footing correction set unrecorded — anchors are treated as "
            "uncorrected (the engine recorded none); never inferred"
        )
    elif uncorrected:
        incomplete.append(
            "corrections enabled but none applied — the correction table has "
            f"no set for {', '.join(models) or 'this model'}; anchors are "
            "uncorrected"
        )
    if sweep.get("sigma_e_eV") is None and newest is not None:
        incomplete.append(
            "per-anchor energy bar unrecorded — propagated bands are "
            "provenance-incomplete"
        )
    m: dict[str, Any] = {
        "anchor_key": jobs[0].get("anchor_key") if jobs else None,
        "scan_jobs": [int(j["ref_id"]) for j in jobs],
        "sweep_job": int(newest["ref_id"]) if newest else None,
        "models": models,
        "engine_version": (newest or jobs[0]).get("engine_version") if jobs else None,
        "corrections": corrections,
        "uncorrected": uncorrected,
        "pH": sweep.get("pH"),
        "prefac_V_per_pH": sweep.get("prefac_V_per_pH", 0.0592),
        "U_RHE_window": window,
        "pH_window": list(DEFAULT_PH_WINDOW),
        "grid": sweep.get("grid"),
        "sigma_e_eV": sweep.get("sigma_e_eV"),
        "n_models": n_models,
        "n_anchors": len(sweep.get("anchors") or []),
        "regions": _regions(sweep, jobs, uncorrected) if sweep.get("envelope") else [],
        "boundaries": boundaries,
        "needs_anchor": [dict(b) for b in sweep.get("needs_anchor") or []],
        "bulk": bulk,
        "incomplete": incomplete,
        "notes": list(sweep.get("notes") or []),
    }
    return m


def newest_or_first(jobs: list[dict[str, Any]], key: str) -> Any:
    """The newest job's value for ``key``, falling back to any job that
    records it (a scan whose own stamp missed the field shouldn't blank the
    footing a sibling recorded)."""
    for j in jobs:
        if j.get(key) is not None:
            return j.get(key)
    return None


def maps_for_structure(
    store: Any,
    ref: Any,
    *,
    jobs: list[dict[str, Any]] | None = None,
    verdicts: dict[int, Any] | None = None,
) -> list[dict[str, Any]]:
    """One map per footing among the structure's succeeded scans; the bulk
    layer rides on every one (it is the same bulk verdict, labelled
    uncertain on each). ``jobs`` lets a caller that already ran
    :func:`scan_jobs` pass the rows through instead of re-querying;
    ``verdicts`` (``{pourbaix job id: job ref}``) does the same for the
    bulk verdict's source job."""
    if jobs is None:
        jobs = scan_jobs(store, int(ref.id))
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in jobs:
        groups.setdefault(_footing_key(row), []).append(row)
    candidate_meta = dict(getattr(ref, "meta", None) or {})
    verdict = None
    if any(k in candidate_meta for k in _BULK_META_KEYS):
        if verdicts is None:
            verdict = bulk_verdict(store, candidate_meta)
        else:
            job_id = candidate_meta.get("pourbaix_job_id")
            if isinstance(job_id, int):
                verdict = _verdict_of(verdicts.get(job_id))
    bulk = bulk_layer(candidate_meta, verdict)
    return [build_map(group, bulk=bulk) for group in groups.values()]


def quest_maps(store: Any, quest_ref: Any) -> list[dict[str, Any]]:
    """``[{ref, handle, maps}]`` per live structure serving the quest —
    candidates without scans come back with an empty ``maps`` list rather
    than dropped, so the view can say "no anchors" by name. Scans and bulk
    verdicts are read for every candidate at once (gr488208: a quest
    carries hundreds)."""
    from precis.quest.gaps import _live_servers

    servers = [
        s for s in _live_servers(store, int(quest_ref.id)) if s.kind == "structure"
    ]
    jobs = scan_jobs_many(store, [int(s.id) for s in servers])
    job_ids = {
        jid
        for s in servers
        if isinstance(jid := (s.meta or {}).get("pourbaix_job_id"), int)
    }
    verdicts = store.fetch_refs_by_ids(job_ids) if job_ids else {}
    return [
        {
            "ref": srv,
            "handle": handle_registry.try_format("structure", srv.id)
            or f"structure:{srv.id}",
            "maps": maps_for_structure(
                store, srv, jobs=jobs[int(srv.id)], verdicts=verdicts
            ),
        }
        for srv in servers
    ]


# ── text render (llm-facing-prose; render_text's style) ─────────────────


def _job_handle(ref_id: int) -> str:
    return handle_registry.try_format("job", ref_id) or f"job:{ref_id}"


def _boundary_name(k: dict[str, Any] | None) -> str:
    return "clean" if k is None else f"{k.get('adsorbate')}*({k.get('n')})"


def render_map(m: dict[str, Any]) -> str:
    """One map as agent-readable text: provenance first, regions, both
    bands per boundary, the bulk overlay labelled, the needs_anchor ranking."""
    lines: list[str] = []
    key = str(m.get("anchor_key") or "?")
    jobs = ", ".join(_job_handle(j) for j in m.get("scan_jobs") or []) or "?"
    corr = m.get("corrections")
    if corr is None:
        corr_txt = "uncorrected (engine recorded none)"
    elif m.get("uncorrected"):
        corr_txt = "uncorrected (none applied) " + json.dumps(corr, sort_keys=True)
    else:
        corr_txt = json.dumps(corr, sort_keys=True)
    lines.append(
        f"map on anchor_key {key[:12]}… · engine {m.get('engine_version') or '?'} "
        f"· models {', '.join(m.get('models') or []) or '?'} · corrections {corr_txt}"
    )
    lines.append(
        f"scan jobs: {jobs}"
        + (
            f" (pooled sweep from {_job_handle(m['sweep_job'])})"
            if m.get("sweep_job")
            else ""
        )
    )
    lo, hi = (m.get("U_RHE_window") or [None, None])[:2]
    if lo is not None:
        lines.append(
            f"U_RHE {lo:+.2f}…{hi:+.2f} V, grid {m.get('grid')}: the sweep is "
            f"pH-flat on RHE; the SHE view tilts {float(m.get('prefac_V_per_pH') or 0.0592):.3f} V/pH "
            f"(labelled at pH {m.get('pH')}). {m.get('n_anchors')} anchor(s)."
        )
    if m.get("regions"):
        lines.append("validity regions (resting termination per U interval):")
        for r in m["regions"]:
            a, b = r["U_RHE_range"]
            lines.append(f"  {a:+.2f}…{b:+.2f} V  {r['label']}  — {r['trust']}")
    for b in m.get("boundaries") or []:
        mf = b.get("band_model_form")
        mf_txt = "n/a (<2 models) [incomplete]" if mf is None else f"±{float(mf):.3f} V"
        prop = b.get("band_propagated")
        prop_txt = "n/a" if prop is None else f"±{float(prop):.3f} V"
        lines.append(
            f"  {_boundary_name(b.get('below'))} → {_boundary_name(b.get('above'))} "
            f"at {float(b.get('U_RHE')):+.3f} V: "
            f"propagated {prop_txt}, model-form {mf_txt}."
        )
    bulk = m.get("bulk")
    if bulk:
        pt = bulk.get("point") or {}
        domain = pt.get("domain")
        dom_txt = (
            ", ".join(str(d) for d in domain)
            if isinstance(domain, list)
            else str(domain)
        )
        lines.append(
            f"bulk layer — {BULK_LAYER_NOTE}: "
            f"{pt.get('verdict')} at U_RHE {pt.get('U_RHE')} V, pH {pt.get('pH')} "
            f"(domain {dom_txt or '?'}; worst in window "
            f"{bulk.get('worst_in_window')}; "
            f"{len(bulk.get('cells') or [])} grid cell(s); "
            f"{_job_handle(bulk['job_id']) if bulk.get('job_id') else 'job ?'})."
        )
    top = (m.get("needs_anchor") or [None])[0]
    if top and top.get("band_propagated") is not None:
        lines.append(
            f"needs_anchor: widest propagated strip at {float(top.get('U_RHE')):+.3f} V "
            f"(±{float(top.get('band_propagated')):.3f} V) — the next anchor is "
            "worth most there."
        )
    if m.get("incomplete"):
        lines.append("incomplete: " + "; ".join(str(i) for i in m["incomplete"]))
    return "\n".join(lines)


def render_structure_maps(ref: Any, maps: list[dict[str, Any]]) -> str:
    """``view='pourbaix'`` body for a structure: its maps, or the no-anchor
    state with the remediation."""
    handle = (
        handle_registry.try_format("structure", int(ref.id)) or f"structure:{ref.id}"
    )
    head = (
        ref.title.splitlines()[0]
        if getattr(ref, "title", None)
        else (ref.slug or handle)
    )
    lines = [
        f"# surface-Pourbaix — {handle} {head}",
        "resting-state validity map(s): one per footing "
        "(anchor_key + engine + correction set — partitioned, never blended)",
        "",
    ]
    if not maps:
        lines.append(
            "no anchors — no succeeded surface_coverage_scan job is tied to "
            "this structure (parent the scan job on the structure or on a "
            "todo under it)."
        )
        return "\n".join(lines)
    for i, m in enumerate(maps):
        if i:
            lines.append("")
        lines.append(render_map(m))
    return "\n".join(lines)


def render_quest_maps(quest_ref: Any, entries: list[dict[str, Any]]) -> str:
    """``view='pourbaix'`` body for a quest: a map block per candidate that
    has scans, then the candidates without anchors by name."""
    head = (
        quest_ref.title.splitlines()[0]
        if getattr(quest_ref, "title", None)
        else f"quest {quest_ref.id}"
    )
    handle = (
        handle_registry.try_format("quest", int(quest_ref.id))
        or f"quest:{quest_ref.id}"
    )
    lines = [
        f"# surface-Pourbaix — {handle} {head}",
        "resting-state validity maps per candidate structure: one map per "
        "footing, mixed footings partitioned (never blended). The bulk "
        "layer is a labelled uncertain overlay, never merged.",
        "",
    ]
    anchored = [e for e in entries if e["maps"]]
    bare = [e for e in entries if not e["maps"]]
    for e in anchored:
        title = (e["ref"].title or "").splitlines()[0] if e["ref"].title else ""
        lines.append(f"## {e['handle']} {title}".rstrip())
        for m in e["maps"]:
            lines.append(render_map(m))
            lines.append("")
    if bare:
        names = ", ".join(
            f"{e['handle']} {(e['ref'].title or '').splitlines()[0] if e['ref'].title else ''}".rstrip()
            for e in bare[:20]
        )
        lines.append(f"no anchors ({len(bare)}): {names}")
    if not anchored and not bare:
        lines.append("no candidate structures serve this quest.")
    return "\n".join(lines)


__all__ = [
    "BULK_LAYER_NOTE",
    "DEFAULT_PH_WINDOW",
    "SCAN_JOB_TYPE",
    "U_RHE_WINDOW",
    "build_map",
    "bulk_layer",
    "bulk_verdict",
    "maps_for_structure",
    "quest_maps",
    "render_map",
    "render_quest_maps",
    "render_structure_maps",
    "scan_jobs",
]

"""Shared dispatch-tail helpers for the ``autocatpath_*`` job types.

Both ``autocatpath_explore`` (the legacy monolith, kept registered so old
queued rows don't error-loop — retired by the per-seed chunking fan-out)
and ``autocatpath_aggregate`` (the seed fan-out's aggregate node)
end up with the same self-contained artifact shape (graph/results/methods)
and finish identically: reduce it to the scalar summary
``quest.compute.harvest_measures`` reads, persist it onto the pathway ref,
then stamp that scalar onto the job's OWN meta. Factored here so the two
callers can't drift.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .types import PathwayArtifact

log = logging.getLogger(__name__)


def _finite_num(v: Any) -> float | None:
    """A finite float, or ``None`` (``bool`` is an ``int`` subclass but never
    a measure; a ``null`` scalar — e.g. an ungated ``P_side`` — is likewise
    ``None``, not a fabricated 0.0)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


#: Electrochemistry (CHE) scalars catpath's ``_apply_electrochemistry`` already
#: computes onto ``results_json`` top level (the potential-lever pass-through, slice 2) — a straight pass-through, no recompute here.
#: ``span_target_at_Uopt``/``T`` are deliberately excluded: diagnostics that
#: stay in ``meta.results`` (the verbatim artifact), never promoted to the
#: job-meta scalar summary.
_ELECTRO_KEYS: tuple[str, ...] = (
    "U_L",
    "U_opt",
    "span_at_UL",
    "span_at_Uopt",
    "P_side",
)


def _selectivity_scalars(results: dict[str, Any]) -> dict[str, Any]:
    """Selectivity / poisoning scalars off catpath >=0.6.0's engine scorecard
    (``results_json.score`` — ``pipeline._assemble_score``). Each margin can
    be ``None`` (no side branches / no poisons config / no off-route states),
    so every lift stays conditional — absent, not fabricated. No fallback to
    a hand-computed span-based measure when ``score`` is missing (a pre-0.6
    artifact simply yields no selectivity measures).

    * ``selectivity_margin`` (eV, maximize) — ``score.selectivity.margin_eV``:
      the worst branch-point margin (side climb minus the competing main-
      route climb at the same fork); positive = side products are
      kinetically disfavored everywhere routes diverge.
    * ``trap_margin`` (eV, maximize) — ``score.trap.margin_eV``: best-route
      span minus the worst OFF-route state's escape climb (excludes on-route
      and required states); negative = that off-route state accumulates
      (self-poisoning).
    * ``poison_margin`` (eV, maximize) — ``score.poison.margin_eV``: worst
      screened poison's mean ``delta_vs_substrate``; negative = that poison
      outcompetes the substrate for vacant sites. Same semantic as before,
      now engine-computed instead of hand-lifted from ``results.poisons``.

    Plus, from the same scorecard: ``limiting_factor`` (which axis has the
    smallest margin) and ``worst_problem`` (its one-line statement) — pure
    naming context for the tick prompt / literature step, never measures.
    """
    out: dict[str, Any] = {}
    sel = results.get("selectivity")
    if isinstance(sel, dict):
        worst = sel.get("most_competitive_side_product")
        if isinstance(worst, str) and worst:
            out["side_worst"] = worst
    poisons = results.get("poisons")
    if isinstance(poisons, dict) and isinstance(poisons.get("species"), dict):
        verdicts: dict[str, str] = {}
        for sp, d in poisons["species"].items():
            if not isinstance(d, dict):
                continue
            if isinstance(d.get("verdict"), str):
                verdicts[str(sp)] = d["verdict"]
        if verdicts:
            out["poison_verdicts"] = verdicts
    score = results.get("score")
    if isinstance(score, dict):
        sel_block = score.get("selectivity")
        if isinstance(sel_block, dict):
            v = _finite_num(sel_block.get("margin_eV"))
            if v is not None:
                out["selectivity_margin"] = v
        trap_block = score.get("trap")
        if isinstance(trap_block, dict):
            v = _finite_num(trap_block.get("margin_eV"))
            if v is not None:
                out["trap_margin"] = v
            worst_state = trap_block.get("worst")
            if isinstance(worst_state, str) and worst_state:
                out["trap_worst"] = worst_state
        poison_block = score.get("poison")
        if isinstance(poison_block, dict):
            v = _finite_num(poison_block.get("margin_eV"))
            if v is not None:
                out["poison_margin"] = v
        for k in ("limiting_factor", "worst_problem"):
            v2 = score.get(k)
            if isinstance(v2, str) and v2:
                out[k] = v2
    return out


def _kinetics_scalars(results: dict[str, Any]) -> dict[str, Any]:
    """Lift microkinetics scalars from ``results_json["kinetics"]`` (folded
    in by :func:`precis_pathway.runner.run_kinetics`, right after the
    aggregate) — same "read the already-computed artifact" move as
    :func:`_selectivity_scalars` above, for the ``solve()`` payload instead
    of the score card.

    Trust gate: a solve is trusted iff its ``tof`` is a finite float **> 0**
    (a turnover frequency can be arbitrarily small but never zero/negative —
    a non-positive value is a solver anomaly, not a dead catalyst) AND
    (there is no ``tof_bracket``, or its ``agree`` isn't literally ``False``
    — the guard against excluded-step barriers that turn out load-bearing at
    their optimistic bound). An untrusted solve still surfaces its raw
    ``tof`` (never ``log_tof``/``log_tof_p5``/``log_tof_p95`` — those stay
    gated on trust) plus a ``kinetics_note`` reason string, so the harvest
    lane (:mod:`precis.quest.compute`) can decide what to keep off the
    candidate's ranked measures.

    ``kinetics_trusted`` is emitted whenever kinetics was attempted at all —
    either a ``kinetics`` dict or a ``kinetics_error`` string is present
    (:func:`precis_pathway.runner.run_kinetics` always sets exactly one) —
    never fabricated when neither key is on ``results``.
    """
    out: dict[str, Any] = {}
    kin = results.get("kinetics")
    err = results.get("kinetics_error")
    if not isinstance(kin, dict):
        if isinstance(err, str) and err:
            out["kinetics_trusted"] = False
            out["kinetics_note"] = err
        return out

    tof = _finite_num(kin.get("tof"))
    if tof is not None:
        out["tof"] = tof
    bracket = kin.get("tof_bracket")
    bracket_ok = not (isinstance(bracket, dict) and bracket.get("agree") is False)
    trusted = tof is not None and tof > 0.0 and bracket_ok
    out["kinetics_trusted"] = trusted
    if tof is None:
        out["kinetics_note"] = "tof not finite"
    elif tof <= 0.0:
        out["kinetics_note"] = "tof non-positive (solver anomaly)"
    elif not bracket_ok:
        out["kinetics_note"] = "guard bracket: excluded steps load-bearing"
    else:
        out["log_tof"] = math.log10(max(tof, 1e-30))
        sensitivity = kin.get("sensitivity")
        band = sensitivity.get("tof") if isinstance(sensitivity, dict) else None
        if isinstance(band, dict):
            p5 = _finite_num(band.get("p5"))
            if p5 is not None and p5 > 0.0:
                out["log_tof_p5"] = math.log10(max(p5, 1e-30))
            p95 = _finite_num(band.get("p95"))
            if p95 is not None and p95 > 0.0:
                out["log_tof_p95"] = math.log10(max(p95, 1e-30))

    drc = kin.get("drc")
    x_rc = drc.get("X_RC") if isinstance(drc, dict) else None
    if isinstance(x_rc, dict):
        finite_x_rc = {k: v for k, v in x_rc.items() if _finite_num(v) is not None}
        if finite_x_rc:
            out["drc_top"] = max(finite_x_rc, key=lambda k: abs(finite_x_rc[k]))
    return out


def summarize(artifact: PathwayArtifact) -> dict[str, Any]:
    """Reduce a run artifact to the scalar summary a caller ranks on.

    Computes the rate-limiting **barrier** (eV), the energetic **span**, and
    the **low_confidence** flag from the reaction graph (``analysis.
    summarize`` — pure graph math, no ML), plus a pass-through of catpath's
    CHE electrochemistry scalars (:data:`_ELECTRO_KEYS`) already sitting on
    ``results_json``. This is the seam the precis quest harvests: it lifts
    the barrier (and now the electro scalars) onto the candidate structure's
    meta and ranks the design on it. Empty on any failure — a missing
    summary must never fail the run/persist.
    """
    try:
        from precis_pathway import analysis

        graph = artifact.get("graph_json") or {}
        results = artifact.get("results_json") or {}
        root, target = analysis.roots(graph, results)
        summ = analysis.summarize(graph, root, target)
        out: dict[str, Any] = {}
        ea = (summ.get("rate_limiting") or {}).get("ea")
        if (
            isinstance(ea, (int, float))
            and not isinstance(ea, bool)
            and math.isfinite(ea)
        ):
            out["barrier"] = float(ea)
        span = summ.get("span")
        if (
            isinstance(span, (int, float))
            and not isinstance(span, bool)
            and math.isfinite(span)
        ):
            out["span"] = float(span)
        if "low_confidence" in summ:
            out["low_confidence"] = bool(summ["low_confidence"])
        for k in _ELECTRO_KEYS:
            v = _finite_num(results.get(k))
            if v is not None:
                out[k] = v
        out.update(_selectivity_scalars(results))
        # Isolated: a kinetics-reduction bug must cost only the kinetics
        # scalars, never the long-established barrier/span/electro summary.
        try:
            out.update(_kinetics_scalars(results))
        except Exception:  # pragma: no cover - defensive
            log.warning("autocatpath: kinetics summary failed", exc_info=True)
        return out
    except Exception:  # pragma: no cover - defensive
        log.warning("autocatpath: summary failed", exc_info=True)
        return {}


def finish(
    ctx: Any,
    artifact: PathwayArtifact,
    pathway_ref_id: int,
    *,
    pathway_slug: str | None,
    produced_by: str,
    extra_meta: dict[str, Any] | None = None,
) -> bool:
    """Persist ``artifact`` onto the pathway ref and stamp the scalar
    summary onto the CALLING job's own meta — the contract
    ``quest.compute.harvest_measures`` reads. Sets ``STATUS:succeeded`` on
    success; on a persist failure, calls ``ctx.record_failure`` and returns
    False (the caller should return without touching status further).
    """
    summary = summarize(artifact)
    try:
        from precis_pathway.persist import persist_result

        pathway_extra: dict[str, Any] = {"produced_by": produced_by, "slice": 1}
        if extra_meta:
            pathway_extra.update(extra_meta)
        if "barrier" in summary:
            pathway_extra["rate_Ea"] = summary["barrier"]
        for k in ("span", "low_confidence"):
            if k in summary:
                pathway_extra[k] = summary[k]
        persist_result(
            ctx.store,
            pathway_ref_id,
            artifact,
            pathway_slug=pathway_slug,
            extra_meta=pathway_extra,
        )
    except Exception as exc:
        log.warning("%s: persist failed", produced_by, exc_info=True)
        ctx.record_failure(f"{produced_by}: persist failed: {exc}")
        return False

    r = artifact["results_json"]
    # Emit the scalar summary + the pathway ref onto the JOB meta — the
    # contract the precis quest harvest reads (barrier/span/U_L/U_opt/
    # span_at_UL/span_at_Uopt/P_side -> candidate meta -> frontier).
    ctx.set_meta(
        content_key=artifact["content_key"],
        n_states=len(r["nodes"]),
        pathway_ref=pathway_ref_id,
        **summary,
    )
    b_s = f", barrier {summary['barrier']:g} eV" if "barrier" in summary else ""
    ctx.append_chunk(
        "job_summary",
        f"{produced_by}: {len(r['nodes'])} states, {len(r['edges'])} steps "
        f"({r['n_samples']} samples, backend {r['backend']}) -> pathway "
        f"#{pathway_ref_id}{b_s}.",
    )
    ctx.set_status("succeeded")
    return True


__all__ = ["finish", "summarize"]

"""Model suitability evidence: beta-binomial rollups over logged calls.

Pure math + naming in this slice; the nightly rollup, drift decay and
card writes live in :func:`rollup` (S5 slice 6). Nothing here reads a
store, makes an LLM call, or feeds :func:`router.decide` — suitability
is dark to routing until S6.

Evidence model: per ``(model@adapter, task kind, difficulty band)`` a
Beta posterior starts from a prior set by the *best available*
capability evidence — observed telemetry outranks a measured precis
eval, which outranks published capability claims — and adds one
observation per validated production outcome. The 90% lower bound of
that posterior, plus headroom over the task's requirement, yields the
label a reviewer reads off the model card.

Thresholds are initial values (design D9) — tune them once real rows
have flowed, not before.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

__all__ = [
    "L_APPROPRIATE",
    "L_MARGINAL",
    "MIN_OBS",
    "OVERKILL_HEADROOM",
    "PRIOR_N_MEASURED",
    "PRIOR_N_PUBLISHED",
    "SuitabilityRow",
    "band_for",
    "busiest_task_kinds",
    "interval",
    "label",
    "lower_bound",
    "offering_key",
    "prior",
    "rollup",
]

#: Label thresholds on the posterior's 90% lower bound (design D9,
#: initial values). ``lower < L_MARGINAL`` → not good enough;
#: ``L_MARGINAL <= lower < L_APPROPRIATE`` → marginal; ``>= L_APPROPRIATE``
#: → appropriate (or vast overkill with headroom).
L_MARGINAL = 0.5
L_APPROPRIATE = 0.75

#: Fewer outcomes than this, with no measured eval behind the prior,
#: leaves an offering ``unqualified`` — absence of evidence, not evidence
#: of inadequacy. An *unseen* offering reads the same way.
MIN_OBS = 5

#: Headroom (offering ordinal minus required ordinal) at or above which an
#: ``appropriate`` offering is ``vast overkill`` for the task kind.
OVERKILL_HEADROOM = 2

#: Effective prior sample size: a measured precis LLM eval is stronger
#: evidence than any published capability claim.
PRIOR_N_MEASURED = 6.0
PRIOR_N_PUBLISHED = 2.0


def lower_bound(a: float, b: float, q: float = 0.10) -> float:
    """Posterior ``q``-quantile of ``Beta(a, b)`` — the conservative end.

    scipy is imported inside the function: this module's pure helpers
    (``label``, ``band_for``, ``offering_key``) stay importable without
    the numeric stack.
    """
    from scipy.stats import beta as _beta

    return float(_beta.ppf(q, a, b))


def interval(a: float, b: float) -> tuple[float, float]:
    """The 80% credible interval ``(q10, q90)`` of ``Beta(a, b)``."""
    from scipy.stats import beta as _beta

    return float(_beta.ppf(0.10, a, b)), float(_beta.ppf(0.90, a, b))


def prior(
    ordinal: int | float | None,
    required: int | float | None,
    provenance: str | None,
) -> tuple[float, float, str]:
    """Beta prior ``(a0, b0, source)`` for one offering.

    - provenance ``measured-eval`` → effective n 6 (a precis eval we ran);
    - any other provenance that isn't ``observed-telemetry`` → n 2
      (published capability — vendor's own claim);
    - ``observed-telemetry`` or a missing ordinal → ``Beta(1, 1)`` uniform
      (observed telemetry IS the outcomes, not a prior; no ordinal means
      nothing to lean on).

    The prior mean is ``0.5 + 0.1 * (ordinal - required)`` clamped to
    ``[0.05, 0.95]`` when both ordinals are known — one ordinal step is a
    ten-point nudge, never a certainty — else ``0.5``.
    """
    if provenance == "observed-telemetry" or ordinal is None:
        return 1.0, 1.0, "uniform"
    n_eff = PRIOR_N_MEASURED if provenance == "measured-eval" else PRIOR_N_PUBLISHED
    m = 0.5
    if required is not None:
        m = min(0.95, max(0.05, 0.5 + 0.1 * (ordinal - required)))
    return n_eff * m, n_eff * (1 - m), provenance or "capability"


def label(
    n_obs: int,
    has_measured: bool,
    lower: float,
    headroom: float | None,
) -> str:
    """The reviewer-facing label for one posterior.

    ``unqualified`` for thin evidence (fewer than ``MIN_OBS`` outcomes and
    no measured eval behind the prior) — unseen is unqualified, not
    inadequate. Then the 90% lower bound: below ``L_MARGINAL`` not good
    enough, below ``L_APPROPRIATE`` marginal, at or above it appropriate —
    upgraded to ``vast overkill`` when the offering's headroom over the
    task requirement is ``OVERKILL_HEADROOM`` or more.
    """
    if n_obs < MIN_OBS and not has_measured:
        return "unqualified"
    if lower < L_MARGINAL:
        return "not good enough"
    if lower < L_APPROPRIATE:
        return "marginal"
    if headroom is not None and headroom >= OVERKILL_HEADROOM:
        return "vast overkill"
    return "appropriate"


def band_for(difficulty_prior: int | None) -> str:
    """A recipe's ``difficulty_prior`` (1-5) → its rollup band.

    ``None`` (no recipe, no prior) lands in ``any`` — the bucket for
    sources whose difficulty is unknown, kept apart from a real band so a
    mid-difficulty pass never borrows a hard-task outcome.
    """
    if difficulty_prior is None:
        return "any"
    if difficulty_prior <= 2:
        return "low"
    if difficulty_prior == 3:
        return "mid"
    return "high"


def offering_key(model: str, adapter: str) -> str:
    """``model@adapter`` — the offering the posterior describes.

    S6 joins on ``Offering.model`` + ``Offering.adapter`` (the log row's
    ``transport``); account and variant are not recoverable from a log
    row, so they are not part of the key.
    """
    return f"{model}@{adapter}"


# ── the nightly rollup (slice 6) ────────────────────────────────────────


class SuitabilityRow(NamedTuple):
    """One computed posterior bucket — ``(model@adapter, kind, band)``.

    ``n`` counts validated outcomes (unweighted); ``n_eff`` is their
    drift-discounted sum. ``identity``/``drift_at`` repeat the offering's
    fingerprint on each of its buckets. ``carded`` marks whether a model
    card exists; ``written`` whether this run updated its card.
    """

    model: str
    adapter: str
    task_kind: str
    band: str
    n: int
    n_eff: float
    alpha: float
    beta: float
    lower90: float
    upper90: float
    mean: float
    label: str
    headroom: float | None
    identity: dict[str, Any] | None
    drift_at: str | None
    carded: bool
    written: bool = False


#: The window aggregate. ``ts`` must stay a bare column in the WHERE —
#: the range predicate rides ``llm_call_log_ts_idx`` (``ts DESC``,
#: migration 0061); a function wrapped around the column would force a
#: seq scan over the whole log.
_ROLLUP_SQL = (
    "SELECT model, transport, "
    "COALESCE(features->>'task_kind', 'unknown'), "
    "COALESCE(source, ''), "
    "(ts AT TIME ZONE 'UTC')::date, "
    "features->'observed'->>'model', "
    "features->'observed'->>'provider', "
    "count(*), "
    "count(*) FILTER (WHERE NOT errored AND data_parsed), "
    "count(*) FILTER (WHERE reason_class = 'unparseable') "
    "FROM llm_call_log "
    "WHERE ts > now() - (%s * interval '1 day') "
    "AND {not_gated} "
    "AND (features->>'paused') IS DISTINCT FROM 'true' "
    "AND model IS NOT NULL AND transport IS NOT NULL"
    "{model_filter} "
    "GROUP BY 1, 2, 3, 4, 5, 6, 7"
)


def _axis_evidence(value: Any) -> tuple[int | float | None, str | None]:
    """``meta.capability[axis]`` → ``(ordinal, provenance)``.

    The card stores a bare int (a claim with no tracked provenance —
    read as published-strength) or ``{score, confidence, provenance}``.
    """
    if isinstance(value, bool):
        return None, None
    if isinstance(value, int | float):
        return value, None
    if isinstance(value, dict):
        score = value.get("score")
        return (
            score
            if isinstance(score, int | float) and not isinstance(score, bool)
            else None
        ), value.get("provenance")
    return None, None


def rollup(
    store: Store,
    *,
    window_days: int = 30,
    write: bool = True,
    model: str | None = None,
) -> list[SuitabilityRow]:
    """Recompute suitability posteriors over ``llm_call_log``; optionally
    write them to model cards. Returns every bucket computed, carded or not.

    One aggregate groups the window's rows by ``model``, ``transport``
    (the adapter), task kind, source, UTC day and the observed identity
    the wire reported. A row is an *outcome* only when it was a validated
    production result: ``not errored and data_parsed`` is a success,
    ``reason_class = 'unparseable'`` a failure; every other row — quota,
    rate, budget, transport, content, unclassified — is no signal and is
    skipped. Gated rows (never ran) and paused rows (a window pause, not
    an outcome) are excluded in SQL, so they cannot move a posterior.

    Drift: per offering, the day-by-day modal ``(observed.model,
    observed.provider)`` fingerprint (rows carrying none don't vote).
    ``drift_at`` is the latest observed day whose modal differs from the
    previous observed day's; outcomes before it weigh 0.5, later ones 1.0.

    The write is idempotent: it recomputes from the window and wholesale
    replaces ``meta.suitability`` — never accumulates. A card whose
    offerings are unchanged is left untouched; one ``observed-telemetry``
    review is appended only when the (offering, kind, band, label) table
    differs from the card's last ``by='suitability'`` review. Models with
    no card are counted and logged, never an error.
    """
    from precis import llm_catalog
    from precis.utils.llm import recipes as _recipes

    model_filter = " AND model = %s" if model is not None else ""
    params: list[Any] = [window_days]
    if model is not None:
        params.append(model)
    sql = _ROLLUP_SQL.format(
        not_gated=llm_catalog._NOT_GATED, model_filter=model_filter
    )
    with store.pool.connection() as conn:
        raw = conn.execute(sql, params).fetchall()

    class _Group(NamedTuple):
        model: str
        adapter: str
        task_kind: str
        source: str
        day: date
        obs_model: str | None
        obs_provider: str | None
        n_rows: int
        n_success: int
        n_failure: int

    groups = [_Group(*r) for r in raw]

    recipe_cache: dict[str, Any] = {}

    def _recipe(source: str) -> Any:
        if source not in recipe_cache:
            recipe_cache[source] = _recipes.lookup(source)
        return recipe_cache[source]

    # ── drift + identity per offering ──
    # Day-by-day modal (obs_model, obs_provider) over rows that reported
    # an observed model; deterministic tiebreak on the fingerprint tuple.
    votes: dict[tuple[str, str], dict[date, dict[tuple[Any, Any], int]]] = {}
    for g in groups:
        if g.obs_model is None:
            continue
        per_day = votes.setdefault((g.model, g.adapter), {})
        fp = (g.obs_model, g.obs_provider)
        day_votes = per_day.setdefault(g.day, {})
        day_votes[fp] = day_votes.get(fp, 0) + g.n_rows

    offering_state: dict[
        tuple[str, str], tuple[date | None, dict[str, Any] | None]
    ] = {}
    for offer, per_day in votes.items():
        days = sorted(per_day)
        # Count, then a strings-only tie-break: a missing provider is
        # NULL in the aggregate and would TypeError a tuple compare.
        modal = {
            d: max(cnt.items(), key=lambda kv: (kv[1], kv[0][0], kv[0][1] or ""))[0]
            for d, cnt in per_day.items()
        }
        prev: tuple[Any, Any] | None = None
        drift_at: date | None = None
        for d in days:
            fp = modal[d]
            if prev is not None and fp != prev:
                drift_at = d
            prev = fp
        last = modal[days[-1]]
        offering_state[offer] = (
            drift_at,
            {"model": last[0], "provider": last[1]},
        )

    # ── accumulate outcomes into (offering, kind, band) buckets ──
    @dataclass
    class _Bucket:
        n: int = 0
        ws: float = 0.0
        wf: float = 0.0
        sources: dict[str, int] = field(default_factory=dict)

    buckets: dict[tuple[str, str, str, str], _Bucket] = {}
    for g in groups:
        if g.n_success + g.n_failure == 0:
            continue  # no-signal rows open no bucket
        rec = _recipe(g.source)
        band = band_for(rec.difficulty_prior if rec is not None else None)
        key = (g.model, g.adapter, g.task_kind, band)
        bucket = buckets.setdefault(key, _Bucket())
        drift_at, _identity = offering_state.get((g.model, g.adapter), (None, None))
        w = 0.5 if drift_at is not None and g.day < drift_at else 1.0
        bucket.n += g.n_success + g.n_failure
        bucket.ws += g.n_success * w
        bucket.wf += g.n_failure * w
        bucket.sources[g.source] = bucket.sources.get(g.source, 0) + g.n_rows

    # ── cards, posteriors, writes ──
    card_cache: dict[str, Any] = {}

    def _card(m: str) -> Any:
        if m not in card_cache:
            card_cache[m] = store.find_ref_by_meta(kind="llm", key="model_id", value=m)
        return card_cache[m]

    # Per-card assembled meta + per-row results.
    offerings_by_model: dict[str, dict[str, Any]] = {}
    pending: list[tuple[tuple[str, str, str, str], dict[str, Any]]] = []
    stamp = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    for (m, adapter, kind, band), bucket in sorted(buckets.items()):
        ref = _card(m)
        capability = (
            ((ref.meta or {}).get("capability") or {}) if ref is not None else {}
        )
        dom_src = max(bucket.sources.items(), key=lambda kv: (kv[1], kv[0]))[0]
        dom_rec = _recipe(dom_src)
        req = (dom_rec.requirement or {}) if dom_rec is not None else {}
        axis, required = req.get("axis"), req.get("min_ordinal")
        ordinal, provenance = _axis_evidence(
            capability.get(axis) if axis is not None else None
        )
        # A measured eval lifts thin evidence only on the axis the recipe
        # actually requires — eval strength elsewhere is a different task.
        has_measured = ordinal is not None and provenance == "measured-eval"
        headroom = (
            ordinal - required if ordinal is not None and required is not None else None
        )
        a0, b0, _src = prior(ordinal, required, provenance)
        a, b = a0 + bucket.ws, b0 + bucket.wf
        q10, q90 = interval(a, b)
        mean = a / (a + b)
        lab = label(bucket.n, has_measured, q10, headroom)
        drift_at, identity = offering_state.get((m, adapter), (None, None))
        cell = {
            "n": bucket.n,
            "n_eff": bucket.ws + bucket.wf,
            "alpha": a,
            "beta": b,
            "lower90": q10,
            "upper90": q90,
            "mean": mean,
            "label": lab,
            "headroom": headroom,
        }
        pending.append(((m, adapter, kind, band), cell))
        offerings = offerings_by_model.setdefault(m, {})
        okey = offering_key(m, adapter)
        entry = offerings.setdefault(
            okey,
            {
                "identity": identity,
                "drift_at": drift_at.isoformat() if drift_at is not None else None,
                "kinds": {},
            },
        )
        entry["kinds"].setdefault(kind, {})[band] = cell

    written_models: set[str] = set()
    cardless = 0
    if write:
        for m, offerings in sorted(offerings_by_model.items()):
            ref = _card(m)
            if ref is None:
                cardless += 1
                continue
            stored = ((ref.meta or {}).get("suitability") or {}).get("offerings") or {}
            if stored != offerings:
                store.update_ref(
                    ref.id,
                    meta_patch={
                        "suitability": {"offerings": offerings, "updated_at": stamp}
                    },
                )
                written_models.add(m)
            table = "\n".join(
                f"{okey} {kind} {band} {cell['label']}"
                for okey, oentry in sorted(offerings.items())
                for kind, bands in sorted(oentry["kinds"].items())
                for band, cell in sorted(bands.items())
            )
            last_text = None
            for chunk in reversed(llm_catalog.list_reviews(store, ref.id)):
                if (chunk.meta or {}).get("by") == "suitability":
                    last_text = chunk.text
                    break
            if table and table != last_text:
                llm_catalog.append_review(
                    store,
                    ref.id,
                    text=table,
                    review_type="observed-telemetry",
                    by="suitability",
                    provenance="llm_call_log",
                )
        if cardless:
            log.info(
                "suitability rollup: %d model(s) with outcomes have no llm card",
                cardless,
            )

    rows = [
        SuitabilityRow(
            model=m,
            adapter=adapter,
            task_kind=kind,
            band=band,
            n=cell["n"],
            n_eff=cell["n_eff"],
            alpha=cell["alpha"],
            beta=cell["beta"],
            lower90=cell["lower90"],
            upper90=cell["upper90"],
            mean=cell["mean"],
            label=cell["label"],
            headroom=cell["headroom"],
            identity=offerings_by_model[m][offering_key(m, adapter)]["identity"],
            drift_at=offerings_by_model[m][offering_key(m, adapter)]["drift_at"],
            carded=_card(m) is not None,
            written=m in written_models,
        )
        for (m, adapter, kind, band), cell in pending
    ]
    return rows


def busiest_task_kinds(
    store: Store, *, window_days: int = 7, limit: int = 10
) -> list[str]:
    """The ``limit`` busiest task kinds by logged call volume.

    All rows count — a kind whose calls mostly failed gates or paused is
    still busy work the reader should see. Volume only; the
    ``/suitability`` view pairs this list with the stored posteriors.
    """
    sql = (
        "SELECT COALESCE(features->>'task_kind', 'unknown'), count(*) "
        "FROM llm_call_log "
        "WHERE ts > now() - (%s * interval '1 day') "
        "GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT %s"
    )
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (window_days, limit)).fetchall()
    return [str(r[0]) for r in rows]

"""Tests for the pure half of the suitability ledger (S5, td568208).

Beta posteriors, priors by evidence provenance, labels, difficulty
bands, offering keys — no DB. Interval values were computed against the
declared scipy version, not transcribed by hand.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from precis.utils.llm.suitability import (
    L_APPROPRIATE,
    L_MARGINAL,
    MIN_OBS,
    OVERKILL_HEADROOM,
    band_for,
    interval,
    label,
    lower_bound,
    offering_key,
    prior,
)


def test_lower_bound_and_interval_match_beta_ppf() -> None:
    # Beta(10,10) is symmetric: q10 + q90 = 1.
    assert lower_bound(10, 10) == pytest.approx(0.3579298801934579)
    q10, q90 = interval(10, 10)
    assert q10 == pytest.approx(0.3579298801934579)
    assert q90 == pytest.approx(0.6420701198065421)


def test_prior_measured_eval_is_stronger_than_published() -> None:
    a_m, b_m, src_m = prior(ordinal=4, required=2, provenance="measured-eval")
    a_p, b_p, src_p = prior(ordinal=4, required=2, provenance="published")
    # Same mean (0.5 + 0.1 * 2 = 0.7), but n_eff 6 vs 2.
    assert (a_m, b_m) == (pytest.approx(4.2), pytest.approx(1.8))
    assert (a_p, b_p) == (pytest.approx(1.4), pytest.approx(0.6))
    assert a_m / (a_m + b_m) == pytest.approx(a_p / (a_p + b_p))
    assert src_m == "measured-eval"
    assert src_p == "published"


def test_prior_uniform_for_observed_telemetry_and_missing_ordinal() -> None:
    assert prior(ordinal=4, required=2, provenance="observed-telemetry") == (
        1.0,
        1.0,
        "uniform",
    )
    assert prior(ordinal=None, required=2, provenance="measured-eval") == (
        1.0,
        1.0,
        "uniform",
    )


def test_prior_mean_clamps_and_defaults() -> None:
    # Extreme ordinal gaps clamp at 0.95 / 0.05.
    a_hi, b_hi, _ = prior(ordinal=10, required=1, provenance="published")
    assert (a_hi, b_hi) == (pytest.approx(1.9), pytest.approx(0.1))
    a_lo, b_lo, _ = prior(ordinal=1, required=10, provenance="published")
    assert (a_lo, b_lo) == (pytest.approx(0.1), pytest.approx(1.9))
    # Unknown requirement → flat mean at n_eff strength.
    a_u, b_u, _ = prior(ordinal=4, required=None, provenance="published")
    assert (a_u, b_u) == (1.0, 1.0)


def test_label_unqualified_below_min_obs_without_measured() -> None:
    assert label(MIN_OBS - 1, False, 0.9, None) == "unqualified"
    assert label(0, False, 0.9, None) == "unqualified"  # unseen ≠ inadequate
    # …but a measured-eval prior qualifies the same thin row count.
    assert label(MIN_OBS - 1, True, 0.9, None) == "appropriate"
    assert label(0, True, 0.9, 0) == "appropriate"


def test_label_lower_bound_thresholds() -> None:
    # Beta(10,10) q10 0.3579 < 0.5 → not good enough.
    assert label(20, False, lower_bound(10, 10), None) == "not good enough"
    # Beta(40,20) q10 0.5878 ∈ [0.5, 0.75) → marginal.
    assert label(60, False, lower_bound(40, 20), None) == "marginal"
    # Beta(100,10) q10 0.8728 ≥ 0.75 → appropriate.
    assert label(110, False, lower_bound(100, 10), None) == "appropriate"
    # Boundary constants drive the same cut.
    assert label(10, False, L_MARGINAL, None) == "marginal"
    assert label(10, False, L_APPROPRIATE, None) == "appropriate"


def test_label_vast_overkill_needs_headroom() -> None:
    lower = lower_bound(100, 10)  # ≥ 0.75
    assert label(110, False, lower, OVERKILL_HEADROOM) == "vast overkill"
    assert label(110, False, lower, OVERKILL_HEADROOM - 1) == "appropriate"
    assert label(110, False, lower, None) == "appropriate"
    # Overkill never rescues a weak posterior.
    assert label(20, False, lower_bound(10, 10), 3) == "not good enough"


def test_band_for() -> None:
    assert band_for(None) == "any"
    assert band_for(1) == "low"
    assert band_for(2) == "low"
    assert band_for(3) == "mid"
    assert band_for(4) == "high"
    assert band_for(5) == "high"


def test_offering_key() -> None:
    assert offering_key("qwen3-32b", "openai_compat") == "qwen3-32b@openai_compat"


# ---------------------------------------------------------------------------
# rollup (DB)
# ---------------------------------------------------------------------------


def _slog(
    store: Any,
    model: str,
    *,
    source: str,
    task_kind: str = "summarize",
    days_ago: float = 0,
    errored: bool = False,
    data_parsed: bool | None = True,
    reason_class: str | None = None,
    transport: str = "openai_compat",
    observed: dict[str, Any] | None = None,
    extra_features: dict[str, Any] | None = None,
) -> None:
    """One ``llm_call_log`` row, defaulting to a validated success.

    ``days_ago`` back-dates ``ts`` so drift tests can spread rows across
    UTC days; ``observed``/``extra_features`` land inside ``features``.
    """
    from psycopg.types.json import Jsonb

    features: dict[str, Any] = {"task_kind": task_kind}
    if observed is not None:
        features["observed"] = observed
    if extra_features:
        features.update(extra_features)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO llm_call_log "
            "(source, tier, transport, model, errored, data_parsed, "
            " reason_class, features, ts) "
            "VALUES (%s, 'frontier', %s, %s, %s, %s, %s, %s, "
            "now() - (%s * interval '1 day'))",
            (
                source,
                transport,
                model,
                errored,
                data_parsed,
                reason_class,
                Jsonb(features),
                days_ago,
            ),
        )


def _card(store: Any, model: str, **kw: Any) -> Any:
    from precis import llm_catalog

    rid, _ = llm_catalog.upsert_card(store, model_id=model, text="A model.", **kw)
    return rid


def _suit_meta(store: Any, model: str) -> dict[str, Any]:
    ref = store.find_ref_by_meta(kind="llm", key="model_id", value=model)
    assert ref is not None
    return (ref.meta or {}).get("suitability") or {}


def _cell(store: Any, model: str, kind: str = "summarize", band: str = "any") -> Any:
    offerings = _suit_meta(store, model).get("offerings") or {}
    key = f"{model}@openai_compat"
    return offerings[key]["kinds"][kind][band]


def test_five_successes_qualify_an_offering(store: Any) -> None:
    from precis.utils.llm.suitability import rollup

    tag = uuid4().hex[:8]
    model, src = f"suit-m-{tag}", f"suit-{tag}"
    _card(store, model)
    for _ in range(5):
        _slog(store, model, source=src)
    rows = rollup(store)
    row = next(r for r in rows if r.model == model)
    assert row.n == 5
    assert row.label == "marginal"  # Beta(6,1) q10 ≈ 0.68 — qualified, not yet strong
    assert row.carded and row.written
    cell = _cell(store, model)
    assert cell["n"] == 5 and cell["label"] == "marginal"
    assert cell["alpha"] == pytest.approx(6.0)  # Beta(1,1) prior + 5 successes


def test_four_successes_leave_it_unqualified(store: Any) -> None:
    from precis.utils.llm.suitability import rollup

    tag = uuid4().hex[:8]
    model, src = f"suit-m-{tag}", f"suit-{tag}"
    _card(store, model)
    for _ in range(4):
        _slog(store, model, source=src)
    rollup(store)
    assert _cell(store, model)["label"] == "unqualified"


def test_no_signal_rows_create_no_bucket(store: Any) -> None:
    """Only a validated outcome opens a (kind, band) cell — quota, rate,
    budget, transport, content and unclassified rows never do, even on a
    card backed by a measured eval."""
    from precis.utils.llm.suitability import rollup

    tag = uuid4().hex[:8]
    model, src = f"suit-m-{tag}", f"suit-{tag}"
    _card(
        store,
        model,
        capability={"code": {"score": 5, "provenance": "measured-eval"}},
    )
    for rc in ("quota", "rate", "budget", "transport", "content"):
        _slog(
            store,
            model,
            source=src,
            errored=True,
            data_parsed=None,
            reason_class=rc,
        )
    # Unclassified error and a prose caller (no parse verdict) — also no signal.
    _slog(store, model, source=src, errored=True, data_parsed=None)
    _slog(store, model, source=src, data_parsed=None)
    rows = rollup(store)
    assert all(r.model != model for r in rows)
    assert _suit_meta(store, model) == {}


def test_measured_eval_applies_only_on_the_requirement_axis(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A measured-eval ordinal lifts ``unqualified`` only on the axis the
    recipe's requirement names — a measured eval elsewhere does not."""
    from psycopg.types.json import Jsonb

    from precis import route_log
    from precis.utils.llm import recipes
    from precis.utils.llm.suitability import rollup

    tag = uuid4().hex[:8]
    src = f"suit-axis-{tag}"
    model_same, model_other = f"suit-same-{tag}", f"suit-other-{tag}"
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO task_recipes (source, task_kind, consequence, "
            "requirement) VALUES (%s, 'test.axis', 'medium', %s)",
            (src, Jsonb({"axis": "summarize-extract", "min_ordinal": 3})),
        )
    try:
        route_log.bind_store(store)
        # The 300 s recipes cache predates the insert — force a reload.
        monkeypatch.setattr(recipes, "_expires", 0.0)
        monkeypatch.setattr(recipes, "_cache", {})
        _card(
            store,
            model_same,
            capability={
                "summarize-extract": {"score": 5, "provenance": "measured-eval"}
            },
        )
        _card(
            store,
            model_other,
            capability={"code": {"score": 5, "provenance": "measured-eval"}},
        )
        for m in (model_same, model_other):
            for _ in range(2):
                _slog(store, m, source=src)
        rows = {r.model: r for r in rollup(store)}
        assert rows[model_same].label != "unqualified"
        assert rows[model_other].label == "unqualified"
    finally:
        route_log.bind_store(None)
        with store.pool.connection() as conn:
            conn.execute("DELETE FROM task_recipes WHERE source = %s", (src,))


def test_no_signal_rows_never_move_a_posterior(store: Any) -> None:
    from precis.utils.llm.suitability import rollup

    tag = uuid4().hex[:8]
    model, src = f"suit-m-{tag}", f"suit-{tag}"
    _card(store, model)
    for _ in range(6):
        _slog(store, model, source=src)
    rollup(store)
    before = _suit_meta(store, model)["offerings"]

    for rc in ("quota", "rate", "budget", "transport", "content"):
        _slog(
            store,
            model,
            source=src,
            errored=True,
            data_parsed=None,
            reason_class=rc,
        )
    _slog(store, model, source=src, extra_features={"gated": True})
    _slog(
        store,
        model,
        source=src,
        errored=True,
        data_parsed=None,
        extra_features={"paused": True},
    )
    rollup(store)
    assert _suit_meta(store, model)["offerings"] == before


def test_gate_verdict_rows_count_as_outcomes(store: Any) -> None:
    """gr595543: the attempts ledger's hard-gate verdict IS the outcome —
    ``gate_verdict='pass'`` a success (even on a lite row with
    ``data_parsed`` unset), ``'rejected'`` a failure even when the reply
    otherwise ``data_parsed``. Where the small rung passes ~90% you start
    small; ~20% you skip to the middle tier."""
    from precis.utils.llm.suitability import rollup

    tag = uuid4().hex[:8]
    model, src = f"suit-m-{tag}", f"suit-{tag}"
    _card(store, model)
    # 4 gated passes on lite rows (data_parsed NULL), 1 ordinary success,
    # 1 rejection that "parsed" — 5 successes / 1 failure of 6 rows.
    for _ in range(4):
        _slog(
            store,
            model,
            source=src,
            data_parsed=None,
            extra_features={"gate_verdict": "pass"},
        )
    _slog(store, model, source=src)
    _slog(store, model, source=src, extra_features={"gate_verdict": "rejected"})

    rows = rollup(store)
    row = next(r for r in rows if r.model == model)
    assert row.n == 6
    cell = _cell(store, model)
    assert cell["alpha"] == pytest.approx(1.0 + 5)  # Beta(1,1) + 5 successes
    assert cell["beta"] == pytest.approx(1.0 + 1)


def test_drift_halves_pre_drift_outcomes(store: Any) -> None:
    from precis.utils.llm.suitability import rollup

    tag = uuid4().hex[:8]
    model, src = f"suit-m-{tag}", f"suit-{tag}"
    plain = f"suit-plain-{tag}"
    _card(store, model)
    _card(store, plain)
    # 10 days of fingerprint A, then 2 of B — the drift day is the first B day.
    for d in range(12, 2, -1):
        _slog(
            store,
            model,
            source=src,
            days_ago=d,
            observed={"model": "wire-a", "provider": "p"},
        )
        _slog(store, plain, source=src, days_ago=d)
    for d in (2, 1):
        _slog(
            store,
            model,
            source=src,
            days_ago=d,
            observed={"model": "wire-b", "provider": "p"},
        )
        _slog(store, plain, source=src, days_ago=d)
    rows = {r.model: r for r in rollup(store)}
    drifted, undrifted = rows[model], rows[plain]
    assert drifted.drift_at is not None
    assert drifted.identity == {"model": "wire-b", "provider": "p"}
    assert drifted.n == undrifted.n == 12
    assert drifted.n_eff == pytest.approx(10 * 0.5 + 2 * 1.0)
    assert undrifted.n_eff == pytest.approx(12.0)
    assert drifted.upper90 - drifted.lower90 > undrifted.upper90 - undrifted.lower90
    assert undrifted.identity is None and undrifted.drift_at is None


def test_secondary_model_variation_is_not_drift(store: Any) -> None:
    """``observed.models`` is informational — the fingerprint rides on
    ``observed.model`` alone, so a day differing only in a small
    secondary-model call sets no ``drift_at``."""
    from precis.utils.llm.suitability import rollup

    tag = uuid4().hex[:8]
    model, src = f"suit-m-{tag}", f"suit-{tag}"
    _card(store, model)
    _slog(
        store,
        model,
        source=src,
        days_ago=2,
        observed={"model": "wire-main", "models": ["wire-main"]},
    )
    _slog(
        store,
        model,
        source=src,
        days_ago=1,
        observed={"model": "wire-main", "models": ["wire-main", "wire-side"]},
    )
    rows = {r.model: r for r in rollup(store)}
    assert rows[model].identity == {"model": "wire-main", "provider": None}
    assert rows[model].drift_at is None


def test_drift_tie_break_compares_strings_only(store: Any) -> None:
    """A provider-NULL fingerprint tied with a string-provider one must
    not crash the modal pick — ``None`` never reaches the comparison."""
    from precis.utils.llm.suitability import rollup

    tag = uuid4().hex[:8]
    model, src = f"suit-m-{tag}", f"suit-{tag}"
    _card(store, model)
    for _ in range(2):
        _slog(store, model, source=src, observed={"model": "wire-a"})
        _slog(
            store,
            model,
            source=src,
            observed={"model": "wire-a", "provider": "p"},
        )
    rows = {r.model: r for r in rollup(store)}
    assert rows[model].identity == {"model": "wire-a", "provider": "p"}


def test_rollup_is_idempotent(store: Any) -> None:
    from precis import llm_catalog
    from precis.utils.llm.suitability import rollup

    tag = uuid4().hex[:8]
    model, src = f"suit-m-{tag}", f"suit-{tag}"
    rid = _card(store, model)
    for _ in range(5):
        _slog(store, model, source=src)
    rollup(store)
    first_meta = _suit_meta(store, model)
    n_reviews = len(llm_catalog.list_reviews(store, rid))
    second = rollup(store)
    assert all(not r.written for r in second)
    assert _suit_meta(store, model)["offerings"] == first_meta["offerings"]
    assert len(llm_catalog.list_reviews(store, rid)) == n_reviews
    # …and the first run left exactly one suitability review behind.
    reviews = [
        r
        for r in llm_catalog.list_reviews(store, rid)
        if r.meta.get("by") == "suitability"
    ]
    assert len(reviews) == 1


def test_cardless_model_is_counted_not_an_error(store: Any) -> None:
    from precis.utils.llm.suitability import rollup

    tag = uuid4().hex[:8]
    model, src = f"suit-none-{tag}", f"suit-{tag}"
    for _ in range(5):
        _slog(store, model, source=src)
    rows = rollup(store)
    row = next(r for r in rows if r.model == model)
    assert row.carded is False and row.written is False
    assert row.label == "marginal"


def test_window_aggregate_filters_on_the_ts_index(store: Any) -> None:
    """The window predicate is a plain range on the ``ts`` column — with
    seq scans disabled the planner serves it from
    ``llm_call_log_ts_idx`` (migration 0061). A function wrapped around
    the column would leave a seq scan in this plan."""
    from precis.utils.llm import suitability

    sql = suitability._ROLLUP_SQL.format(not_gated="TRUE", model_filter="")
    with store.pool.connection() as conn:
        try:
            conn.execute("SET enable_seqscan = off")
            plan = "\n".join(
                str(r[0]) for r in conn.execute(f"EXPLAIN {sql}", (30,)).fetchall()
            )
        finally:
            conn.execute("RESET enable_seqscan")
    assert "llm_call_log_ts_idx" in plan


# ---------------------------------------------------------------------------
# factory build runs (gr761723)
# ---------------------------------------------------------------------------


def _build(
    store: Any,
    model: str | None,
    *,
    lane: str = "devin",
    adapter: str = "devin_tree",
    review: str | None = None,
    land: str | None = None,
    gate_pass: bool | None = None,
    explore: bool = False,
    task_class: str = "code",
    run_outcome: str | None = None,
) -> int:
    """One build: a todo plus its ``run_slice`` job carrying the submit's
    stamps, then the review / land signals the tick and the lander write."""
    from precis.plans import outcome
    from precis.workers._mint import mint_background_job

    todo = store.insert_ref(
        kind="todo", slug=None, title="build", meta={"task_class": task_class}
    )
    extra: dict[str, Any] = {"lane": lane, "adapter": adapter}
    if model is not None:
        extra["model"] = model
    if explore:
        extra["explore"] = True
    if gate_pass is not None:
        extra["outcome"] = {"gate_pass": gate_pass}
        if run_outcome is not None:
            extra["outcome"]["outcome"] = run_outcome
    with store.tx() as conn:
        job = mint_background_job(
            store,
            conn=conn,
            job_type="run_slice",
            executor="ssh_node",
            title="run_slice",
            params={"todo_id": int(todo.id)},
            parent_id=int(todo.id),
            status="succeeded",
            meta_extra=extra,
        )
    if review:
        outcome.record_build_signal(store, int(job.id), "review", review, by_job=1)
    if land:
        outcome.record_build_signal(store, int(job.id), "land", land, by_job=2)
    return int(job.id)


def test_rollup_counts_review_and_land_once_per_build(store: Any) -> None:
    """Each build adds ONE outcome for its model: LAND then landed is one
    success, FIX one failure, LAND then a red land one failure, a gate
    failure one failure, a gate pass alone nothing; a re-review rewrites
    the run's signal instead of adding a second outcome."""
    from precis.plans import outcome
    from precis.utils.llm.suitability import rollup

    model = f"swe-x-{uuid4().hex[:8]}"
    landed = _build(store, model, review="LAND", land="landed")
    _build(store, model, review="FIX")
    _build(store, model, review="LAND", land="red")
    _build(store, model, gate_pass=False)
    _build(store, model, gate_pass=True)
    _build(store, model, task_class="docs", gate_pass=False)  # not a build
    outcome.record_build_signal(store, landed, "review", "LAND", by_job=3)
    [row] = rollup(store, write=False, model=model)
    assert (row.adapter, row.n) == ("devin_tree", 4)
    # Beta(1, 1) prior + 1 success, 3 failures.
    assert (row.alpha, row.beta) == (pytest.approx(2.0), pytest.approx(4.0))
    assert row.label == "unqualified"  # 4 outcomes, under MIN_OBS


def test_build_explore_counts_feed_the_exploration_pick(store: Any) -> None:
    """The pick's counts: validated build outcomes per model on one
    adapter and task kind; a run with no signal and an unrecorded model
    key as documented."""
    from precis.utils.llm.suitability import UNNAMED_MODEL, build_explore_counts

    tag = uuid4().hex[:8]
    adapter = f"adapter_{tag}"
    a, b = f"m-a-{tag}", f"m-b-{tag}"
    job = _build(store, a, adapter=adapter, review="LAND")
    _build(store, a, adapter=adapter, land="red")
    _build(store, b, adapter=adapter, gate_pass=True)  # no signal yet
    _build(store, None, adapter=adapter, review="FIX")
    kind = str(store.get_ref(kind="job", id=job).meta["task_kind"])
    counts, tried = build_explore_counts(
        store, adapter=adapter, task_kind=kind, source="code"
    )
    assert counts == {a: 2, UNNAMED_MODEL: 1}
    assert tried == {}


def test_build_runs_prunes_jobs_below_the_ref_id_floor(store: Any) -> None:
    """Separate the id floor from the time filter with out-of-order dates."""
    from precis.utils.llm.suitability import build_runs

    adapter = f"adapter_{uuid4().hex[:8]}"
    below = _build(store, "below-floor", adapter=adapter)
    floor = int(store.insert_ref(kind="todo", slug=None, title="floor", meta={}).id)
    expired = _build(store, "expired", adapter=adapter)
    recent = _build(store, "recent", adapter=adapter)
    assert below < floor < expired < recent

    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '32 days' WHERE ref_id = %s",
            (floor,),
        )
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '30 days 1 hour' "
            "WHERE ref_id = %s",
            (expired,),
        )
    try:
        runs = build_runs(store, window_days=30, adapter=adapter)
    finally:
        with store.tx() as conn:
            conn.execute(
                "UPDATE refs SET created_at = now() WHERE ref_id IN (%s, %s)",
                (floor, expired),
            )
    assert [run.model for run in runs] == ["recent"]


def test_build_runs_window_keeps_builds_under_an_older_row_on_a_newer_id(
    store: Any,
) -> None:
    """td763090: ``created_at`` is the transaction's start and the id the
    insert's draw, so a transaction open across the cutoff leaves a
    pre-cutoff row on an id above in-window builds. The pruning floor
    must not take that row and hide them: the window is ``created_at``."""
    from precis.utils.llm.suitability import build_runs

    tag = uuid4().hex[:8]
    adapter = f"adapter_{tag}"
    job = _build(store, f"m-{tag}", adapter=adapter, review="LAND")
    late = int(store.insert_ref(kind="todo", slug=None, title="late", meta={}).id)
    assert late > job
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '29 days' WHERE ref_id = %s",
            (job,),
        )
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '30 days 1 hour' "
            "WHERE ref_id = %s",
            (late,),
        )
    try:
        runs = build_runs(store, window_days=30, adapter=adapter)
    finally:
        with store.tx() as conn:
            conn.execute(
                "UPDATE refs SET created_at = now() WHERE ref_id = %s", (late,)
            )
    assert [r.outcome for r in runs] == [True]


def test_explore_counts_count_tries_without_outcomes(store: Any) -> None:
    """The pick's try bound reads explored runs whether or not they
    yielded an outcome: ten explored runs that ended empty (gates passed,
    no review, no land) are ten tries and zero outcomes — an empty run is
    not a failure."""
    from precis.utils.llm.suitability import build_explore_counts

    tag = uuid4().hex[:8]
    adapter, m = f"adapter_{tag}", f"m-{tag}"
    jobs = [
        _build(
            store,
            m,
            adapter=adapter,
            explore=True,
            gate_pass=True,
            run_outcome="completed_empty",
        )
        for _ in range(10)
    ]
    _build(store, m, adapter=adapter, review="LAND")  # not an exploration
    kind = str(store.get_ref(kind="job", id=jobs[0]).meta["task_kind"])
    outcomes, tried = build_explore_counts(
        store, adapter=adapter, task_kind=kind, source="code"
    )
    assert tried == {m: 10}
    assert outcomes == {m: 1}


def test_lane_model_table_shows_counts_and_the_exploration_share(
    store: Any,
) -> None:
    from precis.cli.llm import _lane_model_lines
    from precis.utils.llm.suitability import lane_model_outcomes

    tag = uuid4().hex[:8]
    lane, m = f"lane-{tag}", f"m-{tag}"
    _build(store, m, lane=lane, review="LAND", explore=True)
    _build(store, m, lane=lane, review="FIX")
    _build(store, m, lane=lane, gate_pass=True)
    _build(store, m, lane=lane, land="landed")
    [row] = [r for r in lane_model_outcomes(store) if r.lane == lane]
    assert (row.model, row.runs, row.explored, row.success, row.failure) == (
        m,
        4,
        1,
        2,
        1,
    )
    [line] = [ln for ln in _lane_model_lines(store, window_days=30) if lane in ln]
    assert f"{lane} {m}@devin_tree: builds=4 explored=1 (25%)" in line
    assert f"{row.task_kind} ok=2 fail=1" in line


def test_run_llm_suitability_and_cadence(store: Any) -> None:
    tag = uuid4().hex[:8]
    model, src = f"suit-m-{tag}", f"suit-{tag}"
    _card(store, model)
    for _ in range(5):
        _slog(store, model, source=src)
    from precis.workers.llm_suitability import run_llm_suitability

    assert run_llm_suitability(store) == 1
    assert run_llm_suitability(store) == 0  # nothing changed → nothing written

    from precis.workers.scheduler import CADENCES

    cad = next(c for c in CADENCES if c.name == "llm_suitability")
    assert cad.interval_s == 24 * 3600
    assert cad.spends is False

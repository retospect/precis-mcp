"""The `llm` catalog writer + capability vocabulary (docs/proposals/llm-catalog.md).

One writer for the model-card upsert, shared by the :class:`~precis.handlers.llm.LlmHandler`
(the MCP surface) and :mod:`precis.workers.llm_reconcile` (the reconcile pass) — the
same split ``precis.quest.logbook`` gives the quest handler + tick. Keeping it here (a
store-level module, not the handler) lets a background pass mint/refresh a card without a
hub, and keeps the validation + card-emission in one place.

A model card is a numeric ``kind='llm'`` ref: ``title`` = the capability prose (embedded
as ``card_combined`` so the card is a vector), ``meta`` = the structured facts:

* ``model_id``    — the canonical model slug, the human key ``get(kind='llm',
                    id='claude-opus-4-8')`` resolves (via ``store.find_ref_by_meta``).
* ``tier_floor``  — the ``Tier`` this model backstops (the degrade-to-floor).
* ``offerings``   — operating points ``[{effort, transport, endpoint, max_input,
                    max_output, price_in, price_out, quant}]``; effort/window are axes
                    *within* a card, not a row explosion.
* ``capability``  — coarse **1–5 ordinal** axes (:data:`CAPABILITY_AXES`), each a bare int
                    or ``{score, confidence, provenance}``.
* ``provenance``  — where the facts came from + when reconciled.

``upsert_card`` is idempotent on ``model_id``: first sight inserts + emits the card;
later sights merge the fresh facts into ``meta`` (``update_ref`` does ``meta || patch``)
and re-emit the card so a changed capability prose re-embeds. Slice 1 writes facts; the
review-log (``llm_review`` chunks) + the ``llm_call_log`` tote arrive in slice 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from precis.errors import BadInput
from precis.store import Store
from precis.store.types import Block, BlockInsert

#: The workload-relevant capability axes — chosen by what precis *does*, not what
#: academia measures. Each is a coarse 1–5 ordinal (not a continuous scalar). The set
#: is provisional: slice 3 confirms it survives contact with the golden sets (a 6th —
#: multilingual? vision? — may be added). See the proposal.
CAPABILITY_AXES: tuple[str, ...] = (
    "code",
    "long-context-recall",
    "tool-structured",
    "reasoning-convergence",
    "summarize-extract",
)

#: Keys an offering (operating point) may carry. Prices are USD per 1M tokens (the
#: ``PRICE_TABLE`` convention); ``max_input`` / ``max_output`` are token windows.
OFFERING_KEYS: frozenset[str] = frozenset(
    {
        "effort",
        "transport",
        "endpoint",
        "max_input",
        "max_output",
        "price_in",
        "price_out",
        "quant",
        # slice 7 / §6: which hosts serve this offering LOCALLY, and how many
        # concurrent calls each admits. Seeds ``resource_slots`` (the
        # ``max_parallel`` IS the slot capacity); a capability-gated inline-LLM
        # pass reserves a local slot before calling localhost. Absent on
        # external-service offerings (OpenRouter / anthropic — no reservation).
        "served_by",
    }
)

#: Keys one ``served_by`` entry (a local-serving host for an offering) may carry.
#: ``model`` is the SERVER-SIDE model id the endpoint expects (a llama-swap alias
#: or ollama tag) — distinct from the card's ``model_id`` (the precis-side handle a
#: tier resolves to). ``local_serving`` reads it as ``served_model`` and folds it
#: into the dispatch, defaulting to ``model_id`` when absent.
SERVED_BY_KEYS: frozenset[str] = frozenset(
    {"host", "endpoint", "max_parallel", "model"}
)

#: Keys a reconciled **endpoint** (a bookable provider×quant variant, minted by
#: ``llm_reconcile`` from OpenRouter ``/models/{slug}/endpoints``) may carry. An
#: endpoint is the variant-precise unit the ``provider:{order,quantizations}`` pin
#: books (gripe 162624): one OpenRouter slug fans out to ~28 of these, differing
#: by provider / quant (fp4≠fp8) / window (1M..101k) / price — so capability and
#: price are *endpoint*-scoped, not card-scoped. ``meta.endpoints`` is machine-
#: maintained (reconcile rewrites it wholesale each pass) and kept **separate**
#: from the curated ``meta.offerings`` (the operating points the policy ranks):
#: reconcile never clobbers a seeded offering. ``capability`` is an optional
#: per-variant ordinal block (a fp8/1M endpoint scores differently than fp4/101k);
#: ``status`` / ``uptime_1d`` are the availability telemetry a booking consults.
ENDPOINT_KEYS: frozenset[str] = frozenset(
    {
        "provider",
        "quant",
        "tag",
        "max_input",
        "max_output",
        "price_in",
        "price_out",
        "tools",
        "structured",
        "status",
        "uptime_1d",
        "capability",
    }
)

LLM_KIND = "llm"


def _validate_offerings(offerings: Any) -> list[dict[str, Any]]:
    if not isinstance(offerings, list):
        raise BadInput(
            "offerings= must be a list of operating-point dicts",
            next="each is {effort, transport, endpoint, max_input, max_output, "
            "price_in, price_out, quant}",
        )
    out: list[dict[str, Any]] = []
    for o in offerings:
        if not isinstance(o, dict):
            raise BadInput("each offering must be a dict")
        unknown = set(o) - OFFERING_KEYS
        if unknown:
            raise BadInput(
                f"unknown offering key(s) {sorted(unknown)}",
                options=sorted(OFFERING_KEYS),
            )
        if o.get("served_by") is not None:
            _validate_served_by(o["served_by"])
        out.append(o)
    return out


def _validate_served_by(served_by: Any) -> list[dict[str, Any]]:
    """A ``served_by`` list — each entry a local-serving host. ``host`` is
    required; ``max_parallel`` (if given) is a positive int (the slot capacity);
    ``model`` (if given) is a non-empty server-side model id (defaults to the
    card's ``model_id``). Returns the validated list."""
    if not isinstance(served_by, list):
        raise BadInput(
            "served_by must be a list of {host, endpoint, max_parallel, model} dicts"
        )
    for e in served_by:
        if not isinstance(e, dict):
            raise BadInput("each served_by entry must be a dict")
        unknown = set(e) - SERVED_BY_KEYS
        if unknown:
            raise BadInput(
                f"unknown served_by key(s) {sorted(unknown)}",
                options=sorted(SERVED_BY_KEYS),
            )
        if not e.get("host") or not isinstance(e["host"], str):
            raise BadInput("served_by entry requires a non-empty string host")
        mp = e.get("max_parallel")
        if mp is not None and (
            not isinstance(mp, int) or isinstance(mp, bool) or mp < 1
        ):
            raise BadInput(f"served_by max_parallel must be a positive int, got {mp!r}")
        model = e.get("model")
        if model is not None and (not isinstance(model, str) or not model):
            raise BadInput(
                "served_by model must be a non-empty string (the server-side model id)"
            )
    return served_by


def _validate_capability(capability: Any) -> dict[str, Any]:
    if not isinstance(capability, dict):
        raise BadInput(
            "capability= must be a dict axis→ordinal (1..5)",
            options=list(CAPABILITY_AXES),
        )
    for axis, val in capability.items():
        if axis not in CAPABILITY_AXES:
            raise BadInput(
                f"unknown capability axis {axis!r}",
                options=list(CAPABILITY_AXES),
            )
        # A value is a bare ordinal or a {score, confidence, provenance} envelope.
        score = val.get("score") if isinstance(val, dict) else val
        if not isinstance(score, int) or isinstance(score, bool) or not 1 <= score <= 5:
            raise BadInput(
                f"capability axis {axis!r} score must be an int 1..5, got {score!r}"
            )
    return capability


def _validate_endpoints(endpoints: Any) -> list[dict[str, Any]]:
    if not isinstance(endpoints, list):
        raise BadInput(
            "endpoints= must be a list of bookable-variant dicts",
            next="each is {provider, quant, max_input, price_in, price_out, ...}",
        )
    out: list[dict[str, Any]] = []
    for e in endpoints:
        if not isinstance(e, dict):
            raise BadInput("each endpoint must be a dict")
        unknown = set(e) - ENDPOINT_KEYS
        if unknown:
            raise BadInput(
                f"unknown endpoint key(s) {sorted(unknown)}",
                options=sorted(ENDPOINT_KEYS),
            )
        if e.get("capability") is not None:
            _validate_capability(e["capability"])
        out.append(e)
    return out


def _validate_params(params: Any) -> dict[str, Any]:
    """The model's static shape (size / architecture / license) — free-form
    prose-grade facts, so only the container is checked. OpenRouter's feed
    carries no param count, so ``params`` is seeded from curated knowledge and
    left for a human/reconcile to enrich."""
    if not isinstance(params, dict):
        raise BadInput("params= must be a dict (size / arch / license facts)")
    return params


def build_meta(
    *,
    model_id: str,
    tier_floor: str | None = None,
    offerings: Any = None,
    endpoints: Any = None,
    capability: Any = None,
    params: Any = None,
    served_by: Any = None,
    provenance: Any = None,
) -> dict[str, Any]:
    """Validate + assemble the ``meta`` patch for a model card.

    Only the keys that are supplied land in the patch — the update path merges
    (``meta || patch``), so a reconcile refreshing just prices passes only
    ``offerings`` and leaves ``capability`` untouched. ``model_id`` is always
    present (it is the identity + lookup key).

    ``served_by`` is the CARD-LEVEL local-serving declaration (the same shape as
    an offering's ``served_by``); ``local_serving`` reads both card- and
    offering-level entries. Set it to route a whole card to a local endpoint.
    """
    if not model_id or not isinstance(model_id, str):
        raise BadInput("model_id must be a non-empty string (the canonical model slug)")
    meta: dict[str, Any] = {"model_id": model_id}
    if tier_floor is not None:
        meta["tier_floor"] = str(tier_floor)
    if offerings is not None:
        meta["offerings"] = _validate_offerings(offerings)
    if endpoints is not None:
        meta["endpoints"] = _validate_endpoints(endpoints)
    if capability is not None:
        meta["capability"] = _validate_capability(capability)
    if params is not None:
        meta["params"] = _validate_params(params)
    if served_by is not None:
        meta["served_by"] = _validate_served_by(served_by)
    if provenance is not None:
        meta["provenance"] = provenance
    return meta


def upsert_card(
    store: Store,
    *,
    model_id: str,
    text: str,
    tier_floor: str | None = None,
    offerings: Any = None,
    endpoints: Any = None,
    capability: Any = None,
    params: Any = None,
    served_by: Any = None,
    provenance: Any = None,
) -> tuple[int, bool]:
    """Create or refresh the ``llm`` card for ``model_id``. Returns ``(ref_id, created)``.

    Idempotent on ``model_id``: a first sight inserts the ref + emits the embeddable
    ``card_combined``; a later sight merges the fresh facts into ``meta`` and re-emits
    the card (so a changed prose re-embeds). The card body (``refs.title``) is a scalar
    column, not an append-only body chunk, so refreshing it is a plain UPDATE.
    """
    if not text or not text.strip():
        raise BadInput("an llm card requires text= (the capability prose)")
    meta = build_meta(
        model_id=model_id,
        tier_floor=tier_floor,
        offerings=offerings,
        endpoints=endpoints,
        capability=capability,
        params=params,
        served_by=served_by,
        provenance=provenance,
    )
    existing = store.find_ref_by_meta(kind=LLM_KIND, key="model_id", value=model_id)
    if existing is None:
        with store.tx() as conn:
            ref = store.insert_ref(
                kind=LLM_KIND, slug=None, title=text, meta=meta, conn=conn
            )
            store.upsert_card_combined(ref.id, text, conn=conn)
        return ref.id, True
    store.update_ref(existing.id, title=text, meta_patch=meta)
    with store.tx() as conn:
        store.upsert_card_combined(existing.id, text, conn=conn)
    return existing.id, False


# ── the review log (layer 2: the ledger of observations) ────────────────

#: The append-only review-log chunk_kind (seeded by migration 0071). One WORM,
#: dated entry per observation — the gripe body+comment / quest_log pattern.
REVIEW_KIND = "llm_review"

#: Evidence bands, each with a distinct *provenance*: a vendor benchmark, your
#: own golden-set accuracy, aggregated telemetry, and a subjective agent note are
#: NOT the same evidence and must not blend (docs/proposals/llm-catalog.md).
REVIEW_TYPES: frozenset[str] = frozenset(
    {
        "published-benchmark",
        "measured-eval",
        "observed-telemetry",
        "agent-review",
    }
)
DEFAULT_REVIEW_TYPE = "agent-review"

#: Trust ordering for the bands — **observed > measured > published**; a
#: subjective agent note is soft. Slice-4 ranking reads this so a low-trust claim
#: never outweighs a measured one.
PROVENANCE_TRUST: dict[str, int] = {
    "observed-telemetry": 3,
    "measured-eval": 2,
    "published-benchmark": 1,
    "agent-review": 1,
}


def append_review(
    store: Store,
    ref_id: int,
    *,
    text: str,
    review_type: str,
    by: str = "agent",
    provenance: str | None = None,
    axis: str | None = None,
    ordinal: int | None = None,
    variant: str | None = None,
) -> int:
    """Append one WORM review entry to a card; return its 1-based number.

    Permissive (stamps what it is given) — the handler validates ``review_type``
    against :data:`REVIEW_TYPES` to reject a typo. ``axis``/``ordinal`` tag an
    eval result so the derived capability can be traced back to its evidence;
    ``variant`` (e.g. ``fp8`` or ``fp8@Baidu``) scopes a ``published-benchmark``
    entry to the endpoint it was measured on — a card-level number silently
    inherited by a fp4/101k endpoint is the bug gripe 162624 fixes.
    """
    meta: dict[str, Any] = {
        "chunk_kind": REVIEW_KIND,
        "entry_type": review_type,
        "by": by,
    }
    if provenance is not None:
        meta["provenance"] = provenance
    if axis is not None:
        meta["axis"] = axis
    if ordinal is not None:
        meta["ordinal"] = int(ordinal)
    if variant is not None:
        meta["variant"] = variant
    # list_blocks_for_ref excludes the ord=-1 card, so the first review is pos=0.
    next_pos = len(store.list_blocks_for_ref(ref_id))
    with store.tx() as conn:
        store.insert_blocks(
            ref_id, [BlockInsert(pos=next_pos, text=text, meta=meta)], conn=conn
        )
    return next_pos + 1


def list_reviews(store: Store, ref_id: int) -> list[Block]:
    """The review-log entries (``llm_review`` chunks) in append order."""
    return [b for b in store.list_blocks_for_ref(ref_id) if b.chunk_kind == REVIEW_KIND]


# ── the tote (layer 2: derived telemetry over llm_call_log) ─────────────


@dataclass(frozen=True, slots=True)
class ToteRow:
    """A rollup of realized calls for a model (the quest-tote analogue)."""

    calls: int
    cost_usd: float
    error_rate: float | None
    p50_duration_ms: float | None
    avg_turns: float | None


_TOTE_COLS = (
    "count(*), COALESCE(sum(cost_usd), 0), AVG((errored)::int), "
    "percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms), AVG(turns_used)"
)


def _tote_row(row: tuple[Any, ...]) -> ToteRow:
    return ToteRow(
        calls=int(row[0] or 0),
        cost_usd=float(row[1] or 0.0),
        error_rate=(float(row[2]) if row[2] is not None else None),
        p50_duration_ms=(float(row[3]) if row[3] is not None else None),
        avg_turns=(float(row[4]) if row[4] is not None else None),
    )


def llm_tote(store: Store, model_id: str, *, window_days: int = 30) -> ToteRow:
    """Roll up ``llm_call_log`` for ``model_id`` over the last ``window_days``.

    Realized cost, error rate, p50 duration, avg turns — the derived telemetry
    a card learns from. Not stored (a live query, like the quest tote).
    """
    sql = (
        f"SELECT {_TOTE_COLS} FROM llm_call_log "
        "WHERE model = %s AND ts > now() - (%s * interval '1 day')"
    )
    with store.pool.connection() as conn:
        row = conn.execute(sql, (model_id, window_days)).fetchone()
    return _tote_row(row) if row is not None else _tote_row((0,))


def llm_tote_by_source(
    store: Store, model_id: str, *, window_days: int = 30, limit: int = 8
) -> list[tuple[str, ToteRow]]:
    """Per-``source`` tote rows for a model, busiest first."""
    sql = (
        f"SELECT source, {_TOTE_COLS} FROM llm_call_log "
        "WHERE model = %s AND ts > now() - (%s * interval '1 day') "
        "GROUP BY source ORDER BY count(*) DESC LIMIT %s"
    )
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (model_id, window_days, limit)).fetchall()
    return [(r[0] or "?", _tote_row(r[1:])) for r in rows]


# ── observed-axis derivation (the operational "reasoning" signal) ───────

#: Minimum realized calls before telemetry is trusted enough to set an ordinal.
_OBSERVED_MIN_CALLS = 20


def _bucket_success(success: float) -> int:
    """Map a success rate (1 - error_rate) to a coarse 1–5 ordinal."""
    if success >= 0.98:
        return 5
    if success >= 0.95:
        return 4
    if success >= 0.90:
        return 3
    if success >= 0.80:
        return 2
    return 1


def derive_observed_axes(
    store: Store, model_id: str, *, min_calls: int = _OBSERVED_MIN_CALLS
) -> dict[str, int]:
    """Coarse axis ordinals from realized telemetry — precis's own workload as
    the benchmark. ``reasoning-convergence`` is the operational signal: empirical
    success (1 - error rate) on the model's own calls, bucketed 1–5. Returns
    ``{}`` when the sample is too small to trust.
    """
    tote = llm_tote(store, model_id)
    if tote.calls < min_calls or tote.error_rate is None:
        return {}
    return {"reasoning-convergence": _bucket_success(1.0 - tote.error_rate)}


def record_observed_axes(store: Store, model_id: str) -> dict[str, int]:
    """Derive observed ordinals + write them onto the card (``observed-telemetry``
    provenance) with a review-log entry. No-op (``{}``) below the sample floor or
    when the card is absent.
    """
    axes = derive_observed_axes(store, model_id)
    if not axes:
        return {}
    ref = store.find_ref_by_meta(kind=LLM_KIND, key="model_id", value=model_id)
    if ref is None:
        return {}
    cap = dict((ref.meta or {}).get("capability") or {})
    for axis, score in axes.items():
        cap[axis] = {"score": score, "provenance": "observed-telemetry"}
    store.update_ref(ref.id, meta_patch={"capability": cap})
    append_review(
        store,
        ref.id,
        text=f"observed axes from telemetry: {axes}",
        review_type="observed-telemetry",
        by="reconcile",
        provenance="llm_call_log",
    )
    return axes


def record_eval(
    store: Store,
    model_id: str,
    *,
    axis: str,
    ordinal: int,
    by: str = "agent",
    note: str = "",
    provenance: str = "own-golden",
) -> int:
    """Record a golden-set eval result: set the axis ordinal on the card
    (``measured-eval`` provenance) + append a review entry. Returns the review
    number. The write surface a golden-task harness (or a human adjudication)
    reports through; validates the axis + 1–5 range.
    """
    if axis not in CAPABILITY_AXES:
        raise BadInput(
            f"unknown capability axis {axis!r}", options=list(CAPABILITY_AXES)
        )
    if (
        not isinstance(ordinal, int)
        or isinstance(ordinal, bool)
        or not 1 <= ordinal <= 5
    ):
        raise BadInput(f"ordinal must be an int 1..5, got {ordinal!r}")
    ref = store.find_ref_by_meta(kind=LLM_KIND, key="model_id", value=model_id)
    if ref is None:
        raise BadInput(f"no llm card for model {model_id!r}")
    cap = dict((ref.meta or {}).get("capability") or {})
    cap[axis] = {"score": ordinal, "provenance": "measured-eval"}
    store.update_ref(ref.id, meta_patch={"capability": cap})
    return append_review(
        store,
        ref.id,
        text=note or f"{axis} = {ordinal} (measured-eval)",
        review_type="measured-eval",
        by=by,
        provenance=provenance,
        axis=axis,
        ordinal=ordinal,
    )


def record_benchmark(
    store: Store,
    model_id: str,
    *,
    axis: str,
    ordinal: int,
    quant: str | None = None,
    provider: str | None = None,
    source_url: str | None = None,
    by: str = "reconcile",
    note: str = "",
) -> int:
    """Record a **variant-scoped** published benchmark: a low-trust
    ``published-benchmark`` review whose provenance carries the quant + source
    URL, plus (when ``quant`` matches a reconciled endpoint) the ordinal stamped
    onto *that endpoint's* capability rather than the whole card (gripe 162624).

    A card-level published ordinal is the coarse fallback the policy reads when a
    specific endpoint has none; this is the higher-fidelity path — a fp8 SWE-bench
    number lands only on the fp8 endpoints, so booking a fp4/101k variant does not
    inherit it. Returns the review number.
    """
    if axis not in CAPABILITY_AXES:
        raise BadInput(
            f"unknown capability axis {axis!r}", options=list(CAPABILITY_AXES)
        )
    if (
        not isinstance(ordinal, int)
        or isinstance(ordinal, bool)
        or not 1 <= ordinal <= 5
    ):
        raise BadInput(f"ordinal must be an int 1..5, got {ordinal!r}")
    ref = store.find_ref_by_meta(kind=LLM_KIND, key="model_id", value=model_id)
    if ref is None:
        raise BadInput(f"no llm card for model {model_id!r}")
    variant = None
    if quant:
        variant = f"{quant}@{provider}" if provider else quant

    # Stamp the ordinal onto every reconciled endpoint of this quant (the
    # variant-scoped capability the slice-2 policy will read); no-op when the
    # card has no endpoints yet or the quant is unspecified.
    if quant:
        meta = ref.meta or {}
        endpoints = [dict(e) for e in (meta.get("endpoints") or [])]
        touched = False
        for e in endpoints:
            if e.get("quant") != quant:
                continue
            if provider is not None and e.get("provider") != provider:
                continue
            cap = dict(e.get("capability") or {})
            cap[axis] = {"score": ordinal, "provenance": "published-benchmark"}
            e["capability"] = cap
            touched = True
        if touched:
            store.update_ref(ref.id, meta_patch={"endpoints": endpoints})

    prov = source_url or "published-benchmark"
    if quant and source_url:
        prov = f"{source_url} ({quant})"
    return append_review(
        store,
        ref.id,
        text=note
        or f"{axis} = {ordinal} (published-benchmark{f', {variant}' if variant else ''})",
        review_type="published-benchmark",
        by=by,
        provenance=prov,
        axis=axis,
        ordinal=ordinal,
        variant=variant,
    )


#: Short capability prose seeded per tier — the embedded body a card is born
#: with (so ``search(kind='llm', q=…)`` has something to match before the
#: reviewers + evals enrich it in slice 3). Drawn from the ``Tier`` docstring.
_SEED_PROSE: dict[str, str] = {
    "cloud-super": (
        "Cloud reasoning tier (opus-class). Heavy reasoning with tools — the "
        "structural / deep reviewers, fix-gripe, the planner's LLM:opus ticks, "
        "the dream pass, and the generic claude_agent default. Strong at "
        "multi-file refactors and careful SQL; the default when the task is hard."
    ),
    "cloud-mid": (
        "Cloud mid agentic tier (sonnet-class). The workhorse rung — planner "
        "ticks and tex-fix. Capable with tools at lower cost than the super tier."
    ),
    "cloud-small": (
        "Cloud small tier (haiku-class). Tool-less one-shot JSON judgement (the "
        "chase-verifier shape) — fast and cheap classification / triage."
    ),
    "local-small": (
        "Local small tier (the summarizer alias) on the loopback litellm proxy. "
        "The cheapest rung; the per-chunk gloss lives here. Tool-less by "
        "construction."
    ),
    "local-big": (
        "Local big tier (qwen-class) with tools over the OpenAI tools= loop. The "
        "local agentic rung; ADR 0024's dream model."
    ),
}


#: The curated **frontier open-weight** ladder — the best OSS reasoning/agentic
#: models, spanning the tiers Opus→Haiku, all tool- *and* reasoning-capable
#: (``supported_parameters`` on OpenRouter carries ``tools`` + ``reasoning``).
#: These are the Claude-Code-reasoning replacements: seeded so the ``select_offering``
#: policy can pick per task (heavy reasoner vs cheap coder vs fast triage) once a
#: call site routes through it. Facts are seed-grade from the live OpenRouter feed
#: (window + per-1M USD price snapshot); the ``llm_reconcile`` pass then keeps
#: ``facts_openrouter`` (window / price / ``supported_parameters``) authoritative.
#: Capability ordinals are **provisional ``published-benchmark``** (low trust) from
#: mid-2026 SWE-bench/Terminal-Bench/agentic leaderboards — ``record_observed_axes``
#: (telemetry) and ``record_eval`` (golden sets) overwrite them with higher-trust
#: numbers once these models actually run. All route via the OSS backend
#: (``openai_compat`` transport → OpenRouter), so switching to one is env-only
#: (``PRECIS_LLM_BACKEND`` + ``PRECIS_LLM_BASE_URL``).
#:
#: Each row: (model_id, tier_floor, max_input, price_in, price_out, capability).
_FRONTIER_CARDS: tuple[tuple[str, str, int, float, float, dict[str, int]], ...] = (
    # ── cloud-super — Opus-class heavy reasoning with tools ────────────────
    (
        "z-ai/glm-5.2",
        "cloud-super",
        1_048_576,
        0.969,
        3.045,
        {
            "code": 5,
            "reasoning-convergence": 5,
            "tool-structured": 5,
            "long-context-recall": 5,
        },
    ),
    (
        "moonshotai/kimi-k3",
        "cloud-super",
        1_048_576,
        3.0,
        15.0,
        {
            "code": 5,
            "reasoning-convergence": 5,
            "tool-structured": 5,
            "long-context-recall": 5,
        },
    ),
    (
        "deepseek/deepseek-v4-pro",
        "cloud-super",
        1_048_576,
        0.435,
        0.87,
        {
            "code": 4,
            "reasoning-convergence": 5,
            "tool-structured": 4,
            "long-context-recall": 5,
        },
    ),
    # ── cloud-mid — Sonnet-class workhorse, capable with tools, cheaper ────
    (
        "moonshotai/kimi-k2.7-code",
        "cloud-mid",
        262_144,
        0.75,
        3.5,
        {
            "code": 5,
            "reasoning-convergence": 4,
            "tool-structured": 5,
            "long-context-recall": 4,
        },
    ),
    (
        "qwen/qwen3.7-max",
        "cloud-mid",
        1_000_000,
        1.475,
        4.425,
        {
            "code": 4,
            "reasoning-convergence": 4,
            "tool-structured": 4,
            "long-context-recall": 5,
        },
    ),
    (
        "minimax/minimax-m3",
        "cloud-mid",
        1_048_576,
        0.30,
        1.20,
        {
            "code": 3,
            "reasoning-convergence": 3,
            "tool-structured": 4,
            "long-context-recall": 5,
        },
    ),
    (
        "z-ai/glm-4.7",
        "cloud-mid",
        202_752,
        0.40,
        1.75,
        {
            "code": 4,
            "reasoning-convergence": 4,
            "tool-structured": 4,
            "long-context-recall": 3,
        },
    ),
    # ── cloud-small — Haiku-class fast/cheap triage, still tool-capable ────
    (
        "qwen/qwen3.6-flash",
        "cloud-small",
        1_000_000,
        0.188,
        1.125,
        {
            "code": 3,
            "reasoning-convergence": 3,
            "tool-structured": 3,
            "long-context-recall": 5,
        },
    ),
    (
        "deepseek/deepseek-v4-flash",
        "cloud-small",
        1_048_576,
        0.098,
        0.196,
        {
            "code": 3,
            "reasoning-convergence": 4,
            "tool-structured": 3,
            "long-context-recall": 5,
        },
    ),
    (
        "z-ai/glm-4.7-flash",
        "cloud-small",
        202_752,
        0.06,
        0.40,
        {
            "code": 3,
            "reasoning-convergence": 3,
            "tool-structured": 3,
            "long-context-recall": 3,
        },
    ),
    (
        "openai/gpt-oss-120b",
        "cloud-small",
        131_072,
        0.037,
        0.17,
        {
            "code": 3,
            "reasoning-convergence": 3,
            "tool-structured": 3,
            "long-context-recall": 2,
        },
    ),
    (
        "openai/gpt-oss-20b",
        "cloud-small",
        131_072,
        0.03,
        0.13,
        {
            "code": 2,
            "reasoning-convergence": 2,
            "tool-structured": 2,
            "long-context-recall": 2,
        },
    ),
)

#: Short capability prose per frontier model — the embedded body a card is born
#: with (so ``search(kind='llm', q=…)`` matches before reviews/evals enrich it).
_FRONTIER_PROSE: dict[str, str] = {
    "z-ai/glm-5.2": (
        "GLM-5.2 (Z.ai, 744B MoE, MIT) — the strongest all-round open-weight "
        "model: first OSS to beat GPT-5.5 on SWE-bench Pro, leads Terminal-Bench, "
        "1M context, multiple reasoning-effort levels. The primary open-weight "
        "replacement for Claude-class agentic reasoning."
    ),
    "moonshotai/kimi-k3": (
        "Kimi K3 (Moonshot, MoE) — flagship agentic / long-horizon model, 1M "
        "context, reasoning always on. Strong at tool orchestration and repo-"
        "level coding over long agent runs."
    ),
    "deepseek/deepseek-v4-pro": (
        "DeepSeek V4 Pro (MIT) — algorithmic / competitive-programming reasoning "
        "leader at a low price floor; 1M context, tools + reasoning. The cheap "
        "heavy-reasoner rung."
    ),
    "moonshotai/kimi-k2.7-code": (
        "Kimi K2.7 Code (Moonshot) — coding-specialised, ~30% fewer thinking "
        "tokens than K2.6, which directly lowers the cost of long agent runs. "
        "262K context, tools + reasoning."
    ),
    "qwen/qwen3.7-max": (
        "Qwen3.7 Max (Alibaba, Apache) — a capable 1M-context workhorse with "
        "strong tool calling and reasoning; the Sonnet-class rung."
    ),
    "minimax/minimax-m3": (
        "MiniMax M3 — cheapest 1M-context open-weight model with tools + "
        "reasoning and native multimodality; a low-cost long-context workhorse."
    ),
    "z-ai/glm-4.7": (
        "GLM-4.7 (Z.ai) — the mid rung of the GLM line: strong coding + tool use "
        "at a fraction of the flagship's price, 200K context."
    ),
    "qwen/qwen3.6-flash": (
        "Qwen3.6 Flash (Alibaba) — a fast, cheap 1M-context model that still "
        "carries tools + reasoning; the Haiku-class triage rung with a wide window."
    ),
    "deepseek/deepseek-v4-flash": (
        "DeepSeek V4 Flash (MIT) — a remarkably cheap 1M-context reasoning model "
        "with tools; strong value for fast triage that still needs to think."
    ),
    "z-ai/glm-4.7-flash": (
        "GLM-4.7 Flash (Z.ai) — a very cheap fast model with tools + reasoning, "
        "200K context; the low-cost classification / triage rung."
    ),
    "openai/gpt-oss-120b": (
        "gpt-oss-120b (OpenAI, Apache) — an extremely cheap open-weight model with "
        "tools + reasoning; runnable self-hosted, the near-free tool-capable rung."
    ),
    "openai/gpt-oss-20b": (
        "gpt-oss-20b (OpenAI, Apache) — the small open-weight model with tools + "
        "reasoning, runnable on modest hardware; the cheapest tool-capable rung."
    ),
}


#: Static model shape per frontier model — ``meta.params`` (size / arch /
#: license). OpenRouter's ``architecture`` block carries no parameter count, so
#: these are curated (the number you need to reason about "the 744B MoE vs the
#: 20B" when booking). ``reconcile`` leaves ``params`` untouched (it refreshes
#: only the live facts), so a hand-correction survives.
_FRONTIER_PARAMS: dict[str, dict[str, str]] = {
    "z-ai/glm-5.2": {"size": "744B", "arch": "MoE", "license": "MIT"},
    "moonshotai/kimi-k3": {"size": "~1T", "arch": "MoE", "license": "modified-MIT"},
    "deepseek/deepseek-v4-pro": {"size": "~685B", "arch": "MoE", "license": "MIT"},
    "moonshotai/kimi-k2.7-code": {
        "size": "~1T",
        "arch": "MoE",
        "license": "modified-MIT",
    },
    "qwen/qwen3.7-max": {"size": "~1T", "arch": "MoE", "license": "Apache-2.0"},
    "minimax/minimax-m3": {"size": "~456B", "arch": "MoE", "license": "MIT"},
    "z-ai/glm-4.7": {"size": "355B", "arch": "MoE", "license": "MIT"},
    "qwen/qwen3.6-flash": {"size": "~30B", "arch": "MoE", "license": "Apache-2.0"},
    "deepseek/deepseek-v4-flash": {"size": "~100B", "arch": "MoE", "license": "MIT"},
    "z-ai/glm-4.7-flash": {"size": "~106B", "arch": "MoE", "license": "MIT"},
    "openai/gpt-oss-120b": {"size": "117B", "arch": "MoE", "license": "Apache-2.0"},
    "openai/gpt-oss-20b": {"size": "21B", "arch": "MoE", "license": "Apache-2.0"},
}


def seed_frontier_cards(store: Store) -> list[tuple[str, int, bool]]:
    """Mint (or refresh) a card per curated **frontier open-weight** model — the
    best OSS reasoning/agentic models, Opus→Haiku (:data:`_FRONTIER_CARDS`).

    Additive to :func:`seed_default_cards` (which seeds the models precis already
    runs): this fills the catalog with the open-weight menu the ``select_offering``
    policy can pick from. Each card gets one ``openai_compat`` offering (window +
    price from the live OpenRouter snapshot) and provisional ``published-benchmark``
    capability ordinals; ``llm_reconcile`` keeps window/price/flags authoritative.
    Idempotent (``upsert_card`` on ``model_id``). Returns ``[(model_id, ref_id,
    created)]``.
    """
    results: list[tuple[str, int, bool]] = []
    for (
        model_id,
        tier_floor,
        max_input,
        price_in,
        price_out,
        capability,
    ) in _FRONTIER_CARDS:
        offering: dict[str, Any] = {
            "effort": "medium",
            "transport": "openai_compat",
            "endpoint": "openrouter",
            "max_input": max_input,
            "price_in": price_in,
            "price_out": price_out,
        }
        prose = _FRONTIER_PROSE.get(model_id, f"{model_id} — open-weight model.")
        ref_id, created = upsert_card(
            store,
            model_id=model_id,
            text=prose,
            tier_floor=tier_floor,
            offerings=[offering],
            capability=capability,
            params=_FRONTIER_PARAMS.get(model_id),
            provenance={"source": "seed-frontier", "band": "published-benchmark"},
        )
        results.append((model_id, ref_id, created))
    return results


def seed_default_cards(store: Store) -> list[tuple[str, int, bool]]:
    """Mint (or refresh) a card per model precis actually runs — the ``Tier``
    table's resolved models (docs/proposals/llm-catalog.md, slice 1 seed).

    Facts are seed-grade: the tool-using transport for the tier, price from
    ``PRICE_TABLE`` when known (else provider-reported / free), and the tier as
    the ``tier_floor``. The ``llm_reconcile`` pass then refreshes window + price
    from the live OpenRouter feed. Idempotent (``upsert_card`` on ``model_id``).
    Returns ``[(model_id, ref_id, created)]``.
    """
    from precis.budget.pricing import PRICE_TABLE
    from precis.utils.llm.router import Tier, resolve_model, select_transport

    results: list[tuple[str, int, bool]] = []
    for tier in Tier:
        model_id = resolve_model(tier)
        offering: dict[str, Any] = {
            "effort": "medium",
            "transport": select_transport(tier, tools_needed=True).value,
        }
        rates = PRICE_TABLE.get(model_id)
        if rates is not None:
            offering["price_in"], offering["price_out"] = rates
        prose = _SEED_PROSE.get(tier.value, f"{tier.value} tier model.")
        ref_id, created = upsert_card(
            store,
            model_id=model_id,
            text=prose,
            tier_floor=tier.value,
            offerings=[offering],
            provenance={"source": "seed"},
        )
        results.append((model_id, ref_id, created))
    return results


__all__ = [
    "CAPABILITY_AXES",
    "DEFAULT_REVIEW_TYPE",
    "ENDPOINT_KEYS",
    "LLM_KIND",
    "OFFERING_KEYS",
    "PROVENANCE_TRUST",
    "REVIEW_KIND",
    "REVIEW_TYPES",
    "SERVED_BY_KEYS",
    "ToteRow",
    "append_review",
    "build_meta",
    "derive_observed_axes",
    "list_reviews",
    "llm_tote",
    "llm_tote_by_source",
    "record_benchmark",
    "record_eval",
    "record_observed_axes",
    "seed_default_cards",
    "seed_frontier_cards",
    "upsert_card",
]

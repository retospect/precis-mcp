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
        out.append(o)
    return out


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


def build_meta(
    *,
    model_id: str,
    tier_floor: str | None = None,
    offerings: Any = None,
    capability: Any = None,
    provenance: Any = None,
) -> dict[str, Any]:
    """Validate + assemble the ``meta`` patch for a model card.

    Only the keys that are supplied land in the patch — the update path merges
    (``meta || patch``), so a reconcile refreshing just prices passes only
    ``offerings`` and leaves ``capability`` untouched. ``model_id`` is always
    present (it is the identity + lookup key).
    """
    if not model_id or not isinstance(model_id, str):
        raise BadInput("model_id must be a non-empty string (the canonical model slug)")
    meta: dict[str, Any] = {"model_id": model_id}
    if tier_floor is not None:
        meta["tier_floor"] = str(tier_floor)
    if offerings is not None:
        meta["offerings"] = _validate_offerings(offerings)
    if capability is not None:
        meta["capability"] = _validate_capability(capability)
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
    capability: Any = None,
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
        capability=capability,
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
) -> int:
    """Append one WORM review entry to a card; return its 1-based number.

    Permissive (stamps what it is given) — the handler validates ``review_type``
    against :data:`REVIEW_TYPES` to reject a typo. ``axis``/``ordinal`` tag an
    eval result so the derived capability can be traced back to its evidence.
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
    "LLM_KIND",
    "OFFERING_KEYS",
    "PROVENANCE_TRUST",
    "REVIEW_KIND",
    "REVIEW_TYPES",
    "ToteRow",
    "append_review",
    "build_meta",
    "derive_observed_axes",
    "list_reviews",
    "llm_tote",
    "llm_tote_by_source",
    "record_eval",
    "record_observed_axes",
    "seed_default_cards",
    "upsert_card",
]

"""Step-level retry policy for trust-blocked pathway quantities — the pure half.

A 20-step route with ONE fatal convergence failure yields 19 trusted steps
and a quantity whose ``trust_summary`` names the blocker
(``blocked_by: <step>#s<seed>#<check>``). Re-running the whole network for
that is the multiplicative-attrition trap (0.95^20 ≈ 0.36 usable routes at
a 5 % per-step failure rate); re-measuring the ONE blocked step at a fresh
seed is additive. This module decides *what* to retry and how the retry's
partial folds back in; the store/dispatch half lives in
:mod:`precis_pathway.aggregate_job` (the aggregate is where
``trust_summary`` is computed, so the decision sits next to the evidence).

Pieces, all free of store and engine imports so they test without either:

* :func:`retryable_convergence_blockers` — eligibility: fatal
  ``neb_convergence``/``relax_convergence`` records explicitly cited by an
  unavailable barrier/selectivity quantity; any endpoint-mismatch-class
  record withholds the whole selection (a basin mismatch needs diagnosis,
  not a re-roll — never retried here).
* :func:`plan_step_retries` — one :class:`StepRetry` per eligible record,
  bounded by :func:`max_step_retries` per ``(step, model_index)`` across
  the durable event log, each at a fresh seed never used by this pathway
  (the seed is part of the citable record id, so the retry's evidence is
  distinguishable from the measurement it replaces).
* :func:`narrow_partial` — reduce a whole-network seed partial to one
  step's measurement (the step entry, its trust records, the endpoint and
  root state energies). The pinned engine has no single-step entry point,
  so the retry seed still *computes* the network; what it *records* is
  the one step — when the engine grows a step filter the narrowing
  becomes a no-op.
* :func:`apply_step_replacements` — at aggregation, a retry partial
  replaces the failed ``(step, seed, model)`` measurement: the base
  partial loses that step entry and the step's trust records (catpath's
  ``_trust_summary`` blocks on ANY fatal route record, so the failed one
  must leave for a clean retry to unblock), the retry contributes only
  the narrowed step. Endpoint energies of the base partial stay in the
  pool — they were ``min``'d across every step naming the state and
  cannot be disentangled.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

#: Default cap on automatic retries per ``(step, model_index)``; after that
#: the blocker stands and surfaces to the tick/human exactly as before.
DEFAULT_MAX_STEP_RETRIES = 2
#: Env override for the cap (deploy dial); ``0`` disables step retry.
MAX_STEP_RETRIES_ENV = "PRECIS_PATHWAY_STEP_RETRIES"

#: Pathway-ref meta key holding the durable retry event log (one dict per
#: dispatched retry, :meth:`StepRetry.event`), shallow-merged by
#: ``store.stamp_ref_meta`` so it survives every ``persist_result``.
EVENTS_META_KEY = "step_retries"

#: Checks this policy may retry: the measurement failed to converge; a
#: fresh seed is a legitimate re-measurement. Everything else (detachment,
#: wrong binder, endpoint identity, …) is a different question.
RETRYABLE_CHECKS = ("neb_convergence", "relax_convergence")


def max_step_retries() -> int:
    """The per-step cap: :data:`MAX_STEP_RETRIES_ENV` when it parses as a
    nonnegative int, else :data:`DEFAULT_MAX_STEP_RETRIES`."""
    try:
        return max(0, int(os.environ.get(MAX_STEP_RETRIES_ENV, "")))
    except ValueError:
        return DEFAULT_MAX_STEP_RETRIES


def retryable_convergence_blockers(results: dict[str, Any]) -> list[dict[str, Any]]:
    """Return cited fatal convergence records, preserving their full identity.

    Schema 1/2 ids are opaque (state-at-step and attempt suffixes included)
    and may repeat across models. Never manufacture a target from an error
    string or state-only record. Endpoint failures veto the selection even
    when state-only: assigning a basin mismatch to a step needs diagnosis.
    This does not mutate ``results``; returned records belong to the input.
    """
    schema = results.get("trust_schema")
    if type(schema) is not int or schema not in (1, 2):
        return []
    trust = results.get("trust")
    summary = results.get("trust_summary")
    if not isinstance(trust, list) or not isinstance(summary, dict):
        return []
    records = [r for r in trust if isinstance(r, dict)]
    for record in records:
        if record.get("verdict") != "fail" or record.get("severity") != "fatal":
            continue
        evidence = record.get("evidence")
        if record.get("check") in ("endpoint_identity", "endpoint_agreement") or (
            isinstance(evidence, dict) and evidence.get("reason") == "endpoint-mismatch"
        ):
            return []

    cited: set[str] = set()
    for quantity in ("barrier", "selectivity"):
        verdict = summary.get(quantity)
        if not isinstance(verdict, dict) or verdict.get("available") is not False:
            continue
        blockers = verdict.get("blocked_by")
        if not isinstance(blockers, list):
            continue
        for blocker in blockers:
            reasons = blocker.get("reasons") if isinstance(blocker, dict) else [blocker]
            if isinstance(reasons, list):
                cited.update(reason for reason in reasons if isinstance(reason, str))

    return [
        record
        for record in records
        if isinstance(record.get("id"), str)
        and record["id"] in cited
        and record.get("check") in RETRYABLE_CHECKS
        and record.get("verdict") == "fail"
        and record.get("severity") == "fatal"
        and isinstance(record.get("step"), str)
        and record["step"].strip()
        and type(record.get("seed")) is int
        and record["seed"] >= 0
    ]


@dataclass(frozen=True)
class StepRetry:
    """One planned re-measurement: ``step`` of model ``model_index`` at
    ``to_seed``, replacing the failed measurement at ``from_seed`` that
    ``record_id`` (a ``check`` failure) cites. ``attempt`` is 1-based per
    ``(step, model_index)``."""

    step: str
    model_index: int
    model: str | None
    from_seed: int
    to_seed: int
    check: str
    record_id: str
    attempt: int

    def event(self, **extra: Any) -> dict[str, Any]:
        """The durable event-log entry for this retry (``extra`` carries the
        dispatch handles — todo/job ids, ``at`` timestamp, ``round``)."""
        return {**asdict(self), **extra}


def retry_content_key(base_key: str, *, step: str, seed: int, model_index: int) -> str:
    """Content address of one retry unit — distinct from every whole-network
    seed key (``quest.compute._autocatpath_seed_content_key``) by
    construction, and the retry job's idem key (``autocatpath_seed:<key>``)."""
    payload = (
        f"{base_key}\nstep-retry\nstep={step}\nseed={seed}\nmodel_index={model_index}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def retry_count(
    events: Iterable[Mapping[str, Any]], step: str, model_index: int
) -> int:
    """How many retries the log already records for ``(step, model_index)``."""
    return sum(
        1
        for e in events
        if e.get("step") == step and e.get("model_index") == model_index
    )


def already_retried(
    events: Iterable[Mapping[str, Any]], step: str, model_index: int, from_seed: int
) -> bool:
    """Has THIS measurement (``step`` at ``from_seed`` of ``model_index``)
    already been re-dispatched? Its replacement is pending or landed; a
    second retry of the same record would be the aggregate re-running, not
    a new failure — a new failure cites the retry's own seed."""
    return any(
        e.get("step") == step
        and e.get("model_index") == model_index
        and e.get("from_seed") == from_seed
        for e in events
    )


def plan_step_retries(
    results: dict[str, Any],
    *,
    events: Sequence[Mapping[str, Any]],
    model_index_of: Mapping[str | None, int],
    seeds_in_use: Iterable[int],
    max_retries: int | None = None,
) -> list[StepRetry]:
    """Plan the retries this aggregate round should dispatch.

    One retry per eligible record (:func:`retryable_convergence_blockers`),
    in a deterministic order, each capped per ``(step, model_index)`` by
    ``max_retries`` counting ``events`` (the durable log) plus the retries
    planned earlier in this call. Fresh seeds are allocated above every
    seed in ``seeds_in_use`` and every ``to_seed`` already in the log, so
    a seed is never reused by this pathway. A record whose model is not
    in ``model_index_of`` names no unit to dispatch and is skipped; so is a
    record the log shows :func:`already_retried` (the same aggregate
    re-running must not spend a second retry on one measurement).
    """
    cap = max_step_retries() if max_retries is None else max_retries
    used = set(seeds_in_use) | {
        int(e["to_seed"]) for e in events if isinstance(e.get("to_seed"), int)
    }
    next_seed = max(used, default=-1) + 1
    planned: list[StepRetry] = []
    records = sorted(
        retryable_convergence_blockers(results),
        key=lambda r: (r["step"], str(r.get("model")), r["seed"], r["id"]),
    )
    seen: set[tuple[str, str | None, int]] = set()
    for record in records:
        model = record.get("model")
        model_key = model if isinstance(model, str) else None
        key = (record["step"], model_key, record["seed"])
        if key in seen:
            continue  # two checks failing on one measurement → one retry
        seen.add(key)
        model_index = model_index_of.get(model_key)
        if model_index is None or already_retried(
            events, record["step"], model_index, record["seed"]
        ):
            continue
        prior = retry_count(events, record["step"], model_index) + sum(
            1
            for p in planned
            if p.step == record["step"] and p.model_index == model_index
        )
        if prior >= cap:
            continue
        planned.append(
            StepRetry(
                step=record["step"],
                model_index=model_index,
                model=model_key,
                from_seed=record["seed"],
                to_seed=next_seed,
                check=str(record["check"]),
                record_id=record["id"],
                attempt=prior + 1,
            )
        )
        next_seed += 1
    return planned


def exhausted_blockers(
    results: dict[str, Any],
    *,
    events: Sequence[Mapping[str, Any]],
    model_index_of: Mapping[str | None, int],
    max_retries: int | None = None,
) -> list[dict[str, Any]]:
    """Eligible records that get NO retry because their ``(step, model)``
    already spent the cap — what a reader should see as "the blocker
    stands". A record whose own replacement is pending is not standing."""
    cap = max_step_retries() if max_retries is None else max_retries
    out = []
    for record in retryable_convergence_blockers(results):
        model = record.get("model")
        model_index = model_index_of.get(model if isinstance(model, str) else None)
        if model_index is None or already_retried(
            events, record["step"], model_index, record["seed"]
        ):
            continue
        if retry_count(events, record["step"], model_index) >= cap:
            out.append(record)
    return out


def _mentions(text: str, names: Iterable[str]) -> bool:
    return any(n and n in text for n in names)


def narrow_partial(
    partial: Mapping[str, Any],
    step: str | Iterable[str],
    *,
    root: str | None = None,
) -> dict[str, Any]:
    """Reduce a whole-network seed partial to ``step``'s measurement (one
    step name, or several).

    Keeps: the step entries; trust records naming a kept step
    (state-at-step records carry ``step`` too); state energies for the
    steps' endpoints and ``root`` (``aggregate_partials`` references every
    partial to the root before pooling — without it the energies would
    pool unreferenced); per-state ``vib``/``ads_barriers`` for those
    states; warnings that mention a kept step or state. ``refs``/``shed``/
    ``seed``/``model`` pass through unchanged. Returns a new dict;
    ``partial`` is not mutated. A step absent from the partial yields no
    entry (the retry measured nothing usable) rather than raising.
    """
    wanted = [step] if isinstance(step, str) else [str(s) for s in step]
    steps = partial.get("steps") or {}
    entries = {
        s: steps[s]
        for s in wanted
        if isinstance(steps, Mapping) and isinstance(steps.get(s), Mapping)
    }
    keep: set[str] = set()
    for entry in entries.values():
        keep.update(
            str(entry[k]) for k in ("reactant", "product") if entry.get(k) is not None
        )
    if root:
        keep.add(root)
    states = partial.get("states") or {}
    out = dict(partial)
    out["steps"] = deepcopy(entries)
    out["states"] = {n: e for n, e in states.items() if n in keep}
    out["trust"] = [
        deepcopy(r)
        for r in (partial.get("trust") or [])
        if isinstance(r, Mapping) and r.get("step") in wanted
    ]
    for key in ("vib", "ads_barriers"):
        table = partial.get(key)
        if isinstance(table, Mapping):
            out[key] = {n: deepcopy(v) for n, v in table.items() if n in keep}
    out["warnings"] = [
        w
        for w in (partial.get("warnings") or [])
        if isinstance(w, str) and _mentions(w, [*wanted, *keep])
    ]
    return out


def narrow_structures(
    structures: Mapping[str, Any], partial: Mapping[str, Any]
) -> dict[str, Any]:
    """Keep only the geometries of states a narrowed ``partial`` still
    carries — a retry must not win the aggregate's min-energy geometry
    merge for states it did not re-measure."""
    kept = set((partial.get("states") or {}).keys())
    return {n: g for n, g in structures.items() if n in kept}


def step_retry_of(seed_result: Mapping[str, Any]) -> dict[str, Any] | None:
    """The ``{"step", "from_seed"}`` marker a retry seed carries (stamped
    from its job params onto its meta), or ``None`` for a base seed."""
    marker = seed_result.get("step_retry")
    if (
        isinstance(marker, Mapping)
        and isinstance(marker.get("step"), str)
        and type(marker.get("from_seed")) is int
    ):
        return {"step": marker["step"], "from_seed": marker["from_seed"]}
    return None


def apply_step_replacements(
    seed_results: Sequence[Mapping[str, Any]], *, root: str | None = None
) -> list[dict[str, Any]]:
    """Fold retry seeds into the partial set the aggregate combines.

    For every retry seed ``r`` (:func:`step_retry_of`): its partial is
    narrowed to the step (idempotent — a seed job that already narrowed
    is unchanged; a child from an older build that did not is narrowed
    here), its ``structures`` likewise; and the base partial with the same
    ``model_index`` and ``seed == from_seed`` loses that step's entry and
    trust records. Chains work: a retry of a retry prunes the earlier
    retry's narrowed step the same way. Nothing in ``seed_results`` is
    mutated; copies are returned in the input order.
    """
    out = [dict(r) for r in seed_results]
    pruned: dict[tuple[int | None, int | None], set[str]] = {}
    for r in out:
        marker = step_retry_of(r)
        if marker is None:
            continue
        r["partial"] = narrow_partial(r.get("partial") or {}, marker["step"], root=root)
        if isinstance(r.get("structures"), Mapping):
            r["structures"] = narrow_structures(r["structures"], r["partial"])
        pruned.setdefault((r.get("model_index"), marker["from_seed"]), set()).add(
            marker["step"]
        )
    for r in out:
        steps = pruned.get((r.get("model_index"), r.get("seed")))
        if not steps:
            continue
        partial = deepcopy(r.get("partial") or {})
        partial["steps"] = {
            n: s for n, s in (partial.get("steps") or {}).items() if n not in steps
        }
        partial["trust"] = [
            t
            for t in (partial.get("trust") or [])
            if not (isinstance(t, Mapping) and t.get("step") in steps)
        ]
        r["partial"] = partial
    return out


__all__ = [
    "DEFAULT_MAX_STEP_RETRIES",
    "EVENTS_META_KEY",
    "MAX_STEP_RETRIES_ENV",
    "RETRYABLE_CHECKS",
    "StepRetry",
    "already_retried",
    "apply_step_replacements",
    "exhausted_blockers",
    "max_step_retries",
    "narrow_partial",
    "narrow_structures",
    "plan_step_retries",
    "retry_content_key",
    "retry_count",
    "retryable_convergence_blockers",
    "step_retry_of",
]

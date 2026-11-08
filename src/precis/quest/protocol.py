"""Per-quest measurement protocol + protocol-drift visibility (gr593142).

A quest's candidates are only comparable while they were measured the same
way: same MLIP model, same engine version, same network template, same seed
count, same tolerances. Today that provenance is stamped per pathway
(``meta.autocatpath_version``, ``meta.config``, ``requested_seeds``) but
never surfaced — a quest whose candidates span engines 0.20.0 → 0.50.0,
1 → 3 seeds and parked → coadsorbed networks reads as one homogeneous
frontier (qu164903). This module makes the drift visible:

* :class:`QuestProtocol` — the per-quest **protocol record** (model, engine
  version, network template, seed count, tolerances, and the measurement
  ``search`` knobs — ``neb_schedule``/``neb_optimizer``/``neb_multistart``/
  ``screening``/… — that decide how the run computes its numbers). The
  quest's *current* protocol is derived, never stored:
  :func:`quest_protocol` resolves
  ``meta.reaction_config`` through the same tier overlay dispatch applies
  (:func:`precis.quest.compute._apply_tier_config` +
  ``_neb_multiseed_config`) under the live engine pin, so "current" means
  "what a dispatch right now would land" — a config edit or an engine bump
  moves it automatically, with no stale stored copy to disagree with.
* **Protocol drift** — one deduped ``observation`` logbook entry
  (``by=system``) whenever a *dispatched* or *harvested* run differs from
  the quest's current protocol **for that run's own tier** (a screening
  run's parked template is the ladder, not drift; a neb run on parked is).
  Dedup is content-keyed (``meta.protocol_drift_key``): the same run never
  logs twice, a second run with the same drift does (its own line — "one
  logbook line whenever a run differs").
* **The protocol histogram** — :func:`protocol_histogram` counts the
  quest's candidates per engine version and per seed count (plus network
  and model, the other two drift axes); :func:`protocol_lines` renders the
  record + histogram for the frontier header and the quest status report.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from precis.quest.logbook import MEASURED_BY, append_entry

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuestProtocol:
    """The protocol record: the measurement knobs a comparison presumes shared.

    ``None``/empty means unprovable (an unstamped run, a config-less quest) —
    a missing value never counts as a difference, matching the trust gate's
    "unprovable provenance is not staleness" rule.
    """

    model: str | None = None
    engine: str | None = None
    template: str | None = None
    seeds: int | None = None
    tolerances: Mapping[str, float] = field(default_factory=dict)
    #: The measurement ``search`` knobs (``neb_schedule``, ``neb_optimizer``,
    #: ``neb_multistart``, ``screening``, …) — keys already carried by their
    #: own fields (``seeds`` → :attr:`seeds`, ``energy_thresh`` →
    #: :attr:`tolerances`) and runtime-only levers precis itself injects
    #: (``neb_pool_size``, ``neb_batched`` — physics-identical batching
    #: controls) are excluded; see :data:`_NON_MEASURE_SEARCH_KEYS`.
    engine_settings: Mapping[str, Any] = field(default_factory=dict)


#: ``search`` keys that are NOT part of the protocol's engine settings:
#: ``seeds``/``energy_thresh`` have dedicated fields, and
#: ``neb_pool_size``/``neb_batched`` are physics-identical runtime levers —
#: the former stamped by dispatch's GB10 memory guard
#: (:data:`precis.quest.compute._GB10_MACE_NEB_POOL_SIZE`), the latter a
#: batching switch the engine self-checks to serial equivalence.
_NON_MEASURE_SEARCH_KEYS = frozenset(
    {"seeds", "energy_thresh", "neb_pool_size", "neb_batched"}
)


def _model_label(specs: list[tuple[str, str | None]]) -> str | None:
    """``[(backend, model)]`` → ``"mace:medium"`` (``"mace:medium+mace:small"``
    for a multi-model run); ``None`` when there is nothing to name."""
    parts = [f"{b}:{m}" if m else b for b, m in specs]
    return "+".join(parts) or None


def _tolerances_from_config(config: Mapping[str, Any]) -> dict[str, float]:
    """The tolerances a run on ``config`` is judged under: the engine's
    endpoint-agreement tolerance plus any declared ``search.energy_thresh``."""
    from precis.utils.trust_gate import endpoint_tol_ev

    out = {"endpoint_tol_eV": endpoint_tol_ev()}
    search = config.get("search") if isinstance(config, Mapping) else None
    thresh = (search or {}).get("energy_thresh")
    if isinstance(thresh, (int, float)) and not isinstance(thresh, bool):
        out["energy_thresh_eV"] = float(thresh)
    return out


def _engine_settings_from_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """The measurement ``search`` knobs a run on ``config`` is judged under —
    every search key except the ones with dedicated protocol fields or a
    runtime-only role (:data:`_NON_MEASURE_SEARCH_KEYS`)."""
    search = config.get("search")
    if not isinstance(search, Mapping):
        return {}
    return {k: v for k, v in search.items() if k not in _NON_MEASURE_SEARCH_KEYS}


def protocol_from_config(
    config: Mapping[str, Any], *, engine: str | None = None
) -> QuestProtocol:
    """The protocol a run on ``config`` measures under (model, resolved
    network template, seed count, tolerances, engine settings) + the given
    engine version."""
    from precis.quest import compute as _compute

    specs = _compute._autocatpath_mlip_specs(dict(config))
    seeds = _compute._autocatpath_search_seeds(dict(config))
    return QuestProtocol(
        model=_model_label(specs),
        engine=engine,
        template=_compute._resolved_template(config),
        seeds=len(seeds) if seeds else None,
        tolerances=_tolerances_from_config(config),
        engine_settings=_engine_settings_from_config(config),
    )


def quest_protocol(
    store: Store, quest_id: int, *, tier: str | None = None
) -> QuestProtocol | None:
    """The quest's **current** protocol for one ladder ``tier``.

    Derived per call — ``meta.reaction_config`` overlaid with the tier's
    dispatch transform (``_apply_tier_config``; ``_neb_multiseed_config``
    for ``neb``) under the live engine pin — so the record is always what a
    fresh dispatch would land. ``tier=None`` defaults to ``neb``, the
    canonical comparison rung (``_pinned_network_basis``'s choice too).
    ``None`` when the quest declares no reaction config (no autocatpath
    lane — no protocol to compare against).
    """
    from precis.quest import compute as _compute

    reaction = _compute._quest_reaction_config(store, quest_id)
    if reaction is None:
        return None
    rung = tier or _compute._TIER_NEB
    cfg = _compute._apply_tier_config(dict(reaction), rung)
    if rung == _compute._TIER_NEB:
        cfg = _compute._neb_multiseed_config(cfg)
    return protocol_from_config(cfg, engine=_compute._autocatpath_current_version())


def _seed_count(value: Any) -> int | None:
    """The length of a stamped seed list, or ``None`` when unstamped."""
    if (
        isinstance(value, list)
        and value
        and all(isinstance(s, int) and not isinstance(s, bool) for s in value)
    ):
        return len(value)
    return None


def protocol_from_pathway(pw_meta: Mapping[str, Any] | None) -> QuestProtocol | None:
    """The protocol a `pathway` ref's run actually measured under.

    Engine is the dispatch pin the pathway was minted under
    (``meta.autocatpath_version``); seed count prefers intent
    (``requested_seeds``, stamped at mint) over launch
    (``executed_seeds``) over the aggregate's declared ``results.seeds``;
    the endpoint tolerance is the ``tol_eV`` the run's own
    ``endpoint_agreement`` trust record was judged against when stamped
    (the engine constant otherwise — :func:`protocol_from_config`).
    ``None`` when the pathway carries no stored ``config`` (nothing to
    resolve a protocol from — never guess).
    """
    if not isinstance(pw_meta, Mapping):
        return None
    config = pw_meta.get("config")
    if not isinstance(config, Mapping) or not config:
        return None
    engine = pw_meta.get("autocatpath_version")
    proto = protocol_from_config(config, engine=str(engine) if engine else None)
    results = pw_meta.get("results")
    results = results if isinstance(results, Mapping) else {}
    seeds = (
        _seed_count(pw_meta.get("requested_seeds"))
        or _seed_count(pw_meta.get("executed_seeds"))
        or _seed_count(results.get("seeds"))
        or proto.seeds
    )
    tolerances = dict(proto.tolerances)
    records = results.get("trust")
    for rec in records if isinstance(records, list) else []:
        if not isinstance(rec, Mapping) or rec.get("check") != "endpoint_agreement":
            continue
        evidence = rec.get("evidence")
        tol = evidence.get("tol_eV") if isinstance(evidence, Mapping) else None
        if isinstance(tol, (int, float)) and not isinstance(tol, bool):
            tolerances["endpoint_tol_eV"] = float(tol)
            break
    return replace(proto, seeds=seeds, tolerances=tolerances)


def protocol_diff(
    expected: QuestProtocol, actual: QuestProtocol
) -> dict[str, tuple[Any, Any]]:
    """``{field: (expected, actual)}`` for every protocol field that differs.

    A field whose ``actual`` is unprovable (``None``/empty) never diffs —
    an unstamped run predates the stamp, it is not evidence of drift.
    """
    out: dict[str, tuple[Any, Any]] = {}
    for f in ("model", "engine", "template", "seeds"):
        e, a = getattr(expected, f), getattr(actual, f)
        if a is not None and e != a:
            out[f] = (e, a)
    if actual.tolerances and dict(expected.tolerances) != dict(actual.tolerances):
        out["tolerances"] = (dict(expected.tolerances), dict(actual.tolerances))
    if actual.engine_settings and dict(expected.engine_settings) != dict(
        actual.engine_settings
    ):
        out["engine_settings"] = (
            dict(expected.engine_settings),
            dict(actual.engine_settings),
        )
    return out


#: Display labels for :func:`protocol_diff` fields (the gripe's own terms).
_FIELD_LABEL: dict[str, str] = {
    "model": "model",
    "engine": "engine",
    "template": "network",
    "seeds": "seeds",
    "tolerances": "tolerances",
    "engine_settings": "engine settings",
}


def _fmt_value(v: Any) -> str:
    return (
        json.dumps(v, sort_keys=True, separators=(",", ":"))
        if isinstance(v, dict)
        else str(v)
    )


def _drift_key(quest_id: int, subject: str, diff: dict[str, tuple[Any, Any]]) -> str:
    """Content key under which a drift entry dedupes: one entry per
    (quest, run, difference)."""
    canon = json.dumps(
        {k: [_fmt_value(e), _fmt_value(a)] for k, (e, a) in diff.items()},
        sort_keys=True,
    )
    return hashlib.sha256(
        f"protocol-drift|{quest_id}|{subject}|{canon}".encode()
    ).hexdigest()[:16]


def _drift_logged(store: Store, quest_id: int, key: str) -> bool:
    """True once a ``meta.protocol_drift_key`` entry already exists."""
    for b in store.chunks.list_chunks_for_ref(quest_id):
        if (b.meta or {}).get("protocol_drift_key") == key:
            return True
    return False


def note_protocol_drift(
    store: Store,
    quest_id: int,
    *,
    subject: str,
    expected: QuestProtocol,
    actual: QuestProtocol,
) -> int | None:
    """One deduped ``protocol drift`` logbook line (``observation``,
    ``by=system``), or ``None`` on no diff / repeat / missing quest.

    ``subject`` names the run — e.g. ``pw1234`` or ``dispatch
    q7cand-abc-rx-deadbeef`` — and keys the dedup: the same run never
    logs twice; a different run with the same drift gets its own line.
    """
    diff = protocol_diff(expected, actual)
    if not diff:
        return None
    key = _drift_key(quest_id, subject, diff)
    if _drift_logged(store, quest_id, key):
        return None
    fields = "; ".join(
        f"{_FIELD_LABEL.get(k, k)} {_fmt_value(a)} vs protocol {_fmt_value(e)}"
        for k, (e, a) in diff.items()
    )
    return append_entry(
        store,
        quest_id,
        text=f"protocol drift [{subject}]: {fields}",
        entry_type="observation",
        by=MEASURED_BY,
        extra_meta={
            "protocol_drift_key": key,
            "protocol_drift": {
                "subject": subject,
                "expected": {
                    "model": expected.model,
                    "engine": expected.engine,
                    "template": expected.template,
                    "seeds": expected.seeds,
                    "tolerances": dict(expected.tolerances),
                    "engine_settings": dict(expected.engine_settings),
                },
                "actual": {
                    "model": actual.model,
                    "engine": actual.engine,
                    "template": actual.template,
                    "seeds": actual.seeds,
                    "tolerances": dict(actual.tolerances),
                    "engine_settings": dict(actual.engine_settings),
                },
                "diff": {
                    k: [_fmt_value(e), _fmt_value(a)] for k, (e, a) in diff.items()
                },
            },
        },
    )


def note_dispatch_drift(
    store: Store,
    structure_ref_id: int,
    run_config: Mapping[str, Any],
    *,
    tier: str,
    subject: str,
) -> int | None:
    """Dispatch-side check: the run's dispatched config vs the quest's
    current protocol for the same tier. Defensive — a drift check must
    never break the mint it annotates."""
    try:
        from precis.quest import compute as _compute

        quest_id = _compute._serving_quest_id(store, structure_ref_id)
        if quest_id is None:
            return None
        expected = quest_protocol(store, quest_id, tier=tier)
        if expected is None:
            return None
        actual = protocol_from_config(run_config, engine=expected.engine)
        return note_protocol_drift(
            store, quest_id, subject=subject, expected=expected, actual=actual
        )
    except Exception:
        log.warning(
            "protocol drift check failed for structure %s",
            structure_ref_id,
            exc_info=True,
        )
        return None


def note_harvest_drift(
    store: Store,
    quest_id: int,
    *,
    subject: str,
    pw_meta: Mapping[str, Any] | None,
) -> int | None:
    """Harvest-side check: the finished pathway's stamped protocol vs the
    quest's current protocol for the pathway's own tier — resolved by
    :func:`precis.quest.compute._pathway_tier`, which reads the
    dispatch-time ``meta.tier`` stamp AND catpath's verbatim ``results``
    for a pre-stamp pathway (a legacy screening run with no ``tier`` key
    compares against screening, never neb). Defensive — a drift check must
    never break the harvest it annotates."""
    try:
        from precis.quest import compute as _compute

        pw_tier = _compute._pathway_tier(
            dict(pw_meta) if isinstance(pw_meta, Mapping) else None
        )
        expected = quest_protocol(store, quest_id, tier=pw_tier)
        actual = protocol_from_pathway(pw_meta)
        if expected is None or actual is None:
            return None
        return note_protocol_drift(
            store, quest_id, subject=subject, expected=expected, actual=actual
        )
    except Exception:
        log.warning("protocol drift check failed for quest %s", quest_id, exc_info=True)
        return None


@dataclass
class ProtocolHistogram:
    """The quest's protocol spread: one bucket per candidate's **measure
    pathway** — the run its displayed measures came from
    (:func:`_measure_pathway`), not the newest mint.

    ``measured`` counts candidates with a completed run behind their
    displayed measures; candidates whose runs are all in-flight/failed —
    and candidates with no run at all — fill no bucket.
    """

    candidates: int = 0
    measured: int = 0
    engines: Counter[str] = field(default_factory=Counter)
    seeds: Counter[int] = field(default_factory=Counter)
    templates: Counter[str] = field(default_factory=Counter)
    models: Counter[str] = field(default_factory=Counter)


def _pathways_by_candidate(
    store: Store, candidate_ids: list[int]
) -> dict[int, list[tuple[int, dict[str, Any]]]]:
    """``{candidate_ref: [(pathway_ref_id, meta), ...]}`` — every live
    `pathway` minted against the quest's candidates."""
    out: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    if not candidate_ids:
        return out
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = 'pathway' "
            "AND retired_at IS NULL AND meta->>'candidate_ref' = ANY(%s) "
            "ORDER BY ref_id",
            ([str(i) for i in candidate_ids],),
        ).fetchall()
    for ref_id, meta in rows:
        meta = meta if isinstance(meta, dict) else {}
        try:
            cand = int(str(meta.get("candidate_ref") or ""))
        except ValueError:
            continue
        out.setdefault(cand, []).append((int(ref_id), meta))
    return out


def _measure_pathway(
    runs: list[tuple[int, dict[str, Any]]], candidate_meta: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The pathway meta whose run supplied the candidate's **displayed**
    measures — the protocol the frontier actually quotes.

    The kept barrier's provenance stamp wins:
    ``meta.barrier_pathway_ref_id`` names the pathway the displayed
    ``barrier`` was harvested from, which may differ from the newest
    ``ready`` run (a later barrierless harvest leaves the older barrier on
    display). Failing that, the newest COMPLETED run — ``ready`` first,
    then ``superseded``: a superseded pathway's measures stay on display
    until its superseder harvests, so it still counts. An in-flight
    (``computing``) or ``failed`` run supplied nothing — ``None``, and the
    candidate is not measured.
    """
    pid = candidate_meta.get("barrier_pathway_ref_id")
    if isinstance(pid, int) and not isinstance(pid, bool):
        for ref_id, meta in runs:
            if ref_id == pid:
                return meta
    done = [m for _ref_id, m in runs if m.get("status") == "ready"]
    if done:
        return done[-1]
    done = [m for _ref_id, m in runs if m.get("status") == "superseded"]
    return done[-1] if done else None


def protocol_histogram(store: Store, quest_id: int) -> ProtocolHistogram | None:
    """Candidates per engine version / per seed count (plus network and
    model — the other two drift axes), counted under the protocol of the
    run that supplied each candidate's displayed measures. ``None`` when
    the quest has no candidate structures."""
    from precis.quest import compute as _compute

    candidate_ids = _compute._candidate_struct_ids(store, quest_id)
    if not candidate_ids:
        return None
    hist = ProtocolHistogram(candidates=len(candidate_ids))
    by_cand = _pathways_by_candidate(store, candidate_ids)
    cand_refs = store.fetch_refs_by_ids(set(candidate_ids))
    for cand in candidate_ids:
        runs = by_cand.get(cand)
        if not runs:
            continue
        cand_meta = getattr(cand_refs.get(cand), "meta", None) or {}
        meta = _measure_pathway(runs, cand_meta)
        if meta is None:
            continue
        proto = protocol_from_pathway(meta)
        if proto is None:
            continue
        hist.measured += 1
        if proto.engine is not None:
            hist.engines[proto.engine] += 1
        if proto.seeds is not None:
            hist.seeds[proto.seeds] += 1
        if proto.template is not None:
            hist.templates[proto.template] += 1
        if proto.model is not None:
            hist.models[proto.model] += 1
    return hist


def _fmt_settings(settings: Mapping[str, Any]) -> str:
    """``{neb_schedule: best_first, neb_optimizer: neb-ode, neb_multistart: 3}``
    → ``schedule=best_first+optimizer=neb-ode+multistart=3`` — the ``neb_``
    prefix is implied, a ``True`` flag renders bare."""
    return "+".join(
        k.removeprefix("neb_") if v is True else f"{k.removeprefix('neb_')}={v}"
        for k, v in settings.items()
    )


def _fmt_protocol(p: QuestProtocol, *, tier: str | None = None) -> str:
    """``mace:medium · autocatpath 0.50.0 · coadsorbed · 3 seeds · tol 0.05 eV``."""
    bits = [p.model or "?", f"autocatpath {p.engine or '?'}", p.template or "?"]
    bits.append(f"{p.seeds} seeds" if p.seeds is not None else "? seeds")
    tol = p.tolerances.get("endpoint_tol_eV")
    if isinstance(tol, (int, float)):
        bits.append(f"endpoint tol {tol:g} eV")
    if p.engine_settings:
        bits.append(f"search {_fmt_settings(p.engine_settings)}")
    return "protocol" + (f" ({tier})" if tier else "") + ": " + " · ".join(bits)


def _hist_sort_key(v: Any) -> tuple[Any, ...]:
    """Versions sort versionwise (0.9.0 before 0.50.0), ints numerically,
    everything else alphabetically after both."""
    if isinstance(v, int) and not isinstance(v, bool):
        return (0, v, "")
    parts = str(v).split(".")
    if parts and all(p.isdigit() for p in parts):
        return (1, tuple(int(p) for p in parts), "")
    return (2, (), str(v))


def _fmt_hist(counter: Mapping[Any, int]) -> str:
    return " ".join(
        f"{k}×{n}"
        for k, n in sorted(counter.items(), key=lambda i: _hist_sort_key(i[0]))
    )


def protocol_lines(store: Store, quest_id: int) -> list[str]:
    """The ``protocol:``/``measured:`` header lines — the quest's current
    (neb-tier) protocol record, then the candidate histogram.

    Empty when the quest has no reaction config AND no measured candidate —
    nothing to compare, nothing to print.
    """
    lines: list[str] = []
    try:
        from precis.quest import compute as _compute

        current = quest_protocol(store, quest_id, tier=_compute._TIER_NEB)
    except Exception:
        log.debug("protocol_lines: quest_protocol failed", exc_info=True)
        current = None
    try:
        hist = protocol_histogram(store, quest_id)
    except Exception:
        log.debug("protocol_lines: histogram failed", exc_info=True)
        hist = None
    if current is None and not (hist and hist.measured):
        return lines
    if current is not None:
        lines.append(_fmt_protocol(current, tier="neb"))
    if hist is not None and hist.measured:
        parts = []
        if hist.engines:
            parts.append(f"engines {_fmt_hist(hist.engines)}")
        if hist.seeds:
            parts.append(f"seeds {_fmt_hist(hist.seeds)}")
        if hist.templates:
            parts.append(f"network {_fmt_hist(hist.templates)}")
        if hist.models:
            parts.append(f"model {_fmt_hist(hist.models)}")
        line = "measured: " + " · ".join(parts)
        unevaluated = hist.candidates - hist.measured
        if unevaluated > 0:
            line += (
                f" ({unevaluated} of {hist.candidates} candidates have no "
                "completed run yet)"
            )
        lines.append(line)
    return lines


__all__ = [
    "ProtocolHistogram",
    "QuestProtocol",
    "note_dispatch_drift",
    "note_harvest_drift",
    "note_protocol_drift",
    "protocol_diff",
    "protocol_from_config",
    "protocol_from_pathway",
    "protocol_histogram",
    "protocol_lines",
    "quest_protocol",
]

"""Capability ledger — the roadmap body's derived, never-stored read.

``docs/backlog/bootstrap-roadmap-quest.md`` §Design "Derived (never stored)
reads": a roadmap **root** quest is served by *capability* quests
(``meta.quest_body == "roadmap"`` + ``meta.rubric_objectives``), each holding
a per-axis ``meta.demand`` (what a target part needs) and ``meta.supply``
(the best cited literature value). *Rungs* are todos carrying ``meta.rung``
that ``serves`` a capability and ``produces`` a cited value for one of its
axes. This module computes, from those three stored shapes, the one thing
the body acts on and the tick stamps deeds against:

* :func:`best_supply` — for one (capability, key): the better of
  ``meta.supply[key]`` and every **done** rung's ``produces`` for that key,
  "better" decided by the axis' ``sense``. An open rung is a promise, not a
  supply — it never counts here.
* :func:`compute_ledger` — one :class:`LedgerRow` per (capability, axis)
  under a root: demanded · best supply (+ evidence) · closing rung · state.
* :func:`render_ledger_markdown` — the table the tree view shows and the
  next stage pins into the root's dossier as one regenerated chunk.
* :func:`ledger_signature` — the flat ``{capability:key → best_supply}``
  map the tick diffs against the previous one to detect an improvement.

Pure compute + render: no LLM calls, no store writes, and no dossier import.
Malformed stored values (the write guards in ``handlers/quest.py`` /
``handlers/_todo_guards.py`` make them rare, not impossible) are skipped, so
a render never crashes on one bad row.

**Which rung "closes" an axis** when several produce the same key — the
rule, since the spec does not say: the done rung whose ``produces`` *is* the
best supply, if the best supply came from a rung; else the in-flight rung
with the best produced value for the key (ties → the older, lower ``td``);
else none. A rung's ``produces`` entry counts for capability ``C`` only when
the rung ``serves`` ``C`` **and** the entry names ``C`` — a rung mislinked to
one capability while claiming a value for another contributes to neither.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Ref, Store

logger = logging.getLogger(__name__)

# ── tunables ──────────────────────────────────────────────────────────

#: Bound the per-read capability fan-out of a root — same discipline as
#: ``gaps._ALIGN_MAX_SERVERS``: a root with a huge ``serves`` fan-out still
#: renders cheaply; capabilities past the cap are not ledgered (the gap
#: emitter appends a ``fanout-capped`` Gap so the truncation is visible).
LEDGER_MAX_CAPABILITIES = 40

#: Bound the per-capability rung fan-in the same way. Counted over RUNGS,
#: after filtering: a capability's ``serves`` fan-in is mostly papers (supply
#: ticks link them), so capping the raw links would let papers crowd rungs
#: out. Past this many rungs the excess (newest ``td`` first dropped — links
#: come back in insertion order, so the oldest rungs are kept) is not read,
#: and :func:`rungs_for` logs a warning naming the capability.
LEDGER_MAX_RUNGS_PER_CAPABILITY = 200

#: ``STATUS`` values under which a rung has delivered its ``produces``.
DONE_STATUSES: frozenset[str] = frozenset({"done"})

#: ``STATUS`` values under which a rung is neither delivered nor in flight —
#: it closes nothing and promises nothing.
_DEAD_STATUSES: frozenset[str] = frozenset({"won't-do", "wontfix", "cancelled"})

#: Axis ``sense`` spellings accepted from ``rubric_objectives``, normalised
#: to the frontier's ``min``/``max`` vocabulary (``frontier._VALID_SENSES``).
#: ``lower``/``higher`` are the roadmap spec's own words for the same thing.
_SENSE_ALIASES: dict[str, str] = {
    "min": "min",
    "minimise": "min",
    "minimize": "min",
    "lower": "min",
    "less": "min",
    "max": "max",
    "maximise": "max",
    "maximize": "max",
    "higher": "max",
    "more": "max",
}

_LOG_KIND = "quest_log"

#: Ledger row states (spec §Design "capability ledger").
STATE_UNMET = "unmet"
STATE_PARTIAL = "partial"
STATE_MET = "met"
STATE_DEAD_END = "dead-end"


# ── result type ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class LedgerRow:
    """One (capability, axis) line of the capability ledger."""

    capability: str  # "qu<id>"
    capability_title: str  # 60-char display stub (ledger table, gap one-liners)
    key: str
    sense: str  # "min" | "max" (normalised from rubric_objectives)
    unit: str | None
    demanded: float | None
    best_supply: float | None
    best_supply_evidence: tuple[str, ...]
    closing_rung: str | None  # "td<id>" or None
    closing_rung_status: str | None
    state: str  # unmet | partial | met | dead-end
    #: The capability's full statement — what a role prompt shows the model.
    #: ``capability_title`` is cut to 60 characters for the ledger table, and
    #: the first prod dry-run (2026-09-30) showed that stub as the whole
    #: "capability statement" the demand role was told to derive its number
    #: from: "…place a building block where we choose," with the clause that
    #: names the tolerance cut off. Defaulted so hand-built rows still work.
    capability_statement: str = ""


# ── small pure helpers ────────────────────────────────────────────────


def normalise_sense(raw: Any) -> str | None:
    """``rubric_objectives[].sense`` → ``"min"``/``"max"``, or ``None`` when
    absent or unrecognised — an axis with no direction is skipped rather
    than guessed (the caller cannot say which of two numbers is better)."""
    if not isinstance(raw, str):
        return None
    return _SENSE_ALIASES.get(raw.strip().lower())


def _better(a: float, b: float, sense: str) -> bool:
    """Is ``a`` strictly better than ``b`` under ``sense``?"""
    return a < b if sense == "min" else a > b


def meets(value: float | None, demanded: float | None, sense: str) -> bool:
    """Does ``value`` satisfy ``demanded`` under ``sense``? A missing value
    meets nothing; a missing demand is met by nothing (no target to hit)."""
    if value is None or demanded is None:
        return False
    return value <= demanded if sense == "min" else value >= demanded


def _num(raw: Any) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _handles(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(h.strip() for h in raw if isinstance(h, str) and h.strip())


def _handle(kind: str, ref_id: int) -> str:
    from precis.utils import handle_registry

    return handle_registry.try_format(kind, ref_id) or f"{kind}:{ref_id}"


def _first_line(title: str | None) -> str:
    return (title or "").splitlines()[0] if title else ""


def is_capability_quest(ref: Ref) -> bool:
    """A roadmap-marked quest carrying at least one ``rubric_objectives``
    axis — the Design table's *capability* role (pathways carry the marker
    but no axes; the root carries neither)."""
    # Lazy: ``weave_tick`` drags the dossier/weave machinery in; this module
    # is imported on every ``view='tree'`` read via ``gaps``.
    from precis.quest.weave_tick import QUEST_BODY_META_KEY, QUEST_BODY_ROADMAP

    meta = ref.meta or {}
    if ref.kind != "quest" or meta.get(QUEST_BODY_META_KEY) != QUEST_BODY_ROADMAP:
        return False
    axes = meta.get("rubric_objectives")
    return isinstance(axes, list) and any(
        isinstance(a, dict) and str(a.get("key") or "").strip() for a in axes
    )


def capability_axes(ref: Ref) -> list[tuple[str, str, str | None]]:
    """``(key, sense, unit)`` per usable ``rubric_objectives`` axis on a
    capability. Axes with no recognisable ``sense`` are dropped — see
    :func:`normalise_sense`. Duplicate keys keep the first."""
    out: list[tuple[str, str, str | None]] = []
    seen: set[str] = set()
    raw = (ref.meta or {}).get("rubric_objectives")
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        sense = normalise_sense(item.get("sense"))
        if not key or sense is None or key in seen:
            continue
        seen.add(key)
        unit_raw = item.get("unit")
        unit = (
            unit_raw.strip() if isinstance(unit_raw, str) and unit_raw.strip() else None
        )
        out.append((key, sense, unit))
    return out


# ── rung fetch ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Rung:
    ref: Ref
    status: str  # resolved STATUS tag value, "open" when untagged


def rungs_for(store: Store, capability_ids: list[int]) -> dict[int, list[Rung]]:
    """Live ``meta.rung`` todos that ``serves`` each capability, with their
    resolved ``STATUS``. One ``links_for`` per capability, then ONE batched
    ref fetch + ONE batched tag query across all of them. The single reader
    of rung status — ``roadmap_tick`` calls this rather than re-deriving it."""
    per_cap: dict[int, list[int]] = {}
    all_ids: list[int] = []
    for cid in capability_ids:
        links = store.links_for(cid, direction="in", relation="serves")
        ids: list[int] = []
        seen: set[int] = set()
        for ln in links:
            src = int(ln.src_ref_id)
            if src in seen:
                continue
            seen.add(src)
            ids.append(src)
        per_cap[cid] = ids
        all_ids.extend(ids)
    if not all_ids:
        return {cid: [] for cid in capability_ids}

    refs = store.fetch_refs_by_ids(set(all_ids))
    rung_ids = [
        i
        for i in dict.fromkeys(all_ids)
        if (r := refs.get(i)) is not None
        and r.retired_at is None
        and r.kind == "todo"
        and isinstance((r.meta or {}).get("rung"), dict)
    ]
    status: dict[int, str] = {}
    if rung_ids:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT rt.ref_id, t.value FROM ref_tags rt "
                "JOIN tags t ON t.tag_id = rt.tag_id "
                "WHERE rt.ref_id = ANY(%s) AND t.namespace = 'STATUS'",
                (rung_ids,),
            ).fetchall()
        for rid, val in rows:
            status.setdefault(int(rid), str(val))
    rung_set = set(rung_ids)
    out: dict[int, list[Rung]] = {}
    for cid, ids in per_cap.items():
        rungs = [
            Rung(ref=refs[i], status=status.get(i, "open"))
            for i in ids
            if i in rung_set
        ]
        if len(rungs) > LEDGER_MAX_RUNGS_PER_CAPABILITY:
            logger.warning(
                "roadmap ledger: capability qu%d has %d rungs, reading the "
                "oldest %d (LEDGER_MAX_RUNGS_PER_CAPABILITY)",
                cid,
                len(rungs),
                LEDGER_MAX_RUNGS_PER_CAPABILITY,
            )
            rungs = rungs[:LEDGER_MAX_RUNGS_PER_CAPABILITY]
        out[cid] = rungs
    return out


def _produced(
    rung: Rung, capability: str, key: str
) -> tuple[float, tuple[str, ...]] | None:
    """``(value, evidence)`` this rung's ``produces`` claims for
    ``(capability, key)``, or ``None``. Several entries for the same key on
    one rung would be a guard escape; the first well-formed one wins."""
    raw = ((rung.ref.meta or {}).get("rung") or {}).get("produces")
    if not isinstance(raw, list):
        return None
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if entry.get("capability") != capability or entry.get("key") != key:
            continue
        value = _num(entry.get("value"))
        if value is None:
            continue
        return value, _handles(entry.get("evidence"))
    return None


# ── best supply ───────────────────────────────────────────────────────


def _best_supply_from(
    capability_ref: Ref, rungs: list[Rung], key: str, sense: str
) -> tuple[float | None, tuple[str, ...], Rung | None]:
    """The better of ``meta.supply[key]`` and every done rung's ``produces``
    for ``key``. Returns ``(value, evidence, source_rung)`` — ``source_rung``
    is the done rung that supplied the winner, ``None`` when ``meta.supply``
    did (a rung must be *strictly* better to displace the stored value)."""
    best: float | None = None
    evidence: tuple[str, ...] = ()
    source: Rung | None = None

    supply = (capability_ref.meta or {}).get("supply")
    entry = supply.get(key) if isinstance(supply, dict) else None
    if isinstance(entry, dict):
        value = _num(entry.get("value"))
        if value is not None:
            best, evidence = value, _handles(entry.get("evidence"))

    cap_handle = _handle("quest", capability_ref.id)
    for rung in rungs:
        if rung.status not in DONE_STATUSES:
            continue
        got = _produced(rung, cap_handle, key)
        if got is None:
            continue
        value, ev = got
        if best is None or _better(value, best, sense):
            best, evidence, source = value, ev, rung
    return best, evidence, source


def best_supply(
    store: Store, capability_id: int, key: str, sense: str
) -> tuple[float | None, tuple[str, ...]]:
    """Best supply for one (capability, key): the better of ``meta.supply``
    and every **done** rung's ``produces``, by ``sense`` (``min``/``max``,
    or a spec alias — ``lower``/``higher``). ``(None, ())`` when nothing
    cited supplies the axis or the capability does not exist."""
    norm = normalise_sense(sense)
    if norm is None:
        raise ValueError(
            f"unrecognised sense {sense!r} — expected min/max (lower/higher)"
        )
    ref = store.get_ref(kind="quest", id=capability_id)
    if ref is None:
        return None, ()
    rungs = rungs_for(store, [ref.id]).get(ref.id, [])
    value, evidence, _src = _best_supply_from(ref, rungs, key, norm)
    return value, evidence


# ── supply-outcome history ────────────────────────────────────────────

#: ``extra_meta`` key of the one ``observation`` entry every supply tick
#: appends on the capability's logbook: ``{"key", "dry", "external",
#: "queries"}``, plus ``"external_error"`` when an outside search failed
#: (``external`` is then false: a failed search does not count as searched).
SUPPLY_OUTCOME_META = "supply_outcome"

#: ``extra_meta`` key of the one-off entry logged when two escalated supply
#: ticks on a key both came back dry: ``{"key", "queries"}``.
SUPPLY_NOT_FOUND_META = "supply_not_found_outside"


@dataclass(frozen=True)
class SupplyHistory:
    """What the capability logbook says about supply ticks on one key.

    ``streak`` is the run of most-recent ``supply_outcome`` entries that are
    dry (it restarts at every tick that wrote a supply); ``ext_dry`` counts
    those in which the external leg ran; ``ext_queries`` is every query those
    external-dry ticks ran. ``not_found_queries`` is ``None`` unless a
    ``supply_not_found_outside`` entry is newer than the last non-dry tick,
    else the number of queries it names."""

    streak: int = 0
    ext_dry: int = 0
    ext_queries: tuple[str, ...] = ()
    not_found_queries: int | None = None


def supply_history(store: Store, capability_id: int, key: str) -> SupplyHistory:
    """Read the capability's ``supply_outcome`` / ``supply_not_found_outside``
    logbook entries for ``key`` (oldest to newest, append order) into a
    :class:`SupplyHistory`."""
    streak: list[dict[str, Any]] = []
    not_found: int | None = None
    for b in store.chunks.list_chunks_for_ref(capability_id):
        if b.chunk_kind != _LOG_KIND:
            continue
        meta = b.meta or {}
        outcome = meta.get(SUPPLY_OUTCOME_META)
        if isinstance(outcome, dict) and outcome.get("key") == key:
            if outcome.get("dry"):
                streak.append(outcome)
            else:
                streak, not_found = [], None
            continue
        nf = meta.get(SUPPLY_NOT_FOUND_META)
        if isinstance(nf, dict) and nf.get("key") == key:
            qs = nf.get("queries")
            not_found = len(qs) if isinstance(qs, list) else 0
    ext = [o for o in streak if o.get("external")]
    queries = tuple(
        str(q) for o in ext for q in (o.get("queries") or []) if isinstance(q, str)
    )
    return SupplyHistory(
        streak=len(streak),
        ext_dry=len(ext),
        ext_queries=queries,
        not_found_queries=not_found,
    )


def not_found_outside_note(store: Store, capability_id: int, key: str) -> str:
    """``"; not found outside (N queries)"`` for a gap line when the supply
    role searched outside twice and found nothing — else ``""``."""
    n = supply_history(store, capability_id, key).not_found_queries
    return f"; not found outside ({n} queries)" if n is not None else ""


# ── the ledger ────────────────────────────────────────────────────────


def _has_dead_end_for(store: Store, capability_id: int, key: str) -> bool:
    """A ``dead-end`` logbook entry on the capability naming the axis key —
    the bridge role's outcome (c) verdict, read back from the graph."""
    for b in store.chunks.list_chunks_for_ref(capability_id):
        if b.chunk_kind != _LOG_KIND:
            continue
        if str((b.meta or {}).get("entry_type", "")) != "dead-end":
            continue
        if key in (b.text or ""):
            return True
    return False


def _row_for(
    store: Store,
    cap: Ref,
    rungs: list[Rung],
    key: str,
    sense: str,
    unit: str | None,
) -> LedgerRow:
    cap_handle = _handle("quest", cap.id)
    demand = (cap.meta or {}).get("demand")
    d_entry = demand.get(key) if isinstance(demand, dict) else None
    demanded = _num(d_entry.get("value")) if isinstance(d_entry, dict) else None

    supply_value, evidence, source_rung = _best_supply_from(cap, rungs, key, sense)

    # In-flight rungs promising this axis — the best promised value first,
    # ties to the older rung (links_for returns insertion order; stable sort).
    in_flight: list[tuple[float, Rung]] = []
    for rung in rungs:
        if rung.status in DONE_STATUSES or rung.status in _DEAD_STATUSES:
            continue
        got = _produced(rung, cap_handle, key)
        if got is not None:
            in_flight.append((got[0], rung))
    in_flight.sort(key=lambda t: t[0] if sense == "min" else -t[0])
    closer = in_flight[0][1] if in_flight else None

    if meets(supply_value, demanded, sense):
        state = STATE_MET
        closing = source_rung
    elif in_flight and meets(in_flight[0][0], demanded, sense):
        state = STATE_PARTIAL
        closing = closer
    elif demanded is not None and _has_dead_end_for(store, cap.id, key):
        state = STATE_DEAD_END
        closing = closer
    else:
        state = STATE_UNMET
        closing = closer

    return LedgerRow(
        capability=cap_handle,
        capability_title=_first_line(cap.title)[:60],
        capability_statement=(cap.title or "").strip(),
        key=key,
        sense=sense,
        unit=unit,
        demanded=demanded,
        best_supply=supply_value,
        best_supply_evidence=evidence,
        closing_rung=_handle("todo", closing.ref.id) if closing is not None else None,
        closing_rung_status=closing.status if closing is not None else None,
        state=state,
    )


def capability_servers(store: Store, root_quest_id: int) -> list[Ref]:
    """Live capability quests that ``serves`` ``root_quest_id`` (one hop),
    in link order, capped at :data:`LEDGER_MAX_CAPABILITIES`. Use
    :func:`compute_ledger_for` when the caller already holds the servers."""
    from precis.quest.gaps import _live_servers

    return _capabilities_from(_live_servers(store, root_quest_id))


def _capabilities_from(servers: list[Ref]) -> list[Ref]:
    return [r for r in servers if is_capability_quest(r)][:LEDGER_MAX_CAPABILITIES]


def compute_ledger_for(store: Store, capabilities: list[Ref]) -> list[LedgerRow]:
    """The ledger over an already-selected capability list — rows in
    capability order, axes in ``rubric_objectives`` order."""
    if not capabilities:
        return []
    rungs = rungs_for(store, [c.id for c in capabilities])
    rows: list[LedgerRow] = []
    for cap in capabilities:
        for key, sense, unit in capability_axes(cap):
            rows.append(_row_for(store, cap, rungs.get(cap.id, []), key, sense, unit))
    return rows


def compute_ledger(store: Store, root_quest_id: int) -> list[LedgerRow]:
    """One row per (capability, axis) under a roadmap root — see the module
    docstring for the state rules and the closing-rung rule."""
    return compute_ledger_for(store, capability_servers(store, root_quest_id))


# ── signature + render ────────────────────────────────────────────────


def ledger_signature(rows: list[LedgerRow]) -> dict[str, float]:
    """``{"qu<id>:<key>": best_supply}`` for every row that has a best
    supply — the flat map the tick diffs against the previous ledger to
    stamp a deed on an improvement. Rows with no supply are absent (an
    axis appearing for the first time is an improvement from nothing)."""
    return {
        f"{r.capability}:{r.key}": r.best_supply
        for r in rows
        if r.best_supply is not None
    }


def _fmt(value: float | None, unit: str | None) -> str:
    if value is None:
        return "—"
    text = f"{value:g}"
    return f"{text} {unit}" if unit else text


def render_ledger_markdown(rows: list[LedgerRow]) -> str:
    """The capability ledger as one markdown table — key · demanded · best
    supply (with its evidence handles) · closing rung + status · state."""
    if not rows:
        return "_capability ledger: no capability axes under this root._"
    out = [
        "| capability | key | demanded | best supply | evidence | closing rung | state |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        cap = f"{r.capability} {r.capability_title}".strip()
        arrow = "↓" if r.sense == "min" else "↑"
        evidence = ", ".join(r.best_supply_evidence) if r.best_supply_evidence else "—"
        rung = f"{r.closing_rung} ({r.closing_rung_status})" if r.closing_rung else "—"
        out.append(
            f"| {cap} | {r.key} {arrow} | {_fmt(r.demanded, r.unit)} | "
            f"{_fmt(r.best_supply, r.unit)} | {evidence} | {rung} | {r.state} |"
        )
    return "\n".join(out)


__all__ = [
    "LEDGER_MAX_CAPABILITIES",
    "LEDGER_MAX_RUNGS_PER_CAPABILITY",
    "STATE_DEAD_END",
    "STATE_MET",
    "STATE_PARTIAL",
    "STATE_UNMET",
    "LedgerRow",
    "best_supply",
    "capability_axes",
    "capability_servers",
    "compute_ledger",
    "compute_ledger_for",
    "is_capability_quest",
    "ledger_signature",
    "meets",
    "normalise_sense",
    "render_ledger_markdown",
]

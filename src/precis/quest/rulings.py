"""Code-minted measurement rulings — the sims→findings anchor.

quest-dossier-dialectic §Mechanism, agreed 2026-08-29: when a **trusted
measurement** lands on a structure that a hypothesis's **pre-registered
experiment** names, a code pass mints a **measurement-ruling finding** —
templated text stating the value, trust grade, and handles — and links the
measuring pathway ``tests →`` the hypothesis. No LLM authors any of it, so
the ruling cannot be fabricated: it is verifiable by construction, which is
exactly what lets the *next* tick do the interpretive work (emit
``support``/``counter`` citing the ruling's handle, or ``settle`` the
hypothesis) against the experiment's pre-registered branch predictions.

Match contract (v1 — structure-level): an experiment entry pre-registers by
citing ``[st…]`` handles inline; a match is that structure carrying a
trusted canonical barrier (``meta.barrier_trusted is True`` + a ``barrier``
— stamped by :func:`precis.quest.compute._pathway_quality` from catpath
trust schema 1-2). Per-step (reaction-edge) matching is a follow-up once
experiments pre-register ``pw<id>~src→tgt`` step selectors.

Idempotency: each mint records itself under the experiment entry chunk's
``meta.rulings`` (``{key: finding_id}``, key = structure × measuring
pathway) — the primary skip — and the ruling finding carries a
deterministic ``meta.sim_ruling_key`` the pass converges on if the entry
meta was ever lost. Concurrency: neither guard is DB-enforced; the pass
relies on the quest_tick coordinator's one-live-job-per-quest idem key
(``quest_tick:<id>``) for serialization. A hypothetical concurrent run's
worst case is an orphan duplicate ruling finding (the ``tests`` edge is
tuple-idempotent either way) — accepted, not locked against. A re-measure (new pathway, e.g. after supersession or
an engine reset) is a NEW key and mints a new ruling — trust is re-earned,
never edited.

Fence (deliberate, load-bearing): sim-based rulings settle *internal*
hypotheses only — they never ground nanopub evidence (that stays
papers/patents/EDGAR/datasheets; docs/backlog/
computed-pathways-cannot-be-cited-as-claim-evidence.md). Structurally: the
ruling gets **no STATUS tag** (it never enters the default finding-search
cohort) and ``tests`` is not an evidence relation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from precis.quest.dossier import (
    _DIALECTIC_HANDLE_RE,
    _DIALECTIC_MAX_EDGES_PER_ENTRY,
    _hypothesis_is_refuted,
    _load_dialectic_blocks,
    dossier_ref_id,
)
from precis.quest.logbook import MEASURED_BY, append_entry
from precis.store.types import ChunkInsert
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

__all__ = ["mint_measurement_rulings"]


def _experiment_structures(text: str) -> list[int]:
    """The structure ref ids an experiment entry pre-registers — its inline
    ``[st…]`` handles, in citation order, deduped, capped at the dialectic's
    own per-entry fan-out ceiling (a handle-stuffed model payload must not
    fan mints out unboundedly — same rationale as
    :func:`precis.quest.dossier._mint_evidence_edges`; a real experiment
    names one or two structures, not 8+)."""
    out: list[int] = []
    for h in dict.fromkeys(_DIALECTIC_HANDLE_RE.findall(text)):
        parsed = handle_registry.parse(h)
        if parsed is None:
            continue
        kind, is_chunk, hid = parsed
        if kind == "structure" and not is_chunk:
            out.append(hid)
    return out[:_DIALECTIC_MAX_EDGES_PER_ENTRY]


def _measuring_pathway(store: Store, structure_id: int, tier: str) -> int | None:
    """The pathway ref that produced the structure's canonical barrier —
    :func:`precis.quest.compute._find_tier_pathway` on the stamped
    ``barrier_fidelity`` (ladder-aware dispatches only; a legacy candidate's
    pre-ladder pathway is invisible and the ruling degrades to no ``[pw…]``
    handle / no ``tests`` edge rather than guessing)."""
    from precis.quest.compute import _find_tier_pathway

    try:
        return _find_tier_pathway(store, structure_id, tier)
    except Exception:
        return None


def _existing_ruling(store: Store, ruling_key: str) -> int | None:
    """Converge on an already-minted ruling when the entry-chunk bookmark
    was lost (belt to the ``meta.rulings`` suspenders)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'finding' "
            "AND retired_at IS NULL AND meta->>'sim_ruling_key' = %s "
            "ORDER BY ref_id LIMIT 1",
            (ruling_key,),
        ).fetchone()
    return int(row[0]) if row else None


def _mint_ruling_finding(
    store: Store,
    *,
    quest_id: int,
    hypothesis_id: int,
    structure_id: int,
    structure_title: str,
    pathway_id: int | None,
    barrier: float,
    span: float | None,
    tier: str,
    experiment_text: str,
    ruling_key: str,
) -> int:
    """Insert the templated ruling finding (title + ``finding_body`` chunk,
    the :func:`precis.taproot.hub.mint_hub` shape minus the nanopub
    machinery — a sim ruling is internal by design and never a claim hub)."""
    title = (
        f"Measured: {structure_title} — rate-limiting barrier "
        f"{barrier:.3f} eV (trusted, {tier} tier)."
    )
    pw_clause = f", measured by [pw{pathway_id}]" if pathway_id is not None else ""
    span_clause = f" Kozuch–Shaik span: {span:.3f} eV." if span is not None else ""
    body = (
        "Code-minted measurement ruling (templated text — no LLM authored "
        "it).\n\n"
        f"The pre-registered discriminating experiment for [fi{hypothesis_id}] "
        f"has a trusted measurement: [st{structure_id}] {structure_title} — "
        f"rate-limiting barrier {barrier:.3f} eV ({tier} tier, "
        f"barrier_trusted=true{pw_clause}).{span_clause}\n\n"
        f'Pre-registered experiment: "{experiment_text}"\n\n'
        "Interpretation is the next tick's job: support/counter/settle "
        f"[fi{hypothesis_id}] against the pre-registered branch predictions, "
        "citing this ruling. Sim-based rulings settle internal hypotheses "
        "only — never nanopub evidence."
    )
    ref = store.insert_ref(
        kind="finding",
        slug=None,
        title=title,
        meta={
            "source": "quest-measurement-ruling",
            "sim_ruling": True,
            "sim_ruling_key": ruling_key,
            "quest": quest_id,
            "hypothesis": hypothesis_id,
            "structure": structure_id,
            "pathway": pathway_id,
            "barrier_eV": barrier,
            "tier": tier,
        },
    )
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "finding_body"})],
    )
    return int(ref.id)


def mint_measurement_rulings(store: Store, quest_id: int) -> int:
    """Scan the quest's dialectic blocks and mint one measurement-ruling
    finding per (pre-registered experiment structure × trusted measurement)
    not yet ruled. Returns the number minted. Never raises past a single
    candidate — a bad block/structure is skipped, not fatal (the tick's
    degrade-don't-crash convention); the caller additionally wraps the whole
    pass.

    Deliberately a **read-driven pre-pass**: it runs before the tick's
    prompt assembly (:meth:`precis.quest.tick._TickRun._stage_llm`) so a
    freshly-minted ruling renders in the same tick's dialectic view as a
    ``measured:`` line, and it creates nothing when there is nothing to
    rule (no dossier → no-op, never a write).
    """
    did = dossier_ref_id(store, quest_id)
    if did is None:
        return 0
    blocks = _load_dialectic_blocks(store, did)
    minted = 0
    for block in blocks:
        if block.settled or _hypothesis_is_refuted(store, block.hypothesis_id):
            continue
        exp = next((e for e in block.entries if e.role == "experiment"), None)
        if exp is None or exp.handle is None:
            continue
        rulings: dict[str, Any] = dict(exp.rulings)
        dirty = False
        for st_id in _experiment_structures(exp.text):
            try:
                sref = store.get_ref(kind="structure", id=st_id)
                if sref is None:
                    continue
                smeta = sref.meta or {}
                if smeta.get("barrier_trusted") is not True:
                    continue
                barrier = smeta.get("barrier")
                if not isinstance(barrier, (int, float)) or isinstance(barrier, bool):
                    continue
                tier = str(smeta.get("barrier_fidelity") or "neb")
                pw_id = _measuring_pathway(store, st_id, tier)
                key = (
                    f"st{st_id}:pw{pw_id}"
                    if pw_id is not None
                    else f"st{st_id}:{tier}:{float(barrier):.6g}"
                )
                if key in rulings:
                    continue
                ruling_key = f"qu{quest_id}:fi{block.hypothesis_id}:{key}"
                fid = _existing_ruling(store, ruling_key)
                converged = fid is not None
                if fid is None:
                    span = smeta.get("span")
                    fid = _mint_ruling_finding(
                        store,
                        quest_id=quest_id,
                        hypothesis_id=block.hypothesis_id,
                        structure_id=st_id,
                        structure_title=str(sref.title or f"st{st_id}"),
                        pathway_id=pw_id,
                        barrier=float(barrier),
                        span=float(span)
                        if isinstance(span, (int, float)) and not isinstance(span, bool)
                        else None,
                        tier=tier,
                        experiment_text=exp.text,
                        ruling_key=ruling_key,
                    )
                if pw_id is not None:
                    # measurement → tests → hypothesis (migration 0142) —
                    # idempotent on the unique tuple; NOT an evidence edge.
                    try:
                        store.add_link(
                            src_ref_id=pw_id,
                            dst_ref_id=block.hypothesis_id,
                            relation="tests",
                            meta={"ruling": fid, "quest": quest_id},
                        )
                    except Exception:
                        log.info(
                            "rulings: tests edge pw%s -> fi%s not minted",
                            pw_id,
                            block.hypothesis_id,
                        )
                rulings[key] = fid
                dirty = True
                if not converged:
                    minted += 1
                    append_entry(
                        store,
                        quest_id,
                        text=(
                            f"measurement ruling minted: [fi{fid}] — "
                            f"[st{st_id}] barrier {float(barrier):.3f} eV "
                            f"({tier} tier, trusted) rules on "
                            f"[fi{block.hypothesis_id}]'s pre-registered "
                            "experiment"
                        ),
                        entry_type="result",
                        by=MEASURED_BY,
                    )
            except Exception:
                log.exception(
                    "rulings: skipping st%s for hypothesis fi%s (quest %s)",
                    st_id,
                    block.hypothesis_id,
                    quest_id,
                )
        if dirty:
            store.drafts.patch_chunk_meta(exp.handle, {"rulings": rulings})
    return minted

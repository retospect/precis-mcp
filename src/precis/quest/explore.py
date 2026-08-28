"""The quest's tried-set — a pure DB-fact summary, never a chemistry choice.

**The discovery AGENT owns the chemistry** (what dopant, what site, what
coverage to try next); code owns only the capabilities (the ``slab``/
``add_atom``/``set_element`` ops) and the *guarantee that the agent acts*
(:mod:`precis.quest.tick`'s commit re-prompt + tier-escalation ladder). No
Python here ever enumerates elements, sites, or compositions — this module
reads back what has *already* been tried (and how it measured) so the model
can reason from the real state instead of guessing it, in both the
explorer's-creed prompt block and the commit re-prompt
(:func:`precis.quest.tick._explorers_creed`,
:func:`precis.quest.tick._build_commit_prompt`).
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis.quest.frontier import FrontierResult
    from precis.store import Store

#: Cap on how many tried / ruled-out candidates render in the summary line —
#: a quest with a long history still renders a short, model-legible bullet.
_TRIED_SAMPLE = 12


def tried_set_summary(
    store: Store,
    quest_id: int,
    *,
    fr: FrontierResult | None = None,
    limit: int = _TRIED_SAMPLE,
) -> str:
    """A compact "what's been tried, and how it measured" line.

    Reads the quest's live `structure` candidates and, for each, its measures
    via the same reader the frontier uses
    (:func:`precis.quest.frontier._candidate_from_structure`) plus its
    ``ruled-out:`` tag. Renders measured candidates best-first (by the
    quest's primary rubric objective — ``log_tof`` (max) for a catalyst
    quest post kinetics-cutover, ``energy`` by default), then any
    still-awaiting-a-sim ones, then a ``ruled out:`` clause. E.g.::

        Tried: Ag adatom 0.74 (BEST) · Cu adatom 0.96 · Ni adatom 1.02; \
ruled out: PdCuNi alloy 2.84

    Pure DB fact — no enumeration of chemistry, no opinion on what to try
    next. ``""`` when the quest has no candidates at all yet (so a caller can
    render its own "nothing tried yet" framing instead of an empty bullet).

    ``fr`` reuses an already-computed
    :class:`precis.quest.frontier.FrontierResult` (the same one
    :func:`precis.quest.tick._frontier_summary` / ``_champion`` already built
    for this tick) instead of a second live-candidate scan — each candidate's
    measures come from ``structure.meta`` regardless of which Pareto band
    (``frontier``/``dominated``/``provisional``/``unevaluated``) it landed
    in, so reusing ``fr``'s full candidate set (its four bands, concatenated)
    is exactly equivalent to a fresh scan, just without repeating the
    per-candidate ``struct_runs`` read (an N+1) a second/third/... time in
    the same tick. ``None`` (default, and every unit test) computes it fresh
    — unit-testable standalone.

    A ``provisional`` candidate (measured but unconfirmed — an untrusted
    barrier, or a barrier with no converged relax yet) still counts as
    "tried": its :class:`precis.quest.frontier.ProvisionalCandidate.measures`
    (the merged trusted + recovered-untrusted view) is read via
    ``dataclasses.replace`` onto a throwaway :class:`Candidate` copy, so it
    sorts into the same list below — but it renders NAME-ONLY
    (``(measured, unconfirmed)``), no value and never the ``(BEST)`` label:
    this line exists for proposal dedup, and an untrusted number in it reads
    as a prior (the prompt's authority rule: the frontier table is the only
    number source).
    """
    if fr is None:
        from precis.quest.frontier import quest_frontier

        fr = quest_frontier(store, quest_id)

    provisional_as_candidates = [
        dataclasses.replace(pc.candidate, measures=pc.measures) for pc in fr.provisional
    ]
    candidates = [
        *fr.frontier,
        *fr.dominated,
        *provisional_as_candidates,
        *fr.unevaluated,
    ]
    if not candidates:
        return ""

    key, sense = fr.objectives[0] if fr.objectives else ("energy", "min")

    provisional_ids = {pc.candidate.ref_id for pc in fr.provisional}
    measured: list[tuple[float, str, bool]] = []
    unmeasured: list[str] = []
    ruled_out: list[str] = []
    for cand in candidates:
        is_ruled_out = any(
            str(t).startswith("ruled-out:") for t in store.tags_for(cand.ref_id)
        )
        value = cand.measures.get(key)
        if is_ruled_out:
            ruled_out.append(
                f"{cand.name} {value:g}" if value is not None else cand.name
            )
        elif value is not None:
            measured.append((value, cand.name, cand.ref_id in provisional_ids))
        else:
            unmeasured.append(f"{cand.name} (awaiting)")

    measured.sort(key=lambda t: t[0] if sense == "min" else -t[0])
    # (BEST) marks the best CONFIRMED value only. A provisional entry renders
    # NAME-ONLY: this line exists for proposal dedup, and an untrusted number
    # here reads as a prior — the first post-reset tick built a whole
    # "disappeared sub-0.7 eV leads / data loss" theory from ≈-marked
    # pre-trust values (incl. a broken-NEB ≈0). The frontier table is the
    # only number source; names alone still say "tried, don't re-propose".
    best_confirmed = next(
        (i for i, (_, _, prov) in enumerate(measured[:limit]) if not prov), None
    )
    bits: list[str] = []
    for i, (value, name, prov) in enumerate(measured[:limit]):
        if prov:
            bits.append(f"{name} (measured, unconfirmed)")
            continue
        best = " (BEST)" if i == best_confirmed else ""
        bits.append(f"{name} {value:g}{best}")
    bits.extend(unmeasured[: max(0, limit - len(bits))])

    if not bits and not ruled_out:
        return ""
    line = "Tried: " + (" · ".join(bits) if bits else "(none measured or awaiting)")
    if ruled_out:
        line += "; ruled out: " + " · ".join(ruled_out[:limit])
    return line


__all__ = ["tried_set_summary"]

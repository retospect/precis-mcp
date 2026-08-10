"""Narrative growth-ratchet gate — the dossier-hygiene design (quest package docstring).

The dossier narrative is a whole-rewritten "rolling context" paragraph
(:mod:`precis.quest.dossier`) meant to stay bounded, but nothing enforced
that: the only pressure was the prompt phrase "Keep the dossier tight", so a
model that copies everything forward each rewrite makes it accrete without
bound. The failure mode is ACCRETION, not length per se — a long-but-dense
narrative that reflects real new evidence is fine (it's semantically
searchable and structured); a narrative that grows while nothing happened is
the bug. So the gate here is a growth *ratchet* proportional to evidence of
progress, not a fixed style cap (a fixed word budget was tried and rejected
— it punishes dense, genuinely new content and can't tell accretion from
progress apart), plus a pathological-runaway ceiling tripwire.

Pure and side-effect-free: :func:`narrative_growth_gate` only decides
accept/retry from word counts + a caller-supplied progress fact. The caller
(:mod:`precis.quest.tick`'s ``run_quest_tick`` today; a future
``draft_refresh`` job for the paper-writing pipeline — docs/backlog/
draft-refresh.md — tomorrow) owns fetching the word counts, deciding what
counts as *this* rewrite's "progress" in its own context, dispatching a
compress re-prompt worded for :data:`GateResult.reason`, and logging the
outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Prompt-guidance-only target (design decision: "a
#: fixed word budget was considered and rejected") — surfaced in the tick
#: prompt, never enforced by :func:`narrative_growth_gate` itself.
_DEFAULT_TARGET_WORDS = 800
#: A rewrite may exceed the previous narrative's word count by this
#: fraction...
_DEFAULT_RATCHET_PCT = 0.15
#: ...plus this many flat words, before progress evidence is required.
_DEFAULT_RATCHET_FLAT = 50
#: Pathological-runaway tripwire — over this, retry regardless of progress.
_DEFAULT_CEILING_WORDS = 2500


@dataclass(frozen=True)
class NarrativeBudgetConfig:
    """Tunables for :func:`narrative_growth_gate`. Defaults are
    the dossier-hygiene design's decided numbers; per-owner overrides
    come from the owner ref's ``meta.dossier`` via :func:`config_from_meta`."""

    target_words: int = _DEFAULT_TARGET_WORDS
    ratchet_pct: float = _DEFAULT_RATCHET_PCT
    ratchet_flat: int = _DEFAULT_RATCHET_FLAT
    ceiling_words: int = _DEFAULT_CEILING_WORDS


@dataclass(frozen=True)
class GateResult:
    """``ok=True`` accepts the rewrite as-is. ``ok=False`` is the caller's
    retry cue — ``reason`` is ``"ceiling"`` (over the hard cap, regardless of
    progress) or ``"no-progress-growth"`` (over the ratchet with no evidence
    of progress this cycle) — for the compress re-prompt's wording and the
    eventual logbook entry."""

    ok: bool
    reason: str | None = None


def _positive_int(raw: Any, default: int) -> int:
    """``int(raw)`` when it parses to a positive value, else ``default`` —
    never raises on a malformed per-owner override."""
    try:
        n = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def config_from_meta(meta: dict[str, Any] | None) -> NarrativeBudgetConfig:
    """Read ``meta.dossier.narrative_word_target`` /
    ``.narrative_word_ceiling`` overrides off an owner ref's ``meta`` —
    missing or malformed values fall back to the defaults, never raise. The
    ratchet's percentage/flat allowance is not (yet) override-able — only
    the target and the ceiling are, matching the dossier-hygiene design."""
    dossier_meta = (meta or {}).get("dossier")
    d = dossier_meta if isinstance(dossier_meta, dict) else {}
    return NarrativeBudgetConfig(
        target_words=_positive_int(
            d.get("narrative_word_target"), _DEFAULT_TARGET_WORDS
        ),
        ceiling_words=_positive_int(
            d.get("narrative_word_ceiling"), _DEFAULT_CEILING_WORDS
        ),
    )


def narrative_growth_gate(
    prev_words: int,
    new_words: int,
    progress_evidence: bool,
    config: NarrativeBudgetConfig | None = None,
) -> GateResult:
    """Accept a proposed narrative rewrite, or flag it for a compress retry.

    ``prev_words``/``new_words`` are the previous/proposed narrative's word
    counts (the ledger tree never counts toward this — narrative only, it is
    the part of the dossier that is *supposed* to persist).
    ``progress_evidence`` is the caller's own yes/no fact for "did this cycle
    produce something the machinery can already see" (an applied ledger op,
    a frontier update from a harvest, a citation mint — whatever the
    caller's context makes available; this function doesn't compute it).
    The ceiling trips regardless of progress; the ratchet only trips absent
    progress — "interesting is allowed to be large-ish". ``prev_words <= 0``
    (a fresh dossier's very first rewrite — nothing to have accreted from
    yet) skips the ratchet entirely; only the ceiling still applies.
    """
    cfg = config or NarrativeBudgetConfig()
    if new_words > cfg.ceiling_words:
        return GateResult(False, "ceiling")
    if prev_words <= 0:
        return GateResult(True, None)
    allowed = prev_words * (1 + cfg.ratchet_pct) + cfg.ratchet_flat
    if new_words > allowed and not progress_evidence:
        return GateResult(False, "no-progress-growth")
    return GateResult(True, None)


__all__ = [
    "GateResult",
    "NarrativeBudgetConfig",
    "config_from_meta",
    "narrative_growth_gate",
]

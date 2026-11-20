"""Skill discovery at planner and quest prompt assembly.

Two seams over one lazily built, process-wide index:

- :func:`match_sections` + :func:`render_section_injection` — the
  planner-prompt and quest-tick injection path (gr702892). The task's
  own sentences supply bounded search legs: at most
  ``_FUSED_QUERIES_CAP`` sub-questions, each embedded against the skill
  corpus's question-shaped chunks, the confident hits fused (gr568491's
  ``queries=`` machinery in :mod:`precis.handlers.skill`) into one
  section ranking. Every sub-question's top section is a winner and
  lands in the selection before any fisheye filler, so the render-time
  byte budget (:data:`_MAX_INJECT_BYTES`) cuts filler, never a winner.
  The renderer emits the attributed index table plus the selected
  section bodies — sections, never whole skills. Only high-confidence
  question and heading hits can enter the ranking; anything short of
  the threshold injects nothing (the bimodal contract — an almost-right
  skill sitting in context is worse than silence).
- :func:`match_skill` — the older bimodal whole-skill probe autoskill's
  thin-history path uses. One task text, one threshold-clearing skill;
  below threshold it returns ``None`` and nothing injects.

Both seams degrade to a silent no-op (empty selection / ``None``) when
no embedder is configured, mirroring :class:`~precis.skill_index.index.
FileCorpusIndex`'s own "semantic unavailable → caller's existing
behaviour" contract — a harness-controlled tick must never fail because
injection couldn't run.

Reuses :class:`~precis.skill_index.index.FileCorpusIndex` (same class,
same on-disk cache namespace ``SkillHandler`` uses) rather than building
a parallel index — a second `FileCorpusIndex` instance here shares
chunking + embedding logic and, when the cache dir is shared (the
default), the same on-disk cache files.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis._result_budget import DEFAULT_MAX_BODY_BYTES
from precis.skill_index.index import FileCorpusIndex

if TYPE_CHECKING:
    from precis.handlers.skill import _SectionKey, _SkillSearchRow

log = logging.getLogger(__name__)

#: Tier 2 env vars (docs/conventions/env-vars.md) — this is an opt-in,
#: not-loaded-at-boot subsystem, so both are read raw at the call site
#: rather than living on ``PrecisConfig``.
_THRESHOLD_ENV = "PRECIS_SKILL_INJECT_THRESHOLD"
_ENABLE_ENV = "PRECIS_SKILL_INJECT"

#: Score a match must clear to inject. High on purpose — the whole point
#: of §1's question-shaped targets is that a genuine match scores well
#: above casual topical overlap, so the threshold can sit high without
#: starving real hits.
_DEFAULT_THRESHOLD = 0.85

#: Near-miss band for §3 (ledger calibration, not wired by this change):
#: a score within this much below threshold logs at debug so a later
#: calibration pass has visibility into "almost injected, didn't."
_NEAR_MISS_BAND = 0.05

#: How long a tick waits for a cold skill-index build. Ticks run in the
#: background, so waiting beats matching nothing; the interactive
#: default (``skill_index.index._BUILD_WAIT_S``) gives up after 5 s.
_BUILD_WAIT_S = 600.0

#: Chunk variants eligible for injection matching — the question-shaped
#: retrieval targets from chunker.py v4 (front-matter ``summary:``/
#: ``answers:`` twins, and bare section headings). Structural/body_only
#: chunks are prose, not questions; scoring against them would blur the
#: bimodal signal §1 was built to sharpen.
_TARGET_VARIANTS = frozenset({"question_only", "heading_only"})

#: Floor on the over-fetch window (see :func:`_search_page_size`) — large
#: enough that even a tiny/empty corpus (tests) still gets a sane window.
_SEARCH_PAGE_SIZE_FLOOR = 200

#: Multiplier applied to the indexed-skill count to size the over-fetch
#: window generously (see :func:`_search_page_size`).
_SEARCH_PAGE_SIZE_PER_SKILL = 20

#: Byte budget for a rendered :func:`render_section_injection` block —
#: the same derivation ``tests/test_skill_size.py``'s ``FAIL_BYTES`` uses
#: for the corpus (``DEFAULT_MAX_BODY_BYTES - 2048``), so an injected
#: block can never outgrow the whole-skill read the size gate already
#: caps. Counted in UTF-8 bytes, never characters — the transport cap
#: and the skill-file gate both measure bytes, and a char count would
#: let non-ASCII sections breach the ceiling.
_MAX_INJECT_BYTES = DEFAULT_MAX_BODY_BYTES - 2048


@dataclass(frozen=True)
class SkillMatch:
    """One bimodal injection hit — a skill whose score cleared threshold."""

    slug: str
    title: str
    score: float


@dataclass(frozen=True)
class SectionMatch:
    """One selected skill section — a fused-ranking row plus its body.

    ``winner`` marks the section some sub-question ranked first — its
    query's guaranteed top-1. Winners order before fisheye filler in
    the selection, so the render-time byte budget drops filler first.
    """

    slug: str
    chunk_idx: int | None
    row: _SkillSearchRow
    attribution: str
    body: str
    winner: bool


@dataclass(frozen=True)
class SectionSelection:
    """:func:`match_sections`'s result: the ranked, body-deduped matches
    plus the sub-question count the rendered table attributes.

    ``overflow`` holds each sub-question's winner that the per-skill or
    total row caps kept out of ``matches`` — still named, with a
    ``get(kind='skill', id='slug~N')`` link, so a capped winner is never
    silently dropped from the block."""

    matches: tuple[SectionMatch, ...]
    n_queries: int
    overflow: tuple[SectionMatch, ...] = ()


#: Lazily built, process-wide — one embedded index over the skill corpus,
#: reused across calls the same way ``SkillHandler._index`` is. Reset via
#: :func:`_reset_index_for_tests` only; production never needs to.
_index: FileCorpusIndex | None = None

#: Sentinel: a prior :func:`_get_index` call failed to construct an
#: embedder — cached alongside :data:`_index` so a process with no
#: embedder configured (or a broken one) doesn't retry the full,
#: possibly model-loading, construction on every ``match_skill`` call.
#: Reset via :func:`_reset_index_for_tests` only.
_index_construction_failed: bool = False


def _reset_index_for_tests() -> None:
    """Drop the cached index (and cached construction-failure sentinel).
    Test-only — a fresh corpus/cache dir per test needs a fresh
    :class:`FileCorpusIndex`, not the one built (and memoised) by an
    earlier test in the same process, nor a stale "construction already
    failed" verdict from one."""
    global _index, _index_construction_failed
    _index = None
    _index_construction_failed = False


def _threshold() -> float:
    raw = os.environ.get(_THRESHOLD_ENV)
    if raw is None:
        return _DEFAULT_THRESHOLD
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "skill_index.injection: %s=%r is not a float; using default %.2f",
            _THRESHOLD_ENV,
            raw,
            _DEFAULT_THRESHOLD,
        )
        return _DEFAULT_THRESHOLD


def _enabled() -> bool:
    """``PRECIS_SKILL_INJECT=off`` disables injection without embedding.

    Any other value (including unset) leaves injection on — this is an
    opt-OUT knob, not an opt-in one, since the whole point of §2 is that
    injection runs unconditionally at the named harness sites."""
    return os.environ.get(_ENABLE_ENV, "").strip().lower() != "off"


def _default_embedder() -> Any | None:
    """Build an embedder from the process's ``PrecisConfig``.

    Same construction :func:`precis.backfill.workspace.recall_embedder`
    uses for tick-time recall: whatever ``PRECIS_EMBEDDER`` names (``mock``
    in tests — see ``tests/conftest.py``'s session-scoped
    ``_force_mock_embedder_for_tests``), preferring a remote HTTP embedder
    when one is configured so injection never pulls torch into a worker
    process. Returns ``None`` on any construction failure — injection is
    a nice-to-have, never a tick-blocker."""
    try:
        from precis.config import load_config
        from precis.embedder import make_embedder

        cfg = load_config()
        url = cfg.embedder_url.split(",")[0].strip() if cfg.embedder_url else None
        return make_embedder(
            cfg.embedder,
            url=url,
            timeout=cfg.embedder_timeout,
            max_retries=cfg.embedder_max_retries,
        )
    except Exception:
        log.debug("skill_index.injection: embedder construction failed", exc_info=True)
        return None


def _get_index() -> FileCorpusIndex | None:
    """Lazily build and return the injection index, or ``None`` when no
    embedder is available.

    Memoised on the module the same way ``SkillHandler._get_index``
    memoises on the handler instance for the *success* path — but unlike
    ``SkillHandler`` (which just reads a cheap ``hub.embedder`` attribute
    and can afford to retry a ``None`` every call), a failed embedder
    *construction* here is itself expensive (``_default_embedder`` builds
    from ``PrecisConfig``, potentially loading a model) — so that failure
    is cached too, via :data:`_index_construction_failed`, rather than
    retried on every call.
    """
    global _index, _index_construction_failed
    if _index is not None:
        return _index if _index.is_available() else None
    if _index_construction_failed:
        return None
    embedder = _default_embedder()
    if embedder is None:
        _index_construction_failed = True
        log.warning(
            "skill_index.injection: no embedder available; skill injection "
            "disabled for the rest of this process"
        )
        return None

    from precis.handlers.skill import _list_skills, _load_skill

    files: dict[str, str] = {}
    for slug in _list_skills():
        text = _load_skill(slug)
        if text is not None:
            files[slug] = text
    _index = FileCorpusIndex(
        files=files,
        embedder=embedder,
        # Same namespace SkillHandler uses — a shared on-disk cache dir
        # (the default) means the two never re-embed the same skill twice.
        cache_namespace="skill_embeddings",
    )
    return _index if _index.is_available() else None


def _search_page_size() -> int:
    """Corpus-proportional over-fetch window for
    :meth:`~precis.skill_index.index.FileCorpusIndex.search`.

    :func:`match_skill` and :func:`match_sections` rank *all* chunk
    variants and only filter down to :data:`_TARGET_VARIANTS`
    (question-shaped) after the fact — the corpus also carries structural
    and ``body_only`` chunks, which can easily fill a small fixed page
    before the filter ever sees the best question-shaped hit, silently
    dropping it. ``FileCorpusIndex`` scores the whole corpus in memory
    (its module docstring: "sub-ms" for the current size), so a generous
    window costs nothing — sized off the number of indexed skills (each
    contributes several variant chunks) rather than a hand-picked
    constant that can be outgrown."""
    from precis.handlers.skill import _list_skills

    return max(
        _SEARCH_PAGE_SIZE_FLOOR, len(_list_skills()) * _SEARCH_PAGE_SIZE_PER_SKILL
    )


def match_skill(task_text: str) -> SkillMatch | None:
    """Score ``task_text`` against the skill corpus's question-shaped
    targets; return the top skill iff its score clears threshold, else
    ``None``.

    Bimodal by construction: this never returns a second-place or
    below-threshold hit for a caller to render as a lighter-weight cue —
    there is no such tier. ``PRECIS_SKILL_INJECT=off`` short-circuits
    before any embedding happens; ``PRECIS_SKILL_INJECT_THRESHOLD``
    overrides the default 0.85 cutoff.
    """
    if not task_text or not task_text.strip():
        return None
    if not _enabled():
        return None
    index = _get_index()
    if index is None:
        return None

    # Ticks run in the background, so wait out a cold build rather than
    # match nothing (the interactive default gives up after 5 s).
    hits = index.search(
        task_text, page_size=_search_page_size(), build_wait_s=_BUILD_WAIT_S
    )
    best = next((h for h in hits if h.variant in _TARGET_VARIANTS), None)
    if best is None:
        return None

    threshold = _threshold()
    if best.score >= threshold:
        from precis.handlers.skill import _skill_title

        log.info(
            "skill_index.injection: matched %s score=%.3f (threshold=%.3f)",
            best.slug,
            best.score,
            threshold,
        )
        return SkillMatch(
            slug=best.slug, title=_skill_title(best.slug) or best.slug, score=best.score
        )

    if best.score >= threshold - _NEAR_MISS_BAND:
        log.debug(
            "skill_index.injection: near-miss %s score=%.3f (threshold=%.3f)",
            best.slug,
            best.score,
            threshold,
        )
    return None


def match_sections(task_text: str) -> SectionSelection:
    """Fuse the task text's sub-questions into one section ranking.

    The task's first sentences become the sub-questions (near-duplicate-
    folded, at most ``_FUSED_QUERIES_CAP``); each embeds against the
    corpus's question-shaped chunks, and only hits at or above the
    injection threshold enter :func:`~precis.handlers.skill.
    _rank_skill_sections`'s shared fusion. Each sub-question's top
    section is a *winner* and orders before any fisheye filler, so the
    render-time byte budget cuts filler, never a winner's guaranteed
    top-1. A section several sub-questions hit appears once, every
    contributing query in ``attribution``; resolved bodies dedupe, so
    one physical section reached under two spellings (an alias heading
    vs the merged alias group's joined heading) renders a single time.

    Carries :func:`match_skill`'s logging: one info line per matched
    section, a debug line per near-miss (``_NEAR_MISS_BAND`` below
    threshold). Degrades to an empty selection when injection is off,
    no embedder is configured, or nothing clears threshold — no
    cue/snippet tier.
    """
    if not task_text or not task_text.strip():
        return SectionSelection((), 0)
    if not _enabled():
        return SectionSelection((), 0)
    index = _get_index()
    if index is None:
        return SectionSelection((), 0)

    from precis.dispatch import Hub
    from precis.handlers.skill import (
        _FUSED_QUERIES_CAP,
        SkillHandler,
        _fence_fused_sections,
        _fused_phrase_key,
        _rank_skill_sections,
        _section_handle,
    )
    from precis.utils.sentences import split_sentences

    phrasings: dict[str, str] = {}
    for sentence in split_sentences(task_text):
        phrase = sentence.text.strip()
        if key := _fused_phrase_key(phrase):
            phrasings.setdefault(key, phrase)
        if len(phrasings) == _FUSED_QUERIES_CAP:
            break
    if not phrasings:
        return SectionSelection((), 0)

    handler = SkillHandler(hub=Hub())
    threshold = _threshold()
    # Ticks run in the background, so wait out a cold build rather than
    # match nothing (the interactive default gives up after 5 s).
    legs: list[list[tuple[_SectionKey, _SkillSearchRow]]] = []
    for phrase in phrasings.values():
        hits = index.search(
            phrase, page_size=_search_page_size(), build_wait_s=_BUILD_WAIT_S
        )
        confident = [
            hit
            for hit in hits
            if hit.variant in _TARGET_VARIANTS and hit.score >= threshold
        ]
        rows = [
            (key, row)
            for key, row in handler._fused_leg_rows(
                phrase, mode_norm="semantic", semantic_hits=confident
            )
            if key[1] is not None
        ]
        if rows:
            log.info(
                "skill_index.injection: matched %s score=%.3f (threshold=%.3f)",
                _section_handle(rows[0][0]),
                rows[0][1].score,
                threshold,
            )
        else:
            best = next((h for h in hits if h.variant in _TARGET_VARIANTS), None)
            if best is not None and best.score >= threshold - _NEAR_MISS_BAND:
                log.debug(
                    "skill_index.injection: near-miss %s score=%.3f (threshold=%.3f)",
                    best.slug,
                    best.score,
                    threshold,
                )
        legs.append(rows)

    visible_legs, _hidden = _fence_fused_sections(legs)
    ranked, overflow, _more = _rank_skill_sections(visible_legs)
    winner_keys = {rows[0][0] for rows in visible_legs if rows}
    matches: list[SectionMatch] = []
    overflow_matches: list[SectionMatch] = []
    seen_bodies: set[str] = set()
    for bucket, ranked_rows in ((matches, ranked), (overflow_matches, overflow)):
        for section_key, row, attribution in ranked_rows:
            body = handler.get(id=_section_handle(section_key)).body
            # Dedup on the RESOLVED body, not the match's heading
            # spellings: alias twins resolve to the same canonical chunk,
            # and two different keys whose renders come out identical are
            # one physical section and must inject once.
            fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if fingerprint in seen_bodies:
                continue
            seen_bodies.add(fingerprint)
            bucket.append(
                SectionMatch(
                    slug=section_key[0],
                    chunk_idx=section_key[1],
                    row=row,
                    attribution=attribution,
                    body=body,
                    winner=section_key in winner_keys,
                )
            )
    # Winners first, rank order preserved within each group (list.sort
    # is stable) — the byte budget in render_section_injection truncates
    # from the tail, so a winner is the last thing a tight budget drops.
    matches.sort(key=lambda match: not match.winner)
    return SectionSelection(tuple(matches), len(phrasings), tuple(overflow_matches))


def render_section_injection(selection: SectionSelection) -> str:
    """Render ``selection`` as the one shared injection block.

    The attributed index table (section handle per row, so the model can
    ``get(kind='skill', id='slug~N')`` for more) followed by the selected
    section bodies in ranking order. Winners the row caps kept out of
    ``matches`` still render as direct-read links below the table, so a
    sub-question's top-1 is always at least named. The whole block is
    capped at :data:`_MAX_INJECT_BYTES`, counted in UTF-8 bytes — the
    same unit the whole-skill size gate (``tests/test_skill_size.py``'s
    ``FAIL_BYTES``) measures — so an injection can never exceed a
    whole-skill read. Bodies that would breach the cap drop in ranking
    order rather than truncate mid-section. Returns ``""`` for an empty
    selection.
    """
    if not selection.matches and not selection.overflow:
        return ""
    from precis.handlers.skill import _render_fused_table

    ranked = [((m.slug, m.chunk_idx), m.row, m.attribution) for m in selection.matches]
    overflow = [
        ((m.slug, m.chunk_idx), m.row, m.attribution) for m in selection.overflow
    ]
    parts = [
        "# Auto-matched skill sections\n\n",
        _render_fused_table(ranked, selection.n_queries, {}, overflow),
    ]
    size = len("".join(parts).encode("utf-8"))
    if size > _MAX_INJECT_BYTES:
        return ""
    for match in selection.matches:
        extra = len(("\n\n" + match.body).encode("utf-8"))
        if size + extra > _MAX_INJECT_BYTES:
            break
        parts.append("\n\n")
        parts.append(match.body)
        size += extra
    return "".join(parts)


__all__ = [
    "SectionMatch",
    "SectionSelection",
    "SkillMatch",
    "match_sections",
    "match_skill",
    "render_section_injection",
]

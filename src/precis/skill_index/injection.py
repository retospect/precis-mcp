"""Harness skill discovery using the interactive fused section renderer.

Planner and quest ticks split task text into sub-questions and inject a
bounded section index through :func:`discover_sections`. Retrieval failure
is a silent no-op so discovery never aborts a tick. The older bimodal
``match_skill`` / ``render_injection`` API remains for autoskill history
fallback callers; new tick discovery does not inject whole skills.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from precis.skill_index.index import FileCorpusIndex

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


@dataclass(frozen=True)
class SkillMatch:
    """One bimodal injection hit — a skill whose score cleared threshold."""

    slug: str
    title: str
    score: float


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

    :func:`match_skill` ranks *all* chunk variants and only filters down
    to :data:`_TARGET_VARIANTS` (question-shaped) after the fact — the
    corpus also carries structural and ``body_only`` chunks, which can
    easily fill a small fixed page before the filter ever sees the best
    question-shaped hit, silently dropping it. ``FileCorpusIndex`` scores
    the whole corpus in memory (its module docstring: "sub-ms" for the
    current size), so a generous window costs nothing — sized off the
    number of indexed skills (each contributes several variant chunks)
    rather than a hand-picked constant that can be outgrown."""
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


def render_injection(match: SkillMatch) -> str:
    """Render the WHOLE matched skill body, prefixed with a one-line
    banner naming the skill and that it was auto-matched.

    No truncation tier, no snippet mode — the whole point of the bimodal
    contract (module docstring) is that a caller either gets the perfect
    skill in full, or gets nothing from :func:`match_skill` at all."""
    from precis.handlers.skill import _load_skill

    body = _load_skill(match.slug) or ""
    header = (
        f"# Auto-matched skill: {match.slug}\n\n"
        f"This skill was matched to your current task automatically "
        f"(score {match.score:.2f}) and is injected in full below — no "
        f"`get(kind='skill', ...)` needed.\n\n---\n\n"
    )
    return header + body


__all__ = [
    "SkillMatch",
    "match_skill",
    "render_injection",
]


def discover_sections(task_text: str) -> str:
    """Discover task sub-questions through the shared fused section renderer.

    Task sentences and list items are independent goal phrasings. The
    result is a bounded index; agents fetch only the sections they need.
    Retrieval failure must never abort a planner tick.
    """
    if not task_text.strip() or not _enabled():
        return ""
    try:
        import re

        from precis.dispatch import Hub
        from precis.handlers.skill import SkillHandler, _dedupe_phrasings

        index = _get_index()
        if index is None:
            return ""
        questions = _dedupe_phrasings(
            [
                part.strip(" -\t")
                for part in re.split(r"\n+|(?<=[?!;])\s+", task_text)
                if part.strip(" -\t")
            ]
        )[:5]
        handler = SkillHandler(hub=Hub())
        handler._index = index
        result = handler._search_fused(
            questions,
            mode_norm="hybrid",
            tag=None,
            tags=None,
            kinds=None,
            family=None,
            configured_only=False,
        )
        return "# Skill sections for this task\n\n" + result.body
    except Exception:
        log.debug("skill section discovery failed", exc_info=True)
        return ""

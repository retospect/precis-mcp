"""cast_common — shared substrate for the audio *casts* (morning brief + nidra).

A **cast** is a daily audio episode composed as a ``draft`` and narrated onto the
shipped podcast feed (docs/design/audio-feed.md). Two standing casts ride the same
produce → narrate → publish spine, differing only by a **voice profile**:

- ``reading`` — the morning situational-awareness brief (voice ``bm_george``).
- ``nidra``   — the evening conceptual-walk meditation (voice ``af_nicole``).

This module holds what both producers, the audio pass, and the CLI share:

* :data:`CAST_PROFILES` — the per-cast profile table (voice · rate · model ·
  target length · cron · slugs · title).
* the word-budget arithmetic — target minutes ↔ words ↔ ``max_tokens`` via a
  per-voice speaking rate (we never *guess* minutes: the synth reports exact
  duration, but word count is the only lever we have *before* the expensive TTS).
* :func:`create_cast_draft` — the **standalone dated-draft** creator, idempotent
  per ``(cast, date)``. Deliberately NOT ``Store.create_draft``: that binds a
  draft 1:1 to a project and raises on the second draft under the same relation,
  which a *daily* cast would trip on day two.
* :func:`voice_skill_preamble` — loads the ``precis-voice`` craft skill to prepend
  to the compose prompt (degrades to ``""`` when unavailable).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: A ``(ref_id, relation)`` a cast drew on — the source ref it spoke about. A
#: cast names its sources but reads no URL aloud (the voice contract), so nothing
#: is cited inline; :func:`link_sources` writes these edges from the composed
#: draft so a listener can later reopen the paper / concept / quest it mentioned.
Source = tuple[int, str]


def link_sources(
    store: Any,
    draft_id: int,
    sources: list[Source],
    *,
    via: str,
    date_tag: str,
) -> int:
    """Link a composed cast ``draft`` back to each source ref it drew on.

    Shared by both producers (the morning brief's papers/findings/quests, the
    evening nidra's concepts). Best-effort and non-fatal: a bad edge (a
    since-deleted ref, an unknown relation) is logged and skipped — a broken
    back-link must never lose a cast that was already composed and stored.
    ``via`` tags each edge's ``meta`` with the producer for provenance. Returns
    the count of edges written.
    """
    written = 0
    for ref_id, relation in sources:
        try:
            store.add_link(
                src_ref_id=int(draft_id),
                dst_ref_id=int(ref_id),
                relation=relation,  # type: ignore[arg-type]
                set_by="agent",
                meta={"via": via, "date": date_tag},
            )
            written += 1
        except Exception:  # pragma: no cover - per-edge isolation
            log.warning(
                "%s: could not link draft %s → ref %s (%s)",
                via,
                draft_id,
                ref_id,
                relation,
                exc_info=True,
            )
    return written


@dataclass(frozen=True)
class CastProfile:
    """One standing cast's profile — the knobs that differ between casts."""

    cast: str  # id: "reading" | "nidra"
    voice: str  # kokoro voice for the whole cast (per-chunk meta.voice can override)
    wpm: int  # speaking rate — target_minutes → word budget
    model: str  # litellm alias for the compose call
    target_minutes: int
    cron: str  # daily schedule for the recurring watch
    job_type: str  # the compose job_type that produces this cast
    slug_prefix: str  # dated-draft slug + episode-id stem
    title: str  # episode/draft title (date appended)
    source: str  # publish_episode producer tag — DISTINCT per cast
    #: ("brief", "meditation", "news"); a shared feed can subfilter by it, so a
    #: cast must not borrow another's tag. Required (no default) for that reason.
    export_stem: str  # human export basename stem — the mp3 episode-id +
    #: exported PDF filename become ``<export_stem>_<date>`` (e.g.
    #: ``morning_brief_2026-07-21.mp3`` / ``.pdf``). Distinct from the internal
    #: ``slug_prefix`` (``cast-reading``), which stays the DB draft slug so
    #: idempotency + already-published episodes are undisturbed.
    folder: str  # Drive folder title the cast draft is auto-placed under
    #: (e.g. "Morning brief") so its text (and audio/PDF links) show up in
    #: /drive alongside the other authored artifacts (ADR 0045).


CAST_PROFILES: dict[str, CastProfile] = {
    "reading": CastProfile(
        cast="reading",
        voice="bm_george",
        wpm=150,
        model="claude-opus",
        target_minutes=20,
        cron="0 6 * * *",
        job_type="reading_brief",
        slug_prefix="cast-reading",
        title="\U0001f305 Morning brief",  # 🌅
        source="brief",
        export_stem="morning_brief",
        folder="Morning brief",
    ),
    "nidra": CastProfile(
        cast="nidra",
        voice="af_nicole",
        wpm=110,
        model="claude-opus",  # a nice model composes a lovely nidra (once a day)
        target_minutes=45,
        cron="0 21 * * *",
        job_type="meditation",
        slug_prefix="cast-nidra",
        title="\U0001f319 Evening meditation",  # 🌙
        source="meditation",
        export_stem="evening_meditation",
        folder="Evening meditation",
    ),
}

#: A single-completion word ceiling; above this a producer composes in segments
#: (multiple LLM calls concatenated) rather than one over-long generation.
SINGLE_CALL_WORD_CEILING = 2500
#: Rough tokens-per-word for sizing ``max_tokens`` on a compose call.
_TOKENS_PER_WORD = 1.4


def word_budget(profile: CastProfile, *, target_minutes: int | None = None) -> int:
    """Target spoken word count = minutes × the voice's words-per-minute rate."""
    minutes = target_minutes if target_minutes is not None else profile.target_minutes
    return max(1, int(minutes * profile.wpm))


def compose_max_tokens(
    profile: CastProfile, *, target_minutes: int | None = None
) -> int:
    """A generous ``max_tokens`` for a single-call compose at this length."""
    return int(word_budget(profile, target_minutes=target_minutes) * _TOKENS_PER_WORD)


def voice_skill_preamble() -> str:
    """The ``precis-voice`` craft skill body, or ``""`` when unavailable.

    Prepended to a producer's compose system prompt so the model writes for the
    ear (relationships not formulas, expanded abbreviations, no slashes, …).
    Best-effort — a missing skill degrades to no preamble, never an error.
    """
    try:
        from precis.handlers.skill import _load_skill

        return _load_skill("precis-voice") or ""
    except Exception:  # pragma: no cover - skill loading is best-effort
        log.debug("cast_common: precis-voice skill unavailable", exc_info=True)
        return ""


def cast_slug(profile: CastProfile, date_tag: str) -> str:
    """The deterministic per-day draft slug (the internal DB address)."""
    return f"{profile.slug_prefix}-{date_tag}"


def export_stem(profile: CastProfile, date_tag: str) -> str:
    """The human export basename (episode-id + PDF filename stem).

    ``<export_stem>_<date>`` — e.g. ``morning_brief_2026-07-21`` — so the
    published mp3 and the compiled PDF share a stem a listener recognises,
    distinct from the internal ``cast-reading-<date>`` DB slug.
    """
    return f"{profile.export_stem}_{date_tag}"


def export_basename_for_meta(meta: dict[str, Any] | None) -> str | None:
    """The export stem for a *cast* draft, derived from its ``meta``.

    Returns ``<export_stem>_<date>`` when ``meta`` names a known cast and a
    date (the shape :func:`create_cast_draft` stamps), else ``None`` so a
    non-cast draft falls back to its slug for export filenames.
    """
    if not meta:
        return None
    profile = CAST_PROFILES.get(str(meta.get("cast") or ""))
    date_tag = str(meta.get("date") or "").strip()
    if profile is None or not date_tag:
        return None
    return export_stem(profile, date_tag)


def ensure_cast_folder(store: Any, profile: CastProfile) -> int | None:
    """Find (or create) the Drive folder this cast files its drafts under.

    Idempotent on the folder title: an existing ``kind='folder'`` with the
    profile's title is reused; otherwise one is created. Best-effort — a
    failure logs and returns ``None`` so folder placement never blocks a cast
    that was already composed and stored.
    """
    try:
        existing = store.folder_ref_ids_by_title(profile.folder)
        if existing:
            return int(existing[0])
        ref = store.insert_ref(kind="folder", slug=None, title=profile.folder)
        return int(ref.id)
    except Exception:  # pragma: no cover - placement is a nicety, never fatal
        log.warning(
            "cast_common: could not ensure folder %r for cast %s",
            profile.folder,
            profile.cast,
            exc_info=True,
        )
        return None


def find_cast_draft(store: Any, profile: CastProfile, date_tag: str) -> Any | None:
    """The existing cast draft for ``(cast, date)``, or ``None``."""
    return store.get_ref(kind="draft", id=cast_slug(profile, date_tag))


def create_cast_draft(
    store: Any,
    *,
    profile: CastProfile,
    date_tag: str,
    slug: str | None = None,
    title: str | None = None,
    meta: dict[str, Any] | None = None,
) -> tuple[Any, bool]:
    """Create (or find) the standalone dated cast draft. Returns ``(ref, created)``.

    Idempotent on the ``<slug_prefix>-<date>`` slug (or an explicit ``slug``
    override): a second call for the same key returns the existing ref with
    ``created=False`` and writes nothing — so a re-fired schedule or a manual
    re-run never duplicates. Call it *after* the compose succeeds (the producer
    adds the body chunks itself), so a mid-compose failure never leaves an empty
    draft that would poison the guard.

    The draft is **standalone** (no ``draft-of`` project binding) on purpose: a
    project owns exactly one draft per relation (``_draft_ops.create_draft`` raises
    otherwise), which a *daily* cast would trip on day two. ``slug`` lets a caller
    key on something other than the date (used by tests + manual one-offs).
    """
    draft_slug = slug or cast_slug(profile, date_tag)
    existing = store.get_ref(kind="draft", id=draft_slug)
    if existing is not None:
        return existing, False
    full_title = title or f"{profile.title} — {date_tag}"
    draft_meta: dict[str, Any] = {
        "cast": profile.cast,
        "voice": profile.voice,
        "date": date_tag,
        **(meta or {}),
    }
    ref = store.insert_ref(
        kind="draft",
        slug=draft_slug,
        title=full_title,
        meta=draft_meta,
    )
    # The ``cite_key`` identifier is inserted ON CONFLICT DO NOTHING, so under a
    # race another ref may already own the slug — leaving ``ref`` an orphan the
    # audio pass would never find. Resolve by slug and adopt the canonical owner.
    canonical = store.get_ref(kind="draft", id=draft_slug)
    if canonical is not None and int(canonical.id) != int(ref.id):
        return canonical, False
    # File the fresh draft under this cast's Drive folder so its text (and the
    # audio/PDF links the Drive row surfaces) live alongside the other authored
    # artifacts (ADR 0045). Best-effort: placement never blocks a stored cast.
    folder_id = ensure_cast_folder(store, profile)
    if folder_id is not None:
        try:
            store.set_parent(int(ref.id), folder_id)
        except Exception:  # pragma: no cover - placement is a nicety, never fatal
            log.warning(
                "cast_common: could not file draft %s under folder %s",
                ref.id,
                folder_id,
                exc_info=True,
            )
    return ref, True


__all__ = [
    "CAST_PROFILES",
    "SINGLE_CALL_WORD_CEILING",
    "CastProfile",
    "Source",
    "cast_slug",
    "compose_max_tokens",
    "create_cast_draft",
    "ensure_cast_folder",
    "export_basename_for_meta",
    "export_stem",
    "find_cast_draft",
    "link_sources",
    "voice_skill_preamble",
    "word_budget",
]

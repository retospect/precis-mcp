"""briefing_cast — the *morning reading-brief* producer (store + LLM → a draft).

The daytime sibling of :func:`precis.reading.meditation.build_meditation`. Composes
a ~15-minute situational-awareness cast as a ``draft`` (voice ``bm_george``) by
unioning several **lanes** of the human's current state, then narrated onto the
podcast feed by the ``cast_audio`` pass. This module owns only the *content*; it is
store + LLM only (no TTS, no audio deps) so it unit-tests with a fake client.

The lanes, each a contributor that **degrades to empty** when its subsystem isn't
live yet (so the brief ships today and gets richer as the reading-prep + quest
layers land):

- **News** (LIVE) — consume today's ``briefing-<date>`` news ref (already composed
  and shipped to Discord); we speak it, we don't re-derive it.
- **System activity** (LIVE) — what moved overnight: papers acquired, drafts
  advanced, findings + open alerts, and "needs-you" / failed-job attention items.
- **Recall** (PARTIAL) — Anki leeches + concepts newly seen; degrades where the
  intake loop is unbuilt.
- **Reading** (stub) — the weekly booklet gist; lights up at reading-prep slice 2.
- **Quest** (stub) — per-quest momentum + deeds; lights up at quest-layer slice 1.

See docs/design/reading-prep-loop.md (§The briefing, Slice 5) and
docs/proposals/quest-layer.md.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from precis.reading.cast_common import (
    CAST_PROFILES,
    CastProfile,
    compose_max_tokens,
    create_cast_draft,
    find_cast_draft,
    voice_skill_preamble,
)
from precis.workers.briefing import _complete_with_retry
from precis.workers.llm_summarize import LlmClient, LlmConfig

log = logging.getLogger(__name__)

#: Overnight window for the "what moved" lanes (24h + 2h overlap, like the news
#: briefing's lookback).
_LOOKBACK_HOURS = 26
#: Cap how many headline titles a lane names, keeping the compose context tight.
_LANE_ITEM_CAP = 12

_MORNING_CONTRACT = (
    "You are composing a spoken MORNING BRIEFING — a ~{minutes}-minute "
    "situational-awareness cast for one person, read aloud in a calm, crisp, "
    "British register (the voice is 'bm_george'). Aim for about {words} words. "
    "\n\n"
    "This is NOT the world news wire — it is THIS person's own state: what the "
    "system did overnight, what they are reading, and what they should recall "
    "today. You are given labelled lanes of raw material below. Weave the "
    "non-empty ones into a flowing, forward-looking brief in a few short "
    "paragraphs; open by orienting to the day, group naturally, and END ON A "
    "SINGLE LIGHT INTENTION for the day. "
    "\n\n"
    "Write for the EAR: describe relationships in plain words, expand "
    "abbreviations and symbols, never read a URL, a slash, a path, a list "
    "marker, or a formula. Be energising, not exhaustive — detail is a tap "
    "away, so summarise and move on rather than enumerating everything. If a "
    "lane is empty, simply don't mention it. Return plain paragraphs separated "
    "by blank lines — no headings, no markup, no bullet points."
)


def _safe_lane(name: str, fn: Callable[[], str]) -> str:
    """Run a lane contributor, swallowing any error to ``""``.

    A single lane's data source misbehaving (a schema drift, an unbuilt view)
    must never lose the whole morning brief — that lane just goes quiet.
    """
    try:
        return (fn() or "").strip()
    except Exception:  # pragma: no cover - defensive per-lane isolation
        log.warning("reading brief: lane %s failed", name, exc_info=True)
        return ""


def _news_brief_text(store: Any, ref_id: int) -> str:
    """Reconstruct a news ref's body (``ord >= 0``) — the composed brief text."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks "
            "WHERE ref_id = %s AND retired_at IS NULL AND ord >= 0 ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return "\n\n".join((r[0] or "").strip() for r in rows if (r[0] or "").strip())


def _lane_news(store: Any, date_tag: str) -> str:
    """Today's ``briefing-<date>`` news ref, consumed verbatim (LIVE)."""
    ref = store.get_ref(kind="news", id=f"briefing-{date_tag}")
    if ref is None:
        return ""
    body = _news_brief_text(store, ref.id)
    return (
        f"NEWS WIRE (today's world briefing, already published):\n{body}"
        if body
        else ""
    )


def _titles(refs: list[Any], cap: int = _LANE_ITEM_CAP) -> list[str]:
    out = []
    for r in refs[:cap]:
        t = (getattr(r, "title", "") or "").strip()
        if t:
            out.append(t)
    return out


def _lane_system_activity(store: Any, *, cutoff: datetime) -> str:
    """What the untiring collaborator did overnight (LIVE)."""
    parts: list[str] = []

    papers = store.list_refs(kind="paper", updated_after=cutoff, limit=_LANE_ITEM_CAP)
    if papers:
        titles = _titles(papers)
        parts.append(
            f"Papers acquired or updated ({len(papers)}): " + "; ".join(titles)
        )

    drafts = [
        r
        for r in store.list_refs(kind="draft", updated_after=cutoff, limit=40)
        if not (r.meta or {}).get("cast")  # exclude the cast drafts themselves
    ]
    if drafts:
        parts.append(f"Drafts advanced ({len(drafts)}): " + "; ".join(_titles(drafts)))

    findings = store.list_refs(
        kind="finding", updated_after=cutoff, limit=_LANE_ITEM_CAP
    )
    if findings:
        parts.append(f"New findings ({len(findings)}): " + "; ".join(_titles(findings)))

    # Open alerts (health/ops conditions) — machine-raised, best-effort.
    try:
        from precis.alerts import STATE_OPEN

        alerts = store.list_refs(kind="alert", tags=[STATE_OPEN], limit=_LANE_ITEM_CAP)
    except Exception:  # pragma: no cover - alert surface optional
        alerts = []
    if alerts:
        parts.append(f"Open alerts ({len(alerts)}): " + "; ".join(_titles(alerts)))

    # "Needs you" / failed-job attention — count only, the detail is a tap away.
    try:
        from precis.handlers import _todo_views

        ask = _todo_views._attention_ask_user(store)
        failed = _todo_views._attention_child_failed(store)
        if ask:
            parts.append(f"{len(ask)} task(s) are waiting on your input.")
        if failed:
            parts.append(f"{len(failed)} task(s) hit a failure and need a decision.")
    except Exception:  # pragma: no cover - attention view optional
        log.debug("reading brief: attention lane unavailable", exc_info=True)

    if not parts:
        return ""
    return "SYSTEM ACTIVITY OVERNIGHT:\n" + "\n".join(f"- {p}" for p in parts)


def _lane_recall(store: Any, *, cutoff: datetime) -> str:
    """Today's recall surface — Anki leeches + newly-seen concepts (PARTIAL)."""
    parts: list[str] = []

    # Anki leeches: bad-recall cards (high lapses / collapsed ease).
    try:
        with store.pool.connection() as conn:
            leeches = conn.execute(
                "SELECT title FROM refs "
                "WHERE kind='anki' AND deleted_at IS NULL "
                "AND ( (meta->'anki_stats'->>'lapses_total')::int >= 4 "
                "   OR (meta->'anki_stats'->>'ease_min')::float <= 2.0 ) "
                "ORDER BY (meta->'anki_stats'->>'lapses_total')::int DESC NULLS LAST "
                "LIMIT %s",
                (_LANE_ITEM_CAP,),
            ).fetchall()
        names = [r[0] for r in leeches if r[0]]
        if names:
            parts.append(
                f"{len(names)} card(s) keep slipping (leeches): " + "; ".join(names)
            )
    except Exception:  # pragma: no cover - anki_stats may be absent
        log.debug("reading brief: anki leech lane unavailable", exc_info=True)

    # This morning's card work (the card_forge pass runs at 05:30, before us):
    # fresh cards to expect on the phone + concepts escalated to the human.
    try:
        with store.pool.connection() as conn:
            minted = conn.execute(
                "SELECT COUNT(*) FROM refs "
                "WHERE kind='anki' AND deleted_at IS NULL "
                "AND meta->>'authored_by' = 'card_forge' AND created_at >= %s",
                (cutoff,),
            ).fetchone()
            escalated = conn.execute(
                "SELECT meta->>'name' FROM refs "
                "WHERE kind='concept' AND deleted_at IS NULL "
                "AND (meta->>'escalated_at')::timestamptz >= %s "
                "ORDER BY meta->>'escalated_at' DESC LIMIT %s",
                (cutoff, _LANE_ITEM_CAP),
            ).fetchall()
        n_minted = int(minted[0] or 0) if minted else 0
        if n_minted:
            parts.append(f"{n_minted} fresh card(s) were forged for you this morning")
        stuck = [r[0] for r in escalated if r[0]]
        if stuck:
            parts.append(
                "These concepts need you, not another card: " + "; ".join(stuck)
            )
    except Exception:  # pragma: no cover - card_forge may not have run yet
        log.debug("reading brief: card_forge lane unavailable", exc_info=True)

    # Concepts newly promoted since the cutoff.
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT meta->>'name' FROM refs "
                "WHERE kind='concept' AND deleted_at IS NULL AND created_at >= %s "
                "ORDER BY created_at DESC LIMIT %s",
                (cutoff, _LANE_ITEM_CAP),
            ).fetchall()
        new_concepts = [r[0] for r in rows if r[0]]
        if new_concepts:
            parts.append("New concepts entered your graph: " + "; ".join(new_concepts))
    except Exception:  # pragma: no cover - concept graph optional
        log.debug("reading brief: concept lane unavailable", exc_info=True)

    if not parts:
        return ""
    return "RECALL TODAY:\n" + "\n".join(f"- {p}" for p in parts)


def _lane_reading(store: Any) -> str:
    """This week's booklet gist. Empty until reading-prep slice 2 (booklet) lands."""
    return ""


def _lane_quest(store: Any) -> str:
    """Per-quest momentum + deeds. Empty until quest-layer slice 1 (the ``quest``
    kind + ``serves`` graph + logbook) lands — see docs/proposals/quest-layer.md."""
    return ""


def _gather_lanes(store: Any, *, date_tag: str, cutoff: datetime) -> dict[str, str]:
    """All lanes, each degrading to ``""``. Keys preserve reading order."""
    return {
        "news": _safe_lane("news", lambda: _lane_news(store, date_tag)),
        "system": _safe_lane(
            "system", lambda: _lane_system_activity(store, cutoff=cutoff)
        ),
        "reading": _safe_lane("reading", lambda: _lane_reading(store)),
        "recall": _safe_lane("recall", lambda: _lane_recall(store, cutoff=cutoff)),
        "quest": _safe_lane("quest", lambda: _lane_quest(store)),
    }


def _compose(
    llm: LlmClient, lanes: dict[str, str], *, date_tag: str, profile: CastProfile
) -> str:
    """One LLM call → the spoken morning-brief script (plain paragraphs)."""
    from precis.reading.cast_common import word_budget

    body = "\n\n".join(v for v in lanes.values() if v)
    contract = _MORNING_CONTRACT.format(
        minutes=profile.target_minutes, words=word_budget(profile)
    )
    preamble = voice_skill_preamble()
    system = f"{preamble}\n\n{contract}".strip() if preamble else contract
    user = f"Today is {date_tag}. Here are the lanes:\n\n{body}"
    out = _complete_with_retry(
        llm,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (getattr(out, "text", "") or "").strip()


def build_reading_briefing(
    store: Any,
    *,
    client: Any = None,  # any object with .complete(messages); tests inject a fake
    now: datetime | None = None,
    date_tag: str | None = None,
) -> int | None:
    """Compose today's morning reading-brief and store it as a ``draft``.

    Returns the draft ref id, ``None`` when there is nothing in any lane or the
    model returned nothing. Idempotent per day: a second call once today's cast
    exists returns the existing draft id without re-composing.
    """
    now = now or datetime.now(UTC)
    date_tag = date_tag or now.date().isoformat()
    profile = CAST_PROFILES["reading"]

    existing = find_cast_draft(store, profile, date_tag)
    if existing is not None:
        log.info("reading brief: %s already composed (ref %s)", date_tag, existing.id)
        return int(existing.id)

    cutoff = now - timedelta(hours=_LOOKBACK_HOURS)
    lanes = _gather_lanes(store, date_tag=date_tag, cutoff=cutoff)
    if not any(lanes.values()):
        log.info("reading brief: no material in any lane — nothing to compose")
        return None

    model = os.environ.get("PRECIS_READING_BRIEF_MODEL") or profile.model
    llm = client or LlmClient(
        replace(
            LlmConfig.from_env(), model=model, max_tokens=compose_max_tokens(profile)
        )
    )
    script = _compose(llm, lanes, date_tag=date_tag, profile=profile)
    if not script:
        log.warning("reading brief: model returned no script")
        return None

    ref, _created = create_cast_draft(store, profile=profile, date_tag=date_tag)
    store.add_chunks(ref_id=ref.id, chunk_kind="paragraph", text=script, split=True)
    log.info("reading brief: composed %s → draft ref %s", date_tag, ref.id)
    return int(ref.id)


__all__ = ["build_reading_briefing"]

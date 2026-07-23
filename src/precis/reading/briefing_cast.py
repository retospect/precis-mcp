"""briefing_cast — the *morning reading-brief* producer (store + LLM → a draft).

The daytime sibling of :func:`precis.reading.meditation.build_meditation`. Composes
a ~20-minute situational-awareness cast as a ``draft`` (voice ``bm_george``) by
unioning several **lanes** of the human's current state, then narrated onto the
podcast feed by the ``cast_audio`` pass. This module owns only the *content*; it is
store + LLM only (no TTS, no audio deps) so it unit-tests with a fake client.

The lanes, each a contributor that **degrades to empty** when its subsystem isn't
live yet (so the brief ships today and gets richer as the reading-prep + quest
layers land):

- **News** (LIVE) — consume today's ``briefing-<date>`` news ref (already composed
  and shipped to Discord); we speak it, we don't re-derive it.
- **System activity** (LIVE) — what moved overnight: papers acquired (the top few
  carrying their abstracts so the brief can go deep on claim/method/significance),
  drafts advanced, findings + open alerts, and "needs-you" / failed-job items.
- **Recall** (PARTIAL) — Anki leeches (carrying the card body + note so the brief
  can *teach* the idea, not just restate it) + concepts newly seen; degrades where
  the intake loop is unbuilt.
- **Reading** (stub) — the weekly booklet gist; lights up at reading-prep slice 2.
- **Quest** (LIVE) — per **active** quest: its momentum + latest deed; the brief's
  closing "on to the quests" report. When nothing is active, a *decaying* nudge
  about the dormant strivings takes its place (doubling cadence, so it fades
  rather than nags); silent when there are no quests at all.

As it renders, each lane also records the source refs it surfaced; the finished
draft is then linked back to them (papers/findings ``cites``, the news wire
``derived-from``, drafts/quests ``related-to``) — the durable graph edges that
reopen a source from the Connections panel.

The *paper* lane additionally hands the model a bracketed ``[[pa<id>]]``
citation token per named paper and asks it to drop that marker inline after any
claim it draws from that paper. The marker renders as a compact ``§`` citation
link in the ``/drafts`` reader and is stripped entirely from the spoken/audio
path (``narrate.speakable`` drops the ``[[…]]`` form), so the cast reads clean
aloud while the page carries in-text citations back to the paper.

See docs/design/reading-prep-loop.md (§The briefing, Slice 5) and
docs/proposals/quest-layer.md.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from precis.reading.cast_common import (
    CAST_PROFILES,
    CastProfile,
    Source,
    create_cast_draft,
    find_cast_draft,
    link_sources,
    voice_skill_preamble,
)
from precis.utils import handle_registry
from precis.utils.llm.router import DispatchClient, Tier
from precis.workers.briefing import _complete_with_retry

log = logging.getLogger(__name__)

#: Overnight window for the "what moved" lanes (24h + 2h overlap, like the news
#: briefing's lookback).
_LOOKBACK_HOURS = 26
#: Cap how many headline titles a lane names, keeping the compose context tight.
_LANE_ITEM_CAP = 12

# ``Source`` (a ``(ref_id, relation)`` a lane surfaced) + ``link_sources`` live in
# cast_common — shared with the nidra producer. Lanes collect the sources they
# render; the composed draft is then linked back to them (nothing is cited inline
# because the brief reads no URL aloud, so the link is the durable pointer back).

_MORNING_CONTRACT = (
    "You are composing a spoken MORNING BRIEFING — a roughly {minutes}-minute "
    "situational-awareness cast for one person, read aloud in a calm, crisp, "
    "British register (the voice is 'bm_george'). Aim for about {words} words. "
    "\n\n"
    "This is NOT the world news wire — it is THIS person's own state: what the "
    "system did overnight, what they are reading, and what they should recall "
    "today. You are given labelled lanes of raw material below. Weave the "
    "non-empty ones into a substantive, well-organised brief of several "
    "paragraphs. Open by orienting to the day in a sentence or two, then get "
    "straight into the material.\n\n"
    "GO DEEP, NOT WIDE — this person is a working scientist, so treat them as "
    "one and give real substance rather than a breezy tour:\n"
    "- NEWS: give a dry, plain recap of the day's world-news wire above — the "
    "headline facts, a sentence or two per story, nothing trivial. No "
    "editorializing, no forced connection to the person's own work, no framing "
    "it as important — this is orientation to the world, not analysis. Save the "
    "depth for the papers below.\n"
    "- PAPERS: introduce each one before you dig in — give the listener a handle "
    "('Here is a paper on …', or by its title / where it comes from) so they know "
    "which paper you mean. Then say what it actually claims (the core result), "
    "HOW the work supports that claim (the method, system, or experimental "
    "approach), and WHY it matters (the gap it fills, what it changes, or the "
    "open question it bears on). Pull a concrete specific from the abstract — a "
    "number, a system, a named result — rather than staying general. Cover a few "
    "papers with real depth rather than listing many by title, and ground every "
    "statement in the abstract you are given — never invent a finding that isn't "
    "there. CITE AS YOU GO: each paper is given with a bracketed citation marker "
    "like '[[pa1234]]'. When you state a claim, method, or number that comes from "
    "a paper, place that paper's marker inline immediately after the sentence it "
    "supports, copied verbatim (brackets and all). These markers are SILENT — "
    "they become a citation link on the page and are removed entirely from the "
    "audio — so cite freely; they never disrupt what is read aloud.\n"
    "- RECALL AND FLASHCARDS: for the cards that keep slipping, actually TEACH "
    "the underlying idea — explain the reasoning behind the answer, connect it "
    "to adjacent concepts, and where it helps, name the mistake that is easy to "
    "make. Do not merely restate the card.\n"
    "- ACTIVITY AND ATTENTION: be concrete about what moved overnight and what "
    "now needs a decision from the person.\n"
    "- QUESTS: CLOSE the brief here — 'on to the quests'. If active quests are "
    "given, report each honestly: whether work is flowing toward it (its "
    "momentum), the latest deed or that it has gone quiet. Report, don't "
    "cheerlead; a stalled quest is named as stalled. If instead you are handed "
    "a DORMANT STRIVINGS nudge (nothing active), deliver it as ONE short, "
    "low-key prompt to revive or rest one — a single sentence or two, not a "
    "full report.\n\n"
    "Structure the brief roughly as: a brief orientation, then the news, then "
    "the papers, then what moved and what needs you, then recall, and finish "
    "on the quests.\n\n"
    "TONE: grounded, specific, and technical-but-plain. NOT inspirational, "
    "vague, or 'newagey' — no affirmations, no motivational filler. Trust the "
    "material to carry the brief.\n\n"
    "Write for the EAR: describe relationships in plain words, expand "
    "abbreviations and symbols, never read a URL, a slash, a path, a list "
    "marker, or a formula aloud. The ONE exception is the '[[pa…]]' citation "
    "markers described above — those are invisible to the ear (stripped from the "
    "audio), so keep them exactly as given. If a lane is empty, simply don't "
    "mention it. Return plain paragraphs separated by blank lines — no headings, "
    "no markup, no bullet points (the '[[pa…]]' citation markers are allowed "
    "inline)."
)

#: How many overnight papers get the full claim/method/significance treatment
#: (each carrying its abstract into the compose context); the rest are named.
_PAPER_DEPTH_CAP = 6
#: Trim each carried abstract to keep the compose context bounded.
_PAPER_ABSTRACT_CHARS = 700
#: How many active quests the closing "on to the quests" report covers.
_QUEST_DEPTH_CAP = 5
#: ``app_state`` key holding the decaying-nudge cursor (JSON: last-fired date +
#: fire count) for the "you have dormant strivings" reminder.
_DORMANT_NUDGE_KEY = "reading_brief:dormant_nudge"
#: The nudge fires on a doubling cadence: base × 2**(fires-1) days between
#: reminders — 1, 2, 4, 8 … — so it fades rather than nags. Capped so it never
#: goes fully silent.
_DORMANT_NUDGE_BASE_DAYS = 1
_DORMANT_NUDGE_MAX_DAYS = 32


def _safe_lane(
    name: str, fn: Callable[[], str], sources: list[Source] | None = None
) -> str:
    """Run a lane contributor, swallowing any error to ``""``.

    A single lane's data source misbehaving (a schema drift, an unbuilt view)
    must never lose the whole morning brief — that lane just goes quiet. If the
    lane appends to ``sources`` and then raises, we roll those partial appends
    back so the linked set never claims a source whose text never made the
    prompt (text and links stay in lock-step per lane).
    """
    mark = len(sources) if sources is not None else 0
    try:
        return (fn() or "").strip()
    except Exception:  # pragma: no cover - defensive per-lane isolation
        if sources is not None:
            del sources[mark:]
        log.warning("reading brief: lane %s failed", name, exc_info=True)
        return ""


def _collect(sources: list[Source] | None, refs: list[Any], relation: str) -> None:
    """Record each titled ref in ``refs`` as a ``relation`` source (best-effort).

    Skips refs with no id or no title — a titleless ref is one the brief never
    names, so there is nothing to point back to. Mirrors the ``title``-skip in
    :func:`_render_papers` / :func:`_titles`, keeping the linked set aligned
    with what the prompt actually mentioned.
    """
    if sources is None:
        return
    for r in refs:
        rid = getattr(r, "id", None)
        title = (getattr(r, "title", "") or "").strip()
        if rid is not None and title:
            sources.append((int(rid), relation))


def _news_brief_text(store: Any, ref_id: int) -> str:
    """Reconstruct a news ref's body (``ord >= 0``) — the composed brief text."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks "
            "WHERE ref_id = %s AND retired_at IS NULL AND ord >= 0 ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return "\n\n".join((r[0] or "").strip() for r in rows if (r[0] or "").strip())


def _lane_news(
    store: Any, date_tag: str, *, sources: list[Source] | None = None
) -> str:
    """Today's ``briefing-<date>`` news ref, consumed verbatim (LIVE)."""
    ref = store.get_ref(kind="news", id=f"briefing-{date_tag}")
    if ref is None:
        return ""
    body = _news_brief_text(store, ref.id)
    if not body:
        return ""
    # The brief is derived from (re-speaks) the wire → point back to it.
    _collect(sources, [ref], "derived-from")
    return f"NEWS WIRE (today's world briefing, already published):\n{body}"


def _titles(refs: list[Any], cap: int = _LANE_ITEM_CAP) -> list[str]:
    out = []
    for r in refs[:cap]:
        t = (getattr(r, "title", "") or "").strip()
        if t:
            out.append(t)
    return out


def _abstract_snippet(meta: Any, *, limit: int = _PAPER_ABSTRACT_CHARS) -> str:
    """A JATS-stripped, length-bounded abstract for a paper ref, or ``""``.

    This is the *substance* the depth-first contract needs: the model can only
    speak to a paper's claim / method / significance if it is handed the
    abstract, not just the title.
    """
    abstract = (meta or {}).get("abstract") if isinstance(meta, dict) else None
    if not isinstance(abstract, str) or not abstract.strip():
        return ""
    try:
        from precis.handlers._paper_format import _strip_jats

        text = _strip_jats(abstract).strip()
    except Exception:  # pragma: no cover - formatter import is best-effort
        text = abstract.strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip() + "…"
    return text


def _cite_token(ref: Any) -> str:
    """The ``[[pa<id>]]`` citation marker for a paper ref, or ``""``.

    Handed to the model beside a paper so it can drop the marker inline after
    a claim; :func:`precis.utils.handle_registry.try_format` gives the record
    handle (``pa<ref_id>``), which the ``/drafts`` reader renders as a compact
    ``§`` citation link and ``narrate.speakable`` strips from the audio.
    """
    rid = getattr(ref, "id", None)
    if rid is None:
        return ""
    handle = handle_registry.try_format("paper", int(rid))
    return f"[[{handle}]]" if handle else ""


def _render_papers(papers: list[Any], *, total: int | None = None) -> str:
    """The overnight papers, rendered claim-first: the top few carry their
    abstracts (so the model can go deep), any tail is named for context.

    Each named paper carries its ``[[pa<id>]]`` citation marker so the model
    can cite it inline. ``total`` is the true overnight count (which may exceed
    the naming cap): the header reports it, and any un-named remainder is noted
    so the brief never under-counts the night's reading."""
    if not papers:
        return ""
    lines: list[str] = []
    for r in papers[:_PAPER_DEPTH_CAP]:
        title = (getattr(r, "title", "") or "").strip()
        if not title:
            continue
        cite = _cite_token(r)
        head = f"  * {title}" + (f" — cite as {cite}" if cite else "")
        snippet = _abstract_snippet(getattr(r, "meta", None))
        lines.append(head + (f"\n    abstract: {snippet}" if snippet else ""))
    tail = [
        f"{title} {cite}".strip()
        for r in papers[_PAPER_DEPTH_CAP:]
        if (title := (getattr(r, "title", "") or "").strip())
        for cite in (_cite_token(r),)
    ]
    if tail:
        lines.append("  * also acquired (title + cite marker): " + "; ".join(tail))
    if not lines:
        return ""
    shown = len(papers)
    count = total if total is not None else shown
    extra = (
        f" — and {count - shown} more not listed here"
        if total is not None and count > shown
        else ""
    )
    return f"Papers acquired or updated ({count}){extra}:\n" + "\n".join(lines)


def _count_papers_since(store: Any, cutoff: datetime) -> int:
    """The true count of papers acquired/updated since ``cutoff``.

    :func:`_render_papers` only *names* up to :data:`_LANE_ITEM_CAP`, so the
    naming cap is not the night's real total — counting off the capped list
    under-reported (the ``"12 papers"`` bug). Mirrors the ``list_refs`` filter
    (``kind='paper'`` · ``updated_at > cutoff`` · not deleted). Best-effort: any
    read error returns ``0`` so the caller falls back to the named count."""
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM refs "
                "WHERE kind = 'paper' AND deleted_at IS NULL AND updated_at > %s",
                (cutoff,),
            ).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:  # pragma: no cover - a count failure just falls back
        log.debug("reading brief: paper count unavailable", exc_info=True)
        return 0


def _lane_system_activity(
    store: Any, *, cutoff: datetime, sources: list[Source] | None = None
) -> str:
    """What the untiring collaborator did overnight (LIVE)."""
    parts: list[str] = []

    papers = store.list_refs(kind="paper", updated_after=cutoff, limit=_LANE_ITEM_CAP)
    rendered_papers = _render_papers(papers, total=_count_papers_since(store, cutoff))
    if rendered_papers:
        parts.append(rendered_papers)
        _collect(sources, papers, "cites")  # open the paper later

    drafts = [
        r
        for r in store.list_refs(kind="draft", updated_after=cutoff, limit=40)
        if not (r.meta or {}).get("cast")  # exclude the cast drafts themselves
    ]
    if drafts:
        parts.append(f"Drafts advanced ({len(drafts)}): " + "; ".join(_titles(drafts)))
        _collect(sources, drafts, "related-to")

    findings = store.list_refs(
        kind="finding", updated_after=cutoff, limit=_LANE_ITEM_CAP
    )
    if findings:
        parts.append(f"New findings ({len(findings)}): " + "; ".join(_titles(findings)))
        _collect(sources, findings, "cites")

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

    # Anki leeches: bad-recall cards (high lapses / collapsed ease). Carry the
    # card body + any back-extra note so the brief can TEACH the idea (the
    # depth-first contract), not merely restate the title.
    try:
        with store.pool.connection() as conn:
            leeches = conn.execute(
                "SELECT title, meta->'fields'->>'Text', "
                "       meta->'fields'->>'Back Extra' FROM refs "
                "WHERE kind='anki' AND deleted_at IS NULL "
                "AND ( (meta->'anki_stats'->>'lapses_total')::int >= 4 "
                "   OR (meta->'anki_stats'->>'ease_min')::float <= 2.0 ) "
                "ORDER BY (meta->'anki_stats'->>'lapses_total')::int DESC NULLS LAST "
                "LIMIT %s",
                (_LANE_ITEM_CAP,),
            ).fetchall()
        cards: list[str] = []
        for title, text, extra in leeches:
            label = (title or "").strip()
            body = (text or "").strip()
            note = (extra or "").strip()
            if not label and not body:
                continue
            entry = f"  * {label or body[:80]}"
            if body:
                entry += f"\n    card: {body}"
            if note:
                entry += f"\n    note: {note}"
            cards.append(entry)
        if cards:
            parts.append(
                f"{len(cards)} card(s) keep slipping (leeches) — teach the idea, "
                "don't just restate:\n" + "\n".join(cards)
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


def _quest_report(store: Any, quest: Any, *, status: str) -> str:
    """One quest → its striving, lifecycle, momentum, and latest deed (or quiet)."""
    from precis.quest.gaps import quest_momentum
    from precis.quest.logbook import LOG_KIND

    statement = ((getattr(quest, "title", "") or "").splitlines() or [""])[0].strip()
    if not statement:
        return ""
    lines = [f"  * [{status}] {statement}"]

    try:
        m = quest_momentum(store, int(quest.id))
        lines.append(
            f"    momentum: {m.label} — {m.recent_entries} log entries, "
            f"{m.recent_server_events} server events, "
            f"{m.open_todo_servers} open task(s) this past week"
        )
    except Exception:  # pragma: no cover - momentum read is best-effort
        log.debug("reading brief: quest momentum unavailable", exc_info=True)

    # Latest deed (a milestone) if there is one, else the most recent log line.
    try:
        entries = [
            b
            for b in store.list_blocks_for_ref(int(quest.id))
            if b.chunk_kind == LOG_KIND
        ]
        deed = next(
            (
                b
                for b in reversed(entries)
                if (b.meta or {}).get("entry_type") == "milestone"
            ),
            None,
        )
        latest = deed or (entries[-1] if entries else None)
        if latest is not None:
            etype = (latest.meta or {}).get("entry_type", "note")
            stamp = latest.created_at.date().isoformat() if latest.created_at else "?"
            first = ((latest.text or "").splitlines() or [""])[0][:200]
            label = "latest deed" if etype == "milestone" else "latest log"
            lines.append(f"    {label} [{etype} · {stamp}]: {first}")
        else:
            lines.append("    (no logbook entries yet — quiet so far)")
    except Exception:  # pragma: no cover - logbook read is best-effort
        log.debug("reading brief: quest logbook unavailable", exc_info=True)

    return "\n".join(lines)


def _read_nudge_cursor(store: Any) -> tuple[str | None, int]:
    """The dormant-nudge cursor from ``app_state`` → ``(last_fired_iso, fires)``.

    Degrades to ``(None, 0)`` (fire-now) on any read/parse problem.
    """
    try:
        raw = store.get_setting(_DORMANT_NUDGE_KEY)
        if not raw:
            return None, 0
        state = json.loads(raw)
        return state.get("last"), int(state.get("fires", 0))
    except Exception:  # pragma: no cover - malformed marker → fire fresh
        log.debug("reading brief: dormant-nudge cursor unreadable", exc_info=True)
        return None, 0


def _clear_nudge_cursor(store: Any) -> None:
    """Reset the decay so the *next* dormancy nudges from scratch. Called the
    moment a quest is active again (the human re-engaged)."""
    try:
        if store.get_setting(_DORMANT_NUDGE_KEY):
            store.set_setting(
                _DORMANT_NUDGE_KEY, json.dumps({"last": None, "fires": 0})
            )
    except Exception:  # pragma: no cover - best-effort
        log.debug("reading brief: could not clear dormant-nudge cursor", exc_info=True)


def _dormant_nudge(
    store: Any,
    dormant: list[Any],
    *,
    now: datetime,
    sources: list[Source] | None = None,
) -> str:
    """A **decaying** reminder that strivings sit dormant with nothing active.

    Fires on a doubling cadence (1, 2, 4, 8 … days, capped at
    :data:`_DORMANT_NUDGE_MAX_DAYS`) so it fades instead of nagging every
    morning. Advances the ``app_state`` cursor only on the mornings it actually
    fires; returns ``""`` on the quiet mornings in between.
    """
    if not dormant:
        return ""
    last_iso, fires = _read_nudge_cursor(store)
    today = now.date()
    if last_iso:
        try:
            last = date.fromisoformat(last_iso)
        except ValueError:  # pragma: no cover - malformed date → fire now
            last = None
        if last is not None:
            interval = min(
                _DORMANT_NUDGE_MAX_DAYS,
                _DORMANT_NUDGE_BASE_DAYS * (2 ** max(0, fires - 1)),
            )
            if today < last + timedelta(days=interval):
                return ""  # still inside the quiet window — stay silent
    # Fire, and widen the next window.
    try:
        store.set_setting(
            _DORMANT_NUDGE_KEY,
            json.dumps({"last": today.isoformat(), "fires": fires + 1}),
        )
    except Exception:  # pragma: no cover - a failed write just re-nudges tomorrow
        log.debug(
            "reading brief: could not advance dormant-nudge cursor", exc_info=True
        )
    reported = dormant[:_QUEST_DEPTH_CAP]
    # Link the nudged strivings back from the draft (the same graph edge the
    # active-quest branch writes) so "the palladium catalyst" et al. are one
    # click from the report, not just named in prose. Collected only on a
    # firing morning (a quiet morning returned "" above and wrote nothing).
    _collect(sources, reported, "related-to")
    names = _titles(reported)
    return (
        f"DORMANT STRIVINGS ({len(dormant)}) — a fading nudge (this reminder "
        "spaces itself out the longer it's left): no quest is active right now, "
        "and these are set aside. Worth a beat to decide: revive one, or let it "
        "rest?\n" + "\n".join(f"  * {t}" for t in names)
    )


def _lane_quest(
    store: Any, *, now: datetime, sources: list[Source] | None = None
) -> str:
    """The brief's closing "on to the quests" report. **Active quests only** get
    the full momentum + latest-deed treatment; when none is active, a *decaying*
    nudge about the dormant strivings takes its place (and goes quiet again on the
    off-cadence mornings). ``abandoned`` quests are never mentioned. LIVE once
    quests exist; degrades to empty otherwise. See docs/proposals/quest-layer.md."""
    try:
        active = store.list_refs(
            kind="quest", tags=["STATUS:active"], limit=_LANE_ITEM_CAP
        )
    except Exception:  # pragma: no cover - quest kind may be unregistered
        log.debug("reading brief: quest lane unavailable", exc_info=True)
        return ""

    if active:
        # The human is engaged — reset the decay so a future dormancy nudges fresh.
        _clear_nudge_cursor(store)
        reported = active[:_QUEST_DEPTH_CAP]
        blocks = [
            r for q in reported if (r := _quest_report(store, q, status="active"))
        ]
        if not blocks:
            return ""
        _collect(sources, reported, "related-to")  # jump to the quest later
        return (
            "QUESTS IN FLIGHT (the standing strivings above the day's work):\n"
            + "\n".join(blocks)
        )

    # Nothing active → the fading dormant-strivings nudge (empty most mornings).
    try:
        dormant = store.list_refs(
            kind="quest", tags=["STATUS:dormant"], limit=_LANE_ITEM_CAP
        )
    except Exception:  # pragma: no cover - quest kind may be unregistered
        log.debug("reading brief: dormant quest read unavailable", exc_info=True)
        return ""
    return _dormant_nudge(store, dormant, now=now, sources=sources)


def _gather_lanes(
    store: Any, *, date_tag: str, cutoff: datetime, now: datetime
) -> tuple[dict[str, str], list[Source]]:
    """All lanes, each degrading to ``""``. Keys preserve reading order.

    Also returns the ``(ref_id, relation)`` sources the lanes surfaced, deduped,
    so the composed draft can be linked back to the papers / findings / quests it
    drew on — the durable "for later" pointer the spoken brief can't carry.
    """
    src: list[Source] = []
    lanes = {
        "news": _safe_lane(
            "news", lambda: _lane_news(store, date_tag, sources=src), src
        ),
        "system": _safe_lane(
            "system",
            lambda: _lane_system_activity(store, cutoff=cutoff, sources=src),
            src,
        ),
        "reading": _safe_lane("reading", lambda: _lane_reading(store)),
        "recall": _safe_lane("recall", lambda: _lane_recall(store, cutoff=cutoff)),
        "quest": _safe_lane(
            "quest", lambda: _lane_quest(store, now=now, sources=src), src
        ),
    }
    # Dedup on (ref_id, relation), keeping first-seen order (a ref could surface
    # in two lanes — link it once per relation).
    seen: set[Source] = set()
    deduped: list[Source] = []
    for s in src:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return lanes, deduped


def _compose(
    llm: Any, lanes: dict[str, str], *, date_tag: str, profile: CastProfile
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
    lanes, sources = _gather_lanes(store, date_tag=date_tag, cutoff=cutoff, now=now)
    if not any(lanes.values()):
        log.info("reading brief: no material in any lane — nothing to compose")
        return None

    # Fold through the router (ADR 0046) instead of holding a raw litellm
    # client — so this cloud-tier call gets the budget breaker + the route-log
    # (llm_call_log starts capturing real data on this pass). tools_needed=True
    # lands on claude_agent (free-text final answer + system prompt honored,
    # no tools advertised since mcp_config is left unset) rather than the
    # tool-less claude_p judge shape, which would drop the system prompt and
    # demand a parseable JSON block this pass's prose brief never has. A
    # PRECIS_READING_BRIEF_MODEL override still wins, but must now name a real
    # model id (e.g. claude-opus-4-8), not the retired litellm claude-opus alias.
    llm = client or DispatchClient(
        tier=Tier.CLOUD_SUPER,
        model=os.environ.get("PRECIS_READING_BRIEF_MODEL") or None,
        tools_needed=True,
        source="reading_brief",
        log_call=True,
    )
    script = _compose(llm, lanes, date_tag=date_tag, profile=profile)
    if not script:
        log.warning("reading brief: model returned no script")
        return None

    ref, _created = create_cast_draft(store, profile=profile, date_tag=date_tag)
    store.add_chunks(ref_id=ref.id, chunk_kind="paragraph", text=script, split=True)
    # Link the draft back to its sources — the brief names them but reads no URL
    # aloud, so these edges are the only way to reach the paper/finding later.
    n_links = link_sources(
        store, int(ref.id), sources, via="reading_brief", date_tag=date_tag
    )
    log.info(
        "reading brief: composed %s → draft ref %s (%d source link(s))",
        date_tag,
        ref.id,
        n_links,
    )
    return int(ref.id)


__all__ = ["build_reading_briefing"]

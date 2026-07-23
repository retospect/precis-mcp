"""Daily card work — mint today's new anki cards from concepts, and rework the
cards that aren't working (reading-prep loop: cards-as-representations + the
adaptive re-munge, observe-first).

Runs every morning *before* the reading brief (the `card_forge` job_type), so
the day starts with fresh cards on the phone and the brief can report what
changed. Two halves:

**Mint** — up to a daily cap of concepts (candidate/active, no cards yet, newest
first) each get 1–3 cloze cards authored by the model (`precis-cloze` craft),
written as `anki` refs `represents`-linked to their concept. They ride the
existing add-only `precis anki-sync` up to the phone; the stats that come back
feed `reading/mastery.py`.

**Rework** — a precis-authored card that has had a fair chance
(``min_age_days``, default 4) and is still failing (the `/leeches` heuristic)
gets a graph-informed decision, per the design's diagnosis ladder:

- concept already **mastered** (its other cards carried it) → **retire** the
  failing card (soft-delete; the sync tick removes the note from the mirror);
- an unlearned **prerequisite** exists → **teach-prereq** (activate the prereq
  so tomorrow's mint picks it up; the card is left alone — rewording the target
  wouldn't fix a missing foundation);
- rewrite **streak cap** reached → **escalate** ("this concept needs you, not
  another card") — stamped on the concept, surfaced in the brief;
- otherwise → **rewrite**: a fresh didactic (delete + put ⇒ new guid ⇒ the
  Anki curve resets, which is fine — the old card wasn't achieving the
  objective anyway).

Ships **observe-first**: with ``act=False`` (the default autonomy) every
decision is computed and reported but nothing is written. See
docs/design/reading-prep-loop.md (§The three genuinely hard parts, decision 3).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import Any

from precis.handlers.anki import _CLOZE_RE, _split_extra, _strip_cloze
from precis.reading.concepts import STATE_ACTIVE, STATE_MASTERED
from precis.reading.mastery import (
    DEFAULT_MASTERY_THRESHOLD,
    DEFAULT_MIN_AGE_DAYS,  # the proving window ("3-4 days") — shared with mastery
    LEECH_EASE,
    LEECH_LAPSES,
    _env_float,
)

log = logging.getLogger(__name__)

DEFAULT_CARDS_PER_DAY = 5  # concepts carded per morning, not cards
DEFAULT_STREAK_CAP = 3  # rewrites per concept before escalating to the human
_MAX_CARDS_PER_CONCEPT = 3
_AUTHORED_BY = "card_forge"

_MINT_SYS = (
    "You author Anki cloze cards. Given a concept (name, definition, related "
    "concepts), write 1-3 excellent cloze cards that teach it: one idea per "
    "card, the concept before the label, {{c1::…}} deletions on the load-"
    "bearing words (::hint allowed). Optionally add a terse Back Extra (a "
    "source, mnemonic, or gotcha) — or omit it. Reply with ONLY JSON: "
    '{"cards":[{"text":"<cloze sentence>","back_extra":"<terse or empty>"}]}'
)

_REWORK_SYS = (
    "You repair a failing Anki cloze card. The learner keeps lapsing on it, so "
    "the didactic — not the learner — is at fault. Write ONE replacement card "
    "with a genuinely different breakdown: a new angle, a smaller step, or an "
    "anchor on the neighboring concepts the learner already knows. Keep it a "
    "single cloze sentence with {{c1::…}} deletions. Reply with ONLY JSON: "
    '{"text":"<cloze sentence>","back_extra":"<terse or empty>"}'
)


@dataclass
class CardDecision:
    """One rework decision — the observe-first audit record."""

    card_id: int
    concept_id: int
    action: str  # retire | teach-prereq | escalate | rewrite
    reason: str
    applied: bool = False
    new_card_id: int | None = None


@dataclass
class ForgeReport:
    """What the morning pass did (or, in report mode, would do)."""

    minted: list[tuple[int, list[int]]] = field(
        default_factory=list
    )  # (concept, cards)
    decisions: list[CardDecision] = field(default_factory=list)
    skipped: int = 0

    def lines(self) -> list[str]:
        out: list[str] = []
        for concept_id, card_ids in self.minted:
            out.append(
                f"minted {len(card_ids)} card(s) for concept cn{concept_id}: "
                + ", ".join(f"ak{c}" for c in card_ids)
            )
        for d in self.decisions:
            verb = "did" if d.applied else "would"
            extra = f" → ak{d.new_card_id}" if d.new_card_id else ""
            out.append(
                f"{verb} {d.action} ak{d.card_id} (concept cn{d.concept_id}): "
                f"{d.reason}{extra}"
            )
        if self.skipped:
            out.append(f"skipped {self.skipped} concept(s) (model gave no valid card)")
        return out


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    a, b = text.find("{"), text.rfind("}")
    if 0 <= a < b:
        try:
            obj = json.loads(text[a : b + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def cloze_skill_preamble() -> str:
    """The `precis-cloze` craft skill body, or ``""`` (best-effort, like
    `cast_common.voice_skill_preamble`)."""
    try:
        from precis.handlers.skill import _load_skill

        return _load_skill("precis-cloze") or ""
    except Exception:  # pragma: no cover - skill loading is best-effort
        log.debug("cards: precis-cloze skill unavailable", exc_info=True)
        return ""


def author_card(
    store: Any,
    *,
    text: str,
    concept_id: int,
    deck: str,
    extra_meta: dict[str, Any] | None = None,
) -> int:
    """Write one authored cloze card the way `AnkiHandler._create` does — ref +
    stripped `card_combined` + the concept `represents` link — plus the pass's
    provenance meta. Raises ``ValueError`` on a body with no cloze deletion."""
    cloze_text, extra = _split_extra(text)
    if not _CLOZE_RE.search(cloze_text):
        raise ValueError("card body has no {{cN::…}} cloze deletion")
    fields: dict[str, str] = {"Text": cloze_text}
    if extra:
        fields["Back Extra"] = extra
    meta: dict[str, Any] = {
        "notetype": "Cloze",
        "deck": deck,
        "fields": fields,
        "authored_by": _AUTHORED_BY,
        "concept_ref_id": concept_id,
        **(extra_meta or {}),
    }
    stripped = _strip_cloze(cloze_text)
    card_text = f"{stripped}\n\n{extra}".rstrip() if extra else stripped
    with store.tx() as conn:
        ref = store.insert_ref(kind="anki", slug=None, title=text, meta=meta, conn=conn)
        store.upsert_card_combined(ref.id, card_text, conn=conn)
        store.add_link(
            src_ref_id=concept_id,
            dst_ref_id=ref.id,
            relation="represents",
            conn=conn,
        )
    return int(ref.id)


def _deck_for(cohort: str | None) -> str:
    return f"Precis::{cohort}" if cohort else "Precis::reading"


def _minted_today(store: Any, now: datetime, *, cohort: str | None = None) -> int:
    """Concepts already carded today by this pass — makes a re-run top up to
    the cap instead of doubling it. Scoped to the cohort when given (a
    cohort-scoped forge isn't starved by another cohort's mints)."""
    midnight = datetime.combine(now.date(), time.min, tzinfo=UTC)
    cohort_clause = (
        "AND EXISTS (SELECT 1 FROM refs c WHERE c.kind='concept' "
        "AND c.ref_id = (a.meta->>'concept_ref_id')::bigint "
        "AND jsonb_exists(c.meta->'cohorts', %s))"
        if cohort
        else ""
    )
    params: tuple[Any, ...] = (
        (_AUTHORED_BY, midnight, cohort) if cohort else (_AUTHORED_BY, midnight)
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT a.meta->>'concept_ref_id') FROM refs a "
            "WHERE a.kind='anki' AND a.deleted_at IS NULL "
            f"AND a.meta->>'authored_by' = %s AND a.created_at >= %s {cohort_clause}",
            params,
        ).fetchone()
    return int(row[0] or 0)


def _cardless_concepts(
    store: Any, *, cohort: str | None, limit: int
) -> list[tuple[int, str, str]]:
    """Unmastered concepts with no live card yet, newest first."""
    cohort_clause = "AND jsonb_exists(c.meta->'cohorts', %s)" if cohort else ""
    sql = f"""
        SELECT c.ref_id, c.meta->>'name', c.meta->>'definition'
        FROM refs c
        WHERE c.kind='concept' AND c.deleted_at IS NULL
          AND COALESCE(c.meta->>'state','candidate') != 'mastered'
          {cohort_clause}
          AND NOT EXISTS (
            SELECT 1 FROM links l
            JOIN refs a ON a.kind='anki' AND a.deleted_at IS NULL
                       AND a.ref_id = CASE WHEN l.src_ref_id = c.ref_id
                                           THEN l.dst_ref_id ELSE l.src_ref_id END
            WHERE (l.src_ref_id = c.ref_id AND l.relation='represents')
               OR (l.dst_ref_id = c.ref_id AND l.relation='represented-by'))
        ORDER BY c.ref_id DESC LIMIT %s
    """
    params: tuple[Any, ...] = (cohort, limit) if cohort else (limit,)
    with store.pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [(int(r[0]), r[1] or "", r[2] or "") for r in rows]


def _neighbor_names(store: Any, concept_id: int, *, mastered_only: bool) -> list[str]:
    """Names of graph-adjacent concepts (optionally only mastered ones) — the
    context a new didactic can lean on."""
    sql = """
        SELECT DISTINCT n.meta->>'name'
        FROM links l
        JOIN refs n ON n.kind='concept' AND n.deleted_at IS NULL
                   AND n.ref_id = CASE WHEN l.src_ref_id = %s
                                       THEN l.dst_ref_id ELSE l.src_ref_id END
        WHERE (l.src_ref_id = %s OR l.dst_ref_id = %s)
          AND l.relation IN ('has-prerequisite','prerequisite-of',
                             'analogy-of','contrasts-with')
    """
    if mastered_only:
        sql += " AND n.meta->>'state' = 'mastered'"
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (concept_id, concept_id, concept_id)).fetchall()
    return [r[0] for r in rows if r[0]][:8]


def mint_daily_cards(
    store: Any,
    *,
    client: Any,
    per_day: int = DEFAULT_CARDS_PER_DAY,
    cohort: str | None = None,
    skill_preamble: str = "",
    now: datetime | None = None,
    report: ForgeReport | None = None,
) -> ForgeReport:
    """Author today's new cards: up to ``per_day`` cardless concepts, 1–3 cloze
    cards each. Idempotent per day (a re-run tops up to the cap)."""
    now = now or datetime.now(UTC)
    report = report or ForgeReport()
    remaining = per_day - _minted_today(store, now, cohort=cohort)
    if remaining <= 0:
        return report
    system = f"{skill_preamble}\n\n{_MINT_SYS}".strip() if skill_preamble else _MINT_SYS
    deck = _deck_for(cohort)
    for concept_id, name, definition in _cardless_concepts(
        store, cohort=cohort, limit=remaining
    ):
        lines = [f"CONCEPT: {name}"]
        if definition:
            lines.append(f"DEFINITION: {definition}")
        neighbors = _neighbor_names(store, concept_id, mastered_only=False)
        if neighbors:
            lines.append("RELATED CONCEPTS: " + ", ".join(neighbors))
        out = client.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": "\n".join(lines)},
            ]
        )
        data = _extract_json(getattr(out, "text", "") or "")
        cards = (data or {}).get("cards") or []
        minted: list[int] = []
        for card in cards[:_MAX_CARDS_PER_CONCEPT]:
            if not isinstance(card, dict):
                continue
            text = str(card.get("text") or "").strip()
            extra = str(card.get("back_extra") or "").strip()
            if extra:
                text = f"{text}\n---\n{extra}"
            try:
                minted.append(
                    author_card(store, text=text, concept_id=concept_id, deck=deck)
                )
            except ValueError:
                log.info("cards: dropped non-cloze card for concept %s", concept_id)
        if minted:
            report.minted.append((concept_id, minted))
            # A carded concept is being learned — it leaves the candidate pool.
            store.update_ref(concept_id, meta_patch={"state": STATE_ACTIVE})
        else:
            report.skipped += 1
    return report


def _stale_leeches(
    store: Any, *, min_age_days: float, now: datetime
) -> list[tuple[int, int, str, dict[str, Any]]]:
    """Precis-authored, concept-linked cards past their proving window that the
    `/leeches` heuristic flags: ``(card_id, concept_id, cloze_text, stats)``."""
    sql = """
        SELECT a.ref_id,
               CASE WHEN l.src_ref_id = a.ref_id THEN l.dst_ref_id
                    ELSE l.src_ref_id END AS concept_id,
               a.meta->'fields'->>'Text',
               a.meta->'anki_stats'
        FROM refs a
        JOIN links l ON (
                 (l.dst_ref_id = a.ref_id AND l.relation = 'represents')
              OR (l.src_ref_id = a.ref_id AND l.relation = 'represented-by'))
        WHERE a.kind='anki' AND a.deleted_at IS NULL
          AND COALESCE(a.meta->>'source','') != 'anki-foreign'
          AND NOT COALESCE((a.meta->>'readonly')::boolean, FALSE)
          AND a.meta ? 'anki_stats'
          AND a.created_at <= %s - (%s * interval '1 day')
          AND ( (a.meta->'anki_stats'->>'lapses_total')::int >= %s
             OR (a.meta->'anki_stats'->>'ease_min')::float <= %s )
        ORDER BY (a.meta->'anki_stats'->>'lapses_total')::int DESC NULLS LAST
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            sql, (now, min_age_days, LEECH_LAPSES, LEECH_EASE)
        ).fetchall()
    return [(int(r[0]), int(r[1]), r[2] or "", r[3] or {}) for r in rows]


def _weak_prereq(
    store: Any, concept_id: int, *, threshold: float
) -> tuple[int, str] | None:
    """An unlearned prerequisite of this concept, or None."""
    sql = """
        SELECT p.ref_id, p.meta->>'name'
        FROM links l
        JOIN refs p ON p.kind='concept' AND p.deleted_at IS NULL
                   AND p.ref_id = CASE WHEN l.relation = 'has-prerequisite'
                                       THEN l.dst_ref_id ELSE l.src_ref_id END
        WHERE (l.src_ref_id = %s AND l.relation = 'has-prerequisite')
           OR (l.dst_ref_id = %s AND l.relation = 'prerequisite-of')
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (concept_id, concept_id)).fetchall()
    for pid, name in rows:
        ref = store.get_ref(kind="concept", id=int(pid))
        if ref is None:
            continue
        meta = ref.meta or {}
        mastery = meta.get("mastery")
        val = float(mastery) if isinstance(mastery, int | float) else 0.0
        if val < threshold:
            return int(pid), name or ""
    return None


def rework_stale_cards(
    store: Any,
    *,
    client: Any,
    min_age_days: float = DEFAULT_MIN_AGE_DAYS,
    streak_cap: int = DEFAULT_STREAK_CAP,
    act: bool = False,
    skill_preamble: str = "",
    now: datetime | None = None,
    report: ForgeReport | None = None,
) -> ForgeReport:
    """Decide (and with ``act=True`` apply) the retire/teach-prereq/escalate/
    rewrite ladder over every stale leech card."""
    now = now or datetime.now(UTC)
    report = report or ForgeReport()
    threshold = _env_float("PRECIS_MASTERY_THRESHOLD", DEFAULT_MASTERY_THRESHOLD)
    system = (
        f"{skill_preamble}\n\n{_REWORK_SYS}".strip() if skill_preamble else _REWORK_SYS
    )
    for card_id, concept_id, cloze_text, stats in _stale_leeches(
        store, min_age_days=min_age_days, now=now
    ):
        concept = store.get_ref(kind="concept", id=concept_id)
        if concept is None:
            continue
        cmeta = concept.meta or {}
        stat_str = (
            f"lapses={stats.get('lapses_total')}, ease={stats.get('ease_min')}, "
            f"reps={stats.get('reps_total')}"
        )

        if cmeta.get("state") == STATE_MASTERED:
            d = CardDecision(
                card_id,
                concept_id,
                "retire",
                f"concept is mastered via its other cards; this one only lapses "
                f"({stat_str})",
            )
            if act:
                store.soft_delete_ref(card_id)
                d.applied = True
            report.decisions.append(d)
            continue

        prereq = _weak_prereq(store, concept_id, threshold=threshold)
        if prereq is not None:
            pid, pname = prereq
            d = CardDecision(
                card_id,
                concept_id,
                "teach-prereq",
                f"prerequisite cn{pid} ({pname!r}) is unlearned — teach it first, "
                f"leave this card ({stat_str})",
            )
            if act:
                store.update_ref(pid, meta_patch={"state": STATE_ACTIVE})
                d.applied = True
            report.decisions.append(d)
            continue

        streak = cmeta.get("remunge_streak")
        streak_n = int(streak) if isinstance(streak, int | float) else 0
        if streak_n >= streak_cap:
            d = CardDecision(
                card_id,
                concept_id,
                "escalate",
                f"{streak_n} rewrites already — this concept needs you, not "
                f"another card ({stat_str})",
            )
            if act:
                store.update_ref(
                    concept_id, meta_patch={"escalated_at": now.isoformat()}
                )
                d.applied = True
            report.decisions.append(d)
            continue

        d = CardDecision(
            card_id,
            concept_id,
            "rewrite",
            f"prereqs known, wording at fault — new didactic ({stat_str})",
        )
        if act:
            mastered = _neighbor_names(store, concept_id, mastered_only=True)
            lines = [
                f"FAILING CARD: {cloze_text}",
                f"STATS: {stat_str}",
                f"CONCEPT: {cmeta.get('name') or concept.title}",
            ]
            if cmeta.get("definition"):
                lines.append(f"DEFINITION: {cmeta['definition']}")
            if mastered:
                lines.append("LEARNER ALREADY KNOWS: " + ", ".join(mastered))
            out = client.complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": "\n".join(lines)},
                ]
            )
            data = _extract_json(getattr(out, "text", "") or "") or {}
            text = str(data.get("text") or "").strip()
            extra = str(data.get("back_extra") or "").strip()
            if extra:
                text = f"{text}\n---\n{extra}"
            old = store.get_ref(kind="anki", id=card_id)
            deck = ((old.meta or {}).get("deck") if old else None) or _deck_for(None)
            try:
                new_id = author_card(
                    store,
                    text=text,
                    concept_id=concept_id,
                    deck=deck,
                    extra_meta={"rework_of": card_id},
                )
            except ValueError:
                d.reason += " — model gave no valid cloze, card left in place"
                report.decisions.append(d)
                continue
            store.soft_delete_ref(card_id)
            store.update_ref(concept_id, meta_patch={"remunge_streak": streak_n + 1})
            d.applied = True
            d.new_card_id = new_id
        report.decisions.append(d)
    return report


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _default_client() -> Any:
    """The production compose client — folds through the router (ADR 0046)
    onto the ``CLOUD_SUPER`` reasoning tier (``claude_agent``, direct Anthropic
    OAuth) instead of holding a raw litellm client, so this cast-authoring pass
    gets the budget breaker + the route-log. ``tools_needed=True`` lands on
    ``claude_agent`` (free-text/JSON final answer, no tools advertised) rather
    than the tool-less ``claude_p`` judge shape, which drops the system prompt
    these cloze/rework prompts rely on. A ``PRECIS_CARD_FORGE_MODEL`` override
    still wins, but must now name a real model id (e.g. ``claude-opus-4-8``),
    not the retired litellm ``claude-opus`` alias. Mirrors
    `build_meditation`'s default-client branch. Tests inject a fake.

    ``max_tokens=1500`` restores the pre-router-migration litellm cap (a
    ``claude_agent`` call has no native completion-length flag, so this is a
    best-effort post-hoc truncation — see
    :class:`~precis.utils.llm.router.ClaudeAgentProvider` — not a real
    generation-time stop, but it keeps a cloze/rework rewrite bounded rather
    than running unchecked to the $2 cost ceiling)."""
    from precis.utils.llm.router import DispatchClient, Tier

    return DispatchClient(
        tier=Tier.CLOUD_SUPER,
        model=os.environ.get("PRECIS_CARD_FORGE_MODEL") or None,
        tools_needed=True,
        max_tokens=1500,
        source="card_forge",
        log_call=True,
    )


def run_card_forge(
    store: Any,
    *,
    client: Any | None = None,
    cohort: str | None = None,
    per_day: int | None = None,
    min_age_days: float | None = None,
    streak_cap: int | None = None,
    act: bool | None = None,
    now: datetime | None = None,
) -> ForgeReport:
    """The whole morning card pass: refresh mastery from the latest Anki stats,
    rework the cards that aren't working, then mint today's new cards. Env
    defaults: ``PRECIS_CARD_FORGE_AUTONOMY`` (``report``, the observe-first
    default, or ``act``), ``PRECIS_READING_CARDS_PER_DAY``,
    ``PRECIS_CARD_REWORK_MIN_DAYS``, ``PRECIS_CARD_REWORK_STREAK_CAP``."""
    from precis.reading.mastery import run_mastery_pass

    now = now or datetime.now(UTC)
    if act is None:
        act = (os.environ.get("PRECIS_CARD_FORGE_AUTONOMY") or "report") == "act"
    per_day = (
        per_day
        if per_day is not None
        else _env_int("PRECIS_READING_CARDS_PER_DAY", DEFAULT_CARDS_PER_DAY)
    )
    if min_age_days is None:
        min_age_days = _env_float("PRECIS_CARD_REWORK_MIN_DAYS", DEFAULT_MIN_AGE_DAYS)
    streak_cap = (
        streak_cap
        if streak_cap is not None
        else _env_int("PRECIS_CARD_REWORK_STREAK_CAP", DEFAULT_STREAK_CAP)
    )
    if client is None:
        client = _default_client()

    run_mastery_pass(store, now=now)
    preamble = cloze_skill_preamble()
    report = rework_stale_cards(
        store,
        client=client,
        min_age_days=min_age_days,
        streak_cap=streak_cap,
        act=act,
        skill_preamble=preamble,
        now=now,
    )
    return mint_daily_cards(
        store,
        client=client,
        per_day=per_day,
        cohort=cohort,
        skill_preamble=preamble,
        now=now,
        report=report,
    )


__all__ = [
    "DEFAULT_CARDS_PER_DAY",
    "DEFAULT_MIN_AGE_DAYS",
    "DEFAULT_STREAK_CAP",
    "CardDecision",
    "ForgeReport",
    "author_card",
    "cloze_skill_preamble",
    "mint_daily_cards",
    "rework_stale_cards",
    "run_card_forge",
]

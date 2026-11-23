"""Weekly card work — mint today's new anki cards from concepts, and rework the
cards that aren't working (reading-prep loop: cards-as-representations + the
adaptive re-munge, observe-first).

Runs Monday mornings *before* the reading brief (the `card_forge` job_type), so
the week starts with fresh cards on the phone and the brief can report what
changed. Two halves:

**Mint** — within a load-adjusted daily budget of Anki cards, the model
authors cloze notes for pending highlights and cardless concepts,
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
me690334 (§The three genuinely hard parts, decision 3).
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING, Any

from precis.anki.cloze import (
    acronym_hint_leaks,
    cloze_card_count,
    space_adjacent_braces,
)
from precis.anki.creds import anki_logins
from precis.anki.work_tracking import (
    has_work_source_tag,
    is_work_item_text,
    is_work_title,
)
from precis.errors import NotFound
from precis.handlers.anki import _split_extra, _strip_cloze
from precis.reading.asked import LIVE_CARD_SQL, live_card_exists
from precis.reading.concepts import STATE_ACTIVE, STATE_MASTERED
from precis.reading.mastery import (
    DEFAULT_MASTERY_THRESHOLD,
    DEFAULT_MIN_AGE_DAYS,  # the proving window ("3-4 days") — shared with mastery
    LEECH_EASE,
    LEECH_LAPSES,
    _env_float,
)
from precis.reading.memory_match import backfill_memory_matches
from precis.utils.llm.json_reply import extract_json_object

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store.store import Store

log = logging.getLogger(__name__)

DEFAULT_CARDS_PER_DAY = 5
DEFAULT_DUE_REVIEW_LIMIT = 100
DEFAULT_STREAK_CAP = 3  # rewrites per concept before escalating to the human
_MAX_CARDS_PER_CONCEPT = 3
_AUTHORED_BY = "card_forge"
_MINT_BUDGET_LOCK = 0x63666F726765  # pg_advisory_xact_lock bigint namespace

_HINT_RULE = (
    "Use a one-word category hint that never gives away the answer. For an "
    "acronym or abbreviation, show the short form and cloze its expansion or "
    "meaning, e.g. RMSD ({{c1::root-mean-square deviation::expansion}}). "
    "Never hide the short form with its expansion as the hint. "
)

_GLOSSARY_RULE = (
    "Back Extra defines every symbol, variable and acronym that appears on "
    "the card or in Back Extra itself, as 'symbol = meaning (units)' — no "
    "exceptions for obvious ones (k and T included); units in SI or the "
    "field's customary unit (mol/kg, S/cm, eV). A Greek letter carries its "
    "spoken name in parentheses, capitalised to match the letter's case: "
    "'Γ (Gamma)', 'ν (nu)', 'Ω (Omega)', 'ω (omega)'. One symbol per line "
    "as an HTML list, then <hr>, then any terse context (source, mnemonic, "
    "gotcha): <ul><li>Γ (Gamma) = hop rate (1/s)</li><li>ν (nu) = attempt "
    "frequency (1/s)</li><li>E_a = activation energy (eV)</li></ul><hr>"
    "context. Anki renders the field as HTML: no raw newlines, no markdown "
    "bullets, no run-on '; ' list. Before replying, re-read the card text "
    "and its Back Extra and add a line for every symbol still undefined. "
    "Back Extra is empty only when the card has no symbol, variable or "
    "acronym and needs no context. "
)

_MINT_SYS = (
    "You author Anki cloze cards from a concept (name, definition, related "
    "concepts).\n"
    "FIRST decide whether the concept deserves a card at all. A card is worth "
    "making only if the concept carries a SPECIFIC, durable fact a curious "
    "learner would want to know cold. REFUSE — reply with an empty cards list "
    "and a short skip_reason — when it is instead:\n"
    "- a vague topic label or research-direction ('new trends', 'new "
    "compounds', 'recent advances', 'critical evaluation') — the card could "
    "only restate the label;\n"
    "- a stock academic phrase ('extensively examined', 'widely studied') — "
    "what a rhetorical phrase signals is not domain knowledge;\n"
    "- document front-matter or scaffolding (a dedication, acknowledgements, an "
    "author, a header) — almost never a real concept;\n"
    "- an incidental proper noun or one-of-many example the source merely names "
    "in passing (a place/org/acronym mentioned once) — not the subject being "
    "learned;\n"
    "- a to-do or gap about OUR OWN system (a repo-dev memory, a threads "
    "section, a gap or backlog list, or wording like 'we lack', 'our spec has "
    "no', 'missing is') — work tracking is not a fact; if it names a real "
    "concept, card the concept alone with no 'gap Cn' framing;\n"
    "- so generic that every cloze would just echo the words already on the "
    "card.\n"
    "Cards must not duplicate existing ones: the caller only offers concepts "
    "with no card yet, so write each card around one new fact. "
    "When in doubt, refuse: a missing card costs nothing; a bad one wastes "
    "reviews forever and buries the good cards.\n"
    "If it IS worth teaching, write 1-3 cloze cards: one idea per card, the "
    "concept before the label, easiest cN first / hardest exact term last "
    "(a NEW card only — never renumber existing cards: Anki keys review "
    "history to the cN ordinal). Write the fully revealed sentence (every "
    "answer shown) telegraphic: no filler, no lead-in scaffolding that only "
    "names what a hint already names ('The term', 'The character', 'A X is') "
    "— the hint shows on its own card only, the prose shows on every "
    "sibling and on the revealed sentence. No context prefix inside the "
    "sentence ('In cell biology, ...') — context lives in tags. A ::hint "
    "must carry what the sentence does not (category, field, distinction); "
    "drop a hint that only restates the grammar. Every {{cN::…}} answer "
    "must be SPECIFIC and UNIQUELY recoverable from the rest of the "
    "sentence — never cloze a vague phrase ('improved properties', "
    "'various applications'), and never cloze one arbitrary member of a "
    "large open set (do not hide 'Machu Picchu' as 'an example of a World "
    "Heritage Site' — a thousand others fit). For EACH card verify every "
    "blank has exactly ONE answer a learner who knows the field could "
    "recover from the rest of the sentence. Report the verdict in the "
    "required 'check' field: 'pass' or 'fail: <which blank and why>' — a recoverability "
    "verdict, NOT a difficulty score: the learner's own grades are the "
    "difficulty signal, and you already know the fact, so you cannot rank "
    "difficulty for them. " + _HINT_RULE + _GLOSSARY_RULE + "Reply with ONLY JSON: "
    '{"cards":[{"text":"<cloze>",'
    '"back_extra":"<glossary list, <hr>, context; or empty>",'
    '"check":"pass|<fail: which blank and why>"}],'
    '"skip_reason":"<empty, or why you refused>"}'
)

_REWORK_SYS = (
    "You repair a failing Anki cloze card. The learner keeps lapsing on it, so "
    "the didactic — not the learner — is at fault. Write ONE replacement card "
    "with a genuinely different breakdown: a new angle, a smaller step, or an "
    "anchor on the neighboring concepts the learner already knows. Keep it a "
    "single cloze sentence with {{c1::…}} deletions, telegraphic: no filler, "
    "no lead-in scaffolding that only names what a hint already names ('The "
    "term', 'The character'), no context prefix — context lives in tags. A "
    "::hint must carry what the sentence does not; drop one that only "
    "restates the grammar. Make each answer SPECIFIC and uniquely recoverable "
    "from its context — never cloze a vague phrase or one arbitrary member of "
    "a large open set — and verify each blank has exactly ONE answer a "
    "learner who knows the field could recover from the rest of the sentence; "
    "report the verdict in the required 'check' field: 'pass' or 'fail: <why>'. "
    "If the underlying "
    "concept is not actually a learnable fact (a topic label, stock phrase, or "
    "front-matter), do not manufacture another card — reply with an empty "
    "text. " + _HINT_RULE + _GLOSSARY_RULE + "Reply with ONLY JSON: "
    '{"text":"<cloze sentence or empty>",'
    '"back_extra":"<glossary list, <hr>, context; or empty>",'
    '"check":"pass|<fail: why>"}'
)


class ConceptGoneError(ValueError):
    """``author_card``'s concept was retired or missing at write time
    (gr742735). A ``ValueError`` so existing ``except ValueError``
    callers keep catching it, but the mint/rework passes can tell "the
    concept went away mid-write" apart from "the model wrote a bad
    cloze" — only the second counts against the model."""


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
    refused: list[tuple[int, str]] = field(default_factory=list)  # (concept, why)
    skipped: int = 0
    mint_throttled: bool = False

    def lines(self) -> list[str]:
        out: list[str] = []
        if self.mint_throttled:
            out.append("reduced new-card budget for due Anki reviews")
        for concept_id, card_ids in self.minted:
            out.append(
                f"minted {len(card_ids)} note(s) for concept cn{concept_id}: "
                + ", ".join(f"ak{c}" for c in card_ids)
            )
        for d in self.decisions:
            verb = "did" if d.applied else "would"
            extra = f" → ak{d.new_card_id}" if d.new_card_id else ""
            out.append(
                f"{verb} {d.action} ak{d.card_id} (concept cn{d.concept_id}): "
                f"{d.reason}{extra}"
            )
        for concept_id, why in self.refused:
            out.append(f"refused concept cn{concept_id} (not a card): {why}")
        if self.skipped:
            out.append(f"skipped {self.skipped} concept(s) (model gave no valid card)")
        return out


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
    store: Store,
    *,
    text: str,
    concept_id: int,
    deck: str,
    extra_meta: dict[str, Any] | None = None,
    owner_login: str | None = None,
    conn: Connection | None = None,
) -> int:
    """Write one authored cloze card the way `AnkiHandler._create` does — ref +
    stripped `card_combined` + the concept `represents` link — plus the pass's
    provenance meta. Raises ``ValueError`` for missing deletions or acronym
    hints that give the answer away, and :class:`ConceptGoneError` (a
    ``ValueError``) when the concept is retired or missing at write time."""
    cloze_text, extra = _split_extra(text)
    card_count = cloze_card_count(cloze_text)
    if not card_count:
        raise ValueError("card body has no {{cN::…}} cloze deletion")
    if leaks := acronym_hint_leaks(cloze_text):
        raise ValueError(f"hint initials spell the answer for {', '.join(leaks)}")
    cloze_text, _spaced = space_adjacent_braces(cloze_text)
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
        "minted_card_count": card_count,
    }
    stripped = _strip_cloze(cloze_text)
    card_text = f"{stripped}\n\n{extra}".rstrip() if extra else stripped
    with store.tx() if conn is None else nullcontext(conn) as tx:
        # Row-lock the concept so a concurrent ``retire_ref`` (which cascades
        # to its cards) either waits for this write or is seen here: no card
        # is minted against a concept that retired meanwhile (gr742735).
        live = tx.execute(
            "SELECT 1 FROM refs WHERE ref_id = %s AND kind = 'concept' "
            "AND retired_at IS NULL FOR SHARE",
            (concept_id,),
        ).fetchone()
        if live is None:
            raise ConceptGoneError(f"concept {concept_id} is retired or missing")
        ref = store.insert_ref(
            kind="anki",
            slug=None,
            title=text,
            meta=meta,
            owner_login=owner_login,
            conn=tx,
        )
        store.chunks.upsert_card_combined(ref.id, card_text, conn=tx)
        store.add_link(
            src_ref_id=concept_id,
            dst_ref_id=ref.id,
            relation="represents",
            conn=tx,
        )
    return int(ref.id)


def _deck_for(cohort: str | None) -> str:
    return f"Precis::{cohort}" if cohort else "Precis::reading"


def _minted_today(
    conn: Connection, now: datetime, *, cohort: str | None, solo_login: str | None
) -> Counter[str | None]:
    """Count today's intake at publication time, including subsequently retired notes."""
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
    counts: Counter[str | None] = Counter()
    for owner, count, body in conn.execute(
        "SELECT a.owner_login, a.meta->'minted_card_count', a.meta->'fields'->>'Text' "
        "FROM refs a WHERE a.kind='anki' AND NOT (a.meta ? 'rework_of') "
        f"AND a.meta->>'authored_by' = %s AND a.created_at >= %s {cohort_clause}",
        params,
    ):
        counts[owner or solo_login] += (
            count
            if isinstance(count, int) and count > 0
            else max(1, cloze_card_count(body or ""))
        )
    return counts


def _due_reviews_by_owner(store: Store) -> dict[str, int]:
    """Read live own-note review counts from the latest successful syncs."""
    with store.pool.connection() as conn:
        return dict(
            conn.execute(
                """SELECT owner_login, SUM((meta->'anki_stats'->>'due_reviews')::integer)
                   FROM refs WHERE kind = 'anki' AND retired_at IS NULL
                     AND owner_login IS NOT NULL
                     AND (meta->>'source') IS DISTINCT FROM 'anki-foreign'
                     AND jsonb_typeof(meta->'anki_stats'->'due_reviews') = 'number'
                   GROUP BY owner_login"""
            ).fetchall()
        )


def _review_budget(per_day: int, due: int, limit: int) -> int:
    # Round up to leave at least one card of headroom until the pause threshold.
    return (per_day * max(0, limit - due) + limit - 1) // limit


_LATEST_HIGHLIGHT = """
    SELECT submission, context
    FROM anki_highlight_context h
    WHERE h.concept_id = c.ref_id AND h.owner_login = c.owner_login
      AND c.meta->'asked_submissions'->h.submission->>'forge_skipped' IS NULL
    ORDER BY queued_at DESC, submission DESC
    LIMIT 1
"""
_CARD_OWNER = "COALESCE(c.owner_login, c.meta->>'asked_by')"


def _cardless_concepts(
    store: Store,
    *,
    cohort: str | None,
    limit: int,
    paused_owners: list[str] | None = None,
) -> list[tuple[int, str, str, str | None, str | None, str]]:
    """Pending highlights and unmastered concepts without a live owner card."""
    cohort_clause = "AND jsonb_exists(c.meta->'cohorts', %s)" if cohort else ""
    live_card = LIVE_CARD_SQL.format(concept="c.ref_id", owner=_CARD_OWNER)
    sql = f"""
        SELECT c.ref_id, c.meta->>'name', c.meta->>'definition',
               {_CARD_OWNER}, h.submission, h.context
        FROM refs c
        LEFT JOIN LATERAL ({_LATEST_HIGHLIGHT}) h ON true
        WHERE c.kind='concept' AND c.retired_at IS NULL
          {cohort_clause}
          AND COALESCE({_CARD_OWNER}, '') <> ALL(%s)
          AND (h.submission IS NOT NULL OR (
              COALESCE(c.meta->>'state','candidate') != 'mastered'
              AND NOT (c.meta ? 'card_forge_skip')
              AND NOT EXISTS ({live_card})
          ))
        ORDER BY EXISTS (
            SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
            WHERE rt.ref_id = c.ref_id
              AND t.namespace = 'OPEN' AND t.value = 'twin:asked'
        ) DESC, c.ref_id DESC
        LIMIT %s
    """
    params: tuple[Any, ...] = (
        (cohort, paused_owners or [], limit) if cohort else (paused_owners or [], limit)
    )
    with store.pool.connection() as conn:
        return [
            (int(cid), name or "", definition or "", owner, submission, context or "")
            for cid, name, definition, owner, submission, context in conn.execute(
                sql, params
            )
        ]


def _current_mint_state(
    conn: Connection, concept_id: int, owner: str | None, submission: str | None
) -> str | None:
    # Get the row lock before reading the current capture. Queue/Undo may
    # have committed while we waited for the lock or for the model.
    row = conn.execute(
        f"SELECT {_CARD_OWNER}, COALESCE(c.meta->>'state', 'candidate'), "
        "c.meta ? 'card_forge_skip' FROM refs c WHERE c.ref_id = %s "
        "AND c.retired_at IS NULL FOR NO KEY UPDATE",
        (concept_id,),
    ).fetchone()
    if row is None or row[0] != owner:
        return None
    _, state, skipped = row
    if submission is None and (
        skipped or state == STATE_MASTERED or live_card_exists(conn, concept_id, owner)
    ):
        return None
    latest = conn.execute(
        f"SELECT h.submission FROM refs c "
        f"JOIN LATERAL ({_LATEST_HIGHLIGHT}) h ON true WHERE c.ref_id = %s",
        (concept_id,),
    ).fetchone()
    return str(state) if (latest[0] if latest else None) == submission else None


def _concept_is_work_tracking(
    store: Store, concept_id: int, name: str, definition: str
) -> bool:
    """The concept's name/definition is work-item wording, or it is tagged as
    a work-tracking source (same predicate as `AnkiHandler._create`)."""
    return (
        is_work_title(name)
        or is_work_item_text(definition)
        or has_work_source_tag(store.tags_for(concept_id))
    )


def _neighbor_names(store: Store, concept_id: int, *, mastered_only: bool) -> list[str]:
    """Names of graph-adjacent concepts (optionally only mastered ones) — the
    context a new didactic can lean on."""
    sql = """
        SELECT DISTINCT n.meta->>'name'
        FROM links l
        JOIN refs n ON n.kind='concept' AND n.retired_at IS NULL
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


def _check_failed(check: str) -> bool:
    """The model's per-card recoverability verdict (gr743086) — 'fail: …'
    means at least one blank has no single recoverable answer."""
    return check.strip().lower().startswith("fail")


def mint_daily_cards(
    store: Store,
    *,
    client: Any,
    per_day: int = DEFAULT_CARDS_PER_DAY,
    cohort: str | None = None,
    skill_preamble: str = "",
    now: datetime | None = None,
    report: ForgeReport | None = None,
) -> ForgeReport:
    """Author within today's cloze-card budget, reduced by each owner's load."""
    now = now or datetime.now(UTC)
    report = report or ForgeReport()
    if per_day <= 0:
        return report
    due_reviews = _due_reviews_by_owner(store)
    logins = anki_logins(store) if any(due_reviews.values()) else []
    solo_login = logins[0] if len(logins) == 1 else None
    due_limit = max(
        1, _env_int("PRECIS_READING_DUE_REVIEW_LIMIT", DEFAULT_DUE_REVIEW_LIMIT)
    )
    budgets = {
        owner: _review_budget(per_day, due, due_limit)
        for owner, due in due_reviews.items()
    }
    report.mint_throttled = any(budget < per_day for budget in budgets.values())
    with store.pool.connection() as conn:
        used = _minted_today(conn, now, cohort=cohort, solo_login=solo_login)
    remaining = per_day - used.total()
    if remaining <= 0:
        return report
    paused_owners = [
        owner for owner, budget in budgets.items() if used[owner] >= budget
    ]
    if solo_login in paused_owners:
        paused_owners.append("")
    system = f"{skill_preamble}\n\n{_MINT_SYS}".strip() if skill_preamble else _MINT_SYS
    deck = _deck_for(cohort)
    for concept_id, name, definition, owner, submission, context in _cardless_concepts(
        store, cohort=cohort, limit=remaining, paused_owners=paused_owners
    ):
        budget_owner = owner or solo_login
        owner_budget = budgets.get(budget_owner, per_day) if budget_owner else per_day
        remaining = min(per_day - used.total(), owner_budget - used[budget_owner])
        if remaining <= 0:
            continue
        if _concept_is_work_tracking(
            store, concept_id, name, definition
        ) or is_work_item_text(context):
            cards: list[Any] = []
            skip_reason = (
                "work tracking (a to-do or gap about our own system), not a fact"
            )
        else:
            lines = [f"CONCEPT: {name}"]
            if definition:
                lines.append(f"DEFINITION: {definition}")
            if context:
                lines.append(f"HIGHLIGHT CONTEXT: {context}")
            neighbors = _neighbor_names(store, concept_id, mastered_only=False)
            if neighbors:
                lines.append("RELATED CONCEPTS: " + ", ".join(neighbors))
            lines.append(
                f"ANKI CARD BUDGET: {remaining} across all notes. Within each "
                "note, each distinct cN ordinal uses one card. Put the most "
                "useful note first and keep every note within the budget."
            )
            out = client.complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": "\n".join(lines)},
                ]
            )
            data = extract_json_object(getattr(out, "text", "") or "")
            cards = (data or {}).get("cards") or []
            skip_reason = str((data or {}).get("skip_reason") or "").strip()
            if submission and skip_reason:
                skip_reason = "the forge declined the captured term"
        minted: list[int] = []
        deferred = False
        extra_meta = {"highlight_submission": submission} if submission else {}
        with store.tx() as conn:
            # Recheck under a transaction lock after model I/O. Concurrent
            # passes for different concepts must share the same budget.
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MINT_BUDGET_LOCK,))
            used = _minted_today(conn, now, cohort=cohort, solo_login=solo_login)
            remaining = min(per_day - used.total(), owner_budget - used[budget_owner])
            if remaining <= 0:
                continue
            state = _current_mint_state(conn, concept_id, owner, submission)
            if state is None:
                continue
            for card in cards[:_MAX_CARDS_PER_CONCEPT]:
                if not isinstance(card, dict):
                    continue
                check = str(card.get("check") or "").strip()
                if _check_failed(check):
                    # The model's own recoverability verdict failed (gr743086):
                    # a blank with no single recoverable answer — mint nothing
                    # rather than ship an unanswerable card.
                    log.info(
                        "cards: dropped self-check-failing card for concept %s: %s",
                        concept_id,
                        check if submission is None else "failed",
                    )
                    continue
                text = str(card.get("text") or "").strip()
                extra = str(card.get("back_extra") or "").strip()
                if extra:
                    text = f"{text}\n---\n{extra}"
                count = cloze_card_count(_split_extra(text)[0])
                if count > remaining:
                    deferred = True
                    continue
                try:
                    if is_work_item_text(_strip_cloze(_split_extra(text)[0])):
                        raise ValueError("card is a to-do or gap about our own system")
                    minted.append(
                        author_card(
                            store,
                            text=text,
                            concept_id=concept_id,
                            deck=deck,
                            owner_login=owner,
                            conn=conn,
                            # The verdict rides the card so the mint is auditable.
                            extra_meta={
                                **extra_meta,
                                "recoverability_check": check or "unchecked",
                            },
                        )
                    )
                    remaining -= count
                    used[budget_owner] += count
                except ValueError as exc:
                    log.info(
                        "cards: dropped card for concept %s: %s",
                        concept_id,
                        exc if submission is None else "invalid card",
                    )
            if minted:
                if state != STATE_MASTERED:
                    store.update_ref(
                        concept_id, meta_patch={"state": STATE_ACTIVE}, conn=conn
                    )
                if submission:
                    conn.execute(
                        "DELETE FROM anki_highlight_context "
                        "WHERE concept_id = %s AND owner_login = %s AND submission = %s",
                        (concept_id, owner, submission),
                    )
            elif deferred:
                continue
            elif skip_reason and submission:
                conn.execute(
                    "UPDATE refs SET meta = jsonb_set(meta, %s, 'true'::jsonb) "
                    "WHERE ref_id = %s",
                    (["asked_submissions", submission, "forge_skipped"], concept_id),
                )
            elif skip_reason:
                store.update_ref(
                    concept_id, meta_patch={"card_forge_skip": skip_reason}, conn=conn
                )
        if minted:
            report.minted.append((concept_id, minted))
            backfill_memory_matches(store, concept_ids=[concept_id])
        elif skip_reason:
            report.refused.append((concept_id, skip_reason))
        else:
            report.skipped += 1
    return report


def _stale_leeches(
    store: Store, *, min_age_days: float, now: datetime
) -> list[tuple[int, int, str, dict[str, Any]]]:
    """Precis-authored, concept-linked cards past their proving window that the
    `/leeches` heuristic flags: ``(card_id, concept_id, cloze_text, stats)``.

    One row per card even when several ``represents`` links home it: the
    pass makes a single decision per leech — a second row would spend a
    second model call reworking (or retiring) a card the first iteration
    already handled. The reported concept is the card's authoring home
    (``meta.concept_ref_id``) when that link is still live, else any live
    home, else whatever link exists — the loop's ``get_ref`` then skips
    a fully orphaned row."""
    sql = """
        SELECT a.ref_id,
               (SELECT CASE WHEN l.src_ref_id = a.ref_id
                            THEN l.dst_ref_id ELSE l.src_ref_id END
                FROM links l
                LEFT JOIN refs c ON c.kind = 'concept'
                    AND c.ref_id = CASE WHEN l.src_ref_id = a.ref_id
                                        THEN l.dst_ref_id ELSE l.src_ref_id END
                WHERE (l.dst_ref_id = a.ref_id AND l.relation = 'represents')
                   OR (l.src_ref_id = a.ref_id AND l.relation = 'represented-by')
                ORDER BY (c.retired_at IS NULL) DESC NULLS LAST,
                         (CASE WHEN l.src_ref_id = a.ref_id
                               THEN l.dst_ref_id ELSE l.src_ref_id END
                          = COALESCE((a.meta->>'concept_ref_id')::bigint, -1))
                         DESC,
                         1
                LIMIT 1) AS concept_id,
               a.meta->'fields'->>'Text',
               a.meta->'anki_stats'
        FROM refs a
        WHERE a.kind='anki' AND a.retired_at IS NULL
          AND COALESCE(a.meta->>'source','') != 'anki-foreign'
          AND NOT COALESCE((a.meta->>'readonly')::boolean, FALSE)
          AND a.meta ? 'anki_stats'
          AND a.created_at <= %s - (%s * interval '1 day')
          AND ( (a.meta->'anki_stats'->>'lapses_total')::int >= %s
             OR (a.meta->'anki_stats'->>'ease_min')::float <= %s )
          AND EXISTS (
            SELECT 1 FROM links l
            WHERE (l.dst_ref_id = a.ref_id AND l.relation = 'represents')
               OR (l.src_ref_id = a.ref_id AND l.relation = 'represented-by'))
        ORDER BY (a.meta->'anki_stats'->>'lapses_total')::int DESC NULLS LAST
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            sql, (now, min_age_days, LEECH_LAPSES, LEECH_EASE)
        ).fetchall()
    return [(int(r[0]), int(r[1]), r[2] or "", r[3] or {}) for r in rows]


def _weak_prereq(
    store: Store, concept_id: int, *, threshold: float
) -> tuple[int, str] | None:
    """An unlearned prerequisite of this concept, or None."""
    sql = """
        SELECT p.ref_id, p.meta->>'name'
        FROM links l
        JOIN refs p ON p.kind='concept' AND p.retired_at IS NULL
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


def _concept_gone_decision(
    store: Store, d: CardDecision, card_id: int, stat_str: str
) -> None:
    """Fold a mid-rework concept retire into the decision (gr742735):
    the concept's cascade retired the original card iff it had no other
    live concept home — report whichever end state holds rather than a
    model-facing outcome."""
    if store.get_ref(kind="anki", id=card_id) is None:
        d.action = "retire"
        d.applied = True
        d.reason = f"concept retired mid-rework, original card retired ({stat_str})"
    else:
        d.reason = f"concept retired mid-rework, original card still live ({stat_str})"


def rework_stale_cards(
    store: Store,
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
                try:
                    store.retire_ref(card_id)
                except NotFound:
                    pass  # the concept's cascade already retired it
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
                try:
                    store.update_ref(pid, meta_patch={"state": STATE_ACTIVE})
                except NotFound:
                    d.reason += " — prerequisite retired, nothing activated"
                else:
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
                try:
                    store.update_ref(
                        concept_id, meta_patch={"escalated_at": now.isoformat()}
                    )
                except NotFound:
                    d.reason += " — concept retired, nothing stamped"
                else:
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
            data = extract_json_object(getattr(out, "text", "") or "") or {}
            check = str(data.get("check") or "").strip()
            if _check_failed(check):
                d.reason += f" — model self-check failed ({check}), card left in place"
                report.decisions.append(d)
                continue
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
                    owner_login=getattr(old, "owner_login", None) if old else None,
                    extra_meta={
                        "rework_of": card_id,
                        "recoverability_check": check or "unchecked",
                    },
                )
            except ConceptGoneError:
                _concept_gone_decision(store, d, card_id, stat_str)
                report.decisions.append(d)
                continue
            except ValueError:
                d.reason += " — model gave no valid cloze, card left in place"
                report.decisions.append(d)
                continue
            # The follow-up (retire the original, stamp the streak) runs
            # only while the concept — and with it the fresh replacement —
            # is still live, and only while the concept row is locked
            # (gr742735). ``FOR SHARE`` is author_card's own
            # serialization: a concurrent concept retire either already
            # committed (visible below, its cascade having decided the
            # original's fate) or waits for this transaction — the pass
            # can never retire a shared original the cascade just spared.
            with store.tx() as conn:
                live = conn.execute(
                    "SELECT 1 FROM refs WHERE ref_id = %s AND kind='concept' "
                    "AND retired_at IS NULL FOR SHARE",
                    (concept_id,),
                ).fetchone()
                if live is not None:
                    if (
                        conn.execute(
                            "SELECT 1 FROM refs WHERE ref_id = %s "
                            "AND retired_at IS NULL",
                            (new_id,),
                        ).fetchone()
                        is None
                    ):
                        d.reason += (
                            " — replacement retired before the swap, "
                            "original left in place"
                        )
                    else:
                        try:
                            store.retire_ref(card_id, conn=conn)
                        except NotFound:
                            # An independent drop (not the concept's
                            # cascade — that door is shut above) already
                            # retired the original. The replacement is
                            # live, so the rewrite still stands and the
                            # streak still counts.
                            d.reason += " — original already retired"
                        store.update_ref(
                            concept_id,
                            meta_patch={"remunge_streak": streak_n + 1},
                            conn=conn,
                        )
                        d.applied = True
                        d.new_card_id = new_id
            if live is None:
                # The concept's retire landed after author_card committed
                # (gr742735): the cascade retired the rewrite — and the
                # original unless another live concept homes it — so the
                # follow-up must not run at all.
                _concept_gone_decision(store, d, card_id, stat_str)
        report.decisions.append(d)
    return report


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


#: ``app_settings`` keys for the daily mint cap (gr548276): the durable key,
#: and the one-day override ``reading.cards_per_day:<YYYY-MM-DD>`` the
#: ``/anki`` page's "today only" box writes. Resolution is dated → durable →
#: ``PRECIS_READING_CARDS_PER_DAY`` env → compiled default; a malformed row
#: falls through to the next tier rather than failing the morning pass.
CARDS_PER_DAY_KEY = "reading.cards_per_day"


def _setting_int(store: Store, key: str) -> int | None:
    """One ``app_settings`` row as a non-negative int, or None (absent/bad)."""
    from precis.budget import settings as app_settings

    raw = app_settings.get_setting(store, key)
    if raw is None:
        return None
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        log.warning("cards: ignoring malformed %s=%r", key, raw)
        return None


def effective_cards_per_day(store: Store, *, now: datetime | None = None) -> int:
    """The mint cap in force *today*: the dated ``app_settings`` override
    when one was written for this UTC date, else the durable setting, else
    the ``PRECIS_READING_CARDS_PER_DAY`` env (kept as the bootstrap
    fallback), else the compiled default."""
    now = now or datetime.now(UTC)
    for key in (
        f"{CARDS_PER_DAY_KEY}:{now.date().isoformat()}",
        CARDS_PER_DAY_KEY,
    ):
        val = _setting_int(store, key)
        if val is not None:
            return val
    return _env_int("PRECIS_READING_CARDS_PER_DAY", DEFAULT_CARDS_PER_DAY)


def _default_client() -> Any:
    """The production compose client — folds through the router
    onto the ``BIG`` reasoning tier (local-first via ``llm.chain.big``) instead
    of holding a raw litellm client, so this cast-authoring pass gets the budget
    breaker + the route-log. This was ``FRONTIER``; card_forge is a background
    cadence pass with nothing waiting on it, so it buys nothing from opus
    latency-or-price. ``tools_needed=True`` lands on
    ``claude_agent`` (free-text/JSON final answer, no tools advertised) rather
    than the tool-less ``claude_p`` judge shape, which drops the system prompt
    these cloze/rework prompts rely on. A ``PRECIS_CARD_FORGE_MODEL`` override
    still wins, but must now name a real model id (e.g. ``claude-opus-5-5``),
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
        tier=Tier.BIG,
        model=os.environ.get("PRECIS_CARD_FORGE_MODEL") or None,
        tools_needed=True,
        max_tokens=1500,
        source="card_forge",
        log_call=True,
    )


def run_card_forge(
    store: Store,
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
    default, or ``act``), ``PRECIS_READING_CARDS_PER_DAY`` (the fallback tier
    of :func:`effective_cards_per_day`, behind the ``app_settings`` rows),
    ``PRECIS_CARD_REWORK_MIN_DAYS``, ``PRECIS_CARD_REWORK_STREAK_CAP``."""
    from precis.reading.mastery import run_mastery_pass

    now = now or datetime.now(UTC)
    if act is None:
        act = (os.environ.get("PRECIS_CARD_FORGE_AUTONOMY") or "report") == "act"
    if per_day is None:
        per_day = effective_cards_per_day(store, now=now)
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
    report = mint_daily_cards(
        store,
        client=client,
        per_day=per_day,
        cohort=cohort,
        skill_preamble=preamble,
        now=now,
        report=report,
    )
    # Backfill + retry concepts whose embedding was not ready at mint time.
    backfill_memory_matches(store)
    return report


__all__ = [
    "CARDS_PER_DAY_KEY",
    "DEFAULT_CARDS_PER_DAY",
    "DEFAULT_MIN_AGE_DAYS",
    "DEFAULT_STREAK_CAP",
    "CardDecision",
    "ForgeReport",
    "author_card",
    "cloze_skill_preamble",
    "effective_cards_per_day",
    "mint_daily_cards",
    "rework_stale_cards",
    "run_card_forge",
]

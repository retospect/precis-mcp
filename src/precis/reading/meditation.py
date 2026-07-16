"""Evening conceptual-walk meditation — a calm nidra cast over the concept graph
(reading-prep loop, slice: the first cast producer).

Walk a cohort's concepts along their closest edges, then compose a gentle
second-person meditation script (induction → soft walk → tapering coda) as a
`draft` narrated with the `af_nicole` voice. The draft flows through the shipped
audio path (`export_audio` → ffmpeg → `audio_feed.publish_episode`); this module
owns only the *content* — the walk ordering + the script + the draft. Authoring
craft is the `precis-voice` skill (nidra profile). See
docs/design/reading-prep-loop.md.

This module is store + LLM only (no TTS, no audio deps), so it unit-tests with a
fake client. The render+publish step lives in the `precis meditation` CLI, which
runs where Kokoro is installed (spark).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from precis.reading.cast_common import (
    CAST_PROFILES,
    SINGLE_CALL_WORD_CEILING,
    cast_slug,
    create_cast_draft,
    word_budget,
)

log = logging.getLogger(__name__)

MEDITATION_VOICE = "af_nicole"  # the evening voice (per Reto)
_DEFAULT_MAX_CONCEPTS = (
    14  # a short meditation is a gentle handful, not the whole graph
)
#: Below this many concepts a long walk can't be stretched meaningfully, so we
#: fall back to a single-call compose regardless of the target length.
_SEGMENT_MIN_CONCEPTS = 8
#: Concepts per segment call when composing a long walk. Smaller = more segments,
#: each dwelling longer on fewer ideas (a slower, fuller nidra at the target length).
_CONCEPTS_PER_SEGMENT = 4
_WALK_RELATIONS = ("has-prerequisite", "analogy-of", "contrasts-with")

#: The nidra narration contract. Kept terse; the craft detail is the
#: `precis-voice` skill (which the caller may also prepend).
_NIDRA_SYS = (
    "You are a calm evening-meditation narrator guiding a listener toward sleep. "
    "Write in a warm, slow, second-person present-tense voice. Open with a short "
    "settling induction (a few breaths, letting the day set down). Then drift "
    "gently from one idea to the next along the order given, describing each "
    "softly and its relationship to the last — always in plain words, NEVER as a "
    "formula, symbol, list, or question. Keep it soothing and mostly gentle, but "
    "never say anything false. Close with a tapering coda: sentences shorten, the "
    "words soften and fade as the listener drifts. Return the script as plain "
    "paragraphs separated by blank lines — no headings, no markup."
)


def _load(
    store: Any,
    cohort: str | None,
    limit: int,
    *,
    bias_active_quests: bool = False,
    prefer_mastered: bool = False,
) -> tuple[list[tuple[int, str, str]], dict[int, set[int]]]:
    """``(concepts, adjacency)`` for the walk. Cohort-scoped when given, else the
    most recent concepts. Adjacency unions every graph edge among them.

    ``prefer_mastered`` (the evening drift): order by ``meta.mastery``
    descending, so the walk moves through what the listener *knows* — familiar
    ideas at the day's end, exposure not testing. Degrades to the recency order
    while mastery data is sparse (all-zero mastery ties fall back to the same
    ``ref_id`` ordering).

    Quest reweighting (slice 2): with ``bias_active_quests``, concepts that
    ``serve`` an active quest are pulled into the selection (even if outside the
    ordinal window) and sorted to the front by striving weight, so the reading
    opens on what serves the striving. No-op when no quest is active."""
    # Computed before the conn block so the reweight primitive's own connection
    # doesn't nest inside ours.
    quest_weight: dict[int, float] = {}
    if bias_active_quests:
        from precis.quest.reweight import server_weights_for_active_quests

        quest_weight = server_weights_for_active_quests(store, server_kind="concept")
    mastery_order = "(COALESCE(meta->>'mastery','0'))::float DESC, "
    with store.pool.connection() as conn:
        if cohort:
            order = (mastery_order if prefer_mastered else "") + "ref_id"
            rows = conn.execute(
                "SELECT ref_id, meta->>'name', meta->>'definition' FROM refs "
                "WHERE kind='concept' AND deleted_at IS NULL "
                f"AND jsonb_exists(meta->'cohorts', %s) ORDER BY {order} LIMIT %s",
                (cohort, limit),
            ).fetchall()
        else:
            order = (mastery_order if prefer_mastered else "") + "ref_id DESC"
            rows = conn.execute(
                "SELECT ref_id, meta->>'name', meta->>'definition' FROM refs "
                "WHERE kind='concept' AND deleted_at IS NULL "
                f"ORDER BY {order} LIMIT %s",
                (limit,),
            ).fetchall()
        concepts = [(int(r[0]), r[1] or "", r[2] or "") for r in rows]
        if quest_weight:
            # Pull in quest-serving concepts missing from the ordinal window,
            # then stable-sort so the highest-weight strivers lead; trim to
            # limit. Stable sort keeps unrelated concepts in their prior order.
            have = {c[0] for c in concepts}
            missing = [q for q in quest_weight if q not in have]
            if missing:
                extra = conn.execute(
                    "SELECT ref_id, meta->>'name', meta->>'definition' FROM refs "
                    "WHERE kind='concept' AND deleted_at IS NULL "
                    "AND ref_id = ANY(%s)",
                    (missing,),
                ).fetchall()
                concepts += [(int(r[0]), r[1] or "", r[2] or "") for r in extra]
            concepts.sort(key=lambda c: -quest_weight.get(c[0], 0.0))
            concepts = concepts[:limit]
        ids = [c[0] for c in concepts]
        adjacency: dict[int, set[int]] = {i: set() for i in ids}
        if len(ids) >= 2:
            for s, d in conn.execute(
                "SELECT src_ref_id, dst_ref_id FROM links "
                "WHERE src_ref_id = ANY(%s) AND dst_ref_id = ANY(%s) "
                "AND relation = ANY(%s)",
                (ids, ids, list(_WALK_RELATIONS)),
            ).fetchall():
                if s in adjacency and d in adjacency:
                    adjacency[s].add(d)
                    adjacency[d].add(s)
    return concepts, adjacency


def _walk_order(ids: list[int], adjacency: dict[int, set[int]]) -> list[int]:
    """A gentle greedy walk: from a start, always step to a *connected* unvisited
    concept (smoothest transition); when none, jump to the next remaining one.
    Deterministic (min tie-break) so the same graph yields a stable path."""
    if not ids:
        return []
    remaining = set(ids)
    cur = ids[0]
    order = [cur]
    remaining.discard(cur)
    while remaining:
        nbrs = adjacency.get(cur, set()) & remaining
        nxt = min(nbrs) if nbrs else min(remaining)
        order.append(nxt)
        remaining.discard(nxt)
        cur = nxt
    return order


def compose_script(
    ordered: list[tuple[str, str]],
    *,
    client: Any,
    anchors: list[str] | None = None,
    skill_preamble: str = "",
) -> str:
    """One LLM call → the calm meditation script (plain paragraphs). ``ordered``
    is ``(name, definition)`` in walk order; ``anchors`` are retained motifs to
    weave in; ``skill_preamble`` optionally prepends the `precis-voice` craft."""
    lines = ["Walk these ideas, gently, in this order:"]
    for name, definition in ordered:
        lines.append(f"- {name}: {definition}" if definition else f"- {name}")
    if anchors:
        lines.append(
            "\nReturn to these familiar anchor motifs, softly: " + ", ".join(anchors)
        )
    lines.append(
        "\nWrite the meditation now: induction, then the gentle walk touching each "
        "idea in order and how it connects to the one before, then the tapering "
        "coda."
    )
    system = (
        f"{skill_preamble}\n\n{_NIDRA_SYS}".strip() if skill_preamble else _NIDRA_SYS
    )
    out = client.complete(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n".join(lines)},
        ]
    )
    return (getattr(out, "text", "") or "").strip()


def _compose_long(
    ordered: list[tuple[str, str]],
    *,
    client: Any,
    target_words: int,
    anchors: list[str] | None = None,
    skill_preamble: str = "",
) -> str:
    """Segmented long-form walk → the full nidra script (plain paragraphs).

    A 45-minute nidra is ~5000 words — past a single completion's clean ceiling —
    so compose it in pieces: induction (one call) → walk sections over concept
    batches (one call each, fed the tail of the prior passage for continuity) →
    tapering coda (one call), concatenated.

    ``target_words`` (from the profile's minutes × words-per-minute) is what makes
    the cast hit its target length: it is split across the calls and each prompt
    asks for that many words, so the whole nidra runs to the intended duration.
    Without a per-segment target the model writes short and the cast lands far
    under the target (the 2026-07-15 nidra came out ~18 min against a 45-min
    budget precisely because the budget wasn't threaded into the prompts)."""
    system = (
        f"{skill_preamble}\n\n{_NIDRA_SYS}".strip() if skill_preamble else _NIDRA_SYS
    )

    def _call(user: str) -> str:
        out = client.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        )
        return (getattr(out, "text", "") or "").strip()

    batches = [
        ordered[i : i + _CONCEPTS_PER_SEGMENT]
        for i in range(0, len(ordered), _CONCEPTS_PER_SEGMENT)
    ]
    # Split the word budget across every call (induction + each walk batch + coda);
    # the model under-writes an open-ended "write a passage", so give it a number.
    n_calls = len(batches) + 2
    per_call = max(150, target_words // n_calls)

    parts: list[str] = []

    induction = _call(
        f"Write ONLY the opening induction of a long evening meditation: a slow "
        f"settling — a few breaths, the day setting down, the body growing heavy "
        f"and still. Unhurried, dwelling, roughly {per_call} words (do not go "
        f"shorter). Do not begin the walk of ideas yet."
    )
    if induction:
        parts.append(induction)

    for batch in batches:
        lines = [
            "Continue the gentle walk. Drift softly through these ideas in order, "
            "each described in plain words and how it relates to the one before. "
            f"Dwell slowly — roughly {per_call} words for this passage (do not go "
            "shorter); linger on each image before moving on:"
        ]
        for nm, df in batch:
            lines.append(f"- {nm}: {df}" if df else f"- {nm}")
        tail = parts[-1][-400:] if parts else ""
        if tail:
            lines.append(
                f"\nThe previous passage ended: …{tail}\nFlow on from there — no "
                "restart, no fresh induction, no coda yet."
            )
        section = _call("\n".join(lines))
        if section:
            parts.append(section)

    anchor_line = (
        "Softly return to these familiar motifs as you close: "
        + ", ".join(anchors)
        + ". "
        if anchors
        else ""
    )
    coda = _call(
        anchor_line + f"Write ONLY the tapering coda now, roughly {per_call} words: "
        "sentences shorten, the words soften and fade as the listener drifts toward "
        "sleep. No new ideas."
    )
    if coda:
        parts.append(coda)

    return "\n\n".join(p for p in parts if p).strip()


def ensure_reading_project(store: Any) -> int:
    """Find-or-create the strategic todo that owns reading-prep casts (so cast
    drafts have a `draft-of` home). Marked by ``meta.reading_prep_root``."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind='todo' AND deleted_at IS NULL "
            "AND meta->>'reading_prep_root' = '1' ORDER BY ref_id LIMIT 1"
        ).fetchone()
        if row:
            return int(row[0])
    ref = store.insert_ref(
        kind="todo",
        slug=None,
        title="Reading-prep (concept graph + casts)",
        meta={"reading_prep_root": "1"},
    )
    return int(ref.id)


def build_meditation(
    store: Any,
    *,
    client: Any | None = None,
    name: str | None = None,
    cohort: str | None = None,
    max_concepts: int | None = None,
    target_minutes: int | None = None,
    anchors: list[str] | None = None,
    skill_preamble: str = "",
    bias_active_quests: bool = False,
    prefer_mastered: bool = True,
    now: datetime | None = None,
    date_tag: str | None = None,
) -> int | None:
    """Compose the evening meditation and store it as a standalone dated `draft`.

    Returns the draft ref id, or ``None`` if there aren't at least two concepts to
    walk or the model returned nothing. The draft narrates with `af_nicole` (from
    ``meta.voice``, honoured at render time).

    Idempotent per day: once today's ``cast-nidra-<date>`` draft exists a second
    call returns it without re-composing. ``name`` overrides the slug (tests /
    manual one-offs). ``target_minutes`` (default 45) scales both the number of
    concepts walked and whether the script is composed in one call or in segments.

    ``bias_active_quests`` (quest layer slice 2) biases the concept selection
    toward those serving active quests — a no-op until quests + serving concepts
    exist.

    ``prefer_mastered`` (default on) makes the evening walk a drift through the
    listener's *mastered* concepts — highest ``meta.mastery`` first, the
    complement to the morning's retrieval practice. A no-op until the mastery
    pass (`reading/mastery.py`) has data to write.
    """
    profile = CAST_PROFILES["nidra"]
    target = target_minutes if target_minutes is not None else profile.target_minutes
    if max_concepts is None:
        # Scale the walk with the target length (~1 concept per 1.5 min), floored
        # at the short-walk default so a brief nidra still has a handful.
        max_concepts = max(_DEFAULT_MAX_CONCEPTS, (target * 2) // 3)

    now = now or datetime.now(UTC)
    date_tag = date_tag or now.date().isoformat()
    slug = name or cast_slug(profile, date_tag)
    existing = store.get_ref(kind="draft", id=slug)
    if existing is not None:
        log.info("meditation: %s already composed (ref %s)", slug, existing.id)
        return int(existing.id)

    concepts, adjacency = _load(
        store,
        cohort,
        max_concepts,
        bias_active_quests=bias_active_quests,
        prefer_mastered=prefer_mastered,
    )
    if len(concepts) < 2:
        log.info("meditation: only %d concept(s) — nothing to walk", len(concepts))
        return None
    order = _walk_order([c[0] for c in concepts], adjacency)
    by_id = {c[0]: (c[1], c[2]) for c in concepts}
    ordered = [by_id[i] for i in order]

    if client is None:
        # No injected client (the production path) — build one from the nidra
        # profile. Tests always inject a fake client, so this branch stays out of
        # unit runs and off the cheap MCP import graph.
        import os
        from dataclasses import replace

        from precis.reading.cast_common import compose_max_tokens
        from precis.workers.llm_summarize import LlmClient, LlmConfig

        model = os.environ.get("PRECIS_MEDITATION_MODEL") or profile.model
        client = LlmClient(
            replace(
                LlmConfig.from_env(),
                model=model,
                max_tokens=compose_max_tokens(profile, target_minutes=target),
            )
        )

    budget = word_budget(profile, target_minutes=target)
    if budget > SINGLE_CALL_WORD_CEILING and len(concepts) >= _SEGMENT_MIN_CONCEPTS:
        script = _compose_long(
            ordered,
            client=client,
            target_words=budget,
            anchors=anchors,
            skill_preamble=skill_preamble,
        )
    else:
        script = compose_script(
            ordered, client=client, anchors=anchors, skill_preamble=skill_preamble
        )
    if not script:
        log.warning("meditation: model returned no script")
        return None

    ref, _created = create_cast_draft(
        store,
        profile=profile,
        date_tag=date_tag,
        slug=slug,
        title=f"Evening meditation — {date_tag}",
        meta={"cohort": cohort},
    )
    store.add_chunks(ref_id=ref.id, chunk_kind="paragraph", text=script, split=True)
    return int(ref.id)


__all__ = [
    "MEDITATION_VOICE",
    "build_meditation",
    "compose_script",
    "ensure_reading_project",
]

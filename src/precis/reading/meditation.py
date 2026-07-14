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
from typing import Any

log = logging.getLogger(__name__)

MEDITATION_VOICE = "af_nicole"  # the evening voice (per Reto)
_DEFAULT_MAX_CONCEPTS = 14  # a meditation is a gentle handful, not the whole graph
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
    store: Any, cohort: str | None, limit: int
) -> tuple[list[tuple[int, str, str]], dict[int, set[int]]]:
    """``(concepts, adjacency)`` for the walk. Cohort-scoped when given, else the
    most recent concepts. Adjacency unions every graph edge among them."""
    with store.pool.connection() as conn:
        if cohort:
            rows = conn.execute(
                "SELECT ref_id, meta->>'name', meta->>'definition' FROM refs "
                "WHERE kind='concept' AND deleted_at IS NULL "
                "AND jsonb_exists(meta->'cohorts', %s) ORDER BY ref_id LIMIT %s",
                (cohort, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ref_id, meta->>'name', meta->>'definition' FROM refs "
                "WHERE kind='concept' AND deleted_at IS NULL "
                "ORDER BY ref_id DESC LIMIT %s",
                (limit,),
            ).fetchall()
        concepts = [(int(r[0]), r[1] or "", r[2] or "") for r in rows]
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
    client: Any,
    name: str,
    cohort: str | None = None,
    max_concepts: int = _DEFAULT_MAX_CONCEPTS,
    anchors: list[str] | None = None,
    skill_preamble: str = "",
) -> int | None:
    """Compose the evening meditation and store it as a `draft`. Returns the draft
    ref id, or ``None`` if there aren't at least two concepts to walk or the model
    returned nothing. The draft narrates with `af_nicole` (set at render time)."""
    concepts, adjacency = _load(store, cohort, max_concepts)
    if len(concepts) < 2:
        log.info("meditation: only %d concept(s) — nothing to walk", len(concepts))
        return None
    order = _walk_order([c[0] for c in concepts], adjacency)
    by_id = {c[0]: (c[1], c[2]) for c in concepts}
    script = compose_script(
        [by_id[i] for i in order],
        client=client,
        anchors=anchors,
        skill_preamble=skill_preamble,
    )
    if not script:
        log.warning("meditation: model returned no script")
        return None
    ref, _title = store.create_draft(
        name=name,
        title="Evening meditation",
        project_ref_id=ensure_reading_project(store),
        meta={"cast": "nidra", "voice": MEDITATION_VOICE, "cohort": cohort},
    )
    store.add_chunks(ref_id=ref.id, chunk_kind="paragraph", text=script, split=True)
    return int(ref.id)


__all__ = [
    "MEDITATION_VOICE",
    "build_meditation",
    "compose_script",
    "ensure_reading_project",
]

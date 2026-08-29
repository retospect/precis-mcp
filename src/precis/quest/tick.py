"""quest_tick — one bounded step of a quest's autonomous research loop.

Slice 4a of the quest layer (``quest-layer`` (git-only) §The autonomous
research loop). This is the **skeleton** of the loop: a single, in-process,
structured LLM step routed through the LLM routing seam (``dispatch(LlmRequest)``)
that reads the quest's rolling context — its striving statement, the current
dossier, the slice-3 gaps + momentum, and the recent logbook tail — and returns
two things:

* **logbook entries** — 1–4 dated observations / hypotheses / decisions
  reflecting one step of thinking, appended to the WORM logbook; and
* a **rewritten dossier** — the living synthesis (current understanding, best
  leads, what's ruled out, open questions), whole-replaced in place. The
  per-hypothesis dialectic is NOT part of that rewrite: it lives in pinned
  blocks the model maintains through ``dialectic_ops``
  (:func:`precis.quest.dossier.apply_dialectic_op` — quest-dossier-dialectic
  §Mechanism), so it cannot flatten with the prose.

With ``compute=True`` (rung 4b) the tick also materialises the model's
**proposals** into candidate `structure` servers, dispatches their relax sims
(the derived compute lane), and harvests finished results back into the logbook
(:mod:`precis.quest.compute`); off by default so the tick stays a pure reasoning
step unless a caller opts in. No autonomous scheduling yet (rung 4d — a
dispatcher picks which quest ticks when a slot frees). So this rung is **dark**:
nothing mints a tick automatically; it runs only from ``precis quest tick <id>``
or an explicit caller. The ``PRECIS_QUEST_LOOP_ENABLED`` flag
(:func:`quest_loop_enabled`) is defined here for the future autonomous
dispatcher to gate on.

The single model call is injectable (``dispatch_fn``) so the tick is
deterministically unit-testable without a live model.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, overload

import numpy as np

from precis.quest import dossier as dossier_mod
from precis.quest import gaps as gaps_mod
from precis.quest import narrative_budget
from precis.quest.logbook import (
    ENTRY_TYPES,
    LOG_KIND,
    MEASURED_BY,
    append_entry,
    clamp_entry_type,
)
from precis.reading.cast_common import _TOKENS_PER_WORD
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store import Ref, Store

log = logging.getLogger(__name__)

QUEST_LOOP_ENABLED_ENV = "PRECIS_QUEST_LOOP_ENABLED"

#: How many trailing logbook entries to feed the tick as episodic context.
_LOGBOOK_TAIL = 8

#: Per-rung wall ceiling (seconds) for the tick's LLM call. The transport
#: default (600s) proved too tight for a big-tier reasoning pass over a ~20k-char
#: quest prompt — glm-5.2 via OpenRouter routinely needs >10 min, so both rungs
#: of the BIG chain timed out back-to-back (2×600s = the observed 1200s "timed
#: out" ticks, 2026-08-11) and the tick paused with zero output. 900s per rung
#: keeps the worst case (2 rungs) inside the slot-hold TTL
#: (:data:`precis.utils.llm.local_serving._HOLD_TTL_S`). Override via env.
_TICK_LLM_TIMEOUT_ENV = "PRECIS_QUEST_TICK_LLM_TIMEOUT_S"
_TICK_LLM_TIMEOUT_S = 900.0

#: Cap (chars) on the partial-output artifact persisted to the agentlog when
#: the tick's LLM call dies mid-generation. Head + tail halves survive (the
#: setup and wherever the reasoning got to); the middle is elided.
_PARTIAL_RESULT_CAP = 20_000


def _tick_llm_timeout_s() -> float:
    raw = os.environ.get(_TICK_LLM_TIMEOUT_ENV)
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return _TICK_LLM_TIMEOUT_S


#: Per-call budget for the tick's LLM calls, threaded to the claude_p
#: transport's ``--max-budget-usd`` — TIER-AWARE. The library-wide claude_p
#: default ($0.10) killed the first post-reset dialectic-dossier tick
#: mid-generation (``error_max_budget_usd``, 2026-08-27 — a full
#: per-hypothesis rewrite is a longer output than the pre-reset ticks; a
#: completed haiku tick metered ~$0.14), and a flat $0.50 then killed the
#: escalated FRONTIER (opus-class) review the same way — senior-tier
#: pricing needs senior-tier headroom, and the pre-fix $0.10 cap means prod
#: review ticks can rarely have completed at all. Scoped here rather than
#: raised globally so the figure lane and other claude_p users keep their
#: tighter cap. The env override, when set, applies to EVERY tier (operator
#: escape hatch).
_TICK_LLM_MAX_USD_ENV = "PRECIS_QUEST_TICK_MAX_USD"
_TICK_LLM_MAX_USD = 0.50
_TICK_LLM_MAX_USD_BY_TIER = {"frontier": 2.50, "big": 1.50}


def _tick_llm_max_usd(tier: Any = None) -> float:
    raw = os.environ.get(_TICK_LLM_MAX_USD_ENV)
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return _TICK_LLM_MAX_USD_BY_TIER.get(str(tier or ""), _TICK_LLM_MAX_USD)


def _cap_partial(text: str, cap: int = _PARTIAL_RESULT_CAP) -> str:
    if len(text) <= cap:
        return text
    half = cap // 2
    return text[:half] + f"\n…[{len(text) - cap} chars elided]…\n" + text[-half:]


#: Same 1 MiB cap ``meta.transcript`` uses for a ``plan_tick`` (see
#: ``workers/executors/claude_inproc.py``'s ``_TRANSCRIPT_CAP``) — head-
#: preserved, tail-truncated with a matching marker, so a runaway response
#: can't bloat ``refs.meta``.
_JOB_TRANSCRIPT_RAW_CAP = 1_000_000


def _cap_transcript_raw(text: str, cap: int = _JOB_TRANSCRIPT_RAW_CAP) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + "\n…(truncated)"


#: Cap on the *accumulated* per-tick digest log below. Digests run ~300
#: bytes/tick, so this holds a couple hundred ticks — far past any real
#: coordinator lifetime — while still bounding ``refs.meta`` growth.
_JOB_TRANSCRIPT_DIGEST_CAP = 65_536


def _cap_transcript_digests(text: str, cap: int = _JOB_TRANSCRIPT_DIGEST_CAP) -> str:
    """Tail-preserving cap for the accumulated digest log — opposite of
    :func:`_cap_transcript_raw`, because here the *newest* entries (the final
    state and any escalation) are the ones worth keeping."""
    if len(text) <= cap:
        return text
    return "…(older ticks truncated)\n" + text[-cap:]


def _render_tick_conclusion(outcome: QuestTickOutcome) -> str:
    """Condensed forensic record of one quest_tick LLM slice — the quest-loop
    analogue of a ``plan_tick``'s ``meta.transcript`` (gr170252: the loop had
    zero record of what the model saw/did when it misbehaved). Deliberately
    terse — the full prompt already lives on the tick's ``agentlog`` row; this
    is only the outcome digest, cheap enough to keep on every tick."""
    lines = [
        f"quest_tick #{outcome.quest_id} [{outcome.status}] mode={outcome.mode}",
        f"note: {outcome.note}",
        (
            f"logbook+{outcome.logbook_added} dossier_rewritten="
            f"{outcome.dossier_rewritten} proposals={outcome.proposals} "
            f"sims={outcome.sims_dispatched} harvested={outcome.results_harvested} "
            f"searches={outcome.searches_run} papers_linked={outcome.papers_linked} "
            f"ledger_added={outcome.ledger_added} "
            f"dialectic={outcome.dialectic_applied} graduated={outcome.graduated} "
            f"ruled_out={outcome.ruled_out}"
        ),
    ]
    if outcome.cost_usd is not None:
        lines.append(f"cost_usd: {outcome.cost_usd:.4f}")
    return "\n".join(lines)


def _persist_job_transcript(
    store: Store, job_ref_id: int, outcome: QuestTickOutcome, res: Any
) -> None:
    """Persist this slice's outcome onto the owning ``quest_tick`` coordinator
    job's ``refs.meta`` — the same ``transcript`` / ``transcript_raw`` keys a
    ``plan_tick`` writes (:mod:`precis.workers.executors.claude_inproc`), so
    the confusion-mining SQL (``kind='job' AND meta ? 'transcript'``) and the
    sweeper's retention GC (``workers.sweeper._gc_transcripts``) automatically
    cover quest_tick without any bespoke query/GC path.

    ``transcript`` (condensed conclusion) is **appended** on every slice,
    success or failure — the coordinator job ref persists across every
    ``Yield``/resume of one loop, so a wholesale overwrite would keep only
    the *last* tick's digest and lose exactly the earlier-tick record this
    exists to capture. Entries are separated by ``\\n---\\n``, newest last,
    tail-preserved under :data:`_JOB_TRANSCRIPT_DIGEST_CAP`.
    ``transcript_raw`` (the model's full raw output, capped, last-failure-
    wins — raw dumps are too big to accumulate) is added when the slice
    failed, OR — mirroring ``plan_tick``'s
    own ``_precis_tools_used`` gate (``workers/job_types/plan_tick.py``) —
    when it completed with zero precis tool calls on a transport that
    surfaces ``raw_text`` (quest_tick's own structured-JSON dispatch never
    does today, so this branch is dormant here, kept for parity if the
    transport changes). A successful, tool-engaged tick's condensed digest
    is enough; only the case that actually needs debugging gets the raw dump.

    Slices don't get their own job ref (the coordinator's ``kind='job'`` ref
    persists across every ``Yield``/resume of the loop), so this always
    targets ``job_ref_id`` — matching wherever ``plan_tick`` would put a
    per-slice transcript, since there is no finer-grained ref to write onto
    here. Best-effort: a write failure must never abort the tick.
    """
    from precis.workers.executors._common import set_meta

    fields: dict[str, Any] = {}
    conclusion = _render_tick_conclusion(outcome)
    raw_text = getattr(res, "raw_text", None) if res is not None else None
    no_tool_calls = False
    if raw_text:
        from precis.utils.claude_agent import count_tool_use_events

        no_tool_calls = (
            count_tool_use_events(raw_text, name_prefix="mcp__precis__") == 0
        )
    if outcome.status == "failed" or no_tool_calls:
        text = (getattr(res, "text", "") or "") if res is not None else ""
        raw = raw_text or text
        if raw:
            fields["transcript_raw"] = _cap_transcript_raw(raw)
    try:
        with store.tx() as conn:
            row = conn.execute(
                "SELECT meta->>'transcript' FROM refs WHERE ref_id = %s",
                (job_ref_id,),
            ).fetchone()
            prior = row[0] if row and row[0] else None
            fields["transcript"] = _cap_transcript_digests(
                prior + "\n---\n" + conclusion if prior else conclusion
            )
            set_meta(conn, job_ref_id, **fields)
    except Exception:
        log.warning(
            "run_quest_tick: failed to persist job transcript for job #%s",
            job_ref_id,
            exc_info=True,
        )


#: A frontier review steps back over accumulated history — a deeper tail than
#: a cheap local tick.
_LOGBOOK_TAIL_REVIEW = 20


def max_proposals_per_tick() -> int:
    """Materialise/dispatch at most this many proposals per tick (WIP cap).

    Operator decision (2026-08-08): **one proposal at a time** — combined
    with the coordinator's per-quest backpressure (no new tick while this
    quest's sims are in flight, ``workers/job_types/quest_tick.py``), a cap
    of 1 serializes the whole loop to a single proposal's fan-out in flight.
    Extra proposals still land as `hypothesis` logbook entries (leads), just
    not as sims — the next tick can re-propose the best of them against the
    fresh frontier. Env ``PRECIS_QUEST_MAX_PROPOSALS`` overrides.
    """
    try:
        n = int(os.environ.get("PRECIS_QUEST_MAX_PROPOSALS", "1"))
    except ValueError:
        return 1
    return max(1, min(10, n))


def quest_loop_enabled() -> bool:
    """True when the autonomous quest loop is switched on (default OFF).

    Gates the *autonomous* dispatcher (rung 4d), not the manual CLI tick — a
    human running ``precis quest tick`` is explicit intent.
    """
    return os.environ.get(QUEST_LOOP_ENABLED_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(frozen=True)
class QuestTickOutcome:
    quest_id: int
    status: str  # "succeeded" | "failed" | "paused"
    logbook_added: int
    dossier_rewritten: bool
    cost_usd: float | None
    note: str
    # Compute (rung 4b) — all 0 when the tick runs without compute.
    proposals: int = 0
    candidates_created: int = 0
    sims_dispatched: int = 0
    results_harvested: int = 0
    ruled_out: int = 0
    graduated: int = 0  # rung 4e — candidates that crossed the ceiling
    # Lit-search grounding action.
    searches_run: int = 0
    papers_linked: int = 0
    hypotheses_deduped: int = 0
    # Dossier-owned-by-process pinned ledger — applied (non-deduped) `ledger_add`
    # entries plus applied `ledger_ops` (add/mark) this tick. An engagement
    # signal for the coordinator's punt-vs-genuine-dry split
    # (:mod:`precis.workers.job_types.quest_tick`): a tick that only pinned/
    # marked a ledger direction still counted as "the model engaged".
    ledger_added: int = 0
    # Applied `dialectic_ops` (quest-dossier-dialectic §Mechanism) — same
    # engagement standing as `ledger_added`: maintaining a hypothesis's
    # support/counter/experiment block is the model engaging, not a dry tick.
    dialectic_applied: int = 0
    # Cascade (rung 4c).
    escalated: bool = False
    mode: str = "local"  # "local" | "frontier-review"
    #: Why a ``paused`` tick paused — ``None`` on every non-paused outcome (and
    #: on any construction site that predates this field, which is exactly
    #: today's behaviour for them).
    #:
    #: * ``"timeout"`` — the rung's LLM call hit a wall-clock ceiling
    #:   (:attr:`~precis.utils.llm.router.LlmResult.timed_out`).
    #: * ``"window"`` — a wait-for-window pause: breaker trip, dollar cap,
    #:   OAuth quota, worker drain, 429/5xx.
    #:
    #: The coordinator (:mod:`precis.workers.job_types.quest_tick`) splits its
    #: give-up budget on this: a window pause retries for free, a timeout pause
    #: consumes the budget, because the retry is not free — it re-burns the same
    #: ceiling. Carried structurally rather than sniffed out of :attr:`note`, so
    #: the worker never string-matches an LLM error message.
    pause_kind: str | None = None


# ── context assembly ──────────────────────────────────────────────────


def _logbook_tail(store: Store, quest_id: int, n: int = _LOGBOOK_TAIL) -> list[str]:
    """The last ``n`` logbook entries, formatted one per line (oldest first).

    Each line carries the entry's own ``[ql<id>]`` handle (the logbook chunk
    code, ``handle_registry.CHUNK_CODES["quest"]``) — the tick is a single
    tool-less LLM call, so a logbook entry is only citable in the rewritten
    dossier ("belief weakened when the replicate diverged [ql…]") if its
    handle is handed to it here.
    """
    entries = [
        b
        for b in store.blocks.list_blocks_for_ref(quest_id)
        if b.chunk_kind == LOG_KIND
    ]
    lines: list[str] = []
    for b in entries[-n:]:
        meta = b.meta or {}
        etype = meta.get("entry_type", "note")
        by = meta.get("by", "?")
        stamp = b.created_at.date().isoformat() if b.created_at else "?"
        cost = meta.get("cost")
        cost_s = f" cost={cost:g}" if cost else ""
        first = (b.text or "").splitlines()[0] if b.text else ""
        handle = handle_registry.try_format("quest", b.id, chunk=True)
        handle_s = f" [{handle}]" if handle else ""
        lines.append(f"- [{etype} · {stamp} · {by}{cost_s}] {first[:160]}{handle_s}")
    return lines


def _servers_summary(store: Store, quest_id: int) -> list[str]:
    """One line per server kind: count + a couple of ``[handle] title`` items.

    Per-item handles (not just titles) — the tick's single LLM call can only
    cite what's served if the citable handle is in-context.
    """
    live = gaps_mod._live_servers(store, quest_id)
    by_kind: dict[str, list[tuple[str, str]]] = {}
    for r in live:
        title = (r.title or "").splitlines()[0][:50] if r.title else ""
        handle = handle_registry.try_format(r.kind, r.id) or f"{r.kind}:{r.id}"
        by_kind.setdefault(r.kind, []).append((handle, title))
    out: list[str] = []
    for kind in sorted(by_kind):
        items = by_kind[kind][:3]
        sample = "; ".join(f"[{h}] {t}" if t else f"[{h}]" for h, t in items)
        out.append(f"- {kind} ({len(by_kind[kind])}): {sample}")
    return out


#: Token budget for the literature section — replaces a bare paper-count cap
#: so a quest with many served papers (the audited qu164903 case had 852)
#: still gets *some* detail without ballooning the tick prompt. See
#: docs/backlog/quest-dossier-dialectic.md §"Tick diet fixes".
_LITERATURE_TOKEN_BUDGET = 1500
#: Length bound on a served paper's abstract snippet.
_PAPER_DETAIL_CHARS = 300


def _estimate_tokens(text: str) -> int:
    """Rough token count for budgeting the literature section — chars/4 (the
    standard chars-per-token rule of thumb) scaled by the same words→token
    ratio :data:`precis.reading.cast_common._TOKENS_PER_WORD` uses for the
    audio-cast word budget (reused rather than duplicated)."""
    return int(len(text) / 4 * _TOKENS_PER_WORD)


def _paper_abstract_snippet(store: Store, ref: Ref) -> str:
    """A length-bounded snippet of ``ref``'s substance for a paper server.

    Prefers ``meta.abstract`` (the ingest-populated field); falls back to the
    first non-heading body chunk when the abstract hasn't been fetched yet.
    Titles alone don't tell the model what a paper *found* — this hands it
    enough substance to judge relevance, mirroring the depth-first contract
    in :mod:`precis.reading.briefing_cast`.
    """
    meta = getattr(ref, "meta", None) or {}
    abstract = meta.get("abstract") if isinstance(meta, dict) else None
    text = abstract.strip() if isinstance(abstract, str) else ""
    if not text:
        try:
            blocks = store.blocks.list_blocks_for_ref(ref.id)
        except Exception:
            blocks = []
        for b in blocks:
            if b.chunk_kind == "heading":
                continue
            t = (b.text or "").strip()
            if t:
                text = t
                break
    if not text:
        return "(no abstract held — stub awaiting fetch)"
    try:
        from precis.handlers._paper_format import _strip_jats

        text = _strip_jats(text).strip()
    except Exception:  # pragma: no cover - formatter import is best-effort
        pass
    if len(text) > _PAPER_DETAIL_CHARS:
        text = text[:_PAPER_DETAIL_CHARS].rsplit(" ", 1)[0].rstrip() + "…"
    return text


def _paper_citable_handle(store: Store, ref: Ref) -> str | None:
    """The ``[pc<id>]`` handle of ``ref``'s first substantive body chunk, or
    ``None`` when it has none yet (a metadata-only stub awaiting fetch).

    The tick is a single structured LLM call with no live ``search``/``get``
    mid-generation (unlike an agentic writer), so a served paper's citation
    handle has to be handed to it in-context to be citable at all. This is
    the same bare ``[pc<id>]`` paper-chunk convention
    ``get(kind='skill', id='precis-cite-paper-help')`` teaches — copy the
    handle, never guess it — and the dossier is a ``draft`` kind, so the
    export/bibliography materializer already recognizes it inline.
    """
    try:
        blocks = store.blocks.list_blocks_for_ref(ref.id)
    except Exception:
        return None
    for b in blocks:
        if b.chunk_kind == "heading":
            continue
        if (b.text or "").strip():
            return handle_registry.try_format("paper", b.id, chunk=True)
    return None


def _cosine(a: np.ndarray[Any, Any], b: np.ndarray[Any, Any]) -> float:
    """Mirrors :func:`precis.quest.placement._cosine` — kept as a local copy
    per that module's own convention (each caller of this one-line-of-math
    keeps its own copy rather than growing a shared micro-util)."""
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


def _rank_papers_by_relevance(
    store: Store, quest_id: int, papers: list[Ref], budget_tokens: int
) -> list[tuple[Ref, str | None]]:
    """Order a quest's live ``paper`` servers by relevance, tiered so a held
    body and a query-comparable vector both count:

    - tier 1: has a vector *and* a held body (citable now)
    - tier 2: has a vector, stub (no body chunk yet)
    - tier 3: no vector, held body
    - tier 4: no vector, stub

    Cosine (descending, against the quest's own gist vector) breaks ties
    within tiers 1-2; ``papers``' incoming (serves-graph insertion) order
    breaks ties within 3-4. Gaps/hypotheses have no cached vectors and this
    is a prompt-building call, not a model call, so nothing here ever
    embeds on the fly — a paper (or the quest itself) with no cached vector
    just scores 0 / drops to the no-vector tiers.

    When the quest itself has no gist vector yet (``seed_chunk_for_ref`` /
    ``get_chunk_vector`` -> ``None`` — the ~2h demand-driven embedding blind
    window), cosine is skipped entirely and papers degrade to two tiers,
    held-body first then stubs — the same degrade-rather-than-guess posture
    ``embed_query``'s lexical fallback uses.

    Returns ``(ref, citable_handle)`` pairs, ranked — the handle is
    :func:`_paper_citable_handle`'s result, computed once here rather than
    a second time by the renderer. ``budget_tokens`` isn't consulted for
    the ordering itself (every paper costs the same to score); it's part of
    this helper's signature so the budgeted-serving contract — rank, then
    fill to budget — is documented on the ranking call, even though the
    accumulate/cut loop lives in :func:`_served_papers_detail`, the
    renderer.
    """
    del budget_tokens  # not consulted for ordering — see docstring
    seed_cid = store.blocks.seed_chunk_for_ref(quest_id)
    quest_vec = (
        store.blocks.get_chunk_vector(seed_cid) if seed_cid is not None else None
    )
    quest_arr = (
        np.asarray(quest_vec, dtype=np.float64) if quest_vec is not None else None
    )

    keys: list[tuple[int, float, int]] = []
    handles: list[str | None] = []
    for idx, r in enumerate(papers):
        handle = _paper_citable_handle(store, r)
        handles.append(handle)
        held = handle is not None
        score = 0.0
        has_vec = False
        if quest_arr is not None:
            paper_seed_cid = store.blocks.seed_chunk_for_ref(r.id)
            paper_vec = (
                store.blocks.get_chunk_vector(paper_seed_cid)
                if paper_seed_cid is not None
                else None
            )
            if paper_vec is not None:
                has_vec = True
                score = _cosine(quest_arr, np.asarray(paper_vec, dtype=np.float64))
        if quest_arr is None:
            tier = 0 if held else 1
        else:
            tier = (0 if held else 1) if has_vec else (2 if held else 3)
        keys.append((tier, -score, idx))

    order = sorted(range(len(papers)), key=lambda i: keys[i])
    return [(papers[i], handles[i]) for i in order]


def _served_papers_detail(store: Store, quest_id: int) -> list[str]:
    """One line per served `paper`, relevance-ranked and token-budgeted: its
    citable ``[pc<id>]`` handle (when it has a body chunk to point at), a
    short title, and an abstract snippet.

    Ranked by :func:`_rank_papers_by_relevance` against the quest's own
    gist — a quest serving hundreds of papers (the audited qu164903 case)
    gets the *relevant* ones, not just the first few in serves-graph
    insertion order. Filled to :data:`_LITERATURE_TOKEN_BUDGET` rather than
    a bare count cap, always showing at least one paper when there are any;
    a trailing line reports what didn't fit.
    """
    live = gaps_mod._live_servers(store, quest_id)
    papers = [r for r in live if r.kind == "paper"]
    ranked = _rank_papers_by_relevance(
        store, quest_id, papers, _LITERATURE_TOKEN_BUDGET
    )
    out: list[str] = []
    used_tokens = 0
    shown = 0
    for r, handle in ranked:
        title = (r.title or "").splitlines()[0][:80] if r.title else "(untitled)"
        cite = f"[{handle}] " if handle else ""
        line = f"- {cite}{title} — {_paper_abstract_snippet(store, r)}"
        line_tokens = _estimate_tokens(line)
        if shown and used_tokens + line_tokens > _LITERATURE_TOKEN_BUDGET:
            break
        out.append(line)
        used_tokens += line_tokens
        shown += 1
    remaining = len(ranked) - shown
    if remaining > 0:
        out.append(f"(+{remaining} more served papers not shown)")
    return out


#: Instruction appended to the literature section — the dossier is a
#: `draft` kind (module docstring, dossier.py), so its narrative honors the
#: same bare `[pc<id>]` inline-citation convention as any other draft
#: (`get(kind='skill', id='precis-cite-paper-help')`); this tells the model
#: to actually use it against the handles just listed above. The general
#: "copy the handle, never invent one" rule lives once in the dossier-format
#: block near the schema (below) — this stays literature-specific so the two
#: don't repeat/contradict each other.
_CITE_INSTRUCTION = (
    "\nWhen the rewritten dossier states a claim this literature supports, "
    "cite the specific paper inline by the bare `[pc<id>]` handle shown "
    "above (e.g. `...a markedly lower barrier [pc234]`). A served paper "
    "listed with no handle has no body chunk yet (a stub awaiting fetch) — "
    "do not cite it. See `get(kind='skill', id='precis-cite-paper-help')`.\n"
)


def _literature_section(store: Store, quest_id: int) -> str:
    """The ``## Held literature (abstracts)`` section, or ``""`` when none."""
    detail = _served_papers_detail(store, quest_id)
    if not detail:
        return ""
    return (
        "\n## Held literature (abstracts)\n"
        + "\n".join(detail)
        + "\n"
        + _CITE_INSTRUCTION
    )


def _skill_injection_section(stmt: str) -> str:
    """Bimodal skill injection (docs/backlog/skill-question-targets-and-
    injection.md §2): embed the quest's striving statement against the
    skill corpus's question-shaped retrieval targets; on a high-confidence
    match, inject the WHOLE matched skill body. Returns ``""`` (no section
    at all) when nothing clears threshold — no cue/snippet tier, see
    :mod:`precis.skill_index.injection`.

    injection.py's module docstring promises a harness-controlled tick can
    never fail because injection couldn't run; unlike the planner-prompt
    path (guarded by the module-level assembler's try/except), this is the
    only call site in the tick path, so the guard lives here — any
    exception from ``match_skill``/``render_injection`` degrades to no
    injection rather than aborting the tick."""
    try:
        from precis.skill_index.injection import match_skill, render_injection

        match = match_skill(stmt)
        if match is None:
            return ""
        return "\n" + render_injection(match) + "\n"
    except Exception:
        log.warning("run_quest_tick: skill injection failed", exc_info=True)
        return ""


def _ruled_out_handles(
    store: Store, quest_id: int, *, fr: Any | None = None
) -> list[str]:
    """Handles of live candidates carrying a ``ruled-out:`` tag.

    Surfaced in the frontier section so the model does not re-propose a
    material the ledger already killed (gripe 171149) — a ruled-out candidate
    with no converged run would otherwise silently fall into the "awaiting a
    sim" band below, reading as merely unexplored rather than dead.

    ``fr`` reuses an already-computed frontier (see :func:`_frontier_summary`)
    instead of a second live-candidate scan — its ``Candidate.handle`` is the
    same value ``handle_registry.try_format`` would produce, so no extra work
    is needed once ``fr`` is in hand.
    """
    if fr is None:
        from precis.quest.frontier import quest_frontier

        fr = quest_frontier(store, quest_id)
    out = []
    provisional = (p.candidate for p in fr.provisional)
    for c in (*fr.frontier, *fr.dominated, *provisional, *fr.unevaluated):
        if any(str(t).startswith("ruled-out:") for t in store.tags_for(c.ref_id)):
            out.append(c.handle)
    return out


def _frontier_summary(store: Store, quest_id: int, *, fr: Any | None = None) -> str:
    """A compact rendering of the Pareto frontier for a review prompt.

    ``fr`` reuses an already-computed :class:`precis.quest.frontier.
    FrontierResult` (``build_tick_prompt`` computes one per tick and threads
    it here, plus into :func:`_champion` / :func:`_ruled_out_handles` /
    :func:`precis.quest.explore.tried_set_summary`) instead of each
    independently re-scanning live candidates + re-reading every candidate's
    ``struct_runs`` (an N+1) — a reaction-quest tick that also fires the
    commit ladder would otherwise repeat this scan up to ~6×. ``None``
    (default, and every unit test) computes it fresh — unit-testable
    standalone.
    """
    if fr is None:
        from precis.quest.frontier import quest_frontier

        fr = quest_frontier(store, quest_id)
    if not (fr.frontier or fr.dominated or fr.provisional or fr.unevaluated):
        return "(no candidate materials simulated yet)"
    lines = [f"objective: {' · '.join(f'{k} ({s})' for k, s in fr.objectives)}"]
    for c in fr.frontier:
        ms = " ".join(f"{k}={v:g}" for k, v in sorted(c.measures.items()))
        lines.append(f"- FRONTIER {c.handle} {c.name} — {ms}")
    for c in fr.dominated[:5]:
        ms = " ".join(f"{k}={v:g}" for k, v in sorted(c.measures.items()))
        lines.append(f"- beaten   {c.handle} {c.name} — {ms}")
    # Provisional rows: measured but unconfirmed (untrusted values marked ≈,
    # exclusion reason in brackets). Frontier-leaders-if-trusted first; within
    # that, a row whose barrier itself carries an untrusted/nonphysical flag
    # (gripe 263257 — an auto-untrusted ~73.7 eV reading topped the listing
    # ahead of clean measured rows purely by insertion order) sinks below
    # rows with a clean measured barrier — a flagged number is noise the
    # model should read last, not first. Then capped — a long provisional
    # tail is re-sim queue, not reasoning input.
    provisional = sorted(
        fr.provisional,
        key=lambda p: (
            not p.on_frontier,
            p.candidate.flags.get("barrier_trusted") is False,
        ),
    )
    for p in provisional[:10]:
        c = p.candidate
        ms = " ".join(
            (f"{k}≈{v:g}" if k in p.untrusted_keys else f"{k}={v:g}")
            for k, v in sorted(p.measures.items())
        )
        star = " ★provisional-frontier" if p.on_frontier else ""
        why = f" [{'; '.join(p.reasons)}]" if p.reasons else ""
        lines.append(f"- provisional {c.handle} {c.name} — {ms}{star}{why}")
    if len(provisional) > 10:
        lines.append(f"  (+{len(provisional) - 10} more provisional)")
    if fr.unevaluated:
        named = ", ".join(f"{c.handle} {c.name}" for c in fr.unevaluated[:5])
        rest = f" (+{len(fr.unevaluated) - 5} more)" if len(fr.unevaluated) > 5 else ""
        lines.append(f"- awaiting a sim ({len(fr.unevaluated)}): {named}{rest}")
    ruled_out = _ruled_out_handles(store, quest_id, fr=fr)
    if ruled_out:
        lines.append(f"- ruled out (do not re-propose): {', '.join(ruled_out)}")
    return "\n".join(lines)


#: One-liner explaining each KNOWN Pareto axis, for :func:`_axis_reading_notes`
#: — gripe 263257: the prompt's axis explainer used to be a static paragraph
#: that unconditionally declared `log_tof` the activity axis, even for a
#: quest whose `rubric_objectives` had moved off it entirely (e.g. onto the
#: electro axes below). Keyed by measure name, not by quest type, so a new
#: axis needs only a new entry here — never a code branch. An axis with no
#: entry still renders (see `_axis_reading_notes`'s fallback), just without
#: the bespoke read.
_AXIS_NOTES: dict[str, str] = {
    "log_tof": (
        "`log_tof` is the ACTIVITY axis — a candidate that is measured but "
        "dead-slow is legitimately dominated by a faster one, `barrier` alone "
        'no longer settles that. A "provisional" row on kinetics '
        "(`kinetics_note` shown) means the run is UNKNOWN and worth fixing or "
        're-running — never read it as "this candidate is bad", only as "not '
        'yet resolved".'
    ),
    "atom_cost": (
        "`atom_cost` is a SOFT economic axis: a dear composition that buys "
        "real activity can still be worth proposing — it only loses if a "
        "cheaper design beats it on both axes at once."
    ),
    "barrier": (
        "`barrier` is the rate-limiting reaction barrier (eV) — lower means a "
        "faster elementary step; on a quest that also declares `log_tof` it "
        "is a context scalar, not the ranked activity axis."
    ),
    "energy": (
        "`energy` is the relaxed total energy — a stability signal, not "
        "activity; it only orders candidates meaningfully relative to a "
        "shared composition/reference, never across unrelated ones."
    ),
    "span_at_Uopt": (
        "`span_at_Uopt` is the electrochemical rate-limiting span (eV) at "
        "the reaction's own optimal applied potential — lower is a more "
        "kinetically accessible path at that operating voltage."
    ),
    "U_L": (
        "`U_L` is the limiting potential (V) — the applied voltage at which "
        "every elementary step turns exergonic; closer to the "
        "reaction's thermodynamic minimum voltage is better."
    ),
    "U_L_abs": (
        "`U_L_abs` ranks the limiting potential by magnitude regardless of "
        "sign — the rubric minimises how far the limiting step sits from 0 V."
    ),
    "P_side": (
        "`P_side` is the side-reaction pressure at the operating point — "
        "lower means less competing chemistry eating into yield."
    ),
    "selectivity_margin": (
        "`selectivity_margin` is the worst branch-point margin — the main "
        "route's climb minus its closest competing side route; higher means "
        "safer against a side product taking over."
    ),
    "poison_margin": (
        "`poison_margin` is the worst screened poison's margin over the "
        "substrate — higher means more resistance to that poison shutting "
        "the active site down."
    ),
    "trap_margin": (
        "`trap_margin` is the best route's span minus the worst off-route "
        "escape climb — higher means less risk of getting stuck in a "
        "self-poisoning trap state."
    ),
}

#: Rendered only when the quest declares BOTH components — `$/rate` is
#: meaningless (and the leaderboard/frontier rows show `—` for it) otherwise.
_RATE_READOUT_NOTE = (
    "Where both are measured, `$/rate` (atom_cost − log_tof, shown per row) "
    'reads directly as "100× more active buys 100× less catalyst for the '
    'same spend" — use it to judge a dear-but-active tradeoff at a glance.'
)


def _axis_reading_notes(fr: Any) -> str:
    """The tick prompt's "Reading the axes" paragraph — derived from the
    quest's OWN current objective vector (``fr.objectives``), never a
    hardcoded assumption (gripe 263257: the prior static text always named
    `log_tof` the activity axis, even for a quest whose rubric had moved
    entirely onto other axes — e.g. the electro span_at_Uopt/U_L/P_side set —
    which would render a flatly false claim about what the frontier table
    actually ranks on).

    One :data:`_AXIS_NOTES` one-liner per axis the quest currently
    declares, in declaration order; an axis with no known entry gets a
    generic fallback rather than being silently dropped (a quest may declare
    a composite or a not-yet-catalogued measure). `$/rate`'s explainer is
    appended only when BOTH its components (``atom_cost``, ``log_tof``) are
    declared. Empty when the quest declares no objectives at all (never
    happens in practice — :func:`precis.quest.frontier._objectives_for`
    always falls back to :data:`precis.quest.frontier.DEFAULT_OBJECTIVES`).
    """
    keys = [k for k, _ in fr.objectives]
    if not keys:
        return ""
    notes = [
        _AXIS_NOTES.get(k, f"`{k}` is one of this quest's declared objective axes.")
        for k in keys
    ]
    if "atom_cost" in keys and "log_tof" in keys:
        notes.append(_RATE_READOUT_NOTE)
    return "Reading the axes: " + " ".join(notes) + "\n"


def _ledger_constraints(ledger_text: str) -> str:
    """Bullet lines for the pinned attempt tree's tried/ruled-out directions.

    This is dossier-owned-by-process's structural "do not re-propose" constraint —
    strategic *directions* the ledger has pinned as tried/dead (own status, or
    inherited from a ruled-out ancestor; a fully-dead subtree collapses to one
    summary line), distinct from :func:`_ruled_out_handles`'s per-candidate
    `structure` tags. An open/active direction with no ruled-out ancestor is
    the exploration queue, not a constraint, so it's excluded — see
    :func:`precis.quest.dossier.ledger_do_not_repropose`, which does the
    parsing + inheritance + collapse.
    """
    return dossier_mod.ledger_do_not_repropose(ledger_text)


def _ledger_open_summary(ledger_text: str) -> str:
    """Bullet lines for the pinned attempt tree's ``open``/``active``
    directions — the upsert counterpart to :func:`_ledger_constraints`'s
    tried/ruled-out list.

    Without this, the proposer only ever saw what's dead, never what's
    already pinned as a still-open direction — so "when in doubt, add" (the
    old prompt guidance) meant re-adding a rephrasing of something it had
    already proposed. :func:`precis.quest.dossier.add_attempt`'s near-dup
    merge is the correctness backstop for when the model does that anyway;
    this list is the prompt-quality fix that makes it less likely to in the
    first place. See :func:`precis.quest.dossier.ledger_open_nodes`.
    """
    return dossier_mod.ledger_open_nodes(ledger_text)


def _champion(
    store: Store, quest_id: int, *, fr: Any | None = None
) -> tuple[float, str] | None:
    """The current frontier-leading measure as ``(value, name)``, or ``None``.

    Reads the quest's primary rubric objective (``barrier`` for a catalyst
    quest, ``energy`` by default — :mod:`precis.quest.frontier`) and picks
    the best value among the **non-dominated** frontier — nothing needs
    comparing against the dominated/unevaluated bands since a candidate that
    beats every frontier member on the primary key would itself already be on
    the frontier. Feeds the explorer's-creed "champion to beat" line
    (:func:`_explorers_creed`); ``None`` (nothing converged yet) drops that
    line rather than citing a bogus champion.

    ``fr`` reuses an already-computed frontier (see :func:`_frontier_summary`)
    instead of a second frontier/candidate scan. ``None`` (default, and every
    unit test) computes it fresh — unit-testable standalone.
    """
    if fr is None:
        from precis.quest.frontier import quest_frontier

        fr = quest_frontier(store, quest_id)
    if not fr.objectives:
        return None
    key, sense = fr.objectives[0]
    best: tuple[float, str] | None = None
    for c in fr.frontier:
        v = c.measures.get(key)
        if v is None:
            continue
        if best is None or (v < best[0] if sense == "min" else v > best[0]):
            best = (v, c.name)
    return best


def _explorers_creed(store: Store, quest_id: int, *, fr: Any | None = None) -> str:
    """The "relentless researcher" prompt block for a catalyst/reaction quest.

    Code's job here is only the *guarantee the agent acts* (the commit
    re-prompt + tier-escalation ladder in :func:`run_quest_tick`) — never the
    chemistry itself. This block reframes the objective as a **moving**
    champion (beat the current best; there is no fixed finish line) so a
    graduated candidate (:mod:`precis.quest.graduate`) reads as a new floor to
    beat, not a stop signal, and states the tried-set explicitly
    (:func:`precis.quest.explore.tried_set_summary` — a pure DB-fact read, no
    chemistry enumeration) so the model reasons from the live state instead of
    guessing it. The untried composition itself is always the model's own
    chemistry judgment call.

    ``fr`` reuses an already-computed frontier (see :func:`_frontier_summary`)
    and is threaded into both :func:`_champion` and
    :func:`precis.quest.explore.tried_set_summary` — a reaction-quest tick
    would otherwise pay for the same frontier/candidate scan (an N+1 over
    ``struct_runs``) twice more per call. ``None`` (default, and every unit
    test) computes it fresh in each helper — unit-testable standalone.
    """
    from precis.quest import explore as explore_mod

    champion = _champion(store, quest_id, fr=fr)
    if champion is not None:
        value, name = champion
        # Generic over the primary objective (kinetics cutover: this is
        # `log_tof`, sense=max, not the old `barrier`, sense=min — `_champion`
        # already picks the best value on EITHER sense; only the wording here
        # must not hard-code "rate-limiting" (a barrier-specific idea).
        champion_line = (
            f"Champion to beat: the current frontier leader's primary "
            f"measure is {value:g} ({name}). Every tick, propose at least "
            "one untried variant you predict will beat it, and state (a) "
            "the mechanistic reason you expect it to win and (b) your "
            "predicted value.\n"
        )
    else:
        champion_line = ""

    tried_line = explore_mod.tried_set_summary(store, quest_id, fr=fr)
    tried_block = (
        f"{tried_line}\n"
        if tried_line
        else "Nothing tried yet — you have the first move.\n"
    )

    return (
        "\n## The explorer's creed\n"
        "You are a relentless catalysis researcher. Belief: there is always "
        "a better catalyst. A candidate that meets the target is a NEW FLOOR "
        "TO BEAT, never a finish line — it is promoted to a real-world "
        "experiment AND you immediately look for something better.\n"
        f"{champion_line}"
        f"{tried_block}"
        "From why to what: reason from WHY the leader works (e.g. d-band "
        "downshift weakens N–O) to the next variant that should push it "
        "further — transfer the mechanism, don't restate it. YOU choose the "
        "dopant, its placement, and coverage — this system never picks the "
        "chemistry for you, only the ops that build what you choose "
        "(placement = site type + relative offsets — the cell tiles; "
        "absolute positions don't exist).\n"
        'Forbidden: never write "solved", "done", or "closed" about the '
        'quest. "Ruled out" applies to ONE failed variant, never to the '
        "search. Narrating or lit-searching WITHOUT a new proposal is not "
        "progress. Meeting the bar promotes a candidate to a real "
        "experiment; the search continues.\n"
    )


def _reaction_context(store: Store, quest: Ref, *, fr: Any | None = None) -> str:
    """Proposal rules for a **barrier quest** that declares a reaction (autocatpath).

    When the quest carries ``meta.reaction_config`` every candidate is a *catalyst
    slab* — autocatpath places the reactants and measures the rate-limiting barrier,
    so a proposal builds the reaction's slab with the compact ``slab`` op and
    varies only its surface composition. **Prose, not enumeration**: no closed
    element list, no fixed site/coadsorbate menu — the discovery agent picks
    the dopant, its placement, and coverage using its own chemistry judgment
    every tick; code only states what it can build (see the explorer's creed
    below). Absent → ``""`` (a generic materials quest keeps the free-form,
    hand-built-cell proposal rules already in the template).

    ``fr`` is the tick's already-computed frontier (see
    :func:`_frontier_summary`), threaded straight through to
    :func:`_explorers_creed`.
    """
    meta = getattr(quest, "meta", None) or {}
    rc = meta.get("reaction_config")
    if not isinstance(rc, dict) or not rc:
        return ""
    slab = rc.get("slab") or {}
    el = slab.get("element", "Pd")
    size = list(slab.get("size", [3, 3, 4]))
    vac = slab.get("vacuum", 10.0)
    fixl = slab.get("fix_layers", 2)
    sub, tgt, net = (
        rc.get("substrate", "?"),
        rc.get("target", "?"),
        rc.get("network", "?"),
    )
    # Degrees of freedom in PROSE, not an enumerated choices list — the
    # discovery agent owns the chemistry (which element, which site, how
    # much coverage); code only states what it is capable of building.
    knobs = (
        "pick ANY dopant element (your own chemistry judgment), its "
        "placement (an adatom on the surface / a substitution at the "
        "surface layer / a substitution one layer down — a site type + "
        "offset relative to your co-adsorbate, never an absolute cell "
        "position), coverage (1–3 atoms), and an optional co-adsorbate "
        "(e.g. H). Only the fcc(111) facet is buildable today"
    )
    slab_op = (
        f'{{"op": "slab", "element": "{el}", "size": {size}, '
        f'"vacuum": {vac}, "fix_layers": {fixl}}}'
    )
    base = f'{{"ops": [{slab_op}]}}'
    # Illustrative top-layer labels for the examples below — the `slab` op
    # numbers atoms a<El>1..N in ascending-z (ASE fcc111) order, so the top
    # surface layer is the highest-numbered labels. These indices are just
    # plausible ones for the example, not a guarantee for every size.
    try:
        nx, ny, nz = int(size[0]), int(size[1]), int(size[2])
    except Exception:
        nx, ny, nz = 3, 3, 4
    top_index = nx * ny * (nz - 1) + -(-(nx * ny) // 2)  # + ceil(nx*ny/2)
    label = f"a{el}"
    top_label = f"{label}{top_index}"
    # First top-layer atom + two neighbours forming a hollow triangle
    # (row-major layer ordering: i and i+1 in-row, i+nx in the next row).
    top_first = nx * ny * (nz - 1) + 1
    doped = (
        f'{{"ops": [{slab_op}, '
        f'{{"op": "add_atom_site", "element": "Cu", "site": '
        f'{{"type": "hollow", "anchors": ["{label}{top_first}", '
        f'"{label}{top_first + 1}", "{label}{top_first + nx}"]}}}}]}}'
    )
    # `Cu` in the worked examples below is a SYNTAX example only, not a
    # suggested element or a menu — pick your own dopant.
    op_menu = (
        "\nComposition ops you can use on the slab (the slab op labels atoms "
        f"{label}1..N in ascending-z order — the TOP surface layer is the "
        "highest-numbered labels; `Cu` below is a worked SYNTAX example, "
        "not a suggested element — pick your own):\n"
        "- add_atom_site — an adatom placed by NAMING a site, never guessing "
        'coordinates: {"op":"add_atom_site","element":"H","site":'
        '{"type":"hollow","anchors":["aPd1","aPd2","aPd3"]}} '
        '("type" is top/bridge/hollow with 1/2/3 anchors; code resolves the '
        "exact position from the anchor atoms — prefer this over add_atom)\n"
        "- set_element — SUBSTITUTE a surface atom (in-plane dopant / "
        f'single-atom-alloy motif): {{"op":"set_element","atom":"{top_label}",'
        '"element":"Cu"}\n'
        "- vacancy — REMOVE a surface atom (defect site): "
        f'{{"op":"vacancy","atom":"{top_label}"}}\n'
        "- add_atom (advanced/escape hatch) — a raw fractional coordinate: "
        '{"op":"add_atom","element":"Cu","frac":[0.33,0.33,0.52]} '
        "— only when no existing atom anchors the site you mean. DERIVE z "
        "from the cell, never copy it: the top layer sits near frac z≈0.40 "
        "in the standard cell and an adsorbate belongs ONE BOND LENGTH above "
        "it (≈0.46–0.47 for H, ≈0.52 for a metal adatom); z=0.66 floats the "
        "atom ~4 Å above the surface (preflight rejection, or a silent relax "
        "into a subsurface-degenerate site). add_atom_site computes the "
        "height for you — prefer it.\n"
        "You may combine several ops (e.g. two set_element for a 2-atom alloy, "
        "or set_element + vacancy). Vary composition; do not hand-enumerate "
        "atoms.\n"
    )
    # PBC ground rules (qu164903 corner saga): without these the model treats
    # supercell positions as physical sites ("corner" vs "central"), proposes
    # translation-image duplicates as new experiments, and narrates the
    # resulting noise as chemistry. Stated here, in the one prompt block a
    # reaction quest is guaranteed to see — skills are not injected reliably.
    tiling = (
        "\n### The cell tiles\n"
        f"The {nx}x{ny} cell repeats in-plane. A lone dopant has no absolute "
        "position — frac [0,0] and [0.33,0.33] are the SAME crystal: no "
        "corner/edge/center sites exist, and a shifted/rotated/mirrored "
        "copy of an existing candidate is collapsed, not simulated. Only "
        "relative geometry is physical — state placements as offsets from "
        "the co-adsorbate, never cell positions. Max separation is ~half a "
        f"cell vector and images repeat at the {nx}-site spacing, so nothing "
        f"can be isolated. 1 dopant/cell = 1/{nx * ny} ML everywhere. Stored "
        "candidates are canonicalized: read-back coords may be shifted, "
        "relative geometry never.\n"
    )
    # Novelty steer (gripe 171149): a stalled loop tended to re-propose the same
    # handful of dopants. The PRINCIPLE, not a fixed shortlist (no code-owned
    # element list) — the tried-set summary in the creed below states the
    # live "what's already been tried" fact; picking the untried lever
    # (dopant, coverage, site, co-adsorbate) is the model's own judgment.
    novelty = (
        "\nPropose a composition NOT already in the frontier/beaten/awaiting "
        "table or the tried-set below. Use your own chemistry judgment for "
        "which dopant, coverage, site, or co-adsorbate to vary — do not "
        "repeat a composition already tried.\n"
    )
    creed = _explorers_creed(store, quest.id, fr=fr)
    poisons = rc.get("poisons") or []
    poisons_s = ", ".join(str(p) for p in poisons) if poisons else "none screened"
    side_eg = " (e.g. NH2OH*, N2O*, N2)" if net == "ammonia" else ""
    selectivity = (
        "\n### Selectivity & poisoning — the bad energies are part of the "
        "score\n"
        "Each evaluation also measures, on YOUR slab:\n"
        f"- `selectivity_margin` (eV, **maximize**): the worst branch-point "
        f"margin — how much more the easiest side-product step climbs than "
        f"the competing main-route step at the same fork. Positive = side "
        f"products{side_eg} are kinetically disfavored where routes "
        f"diverge; negative = the surface prefers a side product at that "
        "fork. The most competitive side product is named per candidate "
        "(`side_worst`).\n"
        "- `trap_margin` (eV, **maximize**): best-route span minus the "
        "worst off-route state's escape climb — a state the mechanism "
        "reaches but cannot usefully leave (self-poisoning, an "
        "over-stabilised resting state). Negative = that off-route state "
        "accumulates; the trapped state is named (`trap_worst`).\n"
        f"- `poison_margin` (eV, **maximize**): site competition vs screened "
        f"poisons ({poisons_s}) — negative means the poison outcompetes "
        f"{sub} for vacant sites (verdicts per species in "
        "`poison_verdicts`).\n"
        "Each evaluated candidate also carries the engine's verdict on "
        "which axis limits it (`limiting_factor`: activity / selectivity / "
        "poison / trap) and a one-line `worst_problem` — read these first "
        "when deciding what the NEXT candidate should fix.\n"
        "**Literature duty:** the dossier must answer, from the literature, "
        f"(a) what is the most UNDESIRED side product of {sub} → {tgt} on "
        "this class of surface, and (b) what is the most LIKELY poison in "
        "realistic feeds. If it doesn't yet, emit `searches` for exactly "
        "those questions. When the computed `side_worst`/`trap_worst`/"
        "`poison_verdicts` name a species, say in the dossier whether the "
        "literature considers it relevant for this chemistry — and if the "
        "literature names a poison we do NOT screen yet, record that as a "
        "gap (`ledger_ops`, an `add` op with `status: open`).\n"
    )
    return (
        "\n## Reaction R — this is a catalyst-barrier quest\n"
        f"Every candidate is a **catalyst slab**. autocatpath places the reactants "
        f"(**{sub} → {tgt}** via the `{net}` network) on *your* slab and measures "
        f"the rate-limiting **barrier** (eV, an ML-potential NEB); a relax measures "
        f"the slab's **stability** (`energy`). You design the **surface**, NOT the "
        f"adsorbate — {knobs}. Minimise the barrier — beat the current best; "
        f"there is no fixed floor, only a better catalyst.\n"
        f"{selectivity}\n"
        f"Build the slab with the compact `slab` op (do NOT hand-enumerate the {el} "
        f"atoms — the op builds the fcc(111) geometry ASE-exact so autocatpath can "
        f"inject it), then edit composition. Omit the top-level `cell` (the `slab` "
        f"op provides it).\n"
        f"- reference point (propose this verbatim first): `{base}`\n"
        f"- a worked SYNTAX example of a doped variant (Cu here is "
        f"illustrative only — choose your own element): `{doped}`\n"
        f"{op_menu}"
        f"{tiling}"
        f"{novelty}"
        f"{creed}"
    )


def build_tick_prompt(
    store: Store,
    quest: Ref,
    *,
    review: bool = False,
    narrative_override: str | None = None,
) -> str:
    """Assemble the full rolling-context prompt for one tick.

    ``review=True`` builds the **frontier-review** prompt (rung 4c): the senior
    model reviews the accumulated evidence + the Pareto frontier and sets the
    next strategic directions, rather than doing one more local increment.

    ``narrative_override``, when given, is used in place of the PERSISTED
    narrative (:func:`precis.quest.dossier.read_narrative`) — the commit
    re-prompt ladder's rebuild (:func:`_build_commit_prompt`) passes the
    primary tick's just-proposed (not-yet-written) ``dossier_text``
    through here, since the narrative write is deferred past the ladder (the
    growth-ratchet gate needs the ladder's own harvest as progress evidence,
    :func:`run_quest_tick`) — without this, a ladder rebuild would show the
    model its OWN previous-tick narrative and ask it to act on stale context.
    """
    qid = quest.id
    stmt = quest.title or f"quest {qid}"
    prio = quest.prio if quest.prio is not None else "unset"
    dossier_text = (
        narrative_override
        if narrative_override is not None
        else dossier_mod.read_narrative(store, qid)
    )
    ledger_text = dossier_mod.read_ledger(store, qid)
    gaps = gaps_mod.quest_gaps(store, qid)
    momentum = gaps_mod.quest_momentum(store, qid)

    gap_lines = [
        f"- {g.kind}: {g.detail}" + (f" [{g.handle}]" if g.handle else "") for g in gaps
    ] or ["- (none)"]
    tail = _logbook_tail(
        store, qid, n=_LOGBOOK_TAIL_REVIEW if review else _LOGBOOK_TAIL
    ) or ["- (no logbook entries yet)"]
    servers = _servers_summary(store, qid) or ["- (nothing serves this quest yet)"]
    # Always-on measurement table (rung 4c's review banner used to be the only
    # place this rendered; the local tick reasons from the same numbers now).
    # Computed ONCE here and threaded into _frontier_summary / _reaction_context
    # (→ _explorers_creed → _champion / tried_set_summary) — those each used to
    # independently re-run quest_frontier (a live-candidate scan + a
    # struct_runs read per candidate), so a reaction-quest tick that also
    # fires the commit ladder was repeating this ~6× per tick.
    from precis.quest.frontier import quest_frontier

    fr = quest_frontier(store, qid)
    frontier_text = _frontier_summary(store, qid, fr=fr)
    literature = _literature_section(store, qid)

    if review:
        banner = (
            "## FRONTIER REVIEW — you are the senior reviewer\n"
            "Enough has accumulated to step back. Review the evidence + the "
            "Pareto frontier below, decide what it means, rewrite the dossier, "
            "and set 1–3 strategic **directions** for the next phase (in the "
            "`directions` field). Rule out what's beaten.\n\n"
        )
    else:
        banner = ""

    return _PROMPT_TEMPLATE.format(
        review_banner=banner,
        statement=stmt,
        prio=prio,
        momentum=momentum.label,
        momentum_detail=(
            f"{momentum.recent_entries} recent log · "
            f"{momentum.recent_server_events} server events · "
            f"{momentum.open_todo_servers} open todos · "
            f"{momentum.blocked_todo_servers} blocked"
        ),
        dossier=dossier_text or "(no dossier yet)",
        dialectic=dossier_mod.read_dialectic(store, qid),
        ledger_constraints=_ledger_constraints(ledger_text),
        ledger_open=_ledger_open_summary(ledger_text),
        gaps="\n".join(gap_lines),
        logbook="\n".join(tail),
        servers="\n".join(servers),
        frontier=frontier_text,
        axis_notes=_axis_reading_notes(fr),
        literature=literature,
        reaction_context=_reaction_context(store, quest, fr=fr),
        skill_injection=_skill_injection_section(stmt),
        entry_types=", ".join(sorted(ENTRY_TYPES)),
        proposal_cap=max_proposals_per_tick(),
        narrative_word_target=narrative_budget.config_from_meta(
            getattr(quest, "meta", None)
        ).target_words,
    )


_PROMPT_TEMPLATE = """\
You are advancing a long-running research programme toward a perpetual striving \
(a "quest"). This is ONE bounded step of local reasoning — not the whole \
project. Ground everything in the context below; do not invent results you have \
no evidence for.

{review_banner}## The striving
{statement}
(priority {prio}; momentum: {momentum} — {momentum_detail})

## Current dossier (the living synthesis — you will rewrite it)
{dossier}

## Dialectic blocks (live hypotheses — maintain via `dialectic_ops`, do NOT restate in `dossier_text`)
{dialectic}

## Ruled-out ledger (do NOT re-propose these directions)
{ledger_constraints}

## Open ledger directions (already pinned — transition/refine these, don't re-add them)
{ledger_open}

## Gaps (the exploration queue — what is thin or unanswered)
{gaps}

## Recent logbook (episodic — what happened, most recent last)
{logbook}

## What serves this quest
{servers}

## Current Pareto frontier (the measurement table — reason from these numbers)
{frontier}
(This table is computed fresh at tick time — treat it as ground truth. If it \
conflicts with a claim in the dossier above, trust this table and correct the \
dossier. The frontier table is the ONLY authoritative source of measured \
barriers. A "provisional" row is a real measurement that failed a trust check \
(the reason is shown; ≈ marks each unconfirmed value): you MAY reason with it, \
compare against it, and prioritise re-simulating it — but every claim built on \
one must carry the word "provisional", and it never counts as a confirmed \
barrier or a frontier member. A candidate shown as "awaiting a sim" has an \
UNKNOWN barrier — you may NOT cite, claim, or rank on a barrier for it. NEVER \
restate a barrier value from the dossier or logbook; if a dossier claim \
conflicts with this table, the table wins and you must correct the dossier. \
You do not emit `result`/`milestone` entries — the system stamps those from \
simulations; you close a lead with a `dead-end` when the table shows it \
beaten.)

{axis_notes}{literature}{reaction_context}{skill_injection}
## Your step
Do ONE increment of thinking: interpret the state, pick the most promising \
next direction to close a gap, and note what you'd try. Then rewrite the \
dossier to reflect current understanding.

**Progress means new external evidence, not more restating.** If there are open \
hypotheses above, your job is to *close* them — resolve one with evidence \
(a `result`) or kill it (a `dead-end`) — NOT to restate it as a fresh \
hypothesis. Do not mint a hypothesis that merely rephrases one already open. \
When the answer lies in the literature you don't yet hold (a `no-literature` or \
`thin-support` gap, or a hypothesis that points at "published data"), emit \
`searches` to go get it instead of hypothesising in a vacuum. A plain keyword \
`query` works, but add a `hypothetical` (see the `searches` field below) when \
a question-phrased query keeps missing — phrase it as one or two sentences \
that could appear verbatim in the abstract of the paper you wish existed, NOT \
as a question: retrieval matches documents, not questions (this is HyDE).

**The dialectic lives in blocks, not prose.** Each live hypothesis's \
argument state — its supports, its steelman counter, its ONE discriminating \
experiment — is maintained through `dialectic_ops` (see the Dialectic-blocks \
section above and the field below), addressed by the hypothesis's `[fi…]` \
handle. The blocks survive every rewrite; `dossier_text` must NOT restate \
their content — it is the synthesis layer only (what changed, what it \
means, what to do next). When new evidence bears on a hypothesis, emit a \
`support` or `counter` op citing that evidence's handle inline; when a \
hypothesis is resolved either way, emit `settle` with one linked sentence. \
A block showing "experiment: (MISSING …)" owes a discriminating experiment \
with pre-registered branch predictions — emit an `experiment` op for it.

When you rule out or complete a *direction* that must never be revisited, pin \
it to the ledger via `ledger_ops` (permanently preserved); `dossier_text` \
is rewritten every tick, so a rule-out placed only there is forgotten.

The ledger is a TREE of directions, not a flat list: a node is one durable \
direction (`open`/`active`/`tried`/`ruled-out`), and refining a direction \
into a variant is a CHILD node under it, not a new unrelated bullet — so \
"tried c, then tried c-with-x and c-with-y" reads as one branch, not three \
unconnected facts. Use `ledger_ops`, a list of ops applied in order:
- `{{"op": "add", "text": "<direction>", "parent": "<optional: exact text of \
the existing node this refines/varies>", "status": "<optional, default open>"}}`
- `{{"op": "mark", "node": "<exact text of the existing node>", "status": \
"<open|active|tried|ruled-out>", "parent": "<optional: exact text of its \
parent, only needed when that node's text is ambiguous>"}}`
Address a node by quoting its EXISTING text exactly (case doesn't matter) — \
there are no ids. An op that can't resolve its node (not found, or the same \
text in two branches with no disambiguating `parent`) is silently dropped. \
**Upsert discipline**: check the open/active list above FIRST. If an existing \
node already covers the thought you're about to add, `mark` it instead — \
transition its status, or refine it with a CHILD `add` under its exact text \
— rather than `add`-ing a rephrased restatement of it as a new unrelated \
node. (The system also merges an obvious near-duplicate `add` into the \
existing node rather than duplicating it, but that's a backstop, not \
license to skip checking — a merge advances/no-ops silently and teaches the \
ledger nothing a `mark` wouldn't have said more precisely.) Only `add` a \
node that is a genuinely new direction. Ruling out a \
direction implicitly rules out its still-open children in what you're shown \
next tick — you do not need to mark each child individually.

## Dossier format
`dossier_text` is a precis `draft` — stored and displayed as chunks, so the \
chunk IS the block structure. Write blank-line-separated paragraphs of prose. \
BLOCK-level markdown has no renderer and shows up as literal characters to the \
reader: no `#`/`##` headings, no `-`/`*` bullet lists, no code fences, no \
tables. INLINE markup does render and is welcome where it earns its place: \
`**bold**`, `*italic*`, `` `code` ``, `<sub>`/`<sup>`, and `$…$` math (KaTeX — \
use it for formulae and chemical species, e.g. `$NH_3$`, `$C_{{60}}$`). \
Reference anything by copying its exact handle in square brackets from the \
context above: `[st<id>]` a candidate structure (the frontier table), \
`[pc<id>]`/`[pa<id>]` literature (see above), `[fi<id>]` a finding, \
`[cn<id>]` a served concept (the gaps section), `[ql<id>]` a logbook entry \
(the recent-logbook section) — never invent one: parentheses don't linkify \
and a made-up handle resolves to nothing. \
EVERY quantitative value carries its handle inline — including comparison \
numbers recalled from earlier ticks. A number you cannot source from the \
context above, flag as unsourced rather than stating bare.

Respond with EXACTLY ONE JSON object and nothing else:
{{
  "logbook": [
    {{"entry_type": "<one of: {entry_types}>", "text": "<one concise entry>"}}
  ],
  "searches": ["<0–3 literature searches to ground this quest — papers found \
are linked as servers and feed the next step. Either a plain keyword string, \
or {{\"query\": \"<short keyword phrasing, for the external engine>\", \
\"hypothetical\": \"<optional — one or two sentences that could appear \
verbatim in the abstract of the paper you wish existed, not a question \
(HyDE)>\"}}>"],
  "dossier_text": "<the FULL rewritten dossier: current understanding, best \
leads so far, what's ruled out, open questions — plain prose per the format \
above>",
  "ledger_ops": [
    {{"op": "add", "text": "<one durable, permanently-pinned direction — a \
strategy tried/killed/still open, not a single candidate material>",
      "parent": "<optional>", "status": "<optional, default open>"}},
    {{"op": "mark", "node": "<exact existing node text>", "status": \
"<open|active|tried|ruled-out>", "parent": "<optional>"}}
  ],
  "dialectic_ops": [
    {{"op": "open", "hypothesis": "fi<id>"}},
    {{"op": "support", "hypothesis": "fi<id>", "text": "<one why-clause \
with its evidence handle(s) inline, e.g. … [pc123] or [ql456]. At least \
one handle is REQUIRED — an unanchored support/counter is dropped>"}},
    {{"op": "counter", "hypothesis": "fi<id>", "text": "<the steelman \
against it, evidence handles inline — same anchor requirement>"}},
    {{"op": "experiment", "hypothesis": "fi<id>", "text": "<the ONE \
discriminating experiment>", "predicts": "<pre-registered branch \
predictions: what each outcome would mean>"}},
    {{"op": "settle", "hypothesis": "fi<id>", "text": "<one linked \
sentence>", "ruling": "<optional fi<id> of the ruling that settled it>"}}
  ],
  "proposals": [
    {{"name": "<candidate material>", "rationale": "<why test it>",
      "parent": "<optional: slug of the candidate this varies, when you are \
refining an existing one>",
      "structure": {{"cell": {{"a": 8.4, "b": 8.4, "c": 24.0, \
"pbc": [true, true, false]}},
        "ops": [{{"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.5]}}]}}}}
  ],
  "directions": ["<0–3 strategic directions — set these on a frontier review>"]
}}

Give 1–4 logbook entries. A `hypothesis` you'd test, an `observation` from the \
state, a `result` or `dead-end` that *closes* an open hypothesis, or a \
`decision` on direction are the most useful. Aim for roughly {narrative_word_target} \
words in `dossier_text` — but that's a target, not a hard cap: growth is \
fine when it reflects genuinely new evidence (a ruling, a result), not when \
it's restated history. A rewrite that grows well past its previous length \
with nothing new to show for it gets bounced back to you to compress.

`proposals` (0–{proposal_cap} — the loop dispatches at most {proposal_cap} \
per tick and waits for its sims before the next tick, so pick your best next \
experiment rather than a spread) are candidate \
materials to simulate — each an atomistic \
`structure` (a periodic `cell` + `add_atom` ops with fractional coords, or a \
`slab` bulk-template op that builds a metal surface for you — see the reaction \
rules above if this is a catalyst quest). A periodic cell TILES: specs \
differing only by a lattice translation/rotation/mirror are one candidate. \
Only propose a candidate you can \
express as a concrete structure and that is NOT already ruled out; omit \
`structure` if you cannot, and it will be recorded as a lead but not simulated. \
`parent`: the slug of the candidate this one varies, when you are refining an \
existing one rather than starting a fresh direction — enables the frontier \
tree (skip it for a genuinely new direction). A re-proposed spec you've \
already tried gets you a `duplicate proposal` note in the log, not a fresh \
simulation — check the frontier table / recent log before re-proposing. \
Propose nothing if the next step is analysis, not a new material."""


# ── model call + parsing ──────────────────────────────────────────────


def _resolve_tier(tier: Any) -> Any:
    from precis.utils.llm.router import Tier, tier_from_str

    if tier is None:
        return Tier.MEDIUM
    if isinstance(tier, Tier):
        return tier
    # tier_from_str degrades a pre-Phase-C legacy string (a quest's stored
    # meta.loop.tier or an already-baked job's meta.params.tier) onto its
    # capability-tier analogue instead of raising — see router.tier_from_str.
    return tier_from_str(str(tier))


def _extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort parse: the last balanced ``{...}`` block in ``text``."""
    if not text:
        return None
    depth = 0
    start = -1
    candidate: str | None = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidate = text[start : i + 1]
    if candidate is None:
        return None
    try:
        obj = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


#: Top-level keys a tick payload can carry — used to sanity-check the
#: transport's pre-parsed ``.data`` before trusting it. A transport JSON
#: extractor that mis-parses a nested response (the ≤2-deep-regex claude_p
#: bug, 2026-08-27) hands back an inner *fragment* — a dict, but with none
#: of these keys — and preferring it unchecked turned every such tick into
#: a silent no-op.
_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "logbook",
        "ledger_ops",
        "dialectic_ops",
        "dossier_text",
        "dossier_markdown",
        "proposals",
        "searches",
        "directions",
    }
)


def _payload_from_result(res: Any) -> dict[str, Any] | None:
    """Prefer the router's parsed ``.data``; fall back to parsing ``.text``.

    ``.data`` only wins when it looks like a tick payload (carries at least
    one :data:`_PAYLOAD_KEYS` key) — otherwise it is a transport mis-parse
    and the raw text is the better source.
    """
    data = getattr(res, "data", None)
    if isinstance(data, dict) and data and (_PAYLOAD_KEYS & data.keys()):
        return data
    return _extract_json(getattr(res, "text", "") or "")


#: The hypothesis-dedup Jaccard floor + its token-overlap primitives now
#: live in :mod:`precis.quest.dossier` (shared with :func:`add_attempt`'s
#: near-dup ledger merge, dossier-hygiene design) — re-exported here under
#: their original names so this module's one call site
#: (:func:`run_quest_tick`'s hypothesis dedup, below) needed no change.
_HYP_DUP_JACCARD = dossier_mod._HYP_DUP_JACCARD
_sig_tokens = dossier_mod._sig_tokens
_is_near_dup = dossier_mod._is_near_dup


#: Entry types the MODEL may author. `result`, `milestone`, `cost` are
#: SYSTEM-ONLY — stamped by :mod:`precis.quest.compute` (measured facts) or
#: the tick's own tote accounting, never by the model's narration. A model
#: "result" is at best an observation, never a trusted measurement (gripes
#: 171148/171149: a model-fabricated "result" — a barrier it invented, not
#: one autocatpath measured — was indistinguishable from a real one and made the
#: loop believe the quest was solved).
_MODEL_ALLOWED_ENTRY_TYPES: frozenset[str] = frozenset(
    {"note", "observation", "hypothesis", "decision", "dead-end", "reflection"}
)

#: A model-stated numeric barrier claim — e.g. "barrier=0.892 eV" or "0.89 eV"
#: — must never read as fact; only the frontier table / a system harvest is
#: authoritative on a barrier value.
_BARRIER_CLAIM_RE = re.compile(
    r"barrier\s*[=:]\s*[0-9]*\.?[0-9]+|[0-9]*\.?[0-9]+\s*ev", re.IGNORECASE
)

_UNVERIFIED_PREFIX = "[unverified model claim] "


def _sanitize_model_entry(entry_type: str, text: str) -> tuple[str, str]:
    """Clamp a model-authored logbook entry to the model-safe vocabulary.

    Two independent guards, applied in order:

    1. **Type clamp** — `result`/`milestone`/`cost` are system-only, so a
       model emitting one is downgraded to `observation` (the text is kept,
       just demoted — a model "result" is at best an observation).
    2. **Barrier-claim scrub** — a model entry whose text states a numeric
       barrier (``barrier=0.89`` / ``0.89 eV``) gets an ``"[unverified model
       claim] "`` prefix, so it can never be mistaken for a system-measured
       fact downstream (the logbook tail, a dossier synthesis, a search hit).
    """
    if entry_type not in _MODEL_ALLOWED_ENTRY_TYPES:
        entry_type = "observation"
    if _BARRIER_CLAIM_RE.search(text):
        text = _UNVERIFIED_PREFIX + text
    return entry_type, text


# ── the commit re-prompt + tier-escalation ladder ───────────────────────
#
# Core principle: the discovery AGENT owns the chemistry (what to try); code
# only owns the capabilities (the ops it can build) and the *guarantee that
# the agent acts*. So this is not a deterministic proposer — it never picks
# an element/site/composition itself. When the model has dispatched no new
# experiment for PRECIS_QUEST_FORCE_EXPERIMENT_EVERY consecutive ticks, it
# re-prompts the SAME model with a hard "you must propose now" directive; if
# that still produces nothing, it escalates one tier (to the senior/review
# tier) and asks once more. If the model still proposes nothing after that,
# the tick backs off rather than fabricating a dispatch — the coordinator's
# own dry/punt budget (`precis.workers.job_types.quest_tick`) handles a
# persistent stall.


def _build_commit_prompt(
    store: Store,
    quest: Ref,
    *,
    stall: int,
    base_prompt: str | None = None,
    narrative_override: str | None = None,
) -> str:
    """The "you must propose now" re-prompt the commit ladder fires.

    The base context is the normal propose-mode prompt (:func:`build_tick_prompt`,
    ``review=False`` — this never escalates to the frontier-review banner,
    only the model *tier* escalates) plus a hard directive appended. No
    chemistry menu, no enumeration — the agent picks the untried composition
    using its own judgment; this only insists that it act.

    ``base_prompt``, when given, is the primary tick's ALREADY-BUILT prompt
    (``run_quest_tick`` only passes it when that tick itself ran with
    ``review=False`` — i.e. it's byte-identical to what a fresh
    ``build_tick_prompt(..., review=False)`` call would produce), so the
    commit ladder skips rebuilding the whole context (another frontier +
    live-candidate scan) from scratch. ``None`` (default, and every unit
    test) builds it fresh. ``narrative_override`` (only meaningful on a fresh
    rebuild — ``base_prompt is None``) is the primary tick's just-proposed
    ``dossier_text``, threaded straight through to
    :func:`build_tick_prompt` — the narrative write is deferred past this
    ladder (:func:`run_quest_tick`), so without it a rebuild would show the
    model last tick's persisted narrative instead of its own current one.
    """
    base = (
        base_prompt
        if base_prompt is not None
        else build_tick_prompt(
            store, quest, review=False, narrative_override=narrative_override
        )
    )
    directive = (
        "\n## COMMIT NOW\n"
        f"You have dispatched no new experiment for {stall} tick(s). You "
        "MUST now output at least one entry in `proposals` for a "
        "composition NOT in the tried-set above — use your own chemistry "
        "judgment to choose the most promising untried variant (dopant, "
        "placement, coverage, co-adsorbate; a mere in-plane shift of an "
        "existing candidate is the same crystal, not a variant). Do not "
        "review, narrate, or "
        "lit-search this turn; propose a buildable `structure` (a `slab` op "
        "plus composition ops).\n"
    )
    return base + directive


#: Source tag on the commit ladder's own LlmRequest — distinct from the
#: primary tick's "quest_tick"/"quest_review" so per-quest spend is mineable
#: separately (mirrors the existing local-vs-review split, gr162130).
_COMMIT_SOURCE = "quest_tick_commit"


def _commit_ladder_tiers(tier: Any) -> list[Any]:
    """The ladder's rungs, in order: re-prompt at ``tier``, then one tier up.

    At most two — the current tier, then the senior/review tier — and one
    **slice** each (:meth:`_TickRun._stage_ladder`), since each rung is its
    own LLM call and the whole point of the slicing is that no stage bundles
    two.
    """
    from precis.utils.llm.router import Tier

    tiers = [tier]
    if tier != Tier.FRONTIER:
        tiers.append(Tier.FRONTIER)  # escalate once — the senior/review tier
    return tiers


def _commit_rung(
    store: Store,
    quest_id: int,
    attempt_tier: Any,
    prompt: str,
    *,
    disp: Callable[[Any], Any],
    by: str,
) -> tuple[tuple[Any, list[str]] | None, bool]:
    """One rung of the commit re-prompt ladder — a single LLM call.

    Returns ``(committed, transport_error)``:

    * ``committed`` is ``(ComputeStep, proposal_names)`` when this rung's
      model proposed something, materialised/dispatched via the SAME
      :func:`precis.quest.compute.run_compute_step` path as any ordinary
      proposal (idempotent, content-addressed), or ``None`` when the
      response carried no usable ``proposals`` entry. Never fabricates a
      candidate itself.
    * ``transport_error`` is ``True`` when the rung came back with an LLM
      ``error``/``paused`` result (breaker/quota/transport trouble) rather
      than a genuine empty ``proposals`` — the caller ORs it across rungs so
      its back-off log can say "agent unreachable" instead of "agent
      declined" (this feature exists to diagnose stalls from the logbook, so
      that distinction is the whole point).

    The caller wraps this in a ``try/except`` — a raise here must never
    crash the tick. Carries an explicit :func:`_tick_llm_timeout_s` ceiling
    (the transport default used to apply here, so a hung ladder rung could
    outlive the tick's own budget).
    """
    from precis.quest.compute import run_compute_step
    from precis.utils.llm.router import LlmRequest

    res = disp(
        LlmRequest(
            tier=attempt_tier,
            prompt=prompt,
            source=_COMMIT_SOURCE,
            ref_id=quest_id,
            timeout_s=_tick_llm_timeout_s(),
            max_usd=_tick_llm_max_usd(attempt_tier),
        )
    )
    if getattr(res, "error", None) or getattr(res, "paused", False):
        return None, True  # transient/breaker/quota trouble — try the next rung
    payload = _payload_from_result(res)
    proposals = [
        p for p in ((payload or {}).get("proposals") or []) if isinstance(p, dict)
    ]
    if not proposals:
        return None, False
    proposals = proposals[: max_proposals_per_tick()]  # WIP cap (one at a time)
    step = run_compute_step(store, quest_id, proposals, by=by)
    names = [str(p.get("name") or "?") for p in proposals]
    return (step, names), False


# ── narrative growth-ratchet gate (dossier-hygiene design) ─────────────
#
# The dossier's design intent is a whole-rewritten narrative that stays
# BOUNDED (the tick reads it as rolling context instead of replaying the
# logbook) — but the only compactness pressure used to be the prompt phrase
# "Keep the dossier tight", no code-enforced pressure, so a model that copies
# everything forward makes it accrete tick over tick. The pure accept/retry
# decision lives in :mod:`precis.quest.narrative_budget` (reusable outside
# the tick — a future `draft_refresh` job needs the same pressure); this
# section is the tick-specific glue: what counts as "progress" here, how the
# compress re-prompt is worded per :data:`~precis.quest.narrative_budget.
# GateResult.reason`, and how a failed retry is logged.

#: Source tag on the compress re-prompt's own LlmRequest — distinct from the
#: primary tick's "quest_tick"/"quest_review" and the commit ladder's
#: "quest_tick_commit" so per-quest spend stays mineable per lane (gr162130).
_COMPRESS_SOURCE = "quest_tick_narrative_compress"


def _narrative_compress_prompt(md: str, reason: str, *, ceiling_words: int) -> str:
    """The compress re-prompt's instruction, worded per the gate's
    :data:`~precis.quest.narrative_budget.GateResult.reason` — a
    ``"ceiling"`` trip and a ``"no-progress-growth"`` trip call for different
    asks (compress under a hard number, vs. justify NO growth at all)."""
    if reason == "ceiling":
        instruction = (
            f"Your dossier rewrite is over the hard {ceiling_words}-word "
            f"ceiling. Rewrite it to UNDER {ceiling_words} words."
        )
    else:
        instruction = (
            "No new results this tick justify this narrative's growth — "
            "merge redundancy and return to AT MOST the previous "
            "narrative's length."
        )
    return (
        f"{instruction} Preserve every ruling (what's tried, what's ruled "
        "out) and every open question — cut restated background and "
        "repetition, not conclusions. Plain prose, no headings/bullets/bold, "
        "same as before. Respond with ONLY the compressed dossier text, "
        "nothing else.\n\n"
        f"{md}"
    )


def _reprompt_narrative_compress(
    store: Store,
    quest_id: int,
    tier: Any,
    disp: Callable[[Any], Any],
    md: str,
    reason: str,
    *,
    ceiling_words: int,
) -> str:
    """One compress re-prompt for a narrative the growth gate flagged.

    Returns the model's reply text (the caller re-checks it against the
    gate — a compress reply is not trusted blindly), or ``""`` on any
    transport trouble / empty reply. Mirrors the commit ladder's own
    degrade-don't-crash convention: a raise here must never crash the tick.

    Carries the same explicit :func:`_tick_llm_timeout_s` ceiling as every
    other tick call: without one this rode the transport default, so a
    compress re-prompt that hung burned an unbounded slice on what is by
    construction the *cheapest* call of the tick (rewrite one paragraph
    shorter).
    """
    from precis.utils.llm.router import LlmRequest

    try:
        res = disp(
            LlmRequest(
                tier=tier,
                prompt=_narrative_compress_prompt(
                    md, reason, ceiling_words=ceiling_words
                ),
                source=_COMPRESS_SOURCE,
                ref_id=quest_id,
                timeout_s=_tick_llm_timeout_s(),
                max_usd=_tick_llm_max_usd(tier),
            )
        )
    except Exception:
        log.exception(
            "run_quest_tick: narrative-compress re-prompt raised for quest %s",
            quest_id,
        )
        return ""
    if res is None or getattr(res, "error", None) or getattr(res, "paused", False):
        return ""
    data = getattr(res, "data", None)
    if isinstance(data, dict):
        text = str(data.get("dossier_text") or data.get("dossier_markdown") or "")
        if text.strip():
            return text.strip()
    return str(getattr(res, "text", "") or "").strip()


def _apply_narrative_gate(
    store: Store,
    quest: Ref,
    quest_id: int,
    md: str,
    *,
    progress_evidence: bool,
    tier: Any,
    disp: Callable[[Any], Any],
) -> str | None:
    """Run the growth-ratchet gate on a proposed `dossier_text`; return
    the markdown to actually write, or ``None`` when even the compress
    retry is rejected — the caller must then keep the previous narrative
    unrewritten. A rejected-then-accepted retry writes nothing to the
    logbook (silent success); a rejected-then-still-rejected retry appends
    ONE `observation` entry (`by=MEASURED_BY` — a system-enforced fact, not
    model narration) carrying the reason + word counts in ``meta`` (never
    just prose) so the ratchet's thresholds are tunable from data later —
    the dossier-hygiene design.
    """
    prev_words = len(dossier_mod.read_narrative(store, quest_id).split())
    new_words = len(md.split())
    cfg = narrative_budget.config_from_meta(getattr(quest, "meta", None))
    gate = narrative_budget.narrative_growth_gate(
        prev_words, new_words, progress_evidence, cfg
    )
    if gate.ok:
        return md

    reason = gate.reason or "no-progress-growth"
    retry_md = _reprompt_narrative_compress(
        store, quest_id, tier, disp, md, reason, ceiling_words=cfg.ceiling_words
    )
    retry_words = len(retry_md.split()) if retry_md else 0
    if retry_md:
        retry_gate = narrative_budget.narrative_growth_gate(
            prev_words, retry_words, progress_evidence, cfg
        )
        if retry_gate.ok:
            return retry_md

    append_entry(
        store,
        quest_id,
        text=(
            f"dossier narrative rewrite refused ({reason}): {prev_words} → "
            f"{new_words} words"
            + (
                f"; retry {retry_words} words"
                if retry_md
                else "; retry produced nothing usable"
            )
            + " — kept the previous narrative"
        ),
        entry_type="observation",
        by=MEASURED_BY,
        extra_meta={
            "gate_reason": reason,
            "prev_words": prev_words,
            "new_words": new_words,
            "retry_words": retry_words,
        },
    )
    return None


# ── the tick, sliced into resumable stages ────────────────────────────
#
# One tick used to be a single monolithic in-process call: prompt assembly
# → primary LLM call → apply/ledger → inline lit-search → compute dispatch
# → the commit re-prompt ladder (up to 2 more LLM calls) → the narrative
# compress re-prompt (a 4th) → dossier regen. Up to four LLM calls and
# hours of wall-clock behind ONE coordinator slice, and ``coordinator_
# state`` checkpointed only the loop phase (``tick``/``await``) — so a
# slice killed at the last phase redid the prompt and the primary LLM from
# scratch. Quest 164903 burned 53% of 24 h of tick time re-running one
# doomed cloud call that way, and during the 2026-08-10→16 deploy-bounce
# storm the tick could never finish inside the inter-restart window at all.
#
# The tick is now a small state machine over the phase boundaries below,
# each of which makes **at most one LLM call**, so a kill costs one stage
# rather than the whole tick:
#
#     llm → apply → search → compute → ladder(×rungs) → finish
#
# :func:`run_quest_tick` with ``sliced=True`` (the coordinator — see
# :mod:`precis.workers.job_types.quest_tick`) returns a :class:`TickSlice`
# at every boundary and is handed its ``state`` back on the next slice;
# every other caller (the CLI, the allocator, every unit test) gets the old
# run-to-completion behaviour out of the same loop over the same stages, so
# the sliced and un-sliced paths cannot drift.
#
# **Where the checkpoint lives.** The small counters ride in
# ``TickSlice.state`` (→ the job's ``meta.coordinator_state``). The bulky
# things one stage hands the next — the primary LLM's parsed payload, the
# assembled prompt, the ladder's re-prompt — ride on the tick's OWN
# ``agentlog`` row (``meta.prompt`` was already written there at open;
# ``meta.checkpoint`` is the rest, see :func:`precis.agentlog.
# stash_checkpoint`), referenced from the state by ``agentlog_id`` alone. A
# 50 KB payload therefore never lands in job or quest ``meta``, and the
# agentlog's existing 30-day retention GC reaps it. A tick whose agentlog
# failed to open (best-effort, as before) has nowhere to checkpoint and
# simply runs to completion in one slice — degrade, don't crash.
#
# **Idempotence on re-run.** The checkpoint advances only at a stage
# boundary, so a stage whose writes landed but whose Yield didn't is
# replayed from its start:
#
# ===========  ==============================================================
#  stage        replay is safe because…
# ===========  ==============================================================
#  llm          it is the FIRST stage — a replay is exactly today's
#               behaviour (a fresh tick), nothing earlier to lose.
#  apply        ledger ops are content-addressed (``add_attempt`` dedups an
#               identical/near-duplicate node and only ever advances a
#               status; ``mark_attempt`` is a set-status; ``append_ledger_
#               entry`` dedups), and a re-added `hypothesis` is dropped by
#               the same near-dup guard the model's own spins hit. NOT
#               idempotent: plain `note`/`observation`/`decision` entries
#               would duplicate — WORM by design, ≤4 lines, read only as
#               logbook tail.
#  search       ``PaperHandler.acquire`` collapses identifiers on an
#               already-held/already-wanted paper and the `serves` link is
#               unique, so a replay re-links nothing; it re-issues the
#               query (the only real cost) and re-logs one entry per query.
#  compute      ``_ensure_candidate_detail`` is content-addressed (returns
#               ``was_dup``) and ``dispatch_relax`` / ``dispatch_
#               autocatpath`` mint content-addressed jobs that collapse
#               onto the in-flight one; the harvest is a fold over finished
#               runs. Fully idempotent.
#  ladder       one rung per slice; a replayed rung costs one LLM call and
#               lands on the same idempotent ``run_compute_step`` path.
#  finish       ``rewrite_dossier`` is a whole-replace and
#               ``stamp_ref_meta`` an overwrite (both idempotent). NOT
#               idempotent: ``update_cascade_state``'s counter bump and the
#               `cost` deed would double-count.
# ===========  ==============================================================
#
# Requeue-from-checkpoint of a crashed ``STATUS:running`` slice is
# deliberately NOT part of this (the claim SQL requires ``queued`` — see
# ``workers/executors/coordinator.py`` §_persist_dispatch_result); the
# reap/re-mint path stays, it just got cheap.

#: The tick's stage boundaries, in order. The coordinator mirrors these
#: into ``coordinator_state.phase`` as ``tick:<stage>``.
TICK_STAGES: tuple[str, ...] = (
    "llm",
    "apply",
    "search",
    "compute",
    "ladder",
    "finish",
)


@dataclass(frozen=True)
class TickSlice:
    """A tick paused at a stage boundary — the ``sliced=True`` counterpart
    of a :class:`QuestTickOutcome`.

    ``state`` is opaque, JSON-serializable, and belongs to the tick: the
    coordinator parks it verbatim in ``coordinator_state.tick`` and hands
    it straight back as ``run_quest_tick(tick_state=…)``. ``stage`` is the
    stage that just *completed*'s successor — i.e. where the next slice
    will resume — and exists so the coordinator can name the phase it is
    parked at (``tick:apply``) for an operator reading the job.
    """

    stage: str
    state: dict[str, Any]


def _tick_proposals(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The payload's well-formed ``proposals`` — derived, never checkpointed
    (the payload itself is the one parked artifact; everything shaped out of
    it stays a pure function of it, so no two copies can disagree)."""
    return [p for p in (payload.get("proposals") or []) if isinstance(p, dict)]


def _tick_narrative(payload: dict[str, Any]) -> str:
    """The payload's proposed dossier rewrite. New field name is
    ``dossier_text`` (the old ``dossier_markdown`` name was itself the
    strongest wrong-format signal in the prompt — chunks render plain prose,
    not markdown); the old key is still accepted so a mid-flight response
    lands."""
    return str(payload.get("dossier_text") or payload.get("dossier_markdown") or "")


@dataclass
class _TickRun:
    """Driver for one :func:`run_quest_tick` — the stage machine above.

    ``st`` is the ONLY thing that crosses a slice boundary besides the
    agentlog row it points at, so every value in it must stay
    JSON-serializable (ints, strs, bools, ``None``).
    """

    store: Store
    quest_id: int
    tier: Any
    dispatch_fn: Callable[[Any], Any] | None
    by: str
    compute: bool
    review: bool | None
    search_fn: Any
    job_ref_id: int | None
    embedder: Any
    st: dict[str, Any]
    #: In-process mirror of the agentlog-parked blob, so an un-sliced run
    #: never reads back what it just wrote. A *resumed* slice starts cold
    #: and loads it from the agentlog on first use.
    stash: dict[str, Any] | None = None

    # ── plumbing ──────────────────────────────────────────────────────

    @property
    def disp(self) -> Callable[[Any], Any]:
        if self.dispatch_fn is not None:
            return self.dispatch_fn
        from precis.utils.llm.router import dispatch as _dispatch

        return _dispatch

    def resolved_tier(self) -> Any:
        """The tick's tier, rebuilt from the checkpointed string.
        ``Tier`` is a ``StrEnum``, so this round-trips exactly (and
        ``tier_from_str`` degrades rather than raising on a stale value)."""
        from precis.utils.llm.router import tier_from_str

        return tier_from_str(str(self.st.get("tier") or ""))

    def _quest_meta(self) -> dict[str, Any]:
        """The quest's ``meta``, re-read fresh — ``{}`` if it vanished."""
        ref = self.store.get_ref(kind="quest", id=self.quest_id)
        return (getattr(ref, "meta", None) or {}) if ref is not None else {}

    def _quest(self) -> Ref:
        """The quest ref, re-read fresh (a stage can't hold one across a
        slice). Raises if it vanished mid-tick — both call sites sit inside
        a degrade-don't-crash ``try``."""
        ref = self.store.get_ref(kind="quest", id=self.quest_id)
        if ref is None:
            raise RuntimeError(f"quest {self.quest_id} vanished mid-tick")
        return ref

    def _load_stash(self) -> dict[str, Any]:
        from precis import agentlog

        if self.stash is None:
            log_id = self.st.get("agentlog_id")
            self.stash = (
                agentlog.read_checkpoint(self.store, log_id=int(log_id))
                if log_id is not None
                else {}
            )
        return self.stash

    def _park(self, **fields: Any) -> None:
        """Park bulky per-tick state on this tick's own agentlog row.

        Best-effort in both directions: a tick with no agentlog (open
        failed) keeps everything in memory and just can't be sliced, and a
        failed write degrades the same way rather than aborting the tick.
        """
        from precis import agentlog

        stash = self._load_stash()
        stash.update(fields)
        log_id = self.st.get("agentlog_id")
        if log_id is None:
            return
        try:
            agentlog.stash_checkpoint(
                self.store,
                log_id=int(log_id),
                # ``prompt`` already has its own agentlog meta key (written
                # at open) — read back into the stash, never re-written.
                checkpoint={k: v for k, v in stash.items() if k != "prompt"},
            )
        except Exception:
            log.warning(
                "run_quest_tick: failed to park a stage checkpoint on agentlog %s",
                log_id,
                exc_info=True,
            )

    def payload(self) -> dict[str, Any]:
        """The primary LLM call's parsed payload, from the stash."""
        p = self._load_stash().get("payload")
        return p if isinstance(p, dict) else {}

    def _bump(self, key: str, n: int = 1) -> None:
        self.st[key] = int(self.st.get(key) or 0) + n

    def _finalize(
        self,
        outcome: QuestTickOutcome,
        *,
        res: Any = None,
        partial: str | None = None,
    ) -> QuestTickOutcome:
        from precis import agentlog

        agentlog_id = self.st.get("agentlog_id")
        if agentlog_id is not None:
            # The tick is over, so its stage checkpoint is dead weight —
            # dropped in the same write that stamps run-end, so a live
            # ``meta.checkpoint`` always means "a tick is mid-flight here"
            # and a finished tick's ~50 KB payload doesn't sit on the row
            # for the whole 30-day agentlog retention window.
            meta_extra: dict[str, Any] = {agentlog.CHECKPOINT_KEY: {}}
            # Non-succeeded ticks stamp WHY (the outcome note) — a bare
            # status="failed" row is undiagnosable.
            if outcome.status != "succeeded" and outcome.note:
                meta_extra["error"] = outcome.note
            try:
                agentlog.finalize_log(
                    self.store,
                    log_id=int(agentlog_id),
                    status=outcome.status,
                    # Partial output salvaged from a mid-generation abort
                    # (a streamed rung's StreamTimeout) — persisted so the
                    # reasoning isn't lost with the connection; the
                    # /agentlogs viewer shows it next to the prompt.
                    result=partial or None,
                    meta_extra=meta_extra,
                )
            except Exception:
                log.warning(
                    "run_quest_tick: failed to finalize agentlog", exc_info=True
                )
        # Job-ref transcript (gr170252 forensic gap) — distinct from the
        # agentlog record above: this is what the sweeper's existing
        # transcript GC + confusion-mining SQL already know how to find.
        if self.job_ref_id is not None:
            _persist_job_transcript(self.store, self.job_ref_id, outcome, res)
        return outcome

    def _result_stub(self) -> Any:
        """A stand-in for the primary ``LlmResult`` on a stage that no
        longer holds it (it died with the slice that made the call). Only
        :func:`_persist_job_transcript` reads it, and only for its
        ``raw_text`` no-tool-calls gate — which is why ``raw_text`` is the
        one field parked alongside the payload."""
        from types import SimpleNamespace

        stash = self._load_stash()
        return SimpleNamespace(
            text="", raw_text=stash.get("raw_text"), cost_usd=self.st.get("cost")
        )

    # ── the stage machine ─────────────────────────────────────────────

    def step(self) -> QuestTickOutcome | None:
        """Run the current stage. Returns the tick's outcome when it ends
        here, else ``None`` after advancing ``st['stage']``."""
        stages: dict[str, Callable[[], QuestTickOutcome | None]] = {
            "llm": self._stage_llm,
            "apply": self._stage_apply,
            "search": self._stage_search,
            "compute": self._stage_compute,
            "ladder": self._stage_ladder,
            "finish": self._stage_finish,
        }
        stage = str(self.st.get("stage") or "llm")
        fn = stages.get(stage)
        if fn is None:
            # A checkpoint from a future/renamed build. Restarting the tick
            # costs one LLM call; honoring an unknown stage would silently
            # skip work, so restart loudly.
            log.warning("run_quest_tick: unknown stage %r — restarting the tick", stage)
            self.st = {"stage": "llm"}
            fn = self._stage_llm
        return fn()

    def _stage_llm(self) -> QuestTickOutcome | None:
        """Assemble the prompt, open the run's agentlog, make **the** primary
        LLM call, park its payload. The only stage that can end the tick
        early (quest gone / LLM error / unparseable output)."""
        from precis import agentlog
        from precis.quest import cascade as cascade_mod
        from precis.utils.llm.router import LlmRequest, Tier

        store, quest_id = self.store, self.quest_id
        qref = store.get_ref(kind="quest", id=quest_id)
        if qref is None or qref.deleted_at is not None:
            return QuestTickOutcome(
                quest_id, "failed", 0, False, None, "quest not found"
            )

        # Cascade: decide local vs. frontier review (unless the caller forces it).
        signal = cascade_mod.escalation_signal(store, quest_id)
        is_review = signal.escalate if self.review is None else self.review
        reason = (
            signal.reason
            if (self.review is None and is_review)
            else ("forced" if is_review else "")
        )
        if self.tier is not None:
            resolved_tier = _resolve_tier(self.tier)
        elif is_review:
            resolved_tier = Tier.FRONTIER
        else:
            resolved_tier = Tier.MEDIUM

        prompt = build_tick_prompt(store, qref, review=is_review)

        # Open a run-attribution record (kind='agentlog') carrying the full
        # assembled prompt — the twin of ``plan_tick``'s own agentlog wiring
        # (:mod:`precis.workers.job_types.plan_tick`) so the ``/agentlogs/<id>``
        # web viewer can show a quest tick's prompt + session the same way it
        # shows a planner tick's. Best-effort: a failure here must never abort
        # the tick — it only costs this tick its ability to be sliced (the
        # stage checkpoint has nowhere to live; see the banner above).
        model_str = str(resolved_tier)
        agentlog_id: int | None = None
        try:
            agentlog_id = agentlog.open_log(
                store,
                source="quest_tick",
                title=f"quest_tick #{quest_id} ({model_str})",
                model=model_str,
                prompt=prompt,
                parent_ref_id=quest_id,
                job_ref_id=self.job_ref_id,
            )
        except Exception:
            log.warning("run_quest_tick: failed to open agentlog", exc_info=True)

        self.st.update(
            {
                "agentlog_id": agentlog_id,
                "is_review": bool(is_review),
                "reason": reason,
                "tier": model_str,
                "prompt_chars": len(prompt),
            }
        )
        self.stash = {"prompt": prompt}

        # Attribute the call to *this* quest (llm_call_log.ref_id) and split the
        # lane in `source` so per-quest, local-vs-review spend is mineable from
        # the log — neither is back-fillable, so it must be stamped at dispatch
        # (gr162130).
        res = self.disp(
            LlmRequest(
                tier=resolved_tier,
                prompt=prompt,
                source="quest_review" if is_review else "quest_tick",
                ref_id=quest_id,
                # The transport-default 600s wall cap cut big-tier reasoning off
                # mid-thought (see _TICK_LLM_TIMEOUT_S); streaming makes the cap a
                # hard ceiling with an idle timeout underneath, and preserves the
                # partial output on abort instead of dropping the connection's
                # entire generation.
                timeout_s=_tick_llm_timeout_s(),
                max_usd=_tick_llm_max_usd(resolved_tier),
                stream=True,
            )
        )
        cost = getattr(res, "cost_usd", None)
        self.st["cost"] = float(cost) if isinstance(cost, (int, float)) else None
        if getattr(res, "error", None):
            # Whatever the model produced before the failure (a streamed rung's
            # partial reasoning/content rides LlmResult.text alongside the error)
            # is persisted to the agentlog rather than lost with the connection.
            salvage = _cap_partial((getattr(res, "text", "") or "").strip())
            # A window-scoped breaker trip (dollar cap / claude-OAuth quota) is a
            # pause, not a failure: report "paused" so the allocator skips it (no
            # pick recorded, no panel "failed") and re-picks once the window
            # clears — instead of burning a tick + a FAILED-PASSES row every
            # worker cycle.
            if getattr(res, "paused", False):
                # Split the pause by *cause* for the coordinator's give-up budget
                # (see QuestTickOutcome.pause_kind): a wall-clock timeout is not a
                # window that will roll off — the same prompt on the same rung
                # re-burns the same ceiling — so it must not retry for free.
                return self._finalize(
                    QuestTickOutcome(
                        quest_id,
                        "paused",
                        0,
                        False,
                        cost,
                        f"paused: {res.error}",
                        pause_kind=(
                            "timeout" if getattr(res, "timed_out", False) else "window"
                        ),
                    ),
                    res=res,
                    partial=salvage,
                )
            return self._finalize(
                QuestTickOutcome(
                    quest_id, "failed", 0, False, cost, f"llm error: {res.error}"
                ),
                res=res,
                partial=salvage,
            )

        payload = _payload_from_result(res)
        if payload is None:
            # The model completed cleanly but its text didn't parse as a tick
            # action — persist the raw text so the malformed output can be
            # inspected instead of vanishing with the failure (glm-5.2 has
            # returned prose/near-empty answers here; without the text the
            # failure mode is invisible).
            return self._finalize(
                QuestTickOutcome(
                    quest_id, "failed", 0, False, cost, "unparseable model output"
                ),
                res=res,
                partial=_cap_partial((getattr(res, "text", "") or "").strip()),
            )

        raw_text = getattr(res, "raw_text", None)
        self.st["resp_chars"] = len(getattr(res, "text", "") or "")
        self._park(
            payload=payload,
            raw_text=_cap_transcript_raw(str(raw_text)) if raw_text else None,
        )
        self.st["stage"] = "apply"
        return None

    def _stage_apply(self) -> QuestTickOutcome | None:
        """Apply the payload's WORM writes — logbook, ledger, proposal leads,
        review directions. No LLM call, no network."""
        store, quest_id, by = self.store, self.quest_id, self.by
        payload = self.payload()

        # Open hypotheses to dedup fresh ones against — a spin is the same
        # question restated, so a near-duplicate `hypothesis` is dropped rather
        # than appended.
        open_hyps = list(gaps_mod._open_hypotheses(store, quest_id))
        deduped = 0

        # Apply logbook entries (clamp unknown entry types rather than reject).
        added = 0
        for e in payload.get("logbook") or []:
            if not isinstance(e, dict):
                continue
            text = str(e.get("text") or "").strip()
            if not text:
                continue
            etype = clamp_entry_type(e.get("entry_type"))
            # The model may only narrate, not measure: `result`/`milestone`/`cost`
            # clamp to `observation`, and a stated barrier number gets flagged
            # unverified — see :func:`_sanitize_model_entry` (gripes 171148/171149).
            etype, text = _sanitize_model_entry(etype, text)
            if etype == "hypothesis" and _is_near_dup(text, open_hyps):
                deduped += 1
                continue
            raw_cost = e.get("cost")
            cost_val = float(raw_cost) if isinstance(raw_cost, (int, float)) else None
            append_entry(
                store, quest_id, text=text, entry_type=etype, by=by, cost=cost_val
            )
            if etype == "hypothesis":
                open_hyps.append(text)
            added += 1

        # Ledger — pin any tried/ruled-out/open *directions* the model wants to
        # survive the whole-rewrite below. Applied BEFORE `rewrite_dossier` so a
        # same-tick rule-out is pinned even if the fresh narrative drops it
        # (dossier-owned-by-process — the structural fix for the autocatpath
        # dead-3-days spin).
        ledger_added = 0
        for e in payload.get("ledger_add") or []:
            if not isinstance(e, dict):
                continue
            text = str(e.get("text") or "").strip()
            if not text:
                continue
            section = str(e.get("section") or "").strip()
            if dossier_mod.append_ledger_entry(store, quest_id, section, text):
                ledger_added += 1

        # ledger_ops (dossier-hygiene design) — the tree-capable successor to
        # `ledger_add` above: `add` a node (optionally under `parent`) or `mark`
        # an existing one's status. Applied in order, same BEFORE-the-rewrite
        # placement as `ledger_add` for the same reason. Each op degrades
        # silently on a bad shape / an ambiguous or unmatched node — that's
        # `add_attempt`/`mark_attempt`'s own no-op contract (never a guess) — but
        # logged here so a persistently-malformed model payload is diagnosable;
        # a raise from either call must never crash the tick (mirrors the
        # compute-step / commit-ladder degrade-don't-crash convention below).
        for op in payload.get("ledger_ops") or []:
            if not isinstance(op, dict):
                continue
            kind = str(op.get("op") or "").strip()
            raw_parent = op.get("parent")
            parent = str(raw_parent).strip() if raw_parent else None
            try:
                if kind == "add":
                    text = str(op.get("text") or "").strip()
                    status = str(op.get("status") or "open").strip() or "open"
                    applied = dossier_mod.add_attempt(
                        store, quest_id, text, parent=parent, status=status
                    )
                elif kind == "mark":
                    node = str(op.get("node") or "").strip()
                    status = str(op.get("status") or "").strip()
                    applied = dossier_mod.mark_attempt(
                        store, quest_id, node, status, parent=parent
                    )
                else:
                    applied = False
            except Exception:
                log.exception(
                    "run_quest_tick: ledger_ops entry raised for quest %s: %r",
                    quest_id,
                    op,
                )
                applied = False
            if applied:
                ledger_added += 1
            else:
                log.info(
                    "run_quest_tick: ledger_ops entry for quest %s not applied "
                    "(bad shape / ambiguous / unmatched): %r",
                    quest_id,
                    op,
                )

        # dialectic_ops (quest-dossier-dialectic §Mechanism) — maintain the
        # pinned per-hypothesis dialectic blocks (support/counter/experiment/
        # settle, addressed by fi handle). Same BEFORE-the-rewrite placement
        # and degrade-don't-crash contract as ledger_ops: each op is a silent
        # no-op on a bad shape or an unresolvable hypothesis, logged here so a
        # persistently-malformed payload stays diagnosable.
        # Capped per tick: unlike ledger nodes (quest-local markdown), these
        # ops mint blocks/entries AND corpus-wide evidence edges — a looping
        # payload must not fan out unboundedly. 16 is generous: a tick
        # legitimately touches a few hypotheses, not dozens.
        dialectic_applied = 0
        for op in (payload.get("dialectic_ops") or [])[:16]:
            if not isinstance(op, dict):
                continue
            try:
                applied = dossier_mod.apply_dialectic_op(store, quest_id, op)
            except Exception:
                log.exception(
                    "run_quest_tick: dialectic_ops entry raised for quest %s: %r",
                    quest_id,
                    op,
                )
                applied = False
            if applied:
                dialectic_applied += 1
            else:
                log.info(
                    "run_quest_tick: dialectic_ops entry for quest %s not "
                    "applied (bad shape / unresolvable hypothesis / dup): %r",
                    quest_id,
                    op,
                )

        # Proposals — log each candidate as a hypothesis (WORM). The
        # materialise + dispatch half is the `compute` stage.
        proposals = _tick_proposals(payload)
        for p in proposals:
            name = str(p.get("name") or "").strip()
            if not name:
                continue
            rationale = str(p.get("rationale") or "").strip()
            buildable = " [buildable]" if isinstance(p.get("structure"), dict) else ""
            append_entry(
                store,
                quest_id,
                text=f"candidate: {name}{buildable} — {rationale}"[:400],
                entry_type="hypothesis",
                by=by,
            )
            added += 1

        # Directions — set on a frontier review; recorded as a `decision` deed.
        if self.st.get("is_review"):
            directions = [
                str(d).strip()
                for d in (payload.get("directions") or [])
                if str(d).strip()
            ]
            if directions:
                append_entry(
                    store,
                    quest_id,
                    text="frontier review — next directions: " + "; ".join(directions),
                    entry_type="decision",
                    by=by,
                )
                added += 1

        self.st.update(
            {
                "stage": "search",
                "added": added,
                "deduped": deduped,
                "ledger_added": ledger_added,
                "dialectic_applied": dialectic_applied,
                "n_proposals": len(proposals),
            }
        )
        return None

    def _stage_search(self) -> QuestTickOutcome | None:
        """Lit-search — go ground the quest in the literature (the missing half
        of the loop). Runs in the same acting mode as compute; linking a paper
        is external progress, so it must land BEFORE update_cascade_state
        resets the stall clock. Its own stage because a per-DOI acquire walk is
        minutes of network, not a fraction of the compute step."""
        self.st["stage"] = "compute"
        if not self.compute:
            return None

        from precis.quest.search import run_search_step

        # Each entry is either the legacy plain query string, or
        # `{"query": "...", "hypothetical": "..."}` (HyDE — dossier-hygiene
        # design) — passed through RAW; `run_search_step` does its own
        # per-entry parsing/blank-filtering (a plain `str(q)` coercion here
        # would mangle a dict entry into its Python repr).
        raw_searches = self.payload().get("searches")
        if isinstance(raw_searches, list) and raw_searches:
            sstep = run_search_step(
                self.store,
                self.quest_id,
                raw_searches,
                by=self.by,
                search_fn=self.search_fn,
                embedder=self.embedder,
            )
            self.st["searches_run"] = sstep.queries_run
            self.st["papers_linked"] = sstep.papers_linked
            self._bump("added", sstep.queries_run)
        return None

    def _stage_compute(self) -> QuestTickOutcome | None:
        """Materialise + dispatch the capped proposals, harvest what landed,
        and advance the stall counter — routing to the commit ladder when the
        model has gone ``PRECIS_QUEST_FORCE_EXPERIMENT_EVERY`` ticks without
        dispatching an experiment."""
        if not self.compute:
            self.st["stage"] = "finish"
            return None

        from precis.quest.compute import run_compute_step

        store, quest_id, by = self.store, self.quest_id, self.by
        proposals = _tick_proposals(self.payload())

        # WIP cap — dispatch at most max_proposals_per_tick() (default 1);
        # the rest were already logged as `hypothesis` leads by `apply`.
        capped = proposals[: max_proposals_per_tick()]
        if len(proposals) > len(capped):
            append_entry(
                store,
                quest_id,
                text=(
                    f"WIP cap: dispatching {len(capped)} of {len(proposals)} "
                    "proposals this tick — the rest stay leads; re-propose "
                    "the best against the fresh frontier next tick"
                ),
                entry_type="observation",
                by=MEASURED_BY,
            )
        # A raise here must never crash the tick (mirrors _phase_weave_tick and
        # the commit-ladder try/except). run_compute_step's dispatch lane
        # (dispatch_autocatpath) raises loudly on the gr172886 no-GPU null-route
        # misconfiguration; without this guard that RuntimeError propagates out
        # of run_quest_tick to the coordinator's blanket except, which
        # terminalizes the WHOLE coordinator job and loses the mid-slice
        # checkpoint. Log it loudly (the misconfig stays visible) and degrade to
        # a zero-dispatch outcome — the stall counter then advances and the
        # escalation ladder handles the persistent failure.
        try:
            step = run_compute_step(store, quest_id, capped, by=by)
        except Exception:
            log.exception(
                "run_quest_tick: compute step raised for quest %s — degrading "
                "to a backed-off (zero-dispatch) outcome",
                quest_id,
            )
            step = None
        if step is not None:
            self.st["created"] = step.candidates_created
            self.st["dispatched"] = step.sims_dispatched
            self.st["harvested"] = step.results_harvested
            self.st["ruled"] = step.ruled_out
            self.st["graduated"] = step.graduated

        # Commit re-prompt + tier-escalation ladder: a structural guarantee
        # that the AGENT is asked to act — never a code-chosen dispatch. A
        # model tick that dispatched a real sim resets the stall counter; one
        # that dispatched nothing advances it, and once it reaches
        # PRECIS_QUEST_FORCE_EXPERIMENT_EVERY consecutive dry ticks (default 2)
        # the tick fires the ladder — one rung per slice, from `_stage_ladder`.
        # The counter is stamped by `finish`, whichever way the ladder goes.
        if int(self.st.get("dispatched") or 0) > 0:
            self.st["stall"] = 0
            self.st["stage"] = "finish"
            return None
        stall = int(self._quest_meta().get("ticks_since_experiment", 0) or 0) + 1
        self.st["stall"] = stall
        force_every = int(os.environ.get("PRECIS_QUEST_FORCE_EXPERIMENT_EVERY", "2"))
        self.st["stage"] = "ladder" if stall >= force_every else "finish"
        return None

    def _commit_prompt(self) -> str:
        """The ladder's "you must propose now" re-prompt — built once on the
        first rung and parked, so the escalated rung re-asks with the
        byte-identical context (as it did when both rungs ran inside one
        call) instead of paying for a second full-context rebuild."""
        stash = self._load_stash()
        cached = stash.get("commit_prompt")
        if isinstance(cached, str) and cached:
            return cached
        # Reuse the primary tick's already-built prompt when it was built in
        # propose mode (review=False) — byte-identical to what the ladder
        # would otherwise rebuild from scratch (another frontier +
        # live-candidate scan). A review tick's prompt carries the review
        # banner, so the ladder must rebuild its own propose-mode one. And if
        # this same tick just created a candidate (created > 0), the cached
        # prompt predates it — rebuild so the re-prompt's tried-set/frontier
        # reflects the new candidate.
        reuse = not self.st.get("is_review") and not int(self.st.get("created") or 0)
        base = stash.get("prompt") if reuse else None
        prompt = _build_commit_prompt(
            self.store,
            self._quest(),
            stall=int(self.st.get("stall") or 0),
            base_prompt=base if isinstance(base, str) else None,
            # The narrative write is deferred past this ladder (the
            # growth-ratchet gate needs the ladder's own harvest as progress
            # evidence) — a rebuild must see THIS tick's just-proposed
            # narrative, not last tick's persisted one. Irrelevant when the
            # base prompt is reused (no rebuild happens).
            narrative_override=_tick_narrative(self.payload()).strip() or None,
        )
        self._park(commit_prompt=prompt)
        return prompt

    def _ladder_gave_up(self) -> None:
        """Log why the ladder ended without a commit — never fabricating a
        dispatch of its own. The two reasons read differently on purpose:
        diagnosing a stall from the logbook is the whole point of the
        ladder's log line."""
        if self.st.get("ladder_error"):
            # LLM transport/breaker/quota trouble, not a genuine decline.
            text = "agent unreachable (LLM error/paused) — backing off, will retry"
        else:
            text = (
                "agent declined to propose an untried variant after commit "
                "re-prompt + tier escalation"
            )
        append_entry(
            self.store, self.quest_id, text=text, entry_type="decision", by=self.by
        )
        self._bump("added")

    def _stage_ladder(self) -> QuestTickOutcome | None:
        """ONE rung of the commit re-prompt ladder — one LLM call, one slice.

        A rung that errors or declines advances ``ladder_rung`` and yields; a
        rung that proposes folds its :class:`~precis.quest.compute.ComputeStep`
        into the tick's counters and ends the ladder. A raise anywhere here
        must never crash the tick (mirrors the weave-tick try/except
        convention in ``workers/job_types/quest_tick.py``'s
        ``_phase_weave_tick``) — degrade to a normal backed-off outcome, and
        let `finish` stamp the counter either way.
        """
        store, quest_id, by = self.store, self.quest_id, self.by
        tiers = _commit_ladder_tiers(self.resolved_tier())
        rung = int(self.st.get("ladder_rung") or 0)
        self.st["stage"] = "finish"

        if rung >= len(tiers):  # a stale / foreign checkpoint — give up here
            self._ladder_gave_up()
            return None

        try:
            committed, rung_error = _commit_rung(
                store,
                quest_id,
                tiers[rung],
                self._commit_prompt(),
                disp=self.disp,
                by=by,
            )
        except Exception as exc:  # defensive — a ladder bug must not crash
            # the tick; see the docstring above.
            log.exception("tick #%s: commit re-prompt ladder raised", quest_id)
            append_entry(
                store,
                quest_id,
                text=(
                    "commit re-prompt ladder errored "
                    f"({type(exc).__name__}: {exc}); backing off"
                ),
                entry_type="observation",
                by=by,
            )
            self._bump("added")
            return None

        if rung_error:
            self.st["ladder_error"] = True
        if committed is None:
            if rung + 1 < len(tiers):
                self.st["ladder_rung"] = rung + 1
                self.st["stage"] = "ladder"  # next slice takes the next rung
                return None
            # Last rung: give up in THIS slice rather than burning another
            # one on a stage that would make no LLM call at all.
            self._ladder_gave_up()
            return None

        fstep, names = committed
        self._bump("created", fstep.candidates_created)
        self._bump("dispatched", fstep.sims_dispatched)
        self._bump("harvested", fstep.results_harvested)
        self._bump("ruled", fstep.ruled_out)
        self._bump("graduated", fstep.graduated)
        stall = int(self.st.get("stall") or 0)
        if fstep.sims_dispatched > 0:
            self._bump("proposals_committed", len(names))
            append_entry(
                store,
                quest_id,
                text=(
                    f"committed after re-prompt: {', '.join(names)} — "
                    f"model stalled {stall} tick(s) with no experiment"
                ),
                entry_type="decision",
                by=by,
            )
            self._bump("added")
            self.st["stall"] = 0
        else:
            # The ladder returned a proposal but nothing reached simulation
            # (e.g. a candidate materialised without wiring/dispatch —
            # gr201814). Report the truth, carrying the step notes, so the
            # logbook is self-diagnosing rather than claiming a commit that
            # never happened — and leave ``stall`` advanced so the next tick
            # keeps pressing.
            detail = "; ".join(n for n in fstep.notes if n) or "no dispatch"
            append_entry(
                store,
                quest_id,
                text=(
                    f"re-prompt proposed {', '.join(names)} but 0 "
                    f"sims dispatched ({detail}) — still stalled "
                    f"{stall} tick(s)"
                ),
                entry_type="observation",
                by=by,
            )
            self._bump("added")
        return None

    def _stage_finish(self) -> QuestTickOutcome | None:
        """Stamp the stall counter, regenerate the frontier-tree chunk, run the
        narrative growth-ratchet gate (the tick's last possible LLM call — one
        compress re-prompt), advance the cascade, meter the spend, and return
        the outcome."""
        from precis.quest import cascade as cascade_mod

        store, quest_id, by = self.store, self.quest_id, self.by
        st = self.st
        is_review = bool(st.get("is_review"))
        added = int(st.get("added") or 0)
        ledger_added = int(st.get("ledger_added") or 0)
        dialectic_applied = int(st.get("dialectic_applied") or 0)
        harvested = int(st.get("harvested") or 0)
        papers_linked = int(st.get("papers_linked") or 0)
        created = int(st.get("created") or 0)
        ruled = int(st.get("ruled") or 0)
        graduated = int(st.get("graduated") or 0)
        rewritten = False

        if self.compute:
            store.stamp_ref_meta(
                quest_id, {"ticks_since_experiment": int(st.get("stall") or 0)}
            )
            # Regenerate the pinned frontier-tree dossier chunk (Slice 4c-4) now
            # that harvest (above) has landed this tick's measures — a code-only
            # rewrite, never surfaced to the model as something to author.
            # Defensive: a render bug must not crash the tick (mirrors the
            # commit-ladder / compute-step try/except convention).
            try:
                from precis.quest.dossier import update_frontier_tree

                update_frontier_tree(store, quest_id)
            except Exception:
                log.exception(
                    "run_quest_tick: frontier-tree regen failed for quest %s", quest_id
                )

        # Narrative growth-ratchet gate (dossier-hygiene design) — deferred to
        # HERE (the payload captured it back at `apply` time) so
        # `progress_evidence` reflects this tick's FULL outcome: an applied
        # ledger op, a harvest (`frontier update`), or a linked paper (this
        # loop's stand-in for "citation mint" — `precis.quest.citation_mint`
        # itself is the paper-writing weave's own minter, not called from here).
        # The ledger tree is never gated — see the module banner above.
        # Defensive: a gate/rewrite bug must not crash the tick — on a raise,
        # the previous narrative simply survives untouched.
        narrative_md = _tick_narrative(self.payload()).strip()
        if narrative_md:
            try:
                progress_evidence = (
                    bool(ledger_added)
                    or bool(dialectic_applied)
                    or bool(harvested)
                    or bool(papers_linked)
                )
                accepted_md = _apply_narrative_gate(
                    store,
                    self._quest(),
                    quest_id,
                    narrative_md,
                    progress_evidence=progress_evidence,
                    tier=self.resolved_tier(),
                    disp=self.disp,
                )
                if accepted_md is not None:
                    dossier_mod.rewrite_dossier(store, quest_id, accepted_md)
                    rewritten = True
            except Exception:
                log.exception(
                    "run_quest_tick: narrative growth-ratchet gate failed for quest %s",
                    quest_id,
                )

        # Advance the cascade counters + recompute `promise` (rung 4d reads it).
        cascade_mod.update_cascade_state(store, quest_id, reviewed=is_review)

        did_work = (
            added
            or rewritten
            or created
            or harvested
            or ruled
            or graduated
            or papers_linked
        )
        reason = str(st.get("reason") or "")
        note = (
            (f"frontier-review ({reason})" if is_review else "ok")
            if did_work
            else "no-op"
        )
        # Attribute the tick's *real* measured usage to the tote (gripe 162594).
        # Quest ticks run on the claude_p transport at MEDIUM, where
        # ``cost_usd`` is null/0.00 for 100% of prod rows (free/quota-bound lane)
        # and ``total_tokens`` is never populated either — so metering the tote in
        # dollars or tokens silently starves ``over_budget`` of any signal. Chars
        # (prompt + response text) IS always available, so that's the unit: one
        # terse ``cost`` deed per successful tick carries the char count into the
        # dated ledger. ``cost`` is still recorded when a transport happens to
        # report one (future priced lanes), but it no longer gates whether the
        # deed is written — a paused/errored tick returns early at `llm`, so this
        # only runs on success.
        cost = st.get("cost")
        chars = int(st.get("prompt_chars") or 0) + int(st.get("resp_chars") or 0)
        cost_val = float(cost) if isinstance(cost, (int, float)) and cost > 0 else None
        cost_note = f" (${cost_val:.4f})" if cost_val is not None else ""
        append_entry(
            store,
            quest_id,
            text=(
                f"tick spend {chars:,} chars{cost_note} "
                f"({'review' if is_review else 'local'})"
            ),
            entry_type="cost",
            by=by,
            cost=cost_val,
            chars=chars,
        )
        return self._finalize(
            QuestTickOutcome(
                quest_id,
                "succeeded",
                added,
                rewritten,
                cost,
                note,
                proposals=(
                    int(st.get("n_proposals") or 0)
                    + int(st.get("proposals_committed") or 0)
                ),
                candidates_created=created,
                sims_dispatched=int(st.get("dispatched") or 0),
                results_harvested=harvested,
                ruled_out=ruled,
                graduated=graduated,
                searches_run=int(st.get("searches_run") or 0),
                papers_linked=papers_linked,
                hypotheses_deduped=int(st.get("deduped") or 0),
                ledger_added=ledger_added,
                dialectic_applied=dialectic_applied,
                escalated=is_review,
                mode="frontier-review" if is_review else "local",
            ),
            res=self._result_stub(),
        )


@overload
def run_quest_tick(
    store: Store,
    quest_id: int,
    *,
    tier: Any = ...,
    dispatch_fn: Callable[[Any], Any] | None = ...,
    by: str = ...,
    compute: bool = ...,
    review: bool | None = ...,
    search_fn: Any = ...,
    job_ref_id: int | None = ...,
    embedder: Any | None = ...,
    tick_state: dict[str, Any] | None = ...,
    sliced: Literal[False] = ...,
) -> QuestTickOutcome: ...


@overload
def run_quest_tick(
    store: Store,
    quest_id: int,
    *,
    tier: Any = ...,
    dispatch_fn: Callable[[Any], Any] | None = ...,
    by: str = ...,
    compute: bool = ...,
    review: bool | None = ...,
    search_fn: Any = ...,
    job_ref_id: int | None = ...,
    embedder: Any | None = ...,
    tick_state: dict[str, Any] | None = ...,
    sliced: Literal[True],
) -> QuestTickOutcome | TickSlice: ...


def run_quest_tick(
    store: Store,
    quest_id: int,
    *,
    tier: Any = None,
    dispatch_fn: Callable[[Any], Any] | None = None,
    by: str = "agent",
    compute: bool = False,
    review: bool | None = None,
    search_fn: Any = None,
    job_ref_id: int | None = None,
    embedder: Any | None = None,
    tick_state: dict[str, Any] | None = None,
    sliced: bool = False,
) -> QuestTickOutcome | TickSlice:
    """Run one structured research step against ``quest_id``.

    ``dispatch_fn`` is injectable (defaults to the real router ``dispatch``) so
    the tick is unit-testable with a canned ``LlmResult``. ``compute=True`` (rung
    4b) materialises the model's proposals into candidate `structure` servers,
    dispatches their relax sims, and harvests results. ``review`` (rung 4c) forces
    the tier: ``None`` (default) lets the escalation signal decide — a local tick
    unless enough evidence / a stall triggers a **frontier review** at the senior
    tier; ``True``/``False`` overrides it. An explicit ``tier`` wins over both.
    ``job_ref_id`` is the owning ``quest_tick`` coordinator job's ref id, when
    known (threaded onto the run-attribution ``agentlog`` — see below).
    ``embedder`` (optional) powers a HyDE `searches` entry's corpus leg
    (:func:`precis.quest.search.run_search_step` — dossier-hygiene design);
    ``None`` degrades that leg to fused-lexical-only, same as the broad
    `search()` verb with no embedder configured.

    **Slicing.** By default this runs the whole tick — every stage in the
    banner above — in one call and returns its :class:`QuestTickOutcome`.
    ``sliced=True`` instead returns a :class:`TickSlice` at each stage
    boundary, which the caller parks and hands back as ``tick_state`` on the
    next slice; the tick then costs at most one LLM call per call. Only the
    coordinator uses it (a kill between stages is the failure mode it
    exists for). A tick that could not open its agentlog has nowhere to park
    its checkpoint and runs to completion even under ``sliced=True``.
    """
    run = _TickRun(
        store=store,
        quest_id=quest_id,
        tier=tier,
        dispatch_fn=dispatch_fn,
        by=by,
        compute=compute,
        review=review,
        search_fn=search_fn,
        job_ref_id=job_ref_id,
        embedder=embedder,
        st=dict(tick_state) if tick_state else {"stage": "llm"},
    )
    while True:
        outcome = run.step()
        if outcome is not None:
            return outcome
        if sliced and run.st.get("agentlog_id") is not None:
            return TickSlice(stage=str(run.st["stage"]), state=dict(run.st))


__all__ = [
    "QUEST_LOOP_ENABLED_ENV",
    "TICK_STAGES",
    "QuestTickOutcome",
    "TickSlice",
    "build_tick_prompt",
    "quest_loop_enabled",
    "run_quest_tick",
]

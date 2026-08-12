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
  leads, what's ruled out, open questions), whole-replaced in place.

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
from typing import TYPE_CHECKING, Any

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


def _cap_partial(text: str, cap: int = _PARTIAL_RESULT_CAP) -> str:
    if len(text) <= cap:
        return text
    half = cap // 2
    return text[:half] + f"\n…[{len(text) - cap} chars elided]…\n" + text[-half:]


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
    # Cascade (rung 4c).
    escalated: bool = False
    mode: str = "local"  # "local" | "frontier-review"


# ── context assembly ──────────────────────────────────────────────────


def _logbook_tail(store: Store, quest_id: int, n: int = _LOGBOOK_TAIL) -> list[str]:
    """The last ``n`` logbook entries, formatted one per line (oldest first)."""
    entries = [
        b for b in store.list_blocks_for_ref(quest_id) if b.chunk_kind == LOG_KIND
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
        lines.append(f"- [{etype} · {stamp} · {by}{cost_s}] {first[:160]}")
    return lines


def _servers_summary(store: Store, quest_id: int) -> list[str]:
    """One line per server kind: count + a couple of titles."""
    live = gaps_mod._live_servers(store, quest_id)
    by_kind: dict[str, list[str]] = {}
    for r in live:
        title = (r.title or "").splitlines()[0] if r.title else ""
        by_kind.setdefault(r.kind, []).append(title[:50])
    out: list[str] = []
    for kind in sorted(by_kind):
        titles = [t for t in by_kind[kind] if t][:3]
        sample = ("; ".join(titles)) if titles else ""
        out.append(f"- {kind} ({len(by_kind[kind])}): {sample}")
    return out


#: Cap on how many served papers get an abstract snippet in the tick prompt.
_MAX_DETAIL_PAPERS = 6
#: Length bound on a served paper's abstract snippet.
_PAPER_DETAIL_CHARS = 300


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
            blocks = store.list_blocks_for_ref(ref.id)
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
        blocks = store.list_blocks_for_ref(ref.id)
    except Exception:
        return None
    for b in blocks:
        if b.chunk_kind == "heading":
            continue
        if (b.text or "").strip():
            return handle_registry.try_format("paper", b.id, chunk=True)
    return None


def _served_papers_detail(store: Store, quest_id: int) -> list[str]:
    """One line per served `paper`: its citable ``[pc<id>]`` handle (when it
    has a body chunk to point at), a short title, and an abstract snippet."""
    live = gaps_mod._live_servers(store, quest_id)
    papers = [r for r in live if r.kind == "paper"][:_MAX_DETAIL_PAPERS]
    out: list[str] = []
    for r in papers:
        title = (r.title or "").splitlines()[0][:80] if r.title else "(untitled)"
        handle = _paper_citable_handle(store, r)
        cite = f"[{handle}] " if handle else ""
        out.append(f"- {cite}{title} — {_paper_abstract_snippet(store, r)}")
    return out


#: Instruction appended to the literature section — the dossier is a
#: `draft` kind (module docstring, dossier.py), so its narrative honors the
#: same bare `[pc<id>]` inline-citation convention as any other draft
#: (`get(kind='skill', id='precis-cite-paper-help')`); this tells the model
#: to actually use it against the handles just listed above.
_CITE_INSTRUCTION = (
    "\nWhen the rewritten dossier states a claim this literature supports, "
    "cite the specific paper inline by the bare `[pc<id>]` handle shown "
    "above (e.g. `...a markedly lower barrier [pc234]`) — copy the handle "
    "from the list, never invent one. A served paper listed with no handle "
    "has no body chunk yet (a stub awaiting fetch) — do not cite it. See "
    "`get(kind='skill', id='precis-cite-paper-help')`.\n"
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
    for c in (*fr.frontier, *fr.dominated, *fr.unevaluated):
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
    if not (fr.frontier or fr.dominated or fr.unevaluated):
        return "(no candidate materials simulated yet)"
    lines = [f"objective: {' · '.join(f'{k} ({s})' for k, s in fr.objectives)}"]
    for c in fr.frontier:
        ms = " ".join(f"{k}={v:g}" for k, v in sorted(c.measures.items()))
        lines.append(f"- FRONTIER {c.handle} {c.name} — {ms}")
    for c in fr.dominated[:5]:
        ms = " ".join(f"{k}={v:g}" for k, v in sorted(c.measures.items()))
        lines.append(f"- beaten   {c.handle} {c.name} — {ms}")
    if fr.unevaluated:
        named = ", ".join(f"{c.handle} {c.name}" for c in fr.unevaluated[:5])
        rest = f" (+{len(fr.unevaluated) - 5} more)" if len(fr.unevaluated) > 5 else ""
        lines.append(f"- awaiting a sim ({len(fr.unevaluated)}): {named}{rest}")
    ruled_out = _ruled_out_handles(store, quest_id, fr=fr)
    if ruled_out:
        lines.append(f"- ruled out (do not re-propose): {', '.join(ruled_out)}")
    return "\n".join(lines)


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
        champion_line = (
            f"Champion to beat: the current best rate-limiting measure is "
            f"{value:g} ({name}). Every tick, propose at least one untried "
            "variant you predict will beat it, and state (a) the mechanistic "
            "reason you expect it to win and (b) your predicted value.\n"
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
        "chemistry for you, only the ops that build what you choose.\n"
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
        "surface layer / a substitution one layer down), coverage (1–3 "
        "atoms), and an optional co-adsorbate (e.g. H). Only the fcc(111) "
        "facet is buildable today"
    )
    slab_op = (
        f'{{"op": "slab", "element": "{el}", "size": {size}, '
        f'"vacuum": {vac}, "fix_layers": {fixl}}}'
    )
    base = f'{{"ops": [{slab_op}]}}'
    doped = (
        f'{{"ops": [{slab_op}, '
        f'{{"op": "add_atom", "element": "Cu", "frac": [0.33, 0.33, 0.66]}}]}}'
    )
    # An illustrative top-layer label for the op menu below — the `slab` op
    # numbers atoms a<El>1..N in ascending-z (ASE fcc111) order, so the top
    # surface layer is the highest-numbered labels. This index is just a
    # plausible central one for the example, not a guarantee for every size.
    try:
        nx, ny, nz = int(size[0]), int(size[1]), int(size[2])
    except Exception:
        nx, ny, nz = 3, 3, 4
    top_index = nx * ny * (nz - 1) + -(-(nx * ny) // 2)  # + ceil(nx*ny/2)
    label = f"a{el}"
    top_label = f"{label}{top_index}"
    # `Cu` in the worked examples below is a SYNTAX example only, not a
    # suggested element or a menu — pick your own dopant.
    op_menu = (
        "\nComposition ops you can use on the slab (the slab op labels atoms "
        f"{label}1..N in ascending-z order — the TOP surface layer is the "
        "highest-numbered labels; `Cu` below is a worked SYNTAX example, "
        "not a suggested element — pick your own):\n"
        "- add_atom  — an adatom ON the surface: "
        '{"op":"add_atom","element":"Cu","frac":[0.33,0.33,0.66]}\n'
        "- set_element — SUBSTITUTE a surface atom (in-plane dopant / "
        f'single-atom-alloy motif): {{"op":"set_element","atom":"{top_label}",'
        '"element":"Cu"}\n'
        "- vacancy — REMOVE a surface atom (defect site): "
        f'{{"op":"vacancy","atom":"{top_label}"}}\n'
        "You may combine several ops (e.g. two set_element for a 2-atom alloy, "
        "or set_element + vacancy). Vary composition; do not hand-enumerate "
        "atoms.\n"
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
    primary tick's just-proposed (not-yet-written) ``dossier_markdown``
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

    gap_lines = [f"- {g.kind}: {g.detail}" for g in gaps] or ["- (none)"]
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
        ledger_constraints=_ledger_constraints(ledger_text),
        gaps="\n".join(gap_lines),
        logbook="\n".join(tail),
        servers="\n".join(servers),
        frontier=frontier_text,
        literature=literature,
        reaction_context=_reaction_context(store, quest, fr=fr),
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

## Ruled-out ledger (do NOT re-propose these directions)
{ledger_constraints}

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
barriers. A candidate shown as "awaiting a sim" has an UNKNOWN barrier — you \
may NOT cite, claim, or rank on a barrier for it. NEVER restate a barrier \
value from the dossier or logbook; if a dossier claim conflicts with this \
table, the table wins and you must correct the dossier. You do not emit \
`result`/`milestone` entries — the system stamps those from simulations; you \
close a lead with a `dead-end` when the table shows it beaten.)
{literature}{reaction_context}
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
`searches` to go get it instead of hypothesising in a vacuum.

When you rule out or complete a *direction* that must never be revisited, pin \
it to the ledger via `ledger_ops` (permanently preserved); `dossier_markdown` \
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
text in two branches with no disambiguating `parent`) is silently dropped, \
so when in doubt add a new node rather than guess at one. Ruling out a \
direction implicitly rules out its still-open children in what you're shown \
next tick — you do not need to mark each child individually.

Respond with EXACTLY ONE JSON object and nothing else:
{{
  "logbook": [
    {{"entry_type": "<one of: {entry_types}>", "text": "<one concise entry>"}}
  ],
  "searches": ["<0–3 literature queries to ground this quest — papers found are \
linked as servers and feed the next step>"],
  "dossier_markdown": "<the FULL rewritten dossier in markdown: current \
understanding, best leads so far, what's ruled out, open questions>",
  "ledger_ops": [
    {{"op": "add", "text": "<one durable, permanently-pinned direction — a \
strategy tried/killed/still open, not a single candidate material>",
      "parent": "<optional>", "status": "<optional, default open>"}},
    {{"op": "mark", "node": "<exact existing node text>", "status": \
"<open|active|tried|ruled-out>", "parent": "<optional>"}}
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
words in `dossier_markdown` — but that's a target, not a hard cap: growth is \
fine when it reflects genuinely new evidence (a ruling, a result), not when \
it's restated history. A rewrite that grows well past its previous length \
with nothing new to show for it gets bounced back to you to compress.

`proposals` (0–{proposal_cap} — the loop dispatches at most {proposal_cap} \
per tick and waits for its sims before the next tick, so pick your best next \
experiment rather than a spread) are candidate \
materials to simulate — each an atomistic \
`structure` (a periodic `cell` + `add_atom` ops with fractional coords, or a \
`slab` bulk-template op that builds a metal surface for you — see the reaction \
rules above if this is a catalyst quest). Only propose a candidate you can \
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


def _payload_from_result(res: Any) -> dict[str, Any] | None:
    """Prefer the router's parsed ``.data``; fall back to parsing ``.text``."""
    data = getattr(res, "data", None)
    if isinstance(data, dict) and data:
        return data
    return _extract_json(getattr(res, "text", "") or "")


#: Jaccard overlap of significant tokens above which two hypotheses are "the
#: same question restated" and the new one is dropped (the spin was ~10
#: rephrasings of one hypothesis).
_HYP_DUP_JACCARD = 0.6


def _sig_tokens(text: str) -> set[str]:
    """Lowercased word tokens ≥4 chars — a cheap topical fingerprint."""
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 4}


def _is_near_dup(text: str, existing: list[str]) -> bool:
    """True when ``text`` restates any of ``existing`` (token Jaccard ≥ floor)."""
    toks = _sig_tokens(text)
    if not toks:
        return False
    for other in existing:
        ot = _sig_tokens(other)
        if not ot:
            continue
        inter = len(toks & ot)
        union = len(toks | ot)
        if union and inter / union >= _HYP_DUP_JACCARD:
            return True
    return False


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
    ``dossier_markdown``, threaded straight through to
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
        "placement, coverage, co-adsorbate). Do not review, narrate, or "
        "lit-search this turn; propose a buildable `structure` (a `slab` op "
        "plus composition ops).\n"
    )
    return base + directive


#: Source tag on the commit ladder's own LlmRequest — distinct from the
#: primary tick's "quest_tick"/"quest_review" so per-quest spend is mineable
#: separately (mirrors the existing local-vs-review split, gr162130).
_COMMIT_SOURCE = "quest_tick_commit"


def _commit_reprompt_ladder(
    store: Store,
    quest: Ref,
    tier: Any,
    *,
    stall: int,
    disp: Callable[[Any], Any],
    by: str,
    base_prompt: str | None = None,
    narrative_override: str | None = None,
) -> tuple[tuple[Any, list[str]] | None, bool]:
    """At most 2 extra LLM calls: re-prompt at ``tier``, then one tier up.

    Returns ``(committed, any_transport_error)``:

    * ``committed`` is ``(ComputeStep, proposal_names)`` on a successful
      commit (the model proposed something, materialised/dispatched via the
      SAME :func:`precis.quest.compute.run_compute_step` path as any
      ordinary proposal — idempotent, content-addressed), or ``None`` when
      neither rung's response carried a usable ``proposals`` entry. Never
      fabricates a candidate itself.
    * ``any_transport_error`` is ``True`` when at least one rung came back
      with an LLM ``error``/``paused`` result (breaker/quota/transport
      trouble) rather than a genuine empty ``proposals`` — so the caller's
      back-off log can say "agent unreachable" instead of "agent declined"
      (this feature exists to diagnose stalls from the logbook, so that
      distinction is the whole point).

    The caller wraps this in a ``try/except`` — a raise here must never crash
    the tick. ``base_prompt`` (see :func:`_build_commit_prompt`) skips a
    redundant full-context rebuild when the primary tick already built the
    identical (``review=False``) prompt this call; ``narrative_override``
    (only meaningful on a rebuild) keeps that rebuild showing the model its
    own just-proposed narrative rather than last tick's persisted one.
    """
    from precis.quest.compute import run_compute_step
    from precis.utils.llm.router import LlmRequest, Tier

    prompt = _build_commit_prompt(
        store,
        quest,
        stall=stall,
        base_prompt=base_prompt,
        narrative_override=narrative_override,
    )
    quest_id = quest.id

    tiers = [tier]
    if tier != Tier.FRONTIER:
        tiers.append(Tier.FRONTIER)  # escalate once — the senior/review tier

    any_error = False
    for attempt_tier in tiers:
        res = disp(
            LlmRequest(
                tier=attempt_tier,
                prompt=prompt,
                source=_COMMIT_SOURCE,
                ref_id=quest_id,
            )
        )
        if getattr(res, "error", None) or getattr(res, "paused", False):
            any_error = True  # transient/breaker/quota trouble — try the next rung
            continue
        payload = _payload_from_result(res)
        proposals = [
            p for p in ((payload or {}).get("proposals") or []) if isinstance(p, dict)
        ]
        if not proposals:
            continue
        proposals = proposals[: max_proposals_per_tick()]  # WIP cap (one at a time)
        step = run_compute_step(store, quest_id, proposals, by=by)
        names = [str(p.get("name") or "?") for p in proposals]
        return (step, names), any_error
    return None, any_error


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
        "repetition, not conclusions. Respond with ONLY the compressed "
        "dossier markdown, nothing else.\n\n"
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
    if isinstance(data, dict) and str(data.get("dossier_markdown") or "").strip():
        return str(data["dossier_markdown"]).strip()
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
    """Run the growth-ratchet gate on a proposed `dossier_markdown`; return
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


# ── the tick ──────────────────────────────────────────────────────────


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
) -> QuestTickOutcome:
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
    """
    from precis.quest import cascade as cascade_mod
    from precis.utils.llm.router import Tier

    qref = store.get_ref(kind="quest", id=quest_id)
    if qref is None or qref.deleted_at is not None:
        return QuestTickOutcome(quest_id, "failed", 0, False, None, "quest not found")

    # Cascade: decide local vs. frontier review (unless the caller forces it).
    signal = cascade_mod.escalation_signal(store, quest_id)
    is_review = signal.escalate if review is None else review
    reason = (
        signal.reason
        if (review is None and is_review)
        else ("forced" if is_review else "")
    )
    if tier is not None:
        resolved_tier = _resolve_tier(tier)
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
    # the tick.
    from precis import agentlog

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
            job_ref_id=job_ref_id,
        )
    except Exception:
        log.warning("run_quest_tick: failed to open agentlog", exc_info=True)

    def _finalize(
        outcome: QuestTickOutcome, *, partial: str | None = None
    ) -> QuestTickOutcome:
        if agentlog_id is not None:
            try:
                agentlog.finalize_log(
                    store,
                    log_id=agentlog_id,
                    status=outcome.status,
                    # Partial output salvaged from a mid-generation abort
                    # (a streamed rung's StreamTimeout) — persisted so the
                    # reasoning isn't lost with the connection; the
                    # /agentlogs viewer shows it next to the prompt.
                    result=partial or None,
                    # Non-succeeded ticks stamp WHY (the outcome note) —
                    # a bare status="failed" row is undiagnosable.
                    meta_extra=(
                        {"error": outcome.note}
                        if outcome.status != "succeeded" and outcome.note
                        else None
                    ),
                )
            except Exception:
                log.warning(
                    "run_quest_tick: failed to finalize agentlog", exc_info=True
                )
        return outcome

    from precis.utils.llm.router import LlmRequest
    from precis.utils.llm.router import dispatch as _dispatch

    disp = dispatch_fn if dispatch_fn is not None else _dispatch
    # Attribute the call to *this* quest (llm_call_log.ref_id) and split the lane
    # in `source` so per-quest, local-vs-review spend is mineable from the log —
    # neither is back-fillable, so it must be stamped at dispatch (gr162130).
    res = disp(
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
            stream=True,
        )
    )
    cost = getattr(res, "cost_usd", None)
    if getattr(res, "error", None):
        # Whatever the model produced before the failure (a streamed rung's
        # partial reasoning/content rides LlmResult.text alongside the error)
        # is persisted to the agentlog rather than lost with the connection.
        salvage = _cap_partial((getattr(res, "text", "") or "").strip())
        # A window-scoped breaker trip (dollar cap / claude-OAuth quota) is a
        # pause, not a failure: report "paused" so the allocator skips it (no
        # pick recorded, no panel "failed") and re-picks once the window clears —
        # instead of burning a tick + a FAILED-PASSES row every worker cycle.
        if getattr(res, "paused", False):
            return _finalize(
                QuestTickOutcome(
                    quest_id, "paused", 0, False, cost, f"paused: {res.error}"
                ),
                partial=salvage,
            )
        return _finalize(
            QuestTickOutcome(
                quest_id, "failed", 0, False, cost, f"llm error: {res.error}"
            ),
            partial=salvage,
        )

    payload = _payload_from_result(res)
    if payload is None:
        # The model completed cleanly but its text didn't parse as a tick
        # action — persist the raw text so the malformed output can be
        # inspected instead of vanishing with the failure (glm-5.2 has
        # returned prose/near-empty answers here; without the text the
        # failure mode is invisible).
        return _finalize(
            QuestTickOutcome(
                quest_id, "failed", 0, False, cost, "unparseable model output"
            ),
            partial=_cap_partial((getattr(res, "text", "") or "").strip()),
        )

    # Open hypotheses to dedup fresh ones against — a spin is the same question
    # restated, so a near-duplicate `hypothesis` is dropped rather than appended.
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
        append_entry(store, quest_id, text=text, entry_type=etype, by=by, cost=cost_val)
        if etype == "hypothesis":
            open_hyps.append(text)
        added += 1

    # Ledger — pin any tried/ruled-out/open *directions* the model wants to
    # survive the whole-rewrite below. Applied BEFORE `rewrite_dossier` so a
    # same-tick rule-out is pinned even if the fresh narrative drops it
    # (dossier-owned-by-process — the structural fix for the autocatpath dead-3-days spin).
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

    # The proposed narrative rewrite — captured here, but NOT yet gated or
    # written: the growth-ratchet gate (dossier-hygiene design) needs this
    # tick's full "progress" fact (the ledger ops already applied above, plus
    # any harvest/paper-link the compute+search steps below still produce),
    # so the gate+write is deferred to just before `update_cascade_state`.
    narrative_md = str(payload.get("dossier_markdown") or "").strip()
    rewritten = False

    # Proposals — log each candidate as a hypothesis (WORM), then optionally
    # materialise + dispatch them as `structure` sims (rung 4b).
    proposals = [p for p in (payload.get("proposals") or []) if isinstance(p, dict)]
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
    if is_review:
        directions = [
            str(d).strip() for d in (payload.get("directions") or []) if str(d).strip()
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

    # Lit-search — go ground the quest in the literature (the missing half of
    # the loop). Runs in the same acting mode as compute; linking a paper is
    # external progress, so it must land BEFORE update_cascade_state resets the
    # stall clock. Injectable search seam lives in run_quest_tick's `search_fn`.
    searches_run = papers_linked = 0
    if compute:
        from precis.quest.search import run_search_step

        queries = [
            str(q).strip() for q in (payload.get("searches") or []) if str(q).strip()
        ]
        if queries:
            sstep = run_search_step(
                store, quest_id, queries, by=by, search_fn=search_fn
            )
            searches_run = sstep.queries_run
            papers_linked = sstep.papers_linked
            added += sstep.queries_run

    created = dispatched = harvested = ruled = graduated = 0
    proposals_committed = 0  # extra proposals the commit ladder got dispatched
    if compute:
        from precis.quest.compute import run_compute_step

        # A raise here must never crash the tick (mirrors _phase_weave_tick and
        # the commit-ladder try/except). run_compute_step's dispatch lane
        # (dispatch_autocatpath) raises loudly on the gr172886 no-GPU null-route
        # misconfiguration; without this guard that RuntimeError propagates out
        # of run_quest_tick to the coordinator's blanket except, which
        # terminalizes the WHOLE coordinator job and loses the mid-slice
        # checkpoint. Log it loudly (the misconfig stays visible) and degrade to
        # a zero-dispatch outcome — the stall counter then advances and the
        # escalation ladder handles the persistent failure.
        # WIP cap — dispatch at most max_proposals_per_tick() (default 1);
        # the rest were already logged as `hypothesis` leads above.
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
            created = step.candidates_created
            dispatched = step.sims_dispatched
            harvested = step.results_harvested
            ruled = step.ruled_out
            graduated = step.graduated

        # Commit re-prompt + tier-escalation ladder: a structural guarantee
        # that the AGENT is asked to act — never a code-chosen dispatch. A
        # model tick that dispatched a real sim resets the stall counter;
        # one that dispatched nothing advances it, and once it reaches
        # PRECIS_QUEST_FORCE_EXPERIMENT_EVERY consecutive dry ticks (default
        # 2) the tick fires the commit ladder (see above): re-prompt at the
        # current tier, then one tier up, each asking the model to propose a
        # composition using its own judgment. A raise anywhere in the ladder
        # must never crash the tick (mirrors the weave-tick try/except
        # convention in workers/job_types/quest_tick.py's
        # _phase_weave_tick) — degrade to a normal backed-off outcome, and
        # ALWAYS stamp the counter before returning either way.
        prev_stall = int((qref.meta or {}).get("ticks_since_experiment", 0) or 0)
        if dispatched > 0:
            stall = 0
        else:
            stall = prev_stall + 1
            force_every = int(
                os.environ.get("PRECIS_QUEST_FORCE_EXPERIMENT_EVERY", "2")
            )
            if stall >= force_every:
                try:
                    # Reuse the primary tick's already-built prompt when it
                    # was built in propose mode (review=False) — byte-
                    # identical to what the ladder would otherwise rebuild
                    # from scratch (another frontier + live-candidate scan).
                    # A review tick's prompt carries the review banner, so
                    # the ladder must rebuild its own propose-mode one. And if
                    # this same tick just created a candidate (created > 0),
                    # the cached prompt predates it — rebuild so the re-prompt's
                    # tried-set/frontier reflects the new candidate.
                    reuse_prompt = not is_review and created == 0
                    committed, ladder_had_error = _commit_reprompt_ladder(
                        store,
                        qref,
                        resolved_tier,
                        stall=stall,
                        disp=disp,
                        by=by,
                        base_prompt=prompt if reuse_prompt else None,
                        # The narrative write is deferred past this ladder
                        # (the growth-ratchet gate needs the ladder's own
                        # harvest as progress evidence) — a rebuild must see
                        # THIS tick's just-proposed narrative, not last
                        # tick's persisted one. Irrelevant when reuse_prompt
                        # is True (no rebuild happens).
                        narrative_override=narrative_md or None,
                    )
                except Exception as exc:  # defensive — a ladder bug must not
                    # crash the tick; see the docstring above.
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
                    added += 1
                else:
                    if committed is not None:
                        fstep, names = committed
                        created += fstep.candidates_created
                        dispatched += fstep.sims_dispatched
                        harvested += fstep.results_harvested
                        ruled += fstep.ruled_out
                        graduated += fstep.graduated
                        if fstep.sims_dispatched > 0:
                            proposals_committed += len(names)
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
                            added += 1
                            stall = 0
                        else:
                            # The ladder returned a proposal but nothing reached
                            # simulation (e.g. a candidate materialised without
                            # wiring/dispatch — gr201814). Report the truth,
                            # carrying the step notes, so the logbook is
                            # self-diagnosing rather than claiming a commit that
                            # never happened — and leave ``stall`` advanced so
                            # the next tick keeps pressing.
                            detail = (
                                "; ".join(n for n in fstep.notes if n) or "no dispatch"
                            )
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
                            added += 1
                    elif ladder_had_error:
                        # LLM transport/breaker/quota trouble, not a genuine
                        # decline — distinct from the branch below so the
                        # logbook (the whole point of this ladder) tells the
                        # two apart.
                        append_entry(
                            store,
                            quest_id,
                            text=(
                                "agent unreachable (LLM error/paused) — "
                                "backing off, will retry"
                            ),
                            entry_type="decision",
                            by=by,
                        )
                        added += 1
                    else:
                        append_entry(
                            store,
                            quest_id,
                            text=(
                                "agent declined to propose an untried variant "
                                "after commit re-prompt + tier escalation"
                            ),
                            entry_type="decision",
                            by=by,
                        )
                        added += 1
        store.stamp_ref_meta(quest_id, {"ticks_since_experiment": stall})

        # Regenerate the pinned frontier-tree dossier chunk (Slice 4c-4) now
        # that harvest (above) has landed this tick's measures — a code-only
        # rewrite, never surfaced to the model as something to author.
        # Defensive: a render bug must not crash the tick (mirrors the
        # commit-ladder / compute-step try/except convention above).
        try:
            from precis.quest.dossier import update_frontier_tree

            update_frontier_tree(store, quest_id)
        except Exception:
            log.exception(
                "run_quest_tick: frontier-tree regen failed for quest %s", quest_id
            )

    # Narrative growth-ratchet gate (dossier-hygiene design) — deferred to
    # HERE (see the capture point above) so `progress_evidence` reflects this
    # tick's FULL outcome: an applied ledger op, a harvest (`frontier
    # update`), or a linked paper (this loop's stand-in for "citation mint" —
    # `precis.quest.citation_mint` itself is the paper-writing weave's own
    # minter, not called from here). The ledger tree is never gated — see
    # the module banner above. Defensive: a gate/rewrite bug must not crash
    # the tick (mirrors the frontier-tree regen / commit-ladder / compute-step
    # try/except convention above) — on a raise, the previous narrative
    # simply survives untouched.
    if narrative_md:
        try:
            progress_evidence = (
                bool(ledger_added) or bool(harvested) or bool(papers_linked)
            )
            accepted_md = _apply_narrative_gate(
                store,
                qref,
                quest_id,
                narrative_md,
                progress_evidence=progress_evidence,
                tier=resolved_tier,
                disp=disp,
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
    note = (
        (f"frontier-review ({reason})" if is_review else "ok") if did_work else "no-op"
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
    # deed is written — a paused/errored tick returns early above, so this
    # only runs on success.
    resp_text = getattr(res, "text", "") or ""
    chars = len(prompt) + len(resp_text)
    cost_val = float(cost) if isinstance(cost, (int, float)) and cost > 0 else None
    cost_note = f" (${cost_val:.4f})" if cost_val is not None else ""
    append_entry(
        store,
        quest_id,
        text=f"tick spend {chars:,} chars{cost_note} ({'review' if is_review else 'local'})",
        entry_type="cost",
        by=by,
        cost=cost_val,
        chars=chars,
    )
    return _finalize(
        QuestTickOutcome(
            quest_id,
            "succeeded",
            added,
            rewritten,
            cost,
            note,
            proposals=len(proposals) + proposals_committed,
            candidates_created=created,
            sims_dispatched=dispatched,
            results_harvested=harvested,
            ruled_out=ruled,
            graduated=graduated,
            searches_run=searches_run,
            papers_linked=papers_linked,
            hypotheses_deduped=deduped,
            ledger_added=ledger_added,
            escalated=is_review,
            mode="frontier-review" if is_review else "local",
        )
    )


__all__ = [
    "QUEST_LOOP_ENABLED_ENV",
    "QuestTickOutcome",
    "build_tick_prompt",
    "quest_loop_enabled",
    "run_quest_tick",
]

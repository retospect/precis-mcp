"""quest_tick — one bounded step of a quest's autonomous research loop.

Slice 4a of the quest layer (docs/proposals/quest-layer.md §The autonomous
research loop). This is the **skeleton** of the loop: a single, in-process,
structured LLM step routed through the ADR-0046 seam (``dispatch(LlmRequest)``)
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
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.quest import dossier as dossier_mod
from precis.quest import gaps as gaps_mod
from precis.quest.logbook import (
    ENTRY_TYPES,
    LOG_KIND,
    append_entry,
    clamp_entry_type,
)

if TYPE_CHECKING:
    from precis.store import Ref, Store

QUEST_LOOP_ENABLED_ENV = "PRECIS_QUEST_LOOP_ENABLED"

#: How many trailing logbook entries to feed the tick as episodic context.
_LOGBOOK_TAIL = 8
#: A frontier review steps back over accumulated history — a deeper tail than
#: a cheap local tick.
_LOGBOOK_TAIL_REVIEW = 20


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


def _served_papers_detail(store: Store, quest_id: int) -> list[str]:
    """One line per served `paper`: a short title + its abstract snippet."""
    live = gaps_mod._live_servers(store, quest_id)
    papers = [r for r in live if r.kind == "paper"][:_MAX_DETAIL_PAPERS]
    out: list[str] = []
    for r in papers:
        title = (r.title or "").splitlines()[0][:80] if r.title else "(untitled)"
        out.append(f"- {title} — {_paper_abstract_snippet(store, r)}")
    return out


def _literature_section(store: Store, quest_id: int) -> str:
    """The ``## Held literature (abstracts)`` section, or ``""`` when none."""
    detail = _served_papers_detail(store, quest_id)
    if not detail:
        return ""
    return "\n## Held literature (abstracts)\n" + "\n".join(detail) + "\n"


def _frontier_summary(store: Store, quest_id: int) -> str:
    """A compact rendering of the Pareto frontier for a review prompt."""
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
    return "\n".join(lines)


def _reaction_context(quest: Ref) -> str:
    """Proposal rules for a **barrier quest** that declares a reaction (catpath).

    When the quest carries ``meta.reaction_config`` every candidate is a *catalyst
    slab* — catpath places the reactants and measures the rate-limiting barrier,
    so a proposal builds the reaction's slab with the compact ``slab`` op and
    varies only its surface composition (the ``meta.param_space`` design knobs).
    Absent → ``""`` (a generic materials quest keeps the free-form,
    hand-built-cell proposal rules already in the template).
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
    knob_bits: list[str] = []
    for name, spec in (meta.get("param_space") or {}).items():
        if isinstance(spec, dict) and spec.get("choices"):
            knob_bits.append(f"{name} ∈ {{{', '.join(map(str, spec['choices']))}}}")
        elif isinstance(spec, dict) and "low" in spec:
            knob_bits.append(f"{name} ∈ [{spec['low']}..{spec.get('high', '?')}]")
        else:
            knob_bits.append(str(name))
    knobs = "; ".join(knob_bits) or "surface composition"
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
    op_menu = (
        "\nComposition ops you can use on the slab (the slab op labels atoms "
        f"{label}1..N in ascending-z order — the TOP surface layer is the "
        "highest-numbered labels):\n"
        "- add_atom  — an adatom ON the surface (what you've used): "
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
    return (
        "\n## Reaction R — this is a catalyst-barrier quest\n"
        f"Every candidate is a **catalyst slab**. catpath places the reactants "
        f"(**{sub} → {tgt}** via the `{net}` network) on *your* slab and measures "
        f"the rate-limiting **barrier** (eV, an ML-potential NEB); a relax measures "
        f"the slab's **stability** (`energy`). You design the **surface**, NOT the "
        f"adsorbate — so vary only its composition: {knobs}.\n\n"
        f"Build the slab with the compact `slab` op (do NOT hand-enumerate the {el} "
        f"atoms — the op builds the fcc(111) geometry ASE-exact so catpath can "
        f"inject it), then edit composition. Omit the top-level `cell` (the `slab` "
        f"op provides it).\n"
        f"- reference point (propose this verbatim first): `{base}`\n"
        f"- a doped variant (an adatom is the design knob): `{doped}`\n"
        f"{op_menu}"
    )


def build_tick_prompt(store: Store, quest: Ref, *, review: bool = False) -> str:
    """Assemble the full rolling-context prompt for one tick.

    ``review=True`` builds the **frontier-review** prompt (rung 4c): the senior
    model reviews the accumulated evidence + the Pareto frontier and sets the
    next strategic directions, rather than doing one more local increment.
    """
    qid = quest.id
    stmt = quest.title or f"quest {qid}"
    prio = quest.prio if quest.prio is not None else "unset"
    _did, _h, dossier_text = dossier_mod.read_dossier(store, qid)
    gaps = gaps_mod.quest_gaps(store, qid)
    momentum = gaps_mod.quest_momentum(store, qid)

    gap_lines = [f"- {g.kind}: {g.detail}" for g in gaps] or ["- (none)"]
    tail = _logbook_tail(
        store, qid, n=_LOGBOOK_TAIL_REVIEW if review else _LOGBOOK_TAIL
    ) or ["- (no logbook entries yet)"]
    servers = _servers_summary(store, qid) or ["- (nothing serves this quest yet)"]
    # Always-on measurement table (rung 4c's review banner used to be the only
    # place this rendered; the local tick reasons from the same numbers now).
    frontier_text = _frontier_summary(store, qid)
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
        gaps="\n".join(gap_lines),
        logbook="\n".join(tail),
        servers="\n".join(servers),
        frontier=frontier_text,
        literature=literature,
        reaction_context=_reaction_context(quest),
        entry_types=", ".join(sorted(ENTRY_TYPES)),
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
dossier.)
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

Respond with EXACTLY ONE JSON object and nothing else:
{{
  "logbook": [
    {{"entry_type": "<one of: {entry_types}>", "text": "<one concise entry>"}}
  ],
  "searches": ["<0–3 literature queries to ground this quest — papers found are \
linked as servers and feed the next step>"],
  "dossier_markdown": "<the FULL rewritten dossier in markdown: current \
understanding, best leads so far, what's ruled out, open questions>",
  "proposals": [
    {{"name": "<candidate material>", "rationale": "<why test it>",
      "structure": {{"cell": {{"a": 8.4, "b": 8.4, "c": 24.0, \
"pbc": [true, true, false]}},
        "ops": [{{"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.5]}}]}}}}
  ],
  "directions": ["<0–3 strategic directions — set these on a frontier review>"]
}}

Give 1–4 logbook entries. A `hypothesis` you'd test, an `observation` from the \
state, a `result` or `dead-end` that *closes* an open hypothesis, or a \
`decision` on direction are the most useful. Keep the dossier tight.

`proposals` (0–3) are candidate materials to simulate — each an atomistic \
`structure` (a periodic `cell` + `add_atom` ops with fractional coords, or a \
`slab` bulk-template op that builds a metal surface for you — see the reaction \
rules above if this is a catalyst quest). Only propose a candidate you can \
express as a concrete structure and that is NOT already ruled out; omit \
`structure` if you cannot, and it will be recorded as a lead but not simulated. \
Propose nothing if the next step is analysis, not a new material."""


# ── model call + parsing ──────────────────────────────────────────────


def _resolve_tier(tier: Any) -> Any:
    from precis.utils.llm.router import Tier

    if tier is None:
        return Tier.CLOUD_SMALL
    if isinstance(tier, Tier):
        return tier
    return Tier(str(tier))


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
    import re

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
) -> QuestTickOutcome:
    """Run one structured research step against ``quest_id``.

    ``dispatch_fn`` is injectable (defaults to the real router ``dispatch``) so
    the tick is unit-testable with a canned ``LlmResult``. ``compute=True`` (rung
    4b) materialises the model's proposals into candidate `structure` servers,
    dispatches their relax sims, and harvests results. ``review`` (rung 4c) forces
    the tier: ``None`` (default) lets the escalation signal decide — a local tick
    unless enough evidence / a stall triggers a **frontier review** at the senior
    tier; ``True``/``False`` overrides it. An explicit ``tier`` wins over both.
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
        resolved_tier = Tier.CLOUD_SUPER
    else:
        resolved_tier = Tier.CLOUD_SMALL

    prompt = build_tick_prompt(store, qref, review=is_review)

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
        )
    )
    cost = getattr(res, "cost_usd", None)
    if getattr(res, "error", None):
        # A window-scoped breaker trip (dollar cap / claude-OAuth quota) is a
        # pause, not a failure: report "paused" so the allocator skips it (no
        # pick recorded, no panel "failed") and re-picks once the window clears —
        # instead of burning a tick + a FAILED-PASSES row every worker cycle.
        if getattr(res, "paused", False):
            return QuestTickOutcome(
                quest_id, "paused", 0, False, cost, f"paused: {res.error}"
            )
        return QuestTickOutcome(
            quest_id, "failed", 0, False, cost, f"llm error: {res.error}"
        )

    payload = _payload_from_result(res)
    if payload is None:
        return QuestTickOutcome(
            quest_id, "failed", 0, False, cost, "unparseable model output"
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
        if etype == "hypothesis" and _is_near_dup(text, open_hyps):
            deduped += 1
            continue
        raw_cost = e.get("cost")
        cost_val = float(raw_cost) if isinstance(raw_cost, (int, float)) else None
        append_entry(store, quest_id, text=text, entry_type=etype, by=by, cost=cost_val)
        if etype == "hypothesis":
            open_hyps.append(text)
        added += 1

    # Rewrite the dossier (the rolling context) if the model produced one.
    md = str(payload.get("dossier_markdown") or "").strip()
    rewritten = False
    if md:
        dossier_mod.rewrite_dossier(store, quest_id, md)
        rewritten = True

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
    if compute:
        from precis.quest.compute import run_compute_step

        step = run_compute_step(store, quest_id, proposals, by=by)
        created = step.candidates_created
        dispatched = step.sims_dispatched
        harvested = step.results_harvested
        ruled = step.ruled_out
        graduated = step.graduated

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
    # Quest ticks run on the claude_p transport at CLOUD_SMALL, where
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
    return QuestTickOutcome(
        quest_id,
        "succeeded",
        added,
        rewritten,
        cost,
        note,
        proposals=len(proposals),
        candidates_created=created,
        sims_dispatched=dispatched,
        results_harvested=harvested,
        ruled_out=ruled,
        graduated=graduated,
        searches_run=searches_run,
        papers_linked=papers_linked,
        hypotheses_deduped=deduped,
        escalated=is_review,
        mode="frontier-review" if is_review else "local",
    )


__all__ = [
    "QUEST_LOOP_ENABLED_ENV",
    "QuestTickOutcome",
    "build_tick_prompt",
    "quest_loop_enabled",
    "run_quest_tick",
]

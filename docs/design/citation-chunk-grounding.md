# Design — cross-paper citation-chunk grounding

> Status: design captured 2026-07-23, revised 2026-07-23 after reading the
> actual `workers/chase.py`/`workers/_chase_llm.py` implementation (the first
> pass of this doc under-estimated what already exists — corrected below).
> Grew out of `docs/design/graph-based.md` and ADR 0054
> (`docs/decisions/0054-argument-graph-lemmas-inferences-reasoning-shadow.md`).
>
> **Built 2026-07-23** (this worktree, unshipped — no `/land` yet): paper
> link-blindness fix (`handlers/_links_render.py` + `view='links'` on every
> flagged `Handler`-direct kind); inbound chase
> (`workers/inbound_chase.py`, dark behind `PRECIS_INBOUND_CHASE_ENABLED`);
> the Part 3 sidecar render (`handlers/_citer_sidecar.py`, same flag). See
> each module's own docstring for the implementation-discretion calls this
> doc left open (trigger event, staleness/backoff, tiebreak fallback).
>
> **Deepened 2026-07-23 (second pass, same worktree):** `_resolve_citer_chunk`
> now runs a *second* `_locate_chunk_in_target` pass into the cited paper Y's
> own chunks (lexical-overlap proposal over Y's chunks, then the same LLM
> confirm hook), so a chunk-scoped `cites` link's `dst_pos` gets populated
> whenever that pass finds a confident match in Y — no forced guess when it
> doesn't. This closes the gap both `inbound_chase.py` and
> `_citer_sidecar.py` used to flag as future work: the sidecar now has a
> symmetric inbound render (`render_cited_by_sidecar`, `direction='in'`
> filtered on `dst_pos`) wired into `PaperHandler._render_chunks` alongside
> the outbound one. Cost note: this roughly doubles per-citer LLM *locate*
> calls (see `inbound_chase.py`'s cost-caveat paragraph) — still gated
> behind the same dark flag, still leaning on the unshipped spend
> circuit-breaker as its backstop.

## The gap this closes

When paper/draft chunk X cites paper Y, precis knows the *paper-level* edge
but generally not which chunk of Y actually supports the claim, whether that
support is full/partial/absent, and has no way to walk the *inverse*
direction (who cites Y, at which chunk) at all. The corpus is flatter than it
should be.

## Two distinct relation types — keep them separate

1. **Structural, citation-graph-confirmed.** S2 (or an inline `[12]`-style
   marker) says X cites Y — chase down to the specific chunk, verify support,
   record a terse verdict + caveats. This is evidential; it belongs on the
   `cites`/`cited-by` relation (closed vocabulary, ADR-0054-disciplined).
2. **General content similarity — "close but doesn't cite."** Topically
   related, found by search, *not* a real citation. This must **not** ride on
   `cites` — mixing a confirmed citation with a similarity guess on the same
   relation corrupts the one guarantee the evidential graph needs (which
   edges are citation-backed). Use `related-to` + `meta.note` (the existing
   "free-form edge nuance" pattern from ADR 0054) instead. Not built.

## What already exists for type 1 (corrected 2026-07-23 — read the code, not the skill docs)

The `finding`-chase worker (`workers/chase.py` + `workers/_chase_llm.py`)
already implements almost the entire outbound half:

| Need | Already built as |
|---|---|
| Chunk-scoped citation link | `link(target='pc38', rel='cites')` — works today; inverse `cited-by` auto-resolves. |
| Locate the specific chunk in a *known* target paper | `_locate_chunk_in_target` — a lexical-overlap ranker proposes a chunk, an LLM hook (`Tier.CLOUD_SMALL`) confirms or picks a shown alternate. Answers last round's open question: chase **is** scoped to a known paper, not an open search. |
| Terse support verdict | `_verify_support_with_caveats` → `{supports: yes/partial/no, support_reason, caveats[], cited_others[], terminal}`. Exactly the "well supported / partial / author's claim isn't grounded" shape asked for. |
| Multi-hop, following inline cites | `_pick_next_hop`/`_load_s2_references`/`_ref_to_target`, numbered-citation resolution against the S2 reference list, hop after hop. |
| Multi-cite disambiguation | `_disambiguate_candidates`. |
| "Queue missing papers, don't wait" | `_resolve_or_create_stub` — mints a stub `paper` ref (`meta={'set_by':'chase'}`) the instant the chase reaches an unheld target, continues without blocking. `paper_reconcile.py` already has stub-upgrade awareness. |
| Cost | ~$0.05-0.10/established finding, `Tier.CLOUD_SMALL` (Haiku-class) — already cheap. |

**Gating**: all of the above LLM verification is behind `PRECIS_CHASE_LLM`
(default `"0"`). The deterministic (no-verdict) chase runs today; the rich
verify/caveat/disambiguate path is opt-in and currently off.

## What's actually missing

1. **Inbound direction isn't wired.** `_load_s2_references` only fetches what
   the source paper *references* (outbound). Nothing in `chase.py` walks S2's
   *citations* list (who cites this paper) — that's only exposed as the
   standalone read verb `get(kind='semanticscholar', id='cites:<paper>')`,
   disconnected from the verify/locate/chunk machinery above. Building this
   is the real remaining "type 1" work — wire an inbound counterpart to
   `_pick_next_hop`/`_load_s2_references` that walks `cites:` instead of
   `refs:`, then reuses the *same* `_locate_chunk_in_target` +
   `_verify_support_with_caveats` hooks against the citing paper's chunks.
2. **`PRECIS_CHASE_LLM` cost/policy decision.** Turning verification on
   broadly enough that an agent gets verdicts "for free" while reading
   changes the standing cost profile — a real decision, not a flag flip.
   Recommend scoping any "on" decision to the same locality-bounded
   opportunistic-drain policy as before (active paper/session only), not a
   global flip.
3. **No presentation surface** (see below — this is where the "toon table vs.
   inline" question lives). Chase output lands on the `finding`'s chain /
   `meta.caveats`; nothing renders it when an agent is just reading paper Y
   directly, unprompted.
4. **Type 2 (general-content similarity) doesn't exist at all.** New feature,
   `related-to` + `meta.note`, separate scope from type 1.
5. **Paper link-blindness** (unchanged from the first pass of this doc, still
   blocking): `PaperHandler` has no links view (`OPEN-ITEMS.md` 🕸️ audit
   item 1) — build first, both types of relation are invisible without it.

## Presentation — the fidelity-ladder answer, not a table or inline breaks

The open design question (2026-07-23 discussion): given pc123 is (a) a
confirmed grounding target from paper X, (b) itself cites paper A with its
own verdict, and (c) has 3 topically-close-but-uncited neighbors — how does
an agent see all that without either an unbounded relation dump or breaking
paragraph flow?

**Don't invent a new rendering mode.** `docs/proposals/turn-routing-and-context-dsl.md`
already designs exactly this shape — a fidelity ladder (`full/summary/keywords/drop`)
and `view='fisheye'`, documented as returning "a **neighborhood**, never a
bare chunk," fidelity auto-decaying by distance from the eye. Compose it from
pieces that already exist:

- **The eye chunk (pc123)** — verbatim, full fidelity, in-flow. Unchanged.
- **Its immediate paragraph/section** — high fidelity, unchanged (this is
  what protects reading flow — don't interrupt it).
- **The relation neighborhood** — a **capped sidecar list**, structurally
  attached the way `links_for`'s one-hop `Links:` section attaches to a ref
  today (F8), just one level down at chunk granularity: one line per edge —
  relation kind + terse verdict + target identity at `card_combined`
  fidelity (title/author/year — already an existing embedded card, `ord=-1`)
  or `view='summaries'` fidelity (per-chunk gloss) for anything the agent
  wants to go one level deeper on. Bounded count. Each entry expandable via
  `get(handle)` — never eagerly expanded.
- Terse "paper view for the big LLM to think on" (title/author/abstract) —
  **already exists**: `card_combined` (embedded title+authors+abstract+
  keywords card) and `view='summaries'` (per-chunk gloss for a whole body)
  are both live features, just not yet composed into a citer/cited render.

**Real sequencing risk to flag, not resolve here**: this reuses a proposal
that's `deferred — design captured, not sliced` in `OPEN-ITEMS.md`. Building
the citer/cited sidecar means either slicing a first piece of that DSL now,
or building a narrower one-off render that duplicates its shape and diverges
later. Decide explicitly which, don't drift into it.

## Execution model — registration inline, standing pass does the work (resolved 2026-07-23)

Neither "agentic todos" nor a synchronous/blocking MCP call. `chase` is
already registered in `workers/registry.py` as a **standing recurring worker
pass** (same category as `embed`/`classify`/`card_forge`/`corpus_reconcile`),
not a todo and not a per-instance dispatched job — reuse that pattern rather
than inventing a new one:

- **Todos are the wrong abstraction.** They're for judgment-requiring work
  routed through the planner (`LLM:`-tagged → `plan_tick`). A citation chase
  is mechanical prep (see the graph-locality "mechanical prep vs actual work"
  distinction, held-off item above) — one todo per citation would flood the
  todo tree with bookkeeping the planner isn't meant to prioritize.
- **Trigger — inline, cheap, at read/fisheye-render time.** When the render
  path hits a chunk with an inline `[N]`-citation marker lacking a
  corresponding chunk-scoped `cites` link, fire a `put(kind='finding',
  cited_in=<chunk>, ...)` — a few ms, non-blocking, safe on every read
  because `finding` already dedups on `(body, scope, cited_in)`.
- **Job granularity: one `finding` per (citing chunk, cited paper) pair** —
  not per paragraph, not per paper. Matches the existing dedup key exactly.
- **Execution: the existing `chase` pass** drains registrations hop-by-hop on
  its own cadence — already built, already scheduled.
- **Inbound is categorically different, never a sweep.** "Who cites X" is one
  external S2 lookup scoped to a whole paper, expanded one hop further only
  when the agent opens a specific citer — purely interactive/lazy, no job to
  schedule until requested.
- **Optional, not v1 — and already half-built, don't reinvent it:**
  `dream_agent.py::_recent_draft_cited_paper_ids` already draws papers cited
  by recently-active drafts into the dream fisheye (documented payoff in
  `docs/design/dreaming.md`: catch a paragraph that drifted from its own
  cited evidence). That's the "keep this draft's citation graph fresh"
  hook, already wired to the dream's recency scoring — it currently only
  *notices* drift, doesn't *trigger* grounding. Natural extension later: when
  the dream fisheye lands on one of these papers and finds an unresolved
  chunk citation, fire the same inline `finding` registration as any other
  reader would. Don't build a separate `level:recurring` watch for this.

## Stub-arrival ("ungotten paper") handling (resolved 2026-07-23)

- **For a finding's own chase chain: already solved, no new mechanism.**
  `chase.py` tracks a `waiting` outcome ("frontier stub still has no chunks")
  with exponential backoff (`base × 2^(waits-1)`, capped 24h, reset to base on
  any progress) — hardened after a real spin-loop incident (memory
  `bg_job_spin_loops`, fixed). A finding blocked on a stub notices the stub
  landing within the backoff window on its own. Do not add a tag/hook for
  this case.
- **For downstream work that isn't the finding itself** (a draft/linkage task
  that needs to know a specific stub landed) — also an existing generic
  primitive, just not wired to stub creation: `workers/auto_check_evaluators/
  paper_ingested.py`, a todo `meta.auto_check={'type':'paper_ingested',
  'doi':...}` that resolves `True` only once the paper is live *and* has an
  embedded chunk. The standing `auto_check` pass (~60s poll) flips it to
  `STATUS:done`. **The actual gap**: `_resolve_or_create_stub` doesn't mint
  this follow-up today. Fix: on demand, when a *specific* downstream consumer
  needs to know (not automatically for every stub — most stubs need no active
  follow-up beyond their own chase's backoff), mint a child todo under that
  consumer carrying the `paper_ingested` spec + enough payload in the body to
  resume the right work.

## Inbound sweep policy (resolved 2026-07-23, second pass)

Not per-chunk-lazy, not corpus-wide-eager: **per-paper, triggered once by
first activation, permanent thereafter.**

- **"Active" = a permanent DB marker, not a session heuristic.** The first
  time a paper is engaged with in a way that matters (read/cited/chase
  target), trigger the inbound sweep once and mark it done/tracked. It does
  **not** expire — a paper stays in the tracked set forever, "waiting
  patiently in the DB" (same posture as chase's own `waiting`-backoff state)
  until its inbound graph is as resolved as it'll get. No time-boxed
  "session-active" heuristic needed.
- **No degree cap on the chase itself — cap only at display time.** Chase
  cost is cheap and one-time per paper (~$0.05-0.10/citer, `Tier.CLOUD_SMALL`,
  and staleness is handled incrementally per new citer, not by re-sweeping —
  see below), while context is the reader's genuinely scarce resource. So:
  chase every corpus-intersecting citer exhaustively in the background:
  storage is not scarce. The render picks the best few (draft: filter to
  `supports ∈ {yes, partial}` — a `no` verdict usually isn't worth surfacing
  at all — sort by verdict quality then a secondary signal such as the
  citer's own citation count, cap at ~5, "N more" expandable on request).
  **Caveat, not a reason to abandon this**: the natural cost backstop for an
  uncapped exhaustive chase is the global spend circuit breaker
  (`OPEN-ITEMS.md` "💰 Budget guardrails" Piece B) — which is **implemented,
  green, but unshipped**, not live in prod today. Until it ships, an
  outlier landmark paper (unusually high in-corpus citation count) has no
  automatic cost backstop beyond manual observation. Either ship/enable
  Piece B alongside this, or add a lightweight interim per-paper cost/count
  log (soft warn, not hard-block) until it lands. Don't build a bespoke
  chase-side cap — that's solving a problem the repo already has a general
  answer for, just not deployed yet.
- **Staleness — link immediately at whatever granularity is known, refine
  later, no re-sweep.** The moment a citer is discovered (even before it's
  fully ingested — could be a bare S2 record), link the citing paragraph to
  the (possibly-stub) target paper right away — don't wait for chunk
  resolution to record the paper-level fact. Then, exactly as already
  designed for the outbound direction: when the stub gets real chunks (via
  chase's own `waiting`-backoff for chains that reach it, or the
  `auto_check`/`paper_ingested` wiring for any other consumer that
  registered interest), that's what triggers the chunk-level resolution
  pass — never a full re-sweep of the paper.
- **One hop only, confirmed unchanged.** The exhaustive sweep applies to
  paper Y's direct citers only. A citer's own citers stay fully lazy —
  expand only when the agent actually opens that citer, exactly as
  originally designed.
- **Auto-ingest missing citers.** Resolves the earlier open question: mirror
  `_resolve_or_create_stub`'s existing outbound behavior — mint a stub (or
  trigger real ingestion if cheap enough) for an S2-known citer that isn't
  in corpus yet, don't block on it, don't just report-and-drop.

## Paper link-blindness fix — recommended approach

`_render_links_section` (`_numeric_ref.py:1239`) is a method on
`NumericRefHandler`; `PaperHandler` is a sibling `Handler` subclass, not a
`NumericRefHandler` subclass, so it can't just call `self._render_links_section`.
**Best practice: extract, don't duplicate.** Pull the method's body into a
shared free function (e.g. `handlers/_links_render.py::render_links_section(store,
ref) -> str`, or a small mixin if it turns out to need more shared state than
just `store`+`ref`) that `NumericRefHandler` delegates to (refactor, behavior
unchanged) and that `PaperHandler` (+ the other `Handler`-direct kinds the
audit flagged: draft, structure, cad, pcb, plan, pres, patent) call directly,
each registering `'links'` in its own `_SUPPORTED_VIEWS`. One extraction,
sweeps every flagged kind in the same change — don't fix paper alone and
leave the rest of the audit's finding #1 open.

## Status (2026-07-23, third pass) — ready to build

All design questions resolved except one, which gets a pragmatic call rather
than blocking the build:

- **Resolved, build now:** paper link-blindness fix (shared-function
  extraction, sweep all flagged kinds); inbound chase wiring, fully (S2
  `cites:` walk → reuse `_locate_chunk_in_target` +
  `_verify_support_with_caveats`, exhaustive per activated paper, no chase-side
  cap); active-paper = permanent DB marker; staleness via immediate
  paper-level link + existing `waiting`-backoff / `auto_check` upgrade path,
  no re-sweep; one-hop-only confirmed; auto-ingest missing citers (mirror
  `_resolve_or_create_stub`).
- **Pragmatic call, not left open**: build a **narrow one-off sidecar
  render** for the capped best-few display, rather than slicing the
  deferred turn-routing-and-context-DSL proposal. Reuses the *shape*
  (`card_combined` identity + capped count + expand-on-request) without
  committing to that larger proposal's scope. Revisit if this render's
  shape needs to generalize later.
- **Flagged dependency, not this build's job**: the exhaustive-chase-no-cap
  policy leans on the global spend circuit breaker (`OPEN-ITEMS.md` 💰
  Budget guardrails Piece B) as its cost backstop — implemented, green,
  **unshipped**. Either ship it alongside this work or add an interim
  per-paper cost/count log (soft warn) until it lands. Raise this
  explicitly with whoever scopes the build; don't silently assume the
  backstop is live.
- **Explicitly separate, not this build**: type-2 (general-content-
  similarity) linking — `related-to` + `meta.note`, own future scope, do not
  touch `cites`.
- **`PRECIS_CHASE_LLM` policy**: turning it on is implied by "wire the
  inbound chase fully" above (the verify/locate hooks are load-bearing for
  the terse verdicts this whole feature exists to produce) — not a separate
  open question anymore, just note it's a real prod cost-profile change the
  first time it flips on.

## Status (2026-07-23, fourth pass) — dst_pos gap closed

The "build now" list above shipped locating only into the *citer's* chunks
(`src_pos`); it left `dst_pos` — which chunk of the *cited* paper Y is
actually being referenced — unset, so the sidecar could only render
outbound ("chunk C of X cites Y"), not inbound ("chunk D of Y is cited by
X"). That gap is now closed in the same worktree:

- `_resolve_citer_chunk` runs a second lexical-overlap-then-LLM-confirm
  locate pass into Y's own chunks (using the citer's located chunk text as
  the query) and writes `dst_pos` when it finds a confident match — no
  forced guess when it doesn't (a citer citing Y's paper-level contribution
  rather than one specific passage is a real, expected outcome).
- `_citer_sidecar.py` gained `render_cited_by_sidecar` (`direction='in'`,
  filtered on `dst_pos`), sharing the same verdict-filtering/capping/
  best-first logic as the outbound render, wired into
  `PaperHandler._render_chunks` as a second small section alongside the
  existing outbound one.
- Cost: this roughly **doubles** the per-citer LLM *locate* work (one
  locate into the citer's chunks, one more into Y's), on top of the
  existing one `_verify_support_with_caveats` call — noted in
  `inbound_chase.py`'s cost-caveat paragraph. Still dark behind
  `PRECIS_INBOUND_CHASE_ENABLED`; still leans on the unshipped spend
  circuit-breaker as its cost backstop (unchanged from the prior status
  entry above).
- Out of scope, unchanged: type-2 similarity linking, the turn-routing/
  context-DSL proposal, changing `PRECIS_CHASE_LLM`'s default, removing the
  dark flag gating.

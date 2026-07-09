# 0051 — Turn-taking: persona threads, tool-call context curation, blackboard convergence

- **Status**: proposed (2026-07-09) · design conversation captured, not
  yet sliced. This ADR records the *decisions*; the exploratory
  reasoning + open issues live in the design of record
  [`docs/proposals/turn-routing-and-context-dsl.md`](../proposals/turn-routing-and-context-dsl.md).
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0036 — Universal handles](./0036-universal-handles.md) — the
    address grammar (`pc1234`, signed steps/spans, `^` ancestor) the
    context-curation tool calls resolve against. Its **universal
    short-code generalization is a hard prerequisite** for the curation
    surface below.
  - [ADR 0038 — Prompt assembly & principles](./0038-prompt-assembly-and-principles.md)
    — the assembler (`Module`/`Block`/`Layer`, `Profile`) this ADR
    extends: the thread persona is the fine-grained successor to the
    coarse `AGENT`/`HELPER` `Profile`, and the ordering gradient is a
    refinement of the `CACHED`/`VARIABLE` layer split.
  - [ADR 0046 — The LLM routing layer](./0046-llm-routing-layer.md) —
    the `Tier`/`Transport` seam the delegate-on-confidence router drives.
    Two concrete tiers are live today (Opus / Sonnet); the tier
    *vocabulary* generalization is 0046's concern, not this one.
  - [ADR 0044 — Derived-job lane](./0044-derived-job-lane.md) — the
    content-addressed idempotent substrate a promoted `<<expr>>:save`
    calc lands in.

## Context

Two mechanisms are ad-hoc today and want a single frame:

- **Model selection per turn** is a half-adopted patchwork (ADR 0046),
  and the decision of *which model* runs a turn is made structurally
  (an `LLM:*` tag on the todo), not by the work itself.
- **Context assembly for a turn** is either hand-built (the planner's
  two-layer prompt, `workers/planner_prompt.py`) or implicit. The model
  has no way to curate its own working set, and there is no lateral
  awareness between concurrent threads working the same artifacts.

The unifying frame: **every turn is a `kind='job'`.** `plan_tick` is
already one; generalizing that to *every* turn (interactive web,
reviewers, dreamer) gives one substrate, one router, one context
surface — and, because each turn persists its prompt/route/outcome, the
training corpus that makes routing tunable.

This ADR fixes the shape of the turn: **who the thread is** (persona),
**how its context is ordered and curated**, **how it delegates**
(forks / diverges), and **how forked threads re-converge** (the parent
barrier + the blackboard, §11). Divergence and convergence are the two
halves of one fan-out; the ADR name foregrounds *convergence* because
that is the harder, previously-missing half. It does *not* commit an
implementation slice; the first slice (shadow-mode router) is named in
the proposal.

## Decision

### 1. Thread vs. turn; every turn is a job

A **thread** is the persistent identity (owns the persona + the working
context set); a **turn** is a disposable job row. A thread never
re-runs a turn in place — each turn is its own row, so the routing
corpus stores every turn independently. A "followup" is not a distinct
primitive: it is simply the thread's next turn, inheriting the set.

A turn is **one model pass, not one message**: within a single turn the
model may emit tool calls and receive their **inline responses** (reads,
§6b) — several request/response exchanges in one pass — before the pass
ends. What a turn *cannot* do is straddle two render cycles: the
re-assembled fisheye context (§14) is the boundary between this turn and
the next. So "one pass per tick" bounds the *render*, not the number of
tool calls inside the pass.

### 2. The persona is a skill, pinned first, exempt from decay

Each **thread type** (`write-document`, `dream`, `review`, `triage`, …)
is fronted by a **persona**, authored and loaded via the *existing
skill mechanism* — a skill carrying a `persona` tag/subtype, not a new
kind. The chosen persona is the **first block** of the prompt and is a
**floor**: it never ages out, never demotes, and its cache segment
changes only when the thread type does (i.e. never, within a thread).

This generalizes the coarse `Profile` enum (ADR 0038 §4) into a
registry `thread_type → persona_skill_id`, and folds the current
one-offs (`_PINNED_SKILL_ID`, the `has_review`-gated reviewer persona)
into one mechanism.

Persona is **per-thread, not per-turn** — primarily for **coherence**: a
thread is *one* working identity, and swapping its stance mid-stream
fractures the reasoning it is meant to sustain. (The immutable cache
floor and affinity batching also benefit, but that is a *consequence*,
not the reason — and the weakest argument for the choice.) If a genuine
need to re-persona a live thread ever surfaces we will revisit; the
expectation is that it does not. A review is therefore a *separate
spawned thread* (reviewer persona), **not** a mid-thread persona swap. **Thread identity = the todo `ref_id`**
(`PRECIS_CURRENT_TODO`); a turn = a job row under it. A forked peer is a
*new* todo → new thread → new (forked) working set. The persona skill
carries a `persona` tag that also **excludes it from the on-demand skill
menu/index** (a persona is not a reference doc).

**Beyond the persona, a thread pins a skill *index* + known-needed
skills.** The registry (above) also carries, per thread type: a pinned
**skill index** — a compact menu of the skills reachable on-demand
(personas excluded) — plus a few **skills the thread type is known to
want**, pre-loaded into the floor. An on-demand skill the model
*expands* mid-thread does not join the floor immediately (that would
bust the prefix); it follows the **general sorting model** (§3): enter
at the tail, migrate to the skill floor at the next cache break.

### 2b. Two documents on one substrate; plan ≠ dispatchable tree

A synthesis thread works over **two documents**, both chunk-trees with
handles (ADR 0033/0036), distinguished by role:

- **The deliverable** — the draft being written. The product.
- **The plan** — a hierarchical outline that is the thread's *todo list
  + reasoning notes* (the *forward* facet of the logbook; terminology
  anchor below). Authored via the *same* draft verbs
  (`put`/`edit` split/merge/reorder), numbered by reading-order position
  (ADR 0037 — not stored, so reorder renumbers for free), addressable by
  `dc<id>`. A **distinct kind/subtype** so it is never exported as the
  product. It is the thread's **spine**: small, rendered **whole** every
  turn (a compact tree with `[done]`/`[wip]`/`[open]` markers), and
  **store-backed** so it survives turn exhaustion.

The plan is **not a memory** (too structured for flat prose) and **not a
new structure** (reuses chunks/handles/numbering/edit). It is distinct
from the **dispatchable todo tree** (`kind='todo'`): the plan is the
*reasoning map*; the todo tree is the *dispatch queue*. Only plan nodes
that need an agent are **promoted** to todos, anchored back to the plan
node (`meta.anchor=dc<id>`). This split is also the fix for the
**runaway-planner** failure (roots SQL-killed 3×, OPEN-ITEMS): reasoning
the whole outline is free and inert; only *promotion* arms dispatch, so
authoring a plan no longer auto-fans a dispatching tick per node.

**The plan is also the ledger** (§7 — the *backward* facet of the same
logbook). Worked in place, a node
accretes a terse **fact** annotation of what was done *and what was
tried* — the `[done]` marker plus rejected attempts (`§3.2: considered
X, empty → used Y`); dead-ends that map to no node hang on the
current-focus node or the plan root. So the retrospective trace needs no
separate structure: the reasoning map *is* the ledger, and the agentlog
is only the automatic safety net for what the model did not write down.
The plan also carries **prospective** continuity (§7): a `▸ you-are-here`
cursor, `[open]` nodes as the next actions, and pre-structural hunches
as belief-marked annotations (`? verify Z`). The cursor is
**model-owned** — it **stays where the model last placed it** until the
model moves it; the harness does *not* infer it from the last-edited
node (that node is often future scratch, not "where I am"). Cold-start
fallback only: the first `[open]` in reading order. Moving the cursor is
part of the **mandatory per-turn log update** (§7). Fact and belief are
distinguished by marker (`done:` vs `?`/`⚠`), so the plan **subsumes the
old handoff note entirely**.

**Terminology (canonical, used throughout this ADR).** The plan, its
accreted ledger, and the `▸ you-are-here` cursor together are the
thread's **logbook** — one store-backed artifact with three facets: the
**plan** (forward — the outline + `[open]` next actions), the **ledger**
(backward — the accreted fact/attempt trace), and the **cursor**
(present — where the model is). "Logbook" names the whole; "plan" and
"ledger" name its two directional views. The older terms *notebook* and
*action ledger* are **retired** in favor of *plan* and *ledger*.

### 3. Ordering: a four-tier staticness gradient

Prompt caching is a strict prefix match, so **block order is the cache
policy**. Lay blocks in a monotonic staticness gradient (front → back);
never a more-volatile block before a less-volatile one:

1. **Immutable-within-session** — persona/skills **and** static
   resources (papers, paper chunks, held figures). Cannot change in a
   session → deepest cache prefix.
2. **Static-ish snapshots** — indexes, search results, glossaries.
   *Snapshot-on-fetch*: a fetched search result is frozen and cacheable;
   **re-running it is the explicit invalidation act**. (This is also how
   the liveness/cache tension is resolved — see §4.)
3. **Transient memories** (§7) — the pinned ledger tail + inbound
   cross-thread messages, re-surfaced from the store rather than kept
   cache-warm.
4. **Active work** — the tail: current draft blocks, `doc_context`,
   children's returns. Volatile *and* attention-hot (front-and-tail
   placement also counteracts lost-in-the-middle — a mid-context block
   at full fidelity is not necessarily attended to).

Cache breakpoints fall on the tier boundaries.

**General sorting model — new static content enters at the tail, then
migrates to the floor at the next break.** An immutable-ish block that
first appears *mid-thread* (an expanded skill pulled from the index §2,
a freshly-held figure) cannot be spliced into the cached prefix without
busting it. So it **enters at the active tail** (tier 4), stays there
until the **next cache crunch** (§5 island-switch / §6b bunched
eviction — a break we pay for anyway), and *then* **migrates forward**
into its natural static tier (an expanded skill → the skill floor,
tier 1). Deferring the prefix insertion to a break that was already
happening makes the promotion free. This is one rhythm with
island-switching (§5) and bunched eviction (§6b): **structural change
rides the crunch**, never pays for a mid-lineage prefix rewrite.

### 4. Liveness lives in the tail only

The earlier "working set is always live" stance contradicted a stable
cache prefix: a live handle whose underlying ref is edited changes the
rendered bytes and busts the whole downstream prefix. Resolution:
**live-resolved handles may appear only in the active tail (tier 4);
the cached prefix (tiers 1–2) holds immutable resources or frozen
snapshots.** A soft-deleted handle renders `[gone]` rather than
crashing. The audit/tuning log still snapshots the *rendered* prompt at
turn time (the set holds live pointers; the log holds the immutable
render).

*Notes for later (not yet decided):* **(a)** `[gone]` should probably
carry a **reason** (`[gone: superseded by dc44]` / `[gone: deleted]`) so
the model can *react* rather than merely notice absence. **(b)** Editing
a chunk that is *embedded* in a cached prefix busts that prefix anyway
(§3/§5). Since we now allow tool-driven edits mid-lineage, the harness
could **tell the model the cache is already busted** and hand it a
**return hint — "prefix cold; good moment for a cleanup phase"** — so a
curate pass (prune stale eyes, re-`focus`) piggybacks on a break we were
going to pay for regardless, instead of churning the prefix twice.

### 5. Cache economics + affinity scheduling

Cached tokens are **not free**: an Anthropic cache *hit* bills ~10% of
input price; a *write* carries a premium (~1.25× for the 5-min TTL, ~2×
for 1-hour), and the TTL is a sliding window refreshed on each hit.
Local llama.cpp has no per-token billing but ~one hot KV slot, so a
prefix switch is a cold prefill. Consequences:

- The dollar incentive to evict is weak (reads are cheap); **eviction is
  driven by context *quality* (lost-in-the-middle / context rot) and by
  *churn avoidance* (the write premium), not by token cost.**
- **Token budget is not a per-turn forcing function.** Task needs vary
  too much to set a fixed ceiling (a synthesis wants a huge window; a
  targeted edit wants almost none), so the driver **right-sizes its own
  context** by placing eyes (§6) — better than any static policy. There
  is **no "force-demote the oldest under budget pressure"** rule. The
  only hard limits are backstops, not sculptors: the **model context
  window** (rare to hit; fisheye is the backstop that lets a huge task
  still fit) and the **runaway-spend safety cap** (the existing
  `--max-budget-usd`; a loop guard, not a context shaper). Cost-awareness
  narrows to where it is actually sharp: **local single-slot models**
  (prefill/slot) and **churn** — it does not trim the driver's turn.
- **Persona-first is chosen**, accepting that each thread type is its own
  cache island with zero cross-fleet prefix sharing. This is made
  affordable by **affinity-batched scheduling**: the dispatcher bunches
  ready turns by persona (then lineage) and runs them consecutively,
  keeping each island hot for its batch. The pressure is
  provider-asymmetric — an optimization on Anthropic's multi-prefix TTL
  cache, near-mandatory for the single-slot local models.
- **The scheduling quantum is a lineage run to the next cache-break,
  not a fixed turn count.** The dispatcher holds one persona/lineage
  and runs it consecutively (tens of ticks) *until a
  cache-invalidation event it was going to pay for anyway* — the
  **bunched decay-drop** (§6b: one break at the earliest dropped
  position) or a **snapshot bust** (§3 tier 2: a re-run `search`). At
  that boundary the prefix is already cold, so switching islands costs
  **no extra write/prefill** — it is the zero-waste cut point. This
  makes eviction and scheduling the *same* rhythm: batch the drops,
  and the drop batch is also when you swing to another project. (On
  single-slot local models this is near-mandatory; on Anthropic it
  merely avoids paying a fresh write premium mid-lineage.) A safety
  cap on the quantum still applies — a lineage that never triggers a
  cache-break must yield eventually so siblings are not starved.

### 6. The fisheye context model — eyes on two axes

The render primitive for every structured artifact (paper, draft,
logbook) is a **degree-of-interest fisheye** (Furnas 1986): default-on
above a size threshold, whole below it. The model places **eyes** on
nodes — the single render mechanism — and an eye is a point on **two
orthogonal axes**:

- **Extent** — *how much* to render, an ordinal ladder: **`none`** (a
  parent TOC line) < **`toc`** (one-line bookmark, ancestor path
  un-collapsed — "don't let §3.2.1 vanish under a collapsed §3") <
  **`summary`** (node summarized + skirt) < **`full`** (node verbatim +
  skirt) < **`fidelity`** (verbatim center fanning out over a wide
  graduated span — **±5 full / ±10 summary / ±15 kwd**, forward-biased;
  the "wide-angle" eye for working a whole region, the logbook cursor's
  default, §7).
- **Persistence** — *how long* it survives the decay machinery:
  **transient** (`ttl=1`, dies at the next crunch — the default for
  *inferred* eyes and search candidates, §6b), **normal** (adaptive TTL
  — the default for an *explicitly requested* eye), **pinned** (never
  decays — the logbook cursor and the last-5 ledger, §7).

The axes are **independent**: a `full` eye may be transient (a salience
lens) or pinned (the cursor); a `toc` eye may be normal or pinned.
`focus(handles, level)` sets **extent**; **persistence is derived** from
provenance/role (explicit→normal, auto-lens→transient, logbook→pinned),
with re-`focus` as the refresh/adopt action — no extra verb or argument.
This is **not** the rejected "second *automatic* axis": there is no
parallel auto-fidelity computed behind the eyes' backs — each eye the
model (or an auto-lens) places simply *is* a point in this 2-D space.
(The term "DOI" is **retired** to avoid clashing with Digital Object
Identifier.)

**The context skirt** (shared by `summary`/`full`, forward-biased): the
node's few **preceding** siblings render as **TOC lines** (you have
passed them), its few **following** ones as **summaries** (you are
heading into them), plus pre/post keywords and the **ancestor branch**
(`section_path`). So an eye returns a **neighborhood, not a bare chunk**
(opt-in `view='fisheye'`, tree-kinds only — non-tree kinds like
`calc`/`web`/`memory` have no neighborhood). It is assembly of existing
data — `resolve_relative`, `chunk_summaries`, `chunks.keywords`,
`view='toc'` — needing neighbours' derived summaries/keywords, so
**touched refs get a derived-compute priority lane** (ADR 0044): touching
a ref bumps it + its neighbours ahead of the round-robin backfill.

**Collapse is driven by sharpness, not cost** (§5): a bloated window
degrades reasoning even when tokens are free, so far-from-eye nodes
collapse. But there is **no size-scaled aggressiveness function** — too
much complexity for too little gain. The one concession to a growing
document is a modestly **higher pruning-cycle frequency** (crunch a
little more often as the window fills); the collapse *levels* themselves
stay fixed. The driver still opens as many eyes as a synthesis needs.

**Auto-eyes: recency + salience lenses.** Beyond what the driver
explicitly places — and beyond a focus's *structural* neighborhood
(above) — the render raises a few **small eyes it was not asked for**,
from two signals, because structural locality misses *temporal* and
*salience* locality:

- **Recency lens** — when any eye lands on a document, the ~10
  most-recently-mutated nodes also get small eyes (from agentlog
  `touched` / chunk mtime). Edits cluster at a moving front that is
  often structurally scattered (you just touched §5.2 *and* §2.1); the
  recency lens keeps that working front visible without re-`focus`ing it
  every tick. This is also why **a thread's own output is never a
  *silent* blind spot**: what you write is auto-eyed on write and, being
  durable in the store, is one `search`/`focus` away even after it fades
  from view — only *un-retrieved inputs* can be silently missing (see the
  distributional-bet risk in Consequences).
- **Salience lens** — a node carrying an annotation (a linked
  `finding`, transient memory, or highlight) auto-raises a small eye
  when its document is in view — **papers especially**, where an
  annotated passage is evidence you cared. Sourced from the inbound
  link graph, **one hop deep**. This lens fires **hardest on the
  thread's first turn** (cold-start): when a document enters, its
  1-hop-linked memories/findings auto-eye immediately, so a thread
  resumes *already* looking at what was previously flagged on that
  material — no warm-up `search` needed. After the first turn these
  lenses decay like any inferred eye unless adopted.

Both are **small eyes only** (never big — they must not seize the focus
budget) and are `close`-able. Crucially they **decay faster than
requested eyes**: a lens is a *fading suggestion*, so it enters near
`ttl=1` (dies at the next crunch, like a search candidate, §6b) rather
than on the normal adaptive schedule. **Adopting** one (a re-`focus`)
promotes it to a requested eye with a refreshed TTL; otherwise it fades —
which is what keeps inferred context from silently accreting. So eyes
come from **three sources** but only **two provenances**: *requested*
(an explicit `focus` **or** its structural neighborhood — you asked,
directly or by implication) vs *inferred* (the recency/salience
auto-lenses — the system offered).

**Render-time derive dependency: bounded off-clock wait, then fall back.**
A `focus` on a just-requested paper/patent/search hit may reach the
render before its summaries/keywords exist. The resolution exploits the
loop's shape (§14): **assembly runs *between* ticks, off the model's
clock**, so a short wait here costs wallclock — not model time, tokens,
or budget — and the usual "never block a live turn on a worker" rule
does not apply. A **three-rung ladder**:

1. **Prioritize** — the touched ref + neighbours jump the derived queue.
2. **Wait, bounded** — assembly waits up to a cap for that compute
   (cheap, off-clock). The common case: it lands, the neighborhood
   renders complete on the very next tick.
3. **Fall back if it overruns** — render what exists (raw chunk text
   always exists post-ingest), placeholder the missing derived bits
   (`[summary pending]` / `[keywords pending]`), and **leave the ref
   prioritized** so the *following* tick renders complete. The eye stays
   armed; nothing is lost, only deferred.

Two edges: a **search-result first page renders immediately** — the
handle + snippet/keyword line comes from the search index itself, no
derive needed (§6b); only `focus`-ing an entry pulls the neighborhood
onto the ladder above. A **freshly-fetched paper/patent may lack even
chunks** (ingest still running): `focus(pc)` then renders an `[ingest
pending]` stub, keeps the eye armed, and re-resolves once ingest +
derive complete — a *suspend* (adjacent to the `wait-for` state, §10),
not a failure.

### 6b. Curation, the render-loop, and the three-phase turn

Curation is a **single structured MCP tool call** — on-distribution,
validated, logged for the routing corpus, and a **clean trust boundary**
(§12). **One verb, one `level` dial** — the **extent** axis of §6
(persistence is derived, not set here) (ASCII English; "eyes" stays as
the design metaphor):

- **`focus(handles, level)`**, `level ∈ {none, toc, summary, full,
  fidelity}` (§6). `full`/`summary` expand the node + skirt and hold it
  across ticks; `fidelity` opens a graduated forward-biased span; `toc`
  is a bookmark; `none` clears (a handle-list receipt — because
  continuity is **store-first**, clearing loses nothing but working-set
  membership, so bold clearing is safe: for sharpness, not cost).

For readability the ADR still uses the shorthand **focus** (a `full`/
`summary` eye), **keep** (a `toc` eye), **close** (`none`) — but they are
*one verb, one dial*.
Delegation (`spawn`/`call`) is likewise a tool call over
`put(kind='todo')` + `requested`→job — no inline `[Spawn:…]`.

**The render-loop (see §14).** A tick = **one model pass**; precis
re-assembles the fisheye context *between* ticks and discards the
transcript. So **state-changing calls** (`focus`/`put`/`edit`/`call`/`spawn`) do
**not** return content mid-tick — they reshape the **next** tick's
render, where a focused chunk appears **expanded in its structural
position** (under its heading, among its siblings), never as a dangling
tool-result. **Reads** (`search` and the cached fetch-kinds) are the
exception: they return a bounded page **inline, same pass** (ephemeral,
uncached — the pass's triage scratch, discarded next tick), so the model
scans and curates *within the turn*; only durable **placement** defers to
the render. Queue what you change, read what you need, re-render. Hence the **three-phase turn**: **(1) curate** (rescue what
the tail flags as decaying), **(2) work**, **(3) log** (advance
`▸ you-are-here` and annotate done/tried/hunches on the plan, §7).

**A read returns inline; what *persists* is a separate knob.** A `search`
(or `perplexity`/`web`/`youtube`) returns its first page (~100 hits)
**inline this pass** (ephemeral, uncached), so the model triages
immediately. Two **orthogonal** parameters govern it:

- **`return`** — the inline **verbosity** *now* (`kwd`/`toc`/`summary`/
  `full`, the extent ladder §6): how much of each hit you get back to
  judge, this pass.
- **`eye`** — an **auto-eye planted on each hit for the *next* turn** (an
  ordinary extent×persistence eye, §6; default **`none`** for search).
  `eye='toc'` = "bookmark all these passages next turn"; `eye='full'` =
  keep them open.

The two are independent: `return='full', eye='toc'` reads every hit in
full *now* yet keeps only cheap bookmarks *next turn* (you `focus` the
few you actually want). **Planted eyes are a default the rest of the pass
may prune** — a later `focus(hit, none)` drops one before the pass ends.
With the default `eye=none` the page is **pure one-shot triage**: gone
next tick unless acted on.

**A search is a simple, stateless tool call — it does *not* auto-page.**
There is no sliding scan and no auto-`more`: `search` returns *a* page
inline, the model decides what to sticky (`focus`/`eye`), and that is the
end of it; wanting more results is just issuing another `search` (a fresh
tool call), not a kept self-advancing cursor. Paging is a judgment call,
not an automatic drip that quietly eats context. Better still,
**triage-and-sticky is itself delegable**: the driver need not read 100
hits itself — it can `call` a **lesser model** (Haiku/Sonnet) whose whole
job is "skim this result page and sticky the relevant handles," so the
net-cast *and* the skim happen in a **throwaway context** and only the
chosen handles cross back (§9 find-Call docking). Cached tool-kinds stay
**durable in the store** (re-fetchable by handle) but **transient in
context** like everything else.

Mechanically this is **not a special one-tick path** but the ordinary
decay machinery with a floor TTL: a likely-contender / search hit enters
at **`ttl=1`** — marked to drop at the **next crunch point** (the bunched
eviction, below), not necessarily the literal next tick — so peripheral
candidates cost one cache-cycle of context and vanish in the same batch
as everything else, unless `focus`/`keep` promotes them to a refreshed
TTL.

**Decay ladder: `full` —warn→ `toc` —warn→ gone**, with **adaptive
turn-TTL** (§5: TTL shortens as context grows; wallclock only a GC
backstop). The **"about to expire" list renders in the tail every N
ticks** (not every tick — less noise/churn); the model rescues; then
drops are **applied in one bunched batch** (one cache break at the
earliest dropped position). TTL is metadata, not a sort key.
**LRU-by-usage is rejected** — presence ≠ use; the `focus` level is the
authoritative signal.

**Render markers + status line.** Two things *could* be marked — **fidelity**
(full vs toc) and **provenance** (requested vs inferred). Fidelity is
already visible (a full node shows its body; a toc node is one line), so
marking it is redundant. Only **provenance** needs a glyph, and only on
the inferred ones: an **`◦` prefix = inferred** (a recency/salience lens,
§6 — a fading suggestion you did not place); **everything unmarked is
yours** (an explicit `focus` or its neighborhood). One glyph, not three.
A **second glyph marks imminent death** (a different axis — lifecycle,
not provenance): a handle scheduled to drop at the upcoming crunch
renders a **`†` (tombstone) prefix** *inline, next to its id*, so "this
is about to go" is visible **in place**, not only in a separate list.
**Both channels run:** the **tail status line** (every tick) carries eye
count, budget used, and **"context review in N ticks"** (the countdown to
the next crunch); and on the review tick the full **soon-to-die list**
renders inline **as well** — so the curate phase can rescue either by
scanning the list or by spotting a `†` on a node it is already reading.
(Glyphs tunable; the invariants are *a visible requested-vs-inferred
mark*, *a per-handle soon-to-die mark*, and *a decay countdown*.)

### 6c. The turn-facing verb surface

The persona-driven turn sees a **7-verb base surface** (plus per-persona
extensions, below), not the full precis kit. The property that makes it
coherent: under the render-loop (§6b/§14) **state-changing calls**
(`focus`/`put`/`edit`/`call`/`spawn`) are **queued intents** whose effect
materializes in the *next* render, so they return a **receipt/ack, not
content** (a malformed handle is the only error return). **Reads**
(`search`, and the cached fetch-kinds) are the exception the substrate
already affords — they return a **bounded page inline, same pass**
(ephemeral, uncached), so the model triages and curates within the turn;
only durable *placement* defers to the render.

| verb | role | effect | returns |
|---|---|---|---|
| `focus(handles, level)` | curate | the **extent** dial (§6; persistence is *derived* from provenance, **not** an arg); tree kinds render a neighborhood, atomic kinds the ref | ack (renders next tick) |
| `search(q, kind?, return=, eye=)` | retrieve | returns a triage page **inline this pass**; `eye=` plants an auto-eye on each hit for next turn (default `none`) | **content** (inline page) |
| `put(kind, …)` | author | create — draft/plan chunks, todos (= promote, anchored §2b), memory-notes | receipt |
| `edit(…)` | author | modify an existing chunk (split/merge/reorder/replace) | receipt |
| `call(brief, tier?, scope)` | delegate | blocking one-shot helper; mutations staged | return docks next tick |
| `spawn(brief, tier?, scope)` | delegate | detached multi-turn child (peer/up-tier ok = fork) | receipt |
| `wait(on)` | delegate | suspend on external event (→ `blocked-by`, §10) | suspends thread |

**The `eye` axis is universal; only its default varies.** Every verb that
surfaces content can leave an eye behind for next turn — the question is
just the default. **Place/author verbs default it on** (a `focus` *is* an
eye; `put`/`edit` auto-eye the write at `full`, §6); **`search` alone
defaults it off** (`eye=none`), because a net-cast is triage, not
acquisition — you keep only what you `focus`. This is **not** arbitrary
default-flipping that would confuse the model: it tracks **intent** (what
you explicitly place or write, you keep; what you fish up, you sift), and
every planted eye is **visible** (the `◦` inferred-eye glyph, §6) and
**overridable** (prune with `focus(…, none)` in the same pass, or let it
fade). And the planted eye's **extent is independent of the inline
`return` verbosity** — the distinct axis: read in `full` now, keep as
`toc` next turn.

**What folded in (nothing lost):**

- **`keep`/`close` → `focus(level='toc'/'none')`** — the eye triad is one
  fidelity dial, not three verbs (§6b).
- **`get` → `focus`** — `focus` is the read for *every* kind; tree kinds
  yield a neighborhood (§6), atomic kinds (calc/web/memory) just the ref.
  There is no bare per-chunk read.
- **`promote` → `put(kind='todo', anchor=dc…)`**; **`note` →
  `put(kind='memory', MEM:transient)`** — both are just a typed `put`.
- **`approve` dropped** — a Call's staged mutations render as a **pending
  set** in the driver's next tick; it commits them by referencing them in
  an ordinary following `edit`/`put`, so no dedicated commit verb.
- **`resticky`/`more` are not verbs** — rescue is re-`focus`; and
  `search` does not auto-page (§6b), so "next page" is simply another
  `search` call, not a stateful cursor.

**Per-persona extensions (base-7 + declared menu).** The full kit
(`link`/`tag`/`delete`/`supersede`/…) is **not dropped** — it is reached
via a **persona's declared extension menu**, off the synthesis base. The
registry that binds `thread_type → persona_skill` (§2) also binds
`persona → extra verbs`:

- **synthesis / write-document** — the base 7 only.
- **curator / librarian** — `+link` (typed relations; also written
  *implicitly* when an `edit` cites `pc123` → a provenance link), `+tag`.
- **dream** — `+supersede`, `+acquire` (already gated in the dream loop).
- **review** — `+flag-claim`, `+request-evidence`.
- **`delete`** — destructive; harness/worker or an explicit persona only.

Specialization is **additive and declared**, never a mid-thread surface
swap (same argument as the persona floor, §2).

- **Why so few — and why it is *not* mainly about tokens.** The tool
  schemas live in the **cached floor** (§2/§3 tier 1), written once and
  billed at cache-hit rates, so an extra verb is nearly free per-turn.
  The real cost of surface bloat is **decision confusion** (more
  off-distribution ways to pick wrong) and **cold-slot prefill** on
  single-slot local models. So the surface is minimized for *clarity and
  prefill*, not token budget. The one verb that *earns* its keep by doing
  a lot is **`focus`** (curation + all reads); the rest are irreducible
  (retrieve / create / modify / two delegations / suspend).

### 6d. Artifact variants + eye transfer

Model-driven **exploration** (draft rewrites, and especially `cad` /
`structure` atomistic / `pcb` designs) wants to try a change without
losing the original. A **variant** is a **clone-with-diff** of a
structured artifact — the artifact analogue of a thread fork (§9) — kept
on the blackboard with its own attached notes. Each change shows the
**new variant beside the old** so the driver can judge and keep the
better (soft-deleting the worse, §4; its notes survive as provenance).

The move that makes comparison cheap: **eyes are a lens, separable from
the artifact.** An eye set is a view spec — handle / `section_path` /
atom-id positions at fidelities — so the *same lens* points at any
variant in a lineage:

- **Eye transfer by structural key.** A clone inherits the parent's
  eyes; because eyes address nodes by stable keys, corresponding nodes
  render at the **same fidelity** in A and B — you compare the exact
  region you cared about without re-placing eyes. A node deleted in a
  variant renders `[gone]` there (reusing soft-delete rendering, §4); a
  node the variant *added* raises a fresh inferred eye (§6).
- **Compare render** — a `view='compare'` over two (or more) variant
  handles renders them under the shared lens, **diff-highlighted**, as
  the fisheye neighborhood (§6) of the changed region; old and new stay
  in context together until the driver keeps one.

Design-space exploration expressed entirely in the fisheye vocabulary —
no new curation concept, just the lens applied across a variant lineage.

### 6e. The conceptual surface is large; the model's use surface is small

Most of this ADR is **substrate the model never reasons about**: decay /
TTL, bunched eviction, affinity batching, cache economics, the derived
lane, ledger distillation, snapshot storage. That machinery runs
*between* ticks (§14) and is **invisible at the tool boundary**. What the
model actually **sees and does** is deliberately small — and *that*, not
the spec's length, is its cognitive load.

**What it does — 7 verbs, one dial** (§6c): `focus(handles, level)`,
`search`, `put`, `edit`, `call`, `spawn`, `wait`. The only knob with
range is `focus`'s `level`; everything else is "name a handle, name a
kind."

**What it sees — a compact HUD** (a handful of terse signals, each
defined above):

- `▸ you-are-here` — the cursor: where I am (§7).
- `[open]` / `[wip]` / `done:` — plan-node state; `?` / `⚠` — hunches (§2b).
- `◦` — an inferred eye: a fading suggestion I did not place (§6b).
- `†` — this handle drops at the next crunch: rescue it or let it go (§6b).
- `[gone]` (+ reason) — a handle whose ref was deleted/superseded (§4).
- `[summary pending]` / `[ingest pending]` — derived data not ready yet (§6).
- **status line** — `eyes N · budget · context review in N ticks` (§6b).
- the **persona** (pinned *how-to-work* advice, §2/§13) + the **skill
  index** (a menu it can expand, §2).

That is the whole interface: **seven verbs and roughly eight
glyphs/markers**, plus a persona that tells it how to behave. A model no
more needs to understand TTL math or cache islands than a writer needs
to understand a filesystem to save a file. So the **real** risk is not
surface complexity but the **distributional bet** (Consequences) — that
the model wields this small surface *well* under an unfamiliar loop; and
the mitigation is exactly to keep the hints **few, terse, and
consistent** (this list is the whole set) so the affordances stay
legible.

### 7. Continuity: the logbook (living plan + pinned ledger, no separate note)

There is **no transcript.** "The full conversation" bundles three
functions, and only the first leaves the render. **(a) intra-turn
chain-of-thought** (the model's scratchpad within a single pass) is
**dropped from the render** and never re-injected into a later turn,
because re-injecting it *is* the lost-in-the-middle / telephone-game rot.
It is still **retained on the tick row** for the corpus/audit (§15), so
"discarded" means *out of context*, not *deleted from disk*. **(b) the
retrospective trace** (*what was done*) and **(c) prospective intent**
(*what's next + hunches*) survive, and **both live in the plan**, not in
a separate handoff note. Continuity is
a **duo**:

- **The living plan** (§2b), rendered **whole every turn** (every node
  present, at *graduated fidelity* around the cursor — below) and
  store-backed, carries everything a note used to: **where I am** (a
  `▸ you-are-here` cursor, **model-owned** — it stays put until the
  model moves it, §2b), **what's next** (`[open]` nodes), and **hunches**
  (pre-structural beliefs as node annotations, `? verify Z`). Durable
  *reasoning conclusions* live here too, written down — not in scrollback.
- **The pinned ledger** (fact): the retrospective action+outcome trace
  (`t34 focus pc12 · drafted §3.2`; `t35 search "X" → nothing`). The
  fact line is best produced **by a cheap distiller, not self-report**:
  a **Haiku pass reads the tick's retained raw completion + served
  tool-calls (§15) off-clock** (the derived lane, ADR 0044) and emits
  the one-line digest into the ledger — reliable and *always useful*,
  where a hand-written log is only checked to *exist* (the metacognition
  risk, Consequences). This is **not** the rejected transcript
  re-injection (§7 head): the distilled fact is the same digest the
  model would have written, produced reliably — not the raw pass
  re-rendered. The model's own done/attempt annotations (§2b) and the
  agentlog / `touched` links (§1) remain as corroboration/safety net.
  The **anti-thrashing memory** — the "I already tried that" record — so
  **negatives retain longer than successes**; fact-framed, no
  hallucination ratchet (the distiller summarizes *what happened*, it
  does not invent).

**A distinct decay model — a pinned rolling window.** The plan and ledger
do **not** ride the fisheye TTL (§6b). The plan is the spine (always
whole). The ledger has a **hard floor: the last ~5 turn-reports stay
pinned**, exempt from the bunched eviction — so at a crunch where fisheye
*handles* are dropped, the last-5 fact lines survive. Older entries fall
off the render but remain reconstructable (agentlog + annotations).
Adaptive-N extends *above* the floor when thrashing, never below it.
(These are **fact lines, not verbatim turns** — not the rejected
scrollback window.)

**The cursor carries a `fidelity` eye by default.** "Look ahead a few
steps" is not a special mechanism: the `▸ you-are-here` cursor simply
holds a **`fidelity` eye** (§6) — a graduated, forward-biased span (±5
full / ±10 summary / ±15 kwd) over the surrounding logbook. It is an
*inferred* placement (fast-decaying, §6) the model can override — any
node it deems essential it `focus`es to `full` the normal way
(**explicit beats inferred**).

**Log every turn — a hard post-condition, but the burden splits.**
Keeping the plan *living* is **not optional** — yet the two halves have
different authors. The **retrospective** fact line is **distilled by
Haiku from the raw completion** (above), so the model no longer
hand-writes "what I did"; that half is guaranteed useful by
construction. What stays **model-owned and mandatory** is the
**prospective** update — advance `▸ you-are-here`, mark `[open]` next,
record hunches (`? verify Z`) — because *intent* is not recoverable from
the completion's rearview. The harness **checks the cursor/intent
advanced**; a turn that ends without it is **re-prompted and repeated**
("move your cursor and say what's next before ending"). This is
*enforcement*, distinct from the store-reconstruction safety net below
(which covers a hard *kill*): a lazy-but-completed turn is caught and
redone; a killed turn still degrades gracefully rather than lobotomizing.

**Why no separate handoff note.** Its jobs all have better homes. The
living plan is store-backed and written incrementally, so it is *more*
robust than a note a killed turn might never write — which is exactly why
`plan_tick`'s exhaustion-resume already re-renders from the store, not
from a note, and why **no end-of-turn "write your handoff" reserve is
needed** (the plan is checkpointed by construction). So the self-handoff
note is **dropped**. What survives from the old transient-memory
substrate is only its **irreducible** job — a **cross-thread directed
message** (sibling→sibling), which is *not* per-thread continuity and so
cannot fold into the plan: a transient memory (`kind='memory'`,
`MEM:transient`, GC-swept, out of default search) readable by a named
sibling until read or a TTL backstop. Messages stay best-effort /
eventually-consistent — fine for hints, **useless for mutual exclusion**
(§10).

### 8. Routing: delegate-on-confidence, top-down

There is no separate router: the **driver (Opus) continues the thread
and delegates as one of its moves.** Triage *is* briefing — but the
delegation is an **ordinary `call`/`spawn` MCP tool call** (§6c), **never
a parsable "packet"**: all control travels the structured tool-call
channel (§12), nothing is serialized into prose. Its arguments are
`(tier, scope, terse instruction)`, and the "context-set delta" is not a
blob to parse — it is simply the **eyes injected into the child's first
render** (§9: the child starts looking at exactly the briefed handles).
So a brief *is* a context set + a prompt, delivered the same structured
way as every other op. The strong model curating context for a weaker
one is the opposite of the rejected bottom-up scheme (weak model
self-grading + summarizing *up*, which launders its errors into
"facts"). Evaluation always lives *above* the
work: a helper's return surfaces as a handle in the parent's next turn
and the parent is the gate. Dropping the driver tier (letting a weaker
model drive) is the one move with no evaluator above it and is covered
by **audit sampling**, adaptive-rate, feeding the tuning corpus. Ship
**shadow-mode first** — run at current tier, log the triage decision and
a sampled counterfactual — before the router decides anything live.

Two concrete tiers today: **Opus drives, Sonnet helps** (and Haiku
remains useful for mechanical reductions — summarize/filter/extract).
The high/medium/low tier vocabulary is a later refactor (ADR 0046); the
routing shape is tier-count-agnostic.

### 9. Taxonomy: Followup / Call / Spawn

| primitive | who writes next prompt | scheduling | commit model |
|---|---|---|---|
| **Followup** | this turn (its own next turn) | sequential | n/a — just continue |
| **Call** | this turn authors a helper's | **blocks**, returns a value | one-shot(ish); mutations **staged, driver bulk-approves** |
| **Spawn** | this turn authors a child's | detached, runs free | multi-turn autonomous; **receipt-default + post-hoc audit** |

- **Call** suits *pure, bounded* helpers (summarize / filter / extract /
  reformat) where the driver already supplied all context, so a one-shot
  turn is realistic; its proposed tool calls can be **staged and
  bulk-approved** by the driver — a cheap commit gate, the lightweight
  cousin of the deferred transactional subtree.
- **Spawn** suits *autonomous, multi-turn* work; it cannot be one-shot
  and is not staged — it runs free, returns a receipt, and is
  audit-sampled. **Spawn may target a peer or up-tier** (Opus spawning
  Opus): this is how a thread **forks** when two distinct focus areas
  arrive — spawn a peer for line B, keep driving line A. **No join is
  required** (a driver that wants the conclusion uses a wait-for; §10).
  **The detached return is non-blocking**: whenever the child completes,
  its ledger digest docks as a done-annotation on the anchoring node and
  raises a **small (inferred) eye** there (§6/§11) — even if the parent
  has moved on. The parent promotes it if it cares; a prior `wait` is
  what turns that soft dock into a hard join. So a detached child never
  *interrupts*; it leaves a fading marker the parent may or may not
  adopt, exactly like any other inferred eye.
- Forked context for Call/Spawn is a copy-with-diff of the current set.
  The **logbook forks with it** (§7), exactly like an artifact variant
  (§6d): the child gets a copy-with-diff and its **own `▸ you-are-here`
  cursor**, which descends into the child's sub-scope while the parent's
  stays in its own — two independent cursors over a shared lineage. A
  spawned child starts **already looking at its brief**: its initial
  **eyes are exactly the briefed handles** (it knows every `dc`/`pc` it
  was handed), and its cursor sits on the first of them — no warm-up
  turn spent re-locating its own assignment.
  Cursors are **never merged** (control-merge deferred, §11); on return
  the child's ledger digest **docks** as a done-annotation on the
  parent's anchoring node (§9 find-Call docking).
- **Call must time out** → failed-return, so a hung/bad helper cannot
  wedge the blocked driver.

**Return archetype = part of the brief** (caller declares it): a
*mutation* Call returns a **receipt** (the work is in the store; the
parent pulls with a `get`), but a *find/fetch/reduce* Call returns
**content** — and a **find-Call auto-docks its result in the parent**.
The found result appears as a **fisheye neighborhood** (§6) centered on
the hit, **anchored where it was asked** (e.g. next to §3.2 if that
motivated the search), carrying a **provenance note** (`found · query …
· by jb123 · src pc4471`) — handle-anchored, never an ungrounded
assertion. The real win: the child runs the search in *its own throwaway
context* (the 100 skimmed hits, the dead ends live and die there); only
the distilled attributed region crosses back, so the parent's window
stays clean. It arrives at **summary fidelity** (readable, not full —
the parent promotes to big if it wants), and **decays like any fetched
content** unless cited into the plan/draft (then it self-promotes).
Negative results dock too (`found nothing; nearest pc12/pc88`).

### 10. Fan-out has three outcomes

- **Success** → return surfaces as a handle; parent evaluates next turn.
- **Failure** → *is a result*: it comes back as a failed return and the
  parent decides (retry / re-brief / escalate / abandon). No special
  machinery.
- **Wait-for** (user input, paper delivery, external event) → *not a
  result but a suspended state*, mapped onto the existing `blocked` /
  `blocked-by` mechanism. Terminal failure and non-terminal suspension
  route completely differently despite both "not finishing."

**Children run in parallel** (the point of fan-out: 20 out, evaluate 20
returns), with three constraints. **(a) Partition up front.** Parallel
writers with no mid-flight visibility of each other collide on the
blackboard; the roster (§11) reflects only *committed* state, so it is
useless *within* a batch. The driver's brief must hand each child a
**disjoint scope** (anchors). Messages cannot substitute (best-effort,
§7). **(b) Homogeneity is cache-friendly.** A same-persona batch shares
one hot prefix → parallel *and* cheap; a heterogeneous batch thrashes
the local single-slot cache. Fan-out is usually homogeneous (20
filter-helpers), so this aligns. **(c) Concurrency + spend caps** — 20
parallel cloud calls hit rate limits and burst cost, and fan-out
amplifies runaway (the roots-SQL-killed incident).

**Fan-in is as-they-land, not a barrier.** The driver does **not** block
on all 20: each return **docks as a handle in whatever tick it lands**
(§9 find-Call docking), and the driver evaluates the arrived subset —
acting or re-briefing without waiting for stragglers. "As-they-land"
means at **tick boundaries, not mid-pass**: a return that completes
during a pass docks in the driver's **next render** (like any deferred
effect, §6b), so returns that land during one pass **batch into that
next render** and the driver always sees a *coherent arrived subset* at
the start of a turn — never a result materializing mid-reasoning. This
is the same one-render-per-tick granularity as everything else, which is
why it is consistent rather than a special path. A hard join is
**opt-in** (a `wait` on the specific children whose conclusion is
actually needed), not the default; most fan-outs are "harvest what
returns." **Wait-for is per-child**: one child suspended on a paper
delivery blocks only itself, not its 19 siblings nor the parent's
ability to process the other 19 returns.

**Amplification guard — fan-out is *the* runaway vector.** The
roots-SQL-killed incident was fan-out with no ceiling. Backstops (hard
limits, not shapers): a **width cap** (max children per fan-out); a
**depth/total budget** — a child inherits a *shrinking* fan-out
allowance so recursive fan-out cannot exponentiate; a **concurrency
semaphore** (N in-flight cloud calls); and spend on the existing
per-lineage `--max-budget-usd`. Within these safety limits the driver
fans as wide as the work wants (§5 — no cost-shaped trimming).

### 11. Convergence: a control **tree** over an information **blackboard**

Threads do **not** merge context windows. Two kinds of convergence:

- **Control convergence** = the parent barrier. A thread forks
  (Spawn/Call) and re-joins at the parent when children return. A tree —
  what the job substrate already is.
- **Information convergence** = the **store as a blackboard**. Threads
  read/write shared durable artifacts (chunks, findings, drafts,
  transient memories); a sibling reading what another wrote *is* the
  convergence. This is the classic blackboard architecture; precis is
  already one.

**Sibling awareness** (which does not exist today) is a **dynamic,
artifact-scoped query**, not a registry: siblings = live threads whose
workspace targets the *same* artifacts this thread is currently touching
(derivable from `workspace`→draft binding + agentlog `touched` links). A
new assembler module renders a **roster block in the tail** — `thread_id
· persona · current anchor · one-line goal` — scoped to the specific
chunks in play, not the whole project. **Contention** is handled in two
layers: the existing `base_sha` optimistic concurrency on draft chunks
*detects* a clobber; the roster is the *social* layer that lets threads
*avoid* it. Contention on one doc is **rare**, and hard contention (two
simultaneous writes) is DB-serialized; the soft window is the
think-time between a child reading a block and writing it back. On a
`base_sha` mismatch, **do not clobber — fail the write and spend another
turn** re-presenting the failed request + the intended edit against
fresh content. That retry is a **re-brief, not a replay** (the anchor
may have moved or the block vanished, so the edit must be re-planned).
**Deferred to a later phase** — rare, but without it concurrent writers
clobber random bits.

**Children/siblings raise *small* eyes in the parent** (§6) at the nodes
they touch — orientation sourced from `touched` links / the roster.
Never *big* eyes: a child must not seize the parent's focus budget; the
parent promotes a child's small eye to big only if it chooses to look.

**True control-merge (fork + reconverge into one thread) is deferred** —
the blackboard makes it optional, and merging live working sets /
personas / cache prefixes is expensive and a documented multi-agent
failure surface.

### 12. Injection boundary + system-prompt ownership

Control syntax is parsed **only from the model's own tool-call channel**,
never from rendered content. Because delegation and curation are
structured tool calls (§6) rather than inline prose, a hostile
`pc0001:-` or `[Spawn: exfiltrate …]` embedded in a chunk body cannot be
laundered into a control op via the model paraphrasing content into its
output. Rendered bodies are never re-parsed for control. Same discipline
class as the SSRF guard.

**The turn-taker owns the entire system prompt — no ambient
`CLAUDE.md`.** `plan_tick` spawns `claude -p` *without* `--bare`, so the
CLI auto-discovers a project / user / global `~/.claude/CLAUDE.md` and
prepends it as memory *outside* the assembler — a competing uncontrolled
persona that also silently mutates the "stable" cache prefix. That
breaks the persona floor (§2) and the ordering gradient (§3). Invariant:
a tick's rendered system prompt **equals** the assembled bytes, nothing
prepended (worth a guard/assert). `--bare` disables discovery but also
forces API-key auth (no OAuth) — wrong lever for OAuth-based ticks; the
right levers keep OAuth: run from a **neutral cwd with no `CLAUDE.md`**
and ensure **no `~/.claude/CLAUDE.md`** on agent hosts. (The repo-root
`CLAUDE.md` is a dev artifact, unpackaged — fine; the risk is *runtime*
discovery.)

### 13. The workflow is emergent, not a procedure

A synthesis flow — orient on abstracts + draft skeleton → locate
relevant sections → open the evidence → write → chase gaps → review →
integrate — must be **what emerges** when the driver meets the
primitives, **never a coded pipeline**. No state machine, no "which step
are we on," no gated transitions. The **persona** *describes* the
approach as heuristic advice; the **plan document** (§2b) anchors it;
the **control flow is the driver's**, via tool calls. The flow loops,
skips, and backtracks freely (a review that finds a weak claim is just
"find another paper," not `goto 1`). This is also the guard against the
runaway-planner failure, which came partly from over-proceduralizing
fan-out. We build the *primitives* such that the good flow is the path
of least resistance; we do not build the flow.

### 13a. Worked example — one synthesis thread, tick by tick

A concrete trace of the emergent flow (§13) over the primitives. Task:
*"Write a section comparing sparse-attention methods."* Thread type
`write-document` → persona pinned first (§2). Handles: `pc*` paper
chunks, `dc*` plan/draft chunks, `jb*` spawned jobs; `▸` is the cursor,
`◦` an inferred eye (§6b), `†` a soon-to-die handle (§6b). Ledger lines
are the `t… ` entries (§7). This is **illustrative, not a spec** —
nothing here is a coded step; every move is a tool call the driver chose.

- **t0 · cold-start.** Render = persona floor + skill index (§2) + an
  empty deliverable + a bare plan `dc1 [open] compare sparse-attention
  methods`. The salience lens fires on entry (§6): two prior `finding`s
  linked to the topic auto-eye as `◦` small eyes. `▸` on `dc1`.
  → *does*: `put` a plan skeleton — `dc2 [open] survey candidates`,
  `dc3 [open] axes of comparison`, `dc4 [open] draft §3.2`. `▸` → `dc2`.
  Log: `t0 put plan skeleton (4 nodes)`.
- **t1 · orient.** Plan renders whole; `dc2` under the cursor's
  `fidelity` eye.
  → *does*: `search("sparse attention long-context", kind=paper,
  return=summary, eye=none)` — ~40 hits **inline this pass**. The driver
  scans, `focus`es three (`pc812`, `pc1447`, `pc2003` → `full`), lets
  the other 37 fade (`eye=none`). Annotates `dc2 done: 3 candidates`.
  `▸` → `dc3`. Log: `t1 search sparse-attn → 3 kept`.
- **t2 · delegate the skim** (don't read 100 hits yourself).
  → *does*: `call(tier=haiku, brief="skim these 60 hits, sticky any with
  an efficiency/quality tradeoff table")`. Returns a **receipt** (`jb31`);
  the skim runs in the child's throwaway context (§8/§9). `▸` stays on
  `dc3`; the driver keeps working the axes meanwhile.
  Log: `t2 call jb31 skim-for-tradeoff-tables`.
- **t3 · fan-in docks.** `jb31` completed during t2's pass → its digest
  **docks at this render** (§10 tick-boundary), a `summary`-fidelity
  neighborhood anchored at `dc3`, provenance `found · by jb31 · src
  pc4471`. Two new handles arrive as `◦` eyes.
  → *does*: `focus(pc4471, full)` (adopts one), drops the other; writes
  `dc3` (three axes). `▸` → `dc4`.
  Log: `t3 dock jb31 · adopted pc4471 · wrote dc3 axes`.
- **t4 · fork a second line.** A distinct sub-question appears (do any
  methods *compose*?) — independent enough to parallelize.
  → *does*: `spawn(tier=opus, brief="does any pair compose? write dc9",
  scope={pc812,pc1447,pc2003})`. Receipt `jb44`; detached, non-blocking
  (§9) — the child starts already looking at its briefed `pc*`. Parent
  keeps driving line A. Log: `t4 spawn jb44 composition-question`.
- **t5 · work + a tombstone rescue.** Status line: `eyes 7 · context
  review in 1`. `pc2003` (opened t1, untouched since) renders with a `†`
  — about to drop at the next crunch.
  → *does*: still needs it → `focus(pc2003, full)` refreshes its TTL;
  drafts §3.2 ¶1–2 into the deliverable, citing `pc812`/`pc4471` (→
  implicit provenance links). Log: `t5 rescue pc2003 · drafted §3.2 ¶1–2`.
- **t6 · crunch.** The bunched eviction fires (§6b): the 37 faded
  candidates + one un-rescued lens drop in **one cache break**. Rescued
  `pc2003` and the last-5 ledger survive (pinned, §7). The skill expanded
  back at t1 migrates from the tail into the skill floor **at this same
  break** (§3 sorting model).
  → *does*: drafts §3.2 ¶3. Log: `t6 drafted §3.2 ¶3`.
- **t7 · the detached child lands.** `jb44` finished; its digest docks as
  a done-annotation on `dc4` + a `◦` eye on new node `dc9` — the parent
  had moved on, so it is a fading marker, **not** an interrupt (§9).
  → *does*: reads `dc9`, decides composition earns a sentence,
  `focus(dc9, full)` and cites it. Log: `t7 dock jb44 → §3.2 note`.
- **t8 · variant, then compare.** Unsure whether §3.2 reads better as a
  table or prose.
  → *does*: `put` a **variant** `dc4'` (table version, §6d); eyes
  transfer by structural key, so both render under the same lens;
  `view='compare'` shows them diff-highlighted side by side.
  Log: `t8 variant dc4' (table) · compare`.
- **t9 · keep one, log, done.** Prose wins.
  → *does*: soft-deletes `dc4'` (renders `[gone]`; its notes survive as
  provenance, §4); marks `dc4 done:`; advances `▸` → `dc5 [open] review`.
  The mandatory log post-condition (§7) was met every tick above; a lazy
  turn would have been re-prompted. Log: `t9 kept prose · dc4 done · ▸
  review`.

**What the trace exercised:** cold-start salience (§6) → inline-read
triage + `focus`/fade (§6b) → delegated skim in throwaway context
(§8/§9) → tick-boundary fan-in (§10) → fork with an independent cursor
(§9) → tombstone rescue + bunched crunch + skill migration (§3/§6b) →
non-blocking detached return (§9) → variant/compare (§6d) → a mandatory
per-turn log (§7). No state machine, no gated steps: the persona
described the *approach*; the control flow was entirely the driver's
tool calls (§13).

### 14. Execution substrate: PoC on `claude -p`, target own-loop-local

The **render-loop** (one model pass per tick; precis re-assembles the
fisheye context *between* ticks; the transcript leaves the render but is
retained cold, §15) can be prototyped **without** a bespoke inference
loop by driving **`claude -p --max-turns 1` once per tick**: precis
assembles the prompt, Claude takes one turn, precis (as the MCP server)
records the tool calls it served (`focus`, `put`, …), then re-renders for
the next invocation. This keeps the
flat-rate **Max plan** (raw API is metered and off-Max), so it is the
right **PoC** substrate. The **target** stays own-the-loop over a
**local** model (litellm tool-call loop; pay-for-time, §5 + ADR 0046),
which the render-loop needs anyway for KV-slot control.

**Known caveats (verify or accept for the PoC):**

- **Confirm precis observes the tool calls under `--max-turns 1`** — the
  model must emit *and* Claude must execute the `focus`/`close` calls
  (hitting the MCP server) before the process exits, else the eyes are
  never recorded. The one make-or-break spike.
- **Kill ambient `CLAUDE.md` injection** (§12) — neutral cwd, no
  `~/.claude/CLAUDE.md`, or the "clean render" is silently contaminated.
- **Per-invocation overhead** — a fresh process + MCP handshake per tick
  is fine for a correctness PoC, not for throughput; it is one reason the
  local own-loop is the real target.
- **Rhythm mismatch** — the persona must be written for "one pass,
  request-for-next-tick," not Claude Code's default "loop until done."
- Server-side prompt caching still applies across identical-prefix
  invocations, so the floor stays warm even on the PoC.

### 15. Storage model: context-ephemeral, store-durable

The render is aggressive about *what it shows*; the store is
conservative about *what it keeps*. These are **different axes**, and
conflating them is what makes "the transcript is discarded" (§7) sound
lossier than it is. "Discarded" means **out of the next render**, not
**deleted from disk** — almost nothing is truly thrown away. Everything
reuses the existing substrate; the **only net-new durable schema is the
`plan` kind** (modeled on `draft`) plus a documented `meta` **convention**
on tick rows — no new mutable table.

| bit | home | reuse? |
|---|---|---|
| Thread | a `todo` ref (parent tree) | exists |
| Turn / tick (one per model pass) | a `job` ref under the thread | exists |
| Working set — eyes (extent × persistence × provenance), cursor pointer, planted-eye queue | **`job.meta` JSONB snapshot**, re-read from the previous tick each turn | meta convention |
| Route meta — model, tier, tokens, ids | `job` columns (agentlog already carries these) / meta | exists |
| Rendered prompt (immutable) | audit snapshot on the tick (JSONB / side-blob) | §4 |
| Raw completion + served tool-calls | **retained on the tick** for corpus/audit — never re-rendered, but **distilled into the ledger** (§7) | new field |
| Durable products (`put`/`edit`) | chunks in the draft / logbook | exists |
| Plan + ledger content | chunks on a `plan` ref | **net-new kind** |
| Ledger fact-lines (last-N) | *are the tick rows* — read the last N `job`s | exists |
| Cursor | a plan-node pointer in the tick meta | meta |
| Delegation brief | the child's `job.meta` | meta |
| Return digest | docks as a plan annotation (a chunk edit) | exists |
| Cross-thread message | `memory` + `MEM:transient` | exists |
| Persona | a skill file, `flavor: persona` | exists |

**Working set = a per-tick snapshot, re-read each turn.** Each tick reads
the **previous tick's `meta`** to reconstruct the eye set + cursor,
applies the turn's curation deltas, and writes a **fresh snapshot** into
its own `meta`. No dedicated mutable table and no in-place mutation — an
**append-only snapshot per tick** that is auditable and replayable, and
doubles as the **store-first reconstruction path** when a turn is killed
(re-read the last snapshot). This matches the append-only discipline of
the rest of the system (chunks are never mutated in place).

**We keep more than we show — the deliberate stance.** Nothing is *ever*
deleted by the loop; the only thing never *re-injected* is the raw
completion + prior tool-results (re-injection is the rot §7 rejects).
Retaining the raw completion on the tick is cheap (text on a `job` row)
and is exactly the corpus the framing wants ("each turn persists its
prompt/route/outcome", Context) — **audit, replay, and SFT distillation**
all read it. So the answer to "should we throw things away so quickly?"
is: **we don't.** We evict from *context* (for sharpness, §5/§6) while
the store keeps the full record; eviction is a *rendering* decision, not
a *retention* decision.

**The cold source feeds the warm view.** The retained raw completion is
also what the **ledger distills from** (§7): a Haiku pass turns each
tick's completion into the one-line fact that *does* render in the
logbook. So the tick holds the **cold, full source** (never re-rendered)
and the logbook holds the **warm, distilled view** — the raw pass is not
re-injected, but its *summary* is exactly the re-rendered continuity
line. This is why retention is not waste: the corpus row and the
continuity line are the same bytes at two fidelities.

**Cost caveat (already flagged Unresolved).** Retaining raw completions
+ prompt snapshots per tick *is* the "persist-every-turn write
amplification" line — they are the bulk. Both are **cold** (write-once,
read-rarely), so they belong in a side-blob / compressed column off the
hot `job` row, and the working-set snapshot can be **delta-encoded**
against the prior tick if it ever bites. None of this is on the model's
clock (§14 — assembly runs between ticks).

**One thing to verify before building.** The model assumes
`tick = turn = one job row` (one row per model pass). If the current
`job`/`agentlog` grain is per-*run* rather than per-*pass*, either write
a `job` row per tick or add a thin per-tick row; the `meta`-snapshot
scheme is unchanged either way.

## Consequences

- **One turn shape** across planner / web / reviewers / dreamer; every
  turn is an auditable, replayable `kind='job'` row → the routing corpus.
- **Net-new mechanisms**: a `persona` skill tag + registry; a `plan`
  kind/subtype + a whole-tree render module + promote-node-to-todo; a
  **fisheye** render (`view='fisheye'` neighborhood; **eyes on two axes**
  — *extent* `none/toc/summary/full/fidelity` + forward-biased skirt ×
  *persistence* transient/normal/pinned; richer→toc→gone decay) + the
  **7-verb base surface**
  (§6c: `focus(level)`/`search`/`put`/`edit`/`call`/`spawn`/`wait`;
  **state-changing calls receipt-returning** (effects deferred to the
  next render) but **reads return a bounded page inline this pass**
  (ephemeral, uncached), with `search`'s **orthogonal `return`/`eye`
  knobs** — `eye` plants next-turn auto-eyes on hits (default `none`),
  independent of inline verbosity; `focus` absorbs keep/close/get;
  `promote`/`note` fold into `put`) + a
  **per-persona extension registry** (`persona → +link/+tag/+supersede/
  +flag-claim/…`); a **render marker** (`◦` = inferred eye; fidelity is
  visible) + a tail **status line** ("context review in N ticks");
  inferred (auto-lens) eyes decay faster (`ttl=1`) than requested eyes,
  adopted by re-`focus`; a
  **derived-compute priority lane** for
  touched refs, with a
  bounded off-clock render-wait + `[summary pending]`/`[ingest pending]`
  fallback; a
  `MEM:transient` axis + GC sweeper for **cross-thread directed messages
  only** (the self-handoff note is folded into the living plan); the
  **logbook** (plan + ledger + cursor) carrying continuity — a
  **model-owned** `▸ you-are-here`
  cursor (stays put until moved; harness never infers it from last-edit),
  `[open]` = next, `?`/`⚠` hunch annotations, a default **`fidelity`
  eye** on the cursor (±5 full / ±10 summary / ±15 kwd, forward-biased);
  a **mandatory per-turn log post-condition** (re-prompt +
  repeat the turn if the cursor/intent was not advanced; the
  retrospective fact line is Haiku-distilled off the raw completion,
  §7); a **pinned retrospective
  ledger** — plan done/attempt annotations (§2b) + agentlog safety net,
  **last ~5 turn-reports pinned** (exempt from fisheye decay), negatives
  retained longer, adaptive N above the floor; **artifact variants**
  (clone-with-diff) + a `view='compare'` diff render +
  **eye-as-transferable-lens** (eyes carry across a variant lineage by
  structural key); a
  sibling-roster query + assembler module; affinity batching in the
  scheduler; a **storage model** (§15, *context-ephemeral,
  store-durable*): the working set + cursor as a **per-tick `job.meta`
  snapshot** re-read each turn, and the raw completion + rendered prompt
  **retained on the tick** for the corpus (audit / replay / SFT) but
  **never re-rendered** — eviction is a rendering decision, not a
  retention one.
- **Reused as-is**: ADR 0036 handles, 0037 numbering, 0033 draft chunks,
  0038 assembler, 0046 routing seam, 0044 derived lane; `resolve_relative`,
  `section_path`, `chunk_summaries`, `chunks.keywords`, `view='toc'`;
  `requested`→job / `derived_job_succeeded` / `dispatch`, `blocked-by`,
  `base_sha`, agentlog `touched` links, the skill mechanism.
- **Hard prerequisite**: ADR 0036 universal short codes must land before
  the curation surface is usable (uniform cross-kind resolvable handles).
- **Rejected**: an inline magic-text context DSL (off-distribution,
  unvalidated, injection-porous); LRU-by-usage eviction (usage is
  unmeasurable under lost-in-the-middle); a token-budget *forcing
  function* (task needs vary; the driver right-sizes via eyes); a
  worldview-dump handoff note (telephone-game ratchet) — and indeed
  **any** separate handoff note (continuity is the living plan + pinned
  ledger, §7); a **raw-transcript / scrollback window** (even
  "last 2 turns verbatim" — the whole-plan render + fact ledger dominate
  it on fidelity and cost, §7); a live working set in the cached prefix;
  a coded synthesis pipeline (emergent, §13); true thread-merge.
- **Deferred**: the transactional-subtree/undo-stack (Call staging is the
  cheap interim); the learned pre-router (shadow-mode corpus feeds it);
  the `base_sha`-mismatch retry-as-rebrief (§11, rare); control-merge; a
  **`[gone]` reason** (superseded / deleted, §4); a **cache-bust
  cleanup-hint** — when a mid-lineage edit busts the prefix, surface a
  "prefix cold; good moment to curate" return hint so a cleanup phase
  rides the break we already pay for (§4).
- **Unresolved (see proposal open issues)**: router calibration + the
  **cutover gate** from shadow→live (tied to the tuning *objective*,
  undefined and load-bearing); **audit economics are back-loaded/circular**
  (auditing driver-descent costs the tier we saved; ROI negative until
  audit tapers via the learned router); silent-bad delegation blast radius
  under receipt-default + fan-out; **curation deferred-effect timing** +
  a "recently-dropped limbo" so a re-`focus`/`keep` can reach evicted
  handles; TTL
  / warn / working-window / fan-out-width / audit-rate / concurrency+spend
  constants; turn-corpus retention (persist-every-turn write amplification).
- **Primary risk — the distributional bet.** The loop is
  *off-distribution*: models are trained on **inline** tool results, a
  **persisting** transcript, and **complete** visible history, and this
  design breaks all three. But the *functional* objection to each is
  already answered in-design: **reads return content inline, same pass**
  (the chat-native flow, which directly *shrinks* this tax), while
  **state-changing** calls are **receipt-returning** (the synchronous
  receipt *is* the "queued — renders next tick" ack, not a new turn) and
  the placed effect appears next tick **expanded in place** (§6b), and anything worth keeping — deductions, hunches,
  actions-tried — is **written to the logbook**, which is store-backed
  and **searchable**, with **writes auto-eyed** so a thread's own output
  is never a *silent* blind spot (§6). What remains is therefore not a
  correctness gap but two softer, **metacognition** risks: **(1) a
  capability tax** — inline reads cover the same-turn-*content* reflex,
  but trained reflexes still expect same-turn *effects* (state changes
  defer to the render) and a persisting transcript, so the model may
  underperform its chat-native self until the loop is in-distribution; **measure via shadow-mode A/B**, and
  let the routing corpus double as SFT data to close it; **(2) judgment
  quality** — only *un-retrieved inputs* can be silently missing (the
  model must know to `search`), and the mandatory-log post-condition
  (§7) checks the *prospective* self-report (cursor/hunches) was
  *written*, not that it is *useful* — though the **retrospective** half
  is now **Haiku-distilled from the raw completion** (§7/§15), useful by
  construction, so only the intent half remains a presence-check.
  Neither is a missing mechanism; both are limits on the model's
  judgment, to be
  **measured, not assumed away**. (Distinct from the multi-tier
  *cost*-efficiency concern — learned-router + audit economics, above.)

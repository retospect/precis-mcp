# Reading-prep loop — an adaptive, activity-driven study system

> **Status: design / proposal. Not built.** Design-of-record for the
> adaptive learning layer that sits on top of the `anki` kind (see
> `docs/design/anki-integration.md`). Ships dark when built — every slice
> is gated and additive. This file captures the shape we converged on in
> design discussion; the **open decisions** at the end are for Reto to mark
> up before slice 1 starts.

## Goal

An **adaptive, activity-driven system that preps the human to keep up with
what the system is working on.** Not a fixed curriculum — a loop driven by
*activity*: new papers acquired, drafts updated/reviewed, news, (later)
email. The system extracts the vocabulary and concepts you'd need, teaches
them in short spaced-repetition cards backed by a weekly reading supplement,
and **adapts** — cards that don't stick get re-taught a different way, the
supplement re-composes, and the whole thing surfaces as a ~15-minute morning
briefing with optional drilldown. Anki review closes the feedback loop.

One-line framing:

> **The dream keeps the *system* current; the prep loop keeps the *human*
> current.** Same activity substrate, same cadence, two audiences.

## The loop

```
  new papers acquired ─┐
  drafts updated/reviewed ─┼─▶  dream threads (existing signal)
  news / (later) email ─┘            │  nominates a reading cohort
                                     ▼
                       extract vocab + meaning (mechanical + LLM)
                                     │
                                     ▼
                   per-paper inferred glossary  ← the OBJECTIVE registry (a DAG)
                                     │
                     ┌───────────────┼────────────────┐
                     ▼               ▼                 ▼
              shared-background   perplexity      "reading this week"
                 rollup           enrichment       booklet (draft + audio)
                     └───────────────┼────────────────┘
                                     ▼
                       supporting Anki cards (disposable didactics)
                                     │
                                     ▼
                       morning briefing (15 min, drilldown)
                                     │
                                     ▼
                   human reads / reviews ──▶ Anki decay stats
                                     │
                                     └──▶ leech signal ──▶ diagnose via concept graph
                                                            ├─ missing prereq → teach it
                                                            ├─ wording bad   → re-munge card
                                                            └─ N failures    → escalate to human
```

The load-bearing insight: **Anki review is the only hard engagement signal
you get for free.** Everything adaptive hangs off it. The loop is really
"author → let the forgetting curve grade the human → re-teach what didn't
stick, informed by what did."

## The dream is the activity spine

The prep loop does **not** build its own activity harvester or scheduler. It
subscribes to signals the `dream_agent` already integrates on its 15-min
cadence:

- **New papers acquired** — ingest / acquisition.
- **Drafts updated & reviewed** — draft edits + the `structural`/`deep_review` tiers.
- **News** — the `news` kind + the existing morning briefing.
- **Dream threads** — `dream_agent` already emits `thread:` memories about
  what is live and interesting.
- *(later: email — same interface, secondary, not designed for here.)*

So the **reading cohort is emergent, not hand-curated**: what the dream is
pursuing + what just landed *nominates* papers into "what the human should be
prepped on." A `soon-reading` flag drops from *the* driver to *a* manual
boost/override. The prep loop is "the dream, aimed at the human's learning
readiness instead of the system's own knowledge" — it reads the same signals
and, instead of writing `thread:` memories, writes glossary chunks, cards,
and the booklet, on the same tick (gated/dedup'd/load-gated like the dream
already is). No new cron.

## The three-lifetime layer model

The core correctness idea. Three things with **three different lifetimes**,
and conflating them is the mistake to avoid:

| Layer | Lifetime | Substrate |
|---|---|---|
| **Objective** (concept to be understood) | **Durable** — until *mastered*, *superseded*, or *no longer read* | inferred glossary entry (a graph node) |
| **Card** (one didactic rendering of an objective) | **Disposable** — replaced when a better teaching exists | `anki` kind, `derived-from` the objective |
| **Curve** (the forgetting schedule of a card) | **Per-card** — resets when the *didactic* changes | Anki, mirrored to `meta.anki_stats` |

Consequences:

- **The objective survives card replacement.** Swapping a failing card for a
  better breakdown loses that card's Anki curve — but **zero** progress on the
  objective, because the failing card *wasn't achieving it anyway.* That is
  exactly why the curve reset is acceptable: you only reset cards that weren't
  working.
- **The glossary is the objective registry** (so slice 1 is load-bearing, not
  just reference material): the durable ledger of "what this human still needs
  to understand," marked *mastered* when its cards graduate.
- **An objective ends three ways, not one.** *Mastered* (cards graduated),
  *superseded* (a replacement concept subsumes it — a `supersedes` link, the
  same relation papers already use), or *retired* — the activity moved on. The
  retire signal is the one Reto named: **"is this still something we read?"**
  Relevance decay straight from the activity spine — an objective the
  dream/cohort no longer touches may retire **even unlearned**, so the backlog
  doesn't hoard dead vocab forever. "Durable" means *durable against card
  churn*, **not** eternal.
- **Retirement-on-irrelevance is a *learning-effort* economy, not a
  maintenance one.** Once an objective is **mastered**, keeping it costs almost
  nothing: Anki's exponential intervals mean a known card resurfaces rarely (a
  well-learned card is months/years apart). So a **mastered** objective is
  **not** retired for mere irrelevance — the maintenance load is negligible, so
  let it ride; only *supersession* (it's now wrong) removes it. Retirement
  targets the **candidate/active** (unlearned) tier, where the cost is the
  human's finite attention — dropping dead vocab there saves *learning* effort,
  which is the scarce resource. This makes decision #6 mostly moot for
  mastered work and sharp only for unlearned work.
- **Replace-policy is weighted by whether the card is earning its keep:**

  | | Failing card (leech) | Winning card (building mastery) |
  |---|---|---|
  | **Replace it?** | Freely — no curve worth keeping | Only for a *clear* didactic win |
  | **Why** | The card failed the objective | The card is *achieving* the objective; its curve is real progress |

- **The dream's drift is safe:** the *booklet* drifts with the dream, the
  *objectives* persist until mastered, *cards* churn freely in service of
  objectives. Three lifetimes, each correct for its layer.

## The concept graph

Objectives are not a flat list — a `prerequisite` (`depends-on`) link makes
the glossary a **DAG**. This buys two things:

**1. Sequencing strategy (a policy over the graph).**
- **Dive right in** — teach the target directly; pull a prerequisite in only
  when the human stumbles (just-in-time, top-down). Default.
- **Foundations-first** — teach prerequisites bottom-up (etymology/roots), so
  the target is *derivable* rather than memorized as an atom.
- Policy is per-cohort or per-concept; the etymology-first case is the
  concrete "start with the root" example.

**2. Leech diagnosis via the graph (upgrades the re-munge).**
When a card fails, "reword it" is not always the right move. First ask the
graph:
- **missing prerequisite** → teach *that* first (don't re-word the target);
- **prereqs known, wording bad** → re-munge the card (new breakdown);
- **N failures with prereqs known** → escalate: *"this concept needs you,
  not another card."*

This is a far better adaptive response than blind rewording, and it falls out
of the graph for free.

## Persistence — the objective graph is a durable asset (the textbook skeleton)

Reto's question: if we later want to make a *textbook*, do we re-derive the same
tree, or look at the (silently?) retired records? Answer: **the objective graph
persists and accretes — an append-mostly knowledge asset, not an ephemeral
working set. You mine what's there; you don't re-derive.**

- **Retirement is a soft state transition, not deletion — nothing is silently
  lost.** A retired objective is state-tagged / soft-deleted (precis's
  `deleted_at` norm), still in the DB, still carrying its `prerequisite` edges.
  "Retired" means *not actively pushed at the human right now*, never *erased*.
  Mastered objectives persist forever regardless (decision 6).
- **Later syntheses are new traversals over the accumulated graph.** A
  textbook, a review sheet, an exam is a *different composition* over the same
  persistent DAG — and the DAG's **topological order IS the textbook's chapter
  order** (prerequisite before dependent). The graph you build for the daily
  spaced-rep drip is *directly* the skeleton of the eventual book. One asset,
  many traversals: the drip and the synthesis.
- **The state annotations are the payoff of persisting.** What's mastered, what
  stuck slowly, what was a leech, which breakdown finally worked — a
  re-derivation throws all that away. A textbook can *use* it: mastered →
  brief/reference treatment; unlearned or retired → full treatment; ex-leech →
  the didactic that finally landed. Mining keeps the learning history;
  re-deriving loses it.

So: **build the graph once, keep it forever, compose over it many times.** The
per-paper glossary chunks (slice 1) likewise persist on the papers (cached until
the `paper_glossary:v<N>` artifact bumps), so even the extraction isn't
repeated. This is also why objectives-as-todos (decision 7) is safe: the todo
tree is already a persistent, soft-deleting, linkable store — exactly the
substrate a durable knowledge graph wants.

## Language drills (a card genre)

Not every objective is a research term — **language drills** are a first-class
content genre in the same machinery, and the `precis-cloze` skill already
carries the pattern (the Chinese/Japanese character scheme: char + pronunciation
paired at the same, highest cN index; concept before label). Language fits the
loop unusually well because:

- **It has the strongest prerequisite graph** — radicals → characters →
  compounds; roots/etymology → words; kana → vocab → grammar. That is exactly
  the concept-graph DAG, so foundations-first sequencing ("start with the
  etymology/root") is its native mode, not an exception.
- **It has its own drill types** beyond definition-recall — tone, conjugation,
  character↔pronunciation, production vs. recognition. These are just card
  *shapes* under one objective (a "word" objective spawns a recognition card,
  a production card, a tone card), and the ~3–4-cards-per-concept rule already
  covers them.
- **Its objectives rarely retire on relevance** (a learned word stays useful),
  which is a useful contrast: language objectives are mostly *mastered*-exit,
  research-vocab objectives are more often *retired*/*superseded*-exit. Same
  state machine, different exit distribution.

Activity source: language drills can be nominated the same way (a paper/source
in a target language, or a manual language-study project) — but the drill genre
is worth calling out because its prerequisite structure and multi-shape cards
exercise the graph and the three-lifetime model harder than research vocab does.

## Intake throttle — daily limit + backlog

The extractor produces far more candidate objectives than a human can absorb.
So objectives carry a **state machine**, and a bounded number go live per day:

```
  candidate (backlog) ──▶ active (learning, capped/day) ──▶ mastered
        │                        ├──▶ escalated (needs human)
        │                        │
        └────────┬───────────────┘
                 ▼
         retired ("no longer read")   ·   superseded (replaced by another concept)
```

`retired` / `superseded` are reachable from **either** `candidate` (a backlog
idea that went stale) **or** `active` (a concept whose topic went cold
mid-learning — the "let it finish or drop it" judgment call).

- **Daily release** promotes `candidate → active` up to a per-day limit,
  **gated by topological readiness** (don't activate a concept whose prereqs
  aren't `active`/`mastered` — unless dive-in overrides), prioritized by
  dream/cohort relevance + paper recency.
- **The backlog is the reservoir** of ideas. The briefing shows *today's N
  new concepts* (bounded, digestible — supports the 15-min goal); the
  drilldown is the backlog.
- Richer than Anki's own new-cards/day: ideas exist as **objectives before
  they are ever minted as cards.** Anki's per-deck new limit is one downstream
  enforcement point, not the throttle itself.

Triage ordering: the backlog is **prioritized** (relevance to the live
cohort/dream + prerequisite readiness + paper recency), and the daily release
just takes from the top. The list grows continuously; you "start at the top,
work down."

## Clustering & recall — cluster to *encode*, interleave to *review*

The objective list reveals clusters (conceptual, etymological, …). Is it better
to cluster them for recall? The recall literature splits this by **stage**, and
the answer is "cluster on one side, interleave on the other" — which happens to
match our two surfaces exactly:

- **Encoding / comprehension (the booklet + concept graph): cluster.**
  Semantically organized material is understood and recalled better than random
  order (organization in free recall — Bower et al. 1969). Teaching a term
  inside its conceptual/etymological family — with its root and neighbors — is
  the "start with the etymology" strategy, and it aids understanding. The
  concept graph *is* the cluster.
- **Retrieval practice (the Anki reviews): interleave.** For long-term
  retention and especially for *discriminating confusable items*, interleaved
  practice beats blocked (Rohrer & Taylor 2007; Kornell & Bjork 2008) — even
  though blocking feels easier in the moment (a "desirable difficulty," Bjork).
  **Anki already interleaves for free** (its scheduler mixes due cards across
  topics), so the review side wants *no* special clustering — don't force a
  cluster to be reviewed as a block.
- **Confusability caveat.** Highly similar items (etymological families,
  near-synonyms, look-alike characters) can *interfere* if learned together
  (associative interference) — a shared root is a mnemonic scaffold *and* a blur
  risk. For those, establish distinct traces first (separate first exposure),
  then let interleaving do the discrimination.

Net: **clustering is an authoring-side structure (booklet + graph); the review
schedule stays interleaved (Anki default).** Don't conflate "cluster to
understand" with "review as a block." *(These are well-established effects; pull
real citations into this section before building slice 2/4 if we want it
formalized.)*

## Where each piece lives (mostly exists)

| Piece | Substrate |
|---|---|
| **Activity / cohort nomination** | `dream_agent` threads + attention view (existing). `soon-reading` tag = manual override. |
| **Vocab extraction** | `utils/abbreviations.py` (`find` Schwartz-Hearst + `find_acronyms`), per-paper abbrev dict cached on `refs.meta['abbrevs']`, per-chunk `keywords_meta` (already computed corpus-wide). Nearly free. |
| **Per-paper inferred glossary** | **negative-ord synthesis chunks** on the paper (`ord ≤ -1000`) — the sanctioned lane for derived materials a registered pass may DELETE+INSERT (how `card_combined` at `-1` works). Embeddable, searchable, reversible. |
| **Shared-background rollup** | `utils/section_keywords.py` c-TF-IDF, read for *common-across-cohort* (shared background) vs *distinctive-per-paper*. |
| **The booklet** | a **`draft`** — chunk-native, embedded, exports to PDF/DOCX, has a glossary layer. One draft per reading cohort, owned by the reading **project**. |
| **Enrichment** | `perplexity-research` / `perplexity-reasoning` refs (cacheable), for shared/high-value terms only (gated). |
| **Objective registry / graph** | glossary entries + `prerequisite` links (the DAG). |
| **Cards** | `anki` kind, `derived-from` the objective + `link`ed to the booklet. `/leeches` is the recall signal (already built). |
| **Audio** | TTS render of the booklet/briefing (new — an output stage). |
| **Briefing** | the existing asa-bot morning briefing channel; reading + recall become segments with drilldown. |

The cohort container is a **project** (strategic-root todo owning a
`meta.workspace`): it owns the booklet draft, the card set, and a
`Workspace.brief`. Its *membership* is fed by the activity view, not typed in.

## The three genuinely hard parts

**1. The adaptive re-munge (the novel core).** `precis-fix` generalized:
*system-initiated* (leech signal, not a user tag) and *context-aware* (the
rewrite prompt gets the neighboring *mastered* cards, so a new breakdown can
lean on what's now known). Curve semantics: a `delete + put` mints a new ref
→ new guid → new card → **curve resets**. Per decision 2, **reset freely** —
that's the v1 default (simplicity; a reset is cheap for trivial cards). A
guid-preserving in-place edit (which would let a card re-mature faster) is a
**deferred optimization**, not in v1. Streak-capped to human escalation.
Diagnosis routed through the concept graph (see above). Ships **observe-first**
— logs every decision (leech → diagnosis → action) before it's trusted to churn
cards silently (decision 3).

**2. Read-tracking (the honest gap).** Do we know a human read the booklet?
Today: no. **Lean: don't build it in v1.** Anki review *is* the engagement
proxy; a card failure drives re-munge regardless of whether the prose was
read. Add explicit read-receipts only if the loop later needs to distinguish
"ignored it" from "read it and still failed."

**3. Churn & cost.** An always-on authoring + perplexity loop over a rolling
cohort can spend a lot and thrash cards. Gates from day one: perplexity only
for shared/high-value terms; re-munge only past the existing leech threshold
*and* streak-capped; dedup new cards against the whole collection (skill rule
1) *and* across weeks; a fresh booklet per week linked to the project (prior
weeks archived, not mutated — except the adaptive "re-explain the sticky term"
edit).

## The briefing

A ~15-minute **situational-awareness digest**, three lanes, drilldown for
detail:
- **System activity** — what the untiring collaborator did overnight (papers
  acquired, drafts advanced, findings, alerts). Raw material in the attention
  view + news.
- **Your reading** — this week's booklet gist + what's queued.
- **Your recall** — today's N new concepts, due cards, new leeches, "needs
  you" escalations.

Rides the existing briefing channel. Integrates with news but is its own
composition (news is *external*; this is *your* activity + learning state).

### Audio: two drafts over the graph, on the shipped feed

**Delivery is already built** — do NOT reinvent it. `docs/design/audio-feed.md`
ships the podcast feed (`audio_feed.publish_episode(dir, m4a, episode_id=,
title=, …, source=)` / `precis podcast add`), the TTS seam
(`precis.tts.kokoro.KokoroSynth().synthesize(text, voice=, lang=)`), and runs on
spark (`[tts]` + models + espeak-ng + ffmpeg, `PRECIS_PODCAST_DIR`, cron-tick).
Consumption (Apple Podcasts add-by-URL over Tailscale) is solved and out of
scope. Our job is only to **produce the two episodes**.

Both are `draft`s, so they flow through the **draft-narration path**:
`render_narration` (draft → voice score, stripping markup + honoring per-chunk
`meta.voice`/`meta.lang`) → `export_audio` → `publish_episode(source="reading")`.
Authoring them well is the `precis-voice` skill (write for the ear: describe
relationships not formulas, expand abbreviations, avoid slashes, lexicon the hard
words).

An **audio draft is a graph-walk rendered through a *voice profile*** — same walk
engine (§Routing), different profile (selection policy · transition tightness ·
narration voice · interaction · structure). Two standing profiles:

- **Morning brief — voice `bm_george` (British).** Clear-mind priming: today's
  new concepts + what's due + live activity + human-queued "clear-mind" items.
  Crisp, present, forward-looking; ends on a light intention. ~15 min, detail a
  tap away.
- **Evening meditation (nidra) — voice `af_nicole`.** A calm hypnagogic walk:
  **induction** (retained) → a gentle drift between *familiar* concepts along
  their closest edges (no jarring jumps, no recall prompts, relationships in
  words, *mostly* correct but never false) → a tapering **coda** (retained). It
  **alters daily but retains elements** — induction, coda, and a few anchor
  concepts recur; the path between them varies. Exposure, not testing — the
  complement to Anki's retrieval, at the other end of the day.

Contribution: each draft has an intake — the morning brief takes system content +
a human "clear-mind" queue; the nidra takes a pinnable anchor set. Both accrete
over time.

## Slice plan

1. **Per-paper inferred glossary** (negative-ord synthesis pass). Pure derived
   material, no human loop, no account writes. Proves extraction + clustering.
   *Foundation, low-risk — and it's the objective registry.*
2. **Concept graph + cohort → booklet.** `prerequisite` links; reading-project
   + weekly draft synthesis + shared-background rollup + supporting cards
   (`derived-from`). Author-only; cards sync add-only as they already do.
3. **Intake throttle.** Objective state machine + daily release + topological
   gating + backlog. Bounds the human's load; feeds the briefing.
4. **The adaptive re-munge.** Leech-driven, graph-diagnosed, context-aware,
   streak-capped; `delete + put` reset (guid-preserving edit deferred). *The
   research-y core.*
5. **Briefing segment + audio.** Composition over activity/reading/recall +
   TTS. Rides the existing briefing.

Slices 1–3 are mostly wiring existing organs; 4 is the novel one; 5 is polish.

## The datastructure — a concept graph (tier c; supersedes decision 7)

**Decision (2026-07-14, Reto): go bespoke — tier (c).** Objectives are **not**
todos; they are `concept` nodes in a personal knowledge graph, with continuous
mastery *and* embedding-native routing. This supersedes decision 7
(objectives-as-todos). The ceiling is the reason: a todo is a task with a flag;
a concept is a node in *a model of the learner's mind*, and that unlocks
capabilities todos can only cosplay (below). **Slice 1 (the glossary) is
untouched** — it is the raw feed into concepts either way.

### The `concept` node

A numeric-ref kind (like `memory`/`anki`), **embeddable** — its canonical
definition is a `card_combined` chunk, so *the concept is a vector in the same
manifold as the papers*.

- `refs.title` — the concept name/term; body/card = the canonical one-line
  definition (embeds → the vector).
- `refs.meta`:
  - `mastery` — a continuous estimate in [0,1] + `updated_at` + decay params
    (not a flag).
  - `state` — candidate / active / mastered, **derived** from mastery
    thresholds (the number is the truth; the state is a view).
  - cohort membership via a `reading:<slug>` tag.
- **Links** (the graph): `has-prerequisite` / `prerequisite-of` (the DAG —
  `Y has-prerequisite X` ⇒ learn X before Y), `analogy-of`, `contrasts-with`
  (both symmetric), `derived-from` (→ the paper chunks that define / use it =
  provenance), and concept↔card `represents` / `represented-by`.
- **Corpus-wide dedup is native**: one `backpropagation` node, not 40 per-paper
  glossary entries — matched by name + embedding proximity at promotion.

### Mastery as a field (from Anki feedback)

Aggregated from the concept's cards' `anki_stats` (interval → stability, ease,
lapses) with a forgetting-curve **time-decay** → a continuous per-concept
mastery. `state` is thresholded from it. **Propagation along `prerequisite`
edges** (mastering a concept bumps confidence in its prereqs; weakness flows to
dependents) is a refinement layered on once the base field is flowing.

> **Open decision — scalar vs event-sourced vector (fold-in from the retired
> `docs/proposals/user-knowledge-model.md`, 2026-07-14).** The concept `meta`
> currently carries a **scalar** `mastery` float (+ `mastery_updated_at`),
> mutated in place. The retired proposal argued instead for an **event-sourced
> vector**: an append-only evidence timeline as the source of truth (immutable
> events — "card ak42 passed ease 2.3", "used in conv X", "read citing paper
> Y"), with a small typed **axis projection** (exposure / retention / fluency)
> materialized over it and a scalar display-confidence computed on read. Two
> arguments for the vector: (a) the axes call for *opposite* precis behavior —
> retained-but-never-used (explain the application) vs used-but-decaying (just
> refresh) vs seen-once-never-tested (explain from scratch) — which a scalar
> can't tell apart; (b) an axis invented later recomputes over the *full*
> history, not just data collected after it existed. Cost: more storage +
> per-read projection vs a single mutable float. **This is unresolved and lands
> when this slice is built** — the scalar is the shipped default, the vector is
> the richer option; decide with real anki data in hand. Either way keep it
> "storage-liberal, action-conservative": log liberally, let only a few axes
> gate explain-vs-assume.

### Embedding-native routing (the payoff)

- **Reading-readiness as a number** — a paper's glossary terms → nearest concept
  nodes → *known / new / near-frontier* breakdown + a distance ("you know 80% of
  this; 6 new concepts, 2 one hop from mastered").
- **Shortest-path curricula** — from your mastered set to a target concept over
  the prereq DAG (+ embedding to bridge missing edges): the minimal learning path.
- **Daily review routing** — a greedy walk over *due* concepts (mastery decayed
  below threshold), hopping along edges / embedding proximity into a **connected
  narrative path**, different each day. It's *structured interleaving* (the
  smarter third mode beyond cluster-to-encode / interleave-to-review): connected
  enough to force elaborative encoding and tell a story, moving enough to keep
  the discrimination benefit — with a **tunable semantic step-size**. Recalling
  A-then-B along an edge strengthens the association *and* is a signal that can
  strengthen the edge weight — review and structure co-evolve. Constraint:
  maximize narrative coherence subject to spacing validity.

### Anki is a renderer, not the brain

The **concept graph is the source of truth** (mastery, edges, representations,
routing); **Anki is one sync adapter** — leaf cards pushed down so there's a
working phone review *today*, stats read back to feed mastery. Keep the Anki
layer thin (exactly what the `anki` slice already is). Scheduling intelligence
migrates *up* into the graph over time; Anki just renders what the router picked.
A future **graph-native phone client** (much later, Reto) reads the graph
directly — the path ("today's journey: entropy → mutual information → channel
capacity"), the mastery heatmap, the "why this card / what it connects to."
Nothing built for the graph is wasted when Anki is replaced — Anki never held the
important state.

### Revised slice plan (concept-graph edition)

Supersedes the todo-based plan below (slices 2–5 there) for everything after the
glossary.

1. ✅ **Glossary** (slice 1, shipped-dark) — the raw feed.
2. **`concept` kind + promotion** — migration (kind + `prerequisite`/`analogy-of`/
   `contrasts-with` relations); numeric-ref handler with an embeddable
   `card_combined`; promote glossary terms → concept nodes, **deduped
   corpus-wide by name+embedding**; `derived-from` provenance; `reading:<slug>`
   cohort tag. *The substrate.*
3. **Graph edges** — LLM+embedding inference of `prerequisite` / `analogy-of` /
   `contrasts-with` among cohort concepts. *The DAG.*
4. **Mastery field** — aggregate `anki_stats` → continuous per-concept mastery +
   decay; card↔concept `represents` links; state derivation. (Propagation later.)
5. **Routing** — reading-readiness (frontier distance), shortest-path curriculum,
   the daily review walk.
6. **Booklet** — a *traversal over the concept graph* rendered to a `draft`.
7. **Cards as representations** — minted from concepts (deduped), deck per
   cluster, pushed to Anki as the render target.
8. **Briefing + audio** — graph-aware (today's path + readiness + due/weak).

Risk note: mastery + routing are the research-y pieces; the kind + graph + dedup
are composition of existing substrates (embeddings, the `relations` table, chunk
provenance, the Anki stats already flowing). Ship each slice dark.

## Implementation plan — concrete build steps

Grounded against the codebase (three probes, 2026-07-14). **Headline: almost
everything is wiring existing organs; the only true greenfield is TTS.** Every
slice ships **default-OFF** (gated env flag) so it merges dark.

### Key representation decision (confirm before slice 2)

**Objectives are `todo`s, not a new kind.** The todo tree already provides
everything an "objective" needs: state (`STATUS:`/tags → candidate/active/
mastered), priority (`PRIO` column, for triage), the prerequisite DAG (links),
a project container, and views. A leaf `todo` tagged `objective`, **without**
`meta.executor` (so `dispatch` ignores it — it's not system work), under the
reading project, `derived-from`→ the paper chunk, is the objective. This avoids
a whole new kind + handler + migration. (Alternative: a `kind='objective'` ref
— cleaner semantics, much more code. Lean: reuse todo.) *This is new decision
#7 below.*

### Slice 1 — per-paper inferred glossary  *(~1–2 days, low risk)* — ✅ BUILT (worktree, unshipped)

The foundation and the objective source. No account writes, reversible.
**Built** in `src/precis/workers/paper_glossary.py` + migration
`0062_paper_glossary_chunk.sql` + `cli/worker.py` registration +
`tests/test_paper_glossary.py` (11 tests green; ruff+mypy clean). Notes vs the
plan below: the chunk kind is **`card_glossary`** (not `paper_glossary`) — the
`chunks_check` constraint requires `ord<0 ⟺ chunk_kind LIKE 'card_%'`, so a
negative-ord derived chunk must be a registered `card_*` kind; done-marker is
the chunk's `meta.glossary_version` (no separate lease table); an optional
`ref_ids=` filter supports targeted backfill.

- **New:** `src/precis/workers/paper_glossary.py` — `run_paper_glossary_pass(
  store, *, batch_size) -> {claimed, ok, failed}`. Template: `workers/
  llm_summarize.py` (`run_llm_summarize_pass`) + `workers/classify.py`.
  Claim on artifact `paper_glossary:v1` in `chunk_claims` (`NOT EXISTS`
  pattern). Per paper: read `store.defined_abbrevs(ref_id)` (Schwartz-Hearst +
  term chunks) + `abbreviations.find_acronyms` + per-chunk `keywords_meta`
  (`chunks.keywords_meta`) → one LLM call (`dispatch(LlmRequest)`) to
  **cluster + define** the terms → write derived chunk(s) at `ord ≤ -1000`,
  `chunk_kind='paper_glossary'`, via direct `DELETE … WHERE ord <= -1000` +
  `INSERT`. Embedding/keyword cascade re-runs automatically (the ord<0 lane,
  like `card_combined` at −1 in `store/_blocks_ops.py:1490`).
- **Migration:** next forward number — register the `paper_glossary` chunk kind
  (model: `migrations/0025_register_llm_summarizer.sql`).
- **Wire:** `cli/worker.py` — closure → `ref_passes`, `_pass_enabled`, gate
  `PRECIS_PAPER_GLOSSARY_ENABLED`, **agent profile** (needs the LLM call;
  movable to system if routed to a local model), priority 30.
- **Docs:** `precis-overview` (new chunk kind).

### Slice 2 — concept graph + objectives + cohort → booklet  *(~3–5 days, medium)*

- **Relation:** add `prerequisite` / `prerequisite-of` to the frozen `Relation`
  Literal (`store/types.py:24`), the inverse pair (`types.py:136`
  `_INVERSE_RELATIONS`), and seed the `relations` table (migration). Reuse the
  existing `supersedes`. Graph walk = `store.links_for(ref_id, direction,
  relation='prerequisite')`.
- **Reading project + booklet:** mint a strategic-root `todo` with
  `meta.workspace` (`handlers/todo.py:412`; auto-stamps `level:strategic` +
  `project:<slug>`); `store.create_draft(name, title, project_ref_id)` for the
  booklet; compose with `store.add_chunks(ref_id, chunk_kind, text, at)`.
- **Objective promotion:** worker/step that turns selected glossary terms into
  objective-todos (per the decision above), `derived-from`→ paper chunk, wired
  into the prerequisite DAG.
- **Booklet synthesis:** shared-background rollup (`utils/section_keywords.py`
  c-TF-IDF read for *common-across-cohort*) + per-paper distinctive terms +
  optional **perplexity enrichment** (`handlers/perplexity.py` —
  `ResearchHandler`/`ThinkHandler`, cached as refs) for high-value terms only.
- **Cards:** `put(kind='anki', …, link='paper:<slug>~<chunk>', rel='derived-from')`,
  `deck-<topic>` per cluster. ~3–4 per objective (skill rule).
- **Cohort nomination:** a query (recent papers + `soon-reading` tag + dream
  `thread:` memories referencing papers), **not** a dream_agent change — with a
  `not-reading` veto tag (decision 1). Extending `dream_agent` itself is
  optional/later.

### Slice 3 — intake throttle + triage  *(~2–3 days, low-medium)*

- **New:** `src/precis/workers/reading_release.py` — cheap SQL pass (**system
  profile**), daily-gated via an `app_state` marker (model: `paper_reconcile`'s
  `last_run`). Promotes `candidate → active` up to a per-day cap, **gated by
  topological readiness** (a `prerequisite` whose target isn't active/mastered
  blocks release, unless dive-in), ordered by `PRIO` + cohort relevance + paper
  recency. State = `STATUS:`/tags on the objective-todos.
- No migration (reuses todo columns/links).

### Slice 4 — adaptive re-munge (observe-first)  *(~3–5 days, highest risk)*

- **New:** `src/precis/workers/reading_remunge.py` (**agent profile**). Reads
  `/leeches` (already built: `handlers/anki.py:_render_leeches`, `anki_stats`).
  Per leech objective, **diagnose via the graph**: unlearned `prerequisite` →
  activate it first; else LLM re-munge the card (context = neighboring
  *mastered* cards) → `delete + put` (reset, per decision 2); streak-cap in
  `meta` (model: `plan_tick_resume_streak`), escalate past the cap.
- **Observe-first (decision 3):** log every decision (leech → diagnosis →
  action) and start in **report mode** (propose, don't write) behind the
  autonomy flag, mirroring the fixer's report→ship→full dial, until trusted.

### Slice 5 — briefing segment + audio  *(~3–4 days; RSS bounded, TTS greenfield)*

- **Briefing:** extend `workers/briefing.py` (`run_briefing`) or add
  `run_reading_briefing` — union system-activity (attention/news) + reading
  (booklet gist) + recall (today's N, due, leeches, escalations). Delivery
  reuses the `_deliver` → `pg_notify('precis.messages')` → asa_bot path.
- **Audio (GREENFIELD — no TTS in repo today):** a pluggable TTS stage
  (Piper/Kokoro on-cluster, or macOS `say` stub) → MP3, cached under a corpus
  path or a ref; `asyncio.to_thread` to not block the worker. New dependency +
  ansible install.
- **RSS to phone:** `src/precis_web/routes/briefing.py` — `/briefing/feed.xml`
  (RSS 2.0 + `<enclosure>` MP3 URLs) + an MP3 `FileResponse` route
  (`media_type='audio/mpeg'`); token-auth via the **already-present**
  `web config.auth_token` / `PRECIS_WEB_AUTH_TOKEN` (wire the FastAPI
  dependency — plumbing exists, middleware doesn't). Register in
  `precis_web/app.py`.

### Effort & risk summary

| Slice | Effort | Risk | Greenfield? |
|---|---|---|---|
| 1 glossary | 1–2 d | low | no — copy llm_summarize |
| 2 graph + booklet | 3–5 d | medium | no — wiring |
| 3 throttle | 2–3 d | low-med | no — todo + SQL |
| 4 re-munge | 3–5 d | **high** | partly — the novel logic |
| 5 briefing + audio | 3–4 d | medium | **TTS + RSS** |

~2–3 weeks for all five; **slice 1 alone is ~1–2 days** and delivers standalone
value (searchable per-paper glossaries) with zero account risk — the right
first cut.

## Open decisions (for Reto)

1. ✅ **RESOLVED — dream-nominated + manual override, yes.** Project as
   container, membership fed by the activity view. **Override is
   bidirectional**, not just additive: `soon-reading` *pins in* (boost), and
   there must also be a *veto/exclude* to *push out* — **the dream nominations
   veer strangely sometimes**, so the human is the steering authority over the
   emergent cohort. Implication: the cohort must be **surfaced for steering**
   (in the briefing) and reviewable **before** it drives expensive downstream
   work (perplexity / authoring) — a cheap veto checkpoint so a weird
   nomination doesn't spend. (Boost = a `soon-reading` tag; veto = a
   `not-reading` / exclude tag on the ref or the cohort.)
2. ✅ **RESOLVED — reset/replace freely when it helps the didactics.** Default
   is `delete + put` (curve resets); simplicity wins. Preserving the curve
   would let a card re-*mature* faster (nice), but that's an **optional later
   optimization**, justified only for cards whose accumulated maturity is worth
   protecting — for trivial cards the relearn cost (cog load) is minimal, so a
   reset is cheap. So **v1 = always delete+put**; the guid-preserving in-place
   edit path is **deferred**, added later only if mature-card churn proves
   costly.
3. ✅ **RESOLVED — v1 = slices 1–4, including the adaptive re-munge.** Pull
   slice 4 in; it's the part that makes this *adaptive* rather than static.
   Treat it as **experimental** — "let's see how it goes." So it ships in an
   **observe-first posture** (mirroring the fixer's report→ship→full autonomy
   dial): every re-munge decision (leech → graph diagnosis → action taken) is
   logged for review *before* it silently churns cards, and the leech
   thresholds / streak cap get tuned from real behavior rather than guessed up
   front. Slice 5 (briefing + audio) remains the follow-on.
4. ✅ **RESOLVED — infer engagement from Anki; no booklet read-receipts in
   v1.** The delivery channel is an **audio stream pushed to the phone every
   morning** (see *Audio: two drafts over the graph* below); the human
   *listens*, and the *Anki
   review* is the hard engagement signal that drives the loop — a card failure
   re-munges regardless of whether the prose was read, so tracking "did he open
   the booklet" buys nothing in v1. (Read-receipts remain a later add if the
   loop ever needs to distinguish "ignored" from "consumed but still failed.")
5. ✅ **RESOLVED — triage + prioritize; not every term becomes a card.** The
   objective list is a **prioritized, ever-growing backlog**: triage it, start
   at the top, work down under the daily cap (this *is* the intake throttle,
   with an explicit priority ordering). Not every glossary term is a recall
   target — triage promotes the worthwhile ones; the rest stay **booklet-only
   reference**. And the list reveals conceptual/etymological **clusters** — see
   §*Clustering & recall*: cluster on the *encoding* side (booklet + concept
   graph), keep the *review* side interleaved (Anki default), watch confusable
   clusters for interference.
6. ⏸️ **POSTPONED — punt the retirement *threshold*.** The economics already
   settled the shape (mastered = keep ~forever, near-zero upkeep; candidates
   auto-retire on staleness; active objectives finish unless superseded). The
   open bit — *how* stale is "no longer read," and who trips it — waits until
   we see real backlog growth. North star: *"ideally I know everything
   forever"* — which the maintenance economy actually permits for **mastered**
   work (exponential intervals make a forever-deck nearly free), so retirement
   only ever prunes the *unlearned* backlog, never what you've learned.
7. ✅ **SUPERSEDED (2026-07-14) — go bespoke: a `concept` kind, not a todo.**
   Originally resolved as "reuse `todo`" (fast path), but on reviewing the
   ceiling Reto chose **tier (c)**: objectives become `concept` nodes in a
   personal knowledge graph with continuous mastery + embedding-native routing.
   See §*The datastructure — a concept graph*. The todo-reuse reasoning is kept
   for history (the "subtask feel" was real), but a concept is a node in a model
   of the mind, not a task — and the amazeballs (mastery field, reading-readiness
   distance, shortest-path curricula, daily review routing) need the bespoke
   structure. §*Persistence* still holds: the concept graph is a durable,
   accreting asset (the textbook is a mastery-aware traversal of it).
```
# Proposal — user-knowledge model (`concept` kind) + morning audio brief

> **⚠️ SUPERSEDED (2026-07-14) — see `docs/design/reading-prep-loop.md`.** This
> proposal ran in parallel with the reading-prep-loop track and reached the same
> headline conclusions: a first-class `concept` kind (shipped, migration 0063),
> anki-as-renderer, typed graph edges, Kokoro TTS, and a morning audio brief on
> the podcast feed (all built or being built there). Since that design is
> **further along and has code**, it is the design-of-record; this file is kept
> for history only — do **not** implement against it. The **one** contribution
> that didn't converge — confidence as a *scalar* (shipped) vs an *event-sourced
> vector* (this doc's argument, §*Confidence model*) — was folded into
> reading-prep-loop.md's *Mastery as a field* section as an explicit open
> decision, to be resolved when that slice is built. The audio-brief delivery is
> shipped and documented in `docs/design/audio-feed.md`.

Status: ~~**proposal**~~ **retired/superseded** (2026-07-14 design session). Core
shape + decisions #1–#3 settled; #4 (feed privacy), the confidence-collapse
weights, and entity-linking remain — see *Decisions*. Evolves the card-centric
"knowledge model (north star)" in `docs/design/anki-integration.md` into a
**concept-centric** model.

## Motivation

precis should adapt how it explains to **what the user actually knows** — build
on the known, reinforce the shaky, explain the unseen from first principles. The
anki north-star framed this around *cards* (card = fact + retention signal). The
gap that framing hides: **knowledge outlasts the card that introduced it.** A
card gets reformulated, deleted, or never made — but the user still knows (or
used to know) the thing. So the durable record must be the **concept**, with the
card as one disposable instrument pointing at it.

## Two graphs (keep them separate)

- **Objective graph** — concepts + how they relate + the evidence under them.
  Mostly *emergent* from primitives precis already maintains: `refs`, the
  `links`/relation table (`cites`, `supersedes`, …), `chunk_embeddings`
  (implicit similarity edges), and the `term` registry (ADR 0052) as concept-ish
  nodes. Example already in place: a draft auto-materializes `cites` edges to the
  papers it references (`[pc<id>]` handles → `store.links_for(draft, "cites")`).
- **User-knowledge overlay** — per-`(user, concept)` epistemic state. This is the
  new subsystem. Same nodes, different edges. A concept can be well-cited and
  true while the user has never heard of it; conflating the two loses exactly
  that distinction.

## The `concept` kind

A first-class node, modeled as a **durable per-user memory-sibling** (decays and
is dream-eligible like `memory`, but with stable identity + structure):

- **Identity** — one node per concept (name + short canonical description). Stable
  so re-mentions attach to the *same* node.
- **State + confidence** — a **confidence *vector*** (not a scalar; see
  *Confidence model* below), collapsed to a `given | learning | explain | known`
  display state on read.
- **Background** — an **append-only evidence timeline** (chunks, like gripe
  comments / memory history): each state change logged with its *trigger* + date
  ("introduced via card ak42", "lapsed the card", "re-mentioned in conv X",
  "cited in draft Y"). Doubles as *show-your-work* ("why do you think I know
  this?").
- **Links** — to the anki card(s) that drill it, the papers/drafts that are
  evidence, and related concepts.

**Own decay clock (the key mechanism).** Confidence decays on the concept's own
schedule and is *refreshed* by any available source — anki pass, re-mention,
re-read. Because it's decoupled from any single card, when the card is deleted
the concept keeps decaying with no reinforcement and eventually resurfaces as
*"you used to know this, worth a refresh."* Impossible if knowledge lived on the
card. This is the payoff of "outlasts the card."

**anki is subordinate.** Not the home, not privileged — one reinforcement +
retention-measurement instrument among several (mentions, citations, re-reads),
all feeding one durable node. `meta.anki_stats` (retention/decay from AnkiWeb) is
the most *objective* refresh signal; interaction-mining is the noisiest.

### Confidence model (decision #1 — resolved: vector, event-sourced)

"Knows" is multi-faceted: the evidence sources each measure a *different* thing,
and a single number smashes them together. So confidence is a **vector**, and we
**keep all the axes**. Starting axes (extensible):

- **exposure** — encountered it (read a citing paper, saw it in a draft).
- **retention** — sticks under test (the anki forgetting-curve signal).
- **fluency** — wields it (used it confidently in their own work).

These call for *opposite* precis behavior — retained-but-never-used (explain the
*application*), used-but-decaying (just refresh), seen-once-never-tested (explain
from scratch) — which a scalar can't tell apart. Each axis also decays on its own
clock (retention fast, exposure barely, fluency with disuse).

**Event-sourced.** The append-only **background timeline is the source of truth**
(immutable raw events: "card ak42 passed ease 2.3", "used in conv X", "read
citing paper Y", "taught it to someone"). The **axis vector is a materialized
projection** over that log. This is what makes "keep them all" true: a *new* axis
invented later is recomputed over the **full history**, not just data collected
after it existed.

**Storage shape (by cadence).** A **typed axis projection** — a small,
migration-managed set of columns for the axes we actually act on — materialized
over a **semi-structured append-only event log** (JSONB payload, no migration per
event type; new feed-loops emit new event shapes freely). Migrate for the handful
of *driving* axes (rare, deliberate — the "action-conservative" gate); never
migrate for the long tail of raw signals. A **scalar display confidence** is
computed on read via a small tunable weight function (unknown-axis weight
defaults to 0, so adding an axis is strictly additive).

**Discipline:** storage-liberal (log every event), action-conservative (only a
few axes gate explain-vs-assume until others earn it). Illustrative future axes,
all just projections over the same log: **recency**, **depth** (skimmed vs
studied), **source-trust**, **teaching** (explained it — strongest mastery
signal), **self-assessment**.

### The hard part — canonical identity

Entity-linking a mention to the *same* node is make-or-break: get it wrong and
you fragment into near-duplicate concepts and the background becomes noise. Tools:
the term registry + embeddings + an LLM linker. Everything else (storage, decay,
links, timeline) precis already has primitives for; **this** is the real risk.

## Feed loops (all feed the one node)

- **anki retention** — mature card → raise confidence; lapsing → decay → concept
  re-enters the review/explain frontier.
- **cited papers / drafts** — evidence edges seed + refresh concept nodes (small
  wiring; the `cites` edges already exist).
- **dream frontier** — dreams over the explain / low-confidence concepts (same
  node set).
- **interaction mining** — an LLM pass over conv turns: confident use → toward
  *given*; a question → *explain*. Cheapest to defer to a later slice (fuzziest
  signal + the entity-linking cost).

## Surfaces

1. **Retention-aware explanation** — co-retrieve the user's concept state near a
   topic and adapt altitude (from the anki north-star; now reads concept state,
   not raw card maturity).
2. **Morning audio brief** — narrated from the *decaying / explain* concepts (the
   node set *is* the brief's content). See below.

## Morning audio brief (delivery)

Passive priming on the phone; the honest read-tracking answer.

- **Content** — the explain-list / decaying concepts, rendered to narration text.
- **Pipeline** — a `cron-tick` pass (same lane as the daily news briefing):
  render narration → TTS → `mp3` → update a podcast `feed.xml` + audio dir served
  by `precis_web`.
- **Delivery = private RSS podcast feed over Tailscale.** `precis_web` already
  serves via `tailscale serve --https=443` on melchior. Expose
  `/<podcast>/feed.xml` + enclosures there; put the Tailscale app on the iPhone;
  subscribe in **Overcast / Pocket Casts** (they take arbitrary private feed URLs
  cleanly; Apple Podcasts add-by-URL is fussier). Fully private, no public
  hosting, auto-downloads each morning. Alternative: a public feed at an
  unguessable URL (works with Apple Podcasts) at the cost of "unlisted, not
  private."
- **Read receipt** — the enclosure-GET in the feed server's access log is a
  *delivery* receipt ("it reached the phone"), upgrading "no receipt" → "delivery
  receipt". Play-through is **not** reliably reportable by podcast apps, so
  **anki review stays the real engagement signal.** No explicit read-receipts in
  v1.

### TTS — local-first (folded in)

A good **local neural TTS collapses** the earlier "`say` (private but robotic) vs
cloud API (natural but off-box)" tradeoff — cloud-quality naturalness without the
(possibly personal) brief text leaving the tailnet. On-grain with precis's
local-first + proprietary-stays-local posture.

**Decision #3 — resolved: Kokoro (native per-language voices), Piper fallback.**
The use case is partly a *language-training* instrument, and that flips the pick:
you want a **native-tuned voice per language**, not one voice *approximating*
accents. A distinct, memorable voice per language is also pedagogically good
("French = this voice").

- **Pick: Kokoro** — Apache-licensed, ~82M, high quality-per-watt, and
  **per-language pipelines with native voices** (voice + phonemizer are
  language-specific → each language sounds native). Check coverage for the target
  languages first.
- **Fallback: Piper** — MIT, fully offline, huge community voice catalog per
  language; covers anything Kokoro misses.
- **Dropped for this use case: XTTS-v2** — its one-cloned-voice-across-languages
  is the *wrong* shape for pronunciation modeling (accented, not native), plus
  non-commercial weights. Revisit only if we later want a *cloned personal voice*
  reading a mono-language brief.
- **Tiebreaker:** an ear-test — run one brief paragraph through Kokoro vs Piper
  and pick by ear; quality is voice-dependent, not a spec-sheet fact.
- Keep the **engine behind a config seam** so the choice stays swappable.

### Narration-markup seam (engine-agnostic)

None of Piper / XTTS / Kokoro parse SSML or inline `<lang>` markup within a
single synthesis call — they all take **one language (and voice) per call**. So
"multilingual markup" is done by **segment-and-stitch**, and we own that layer:

- Define a **minimal, engine-agnostic narration markup** — language spans (and
  maybe pauses/emphasis) — that the brief author (the render pass) emits and the
  **TTS adapter compiles** per engine. This keeps briefs *portable across
  engines* regardless of what any engine natively supports.
- The adapter splits text into language runs and routes **each language span to
  that language's native voice** (Kokoro/Piper), then concatenates. With the
  Kokoro decision, per-language voice-switching is the **feature**, not a
  compromise: it's exactly the native-per-language model a learning brief wants.
- **v1 pragmatics:** likely *don't* do per-run switching yet — single language,
  let the base voice handle stray names/terms. Add segment-and-stitch when you
  actually produce multilingual briefs. The markup seam is defined up front so
  authored briefs don't need reworking later.

## Decisions

- **#1 Confidence: vector — RESOLVED.** Vector (extensible axes), event-sourced
  from the append-only background, typed axis projection over a semi-structured
  event log, scalar display-confidence on read. Storage-liberal, action-
  conservative. (See *Confidence model*.)
- **#2 `concept` vs `term`: new `concept` kind — RESOLVED (leaned).** User-
  knowledge concepts exist without glossary entries; need stable identity +
  one-node dedup + append-only background a `term`/`memory` pile can't give.
- **#3 TTS engine: Kokoro (native per-language voices), Piper fallback —
  RESOLVED.** XTTS dropped for this use case (accented not native + non-commercial
  weights). Ear-test tiebreaker before committing.

### Still open

- **#4 Feed privacy** — private-over-Tailscale (Tailscale app on the phone,
  Overcast) vs public-unguessable-URL (Apple Podcasts, "unlisted not private").
  Lean: private-over-Tailscale.
- **Confidence collapse weights** — the read-time weight function (which axes gate
  explain-vs-assume, and how). Deliberately deferred: calibrate on real data;
  start naive (weighted sum, or "max with a retention penalty").
- **Entity-linking approach** — the make-or-break identity resolver (term
  registry + embeddings + LLM linker). Needs its own design before build.

## Non-goals / deferred

- Explicit read-receipts (audio fetch-log + anki cover it).
- Interaction-mining pass (defer until storage + anki loop earn their keep).
- Multi-user generalization beyond `PRECIS_OWNER` (design per-user; ship
  single-user).

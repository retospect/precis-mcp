# Capability discovery on a sprawling surface

status: draft
priority: high

Reto, 2026-09-08: *"This is kind of a sprawling surface now, how will agents
become aware of relevant capability existence? Sub-indexes?"*

The surface is 58 live kinds and 158 skill files. This item records what the
same session's dogfooding actually showed about how an agent finds a
capability, because the evidence contradicts the obvious answer.

## The evidence (one session, unplanned)

I needed a structural-design capability to model a mechanical assembly.

1. I **guessed a slug** — `precis-se-help`. It does not exist. The reply was
   `NotFound` with `options=` listing ~160 skills and a `next:` pointing at
   the flat TOC. Cost: one round trip and no closer to the answer.
2. The kind I actually wanted, **`nm`**, existed, was purpose-built for the
   exact problem, was tested and had a backlog design doc — and was
   **invisible**, dark behind `PRECIS_NM_ENABLED`. No index would have found
   it, because it was not in the catalogue at all.
3. Even had it been live, I would not have guessed it. `nm` does not say
   "hierarchical block tree with spatial envelopes" to anyone who does not
   already know.
4. Earlier in the *same session* I ran
   `search(kind='skill', q='perplexity research import deep query')`. Top hit
   was correct, with ranked alternatives. It took one call and no guessing.

**Enumeration and name-guessing failed; intent-search worked.** That is the
finding, and it points away from "more indexes".

## What follows

### 1. The primary door is intent-search, not an index — it already works

`search(kind='skill', q=...)` ranks over each skill's `summary` and its
`answers:` frontmatter list. `answers:` is literally a list of questions the
skill answers, i.e. a question-indexed capability map. This is the right
abstraction: an agent arrives holding a *goal*, never a slug.

The gap was not capability, it was **signposting** — nothing pointed at
search, and the one error message that had the agent's attention pointed at a
flat list instead. Fixed 2026-09-08: the skill `NotFound` now returns
`difflib` near-misses plus an explicit "describe the goal, don't guess a slug"
redirect to `search`, instead of dumping ~160 options.

**Highest-leverage remaining work: `answers:` coverage.** It is worth more
than any index restructure, and it is cheap and incremental — one frontmatter
line per skill, per question an agent might actually arrive with.

### 2. Sub-indexes: yes, but clustered by TASK, not by kind

The flat list fails because it is organised by *implementation* — one skill
per kind. An agent does not think "I need the `nm` kind"; it thinks "I need to
design a physical thing".

So: ~8 task-shaped clusters ("design a physical thing", "ground a claim in
literature", "run compute", "author prose", "operate the fleet"), each a
handful of pointers. Small enough to live in the always-loaded overview, and
it gives a **second hop when search misses** — which matters, because search
misses silently and an agent that gets a bad ranking has no signal that it
should look further.

Not a replacement for search. A fallback with different failure modes.

### 3. Capability *existence* is a different problem from capability *availability*

This is the sharper thing the session exposed, and no index fixes it.

`nm` and `se` were registered handlers with entry points, migrations, tests
and design docs — and an agent could not discover them, because a dark-shipped
kind is *absent*, not *disabled*. Absence is indistinguishable from
nonexistence.

- **Done 2026-09-08**: removed the per-plugin dark flags (`chem`/`bio`/
  `pathway`/`nm`/`se`). `PRECIS_KINDS_DISABLED` remains as the one general
  operator control, so nothing was lost.
- **Still to do**: make the gated-out set *visible instead of absent*. The hub
  already records `loadabilities` per kind (`runtime/dispatch.py`, consulted
  on the unknown-kind path). A "kinds that exist on this build but are off
  here, and why" view turns a silent absence into a discoverable fact — and
  lets an agent say "this is off, ask the operator" rather than "this does not
  exist".

### 4. Point-of-need nudges beat any index

The hint bus (`hints.py`, `HintBus.emit`, per-request `ContextVar`) already
exists and is already wired into every dispatch. The best moment to tell an
agent a capability exists is when it is visibly doing that thing the hard way
— hand-rolling geometry in draft prose, or hand-maintaining a component list
that `view='bom'` would roll up.

This is the only mechanism here that scales *with* the sprawl rather than
against it: an index gets worse as the surface grows, a point-of-need hint
does not.

## Order

1. `answers:` coverage sweep (cheap, incremental, highest leverage).
2. Loadability view for gated/absent kinds (small; the data already exists).
3. Task-shaped clusters in the overview (needs an editorial pass).
4. Hint rules for the common "doing it the hard way" shapes (needs
   per-shape judgement; do last, and only for shapes seen in real transcripts
   — `llm_confusion_log_mining` is the source).

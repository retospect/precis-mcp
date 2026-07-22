# Topic dossiers — standing paper classification + quest-family synthesis (ADR 0060)

How a new paper finds its way into the right standing living document, how
"not yet integrated" stays a live query instead of a batch job, and how the
weekly synthesis becomes both a shareable digest and a quiet daily heads-up.
**§1-2 (taxonomy + cascade classifier) are built** (default-OFF); §3-5
(integration link, synthesis tick, digest cast, daily lane) remain
design-of-record — see Status.

## The shape

```
ingest → paper-topic cascade (tier0/1/2) → topic: tags (multi-label)
                                                  │
                                     search(kind='paper', tags=['topic:X'])
                                     minus  paper --integrated-into--> draft:X
                                                  │
                                     topic-quest synthesis tick (weekly)
                                          │                    │
                                   revises dossier        writes quest_log
                                   `draft:X`, links        entry + links
                                   papers in                papers in
                                                  │
                                     ┌────────────┴────────────┐
                                weekly digest cast      daily-brief lane
                                (shareable, only when   (quiet heads-up,
                                 there was activity)      Reto-only)
```

## 1. Paper→topic cascade

Same three-tier shape as the chunk cascade
(`docs/design/chunk-classifier-cascade.md`), one level up:

| tier | what | model | cost |
|---|---|---|---|
| **0** | keyword/regex screen per topic against title+abstract | none | free |
| **1** | confirm / expand candidate set — **multi-label** | local cheap model | cheap |
| **2** | re-judge when tier-1 confidence is low, or when tier-0 hit zero topics but the paper doesn't look like an obvious non-match | stronger model | only the residual |

Unlike `ROLE3` (single-select, `replace_prefix=True`), topic tags are
**multi-valued**: a paper keeps every `topic:` tag tier-1 assigns. A paper
matching nothing is left untagged and, if tier-2's "novel cluster" signal
fires above threshold, queued (e.g. a `gripe` or a todo) for Reto to review —
confirm a new top-level topic, fold it under an existing one as a new
sub-tag, or dismiss.

**Idempotency / versioning** mirrors ADR 0047: a paper-level claim lease
(new artifact key, e.g. `classify:topic-cascade-v<VERSION>`, same shape as
`chunk_claims` but keyed on paper id), bump the version to force a
re-classify pass (backfills existing corpus for free — this is the
"retroactively, for all the others" the taxonomy needs whenever a topic is
added or its keyword list changes).

## 2. Taxonomy config

`src/precis/data/topics/*.yaml`, one file per top-level topic, same shape as
`data/axes/*.yaml` (closed vocabulary, versioned, prompt-carrying):

```yaml
# data/topics/healthspan.yaml
slug: healthspan
quest: <quest ref, created once>
dossier: <draft ref, the quest's own dossier>
keywords:            # tier-0 screen
  - senescence
  - inflammaging
  - inflammatory cascade
  - cytokine
  - rheumatoid
  - rheumatism
  - circadian
  - parabiosis
  - biomarker
  - neuroprotection
  - neurodegeneration
  - UV damage
  - photoaging
  - skin repair
  - healthspan
sub_tags:             # open — organizational only, not identity-defining
  - healthspan-sleep
  - healthspan-fitness
  - healthspan-biomarkers
  - healthspan-inflammation
  - healthspan-neuroprotection
  - healthspan-skin
  - healthspan-data-driven
prompt: |
  Does this paper's title+abstract concern human/animal health-span —
  aging biology, the inflammatory cascade or rheumatic disease,
  sleep/circadian health, fitness, blood/biomarker studies (including
  cross-organism transplant effects), neuroprotection/neurodegeneration,
  skin repair or sun/UV damage, or data-driven personal health? ...
```

The **top-level topic list itself is closed** (seed: `healthspan`,
`molelec`, `noxrr`, `llm-improvements`) — new entries are added by Reto, not
minted by the classifier. This is the direct lesson from ADR 0047's measured
folksonomy drift (`interest:` vs `topic:` facet duplication in this same
corpus): a closed identity layer with an open descriptive layer underneath
it, not a fully open tag space.

`noxrr` is the pre-existing catalyst-discovery quest — this file just adds
its taxonomy entry so the cascade also feeds it papers found by ordinary
ingest, on top of its own active lit-search tick.

## 3. Integration tracking

No new column, no new kind. A synthesized paper gets one link:

```python
link(kind='paper', id=<paper>, target='draft:<dossier-ref>', rel='integrated-into')
```

"Unintegrated for topic X":

```python
candidates = search(kind='paper', tags=['topic:X'])
# minus any paper already linked integrated-into → X's dossier draft
```

This is a live view — usable any time, not just materialized at the weekly
tick. It's also the natural backlog surface if Reto wants to check "what's
piled up for molelec" between synthesis runs.

## 4. Quest-family synthesis tick

One `quest` per top-level topic (created once, same as any quest: dossier
draft + WORM `quest_log`). The **existing** coordinator loop
(`src/precis/workers/job_types/quest_tick.py`) is unchanged — harvest →
review/propose → dispatch → await-heartbeat. What's new is a **second tick
body** alongside catalyst-discovery's propose-experiment body:

1. Harvest: unintegrated-papers query (§3) for this topic.
2. LLM reads each paper's chunks against the dossier's current state,
   decides what's genuinely new vs. redundant with what's already
   synthesized.
3. Revise the dossier draft (append new sections / amend existing ones —
   same DELETE+INSERT discipline as any draft edit; body chunks are
   append-only, never in-place UPDATE).
4. Log the merge: one `quest_log` entry per tick (reuse existing entry
   types — a synthesis tick is a `result`/`decision`-shaped event, not a new
   type).
5. Link each folded-in paper `integrated-into` the dossier.

`noxrr` keeps its existing propose-experiment tick body; whether it also
gets the synthesis body (to fold in passively-classified papers alongside
its own active search) or stays purely active-search-driven is an open
implementation question (ADR 0060 §"Open questions").

## 5. Cadence and output

A `level:recurring` weekly todo fires every topic-quest's synthesis tick (one
watch, fans out per topic — mirrors how other recurring watches fan out per
subject).

Two outputs, matching Reto's stated split:

- **Weekly digest cast** — new cast type, own cadence, only composes when at
  least one topic had integration activity that cycle. Reuses
  `briefing_cast.py`'s lane-union → LLM-compose → save as dated `draft`
  (`meta.cast`) → link sources back (`derived-from`) pattern wholesale; the
  "lanes" here are per-topic delta summaries instead of news/activity/recall/
  quest lanes. Shareable.
- **Daily-brief lane** — a quiet addition to the existing daily morning
  brief: "N papers classified today" / "topic X integrated Y papers" —
  Reto-only visibility, usually near-empty, fuller right after the weekly
  tick runs.

## Files

| file | role | status |
|---|---|---|
| `src/precis/data/topics/*.yaml` | taxonomy: slug, description, keywords, sub_tags | **built** |
| `src/precis/workers/classify_topics.py` | the paper-level cascade pass, mirrors `workers/classify.py`/`paper_glossary.py` | **built** |
| `src/precis/workers/job_types/quest_tick.py` | unchanged coordinator; new synthesis tick body registered alongside catalyst-discovery's | not built |
| `src/precis/reading/briefing_cast.py` | extended with a per-topic-delta lane (daily) + a new weekly digest cast type | not built |

Note on the built classifier: it deviates from §1's original sketch in one
way — no paper-level claim-lease table. Following the more recent
`paper_glossary` precedent instead: existence of a `TOPICCASCADE:<version>`
marker tag is the 'done' check, no separate lease table (the paper corpus is
orders of magnitude smaller than the chunk corpus that motivated
`chunk_claims`, and the LLM call is short). Also, tier-2 escalation is not
implemented — v1 is tier-0 keyword screen + tier-1 confirm only; add tier-2
when real ambiguity data justifies it.

## Status

**§1-2 (taxonomy + cascade classifier) built 2026-07-22**, default-OFF
(`PRECIS_CLASSIFY_TOPICS_ENABLED`). No migration was needed. **§3-5
(integration link, synthesis tick body, weekly digest cast, daily-brief
lane) remain design-of-record**, tracked in `OPEN-ITEMS.md` § "Topic
dossiers (ADR 0060)". Suggested next: the synthesis tick body (needs the
`integrated-into` link — check ADR 0054's closed-relations discipline before
adding it), then the weekly digest cast, then the daily-brief lane.

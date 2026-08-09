# Topic dossiers — unbuilt §3-5 (integration tracking, synthesis tick, cadence)

## 3. Integration tracking

No new column, no new kind. A synthesized paper gets one link:

```python
link(kind='paper', id=<paper>, target='draft:<dossier-ref>', rel='integrated-into')
```

"Unintegrated for topic X":

```python
candidates = search(kind="paper", tags=["topic:X"])
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

## Residuals (from OPEN-ITEMS)

- Weave-quest creation flow: nothing creates one end-to-end (mint quest +
  ensure_dossier + topic: tag + mark_weave_quest); the ADR-0060
  synthesis-tick path is the home.
- Weave v1 refinements: multi-place (top-1 only); claim-clustering dedup;
  review-todos parented on the quest lack a level:strategic ancestor.
- Stamp `topic:<slug>` on the dossier draft at creation/quest-binding — until
  then `view='integration'` gap lists need a manual tag.
- Synthesis tick body for topic-quests (`workers/job_types/quest_tick.py`):
  harvest unintegrated papers → merge into the dossier → log → link; decide
  whether noxrr adopts it or stays active-search-driven.
- Weekly digest cast + a quiet daily-brief lane. Reto's open design musings:
  weekly "new papers" front-matter vs a running changelog vs an
  eye-focus-like view of paras touched in the last week (he likes the
  hierarchical view).
- Rungs 7 (weekly/deep review + batching) and 8 (freshness + digest +
  contradiction/re-org) remain design-of-record.

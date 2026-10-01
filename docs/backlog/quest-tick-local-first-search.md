---
status: draft
title: quest tick literature step searches the whole local graph first and goes outside only on a miss
pillar: quests
prio: high
---

# Quest tick: search the local graph first, go outside only on a miss

## Motivation / why

Reto, 2026-10-01: ticks should write to prod, and they should "have skills
to look locally first". Today they half do. In `src/precis/quest/search.py`,
`make_acquiring_search` returns held-paper hits first, but it **also calls
Semantic Scholar and queues `PaperHandler.acquire` on every query**, whether
or not the local leg answered it. The local leg itself is
`_default_paper_search`: held **papers**, lexical only. Findings, drafts,
memories, concepts and quest logbooks are never consulted, so a tick can
pay to fetch a paper whose claim is already a signed finding hub. The tick
prompt (`src/precis/quest/tick.py`, both the materials and the inquiry
bodies) says only "emit `searches`" and teaches no order.

First seen on qu459585's first tick (2026-10-01 13:25Z), which planned a
literature search on claim provenance that precis's own nanopub and
finding-hub work partly answers.

## In scope

1. **Local leg widens** to the hybrid search across papers, findings,
   drafts, concepts and memories, scoped the way `search` without `kind`
   fans out.
2. **External leg runs only on a miss**: when the local leg returns fewer
   than N hits above the relevance floor (N and the floor are named
   constants, start at 3 and the existing floor).
3. **Prompt teaches it**: both tick bodies say the graph is searched first
   and outside sources only fill what it lacks; the step's logbook line
   reports local hits versus acquisitions per query.
4. **A local hit links like a paper hit**: `serves` the quest, bounded by
   `MAX_LINK_PER_QUERY`.

## Explicitly NOT in scope

- Changing what acquisition does once triggered.
- The HyDE stage in `run_search_step` (already local).

## Acceptance criteria

1. A query whose answer is a held finding acquires nothing (test: stub S2
   records zero calls).
2. A query with no local hit still acquires as today.
3. The logbook entry for a search step names local hits and acquisitions.

## Target + blast radius

`src/precis/quest/search.py` (`make_acquiring_search`,
`_default_paper_search`, `run_search_step`), `src/precis/quest/tick.py`
prompt text, `src/precis/workers/job_types/quest_tick.py` wiring;
`precis-quest-help` skill's research-tick section.

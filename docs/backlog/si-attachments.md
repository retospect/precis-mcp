---
status: ready
title: Supplementary information is found where attention is, fetched, and ingested as a linked ref
pillar: memory-graph
prio: high
---

# Supplementary information is found where attention is, fetched, and ingested as a linked ref

Reto's ruling, 2026-10-03 (review session 20:02Z), on the pa5303 SI
question from catalysis-selectivity-17:
1. "If we find it, we want to get and ingest it." Found SI is fetched
   and ingested — never a placeholder.
2. Discovery is targeted where attention is: papers someone opens or
   reads (web page open, MCP `get`), and papers the graph walkers /
   fisheye touch. Not a corpus-wide sweep.
3. Each SI file is its own ref, linked to the paper, and cited as the
   paper (the citation points at the parent).
4. catalysis-selectivity-17 (quest qu164903, NO→NH3 on Pd(111)) WAITS on
   the auto-ingestion of pa5303's and pa166889's SI; he accepts one to
   two build cycles and will not hand-drop. This item blocks a quest.

## Motivation / why

`refs.pdf_role` allows `supplement` but nothing writes it
(`src/precis/ingest/pipeline.py` hardcodes `main`); `fetch_oa`
(`src/precis/workers/fetch_oa.py`) collects only the main PDF from its
ten sources; an SI PDF dropped without a sidecar carries no DOI, misses
`probe_existing` (`src/precis/ingest/db_writer.py`) and mints a junk
stub. For paywalled papers the SI is often the only full text available,
and the quest above needs the SI's data, not the abstract.

## In scope

Build 1 (unblocks the quest):
- SI discovery for one paper: Crossref `relation`/`component`
  entries, Unpaywall/publisher landing-page patterns (ACS `suppl_file`,
  Elsevier `mmc*`, RSC `suppdata`, Wiley `downloadSupplement`), through
  `safe_get` only.
- Ingest each SI file as its OWN paper-kind ref with
  `pdf_role='supplement'`, linked to the parent (`supplements` →
  parent; parent `has-supplement` → SI), title "Supporting Information:
  <parent title>", the parent's DOI recorded as the parent's, not the
  SI's own identifier. Citation resolution for an SI ref returns the
  parent's citation (ruling 3).
- An explicit trigger: `put(kind='paper', id=<slug>, mode='fetch-si')`
  (or the CLI equivalent) that queues the discovery+fetch for that
  paper; run it for pa5303 and pa166889 on deploy and record the result
  in catalysis-selectivity-17.
- Sidecar `role: supplement` + `ref_id` for the hand-drop case stays
  supported.

Build 2 (attention trigger, ruling 2):
- The same queueing fires once per paper, deduplicated, on: web paper
  page open (`precis_web` items route), MCP `get(kind='paper')`, and a
  graph-walker / fisheye touch (`knowledge-mesh`'s walker hooks). A
  paper is tried once; a miss is recorded on the ref
  (`meta.si_checked_at`, `si_found: n`) so attention does not re-fetch.
- `fetch_oa` lane priority: attention-queued SI fetches go ahead of the
  stub backlog (a priority bump, not a sweep).

## Explicitly NOT in scope

- Corpus-wide SI discovery.
- Parsing SI tables/figures into structured records (the SI's chunks
  enter search like any body text; that is enough for the quest).
- Changing how the main-body PDF is chosen.

## Acceptance criteria

- After deploy, `fetch-si` on pa5303 and pa166889 creates one SI ref per
  SI file found, linked to the parent, with chunks searchable; `search`
  hits in SI text cite the parent paper. Result (found / not found per
  paper, with the source used) written into catalysis-selectivity-17.
- A paper with no SI: one check recorded, no ref minted, no re-check on
  the next open.
- Build 2: opening a paper page or `get`ting it queues exactly one
  check; the fisheye touch does the same; the lane runs attention SI
  fetches before stub fetches.
- Tests: discovery per publisher pattern (fixtures), linked-ref mint +
  citation-to-parent, dedup on repeat attention, sidecar role, lane
  ordering.

## Target + blast radius

`src/precis/workers/fetch_oa.py`, `src/precis/ingest/pipeline.py`,
`db_writer.py`, `fetch_sidecar.py`, the paper handler (`put` mode),
`precis_web/routes/items.py` (open hook), fisheye/walker touch hook,
`precis-paper-help` skill. Blast radius: every paper open in build 2
can queue a fetch — the dedup mark on the ref is what keeps that from
becoming a sweep; a publisher pattern that matches a non-SI link would
ingest a wrong PDF as a linked ref (mint only from links whose anchor or
URL says supplement/supporting/ESI/mmc).

## Open questions / decisions log

- Decided (Reto 2026-10-03): always ingest when found; attention-targeted
  discovery; separate linked ref per SI file, cited as the paper; quest
  waits, one to two builds accepted.

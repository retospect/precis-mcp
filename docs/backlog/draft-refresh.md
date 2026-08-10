---
status: draft
title: draft_refresh: staleness-driven continuous section refresh for living drafts
---

# draft_refresh: staleness-driven continuous section refresh for living drafts

## Motivation / why

Long-lived drafts (the "big book", dr42995) should improve continuously without an operator queuing work. No todo pile: a recurring pass finds the *stalest section* of an opted-in draft every few hours and mints exactly one bounded refresh job for it. Workers do the rest.

## In scope

**Staleness clock — free, no schema change.** Body chunks are append-only; a section rewrite is DELETE+INSERT, so the new rows get fresh `created_at`. Therefore `min(created_at)` over a section's body chunks *is* the section's "last refreshed" timestamp. Sections are delimited by `chunk_kind='heading'` (the smartdraft outline convention, `src/precis_web/smartdraft.py` heading-tree folding) and addressable the same way `taproot_backfill` scopes are (heading anchor / `dc<id>` via `draft_handler._scope_chunks`).

**Opt-in + config via ref meta** (pattern: recurring-todo `meta.schedule`, `src/precis/workers/schedule/parse.py`):
```
meta.draft_refresh = {"enabled": true, "staleness_days": 30}
```
`staleness_days` defaults to 30; only drafts with `enabled` are scanned.

**Scheduler cadence** — new `Cadence` entry in `src/precis/workers/scheduler.py::CADENCES` (`draft_refresh_scan`, interval ~4h, off-minute; fleet-exactly-once via the existing scheduler lease). At fire time: for each opted-in draft, compute per-section staleness, pick the single stalest section older than threshold, and mint ONE job:
```
put(kind='job', job_type='draft_refresh',
    params={'scope': '<section anchor>', 'draft': '<slug>'},
    idem_key='draft_refresh:<slug>:<anchor>:<min-created_at-date>')
```
The idem_key includes the section's current min-`created_at` date, so a section is never double-poked: after a successful rewrite the date changes (new chunks), naturally re-arming it; a failed/stuck job dedups until resolved. One job per fire = a continuous trickle, bounded by design (gr191337 lesson: never unbounded scope on the serial `claude_inproc` lane).

**The job** (`draft_refresh` job type, registered in `src/precis/workers/job_types/`, executor `claude_inproc` on the melchior agent worker, LLM at `Tier.BIG` → `llm.chain.big` → local big model on castor/pollux). Per section:

1. Assemble a fisheye workspace: the section's full text, neighbors as outline/summaries.
2. Evidence gathering: the backfill view for the section (missing-citation recall, `src/precis/backfill/`), stub ranking (S2-enriched refs.prio, shipping separately), existing claim-hub (`[fi]`) evidence.
3. New-paper seeking: run the lit-search leg; genuinely new relevant papers get queued for ingest (bounded count per run).
4. Reviewer + rewrite pass: Tier.BIG dispatch — critique, then rewrite the section (tightened prose, new citations woven, stale claims flagged/updated, missing subsection stubs proposed).
5. Apply: DELETE+INSERT the section's body chunks (never in-place UPDATE — the embedding/summary cascade must re-run). Cite upgrades ride the taproot pass conventions ([pc]/[pa] → [fi]).
6. Record process memory: append a logbook entry to the owning quest's `quest_log` (what changed, what was tried and failed, what's proposed next). The book itself stays pure content.

## Decided: book vs dossier

**The book is the deliverable; the quest dossier is process memory.** They stay separate. The quest machinery already has the needed split: a narrative dossier whole-rewritten per tick, a pinned ledger chunk that survives rewrites, and an append-only `quest_log` logbook. draft_refresh writes outcomes/intentions to the logbook+ledger and content to the book — never meta-commentary into the book.

Dossier hygiene shipped alongside this spec: the pinned ledger is a nested attempt tree (`precis.quest.dossier.add_attempt`/`mark_attempt`) and the narrative rewrite passes the growth-ratchet gate (`precis.quest.narrative_budget` — reusable, owner-agnostic). draft_refresh writes its per-section process notes into that attempt tree on the owning quest (one branch per section) and MUST run its own section rewrites through the same gate.

## Explicitly NOT in scope

- **Logbook/dossier compaction.** The narrative dossier needs none (wholesale rewrite each tick). The `quest_log` logbook is append-only with no trimming today and grows unbounded — acceptable for now; a roll-up (summarize entries older than N into one digest entry) becomes worthwhile once refresh runs every few hours. Tracked in Open questions until it hurts.
- **Changes to the quest_tick loop itself** — draft_refresh is a sibling pass, decoupled from quest activation (see Open questions).
- **Autonomous creation of new sections** — first cut proposes them in the logbook only (see Open questions).

## Acceptance criteria

- A draft with `meta.draft_refresh.enabled` and a section whose min chunk `created_at` exceeds `staleness_days` gets exactly one `draft_refresh` job within one cadence interval; re-firing the cadence does not mint a duplicate (idem_key holds).
- A completed job leaves the section's body chunks rewritten via DELETE+INSERT (fresh `created_at`, embeddings/summaries re-cascade), and a logbook entry on the owning quest.
- A draft without the meta flag is never touched.
- The job is section-scoped and bounded: one section, capped new-paper queue, one Tier.BIG dispatch chain — it cannot monopolize the serial lane (gr191337 regression guard).
- Unit tests cover: staleness computation from chunk `created_at`, section selection (stalest-first), idem_key re-arm after rewrite, opt-in gating.

## Target + blast radius

- **New code:** `src/precis/workers/scheduler.py` (new cadence entry), `src/precis/workers/job_types/draft_refresh.py` (job executor), draft-section scoping in the draft handler, quest logbook API.
- **Modified code:** `src/precis/workers/job_types/` (register `draft_refresh`), quest-tick machine (logbook append), possibly `src/precis_web/smartdraft.py` for section-boundary queries.
- **Tests:** staleness computation, section selection, idem_key re-arming, opt-in gating; integration test for end-to-end job dispatch and section rewrite.

## Open questions / decisions log

- **Logbook roll-up/compaction policy** (deferred until volume hurts — append-only is acceptable for now).
- **New-paper ingest budget per run** (start: ≤3 queued papers?).
- **Should a refresh be allowed to *create* a new empty section** (discovery of a missing topic) or only propose it in the logbook for operator approval? (Lean: propose-only at first.)
- **Whether qu161907 must be STATUS:active** for its book to refresh, or the meta flag alone suffices. (Lean: meta flag alone — decouple from quest tick.)

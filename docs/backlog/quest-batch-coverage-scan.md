---
status: draft
title: "quest: one op that fans a surface_coverage_scan across a named list of candidates and models"
pillar: quests
prio: low
---

# Quest batch coverage scan

Source: gr491442 (`idea`), split from gr486841 (done) whose comment 3
flagged "a real batch op is a design call". Related: the opt-in
`meta.surface_anchors` rung (`src/precis/quest/surface_anchors.py`,
`docs/backlog/surface-pourbaix-staircase-optimizer.md` wave B).

## Ruling status

The task that filed this item says Reto ruled option (a) for gr491442. No
comment on gr491442 or gr486841 names the options or the ruling (gr491442
has none). **Reto decides: paste the option (a) text here** before this
goes `ready`; §Design below is written from the gripe text only and is the
shape the batch op must take, not a choice between options.

## Problem

Two ways to scan candidates exist today, and neither fits "these N
structures, these models":

| Path | Cost for a named list |
|---|---|
| `meta.surface_anchors: true` on the quest | `dispatch_surface_anchors` mints one scan for **every** eligible candidate (reached `neb`, or on the merged front). gr486841 measured ~227 jobs on qu164903. No way to name a subset. |
| One `put(kind='job', job_type='surface_coverage_scan', params={'structure_ref': ...})` per (candidate, model) | Documented recipe, skill `precis-pathway-help` "Scan a candidate's coverage anchors". N × M puts, each with an explicit `idem_key`; the caller re-derives `scan_idem_key(structure_ref, model_index, config)` by hand. |

## In scope

- One quest-tier op taking `candidates` (structure ids; named list) and
  optional `models` (indices into `config.mlip.models`; default the config's
  first), that mints the same jobs the per-candidate recipe mints.
- Reuse, do not fork, the existing mint path: `_request` (params + idem key),
  `_resolve_scan` (retry generations), `_mint_scan_job`, so a batch scan and
  a `surface_anchors`-minted scan of the same candidate share one idem key
  and never duplicate.
- Honour the same fail-closed rules `_request` has: no `reaction_config`, no
  `geom_hash_c` ⇒ a per-candidate skip note, not an error for the batch.
- A reply listing, per (candidate, model): minted, already live or done
  (dedupe hit), or skipped with the reason.
- Parent each job on the candidate structure (so `view='pourbaix'` picks it
  up) and link it to the quest, as the recipe does.
- Skill text: replace the N-puts recipe in
  `src/precis_pathway/skills/precis-pathway-help.md` with the batch call; keep
  the one-job recipe for geometry no structure holds.

## Explicitly NOT in scope

- Changing the `surface_anchors` mint rule (neb-reached or on the front).
- New scan physics, adsorbate sets or coverage grids (the quest's
  `reaction_config` carries those).
- Retry policy: `MAX_SCAN_RETRIES` and the fresh-seed rule (Reto 2026-10-16,
  ruling 3a) apply unchanged.

## Acceptance criteria

- A call naming 3 candidates × 2 models mints 6 jobs, each with the
  `scan_idem_key` the one-job recipe would derive.
- Repeating the call mints nothing and reports 6 dedupe hits.
- A candidate with no `geom_hash_c` is skipped with a note; the other
  candidates still mint.
- A quest with `surface_anchors: true` and a batch call for an already
  scanned candidate mints nothing for it.
- A `structure_ref` that is not a (111) slab or pins no atoms is refused with
  the existing `validate_submit` reason, per candidate.

## Target + blast radius

`src/precis/quest/surface_anchors.py` (shared helpers),
`src/precis/handlers/` quest verb surface, `src/precis_pathway/coverage_job.py`
(`scan_idem_key`, `validate_submit`), `precis-pathway-help.md`. Core may not
import `precis_pathway` (`tests/test_plugin_import_boundary.py`): the op
mints by job-type name as `SCAN_JOB_TYPE` does. Each minted scan is cheap
(about 1 min on MACE, `surface_anchors.py` docstring) but a long list is a
long queue: the op must cap the list.

## Open questions / decisions log

- Reto decides: the option (a) ruling text (see §Ruling status).
- Reto decides: the surface: a new `quest` verb arg, a quest meta key such as
  `meta.surface_anchor_candidates` read by `dispatch_surface_anchors`, or a
  tool-level helper. A meta key keeps the mint inside the tick and the idem
  rules; a verb call is immediate.
- Reto decides: the cap on candidates × models per call.
- Reto decides: whether a named candidate that has not reached `neb` and is
  off the front may be scanned (the gripe asks for exactly that).

---
status: draft
title: 45 claim hubs have a refs.title that differs from their ord=0 body chunk — search and dedup run on the wrong string (297 was the contaminated-predicate count)
model: sonnet
---

# One hub in five says two different things

Measured 2026-08-19 over the live claim cohort (`kind='finding'` +
`TAPROOT:claim`, 1,524 hubs):

| | hubs |
|---|---|
| `btrim(title) = btrim(body)` | 1,227 |
| **diverged** | **297 (19.5%)** |
| no `ord=0` chunk at all | 0 |

Found while picking an exemplar hub, not by any check — nothing in the
codebase compares the two.

## APPLIED 2026-08-21 — 42 repaired on prod, 3 held

Ran against `precis_prod` (Reto authorized the writes), via
`~/precis-experiments/taproot-divergence-45-2026-08-21/repair.py` — a one-off,
**not** a `lint --fix` arm, since the "never wire this into `--fix`" directive
below is about who makes the call, and this run is that call made once for one
measured cohort.

Result: **42 repaired, 0 failed.** Corpus-wide strict divergence went
45 → **3**, and the 3 remaining are exactly the held-back rows
(fi190976, fi191129, fi191134). Each repaired hub now has one live `ord=0`
`finding_body` chunk whose text equals `refs.title`.

### Every one of the 42 moved its `pub_id` — which is a *correction*, not churn

Predicted zero: `refine_claim_sentence` was called with the hub's existing
title, so the sentence input to `make_taproot_hub_paper_id` was unchanged.
All 42 nonetheless inserted a new `pub_id` row. The reason is the divergence
itself — the stored `pub_id` was minted from the *original* sentence, the one
that still matches the **body**; the title drifted afterwards via `approve()`'s
old direct-write door. So the identity of a diverged hub tracked neither
current string, exactly as this document's opening section warned, and
re-deriving it from the repaired sentence is the fix rather than a side effect.

Old `pub_id` rows are **kept as aliases** (the door's contract — draft prose
citing `[<old pub_id>]` keeps resolving); hubs now carry 2–4 `pub_id` rows.
Nothing is published, so this was free to do now. It also means step 5 of
`nanopub-corpus-remediation.md` (`pub_id` re-hash) is already done for these 42.

### Transient: the 42 have no body embedding until the embedder catches up

`replace_body_chunk` is DELETE+INSERT, so each repaired hub's new chunk starts
with no embedding — all 42 confirmed awaiting one. Until the pass reaches them,
their ANN blocking entry is *absent* rather than *wrong*, which is a temporary
regression in dedup recall for those hubs specifically. Re-check before
trusting a dedup sweep over these 42.

It clears fast, though: a first pass measured the corpus-wide unembedded count
at 182,860 and concluded ~9 days at the embedder's ~800/hour. That was wrong —
**182,415 of those are `chunk_kind='references'`, which
`EmbedHandler.skip_chunk_kinds` drops at the claim query by design** (tagged at
ingest by `pipeline._retag_references`; bibliography lines are retrieval noise).
The real queue is ~540 chunks — 424 `paragraph`, 52 `job_summary`, and these 42,
which are the *only* unembedded `finding_body` chunks in the corpus. One pass.

Worth keeping as a measurement lesson, because it is the same error as the
`prose_marked_hubs` bug fixed the same day (`8d985d2b`): a population counted
without the filter its consumer applies. There the query was narrower than the
gate's reach; here it was wider than the worker's. Both directions mislead —
match the predicate to the consumer, every time.

Snapshots for reversal: `before-snapshot.json` (prior body text + `pub_id`s per
hub) and `apply-result.json` in the same directory.

## Re-measured strict 2026-08-21 — it is 45, and there is only one cohort

The count above used `kind='finding'` + `TAPROOT:claim`, which sweeps in the
~280 chase-tree findings that never mint. Adding `STATUS:canonical` (the real
claim-hub predicate — `taproot/canon.py::claim_hub_predicate_sql`):

| | hubs |
|---|---|
| strict claim hubs | 1,249 |
| **diverged** | **45 (3.6%)** |
| of those, body longer | **3** |
| of those, title longer | 42 |

By `created_at`: 2026-08-03 (5), 2026-08-04 (37), 2026-08-17 (3). **Zero June.**

So the whole ~209-hub June cohort below — the half that needs the *opposite*
repair — consists entirely of non-canonical chase-tree rows. They never mint,
and their bodies are internal (see "the body is internal", below).

The 3 remaining body-longer rows are all 2026-08-04, i.e. the same August
incident. Length is not evidence of authority — but reading them in full (not
the truncated prefixes an earlier pass judged from) they are **not** uniformly
title-authoritative, and two must not be repaired mechanically:

| hub | verdict |
|---|---|
| fi190976 | **title-authoritative, safe.** Title is the claim (max fullerene coverage at 1000 °C); body is a narrative recap attributing to Nasibulin et al. over a broader 1000–1150 °C process range. Nothing lost. |
| fi191129 | **do not overwrite — the body holds a second atom.** It ends "…with the width of this plateau set by the length of the neck connecting tube and fullerene", a clause the title does not carry. This is a decomposition candidate (`conjunct-of`), not a divergence repair. |
| fi191134 | **contradiction — adjudicate against the source.** Title: higher defect concentrations "reduce the bandgap back toward zero" (non-monotonic). Body: "the degree of opening scaling linearly with defect content" (monotonic). These are different physical claims; one is wrong. Repairing either direction enshrines a possibly-false claim. |

Full text of all 45 exported for review:
`~/precis-experiments/taproot-divergence-45-2026-08-21/review.md`.

So the run is **42 mechanical + 3 held**, not 45. That the three exceptions
are all *content* problems rather than formatting ones is the same lesson the
original two-cohort analysis taught, arriving by a different route: the cheap
signal (length, date) sorts the population, and the expensive one (reading it)
is still required at the boundary.

**Consequence: for real hubs this is one cohort in one direction — chunk ←
title, which is exactly what `refine_claim_sentence` already does.** The
opposite-repair hazard, and the ordering constraint it forces in work item 4,
apply only to chase-tree rows. Both are recorded below as originally written;
read them as describing the permissive population.

## Why this is worse than a cosmetic split

**Embeddings are computed from the chunk, not the title.** So for 297 hubs:

- semantic search and `view='toc'` match against text no human reads;
- the ANN blocking step in `taproot/canon.py::block` — the first stage of the
  dedup cascade — compares the *chunk*, so two hubs whose titles are near
  identical can fail to block if their chunks diverged, and vice versa.
  **This got sharper on 2026-08-20**, when `block()` was repointed from the
  `card_combined` card (`ord=-1`, 12% coverage) to `finding_body` (`ord=0`,
  100%) — see `claim-hub-dedup-sweep.md`. Coverage went to 100%, but for these
  297 the string it now reliably retrieves is the *wrong* one. Fixing coverage
  promoted divergence from a latent defect to the live limiter on dedup
  precision, which is why this sequences ahead of the sweep;
- the corpus lint triage in `nanopub-corpus-remediation.md` ran over
  `refs.title`, so for one hub in five its verdict describes a different
  string than the one search sees.

`pub_id` is derived from the sentence at mint time, so a diverged hub's
identity may correspond to neither current string.

## Two incidents, not one — and they need opposite repairs

Grouping the 297 by `refs.created_at`:

- **~209 hubs, 2026-06-15 → 2026-07-04** — body is *longer* than title in
  every case (0 title-longer). Pre-dates `refine_claim_sentence`; the title
  was a short label and the chunk carried the real sentence.
- **~42 hubs, 2026-08-03/04 and 2026-08-17** — title is *longer* (34 of 38 on
  08-04 alone). This is the taproot regrounding pass enriching `refs.title`
  without syncing the chunk.

So the authoritative side differs by cohort: **title** for the August set,
**body** for the June set. A single blanket rule would destroy content in one
group or the other — the same trap as the 200-char repair, where restoring
frozen hubs from the chunk would have reverted 21 deliberate reviewer
rewordings (root-caused and repaired 2026-08-17, 9d0b9206; the two hardening
residuals live in `mcp-staleness-title-roundtrip-guards.md`).

Example: fi191126 — title 224 chars, body 115, both coherent sentences, the
title strictly richer.

## Root cause is the same shape as the truncation bug

A writer that updates `refs.title` without going through
`taproot/hub.py::refine_claim_sentence`, which is the only door that updates
all three sites (title, `ord=0` chunk, `pub_id`) in one transaction. The
generic `store/_refs_ops.py::replace_ref_text` is not the culprit — it is
reachable only from `handlers/quest.py` and `handlers/todo.py`.

### Swept 2026-08-21 — no live bypassing writer remains

The regrounding pass was the standing suspect. It is not the writer *now*:
`taproot/reground.py` contains no reference to `title` at all. Swept the rest
of the hub-writing surface, and every remaining path either routes through the
door or never touches a hub title:

| path | verdict |
|---|---|
| `taproot/reground.py` | no `title` reference at all |
| `store/_blocks_ops.py::set_ref_title` | bare title UPDATE, no chunk sync — but called **only** from `handlers/memory.py`, never a hub |
| `taproot/{apply_migrate,backfill,directed}.py` | only ever set *todo* titles (review-merge tasks) |
| `workers/hub_refine.py` | reads title, computes `claim_sha`; writes via the door |
| `cli/taproot.py`, `nanopub/mint.py` | call `refine_claim_sentence` explicitly |

The August cohort is most likely residue of `approve()`'s title-override door,
which wrote titles directly until it was routed through `refine_claim_sentence`
on 2026-08-20 (see `acquisition-marker-lives-in-the-wrong-place.md`, "How it
surfaced"). That door is fixed.

**Consequence: work item 1 is closed and the repair is no longer temporary.**
A cohort repair run now will not re-diverge. Note this is a point-in-time
sweep, not a structural guarantee — `set_ref_title` still exists as an
unsynced title writer one import away from a hub path, and nothing fails
loudly if a future caller reaches for it.

## The body is internal — it is never published

Traced 2026-08-19. `nanopub/evidence.py::HubBundle.sentence` is
`hub_ref.title`; the artifact, AIDA URI, `claim_sha` and drift all read the
title. `assemble.py` never touches the body. `bundle.body` is referenced in
**exactly one place in the codebase**: `nanopub/gates.py` (~line 216), the
acquisition-marker hearsay gate.

So the `ord=0` chunk has two jobs, both internal: embedder input, and that one
gate. Nothing about it is externally visible. This is what makes the
"one authored sentence, reassembly on export" design safe — see
`nanopub-corpus-remediation.md`.

### Precondition: migrate the 6 acquisition markers first — SATISFIED

**Status 2026-08-21: satisfied on prod — this no longer blocks the repair.**
All six marker-carrying rows already carry `meta.primary_source_unheld`, so the
gate's structural `declared` arm covers every one of them and deriving the body
chunk from the title can no longer silently disable it.

Two caveats worth carrying, since the table below is what made this a
precondition:

- the six are **not canonical claim hubs** — they carry `TAPROOT:claim` without
  `STATUS:canonical`, i.e. chase-tree rows. The 2026-08-19 measurement used the
  permissive predicate, so "6 hubs" overstated what they are;
- the dry run that reports this was itself scoped to canonical hubs and so
  could not see them. Fixed 2026-08-21 — full account in
  `acquisition-marker-lives-in-the-wrong-place.md`.

The structural replacement
shipped 2026-08-20 — `gates.check_primary_source` now has three structural arms
(`derived` / `awaiting` / `declared`) ahead of the prose one, and the migration
off prose is a dry-run-by-default backfill:

```
precis nanopub backfill-unheld            # dry run: lists the hubs + matched marker
precis nanopub backfill-unheld --apply    # stamp meta.primary_source_unheld
```

Its empty listing **is** the retirement test. Full detail and the teardown list
in `acquisition-marker-lives-in-the-wrong-place.md`. Everything below records
why the precondition exists.

The hearsay gate reads `body` **because** it currently differs from `title` —
its own comment says "title alone misses the harvester's 'not in corpus'
note". Measured over the live cohort:

| | hubs |
|---|---|
| `ACQUISITION_MARKER` matches **body** | 6 |
| matches **title** | **0** |
| body-only | **6** |

Deriving the chunk from the title would silently disable that gate for all
six. The gate blocks a hub from being grounded in a *citing* paper when its
own prose admits the primary source was never ingested — a real
provenance check, not a nicety.

The fix is not to keep the divergence but to stop encoding a machine-readable
fact as prose: move "primary source not in corpus" to a tag (or the existing
hanging-mint path) so the gate reads it structurally. That is strictly better
than today — `ACQUISITION_MARKER` is a regex over free text
(`not (yet )?in (the )?corpus|needs? acquisition`), so a reworded note like
"primary not yet acquired" already slips past it.

~~**Do not derive the body chunk until those 6 are migrated.**~~ Migrated —
see the status note above.

## Work

1. ~~**Find and fix the writer.**~~ **Done 2026-08-21** — swept, no live
   bypassing writer remains (see above). The repair is no longer temporary.
2. **Repair 42 of the 45 strict hubs: chunk from title, one direction.** The
   June cohort that motivated "per cohort, not globally" is not in the strict
   population at all (see the re-measure above). **Hold fi191129** (second
   atom — route to the decomposition pass) **and fi191134** (title/body
   contradict on the physics — needs the source paper); fi190976 is safe. Re-derive `pub_id` after, and re-run the duplicate
   scan, since collapsing divergence can surface hidden duplicates (this
   already happened twice: fi191259/191268 and fi191179/191260).
   Chase-tree rows are out of scope: they never mint, and nothing publishes
   their bodies. If they are ever repaired it is a separate, lower-stakes pass
   that still needs the per-cohort split.
3. **Detection has landed** — `title-body-divergence` and
   `missing-body-chunk` in the `precis taproot lint` cohort sweep, reporting
   only. Do not add either to `--fix`; see the opposite-repair argument above.
4. ~~Sequence this **before Phase 3.1 (notation normalization)**~~ —
   **dissolved 2026-08-21 for hubs.** The constraint was: `refine_claim_sentence`
   sets the title *and derives the body chunk from it*, so normalizing a
   June-cohort hub would overwrite its good body with the normalized short
   label. There are no June-cohort *hubs* — all 45 are title-authoritative, so
   deriving the chunk is already the correct repair and normalization can run
   in either order. Kept here because the reasoning still governs any future
   pass over chase-tree rows.
   Re-approval comes after both, per the drift-ordering constraint in
   `nanopub-corpus-remediation.md`: repairing a title once its `claim_sha` has
   frozen re-triggers gate #14.

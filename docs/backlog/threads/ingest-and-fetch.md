# ingest and fetch — acquisition and extraction fidelity

## Resume

- **Pillar:** memory-graph
- **Next:** item 3’s still-bodiless policy pass and close td461154; then the stub-queue policy decision (item 8 — mint outpaces fetch ~7:1, queue 8,085). Fixes for gr483930 (S2 acquire), gr484234 (DOI addressing) and the embed_batch lock-contention head are committed in `worktree-ingest-fix` (40a8b1cb2, 72b9ea243, 9b4adcdf8), awaiting the release owner — verify post-deploy error rates per item 9.
- **Blocked by:** Nothing from deployment; safe_fetch fallback remains at the orchestrator’s gate.
- **Unblocks:** Evidence acquisition for [catalysis](catalysis-selectivity.md#resume).
- **Acceptance:** Use [the latest handoff](#thread-context) and [ranked work](#do-next): account for the bodiless remainder and verify the 10-14 gripe fixes on prod.
- **Worktree:** `ingest-fix`
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

Current declared activity: [fleet roster](../../../.claude/fleet/threads.tsv); dated allocation decisions below are historical.

**Status:** ends when a paper that precis holds is either faithfully
extracted or visibly counted as not extracted — no silent character loss, no
state where the acquisition backlog's own count is wrong. Triage td458898 ran
2026-10-02 (counts and verdicts are comments on each gripe), so the list
below is ranked on measured rows, not age: silent corruption first, then the
states that count a paper as usable when it is not, then metadata.
**Last reviewed:** 2026-10-14
**Worktree:** `ingest-fix`
**Allocation decision (historical):** yes — Reto 2026-10-01: "ingest must work".

**Resume (2026-10-14, ingest-fix round):** three fix commits await the
release owner in `worktree-ingest-fix`; the round-6 after-deploy cleanup
is verified done on prod.
- Diagnosed (prod, read-only): of 2,564 papers minted in 7d, 1,276 have
  no body — 1,199 (94%) are `stub_rank`/citation-expansion stubs never
  fetch-attempted (mint ~366/d vs fetch ~52 ok/d; queue 8,085), 77
  tried→no_oa. embed_batch all-time: 2,332 jobs / 705 failed — 480
  "embedder unreachable", 196 stale-FK (guarded 2026-09-26), and the
  current recurring head LockNotAvailable on the chunk_embeddings write
  (5 of the last 8 failures).
- Landed in branch: (a) `get_paper_by_id(propagate=True)` splits a
  genuine 404 from a resolver fault (`S2ResolverError`, never cached —
  an outage no longer poisons the miss cache for the TTL); `acquire`
  mints `acquire:unverified` + `meta.acquire_unverified` on a resolver
  fault instead of a "hallucinated id" BadInput (gr483930). (b)
  `verify=`/`citation=`/`context_ref_id=` declared on `put()`; dispatch
  promotes an args= key naming a real handler kwarg; DOI/arXiv
  canonicalisation via `precis.identity`; `get` resolves doi:/URL-form
  DOIs, arxiv ids, s2, pubmed through `ref_identifiers` (gr484234).
  (c) `retry_locked` wraps the embed write tx in `embed_batch` and
  `run_handler_once`.
- Confirmed done on prod (was never verified after round 6): ref 202942
  re-ingested (111 chunks); anon 465241 merged+retired; anons
  464753/464754/464755/465135 bodied; the bodiless re-fetch batches
  drained (120 gained bodies, 18 tried-none, 45 held Elsevier previews).
  Only 464821 remains a no-pdf stub.
- Landed earlier this thread (historical): SI builds 1 and 2 with re-arm
  fixes (item 1, retired 2026-10-15 — prod shows both attention triggers
  firing and claimed: 17 of 21 `by='web'` and 61 of 120 `by='walker'`
  requests carry `si_checked_at >= requested_at`, e.g. refs 1254
  (`si_none`) and 2954 (`si_blocked`→`si_found`) for web, 543/1944/2069
  for walker; the rest sit queued behind fetch-lane depth, not a defect),
  the arXiv e-print magic-byte and plain-TeX staging,
  a connect-failure retry on Crossref and bib_parse (round 6, c83c46973),
  the gr465474 no-body fold fix, and the FOR NO KEY UPDATE fold
  serialisation. `precis paper fetch-si <id>` is the CLI spelling of
  `put(kind='paper', id=…, mode='fetch-si')`.
- At the orchestrator's gate, not landed by this thread: the safe_fetch
  next-address fallback, branch `worktree-agent-a3b62578f305e3772` tip
  3e1e8cb5c (reviews §3, verdict LAND; gr465931). Ruled (Reto 2026-10-12,
  design-fold; gr465931): the built packet capture stays off. Reopen
  trigger: si_fetch ConnectTimeouts still occur after the fallback is
  deployed; then enable the capture on one host for two weeks, not on
  all fetchers.
- Historical (round-6, kept for context): timeout budgets on connect
  failures — bib_parse 123 s (3 × 40 s + 3 s, no nested retry_transient),
  DOI validity 63 s, SI Crossref leg 43 s, habanero 2× timeout + 3 s.

## Do next

2. **`backlog/ingest-strips-greek-glyphs.md`** — μ/Greek
   destroyed at extraction. Confirmed live, and its deployed detector was
   inert until the stub-upgrade fix (the upgrade dropped `paper.meta` for
   99.7% of new papers; fixed alongside this re-rank). Reto ruled
   2026-10-02 (review item ingest-and-fetch-2): detection only for now.
   Flagged papers get marked, and findings and cites drawn from them carry
   a caveat. Recovery is decided once the post-deploy `meta.glyph_health`
   count exists. The caveat shipped
   2026-10-02: a paper-view banner, a `source caveats:` section on
   findings, and a draft write-path hint (`glyph_cite_hint`). Not built: a
   whole-draft warning in the citations view or the export preflight. The
   hint fires per write, so it only flags chunks someone touches. First post-deploy flag (ref 461434,
   10-02) was a false positive: its 12 control characters were all TeX
   CMEX delimiter glyphs. Fixed in round 2 by not counting C0 from
   math-extension fonts; the stored flag on 461434 stays until re-analysed.
   The stub-upgrade fix verified live: 1 of 15 PDF ingests since the deploy carries
   `glyph_health`, against 0 of 1,198 the week before. Next, about a day
   after the round-2 deploy (63301c5c, 2026-10-03 13:49Z): measure
   detector precision, as the orchestrator accepted it 2026-10-02. First,
   re-run `analyze_pdf` with the deployed rule over every flagged paper
   (16 on 10-03, all flagged under the old C0 rule), and rewrite or drop
   each stored record. Flagged and unflagged papers then mean the same
   thing whatever their ingest date (round-2 review finding). No CLI
   re-analyses a stored PDF yet, so this runs on the node that holds the
   corpus.
   - Report the flag rate over new ingests. More than a few percent means
     the orphan-span signal is firing on clean symbol fonts.
   - Inspect 20 flagged papers per signal: the `/ToUnicode` font tell, and
     orphan-span-only flags.
   - Rule, pinned before looking: a paper is damaged if at least one Greek
     or symbol character on the flagged page is missing or wrong in the
     stored chunk. Record the page and the character for each.
   - Report each signal's precision with its interval.
   - If the orphan signal is below 80%, gate the banner on the font tell
     or an orphan-count threshold, and keep the flag stored.
   - Recovery (Reto 2026-10-11, outline fingerprinting first, superseding
     the 10-02 detection-only ruling): the resolver shipped 2026-10-12
     (`ingest/glyph_outline.py`; writes `glyph_health['recovery']`). The
     outline table is still empty. Next: seed it from pa494/pa47024, then
     the step-3 repair backfill (see the file's §Recovery).
   - Detector tightened 2026-10-14 (gr228594 comment 8): the 10-12
     backfill dry run flagged 456/900 and a read-only sample found 30/30
     false positives. `suspected` now needs a positive tell for weak
     evidence (micron-word + bare-`m`, or stray-space `V/ mm`); mode-a/b
     fonts still flag unconditionally; en/em dashes are out of the
     symbol set. Reproduction over the same cohort: 2 tell refs (494,
     933). Verify on a corpus node once deployed: `precis
     backfill-glyph-health --after 0 --limit 900` should land near the
     tell count, then the --apply batches resume.
   - The result goes to Reto as a release look-at item either way. Rank 1: the corpus is wrong
   with no error, and embeddings, findings and cites all inherit it.
3. **gr453860** / td461154 — 947 papers have a PDF and no body. Measured
   2026-10-02:
   - 814 Elsevier not-entitled: the XML has no body and the PDF is the
     1-page preview. This is the same gap as item 5, now bodiless instead
     of truncated.
   - 65 have no ingest trace at all.
   - 43 corrupt PDFs.
   - 20 PDF files missing on disk.
   - 5 scanned.
   Reto approved the policy (review item ingest-and-fetch-4, 10-02) with
   one step first: re-fetch all 947 through the current pipeline, then
   apply the policy to what remains. Re-fetch = `meta.markup_refetch` pin
   + `oa_requeued` backoff bypass + a `meta.bodiless_refetch.batch` mark,
   with fetch history kept. A byte-identical re-fetch skips Marker
   (`add.py` fast path), so only a new source (markup, a different PDF)
   heals. Canary batch 1 (50 Elsevier + 50 other) went out 2026-10-03
   06:53Z. At 08:44Z, 18 had been tried:
   - Other: 13 tried, 9 gained a full body, all from arXiv (28–356
     chunks).
   - Elsevier: 5 tried, 4 gained only the preview again (8–12 chunks,
     stopping after the introduction).
   Re-fetching Elsevier turns a bodiless paper into a preview-body paper,
   so the 45 untried Elsevier papers in batch 1 were unpinned
   (`bodiless_refetch.outcome='held_elsevier_preview'`). The 814 Elsevier
   papers wait for the vault key (td462729) and are not re-fetched. The
   no-body ingest branch now clears the pin once a body lands (a0bb7b707,
   live since 13:49Z). Prod check: waiting for the first post-deploy
   re-fetch that gains a body. Queuing must also set `prio=1`
   (`prio_by='acquire'`, as `pin_stub_for_fetch` does). The claim orders
   by `prio` before the re-queue flag (`claim_stubs_to_fetch`). Only the 18
   prio-1 papers of batch 1 were tried; the other 37 sat behind 1,205
   prio-1 stubs until re-pinned at 12:35Z (old value in
   `bodiless_refetch.prev_prio`). Re-pinning did not help: by 14:40Z, 0 of
   the 37 had been tried. prio 1 is the floor, and the next tiebreak
   (`oa_requeued`, then `ref_id DESC`) still ranks them behind 1,325
   newer acquire stubs, with the lane trying about 25 refs an hour. Round
   3 ranks a `markup_refetch` pin directly after `prio`. Only these 37
   refs carry the pin.
   Resume (2026-10-03 21:45Z): the 37 were all tried before round 3
   deployed. Of batch 1's 50 non-Elsevier papers, 30 gained a body and
   20 did not; those 20 keep the pin and sit in normal backoff. Batch 2
   (the other 83 non-Elsevier) was queued 21:40Z at prio 1 with the pin.
   It is the first test of round 3's ordering: it should be claimed
   within about three 32-stub passes. Round 3 verified 22:58Z: the first
   pass after queueing worked through batch-2 refs ahead of the backlog.
   By 02:33Z, 58 of 83 had been tried and 39 gained a body (34–412
   chunks, none preview-sized). Tried-but-no-body refs with a `fetch_ok`
   re-downloaded the same bytes: the watcher logs `duplicate <x>.pdf`
   (sha probe in `precis_add`), so Marker does not re-run. Their stored
   PDF failed its first ingest with `PdfiumError: Failed to load
   document`, so a re-fetch cannot heal them: they go to the policy's
   corrupt bucket (repair or OCR), not another re-fetch. Seen on 6 of
   batch 2's no-body refs and 5 of batch 1's. Side defects filed:
   the arXiv e-print of a PDF-only submission parsed as a
   LaTeX tarball (fixed by a magic-byte check in the arxiv_source leg), and gr465474 (an arXiv fetch for ref 202942 minted
   anon ref 465241 instead of folding into it). Next: once batch 2
   drains, apply the policy to what is still bodiless, routing
   Pdfium-unreadable PDFs to repair. This
   Prod 2026-10-11: 857 still bodiless; `heal:bodiless` has judged
   preview=811, unreadable=43, extracted=10. The PDF leg now journals
   `ingest:pdf` / `no_body` (reason `deferred_printable_only`,
   `extraction_empty_fallback` or `extraction_empty`) at ingest time
   (gr453860). Reto 2026-10-11, both in `paper_hygiene` and run by
   `paper_reconcile`: (a) gr464259 — a body that is >= 90% references/SI
   (`heal_partial_body_papers`) gets ONE re-fetch via the
   `markup_refetch` pin (`heal:partial_body` `refetch_queued`); judged 6h
   after the next `fetch_ok`, or after 7 days with none, it becomes
   `terminal` (`same_file` / `still_partial` / `refetch_unavailable`),
   tagged `body:partial`, and is never queued again. (b) empty extraction —
   `heal:bodiless` `extract_retry` once, then `unreadable` + tag
   `body:needs_ocr` (also for scanned PDFs). No OCR engine exists beyond
   Marker, so that tag is the OCR route. Both tags read as "no usable
   body" in the taproot `[pa]` planner, the citations view and the
   finding-evidence "(unfetched)" label (`count_usable_chunks`,
   `usable_body_ref_ids`), and /status shows both counts. The draft
   fetch list is unchanged, so a tagged ref is never re-queued.
   This thread owns td461154 (STATUS:doing). Close it once the policy is
   applied to the remainder and the gained-body count is reported to Reto. Vault-key
   follow-up for the 2,796 preview bodies: td462729. Evidence is in
   `~/.claude/projects/-Users-reto-precis-mcp/bodiless/`.
4. **No-OA stubs** — of 13,874 stubs, ~3,926 have been tried and every leg
   said no OA copy. Shipped 2026-10-02: a `no-oa` bucket in
   `precis stats --stubs`, the manual-retrieval list `precis stubs --no-oa`
   (≥3 hour-bucketed passes, every fetcher event `no_oa_version`), and an
   acquire re-stamp guard (`ACQUIRE_REARM_DAYS`). Still open: the /drive
   "Stubs (to get)" queue (`precis_web/routes/drive.py`) mixes the no-OA
   set in, and the stub-readout remainder.
5. **`backlog/elsevier-preview-remediation.md`** — ~2,796 papers whose
   body is a 1-page preview. Same "looks done, isn't" shape as 2. Ranked
   below it because the fix is a cluster ops run (the vault key), not code.
6. **`backlog/ref-2615-is-a-mis-bound-record.md`** — one ref bound to two
   different papers' PDFs/DOI. Silent corruption, but a single row.
7. **S2 venue re-arm** — 4,313 S2-enriched papers have no venue. Reto approved
   the re-arm 2026-10-02 on condition of gentleness. The enrich lane
   (`stub_rank`) claims stubs only, so it reaches 3,119 of them; the live
   `external_rate_limits` row holds S2 at 1 req/s, and one `/paper/batch`
   call covers 500 ids. Re-arm runs in batches of 500 at least an hour
   apart, each row stamped `meta.s2_rearm.batch` (which also stops a
   re-enriched-but-still-venueless row being re-armed). Re-arm done
   2026-10-03 14:53Z: 7 batches, 3,128 papers, all re-enriched, and
   1,658 (53%) now carry a venue (`meta.journal`; 27–64% per batch). S2
   holds no venue for the other 1,470, so a further S2 pass gains
   nothing. Open: those 1,470, plus the 1,200 *held* venue-less papers no
   lane re-enriches. Both need the Crossref fallback
   (`backlog/crossref-enrichment.md`); the held papers could also take a
   held-paper S2 pass.
8. **Stub-queue depth (policy, not a defect)** — measured 2026-10-14:
   8,085 no-PDF stubs queued; citation-expansion/stub_rank mints ~366/day
   while the fetch lane gains ~52 bodies/day (~370 attempts/day, mostly
   no-OA). The backlog therefore grows ~300/day and a fresh unranked stub
   waits weeks. This is why 94% of last week's bodiless papers were never
   attempted — the queue is doing what it was told. Options for Reto:
   (a) tighten `stub_rank` minting (a higher bar before a citation earns
   a stub); (b) widen fetch capacity (more fetcher workers / larger
   claim limit); (c) accept it as backlog and keep prios honest — the
   explore-fraction slice already surfaces unranked stubs. A mass
   re-queue is NOT recommended: an `oa_requeued` stamp gives each stub
   one front-of-queue bypass, and stamping 8k refs only reorders them
   against the same drain rate.
9. **Post-deploy verification of the 10-14 fixes (do not run now)** —
   once `worktree-ingest-fix` lands:
   - `put|paper` / `get|paper` error rates in `tool_calls` should drop
     to ~0 (`status='error'` count by tool name, 24 h before vs after).
   - The 95 OPEN `acquire:unverified` stubs predate the outage (minted
     via title= or verify=False over weeks); the outage puts minted
     nothing — they were rejected outright. To re-verify each stub's
     identifier now that S2 is up, re-acquire it (idempotent — re-pins
     prio=1, re-runs the S2 lookup):
     ```sql
     SELECT r.ref_id, i.id_kind, i.id_value
     FROM refs r
     JOIN ref_tags rt ON rt.ref_id = r.ref_id
     JOIN tags t ON t.tag_id = rt.tag_id
     JOIN ref_identifiers i ON i.ref_id = r.ref_id
     WHERE t.namespace = 'OPEN' AND t.value = 'acquire:unverified'
       AND r.kind = 'paper' AND r.retired_at IS NULL
       AND i.id_kind IN ('doi', 'arxiv', 's2');
     ```
     then per row: `put(kind='paper', identifier='<id_kind>:<id_value>')`.
     Clearing the OPEN tag after a verified mint is a manual step —
     `put(kind='paper', id='<slug>', untags=['acquire:unverified'])`.
   - embed_batch: failures should no longer carry LockNotAvailable;
     remaining failures should be embedder-unreachable/infra only.
   - Ref 464821 (last bodiless anon) is a no-pdf stub — it will drain
     through the fetch queue like the rest; no special action.

## Horizon

1. **`backlog/pdf-sha256-identifier-hygiene.md`** — the missing-sha sweep
   (the defect subset of the 22,106 sha-less refs is unmeasured; needs a
   join against the corpus dir). It gates nanopub provenance, not text.
2. **`backlog/crossref-enrichment.md`** — abstract fallback; mailto still
   unset on `paper_meta_enrich`.
3. **`backlog/papers-edit-reslug-hangs.md`** — broken feature, 1 repro.
4. **`backlog/paper-dedup-bucket-b.md`** — 94 refs, CLI-gated ops run.
5. **`backlog/acquisition-marker-lives-in-the-wrong-place.md`** — backfill
   done; the prose-grep cleanup remains.
6. **`backlog/oa-acquisition-roadmap.md`** — new OA legs and bulk arms,
   after the counting bugs (3, 4) make the yield measurable.
7. **`backlog/ms-teams-paper-feed.md`** — Teams channels as an ingest
   source; pillar 4, 2026-10-01.
8. **`backlog/april-corpus-nas-migration.md`** — 5,335 April-era PDFs never
   merged into the NAS corpus; platform pass 2026-10-02.

## Parked

- **`axis:patent_example` enable + two watches** — dormant on Reto's word
  2026-10-01. Enable = `precis service prio '*' axis:patent_example 1`
  (prod write). Watches: the first live priority-claims extraction
  (`_patent_xml.py`) and `prophetic` axis precision.
- **Backlinks panel text-scan coverage** — needs design; owner
  `src/precis_web/routes/papers.py::_backlinks`.
- **Feature items, no fidelity defect:** `patent-kind-followons` ·
  `edgar-kind-spec` (phase 2) · `paper-refs-panel-editing` ·
  `paper-reader-bbox-backfill` (GPU re-run) · `equation-chunk-retirement`
  (papers half) · `merged-chunk-handle-redirect` (design limitation).
  Unpark when the thread's Do next empties.

## No action needed

- (none)

## Seam

- `knowledge` (taxonomy, quests, papers) consumes this pipeline's output.
  Every fidelity defect here reaches that programme as a wrong answer with no
  error.
- `backlog/graph-maintenance-queue.md` (local-compute) expects to spend local
  capacity on this pipeline's output; a corrupt extraction makes that spend
  worse than idle.

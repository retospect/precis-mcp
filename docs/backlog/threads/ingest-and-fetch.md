# ingest and fetch — acquisition and extraction fidelity

**Status:** ends when a paper that precis holds is either faithfully
extracted or visibly counted as not extracted — no silent character loss, no
state where the acquisition backlog's own count is wrong. Triage td458898 ran
2026-10-02 (counts and verdicts are comments on each gripe), so the list
below is ranked on measured rows, not age: silent corruption first, then the
states that count a paper as usable when it is not, then metadata.
**Last reviewed:** 2026-10-04
**Worktree:** `ingest-and-fetch`
**Active:** yes — Reto 2026-10-01: "ingest must work".

**Resume (2026-10-04, ship-all):**
- Landed: SI builds 1 and 2 with re-arm fixes (item 1), the arXiv
  e-print magic-byte and plain-TeX staging (gr465473 closed), and a
  connect-failure retry on Crossref and bib_parse (c83c46973, round 6).
- At the orchestrator's gate, not landed by this thread: the safe_fetch
  next-address fallback, branch `worktree-agent-a3b62578f305e3772` tip
  3e1e8cb5c (reviews §3, verdict LAND; gr465931). Two follow-ups for the
  next safe_fetch branch: guard a finite timeout <= 0 with 2+ addresses
  (today it raises before any dial), and a test that fails when
  `httpcore.NetworkStream` gains a public method `_FallbackStream` lacks.
- Fixed in this ship: gr465474. The bodiless re-fetch minted a duplicate
  anon ref when the fetched file differed from the stored one, because
  `add.py::_valid_fold_stub` required `pdf_sha256 IS NULL`; it now also
  accepts a live ref with no body chunks. A ref with a sha and a body
  still refuses a different file (unchanged). Round-6 review follow-up,
  same round: (a) the fold is refused when the fetched file's journal
  DOI differs from the target's (preprint-server DOIs exempt;
  `add.py::_doi_conflicts`). With no DOI on the fetched file there is
  still no identity check, the same trust a `pdf_sha256 IS NULL` stub
  fold always had. A refused fold into a sha-NULL stub can still be
  folded back by the filename reconcile (`_reconcile_orphan_stub`,
  unchanged). (b) Two folds into one ref are serialised by a
  `FOR NO KEY UPDATE` row lock in `register_aliases_and_maybe_upgrade`.
  Before it, chunk inserts under `ON CONFLICT (ref_id, ord) DO NOTHING`
  could interleave two files. Worst-case hold per item on connect
  failures, counting 2T per connect attempt (the TCP plus TLS budget,
  the same before and after the safe_fetch fallback):
  - bib_parse: 123 s, 3 attempts × 40 s + 3 s backoff. It no longer
    nests `retry_transient`, which would have made it 252 s.
  - DOI validity: 63 s, 2 × 30 s + 3 s (not nested).
  - SI Crossref relation leg: 43 s, 2 × 20 s + 3 s, inside the 120 s SI
    pass budget.
  - Habanero sites: 2 × the requests timeout + 3 s.
  After deploy: re-ingest the 8dc037e4 PDF into ref 202942, merge anon 465241
  into it, and triage anons 464753, 464754, 464755, 464821 and 465135.
  As of 06:48Z, 47 queued re-fetches had not run.
- Next: item 3 policy pass on the still-bodiless remainder, then close
  td461154; glyph precision run (item 2) from 2026-10-04 14:00Z; delete
  item 1 once walker and web SI triggers are seen on prod.

## Do next

1. **`backlog/si-attachments.md`** — supplementary-information PDFs
   are found where attention is, always fetched, and ingested as their
   own ref linked to the parent paper and cited as the parent. Reto
   ruled 2026-10-03 (review session 20:02Z). Blocks quest qu164903
   (NO→NH3 on Pd(111)) through catalysis-selectivity-17. Build 1 landed
   2026-10-03 (`si_discovery.py`, `si_fetch.py`, `si_links.py`;
   `put(kind='paper', id=…, mode='fetch-si')`). Discovery runs Figshare
   (`resource_doi`; ACS mirrors its SI there), then Crossref relation, a
   component-DOI probe and landing-page patterns. pubs.acs.org answers a
   Cloudflare challenge, which is recorded as a miss, not bypassed. Two
   deviations from the spec: the link is `part-of`/`contains` with
   `meta.role='supplement'`, because a new relation pair needs a
   migration (switch point: `SI_RELATION` in `store/si_links.py`); the event
   source is `si_fetch`, because `fetcher:%` events start the main-PDF
   backoff. Not built: a CLI, and a bare-slug cite of an SI ref is not
   redirected to the parent (search hits and exports are). Next, after the
   round-4 deploy: run `fetch-si` on pa5303 and pa166889 and write the
   result into catalysis-selectivity-17. Build 2 landed 2026-10-03: the
   attention trigger, `queue_si_on_attention` (`store/si_links.py`). It is
   called from the web paper page (`routes/papers.py::detail`), the MCP
   `get` overview (`PaperHandler.get`) and the fisheye ring walk
   (`refeye.py::collect_ring`, at most 20 papers per walk). A paper is
   checked once by attention, ever; only `fetch-si` re-checks it.
   Explicit `fetch-si` requests claim ahead of attention ones. The ring
   walk also runs inside backfill and working-set renders, so a paper
   cited by a backfilled section is queued without a human look. That is
   accepted under the "walkers touch" ruling. Off switch:
   `si.attention_enabled` (env `PRECIS_SI_ATTENTION`). After deploy:
   watch the SI-pass yield and how much of each fetch pass it takes;
   delete this item once the two quest papers are done. First prod run,
   2026-10-04 00:44Z (build 1): pa5303 (chen23g) got its SI from Figshare,
   minted as pa465134 (chen23gsi), 32 chunks. pa166889 (chen24p) found
   its Figshare SI, but the download was skipped on `deadline`: from the
   fetcher host, Crossref and doi.org connect timeouts used up the
   shared 120 s pass budget. A budget-cut parent was then never retried.
   Fixed: it is re-armed for the next pass, up to 3 times, and discovery
   requests are capped at 10 s connect / 20 s read. pa166889 was
   re-queued by hand. Its 02:37Z retry missed again on Figshare and
   doi.org connect timeouts, so a no-PDF check that failed on a
   transient network error now also re-arms (4ed4c40b0). Probe
   2026-10-04 from the fetcher host:
   - Crossref failed 5 of 10 requests, with TLS handshakes up to 20 s.
   - Figshare took 35 ms every time.
   - doi.org took up to 3 s.
   - DNS, IPv6 and the route were all clean.
   Crossref answers `x-concurrency-limit: 1`, and several worker
   processes on that host call it (retraction gate, enrich, provenance,
   SI discovery). Exceeding that limit is the likely cause.
   Unconfirmed, and open: whether the polite-pool mailto is sent on
   every Crossref call.
   Quest result (2026-10-04): both SIs are ingested, pa465134
   (chen23gsi) and pa465698 (chen24psi). Both state V vs RHE, so
   catalysis-selectivity-17 is resolved and routed to the
   catalysis-selectivity thread. Round-4 dogfood (prod 727728cc9):
   - Attention: an MCP `get` queued ref 458964 once; a second `get`
     left `requested_at` alone.
   - Re-arm: its SI pass (06:24Z) missed Figshare and doi.org on
     `ConnectTimeout` and re-armed (`rearmed: true`,
     `deadline_retries: 1`).
   - No SI or e-print errors in `worker_logs`.
   Two problems remain:
   - The first fetch pass after the 04:40Z deploy started only at
     06:23Z: `_hub_refine_pass` held the fetcher host from 05:26Z.
   - The worker hits `ConnectTimeout` on api.figshare.com in 3 of 4
     SI passes. It is not the worker runtime (no proxy env, a fresh
     client per request). The fetcher host has per-IP TLS-handshake
     stalls (`_ssl.c:989: The handshake operation timed out`) to
     Figshare, Crossref and doi.org. A failing IP stalls on every try,
     then recovers, and the IPs swap within a minute. Crossref
     timeouts show on a second worker host too. Egress problem, ops
     gripe gr465931 (`safe_fetch` pins the first resolved address only,
     so a pass keeps hitting the stalled IP). Until fixed, the SI
     re-arm absorbs it. Crossref and bib_parse calls now retry once on a
     connect failure (`utils/http.py::retry_transient`). The safe_fetch
     next-address fallback design is accepted (reviews §3) and goes
     through the orchestrator's gate as a branch.
   Delete this item once walker and web triggers are seen on prod.
2. **gr228652** (`backlog/ingest-strips-greek-glyphs.md`) — μ/Greek
   destroyed at extraction. Confirmed live, and its deployed detector was
   inert until gr461607 (stub upgrade dropped `paper.meta` for 99.7% of new
   papers; fixed alongside this re-rank). Reto ruled
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
   gr461607 verified live: 1 of 15 PDF ingests since the deploy carries
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
   gr465473 (the arXiv e-print of a PDF-only submission is parsed as a
   LaTeX tarball; fixed by a magic-byte check in the arxiv_source leg) and gr465474 (an arXiv fetch for ref 202942 minted
   anon ref 465241 instead of folding into it). Next: once batch 2
   drains, apply the policy to what is still bodiless, routing
   Pdfium-unreadable PDFs to repair. This
   thread owns td461154 (STATUS:doing). Close it once the policy is
   applied to the remainder and the gained-body count is reported to Reto. Vault-key
   follow-up for the 2,796 preview bodies: td462729. Evidence is in
   `~/.claude/projects/-Users-reto-precis-mcp/bodiless/`.
4. **gr453859** — of 13,874 stubs, ~3,926 have been tried and every leg
   said no OA copy. Shipped 2026-10-02: a `no-oa` bucket in
   `precis stats --stubs`, the manual-retrieval list `precis stubs --no-oa`
   (≥3 hour-bucketed passes, every fetcher event `no_oa_version`), and an
   acquire re-stamp guard (`ACQUIRE_REARM_DAYS`). Still open: the /drive
   "Stubs (to get)" queue (`precis_web/routes/drive.py`) mixes the no-OA
   set in, and gr453862's stub-readout remainder.
5. **`backlog/elsevier-preview-remediation.md`** — ~2,796 papers whose
   body is a 1-page preview. Same "looks done, isn't" shape as 2. Ranked
   below it because the fix is a cluster ops run (the vault key), not code.
6. **`backlog/ref-2615-is-a-mis-bound-record.md`** — one ref bound to two
   different papers' PDFs/DOI. Silent corruption, but a single row.
7. **gr456181** — 4,313 S2-enriched papers have no venue. Reto approved
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

- **gr228699** — duplicate of gr228652, closed wontfix 2026-10-02.
- **gr453862**, **gr453913** — fixes verified in prod 2026-10-02 (1
  residual audit row; 0 recurrences since 09-28).

## Seam

- **gr463966** (session advisory locks under pgbouncer transaction
  pooling, a blocker for the Stage B DISCARD ALL): claims-and-evidence
  owns the fix for all three sites. That includes this pipeline's
  `ingest/claim.py` (`Claim`, the per-PDF Marker claim) and
  `workers/chunk_keywords.py`. The leaked lock is chunk_keywords'
  `_LOCK_KEY`. It recurs on fresh pooled backends: it was on pid 11267,
  then on 15730 (born 14:14Z) at 14:44Z on 2026-10-03. Recycling one
  backend therefore does not clear it. Only the code fix does, and Stage
  B's DISCARD ALL waits on that fix, not on the held lock. The `Claim`
  leak means two hosts can run Marker on the same PDF until the fix
  deploys.

- `local-compute` parks the **embed-drain** half of what was one cluster
  (gr456034, gr454865). That is throughput, this thread is fidelity; they
  touch different code and neither sequences the other.
- `knowledge` (taxonomy, quests, papers) consumes this pipeline's output.
  Every fidelity defect here reaches that programme as a wrong answer with no
  error.
- `backlog/graph-maintenance-queue.md` (local-compute) expects to spend local
  capacity on this pipeline's output; a corrupt extraction makes that spend
  worse than idle.

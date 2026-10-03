# ingest and fetch — acquisition and extraction fidelity

**Status:** ends when a paper that precis holds is either faithfully
extracted or visibly counted as not extracted — no silent character loss, no
state where the acquisition backlog's own count is wrong. Triage td458898 ran
2026-10-02 (counts and verdicts are comments on each gripe), so the list
below is ranked on measured rows, not age: silent corruption first, then the
states that count a paper as usable when it is not, then metadata.
**Last reviewed:** 2026-10-02
**Worktree:** `ingest-and-fetch`
**Active:** yes — Reto 2026-10-01: "ingest must work".

## Do next

1. **gr228652** (`backlog/ingest-strips-greek-glyphs.md`) — μ/Greek
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
   after the round-2 deploy: measure detector precision, as the
   orchestrator accepted it 2026-10-02.
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
2. **gr453860** / td461154 — 947 papers have a PDF and no body. Measured
   2026-10-02:
   - 814 Elsevier not-entitled: the XML has no body and the PDF is the
     1-page preview. This is the same gap as item 4, now bodiless instead
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
   no-body ingest branch now clears the pin once a body lands (round 2,
   this commit). Until that deploys, clear pins by hand after each batch.
   Next: let the 32 pinned "other" papers finish, then queue the other
   ~83 non-Elsevier papers. Throughput is about 9 tried per hour. Vault-key
   follow-up for the 2,796 preview bodies: td462729. Evidence is in
   `~/.claude/projects/-Users-reto-precis-mcp/bodiless/`.
3. **gr453859** — of 13,874 stubs, ~3,926 have been tried and every leg
   said no OA copy. Shipped 2026-10-02: a `no-oa` bucket in
   `precis stats --stubs`, the manual-retrieval list `precis stubs --no-oa`
   (≥3 hour-bucketed passes, every fetcher event `no_oa_version`), and an
   acquire re-stamp guard (`ACQUIRE_REARM_DAYS`). Still open: the /drive
   "Stubs (to get)" queue (`precis_web/routes/drive.py`) mixes the no-OA
   set in, and gr453862's stub-readout remainder.
4. **`backlog/elsevier-preview-remediation.md`** — ~2,796 papers whose
   body is a 1-page preview. Same "looks done, isn't" shape as 2. Ranked
   below it because the fix is a cluster ops run (the vault key), not code.
5. **`backlog/ref-2615-is-a-mis-bound-record.md`** — one ref bound to two
   different papers' PDFs/DOI. Silent corruption, but a single row.
6. **gr456181** — 4,313 S2-enriched papers have no venue. Reto approved
   the re-arm 2026-10-02 on condition of gentleness. The enrich lane
   (`stub_rank`) claims stubs only, so it reaches 3,119 of them; the live
   `external_rate_limits` row holds S2 at 1 req/s, and one `/paper/batch`
   call covers 500 ids. Re-arm runs in batches of 500 at least an hour
   apart, each row stamped `meta.s2_rearm.batch` (which also stops a
   re-enriched-but-still-venueless row being re-armed). Batch 1 (13:23Z):
   58% gained a venue. Batches 2-3 done; 4-7 run 2 h apart from a
   detached `s2-rearm.sh` (Reto: ample breaks). Open: the
   1,200 *held* venue-less papers no lane re-enriches — Crossref fallback
   (`backlog/crossref-enrichment.md`) or a held-paper S2 pass.

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
   after the counting bugs (2, 3) make the yield measurable.
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

- `local-compute` parks the **embed-drain** half of what was one cluster
  (gr456034, gr454865). That is throughput, this thread is fidelity; they
  touch different code and neither sequences the other.
- `knowledge` (taxonomy, quests, papers) consumes this pipeline's output.
  Every fidelity defect here reaches that programme as a wrong answer with no
  error.
- `backlog/graph-maintenance-queue.md` (local-compute) expects to spend local
  capacity on this pipeline's output; a corrupt extraction makes that spend
  worse than idle.

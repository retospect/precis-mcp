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
   papers; fixed alongside this re-rank). Next: confirm the first
   `meta.glyph_health` rows after deploy, re-analyse the stored PDFs to get
   a real count, then Reto rules on recovery. Rank 1: the corpus is wrong
   with no error, and embeddings, findings and cites all inherit it.
2. **gr453860** — 947 papers have a PDF and no body (914 on 09-27). Every
   surface counts them as held. Visibility has shipped; still open are a
   journal reason, a heal and the pdf_sha256-as-usable call-site audit.
   Ranked above 3: this one inflates "usable", 3 only mis-buckets stubs.
3. **gr453859** — of 13,874 stubs, ~3,926 have been tried and every leg
   said no OA copy, yet they count as pending. Needed: a cooling bucket, a
   manual-retrieval list, and an acquire re-stamp guard. It also owns
   gr453862's stub-readout remainder.
4. **`backlog/elsevier-preview-remediation.md`** — ~2,796 papers whose
   body is a 1-page preview. Same "looks done, isn't" shape as 2. Ranked
   below it because the fix is a cluster ops run (the vault key), not code.
5. **`backlog/ref-2615-is-a-mis-bound-record.md`** — one ref bound to two
   different papers' PDFs/DOI. Silent corruption, but a single row.
6. **gr456181** — 4,313 S2-enriched papers have no venue (2,477 have a
   DOI). The code fix is deployed. The re-arm of `s2_enriched_at` is a bulk
   prod write that needs Reto's go. Metadata only, no body harmed.

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

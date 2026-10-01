# ingest and fetch — acquisition and extraction fidelity

**Status:** ends when a paper that precis holds is either faithfully
extracted or visibly counted as not extracted — no silent character loss, no
state where the acquisition backlog's own count is wrong. Today the cluster
has never been triaged, so Do-next 1 is the triage and everything under it is
provisional: ranking seven items by age and prose is how a fixed bug keeps a
rank and a live corruption loses one.
**Last reviewed:** 2026-10-01
**Worktree:** `ingest-and-fetch`
**Active:** yes — Reto 2026-10-01: "ingest must work".

Created 2026-10-01 on Reto's ruling: this cluster gets its own thread rather
than folding into `knowledge` (which owns the layer that *consumes* the
pipeline) or staying parked in `local-compute` (which owns what local capacity
does, not what it ingests). Triage first, then fold the survivors into the
ranking below.

## Do next

1. **td458898** — the triage pass over the seven gripes below **and** the
   nineteen ingest-cluster items: `backlog/patent-kind-followons.md` ·
   `papers-edit-reslug-hangs` · `oa-acquisition-roadmap` ·
   `arxiv-add-lane-text-search-mismatch` · `pdf-sha256-identifier-hygiene` ·
   `ref-2615-is-a-mis-bound-record` · `paper-refs-panel-editing` ·
   `crossref-enrichment` · `paper-reader-bbox-backfill` ·
   `acquisition-marker-lives-in-the-wrong-place` · `ingest-strips-greek-glyphs`
   · `elsevier-preview-remediation` · `paper-dedup-bucket-b` ·
   `edgar-kind-spec` · `equation-chunk-retirement` ·
   `merged-chunk-handle-redirect` (all `backlog/<slug>.md`). Three more of the
   nineteen left with the deletes below, their live residue in Parked. First by
   dependency, not by cost: nothing can be ranked honestly until it is known
   which of these still reproduce, which are duplicates of each other, and how
   many corpus rows each one touches today. Several are six weeks old.
2. **gr228652** + **gr228699** — the Greek/micro-character pair, provisionally
   rank 2 and expected to take rank 1 the moment triage confirms them. Blast
   radius: they do not fail, they write a quietly wrong corpus, and
   embeddings, findings and cites all inherit it. A defect that emits no
   finding outranks one that fails loudly.
3. **gr453913** — arxiv_html markup ingest drops the arXiv identifier, so
   every arXiv-HTML fold dies at `make_paper_id`. Above the counting bugs
   because it blocks an acquisition path outright rather than mis-reporting
   one.
4. **gr453859** + **gr453860** — the two counting holes: no terminal "no OA
   copy exists" state, and a paper may carry `pdf_sha256` with zero body
   chunks indefinitely. Ranked together because they are the same defect
   shape — the backlog's own number is wrong, so nobody can tell an untried
   paper from an impossible one — and because fixing either alone still
   leaves the count unusable.
5. **gr453862** — one ref sat as `no_oa_version` with its arXiv copy one GET
   away. Likely an instance of 4 rather than its own bug; triage decides
   whether it survives as a separate entry.
6. **gr456181** — venue dropped on S2 enrich. Last: metadata loss on one
   field, loud enough to spot downstream, and no corpus body is harmed.

## Horizon

1. **A fidelity check that runs at extraction, not after it** — the glyph pair
   is only findable today by noticing wrong characters in a rendered draft.
   Already designed: `backlog/ingest-strips-greek-glyphs.md` (per-document
   `glyph_health` written during extraction; root cause for gr228652,
   gr228699, gr228594). Rank above Parked once triage confirms the pair.
2. **backlog/ms-teams-paper-feed.md** — papers posted in Teams channels as an
   ingest source; from pillar 4, 2026-10-01.

## Parked

- **`axis:patent_example` enable + two watches** — dormant on Reto's word
  2026-10-01. Enable = `precis service prio '*' axis:patent_example 1` (prod
  write; until then patent chunks stay unclassified and the prophetic caveat
  never fires). Watches: first live priority-claims extraction
  (`_patent_xml.py`, built from the ST.36 shape without an OPS sample), and
  `prophetic` axis precision. Build shipped; was `patent-evidence-parity`.
- **Backlinks panel text-scan coverage** — materialize inline `[pa]`/`[pc]`
  cites into `links` at draft-save + backfill (repeat cites count 1 today; no
  trigram index on `chunks.text`), then a deep `/papers/<id>/backlinks` page;
  owner `src/precis_web/routes/papers.py::_backlinks`. Needs design; was
  `paper-backlinks-completeness`.

## No action needed

- (none)

## Seam

- `local-compute` parks the **embed-drain** half of what was one cluster
  (gr456034, gr454865 — backlog not draining, `chase_trigger`'s dead
  batch-size knob). That is throughput, this thread is fidelity; they touch
  different code and neither sequences the other.
- `knowledge` (taxonomy, quests, papers) consumes this pipeline's output.
  Every fidelity defect here reaches that programme as a wrong answer with no
  error, which is why this thread exists separately rather than under it.
- `backlog/graph-maintenance-queue.md` (local-compute) expects to spend local
  capacity on this pipeline's output; a corrupt extraction makes that spend
  worse than idle.

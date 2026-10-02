# draft authoring — drafts, export and the tex layer

**Status:** ends when a draft can be written, linted and exported to every
format without a human working around the tool. Do-next is ordered by what
the month's preprint hits (quest qu459585; October = td459586, the nanobud
paper dr173020); triaged against it 2026-10-02.
**Last reviewed:** 2026-10-02
**Worktree:** `draft-authoring`
**Active:** yes — Reto 2026-10-01: "draft authoring must work".

Created 2026-10-01 from the memory-graph pillar review.

## Do next

Triaged 2026-10-02 against the October paper (dr173020, td459586), in the
order its path reaches each item: Reto's findings check and hub rewording
(many edits) → pre-submission polish → export against the RSC template.
Evidence: jo461157, the placeholder-figure export, compiled 34 pages and 121
citations with no warnings beyond the ten placeholders and 160 unsigned hubs;
`view='hygiene'` reports only "all 8 cited papers have a validated DOI".

1. **`draft-write-latency-whole-draft-rescan`** — the findings check is a burst
   of edits on a ~190-chunk draft. Root-caused 2026-10-02 from prod
   `tool_calls` (09-27..10-01): latency splits by input shape, not by time.
   Text writes (`edit` find+text p50 47 s, `put` text p50 33 s) pay
   `sync_draft_links`' re-upsert of every edge (dr173020: 193 chunks, 209
   links, ~1,000–1,500 serial round trips); `move`/`not_abbrev` edits skip it
   and run in 0.3 s. Built 2026-10-02: a chunk-scoped write re-resolves only
   that chunk, carries the others' edges over, and skips edges whose stored
   meta is unchanged. The hub-mint cost (`put(kind='finding', supporters=)`
   94–182 s: ~10 serial MEDIUM dedup judges) is fixed by
   `canon.judge_candidates` (concurrent judges). Deployed in 567f207f
   (15:05Z 2026-10-02): the first 2 text edits after it ran in 0.23 s and
   0.26 s, against a 24 s p50 before. Round 2 (d9a4c6568) makes export run
   a full link sync before its edge-reading gates and caps the judges at 4.
   Next: re-read text-write latency at n≥20 and the first
   `put(kind='finding', supporters=)` after the deploy (log in
   `reviews/draft-authoring.8df403606.md`), then close the item.

Hit, owned by another thread: `draft-authoring-graph-affordances`
(graph-memory-consumers) — (b) chunk history and (c) "did my edit land" are
the reads the findings check needs; its evidence came from this paper.

## Horizon

All `backlog/<slug>.md`. Not hit by the October paper (reason in brackets).

- **Authoring correctness** — `draft-hygiene-lints` remaining sections
  (notation drift: dr173020's only hits are the legitimate "NanoBud"
  trademark) · `authoring-tex-lint` (slice 2 is corpus chunks, not drafts) ·
  `degree-spacing-canon-vs-draft-lint-collision` (dr173020 has no tight °C) ·
  `draft-text-route-dc-handle-wrap` (live, but the web editor posts base58
  handles, so only direct POSTs hit it) · `draft-doi-completeness-check`
  (dr173020's DOIs all validate) · `draft-cite-groundwork-prepass` (hold) ·
  `tex-layer2-fixer-fate` · `dossier-paper-handles-emitted-bare-not-bracketed`
  (quest tick prompt, not drafts; still a small live bug).
- **Export** — `draft-export-panel-per-format-tabs` (gate fix gr454753 is
  deployed; the submission export can pass `doi_links`/`library_links` as job
  params) · `endnote-export-validation` (hits only if submission goes docx +
  EndNote) · `export-glyph-allowlist` (jo461157 compiled with no glyph
  failures) · `draft-poster-genre-and-themes` · `draft-section-styles`.
- **Content model** — `smartdraft-review-parity` (may hit td461162, the Phase 5
  review from the web review block; re-check when it starts) ·
  `draft-table-structured-enrichment` · `draft-footnotes-annotations` ·
  `diagram-editing-and-chunk-binding` · `draft-refresh` ·
  `draft-inline-editor`.

## Parked

- **convert-remaining-markdown-list-drafts** — unparks when a markdown-list draft
  is next needed as a real draft.
- **dr42995-boxel-draft** · **dft-hbond-draft-27-paywalled-dois** — one-draft
  content chores, not tooling; unpark with Reto's word on that draft.
- **Patents** — `patent-drafting-merge` · `patent-authoring-loop` (status
  draft) · `fto-defensive-publication`: dormant on Reto's word (2026-10-01);
  unparks when he names patent work.
- **Source-backfill follow-ups** — a HyDE query lens, a Tier-1 relevance cull for
  candidate lists, and an `integrate` planner coroutine that walks accepted
  candidates into the draft; owner `src/precis/backfill/candidates.py`, each
  independently shippable, needs design per piece. Was
  `source-backfill-followups`.

## No action needed

- (none)

## Seam

- `knowledge-mesh.md` Horizon holds draft-linearization (the draft as a graph
  view); this thread holds authoring and export of the draft itself. Neither
  reorders the other.
- `graph-memory-consumers.md` parks gr454753/gr454749 (export gate) behind its
  Do-next 1 (`memory-native-authoring`); if the October triage hits them they move
  here.
- `claims-and-evidence.md` owns the claim side of cite-time attach.

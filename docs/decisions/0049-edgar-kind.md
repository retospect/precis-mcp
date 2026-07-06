# ADR 0049 — the `edgar` kind: SEC filings + quarter-to-quarter diff

- **Status**: accepted (2026-07-05)
- **Deciders**: Reto + agent
- **Builds on**:
  - `docs/design/edgar-kind-spec.md` — the plan this ADR ratifies
  - ADR (patent kind) / `handlers/patent.py` — the read-only,
    fetch-as-ingest, local+remote-merge template mirrored here
  - ADR 0007 — derived-queue lazy embedding (no synchronous embed)
  - `KindSpec.corpus_role='evidence'` / `role='corpus'` precedent
  - migration `0053_edgar_kind.sql`

## Context

Agents on the cluster search papers (academic) and patents (EPO OPS).
Company disclosure — SEC filings — is the third public-record corpus and
answers a different question: *what has a company told the market, and
when*. 10-K / 10-Q / 8-K / S-1 are the high-value shapes.

The `edgar` kind extends precis with a durable disclosure corpus in the
same address space as `paper` and `patent`, with the same search +
get-as-ingest loop. Beyond the base spec, a stakeholder requirement
landed during the build: **identify and tag the interesting bits so we
can compare quarter to quarter.**

## Decisions

1. **Backend: key-less SEC APIs, own `httpx` shim.** Full-text search
   (`efts.sec.gov`), submissions index (`data.sec.gov`), and the filing
   archive (`www.sec.gov`). No credentials — only a descriptive
   `User-Agent` (SEC blocks requests without one). The kind gates on
   `PRECIS_EDGAR_USER_AGENT` + `PRECIS_EDGAR_RAW_ROOT`. We wrote a thin
   shim (`_edgar_client.py`) rather than depend on `sec-edgar-api`, and
   reused the existing top-level `httpx` dep — **no new top-level
   dependency** (AGENTS.md).

2. **Client-side rate limiting.** SEC just blocks abusers (no throttling
   headers). A token bucket in the client self-throttles at the 10 req/s
   courtesy limit.

3. **Store filings whole.** No per-filing block cap, no truncation — a
   full 10-K ingests in its entirety. The only splitting is the natural
   paragraph/section split into blocks.

4. **Section identity at the block level.** `_edgar_sections.py`
   classifies each heading into a canonical section (`item-1a`,
   `item-2.02`, `prospectus-summary`, …); `_edgar_ingest.py` stamps every
   block with `chunk_kind='edgar_section'`, `section_path`, and
   `meta.item_code`. Section identity lives on the block (not a ref tag)
   so search can scope to a section and the diff can align sections.

5. **Lazy embeddings from day one.** Ingest writes blocks with NULL
   embeddings; the `embed:bge-m3` derived-queue worker vectorises them
   (ADR 0007). No synchronous embed in the verb (the mistake patent
   originally made).

6. **Accession is the slug.** Canonical dashed `0000320193-23-000106`;
   the dashless archive form is accepted and re-dashed. `cik:` / `ticker:`
   handles resolve to a company's recent-filings **list view** (no
   ingest), mirroring patent's list handles.

7. **Quarter-to-quarter comparison (new capability).** A `_edgar_diff.py`
   layer aligns a filing against the **prior same-form filing for the
   same CIK** (10-Q vs prior 10-Q, 10-K vs prior 10-K), section by
   section on the canonical section id, and surfaces the material changes
   (added / removed sections, paragraph-level additions such as new risk
   factors). The compute step is pure (no writes, no network) and works
   on already-ingested filings, so it is unit-testable in isolation. It
   surfaces three ways (stakeholder chose all three):
   - **Tags + `view='diff'`** — the diff stamps `changed:<canonical_id>`
     and `new-risk-factor` open tags on the current filing and renders a
     section-by-section delta on demand.
   - **Findings** *(phase 2)* — each material change can mint a `finding`
     ref linked to both filings for a durable, chase-able record.
   - **Morning brief** *(phase 2)* — material changes mint a `news` ref so
     quarter-over-quarter changes fold into the 06:00 briefing, extending
     the spec's notability→news pipeline.

   Phase 1 ships the compute + tags + `view='diff'`; the findings/news
   minting rides the phase-2 `edgar_watches` runner.

## Consequences

- A new content `chunk_kind` (`edgar_section`) is added; it is a content
  kind, so the `chunk_keywords` + embed workers claim it automatically
  (`view='toc'` works with no per-kind wiring).
- The diff's "material change" predicate is deliberately simple
  (SequenceMatcher ratio floor + paragraph-level set diff after
  whitespace/case normalisation); cosmetic churn and reordering are
  filtered. A semantic / sentence-level diff is a future refinement.
- `edgar` reaches outside its own tables only at the phase-2 news-minting
  step (into `news`), a deliberate reuse of the news→briefing pipeline.

## Out of scope (v1)

- XBRL `companyfacts` / `companyconcept` financial-fact APIs (numeric
  timeseries, not document retrieval).
- Exhibits (EX-*) as additional blocks — link-only for now.
- The phase-2 `edgar_watches` saved-search runner, notability predicate,
  and CLI (`docs/design/edgar-kind-spec.md` § Phase 2).

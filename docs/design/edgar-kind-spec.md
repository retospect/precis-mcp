# `edgar` — read-only SEC EDGAR filings kind

> Status: **draft plan**. Modelled directly on the `patent` kind
> (EPO OPS). Read `docs/user-facing/patent-kind-spec.md` first —
> this doc only calls out where `edgar` diverges from that
> template. New optional deps group `edgar`. Unlike `patent`, the
> SEC APIs need **no credentials** — only a descriptive
> `User-Agent`. The kind therefore gates on a single required env
> var (`PRECIS_EDGAR_USER_AGENT`) plus the raw-cache root.

## Why

Agents on the cluster search papers and patents; company
disclosure (SEC filings) is the third public-record corpus that
answers a different question — *what has a company told the market,
and when*. 10-K (annual report), 10-Q (quarterly), 8-K (material
event), S-1 (IPO registration), and the ownership forms (3/4/5) are
the high-value shapes. "k7" in the request maps to this family;
the canonical high-value forms are **10-K / 10-Q / 8-K**.

`edgar` extends precis with a durable disclosure corpus alongside
`paper` and `patent`, in the same address space, with the same
search + get-as-ingest loop.

## Backend (decided: full-text + submissions API)

Three free SEC endpoints, no API key, shared **10 req/s** limit and
a **mandatory descriptive `User-Agent`** (SEC blocks requests
without one):

| Endpoint | Base | Use |
|----------|------|-----|
| Full-text search | `https://efts.sec.gov/LATEST/search-index?q=...` (JSON) | search leg — full-text over filings from 2001+ |
| Submissions | `https://data.sec.gov/submissions/CIK##########.json` | company → recent-filings index (form, accession, date) |
| Filing archive | `https://www.sec.gov/Archives/edgar/data/<cik>/<accession-nodash>/<primary-doc>` | get-as-ingest — the actual filing document (HTML/XML) |

- Free, no per-call cost; the only budget is the 10 req/s throttle
  and courtesy volume — we mirror patent's fair-use accounting to
  stay well-behaved.
- Full US coverage back to 2001 (full-text); submissions index
  covers all filers.
- Stable, documented JSON. A maintained wrapper exists
  (`sec-edgar-api`) but it's thin; we prefer our own `httpx` shim
  (mirrors `_patent_ops.py`) to keep the surface small + testable
  and avoid a heavy dep.

USPTO-style bulk downloads and the XBRL `companyfacts`/`companyconcept`
financial-fact APIs are **out of scope for v1** — they answer a
numeric-timeseries question, not a document-retrieval one. Tracked
as a follow-up if usage shows demand.

## Mental model — identical to patent

| Layer | Postgres | Created by | Retention |
|-------|----------|------------|-----------|
| 1 | `cache_state` | `search(...)` — EDGAR full-text hit-list cache | 7 days |
| 2 | `refs` + `blocks` | `get(id=...)` — durable, embedded | perpetual |

`search` merges layer 1 + layer 2 by accession number.
`get(id=<accession>)` is the only way to materialise a layer-2 row:
on miss it fetches the filing document, parses it into blocks,
embeds lazily via the `embed:bge-m3` worker (ADR 0007 derived
queue — **not** synchronously; patent already fixed this), and
inserts. Getting a filing IS the ingest.

## Slug shape

Canonical slug = the **accession number**, dashes stripped and
lowercased is unnecessary (accessions are digits + dashes only):
store the canonical dashed form `0000320193-23-000106`.

- Regex: `^\d{10}-\d{2}-\d{6}$`.
- Normalisation: strip whitespace; accept a dashless
  `000032019323000106` and re-insert dashes (the SEC archive URL
  uses the dashless form, so we carry both). Reject anything else
  with a `BadInput` + recovery hint — same discipline as
  `parse_docdb_id`.
- A `cik:` handle (`get(id='cik:320193')` or ticker
  `get(id='ticker:aapl')`) resolves to a **list view** of that
  company's recent filings (via submissions API), mirroring
  patent's `/recent` + `/published` list handles. It does not
  ingest; it lists, and each row links to a `get(id=<accession>)`.

New helper module `_edgar_accession.py` (mirrors `_patent_slug.py`):
`Accession` dataclass (`cik`, `year2`, `seq`, dashed/dashless
forms, archive subpath) + `parse_accession` + `looks_like_accession`.

## Surface (cross-kind contract — same as patent)

```python
# Search: merged local + remote, remote cached 7d
search(kind='edgar', q='climate risk disclosure')
search(kind='edgar', q='revenue recognition', page_size=20)

# Tag filters lift to EDGAR full-text query params on the remote leg
search(kind='edgar', q='going concern', tags=['form:10-k'])
search(kind='edgar', q='cyber incident', tags=['form:8-k', 'cik:320193'])

# Get: render (and persist on cache miss)
get(kind='edgar', id='0000320193-23-000106')            # overview
get(kind='edgar', id='0000320193-23-000106', view='body')
get(kind='edgar', id='0000320193-23-000106', view='biblio')
get(kind='edgar', id='0000320193-23-000106~5..12')      # chunk nav

# List views
get(kind='edgar')                       # recently ingested (local)
get(kind='edgar', id='/recent')         # alias
get(kind='edgar', id='cik:320193')      # company's recent filings (remote index)
get(kind='edgar', id='ticker:aapl')     # same, ticker→CIK resolved

# put: unsupported (read-only public record)
put(kind='edgar', ...)                  # raises Unsupported
```

### Tag → EDGAR full-text query lift (`_edgar_query.py`)

Mirrors `_patent_cql.py::build_cql`, but the EDGAR full-text API
takes structured query params rather than CQL, so the "lift" builds
a param dict instead of a string:

| Open tag prefix | EDGAR FTS param | Example |
|-----------------|-----------------|---------|
| `form:` | `forms` | `form:10-k` → `forms=10-K` |
| `cik:` | `ciks` (zero-padded 10) | `cik:320193` → `ciks=0000320193` |
| `ticker:` | resolved to `ciks` | via `company_tickers.json` map |
| `dateRange` (future) | `startdt`/`enddt` | deferred to search-future-filters |

Open prefixes with no FTS equivalent (`topic:`, `project:`) narrow
only the local SQL leg — same rule as patent. `q=` free text goes
to the `q=` FTS param verbatim (no auto-promote gymnastics needed;
EDGAR FTS is already a keyword engine).

### Closed-axis whitelist

Add to `store/types.py::_KIND_ALLOWED_AXES`:

```python
# EDGAR filings are public record; SRC (primary/secondary provenance)
# + CACHE (cluster cache discipline). No STATUS lifecycle.
"edgar": frozenset({"SRC", "CACHE"}),
```

Auto-tags applied at ingest (lowercase open prefixes, best-effort
like patent): `form:<lower>`, `cik:<digits>`, and optionally
`fiscal-year:<yyyy>`. Company name / ticker stay in `refs.meta`
(structured JSONB), not as tag rows — same anti-clutter lesson as
patent's applicant/CPC removal (2026-06-16).

## DB layout

### Migration `0053_edgar_kind.sql`

Head is currently `0052_pdf_locations.sql`; the new file slots in
as **0053**. Registers the kind + providers (mirrors
`0012_epo_ops_provider.sql` shape — see that file, not the stale
inline SQL in the patent spec):

```sql
INSERT INTO kinds (slug, title, description, supports_get,
                   supports_search, supports_put, is_numeric, is_file)
VALUES ('edgar', 'SEC Filing',
        'Read-only SEC EDGAR filing. Accession-slugged. Search '
        'merges local + EDGAR full-text; get(id=...) fetches + stores.',
        TRUE, TRUE, FALSE, FALSE, FALSE);

INSERT INTO providers (slug, title, base_url, kind, attribution_template)
VALUES
  ('sec_edgar', 'SEC EDGAR', 'https://www.sec.gov',
   'edgar',
   '_Source: SEC EDGAR — https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&...'),
  ('sec_edgar_search', 'SEC EDGAR — full-text search',
   'https://efts.sec.gov', 'cache',
   '_See EDGAR full-text search: https://efts.sec.gov/LATEST/search-index?q={query}_');
```

Follow the live `kinds`/`providers`/`kind_provider` column contract
in `0012_epo_ops_provider.sql` + `0022_kind_provider.sql` verbatim
— don't trust the patent spec's older inline column list.

### Saved-watch table `0054_edgar_watches.sql` (phase 2)

Clone `patent_watches` (see `migrations/archive/0014_patent_watches.sql`)
as `edgar_watches`, swapping `cql TEXT` for `query JSONB` (the FTS
param dict) + `last_seen_accession TEXT[]`. Same due-index shape.
DAO `_edgar_watch_db.py` mirrors `_patent_watch_db.py`.

### `refs` / `blocks` reuse

```
refs.kind     = 'edgar'
refs.slug     = '<accession dashed>'
refs.title    = "<company> — <form> (<period/filed date>)"
refs.provider = 'sec_edgar'
refs.meta     = {
  "accession": "0000320193-23-000106",
  "cik": "320193",
  "company": "Apple Inc.",
  "ticker": "AAPL",
  "form": "10-K",
  "filed_date": "2023-11-03",
  "period_of_report": "2023-09-30",
  "primary_doc": "aapl-20230930.htm",
  "items": ["1A", "7", "7A"],          # 8-K item codes / 10-K sections
  "fair_use_bytes": 812345
}
```

`blocks`: one block per parsed section/paragraph of the primary
document (10-K item, 8-K item, narrative paragraph), density-
classified via `precis.ingest.blocks.classify_density`, embedded
lazily by the derived-queue worker.

**Filing size — store it whole (decided).** No per-filing block cap
and no truncation: a full 10-K (300k+ tokens) is ingested in its
entirety. We have the disk + Postgres space, and a truncated filing
is a foot-gun for search. The raw document is also mirrored under
`$PRECIS_EDGAR_RAW_ROOT` for re-parse. The only splitting we do is
the natural paragraph/section split into blocks (needed for
block-level search + TOC clustering anyway).

### Section tagging — standard sections are searchable (decided)

Standard filing sections carry stable, well-known structure; the
parser labels each block with its section so search can scope to a
section. Section identity lives at the **block level** (not ref
tags — a ref tag would apply to the whole filing), stored on
`blocks.meta.section_path` + a `chunk_kind`, matching how
paper-ingest stamps `section_path` / `chunk_kind` per Marker block:

| Form | Standard sections labelled |
|------|----------------------------|
| 10-K / 10-Q | Item 1 Business, Item 1A Risk Factors, Item 3 Legal Proceedings, Item 7 MD&A, Item 7A Market Risk, Item 8 Financial Statements, … |
| 8-K | Item codes (1.01 Entry into Material Agreement, 2.02 Results of Ops, 5.02 Officer/Director changes, 7.01 Reg FD, 8.01 Other, …) |
| S-1 | Prospectus Summary, Risk Factors, Use of Proceeds, MD&A, Business |

- `_edgar_parse.py` owns the section classifier: a table of
  regex/heading patterns per form → canonical section id
  (`item-1a`, `item-2.02`, …). Unrecognised headings fall back to
  `section:body`.
- Each block gets `chunk_kind='edgar_section'` +
  `meta.section_path=['Item 1A', 'Risk Factors']` +
  `meta.item_code='1a'`. `store.search_blocks(scope=...)` and the
  `tags=`/section filter can then narrow to "risk factors across
  all 10-Ks", etc.
- The distinct `item_code` set per filing is also summarised into
  `refs.meta.items` (above) for the overview + list views.

### Raw cache on disk (`$PRECIS_EDGAR_RAW_ROOT`)

Same rationale as patent (`$PRECIS_PATENT_RAW_ROOT`): Postgres holds
parsed/queryable state, filesystem holds raw upstream artefacts.
Layout mirrors the EDGAR archive URL space:

```
$PRECIS_EDGAR_RAW_ROOT/
└── 320193/                        # CIK
    └── 000032019323000106/        # accession, dashless (archive form)
        ├── submission.json        # submissions-API slice
        ├── primary.htm            # primary document as fetched
        └── ingest.log             # JSONL: timestamps + bytes per call
```

## TOC + summary conveniences (decided: reuse the generic path)

These are already kind-agnostic — `edgar` gets them almost for free:

- **`view='toc'`** — `precis.utils.toc_db.render_from_store(store=,
  ref_id=, handle=, kind='edgar', scope=)` clusters the filing's
  blocks by their per-chunk keyword sets and renders a drill-able
  TOON table. It keys only on `chunks.keywords` + the record handle,
  so wiring it is: add `"toc"` to `_SUPPORTED_VIEWS` and route
  `view='toc'` (and the `~lo..hi/toc` range form) to
  `render_from_store` — exactly as `paper._render_toc` does. A
  300k-token 10-K is precisely where this pays off.
- **`view='summaries'`** — per-chunk glosses render from
  `chunk_summaries`, populated by the summarize worker. Add
  `"summaries"` to `_SUPPORTED_VIEWS`.
- **Chunk keywords come free from the derived-queue worker.**
  `chunk_keywords` claims any chunk whose `chunk_kind` is *not* on
  its skip-list and whose length ≥ 150 chars. `edgar_section` is a
  content kind, so blocks are keyworded automatically once embedded
  — no per-kind wiring, no backfill. (Confirm `edgar_section` is
  absent from `_SKIP_KINDS` in `workers/chunk_keywords.py`; it is,
  since that set is paper front-matter/table/figure kinds.)
- **Abbreviation legend + shared-phrases footer** ride along inside
  `render_from_store`, so a filing's defined-terms glossary surfaces
  in its TOC with no extra code.

Net: the only handler code is adding two view names and a
`_render_toc` that calls the shared renderer. The heavy lifting is
the derived-queue workers that already run cluster-wide.

## New / unusual filings → morning news report (decided)

No new briefing plumbing: the `briefing` worker
(`workers/briefing.py`) already summarises recent **`news`** refs
and renders each as a clickable markdown link from its `meta.url`.
So "surface unusual/new filings in the morning report" =
**the edgar watcher mints a `news` ref for notable filings**, and
the existing brief folds it in on the next 06:00 tick.

Mechanism (in the phase-2 `edgar_watches` runner, mirroring
`news_poll`'s minting call):

```python
store.put_cache_entry(
    kind="news", provider="news", request_hash=<accession-hash>,
    slug=slug_from_text(url), title=f"{company} filed {form} — {headline}",
    body_blocks=article_blocks(summary_text, embedder=None),  # lazy embed
    ttl_seconds=None,                                          # pinned record
    ref_meta={"url": <espacenet-style edgar filing url>, "source": "edgar"},
    cache_meta={"source": "edgar", "form": form, "cik": cik},
)
# then apply_tag_ops(..., tags=["category:news", "source:edgar",
#                               f"form:{form.lower()}", f"published:{filed}"])
```

**What counts as "notable" (the trigger, tunable):**

- a **form type never before seen for that CIK** (novelty per
  company — the "new things" case);
- a filing whose form is in a curated **notable-forms set** —
  `8-K` with high-signal item codes (1.03 bankruptcy, 2.01
  acquisition/disposition, 5.02 exec change), `S-1` (IPO),
  `SC 13D` (activist stake), `DEFM14A` (merger vote),
  `NT 10-K`/`NT 10-Q` (late-filing notice);
- a **form the SEC itself is new/rare** (unrecognised form code)
  → always notable, so genuinely novel disclosure types bubble up.

Routine, expected filings (a quarterly 10-Q from a company that
files them every quarter) do **not** mint news — they still ingest
as `edgar` refs and are searchable, they just don't spam the brief.
The notability predicate lives in `_edgar_notable.py` with a table
of item codes + form allow-list, unit-tested in isolation.

The news ref links straight to the EDGAR filing page, so the
morning brief's rendered link opens the actual document.

## Implementation (files mirror patent 1:1)

```
# Phase 1 — ingest + search + get
src/precis/handlers/edgar.py             # EdgarHandler (mirror patent.py)
src/precis/handlers/_edgar_accession.py  # Accession + parse (mirror _patent_slug.py)
src/precis/handlers/_edgar_client.py     # httpx shim + FakeEdgarClient (mirror _patent_ops.py)
src/precis/handlers/_edgar_parse.py      # filing HTML/XML → ParsedFiling (mirror _patent_xml.py)
src/precis/handlers/_edgar_query.py      # tag → FTS param lift (mirror _patent_cql.py)
src/precis/handlers/_edgar_sections.py   # form → section/item classifier (block labels)
src/precis/handlers/_edgar_ingest.py     # fetch → refs+blocks (mirror _patent_ingest.py)
src/precis/migrations/0053_edgar_kind.sql
src/precis/data/skills/precis-edgar-help.md
tests/test_edgar_accession.py
tests/test_edgar_query.py
tests/test_edgar_sections.py
tests/test_edgar_parse.py
tests/test_edgar_ingest.py
tests/test_edgar_handler.py             # incl. view='toc' + view='summaries'

# Phase 2 — saved full-text watches + morning-report minting
src/precis/migrations/0054_edgar_watches.sql
src/precis/handlers/_edgar_watch_db.py
src/precis/handlers/_edgar_notable.py     # notability predicate (form/item allow-list)
src/precis/cli/edgar.py                   # watch-edgar / list / run (mirror cli/patent.py)
tests/test_edgar_watch_db.py
tests/test_edgar_notable.py
tests/test_edgar_watch_cli.py
```

### Dispatch registration (`dispatch.py`)

Mirror the patent block (`dispatch.py` ~836–869): gate on
`EdgarHandler.spec.requires_env` (`PRECIS_EDGAR_USER_AGENT`,
`PRECIS_EDGAR_RAW_ROOT`) via `_gated`. Probe `importlib.util.find_spec`
for the HTTP dep (`httpx`, already a dep for web/news — so likely no
new probe needed) and surface a `Loadability` reason if missing.

### Optional dependency group (`pyproject.toml`)

```toml
[project.optional-dependencies]
edgar = [
    "lxml>=5.0",          # HTML/XML filing parse (already used by patent)
    "selectolax>=0.3",    # optional fast HTML text extraction — evaluate vs lxml
]
```

`httpx` is already a top-level/handler dep (web, news). If `lxml`
suffices for filing HTML, `edgar` may need **no new top-level
dep** — confirm before adding one (AGENTS.md "don't introduce a new
top-level dependency without an ADR").

## Divergences from patent — the parts that need real design

1. **No OAuth / no credentials.** SEC needs only a descriptive
   `User-Agent`. Env gate shrinks to
   `PRECIS_EDGAR_USER_AGENT` + `PRECIS_EDGAR_RAW_ROOT`. Simpler
   than patent's key/secret pair.
2. **Rate limit is client-side.** EPO returns throttling headers;
   SEC just blocks abusers. We add a token-bucket (10 req/s) in
   `_edgar_client.py` + reuse patent's rolling fair-use byte
   accounting to self-throttle.
3. **Filings are huge, but we store them whole (decided).** A 10-K
   dwarfs a patent; we ingest it in full — no cap, no truncation.
   The only structure work is the section classifier
   (`_edgar_sections.py`): label each block with its 10-K Item /
   8-K item code so search can scope to a section. **Open
   sub-question:** whether to also ingest exhibits (EX-*) as
   additional blocks or leave them link-only — lean *link-only* in
   v1, revisit if search demand shows exhibits matter.
4. **Search backend is JSON params, not CQL.** `_edgar_query.py`
   builds a param dict; no auto-promote heuristic and no
   `validate_strict_cql` bare-keyword guard needed (EDGAR FTS is a
   keyword engine). Watches store a JSONB param dict.
5. **Ticker→CIK resolution.** Needs the SEC
   `company_tickers.json` map, cached locally with a TTL. New
   helper; no patent analog.
6. **Embeddings lazy from day one.** Patent originally embedded
   inline and had to be fixed; `edgar` uses the derived-queue
   worker (ADR 0007) from the start — no synchronous embed in the
   verb.
7. **Cross-kind spillover into `news`.** Unlike patent (self-
   contained), the edgar watcher writes into a *different* kind
   (`news`) for notable filings so the morning brief picks them
   up. This is a deliberate reuse of the news→briefing pipeline,
   not a new subsystem — but it's the one place `edgar` reaches
   outside its own tables, so it's called out for review.

## Build order

1. `_edgar_accession.py` — `Accession` + `parse_accession` + tests.
2. `_edgar_query.py` — tag→FTS-param lift + tests.
3. `_edgar_client.py` — `httpx` shim (search / submissions /
   archive-doc) + `FakeEdgarClient` + token bucket. Live test gated
   on `PRECIS_EDGAR_TEST_LIVE=1`.
4. `_edgar_sections.py` — form → canonical section/item classifier
   + tests (10-K Items, 8-K item codes, S-1 sections).
5. `_edgar_parse.py` — filing → `ParsedFiling` (title, sections via
   the classifier, items, block texts); fixture-driven tests on a
   real 10-K / 8-K.
6. `_edgar_ingest.py` — fetch → write raw → parse → refs+blocks
   (lazy embed, section labels on `blocks.meta`) → auto-tags.
   Idempotent on accession.
7. `EdgarHandler.get(id=)` — render local; on miss ingest;
   `cik:`/`ticker:` list views; `view='toc'` + `view='summaries'`
   via the shared renderers.
8. `EdgarHandler.search(...)` + `search_hits(...)` — remote leg
   cache-backed via `_cache_base.py`, local leg via
   `store.search_blocks` (section-scopeable); `merge_and_render`
   with `[local]` marks.
9. `0053_edgar_kind.sql` + dispatch gate + `_KIND_ALLOWED_AXES` +
   `precis-edgar-help` skill.
10. Phase 2: `edgar_watches` + DAO + `_edgar_notable.py` +
    news-minting in the runner + `cli/edgar.py` + Ansible
    follow-up for the watch runner (mirror the patent launchd job).
    Verify a notable filing surfaces in the next `run_briefing`.

## Definition of done (per AGENTS.md)

- This plan reviewed; ADR **0049-edgar-kind.md** for the
  substantive trade-offs (backend choice, no-XBRL scope,
  store-whole-filing decision, notability→news spillover, dep
  decision).
- Migration `0053` applies cleanly to a fresh DB; only the new
  file pending on `precis migrate --dry-run`.
- `uv run ruff check . && uv run ruff format --check . && uv run
  mypy src tests` pass; full suite green in the dev container
  (`scripts/dev pytest`).
- `uv version` bumped; conventional-commit message.
- CLI (phase 2) has `--help`, an integration test, and a README
  line.

## Quarter-to-quarter comparison (added 2026-07-05)

Stakeholder requirement beyond the original plan: **identify and tag the
interesting bits so we can compare quarter to quarter.** This rides
entirely on the block-level section labels above — the same
`meta.canonical_id` / `section_path` the parser stamps is what a
comparison layer aligns on.

New module `_edgar_diff.py` (phase 1 for compute + tags + view; phase 2
for the minting surfaces):

- **Align.** `find_prior_ref(store, ref)` finds the most-recent
  locally-ingested **same-form** filing for the same CIK with an earlier
  period/filed date (10-Q vs prior 10-Q, 10-K vs prior 10-K).
- **Diff.** `diff_sections(current, prior)` is a **pure** function over
  the two filings grouped by `canonical_id`. It reports added / removed
  sections and, for changed sections, paragraph-level additions/removals
  (new risk factors, changed MD&A). "Material" = raw-text SequenceMatcher
  ratio below `_MATERIAL_CHANGE_RATIO` **and** a non-empty paragraph-level
  set difference (so whitespace churn / reordering are filtered).
- **Surface (all three, per the stakeholder):**
  - `view='diff'` on the handler renders the section-by-section delta and
    stamps `changed:<canonical_id>` + `new-risk-factor` open tags on the
    current filing (so `search(tags=['changed:item-1a'])` finds every
    filing whose risk factors moved).
  - **Findings** — each material change mints a `finding` linked to both
    filings (phase 2, in the watch runner).
  - **Morning brief** — material changes mint a `news` ref so the change
    folds into the 06:00 briefing (phase 2, extends notability→news).

Build order: `_edgar_diff.py` slots after step 7 (the handler wires
`view='diff'`); the findings/news minting is part of step 10 (phase 2).
Tests: `tests/test_edgar_diff.py` (pure `diff_sections` + `diff_tags`)
plus the end-to-end diff case in `tests/test_edgar_handler.py`.

## Configuration summary

```bash
# Required for the kind to register
PRECIS_EDGAR_USER_AGENT="precis-mcp/x.y (you@example.com)"   # SEC mandates a descriptive UA
PRECIS_EDGAR_RAW_ROOT=/opt/nfs/shared/edgar                  # raw filing cache on disk

# Optional
PRECIS_EDGAR_FAIR_USE_LIMIT_GB=3        # rolling 7-day warn-and-pause (courtesy)
# No block cap — filings are stored whole (see § Filing size).
```

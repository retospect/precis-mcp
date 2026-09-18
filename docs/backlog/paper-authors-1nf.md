---
status: ready
title: paper_authors 1NF table + tiered author identity (ORCID → Crossref/OpenAlex → name-only) + author= search fix + meta dialog
prio: high
model: sonnet
---

# paper_authors 1NF table + tiered author identity

## Motivation / why

Authors live as a repeating group — `refs.authors` jsonb — in two shapes
(`{name}` legacy, 79K entries / 12.3K papers; `{given,family}` canonical,
43K / 6.7K) with no convention inside the `{name}` strings ("Family,
Given", "Given Family", initials-first, jammed). Consequences measured on
prod 2026-09-18:

* `search(kind='paper', author=…)` reads `elem->>'name'` only, so every
  canonical-shape paper (and every new ingest) is invisible to it —
  pa346493's "Ronggang Luo" is not found by `author='Ronggang Luo'`.
* 22,464 live papers have `authors IS NULL` — 13,101 of them held PDFs,
  12,954 of those with a DOI. `paper_meta_enrich` would fill them from
  Crossref but claims 50 papers every 6 h (33,843 unvisited ≈ 170 days).
* Identity: 12,008 `kind='orcid'` nodes exist (all consistent with the
  bylines), but 12,004 are name+iD stubs (4 fetched), 0 of 13,028
  `authored` edges carry `author_position`, and the 89 % of byline entries
  without an ORCID have no person identity at all (~97K distinct
  name-strings).

Decisions taken with Reto (2026-09-18 session):

* **Table is truth, jsonb is a projection.** `refs.authors` stays as the
  read-compat surface for the 30+ `author_names` readers and the ingest
  input shape; every write goes through one store method that writes the
  table and regenerates the jsonb.
* **`middle` is a real column**, `NOT NULL DEFAULT ''`. No source
  separates it, so the write rule derives it: split `given` on its
  trailing run of dotted/single-letter tokens — `"Bryan R."` → given
  `Bryan`, middle `R.`; `"J. Robert"` → given `J. Robert`, middle `''`
  (leading initial is a first name); `"Mary Anne"` → given `Mary Anne`,
  middle `''`; `"A.-K."` → given `A.-K.` (hyphenated initials stay put,
  cf. `_tidy_initials`). `source='human'` rows are never re-split.
* **Identity columns**: `orcid` (tier 1) and `openalex_author_id` (the
  fallback identity for authors without an ORCID — OpenAlex's own
  disambiguated `A…` id, already in the `authorships` block
  `openalex_meta.py::_authorships` parses and drops). Google Scholar is
  links-only (no API, scraping is ToS-banned and CAPTCHA'd).
* **ORCID records are fetched in the background, gently** — a registry
  worker over the stub nodes, not a burst.
* Affiliation stays off the table (the orcid node's `meta.employments`
  and the paper's `meta.openalex.authorships` already hold it).

## In scope

### S0 — `author=` search fix (independent; first commit)

`store/_refs_ops.py::find_papers_by_author`: match on
`coalesce(elem->>'name', concat_ws(' ', elem->>'given', elem->>'family'))`
and additionally on the reversed `family, given` form so a "Miller, T."
query hits a `{given,family}` row. Test: a `{given,family}`-only paper is
found by surname, by "Given Family", and by "Family, Given". S1 then
re-points this query at the table; the coalesce is the interim fix. File a
gripe for the finding; close it on ship.

### S1 — schema + write choke point + projection + backfill

* Migration `0168_paper_authors.sql`:
  ```
  paper_authors(
    ref_id              bigint NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    position            smallint NOT NULL,                -- 1-based byline order
    given               text NOT NULL DEFAULT '',
    middle              text NOT NULL DEFAULT '',
    family              text NOT NULL DEFAULT '',
    name_raw            text NOT NULL,                    -- the string we received, never rewritten
    orcid               text,                             -- dashed iD, CHECK format
    openalex_author_id  text,                             -- 'A1234567890'
    person_ref_id       bigint REFERENCES refs(ref_id) ON DELETE SET NULL,  -- the kind='orcid' node
    source              text NOT NULL,                    -- orcid|crossref|openalex|s2|pdf|legacy|llm|human
    verified_at         timestamptz,                      -- ORCID cross-check or human edit
    updated_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ref_id, position),
    CHECK (source IN (...)),
    CHECK (given <> '' OR family <> '' OR name_raw <> '')
  )
  ```
  Indexes: `lower(family)` btree; gin trigram on
  `(given || ' ' || middle || ' ' || family)`; `orcid` btree partial NOT
  NULL; `openalex_author_id` btree partial; `person_ref_id`.
  The migration also **projects every existing jsonb byline into the
  table in SQL** (`jsonb_array_elements … WITH ORDINALITY`; `{name}` →
  `name_raw` + single-comma split when unambiguous, else family=''
  given='' and the name kept in `name_raw`; `{given,family}` → columns
  with `middle=''`; `orcid` key carried; `source='legacy'`) so search
  never goes dark between migrate and backfill. Register the table in
  the schema docs (`docs/reference/schema.md` is generated — `scripts/bump`).
* `utils/authors.py`: `split_middle(given) -> (given, middle)` implementing
  the rule above; `author_row_from_entry(entry, position) -> dict` (the
  jsonb → row mapping, used by the store); `entry_from_author_row(row)
  -> dict` (the projection back: `given` = `given + ' ' + middle` when
  middle, `family`, `orcid` carried; `{name: name_raw}` when both name
  columns are empty).
* `store/_refs_ops.py::set_paper_authors(ref_id, rows | entries, *, source,
  conn=None)` — the ONE writer: DELETE+INSERT the paper's rows, regenerate
  `refs.authors` jsonb from the rows, log `ref_events` `authors_set`.
  If the paper already has any `source='human'` row and the incoming
  write is not human, the write is a no-op (returns the existing rows) —
  the paper-level `human_verified_at` guard, enforced at the store so no
  writer can forget it. Tier 1's cross-check still stamps `verified_at`
  on a human row (names untouched).
  Routing is inside the store, not at call sites: `update_paper_fields`
  projects whenever `authors is not None` and the ref's kind is `paper`
  (gains `authors_source=`; default derives from its `source` — the
  `web-edit`/MCP-edit doors → `human`, otherwise `pdf`); `insert_ref(kind=
  'paper', authors=…)` likewise; `ingest/db_writer.py`'s raw `INSERT INTO
  refs` calls the projection right after `RETURNING ref_id` (`source='pdf'`,
  or `'s2'`/`'crossref'` when the writer knows the lookup provenance). Every
  existing caller (`metadata_resolve`, `remediate`, `openalex_meta`,
  `paper_meta_enrich`, handlers) is covered without edits; non-paper kinds
  stay jsonb-only. **`cite_key` is
  never touched** by an author write: it is a stored identifier coupled
  to the corpus PDF path (`corpus_layout.py::corpus_pdf_dest`, gripe
  264184 comment 2) — a byline correction must not relocate a file.
* `precis paper authors-resplit` CLI: walks `source='legacy'` rows and
  applies `split_middle` + the Python `normalize_authors` heuristics the
  SQL projection couldn't (jammed initials, junk guard). One-time
  post-deploy run; idempotent.
* Drift health check (`health_checks.py`): papers whose
  `jsonb_array_length(authors)` ≠ `count(paper_authors)` → warn (catches
  ad-hoc SQL writes to the jsonb).
* `find_papers_by_author` moves to the table: `lower(family) = lower(q)`
  exact first, then trigram over the full name, held first.

### S2 — lookup tier: throughput + identity capture

* `workers/paper_meta_enrich.py`: `_DEFAULT_BATCH_LIMIT` 50 → 400,
  `_DEFAULT_REFRESH_HOURS` 6 → 1 (Crossref polite pool tolerates far
  more; ~10K/day drains the 33.8K backlog in ~4 days). Env overrides
  unchanged.
* `ingest/paper_meta_enrich.py::enrich_paper` writes through
  `set_paper_authors(source='crossref'|'openalex'|'heuristic'→'pdf')`.
* `ingest/openalex_meta.py::_authorships` keeps `author.id` as
  `openalex_author_id`; the OpenAlex leg of `enrich_paper` merges it onto
  the Crossref rows by position (same count) or by family-name match.
* Semantic Scholar path (`pipeline.py::_paper_from_lookup`) writes with
  `source='s2'`.

### S3 — ORCID tier: background worker + cross-check

* `workers/orcid_enrich.py` (registry entry, throttle key
  `orcid_enrich:last_run`, `PRECIS_ORCID_ENRICH_REFRESH_HOURS` default 1,
  batch 100): claims `kind='orcid'` nodes with `meta.fetched_at IS NULL`
  (newest-linked-paper first), calls `ingest/orcid.py::fetch_record`,
  stores the record on the node (same shape `handlers/orcid.py::get`
  writes), links held works via `enqueue_authored_works(enqueue=0)` (no
  stub minting from a background pass).
* Cross-check per `authored` edge of the node: paper DOI ∈ record works →
  `paper_authors.verified_at = now()` on the matching row (by orcid), and
  the row's names are set from the ORCID record (`given-names` /
  `family-name`, middle via `split_middle`, `source='orcid'`) unless
  `source='human'`; edge meta gets `verified: true`. DOI ∉ works →
  edge meta `orcid_unconfirmed: true`, row untouched.
* Missing `ORCID_CLIENT_ID/_SECRET` (`ingest/orcid.py::has_credentials`)
  → one `alert` row per process start + skip; never a silent idle pass
  (the vault-OAuth outage lesson: an unauthenticated worker must be loud).
* Rate: ≤ 2 req/s inside the batch (ORCID public API allows more; gentle
  by decision).

### S4 — meta dialog

* MCP: `get(kind='paper', id=…, view='authors')` — the ordered rows with
  source, verified tick, and per-author links: ORCID
  (`https://orcid.org/<iD>` + the `or…` node handle), OpenAlex
  (`https://openalex.org/<A…>`), Google Scholar search
  (`https://scholar.google.com/scholar?q="<Given Family>"`); paper-level
  Scholar link `scholar.google.com/scholar_lookup?doi=<doi>` (title
  fallback). `edit(kind='paper', authors=[…])` accepts the row shape
  (`given/middle/family/orcid`) as well as the legacy shapes, writes
  `source='human'`, sets `human_verified_at`.
* Web: `templates/papers/_meta_panel.html.j2` renders the same table
  (position, name, ORCID/OpenAlex/Scholar links, source chip, verified
  tick) instead of the flat `authors_display`; `_meta_forms.html.j2`
  textarea keeps one-author-per-line, grammar `Family, Given Middle
  [0000-0002-1825-0097]` (bracketed ORCID optional); the POST handler
  routes through `set_paper_authors(source='human')`. Paper-level "re-fetch
  from Crossref" button = enqueue the ref for `paper_meta_enrich` (clear
  `meta.authors_resolved_at`).
* Skill `precis-paper-help` (or wherever `author=`/`edit authors=` is
  documented): document `view='authors'`, the row shape, the tiers.

## Explicitly NOT in scope

* The residual LLM/agent pass over rows that survive S1–S3 unsplit or
  with tier disagreements — a follow-up item sized on the post-S3
  residual, not before.
* Name-only person disambiguation (no ORCID, no OpenAlex id).
* Flipping the 30+ `author_names` readers from jsonb to the table.
* `authors` on non-paper kinds (draft byline with affiliations, pres,
  patent inventors) — they keep the jsonb; `paper_authors` is paper-only.
* Fetching all 12K ORCID records in a burst; Scholar as a data source.
* Affiliation columns.

## Acceptance criteria

* S0: test — a paper whose byline is only `{given,family}` is returned by
  `search(kind='paper', author=<surname>)`, `author='Given Family'` and
  `author='Family, Given'`. Prod check after deploy: `author='Ronggang
  Luo'` returns pa346493 first.
* S1: after migrate on a test DB seeded with both jsonb shapes, `SELECT
  count(*) FROM paper_authors` equals the sum of byline lengths; a
  `{name: "Zywucka, N."}` entry projects to family `Zywucka`, given `N.`,
  middle `''`, source `legacy`; a `{given: "Bryan R.", family:
  "Goldsmith"}` entry projects with middle `''` at migrate time and
  middle `R.` after `authors-resplit`. `update_paper_fields(authors=…)` (via the `metadata_resolve` and `remediate` paths too; a `kind='draft'` write creates no rows)
  and an ingest insert both leave table and jsonb equal (round-trip test:
  `entry_from_author_row(author_row_from_entry(e)) == normalize_authors([e])[0]`
  for every fixture shape). A `source='human'` row survives a subsequent
  `source='crossref'` write. Drift health check fires on a hand-edited
  jsonb.
* S2: enrichment on a fixture Crossref+OpenAlex response yields rows with
  `source='crossref'`, `orcid` and `openalex_author_id` populated where
  the fixture has them; the worker's default claim is 400/h.
* S3: worker test with a stubbed `fetch_record`: node gets `fetched_at`,
  the matching row gets `verified_at` + ORCID names + `source='orcid'`, a
  `source='human'` row keeps its names but gets `verified_at`; a DOI not
  in the works leaves the row untouched and stamps the edge
  `orcid_unconfirmed`; with credentials absent the pass emits one alert
  and claims nothing.
* S4: `view='authors'` renders the table with the three link kinds; the
  web meta form round-trips `Family, Given M. [iD]` to a `source='human'`
  row; snapshot/template test for the panel.
* `scripts/test` green; `mypy src tests` clean; schema doc regenerated.

## Target + blast radius

Store `_refs_ops.py` (new writer, search query), `ingest/db_writer.py`,
`ingest/paper_meta_enrich.py`, `ingest/openalex_meta.py`,
`ingest/pipeline.py`, `utils/authors.py`, `handlers/paper.py` (edit,
view='authors'), `handlers/_paper_search.py`, `workers/paper_meta_enrich.py`,
new `workers/orcid_enrich.py` + `workers/registry.py`, `health_checks.py`,
`cli` (authors-resplit), `precis_web` paper meta panel/forms + POST
handler, migration 0168, skill doc. Post-deploy: one prod
`authors-resplit` run (runbook `docs/runbooks/prod-one-off-cli.md`), then
watch `paper_meta_enrich` / `orcid_enrich` counters and the drift check.

## Open questions / decisions log

* **ORCID credentials in prod** — `get(kind='orcid')` fetched 4 nodes today
  (2026-09-18T09:31Z), so *some* process has them; the worker host is not
  confirmed. S3's loud-skip makes absence visible rather than a wedge.
* Enrichment throughput 400/h is a starting point; env-tunable.
* Web textarea grammar `Family, Given Middle [iD]` — simplest that
  round-trips; a row editor is a later polish.
* Middle split for CJK / mononym / `name_raw`-only rows: stays `''`; the
  residual item owns them.

* **[readiness-gate 2026-09-18] blockers — RESOLVED.** (1) The store
  method is `update_paper_fields` (spec text corrected). (2) The projection
  is not a call-site migration: it lives *inside* `update_paper_fields`
  (when `authors is not None`) and `insert_ref` (when `authors` given) and
  right after `db_writer.py`'s raw insert — so `metadata_resolve.py`,
  `remediate.py`, `openalex_meta.py`, `paper_meta_enrich.py` and every
  future caller are covered without being touched. Kind gate: the store
  projects only when the ref's `kind = 'paper'` (one `SELECT kind` in the
  same transaction; `insert_ref` knows it from its argument) — draft /
  patent / datasheet author writes stay jsonb-only, as scoped. Acceptance
  gains: a `metadata_resolve`-path write and a `remediate`-path write both
  leave table = jsonb; a `kind='draft'` `update_paper_fields(authors=…)`
  creates no `paper_authors` rows.
* **[readiness-gate 2026-09-18] advisories — taken.** Affiliation lives on
  the orcid node's `meta.employments` and on the *paper's*
  `meta.openalex.authorships` (Motivation corrected). This item supersedes
  the `refs.authors` line of `docs/backlog/jsonb-column-review.md`; that
  item's other candidates are untouched — drop its `refs.authors` bullet
  on ship.

# Financial-filings kind — proposal

Status: `proposed` — awaiting design calls before spec is written.
Owner: bots.
Sibling: modeled on `kind='patent'` (see `patent-kind-spec.md`).

A read-only precis kind for issuer disclosures (10-K, 10-Q, 8-K,
20-F, annual accounts, yuho, …). Fetch-as-ingest, OPEN lowercase
tag prefixes, raw XBRL/HTML on disk, parsed sections embedded in
Postgres. Same shape as `patent`.

---

## 1. Sources and what they actually give you

- **SEC EDGAR (US)** — free, no key, ~10 req/s with a
  `User-Agent: name email` header. Endpoints:
  - `data.sec.gov/submissions/CIK##########.json` — filing history
  - `data.sec.gov/api/xbrl/companyfacts/CIK##########.json` — every
    tagged XBRL fact across every filing. **The gold.**
  - `www.sec.gov/Archives/edgar/data/<cik>/<accession>/` — raw
    10-K/10-Q/8-K/S-1/20-F/13F/Form 4 bundles (HTML + XBRL +
    exhibits)
  - Full-text search: `efts.sec.gov/LATEST/search-index?q=...`
- **SEDAR+ (Canada)** — API behind login; free path is scraping
  `www.sedarplus.ca`. Painful.
- **ESMA / EU** — no single EDGAR. Per-country OAMs (UK FCA NSM,
  AMF France, BaFin, Consob, CNMV). ESEF iXBRL since 2020 lives on
  the OAMs. ESMA register has JSON for prospectuses/short-selling
  but not annual reports.
- **UK Companies House** — clean free API (key required).
  `api.company-information.service.gov.uk`. Great for private-co
  accounts, officers, filing history.
- **HKEX** — `www1.hkexnews.hk` HTML; no official API. Scraping
  is stable.
- **SSE / SZSE / CSRC (CN)** — Chinese PDF/HTML, no English API.
  `CNINFO` (`www.cninfo.com.cn`) has a semi-documented JSON
  endpoint and the most complete index.
- **JPX / EDINET (Japan)** — clean JSON+ZIP API at
  `disclosure.edinet-fsa.go.jp/api/v2/...`, XBRL, free, key
  required since v2. Often forgotten, worth including in MVP.

**Phase 1 recommendation**: EDGAR + Companies House + EDINET. All
three have actual APIs, cover US/UK/JP. Defer HKEX/CN (scraping)
and EU OAMs (fragmentation) to Phase 2.

---

## 2. Kind surface — mirror of `patent`

```
kind='filing'   # (or 'sec' / 'disclosure' — see §6)

search(q=, tags=, scope=, top_k=, source='local'|'remote'|'both')
get(id='<issuer-slug>~<accession>')
get(id='/recent')   # by ingest time
get(id='/filed')    # by filing date
get(id=..., view='facts')        # XBRL facts as structured JSON
get(id=..., view='facts-table')  # markdown pivot
```

**Slug shape**: `<issuer-slug>~<form>-<date>-<accession-hash>`
e.g. `apple-inc~10k-20241101-0000320193-24-000123`. The `~` matches
patent's slug-with-selector convention.

**Why one kind, not one per jurisdiction**: search/tag surface is
identical (issuer, form, date, jurisdiction). Jurisdiction is a
tag prefix. Same reasoning as patent merging EPO/USPTO-via-OPS
into one kind.

---

## 3. Tag vocabulary (OPEN prefixes)

- `jurisdiction:us|uk|jp|eu-de|eu-fr|hk|cn`
- `form:10-k | 10-q | 8-k | 20-f | s-1 | 13f | 4 | 6-k | ar | accounts | yuho`
- `issuer:apple-inc`
- `cik:0000320193` / `lei:HWUPKR0MPOU8FGXBT394` / `ticker:aapl` /
  `crn:00445790`
- `sic:3571` / `naics:334111` / `sector:technology`
- `fy:2024` / `fp:q3`
- `xbrl:us-gaap` / `xbrl:ifrs`
- `topic:going-concern | topic:material-weakness | …`

Closed axes: `SRC` and `CACHE` — same as patent.

Code-side: add `"filing": frozenset({"SRC", "CACHE"})` to
`store/types.py::_KIND_ALLOWED_AXES`.

---

## 4. Raw storage layout

`$PRECIS_FILING_RAW_ROOT/<jurisdiction>/<issuer-slug>/<accession>/`
containing `primary.htm`, `instance.xml` (XBRL), `index.json`, and
exhibits. Forensic re-parse without re-fetching. Exhibits (large
PDFs, images) stay on disk, never in Postgres.

NFS path on cluster: `/opt/nfs/shared/filings/`. Local dev:
`~/.acatome/filings/`.

---

## 5. Blocks — what gets embedded

A 10-K is 300 pages. Dumping all of it is wasteful. Split:

1. **Metadata block** — issuer, form, dates, auditor, signer.
2. **Item sections** — one block per 10-K Item (1 Business,
   1A Risk Factors, 7 MD&A, 8 Financial Statements, …). Same
   for 20-F items, EDINET yuho sections.
3. **XBRL facts** — NOT embedded as text. Stored in `meta.facts`
   as structured JSON keyed by taxonomy concept
   (`us-gaap:Revenues` → list of `{value, unit, period, context}`).
   Queryable via `view='facts'`, not searched semantically.
4. **Exhibits** — listed in meta, not ingested unless explicitly
   fetched.

Keeps embedding cost sane and lets `search(q="going concern")`
hit the right Item instead of 30 boilerplate hits.

---

## 6. Open design calls

1. **Kind name**: `filing` (generic), `sec` (US-only feel but
   short), or `disclosure` (matches ESMA/EDINET terminology)?
2. **Phase 1 scope**: EDGAR-only first release, then add Companies
   House + EDINET in Phase 1.5? Or build the three-source fetcher
   abstraction from day one (like patent's `epo_ops` +
   `epo_ops_search` provider pair)?
3. **XBRL facts surface**: `view='facts'` (structured JSON) and/or
   `view='facts-table'` (markdown pivot)? Lean toward both.
4. **Watches** (Phase 2, deferred like patent): saved queries
   that notify when a named issuer files new 10-K/8-K? Useful for
   investing workflows; adds a runner + launchd plist mirroring
   quest/patent.

---

## 7. Env / config

- `EDGAR_USER_AGENT="Name email@example.com"` (SEC requires it;
  requests without are 403'd)
- `COMPANIES_HOUSE_API_KEY` (vault: `vault_companies_house_key`)
- `EDINET_API_KEY` (vault: `vault_edinet_key`)
- `PRECIS_FILING_RAW_ROOT` — default
  `/opt/nfs/shared/filings/` on cluster, `~/.acatome/filings/`
  locally

---

## 8. Phase plan (sketch)

- **Phase 1**: kind registration, EDGAR fetcher, slug, blocks,
  tags, `view='facts'`, `/recent`, `/filed`. Migration
  `0007_filing_kind.sql`.
- **Phase 1.5**: Companies House + EDINET fetchers behind the
  same provider abstraction.
- **Phase 2**: saved watches (`filing_watches` table + runner +
  CLI + ansible launchd plist + `28-filing.yml` playbook).
- **Phase 3**: HKEX / CNINFO scrapers, EU OAM coverage where
  endpoints exist.

Skills (drafted alongside Phase 1):
`src/precis/data/skills/precis-filing-help.md` (entry-level) +
`precis-filing-power.md` (raw XBRL queries, fact pivoting).

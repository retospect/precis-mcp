# `edgar` — read-only SEC EDGAR filings kind

> Modelled on the `patent` kind (EPO OPS); read
> `docs/user-facing/patent-kind-spec.md` first — this file keeps only
> the open phase-2 scope and the decided divergences that bound it.

Shipped portion (phase 1): see the `src/precis/handlers/edgar.py`
module docstring; full plan in git history. Live: ingest + search +
get (`_edgar_{accession,query,client,sections,parse,ingest}.py`),
migration `0053_edgar_kind.sql`, the quarter-to-quarter
`view='diff'` + `changed:<canonical_id>` tags (`_edgar_diff.py`),
`precis-edgar-help`. The env gates became defaults during capability
universalization (see the handler docstring) — SEC needs no
credentials, only a descriptive `PRECIS_EDGAR_USER_AGENT`.

## Open scope — Phase 2: saved watches + morning-report minting

Mirror the patent watch machinery 1:1:

- Migration `0054_edgar_watches.sql` — saved full-text watches storing
  a JSONB param dict (EDGAR FTS is keyword params, not CQL — no
  auto-promote heuristic, no strict-CQL guard).
- `handlers/_edgar_watch_db.py` (DAO) + `handlers/_edgar_notable.py`
  (notability predicate: form/item allow-list).
- `cli/edgar.py` — `watch-edgar / list / run` (mirror `cli/patent.py`)
  with `--help`, an integration test, and a README line.
- **Cross-kind spillover into `news` (decided, called out for
  review):** the watch runner mints a `news` ref (`category:news`,
  `source:edgar`, `form:<x>`, `published:<filed>`) for notable
  filings so the 06:00 briefing picks them up — deliberate reuse of
  the news→briefing pipeline, the one place `edgar` reaches outside
  its own tables.
- **Findings from material diffs:** each material `diff_sections`
  change mints a `finding` linked to both filings (rides the watch
  runner).
- Ansible follow-up for the watch runner (mirror the patent launchd
  job); verify a notable filing surfaces in the next `run_briefing`.

## Decided constraints (keep)

- Read-only public record: `put` unsupported; axes = `SRC` + `CACHE`
  only, no STATUS lifecycle.
- Filings are stored whole — no cap, no truncation; structure comes
  from the section classifier, not splitting.
- Client-side token bucket (10 req/s) + rolling fair-use byte
  accounting (`PRECIS_EDGAR_FAIR_USE_LIMIT_GB`, 7-day
  warn-and-pause) — SEC publishes no throttle headers.
- Embeddings lazy from day one (ADR 0007) — no synchronous embed in
  the verb.
- Exhibits (EX-*) stay **link-only** in v1 — ingest as blocks only if
  search demand shows they matter.
- No new top-level dependency without an ADR — `lxml` suffices for
  filing HTML.

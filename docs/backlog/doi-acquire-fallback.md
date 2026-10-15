---
status: in-progress
title: Acquire falls back to DOI registries on an S2 miss
pillar: platform
---

# Acquire falls back to DOI registries on an S2 miss

## Motivation / why

`put(kind='paper', args={'doi': ...})` (gr486090) rejects real DOIs as
"likely a hallucinated or mistyped ID" whenever Semantic Scholar lacks
them — S2's index lags new records and skips whole registries (DataCite
proceedings, e.g. `10.4230/LIPIcs.*`). An S2 miss is not evidence an
identifier is wrong; only "no DOI registry knows it" is.

## In scope

- New `src/precis/ingest/doi_registry.py`: `resolve_registry_doi(doi)`
  queries Crossref (`api.crossref.org/works/<doi>`) then DataCite
  (`api.datacite.org/dois/<doi>`) through `safe_get` on the pinning
  `http_client` — the DOI is agent-supplied. Returns the normalized
  metadata dict (`title`/`authors`/`year`/`journal`/`abstract`,
  `source` = `crossref`/`datacite`), `None` when every registry answered
  not-found, and raises `RegistryLookupError` when a leg faulted and no
  leg hit (the caller mints unverified, never rejects on an outage —
  same miss-vs-fault split as the S2 leg, gr483930).
- `PaperHandler.acquire`: on a genuine S2 miss for a DOI, consult the
  registries; a hit mints the stub with registry metadata (title/year/
  authors columns, journal/abstract + `meta.acquire_registry` patch) and
  the response names the resolving registry. Rejection keeps the
  "hallucinated or mistyped" wording only when no registry knows the
  DOI. `verify=False` stays the explicit escape hatch.
- Skills: `precis-paper-help` / `precis-stubs-help` one-line wording.

## Explicitly NOT in scope

- No changes to `safe_fetch.py` (full-gate file).
- No new identifier kinds (arXiv/S2/pubmed keep today's semantics).
- No migration; `paper_authors.source` keeps the existing tier set
  (DataCite authors write at the generic `pdf` tier — no `datacite`
  tier exists).

## Acceptance criteria

- S2 hit path unchanged.
- S2 miss + Crossref hit → stub minted with Crossref title/year/authors;
  response names Crossref; no `acquire:unverified`.
- S2 miss + Crossref 404 + DataCite hit → same, named DataCite.
- S2 miss + all registries miss → existing BadInput wording.
- Registry fault (non-404/transport) → mint unverified, not reject.
- Fetches routed via `safe_get`; tests carry no live network.

## Target + blast radius

`src/precis/handlers/paper.py` (`acquire`), new
`src/precis/ingest/doi_registry.py`, `tests/test_acquire.py`,
`tests/test_doi_registry.py`, two skill files.

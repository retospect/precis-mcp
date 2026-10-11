---
status: draft
pillar: memory-graph
prio: normal
---

# Crossref enrichment

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Abstract pass — Crossref / Semantic Scholar fallback

_Grouped 2026-09-26; was `abstract-fallback-crossref-s2`._

A DOI stamped `meta.openalex.miss` never gets an abstract from anywhere. Add
a Crossref/S2 abstract fallback lane through safe_fetch; stamp its own miss
the same way so it isn't re-tried. Owner
`src/precis/workers/openalex_enrich.py` +
`src/precis/ingest/openalex_meta.py`. Polish; mechanical.

## paper_meta_enrich hits Crossref's throttled anonymous pool (same unset-mailto bug as the retraction button)

_Grouped 2026-09-26; was `paper-meta-enrich-crossref-mailto`, status idea, prio normal._

### Motivation / why
Found while root-causing the retraction-watch button loop (fixed in
`_crossref_mailto`, da7a667e, 2026-08-12): `PRECIS_CROSSREF_MAILTO` is **unset**
in prod, so any Crossref caller reading it runs against the throttled anonymous
pool, where a single call can stall 60s+ and drop the connection (measured: a
40-call walk took 94s / 1 hard failure without a mailto, 13s / 0 failures with
one).

`workers/paper_meta_enrich.py:119` has the same bug:
```python
mailto = os.environ.get("PRECIS_CROSSREF_MAILTO", "").strip()  # unset in prod
email = os.environ.get("PRECIS_UNPAYWALL_EMAIL", "").strip()  # IS set
```
It passes the (empty) `mailto` to its Crossref enrichment and the (set) `email`
only to Unpaywall — so its Crossref lookups are un-polite and getting throttled,
silently degrading metadata enrichment throughput/reliability.

### In scope
- Give `paper_meta_enrich`'s Crossref calls a real mailto — fall back to
  `PRECIS_UNPAYWALL_EMAIL` when `PRECIS_CROSSREF_MAILTO` is unset, mirroring the
  fix already shipped in `precis_web/routes/drafts.py::_crossref_mailto`.
- Consider centralizing the mailto resolution (one helper) so Crossref callers
  can't drift again — cf. `docs/backlog/env-config-unification.md`.

### Explicitly NOT in scope
- Setting `PRECIS_CROSSREF_MAILTO` in the deploy templates — the env-fallback
  makes it unnecessary, and the code fallback is more robust than a new ops
  knob (though setting it is a fine belt-and-suspenders follow-up).

### Acceptance criteria
- `paper_meta_enrich`'s Crossref lookups run against the polite pool in prod
  (a mailto is passed whenever `PRECIS_UNPAYWALL_EMAIL` is set), verifiable by
  the same before/after timing probe used on the retraction walk.

### Target + blast radius
- `src/precis/workers/paper_meta_enrich.py:119` (mailto sourcing).
- Any other reader of `PRECIS_CROSSREF_MAILTO` (grep — the retraction route is
  already fixed).

## Venue still missing on ~2,700 papers (gr456181)

Bundled in the 2026-10-11 gripe triage. The `s2_stub_meta` venue fix
(a1985d96) is deployed and re-armed, but ~1,470 S2-venueless papers and
~1,200 held papers still carry no venue.

- Fix direction: add a Crossref venue fallback (`container-title`) for DOI
  papers after the S2 pass misses, stamped like the abstract fallback above
  so a miss is not retried. Owner `src/precis/ingest/semantic_scholar.py`.
- Ops: after deploy, re-arm the venue pass over the held cohort and re-count.
  No further S2 re-arm: S2 holds no venue for the 1,470 (comment 6).

Exporters are done (venue-less papers export as `@misc`, not an empty
`In: (YYYY)` `@article`; endnote reads the 3-key precedence). Backfill of
existing rows is only the Crossref pass above.

Who: agent-code (fallback); ops re-arm.

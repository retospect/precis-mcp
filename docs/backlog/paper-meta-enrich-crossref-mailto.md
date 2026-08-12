---
status: idea
title: paper_meta_enrich hits Crossref's throttled anonymous pool (same unset-mailto bug as the retraction button)
prio: normal
---

# paper_meta_enrich hits Crossref's throttled anonymous pool (same unset-mailto bug as the retraction button)

## Motivation / why
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

## In scope
- Give `paper_meta_enrich`'s Crossref calls a real mailto — fall back to
  `PRECIS_UNPAYWALL_EMAIL` when `PRECIS_CROSSREF_MAILTO` is unset, mirroring the
  fix already shipped in `precis_web/routes/drafts.py::_crossref_mailto`.
- Consider centralizing the mailto resolution (one helper) so Crossref callers
  can't drift again — cf. `docs/backlog/env-resolution-fragmentation.md`.

## Explicitly NOT in scope
- Setting `PRECIS_CROSSREF_MAILTO` in the deploy templates — the env-fallback
  makes it unnecessary, and the code fallback is more robust than a new ops
  knob (though setting it is a fine belt-and-suspenders follow-up).

## Acceptance criteria
- `paper_meta_enrich`'s Crossref lookups run against the polite pool in prod
  (a mailto is passed whenever `PRECIS_UNPAYWALL_EMAIL` is set), verifiable by
  the same before/after timing probe used on the retraction walk.

## Target + blast radius
- `src/precis/workers/paper_meta_enrich.py:119` (mailto sourcing).
- Any other reader of `PRECIS_CROSSREF_MAILTO` (grep — the retraction route is
  already fixed).

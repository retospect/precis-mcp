---
status: draft
title: uncited= skips patent/edgar — the sweep is not corpus-wide
prio: medium
---

# `uncited=` skips patent and edgar

## Motivation / why

`search(q=…, uncited=<draft>)` (shipped `d719151a`) subtracts what a
draft already cites by feeding `backfill/candidates.py::draft_cited_ref_ids`
into the existing `exclude_ref_ids` channel. That channel reaches
`store/_blocks_ops.py::search_blocks` — but `patent` and `edgar` are
citeable kinds whose `search`/`search_hits` have **no** `exclude_ref_ids`
wiring (patent runs a local + OPS-remote search, edgar a filing search).

Because every handler's `search_hits` has a `**_kw` catch-all, an
unhonoured kwarg is *swallowed*, not a `TypeError` — so the honest
options were "drop the kind" or "refuse". Today
(`runtime/_shared.py::UNCITED_UNSUPPORTED_KINDS`):

- an **explicit** `kind='patent'` + `uncited=` raises `Unsupported`;
- the **wildcard** fan-out drops both kinds and says so in a note.

Both are loud, so nothing is silently wrong. But the wildcard case means
a discovery sweep — the entire point of the facet — is not corpus-wide:
an uncited patent that bears on your draft will not surface.

## In scope

- Give `PatentHandler` and the edgar handler the same explicit
  `exclude_ref_ids` kwarg `handlers/paper.py` now takes (an explicit
  parameter, NOT `**_kw` passthrough — the whole failure mode here is a
  silently swallowed filter), merged with any existing `exclude=`
  resolution as a union.
- Then delete `UNCITED_UNSUPPORTED_KINDS` and both its call sites, and
  drop the note from the wildcard fan-out.
- Patent's remote OPS leg needs thought: exclusion by local ref id is
  meaningless for a result not yet in the corpus. Filtering the merged
  set after the remote leg returns is probably right, but that is a
  post-hoc cull — check it cannot silently shrink the page.

## Out of scope

- The source-search path (`sort=`/`since=`/`until=`) already excludes
  uniformly across every kind including patent/edgar, via
  `search_chunks_across_kinds` → `search_blocks_multi`. It is the
  documented workaround and needs no change.

## Notes

The general hazard is worth stating once: because `**_kw` swallows
unknown kwargs, adding a filter parameter to the search path is
silently partial by default. Any future filter of this shape needs a
per-kind audit, not a single wiring change — and should fail loudly for
kinds it cannot honour, as this one does.

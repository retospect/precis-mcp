---
status: ready
title: Supplementary information is found where attention is, fetched, and ingested as a linked ref
pillar: memory-graph
prio: low
---

# Supplementary information is found where attention is, fetched, and ingested as a linked ref

Builds 1 and 2 are on `main` (`06066a8e2`, `653a10d72`, re-arm fixes
`a642abdfb`, `4ed4c40b0`, `6c67c065a`) and deployed; both quest papers
(pa5303 → pa465134, pa166889 → pa465698) have their SI ingested and
catalysis-selectivity-17 is resolved. Rationale and the two spec
deviations (`part-of` + `meta.role='supplement'` instead of a new
`supplements` pair; event source `si_fetch`, not `fetcher:*`) live in the
`precis.ingest` package docstring and `store/si_links.py`.

## Remaining

- Observe the web paper-open and fisheye-walk triggers on prod (the MCP
  `get` trigger was seen in the round-4 dogfood): one `si_fetch` request
  with `trigger='attention'`, `by='web'` and one with `by='walker'`,
  each followed by a single pass claim. Then delete this file and item 1
  of `docs/backlog/threads/ingest-and-fetch.md`.
- Not built, optional in the spec: a CLI equivalent of
  `put(kind='paper', id=…, mode='fetch-si')`.

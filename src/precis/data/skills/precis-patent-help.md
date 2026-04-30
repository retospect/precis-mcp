---
id: precis-patent-help
title: precis — search and read patents (EPO OPS)
status: draft
tier: 1
floor: any
applies-to: get/search (kind='patent')
last-updated: 2026-04-29
---

# precis-patent-help — patents via EPO OPS

`patent` is a **read-only** kind backed by the EPO Open Patent
Services (OPS) API. Free under fair-use, covers >100 authorities
(EP, GB, US, WO/PCT, CN, JP, KR, IN, …) — one English-language
backend for "search world patents".

> **Availability**: this skill and the `kind='patent'` registration
> only appear when **`EPO_OPS_CLIENT_KEY`**,
> **`EPO_OPS_CLIENT_SECRET`**, and **`PRECIS_PATENT_RAW_ROOT`**
> are all set in the server's environment. If any is missing you
> won't see this skill in the index and `kind='patent'` raises
> `NotFound`. Free credentials at
> [developers.epo.org](https://developers.epo.org);
> `PRECIS_PATENT_RAW_ROOT` is a directory on disk (NFS on the
> cluster) where raw OPS XML responses are cached for re-parse.

The mental model is small: **`search` finds, `get` fetches**. There
is no separate "ingest" step — calling `get(id=...)` for a patent
pulls it from OPS, stores it durably, embeds it, and from then on
`search` returns it as a local hit. Background watch jobs do the
same `get` on a schedule.

```
search(kind='patent', q='...', tags=[...])  → local + remote hits, merged
get(kind='patent', id='...')                → fetch + persist + embed; returns overview
get(kind='patent', id='/recent')            → list locally-stored patents
```

## Slugs

Patents are addressed by their **EPO DOCDB id**, lowercased:

```
EP1234567B1     →   ep1234567b1
US20240012345A1 →   us20240012345a1
WO2023123456A1  →   wo2023123456a1
```

Inputs are case-insensitive and tolerate spaces (`EP 1234567 B1`);
they're lowercased and whitespace-stripped before storage. **Dots
are not stripped** — the dotted form `EP.1234567.B1` raises
`BadInput` with a recovery hint, because DOIs and arXiv ids
carry semantic dots and we don't want a normaliser that silently
reshapes them.

## Search — find patents

The `search` surface is **uniform with every other precis kind** —
just `q=`, `tags=`, `scope=`, `top_k=`. No patent-specific kwargs.

```python
search(kind='patent', q='photocatalytic NOx reduction')
search(kind='patent', q='Z-scheme photocatalysis', top_k=20)

# Filter by closed-prefix tags (auto-applied at ingest from biblio)
search(kind='patent', q='photocatalysis',
       tags=['cpc:B01J27/24', 'country:ep'])
search(kind='patent', q='MOF synthesis',
       tags=['applicant:siemens-ag', 'kind:b1'])

# Scope to one already-ingested patent (paper-style)
search(kind='patent', q='Z-scheme', scope='ep4123456a1')
```

The handler does two passes and merges them by DOCDB id:

1. **Local** — block-level hybrid (lexical tsvector + semantic
   pgvector, RRF fused) over patents already in the store.
2. **Remote** — OPS keyword search; results cached for 7 days so
   re-issuing the same query is free. Tag filters lift to OPS CQL
   automatically (`cpc:B01J27/24` → `cpc=B01J27/24`,
   `applicant:siemens-ag` → `pa="siemens ag"`, etc.) so they work
   for both legs.

Hits are interleaved by relevance; locally-stored patents are
tagged `[local]`:

```
1. ep4123456a1  [local]  Z-scheme photocatalyst for NOx abatement
   Siemens AG · 2024 · cpc=B01J27/24
   > "…visible-light driven Z-scheme heterojunction…"     ←matched block

2. wo2023123456a1         Method for selective NOxRR over MOF surfaces
   Univ. Limerick · 2023 · cpc=B01J27/24
   Abstract preview: …
```

`[local]` rows return instantly on `get(id=...)`. Untagged rows
are remote-only — calling `get(id=...)` will fetch and persist
them.

> **Need Boolean operators, date windows, citation-graph filters,
> applicant exclusions, or wildcard publication numbers?** The
> handler accepts raw OPS CQL in `q=`. See the **`precis-patent-power`**
> skill for the full grammar — it unlocks queries like
> `q='cpc=B01J27/24 and pd within "2020 2025" and not pa=basf'`.

## Get — read (and persist) one patent

```python
get(kind='patent', id='ep1234567b1')                  # overview
get(kind='patent', id='ep1234567b1', view='abstract')
get(kind='patent', id='ep1234567b1', view='claims')
get(kind='patent', id='ep1234567b1', view='description')
get(kind='patent', id='ep1234567b1', view='family')   # INPADOC family
get(kind='patent', id='ep1234567b1', view='legal')    # legal-status events
get(kind='patent', id='ep1234567b1', view='cite/bib') # BibTeX
```

The first time you `get(id=...)` for an unknown DOCDB id, the
handler fetches biblio + abstract + claims + description from OPS,
parses the WIPO ST.36 XML, embeds the blocks, persists a `refs`
row, and renders the requested view. From then on the patent is
local — `search` lists it as `[local]`, and chunk navigation works
exactly like papers:

```python
get(kind='patent', id='ep1234567b1~5')           # block 5
get(kind='patent', id='ep1234567b1~5..12')       # range
get(kind='patent', id='ep1234567b1~5..12/toc')   # range-scoped TOC
get(kind='patent', id='ep1234567b1', view='toc') # full hierarchical TOC
```

Same path-form aliases as `paper` (`id='ep1234567b1/abstract'`).
Re-`get`-ing within 30 days is a no-op refresh; after that the
etag is rechecked and blocks are replaced if the publication
changed.

To list locally-stored patents:

```python
get(kind='patent')                  # default page (by ingest time)
get(kind='patent', id='/recent')    # newest by **ingest time** (when YOU added it)
get(kind='patent', id='/published') # newest by **publication date** (when EPO published it)
```

Note the difference: `/recent` is the cross-kind precis
convention — "what did I add last?" — while `/published` is the
patent-specific axis for "what is the newest patent in my
store?". Locally-stored patents can be old (filed in 2002 but
ingested last week) so the two views can give very different
lists.

## Tags on local patents

Same tag system as `paper`. Closed-prefix axes auto-applied at
ingest:

| Prefix       | Multi? | Example                |
|--------------|--------|------------------------|
| `cpc:`       | yes    | `cpc:B01J27/24`        |
| `ipc:`       | yes    | `ipc:H01M`             |
| `applicant:` | yes    | `applicant:siemens-ag` |
| `country:`   | no     | `country:ep`           |
| `kind:`      | no     | `kind:b1`              |
| `family:`    | no     | `family:12345678`      |

Plus the open `topic:` prefix (agent-applied) and any other open
lowercase tag the agent wants to coin. See `precis-tags` for the
broader convention.

`tags=` filters work on both legs of search:
- the **local leg** uses precis's regular SQL tag filter;
- the **remote leg** translates supported tags back to OPS CQL.

Tags that don't have a CQL equivalent (e.g. `topic:my-project`)
are ignored on the remote leg — they only narrow the local hits.

## Background watches

Saved CQL watches let you set up "tell me when something new
matches this query" without polling by hand. Each watch carries a
CQL string, an interval (default 7 days), and a mode:

```sh
# Default mode: open a quest for new hits, you triage manually.
precis jobs watch-patents 'cpc=B01J27/24' --name catalysts
precis jobs watch-patents 'ti=nanobud or ab=nanobud' --name nanobud --every 1d

# Auto-get: ingest new hits directly. Use --max-per-pass to cap.
precis jobs watch-patents 'pa=basf and cpc=B01J' --name basf-b01j --auto-get --max-per-pass 5

# Manage them.
precis jobs list-patent-watches
precis jobs list-patent-watches --show-cql
precis jobs run-patent-watches                       # one-shot pass over due watches
precis jobs run-patent-watches --name catalysts --dry-run
precis jobs watch-patents --name catalysts --delete
```

**Watches require strict CQL.** Bare keywords like `'photocatalysis'`
are rejected at create time so meaning doesn't drift if the ad-hoc
auto-promote heuristic ever changes. Always use explicit fields:
`ti="..."`, `ab="..."`, `cpc=`, `pa=`, etc.

Each pass diffs the OPS hit list against `last_seen_pn`. New hits
either become a quest summary (default) or get ingested directly
(`--auto-get`). In auto-get mode, hits past `--max-per-pass` are
**dropped** — they resurface on the next pass, oldest publication
date first.

The runner is fair-use aware: when the rolling 7-day OPS bytes
total exceeds `PRECIS_PATENT_FAIR_USE_LIMIT_GB` (default 3 GiB), it
pauses without mutating any watch row. The next hourly tick retries.

See `precis-patent-power` for the full CQL grammar and usage notes.

## Failure modes

- `BadInput: 'xyz' is not a DOCDB id` — bad slug shape (must
  match `^[a-z]{2}\d+[a-z]\d?$` after lowercasing).
- `BadInput: invalid CQL query` — OPS rejected the search; raw
  `q=` was malformed. (Bare-keyword `q=` is auto-promoted to a
  safe form, so this only fires for explicit CQL.)
- `NotFound: patent 'ep…' not found at OPS` — `get(id=...)` for
  an id that doesn't exist; nothing was stored.
- `Upstream: EPO OPS HTTP 403` — quota exceeded or bad creds.
  Weekly cap is 4 GB rolling.
- `NotFound: unknown kind: patent` — the env vars listed above
  weren't all set when the server started. The kind is hidden at
  registration time, not at fetch time, so this is the only
  signal you'll see when creds are missing.

## Required env

- **`EPO_OPS_CLIENT_KEY`** and **`EPO_OPS_CLIENT_SECRET`** —
  register a free app on the EPO Developer Portal
  (`developers.epo.org`).
- **`PRECIS_PATENT_RAW_ROOT`** — directory where raw OPS XML
  responses are cached on disk (one subtree per patent).
  - Local dev: `~/.acatome/patents/` is the recommended default.
  - Cluster: shared NFS at `/opt/nfs/shared/patents/` so any node
    can re-parse from disk.
  Postgres holds the parsed/embedded state; this directory holds
  the upstream artefacts so the parser can be re-run without
  re-fetching.
- Optional: **`EPO_OPS_USER_AGENT`**.

If any required var is missing the kind is **hidden** at the
agent boundary — this skill won't appear in the index and
`kind='patent'` raises `NotFound: unknown kind`.

## Cost & freshness

- All OPS calls are **free**; bandwidth counts toward 4 GB / week
  fair-use. The runner pauses ingest at 3 GB rolling.
- Search hit-list cache TTL: 7 days.
- Stored patents are **pinned** — re-ingest manually (or via
  `precis jobs sweep-patent-stale`) to refresh.
- Cost trailer: `[cost: free — EPO OPS fair-use]`. Footer carries
  the Espacenet deep-link for attribution.

## What's *not* in scope

- **`put`** — patents are read-only. Notes / annotations belong
  on a `memory` linked to the patent ref via
  `link='ep1234567b1:notes'`.
- **Date / year range filters as kwargs** — currently expressed
  via raw CQL in `q=` (`pd within "2020 2025"`). A first-class
  cross-kind affordance is queued; see
  `docs/search-future-filters.md`.
- **State markers beyond `[local]`** (e.g. `[queued]`, `[stale]`)
  — also queued; see `docs/search-future-filters.md`.
- **`source='local'` / `source='remote'` knob** — today `search`
  always merges (with the remote leg auto-skipped if creds are
  unset). Forcing one side is queued in
  `docs/search-future-filters.md`.
- **USPTO PAIR / bulk dumps** — OPS already covers US/WO/CN/JP
  via DOCDB worldwide.
- **Image / figure retrieval** — deferred. Future `view='images'`
  will fetch TIFFs on-demand to disk under
  `$PRECIS_PATENT_RAW_ROOT/<cc>/<num>/<kc>/images/`; image bytes
  will never enter Postgres. For inline rendering today, follow
  the Espacenet deep-link in the response footer.

## See also

- `precis-overview` — verbs and kinds
- `precis-patent-power` — raw CQL grammar for advanced searches
- `precis-paper-help` — comparison: same `~N..M` chunk syntax
- `precis-tags` — tag conventions used here
- `precis-cache` — TTL, attribution, cost trailers
- `docs/search-future-filters.md` — deferred filter affordances
- `docs/patent-kind-spec.md` — implementation spec

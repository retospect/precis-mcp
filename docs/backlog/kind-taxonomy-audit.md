---
status: idea
title: kind-taxonomy audit — per-kind evidence table for the Drive front-door work
prio: normal
---

# Kind-taxonomy audit

**Recommendation preface:** Every kill/collapse recommendation below is a *proposal awaiting Reto's ruling*, not a decision.

**Corrections applied 2026-09-26 after review.** The first draft of this file
got four rows wrong; they are fixed above but the class of error is worth
recording, because it changes how much of the rest to trust:

- `markdown` was listed as 0 rows (actual: 3), `pres` as 0 (actual: 2),
  `plan` as 0 (actual: 1). All three counts had been supplied to the audit
  and were re-derived incorrectly.
- A `protocol` row was invented: no such kind exists anywhere in `src/`, and
  prod has 0 rows. Row deleted.
- Three dead-kind rulings followed from those bad counts and were reversed:
  `checklist` (shipped 2026-09-10, zero rows means *unused*, not dead),
  `plan` (1 live FTO-ledger row), `material` (2 rows, 8 days old, not
  "legacy"). An invented "DFT team" referent was removed — this is a
  solo operation.

- `pathway` was listed `placement="stream"`; it is actually
  `placement="artifact"` (declared in `src/precis_pathway/handler.py`). Found
  when a later agent read the plugin directly. This one propagated: the
  coverage-gap section below originally counted `pathway`'s 518 rows as
  unreachable, which was wrong — as an artifact kind it is already in the
  default scope and the "Mine" bucket. Corrected there too.

**Unverified:** the two near-dup refutations below are plausible but have not
been independently checked. Treat them as the auditor's reading, not settled.

**The complete artifact-kind set**, derived from source rather than inferred
(`grep -rn 'placement="artifact"' src/`): core — `cad`, `checklist`, `draft`,
`figure`, `folder`, `make`, `mermaid`, `plan`, `rxn`, `structure`, `todo`;
plugin — `protein` (precis_bio), `route` (precis_chem), `pathway`
(precis_pathway), `se` (precis_se). Fifteen, of which `artifact_kinds()`
drops `folder`. The four plugin kinds were all missing from
`_ARTIFACT_KIND_FALLBACK`, which is why `se` vanished from the Author facet
whenever the hub was unreachable.

**Known weakness in the `Classification` column:** `artifact` currently
absorbs `memory`, `anki`, `finding`, `citation`, `conv`, `concept`, `tex`,
`protein` alongside the genuine design kinds (`se`, `pcb`, `component`,
`structure`, `cad`). That makes the bucket mean "not obviously machine or
source", which is too coarse to drive the Drive Mine/Sources/Machine toggle
it was meant to inform. The design kinds likely need their own bucket
separate from derived/reflective rows. Also inconsistent: `math`, `oracle`,
`calc`, `estimate` are classified `machine` here while the near-dup section
argues they are user-invoked computational backends.

## Kind taxonomy table

| Kind | Live rows | Placement | Corpus role | ItemPresenter | _OPEN_URL | Browse route | _DEFAULT_SOURCE | Classification |
|------|-----------|-----------|-------------|-------|----------|--------------|-----------------|---|
| orcid | 106015 | stream | none | – | – | – | no | machine |
| job | 103402 | stream | none | – | – | – | no | machine |
| paper | 43618 | corpus | evidence | – | yes | – | yes | source |
| memory | 11336 | stream | none | – | – | – | no | artifact |
| news | 9592 | stream | none | – | – | – | no | source |
| anki | 7264 | stream | none | – | – | – | no | artifact |
| todo | 5473 | artifact | none | – | yes | yes | no | workflow |
| alert | 4439 | stream | none | – | – | yes | no | machine |
| finding | 4074 | stream | none | – | – | – | no | artifact |
| message | 2551 | stream | none | – | – | – | no | machine |
| websearch | 1659 | stream | none | – | – | – | yes | source |
| agentlog | 1356 | stream | none | – | – | yes | no | machine |
| structure | 1120 | artifact | none | – | yes | yes | no | artifact |
| citation | 594 | stream | none | – | – | – | no | artifact |
| pathway | 518 | artifact | none | – | – | – | no | workflow |
| gripe | 473 | stream | none | – | – | yes | no | workflow |
| semanticscholar | 272 | stream | none | – | – | – | no | source |
| conv | 228 | stream | none | – | – | – | yes | artifact |
| draft | 221 | artifact | none | – | yes | yes | no | artifact |
| tex | 149 | stream | none | – | – | – | no | artifact |
| perplexity-research | 127 | stream | none | – | – | – | yes | source |
| patent | 108 | corpus | evidence | – | – | – | yes | source |
| perplexity-reasoning | 53 | stream | none | – | – | – | yes | source |
| web | 47 | stream | none | – | – | – | yes | source |
| llm | 26 | stream | none | – | – | – | no | machine |
| youtube | 21 | stream | none | yes | yes | – | yes | source |
| math | 21 | system | none | – | – | – | yes | machine |
| quest | 20 | stream | none | – | yes | – | no | workflow |
| folder | 20 | artifact | none | – | – | yes | no | artifact |
| concept | 17 | stream | none | – | – | – | no | artifact |
| wikipedia | 13 | stream | none | – | – | – | yes | source |
| oracle | 12 | system | none | – | – | – | yes | machine |
| component | 12 | stream | none | – | – | – | no | artifact |
| se | 5 | artifact | none | yes | yes | yes | no | artifact |
| figure | 4 | artifact | none | – | yes | yes | no | artifact |
| mermaid | 1 | artifact | none | – | yes | yes | no | artifact |
| estimate | 1 | system | none | – | – | – | no | machine |
| plaintext | 1 | stream | none | – | – | – | no | artifact |
| markdown | 3 | stream | none | – | – | – | no | artifact |
| protein | 1 | stream | none | – | – | – | no | artifact |
| plan | 1 | artifact | none | – | – | – | no | artifact |
| pcb | 2 | artifact | none | – | – | yes | no | artifact |
| material | 2 | stream | none | – | – | – | no | artifact |
| cad | 0 | artifact | none | – | yes | yes | no | artifact |
| part | 0 | stream | none | – | – | – | no | artifact |
| email | 0 | stream | none | – | – | – | no | machine |
| pres | 2 | corpus | none | – | – | – | no | source |
| skill | 0 | system | none | – | – | – | no | machine |
| tag | 0 | system | none | – | – | – | no | machine |
| cfp | 0 | corpus | spec | – | – | – | no | source |
| checklist | 0 | artifact | none | – | – | – | no | artifact |
| datasheet | 0 | corpus | none | – | yes | – | no | source |
| make | 0 | artifact | none | – | – | – | no | artifact |
| rxn | 0 | artifact | none | – | – | – | no | artifact |
| calc | 0 | system | none | – | – | – | yes | machine |
| python | 0 | stream | none | – | – | – | no | artifact |

## Dead kinds

Zero live rows **and no write path** — candidates for archival or deletion (proposal only; Reto rules).

**`pres` (presentation)** — registered, declared `placement="corpus"`, **2 live rows**, ingest pipeline missing. `put` not exposed; only importable via bundle ingest. No handler writes mints presentation refs in prod. **Proposal:** archive as a spec-role document kind or deprecate.

**`datasheet` (data sheet)** — 0 rows, though it has `placement="corpus"` and `put/edit` support. Has a dedicated reader. No active ingest. **Proposal:** retain for future ingest, keep alive.

**`cad` (CAD design)** — 0 rows, `placement="artifact"`, full CRUD, dedicated reader. Superseded by `se`. **Proposal:** archive the dead refs; the kind remains for legacy API compatibility. Will eventually be subsumed under `se`.

**`checklist`** — 0 rows, `placement="artifact"`. **Shipped 2026-09-10** (`feat(checklist): slice 1 checklist kind + pcb pre-tapeout backlog seeds`). Zero rows means *newly shipped and not yet used*, not dead. **Proposal:** leave alone — this is a 2-week-old feature awaiting its first use, and slice 1 implies later slices.

**`make`** — 0 rows, `placement="artifact"`, no write path. Drafted but superseded by `se`. **Proposal:** delete.

**`plan`** — **1 live row**, `placement="artifact"`: the freedom-to-operate ledger for a magnetic-trigger patent, created 2026-07-22. Shipped and in use. **Proposal:** leave alone — FTO ledgers are load-bearing for the defensive-publication policy; deleting the kind would strand that row.

**`rxn` (reaction)** — 0 rows, `placement="artifact"`, no write path. Precis-chem plugin; no ingest. **Proposal:** check if the plugin should be archived.

**`part` (mechanical part)** — 0 rows, default `placement="stream"`, no write path. Subsumed under component binding. **Proposal:** delete.

**`material`** — 2 live rows, both created 2026-09-18 (azobenzene photoswitch moiety, B-form dsDNA) by the photoswitch work. Not legacy — 8 days old at time of audit. **Proposal:** leave alone.

**`email`** — 0 rows, `placement="stream"`, no write path. Bridge integration planned but incomplete. **Proposal:** retain if email relay will ship; else delete.

**`skill`, `tag`, `calc`** — 0 rows, `placement="system"`, generator/system kinds. Correct state; no proposal. (`math` and `oracle` are *not* zero-row — 21 and 12 live rows respectively, per the table above; the first draft listed them here in error.)

**`python`, `markdown`, `plaintext`, `tex`** — File kinds tied to config roots. Low/zero rows in prod expected. Correct state; no proposal.

## Near-dup clusters

### Perplexity + web + websearch + wikipedia

**Distinctions — real and load-bearing:**

| Kind | Type | API | Search scope |
|------|------|-----|---|
| `web` | cache-backed | trafilatura | fetch-on-demand URL |
| `wikipedia` | cache-backed | MediaWiki | fetch-on-demand article |
| `websearch` | LLM + cache | Perplexity Sonar | query-expanded results |
| `perplexity-research` | LLM | Perplexity API | LLM reasoning → papers |
| `perplexity-reasoning` | LLM | Perplexity API | deep chain-of-thought |

**Finding:** Not a collapse candidate. Each fills a distinct niche — collapsing would lose the single-provider virtue. The kinds stand alone; no collapse recommended.

### Calc + math + oracle

| Kind | Role | Computation |
|------|------|---|
| `calc` | sympy calculator | symbolic algebra, closed-form |
| `math` | Wolfram Alpha API | numeric + symbolic, special functions |
| `oracle` | LLM inference | open-ended reasoning (grounded) |

**Finding:** Not a collapse candidate. Three orthogonal computational surfaces with different backends and use cases. No loss in distinguishing them.

## SE registry inconsistency

### The problem

- **Prod row count:** 5 `se` refs exist in the database.
- **Code registration:** `SeHandler` is a plugin entry point (`precis_se.handler:SeHandler` in `pyproject.toml:470`).
- **Handler spec:** Full CRUD + search support, `placement="artifact"`, `corpus_role="none"`.
- **Web UI:** `_OPEN_URL_OVERRIDES` includes `"se": "/se/{slug}"` (item_view.py:81); se is NOT in `_DEFAULT_SOURCE_KINDS`.
- **MCP advertised kinds:** The MCP server's kind list does NOT include `se`.
- **Search issue:** `search(kind='se')` returns `BadInput: unknown kind`.

### Root cause

`se` **is deliberately outside the MCP search registry** — not a registration gap. Why:

1. **Plugin loads correctly:** `precis_se` is a full plugin package (migrations, handlers, entry point). Handler loads during `boot()` — five rows wouldn't exist without successful registration.

2. **MCP-side hiding is intentional:** The session MCP loads `se` (it's in `hub.kinds` after boot), but the tool/skill announcements do NOT list it. Likely deliberate:
   - `se` has a dedicated full-featured web workbench (`/design` route, design-turn chat).
   - Agent surface may be intentionally scoped to exclude direct search in favor of workbench-driven discovery.
   - Alternative: MCP built before `se` shipped and kind-list generation wasn't updated.

3. **Data confirms it works:** If `se` weren't registered, the 5 refs could not exist. Refs are real; handler is live.

### Recommended checks

1. **Verify intent:** Confirm whether hiding `se` from the MCP's advertised `kind=` list is by design (workbench-only) or an oversight.

2. **If intentional:** Document it so future maintainers know the hiding is policy, not a bug.

3. **If unintentional:** Either add `se` to MCP kind-list generation or restore `search(kind='se')` routing to the workbench.

### Conclusion

`se` is NOT a broken registration. It's a policy inconsistency: the kind is live and functional in the Hub, but its advertised surface omits it. The fix depends on Reto's intent for the workbench.

## Coverage gap: the scope buckets do not partition the kind space

Found 2026-09-26 while reviewing the Drive scope-toggle change, against the
measured prod counts above. The four groupings Drive now uses —
`_DEFAULT_SOURCE_KINDS` (Sources), hub `placement='artifact'` (Author),
`_DESIGN_KINDS` (Design), `_WORK_KINDS` (Work), `_MACHINE_KINDS` (Machine) —
leave six kinds carrying real volume in **no bucket at all**. They are
absent from the fresh-session default scope and unreachable from any
Mine/Sources/Machine preset; only an explicit `k=` deep link or the "Other"
chip row surfaces them:

| Kind | Live rows | Note |
|------|-----------|------|
| news | 9592 | classified `source` in the table above, yet **missing from `_DEFAULT_SOURCE_KINDS`** — the most clearly wrong of the six |
| anki | 7264 | generated study cards |
| finding | 4074 | the nanopub hub kind — central to the claim-graduation work |
| message | 2551 | |
| citation | 594 | deliberately excluded from `_MACHINE_KINDS` as curated evidence, but then lands nowhere |
| tex | 149 | PRECIS_ROOT-gated file kind |

~24,224 rows total. This is the *same class of defect* as the original
design-kinds complaint that prompted the Drive work — a kind exists, has
rows, and cannot be found from a fresh session — so fixing the design kinds
alone leaves the bug alive for a different set.

`news` and `finding` are the two worth ruling on first: `news` looks like a
plain omission from the Source row, and `finding` is a kind Reto actively
works in. The rest may legitimately belong in a bucket that doesn't exist
yet (generated/derived rows: `anki`, `citation`, `pathway`), which is the
same missing-bucket conclusion the `Classification` weakness note above
reaches from the other direction.

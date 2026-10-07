---
status: idea
pillar: platform
title: kind-taxonomy audit — per-kind evidence table for the Drive front-door work
prio: normal
---

# Kind-taxonomy audit

Reto ruled 2026-10-05: no kind is removed and no near-duplicate kinds
collapse. Zero rows is not evidence that a kind is obsolete. The table below
retains the historical audit evidence; its counts, classifications and route
columns are not a live inventory. Drive's current partition is described below.

**Corrections applied 2026-09-26 after review.** The first draft of this file
got four rows wrong; they are fixed below but the class of error is worth
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

The source-derived artifact set includes the plugin kinds se/protein/route/
pathway. `item_view._ARTIFACT_KIND_FALLBACK` retains all four when hub
introspection fails. Drive gives se the curated Design category rather than
using artifact placement as author provenance.

The table's `Classification` column is the historical auditor grouping.
It does not drive Drive's categories; operational and computational kinds
retain their distinct APIs and uses.

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

## Retained kinds

`pres`, `datasheet`, `cad`, `checklist`, `make`, `plan`, `rxn`, `part`,
`material` and `email` stay. The earlier cad/part/make conclusions were false:
cad is the geometry package and kind that se builds on; part is the lazy
LCSC catalog kind; make is the make-tree kind. None is superseded by se or
component. The corrected pres/plan/markdown counts and invented protocol row
also rule out drawing removal conclusions from historical row counts.

Generator/system kinds and file kinds retain their existing surfaces.

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

## SE and current Drive coverage

SE is a first-class advertised kind. Its plugin entry point, server's
`hub.kinds` advertisement, overview row and skill toc are present. Direct
and cross-kind searches support se; a missing query raises "requires q",
not "unknown kind". Earlier claims of intentional MCP hiding were false.
Regressions in `tests/test_se_advertised.py` pin this existing contract.

Drive's route-owned `_kind_buckets` partitions the live and compatibility
roster once. Curated categories take precedence over artifact placement:
se is Design; finding/tex are Author; anki/citation are Derived; news is
Sources; message is Machine. New artifact kinds join Author and remaining
registered, saved or URL-selected kinds have visible Other. Every kind
remains reachable without changing its kind API or placement declaration.
Sources, Machine and Derived presets select their buckets; Mine includes
Author, Design and Work. Existing selection precedence and saved cookies
remain. Discover retains findings and derived content with its current
operational exclusions.

The no-removal ruling is settled. Future taxonomy work may refine browsing
categories and the historical evidence table without collapsing kinds.

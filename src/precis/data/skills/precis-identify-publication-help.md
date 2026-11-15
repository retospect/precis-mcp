---
id: precis-identify-publication-help
family: paper
title: precis — identify which publication a citation or abstract is
summary: turn a conference title, byline, abstract or partial citation into a verified real publication, then queue that manuscript
answers:
  - I have a conference title or abstract — which published paper is it?
  - how do I tell a hit is the same work and not a same-author near-miss?
  - how do I identify publications in bulk and resume an interrupted run?
  - a provider rate-limited me mid-identification — what do I record?
applies-to: get/search/put (kind='paper'|'semanticscholar'|'orcid'|'web'|'websearch')
status: active
tags: [external-sources, workflow]
kinds: [paper, semanticscholar, orcid, web, websearch]
---

# precis-identify-publication-help — citation/abstract → verified publication

The job: turn a conference title, byline, abstract or incomplete
citation into the **real published manuscript it corresponds to**, then
queue that manuscript. One source item maps to at most one publication;
"no verified publication" is a legitimate answer. This is a goal
procedure, not a kind — the paper-family index lists the family's
skills, and `kinds='paper'` the wider paper-applicable set:

```python
get(kind="skill", id="toc", args={"family": "paper"})  # the paper family
get(kind="skill", id="toc", kinds="paper")             # every paper-applicable skill
```

## I have a conference title or abstract — which paper is it?

Work the ladder top-down; stop at the first verified hit. Each rung is
a native kind you call unless marked *(backend)* — a capability another
call routes through, not a kind of its own.

```python
# 1. Held corpus — native paper kind; paper records, not body hits.
search(kind="paper", args={"title": "<conference title>"})
search(kind="paper", args={"author": "<senior author surname>"})

# 2. Semantic Scholar — native kind backed by the live S2 API.
get(kind="semanticscholar", id="<conference title>")   # title search → hits + DOIs
get(kind="semanticscholar", id="authors:<doi>")        # a paper's byline + ORCIDs
get(kind="semanticscholar", id="author:<s2-id>", args={"complete": True})  # full works
search(kind="semanticscholar", q="<title>")            # cached rows only — no live call

# 3. ORCID — native kind; a resolved author's complete works list.
get(kind="orcid", id="0000-0002-1825-0097")

# 4. Citation string — native put, backed by Crossref's bibliographic match.
put(kind="paper", citation="<journal>, <vol>, <page> (<year>)")

# 5. Publisher / repository page — native web kind; the primary source.
get(kind="web", id="<publisher landing page or repo record url>")

# 6. Generic web — native websearch kind, metered per call. Last rung.
get(kind="websearch", q="<title> <author surname>")
```

- Rung 2 is the workhorse. Titles change between conference and
  journal versions, so on a title miss re-query with a distinctive
  method/material phrase, then walk the senior author's works —
  `args={'complete': True}` matters, the capped default has hidden the
  right paper before. S2 rate-limits bursts: a 429 or a put that sits
  in backoff is a provider-blocked rung, not a negative.
- Rung 3's complete works list is the strongest "what else did this
  group publish" check; the kind is dark on a deployment without ORCID
  credentials ([[precis-orcid-help]] covers the auth).
- Rung 4 resolves `journal vol page (year)` through Crossref; exactly
  one full-field match mints the stub, weaker matches return `conf=`
  candidates to pick from.
- A DOI you already hold is verified on the way in *(backend)*:
  `put`/`get` checks it against Crossref, then DataCite, then JaLC —
  registration agencies, not kinds (JaLC covers J-STAGE and other
  Japan-minted DOIs). OpenAlex is also backend-only — a fetch-cascade
  provider, never a `kind='openalex'`; an OpenAlex 429 is
  provider-blocked, not "searched and found nothing".
- Rungs 5–6 are the fallback when the providers above are insufficient
  or unavailable. The publisher landing page settles which record is
  the version of record; `websearch` costs ~$0.001 a call, so spend it
  on the hard cases.

## How do I know a hit is the same work?

Identity needs **authors plus substantive agreement** — shared
abstract/method/material content — never DOI existence or title
similarity alone.

- A changed title is normal: conference → journal versions are
  routinely retitled. Match the byline overlap and what the paper
  *does* (method, material system, key result).
- Same-author near-misses are the classic false positive: a group
  publishes a family of adjacent papers, so "right authors" alone is
  not identity either.
- A hit sharing neither byline nor substance is not the corresponding
  publication, however good the title match.

## Classify every hit — six outcomes

One class per source item, with the evidence that decided it:

- **corresponding manuscript** — the publication the source became.
  The only outcome you queue (see below).
- **prior method / cited / related work** — a real paper, wrong
  identity. Keep as context; do not queue it for this source.
- **duplicate version** — a second record of the same work
  (preprint/proceedings/journal). Prefer the published version; queue
  once.
- **meeting-abstract-only** — the source is a conference abstract that
  never became a citable publication. Nothing to queue.
- **no verified public publication** — every rung exhausted; record
  the negative with the rungs tried.
- **provider-blocked** — a rung refused (rate limit, timeout, open
  breaker), so the search is incomplete. Record which provider; this
  is uncertainty to retry, not a negative.

Queue only a **corresponding manuscript**:
`put(kind='paper', doi=…)` on the verified DOI. Preserve the evidence
URLs and, on a negative, the searches that produced nothing — a later
run should be able to audit why an item was skipped.

## Run it over hundreds of items

Make the pass resumable: one append-only JSONL receipt line per item,
written as you go. Replaying the file recreates the state, so an
interrupted run resumes at the first missing id:

```json
{"source_id": "MMM12-014", "outcome": "corresponding manuscript", "doi": "10.1038/…", "evidence": ["<publisher url>", "<s2 hit>"], "note": "retitled at journal"}
{"source_id": "MMM12-015", "outcome": "no verified public publication", "reasons": ["S2 title+author miss", "Crossref citation miss", "publisher site checked"], "evidence": []}
{"source_id": "MMM12-016", "outcome": "provider-blocked", "provider": "openalex", "reasons": ["429 after retries"]}
```

Cache each provider's raw responses keyed on the query sent — a re-run
after an outage re-reads the cache instead of re-spending provider
budget (`search(kind='semanticscholar', …)` already re-queries earlier
S2 hits for free).

## See also

- [[precis-paper-help]] — held-corpus lookup by title/author/DOI
- [[precis-stubs-help]] — the stub → fetch pipeline this feeds
- [[precis-author-discovery-help]] — author-network walk mechanics
- [[precis-orcid-help]] — author identity nodes + works diff
- [[precis-web-help]] — fetching a publisher page as a primary source
- [[precis-perplexity-help]] — the metered websearch tier

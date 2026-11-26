---
id: precis-identify-publication-help
family: paper
title: precis — identify the publication behind a citation or abstract
summary: turn a conference title, byline, abstract fragment or partial citation into a verified manuscript and queue it — non-queuing discovery ladder, identity bar, outcome classes, resumable batch receipts
answers:
  - I have a conference title or abstract — which paper is it?
  - I have a citation string or a candidate DOI — how do I match it without queueing?
  - how do I tell whether a search hit is the same work as my citation?
  - which providers are native kinds, which are backend legs, and what do I record when one can't answer?
  - how do I run publication identification over hundreds of items?
applies-to: search/get/put (kind='paper'|'semanticscholar'|'orcid'|'web'|'websearch'|'job')
status: active
tags: [external-sources, workflow]
kinds: [paper, semanticscholar, orcid, web, websearch, job]
---

# precis-identify-publication-help — citation or abstract to a verified manuscript

The goal: turn a conference title, byline, abstract fragment or
incomplete bibliographic citation into the **verified corresponding
publication**, then queue that manuscript. This is identity
resolution, not topic discovery — "find me papers on X" is
[[precis-literature-search-help]]; the backlog identified papers join
is [[precis-stubs-help]].

Two skill indexes list the paper surface: `kinds='paper'` on the toc
is the cross-family superset, `family='paper'` the paper family
itself — check both; neither alone is exhaustive.

## I have a conference title or abstract — which paper is it?

Work the ladder cheapest-to-dearest. Every rung below is
**non-queuing discovery** — none of them mints a stub or an ingest
job. Queueing is the last step, after a candidate clears the identity
bar.

1. **Held corpus** (native `search`, free):

   ```python
   search(kind="paper", args={"title": "<conference title>"})
   search(kind="paper", args={"author": "<surname>"})
   search(kind="paper", q="<candidate DOI>")  # resolves a held DOI directly
   ```

   `title=`/`author=` match the record (trigram title + byline), not
   body text. A `pa<id>` hit is a *candidate* — a title match can be a
   neighbouring same-author paper — and still faces the identity bar.

2. **Semantic Scholar** (native kind, live API) — the first external
   leg; hits carry DOI, abstract and a `held`/`stub`/`NEW` corpus
   flag:

   ```python
   get(kind="semanticscholar", id="<title or topic>")      # ranked hits + DOIs
   get(kind="semanticscholar", id="doi:<candidate DOI>")   # one record to compare
   get(kind="semanticscholar", id="arxiv:<arXiv id>")      # byline of an arXiv-only preprint
   ```

   Free-text `get` reaches S2 live; a free-text
   `search(kind='semanticscholar', …)` re-queries rows `get` already
   cached, but an identifier-shaped query (a DOI or arXiv id) is a
   lookup like `get`: it reuses a cached lookup or fetches live. The `doi:`/`arxiv:` lookup is the non-queuing way
   to check a candidate exists and pull its byline/abstract; a 404 is
   a clean "no record" row. S2's public tier paces calls at ~1/s; a configured
   key lifts it. A 429 that outlives its retries raises — that is a
   provider fault, not a miss (record provider-blocked).

3. **Author completion** (native kinds) — titles change between
   abstract and publication, so a title search misses the real paper;
   the author's works list is where it surfaces:

   ```python
   get(kind="semanticscholar", id="authors:<paper-id>")  # byline + ORCID/S2 ids
   get(kind="semanticscholar", id="author:<s2-id>", args={"complete": True})
   get(kind="orcid", id="0000-0002-1825-0097")           # disambiguated works
   ```

   The `complete` walk is bounded (≤2000) but reaches past the default
   top-50, which has hidden the real match before. ORCID is the public
   API — the caller supplies no credentials (live reads ride the
   deployment's client creds, a cached node needs none) — and gives
   the cleanest byline identity. Never pass `enqueue=` here: it mints
   fetch stubs, i.e. acquisition.

## I have a citation string or a candidate DOI — how do I match it?

4. **Registered bibliographic matching** — two shapes:

   ```python
   put(kind="paper", citation="Phys. Rev. B 47, 558 (1993)")
   get(kind="web", id="https://doi.org/<candidate DOI>")
   ```

   Crossref first checks the derived DOI for article numbers in recognized
   Wiley journals. Other citations and failed DOI checks go to
   `query.bibliographic`. A weak or split match returns `conf=` candidates
   and mints **nothing** —
   that is the candidate-discovery door; a lone full-confidence hit
   mints the stub on the spot, so run `put(citation=)` only where a
   mint is acceptable (the citation itself is the evidence, or a
   near-verified item). An empty "No Crossref match" cannot be told
   from a Crossref outage — check the `crossref-list` provider row
   (table below) before scoring it searched. For a bare DOI, the
   `doi.org` landing page (redirects revalidated hop by hop) and the
   S2 `doi:` lookup vouch existence and hand you the byline+abstract
   to compare — all without queueing. Inside `put`'s verify step the
   registry legs run S2 → Crossref → DataCite → JaLC; they vouch that
   a DOI *exists*, which is never identity by itself.

5. **Primary publisher / repository page** (native `web` kind, free)
   — the verification step when a record looks thin:

   ```python
   get(kind="web", id="https://www.jstage.jst.go.jp/article/…")
   ```

   J-STAGE, proceedings and repository landing pages carry the byline
   + abstract to compare; a PDF URL extracts like a page.

6. **Generic web fallback** (native `websearch` kind, PAID ~$0.001 a
   call) — last resort when the rungs above are exhausted or
   unavailable; its inline citations name the primary pages to confirm
   via `web`:

   ```python
   get(kind="websearch", id="<title> <author> <venue or year>")
   ```

**Queue last.** `put(kind='paper', doi='<verified DOI>')` (or
`arxiv=`/`title=`) only *after* the candidate clears the identity bar.
A paper `get` on an unheld DOI queues nothing by default; it answers not
held with the `put` hint. Only `args={'queue': 'bounded'}` (or
`'explicit'`) makes a view-less `get` queue ingest; `view=` always
disables it. Queueing before verification
can burn a fetch slot and leave a stub for the wrong paper.

## Which providers are native, and what when one can't answer?

**OpenAlex has no agent-facing match route** — `kinds='openalex'`
refuses as unknown (don't invent one), and `web` can't render
`api.openalex.org` (it extracts HTML/PDF, not JSON). What you can do:

- check its health before trusting silence:
  `get(kind='job', id='/limits?provider=openalex')`;
- reach it through `websearch` (rung 6) or a `doi.org`/publisher page;
- let the backend legs run it for you: once a verified stub is queued,
  the fetch cascade's OpenAlex leg, the `meta.openalex` enrichment and
  author disambiguation apply it automatically. A silent OpenAlex leg
  is provider-blocked, never "searched".

| provider | surface | auth | pacing | can't answer → |
|---|---|---|---|---|
| S2 | native `semanticscholar` | none; a configured key lifts the tier | shared `s2` row, ~1/s public | error after retries → provider-blocked |
| ORCID | native `orcid` | none caller-side (deployment client creds for live reads) | shared `orcid` row | error → provider-blocked |
| Crossref | backend — `put(citation=)`, `put` verify | polite pool when `contact.crossref_mailto` is set, else anonymous | `crossref` / `crossref-list` rows | empty citation result is ambiguous — check `/limits`, else provider-blocked |
| DataCite | backend — `put` verify leg | none | shared `datacite` row | fault mints the stub marked unverified; not proof the DOI is wrong |
| JaLC | backend — `put` verify leg (J-STAGE et al., e.g. 10.69410) | none | `host:api.japanlinkcenter.org`, ~1/s (`/limits?hosts=1`) | as DataCite |
| OpenAlex | backend only — fetch cascade, `meta.openalex`, disambiguation | anonymous + `mailto` fast pool; optional paid key server-side | shared `openalex` row | `/limits?provider=openalex` → provider-blocked |
| publisher pages | native `web` | none | per-host ~1/s | `Upstream` → provider-blocked |
| generic web | native `websearch` | paid per call | — | failure → provider-blocked |

`get(kind='job', id='/limits')` lists every provider row
([[precis-job-observe-help]]); pin one with `?provider=<name>` and add
`?hosts=1` for the `host:<netloc>` lanes. A starved or parked row
means "blocked", not "searched and missed".

## How do I know it's the same work?

Identity needs **authors AND substantive agreement — both**:

- **the byline matches** — identity and order, not just the lead
  surname, AND
- **the abstract / method / material content agrees** — the same
  experiment or result, not just the same topic.

A DOI resolving, a similar title, or byline+venue agreement alone are
never enough: changed titles between abstract and publication are
routine, and the two classic false positives — the neighbouring
same-author paper and the meeting abstract itself — both pass a weak
bar. Venue + year corroborate a match; they cannot establish one.

The bar applies to **every** candidate — a held `pa<id>` surfaced by
a corpus title search is a candidate like any other, not a
pre-verified answer.

A preprint of the same work is still the same work — prefer the
published version when both exist and record the twin.

## What do I do with each candidate?

Sort every input into exactly one outcome class:

- **corresponding manuscript** — the verified publication; queue it:
  `put(kind='paper', doi=…)` on the evidence DOI.
- **related work** — a real paper by the same authors or on the same
  topic that is prior method, a cited or neighbouring work — not this
  contribution.
- **duplicate version** — a second record of the same work (preprint ↔
  published, proceedings ↔ journal edition); keep the published one.
- **meeting-abstract-only** — the contribution exists only as the
  conference abstract; there is no manuscript to queue.
- **no verified publication** — every rung searched, nothing cleared
  the identity bar.
- **provider-blocked** — a 429, timeout or outage kept a rung from
  answering; record which provider and retry later. It is uncertainty,
  not evidence of absence — never collapse it into no verified
  publication.

## How do I run this over hundreds of items?

Keep a resumable receipt — one JSON line per input, appended as each
item resolves, so a crashed batch resumes where it stopped:

```json
{"input": "conf-042", "title": "…", "outcome": "corresponding manuscript",
 "doi": "10.x/…", "evidence": ["https://…"],
 "tried": ["corpus", "s2", "orcid"], "negative": "s2 top-10 same-author misses"}
```

The file is the cache — a re-run skips inputs already resolved. Run
discovery and queueing as separate passes: the discovery pass writes
the receipt, the queue pass `put`s only lines marked `corresponding
manuscript`. Evidence URLs live on the line (the DOI and the page that
verified it); a negative line carries *why* each provider missed, and
a provider-blocked line names the provider. The backlog takes verified
real manuscripts only; everything else stays on the receipt as the
audit trail.

## See also

- [[precis-paper-help]] — held-paper lookup incl. `title=`/`author=`
- [[precis-stubs-help]] — the backlog identified papers join
- [[precis-author-discovery-help]] — author BFS machinery
- [[precis-literature-search-help]] — topic discovery, not identification
- [[precis-doi-extract-help]] — pull DOIs out of citation text
- [[precis-web-help]] — fetch publisher and repository pages
- [[precis-job-observe-help]] — `/limits` provider rows

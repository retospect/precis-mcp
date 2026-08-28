---
id: precis-stubs-help
title: precis — papers we still need to get
summary: paper acquisition backlog — stub list, fetch state, reason each is waiting
answers:
  - how do I see the papers we still need to fetch PDFs for?
  - how do I find just the DOI stubs nobody has tried fetching yet?
  - how do I add a missing paper to the acquisition backlog?
  - why did a stub disappear from the backlog without a PDF landing?
  - how do I find papers on a topic we don't already have?
  - how do I exclude a draft's cites from a discovery search?
applies-to: search(kind='paper', view='stubs'|'chase-queue', exclude=), get(kind='semanticscholar'), put (kind='paper')
status: active
---

# precis-stubs-help — papers we still need to get

A *stub* is a paper the corpus knows about by identifier (DOI / arXiv
/ S2) but doesn't hold the PDF for yet. Stubs are the backlog of
papers still to acquire — surface them, see why each is waiting, and
add new ones.

## List the papers we still need to get
## What papers are missing PDFs?
## Show the acquisition backlog

```python
search(kind="paper", view="stubs")
search(kind="paper", view="stubs", n=50)  # default 25
```

Each row shows the ref id, the best external identifier, the cite key,
and a one-line state (`awaiting fetch`, `no OA version available`,
`PDF downloaded; awaiting watcher ingest`, …), with `prio N` appended
once the `stub_rank` pass has scored it (1=hottest..10=coldest —
relevant-first; unranked stubs carry no `prio` suffix and sort last).
Ties fall back to oldest-request-first. `q=` is ignored — the view *is*
the filter.

Stubs scoring in the uncertain middle of the ranking additionally carry
a one-time LLM triage label — `· core|adjacent|explore|off` on the
state line, with its one-line reason below. The label nudges `prio`
(core hotter, explore/off colder) on every re-rank; explicit signals
(a dream/quest acquisition request, a cite-cold pin) always win over
it. Query labeled stubs directly: `refs.meta->>'llm_label'` (reason in
`llm_reason`, decision metadata in `llm_band`).

## What should I go find a PDF for right now?
## Just the DOI stubs nobody has tried fetching yet

```python
search(kind="paper", view="chase-queue")
search(kind="paper", view="chase-queue", n=50)  # default 25
```

A tighter slice of the same backlog: DOI-only (the identifier kind the
`fetch_oa` cascade covers most reliably), `prio`-first same as
`view='stubs'`, ties falling to never-tried-first rather than
oldest-request-first. Use `view='stubs'` for the full backlog (every id
kind); use `view='chase-queue'` when you want the next thing worth
chasing down manually.

## See just the papers a dream decided to chase

```python
search(kind="paper", tags=["DREAM:acquire"])
```

`view='stubs'` is the whole backlog (chase-worker stubs included);
the `DREAM:acquire` tag marks only the ones a dream explicitly wanted.

## Open a stub to see where it came up

```python
get(kind='paper', id=<ref_id>)
```

A stub has no body yet. `get` shows its metadata and inbound links —
the finding or paper that cited it lives on the other end of a
`related-to` link.

## Add a paper to the backlog
## Queue a missing paper for fetch
## Request a paper the library doesn't have

```python
put(kind="paper", doi="10.1038/nature10352")  # best — resolvable id
put(kind="paper", arxiv="2401.00001", title="…")  # or an arXiv id
put(kind="paper", identifier="s2:<id>")  # or a Semantic Scholar id
put(kind="paper", title="Some Paper With No DOI Yet")  # title-only backlog stub
```

`put(kind='paper', …)` mints a **stub only** — it requests the paper
into this backlog; it never writes a body (paper bodies are
import-only, via `.acatome` ingest). Idempotent: a paper already held
or already wanted is a no-op.

A **DOI / arXiv / S2 id is strongly preferred** — the stub carries the
id, and an explicit `put` jumps the fetch queue: the stub is pinned to
the front (`prio=1`) so `fetch_oa` tries it on its very next pass —
re-requesting an already-wanted stub re-pins it, including one
immediate retry for a stub deep in fetch backoff. (Auto-discovered
stubs — citation chase, watched feeds — instead wait their ranked
turn.) A hallucinated identifier is rejected up front (pass
`verify=False` to force a known-real preprint S2 hasn't indexed). A **title-only** stub
just parks in the backlog until someone supplies an identifier — no
auto-fetch. Optional: `year=` (disambiguates the cite key) and
`reason=` (why it's wanted).

## Don't know the identifier? Find it first

A stub is only auto-fetched when it carries a resolvable id, so find
the DOI before you request. Walk a held paper's citation graph or
search by topic on Semantic Scholar — each hit carries a DOI to stub:

```python
get(kind="semanticscholar", id="refs:<held-doi>")  # papers it cites
get(kind="semanticscholar", id="cites:<held-doi>")  # papers citing it
get(kind="semanticscholar", id="<title or topic>")  # search → ranked hits + DOIs
```

## What don't we have yet, on this topic?

A plain topic search **always** diffs its hits against the held corpus
(DOI → arXiv id → normalized title) and flags every one —
`held: pa123` (a body is on file), `stub: pa456` (known, no PDF yet), or
`NEW` (nothing in the corpus matches) — with the abstract as a preview,
so you don't have to eyeball each title against a separate search:

```python
get(kind="semanticscholar", id="wang tile guided self assembly")
```

**Skip what a draft already cites.** `exclude=` accepts a mixed list of
paper slugs/ids AND containers — a whole draft (`dr…`) or a draft-chunk
subtree (`dc…`) — resolved server-side to every paper cited anywhere
within (`[pa…]` direct, `[pc…]` via its owning paper, `[fi…]` via a
claim hub's grounding/supporter papers). The canonical "find sources
this draft doesn't already have" query:

```python
get(kind="semanticscholar", id="wang tile guided self assembly", exclude=["dr42995"])
search(kind="paper", q="wang tile guided self assembly", per_paper=1, exclude=["dr42995"])
```

The first widens the net to the open literature (flagged); the second
finds the best already-held chunk per paper the draft doesn't cite yet.
`exclude=["dc<subtree-chunk>"]` narrows either to one section's cite
closure instead of the whole draft.

**Accept a `NEW` hit** — mint it into the acquisition backlog the same
one-step way as any other stub, using the DOI/arXiv id the hit carries:

```python
put(kind="paper", doi="<doi from the NEW hit>")
```

## Why did a stub disappear from the backlog without a PDF?

`fetch_oa` checks each claimed stub's own DOI against Crossref *before*
trying to download it. A **retraction** has nothing worth chasing an OA
copy for: the worker stamps `retraction_status='retracted'`, drops a
`retraction_skip` event instead of a fetch attempt, and `view='stubs'` /
`view='chase-queue'` stop listing it. A **correction** or **expression
of concern** is only a flag: the worker stamps the status (so the reader
banner shows it) but still fetches the PDF — so those stubs stay in the
backlog until acquired, and the gate re-checks them each pass to catch a
later escalation to `retracted`. Confirm via `get(kind='paper',
id=<ref_id>)`, which shows the status on the ref.

## See also

```python
get(kind="skill", id="precis-paper-help")  # read, cite, search held papers (+ S2 nav)
get(kind="skill", id="precis-finding-help")  # chasing un-ingested DOIs
get(kind="skill", id="precis-search-help")  # search args incl. view=
```

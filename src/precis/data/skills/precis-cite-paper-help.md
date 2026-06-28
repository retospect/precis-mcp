---
id: precis-cite-paper-help
title: precis — how do I cite a paper?
summary: the cite-a-paper router — in corpus → write the paper-chunk handle inline `[pc234]`; not in corpus → stub, wait for ingest, then cite; empirical claim → finding `[fi<id>]`
applies-to: get/put (kind='paper'|'citation'|'finding'|'todo')
status: active
---

# precis-cite-paper-help — how do I cite a paper?

This is the **router** for "I'm writing and I need to cite something."
A citation in a draft is the **bare supporting paper-chunk handle,
written inline in your prose**: `[pc234]` (paper chunk 234). Citing is
not a verb at all — it's a short decision about *which* handle to drop
in. Pick the branch that matches what you have.

## How do I cite a paper?
## I want to add a citation to my manuscript
## What's the right way to reference a source here?

Three questions, in order:

1. **Is the paper already in the corpus?** Find out with
   `search(kind='paper', q='<author or topic>')` or
   `get(kind='paper', id='<DOI>')`.
   - **Yes** → write its supporting chunk handle inline (below: *Cite a
     paper that's in the corpus*).
   - **No** → request it, then wait (below: *Cite a paper we don't
     have yet*).
2. **Is the claim empirical and worth chasing to its *primary*
   source?** (e.g. "2.4 kV gate bias", "12% efficiency") → register a
   `kind='finding'` and cite the in-flight finding `[fi<id>]` until the
   chase walks the citation chain back. See [[precis-finding-help]].

You may *optionally* mint a `kind='citation'` verification record
alongside (claim + `verifier_confidence`) — but that is not how you
cite and not what builds the bibliography. See [[precis-citation-help]].

## Cite a paper that's in the corpus
## Drop the supporting chunk handle inline
## Several chunks back one sentence

Write the supporting paper-chunk handle directly in your prose:

```text
Aqueous synthesis yields higher quantum yields than hot-injection [pc234].
```

Several supporting chunks — in one paper or across papers — list
together, no separators:

```text
…higher quantum yields than hot-injection [pc232][pc234][pc593].
```

Patents cite the same way by their chunk handle `[pk<id>]`; an in-flight
`finding` is cited `[fi<id>]` until the chase resolves it.

The handle is a value you **copy from search / get output** — never
construct or guess it. There is no slug to assemble. Find it with a
scoped search and read the chunk to confirm support before you write it
(the *reader* side — [[precis-check-source-help]]):

```python
search(kind='paper', q='<claim or key phrase>', scope='<slug>')  # → returns pc<id> handles
get(id='pc234')                                                   # read it; the chunk IS the evidence
```

The author never types `\cite{}` or `\citequote{}` — both are retired
(export-only). The Tier-B export engine resolves each `[pc<id>]` →
its paper and renders `\cite{}` plus **one bibliography entry per
paper** at compile time. A hand-written `\cite{electrochemical22}`
matches nothing and is wrong.

**A memory / thought / other draft is a link, not a citation.** Drop a
`[me<id>]` or `[dc<id>]` handle to record a `related-to` provenance
edge — it never reaches the bibliography. Citations are to the
literature, not to our notes.

## Get a formatted reference string for a paper
## BibTeX / RIS / EndNote for a paper

You rarely need this — the export engine builds the bibliography from
the inline handles. When you do want a formatted reference string,
address the paper by its `pa<id>` handle:

```python
get(id='pa<id>', view='bibtex')    # \bibitem / BibTeX
get(id='pa<id>', view='ris')        # RIS
get(id='pa<id>', view='endnote')    # EndNote
```

Copy the `pa<id>` handle from `search(kind='paper', q='…')` or
`get(kind='paper', id='<DOI>')` — see [[precis-paper-help]].

## Cite a paper we don't have yet
## I only have a DOI / arXiv id — the paper isn't ingested
## How do I cite a paper that isn't in the corpus?

Three steps: **request it → park a todo that waits for it → cite once
it lands.**

**1. Request the paper (mint a stub).** The `fetch_oa` worker chases an
open-access PDF for any stub carrying a resolvable id:

```python
put(kind='paper', doi='10.1038/nature10352')         # best — resolvable id
put(kind='paper', arxiv='2401.00001', title='…')     # or an arXiv id
put(kind='paper', identifier='s2:<id>')              # or a Semantic Scholar id
put(kind='paper', title='Some paper with no DOI yet') # title-only backlog stub
```

This mints a **stub only** — it never writes a body (bodies are
import-only) and is idempotent. Full contract in [[precis-stubs-help]].

**2. Park a todo that waits for the paper to appear.** A
`meta.auto_check` of type `paper_ingested` resolves true once the paper
exists *and* has at least one embedded chunk (i.e. it's actually
citable, not just requested):

```python
wait = put(kind='todo',
           parent_id=<your writing todo>,
           text='[auto] wait for 10.1038/nature10352 ingested+indexed',
           meta={'auto_check': {
               'type': 'paper_ingested',
               'doi': '10.1038/nature10352',
               'timeout_at': '2026-07-10T00:00:00+00:00',  # surface a stalled fetch
           }})
# block the writing leaf behind it so it leaves the doable rotation:
link(kind='todo', id=<your writing leaf>, target=f'todo:{wait.id}', rel='blocked-by')
```

The waiting leaf flips to `STATUS:done` when the paper lands (or
`STATUS:auto-timeout` if the fetch stalls past `timeout_at`). Mechanism
+ the other evaluators: [[precis-auto-tasks-help]].

**3. Cite it once it's in.** When the wait resolves, the paper is a
normal corpus paper — go back to *Cite a paper that's in the corpus*
and drop its `[pc<id>]` handle inline.

If you can't wait (the source may never be OA), register a
`kind='finding'` against the chunk where you read the claim, cite the
finding `[fi<id>]` inline meanwhile, and let the chase try to source
it — see [[precis-finding-help]].

## Cite an empirical claim and chase it to the primary source
## I read a number in a review — find who actually measured it
## Track a claim back to where it was first reported

Reviews cite reviews; the value is the *primary* source. Register a
finding, cite it inline as `[fi<id>]`, and let the worker walk the
chain:

```python
put(kind='finding',
    title='gate-bias 2.4 kV / 30 s on Si/SiO2',
    body='2.4 kV across the 50 nm gate oxide for 30 s, Cu top contact, N2.',
    cited_in='miller23a~42')     # the chunk where YOU read it (corpus handle)
# → cite [fi<id>] inline; the chase swaps it for the primary [pc<id>] once resolved
```

**`cited_in=` wants a corpus handle, not a DOI.** `cited_in='doi:…'` is
**rejected** (the link parser has no `doi` kind). Point it at the paper
chunk you actually read the claim in (`slug` or `slug~ord`). If that
source isn't in the corpus either, stub + wait for it first (above),
then register the finding against its chunk. Full chase contract:
[[precis-finding-help]].

## inline handle vs finding vs citation vs bibtex — which do I use?

| You have… | Use | Gives you |
|---|---|---|
| A corpus chunk that backs the claim | inline `[pc<id>]` (or `[pk<id>]` for a patent) | a citation the export engine resolves |
| A claim whose *primary* source must be chased | `kind='finding'`, cite `[fi<id>]` | an in-flight cite + worker chase |
| Our own note/thought/draft as provenance | inline `[me<id>]` / `[dc<id>]` | a `related-to` link, **not** a bibliography entry |
| Optional verification audit of a claim | `kind='citation'` (claim + `verifier_confidence`) | a verification record (not required to cite) |
| A formatted reference string for a paper | `get(id='pa<id>', view='bibtex'/'ris')` | a BibTeX/RIS string |
| A paper to cite that we don't hold | `put(kind='paper', doi=…)` + waiting todo | a stub the fetcher chases |

A literature-review sentence typically does all of this over its life:
stub the missing source, wait, then drop its `[pc<id>]` handle inline.

## See also

```python
get(kind='skill', id='precis-citation-help')     # the inline [pc<id>] cite + optional verification record
get(kind='skill', id='precis-check-source-help') # find the chunk, read its surrounds, judge support
get(kind='skill', id='precis-finding-help')      # chase a claim to its primary source
get(kind='skill', id='precis-stubs-help')        # request a paper we don't hold
get(kind='skill', id='precis-auto-tasks-help')   # a todo that waits for the paper to appear
get(kind='skill', id='precis-paper-help')        # find/read papers; pa<id>/pc<id> handles; bibtex/ris views
get(kind='skill', id='precis-write-paper-help')  # claim-level citation density discipline
get(kind='skill', id='precis-bibliography-help') # read side: who cites this paper
```

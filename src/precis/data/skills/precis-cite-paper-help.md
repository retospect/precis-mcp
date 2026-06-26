---
id: precis-cite-paper-help
title: precis — how do I cite a paper?
summary: the cite-a-paper router — in corpus → bibtex + verified citation; not in corpus → stub, wait for ingest, then cite; empirical claim → finding
applies-to: get/put (kind='paper'|'citation'|'finding'|'todo')
status: active
---

# precis-cite-paper-help — how do I cite a paper?

This is the **router** for "I'm writing and I need to cite something."
Citing in precis is not one verb — it's a short decision. Pick the
branch that matches what you have, then follow the skill it points at.

## How do I cite a paper?
## I want to add a citation to my manuscript
## What's the right way to reference a source here?

Three questions, in order:

1. **Is the paper already in the corpus?** Find out with
   `search(kind='paper', q='<author or topic>')` or
   `get(kind='paper', id='<DOI>')`.
   - **Yes** → cite it directly (below: *Cite a paper that's in the
     corpus*).
   - **No** → request it, then wait (below: *Cite a paper we don't
     have yet*).
2. **Do I need the exact passage that backs my claim?** (Almost always
   yes for an empirical or quantitative claim.) → mint a verified
   `kind='citation'` pairing the claim with the verbatim source quote.
   See [[precis-citation-help]].
3. **Is the claim empirical and worth chasing to its *primary*
   source?** (e.g. "2.4 kV gate bias", "12% efficiency") → register a
   `kind='finding'` and let the chase walk the citation chain back.
   See [[precis-finding-help]].

These compose: you usually `get` the bibliographic entry **and** mint a
`citation` for the specific quote.

## Cite a paper that's in the corpus
## Get a BibTeX / RIS / EndNote entry for a paper
## I have the slug — give me the reference entry

```python
get(kind='paper', id='<slug>', view='bibtex')    # \bibitem / BibTeX
get(kind='paper', id='<slug>', view='ris')        # RIS
get(kind='paper', id='<slug>', view='endnote')    # EndNote
```

Don't guess the slug — `search(kind='paper', q='…')` or
`get(kind='paper', id='<DOI>')` to find it (slug grammar is lossy;
see [[precis-paper-help]]).

## Cite a specific claim with the passage that supports it
## I need the exact quote that backs this sentence
## Pin a claim to a verbatim source span

This is the high-value move — it's what makes the citation survive
review. Find the passage, confirm it actually supports the claim, then
persist the pair:

```python
put(kind='citation',
    text='MOF X improves CO2 reduction by 12%',           # the claim
    source_handle='pc7',                                   # the chunk
    source_quote='we observed 12% Faradaic efficiency '    # verbatim
                 'for CO2 reduction at -0.3 V vs RHE',
    verifier_confidence=0.95,
    link='paper:collins06', rel='cites')
```

The full write protocol (verbatim discipline, numeric matching, the
`\citequote` bridge for LaTeX) is in [[precis-citation-help]]. The
*reader* side — find the passage, read its surrounds, judge whether it
supports the point — is [[precis-check-source-help]]. Run that before
you persist.

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
normal corpus paper — go back to *Cite a paper that's in the corpus*.

If you can't wait (the source may never be OA), register a
`kind='finding'` against the chunk where you read the claim and let the
chase try to source it — see [[precis-finding-help]].

## Cite an empirical claim and chase it to the primary source
## I read a number in a review — find who actually measured it
## Track a claim back to where it was first reported

Reviews cite reviews; the value is the *primary* source. Register a
finding and let the worker walk the chain:

```python
put(kind='finding',
    title='gate-bias 2.4 kV / 30 s on Si/SiO2',
    body='2.4 kV across the 50 nm gate oxide for 30 s, Cu top contact, N2.',
    cited_in='miller23a~42')     # the chunk where YOU read it (corpus handle)
# → placeholder [42]; precis resolve substitutes the primary cite_key later
```

**`cited_in=` wants a corpus handle, not a DOI.** `cited_in='doi:…'` is
**rejected** (the link parser has no `doi` kind). Point it at the paper
chunk you actually read the claim in (`slug` or `slug~ord`). If that
source isn't in the corpus either, stub + wait for it first (above),
then register the finding against its chunk. Full chase contract:
[[precis-finding-help]].

## citation vs finding vs bibtex — which do I use?

| You have… | Use | Gives you |
|---|---|---|
| A claim + the verbatim quote that backs it | `kind='citation'` | a verified claim→quote record |
| A claim whose *primary* source must be chased | `kind='finding'` | a placeholder + worker chase |
| Just need the formatted reference entry | `get(view='bibtex'/'ris')` | a BibTeX/RIS string |
| A paper to cite that we don't hold | `put(kind='paper', doi=…)` + waiting todo | a stub the fetcher chases |

A literature-review sentence typically uses all three over its life:
stub the missing source, wait, then mint a citation against its chunk.

## See also

```python
get(kind='skill', id='precis-citation-help')     # write a verified citation (the quote pairing)
get(kind='skill', id='precis-check-source-help') # find a citation, read its surrounds, judge support
get(kind='skill', id='precis-finding-help')      # chase a claim to its primary source
get(kind='skill', id='precis-stubs-help')        # request a paper we don't hold
get(kind='skill', id='precis-auto-tasks-help')   # a todo that waits for the paper to appear
get(kind='skill', id='precis-paper-help')        # find/read papers; bibtex/ris views; slug grammar
get(kind='skill', id='precis-write-paper-help')  # claim-level citation density discipline
get(kind='skill', id='precis-bibliography-help') # read side: who cites this paper
```

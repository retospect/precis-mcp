---
id: precis-cite-paper-help
title: precis — how do I cite a paper?
summary: the cite-a-paper router — a cite is always a finding hub `[fi<id>]`; hub exists → cite it; paper in corpus → ground a hub on its passages and cite that; paper not held → stub, wait, then ground; empirical claim with no primary held → chase finding `[fi<id>]`
answers:
  - what's the right way to cite a paper that's already in the corpus?
  - how do I cite a paper I only have a DOI for, that isn't ingested yet?
  - I read a number in a review — how do I trace it back to who actually measured it?
  - should I use an inline handle, a finding, a citation record, or bibtex?
applies-to: get/put/search (kind='paper'|'finding'|'citation'|'todo')
status: active
tags: [drafting, orientation]
kinds: [paper, citation, finding, todo]
---

# precis-cite-paper-help — how do I cite a paper?

This is the **router** for "I'm writing and I need to cite something."
You never cite a paper directly. A citation in a draft is a **finding
hub handle written inline**, `[fi<id>]`; the paper's passages
(`[pc<id>]`, or `[pk<id>]` for a patent) are the hub's *grounding*,
attached as evidence edges. Citing is a short decision about *which
hub* to drop in — and, when none exists, about which passages to mint
one on. The full four-step procedure (search hubs → ground + mint →
adversarial search → cite) is [[precis-citation-help]]; this file
routes you to the branch that matches what you have.

## How do I cite a paper?
## I want to add a citation to my manuscript
## What's the right way to reference a source here?

Three questions, in order:

1. **Does a finding hub already assert this claim?**
   `search(kind='finding', q='<the claim>', status='*', mode='semantic')`
   — same claim, same scope → cite `[fi<id>]` (after the adversarial
   check, [[precis-citation-help]] step 3). Done.
2. **Is the paper in the corpus?** `search(kind='paper', q='<topic or
   phrase>')` or `get(kind='paper', id='<DOI>')`.
   - **Yes** → ground a hub on its supporting passages, cite the hub
     (below: *Cite a paper that's in the corpus*).
   - **No** → request it, wait, then ground (below: *Cite a paper we
     don't have yet*).
3. **Is the claim empirical and its *primary* source not held?**
   ("2.4 kV gate bias", "12% efficiency" read in a review) → register
   a chase `kind='finding'` and cite that in-flight `[fi<id>]`; the
   chase walks the citation chain back ([[precis-finding-help]]).

You may *optionally* mint a `kind='citation'` verification record
alongside (claim + `verifier_confidence`) — not how you cite and not
what builds the bibliography ([[precis-citation-help]]).

## Cite a paper that's in the corpus
## Ground a hub on the paper's passages, then cite the hub

Find the passages, read them, mint (or converge onto) the hub, cite it:

```python
search(kind="paper", q="<the claim's most distinctive phrase>")  # → pc<id> handles
get(id="pc234")  # read it; the chunk must state the claim's core
get(id="pa5", view="claims")  # hubs this paper already grounds — cite one if it fits
put(kind="finding",
    title="<one self-contained claim sentence>",
    supporters=[{"paper": "pa5", "source_handle": "pc234"}])
# → "claim hub fi<id>" — cite [fi<id>]
```

```text
Aqueous synthesis yields higher quantum yields than hot-injection [fi41].
```

Several papers back the same claim → several supporters on **one**
hub, never several cites. Several distinct claims → several hubs,
listed together: `[fi41][fi92]`. Sentence rules, scope, notation and
the search-before-mint gate: [[precis-taproot-mint-help]]. Attaching to
a hub that already exists: [[precis-taproot-hub-edit-help]].

Handles are values you **copy from search / get / put output** — never
constructed. The author never types LaTeX citation commands; the export
engine resolves each `[fi<id>]` to its originator paper(s) and renders
one bibliography entry per paper at compile time.

**A memory / thought / other draft is a link, not a citation.** Drop a
`[me<id>]` or `[dc<id>]` handle to record a `related-to` provenance
edge — it never reaches the bibliography.

## Get a formatted reference string for a paper
## BibTeX / RIS / EndNote for a paper

You rarely need this — the export engine builds the bibliography from
the inline handles. For a formatted string, address the paper by its
`pa<id>` handle:

```python
get(id="pa<id>", view="bibtex")  # \bibitem / BibTeX
get(id="pa<id>", view="ris")  # RIS
get(id="pa<id>", view="endnote")  # EndNote
```

## Cite a paper we don't have yet
## I only have a DOI / arXiv id — the paper isn't ingested
## How do I cite a paper that isn't in the corpus?

Three steps: **request it → park a todo that waits for it → ground and
cite once it lands.**

**1. Request the paper (mint a stub).** The open-access fetcher chases
a PDF for any stub carrying a resolvable id:

```python
put(kind="paper", doi="10.1038/nature10352")  # best — resolvable id
put(kind="paper", arxiv="2401.00001", title="…")  # or an arXiv id
put(kind="paper", identifier="s2:<id>")  # or a Semantic Scholar id
put(kind="paper", title="Some paper with no DOI yet")  # title-only backlog stub
```

A **stub only** — never a body (bodies are import-only); idempotent.
Full contract: [[precis-stubs-help]].

**2. Park a todo that waits for the paper to appear.** A
`meta.auto_check` of type `paper_ingested` resolves true once the paper
exists *and* has at least one embedded chunk (citable, not just
requested):

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
`STATUS:auto-timeout` if the fetch stalls past `timeout_at`) —
[[precis-auto-todo-help]].

**3. Ground and cite once it's in.** The paper is now a normal corpus
paper — go back to *Cite a paper that's in the corpus*.

If you can't wait (the source may never be open access), register a
chase `kind='finding'` against the chunk where you read the claim and
cite that `[fi<id>]` meanwhile — next section.

## Cite an empirical claim and chase it to the primary source
## I read a number in a review — find who actually measured it
## Track a claim back to where it was first reported

**Ground on the paper that did the work, never the paper that mentions
it.** A review's summary, a prior-art/related-work section, an
introduction citing someone else's result — all hearsay, not a source.
Standards here are stricter than for human-authored prose: a
machine-made citation error is judged the way a self-driving-car
fatality is judged against a human driver's, far more harshly — so
provenance has to be airtight, chunk → paper → hub → claim, no hop
skipped. Search with text phrased like the *answer*, not the question,
and skim past any hit sitting in an introduction/background/related-
work block (check the paper's `view='toc'`). **A hanging, uncited
claim beats a hearsay one** — leave it unresolved rather than ground a
hub on the secondhand mention.

Register a chase finding, cite it inline as `[fi<id>]`, and let the
worker walk the chain:

```python
put(
    kind="finding",
    title="gate-bias 2.4 kV / 30 s on Si/SiO2",
    body="2.4 kV across the 50 nm gate oxide for 30 s, Cu top contact, N2.",
    cited_in="miller23a~42",
)  # the chunk where YOU read it (corpus handle)
# → cite [fi<id>] inline; the chase grounds it on the primary once found
```

**`cited_in=` wants a corpus handle, not a DOI.** `cited_in='doi:…'` is
**rejected**. Point it at the paper chunk you read the claim in (its
`pc<id>` handle). If that source isn't held either, stub + wait for it
first (above), then register the finding against its chunk. Full chase
contract: [[precis-finding-help]].

## inline handle vs finding vs citation vs bibtex — which do I use?

| You have… | Use | Gives you |
|---|---|---|
| A hub that asserts your claim at your scope | inline `[fi<id>]` | the living cite; resolves to the current originator(s) |
| A corpus chunk that backs the claim | ground: `[pc<id>]` (or `[pk<id>]` for a patent) as `source_handle` on a hub, cite `[fi<id>]` | a passage-grounded evidence edge under one shared hub |
| A claim whose *primary* source must be chased | chase `kind='finding'`, cite `[fi<id>]` | an in-flight cite + worker chase |
| A paper that disagrees with the hub | `link(rel='disputes')` + cite both hubs where the prose says so | a visible, non-blocking disagreement |
| Our own note/thought/draft as provenance | inline `[me<id>]` / `[dc<id>]` | a `related-to` link, **not** a bibliography entry |
| Optional verification audit of a claim | `kind='citation'` (claim + `verifier_confidence`) | a verification record (not required to cite) |
| A formatted reference string for a paper | `get(id='pa<id>', view='bibtex'/'ris')` | a BibTeX/RIS string |
| A paper to cite that we don't hold | `put(kind='paper', doi=…)` + waiting todo | a stub the fetcher chases |

A literature-review sentence typically does all of this over its life:
stub the missing source, wait, ground a hub on its passage, check for
disputes, cite `[fi<id>]`.

## See also

- [[precis-citation-help]] — the four-step cite procedure (search hubs → ground + mint → adversarial → cite).
- [[precis-read-for-question]] — you have a *question*, not a sentence to cite: the reading loop that leaves hubs behind.
- [[precis-taproot-mint-help]] — admissibility, scope, notation, search-before-mint.
- [[precis-taproot-hub-edit-help]] — attach, reword, merge an existing hub.
- [[precis-check-source-help]] — find the chunk, read its surrounds, judge support.
- [[precis-finding-help]] — chase a claim to its primary source.
- [[precis-stubs-help]] — request a paper we don't hold.
- [[precis-auto-todo-help]] — a todo that waits for the paper to appear.
- [[precis-paper-help]] — find/read papers; `pa<id>`/`pc<id>` handles; bibtex/ris views.
- [[precis-write-paper-help]] — claim-level citation density discipline.
- [[precis-bibliography-help]] — read side: who cites this paper.

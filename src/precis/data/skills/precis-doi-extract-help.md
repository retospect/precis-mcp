---
id: precis-doi-extract-help
title: precis — extract DOIs from research output, queue paper stubs
summary: LLM-extract DOIs and arXiv IDs from perplexity/search results, mint kind=paper stubs for the fetch_oa worker
answers:
  - how do I pull DOIs or arXiv ids out of a Perplexity/search result and mint stubs?
  - why does DOI extraction use an LLM instead of a regex?
  - when should I skip DOI extraction entirely?
applies-to: planner step after perplexity-research / web search; put(kind='paper', identifier=/doi=/arxiv=/title=)
status: active
---

# precis-doi-extract-help — turn research output into paper stubs

The gather phase produces text — `perplexity-research` bodies,
`perplexity-reasoning` notes, lexical/semantic search results, your
own draft text — that names papers without necessarily having them in
the corpus. This skill is the bridge: read that text, identify which
papers are being cited, queue any that aren't already in the corpus
for fetch.

## The pattern in one step

```python
# After a gather tick that produced perplexity-research notes:
notes = get(kind="perplexity-research", id="<id>")

# You (the planner) read the notes and identify cited papers — DOIs,
# arXiv IDs, paper titles with authors. For each one not yet in
# corpus, put() a stub so the fetch_oa worker enriches it:
put(
    kind="paper",
    identifier="10.1038/nature10352",
    title="Graphene field-effect transistors",
    reason="cited by perplexity-research:847 on CNT mobility",
    context_ref_id=847,
)
```

This is `put(kind='paper', ...)` — the real MCP verb, not a special
tool. `PaperHandler.put` is a thin adapter over the handler's own
`acquire()` method (`identifier=`/`doi=`/`arxiv=` all fold to the
same call); `doi=`/`arxiv=` are shorthand for `identifier=` — either
of these works too:

```python
put(kind="paper", doi="10.1038/nature10352")
put(kind="paper", arxiv="2401.00001", title="…")
```

`put(kind='paper', ...)` accepts:

- `identifier=` — `'doi:10.1038/...'`, `'arxiv:2401.00001'`,
  `'s2:<id>'`, `'pubmed:<id>'`, or bare DOI / arXiv ID (`'10.…'` and
  `'2401.…'` are inferred).
- `title=` — best-known title string. Used when no identifier resolves
  to a stub the worker can fetch; the operator can still recognise it
  in the "Papers we need" tab and upload by hand.
- `reason=` — one-line "why we want this" — appears in the stub's
  meta and helps the operator decide whether the auto-fetch is worth
  paywall-paying for.
- `context_ref_id=` — the ref id of the surfacing source (the
  perplexity-research / search result / draft tex chunk). Lets a
  later reviewer chase back to where the paper was first wanted.
- `verify=` — default `True`. Identifier is resolved via Semantic
  Scholar at mint time; if the lookup returns no metadata, the call
  rejects with a `BadInput` (so a hallucinated DOI never lands on
  the backlog). Pass `verify=False` for known-real preprints S2
  hasn't indexed yet. If you pass `title=` alongside an unresolvable
  identifier, the stub mints with the title and is tagged
  `acquire:unverified` so the operator can re-check it.

The stub mints with `pdf_sha256 IS NULL` and one or more
`ref_identifiers` rows. The `precis worker --only fetch` pass
(running continuously on every system worker) picks it up, cascades
through Unpaywall → arXiv → Semantic Scholar / CrossRef, and either
(a) fetches the PDF + ingests text chunks, or (b) leaves the stub
chunkless if every fetch path failed (auth wall, retraction, broken
DOI). Chunkless stubs surface on the **Papers we need** web tab so the
operator can upload the PDF manually.

## Why LLM extraction, not regex

The text you're reading has noisy citation forms — bracketed
`[12]` markers, parenthetical (Smith et al., 2020), inline arXiv:NNNN
strings, full URLs, and titles without identifiers. A regex catches
some but misses many; the LLM reads sentence context, knows
"Javey et al. 2003 Nature 424:654" is one paper, and emits the
right `identifier=` / `title=` to `put(kind='paper', …)`. Default to
that.

## When to call vs when to skip

- **Mint a stub** when you encounter a paper that supports a
  quantitative claim, a method choice, a counterargument, or a
  citation you intend to write. The fetch pass is cheap and
  idempotent (a stub minted again is a no-op via DOI alias matching).
- **Skip it** for review papers / textbooks named only as
  general background and not cited specifically. The corpus grows
  with what you'll actually need to verify.

## Verify the corpus before re-acquiring

```python
# Cheap check before minting a stub — DOI / arXiv both resolve in get():
try:
    get(kind='paper', id='10.1038/nature10352')
    # Already in corpus; skip put.
except NotFound:
    put(kind='paper', identifier='10.1038/nature10352', reason='...')
```

`put(kind='paper', …)` also dedups via DOI aliases, but the explicit
`get` short-circuit avoids the fetch_oa pass touching a stub it'll just
collapse into the existing ref.

## See also

```python
get(kind="skill", id="precis-paper-help")  # corpus read / search side
get(kind="skill", id="precis-citation-help")  # using fetched papers as cite sources
get(
    kind="skill", id="precis-draft-help"
)  # cite a paper inline by its [pc<id>] chunk handle
```

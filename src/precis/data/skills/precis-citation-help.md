---
id: precis-citation-help
title: precis — verified claim → source pointer
summary: kind='citation' verifier workflow — claim, source quote, confidence, verification loop
applies-to: put (kind='citation'), get (kind='citation')
status: active
---

# precis-citation-help — write verified citations

A `citation` is a verified claim paired with the verbatim source
quote that supports it. This is the **write side** of cite-a-passage:
draft the claim, have a verifier subagent confirm the quote precisely
supports it, then persist the pair so future readers can chase the
exact source span.

## Write a citation once a verifier has confirmed the quote
## Persist a verified claim → source pair
## I have a claim and a verbatim quote — store the citation

```python
put(kind='citation',
    text='MOF X improves CO2 reduction by 12%',          # the claim
    source_handle='collins06~7',                          # the chunk
    source_quote='we observed 12% Faradaic efficiency '   # verbatim
                 'for CO2 reduction at -0.3 V vs RHE',
    verifier_confidence=0.95,
    link='paper:collins06',
    rel='cites')
# → created citation id=42
```

`verifier_confidence` is a float in `[0.0, 1.0]` — use any precise
value the verifier emits, or stick to conventions (0.95 strong,
0.8 moderate, 0.5 weak). The `link='paper:<slug>'` + `rel='cites'`
makes the source paper findable via the graph and surfaces
"who cites me?" on the paper itself.

Citations are write-once. Re-verification of the same claim against
a different quote creates a new citation; the old one stays as the
audit trail.

## Read back a citation by id
## Fetch a stored citation
## Show me citation 42

```python
get(kind='citation', id=42)
get(kind='citation', id='citation:42')   # link-target form, equivalent
```

```text
# citation 42
_MOF X improves CO2 reduction by 12%_

source: `collins06~7`
quote: "we observed 12% Faradaic efficiency for CO2 reduction at -0.3 V vs RHE"
verifier_confidence: 0.95
verified_at: 2026-05-31T14:23:00Z
```

## Browse recent or matching citations

```python
search(kind='citation', q='MOF CO2 reduction')
get(kind='citation', id='/recent')
```

## When writing LaTeX, the citation bridges to `\citequote`

The workspace's `main.tex` preamble defines a verbatim-quote citation
macro (see precis-tex-help). When you emit `\citequote{key}{quote}`
in a `.tex` body, use **exactly** the `source_quote` you persisted on
`kind='citation'` — same string, same length, no paraphrase. The
review-pass citation-faithfulness verifier reads the macro's second
argument and confirms it appears in the cited paper's chunks; if you
paraphrase between persist and render, the verifier flags the drift.

```latex
% In tex body — uses the persisted source_quote verbatim:
Aqueous synthesis yields higher quantum yields than hot-injection
\citequote{collins06}{we observed 12% Faradaic efficiency for CO2
reduction at -0.3 V vs RHE}.
```

Bare `\cite{key}` in body text is a lint failure during the
citation-faithfulness review — it strips the audit trail the
verifier needs.

## Discipline that protects the audit trail

**Store the source quote verbatim.** No paraphrase, no cleanup, no
normalization. The point of the record is that a future reviewer
can chase to the exact source span; cleanup destroys that.

**Triage excerpts are not citation-grade.** The `excerpt @ ~N:`
sub-lines in search results are picked for triage — they help you
decide whether to drill in. Fetch the full chunk with
`get(kind='paper', id='<slug>~N')` and run the verifier against
that before persisting.

**Numeric claims need numeric matches.** A "12%" claim demands "12%"
(or "twelve percent") in the quote. A claim of 12% FE supported by
"an improvement was noted" is a hallucination — reject and look
elsewhere.

## See also

```python
get(kind='skill', id='precis-finding-help')    # chase side: claim → primary source via citation chain
get(kind='skill', id='precis-search-help')     # find the chunk to verify against
get(kind='skill', id='precis-paper-help')      # fetch chunks; ~N grammar
get(kind='skill', id='precis-link-help')       # cites and other graph relations
get(kind='skill', id='precis-overview')        # verbs and kinds
```

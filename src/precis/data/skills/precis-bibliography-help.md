---
id: precis-bibliography-help
title: precis — read citations that cite a paper
applies-to: get (kind='paper', view='bibliography')
status: active
---

# precis-bibliography-help — read citations that cite a paper

The bibliography view lists every verified `citation` that points at
a given paper. Read-side counterpart to the verifier workflow in
`precis-citation-help`.

## See which claims have been verified against a paper
## Read the citations that cite a paper
## Who has cited this paper, and for what claim?

```python
get(kind='paper', id='collins06', view='bibliography')
```

```text
# collins06 bibliography — 3 citations

{id	claim	source	conf	quote}
citation:42	MOF X achieves 12% FE for CO2 reduction	collins06~7	0.95	"we observed 12% Faradaic efficiency for CO2 reduction at -0.3 V"
citation:51	Cu-MOF synthesis yields above 85% in solvothermal	collins06~3	0.91	"Yields exceeded 85 percent in all batches"
citation:67	Operating window: −0.3 to −0.5 V vs RHE	collins06~14	0.88	"the device sustained −0.3 V vs RHE for 200 h"
```

One row per citation, oldest first. Empty for a paper nothing has
cited yet — that's the normal state for a freshly-ingested paper.

## What each column means

- `id` — `citation:<N>` handle. Paste into `get(kind='citation', id=<N>)`
  for the full record (verifier caveats, verified-at timestamp).
- `claim` — the persisted assertion, truncated to ~80 chars.
- `source` — the chunk handle the quote came from (`<slug>~N` or `<slug>~A..B`).
- `conf` — verifier confidence in `[0.0, 1.0]`; `?` if unset.
- `quote` — verbatim text from the source chunk, truncated to ~120 chars.
  Treat as verbatim — never paraphrase.

## Drill into one citation

```python
get(kind='citation', id=42)          # full record
get(kind='citation', id='citation:42')   # link-target form, equivalent
```

## Find citations across all papers

```python
search(kind='citation', q='CO2 reduction Faradaic efficiency')
get(kind='citation', id='/recent')
```

The bibliography view is scoped to one paper. To browse citations
across the corpus, query the `citation` kind directly.

## Bibliography vs short-form citation

The bibliography lists *claims that cite this paper*. To cite the
paper itself in a manuscript, fetch a short-form entry:

```python
get(kind='paper', id='<slug>', view='bibtex')
get(kind='paper', id='<slug>', view='ris')
```

## Re-verification appears as a new row

Citations are write-once. A second verification of the same claim
against a different quote creates a fresh `citation` row; both
appear in the bibliography so the audit trail survives.

## See also

```python
get(kind='skill', id='precis-citation-help')   # write-side: verifier loop
get(kind='skill', id='precis-paper-help')      # paper views, slug grammar, short-form cite
get(kind='skill', id='precis-search-help')     # excerpt vs citation distinction
get(kind='skill', id='precis-link-help')       # the cites relation in the graph
```

---
id: precis-bibliography-help
title: precis — read citations that cite a paper
summary: read side of citations — list verified claims pointing at a paper with quotes and confidence
applies-to: get (kind='paper', view='bibliography')
status: active
---

# precis-bibliography-help — read citations that cite a paper

The bibliography view lists everything that cites a given paper: the
verified `citation` records pointing at it **and** the drafts that
cite it inline. Read-side counterpart to authoring citations
(`precis-draft-help`).

When a draft cites a paper inline by writing a bare paper-chunk handle
`[pc<id>]`, that handle resolves to its paper and materialises a
`cites` graph edge from the draft to the paper. So drafts now surface
here in "who cites this paper" alongside `citation` records —
citations are to the literature, and a draft pointing at a paper chunk
is exactly that. (A draft pointing at a memory or another draft via
`[me<id>]`/`[dc<id>]` is a `related-to` **link**, not a citation, and
never appears in a bibliography.)

## See which claims have been verified against a paper
## Read the citations that cite a paper
## Who has cited this paper, and for what claim?

Address the paper by its `pa<id>` handle (the slug still resolves as a
legacy form):

```python
get(kind='paper', id='pa312', view='bibliography')      # pa<id> handle
get(kind='paper', id='collins06', view='bibliography')  # slug (legacy)
```

```text
# collins06 bibliography — 3 citations

{id	claim	source	conf	quote}
ci42	MOF X achieves 12% FE for CO2 reduction	pc7	0.95	"we observed 12% Faradaic efficiency for CO2 reduction at -0.3 V"
ci51	Cu-MOF synthesis yields above 85% in solvothermal	pc3	0.91	"Yields exceeded 85 percent in all batches"
ci67	Operating window: −0.3 to −0.5 V vs RHE	pc14	0.88	"the device sustained −0.3 V vs RHE for 200 h"
```

One row per citation, oldest first. Empty for a paper nothing has
cited yet — that's the normal state for a freshly-ingested paper.

## What each column means

- `id` — `ci<N>` handle. Paste into `get(id='ci<N>')` (or
  `get(kind='citation', id=<N>)`) for the full record (verifier caveats,
  verified-at timestamp).
- `claim` — the persisted assertion, truncated to ~80 chars.
- `source` — the chunk handle the quote came from (`pc<chunk_id>`; a range
  still renders as `<slug>~A..B`).
- `conf` — verifier confidence in `[0.0, 1.0]`; `?` if unset.
- `quote` — verbatim text from the source chunk, truncated to ~120 chars.
  Treat as verbatim — never paraphrase.

## Drill into one citation

```python
get(id='ci42')                       # full record, by handle
get(kind='citation', id=42)          # equivalent
get(kind='citation', id='citation:42')   # legacy link-target form, equivalent
```

## Find citations across all papers

```python
search(kind='citation', q='CO2 reduction Faradaic efficiency')
get(kind='citation', id='/recent')
```

The bibliography view is scoped to one paper. To browse citations
across the corpus, query the `citation` kind directly.

## Bibliography vs short-form citation

The bibliography lists *what cites this paper*. To cite the paper
itself in a draft you write a bare paper-chunk handle `[pc<id>]`
inline (see `precis-draft-help`) — you never hand-author a BibTeX key;
the export engine renders `\cite{}` + one bibliography entry per paper
at compile time. To fetch a short-form entry for external use:

```python
get(kind='paper', id='pa312', view='bibtex')
get(kind='paper', id='pa312', view='ris')
```

## Re-verification appears as a new row

Citations are write-once. A second verification of the same claim
against a different quote creates a fresh `citation` row; both
appear in the bibliography so the audit trail survives.

## See also

```python
get(kind='skill', id='precis-draft-help')      # write-side: inline [pc<id>] citations in a draft
get(kind='skill', id='precis-citation-help')   # the citation kind + verifier loop
get(kind='skill', id='precis-paper-help')      # paper views, pa<id> handle, short-form cite
get(kind='skill', id='precis-link-help')       # the cites relation in the graph
```

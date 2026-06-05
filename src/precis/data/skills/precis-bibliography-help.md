---
id: precis-bibliography-help
title: precis — citations referencing a paper
status: shipped
tier: 1
floor: any
applies-to: get (kind='paper', view='bibliography')
last-updated: 2026-05-31
---

# precis-bibliography-help — read citations that cite a paper

The **bibliography view** assembles every verified `citation` ref
that points at a given paper, served via the `cites` link
relation. It is the read-side of the citation workflow — the
write side is `precis-citation-help`.

This view tells you: "for paper X in the store, which claims have
been verified against it, by whom, with what confidence?" Use it
when you're returning to a paper you've cited before, when you're
auditing a claim someone else filed, or when you want to see
whether a paper has been cited at all.

## Ask

```python
get(kind='paper', id='collins06', view='bibliography')
```

Output is a TOON table — one row per citation, ordered by the
`links.created_at` of the underlying `cites` edge (oldest first
so the audit log reads chronologically).

```
# collins06 bibliography — 3 citations

{id	claim	source	conf	quote}
citation:42	MOF X achieves 12% FE for CO2 reduction	collins06~7	0.95	"we observed 12% Faradaic efficiency for CO2 reduction at -0.3 V"
citation:51	Cu-MOF synthesis yields above 85% in solvothermal	collins06~3	0.91	"Yields exceeded 85 percent in all batches"
citation:67	Operating window: −0.3 to −0.5 V vs RHE	collins06~14	0.88	"the device sustained −0.3 V vs RHE for 200 h"

Next:
  get(kind='citation', id=<N>)                — read one citation's full record
  get(kind='skill', id='precis-citation-help') — the verifier-workflow agent surface
```

The bibliography is **empty** when no citations exist yet — that's
the normal state for a freshly-ingested paper. The empty response
points at the TOC view + the citation help skill so you know how
to make claims appear here.

## Columns

- **id** — `citation:<N>` handle. Paste it into
  `get(kind='citation', id=<N>)` for the full record including
  the verifier's caveats and the verified-at timestamp.
- **claim** — the assertion the writing thread persisted, truncated
  to ~80 chars for the table. Full claim in the citation record.
- **source** — the source chunk handle the verifier confirmed
  the quote came from, e.g. `collins06~7` or `collins06~5..8`.
- **conf** — the verifier subagent's confidence (0..1). `?` when
  the citation was written without it.
- **quote** — the verbatim text from the source chunk that
  supports the claim, truncated to ~120 chars. **Always treated
  as verbatim** — do not paraphrase or clean it up.

## Discipline

**Excerpts in TOC and search are NOT in the bibliography.** The
indented `- excerpt @ ~N: "..."` sub-lines you see in search
results and TOC views are *triage* sentences picked by cosine
similarity. They are not citations until the verifier subagent has
read the verbatim chunk and confirmed the quote precisely supports
the claim. The bibliography view only shows verified citations.

**Re-verification creates a new citation.** Citations are
write-once. If a future read of `collins06~7` finds a better
quote, the workflow files a fresh `citation` and links it via
`cites` to the same paper. The bibliography lists both records as
distinct rows so the audit trail is preserved.

**Cross-paper citations are not aggregated here.** This view shows
citations *of this paper*. To see all citations a specific writing
session produced, search the `citation` kind directly:
`search(kind='citation', q='your topic')` or
`get(kind='citation', id='/recent')`.

## See also

- `precis-citation-help` — write-side workflow (verifier loop)
- `precis-paper-help § Cite` — short-form (BibTeX/RIS) for the
  paper itself, not for claims that cite it
- `precis-link-help` — the `cites` relation in the graph
- `precis-search-help` — excerpt-vs-citation distinction

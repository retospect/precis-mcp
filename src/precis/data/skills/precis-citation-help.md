---
id: precis-citation-help
title: precis — verified claim → source pointer
status: shipped
tier: 1
floor: any
applies-to: put (kind='citation'), get (kind='citation')
last-updated: 2026-05-31
---

# precis-citation-help — write verified citations

A `citation` is a **verified claim → source quote** record. The
writing-thread workflow drafts a claim, a verifier subagent reads
the candidate source chunk and confirms the verbatim quote
precisely supports the claim, and the result lands here as a
durable, queryable bibliography entry.

This skill is for **agents writing papers / reports / answers**
who need to track which exact source sentence backs each claim
they make.

## The workflow shape

```
1. agent drafts claim          : "MOF X improves CO2 reduction by 12%"
2. agent searches               : search(kind='paper', q='MOF CO2 reduction')
3. agent reads top hit          : get(kind='paper', id='collins06~7')
4. verifier subagent checks     : "does this chunk precisely support
                                    the claim?" → yes, quote spans
                                    char 142..210, confidence 0.95
5. agent persists the citation  : put(kind='citation', text=..., ...)
6. bibliography assembles       : citations referencing the paper
                                    are surfaced via the cites link
```

The verifier subagent is **client-side** — this kind only owns
the storage door. Reads are honest: the stored record tells you
exactly where the quote came from and what the verifier said.

## Create

```python
put(
    kind='citation',
    text='MOF X improves CO2 reduction by 12%',          # the claim
    source_handle='collins06~7',                          # the chunk
    source_quote='we observed 12% Faradaic efficiency '   # verbatim
                 'for CO2 reduction at -0.3 V vs RHE',
    char_offset=142,                                      # span start
    verifier_confidence=0.95,                             # 0..1
    verifier_caveats=None,                                # optional
    link='paper:collins06',                               # source paper
    rel='cites',                                          # default rel
)
# → created citation id=N
#   source: collins06~7
#   verifier_confidence: 0.95
#   verified_at: 2026-05-31T...
```

Required: `text`, `source_handle`, `source_quote`. Recommended:
`verifier_confidence`, `link='paper:<slug>'` so the cites link
makes the source paper findable via the graph (and `links_for`
on the paper surfaces "who cites me?" for free).

**Citations are write-once.** Re-verification — same claim
revisited later, possibly with a different quote — creates a
new citation. The old one stays as the audit trail.

## Read

```python
get(kind='citation', id=42)
# → # citation 42
#   _MOF X improves CO2 reduction by 12%_
#
#   source: `collins06~7`
#   quote: "we observed 12% Faradaic efficiency ..."
#   char_offset: 142
#   verifier_confidence: 0.95
#   verified_at: 2026-05-31T14:23:00Z
```

```python
get(kind='citation', id='/recent')        # last N created
search(kind='citation', q='MOF synthesis') # lexical over claims
```

## Discipline

**Source quote is sacred.** Always store the verbatim text from
the chunk — do not paraphrase, clean up, or normalize. The whole
point of the record is that a future reviewer (or future-you)
can chase the citation to the exact source span. Cleanup destroys
the audit trail.

**Excerpts in search results are NOT citation-grade.** The
`- excerpt @ ~N:` lines in search-result rows (see
`precis-search-help`) are *triage* sentences picked by cosine
similarity — they help you decide whether to drill in. They are
not verified support for any claim. Always fetch the verbatim
chunk and run the verifier before writing the citation.

**Numeric claims need numeric matches.** If the claim says
"improves by 12%", the source_quote must say "12%" (or a clearly-
equivalent form: "twelve percent"). A claim of 12 % FE supported
by a quote saying "an improvement was noted" is not a citation —
it is a hallucination. Reject and look elsewhere.

## See also

- `precis-search-help` — discovery layer, query-aligned excerpts
- `precis-paper-help` — chunk-handle grammar (~N, ~A..B)
- `precis-link-help` — graph relations including `cites`
- `precis-overview` — kind-list and address grammar

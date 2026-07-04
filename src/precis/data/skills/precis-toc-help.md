---
id: precis-toc-help
title: precis — table of contents for any TOC-capable kind
summary: TOC views — drillable handles for long documents, keyword-labelled ranges, reading entry point
applies-to: get(view='toc'), slug~N / slug~A..B / slug/toc
status: active
---

# precis-toc-help — table of contents for any TOC-capable kind

The TOC is the reading entry point for anything longer than a snippet.
It returns a table of drillable handles, each labelled with the
keywords that distinguish its range.

## Ask for the TOC of something
## Open the table of contents
## How do I see what's inside a paper or skill?

```python
get(kind='paper', id='pa5', view='toc')          # kwarg form (handle; legacy slug still resolves)
get(kind='paper', id='pa5/toc')                  # path form, equivalent
get(kind='skill', id='precis-overview/toc')      # same on skills
```

Path form and kwarg form are interchangeable. Pick whichever reads
better. Both work on every TOC-capable kind (today: `paper`, `skill`).

## What the TOC looks like
## Read a TOC table
## What do the columns mean?

```text
# pa5 TOC — 142 chunks, 8 clusters

{handle	keywords}
pa5~0..14	keyword phrases for this range
pa5~15..29	keyword phrases for the next range
…
```

Each row is a `<handle>~A..B` handle, where `<handle>` is the record's
universal handle (`pa<id>` for a paper). Paste any row handle straight
back as `id=` to read that range — it round-trips verbatim. (A cite-key
slug, `vaswani17~0..14`, also resolves on input, so older copy-pastes
still work.) Keywords are most-distinctive first — `keywords[0]` is
what makes the range unique, not what the whole document is about.

A short *top-level* TOC (under a page or so of chunks) renders one row
per chunk — its keywords as the label — because there's nothing to
cluster. Drilling is different: see below.

## Drill into a section
## Read a TOC row
## I picked a row — now what?

```python
get(kind='paper', id='pa5~15..29')              # read the range
get(kind='paper', id='pa5~15..29', view='toc')  # sub-TOC of the range
get(kind='paper', id='pa5~15..29/toc')          # path form
```

Sub-TOC re-clusters the chosen range into its own sub-groups —
*regardless of how few chunks it holds*, unlike the top-level TOC
which only clusters a long body. Each sub-group is itself a
`~A..B` handle you can drill again, so recursing walks a hierarchy
(papers have no heading tree; the hierarchy *is* this recursive
keyword clustering). It stops sub-grouping only when a range is small
enough to be a leaf — then it shows those chunks directly. So you never
hit a wall of undrillable one-row-per-chunk singletons on the way down.

## See also

```python
get(kind='skill', id='precis-paper-help')      # paper-specific TOC + drill-in
get(kind='skill', id='precis-overview')        # address grammar (slug~N, /toc)
get(kind='skill', id='precis-search-help')     # search returns pc<id> chunk handles
get(kind='skill', id='precis-toon')            # the table wire format
```

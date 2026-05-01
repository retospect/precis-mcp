---
id: precis-paper-help
title: precis — find, read, cite papers
status: phase-7
tier: 1
floor: any
applies-to: get/search (kind='paper')
last-updated: 2026-04-28
---

# precis-paper-help — find, read, cite papers

Papers are slug-addressed (`abazari2024design`,
`kim2024electrocatalytic`). Slugs are deterministic on author-surname
+ year + first content word of the title; collisions get a `-2`/`-3`
suffix. Use `get(kind='paper')` (no id) to see what's actually
ingested in this build before guessing slugs.

## Find

```python
search(kind='paper', q='photocatalytic NOx reduction')
search(kind='paper', q='photocatalytic NOx reduction', top_k=20)
get(kind='paper')                              # list all papers (page of 50)
```

Block-level hybrid search (lexical tsvector + semantic pgvector, RRF
fused). Returns hits as `<slug>~<pos>` with the matching block
excerpt, ordered best-first. The fused RRF score is **rank-based**
(it doesn't reflect query strength on its own scale), so we don't
surface a misleading numeric — list position is the only honest
relevance signal.

Scope to one paper:

```python
search(kind='paper', q='Z-scheme', scope='abazari2024design')
```

## Read

```python
get(kind='paper', id='abazari2024design')                  # overview
get(kind='paper', id='abazari2024design', view='abstract') # abstract only
get(kind='paper', id='abazari2024design', view='toc')      # hierarchical TOC
get(kind='paper', id='abazari2024design~38')               # block 38
get(kind='paper', id='abazari2024design~38..42')           # block range
```

The id syntax also supports view paths — the kwarg `view=` and the
path `id='slug/<view>'` accept the **same vocabulary** so you can
reach any view either way:

```python
get(kind='paper', id='abazari2024design/abstract')
get(kind='paper', id='abazari2024design/toc')
get(kind='paper', id='abazari2024design/cite/bib')
get(kind='paper', id='abazari2024design', view='cite/bib')   # equivalent
get(kind='paper', id='abazari2024design', view='bibtex')     # also equivalent
```

Supported views: `abstract`, `toc`, `bibtex` (alias `cite/bib`),
`ris` (alias `cite/ris`), `endnote` (alias `cite/endnote`).

## Navigate

The `view='toc'` output is **hierarchical** — section/subsection
ranges are detected from heading patterns and laid out as a jump
table:

```
# acheson2026automated — TOC (177 blocks, 20 sections)

  ~0..7     (8)   <untitled>  (preview from first block)
  ~8..20    (13)  ■ INTRODUCTION
  ~21..40   (20)  ■ THEORY
  ~41..73   (33)  ■ METHODS
    ~43..53   (11)  Physics-Informed Program Synthesis [PIPS]
    ~54..58   (5)   Calculation Details
    ~59..63   (5)   Heterodiatomic Molecules
    ~64..73   (10)  Alkanes
  ~74..116  (43)  ■ RESULTS & DISCUSSION
    …
```

To **drill into a section**, use the combined chunk-range + view path
form:

```python
get(kind='paper', id='abazari2024design~74..116/toc')   # TOC of just this range
get(kind='paper', id='abazari2024design~74..116')       # read this range
```

Each response ends with a column-aligned "Next:" block that suggests
the next likely call (next/previous range, parent TOC, citation, …)
so the agent can keep navigating without re-reading the help.

## Cite

```python
get(kind='paper', id='abazari2024design', view='bibtex')
get(kind='paper', id='abazari2024design', view='ris')
get(kind='paper', id='abazari2024design', view='endnote')
```

Abstracts are stripped of `<jats:*>` namespace tags before render,
so the body is clean markdown safe to quote.

## Figures

**Figure binaries are not served.** The pipeline keeps a
markdown image marker (`![](_page_N_Figure_M.jpeg)`) on the figure's
own block and the legend on the next block; the image file lives
on disk inside the `.acatome` bundle but is not exposed by `get`.

To cite a figure, name it by paper slug + figure number — e.g.
*"Figure 3 of `abazari2024design`"* — and quote the legend text.
Do **not** invent image URLs; the image marker in the block body
is a relative path that nothing serves.

```python
# Read figure 3's legend block (find it via search or /toc).
get(kind='paper', id='abazari2024design~45')
# 'Figure 3. Schematic representation of the structure of NU-1000…'
```

A future view (`view='fig/<N>'`) will return both the legend and a
resolvable image URL once the bundle's image directory is wired
into the cluster's static-file server. Until then, treat figures
as caption-only.

## Tag and cross-link a paper

Paper bodies are import-only — you can't rewrite a paper's text from
`put`. Tag and link operations work today, however:

```python
# Tag a paper. Closed-prefix axes for paper are SRC: and CACHE:; open
# tags (topic-x, etc.) are always allowed.
tag(kind='paper', id='abazari2024design', add=['topic:photocatalysis'])
tag(kind='paper', id='abazari2024design', add=['SRC:primary'])

# Drop tags. STATUS:/PRIO: aren't on paper's allowed-axis list and
# raise BadInput at validation; that's by design.
tag(kind='paper', id='abazari2024design', remove=['topic:photocatalysis'])

# Cross-cite another paper.
link(kind='paper', id='abazari2024design',
     target='paper:other-slug', rel='cites')
```

Chunk selectors (`~N`, `~A..B`) and view paths (`/toc`, `/cite/bib`)
are rejected here — link/tag operates at the ref level. See
`precis-relations` for the relation vocabulary and `precis-tags` for
the closed-prefix axes.

## Not yet

- `put(kind='paper', text=...)` — body mutation. Bodies arrive via
  `.acatome` bundle ingest; there's no API to overwrite them.
- `get(id='doi:10.1002/...')` / `get(id='arxiv:2207.09327')` — URI
  scheme prefixes. Resolution lives in `acatome-quest-mcp` for now;
  inside precis you address by slug.
- `view='representatives'`, `view='methods'`, etc. — semantic section
  views.

## See also

- `precis-overview` — verbs and kinds
- `precis-relations` — `related-to`, `contradicts` between papers
- `precis-tags` — `topic:`, `SRC:`, `DENSITY:*`
- `precis-memory-help` — capturing thoughts from a paper

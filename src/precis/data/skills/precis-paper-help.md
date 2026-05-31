---
id: precis-paper-help
title: precis — find, read, cite papers
status: phase-7
tier: 1
floor: any
applies-to: get/search/tag/link (kind='paper')
last-updated: 2026-05-02
---

# precis-paper-help — find, read, cite papers

Papers are slug-addressed (`abazari2024design`,
`kim2024electrocatalytic`). Slugs are deterministic on author-surname
+ year + first content word of the title; collisions get a `-2`/`-3`
suffix. Use `get(kind='paper')` (no id) to see what's actually
ingested in this build before guessing slugs.

**You can also address papers by bare DOI** — `get` and `search`
both transparently resolve a DOI to its slug before any other
lookup. If you have a DOI in hand (from a citation, a reading
list, a `\todo{cite: 10.xxxx/...}` marker), pass it directly
instead of trying to guess the slug or running keyword searches:

```python
get(kind='paper', id='10.1038/nature10352')           # by DOI
get(kind='paper', id='10.1038/nature10352~38')        # DOI + chunk selector
search(kind='paper', q='10.1038/nature10352')         # check if ingested
```

DOI form does **not** support view paths (`/abstract`, `/toc`) —
DOI suffixes can legally contain `/`, so the parser can't tell
"DOI literal" from "DOI + view". Use the `view=` kwarg alongside
a DOI: `get(kind='paper', id='10.1038/nature10352', view='toc')`.

When a DOI lookup misses, the error response points you at the
sortie's `request_doi.md` queue (perplexity / fetch pipeline)
rather than burning time on keyword searches that will also miss.

## Find

```python
search(kind='paper', q='photocatalytic NOx reduction')
search(kind='paper', q='photocatalytic NOx reduction', top_k=20)
search(kind='paper', q='10.1038/nature10352')  # check if a DOI is ingested
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

## Skip what you've seen (`exclude=`)

To paginate ("show me hits 6-N"), pass back the slugs of the papers
you already saw:

```python
# First page.
search(kind='paper', q='photocatalytic NOx reduction', top_k=5)
# → 5 of 47 paper blocks for 'photocatalytic NOx reduction'
#   ## 1. wang2020state~12 ...
#   ## 2. kim2024electro~7  ...
#   ...

# Next page — drop those 5, get the next 5.
search(kind='paper', q='photocatalytic NOx reduction', top_k=5,
       exclude=['wang2020state','kim2024electro','liu2022zscheme',
                'park2023nitrate','choi2021hybrid'])
# → 5 of 42 paper blocks for 'photocatalytic NOx reduction'
```

Notes:

- **Coarse / ref-level.** `exclude=['wang2020state']` drops every
  block of that paper. Selectors and view paths are stripped, so a
  copy-pasted hit handle (`'wang2020~12'`) and a DOI
  (`'10.1111/jnc.13915'`) both resolve to the bare slug.
- **The `LIMIT` applies after exclusion** — `top_k=5` with five
  excluded papers really does return five new hits, not zero. Same
  for the `N of K` header: it reports the *remaining* universe,
  not the global count.
- **Stale slugs are silent no-ops.** Unknown / soft-deleted slugs
  in the exclude list don't fail the call; the filter just skips
  them.
- **The `Next:` trailer pre-fills the continuation list for you.**
  When `total > len(hits)`, the response ends with a copy-pasteable
  `search(... exclude=[...])` line that already merges your prior
  exclude list with the slugs of refs returned this page — paste
  and go.

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

The `view='toc'` output is a **TOON table** (one row per segment)
with RAKE-extracted keywords per row. When the paper has explicit
H1/H2 headings, those drive the segmentation; when it doesn't, the
TOC falls back to **embedding-sequence clustering** (TextTiling-style:
adjacent-chunk cosine drops mark topic boundaries) so even
un-sectioned papers get 3–9 navigable segments.

```
# cai23 — TOC (104 chunks, 5 segments via embedding clustering)

{handle           keywords}
cai23~0..14       lithium-mediated nitrogen reduction, MEA design, PEO membrane, …
cai23~15..38      BF4 salt doping, lithium salt selection, ion transport, …
cai23~39..62      XPS characterization, ToF-SIMS depth profile, surface composition, …
cai23~63..89      LiF formation, BO species, F 1s spectrum, …
cai23~90..103     performance summary, scalability, comparison with literature, …

Abbrevs: MEA (Membrane Electrode Assembly), XPS (X-ray Photoelectron Spectroscopy), …
Shared across segments: lithium-mediated nitrogen reduction
```

When H2 sections are present and informative, the TOC switches to a
three-column shape with the heading column populated from the paper's
own headings (the keywords column then only fills in for "stupid"
headings like `Methods` / `Results` that don't disambiguate content).

To **drill into a segment**, paste any handle from the table as
`id=`:

```python
get(kind='paper', id='cai23~63..89')              # read the LiF / surface-chemistry segment
get(kind='paper', id='cai23~63..89', view='toc')  # sub-TOC, recursively segments the range
```

Recursive segmentation works on any sub-range that has enough
chunks; below the K_MIN threshold the renderer just lists each
chunk as its own row.

Single-chunk drill-in adds a one-line `Part of segment ~A..B`
header so the agent always knows the surrounding neighbourhood.
Each response ends with a `Next:` block listing the most likely
follow-up calls (drill-in, sub-TOC of the top hit's cluster,
pagination, scope-narrow).

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
- `get(id='arxiv:2207.09327')` — URI scheme prefixes other than
  bare DOI. Resolution lives in `acatome-quest-mcp` for now.
  (Bare DOIs *are* supported here — see the top of this file.)

## See also

- `precis-overview` — verbs and kinds
- `precis-relations` — `related-to`, `contradicts` between papers
- `precis-tags` — `topic:`, `SRC:`
- `precis-memory-help` — capturing thoughts from a paper
- `precis-toc-help` — TOC machinery: smart segmentation, sub-range zoom, abbreviation legend

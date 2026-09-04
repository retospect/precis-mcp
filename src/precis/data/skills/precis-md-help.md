---
id: precis-md-help
title: precis — search workspace markdown (docs, backlog, skills prose)
summary: DB-free hybrid search + navigation over repo markdown — root/file/heading addressing, scope=, read-only
answers:
  - how do I search docs, backlog, or skills prose by topic?
  - how do I browse a markdown file's headings before reading it?
  - what does PRECIS_MD_ROOTS look like?
  - why does a search say "lexical only" or show a percentage?
  - how is this different from kind='markdown'?
applies-to: get/search (kind='md')
status: active
---

# precis-md-help — search workspace markdown

Hybrid lexical + semantic search over one or more configured
directory trees of `.md` files — docs, backlog, skills prose. Read
only; edit the files directly on disk.

## What does an md id look like?
## How do I address a root, a file, or a heading?

```python
get(kind="md")  # list configured roots
get(kind="md", id="docs")  # root overview: file/block counts, top dirs
get(kind="md", id="docs/backlog/some-item.md")  # file's heading outline
get(kind="md", id="docs/backlog/some-item.md", view="source")  # full file text
get(kind="md", id="docs/backlog/some-item.md~Motivation")  # one heading's section
```

`<alias>` comes from `PRECIS_MD_ROOTS`. `~<heading>` takes the
heading plus everything nested under it, up to the next heading of
equal or higher rank — match by exact slug or by title text
(case-insensitive).

## Search workspace prose by topic

```python
search(kind="md", q="how do I make search fresh after shipping")
search(kind="md", q="ship gate", scope="docs")  # restrict to one root
search(kind="md", q="ship gate", scope="docs/backlog")  # restrict to a subtree or file
```

`folder=` (the corpus-kind scoping kwarg) is **not supported here** —
a single-kind `search(kind='md', ...)` never enters the cross-kind
fan-out (only a comma-list or wildcard `kind=` does), so `folder=` is
silently swallowed by `md`'s handler, not an error. Use `scope=`
instead; it's `md`'s own root/subtree/file restriction, above.


Hits are ranked; each carries its heading breadcrumb and a
`<alias>/<file>~<slug>` address you paste into `get`. A search
headline may end with a coverage note:

```text
5 md hits for 'ship gate' (semantic: 62% of blocks indexed)
```

The percentage climbs on its own as the background index fills in —
lexical hits are always complete, semantic ranking widens over time.
`(lexical only: no embedder wired)` means this build has no embedder
configured; results are lexical-only, permanently, not a warm-up
state.

## How do I point precis at a set of docs?

```text
PRECIS_MD_ROOTS=docs:/abs/path/to/docs,backlog:/abs/path/to/backlog
```

Comma-separated `alias:/abs/path` pairs, same format as
`PRECIS_PYTHON_ROOTS`. Relative paths resolve against the server's
working directory.

## Read-only

`put` / `edit` / `delete` / `tag` / `link` aren't supported — edit
the files with your normal editor; the index picks up changes the
next time it's touched (per-file, so unrelated files stay cached).

## `md` vs `markdown` — which kind do I want?

`kind='markdown'` is the DB-backed, `PRECIS_ROOT`-sandboxed kind:
write-capable, ingested into the corpus, cross-kind-searchable.
`kind='md'` is a separate, read-only, DB-free index over
`PRECIS_MD_ROOTS` — for searching a codebase's own docs/backlog/skills
without ingesting them. Use `markdown` to author corpus notes; use
`md` to search a repo's prose in place.

## See also

```python
get(kind="skill", id="precis-python-help")  # sibling DB-free index, over code
get(kind="skill", id="precis-markdown-help")  # the DB-backed, writable markdown kind
get(kind="skill", id="precis-overview")  # verbs and kinds
```

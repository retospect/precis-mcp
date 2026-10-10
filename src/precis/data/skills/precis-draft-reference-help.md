---
id: precis-draft-reference-help
family: drafting
title: precis — the draft kind's quick reference (verbs, views, edit params, move/table/authors grammar)
summary: the lookup tables for kind='draft' — chunk addressing (dc<id>, windows), every get view, every edit param family, the move=/table=/authors= grammars and what put creates; the narrative and rulings stay in precis-draft-help
answers:
  - what views does get take on a draft, and what does each return?
  - which edit param do I use — text, find, move, table, sub, review, authors, title, scaffold?
  - what is the move= grammar for reordering or reparenting a draft chunk?
  - how do I edit one cell of a table chunk?
applies-to: get/put/edit (kind='draft')
status: active
tags: [drafting]
kinds: [draft]
---

# precis-draft-reference-help — the draft quick reference

The tables behind [[precis-draft-help]]: what each verb accepts on
`kind='draft'`. Workflow, examples and rulings live there; this file is
the lookup.

## Quick reference — verbs, views, edit params

**Addressing.** Every chunk has a stable handle `dc<id>` (e.g. `dc41`) —
globally unique, no draft name needed; the draft *record* is its slug or
`dr<id>`. Never guess/compute a handle — `put`/search/get return it.
Windows: `dc41-2..3` (2 before, 3 after), `dc41+1`, `dc41^` (parent). No
positional `~N` ordinals (they rot on insert). A window is a read
address only: `view=` (kwd…fisheye+1hop) targets one chunk, so pair it
with a bare `dc<id>`, never a `-B..A` window.

**`get` — views**

| call | returns |
|---|---|
| `get(id='<slug>')` / `view='outline'` | handle \| §-path \| gist, whole doc |
| `get(id='dc<id>')` | one chunk, verbatim |
| `get(id='dc<id>-B..A')` | that chunk + B before, A after |
| `get(id='dc<id>', view='fisheye')` | verbatim center + graduated neighborhood |
| `get(id='dc<id>', view='fisheye+1hop')` | fisheye + cited/cross-ref/note ring |
| `get(id=<scope>, view='toc')` | heading skeleton (whole draft, or one heading's subtree) |
| `get(id=<scope>, view='hygiene')` | undefined-abbrev + unresolved-citation lists, full, plus house-style counts |
| `get(id=<scope>, view='backfill')` | uncited-but-relevant papers, gap-finder |
| `get(id=<scope>, view='wordcount')` | per-section word counts vs targets |
| `get(id='dc<id>', view='history')` | the chunk's edit events, newest first, with prior text (`args={'limit':N}`, ≤500) |
| `get(id='dc<id>', view='proposals')` | open anchored todos with `meta.proposed_text`, as diffs vs the current text |
| `get(id='<slug>', view='links')` | the draft's link graph (cites/cross-refs/notes) |
| `get(kind='draft', project=<todo-id>)` | reverse lookup: that project's draft |

**`edit` — params** (`text=` rewrite is the default; one param family per
call)

| param | does |
|---|---|
| `text=` | whole-chunk rewrite |
| `find=` (+`text=`) | find-replace within the chunk (implies `mode='find-replace'`) |
| `dry_run=True`/`'full'` | preview a `text=`/`find=` edit, write nothing |
| `move=` | reorder/reparent — grammar below |
| `table=` / `cell=` / `find=`+`text=` / `sub=` | table-chunk cell edits — grammar below |
| `sub=` | regex substitute over a draft/section, dry-run by default, `apply=True` commits |
| `review=` | record a checker's sign-off (`'human'` or a worker name) |
| `authoring=` | `'on'`/`'off'` — let review personas author fixes inline |
| `authors=` | replace the byline — grammar below |
| `title=` | rename (both `refs.title` and the heading, atomically) |
| `scaffold=` | append a document class's section skeleton |
| `word_target=` | a heading's word budget `{'min':…,'max':…}`; `{}` clears |
| `style=` | stamp a heading with a section-style skill |
| `not_abbrev=` | silence the undefined-abbreviation hint for given tokens |
| `origin=` / `permission=` | a figure's provenance + clearance paper-trail |

Structural ops (`move`/`table`/`authors`) have no diff and reject
`dry_run`; `sub=` previews by default and commits on `apply=True`.

## Quick reference — move/table/authors grammar, put

**`move=` grammar**

| form | effect |
|---|---|
| `{'before'\|'after': 'dc<id>'}` | reorder among siblings |
| `{'parent': 'dc<id>', 'before'\|'after'\|'last': …}` | reparent |
| `{'into': 'dc<id>', 'last': True}` | append into a section |

```python
edit(id="dc16", move={"before": "dc15"})  # reorder among siblings
edit(id="dc17", move={"parent": "dc20", "after": "dc18"})  # move into another section
edit(id="dc19", move={"into": "dc20", "last": True})  # to a section's end
```

Moving a heading carries its whole subtree; no text changes, nothing
re-embeds.

**Table-chunk edit grammar** (`chunk_kind='table'`; plain `text=` is
rejected on a table chunk)

| call | effect |
|---|---|
| `table={'header':…, 'rows':…}` | whole grid, re-derives markdown |
| `cell='A1'` or `{'row':,'col':}` + `text=` | one cell (1-based; row 1 = header) |
| `find=` + `text=` | find-replace across string cells (literal) |
| `sub='s/a/b/'` | regex across cells, commits immediately, no dry-run |
| `caption=` / `regen=` | metadata only, data untouched |

**`authors=` grammar** — a list, each entry `{'name', 'affiliation'?,
'ror'?, 'orcid'?}` (or `{'family','given',…}`, or a bare name string; a
name may carry the ORCID as a trailing bracket, `'Doe, Jane
[0000-0002-1825-0097]'`). Replaces the whole byline (not additive). Exports
render the ORCID as a linked iD mark after the name.

**`put`** creates: a new draft, a chunk (`at=` places it —
`{'first'\|'last': True}`, `{'into': 'dc<id>'}`, `{'before'\|'after':
'dc<id>'}`), or a fork — see `precis-draft-help` *Create a draft*.


## See also

- [[precis-draft-help]] — author a living document: create, search, edit, review, audit.
- [[precis-draft-cite-help]] — citations and cross-references in draft prose.
- [[precis-draft-rich-content-help]] — figures, images, and data tables.
- [[precis-draft-export-help]] — LaTeX/PDF/Word/reMarkable export.
- [[precis-edit-help]] — the generic edit verb across kinds.

---
id: precis-draft-help
title: precis — the editable document kind
summary: author a living document as chunks — create, read (outline/verbatim), edit text, reorder/reparent, soft-delete; markdown-ish prose with ¶/§/[[ ]] references
applies-to: get/search/put/edit/delete/tag/link (kind='draft')
status: active
---

# precis-draft-help — author a living document

A `draft` is an **editable, chunk-native document** — the living
source of a project's write-up. Postgres is canonical; it exports to
LaTeX/PDF/Word. Unlike a `paper` (frozen), a draft's chunks are mutable
in structure (reorder/reparent) and in text. **One draft per project**;
a snapshot/backup is a *freeze* (see below).

Everything goes through the normal seven verbs — **no new verbs**:
`put` (create / add a chunk), `edit` (change text **or** move), `get`
(outline / verbatim), `delete` (soft-retire), plus `link`/`tag`/`search`.

## Addressing — opaque handles, never numbers

Each chunk has a minted, opaque **handle** (e.g. `5BL5xQ`). The
inline/prose sigil is `¶`, so a chunk is `¶5BL5xQ`; in verbs use
`id='¶5BL5xQ'` (handles are globally unique — no draft name needed).
You **never** type or compute a handle for a *new* chunk — `put`
returns it. Numeric `~N` ordinals are **not** offered for drafts (they
rot on insert); use handles.

## Start a new draft

A draft is **born with a title heading** (so it is never empty), bound
1:1 to its project todo by a `draft-of` link. The brief lives on the
project's `meta.workspace.brief`; the draft carries `path`/`format`.

```python
# 1 — create the draft (returns the draft + its title heading ¶t0)
put(kind='draft', name='nanotrans', project='<project-todo-id>',
    title='Nanoscale Transistors',
    meta={'workspace': {'path': 'projects/nanotrans', 'format': 'tex'}})

# 2 — add a section heading after the title
put(kind='draft', ref='nanotrans', chunk_kind='heading',
    text='Introduction', at={'after': '¶t0'})       # → returns ¶k7m2aQ

# 3 — a paragraph under it
put(kind='draft', ref='nanotrans', chunk_kind='paragraph',
    text='Nanoscale transistors …', at={'into': '¶k7m2aQ', 'last': True})
```

`at` places the new chunk (all parts optional): `{'first'|'last': True}`,
`{'into': '¶<heading>'}`, `{'before'|'after': '¶<handle>'}`.

## Add prose — one paragraph per put

Write **one paragraph per `put`**. A longer `put` is split at block
boundaries (blank lines; lists/code/tables stay whole) and returns one
handle per chunk:

```python
put(kind='draft', ref='nanotrans', chunk_kind='paragraph',
    text='First para.\n\nSecond para.', at={'after': '¶k7m2aQ'})
# → returns [¶aa1, ¶aa2]
```

## Read the document

```python
get(kind='draft', id='nanotrans')          # outline: handle | §-path | gist
get(id='¶k7m2aQ')                           # one chunk, verbatim source
get(id='¶k7m2aQ-5+3')                       # that chunk + 5 before, 3 after
```

Navigate the **outline** first (cheap — one line per chunk), then pull
**verbatim** only for the region you act on. `¶<handle>-B+A` is a
reading window (B before, A after, in reading order).

## Change a chunk's text

```python
edit(id='¶k7m2aQ', text='Nanoscale transistors, defined as …')
```

In-place: the handle (and every reference to it) survives; embeddings /
keywords / gist re-derive automatically.

## Reorder / move (structure, not a new verb)

```python
edit(id='¶B', move={'before': '¶A'})                  # reorder among siblings
edit(id='¶3', move={'parent': '¶secB', 'after': '¶7'}) # move into another section
edit(id='¶x', move={'into': '¶heading', 'last': True}) # to a section's end
```

Send the *intent* with handles; the system computes the ordering and
records it. No text changes → nothing re-embeds. Moving a heading
carries its whole subtree.

## Soft-delete (retire) — `delete`, reversible

```python
delete(id='¶k7m2aQ')                       # retire a chunk (un-delete restores)
delete(id='¶secB', mode='promote')         # remove heading, keep contents (lift to parent)
delete(id='¶secB', mode='cascade')         # delete heading AND its contents
```

A **heading with children requires a `mode`** — `promote` (keep
contents) or `cascade` (delete the section) — there is no default for
that destructive choice. Retired chunks drop out of the document but
their history (and any anchor to them) survives. You **cannot delete
the last live chunk** — a draft is never empty.

## References in prose — markdown links

Prose is **markdown**; references are markdown links the renderer
resolves per target:

| write | means | renders |
|---|---|---|
| `[DuckDuckGo](https://…)` | web link | hyperlink |
| `[¶<handle>]` | cross-ref to this draft | computed §/number |
| `[§<paper>~<n>]` | **citation** to a paper chunk | `[n]` + bibliography |
| `[the prior result](¶<handle>)` | cross-ref with display text | hyperlinked text |
| `[surface words](¶<term-handle>)` | glossary term | first-use / abbreviation |
| `[[memory:<id>]]` | **authoring** link (any thought) | nothing (provenance only) |

Cite the **exact** paper chunk that holds the detail (`[§miller89~4]`),
not the whole paper. **Thoughts** (memory / think / finding) are
referenceable but **not citeable** — they get a `[[…]]` link only,
never a bibliography entry. Math is `$…$` / `$$…$$` (LaTeX, rendered by
KaTeX on the web).

Bare `kind:ref` mentions (`paper:miller89~4`, `memory:6184`) are
recognised too — the bracket forms are the *superset* over the same
grammar notes use. **Every** reference you write auto-materialises a
`related-to` backlink (the same shared autolinker), so the draft is
discoverable from the cited paper/thought's side; remove a reference and
its link drops on the next edit. Intra-draft `¶` cross-refs are
document-internal (TOC / `\ref`), not graph edges.

## Steer the draft — brief + change requests (don't hand-edit prose)

You usually don't rewrite prose directly; you **steer**:

```python
edit(id='nanotrans', meta={'workspace': {'brief': '…updated brief…'}})
put(kind='todo', parent_id='<project>', text='tighten this paragraph',
    meta={'anchor': '¶k7m2aQ'}, ...)        # a change request, anchored
link(src='¶k7m2aQ', rel='derived-from', dst='memory:7x2')  # provenance
```

A change-request `todo` anchored to a handle flows through the normal
todo tree → dispatch → jobs; the executor decides whether to do it in
one job or fan out per section.

## Freeze / snapshot (release + backup)

A *freeze* copies the draft's current chunks into an immutable
`paper`-like ref (versioned, searchable, citable), linked `snapshot-of`
the draft. The draft keeps evolving. (Operational verb TBD; see
ADR 0033.)

## See also

`precis-draft-prose`, `precis-draft-structure`, `precis-draft-citation`,
`precis-draft-glossary`, `precis-draft-math`, `precis-draft-export`.
Design: ADR 0033.

---
id: precis-addressing-help
title: precis — universal handles (the one address scheme)
summary: the type-prefixed handle (2-char code + decimal id), the relative grammar, the 2-char type codes, address-vs-metadata
applies-to: get / edit / delete / tag / link (any verb that addresses an existing ref)
status: stable
---

# precis-addressing-help — one handle for every ref and chunk

> **ADR 0036.** A handle is the single address form for every record and
> addressable chunk. Legacy forms still resolve on **input** — paper slugs
> `miller23` (slug-keyed kinds), bare numeric ids `158` **only for int-keyed
> kinds** (memory/todo/job/…, *not* papers), `kind:slug~pos`, draft `¶<h>` — so
> nothing you already know breaks, but **output now shows handles**, and a
> handle is the thing to copy back into `get` / `link` / `like` /
> `source_handle`. Authoritative source of the codes:
> `src/precis/utils/handle_registry.py`.

## What a handle is

A **handle** is `<2-char type code><decimal id>` — the row's primary key with a
type prefix. Bare ASCII, no separators, variable length, self-delimiting (letters
= type, digits = id):

```
pa5     a paper (ref_id 5)        pc10    a paper chunk (chunk_id 10)
me42    a memory                  td158   a todo
gr7     a gripe                   jo101   a job
```

**`pa5` is the address; `5` alone is not.** Never strip the 2-char prefix, and
never type a bare number where a handle belongs — a bare `5` for a paper is read
as a cite_key and fails (and a bare number pasted into cited text is not a
citation at all). Copy the handle verbatim from output; don't reconstruct it.
`pa<id>` addresses the paper *record*; `pc<id>` addresses one *chunk* of it — to
cite evidence or link to a paper, use `pa<id>` (or a `pc<id>` for the exact
chunk), never a bare id.

## Merged / superseded refs redirect (you don't need to chase them)

When two refs are found to be duplicates (paper dedup) or a memory is
consolidated, the loser is soft-deleted and stamped `meta.superseded_by =
<survivor>`. Its handle still works: `get(id='pa36264')` or `link=` on a merged
handle **transparently resolves to the survivor** and returns a note like
*"pa36264 was merged into pa44457 — please use that handle going forward."*
Nothing breaks, but take the hint: update your stored reference to the survivor
handle so you stop carrying the stale one. This is one universal behavior across
every kind — there is no per-kind variant to learn.

- **The type code tells you what it is** — `pa…` → a paper. So
  `get(id='pa5')` needs no `kind=`; the prefix infers it. (`kind=` stays
  required for `put` / `search`, which name a *class*, not a record.)
- **Computed, not stored.** A handle is a pure function of `(kind, id)` — there
  is no separate "handle" to mint or look up. `pc10` *is* chunk 10.
- **Flat & stable identity.** The handle names the row, not a position; unlike
  the retired `miller23~4` it does not rot when a doc is re-chunked (the
  chunk keeps its `chunk_id`).
- **Write it inline to reference.** Inside a memory or draft body, a bare
  `[handle]` in the prose is a reference — it auto-links the writing ref to
  the target (`related-to`). See `precis-memory-help` / `precis-draft-help`.

## Relative grammar (navigation sugar — never stored)

Off a stable handle anchor; resolves against *current* structure, yields another
handle. Use for reading / navigation; the durable reference is always the bare
handle.

```
pc10          this chunk
pc10+1 / -1   next / previous sibling  (on a flat paper = next/prev block)
pc10+3        three siblings forward
pc10-2..3     signed sibling span: 2 before … 3 after (inclusive)
dc4^          parent / enclosing heading      ── hierarchical kinds only
dc4^2         two levels up                       (drafts); flat papers have
                                                  no ancestor
```

`..` present ⇒ range, absent ⇒ single step. `++`/`--`/`^^` accepted as aliases
of `+1`/`-1`/`^2`. One trailing operator (no chaining); resolve and re-address to
compose. **Sibling steps + spans** work on any chunk-bearing kind; **`^`
ancestor** needs a hierarchical document (drafts) — on a flat paper it has no
target.

## Address vs metadata

A handle is the **internal** pointer. A ref's **external** identity — DOI,
arXiv, source URL, Discord path, filesystem path — is **metadata, kept as data**
(bibliography, dedup, re-fetch, verify links), *not* the handle. They coexist:
`pa5` ↔ `doi:10.1234/…`.

## The 2-char type codes

Records (left) and their chunk code where the kind has addressable body chunks.
Mirror of `handle_registry.py` — the module is the SSOT; this table is the
agent-facing copy.

| kind | rec | chunk | | kind | rec | chunk |
|---|---|---|---|---|---|---|
| paper | `pa` | `pc` | | memory | `me` | — |
| patent | `pt` | `pk` | | oracle | `or` | — |
| news | `nw` | `nc` | | finding | `fi` | `fb` |
| draft | `dr` | `dc` | | citation | `ci` | — |
| conv | `co` | `cc` | | flashcard | `fc` | — |
| pres | `pr` | `ps` | | todo | `td` | — |
| markdown | `md` | `mc` | | job | `jo` | `jc` |
| plaintext | `pl` | `lc` | | alert | `al` | — |
| tex | `tx` | `xc` | | agentlog | `ag` | — |
| python | `py` | — | | cron | `cr` | `cp` |
| gripe | `gr` | `gc` | | message | `ms` | `mb` |
| skill | `sk` | — | | tag | `tg` | — |

- **`skill` / `python` / `tag`** carry codes for completeness but are still
  addressed by their slug/path or `kind=`+id — a bare `sk…`/`py…`/`tg…` is not
  yet a resolvable handle. Skills: `get(kind='skill', id='precis-tasks-help')`.
- **`draft`** chunks currently keep their ADR-0033 `¶<handle>` form; the `dr`/`dc`
  codes are reserved for a later unification.
- **Providers** (`web`, `youtube`, `wikipedia`, `semanticscholar`, `websearch`,
  `perplexity-*`) and **stateless tools** (`calc`, `math`, `provenance`,
  `random`) have **no handle** — addressed by URL / query / compute.

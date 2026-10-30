---
id: precis-addressing-help
family: core
title: precis — universal handles (the one address scheme)
summary: the type-prefixed handle (2-char code + decimal id), the relative grammar, the 2-char type codes, address-vs-metadata
answers:
  - what does a precis handle like pa5 or pc40 mean?
  - how do I move to the next sibling or the parent of a chunk without knowing its handle?
  - what are the 2-char type-code prefixes for each kind?
  - a ref got merged or superseded — do I need to update the handle I saved?
applies-to: get / edit / delete / tag / link (any verb that addresses an existing ref)
status: stable
tags: [addressing, orientation]
---

# precis-addressing-help — one handle for every ref and chunk

> **ADR 0036.** A handle is the single address form for every record and
> addressable chunk. It is the thing to copy back into `get` / `link` / `like` /
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
handle **transparently resolves to the survivor**. Write calls (`link`, `edit`,
`put`, `tag`) return a hint-footer receipt once per request:
`[info] resolved pa36264 -> pa44457 (superseded)`.
Reads retain the ordinary cooled-down footer note:
`[info] pa36264 was merged into pa44457 — please use that handle going forward.`
The payload stays first, including machine views. `delete` on a superseded
handle refuses and names the survivor's explicit delete call; it never retires
the survivor by following the stale address.
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

- **Read once, write by handle.** After a successful exact-handle `get` in the
  same MCP session, `put`/`edit` accepts `text={'$ref': 'dc123'}`. The server
  writes the handle's current stored text and acknowledges length + SHA-256,
  without echoing it. Chunk handles expand to one chunk; record handles join
  live body chunks with a blank line. Missing/cross-session reads are `Denied`.

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
| conv | `co` | `cc` | | anki | `ak` | — |
| pres | `pr` | `ps` | | todo | `td` | — |
| markdown | `md` | `mc` | | job | `jo` | `jc` |
| plaintext | `pl` | `lc` | | alert | `al` | — |
| tex | `tx` | `xc` | | agentlog | `ag` | — |
| python | `py` | — | | message | `ms` | `mb` |
| gripe | `gr` | `gc` | | tag | `tg` | — |
| skill | `sk` | — | | alias | `as` | — |

- **`skill`** anchors round-trip as `sk<ref_id>` in `get` and `link`; the
  canonical `skill:<slug>` form also works. **`python` / `tag`** keep their
  slug/path or `kind=`+id addressing — bare `py…`/`tg…` is not resolvable.
- **`draft`** refs are `dr<id>` and their chunks `dc<id>` — the live, current
  forms (every `put(kind='draft', …)` returns `dc`-form handles). The
  ADR-0033 legacy `¶<base58>` form still *resolves* as an accepted alias
  during the transition, but write new references in `dc` form.
- **Providers** (`web`, `youtube`, `wikipedia`, `semanticscholar`, `websearch`,
  `perplexity-*`) and **stateless tools** (`calc`, `math`, `provenance`,
  `random`) have **no handle** — addressed by URL / query / compute.
- **A string with no handle** (a long URL, an external id, a path you keep
  re-sending) can get a durable short name through `kind='alias'`:
  `put(kind='alias', args={'a': 'crossref-x', 'b': '<url>', 'rel':
  'short-for'})`, then `get(kind='alias', id='crossref-x')` returns the URL
  and `get(kind='alias', id='<url>')` returns the short name. See
  `precis-alias-help`.

## The `[[…]]` wikilink — skill-to-skill links

Inside a skill's markdown body, `[[precis-overview]]` (double brackets
around a skill slug) is a lateral cross-reference to another skill —
the graph-derived successor to a prose `## See also` list.
It renders as
`get(kind='skill', id='precis-overview')` in a full-skill or `~N`
section serve, and a raw `[[precis-overview]]` in a search snippet
means the same thing. The target must resolve — either a real file
slug or a synthesised meta-skill (`precis-help`/`precis-status`/
`precis-toc`/`toc`) — a dangling link is a static ingest-gate finding
(see `precis.ingest.skill_ingest`).

## Skill ids can name a worktree root — `wt-<tree>/<slug>`

When the server has `PRECIS_PYTHON_WORKTREES` (the spec is shared with
the `python`/`md` kinds), prefixing a skill id with a git-worktree alias
serves that checkout's corpus instead of the shipped one:

```python
get(kind="skill", id="wt-<tree>")                      # list the tree's skill slugs
get(kind="skill", id="wt-<tree>/precis-foo-help")      # body
get(kind="skill", id="wt-<tree>/precis-foo-help/toc")  # toc
get(kind="skill", id="wt-<tree>/precis-foo-help~3")    # one block
```

This is the preview lane for an unlanded skill: the shipped
`get(kind='skill', id='<slug>')` only shows the file once it
is merged, released and deployed. Worktree serves are read-only and
never enter the search fan-out. If the alias does not resolve, fall
back to reading the file directly.

## Skill revisions — `have=`

Every whole-skill serve ends with one line, `skill rev <sha8>`. To
re-read a skill whose text is unchanged, pass it back:
`get(kind='skill', id='<slug>', args={'have': '<sha8>'})` returns a
short stub (section list) instead of the body. Pass `have=<rev>` only if
that skill's full text is still in your context; if unsure, omit it. A
stale or malformed `have=` just returns the full body, and
`full=true` always forces the full body.
